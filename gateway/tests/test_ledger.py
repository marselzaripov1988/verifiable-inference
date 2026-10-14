"""Учёт денег шлюза.

Проверяется то, из-за чего шлюз терял бы деньги или отдавал работу
даром, а не ветки кода.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from artery_gateway.ledger import (  # noqa: E402
    InsufficientFunds,
    Ledger,
    UnknownAccount,
)
from artery_gateway.pricing import PriceNotSet, Pricing  # noqa: E402

PAYER = "artr1m0twhjryj54qcv2m0tga0wlsw9uh8c9s8fugg6"


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "ledger.json")
        self.l = Ledger(self.path)


class Crediting(LedgerCase):
    def test_memo_identifies_account(self):
        a = self.l.open_account()
        got = self.l.credit("H1", 10, PAYER, 5_000_000, f"оплата {a.code}")
        self.assertIsNotNone(got)
        self.assertEqual(self.l.account(a.key).balance, 5_000_000)

    def test_same_payment_credited_once(self):
        """Приёмник отдаёт платежи страницами и без гарантии порядка, а
        перезапуск начинает чтение заново. Без защиты по хэшу один
        перевод зачислился бы дважды."""
        a = self.l.open_account()
        self.l.credit("H1", 10, PAYER, 5_000_000, a.code)
        self.l.credit("H1", 10, PAYER, 5_000_000, a.code)
        self.assertEqual(self.l.account(a.key).balance, 5_000_000)

    def test_unknown_memo_goes_to_unclaimed(self):
        """Деньги не теряются и не зачисляются наугад по адресу
        отправителя: адрес публичен, и тогда любой тратил бы чужое."""
        self.l.open_account()
        got = self.l.credit("H2", 11, PAYER, 7_000_000, "спасибо")
        self.assertIsNone(got)
        self.assertEqual(len(self.l.unclaimed), 1)
        self.assertEqual(self.l.unclaimed[0]["amount"], 7_000_000)

    def test_height_moves_even_for_unclaimed(self):
        """Иначе шлюз перечитывал бы неопознанный платёж вечно."""
        self.l.credit("H3", 42, PAYER, 1, "без кода")
        self.assertEqual(self.l.height, 42)

    def test_code_found_inside_longer_memo(self):
        """Кошельки добавляют к памятке своё, человек — слова и пробелы."""
        a = self.l.open_account()
        self.l.credit("H4", 1, PAYER, 100, f"  Оплата инференса, {a.code} спасибо ")
        self.assertEqual(self.l.account(a.key).balance, 100)

    def test_durable_across_restart(self):
        a = self.l.open_account()
        self.l.credit("H5", 7, PAYER, 3_000_000, a.code)
        again = Ledger(self.path)
        self.assertEqual(again.account(a.key).balance, 3_000_000)
        self.assertEqual(again.height, 7)
        # И повторное зачисление после перезапуска тоже не проходит.
        again.credit("H5", 7, PAYER, 3_000_000, a.code)
        self.assertEqual(again.account(a.key).balance, 3_000_000)


class Spending(LedgerCase):
    def account(self, balance: int):
        a = self.l.open_account()
        self.l.credit("H", 1, PAYER, balance, a.code)
        return a

    def test_reserve_then_settle_less(self):
        """Резерв по максимуму, списание по действительному расходу."""
        a = self.account(1_000_000)
        self.l.reserve(a.key, 500_000)
        self.l.settle(a.key, 500_000, 120_000, tokens=1200)
        got = self.l.account(a.key)
        self.assertEqual(got.balance, 880_000)
        self.assertEqual(got.reserved, 0)
        self.assertEqual(got.spent, 120_000)
        self.assertEqual(got.tokens, 1200)

    def test_reserve_blocks_second_request(self):
        """Пока резерв держится, второй запрос не должен пролезть: иначе
        два одновременных запроса обслужились бы на один остаток."""
        a = self.account(100_000)
        self.l.reserve(a.key, 100_000)
        with self.assertRaises(InsufficientFunds):
            self.l.reserve(a.key, 1)

    def test_release_charges_nothing(self):
        a = self.account(100_000)
        self.l.reserve(a.key, 90_000)
        self.l.release(a.key, 90_000)
        got = self.l.account(a.key)
        self.assertEqual(got.balance, 100_000)
        self.assertEqual(got.reserved, 0)

    def test_settle_never_exceeds_reserve(self):
        """Запас на случай, если расход окажется больше резерва: остаток
        не должен уходить в минус незаметно."""
        a = self.account(100_000)
        self.l.reserve(a.key, 10_000)
        self.l.settle(a.key, 10_000, 999_999, tokens=1)
        self.assertEqual(self.l.account(a.key).balance, 90_000)

    def test_empty_account_cannot_work(self):
        a = self.l.open_account()
        with self.assertRaises(InsufficientFunds):
            self.l.reserve(a.key, 1)

    def test_unknown_key(self):
        with self.assertRaises(UnknownAccount):
            self.l.account("нет такого")


class PricingCase(unittest.TestCase):
    def test_price_must_be_set(self):
        """Курс ARTR в коде не выдумывается."""
        with self.assertRaises(PriceNotSet):
            Pricing(price_micro_per_million=0)

    def test_cost_rounds_up(self):
        """Вниз означало бы, что запрос в несколько токенов бесплатен —
        и тогда бесплатен любой поток таких запросов."""
        p = Pricing(price_micro_per_million=1_000_000)
        self.assertEqual(p.cost(1_000_000), 1_000_000)
        self.assertEqual(p.cost(1), 1)
        self.assertEqual(p.cost(0), 0)

    def test_reserve_covers_prompt_and_answer(self):
        p = Pricing(price_micro_per_million=2_000_000)
        self.assertEqual(p.reserve_for(100, 900), p.cost(1000))


if __name__ == "__main__":
    unittest.main()
