#!/usr/bin/env python3
"""Подпись и отправка сделок без `axmd`: Python и LCD.

Зачем. `tx.Axmd` отправляет сделку через утилиту цепочки, а значит, на
машине агента нужны `axmd`, `libwasmvm` и связка ключей. Для узла это
терпимо: всё это уже лежит в его образе. Агенту хватает трёх вещей:
ключа, Python и доступа к LCD. Поэтому здесь та же отправка собрана
вручную, на стандартной библиотеке:

  * сделка кодируется protobuf руками: `MsgExecuteContract`, `TxBody`,
    `AuthInfo`, `SignDoc`. Полей немного, и все они перечислены ниже;
  * подпись `SIGN_MODE_DIRECT` — secp256k1 над sha256(SignDoc), тот же
    `sign.sign_digest`, что подписывает запросы к узлам;
  * газ берётся симуляцией (`/cosmos/tx/v1beta1/simulate`), а сделка
    уходит `BROADCAST_MODE_SYNC`, после чего ждём её в блоке.

Интерфейс тот же, что у `tx.Axmd` (`address`, `send`, `wait_tx`), и те
же гарантии: `send` возвращается только с `code=0` в блоке, а любой
отказ — `tx.TxFailed` с текстом цепочки. Поэтому `tx.classify` отличает
гонку от поломки так же, как прежде.

Байты сверены с живой цепочкой: сделка, отправленная через `axmd`,
пересобирается здесь байт в байт, и её подпись сходится с нашим
SignDoc (test_wallet.py).
"""

import base64
import hashlib
import hmac
import json
import math
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation

import sign
import tx

MSG_EXECUTE = "/cosmwasm.wasm.v1.MsgExecuteContract"
PUBKEY_TYPE = "/cosmos.crypto.secp256k1.PubKey"
SIGN_MODE_DIRECT = 1

#: Путь ключа Axiome: монета 546 по SLIP-44. Тот же адрес, что в Axiome
#: Wallet, если фраза та же; `keytool.py` печатает адрес для сверки.
AXIOME_PATH = "m/44'/546'/0'/0/0"


# --- protobuf -------------------------------------------------------------
#
# Только то, что нужно сделке. Поля с нулевым значением не пишутся: так
# кодирует и сама цепочка, а SignDoc подписывается по байтам, поэтому
# лишний ноль дал бы чужую подпись.

def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _bytes(field, data):
    if not data:
        return b""
    return _varint(field << 3 | 2) + _varint(len(data)) + data


def _str(field, s):
    return _bytes(field, s.encode())


def _uint(field, n):
    if not n:
        return b""
    return _varint(field << 3) + _varint(n)


def _msg(field, data):
    """Вложенное сообщение пишется и пустым: у `ModeInfo.single` это
    единственный признак выбранного варианта."""
    return _varint(field << 3 | 2) + _varint(len(data)) + data


def coin(denom, amount):
    return _str(1, denom) + _str(2, str(int(amount)))


def any_(type_url, value):
    return _str(1, type_url) + _bytes(2, value)


def parse_coins(funds):
    """`"10485uaxm"` или `"1uaxm,2ubartr"` в [(denom, amount)].

    Порядок — по имени монеты: цепочка отвергает неотсортированный набор.
    """
    if not funds:
        return []
    out = []
    for part in str(funds).split(","):
        m = re.fullmatch(r"\s*(\d+)\s*([a-zA-Z][a-zA-Z0-9/:._-]{2,127})\s*",
                         part)
        if not m:
            raise tx.TxFailed(f"не понял сумму {part!r}: нужно вида 10485uaxm")
        if int(m.group(1)):
            out.append((m.group(2), int(m.group(1))))
    return sorted(out)


def execute_msg(sender, contract, msg, funds=()):
    """`MsgExecuteContract`. `msg` уже байтами: подписываются именно они."""
    body = _str(1, sender) + _str(2, contract) + _bytes(3, msg)
    for d, a in funds:
        body += _msg(5, coin(d, a))
    return body


def tx_body(messages, memo=""):
    out = b"".join(_msg(1, any_(t, v)) for t, v in messages)
    return out + _str(2, memo)


def auth_info(pub, sequence, gas, fee_coins, with_key=True):
    """`AuthInfo` с одним подписантом. Без ключа — для симуляции адреса,
    чей ключ цепочке уже известен (так проверяется кодирование без
    денег на счету)."""
    pk = _msg(1, any_(PUBKEY_TYPE, _bytes(1, pub))) if with_key else b""
    mode = _msg(2, _msg(1, _uint(1, SIGN_MODE_DIRECT)))
    signer = pk + mode + _uint(3, sequence)
    fee = b"".join(_msg(1, coin(d, a)) for d, a in fee_coins) + _uint(2, gas)
    return _msg(1, signer) + _msg(2, fee)


def sign_doc(body, info, chain_id, account_number):
    return (_bytes(1, body) + _bytes(2, info) + _str(3, chain_id)
            + _uint(4, account_number))


def tx_raw(body, info, signature):
    # Пустая подпись (симуляция) всё равно пишется: число подписей должно
    # совпасть с числом подписантов.
    return (_bytes(1, body) + _bytes(2, info)
            + _varint(3 << 3 | 2) + _varint(len(signature)) + signature)


def compact(msg):
    """Сообщение контракту байтами — так же, как его отдаёт `tx.Axmd`."""
    if isinstance(msg, (bytes, bytearray)):
        return bytes(msg)
    return json.dumps(msg, ensure_ascii=False,
                      separators=(",", ":")).encode()


# --- ключ -----------------------------------------------------------------

def mnemonic_seed(words, passphrase=""):
    """BIP-39: фраза в семя. Контрольная сумма фразы здесь не сверяется
    (для неё нужен словарь); опечатка даст другой адрес, поэтому адрес
    печатается и сверяется с кошельком."""
    m = unicodedata.normalize("NFKD", " ".join(words.split()))
    s = unicodedata.normalize("NFKD", "mnemonic" + passphrase)
    return hashlib.pbkdf2_hmac("sha512", m.encode(), s.encode(), 2048)


def derive(seed, path=AXIOME_PATH):
    """BIP-32 для secp256k1: закрытый ключ по пути `m/44'/546'/0'/0/0`."""
    i = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
    k, chain = int.from_bytes(i[:32], "big"), i[32:]
    parts = path.split("/")
    if parts[0] != "m":
        raise ValueError(f"путь начинается с m: {path!r}")
    for p in parts[1:]:
        hard = p.endswith("'") or p.endswith("h")
        idx = int(p.rstrip("'h")) + (0x80000000 if hard else 0)
        if hard:
            data = b"\x00" + k.to_bytes(32, "big")
        else:
            data = sign.pubkey(k.to_bytes(32, "big"))
        i = hmac.new(chain, data + idx.to_bytes(4, "big"),
                     hashlib.sha512).digest()
        t = int.from_bytes(i[:32], "big")
        if t >= sign.N:
            raise ValueError("непригодный шаг пути, возьмите другой индекс")
        k, chain = (t + k) % sign.N, i[32:]
        if k == 0:
            raise ValueError("непригодный шаг пути, возьмите другой индекс")
    return k.to_bytes(32, "big")


def key_from_mnemonic(words, passphrase="", path=AXIOME_PATH):
    return derive(mnemonic_seed(words, passphrase), path)


# --- отправка -------------------------------------------------------------

def _gas_price(s):
    m = re.fullmatch(r"\s*([0-9.]+)\s*([a-zA-Z][a-zA-Z0-9/:._-]{2,127})\s*",
                     s or "")
    try:
        amount = Decimal(m.group(1)) if m else None
    except InvalidOperation:
        amount = None
    if amount is None or amount < 0:
        raise ValueError(f"цена газа вида 0.0025uaxm, а не {s!r}")
    return amount, m.group(2)


class Wallet:
    """Отправитель сделок с ключом в памяти. Замена `tx.Axmd`.

    Последовательность (`sequence`) ведётся здесь же: сделка, ушедшая в
    очередь, ещё не видна в счёте цепочки, и следующая, взяв номер из
    цепочки, получила бы отказ. Поэтому номер растёт локально после
    принятой сделки и перечитывается после отказа «sequence mismatch».
    """

    def __init__(self, contract, key, rest="https://axm-lcd.trickle.pro",
                 chain_id="axiome-1", gas_prices="0.0025uaxm",
                 gas_adjustment=1.3, hrp="axm", wait=90, timeout=20,
                 http=None, sleep=time.sleep):
        self.contract = contract
        self.key = key
        self.pub = sign.pubkey(key)
        self.hrp = hrp
        self.rest = rest.rstrip("/")
        self.chain_id = chain_id
        self.price, self.fee_denom = _gas_price(gas_prices)
        self.gas_adjustment = float(gas_adjustment)
        self.wait = wait
        self.timeout = timeout
        self.http = http or self._urllib
        self.sleep = sleep
        self._lock = threading.Lock()
        self._account = None      # (account_number, sequence)
        self._checked_chain = False

    # --- HTTP -------------------------------------------------------------

    def _urllib(self, method, path, body=None):
        """(код, разобранный JSON). Сетевой сбой — `TxFailed` со словом
        connection/timeout: `tx.classify` отнесёт его к повторяемым."""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.rest + path, data=data, method=method,
            headers={"Content-Type": "application/json",
                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                code, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            code, raw = e.code, e.read()
        except (urllib.error.URLError, OSError) as e:
            raise tx.TxFailed(f"connection: LCD {self.rest} не ответил: "
                              f"{getattr(e, 'reason', e)}")
        try:
            return code, json.loads(raw or b"{}")
        except ValueError:
            return code, {"message": raw[:300].decode(errors="replace")}

    # --- счёт -------------------------------------------------------------

    def address(self):
        return sign.address(self.pub, self.hrp)

    def _check_chain(self):
        """Чужой chain-id дал бы подпись, которую цепочка отвергнет с
        невнятным «signature verification failed». Сверяем заранее."""
        if self._checked_chain:
            return
        code, d = self.http(
            "GET", "/cosmos/base/tendermint/v1beta1/node_info")
        net = ((d or {}).get("default_node_info") or {}).get("network")
        if code == 200 and net and net != self.chain_id:
            raise tx.TxFailed(f"LCD {self.rest} — сеть {net}, а подписываем "
                              f"для {self.chain_id}")
        self._checked_chain = code == 200

    def account(self):
        """(account_number, sequence) из цепочки."""
        a = self.address()
        code, d = self.http("GET", f"/cosmos/auth/v1beta1/accounts/{a}")
        if code == 404 or "not found" in str(d.get("message", "")):
            raise tx.TxFailed(f"счёта {a} в цепочке нет: на него ещё не "
                              f"приходили монеты. Пополните кошелёк агента")
        if code != 200:
            raise tx.TxFailed(f"счёт {a} не прочитан: HTTP {code} "
                              f"{str(d.get('message', ''))[:200]}")
        acc = d.get("account") or {}
        # Счёт с графиком выдачи (vesting) держит номер внутри base_account.
        while "account_number" not in acc and isinstance(
                acc.get("base_account") or acc.get("base_vesting_account"),
                dict):
            acc = acc.get("base_account") or acc.get("base_vesting_account")
        return int(acc.get("account_number", 0)), int(acc.get("sequence", 0))

    # --- сборка -----------------------------------------------------------

    def _build(self, msg, funds, contract, sequence, gas, account_number,
               simulate=False, with_key=True):
        body = tx_body([(MSG_EXECUTE, execute_msg(
            self.address(), contract or self.contract, compact(msg),
            parse_coins(funds)))])
        fee = math.ceil(Decimal(gas) * self.price) if gas else 0
        info = auth_info(self.pub, sequence, gas,
                         [(self.fee_denom, fee)] if fee else [],
                         with_key=with_key)
        if simulate:
            return tx_raw(body, info, b"")
        digest = hashlib.sha256(
            sign_doc(body, info, self.chain_id, account_number)).digest()
        return tx_raw(body, info, sign.sign_digest(self.key, digest))

    def simulate(self, msg, funds=None, contract=None, sequence=None,
                 with_key=True):
        """Газ, который сделка съест. Отказ контракта виден уже здесь, и
        его текст уходит в `TxFailed` как есть — это обычный путь гонки."""
        if sequence is None:
            sequence = self.account()[1]
        raw = self._build(msg, funds, contract, sequence, 0, 0,
                          simulate=True, with_key=with_key)
        code, d = self.http("POST", "/cosmos/tx/v1beta1/simulate",
                            {"tx_bytes": base64.b64encode(raw).decode()})
        if code != 200:
            raise tx.TxFailed(str(d.get("message") or d)[:400])
        return int((d.get("gas_info") or {}).get("gas_used", 0))

    # --- отправка ---------------------------------------------------------

    def send(self, msg, funds=None, contract=None):
        """Отправить и ДОЖДАТЬСЯ `code=0` в блоке. Иначе — `TxFailed`."""
        with self._lock:
            self._check_chain()
            for attempt in range(2):
                if self._account is None:
                    self._account = self.account()
                number, seq = self._account
                try:
                    gas = self.simulate(msg, funds, contract, sequence=seq)
                    gas = max(int(gas * self.gas_adjustment), 50000)
                    raw = self._build(msg, funds, contract, seq, gas, number)
                    h = self._broadcast(raw)
                except tx.TxFailed as e:
                    expected = re.search(r"expected (\d+), got (\d+)", str(e))
                    if expected and attempt == 0:
                        self._account = (number, int(expected.group(1)))
                        continue
                    # Номер мог разойтись с цепочкой (сетевой сбой посреди
                    # отправки): следующий раз перечитать его заново.
                    if "sequence" in str(e) or "connection" in str(e):
                        self._account = None
                    raise
                self._account = (number, seq + 1)
                break
        return self.wait_tx(h)

    def _broadcast(self, raw):
        code, d = self.http("POST", "/cosmos/tx/v1beta1/txs", {
            "tx_bytes": base64.b64encode(raw).decode(),
            "mode": "BROADCAST_MODE_SYNC"})
        r = d.get("tx_response") or {}
        if code != 200 or not r:
            raise tx.TxFailed(str(d.get("message") or d)[:400])
        if int(r.get("code", 0)) != 0:
            raise tx.TxFailed(f"code={r.get('code')}: "
                              f"{str(r.get('raw_log', ''))[:400]}")
        if not r.get("txhash"):
            raise tx.TxFailed(f"в ответе нет txhash: {str(r)[:200]}")
        return r["txhash"]

    def wait_tx(self, txhash):
        """Сделка в блоке с `code=0`. Возврат как у `tx.Axmd.wait_tx`:
        в `tx` — ответ цепочки с событиями, из них берётся номер задания."""
        deadline = time.time() + self.wait
        last = ""
        while time.time() < deadline:
            self.sleep(2)
            try:
                code, d = self.http("GET", f"/cosmos/tx/v1beta1/txs/{txhash}")
            except tx.TxFailed as e:
                # Сделка уже ушла: недоступный LCD не повод считать её
                # провалившейся.
                last = str(e)
                continue
            r = d.get("tx_response")
            if code != 200 or not r:
                last = str(d.get("message", ""))[:200]
                continue
            if int(r.get("code", 0)) != 0:
                raise tx.TxFailed(f"{txhash}: code={r.get('code')}: "
                                  f"{str(r.get('raw_log', ''))[:400]}")
            return {"txhash": txhash, "height": r.get("height"), "code": 0,
                    "tx": r}
        raise tx.TxFailed(f"{txhash}: за {self.wait} с не попала в блок. "
                          f"Последнее: {last[:200]}")
