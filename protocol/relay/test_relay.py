#!/usr/bin/env python3
"""Посредник на подставной цепочке, через настоящий HTTP: `python3 test_relay.py`.

Проверяется то, ради чего он есть, и то, чем он мог бы навредить:

  * агент выкладывает текст исходящим подписанным PUT, узел забирает его
    ТЕМ ЖЕ кодом, что работает на узлах (`delivery.fetch_verified`), и
    получает шифр, который вскрывает только он;
  * текст раздаётся только исполнителю задания, а после сдачи ответа —
    действующим узлам; посторонний и неподписанный не получают ничего;
  * чужой текст не раздать под своим заданием: заказчик задания обязан
    совпасть с тем, кто выложил текст;
  * подмена текста, лишняя длина и засорение отвергаются;
  * закрытое и невостребованное стирается.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "daemon"))
import chain     # noqa: E402
import delivery  # noqa: E402
import relay     # noqa: E402
import sign      # noqa: E402

FAILS = []
REG = "axm1registry"


def check(name, ok, info=""):
    print(("ok   " if ok else "FAIL ") + name +
          ("" if ok or not info else f" — {info}"))
    if not ok:
        FAILS.append(name)


def key(n):
    return bytes([n]) * 32


def addr(k):
    return sign.address(sign.pubkey(k))


AGENT, NODE, CHECKER, STRANGER, OTHER = key(1), key(2), key(3), key(4), key(5)


class Chain:
    """Реестр: задания по номерам и записи узлов."""
    ChainUnavailable = chain.ChainUnavailable
    text_hash = staticmethod(chain.text_hash)

    def __init__(self):
        self.jobs = {}
        self.active = {addr(NODE), addr(CHECKER)}
        self.down = False
        self.pages = 0

    def add(self, prompt, client, state="open", executor=None):
        n = len(self.jobs) + 1
        self.jobs[n] = {"prompt_hash": chain.text_hash(prompt),
                        "client": addr(client), "state": state,
                        "executor": addr(executor) if executor else None}
        return n

    def jobs_page(self, rest, contract, start_after=None, limit=None):
        if self.down:
            raise chain.ChainUnavailable("LCD молчит")
        self.pages += 1
        ids = sorted(i for i in self.jobs if start_after is None
                     or i > start_after)[:limit or 30]
        return ([{"job_id": i, "job": self.jobs[i]} for i in ids],
                len(self.jobs) + 1)

    def load_job(self, rest, contract, job_id):
        if self.down:
            raise chain.ChainUnavailable("LCD молчит")
        return dict(self.jobs[int(job_id)])

    def load_executor(self, rest, contract, a):
        return {"status": "active"} if a in self.active else None


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def start(tmp, ch, clock, **cfgkw):
    env = {"DIR": os.path.join(tmp, "p"), "DB": os.path.join(tmp, "r.db"),
           "REGISTRIES": REG}
    env.update({k.upper(): str(v) for k, v in cfgkw.items()})
    cfg = relay.Config(env)
    acc = {REG: sign.Access("http://lcd", REG, ch, now=clock,
                            granters=lambda a: set())}
    r = relay.Relay(cfg, chainmod=ch, now=clock, access=acc)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), relay.make_handler(r))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return r, srv, f"http://127.0.0.1:{srv.server_address[1]}"


def get(url, k=None, now=None):
    h = sign.headers_for(k, url, now) if k else {}
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def put(url, body, k=None, now=None):
    h = sign.headers_for(k, url, now) if k else {}
    req = urllib.request.Request(url, data=body, headers=h, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main():
    tmp = tempfile.mkdtemp()
    try:
        run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if FAILS:
        print(f"НЕ ПРОШЛО: {len(FAILS)}")
        return 1
    print("посредник ведёт себя как задумано")
    return 0


def run(tmp):
    ch, clock = Chain(), Clock()
    # Часы посредника — подставные, а подпись запроса несёт настоящее
    # время: сверяем их.
    import time as _t
    clock.t = _t.time()
    r, srv, base = start(tmp, ch, clock)
    text = "<|im_start|>user\nСтолица Франции?<|im_end|>\n"
    h = chain.text_hash(text)
    url = f"{base}/{h}.txt"

    # --- выкладка -----------------------------------------------------------
    res = delivery.upload_prompt(base, AGENT, text)
    check("агент выкладывает текст подписанным PUT",
          res.get("ok") and res.get("owner") == addr(AGENT), res)
    check("файл лежит у посредника", os.path.isfile(r.path(h)))
    code, _ = put(url, text.encode())
    check("PUT без подписи — 401", code == 401, code)
    code, d = put(f"{base}/{'0' * 64}.txt", text.encode(), AGENT)
    check("текст не того хэша — 400", code == 400, d)
    code, d = put(f"{base}/{h}.txt", text.encode(), AGENT,
                  now=clock.t - 10_000)
    check("просроченная подпись — 403", code == 403, d)
    big = "я" * 40000
    code, d = put(f"{base}/{chain.text_hash(big)}.txt", big.encode(), AGENT)
    check("слишком длинный — 413", code == 413, d)
    try:
        delivery.upload_prompt(base, AGENT, big)
        check("отказ посредника — исключение с причиной", False)
    except RuntimeError as e:
        check("отказ посредника — исключение с причиной", "413" in str(e),
              str(e))

    # --- раздача: задания ещё нет -------------------------------------------
    code, _, _ = get(url, NODE)
    check("задания ещё нет — 404, текст не отдан", code == 404, code)

    # --- задание взято -------------------------------------------------------
    job = ch.add(text, AGENT, state="claimed", executor=NODE)
    clock.t += 6        # прошёл предел частоты поиска
    code, body, hdr = get(url, NODE)
    check("исполнитель получает текст", code == 200, code)
    check("текст — шифром", hdr.get(sign.H_SEALED) == "1" and
          text.encode() not in body)
    check("шифр вскрывается ключом исполнителя",
          code == 200 and sign.open_sealed(NODE, body).decode() == text)
    code, _, _ = get(url)
    check("без подписи — 401", code == 401, code)
    code, _, _ = get(url, STRANGER)
    check("посторонний — 403", code == 403, code)
    code, _, _ = get(url, CHECKER)
    check("проверяющий до сдачи ответа — 403", code == 403, code)

    # Тот же код, что на узлах.
    delivery.set_sign_key(NODE)
    raw, src, why = delivery.fetch_verified(
        delivery.sources_with(base, ()), (f"{h}.txt", f"{job}.txt"),
        delivery.prompt_check(ch.jobs[job]))
    check("узел берёт текст своим кодом (fetch_verified) и он сходится "
          "с хэшем", raw == text, why)
    delivery.set_sign_key(None)

    # --- ответ сдан ------------------------------------------------------------
    ch.jobs[job]["state"] = "answered"
    r.access[REG].cache.clear()
    code, body, _ = get(url, CHECKER)
    check("после сдачи ответа — действующий узел (проверяющий) получает",
          code == 200 and sign.open_sealed(CHECKER, body).decode() == text)
    code, _, _ = get(url, STRANGER)
    check("после сдачи посторонний — всё равно 403", code == 403, code)

    # --- исполнитель только что взял: в кеше ещё open --------------------------
    t2 = "второй вопрос"
    h2 = chain.text_hash(t2)
    delivery.upload_prompt(base, AGENT, t2)
    j2 = ch.add(t2, AGENT, state="open")
    clock.t += 6
    get(f"{base}/{h2}.txt", NODE)        # закешировали open
    ch.jobs[j2].update(state="claimed", executor=addr(NODE))
    code, _, _ = get(f"{base}/{h2}.txt", NODE)
    check("задание взято секунду назад — кеш перечитан, текст отдан",
          code == 200, code)

    # --- тот же вопрос ещё раз, новым заданием -----------------------------
    ch.jobs[job]["state"] = "settled"
    j_again = ch.add(text, AGENT, state="claimed", executor=CHECKER)
    r.access[REG].cache.clear()
    clock.t += 6
    code, body, _ = get(url, CHECKER)
    check("тот же вопрос новым заданием — новый исполнитель получает текст",
          code == 200 and sign.open_sealed(CHECKER, body).decode() == text,
          code)
    ch.jobs[j_again]["state"] = "settled"
    ch.jobs[job]["state"] = "answered"

    # --- чужой текст под своим заданием ------------------------------------------
    t3 = "чужой текст"
    h3 = chain.text_hash(t3)
    delivery.upload_prompt(base, AGENT, t3)
    ch.add(t3, OTHER, state="claimed", executor=NODE)
    clock.t += 6
    code, _, _ = get(f"{base}/{h3}.txt", NODE)
    check("задание другого заказчика с тем же хэшем — текст не отдан",
          code == 404, code)

    # --- засорение ---------------------------------------------------------
    r2, srv2, base2 = start(os.path.join(tmp, "q"), Chain(), clock,
                            max_pending=2)
    os.makedirs(os.path.join(tmp, "q"), exist_ok=True)
    codes = [put(f"{base2}/{chain.text_hash(t)}.txt", t.encode(), STRANGER)[0]
             for t in ("a1", "a2", "a3")]
    check("больше MAX_PENDING текстов без задания — 429",
          codes == [201, 201, 429], codes)
    check("повтор того же текста не считается новым",
          put(f"{base2}/{chain.text_hash('a1')}.txt", b"a1", STRANGER)[0]
          == 201)
    srv2.shutdown()

    # --- уборка ------------------------------------------------------------
    t4 = "никто не поставил задание"
    delivery.upload_prompt(base, AGENT, t4)
    h4 = chain.text_hash(t4)
    check("уборка: свежий невостребованный остаётся",
          r.sweep() == 0 or os.path.isfile(r.path(h4)))
    ch.jobs[job]["state"] = "settled"
    r.sweep()
    check("уборка: текст закрытого задания стёрт",
          not os.path.isfile(r.path(h)))
    check("уборка: текст задания в работе остаётся",
          os.path.isfile(r.path(h2)))
    clock.t += r.cfg.unclaimed_ttl + 1
    r.sweep()
    check("уборка: невостребованный стёрт через UNCLAIMED_TTL",
          not os.path.isfile(r.path(h4)))
    ch.down = True
    clock.t += r.cfg.max_age + 1
    r.sweep()
    check("уборка: по MAX_AGE стирается и при молчащей цепочке",
          not os.path.isfile(r.path(h2)))
    ch.down = False

    # --- посредник на пути общего домена (/relay/…) --------------------------
    t5 = "через приставку"
    h5 = chain.text_hash(t5)
    clock.t = _t.time()
    delivery.upload_prompt(base + "/relay", AGENT, t5)
    ch.add(t5, AGENT, state="claimed", executor=NODE)
    code, body, _ = get(f"{base}/relay/{h5}.txt", NODE)
    check("путь с приставкой /relay/ — выкладка и раздача работают",
          code == 200 and sign.open_sealed(NODE, body).decode() == t5, code)

    code, body, _ = get(f"{base}/healthz")
    check("/healthz отвечает", code == 200 and json.loads(body)["ok"])
    srv.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
