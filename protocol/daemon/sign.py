#!/usr/bin/env python3
"""Кто спрашивает текст: подпись запроса и правило допуска.

Зачем. Хэш вопроса и номер задания лежат в цепочке открыто. Пока бот
раздавал `<хэш>.txt`, а узел — `<номер>.txt` со связкой (вопрос плюс
ответ) любому, кто спросит, все вопросы и ответы сети мог скачать любой
читатель цепочки. Сверка с хэшем защищает от ПОДМЕНЫ текста, но не от
чтения. Теперь читать вправе только участники задания:

  * вопрос (`<хэш>.txt` у бота) — исполнитель задания, а когда ответ
    сдан — и любой действующий узел реестра: проверяющих заранее не
    знает никто;
  * связку (`<номер>.json` у исполнителя) — заказчик задания и, пока
    идёт проверка, действующие узлы реестра.

Кто спрашивает, видно по подписи: запрос несёт открытый ключ, время и
подпись secp256k1 над путём и временем, а адрес `axm1…` выводится из
ключа так же, как его выводит `axmd`. Горячий ключ (раздел 9 УЗЕЛ.md)
работает через authz: допуск есть, если холодный адрес выдал ему право
на этот реестр.

Чего подпись НЕ даёт. Это допуск, а не шифрование: по обычному HTTP текст
по-прежнему виден тому, кто видит трафик, и он же может повторить
перехваченный запрос в пределах `SKEW` секунд — получив то, что и так
видел. От этого спасает только HTTPS. И узел задания видит текст всегда:
он его считает.

Криптография своя, на стандартной библиотеке: в образе узла и на сервере
бота не должно быть зависимостей сверх Python. Сверено с `cryptography`
и с адресами, которые выдаёт `axmd` (test_sign.py).
"""

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

# --- secp256k1 ------------------------------------------------------------

P = 2**256 - 2**32 - 977
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0]:
        if (a[1] + b[1]) % P == 0:
            return None
        lam = 3 * a[0] * a[0] * pow(2 * a[1], -1, P) % P
    else:
        lam = (b[1] - a[1]) * pow(b[0] - a[0], -1, P) % P
    x = (lam * lam - a[0] - b[0]) % P
    return x, (lam * (a[0] - x) - a[1]) % P


def _mul(k, pt):
    out = None
    while k:
        if k & 1:
            out = _add(out, pt)
        pt = _add(pt, pt)
        k >>= 1
    return out


def pubkey(priv: bytes) -> bytes:
    """Сжатый открытый ключ, 33 байта."""
    d = int.from_bytes(priv, "big")
    if not 0 < d < N:
        raise ValueError("ключ вне допустимого диапазона")
    x, y = _mul(d, G)
    return bytes([2 + (y & 1)]) + x.to_bytes(32, "big")


def _decompress(pub: bytes):
    if len(pub) != 33 or pub[0] not in (2, 3):
        raise ValueError("нужен сжатый ключ, 33 байта")
    x = int.from_bytes(pub[1:], "big")
    if x >= P:
        raise ValueError("ключ вне поля")
    y2 = (pow(x, 3, P) + 7) % P
    y = pow(y2, (P + 1) // 4, P)
    if y * y % P != y2:
        raise ValueError("точки нет на кривой")
    if (y & 1) != (pub[0] & 1):
        y = P - y
    return x, y


def _rfc6979(d: int, z: bytes) -> int:
    x = d.to_bytes(32, "big")
    h = (int.from_bytes(z, "big") % N).to_bytes(32, "big")
    v, k = b"\x01" * 32, b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + x + h, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + x + h, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        t = int.from_bytes(v, "big")
        if 0 < t < N:
            return t
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def sign_digest(priv: bytes, z: bytes) -> bytes:
    """Подпись r‖s (64 байта) над 32-байтным дайджестом, s в нижней половине."""
    d = int.from_bytes(priv, "big")
    e = int.from_bytes(z, "big")
    k = _rfc6979(d, z)
    r = _mul(k, G)[0] % N
    s = pow(k, -1, N) * (e + r * d) % N
    if s > N // 2:
        s = N - s
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def verify_digest(pub: bytes, z: bytes, sig: bytes) -> bool:
    if len(sig) != 64:
        return False
    r = int.from_bytes(sig[:32], "big")
    s = int.from_bytes(sig[32:], "big")
    if not (0 < r < N and 0 < s < N):
        return False
    try:
        q = _decompress(pub)
    except ValueError:
        return False
    w = pow(s, -1, N)
    pt = _add(_mul(int.from_bytes(z, "big") * w % N, G), _mul(r * w % N, q))
    return pt is not None and pt[0] % N == r


# --- адрес ----------------------------------------------------------------

def _ripemd160(b: bytes) -> bytes:
    try:
        return hashlib.new("ripemd160", b).digest()
    except ValueError:
        return _ripemd160_py(b)


def _ripemd160_py(msg: bytes) -> bytes:
    """RIPEMD-160 на чистом Python: OpenSSL 3 прячет его в «legacy»."""
    def rol(x, n):
        return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF

    f = (lambda x, y, z: x ^ y ^ z,
         lambda x, y, z: (x & y) | (~x & z),
         lambda x, y, z: (x | ~y) ^ z,
         lambda x, y, z: (x & z) | (y & ~z),
         lambda x, y, z: x ^ (y | ~z))
    kl = (0, 0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xA953FD4E)
    kr = (0x50A28BE6, 0x5C4DD124, 0x6D703EF3, 0x7A6D76E9, 0)
    rl = [list(range(16)),
          [7, 4, 13, 1, 10, 6, 15, 3, 12, 0, 9, 5, 2, 14, 11, 8],
          [3, 10, 14, 4, 9, 15, 8, 1, 2, 7, 0, 6, 13, 11, 5, 12],
          [1, 9, 11, 10, 0, 8, 12, 4, 13, 3, 7, 15, 14, 5, 6, 2],
          [4, 0, 5, 9, 7, 12, 2, 10, 14, 1, 3, 8, 11, 6, 15, 13]]
    rr = [[5, 14, 7, 0, 9, 2, 11, 4, 13, 6, 15, 8, 1, 10, 3, 12],
          [6, 11, 3, 7, 0, 13, 5, 10, 14, 15, 8, 12, 4, 9, 1, 2],
          [15, 5, 1, 3, 7, 14, 6, 9, 11, 8, 12, 2, 10, 0, 4, 13],
          [8, 6, 4, 1, 3, 11, 15, 0, 5, 12, 2, 13, 9, 7, 10, 14],
          [12, 15, 10, 4, 1, 5, 8, 7, 6, 2, 13, 14, 0, 3, 9, 11]]
    sl = [[11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8],
          [7, 6, 8, 13, 11, 9, 7, 15, 7, 12, 15, 9, 11, 7, 13, 12],
          [11, 13, 6, 7, 14, 9, 13, 15, 14, 8, 13, 6, 5, 12, 7, 5],
          [11, 12, 14, 15, 14, 15, 9, 8, 9, 14, 5, 6, 8, 6, 5, 12],
          [9, 15, 5, 11, 6, 8, 13, 12, 5, 12, 13, 14, 11, 8, 5, 6]]
    sr = [[8, 9, 9, 11, 13, 15, 15, 5, 7, 7, 8, 11, 14, 14, 12, 6],
          [9, 13, 15, 7, 12, 8, 9, 11, 7, 7, 12, 7, 6, 15, 13, 11],
          [9, 7, 15, 11, 8, 6, 6, 14, 12, 13, 5, 14, 13, 13, 7, 5],
          [15, 5, 8, 11, 14, 14, 6, 14, 6, 9, 12, 9, 12, 5, 15, 8],
          [8, 5, 12, 9, 12, 5, 14, 6, 8, 13, 6, 5, 15, 13, 11, 11]]
    h = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0]
    m = msg + b"\x80" + b"\x00" * ((55 - len(msg)) % 64)
    m += (8 * len(msg) & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "little")
    for i in range(0, len(m), 64):
        x = [int.from_bytes(m[i + 4 * j:i + 4 * j + 4], "little")
             for j in range(16)]
        al, bl, cl, dl, el = h
        ar, br, cr, dr, er = h
        for j in range(5):
            for t in range(16):
                v = (al + (f[j](bl, cl, dl) & 0xFFFFFFFF) + x[rl[j][t]]
                     + kl[j]) & 0xFFFFFFFF
                al, bl, cl, dl, el = (el, (rol(v, sl[j][t]) + el)
                                      & 0xFFFFFFFF, bl, rol(cl, 10), dl)
                v = (ar + (f[4 - j](br, cr, dr) & 0xFFFFFFFF) + x[rr[j][t]]
                     + kr[j]) & 0xFFFFFFFF
                ar, br, cr, dr, er = (er, (rol(v, sr[j][t]) + er)
                                      & 0xFFFFFFFF, br, rol(cr, 10), dr)
        t = (h[1] + cl + dr) & 0xFFFFFFFF
        h[1] = (h[2] + dl + er) & 0xFFFFFFFF
        h[2] = (h[3] + el + ar) & 0xFFFFFFFF
        h[3] = (h[4] + al + br) & 0xFFFFFFFF
        h[4] = (h[0] + bl + cr) & 0xFFFFFFFF
        h[0] = t
    return b"".join(v.to_bytes(4, "little") for v in h)


_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values):
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def _bech32(hrp, data8):
    acc, bits, data = 0, 0, []
    for b in data8:
        acc = (acc << 8) | b
        bits += 8
        while bits >= 5:
            bits -= 5
            data.append((acc >> bits) & 31)
    if bits:
        data.append((acc << (5 - bits)) & 31)
    exp = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    pm = _polymod(exp + data + [0] * 6) ^ 1
    data += [(pm >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_CHARSET[d] for d in data)


def address(pub: bytes, hrp="axm") -> str:
    """Адрес аккаунта Cosmos: bech32(ripemd160(sha256(ключ)))."""
    return _bech32(hrp, _ripemd160(hashlib.sha256(pub).digest()))


# --- запрос ---------------------------------------------------------------

DOMAIN = b"verifiable-inference/fetch/1\n"
#: Насколько часы спрашивающего могут расходиться с нашими.
SKEW = 300
H_KEY, H_TIME, H_SIG = "X-VI-Key", "X-VI-Time", "X-VI-Sig"


def _digest(path, t):
    return hashlib.sha256(DOMAIN + path.encode() + b"\n" +
                          str(int(t)).encode()).digest()


def load_key(path):
    """Ключ из файла: 64 шестнадцатеричных знака, остальное отбрасывается."""
    with open(path, encoding="ascii", errors="ignore") as f:
        h = "".join(c for c in f.read() if c in "0123456789abcdefABCDEF")
    if len(h) != 64:
        raise ValueError(f"в {path} не ключ: {len(h)} знаков вместо 64")
    return bytes.fromhex(h)


def headers_for(priv, url, now=None):
    """Заголовки подписи для запроса по `url`. Подписывается ПУТЬ."""
    path = urllib.parse.urlsplit(url).path or "/"
    t = int(now if now is not None else time.time())
    return {H_KEY: pubkey(priv).hex(), H_TIME: str(t),
            H_SIG: sign_digest(priv, _digest(path, t)).hex()}


class Unsigned(Exception):
    """Подписи нет вовсе."""


class BadSignature(Exception):
    """Подпись есть, но не та."""


def requester(headers, path, now=None):
    """Адрес спрашивающего по заголовкам. Нет подписи — Unsigned."""
    k, t, s = (headers.get(H_KEY), headers.get(H_TIME), headers.get(H_SIG))
    if not (k or t or s):
        raise Unsigned("запрос без подписи")
    try:
        pub, sig, t = bytes.fromhex(k), bytes.fromhex(s), int(t)
    except (TypeError, ValueError):
        raise BadSignature("заголовки подписи не разобраны")
    now = time.time() if now is None else now
    if abs(now - t) > SKEW:
        raise BadSignature(f"время подписи расходится на {int(now - t)} с")
    if not verify_digest(pub, _digest(path, t), sig):
        raise BadSignature("подпись не сходится")
    return address(pub)


# --- кто вправе -----------------------------------------------------------

class Access:
    """Правило допуска по цепочке. Ответы цепочки кешируются ненадолго:
    один узел спрашивает текст несколько раз подряд, и каждый раз ходить
    в REST незачем."""

    def __init__(self, rest, contract, chainmod, ttl=20, now=time.time,
                 granters=None):
        self.rest, self.contract, self.chain = rest, contract, chainmod
        self.ttl, self.now = ttl, now
        self._granters = granters or self._authz_granters
        self.cache = {}

    def _cached(self, key, fn):
        hit = self.cache.get(key)
        if hit and self.now() - hit[0] < self.ttl:
            return hit[1]
        v = fn()
        self.cache[key] = (self.now(), v)
        return v

    def _authz_granters(self, grantee):
        """Холодные адреса, выдавшие `grantee` право на ЭТОТ реестр."""
        url = (f"{self.rest.rstrip('/')}/cosmos/authz/v1beta1/grants/"
               f"grantee/{grantee}")
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                d = json.loads(r.read())
        except (urllib.error.URLError, OSError, ValueError):
            return set()
        out = set()
        for g in d.get("grants") or []:
            auth = g.get("authorization") or {}
            for cg in auth.get("grants") or []:
                if cg.get("contract") == self.contract:
                    out.add(g.get("granter"))
        return out

    def identities(self, addr):
        """Сам адрес и все, за кого он работает по authz."""
        return {addr} | self._cached(("authz", addr),
                                     lambda: self._granters(addr))

    def active(self, addr):
        rec = self._cached(("exec", addr), lambda: self.chain.load_executor(
            self.rest, self.contract, addr))
        return bool(rec) and str(rec.get("status", "")).lower() == "active"

    def job(self, job_id):
        return self._cached(("job", int(job_id)), lambda: self.chain.load_job(
            self.rest, self.contract, int(job_id)))

    def may_read(self, what, job, addr):
        """(можно ли, почему). `what` — "prompt" или "bundle"."""
        st = str(job.get("state", "")).lower()
        me = self.identities(addr)
        if what == "prompt" and job.get("executor") in me \
                and st in ("claimed", "answered"):
            return True, "исполнитель задания"
        if what == "bundle" and job.get("client") in me \
                and st in ("answered", "settled"):
            return True, "заказчик задания"
        if st == "answered":
            checker = [a for a in me
                       if a != job.get("executor") and self.active(a)]
            if checker:
                return True, f"действующий узел {checker[0]}"
        return False, f"{addr} не участник задания в состоянии {st}"
