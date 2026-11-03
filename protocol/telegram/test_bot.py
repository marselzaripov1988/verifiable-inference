#!/usr/bin/env python3
"""Бот на подставной цепочке и подставном Telegram.

Проверяется то, из-за чего человек либо получает верный ответ, либо
кошелёк бота платит зря:

  * **текст выложен ДО сделки**, и сделка — та же, что у заказчика из
    командной строки (`client.submit_message`);
  * **лимит держится**, а не прошедшая сделка его не съедает;
  * **ответ показывается только с вердиктом «подтверждён».** Признанный
    негодным и закрытый без проверки не показываются вовсе;
  * **невзятое задание снимается**, и деньги и вопрос возвращаются;
  * **владелец узла узнаёт об обвинении и о молчащем адресе**, а о
    недоступности цепочки — нет: это не новость об узле;
  * **раздача запросов отдаёт только `<хэш>.txt` и `<номер>.txt`.**

Запуск:  python protocol/telegram/test_bot.py
"""

import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot   # noqa: E402
import chain  # noqa: E402

PARAMS = chain.Params(
    checks_required=2, pinned_model="Qwen/Qwen2.5-7B-Instruct",
    pinned_runtime="vllm-0.31.0", pinned_dtype="bfloat16",
    pinned_arch="sm_89", kernel_cache_hash="ab" * 32,
    settlement={"native": "uaxm"}, stake_unit={"cw20": bot.BARTR},
    min_stake=300000, verifier_fee_bps=485, conformance_limit_micro=0,
    conformance_ref_hash="cd" * 32, conformance_lens=(1,),
    claim_ttl_seconds=600, check_ttl_seconds=3600, conformance_version=1)

NODE = "axm1fvfvjp5w6nutyhfs47tmqatr9zjtprdg9fu7jx"
FAILS = []


def ok(cond, what):
    print(("ок   " if cond else "ПРОВАЛ ") + what)
    if not cond:
        FAILS.append(what)


class Api:
    def __init__(self):
        self.sent, self.edits, self.n = [], [], 0

    def send(self, chat, text):
        self.n += 1
        self.sent.append((chat, text))
        return self.n

    def edit(self, chat, msg, text):
        self.edits.append((chat, msg, text))


class Chain:
    """Подставная цепочка: задания и узлы — словари, их меняет тест."""
    ChainUnavailable = chain.ChainUnavailable

    def __init__(self):
        self.jobs, self.nodes, self.down = {}, {}, False

    def load_params(self, rest, contract):
        return PARAMS

    def load_job(self, rest, contract, job_id):
        return dict(self.jobs[int(job_id)])

    def checks_of(self, rest, contract, job_id):
        return [{}] * self.jobs[int(job_id)].get("_checks", 0)

    def load_executor(self, rest, contract, addr):
        return self.nodes.get(addr)

    def query_smart(self, rest, contract, q):
        if self.down:
            raise chain.ChainUnavailable("connection refused")
        a = q["executor"]["address"]
        if a not in self.nodes:
            raise chain.ChainUnavailable("executor not found")
        return dict(self.nodes[a])


class Sender:
    def __init__(self, prompts, fail=False):
        self.sent, self.prompts, self.fail = [], prompts, fail
        self.seen_file, self.next = None, 7

    def address(self):
        return "axm1bot"

    def send(self, msg, funds=None, contract=None):
        if self.fail:
            raise bot.tx.TxFailed("insufficient funds")
        if "submit_job" in msg:
            ph = msg["submit_job"]["prompt_hash"]
            self.seen_file = os.path.isfile(
                os.path.join(self.prompts, f"{ph}.txt"))
        self.sent.append((msg, funds, contract))
        if "submit_job" not in msg:
            return {"txhash": "CD34"}
        self.next += 1
        return {"txhash": "AB12", "tx": {"events": [{"type": "wasm",
                "attributes": [{"key": "job_id",
                                "value": str(self.next - 1)}]}]}}


class Cfg:
    def __init__(self, tmp):
        self.token, self.contract, self.key = "t", "axm1reg", "bot"
        self.prompt_uri = "http://vps:8080"
        self.rest = "rest"
        self.price, self.daily, self.max_chars = 10000, 2, 100
        self.open_timeout = 900
        self.prompts = os.path.join(tmp, "prompts")
        self.admins, self.allowed = set(), set()


def make(tmp, fail=False, answer="Сеть проверяет подпись."):
    cfg = Cfg(tmp)
    clock = [1_000_000.0]
    fetched = []

    def fetch(job, sources, job_id):
        fetched.append(job_id)
        return ({"answer": answer}, "x", []) if answer else (None, None,
                                                             ["молчит"])
    b = bot.Bot(cfg, Api(), bot.Store(":memory:"),
                Sender(cfg.prompts, fail), chainmod=Chain(), fetch=fetch,
                sources=lambda *a: ("http://node",), ping=lambda e: True,
                now=lambda: clock[0])
    return b, clock, fetched


def msg(chat, text):
    return {"message": {"chat": {"id": chat}, "text": text}}


def texts(b):
    return [t for _, t in b.api.sent] + [t for _, _, t in b.api.edits]


def test_ask_and_confirmed(tmp):
    b, clock, _ = make(tmp)
    b.handle(msg(1, "Что такое блокчейн?"))
    m, funds, to = b.sender.sent[0]
    ph = chain.text_hash("Что такое блокчейн?")
    ok(b.sender.seen_file, "текст запроса выложен до сделки")
    ok(m["submit_job"]["prompt_uri"] == "http://vps:8080"
       and m["submit_job"]["prompt_hash"] == ph, "сделка с адресом и хэшем")
    ok(funds == "10485uaxm", "цена плюс доля проверяющих, округление вверх")
    ok(any("Задание 7" in t for t in texts(b)), "человеку назван номер")

    b.chain.jobs[7] = {"state": "claimed"}
    b.tick_jobs()
    b.chain.jobs[7] = {"state": "answered", "answer_hash": "h", "_checks": 1}
    b.tick_jobs()
    ok(not any("Сеть проверяет" in t for t in texts(b)),
       "до вердикта ответ не показан")
    b.chain.jobs[7] = {"state": "settled", "answer_hash": "h",
                       "guilty": False, "executor": NODE, "_checks": 2}
    b.tick_jobs()
    last = b.api.sent[-1][1]
    ok("подтверждён" in last and "Сеть проверяет подпись." in last,
       "после вердикта — ответ с пометкой «подтверждён»")
    n = len(b.api.sent)
    b.tick_jobs()
    ok(len(b.api.sent) == n, "закрытое задание больше не трогается")


def test_guilty_and_no_verdict(tmp):
    for guilty, word in ((True, "негодным"), (None, "без вердикта")):
        b, _, _ = make(tmp)
        b.handle(msg(1, "вопрос"))
        b.chain.jobs[7] = {"state": "settled", "answer_hash": "h",
                           "guilty": guilty}
        b.tick_jobs()
        last = b.api.sent[-1][1]
        ok(word in last and "Сеть проверяет" not in last,
           f"guilty={guilty}: ответ не показан")


def test_answer_unreachable(tmp):
    b, _, fetched = make(tmp, answer=None)
    b.handle(msg(1, "вопрос"))
    b.chain.jobs[7] = {"state": "settled", "answer_hash": "h",
                       "guilty": False}
    b.tick_jobs()
    b.tick_jobs()
    ok(len(fetched) == 2, "ответ не взят — следующий цикл пробует снова")


def test_limits(tmp):
    b, _, _ = make(tmp)
    for _ in range(3):
        b.handle(msg(5, "вопрос"))
    ok(len(b.sender.sent) == 2, "лимит 2 в сутки: третья сделка не ушла")
    ok("кончились" in b.api.sent[-1][1], "человеку сказано про лимит")
    b.handle(msg(5, "x" * 101))
    ok(len(b.sender.sent) == 2, "слишком длинный вопрос не отправлен")

    f, _, _ = make(tmp, fail=True)
    f.handle(msg(6, "вопрос"))
    ok(f.store.used(6, f.day()) == 0, "сделка не прошла — лимит возвращён")
    ok(any("Не удалось" in t for t in texts(f)), "и человек знает почему")


def test_open_timeout(tmp):
    b, clock, _ = make(tmp)
    b.handle(msg(1, "вопрос"))
    b.chain.jobs[7] = {"state": "open"}
    b.tick_jobs()
    ok(len(b.sender.sent) == 1, "до срока задание не снимается")
    clock[0] += 901
    b.tick_jobs()
    ok(b.sender.sent[-1][0] == {"cancel_job": {"job_id": 7}},
       "через 15 минут невзятое снимается")
    ok(b.store.used(1, b.day()) == 0, "вопрос в лимите возвращён")


def test_nodes(tmp):
    b, _, _ = make(tmp)
    rec = {"status": "active", "stake": "300000", "jobs_done": 2,
           "convictions": 0, "abandoned": 0, "endpoint": "http://n:1",
           "conformance": {"divergence_micro": 0}}
    b.chain.nodes[NODE] = dict(rec)
    b.handle(msg(3, f"/node {NODE}"))
    card = b.api.sent[-1][1]
    ok("0,3 BARTR" in card.replace(".", ",") and "active" in card,
       "карточка узла: статус и залог в BARTR")
    b.tick_nodes()
    n = len(b.api.sent)
    ok(n == 1, "без изменений — без сообщений")

    b.chain.nodes[NODE].update(convictions=1, stake="150000")
    b.tick_nodes()
    t = b.api.sent[-1][1]
    ok("обвинение" in t and "залог" in t, "обвинение и залог — сообщены")

    b.chain.down = True
    n = len(b.api.sent)
    b.tick_nodes()
    ok(len(b.api.sent) == n, "цепочка молчит — это не новость об узле")
    b.chain.down = False

    b.ping = lambda e: False
    n = len(b.api.sent)
    b.tick_nodes()
    ok(n == len(b.api.sent), "один промах адреса — ещё не тревога")
    b.tick_nodes()
    ok("не отвечает" in b.api.sent[-1][1], "два промаха подряд — тревога")
    b.ping = lambda e: True
    b.tick_nodes()
    ok("снова отвечает" in b.api.sent[-1][1], "и сообщение о возврате")

    b.handle(msg(3, "/node axm1плохой"))
    ok("Нужен адрес" in b.api.sent[-1][1], "кривой адрес отвергнут")


def test_prompt_server(tmp):
    d = os.path.join(tmp, "serve")
    os.makedirs(d)
    h = "a" * 64
    for name, body in ((f"{h}.txt", "вопрос"), ("bot.sqlite", "секрет")):
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(body)
    srv = bot.serve_prompts(d, 0)
    port = srv.server_address[1]
    got = urllib.request.urlopen(f"http://127.0.0.1:{port}/{h}.txt").read()
    ok(got.decode() == "вопрос", "запрос по хэшу отдаётся")
    for path in ("/bot.sqlite", "/../bot.py", "/"):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}{path}")
            ok(False, f"{path} не отдаётся")
        except urllib.error.HTTPError as e:
            ok(e.code == 404, f"{path} не отдаётся")
    ok(bot.reachable(f"http://127.0.0.1:{port}"),
       "404 на 0.json — сервер связок жив")
    srv.shutdown()

    import http.server
    import threading

    class Proxy(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_error(503)

        def log_message(self, *a):
            pass
    p = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    threading.Thread(target=p.serve_forever, daemon=True).start()
    ok(not bot.reachable(f"http://127.0.0.1:{p.server_address[1]}"),
       "503 от прокси снятой машины — не жив")
    p.shutdown()
    ok(not bot.reachable("http://127.0.0.1:9"), "нет соединения — не жив")


def test_split():
    parts = bot.split_text("строка\n" * 1000, limit=100)
    ok(all(len(p) <= 100 for p in parts) and "".join(parts) == "строка\n" * 1000,
       "длинный ответ режется без потерь")


if __name__ == "__main__":
    tmp = tempfile.mkdtemp()
    try:
        for t in (test_ask_and_confirmed, test_guilty_and_no_verdict,
                  test_answer_unreachable, test_limits, test_open_timeout,
                  test_nodes, test_prompt_server):
            t(tempfile.mkdtemp(dir=tmp))
        test_split()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'ВСЁ ПРОШЛО' if not FAILS else f'ПРОВАЛОВ: {len(FAILS)}'}")
    sys.exit(1 if FAILS else 0)
