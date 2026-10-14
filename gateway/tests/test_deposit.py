"""Адрес на счёт: зачисление по получателю, без памятки.

Зачем так. Памятка требует кошелька, который её умеет, и человека,
который не забудет её вписать. MetaMask для Axiome не годится вовсе (у
цепочки нет EVM-слоя), а у универсальных кошельков своя ловушка:
slip44 у Axiome 546, а не обычные для Cosmos 118, и кошелёк,
предполагающий 118, выведет из той же сид-фразы ДРУГОЙ адрес.

Поэтому второй путь: каждому счёту свой адрес приёма. Тогда платить
можно откуда угодно и памятка не нужна — зачисление идёт по получателю.

Адреса ниже настоящие, из axiome-1: у придуманного не сошлась бы
контрольная сумма, и проверка отвергла бы его раньше теста.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from artery_gateway import inference, server  # noqa: E402
from artery_gateway.ledger import Ledger, NoFreeAddress  # noqa: E402
from artery_gateway.pricing import Pricing  # noqa: E402
from test_gateway import PRICE, TREASURY, FakeRPC  # noqa: E402

# Настоящие адреса axiome-1.
A1 = "axm1l0a927clmz7wqmls2737ctwlxzhxeqmxaf55w7"
A2 = "axm153wetpldgkglhqnuuur0peexg5pg0fvm0jfk3v"
A3 = "axm1a5jf44dynnj3h9qg4gf3h5kx796xjaj82sf5rx"
PAYER = "axm1m0twhjryj54qcv2m0tga0wlsw9uh8c9sh7m2kr"


class Pool(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "l.json")
        self.l = Ledger(self.path)

    def test_added_and_issued_once(self):
        self.assertEqual(self.l.add_addresses([A1, A2]), 2)
        a = self.l.open_account(need_deposit=True)
        b = self.l.open_account(need_deposit=True)
        self.assertEqual({a.deposit, b.deposit}, {A1, A2})
        self.assertNotEqual(a.deposit, b.deposit, "один адрес двум счетам")
        self.assertEqual(self.l.free, [])

    def test_duplicates_and_issued_are_skipped(self):
        """Выдать один адрес двум счетам значит смешать чужие деньги."""
        self.l.add_addresses([A1, A1, A2])
        self.assertEqual(len(self.l.free), 2)
        self.l.open_account(need_deposit=True)
        self.assertEqual(self.l.add_addresses([A1, A2, A3]), 1)

    def test_no_free_address_refuses(self):
        """Отказ лучше тихой выдачи общей казны: клиент заплатил бы без
        памятки, и платёж попал бы в неразобранные."""
        with self.assertRaises(NoFreeAddress):
            self.l.open_account(need_deposit=True)

    def test_survives_restart(self):
        self.l.add_addresses([A1, A2])
        a = self.l.open_account(need_deposit=True)
        again = Ledger(self.path)
        self.assertEqual(again.account(a.key).deposit, a.deposit)
        self.assertEqual(again.free, [A2])
        self.assertEqual(again.by_deposit, {a.deposit: a.key})


class CreditByRecipient(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "l.json")
        self.l = Ledger(self.path)
        self.l.add_addresses([A1, A2])
        self.a = self.l.open_account(need_deposit=True)
        self.b = self.l.open_account(need_deposit=True)

    def test_no_memo_needed(self):
        got = self.l.credit("H1", 10, PAYER, 5_000_000, "", self.a.deposit)
        self.assertIsNotNone(got)
        self.assertEqual(self.l.account(self.a.key).balance, 5_000_000)

    def test_recipient_beats_memo(self):
        """Платёж на адрес A с кодом B в памятке зачисляется A: адрес
        надёжнее, его нельзя забыть вписать."""
        self.l.credit("H2", 11, PAYER, 7_000_000,
                      f"оплата {self.b.code}", self.a.deposit)
        self.assertEqual(self.l.account(self.a.key).balance, 7_000_000)
        self.assertEqual(self.l.account(self.b.key).balance, 0)

    def test_unknown_recipient_unclaimed_with_address(self):
        got = self.l.credit("H3", 12, PAYER, 9_000_000, "", A3)
        self.assertIsNone(got)
        self.assertEqual(len(self.l.unclaimed), 1)
        self.assertEqual(self.l.unclaimed[0]["recipient"], A3,
                         "получатель записан: иначе платёж не разобрать")

    def test_memo_still_works_for_treasury(self):
        """Старый путь не сломан: платёж на общую казну с кодом."""
        c = self.l.open_account()
        self.assertEqual(c.deposit, "")
        self.l.credit("H4", 13, PAYER, 100, f"оплата {c.code}", TREASURY)
        self.assertEqual(self.l.account(c.key).balance, 100)

    def test_same_payment_once(self):
        self.l.credit("H5", 14, PAYER, 100, "", self.a.deposit)
        self.l.credit("H5", 14, PAYER, 100, "", self.a.deposit)
        self.assertEqual(self.l.account(self.a.key).balance, 100)


class EndToEnd(unittest.TestCase):
    """Клиент платит на СВОЙ адрес без памятки и получает ответ."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.rpc = FakeRPC()
        led = Ledger(os.path.join(cls.dir, "l.json"))
        led.add_addresses([A1, A2])
        cls.gw = server.Gateway(
            ledger=led,
            pricing=Pricing(price_micro_per_million=PRICE,
                            max_prompt_tokens=64, max_answer_tokens=128),
            treasury=TREASURY, rpc=cls.rpc, base_url="http://модели-нет/v1",
            per_account=True)
        inference.count_tokens = lambda base, model, text, timeout=30: len(
            text.split())
        inference.complete = lambda base, model, prompt, max_tokens: {
            "answer": " Париж.", "prompt_tokens": len(prompt.split()),
            "answer_tokens": 3, "total_tokens": len(prompt.split()) + 3}
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0),
                                             server.make_handler(cls.gw))
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def call(self, method, path, body=None, key=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if key:
            req.add_header("X-API-Key", key)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_1_account_gets_its_own_address(self):
        code, r = self.call("POST", "/v1/account")
        self.assertEqual(code, 201, r)
        self.assertIn(r["deposit"], (A1, A2))
        self.assertNotEqual(r["deposit"], TREASURY)
        self.assertIn("памятка не нужна", r["как_платить"])
        EndToEnd.key, EndToEnd.dep = r["api_key"], r["deposit"]

    def test_2_payment_without_memo_is_credited(self):
        self.rpc.pay("TXD", 60, 3_000_000, "", recipient=self.dep)
        self.assertEqual(self.gw.poll_payments(), 1)
        _, r = self.call("GET", "/v1/account", key=self.key)
        self.assertEqual(r["balance_micro"], 3_000_000)
        self.assertEqual(r["deposit"], self.dep)

    def test_3_answer_is_paid_for(self):
        code, r = self.call("POST", "/v1/completions",
                            {"prompt": "Столица Франции?", "max_tokens": 16},
                            key=self.key)
        self.assertEqual(code, 200, r)
        self.assertEqual(r["balance_micro"], 3_000_000 - r["charged_micro"])

    def test_4_one_query_per_poll(self):
        """Опрос по диапазону высот: число запросов не зависит от числа
        счетов. Иначе сотня клиентов означала бы сотню запросов за цикл."""
        before = self.rpc.calls
        self.gw.poll_payments()
        self.assertLessEqual(self.rpc.calls - before, 2)

    def test_5_pool_runs_out(self):
        self.call("POST", "/v1/account")          # забирает последний
        code, r = self.call("POST", "/v1/account")
        self.assertEqual(code, 503, r)
        self.assertIn("свободных адресов", r["error"])


if __name__ == "__main__":
    unittest.main()
