#!/usr/bin/env python3
"""Чтение параметров протокола ИЗ ЦЕПОЧКИ.

Шаг 12 плана. До него порог, число проверок и связка модель/рантайм
жили константами в коде демона — то есть договорённостью между людьми.
Договорённость нельзя проверить и нельзя сменить согласованно: обновил
один исполнитель рантайм, и порог для него молча перестал быть
действительным, а сеть об этом не знает.

Теперь источник истины один — настройки контракта. Демон их читает и
обязан отказаться работать, если его собственный узел им не
соответствует.

Запрос умного контракта идёт как GET с base64 от JSON в пути — так
устроен REST Cosmos:

    /cosmwasm/wasm/v1/contract/<адрес>/smart/<base64({"config":{}})>
"""

import base64
import datetime
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass


class ChainUnavailable(Exception):
    """Параметры из цепочки получить не удалось."""


class QueryUnknown(ChainUnavailable):
    """Контракт такого запроса НЕ ЗНАЕТ: его код старее кода демона.

    Отдельно от недоступности затем, что ответ разный. Недоступность —
    повод подождать; незнакомый запрос ждать бессмысленно, зато есть что
    сделать вместо него. Так демон работает и с перенесённым контрактом,
    и с ещё не перенесённым, не требуя от оператора знать, какой перед
    ним.
    """


@dataclass(frozen=True)
class Params:
    """Параметры протокола. Единственный источник — цепочка."""
    checks_required: int
    pinned_model: str
    pinned_runtime: str
    pinned_dtype: str
    #: Остальная часть класса. Вердикт внутри класса выносится равенством
    #: хэшей, а не порогом, и без общего кеша ядер узлы класса считают
    #: по-разному в последнем бите.
    pinned_arch: str
    kernel_cache_hash: str
    #: Чем идёт расчёт: {"native": "uaxm"} либо {"cw20": "axm1…"}.
    #: Вариантом, а не строкой: контракт умеет оба, и демону надо знать
    #: не только имя единицы, но и КАК она платится.
    settlement: dict
    stake_denom: str
    #: Минимальный залог. Нужен петле ДО отправки: контракт проверяет залог
    #: при каждом взятии задания, и узел с недобором платил бы газ за
    #: отказ каждый цикл вместо того, чтобы сказать оператору, чего не
    #: хватает.
    min_stake: int
    #: Доля проверяющих в базисных пунктах. Нужна ЗАКАЗЧИКУ: он платит её
    #: сверх цены, и контракт требует точную сумму — `price + доля`, ни
    #: больше, ни меньше. Считать её по своему представлению о доле
    #: значило бы получать отказ за газ при каждой смене настройки.
    verifier_fee_bps: int
    # --- проба на эталонном векторе ---
    conformance_limit_micro: int
    conformance_ref_hash: str
    conformance_lens: tuple
    # --- сроки заданий ---
    #: Сколько секунд исполнитель вправе держать взятое задание и сколько
    #: собираются проверки. Демоны их УЧИТЫВАЮТ: исполнитель отказывается
    #: начинать работу по заданию с вышедшим сроком (инференс оплачен, а
    #: задание отберут), проверяющий предупреждает о риске, а
    #: deadlines.py печатает сообщения, которые пора отправить.
    claim_ttl_seconds: int
    check_ttl_seconds: int
    conformance_version: int


def settlement_label(settlement: dict) -> str:
    """Имя расчётной единицы для вывода. Неизвестный вариант — отказ.

    Умолчание здесь означало бы, что демон молча считает расчёт чем-то
    своим, а узнается это на выплате.
    """
    if "native" in settlement:
        return settlement["native"]
    if "cw20" in settlement:
        return f"cw20:{settlement['cw20']}"
    raise ChainUnavailable(f"неизвестный вид расчёта: {settlement!r}")


def query_smart(rest, contract, query, timeout=20):
    q = base64.b64encode(json.dumps(query).encode()).decode()
    url = f"{rest.rstrip('/')}/cosmwasm/wasm/v1/contract/{contract}/smart/{q}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())["data"]
    except urllib.error.HTTPError as e:
        # ТЕЛО ошибки обязательно: в нём единственное место, где написано,
        # ЧТО именно не так. Без него «HTTP Error 500» одинаково выглядит у
        # несуществующего задания и у упавшего узла, а петле это надо
        # различать — см. job_or_none.
        try:
            body = e.read().decode(errors="replace")[:400]
        except Exception:
            body = ""
        raise ChainUnavailable(f"{url}: {e}: {body}") from e
    except Exception as e:
        raise ChainUnavailable(f"{url}: {e}") from e


def load_params(rest, contract):
    """Прочитать настройки контракта.

    Отсутствие любого поля — отказ, а не значение по умолчанию: умолчание
    здесь означало бы, что демон работает по своему представлению о
    протоколе вместо того, которое записано в цепочке.
    """
    d = query_smart(rest, contract, {"config": {}})
    need = ("checks_required",
            "pinned_model", "pinned_runtime", "pinned_dtype",
            "pinned_arch", "kernel_cache_hash",
            "settlement", "stake_denom", "min_stake", "verifier_fee_bps",
            "conformance_limit_micro", "conformance_ref_hash",
            "conformance_lens", "conformance_version",
            "claim_ttl_seconds", "check_ttl_seconds")
    miss = [k for k in need if k not in d]
    if miss:
        raise ChainUnavailable(f"в настройках нет полей: {miss}")
    return Params(
        checks_required=int(d["checks_required"]),
        pinned_model=d["pinned_model"],
        pinned_runtime=d["pinned_runtime"],
        pinned_dtype=d["pinned_dtype"],
        pinned_arch=d["pinned_arch"],
        kernel_cache_hash=d["kernel_cache_hash"],
        settlement=d["settlement"],
        stake_denom=d["stake_denom"],
        min_stake=int(d["min_stake"]),
        verifier_fee_bps=int(d["verifier_fee_bps"]),
        conformance_limit_micro=int(d["conformance_limit_micro"]),
        conformance_ref_hash=d["conformance_ref_hash"],
        conformance_lens=tuple(int(x) for x in d["conformance_lens"]),
        claim_ttl_seconds=int(d["claim_ttl_seconds"]),
        check_ttl_seconds=int(d["check_ttl_seconds"]),
        conformance_version=int(d["conformance_version"]),
    )


def load_executor(rest, contract, address):
    """Запись об исполнителе из цепочки. None — не зарегистрирован."""
    try:
        return query_smart(rest, contract, {"executor": {"address": address}})
    except ChainUnavailable:
        return None


def conformance_problems(params, record):
    """Что не так с пробой узла по данным ЦЕПОЧКИ.

    Узнать о просроченной пробе до отправки сделки, а не из её отказа:
    сделка стоит газ, а причина отказа из него не видна.
    """
    if record is None:
        return ["узел не зарегистрирован в контракте"]
    c = record.get("conformance")
    if not c:
        return ["проба на эталонном векторе не пройдена"]
    have = int(c.get("ref_version", 0))
    if have != params.conformance_version:
        return [f"проба снята против эталона версии {have}, "
                f"сейчас {params.conformance_version} — нужна заново"]
    return []


def fee_for(price, bps):
    """Доля проверяющих, которую заказчик платит СВЕРХУ цены.

    Деление с округлением ВВЕРХ — ровно как в контракте
    (`fee_for` в contract.rs). Разойдись округление на единицу, и контракт
    отвергнет сумму целиком: он требует `price + доля` точно, а не
    «примерно». Поэтому считается здесь один раз и этим же кодом для
    родного расчёта и для CW20.
    """
    price, bps = int(price), int(bps)
    return (price * bps + 9_999) // 10_000


def load_job(rest, contract, job_id):
    """Задание из цепочки. Нужно затем, что в нём лежат ХЭШИ текста."""
    return query_smart(rest, contract, {"job": {"job_id": int(job_id)}})


#: Чем «задания с таким номером нет» отличается от «цепочка не ответила».
#:
#: Различать обязательно, и это не придирка. Запрос списка (`Jobs`,
#: `NextJob`) появился у контракта 27 октября, но код до переноса его не
#: знает — а перенос трёх живых адресов дело не одной минуты. Пока он не
#: сделан, петля ищет задания перебором номеров и останавливается на
#: отсутствующем. Если считать отсутствием любую ошибку, то недоступный
#: REST выглядел бы концом списка — петля решила бы, что заданий нет, и
#: тихо простояла бы всю неисправность.
#:
#: Поэтому отсутствие опознаётся по тексту `cosmwasm`, а всё остальное
#: остаётся недоступностью. Закрываемся в сторону осторожности: непонятный
#: ответ — недоступность, то есть петля ждёт, а не делает выводы.
JOB_MISSING = re.compile(
    r"(state::Job|type:\s*\w+::state::Job|Job\s+not\s+found)", re.I)


def job_or_none(rest, contract, job_id):
    """Задание либо None, если задания с таким номером НЕТ.

    Недоступность цепочки пробрасывается как есть — см. JOB_MISSING о том,
    почему эти два случая нельзя смешивать.
    """
    try:
        return load_job(rest, contract, job_id)
    except ChainUnavailable as e:
        s = str(e)
        if "not found" in s.lower() and JOB_MISSING.search(s):
            return None
        raise


#: Чем «такого запроса в контракте нет» отличается от всего остального.
#:
#: Так выглядит отказ разбора сообщения в cosmwasm: запрос приходит JSON-ом,
#: и незнакомый вариант отвергается serde ещё до кода контракта. Текст
#: похож на «Error parsing into type …QueryMsg: unknown variant `jobs`».
UNKNOWN_QUERY = re.compile(
    r"unknown variant|unknown field|Error parsing into type", re.I)


def jobs_page(rest, contract, start_after=None, limit=None):
    """Страница списка заданий. Возвращает (записи, граница).

    Граница — номер, который получит следующее задание, то есть конец
    списка. Приходит тем же ответом нарочно: иначе каждый цикл узла стоил
    бы двух запросов вместо одного.

    `QueryUnknown` — контракт запроса не знает (код до переноса). Это не
    поломка: вызывающий переходит на перебор номеров.
    """
    q = {"jobs": {}}
    if start_after is not None:
        q["jobs"]["start_after"] = int(start_after)
    if limit is not None:
        q["jobs"]["limit"] = int(limit)
    try:
        d = query_smart(rest, contract, q)
    except ChainUnavailable as e:
        if UNKNOWN_QUERY.search(str(e)):
            raise QueryUnknown(str(e)) from e
        raise
    if "next_job" not in d:
        raise ChainUnavailable(f"в ответе списка нет границы: {d!r}")
    return list(d.get("jobs") or []), int(d["next_job"])


def next_job(rest, contract):
    """Номер следующего задания, то есть граница списка."""
    try:
        d = query_smart(rest, contract, {"next_job": {}})
    except ChainUnavailable as e:
        if UNKNOWN_QUERY.search(str(e)):
            raise QueryUnknown(str(e)) from e
        raise
    return int(d["next_job"])


def endpoint_of(rest, contract, address):
    """Адрес, по которому узел отдаёт связки. None — не объявлен.

    Это адрес, а не доверие: что по нему лежит, решает сверка с хэшами
    задания. Поэтому брать связку можно и не отсюда, а отсюда — в первую
    очередь.
    """
    rec = load_executor(rest, contract, address)
    if not rec:
        return None
    e = rec.get("endpoint")
    return e or None


def text_hash(text):
    """Хэш текста — ровно так же, как считает исполнитель перед сдачей."""
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()


def job_problems(job, prompt, answer):
    """Сверить ПОЛУЧЕННЫЙ текст с обязательствами задания в цепочке.

    Зачем это обязательно. Проверяющий получает запрос и ответ вне
    цепочки — в цепочке только их хэши. Если текст подменён, проверяющий
    оценит не то, его вектор не совпадёт с вектором исполнителя, и при
    правиле «вердикт по равенству хэшей» это обвинит ЧЕСТНОГО. Причём
    тихо: расхождение выглядело бы как подлог.

    Подменить текст может кто угодно на пути доставки, и двум
    проверяющим можно подсунуть одну и ту же подмену — тогда не спасёт и
    требование «не совпало у всех».

    Сверка же делает доставку недоверенной: текст можно брать откуда
    угодно, в том числе у самого исполнителя, потому что подмена видна.
    Остаётся вопрос живости — если текст не отдаёт никто, задание просто
    не проверить, — но это не вопрос правильности.

    Возвращает список расхождений; пустой список — текст тот.
    """
    bad = []
    want_p = job.get("prompt_hash")
    want_a = job.get("answer_hash")
    if not want_p:
        bad.append("в задании нет хэша запроса: сверять нечем")
    elif text_hash(prompt) != want_p:
        bad.append(f"запрос не тот: цепочка ждёт {want_p}, "
                   f"у текста {text_hash(prompt)}")
    if not want_a:
        bad.append("в задании нет хэша ответа: исполнитель ещё не сдал")
    elif text_hash(answer) != want_a:
        bad.append(f"ответ не тот: цепочка ждёт {want_a}, "
                   f"у текста {text_hash(answer)}")
    return bad


def check_node_matches(params, actual_model, actual_runtime, actual_dtype,
                       actual_arch=None, actual_cache_hash=None):
    """Сверка ИМЁН класса с тем, что записано в цепочке.

    Этого НЕДОСТАТОЧНО, и это замерено: на одной карте, с той же моделью,
    той же версией и тем же dtype, --enforce-eager против пути по
    умолчанию не дал ни одного побитового совпадения. Все три имени при
    этом совпадают. Поэтому после сверки имён узел обязан пройти пробу на
    эталонном векторе (conformance.py), а её допуск нулевой.

    Архитектура и хэш кеша ядер сверяются здесь же, если переданы. Они
    такая же часть класса: при другом кеше честные узлы расходятся в
    последнем бите, и равенство хэшей — то есть вердикт — становится
    невозможным. Ложь о хэше ловится всё той же пробой.

    Возвращает список расхождений. Пустой список — имена сошлись.
    Решение отказаться принимает вызывающий, но список он получает
    полный: знать надо обо всех расхождениях, а не о первом.
    """
    bad = []
    pairs = [
        ("модель", params.pinned_model, actual_model),
        ("рантайм", params.pinned_runtime, actual_runtime),
        ("dtype", params.pinned_dtype, actual_dtype),
    ]
    if actual_arch is not None:
        pairs.append(("архитектура", params.pinned_arch, actual_arch))
    if actual_cache_hash is not None:
        pairs.append(("кеш ядер", params.kernel_cache_hash, actual_cache_hash))
    for what, want, got in pairs:
        if want != got:
            bad.append(f"{what}: цепочка ждёт {want!r}, на узле {got!r}")
    return bad


def cache_hash(path):
    """sha256 раздаваемого архива кеша ядер.

    Сверяется именно АРХИВ, а не собранный самостоятельно кеш: упаковка
    воспроизводима только при одинаковом содержимом, а у двух карт,
    дающих тождественные векторы, содержимое файлов различается. Доказать
    хэшем равнозначность своей сборки нельзя — и не нужно, узел обязан
    пользоваться раздаваемым архивом.

    Сверка не формальность: при переносе архива на проверочную карту
    передача однажды оборвалась, архив вышел на 32 КБ короче, и хэш это
    поймал. Без сверки узел поднялся бы на половине кеша и выпал из
    класса, не узнав об этом.
    """
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_runtime(version):
    """Версия vLLM к виду, в котором она записана в цепочке.

    Сервер отдаёт «0.31.0», в настройках лежит «vllm-0.31.0». Приведение
    в одном месте: если каждый демон будет приводить по-своему, они
    разойдутся в том, что считать совпадением.
    """
    v = version.strip()
    return v if v.startswith("vllm-") else f"vllm-{v}"


# --- сроки заданий --------------------------------------------------------
#
# Сроки появились в контракте 26 октября: взятое и брошенное задание
# теперь отбирается, а сданный ответ без собранных проверок закрывается
# возвратом заказчику. Демонам это надо УЧИТЫВАТЬ, иначе узел оплачивает
# инференс по заданию, которое у него вот-вот отберут.

#: Состояния задания, у которых есть срок, и из чего он считается.
PHASE_DEADLINE = {
    "claimed": ("claimed_at", "claim_ttl_seconds"),
    "answered": ("answered_at", "check_ttl_seconds"),
}


def chain_time(rest, timeout=20):
    """Время ЦЕПОЧКИ в секундах, а не местные часы.

    Местными часами считать нельзя: срок проверяет контракт по времени
    блока, и расхождение часов узла с цепочкой означало бы, что демон
    считает задание живым, когда оно уже отбираемо, или наоборот. На
    арендованной машине часы запросто уходят.
    """
    url = f"{rest.rstrip('/')}/cosmos/base/tendermint/v1beta1/blocks/latest"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            t = json.loads(r.read())["block"]["header"]["time"]
    except Exception as e:
        raise ChainUnavailable(f"{url}: {e}") from e
    # Формат RFC3339 с наносекундами: 2026-10-26T12:34:56.789012345Z.
    # Доли секунды отбрасываются — сроки измеряются секундами.
    head = t.split(".")[0].rstrip("Z")
    try:
        return int(datetime.datetime.strptime(
            head, "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=datetime.timezone.utc).timestamp())
    except ValueError as e:
        raise ChainUnavailable(f"время блока не разобрано: {t!r}: {e}") from e


def deadline_of(job, params, now):
    """Срок задания: когда истекает и сколько осталось.

    Возвращает dict с полями `phase`, `deadline`, `left`, `expired`. Для
    состояний без срока (`open`, `settled`) — `phase` и None в остальном:
    невзятое задание никто не отбирает, закрытое уже закрыто.

    Отсутствие отметки времени считается «срок давно вышел» — так же,
    как в контракте: такие записи сделаны до появления сроков, и
    застряли именно они.
    """
    phase = str(job.get("state", "")).lower()
    pair = PHASE_DEADLINE.get(phase)
    if pair is None:
        return {"phase": phase, "deadline": None, "left": None, "expired": False}
    at_field, ttl_field = pair
    ttl = int(getattr(params, ttl_field))
    at = job.get(at_field)
    at = int(at) if at is not None else 0
    deadline = at + ttl
    return {
        "phase": phase,
        "deadline": deadline,
        "left": deadline - int(now),
        # Строго больше, как в контракте: ровно на границе срок ЕЩЁ не
        # вышел, и задание не отобрать.
        "expired": int(now) > deadline,
    }


def due_message(job_id, job, params, now, checks=None):
    """Сообщение, которое по этому заданию УЖЕ пора отправить.

    Нужно затем, что сроки в контракте никто не исполняет сам: и
    `release_job`, и `expire_checks` вызывает кто угодно, но вызвать их
    должен кто-то. Пока этого не делает ни один узел, механизм записан,
    но не работает.

    `checks` — сколько проверок уже сдано; без него `expire_checks` не
    предлагается, потому что при собранном кворуме закрывать надо
    вердиктом, и контракт такой вызов отвергнет.
    """
    d = deadline_of(job, params, now)
    if not d["expired"]:
        return None
    if d["phase"] == "claimed":
        return {"release_job": {"job_id": int(job_id)}}
    if d["phase"] == "answered":
        if checks is None or int(checks) >= int(params.checks_required):
            return None
        return {"expire_checks": {"job_id": int(job_id)}}
    return None


def load_checks(rest, contract, job_id):
    """Сколько проверок сдано по заданию."""
    return len(checks_of(rest, contract, job_id))


def checks_of(rest, contract, job_id):
    """Сами проверки, с адресами сдавших.

    Адреса нужны петле: вторую проверку на одно задание контракт
    отвергает (`DuplicateCheck`), а узел после перезапуска не помнит, что
    уже сдавал. Спрашивать цепочку дешевле, чем хранить своё — и
    правдивее: своё может разойтись с её состоянием.
    """
    d = query_smart(rest, contract, {"checks": {"job_id": int(job_id)}})
    return list(d.get("checks") or [])
