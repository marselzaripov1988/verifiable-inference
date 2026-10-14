"""Разбор платежей против НАСТОЯЩИХ данных axiome-1.

Образец в `fixtures/axiome_payments.json` снят 14 октября 2026 с
публичного RPC `api-chain.axiomechain.org` — того, что указан в реестре
цепочек Cosmos. Проверять разбор на придуманных событиях мало: ровно так
и выжил дефект с типом события у Artery, где тест строил событие сам и
повторял ошибку кода.

Что здесь подтверждается живыми данными:

  * стандартный банк Axiome испускает событие `transfer`, атрибуты —
    обычными строками, `index=true` (значит поиск по ним работает);
  * сумма записана строкой `93702693uaxm`, а НЕ JSON-массивом, как у
    Artery;
  * в одной сделке несколько переводов, и считать надо только те, где
    получатель — казна;
  * памятка достаётся из байтов транзакции разбором protobuf.
"""

from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from artery_gateway.chain import AxiomeRPC, _coins, _credited_amount  # noqa: E402
from artery_gateway.tx import memo  # noqa: E402

FIX = json.loads(
    (Path(__file__).parent / "fixtures" / "axiome_payments.json")
    .read_text(encoding="utf-8"))


class RealAxiomeData(unittest.TestCase):
    def setUp(self):
        self.to = FIX["recipient_searched"]
        self.txs = FIX["txs"]
        self.assertTrue(self.txs, "образец пуст")

    def test_stock_transfer_event(self):
        """Событие именно `transfer`, а не типизированное, как у Artery."""
        types = {e["type"] for t in self.txs for e in t["tx_result"]["events"]}
        self.assertEqual(types, {"transfer"})

    def test_attributes_are_plain_and_indexed(self):
        """CometBFT 0.38: строки, не base64. И они помечены для индекса —
        без этого `tx_search` по ним не нашёл бы ничего."""
        a = self.txs[0]["tx_result"]["events"][0]["attributes"][0]
        self.assertIsInstance(a["key"], str)
        self.assertNotIn("=", a["key"], "ключ не base64")
        self.assertTrue(all(x.get("index") for t in self.txs
                            for e in t["tx_result"]["events"]
                            for x in e["attributes"]))

    def test_amount_is_a_coin_string(self):
        """У Artery сумма JSON-массивом, здесь — строкой. Разбор обязан
        уметь оба, иначе один из рельсов молча не видит денег."""
        vals = [x["value"] for t in self.txs
                for e in t["tx_result"]["events"]
                for x in e["attributes"] if x["key"] == "amount"]
        self.assertTrue(vals)
        for v in vals:
            self.assertFalse(v.startswith("["), v)
            self.assertTrue(v.endswith("uaxm"), v)
            self.assertEqual(len(_coins(v)), 1)
            self.assertEqual(_coins(v)[0][0], "uaxm")

    def test_credited_amount_counts_only_treasury(self):
        """В сделке несколько переводов: комиссия и прочее идут другим
        адресам. Зачислять надо только своё."""
        for t in self.txs:
            got = _credited_amount(t["tx_result"]["events"], self.to,
                                   denom="uaxm", types=("transfer",))
            self.assertIsNotNone(got, t["hash"])
            sender, total = got
            self.assertTrue(sender.startswith("axm1"))
            expect = sum(
                _coins(x["value"])[0][1]
                for e in t["tx_result"]["events"]
                for x in e["attributes"] if x["key"] == "amount"
                if {y["key"]: y["value"] for y in e["attributes"]}
                .get("recipient") == self.to)
            self.assertEqual(total, expect)
            self.assertGreater(total, 0)

    def test_someone_elses_treasury_gets_nothing(self):
        other = "axm1" + "0" * 38
        for t in self.txs:
            self.assertIsNone(
                _credited_amount(t["tx_result"]["events"], other,
                                 denom="uaxm", types=("transfer",)))

    def test_memo_decoded_from_real_tx_bytes(self):
        """Разбор protobuf — на настоящих байтах сделки, а не на
        собранных вручную."""
        memos = [memo(base64.b64decode(t["tx"])) for t in self.txs]
        self.assertTrue(any(m for m in memos),
                        "ни у одной сделки памятка не прочиталась")
        for m in memos:
            self.assertIsInstance(m, str)

    def test_receiver_wiring(self):
        """У приёмника Axiome та единица и то событие, что в данных."""
        self.assertEqual(AxiomeRPC.denom, "uaxm")
        self.assertEqual(AxiomeRPC.search_event, "transfer")
        self.assertEqual(AxiomeRPC.events, ("transfer",))

    def test_artery_event_is_not_accepted_here(self):
        """Приёмник Axiome не должен принимать событие Artery: это
        разные сети, и путаница в единице стоила бы денег."""
        fake = [{"type": "artery.bank.v1beta1.EventTransfer",
                 "attributes": [{"key": "recipient", "value": self.to},
                                {"key": "sender", "value": "artr1x"},
                                {"key": "amount",
                                 "value": '[{"denom":"uartr","amount":"1"}]'}]}]
        self.assertIsNone(_credited_amount(fake, self.to, denom="uaxm",
                                           types=AxiomeRPC.events))


if __name__ == "__main__":
    unittest.main()
