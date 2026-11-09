#!/usr/bin/env python3
"""Подпись запроса и допуск к тексту.

Своя криптография проверяется не сама собой, а против чужой:

  * RIPEMD-160 на чистом Python — против `hashlib` на сотне длин, включая
    границы блока 55/56/64;
  * подпись — против `cryptography`, в обе стороны: их подпись сходится у
    нас, наша — у них; RFC 6979 — против их детерминированной подписи;
  * адрес — против того, что выдаёт `axmd keys import-hex` (запускается
    отдельно, см. ADDR_VECTORS).

Допуск — на подставной цепочке: кто что вправе читать в каком состоянии
задания, и что горячий ключ работает за холодный только при праве на
ЭТОТ реестр.

Запуск:  python protocol/daemon/test_sign.py
"""

import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sign  # noqa: E402

fails = []


def ok(cond, what):
    print(f"  {'OK  ' if cond else 'ПЛОХО'} {what}")
    if not cond:
        fails.append(what)


#: Ключ → адрес, выданный `axmd keys import-hex` 9 ноября на одноразовой
#: связке. Ключи случайные и ничего не держат.
ADDR_VECTORS = [
    ("5fba5c2958cb90c85e346b386775e25d1800d77e1b8a8bee5f2f951ac25a3825",
     "axm1uhw8lgx0d3pfrz4tt8vkmsvlty6gx2l4u9rqu5"),
    ("e4e8453cd417bc803a4c0e58b78e3858d78e16d77c143b28867dadee5194b83f",
     "axm1hqy9k7xvgy9e2sz2qp9qt29qlh0l5slnlqwd38"),
    ("51c47a3ffe5f4b679bf8663e9f5dd8273c53fc8d0fa37c1efa1bf4af932b7f23",
     "axm14ywxhzysc0z3m0yy7kalfykh9qx6ft3hlfh5f0"),
]


def test_ripemd():
    print("### RIPEMD-160")
    ok(sign._ripemd160_py(b"").hex()
       == "9c1185a5c5e9fc54612808977ee8f548b2258d31", "пустая строка")
    ok(sign._ripemd160_py(b"abc").hex()
       == "8eb208f7e05d987a9b044a8e98c6b087f15a0bfc", "abc")
    try:
        hashlib.new("ripemd160")
    except ValueError:
        print("  (в этом hashlib RIPEMD-160 нет — сверка с ним пропущена)")
        return
    bad = []
    for n in list(range(0, 130)) + [1000]:
        m = os.urandom(n)
        if sign._ripemd160_py(m) != hashlib.new("ripemd160", m).digest():
            bad.append(n)
    ok(not bad, f"совпадает с hashlib на 131 длине (расхождения: {bad})")


def test_curve():
    print("\n### secp256k1")
    ok(sign.pubkey((1).to_bytes(32, "big")).hex().upper()
       == "0279BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798",
       "ключ 1 даёт точку G")
    priv = os.urandom(32)
    pub = sign.pubkey(priv)
    z = hashlib.sha256(b"x").digest()
    sig = sign.sign_digest(priv, z)
    ok(sign.verify_digest(pub, z, sig), "своя подпись сходится")
    ok(not sign.verify_digest(pub, hashlib.sha256(b"y").digest(), sig),
       "чужой дайджест — нет")
    ok(int.from_bytes(sig[32:], "big") <= sign.N // 2, "s в нижней половине")
    ok(sign.sign_digest(priv, z) == sig, "подпись детерминирована")
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, utils
    except ImportError:
        print("  (cryptography нет — сверка с ней пропущена)")
        return
    key = ec.derive_private_key(int.from_bytes(priv, "big"), ec.SECP256K1())
    theirs = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)
    ok(theirs == pub, "открытый ключ совпадает с cryptography")
    r, s = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
    try:
        key.public_key().verify(utils.encode_dss_signature(r, s), z,
                                ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        ok(True, "наша подпись сходится у cryptography")
    except Exception as e:
        ok(False, f"наша подпись сходится у cryptography: {e}")
    der = key.sign(z, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
    r2, s2 = utils.decode_dss_signature(der)
    ok(sign.verify_digest(pub, z, r2.to_bytes(32, "big") + s2.to_bytes(32, "big")),
       "подпись cryptography сходится у нас")
    try:
        det = key.sign(z, ec.ECDSA(utils.Prehashed(hashes.SHA256()),
                                   deterministic_signing=True))
    except TypeError:
        print("  (детерминированной подписи в этой cryptography нет)")
        return
    r3, s3 = utils.decode_dss_signature(det)
    s3 = min(s3, sign.N - s3)
    ok((r3, s3) == (r, s), "RFC 6979: та же подпись, что у cryptography")


def test_address():
    print("\n### адрес")
    for priv_hex, addr in ADDR_VECTORS:
        ok(sign.address(sign.pubkey(bytes.fromhex(priv_hex))) == addr,
           f"адрес как у axmd: {addr[:12]}…")
    if not ADDR_VECTORS:
        print("  (векторов axmd нет)")


def test_request():
    print("\n### запрос")
    priv = os.urandom(32)
    me = sign.address(sign.pubkey(priv))
    h = sign.headers_for(priv, "http://bot:8080/" + "a" * 64 + ".txt", now=1000)
    path = "/" + "a" * 64 + ".txt"
    ok(sign.requester(h, path, now=1000) == me, "подпись даёт адрес")
    for name, args in (("другой путь", ("/" + "b" * 64 + ".txt", 1000)),
                       ("старая подпись", (path, 1000 + sign.SKEW + 1))):
        try:
            sign.requester(h, *args)
            ok(False, f"{name} отвергается")
        except sign.BadSignature:
            ok(True, f"{name} отвергается")
    try:
        sign.requester({}, path)
        ok(False, "без подписи — Unsigned")
    except sign.Unsigned:
        ok(True, "без подписи — Unsigned")
    forged = dict(h, **{sign.H_KEY: sign.pubkey(os.urandom(32)).hex()})
    try:
        sign.requester(forged, path, now=1000)
        ok(False, "чужой ключ под нашей подписью отвергается")
    except sign.BadSignature:
        ok(True, "чужой ключ под нашей подписью отвергается")


def test_seal():
    print("\n### шифрование ответа")
    priv, other = os.urandom(32), os.urandom(32)
    pub = sign.pubkey(priv)
    for n in (0, 1, 31, 32, 33, 1000, 70_000):
        m = os.urandom(n)
        if sign.open_sealed(priv, sign.seal(pub, m)) != m:
            ok(False, f"вскрывается своим ключом, длина {n}")
            break
    else:
        ok(True, "вскрывается своим ключом на длинах 0…70 000")
    text = "тайный вопрос".encode()
    a, b = sign.seal(pub, text), sign.seal(pub, text)
    ok(a != b, "разовый ключ: один текст — разные шифры")
    ok(text not in a, "открытого текста в шифре нет")
    for name, blob, key in (
            ("чужим ключом", a, other),
            ("с подменённым байтом", a[:40] + bytes([a[40] ^ 1]) + a[41:], priv),
            ("с подменённой меткой", a[:-1] + bytes([a[-1] ^ 1]), priv),
            ("обрезанный", a[:20], priv),
            ("не шифр", b"{}", priv)):
        try:
            sign.open_sealed(key, blob)
            ok(False, f"{name} не вскрывается")
        except sign.SealError:
            ok(True, f"{name} не вскрывается")
    # Соответствие схеме, а не только самой себе: общий секрет обязан
    # совпасть с ECDH из cryptography.
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError:
        print("  (cryptography нет — сверка ECDH пропущена)")
        return
    e = os.urandom(32)
    mine = sign._mul(int.from_bytes(e, "big"), sign._decompress(pub))[0]
    k1 = ec.derive_private_key(int.from_bytes(e, "big"), ec.SECP256K1())
    k2 = ec.derive_private_key(int.from_bytes(priv, "big"), ec.SECP256K1())
    ok(k1.exchange(ec.ECDH(), k2.public_key()) == mine.to_bytes(32, "big"),
       "общий секрет совпадает с ECDH cryptography")


class FakeChain:
    def __init__(self, executors):
        self.executors = executors

    def load_executor(self, rest, contract, addr):
        return self.executors.get(addr)

    def load_job(self, rest, contract, job_id):
        raise AssertionError("не нужен")


def test_access():
    print("\n### допуск")
    ex, cl, ch, out, hot = "axm1ex", "axm1client", "axm1ch", "axm1out", "axm1hot"
    fc = FakeChain({ex: {"status": "active"}, ch: {"status": "active"},
                    "axm1banned": {"status": "banned"}})
    a = sign.Access("rest", "axm1reg", fc,
                    granters=lambda g: {ex} if g == hot else set())

    def may(what, st, who):
        return a.may_read(what, {"state": st, "executor": ex,
                                 "client": cl}, who)[0]

    ok(not may("prompt", "open", ex), "открытое: вопрос не отдаётся никому")
    ok(may("prompt", "claimed", ex), "взятое: исполнителю — да")
    ok(not may("prompt", "claimed", ch), "взятое: другому узлу — нет")
    ok(may("prompt", "answered", ch), "сданное: действующему узлу — да")
    ok(not may("prompt", "answered", "axm1banned"), "забаненному — нет")
    ok(not may("prompt", "answered", out), "постороннему — нет")
    ok(not may("prompt", "answered", cl), "заказчику вопрос не нужен")
    ok(may("prompt", "claimed", hot), "горячий ключ исполнителя — да")
    ok(may("bundle", "answered", cl), "связка: заказчику — да")
    ok(may("bundle", "settled", cl), "закрытое: заказчику — да")
    ok(not may("bundle", "settled", ch), "закрытое: узлу — нет")
    ok(may("bundle", "answered", ch), "проверка: узлу — да")
    ok(not may("bundle", "answered", out), "постороннему — нет")
    b = sign.Access("rest", "axm1reg", fc, granters=lambda g: set())
    ok(not b.may_read("prompt", {"state": "claimed", "executor": ex},
                      hot)[0], "без права на реестр горячий ключ — никто")


def test_bundle_server():
    print("\n### раздача связок узла")
    import http.server
    import tempfile
    import threading
    import urllib.error
    import urllib.request
    import delivery
    import serve_bundles

    cl_key, ch_key, out_key = os.urandom(32), os.urandom(32), os.urandom(32)
    cl = sign.address(sign.pubkey(cl_key))
    ch = sign.address(sign.pubkey(ch_key))
    now = [10_000.0]
    jobs = {5: {"state": "answered", "executor": "axm1ex", "client": cl,
                "answered_at": 9_000}}

    class C(FakeChain):
        def load_job(self, rest, contract, job_id):
            return dict(jobs[int(job_id)])

    access = sign.Access("rest", "axm1reg", C({ch: {"status": "active"}}),
                         ttl=0, now=lambda: now[0],
                         granters=lambda g: set())
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "5.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"prompt": "тайна", "answer": "ответ"}')
        srv = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), serve_bundles.make_handler(
                d, access, retain=3600, now=lambda: now[0], log=lambda s: None))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        def get(name, key=None):
            url = f"{base}/{name}"
            h = sign.headers_for(key, url, now=now[0]) if key else {}
            try:
                return urllib.request.urlopen(
                    urllib.request.Request(url, headers=h)).status
            except urllib.error.HTTPError as e:
                return e.code

        ok(get("") == 200, "/ отвечает: узел жив")
        ok(get("0.json") == 404, "нет файла — 404, как ждёт bot.reachable")
        ok(get("5.json") == 401, "без подписи — 401")
        ok(get("5.json", out_key) == 403, "постороннему — 403")
        ok(get("5.json", cl_key) == 200, "заказчику — да")
        ok(get("5.json", ch_key) == 200, "действующему узлу на проверке — да")
        ok(get("../5.json", cl_key) == 404 and get("x.json", cl_key) == 404,
           "чужие имена не отдаются")
        url = f"{base}/5.json"
        r = urllib.request.urlopen(urllib.request.Request(
            url, headers=sign.headers_for(cl_key, url, now=now[0])))
        raw_body = r.read()
        ok(r.headers.get(sign.H_SEALED) == "1"
           and "тайна".encode() not in raw_body,
           "по проводу идёт шифр, а не текст")
        ok(b"\xd1\x82\xd0\xb0\xd0\xb9" not in raw_body
           and "тайна" in sign.open_sealed(cl_key, raw_body).decode(),
           "вскрывает его ключ спросившего")
        try:
            sign.open_sealed(ch_key, raw_body)
            ok(False, "другой узел этот шифр не вскроет")
        except sign.SealError:
            ok(True, "другой узел этот шифр не вскроет")

        delivery.set_sign_key(cl_key)
        real_time = sign.time.time
        sign.time.time = lambda: now[0]
        try:
            raw = delivery.read_source(base, "5.json")
        finally:
            sign.time.time = real_time
            delivery.set_sign_key(None)
        ok("тайна" in raw, "delivery с ключом подписи забирает связку")

        jobs[5]["state"] = "settled"
        ok(get("5.json", ch_key) == 403, "закрытое: узлу — уже нет")
        ok(get("5.json", cl_key) == 200, "закрытое: заказчику — ещё да")
        now[0] = 9_000 + 3601
        ok(get("5.json", cl_key) == 404 and not os.path.exists(path),
           "после срока хранения связка стёрта")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")
        ok(serve_bundles.sweep(d, access, 3600, now=lambda: now[0]) == 1
           and not os.path.exists(path), "обход стирает закрытые")
        srv.shutdown()


def main():
    test_bundle_server()
    test_ripemd()
    test_curve()
    test_address()
    test_request()
    test_seal()
    test_access()
    print("\nподпись и допуск ведут себя как задумано" if not fails
          else f"\nПРОВАЛОВ: {len(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
