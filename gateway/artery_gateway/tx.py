"""Памятка (memo) из транзакции Cosmos, без зависимостей.

Зачем вообще. Платёж приходит на один адрес казны от множества
плательщиков. Зачислять по адресу отправителя нельзя: адрес — величина
публичная, и тогда любой, кто его знает, тратил бы чужой остаток.
Поэтому клиент получает код счёта и кладёт его в памятку перевода, а
шлюз сопоставляет платёж со счётом по ней. Так работает любой обменник,
и по той же причине.

Почему разбор свой. Публичного REST у Artery нет — только RPC
Tendermint, а он отдаёт транзакцию байтами. Декодировать нужно два поля
из двух сообщений, и тащить protobuf-зависимость ради этого незачем:

    TxRaw  { 1: body_bytes (bytes), 2: auth_info_bytes, 3: signatures }
    TxBody { 1: messages (repeated Any), 2: memo (string), ... }

Разбор НЕ бросает исключений на непонятных данных и возвращает пустую
памятку. Это осознанно: нечитаемая памятка значит «платёж не опознан» и
уходит в неразобранные, а не теряется и не зачисляется наугад. Деньги
при этом видны — сумма берётся из события, не отсюда.
"""

from __future__ import annotations

_WIRE_VARINT = 0
_WIRE_64 = 1
_WIRE_BYTES = 2
_WIRE_32 = 5


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    """Число varint, начиная с i. Возвращает (значение, новый i)."""
    shift = 0
    val = 0
    while True:
        if i >= len(buf) or shift > 63:
            raise ValueError("varint оборван")
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7


def _fields(buf: bytes):
    """Поля сообщения: (номер, тип провода, значение-байты или число).

    Разбирает ровно столько, сколько нужно, чтобы пропустить чужие поля:
    без этого нельзя добраться до нужного, потому что порядок полей в
    protobuf не гарантирован.
    """
    i = 0
    while i < len(buf):
        tag, i = _varint(buf, i)
        num, wire = tag >> 3, tag & 7
        if wire == _WIRE_VARINT:
            val, i = _varint(buf, i)
            yield num, wire, val
        elif wire == _WIRE_BYTES:
            ln, i = _varint(buf, i)
            if i + ln > len(buf):
                raise ValueError("поле длиннее сообщения")
            yield num, wire, buf[i:i + ln]
            i += ln
        elif wire == _WIRE_64:
            yield num, wire, buf[i:i + 8]
            i += 8
        elif wire == _WIRE_32:
            yield num, wire, buf[i:i + 4]
            i += 4
        else:
            raise ValueError(f"неизвестный тип провода {wire}")


def _first_bytes(buf: bytes, want: int) -> bytes | None:
    for num, wire, val in _fields(buf):
        if num == want and wire == _WIRE_BYTES:
            return val
    return None


def memo(tx_bytes: bytes) -> str:
    """Памятка транзакции. Пустая строка, если её нет или не разобрать."""
    try:
        body = _first_bytes(tx_bytes, 1)
        if body is None:
            return ""
        raw = _first_bytes(body, 2)
        if raw is None:
            return ""
        return raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""
