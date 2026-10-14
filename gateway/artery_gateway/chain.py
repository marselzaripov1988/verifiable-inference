"""Чтение входящих платежей ARTR из блокчейна Artery.

Единственный модуль шлюза, завязанный на специфику сети. Всё остальное —
обычный HTTP-сервис, поэтому разбираться с особенностями Artery лучше здесь
и один раз.

HTTP пока на stdlib, чтобы модуль запускался без установки зависимостей.
При переходе на FastAPI слой заменяется на httpx.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Iterator

from .tx import memo as tx_memo

DENOM = "uartr"
MICRO = 1_000_000  # 1 ARTR = 1_000_000 uartr; у AXM разрядность та же

#: Тип события перевода у Artery. НЕ `transfer`.
#:
#: Свой банк Artery испускает типизированное событие, а тип такого
#: события — имя proto-сообщения (util.EmitEvent -> patchEvent ->
#: proto.MessageName). Проверено по исходникам обеих веток: на мейннете
#: (master, SDK 0.42) и на ветке переезда (SDK 0.53) событие одно и то
#: же, меняется только кодировка ключей.
#:
#: Это не придирка к имени: искать по `transfer.recipient` на Artery
#: значит получать ноль платежей и показывать «платежей нет» при
#: пришедших деньгах.
EVENT_TRANSFER = "artery.bank.v1beta1.EventTransfer"

#: Событие стандартного банка Cosmos. Нужно для девнета на обычном
#: wasmd/simd, где шлюз удобно проверять без Artery.
EVENT_TRANSFER_STOCK = "transfer"


@dataclass(frozen=True)
class Payment:
    """Входящий перевод на адрес казны."""

    tx_hash: str
    height: int
    sender: str
    #: Сумма в минимальных единицах: uartr либо uaxm, смотря чей приёмник.
    amount_micro: int
    #: Памятка транзакции: по ней платёж сопоставляется со счётом.
    #: Пустая — платёж не опознан, и он уйдёт в неразобранные, а не
    #: будет зачислен наугад по адресу отправителя.
    memo: str = ""

    @property
    def amount_whole(self) -> float:
        return self.amount_micro / MICRO


class ChainRPC:
    """Приёмник платежей. Различия сетей — в трёх полях ниже.

    Обе сети опрашиваются одинаково, через `tx_search` CometBFT, и обе
    отдают памятку байтами транзакции. Расходятся они в том, какое
    событие испускает банк и как в нём записана сумма.
    """

    #: Минимальная единица, которую приёмник считает оплатой.
    denom = DENOM
    #: Событие перевода. У Artery своё, типизированное.
    events: tuple = (EVENT_TRANSFER, EVENT_TRANSFER_STOCK)
    #: По какому ключу искать в индексе. Составной ключ — это
    #: `<тип события>.<атрибут>`, поэтому у разных банков он разный.
    search_event = EVENT_TRANSFER
    #: Префикс адресов сети. Нужен, чтобы отвергнуть адрес казны из
    #: ДРУГОЙ сети: он выглядит правильным и проходит контрольную сумму,
    #: но платежи ждали бы не там, где их переведут.
    hrp = "artr"

    def __init__(self, endpoint: str = "http://localhost:26657", timeout: float = 10.0):
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout

    #: Узел обязан знать, кто к нему пришёл.
    #:
    #: Без этого публичный RPC axiome-1 отдаёт 403: он отбивает
    #: Python-urllib по User-Agent, хотя тот же путь отвечает curl.
    #: Видно только на живом узле — подставной отдаёт что угодно.
    user_agent = "artery-gateway/0.1 (+payments watcher)"

    def _get(self, path: str, **params: str) -> dict:
        url = f"{self.endpoint}/{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={
            "User-Agent": self.user_agent, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = json.load(resp)
        if "error" in body:
            raise RuntimeError(f"RPC error: {body['error']}")
        return body["result"]

    def latest_height(self) -> int:
        return int(self._get("status")["sync_info"]["latest_block_height"])

    def scan_payments(
        self,
        treasury: str,
        from_height: int = 1,
        page_size: int = 50,
    ) -> Iterator[Payment]:
        """Все успешные переводы на `treasury`, начиная с высоты `from_height`.

        Порядок не гарантирован — Tendermint отдаёт результаты страницами
        по индексу, а не по высоте. Вызывающий код должен опираться на
        tx_hash для дедупликации, а не на порядок.
        """
        query = (f"{self.search_event}.recipient='{treasury}'"
                 f" AND tx.height>={from_height}")
        page = 1

        while True:
            result = self._get(
                "tx_search",
                query=f'"{query}"',
                prove="false",
                page=str(page),
                per_page=str(page_size),
            )
            txs = result.get("txs", [])
            if not txs:
                return

            for tx in txs:
                # Неуспешные транзакции денег не переводят.
                if tx["tx_result"].get("code", 0) != 0:
                    continue

                amount = _credited_amount(tx["tx_result"].get("events", []),
                                          treasury, self.denom, self.events)
                if amount is None:
                    continue

                sender, total = amount
                yield Payment(
                    tx_hash=tx["hash"],
                    height=int(tx["height"]),
                    sender=sender,
                    amount_micro=total,
                    memo=_memo_of(tx),
                )

            if page * page_size >= int(result.get("total_count", 0)):
                return
            page += 1


def _memo_of(tx: dict) -> str:
    """Памятка транзакции из её байтов. Пустая, если не разобрать.

    RPC отдаёт транзакцию целиком в base64, декодировать её приходится
    самим: публичного REST у Artery нет, а тащить protobuf ради двух
    полей незачем. Подробности — в tx.py.
    """
    raw = tx.get("tx") or ""
    if not raw:
        return ""
    try:
        return tx_memo(base64.b64decode(raw))
    except (ValueError, TypeError):
        return ""


# Ключи, которые есть у любого события transfer. Служат признаком того,
# что атрибуты пришли незакодированными.
_TRANSFER_KEYS = frozenset(("sender", "recipient", "amount"))


def _attributes(event: dict) -> dict[str, str]:
    """Атрибуты события словарём, независимо от версии узла.

    Tendermint 0.34 отдаёт ключи и значения атрибутов в base64, CometBFT
    (SDK 0.46 и старше) — обычными строками. Мейннет Artery пока на
    первом, сборка после миграции — на втором, и шлюзу приходится уметь
    оба: во время переезда сети будут работать одновременно.

    Кодировку определяем по ключам, а не по значениям. Значение угадать
    нельзя: короткая строка вроде "abcd" — синтаксически годный base64, и
    попытка «декодировать, если получается» портила бы настоящие данные.
    Ключи же у события transfer известны наперёд, и в base64 ни один из
    них с собой не совпадёт.
    """
    raw = {a.get("key") or "": a.get("value") or "" for a in event.get("attributes", [])}
    if raw.keys() & _TRANSFER_KEYS:
        return raw
    return {_decode(k): _decode(v) for k, v in raw.items()}


def _decode(value: str) -> str:
    """Раскодирует атрибут Tendermint 0.34; при неудаче отдаёт как есть."""
    if not value:
        return ""
    try:
        return base64.b64decode(value, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return value


class AmountUnparsed(ValueError):
    """Сумма в событии записана не так, как шлюз умеет читать.

    Это НАМЕРЕННО исключение, а не пропуск. Пропустив непонятную сумму,
    шлюз зачислил бы меньше, чем пришло, и молча — а плательщик узнал бы
    об этом по недостаче. Остановиться и закричать дешевле.
    """


def _coins(value: str) -> list[tuple[str, int]]:
    """Монеты из атрибута события, в обоих встречающихся видах.

    Artery отдаёт JSON-массив: её событие типизированное, и Coin в нём —
    структура, которую ProtoMarshalJSON печатает как объект. Стандартный
    банк Cosmos отдаёт строку `123uartr` (`Coins.String()`). Шлюзу нужны
    оба: на Artery он работает, а проверять его удобно на обычном девнете.
    """
    v = value.strip()
    if not v:
        return []
    if v.startswith("["):
        try:
            return [(c.get("denom", ""), int(c["amount"])) for c in json.loads(v)]
        except (ValueError, KeyError, TypeError) as e:
            raise AmountUnparsed(f"JSON суммы не разобран: {value!r}") from e
    out = []
    for part in v.split(","):
        m = _COIN_RE.fullmatch(part.strip())
        if not m:
            raise AmountUnparsed(f"сумма не разобрана: {value!r}")
        out.append((m.group(2), int(m.group(1))))
    return out


_COIN_RE = re.compile(r"(\d+)([a-zA-Z][a-zA-Z0-9/:._-]*)")


def _credited_amount(events: list[dict], treasury: str, denom: str = DENOM,
                     types: tuple = (EVENT_TRANSFER, EVENT_TRANSFER_STOCK),
                     ) -> tuple[str, int] | None:
    """Сумма, реально зачисленная на `treasury`, и адрес плательщика.

    Три особенности, из-за которых нельзя взять готовый парсер Cosmos:

    1. Тип события у Artery — `artery.bank.v1beta1.EventTransfer`, а не
       `transfer`: свой банк испускает типизированное событие, и его тип
       это имя proto-сообщения. Фильтр по `transfer` не нашёл бы ничего.

    2. Одна транзакция порождает несколько событий перевода — отдельно
       комиссию сети (0.3%) на служебный адрес и отдельно сам перевод.
       Считать нужно только те, где recipient совпадает с казной.

    3. Порядок атрибутов внутри события непостоянен: в одном событии это
       sender/recipient/amount, в другом amount/sender/recipient. Разбор
       строго по ключам, никакой опоры на позицию.
    """
    sender = ""
    total = 0
    found = False

    for event in events:
        if event.get("type") not in types:
            continue

        attrs = _attributes(event)

        if attrs.get("recipient") != treasury:
            continue

        for got, amount in _coins(attrs.get("amount", "")):
            if got == denom:
                total += amount
                found = True

        if attrs.get("sender"):
            sender = attrs["sender"]

    return (sender, total) if found else None


class ArteryRPC(ChainRPC):
    """Artery: своя монета, свой банк, типизированное событие."""

    denom = "uartr"
    events = (EVENT_TRANSFER, EVENT_TRANSFER_STOCK)
    search_event = EVENT_TRANSFER
    hrp = "artr"


class AxiomeRPC(ChainRPC):
    """Axiome: родная монета, стандартный банк Cosmos.

    Решено 14 октября: шлюз работает здесь. Узел не нужен ни от кого —
    публичный RPC указан в реестре цепочек Cosmos
    (https://api-chain.axiomechain.org, axiome-1, CometBFT 0.38.5), и
    `tx_search` по `transfer.recipient` на нём работает.

    Осторожно с индексатором: `api-idx.axiomechain.pro` отдаёт запросы к
    контрактам и заголовки блоков, но НИ сделок, НИ событий — платежи
    через него не увидеть. Проверено: штатные пути Cosmos там отвечают
    404, подпутей со сделками у блока нет.
    """

    denom = "uaxm"
    #: Стандартный банк: событие `transfer`, сумма строкой `123uaxm`.
    #: Проверено на живой сети, вместе с index=true у атрибутов.
    events = (EVENT_TRANSFER_STOCK,)
    search_event = EVENT_TRANSFER_STOCK
    hrp = "axm"
