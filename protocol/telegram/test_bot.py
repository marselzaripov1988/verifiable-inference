#!/usr/bin/env python3
"""Бот на подставной цепочке и подставном Telegram.

Проверяется то, из-за чего человек либо получает верный ответ, либо
кошелёк бота платит зря:

  * **текст выложен ДО сделки**, и сделка — та же, что у заказчика из
    командной строки (`client.submit_message`);
  * **лимит держится**, а не прошедшая сделка его не съедает;
  * **ответ показывается только с вердиктом «подтверждён».** Признанный
    негодным и закрытый без проверки не показываются вовсе. С
    EARLY_ANSWER=1 ответ виден сразу с пометкой «не проверен», а без
    подтверждения — удаляется;
  * **невзятое задание снимается**, и деньги и вопрос возвращаются;
  * **владелец узла узнаёт об обвинении и о молчащем адресе**, а о
    недоступности цепочки — нет: это не новость об узле;
  * **раздача запросов отдаёт только `<хэш>.txt` заданий в работе**, а
    после закрытия задания текст вопроса и ответ стираются: хэш лежит в
    цепочке открыто, и иначе вопросы скачал бы любой её читатель.

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

    def send_all(self, chat, text):
        return [self.send(chat, p) for p in bot.split_text(text)]

    def delete(self, chat, msg):
        self.deleted = getattr(self, "deleted", []) + [msg]
        return not getattr(self, "no_delete", False)

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
        self.template = "qwen"
        self.node, self.explorer = "rpc", "https://axiomechain.org"
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
                now=lambda: clock[0],
                txs=lambda node, contract, job_id: [
                    ("submit_job", "AA"), ("claim_job", "BB"),
                    ("submit_answer", "CC"), ("submit_check", "DD"),
                    ("submit_check", "EE"), ("finalize", "FF")])
    return b, clock, fetched


def msg(chat, text):
    return {"message": {"chat": {"id": chat}, "text": text}}


def texts(b):
    return [t for _, t in b.api.sent] + [t for _, _, t in b.api.edits]


def test_ask_and_confirmed(tmp):
    b, clock, _ = make(tmp)
    b.handle(msg(1, "Что такое блокчейн?"))
    m, funds, to = b.sender.sent[0]
    wrapped = ("<|im_start|>user\nЧто такое блокчейн?<|im_end|>\n"
               "<|im_start|>assistant\n")
    ph = chain.text_hash(wrapped)
    ok(b.sender.seen_file, "текст запроса выложен до сделки")
    with open(os.path.join(b.cfg.prompts, f"{ph}.txt"), encoding="utf-8") as f:
        ok(f.read() == wrapped, "вопрос выложен в шаблоне диалога Qwen")
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
    ok("проверка 2 — https://axiomechain.org/transactions/EE" in last
       and "вердикт и выплата — https://axiomechain.org/transactions/FF"
       in last, "к вердикту приложены ссылки на сделки задания")
    n = len(b.api.sent)
    b.tick_jobs()
    ok(len(b.api.sent) == n, "закрытое задание больше не трогается")
    ok(not os.path.exists(os.path.join(b.cfg.prompts, f"{ph}.txt")),
       "после вердикта текст вопроса стёрт с сервера")
    ok(b.store.q("SELECT answer FROM jobs WHERE job_id=7") == [(None,)],
       "и ответ в базе не хранится")
    ok(ph not in b.active_hashes(), "и больше не раздаётся")


def test_privacy(tmp):
    b, clock, _ = make(tmp)
    b.handle(msg(1, "вопрос один"))
    ph = b.store.q("SELECT prompt_hash FROM jobs WHERE job_id=7")[0][0]
    ok(ph in b.active_hashes(), "текст задания в работе раздаётся")
    b.chain.jobs[7] = {"state": "open"}
    clock[0] += 901
    b.tick_jobs()
    ok(not os.path.exists(os.path.join(b.cfg.prompts, f"{ph}.txt")),
       "снятое задание: текст стёрт")

    f, _, _ = make(tmp, fail=True)
    f.handle(msg(2, "не ушло"))
    ok(not [n for n in os.listdir(f.cfg.prompts) if n.endswith(".txt")],
       "сделка не прошла — текст не остался")

    c, _, _ = make(os.path.join(tmp, "purge"))
    os.makedirs(c.cfg.prompts)
    c.handle(msg(3, "живой"))
    live = c.store.q("SELECT prompt_hash FROM jobs")[0][0]
    for name in ("b" * 64 + ".txt", "12.txt", "notes.md"):
        with open(os.path.join(c.cfg.prompts, name), "w") as fh:
            fh.write("x")
    c.store.q("INSERT INTO jobs(job_id,chat,msg,created,state,answer,done) "
              "VALUES(1,3,1,0,'settled','старый ответ',1)")
    ok(c.purge() == 2, "чистка при запуске стёрла два чужих текста")
    left = sorted(os.listdir(c.cfg.prompts))
    ok(left == sorted([f"{live}.txt", "notes.md"]),
       f"живой текст и посторонний файл остались ({left})")
    ok(c.store.q("SELECT answer FROM jobs WHERE job_id=1") == [(None,)],
       "старые ответы из базы стёрты")


def test_job_command(tmp):
    b, _, _ = make(tmp)
    b.chain.jobs[7] = {"state": "settled", "answer_hash": "h",
                       "guilty": False, "executor": NODE, "_checks": 2}
    b.handle(msg(1, "/job 7"))
    last = b.api.sent[-1][1]
    ok(last.startswith("Задание 7:"),
       "/job с неразрывным пробелом — та же команда")
    ok("объявление и оплата — https://axiomechain.org/transactions/AA"
       in last, "/job отдаёт ссылки на сделки задания")


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


def test_hours(tmp):
    ok(bot.parse_hours("") is None and bot.parse_hours("19-21") == (19, 21)
       and bot.parse_hours("19:00-21:00") == (19, 21), "часы разбираются")
    try:
        bot.parse_hours("21-19")
        ok(False, "обратный интервал отвергнут")
    except SystemExit:
        ok(True, "обратный интервал отвергнут")
    b, clock, _ = make(tmp)
    b.cfg.hours = (19, 21)
    day = 1_000_000 - 1_000_000 % 86400   # полночь UTC
    clock[0] = day + 12 * 3600            # 15:00 МСК
    b.handle(msg(5, "вопрос"))
    ok(len(b.sender.sent) == 0, "вне часов сделка не ушла")
    ok("откроется через 4 ч 0 мин" in b.api.sent[-1][1],
       "человеку сказано, когда откроется")
    ok(b.store.used(5, b.day()) == 0, "и лимит не тронут")
    b.cfg.admins = {7}
    b.handle(msg(7, "вопрос"))
    ok(len(b.sender.sent) == 1, "админу можно и вне часов")
    clock[0] = day + 16 * 3600 + 30 * 60  # 19:30 МСК
    b.handle(msg(5, "вопрос"))
    ok(len(b.sender.sent) == 2, "в часы работы вопрос уходит")
    b.handle(msg(5, "/hours"))
    ok("Сейчас открыто" in b.api.sent[-1][1], "/hours видит, что открыто")
    clock[0] = day + 18 * 3600 + 10 * 60  # 21:10 МСК
    b.handle(msg(5, "/hours"))
    ok("откроется через 21 ч 50 мин" in b.api.sent[-1][1],
       "после 21:00 — до завтрашних 19:00")
    b.handle(msg(5, "/start"))
    ok("/gpu" in b.api.sent[-1][1] and "19:00 до 21:00" in b.api.sent[-1][1],
       "/start называет часы и /gpu")


def test_gpu(tmp):
    b, _, _ = make(tmp)
    b.cfg.hours = (19, 21)
    b.handle(msg(5, "/gpu"))
    t = b.api.sent[-1][1]
    ok(bot.NODE_IMAGE in t and "-e CONTRACT=axm1reg" in t,
       "/gpu: образ по digest и реестр бота")
    ok("0.3 BARTR" in t or "0,3 BARTR" in t, "/gpu: залог из цепочки")
    ok("sm_89" in t and "с 19:00 до 21:00" in t, "/gpu: класс и часы")
    ok(len(t) < 4096, "/gpu влезает в одно сообщение Telegram")


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
    closed = "c" * 64
    with open(os.path.join(d, f"{closed}.txt"), "w", encoding="utf-8") as f:
        f.write("закрытый вопрос")
    srv = bot.serve_prompts(d, 0, allowed=lambda: {h})
    port = srv.server_address[1]
    got = urllib.request.urlopen(f"http://127.0.0.1:{port}/{h}.txt").read()
    ok(got.decode() == "вопрос", "запрос по хэшу отдаётся")
    for path in ("/bot.sqlite", "/../bot.py", "/", f"/{closed}.txt",
                 "/7.txt"):
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


def test_signed_prompt(tmp):
    import sign
    b, _, _ = make(tmp)
    b.handle(msg(1, "тайный вопрос"))
    ph = b.store.q("SELECT prompt_hash FROM jobs WHERE job_id=7")[0][0]
    ex_key, ch_key, out_key = os.urandom(32), os.urandom(32), os.urandom(32)
    ex = sign.address(sign.pubkey(ex_key))
    ch = sign.address(sign.pubkey(ch_key))
    b.chain.jobs[7] = {"state": "claimed", "executor": ex}
    b.chain.nodes[ch] = {"status": "active"}
    b.access.granters = None
    b.access._granters = lambda g: set()
    srv = bot.serve_prompts(b.cfg.prompts, 0, allowed=b.active_hashes,
                            auth=b.authorize)
    url = f"http://127.0.0.1:{srv.server_address[1]}/{ph}.txt"

    def get(key=None):
        h = {}
        if key:
            # Время подписи — по часам бота: в тесте они подставные.
            h = sign.headers_for(key, url, now=b.now())
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=h))
            return r.status
        except urllib.error.HTTPError as e:
            return e.code

    ok(get() == 401, "без подписи вопрос не отдаётся")
    ok(get(out_key) == 403, "посторонний с подписью — отказ")
    ok(get(ch_key) == 403, "другой узел, пока ответ не сдан, — отказ")
    ok(get(ex_key) == 200, "исполнитель задания получает вопрос")
    r = urllib.request.urlopen(urllib.request.Request(
        url, headers=sign.headers_for(ex_key, url, now=b.now())))
    body = r.read()
    ok(r.headers.get(sign.H_SEALED) == "1"
       and "тайный".encode() not in body,
       "вопрос идёт по проводу шифром")
    ok("тайный вопрос" in sign.open_sealed(ex_key, body).decode(),
       "исполнитель вскрывает его своим ключом")
    b.chain.jobs[7] = {"state": "answered", "executor": ex}
    b.access.cache.clear()
    ok(get(ch_key) == 200, "ответ сдан — проверяющий получает вопрос")
    ok(get(out_key) == 403, "посторонний по-прежнему — нет")
    b.cfg.allow_unsigned = True
    ok(get() == 200, "ALLOW_UNSIGNED=1 — для старого образа узла")
    srv.shutdown()


def test_pay(tmp):
    import base64
    b, clock, _ = make(tmp)
    b.cfg.price_bartr = 2_000_000
    incoming = []
    b.deposits = lambda addr, skip: [d for d in incoming if d[0] not in skip]
    _, _, total = bot.client.totals(PARAMS, b.cfg.price)
    q_axm = total + 3000

    # первый проход: старая история кошелька не зачисляется и не шумит
    incoming.append(("OLD", "", "uaxm", 5_000_000))
    b.tick_deposits(initial=True)
    ok(b.api.sent == [], "история кошелька при запуске — без писем")

    b.handle(msg(5, "/pay 3"))
    t = b.api.sent[-1][1]
    code = b.store.code(5)
    ok(code in t and "axm1bot" in t, "/pay: код и адрес бота")
    link = next(l for l in t.splitlines() if l.startswith("axiomesign://"))
    body = json.loads(base64.b64decode(link[len("axiomesign://"):]))
    ok(body["contract_addr"] == bot.BARTR and body["memo"] == code
       and body["msg"] == {"transfer": {"recipient": "axm1bot",
                                        "amount": "6000000"}},
       "ссылка: transfer BARTR на бота на 3 вопроса, код в заметке")
    ok(b.store.code(5) == code and b.store.code(6) != code,
       "код у чата постоянный и у каждого свой")

    # лимит 2: третий вопрос без денег не уходит
    for _ in range(3):
        b.handle(msg(5, "вопрос"))
    ok(len(b.sender.sent) == 2 and "/pay" in b.api.sent[-1][1],
       "без денег сверх лимита — отказ со ссылкой на /pay")
    b.store.q("UPDATE jobs SET done=1")  # бесплатные дальше не нужны

    incoming.append(("D1", f"за вопросы {code.upper()}", "uaxm", 2 * q_axm))
    incoming.append(("D2", "без кода", "uaxm", 999))
    b.cfg.admins = {1}
    b.tick_deposits()
    ok(b.store.credit(5) == {"uaxm": 2 * q_axm}, "перевод с кодом зачислен")
    ok(any(c == 5 and "Пришло" in t for c, t in b.api.sent),
       "человек знает, что деньги пришли")
    ok(any(c == 1 and "без кода" in t for c, t in b.api.sent),
       "перевод без кода — владельцу, не на чей-то счёт")
    b.tick_deposits()
    ok(b.store.credit(5) == {"uaxm": 2 * q_axm}, "повторный проход не удваивает")

    b.handle(msg(5, "вопрос три"))
    ok(len(b.sender.sent) == 3 and b.store.credit(5)["uaxm"] == q_axm,
       "платный вопрос ушёл, списана цена вопроса")
    job = b.sender.next - 1
    b.chain.jobs[job] = {"state": "settled", "guilty": None}
    b.tick_jobs()
    ok(b.store.credit(5)["uaxm"] == 2 * q_axm,
       "без вердикта — оплата вернулась на счёт")
    b.tick_jobs()
    ok(b.store.credit(5)["uaxm"] == 2 * q_axm, "и только один раз")

    b.handle(msg(5, "вопрос четыре"))
    job = b.sender.next - 1
    b.chain.jobs[job] = {"state": "open"}
    clock[0] += 901
    b.tick_jobs()
    ok(b.store.credit(5)["uaxm"] == 2 * q_axm,
       "невзятое платное снято — деньги на счёте, а не в лимите")

    b.handle(msg(5, "вопрос пять"))
    job = b.sender.next - 1
    b.chain.jobs[job] = {"state": "settled", "guilty": False}
    b.tick_jobs()
    ok(b.store.credit(5)["uaxm"] == q_axm, "подтверждённый ответ — оплата списана")

    incoming.append(("D3", code, "bartr", 2_000_000))
    b.tick_deposits()
    b.store.charge(5, "uaxm", q_axm)
    b.handle(msg(5, "вопрос шесть"))
    ok(b.store.credit(5).get("bartr") == 0, "кончились AXM — платит BARTR")

    f, _, _ = make(tmp, fail=True)
    f.store.take(7, f.day(), 99)
    f.store.add_credit(7, "uaxm", q_axm)
    f.handle(msg(7, "вопрос"))
    ok(f.store.credit(7)["uaxm"] == q_axm, "сделка не прошла — оплата на месте")

    raw = {"tx_response": {"code": 0}, "tx": {"body": {"memo": "vi-abc234",
           "messages": [{"@type": "/cosmwasm.wasm.v1.MsgExecuteContract",
                         "contract": bot.BARTR, "msg": {"transfer": {
                             "recipient": "axm1bot", "amount": "5"}}},
                        {"@type": "/cosmos.bank.v1beta1.MsgSend",
                         "to_address": "axm1other",
                         "amount": [{"denom": "uaxm", "amount": "7"}]}]}}}
    ok(bot.parse_deposit(raw, "axm1bot", bot.BARTR) == ("vi-abc234", "bartr", 5),
       "разбор сделки: только то, что пришло боту")
    raw["tx_response"]["code"] = 5
    ok(bot.parse_deposit(raw, "axm1bot", bot.BARTR)[2] == 0,
       "упавшая сделка не считается")


def test_templates():
    print("### шаблон по модели реестра")
    q3 = bot.template_for("auto", "huihui-ai/Huihui-Qwen3-8B-abliterated-v2")
    ok(q3.format(q="X").endswith("<think>\n\n</think>\n\n"),
       "Qwen3 — пустой блок рассуждений")
    q25 = bot.template_for("auto", "Qwen/Qwen2.5-7B-Instruct").format(q="X")
    ok(q25.endswith("<|im_start|>assistant\n") and "<think>" not in q25,
       "Qwen2.5 — прежний шаблон")
    ok(bot.template_for("none", "huihui-ai/Huihui-Qwen3-8B-abliterated-v2")
       == "{q}", "явная настройка сильнее модели")


def test_early(tmp):
    print("### EARLY_ANSWER: ответ сразу, вердикт потом")
    try:
        os.environ["EARLY_ANSWER"] = "1"
        ok(bot.Config({"TG_TOKEN": "t", "CONTRACT": "c", "KEY": "k",
                       "PROMPT_URI": "u", "EARLY_ANSWER": "1"}).early
           and not bot.Config({"TG_TOKEN": "t", "CONTRACT": "c", "KEY": "k",
                               "PROMPT_URI": "u"}).early,
           "EARLY_ANSWER=1 включает, по умолчанию выключено")
    finally:
        os.environ.pop("EARLY_ANSWER", None)

    def answered(b):
        b.handle(msg(1, "вопрос"))
        b.chain.jobs[7] = {"state": "answered", "answer_hash": "h",
                           "_checks": 0}
        b.tick_jobs()

    b, _, _ = make(os.path.join(tmp, "ok"))
    b.cfg.early = True
    answered(b)
    sent = [t for _, t in b.api.sent]
    ok(any("НЕ ПРОВЕРЕН" in t for t in sent)
       and "Сеть проверяет подпись." in sent,
       "ответ показан до вердикта, с пометкой «не проверен»")
    n = len(b.api.sent)
    b.tick_jobs()
    ok(len(b.api.sent) == n, "второй раз ранний ответ не шлётся")
    head = b.early_ids(7)[0]
    b.chain.jobs[7] = {"state": "settled", "answer_hash": "h",
                       "guilty": False, "executor": NODE, "_checks": 2}
    b.tick_jobs()
    ok(any(m == head and "подтверждён" in t for _, m, t in b.api.edits),
       "подтверждён: заголовок раннего ответа исправлен")
    last = b.api.sent[-1][1]
    ok("подтверждён" in last and "Сеть проверяет" not in last
       and "transactions/FF" in last,
       "и отдельное сообщение со ссылками, без повтора ответа")
    ok(not getattr(b.api, "deleted", []), "подтверждённый ответ не удалён")
    ok(b.store.q("SELECT done, answer FROM jobs WHERE job_id=7") == [(1, None)],
       "задание закрыто, ответ из базы стёрт")

    for guilty, word in ((True, "негодным"), (None, "без вердикта")):
        b, _, _ = make(os.path.join(tmp, f"g{guilty}"))
        b.cfg.early = True
        answered(b)
        ids = b.early_ids(7)
        b.chain.jobs[7] = {"state": "settled", "answer_hash": "h",
                           "guilty": guilty}
        b.tick_jobs()
        ok(b.api.deleted == ids[1:], f"guilty={guilty}: ответ удалён")
        ok(any(m == ids[0] and "удалён" in t for _, m, t in b.api.edits),
           f"guilty={guilty}: заголовок говорит, что ответ удалён")
        ok(word in b.api.sent[-1][1], f"guilty={guilty}: обычное сообщение "
                                      f"о вердикте и возврате")

    b, _, _ = make(os.path.join(tmp, "nodel"))
    b.cfg.early = True
    answered(b)
    b.api.no_delete = True
    b.chain.jobs[7] = {"state": "settled", "answer_hash": "h", "guilty": True}
    b.tick_jobs()
    ok(any("НЕДЕЙСТВИТЕЛЕН" in t for _, _, t in b.api.edits),
       "удалить не вышло (48 ч) — заголовок предупреждает")

    b, _, _ = make(os.path.join(tmp, "off"))
    answered(b)
    ok(not any("Сеть проверяет" in t for t in texts(b)),
       "без EARLY_ANSWER до вердикта ответа нет, как раньше")


def test_split():
    parts = bot.split_text("строка\n" * 1000, limit=100)
    ok(all(len(p) <= 100 for p in parts) and "".join(parts) == "строка\n" * 1000,
       "длинный ответ режется без потерь")


if __name__ == "__main__":
    tmp = tempfile.mkdtemp()
    try:
        for t in (test_ask_and_confirmed, test_job_command,
                  test_guilty_and_no_verdict,
                  test_answer_unreachable, test_limits, test_hours, test_gpu,
                  test_open_timeout,
                  test_nodes, test_prompt_server, test_privacy,
                  test_signed_prompt, test_pay, test_early):
            t(tempfile.mkdtemp(dir=tmp))
        test_split()
        test_templates()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'ВСЁ ПРОШЛО' if not FAILS else f'ПРОВАЛОВ: {len(FAILS)}'}")
    sys.exit(1 if FAILS else 0)
