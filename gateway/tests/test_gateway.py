"""Сквозной прогон шлюза: счёт, платёж, оплаченный ответ.

Подставлена ровно одна вещь — модель, потому что для неё нужна карта.
Всё остальное работает по-настоящему: HTTP поднимается, приёмник
платежей разбирает ответ RPC того самого вида, что отдаёт Artery (её
типизированное событие и памятка из байтов protobuf), учёт пишется на
диск.

Это признак успеха шага 5 в воспроизводимом виде. Чего он не заменяет:
живой карты и настоящего перевода ARTR — «сторонний клиент оплатил и
получил ответ» требует и того, и другого.
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

from artery_gateway import inference, server  # noqa: E402
from artery_gateway.chain import ArteryRPC, AxiomeRPC  # noqa: E402
from artery_gateway.ledger import Ledger  # noqa: E402
from artery_gateway.pricing import Pricing  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tx import tx_body, tx_raw  # noqa: E402

TREASURY = "axm1d2nz3z4z6v9ykys9warl68cc0q4r77f7qyzcjl"
PAYER = "axm1m0twhjryj54qcv2m0tga0wlsw9uh8c9sh7m2kr"
PRICE = 1_000_000  # 1 AXM за миллион токенов — число для теста, не курс


class FakeRPC(AxiomeRPC):
    """RPC, отдающий ответ того вида, что отдаёт Axiome.

    Подменён только транспорт: разбор событий, суммы и памятки идёт
    настоящий. Сеть — Axiome, потому что решено работать там; разбор
    Artery проверяется в test_chain.py, а живые данные Axiome — в
    test_axiome.py.
    """

    def __init__(self):
        super().__init__("http://нет")
        self.txs: list[dict] = []
        #: Сколько запросов к цепочке сделано — им проверяется, что опрос
        #: не растёт с числом счетов.
        self.calls = 0

    def _get(self, path, **params):
        self.calls += 1
        if path == "status":
            return {"sync_info": {"latest_block_height": "100"}}
        if path == "tx_search":
            if int(params.get("page", "1")) > 1:
                return {"txs": [], "total_count": str(len(self.txs))}
            return {"txs": self.txs, "total_count": str(len(self.txs))}
        raise AssertionError(path)

    def pay(self, tx_hash: str, height: int, amount: int, memo: str,
            recipient: str = TREASURY):
        import base64
        self.txs.append({
            "hash": tx_hash,
            "height": str(height),
            "tx": base64.b64encode(tx_raw(tx_body(memo))).decode(),
            "tx_result": {
                "code": 0,
                "events": [{
                    "type": "transfer",
                    "attributes": [
                        {"key": "sender", "value": PAYER},
                        {"key": "recipient", "value": recipient},
                        {"key": "amount", "value": f"{amount}uaxm"},
                    ],
                }],
            },
        })


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.rpc = FakeRPC()
        cls.gw = server.Gateway(
            ledger=Ledger(os.path.join(cls.dir, "ledger.json")),
            pricing=Pricing(price_micro_per_million=PRICE,
                            max_prompt_tokens=64, max_answer_tokens=128),
            treasury=TREASURY, rpc=cls.rpc, base_url="http://модели-нет/v1")

        # Единственная подстановка: модель.
        inference.count_tokens = lambda base, model, text, timeout=30: len(
            text.split())
        inference.complete = lambda base, model, prompt, max_tokens: {
            "answer": " Париж.",
            "prompt_tokens": len(prompt.split()),
            "answer_tokens": 3,
            "total_tokens": len(prompt.split()) + 3,
        }

        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0),
                                             server.make_handler(cls.gw))
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    # --- помощники ---
    def call(self, method, path, body=None, key=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        if key:
            req.add_header("X-API-Key", key)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_01_account_and_payment_instructions(self):
        code, r = self.call("POST", "/v1/account")
        self.assertEqual(code, 201)
        self.assertEqual(r["treasury"], TREASURY)
        self.assertEqual(r["price_micro_per_million"], PRICE)
        self.assertTrue(r["code"].startswith("ag-"))
        EndToEnd.key = r["api_key"]
        EndToEnd.code = r["code"]

    def test_02_no_money_no_answer(self):
        code, r = self.call("POST", "/v1/completions",
                            {"prompt": "Столица Франции?", "max_tokens": 16},
                            key=self.key)
        self.assertEqual(code, 402, r)
        self.assertEqual(r["have_micro"], 0)

    def test_03_payment_is_credited(self):
        self.rpc.pay("TX1", 50, 2_000_000, f"инференс {self.code}")
        self.assertEqual(self.gw.poll_payments(), 1)
        code, r = self.call("GET", "/v1/account", key=self.key)
        self.assertEqual(r["balance_micro"], 2_000_000)
        self.assertEqual(self.gw.ledger.height, 50)

    def test_04_paid_request_answered_and_charged(self):
        code, r = self.call("POST", "/v1/completions",
                            {"prompt": "Столица Франции?", "max_tokens": 16},
                            key=self.key)
        self.assertEqual(code, 200, r)
        self.assertEqual(r["answer"], " Париж.")
        self.assertEqual(r["usage"]["total_tokens"], 5)
        # 5 токенов по 1 ARTR за миллион, с округлением вверх.
        self.assertEqual(r["charged_micro"], 5)
        self.assertEqual(r["balance_micro"], 2_000_000 - 5)
        # Резерв считался по max_tokens и был больше списанного.
        self.assertEqual(r["reserved_micro"], 18)

    def test_05_reserve_released_on_failure(self):
        """Сорвавшаяся работа не должна оставлять резерв: иначе остаток
        тает от каждой неудачи узла."""
        before = self.gw.ledger.account(self.key).balance
        boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("узел упал"))
        ok = inference.complete
        inference.complete = boom
        try:
            code, r = self.call("POST", "/v1/completions",
                                {"prompt": "раз два", "max_tokens": 8},
                                key=self.key)
        finally:
            inference.complete = ok
        self.assertEqual(code, 500, r)
        a = self.gw.ledger.account(self.key)
        self.assertEqual(a.reserved, 0, "резерв снят")
        self.assertEqual(a.balance, before, "ничего не списано")

    def test_06_second_payment_adds_up(self):
        before = self.gw.ledger.account(self.key).balance
        self.rpc.pay("TX2", 51, 1_000_000, self.code)
        self.gw.poll_payments()
        self.assertEqual(self.gw.ledger.account(self.key).balance,
                         before + 1_000_000)

    def test_07_same_payment_not_credited_twice(self):
        before = self.gw.ledger.account(self.key).balance
        self.gw.poll_payments()   # те же транзакции читаются снова
        self.assertEqual(self.gw.ledger.account(self.key).balance, before)

    def test_08_payment_without_code_is_unclaimed(self):
        self.rpc.pay("TX3", 52, 9_000_000, "просто перевод")
        self.gw.poll_payments()
        self.assertEqual(len(self.gw.ledger.unclaimed), 1)
        self.assertEqual(self.gw.ledger.unclaimed[0]["amount"], 9_000_000)

    def test_09_stranger_key_rejected(self):
        # Ключ только из ASCII: заголовки HTTP в latin-1, и кириллицу
        # в них нельзя даже отправить.
        code, r = self.call("GET", "/v1/account", key="not-a-real-key")
        self.assertEqual(code, 401)
        code, r = self.call("POST", "/v1/completions",
                            {"prompt": "раз", "max_tokens": 4},
                            key="not-a-real-key")
        self.assertEqual(code, 401)

    def test_10_limits_enforced(self):
        code, r = self.call("POST", "/v1/completions",
                            {"prompt": "раз", "max_tokens": 9999},
                            key=self.key)
        self.assertEqual(code, 400, r)
        code, r = self.call("POST", "/v1/completions",
                            {"prompt": "слово " * 200, "max_tokens": 8},
                            key=self.key)
        self.assertEqual(code, 400, r)
        code, r = self.call("POST", "/v1/completions",
                            {"prompt": "", "max_tokens": 8}, key=self.key)
        self.assertEqual(code, 400, r)

    def test_11_healthz_tells_the_truth(self):
        code, r = self.call("GET", "/healthz")
        self.assertEqual(code, 200)
        self.assertEqual(r["treasury"], TREASURY)
        self.assertEqual(r["height"], 52)
        self.assertEqual(r["unclaimed"], 1)
        self.assertIsNone(r["poll_error"])

    def test_12_poll_error_is_remembered(self):
        """Молчащий приёмник выглядит как «платежей нет» — и это ровно
        та картина, которой нельзя доверять."""
        bad = ArteryRPC("http://127.0.0.1:1")
        ok = self.gw.rpc
        self.gw.rpc = bad
        try:
            with self.assertRaises(Exception):
                self.gw.poll_payments()
        finally:
            self.gw.rpc = ok
        code, r = self.call("GET", "/healthz")
        self.assertIsNotNone(r["poll_error"])


if __name__ == "__main__":
    unittest.main()
