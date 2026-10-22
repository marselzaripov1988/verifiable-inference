#!/usr/bin/env python3
"""Клиент vLLM. Параметры прибиты теми, при которых измерен порог.

ИМЯ ФАЙЛА ВАЖНО. Он назывался vllm.py, и это ломало сам vLLM: сервер
запускается из рабочего каталога, его подпроцесс инспекции модели делает
import vllm, и мой модуль затенял настоящий пакет. Падало с
«Model architectures ['Qwen2ForCausalLM'] failed to be inspected» —
сообщением, по которому причину не угадать. Обнаружено только на живой
машине; на подставном сервере этого не видно.

--enforce-eager ЗАПРЕЩЁН. Он избавляет от NaN (см. SCORE_ATTEMPTS), и
соблазн его поставить велик, но он же лишает узел воспроизводимости:
два прогона одних и тех же 40 текстов через перезапуск дали лишь 29
побитовых совпадений, и расхождение с самим собой доходило до 402 116
микродолей — выше порога. На скомпилированном пути по умолчанию те же
40 текстов совпали все 40. Порог для eager-узла недействителен, а
self_check этого НЕ ловит: он сравнивает два коротких порождения внутри
одного процесса, где и eager устойчив.

Отступать от них нельзя: порог 0,0360 действителен только для этой
связки. Префикс-кеш обязан быть отключён на СТОРОНЕ СЕРВЕРА
(--no-enable-prefix-caching) — из клиента это не выключается.

ПРОВЕРИТЬ ПРЕФИКС-КЕШ ПОВТОРОМ НЕЛЬЗЯ, и прежняя версия этого файла
утверждала обратное. Кеш делает повторный одинаковый запрос ПОХОЖИМ на
честный, а не отличным от него: он не пересчитывается, а читается, и два
повтора совпадают тем охотнее, чем кеш «хуже». То есть self_check() не
сторожил и не мог сторожить то, ради чего был заявлен. Чем его ловить —
не решено; нужен опыт с двумя текстами, у которых общее начало, а это
отдельный замер.

ВЫБОР ЯДРА ПО ЗАМЕРУ тоже входит в связку, и тоже ничем не закреплён.
Замерено 22 октября: при включённом выборе две пересборки ядер на ОДНОЙ
карте дают 0 побитовых совпадений из 200 и расхождение 10 789 в среднем,
а с выключенным — 200 из 200 и ровно 0. Узел обязан работать с
  --no-enable-flashinfer-autotune
  --compilation-config '{"inductor_compile_config": {"combo_kernels": false,
                          "benchmark_combo_kernel": false}}'
Проверить это из клиента тоже нельзя, и в `check_eligible` этих полей
пока нет.
"""

import json
import urllib.error
import urllib.request

import canon

#: Жадное декодирование. Любая выборка делает ответ невоспроизводимым, и
#: сравнивать вероятности станет бессмысленно.
TEMPERATURE = 0
TOP_P = 1
SEED = 42

#: Сколько раз повторять оценку. vLLM 0.31.0 изредка выдаёт в
#: логвероятностях NaN и тогда ОТКАЗЫВАЕТСЯ отдать свой же ответ:
#: «Out of range float values are not JSON compliant: nan», HTTP 400.
#: Замерено на RTX 4090: с первой попытки падает 34 текста из 40, но
#: повторы сходятся, и полученный вектор — тот самый (два прогона по 40
#: текстов через перезапуск сервера дали 40 побитовых совпадений).
#: Поэтому это помеха доступности, а не правильности, и лечится повтором.
SCORE_ATTEMPTS = 6


class ScoringFailed(Exception):
    """Сервер так и не отдал логвероятности."""


def _post(base, path, payload, timeout=600):
    req = urllib.request.Request(
        f"{base.rstrip('/')}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


class VersionUnknown(Exception):
    """Версию сервера определить не удалось."""


def server_version(base, timeout=15):
    """Какая версия vLLM РЕАЛЬНО запущена.

    Нужно затем, что при регистрации исполнитель версию только
    ЗАЯВЛЯЕТ, и до этой проверки никто не сверял заявленное с
    действительным. Порог же действителен лишь для одной версии.

    Маршрут /version лежит на корне сервера, не под /v1. Формат ответа в
    документации vLLM не описан, поэтому разбор терпимый: берём поле
    version, а при любой неожидаемости бросаем VersionUnknown — вызов
    обязан отказаться работать, а не предположить версию.
    """
    url = f"{base.rstrip('/').removesuffix('/v1')}/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            d = json.loads(r.read())
    except Exception as e:
        raise VersionUnknown(f"{url}: {e}") from e
    v = d.get("version") if isinstance(d, dict) else None
    if not isinstance(v, str) or not v:
        raise VersionUnknown(f"{url} вернул {d!r}, поля version нет")
    return v


def generate(base, model, prompt, max_tokens):
    """Породить ответ. Возвращает (текст, список токенов)."""
    r = _post(base, "/completions", {
        "model": model, "prompt": prompt, "max_tokens": max_tokens,
        "temperature": TEMPERATURE, "top_p": TOP_P, "seed": SEED,
        "logprobs": 1,
    })
    ch = r["choices"][0]
    return ch["text"], ch["logprobs"]["tokens"]


def score(base, model, text):
    """Оценить ГОТОВЫЙ текст: вернуть его вероятности, ничего не порождая.

    max_tokens=0 с echo=True — один проход на всю длину вместо сотни
    последовательных. Это и есть причина, по которой проверка дешевле
    порождения в 41 раз.

    Повторы обязательны: см. SCORE_ATTEMPTS. Без них проверяющий
    отказывался бы работать на большинстве заданий длиннее 56 токенов.
    """
    last = None
    for _ in range(SCORE_ATTEMPTS):
        try:
            r = _post(base, "/completions", {
                "model": model, "prompt": text, "max_tokens": 0,
                "echo": True, "logprobs": 1,
                "temperature": TEMPERATURE, "top_p": TOP_P,
            })
        except urllib.error.HTTPError as e:
            # Отличать «сервер подавился своим NaN» от настоящего отказа:
            # второе повторять бессмысленно.
            body = b""
            try:
                body = e.read()
            except Exception:
                pass
            if e.code == 400 and b"nan" in body.lower():
                last = e
                continue
            raise
        return r["choices"][0]["logprobs"]["token_logprobs"]
    raise ScoringFailed(
        f"логвероятности не получены за {SCORE_ATTEMPTS} попыток, "
        f"последняя ошибка: {last}")


#: Допуск повторяемости ОЦЕНКИ. Замерено 18 октября на суточном прогоне
#: под нагрузкой 16 одновременных запросов: 1000 пар оценок одного и того
#: же текста, побитово совпало 861, с точностью до 7 микродолей — 979,
#: худшее расхождение 23 020, выше порога 36 000 — ни одного. Предел
#: стоит чуть выше худшего наблюдённого: честный узел под нагрузкой
#: проходит, а узел, оценка которого вообще не повторяется, — нет.
SELF_CHECK_SCORE_LIMIT = 24000

#: Доля общего НАЧАЛА у двух порождений. Нужна против выборки
#: (temperature > 0), которую оценка готового текста не видит вовсе.
#: Побитового равенства требовать нельзя: под нагрузкой два жадных
#: порождения совпали лишь 44 раза из 96. Но расходятся они ПОЗДНО — все
#: 52 честные разошедшиеся пары совпали ровно на 56 символах из 127, то
#: есть на 44% ответа, и ни одна не разошлась раньше. Выборка расходится
#: с первых токенов. Предел 25% оставляет честному запас почти вдвое.
SELF_CHECK_MIN_PREFIX_SHARE = 0.25

SELF_CHECK_PROMPT = "Назови пять простых чисел."


def _common_prefix(a, b):
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def self_check_problems(base, model, prompt=SELF_CHECK_PROMPT,
                        max_tokens=64):
    """Чем узел негоден к работе. Пустой список — годен.

    Сторожит две вещи, каждую своим признаком.

    **Повторяемость оценки** — того самого, что протокол и сравнивает.
    С допуском, а не побитово: под нагрузкой побитово совпадает 861 пара
    из 1000, и требовать равенства значило бы отказывать честному узлу в
    14% случаев.

    **Отсутствие выборки** — по длине общего начала двух порождений, а не
    по их равенству. Прежняя версия требовала равенства и этим
    отказывалась работать именно тогда, когда узел зарабатывает: под
    нагрузкой два жадных порождения совпали 44 раза из 96.

    Чего НЕ сторожит: включённый префикс-кеш (повтором он не ловится, см.
    заголовок файла), режим eager и выбор ядра по замеру. Для них нужны
    поля в `check_eligible` и своя проба, которых пока нет.
    """
    problems = []

    try:
        a = canon.to_micro(score(base, model, prompt))
        b = canon.to_micro(score(base, model, prompt))
    except ScoringFailed as e:
        problems.append(f"оценка не получена: {e}")
    else:
        if not a or not b:
            problems.append("оценка вернула пустой вектор")
        elif len(a) != len(b):
            problems.append(
                f"две оценки одного текста дали векторы разной длины: "
                f"{len(a)} и {len(b)}")
        else:
            d = canon.divergence_micro(a, b)
            if d > SELF_CHECK_SCORE_LIMIT:
                problems.append(
                    f"оценка не повторяется: расхождение с самим собой "
                    f"{d} при пределе {SELF_CHECK_SCORE_LIMIT}")

    ga, _ = generate(base, model, prompt, max_tokens)
    gb, _ = generate(base, model, prompt, max_tokens)
    if not ga or not gb:
        problems.append("порождение вернуло пустой ответ")
    else:
        share = _common_prefix(ga, gb) / min(len(ga), len(gb))
        if share < SELF_CHECK_MIN_PREFIX_SHARE:
            problems.append(
                f"два порождения расходятся с {share:.0%} ответа при "
                f"пределе {SELF_CHECK_MIN_PREFIX_SHARE:.0%} — похоже на "
                f"включённую выборку")
    return problems


def self_check(base, model, prompt=SELF_CHECK_PROMPT, max_tokens=64):
    """Годен ли узел. Подробности — в `self_check_problems`."""
    return not self_check_problems(base, model, prompt, max_tokens)
