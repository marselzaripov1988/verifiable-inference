"""Разбор событий перевода.

На stdlib-овском unittest, а не pytest: сам модуль намеренно обходится без
зависимостей, и тесты к нему тоже.

    python -m unittest discover -s gateway/tests
"""

from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from artery_gateway.chain import (  # noqa: E402
    EVENT_TRANSFER,
    AmountUnparsed,
    _coins,
    _credited_amount,
)

TREASURY = "artr1d2nz3z4z6v9ykys9warl68cc0q4r77f75jng0a"
PAYER = "artr1m0twhjryj54qcv2m0tga0wlsw9uh8c9s8fugg6"
FEE_ADDR = "artr1d3paqmusp39t2yhx4ju4vm50pfjmddfkwnn22p"

AMOUNT = json.dumps([{"denom": "uartr", "amount": "4534546632"}], separators=(",", ":"))
FEE = json.dumps([{"denom": "uartr", "amount": "2267273316"}], separators=(",", ":"))


def plain(_type: str = EVENT_TRANSFER, **attrs: str) -> dict:
    """Событие в виде CometBFT: атрибуты обычными строками."""
    return {
        "type": _type,
        "attributes": [{"key": k, "value": v} for k, v in attrs.items()],
    }


def encoded(_type: str = EVENT_TRANSFER, **attrs: str) -> dict:
    """Событие в виде Tendermint 0.34: ключи и значения в base64."""
    b64 = lambda s: base64.b64encode(s.encode()).decode()  # noqa: E731
    return {
        "type": _type,
        "attributes": [{"key": b64(k), "value": b64(v)} for k, v in attrs.items()],
    }


class CreditedAmount(unittest.TestCase):
    def test_plain_attributes(self):
        """Сборка после миграции на SDK 0.47 отдаёт атрибуты строками."""
        events = [plain(sender=PAYER, recipient=TREASURY, amount=AMOUNT)]
        self.assertEqual(_credited_amount(events, TREASURY), (PAYER, 4534546632))

    def test_base64_attributes(self):
        """Мейннет на Tendermint 0.34 отдаёт их в base64 — результат тот же."""
        events = [encoded(sender=PAYER, recipient=TREASURY, amount=AMOUNT)]
        self.assertEqual(_credited_amount(events, TREASURY), (PAYER, 4534546632))

    def test_attribute_order_varies(self):
        """Порядок атрибутов внутри события непостоянен — так на живой сети."""
        events = [plain(amount=AMOUNT, sender=PAYER, recipient=TREASURY)]
        self.assertEqual(_credited_amount(events, TREASURY), (PAYER, 4534546632))

    def test_fee_event_not_counted(self):
        """Комиссия сети едет отдельным transfer на служебный адрес."""
        events = [
            plain(sender=PAYER, recipient=FEE_ADDR, amount=FEE),
            plain(sender=PAYER, recipient=TREASURY, amount=AMOUNT),
        ]
        self.assertEqual(_credited_amount(events, TREASURY), (PAYER, 4534546632))

    def test_nothing_for_treasury(self):
        events = [plain(sender=PAYER, recipient=FEE_ADDR, amount=FEE)]
        self.assertIsNone(_credited_amount(events, TREASURY))

    def test_several_events_sum_up(self):
        """Несколько зачислений в одной транзакции складываются."""
        events = [
            plain(sender=PAYER, recipient=TREASURY, amount=AMOUNT),
            plain(sender=PAYER, recipient=TREASURY, amount=FEE),
        ]
        self.assertEqual(
            _credited_amount(events, TREASURY),
            (PAYER, 4534546632 + 2267273316),
        )


class EventType(unittest.TestCase):
    """Тип события у Artery — имя proto-сообщения, а не `transfer`.

    Этот класс закрывает дефект, который прежние тесты пропустили: они
    сами строили события с типом `transfer`, и разбор, искавший
    `transfer`, на них проходил. На живой Artery он нашёл бы ноль
    платежей и показал «платежей нет» при пришедших деньгах.

    Проверено по исходникам: x/bank/internal/keeper/send.go испускает
    util.EmitEvent(&types.EventTransfer{...}), а patchEvent берёт типом
    proto.MessageName — одинаково на master (SDK 0.42, мейннет) и на
    ветке переезда (SDK 0.53).
    """

    def test_artery_typed_event(self):
        events = [plain(sender=PAYER, recipient=TREASURY, amount=AMOUNT)]
        self.assertEqual(events[0]["type"], "artery.bank.v1beta1.EventTransfer")
        self.assertEqual(_credited_amount(events, TREASURY), (PAYER, 4534546632))

    def test_artery_typed_event_base64(self):
        events = [encoded(sender=PAYER, recipient=TREASURY, amount=AMOUNT)]
        self.assertEqual(_credited_amount(events, TREASURY), (PAYER, 4534546632))

    def test_stock_cosmos_event(self):
        """Обычный банк Cosmos: тип `transfer`, сумма строкой.

        Нужен для девнета: шлюз удобно проверять на обычном узле, где
        Artery нет.
        """
        events = [plain("transfer", sender=PAYER, recipient=TREASURY,
                        amount="4534546632uartr")]
        self.assertEqual(_credited_amount(events, TREASURY), (PAYER, 4534546632))

    def test_other_events_ignored(self):
        events = [plain("artery.referral.v1beta1.EventStatusUpdated",
                        sender=PAYER, recipient=TREASURY, amount=AMOUNT)]
        self.assertIsNone(_credited_amount(events, TREASURY))


class Coins(unittest.TestCase):
    def test_json_array(self):
        self.assertEqual(_coins(AMOUNT), [("uartr", 4534546632)])

    def test_coin_string(self):
        self.assertEqual(_coins("100uartr"), [("uartr", 100)])

    def test_several_coins(self):
        self.assertEqual(_coins("100uartr,5uartrd"),
                         [("uartr", 100), ("uartrd", 5)])

    def test_empty(self):
        self.assertEqual(_coins(""), [])

    def test_garbage_raises_not_skips(self):
        """Непонятную сумму нельзя пропускать молча.

        Пропуск означал бы, что зачислено меньше пришедшего, и узнал бы
        об этом плательщик, а не шлюз.
        """
        with self.assertRaises(AmountUnparsed):
            _coins("сколько-то uartr")
        with self.assertRaises(AmountUnparsed):
            _coins('[{"denom":"uartr"}]')


if __name__ == "__main__":
    unittest.main()
