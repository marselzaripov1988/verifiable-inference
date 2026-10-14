"""Проверка адреса казны — на НАСТОЯЩИХ адресах из цепочек.

Адреса ниже взяты из axiome-1 и из замеров Artery, а не придуманы: у
придуманного адреса контрольная сумма не сошлась бы, и тест проверял бы
только то, что проверка отвергает мусор.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from artery_gateway.bech32 import BadAddress, check, decode  # noqa: E402

# Настоящие адреса axiome-1.
ADMIN = "axm1l0a927clmz7wqmls2737ctwlxzhxeqmxaf55w7"
BARTR = "axm1uzldhz4dvv7k7gtc7jl5ahlj84eghc52p0r6zhnjca0d20dxcfkshsd8uz"
DIST = "axm1xcw72vdrmd76d3krjfwgc2pcx5z9ccywmnkdzxajfclav8lhvytq45n7dl"
# Настоящий адрес Artery из параметров банка её боевого генезиса.
COMPANY = "artr1d3paqmusp39t2yhx4ju4vm50pfjmddfkwnn22p"


class Valid(unittest.TestCase):
    def test_account_address(self):
        self.assertEqual(check(ADMIN, "axm"), ADMIN)

    def test_contract_addresses(self):
        """Контракт даёт 32 байта вместо 20 — казной он быть может."""
        for a in (BARTR, DIST):
            self.assertEqual(check(a, "axm"), a)

    def test_artery_address(self):
        self.assertEqual(check(COMPANY, "artr"), COMPANY)

    def test_upper_case_accepted(self):
        self.assertEqual(check(ADMIN.upper(), "axm"), ADMIN)

    def test_prefix_and_data_returned(self):
        hrp, data = decode(ADMIN)
        self.assertEqual(hrp, "axm")
        self.assertEqual(len(data), 32)


class Rejected(unittest.TestCase):
    def test_single_character_typo(self):
        """Главный случай: один изменённый знак.

        Без этой проверки шлюз исправно опрашивал бы цепочку, исправно
        не находил платежей — и это выглядело бы отсутствием спроса, а не
        ошибкой настройки.
        """
        broken = ADMIN[:-1] + ("8" if ADMIN[-1] != "8" else "9")
        with self.assertRaises(BadAddress) as e:
            check(broken, "axm")
        self.assertIn("контрольная сумма", str(e.exception))

    def test_transposed_characters(self):
        a = list(ADMIN)
        a[10], a[11] = a[11], a[10]
        with self.assertRaises(BadAddress):
            check("".join(a), "axm")

    def test_wrong_network(self):
        """Адрес Artery в шлюзе Axiome: платежи ждали бы не там."""
        with self.assertRaises(BadAddress) as e:
            check(COMPANY, "axm")
        self.assertIn("artr", str(e.exception))

    def test_not_an_address(self):
        for bad in ("", "axm1", "просто строка", "axm1" + "b" * 38,
                    "0x4d2f8c3aA1B2c3D4e5F60718293A4b5C6d7E8f90"):
            with self.assertRaises(BadAddress):
                check(bad, "axm")

    def test_mixed_case_rejected(self):
        mixed = ADMIN[:10].upper() + ADMIN[10:]
        with self.assertRaises(BadAddress) as e:
            check(mixed, "axm")
        self.assertIn("регистр", str(e.exception))

    def test_character_outside_alphabet(self):
        """В алфавите bech32 нет 1, b, i и o — именно чтобы их не путали
        с l, 6, 1 и 0 при наборе руками."""
        with self.assertRaises(BadAddress) as e:
            check(ADMIN[:-1] + "b", "axm")
        self.assertIn("bech32", str(e.exception))


if __name__ == "__main__":
    unittest.main()
