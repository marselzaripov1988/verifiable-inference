"""Разбор памятки из транзакции.

Транзакции собираются здесь вручную из байтов protobuf: это и есть то,
что отдаёт RPC, и разбор надо проверять против настоящего формата, а не
против своего представления о нём.

    python -m unittest discover -s gateway/tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from artery_gateway.tx import memo  # noqa: E402


def field(num: int, payload: bytes) -> bytes:
    """Поле с длиной (тип провода 2)."""
    return varint((num << 3) | 2) + varint(len(payload)) + payload


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def tx_body(memo_text: str, messages: int = 1) -> bytes:
    parts = b"".join(field(1, b"\x0a\x05hello") for _ in range(messages))
    return parts + field(2, memo_text.encode()) + varint((3 << 3) | 0) + varint(0)


def tx_raw(body: bytes) -> bytes:
    return field(1, body) + field(2, b"\x12\x34") + field(3, b"\xaa" * 64)


class Memo(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(memo(tx_raw(tx_body("ag-7f3a9c"))), "ag-7f3a9c")

    def test_unicode(self):
        self.assertEqual(memo(tx_raw(tx_body("счёт ag-01 ✓"))), "счёт ag-01 ✓")

    def test_empty_memo(self):
        self.assertEqual(memo(tx_raw(tx_body(""))), "")

    def test_fields_out_of_order_and_extra(self):
        """Порядок полей protobuf не гарантирован.

        Разбор обязан пропускать чужие поля, а не опираться на позицию:
        иначе достаточно одного нового поля в теле транзакции, чтобы
        памятка перестала читаться, а платежи — опознаваться.
        """
        body = (varint((3 << 3) | 0) + varint(12345)
                + field(2, b"ag-aabbcc")
                + field(1, b"\x0a\x05hello"))
        self.assertEqual(memo(tx_raw(body)), "ag-aabbcc")

    def test_no_body(self):
        self.assertEqual(memo(field(2, b"\x12\x34")), "")

    def test_garbage_returns_empty_not_raises(self):
        """Непонятная транзакция — это «не опознано», а не падение.

        Приёмник обрабатывает поток платежей; исключение на одном
        остановило бы зачисление всем остальным. Сумма при этом берётся
        из события, не отсюда, поэтому деньги не теряются.
        """
        for bad in (b"", b"\xff\xff\xff", b"\x0a\x7f", bytes(range(32))):
            self.assertEqual(memo(bad), "", repr(bad))

    def test_truncated_varint(self):
        self.assertEqual(memo(b"\x0a\x03\x12\x01"), "")


if __name__ == "__main__":
    unittest.main()
