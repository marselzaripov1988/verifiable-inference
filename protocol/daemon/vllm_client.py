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
(--no-enable-prefix-caching) — из клиента это не выключается, и проверить
это клиент тоже не может. Поэтому у исполнителя есть self_check().
"""

import json
import urllib.error
import urllib.request

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


def self_check(base, model, prompt="Назови пять простых чисел.", max_tokens=64):
    """Проверка самого узла перед работой: один и тот же запрос дважды
    обязан дать побитово одинаковый ответ.

    ЭТА ПРОВЕРКА НЕГОДНА ПОД НАГРУЗКОЙ, и это замерено 18 октября 2026 на
    суточном прогоне: два порождения подряд совпали лишь **44 раза из
    96**. Состав батча меняет арифметику, и чем больше узел занят, тем
    чаще он «не повторяет сам себя». То есть демон отказывается работать
    именно тогда, когда узел зарабатывает.

    Оценка готового текста под той же нагрузкой устойчива: побитово 861
    из 1000, а с точностью до 7 микродолей — 979 из 1000; существенных
    расхождений 21 из 1000, худшее 23 020, и ни одно не достигло порога
    36 000. Значит сторожить надо повторяемость ОЦЕНКИ, да ещё и с
    допуском, а не побитовое равенство порождений.

    Почему не переписано сразу: эта проверка сторожит ещё и выборку
    (temperature > 0), а оценка готового текста выборки не касается и
    её бы не поймала. Замена требует решения, что именно проверяется, а
    не правки на ходу. Пока — предупреждение здесь и числа в
    results/soak-24h/.
    """
    a, _ = generate(base, model, prompt, max_tokens)
    b, _ = generate(base, model, prompt, max_tokens)
    return a == b
