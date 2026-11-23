#!/usr/bin/env python3
"""Проверка подписи без axmd: `python3 test_wallet.py`.

Главная проверка — на сделке с живой цепочки. 23 ноября 2026 через `axmd`
был отправлен `submit_job` в боевой реестр 4090 (txhash 9DE233CC…D2F0).
Здесь он пересобирается из разобранного вида, а подпись цепочки
сверяется с нашим SignDoc. Подпись сходится, только если каждый байт
тела, AuthInfo и SignDoc совпал с тем, что подписал `axmd`.
"""

import base64
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sign    # noqa: E402
import tx      # noqa: E402
import wallet  # noqa: E402

FAILS = []


def check(name, ok, info=""):
    print(("ok   " if ok else "FAIL ") + name + (f" — {info}" if info and
                                                  not ok else ""))
    if not ok:
        FAILS.append(name)


def raises(fn, text=""):
    try:
        fn()
    except tx.TxFailed as e:
        return text in str(e)
    return False


# --- сделка с живой цепочки -----------------------------------------------

LIVE = {
    "sender": "axm1uq57waxxcajk6cwumvcl8lv3wcv5xmdw00nnxy",
    "contract": "axm1fe5dsxm3vcnwjkvlgjk9mkvqnl6ggpay3y3r94hls7g3hz4s68tqcp547r",
    "msg": {"submit_job": {
        "prompt_hash": "8da1fe1ab1f2a094d198396a82c7d1c12cfe8342"
                       "d7a4539438246340b4ebe474",
        "prompt_uri": "http://145.63.132.179:8080", "price": "210000"}},
    "funds": "220185uaxm",
    "pub": "A1CDXGV9Qo2m7c9m2yj+tbWIT4uKjAH1UJ2VTjqcuNtD",
    "account_number": 72459, "sequence": 16,
    "gas": 181379, "fee": 454,
    "sig": "I6iwheea7d3s73c05jR58OonyAGDHrGoBv/Ejc5cJMwN19GUM9H9S0HnB1Gf8TAm"
           "53yrstVdPJIggRZ59UQHdw==",
}


def live_digest(chain_id="axiome-1", account_number=None, msg=None):
    pub = base64.b64decode(LIVE["pub"])
    body = wallet.tx_body([(wallet.MSG_EXECUTE, wallet.execute_msg(
        LIVE["sender"], LIVE["contract"], wallet.compact(msg or LIVE["msg"]),
        wallet.parse_coins(LIVE["funds"])))])
    info = wallet.auth_info(pub, LIVE["sequence"], LIVE["gas"],
                            [("uaxm", LIVE["fee"])])
    doc = wallet.sign_doc(body, info, chain_id,
                          account_number or LIVE["account_number"])
    return pub, hashlib.sha256(doc).digest()


pub, dg = live_digest()
sig = base64.b64decode(LIVE["sig"])
check("адрес из ключа живой сделки", sign.address(pub) == LIVE["sender"])
check("подпись живой сделки сходится с нашим SignDoc",
      sign.verify_digest(pub, dg, sig))
check("чужой chain-id — подпись не сходится",
      not sign.verify_digest(pub, live_digest("axiome-2")[1], sig))
check("чужой номер счёта — не сходится",
      not sign.verify_digest(pub, live_digest(account_number=72458)[1], sig))
check("другая цена в сообщении — не сходится", not sign.verify_digest(
    pub, live_digest(msg={"submit_job": dict(
        LIVE["msg"]["submit_job"], price="210001")})[1], sig))


# --- ключи ----------------------------------------------------------------

# BIP-39: официальный вектор Trezor.
check("BIP-39: семя фразы", wallet.mnemonic_seed(
    "abandon " * 11 + "about", "TREZOR").hex().startswith(
    "c55257c360c07c72029aebc1b53c05ed0362ada38ead3e3e9efa3708e5349553"))
check("BIP-39: лишние пробелы не меняют семя",
      wallet.mnemonic_seed("  abandon " * 11 + " about\n", "TREZOR")
      == wallet.mnemonic_seed("abandon " * 11 + "about", "TREZOR"))
# BIP-32: вектор 1.
seed = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
check("BIP-32: корень", wallet.derive(seed, "m").hex() ==
      "e8f32e723decf4051aefac8e2c93c9c5b214313817cdb01a1494b917c8436b35")
check("BIP-32: m/0'", wallet.derive(seed, "m/0'").hex() ==
      "edb2e14f9ee77d26dd93b4ecede8d16ed408ce149b6cd80b0715a2d911a0afea")
check("BIP-32: m/0'/1 (обычный шаг)", wallet.derive(seed, "m/0'/1").hex() ==
      "3c6cb8d0f6a264c91ea8b5030fadaa8e538b020f0a387421a12de9319dc93368")
check("путь Axiome — монета 546", wallet.AXIOME_PATH == "m/44'/546'/0'/0/0")


# --- суммы ----------------------------------------------------------------

check("сумма одной монетой", wallet.parse_coins("10485uaxm") ==
      [("uaxm", 10485)])
check("несколько монет — по имени", wallet.parse_coins("2uaxm,1abc") ==
      [("abc", 1), ("uaxm", 2)])
check("пусто — без монет", wallet.parse_coins(None) == [])
check("ноль не пишется", wallet.parse_coins("0uaxm") == [])
check("кривая сумма — отказ", raises(lambda: wallet.parse_coins("1.5uaxm"),
                                     "не понял сумму"))


# --- отправка через подставной LCD ----------------------------------------

KEY = bytes.fromhex("11" * 32)
ME = sign.address(sign.pubkey(KEY))


class LCD:
    """Отвечает как LCD. Сделки, пришедшие на отправку, разбирать не
    нужно: проверяется их подпись по тем же функциям."""

    def __init__(self, seq=5, network="axiome-1"):
        self.seq = seq
        self.network = network
        self.calls = []
        self.sent = []
        self.simulate_error = None
        self.broadcast_code = 0
        self.mismatch_once = False
        self.block_code = 0
        self.pending = 1
        self.account_exists = True

    def __call__(self, method, path, body=None):
        self.calls.append((method, path))
        if path.endswith("node_info"):
            return 200, {"default_node_info": {"network": self.network}}
        if path.startswith("/cosmos/auth/v1beta1/accounts/"):
            if not self.account_exists:
                return 404, {"code": 5, "message": "account not found"}
            return 200, {"account": {
                "@type": "/cosmos.auth.v1beta1.BaseAccount",
                "address": ME, "account_number": "42",
                "sequence": str(self.seq)}}
        if path == "/cosmos/tx/v1beta1/simulate":
            if self.simulate_error:
                return 400, {"code": 2, "message": self.simulate_error}
            return 200, {"gas_info": {"gas_used": "100000"}}
        if path == "/cosmos/tx/v1beta1/txs":
            raw = base64.b64decode(body["tx_bytes"])
            if self.mismatch_once:
                self.mismatch_once = False
                return 200, {"tx_response": {
                    "code": 32, "txhash": "X", "raw_log":
                    f"account sequence mismatch, expected {self.seq + 3}, "
                    f"got {self.seq}: incorrect account sequence"}}
            self.sent.append(raw)
            return 200, {"tx_response": {"code": self.broadcast_code,
                                         "txhash": f"H{len(self.sent)}",
                                         "raw_log": "отказ проверки"}}
        if path.startswith("/cosmos/tx/v1beta1/txs/"):
            if self.pending:
                self.pending -= 1
                return 404, {"code": 5, "message": "tx not found"}
            return 200, {"tx_response": {
                "code": self.block_code, "height": "100",
                "raw_log": "задание в состоянии Claimed",
                "events": [{"type": "wasm", "attributes": [
                    {"key": "job_id", "value": "77"}]}]}}
        return 404, {"message": "нет пути"}


def make(lcd, **kw):
    return wallet.Wallet("axm1contract", KEY, http=lcd,
                         sleep=lambda s: None, **kw)


def fields(raw):
    """Разбор protobuf верхнего уровня: {номер: [значения]}."""
    out, i = {}, 0
    while i < len(raw):
        tag, i = _uvarint(raw, i)
        f, t = tag >> 3, tag & 7
        if t == 0:
            v, i = _uvarint(raw, i)
        else:
            n, i = _uvarint(raw, i)
            v, i = raw[i:i + n], i + n
        out.setdefault(f, []).append(v)
    return out


def _uvarint(b, i):
    n = s = 0
    while True:
        c = b[i]
        i += 1
        n |= (c & 0x7F) << s
        s += 7
        if not c & 0x80:
            return n, i


lcd = LCD()
w = make(lcd)
check("адрес — из ключа", w.address() == ME)
res = w.send({"cancel_job": {"job_id": 3}}, funds="10uaxm")
check("send ждёт блок и отдаёт сделку", res["code"] == 0 and
      res["height"] == "100")
check("номер задания достаётся как у axmd",
      tx.attribute(res["tx"], "job_id") == "77")

raw = fields(lcd.sent[0])
body, info, sigs = raw[1][0], raw[2][0], raw[3]
signer = fields(fields(info)[1][0])
fee = fields(fields(info)[2][0])
check("последовательность из счёта", signer[3] == [5])
check("газ — симуляция × 1.3", fee[2] == [130000])
check("плата за газ — 0.0025uaxm", fields(fee[1][0]) ==
      {1: [b"uaxm"], 2: [b"325"]})
doc = wallet.sign_doc(body, info, "axiome-1", 42)
check("подпись сходится с ключом агента", len(sigs) == 1 and
      sign.verify_digest(sign.pubkey(KEY), hashlib.sha256(doc).digest(),
                         sigs[0]))
msg = fields(fields(fields(body)[1][0])[2][0])
check("сообщение — тот же компактный JSON, что у axmd",
      msg[3] == [b'{"cancel_job":{"job_id":3}}'] and msg[1] == [ME.encode()]
      and msg[2] == [b"axm1contract"])
check("деньги в сообщении", fields(msg[5][0]) == {1: [b"uaxm"], 2: [b"10"]})

# Вторая сделка, пока первая в очереди: номер растёт локально.
lcd.pending = 1
w.send({"x": {}})
check("следующая сделка — следующий номер, без чтения счёта",
      fields(fields(fields(lcd.sent[1])[2][0])[1][0])[3] == [6] and
      sum(1 for c in lcd.calls if "/accounts/" in c[1]) == 1)
check("сеть сверяется один раз",
      sum(1 for c in lcd.calls if c[1].endswith("node_info")) == 1)
check("другой адрес контракта (CW20) уходит в сообщение",
      (lambda: (w.send({"send": {}}, contract="axm1token"), fields(fields(
          fields(fields(lcd.sent[-1])[1][0])[1][0])[2][0])[2])[1])()
      == [b"axm1token"])

# Номер разошёлся — поправка по тексту отказа и один повтор.
lcd = LCD(seq=10)
lcd.mismatch_once = True
w = make(lcd)
w.send({"x": {}})
check("sequence mismatch: повтор с номером из отказа",
      fields(fields(fields(lcd.sent[0])[2][0])[1][0])[3] == [13])

# Отказ контракта на симуляции — текст цепочки, гонка узнаётся.
lcd = LCD()
lcd.simulate_error = ("failed to execute message; message index: 0: "
                      "задание в состоянии Claimed: execute wasm contract "
                      "failed")
w = make(lcd)
try:
    w.send({"claim_job": {"job_id": 1}})
    check("отказ симуляции — TxFailed", False)
except tx.TxFailed as e:
    check("отказ симуляции — TxFailed, гонка", tx.classify(str(e)) == "race")
check("после отказа симуляции ничего не отправлено", lcd.sent == [])

lcd = LCD()
lcd.broadcast_code = 13
check("отказ при отправке — TxFailed с кодом",
      raises(lambda: make(lcd).send({"x": {}}), "code=13"))

lcd = LCD()
lcd.block_code = 5
check("сделка в блоке с ошибкой — TxFailed",
      raises(lambda: make(lcd).send({"x": {}}), "code=5"))

lcd = LCD()
lcd.account_exists = False
check("пустой кошелёк — понятный отказ",
      raises(lambda: make(lcd).send({"x": {}}), "Пополните кошелёк"))

lcd = LCD(network="axiome-testnet")
check("чужая сеть — отказ до подписи",
      raises(lambda: make(lcd).send({"x": {}}), "axiome-testnet") and
      lcd.sent == [])

lcd = LCD()
lcd.pending = 10 ** 6
check("сделка не попала в блок — TxFailed по сроку",
      raises(lambda: make(lcd, wait=0.01).send({"x": {}}), "не попала"))


def down(method, path, body=None):
    raise tx.TxFailed("connection: LCD не ответил")


try:
    make(down).send({"x": {}})
    check("LCD недоступен — отказ", False)
except tx.TxFailed as e:
    check("LCD недоступен — повторяемый отказ", tx.classify(str(e)) == "retry")

check("цена газа разбирается", wallet._gas_price("0.0025uaxm") ==
      (wallet.Decimal("0.0025"), "uaxm"))
try:
    wallet._gas_price("дёшево")
    check("кривая цена газа — отказ", False)
except ValueError:
    check("кривая цена газа — отказ", True)

print()
if FAILS:
    print(f"НЕ ПРОШЛО: {len(FAILS)}")
    sys.exit(1)
print("подпись без axmd ведёт себя как задумано")
