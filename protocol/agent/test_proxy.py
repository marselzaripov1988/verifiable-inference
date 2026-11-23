#!/usr/bin/env python3
"""Прокси агента на подставной цепочке, через настоящий HTTP.

Проверяется то, из-за чего агент либо получает верный ответ, либо его
кошелёк платит зря:

  * сделка — та же, что у заказчика из командной строки
    (`client.submit_message`), монетой uaxm, и текст выложен ДО неё;
  * диалог OpenAI собирается в шаблон модели, хэш задания — от него;
  * в режиме `settled` ответ отдаётся только с вердиктом «подтверждён»,
    негодный и непроверенный не отдаются вовсе;
  * в режиме `answer` ответ отдаётся сразу, с `verified: false`;
  * потолок трат держится, а непрошедшая сделка его не съедает;
  * невзятое по сроку снимается;
  * реестр с расчётом не в uaxm не обслуживается;
  * без токена API закрыт, если токен задан;
  * узлам текст отдаётся только по подписи участника задания.

Запуск:  python protocol/agent/test_proxy.py
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import proxy   # noqa: E402
import chain   # noqa: E402
import sign    # noqa: E402

PARAMS = chain.Params(
    checks_required=1, pinned_model="Qwen/Qwen2.5-7B-Instruct",
    pinned_runtime="vllm-0.31.0", pinned_dtype="bfloat16",
    pinned_arch="sm_89", kernel_cache_hash="ab" * 32,
    settlement={"native": "uaxm"}, stake_unit={"native": "uaxm"},
    min_stake=300000, verifier_fee_bps=485, conformance_limit_micro=0,
    conformance_ref_hash="cd" * 32, conformance_lens=(1,),
    claim_ttl_seconds=600, check_ttl_seconds=3600, conformance_version=1)

FAILS = []


def ok(cond, what):
    print(("ок   " if cond else "ПРОВАЛ ") + what)
    if not cond:
        FAILS.append(what)


class Chain:
    ChainUnavailable = chain.ChainUnavailable

    def __init__(self):
        self.jobs, self.nodes, self.params = {}, {}, PARAMS

    def load_params(self, rest, contract):
        return self.params

    def load_job(self, rest, contract, job_id):
        return dict(self.jobs[int(job_id)])

    def load_executor(self, rest, contract, addr):
        return self.nodes.get(addr)


class Sender:
    """Подставной кошелёк. `script(job)` — что цепочка сделает с заданием
    после постановки: тест задаёт ход событий."""

    def __init__(self, ch, prompts, fail=False):
        self.ch, self.prompts, self.fail = ch, prompts, fail
        self.sent, self.next, self.seen_file = [], 1, None

    def address(self):
        return "axm1agent"

    def send(self, msg, funds=None, contract=None):
        if self.fail:
            raise proxy.tx.TxFailed("insufficient funds")
        self.sent.append((msg, funds, contract))
        if "cancel_job" in msg:
            j = self.ch.jobs[msg["cancel_job"]["job_id"]]
            j.update(state="settled", guilty=None)
            return {"txhash": "CC"}
        ph = msg["submit_job"]["prompt_hash"]
        self.seen_file = os.path.isfile(os.path.join(self.prompts,
                                                     f"{ph}.txt"))
        jid = self.next
        self.next += 1
        self.ch.jobs[jid] = {"state": "open", "prompt_hash": ph,
                             "client": "axm1agent", "executor": None,
                             "answer_hash": None, "guilty": None}
        return {"txhash": f"TX{jid}", "tx": {"events": [{"type": "wasm",
                "attributes": [{"key": "job_id", "value": str(jid)}]}]}}


class Cfg:
    def __init__(self, tmp, **kw):
        self.contract, self.key = "axm1reg", "agent"
        self.prompt_uri = "http://agent:8090"
        self.rest = "rest"
        self.prompts = os.path.join(tmp, "prompts")
        self.api_token = None
        self.price, self.max_spend, self.max_chars = 10000, 100000, 1000
        self.open_timeout, self.wait, self.poll = 900, 60, 1
        self.mode, self.template = "settled", "auto"
        self.__dict__.update(kw)


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def make(tmp, plan, fail=False, **kw):
    """`plan(ch, job_id, n)` — ход цепочки на n-м шаге ожидания."""
    ch = Chain()
    cfg = Cfg(tmp, **kw)
    store = proxy.Store(os.path.join(tmp, "a.sqlite"))
    snd = Sender(ch, cfg.prompts, fail)
    clock = Clock()
    steps = {}

    def sleep(sec):
        clock.t += sec
        for jid in list(ch.jobs):
            steps[jid] = steps.get(jid, 0) + 1
            plan(ch, jid, steps[jid])

    def fetch(job, sources, job_id):
        if job.get("answer_hash"):
            return {"answer": job["_answer"]}, "узел", []
        return None, None, ["нет"]

    p = proxy.Proxy(cfg, store, snd, chainmod=ch, fetch=fetch,
                    sources=lambda *a: ["узел"], now=clock, sleep=sleep)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), proxy.make_handler(p))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return p, ch, snd, srv, clock


def call(srv, method, path, body=None, token=None):
    url = f"http://127.0.0.1:{srv.server_address[1]}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def honest(answer):
    """Взяли, ответили, подтвердили."""
    def plan(ch, jid, n):
        j = ch.jobs[jid]
        if n == 1:
            j.update(state="claimed", executor="axm1node")
        elif n == 2:
            j.update(state="answered", answer_hash="h", _answer=answer)
        elif n == 3:
            j.update(state="settled", guilty=False)
    return plan


def guilty(ch, jid, n):
    j = ch.jobs[jid]
    if n == 1:
        j.update(state="answered", executor="axm1node", answer_hash="h",
                 _answer="подлог")
    elif n == 2:
        j.update(state="settled", guilty=True)


def nobody(ch, jid, n):
    pass


CHAT = {"model": "any", "messages": [
    {"role": "system", "content": "Отвечай кратко."},
    {"role": "user", "content": "Столица Франции?"}]}


def main():
    tmp = tempfile.mkdtemp()
    try:
        run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if FAILS:
        print(f"\nпровалов: {len(FAILS)}")
        return 1
    print("\nпрокси ведёт себя как задумано")
    return 0


def fresh(tmp, name):
    d = os.path.join(tmp, name)
    os.makedirs(d)
    return d


def run(tmp):
    # --- честный путь ----------------------------------------------------
    p, ch, snd, srv, _ = make(fresh(tmp, "1"),
                              honest(" Париж.<|im_end|>"))
    code, r = call(srv, "POST", "/v1/chat/completions", CHAT)
    ok(code == 200, f"честный ответ: 200 (было {code} {r})")
    ok(r.get("choices", [{}])[0].get("message", {}).get("content")
       == "Париж.", "ответ очищен от маркера конца реплики")
    v = r.get("verification", {})
    ok(v.get("verified") is True and v.get("verdict") == "confirmed",
       "ответ помечен подтверждённым")
    msg, funds, to = snd.sent[0]
    ok(funds == "10485uaxm" and to is None,
       f"платит кошелёк агента монетой uaxm: цена + 4,85% ({funds})")
    ok(snd.seen_file, "текст выложен ДО сделки")
    prompt = ("<|im_start|>system\nОтвечай кратко.<|im_end|>\n"
              "<|im_start|>user\nСтолица Франции?<|im_end|>\n"
              "<|im_start|>assistant\n")
    ok(msg["submit_job"]["prompt_hash"] == chain.text_hash(prompt),
       "хэш задания — от диалога в шаблоне Qwen")
    ok(msg["submit_job"]["prompt_uri"] == "http://agent:8090",
       "адрес текста — PROMPT_URI")
    ok(not os.listdir(p.cfg.prompts), "после вердикта текст стёрт")
    code, j = call(srv, "GET", "/v1/jobs/1")
    ok(code == 200 and j["answer"] == " Париж.<|im_end|>"
       and j["verified"], "задание читается и после закрытия")
    srv.shutdown()

    # --- негодный ответ --------------------------------------------------
    p, ch, snd, srv, _ = make(fresh(tmp, "2"), guilty)
    code, r = call(srv, "POST", "/v1/chat/completions", CHAT)
    ok(code == 502 and "негодным" in r["error"]["message"],
       f"негодный ответ не отдаётся: {code}")
    code, j = call(srv, "GET", "/v1/jobs/1")
    ok(j["answer"] is None and j["verdict"] == "guilty",
       "негодный ответ не хранится и не читается")
    srv.shutdown()

    # --- режим answer: сразу, но без подтверждения ----------------------
    p, ch, snd, srv, _ = make(fresh(tmp, "3"), honest(" Париж."))
    body = dict(CHAT, verification="answer")
    code, r = call(srv, "POST", "/v1/chat/completions", body)
    v = r.get("verification", {})
    ok(code == 200 and v.get("verified") is False
       and v.get("state") == "answered",
       f"режим answer: ответ до вердикта, verified=false ({code})")
    srv.shutdown()

    # --- невзятое снимается ---------------------------------------------
    p, ch, snd, srv, clock = make(fresh(tmp, "4"), nobody, open_timeout=3)
    code, r = call(srv, "POST", "/v1/completions",
                   {"prompt": "Привет", "max_tokens": 10})
    ok(code == 504 and "без вердикта" in r["error"]["message"],
       f"невзятое снято, агенту 504 ({code})")
    ok(any("cancel_job" in m for m, _, _ in snd.sent),
       "отправлен cancel_job — деньги вернутся")
    srv.shutdown()

    # --- потолок трат ----------------------------------------------------
    p, ch, snd, srv, _ = make(fresh(tmp, "5"), honest("да"),
                              max_spend=21000)
    c1, _ = call(srv, "POST", "/v1/completions", {"prompt": "раз"})
    c2, _ = call(srv, "POST", "/v1/completions", {"prompt": "два"})
    c3, r3 = call(srv, "POST", "/v1/completions", {"prompt": "три"})
    ok(c1 == 200 and c2 == 200 and c3 == 402,
       f"потолок: два по 10485 прошли, третий 402 ({c1}, {c2}, {c3})")
    ok(len([m for m, _, _ in snd.sent if "submit_job" in m]) == 2,
       "сверх потолка сделка не отправлена")
    srv.shutdown()

    # --- непрошедшая сделка ----------------------------------------------
    p, ch, snd, srv, _ = make(fresh(tmp, "6"), nobody, fail=True)
    code, r = call(srv, "POST", "/v1/completions", {"prompt": "x"})
    ok(code == 502 and p.store.spent(p.day()) == 0,
       "сделка не прошла: 502, потолок не тронут")
    ok(not os.listdir(p.cfg.prompts), "текст несостоявшегося задания стёрт")
    srv.shutdown()

    # --- асинхронно ------------------------------------------------------
    p, ch, snd, srv, clock = make(fresh(tmp, "7"), honest("сорок два"))
    code, r = call(srv, "POST", "/v1/jobs", {"prompt": "Ответ?"})
    ok(code == 202 and r["job_id"] == 1, f"POST /v1/jobs: 202 и номер")
    for _ in range(3):
        p.sleep(1)
    code, j = call(srv, "GET", "/v1/jobs/1")
    ok(j["verified"] and j["answer"] == "сорок два",
       "GET /v1/jobs/N отдаёт подтверждённый ответ")
    srv.shutdown()

    # --- чужой расчёт ----------------------------------------------------
    p, ch, snd, srv, _ = make(fresh(tmp, "8"), nobody)
    ch.params = replace(PARAMS, settlement={"cw20": "axm1bartr"})
    code, r = call(srv, "POST", "/v1/completions", {"prompt": "x"})
    ok(code == 503 and not snd.sent,
       f"расчёт не в uaxm: отказ без сделки ({code})")
    srv.shutdown()

    # --- токен API -------------------------------------------------------
    p, ch, snd, srv, _ = make(fresh(tmp, "9"), honest("да"), api_token="s3")
    c1, _ = call(srv, "POST", "/v1/completions", {"prompt": "x"})
    c2, _ = call(srv, "POST", "/v1/completions", {"prompt": "x"}, token="s3")
    ok(c1 == 401 and c2 == 200, f"без токена 401, с токеном 200 ({c1}, {c2})")
    srv.shutdown()

    # --- неверные запросы ------------------------------------------------
    p, ch, snd, srv, _ = make(fresh(tmp, "10"), nobody)
    c1, _ = call(srv, "POST", "/v1/chat/completions",
                 {"messages": [{"role": "tool", "content": "x"}]})
    c2, _ = call(srv, "POST", "/v1/chat/completions",
                 dict(CHAT, stream=True))
    c3, _ = call(srv, "POST", "/v1/completions", {"prompt": "x" * 2000})
    ok(c1 == c2 == c3 == 400 and not snd.sent,
       f"неверные запросы — 400 без сделки ({c1}, {c2}, {c3})")
    code, m = call(srv, "GET", "/v1/models")
    ok(m["data"][0]["id"] == "Qwen/Qwen2.5-7B-Instruct",
       "/v1/models называет модель реестра")
    srv.shutdown()

    # --- раздача текста узлам --------------------------------------------
    p, ch, snd, srv, clock = make(fresh(tmp, "11"), nobody)
    call(srv, "POST", "/v1/jobs", {"prompt": "тайна"})
    ph = chain.text_hash("тайна")
    node_key = bytes.fromhex("11" * 32)
    other_key = bytes.fromhex("22" * 32)
    node = sign.address(sign.pubkey(node_key))
    ch.jobs[1].update(state="claimed", executor=node)
    path = f"/{ph}.txt"

    def hdr(key):
        return sign.headers_for(key, f"http://agent:8090{path}", clock())

    ok(p.authorize({}, path, ph) == (401, "signature required"),
       "без подписи — 401")
    ok(p.authorize(hdr(node_key), path, ph) is None,
       "исполнитель задания получает текст")
    ok(p.authorize(hdr(other_key), path, ph)[0] == 403,
       "посторонний — 403")
    srv.shutdown()

    # --- чем подписываются сделки ----------------------------------------
    base = {"CONTRACT": "axm1c", "SIGN_KEY": "k", "PROMPT_URI": "http://x"}
    cfg = proxy.Config(base)
    ok(cfg.signer == "python" and cfg.key is None,
       "по умолчанию подпись без axmd, KEY не нужен")
    snd = proxy.make_sender(cfg, bytes.fromhex("11" * 32))
    ok(isinstance(snd, proxy.wallet.Wallet) and snd.address() == node,
       "отправитель — свой, адрес из SIGN_KEY")
    try:
        proxy.Config(dict(base, SIGNER="axmd"))
        ok(False, "SIGNER=axmd без KEY — отказ при запуске")
    except SystemExit:
        ok(True, "SIGNER=axmd без KEY — отказ при запуске")
    try:
        proxy.Config(dict(base, SIGNER="ledger"))
        ok(False, "неизвестный SIGNER — отказ")
    except SystemExit:
        ok(True, "неизвестный SIGNER — отказ")


if __name__ == "__main__":
    raise SystemExit(main())
