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
import json
import urllib.request
from dataclasses import dataclass


class ChainUnavailable(Exception):
    """Параметры из цепочки получить не удалось."""


@dataclass(frozen=True)
class Params:
    """Параметры протокола. Единственный источник — цепочка."""
    threshold_micro: int
    checks_required: int
    min_answer_tokens: int
    pinned_model: str
    pinned_runtime: str
    pinned_dtype: str
    settlement_denom: str
    stake_denom: str
    # --- проба на эталонном векторе ---
    conformance_limit_micro: int
    conformance_ref_hash: str
    conformance_lens: tuple
    conformance_version: int


def query_smart(rest, contract, query, timeout=20):
    q = base64.b64encode(json.dumps(query).encode()).decode()
    url = f"{rest.rstrip('/')}/cosmwasm/wasm/v1/contract/{contract}/smart/{q}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())["data"]
    except Exception as e:
        raise ChainUnavailable(f"{url}: {e}") from e


def load_params(rest, contract):
    """Прочитать настройки контракта.

    Отсутствие любого поля — отказ, а не значение по умолчанию: умолчание
    здесь означало бы, что демон работает по своему представлению о
    протоколе вместо того, которое записано в цепочке.
    """
    d = query_smart(rest, contract, {"config": {}})
    need = ("threshold_micro", "checks_required", "min_answer_tokens",
            "pinned_model", "pinned_runtime", "pinned_dtype",
            "settlement_denom", "stake_denom",
            "conformance_limit_micro", "conformance_ref_hash",
            "conformance_lens", "conformance_version")
    miss = [k for k in need if k not in d]
    if miss:
        raise ChainUnavailable(f"в настройках нет полей: {miss}")
    return Params(
        threshold_micro=int(d["threshold_micro"]),
        checks_required=int(d["checks_required"]),
        min_answer_tokens=int(d["min_answer_tokens"]),
        pinned_model=d["pinned_model"],
        pinned_runtime=d["pinned_runtime"],
        pinned_dtype=d["pinned_dtype"],
        settlement_denom=d["settlement_denom"],
        stake_denom=d["stake_denom"],
        conformance_limit_micro=int(d["conformance_limit_micro"]),
        conformance_ref_hash=d["conformance_ref_hash"],
        conformance_lens=tuple(int(x) for x in d["conformance_lens"]),
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


def check_node_matches(params, actual_model, actual_runtime, actual_dtype):
    """Сверка ИМЁН связки с тем, что записано в цепочке.

    Этого НЕДОСТАТОЧНО, и это замерено: на одной карте, с той же моделью,
    той же версией и тем же dtype, --enforce-eager против пути по
    умолчанию дал расхождение до 413 488 — внутри полосы подлога. Все три
    имени при этом совпадают. Поэтому после сверки имён узел обязан
    пройти пробу на эталонном векторе (conformance.py).

    Возвращает список расхождений. Пустой список — имена сошлись.
    Решение отказаться принимает вызывающий, но список он получает
    полный: знать надо обо всех расхождениях, а не о первом.
    """
    bad = []
    for what, want, got in (
        ("модель", params.pinned_model, actual_model),
        ("рантайм", params.pinned_runtime, actual_runtime),
        ("dtype", params.pinned_dtype, actual_dtype),
    ):
        if want != got:
            bad.append(f"{what}: цепочка ждёт {want!r}, на узле {got!r}")
    return bad


def normalize_runtime(version):
    """Версия vLLM к виду, в котором она записана в цепочке.

    Сервер отдаёт «0.31.0», в настройках лежит «vllm-0.31.0». Приведение
    в одном месте: если каждый демон будет приводить по-своему, они
    разойдутся в том, что считать совпадением.
    """
    v = version.strip()
    return v if v.startswith("vllm-") else f"vllm-{v}"
