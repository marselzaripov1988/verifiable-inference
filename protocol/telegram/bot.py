#!/usr/bin/env python3
"""Telegram-бот реестра: вопрос — ответ с вердиктом, и присмотр за узлами.

Две стороны, один бот.

**Заказчик.** Человек пишет вопрос. Бот ставит задание от СВОЕГО кошелька
(платит бот, не человек), выкладывает текст запроса по своему адресу,
ждёт вердикта и присылает ответ. Ответ берётся у исполнителя и сверяется
с обоими хэшами задания — тем же `client.fetch_answer`, что у заказчика
из командной строки. Бот здесь не источник доверия, а курьер.

«Ответ получен» и «ответ подтверждён» не склеиваются: ответ показывается
только после вердикта и с ним вместе. Признанный негодным не показывается
вовсе.

**Владелец узла.** `/node axm1…` — запись узла из цепочки и подписка: бот
сообщает, когда меняется статус, залог, адрес, число заданий, обвинений и
брошенных, и когда адрес доставки перестаёт отвечать.

Кошелёк бота платит за всех, поэтому лимит: столько-то заданий на человека
в сутки и потолок длины вопроса. Без лимита кошелёк опустошит первый же,
кто напишет скрипт.

Запуск и настройка — README.md рядом.
"""

import http.server
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "daemon"))
import chain   # noqa: E402
import client  # noqa: E402
import tx      # noqa: E402

BARTR = "axm1uzldhz4dvv7k7gtc7jl5ahlj84eghc52p0r6zhnjca0d20dxcfkshsd8uz"
TG_LIMIT = 4000  # у Telegram 4096; запас на заголовок


# --- настройки ------------------------------------------------------------

class Config:
    """Всё из окружения. Без токена, реестра, ключа и адреса запросов —
    отказ при запуске, а не при первом вопросе."""

    def __init__(self, env=os.environ):
        def need(k):
            v = env.get(k, "").strip()
            if not v:
                raise SystemExit(f"ОТКАЗ: не задан {k} (см. README.md)")
            return v
        self.token = need("TG_TOKEN")
        self.contract = need("CONTRACT")
        self.key = need("KEY")
        # Адрес, по которому УЗЛЫ возьмут текст запроса. Снаружи, а не
        # localhost: исполнитель стоит на чужой машине.
        self.prompt_uri = need("PROMPT_URI").rstrip("/")
        self.rest = env.get("REST", "https://axm-lcd.trickle.pro")
        self.node = env.get("NODE", "https://api-chain.axiomechain.org")
        self.chain_id = env.get("CHAIN_ID", "axiome-1")
        self.gas_prices = env.get("GAS_PRICES", "0.0025uaxm")
        self.home = env.get("AXMD_HOME") or None
        self.price = int(env.get("PRICE", "10000"))
        self.daily = int(env.get("DAILY_LIMIT", "3"))
        self.max_chars = int(env.get("MAX_CHARS", "2000"))
        self.open_timeout = int(env.get("OPEN_TIMEOUT", "900"))
        self.prompts = env.get("PROMPTS_DIR", "./prompts")
        self.port = int(env.get("PROMPT_PORT", "8080"))
        self.db = env.get("DB", "./bot.sqlite")
        self.admins = {int(x) for x in re.findall(r"-?\d+",
                                                   env.get("ADMINS", ""))}
        self.allowed = {int(x) for x in re.findall(r"-?\d+",
                                                   env.get("ALLOWED", ""))}


# --- Telegram -------------------------------------------------------------

class Telegram:
    """Bot API напрямую, без библиотек: три метода, и всё по HTTPS.

    Без `parse_mode` нарочно: ответ модели — произвольный текст, и любая
    звёздочка в нём ломала бы разметку и отправку целиком.
    """

    def __init__(self, token, timeout=35):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.timeout = timeout

    def _call(self, method, **params):
        data = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}).encode()
        req = urllib.request.Request(self.base + method, data=data)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            d = json.loads(r.read())
        if not d.get("ok"):
            raise RuntimeError(f"{method}: {d.get('description')}")
        return d["result"]

    def updates(self, offset):
        return self._call("getUpdates", offset=offset, timeout=25)

    def send(self, chat, text):
        last = None
        for part in split_text(text):
            last = self._call("sendMessage", chat_id=chat, text=part,
                              disable_web_page_preview="true")["message_id"]
        return last

    def edit(self, chat, msg_id, text):
        try:
            self._call("editMessageText", chat_id=chat, message_id=msg_id,
                       text=text[:TG_LIMIT])
        except Exception:
            # «message is not modified» и удалённое сообщение — не повод
            # останавливать присмотр за заданием.
            pass


def split_text(text, limit=TG_LIMIT):
    """Длинный ответ — несколькими сообщениями, по границам строк."""
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) > limit:
            out.append(cur)
            cur = ""
        cur += line
    if cur or not out:
        out.append(cur or " ")
    return out


# --- хранилище ------------------------------------------------------------

class Store:
    """SQLite: лимиты, задания в работе, подписки. Один замок на всё —
    пишут три потока, а записей мало."""

    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS usage(chat INTEGER, day TEXT,
                n INTEGER, PRIMARY KEY(chat, day));
            CREATE TABLE IF NOT EXISTS jobs(job_id INTEGER PRIMARY KEY,
                chat INTEGER, msg INTEGER, created REAL, state TEXT,
                answer TEXT, done INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS subs(chat INTEGER, address TEXT,
                last TEXT, down INTEGER DEFAULT 0,
                PRIMARY KEY(chat, address));
            """)
            self.db.commit()

    def q(self, sql, *args):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.fetchall()

    # лимит: занимается ДО сделки и возвращается, если сделка не прошла
    def take(self, chat, day, limit):
        with self.lock:
            row = self.db.execute("SELECT n FROM usage WHERE chat=? AND day=?",
                                  (chat, day)).fetchone()
            n = row[0] if row else 0
            if n >= limit:
                return False
            self.db.execute("INSERT OR REPLACE INTO usage VALUES(?,?,?)",
                            (chat, day, n + 1))
            self.db.commit()
            return True

    def give_back(self, chat, day):
        self.q("UPDATE usage SET n=MAX(n-1,0) WHERE chat=? AND day=?",
               chat, day)

    def used(self, chat, day):
        r = self.q("SELECT n FROM usage WHERE chat=? AND day=?", chat, day)
        return r[0][0] if r else 0


# --- раздача запросов -----------------------------------------------------

NAME = re.compile(r"^/([0-9a-f]{64}|\d+)\.txt$")


def serve_prompts(directory, port):
    """Отдавать узлам тексты запросов: `<хэш>.txt` и `<номер>.txt`.

    Ничего кроме: ни списка каталога, ни других файлов. Доверия к этому
    адресу узлам не нужно — текст они сверяют с хэшем из задания.
    """
    directory = os.path.abspath(directory)

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            m = NAME.match(self.path)
            p = m and os.path.join(directory, m.group(0)[1:])
            if not p or not os.path.isfile(p):
                self.send_error(404)
                return
            with open(p, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# --- оформление -----------------------------------------------------------

def money(amount, unit):
    """Сумма в целых единицах: uaxm → AXM, BARTR → BARTR, 6 знаков."""
    a = int(amount) / 1_000_000
    s = f"{a:,.6f}".rstrip("0").rstrip(".").replace(",", " ")
    if unit == {"native": "uaxm"} or unit == "uaxm":
        return f"{s} AXM"
    if unit == {"cw20": BARTR}:
        return f"{s} BARTR"
    return f"{amount} {chain.settlement_label(unit)}"


def stage(job, params, checks=None):
    """Что с заданием — словами для человека, а не для оператора."""
    st = str(job.get("state", "")).lower()
    if st == "open":
        return "⏳ ждёт исполнителя"
    if st == "claimed":
        return "⚙️ узел считает ответ"
    if st == "answered":
        n = len(checks) if checks is not None else 0
        return (f"🔍 ответ готов, идёт проверка: {n} из "
                f"{params.checks_required}")
    return client.verdict_line(job, params, checks)


def node_card(addr, rec, params):
    if not rec:
        return f"Узел {addr} в реестре не найден."
    conf = rec.get("conformance")
    return "\n".join([
        f"Узел {addr}",
        f"статус: {rec.get('status')}",
        f"залог: {money(rec.get('stake', 0), params.stake_unit)} "
        f"(нужно не меньше {money(params.min_stake, params.stake_unit)})",
        f"заданий выполнено: {rec.get('jobs_done')}",
        f"обвинений: {rec.get('convictions')}, брошено: {rec.get('abandoned')}",
        f"проба: {'сдана, расхождение ' + str(conf.get('divergence_micro')) if conf else 'не сдана'}",
        f"адрес доставки: {rec.get('endpoint') or 'не объявлен'}",
    ])


WATCH = ("status", "stake", "endpoint", "jobs_done", "convictions",
         "abandoned")


def node_changes(old, new, params):
    """Что изменилось в записи узла — строками. Пусто — ничего."""
    out = []
    if old is None:
        return out
    if new is None:
        return ["⚠️ запись узла пропала из реестра"]
    for k in WATCH:
        a, b = old.get(k), new.get(k)
        if a == b:
            continue
        if k == "stake":
            out.append(f"💰 залог: {money(a or 0, params.stake_unit)} → "
                       f"{money(b or 0, params.stake_unit)}")
        elif k == "convictions":
            out.append(f"🚨 обвинение! было {a}, стало {b} — часть залога "
                       f"конфискована")
        elif k == "abandoned":
            out.append(f"⚠️ брошено заданий: {a} → {b} (взял и не сдал в "
                       f"срок)")
        elif k == "status":
            out.append(f"{'✅' if b == 'active' else '⚠️'} статус: {a} → {b}")
        elif k == "jobs_done":
            out.append(f"✅ выполнено заданий: {a} → {b}")
        else:
            out.append(f"🔗 адрес доставки: {a} → {b}")
    if bool(old.get("conformance")) and not new.get("conformance"):
        out.append("⚠️ проба обнулена — сдайте её заново, иначе заданий не "
                   "будет")
    return out


def reachable(endpoint, timeout=10):
    """Жив ли сервер связок узла. Связки раздаёт `python3 -m http.server`,
    и на несуществующий файл он отвечает 404 — это «жив». Любой другой код
    — нет: снятая машина vast.ai отвечает с того же порта 503 от прокси
    хоста, и «хоть какой-то ответ» принял бы её за живую."""
    try:
        urllib.request.urlopen(endpoint.rstrip("/") + "/0.json",
                               timeout=timeout)
        return True
    except urllib.error.HTTPError as e:
        return e.code == 404
    except Exception:
        return False


HELP = """Бот проверяемого вывода на Axiome.

Задайте вопрос — просто напишите его сообщением. Ответ считает узел сети,
два других узла независимо проверяют его, и только после их вердикта ответ
приходит вам. Обычно это 2–5 минут.

/ask <вопрос> — то же, что просто текст
/job <номер> — что с заданием
/limits — сколько вопросов осталось сегодня
/registry — настройки реестра

Для владельцев узлов:
/node <адрес> — запись узла и подписка на изменения
/unnode <адрес> — отписаться
/nodes — мои подписки"""


# --- бот ------------------------------------------------------------------

class Bot:
    def __init__(self, cfg, api, store, sender, chainmod=chain,
                 fetch=client.fetch_answer, sources=client.answer_sources,
                 ping=reachable, now=time.time):
        self.cfg, self.api, self.store = cfg, api, store
        self.sender, self.chain = sender, chainmod
        self.fetch, self.sources, self.ping, self.now = (fetch, sources,
                                                         ping, now)
        # Одна сделка за раз: у кошелька один номер последовательности, и
        # две одновременные отправки — отказ одной из них.
        self.tx_lock = threading.Lock()
        self._params, self._params_at = None, 0

    def params(self):
        if not self._params or self.now() - self._params_at > 300:
            self._params = self.chain.load_params(self.cfg.rest,
                                                  self.cfg.contract)
            self._params_at = self.now()
        return self._params

    def day(self):
        return time.strftime("%Y-%m-%d", time.gmtime(self.now()))

    # --- входящие ---------------------------------------------------------

    def handle(self, upd):
        m = upd.get("message") or {}
        text = (m.get("text") or "").strip()
        chat = (m.get("chat") or {}).get("id")
        if chat is None or not text:
            return
        if self.cfg.allowed and chat not in self.cfg.allowed \
                and chat not in self.cfg.admins:
            self.api.send(chat, "Бот пока в закрытом тесте.")
            return
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()
        try:
            if cmd in ("/start", "/help"):
                self.api.send(chat, HELP)
            elif cmd == "/limits":
                left = self.cfg.daily - self.store.used(chat, self.day())
                self.api.send(chat, f"Сегодня осталось вопросов: "
                                    f"{max(left, 0)} из {self.cfg.daily}.")
            elif cmd == "/registry":
                self.api.send(chat, self.registry_text())
            elif cmd == "/job":
                self.api.send(chat, self.job_text(arg))
            elif cmd == "/node":
                self.api.send(chat, self.subscribe(chat, arg))
            elif cmd == "/unnode":
                self.store.q("DELETE FROM subs WHERE chat=? AND address=?",
                             chat, arg)
                self.api.send(chat, f"Отписка от {arg}.")
            elif cmd == "/nodes":
                rows = self.store.q("SELECT address FROM subs WHERE chat=?",
                                    chat)
                self.api.send(chat, "\n".join(r[0] for r in rows)
                              or "Подписок нет. /node <адрес>")
            elif cmd == "/balance" and chat in self.cfg.admins:
                self.api.send(chat, self.balance_text())
            elif cmd == "/ask":
                self.ask(chat, arg)
            elif cmd.startswith("/"):
                self.api.send(chat, "Не знаю такой команды. /help")
            else:
                self.ask(chat, text)
        except chain.ChainUnavailable as e:
            self.api.send(chat, f"Цепочка сейчас не отвечает, попробуйте "
                                f"позже. ({str(e)[:150]})")

    def registry_text(self):
        p = self.params()
        price, fee, total = client.totals(p, self.cfg.price)
        return "\n".join([
            f"Реестр {self.cfg.contract}",
            f"модель: {p.pinned_model}, {p.pinned_runtime}, карта {p.pinned_arch}",
            f"проверяющих на задание: {p.checks_required}",
            f"залог узла: не меньше {money(p.min_stake, p.stake_unit)}",
            f"цена вопроса: {money(total, p.settlement)} "
            f"(узлу {money(price, p.settlement)}, проверяющим "
            f"{money(fee, p.settlement)}) — платит бот",
        ])

    def balance_text(self):
        addr = self.sender.address()
        url = (f"{self.cfg.rest.rstrip('/')}/cosmos/bank/v1beta1/balances/"
               f"{addr}")
        with urllib.request.urlopen(url, timeout=20) as r:
            bal = json.loads(r.read()).get("balances", [])
        ax = next((b["amount"] for b in bal if b["denom"] == "uaxm"), "0")
        _, _, total = client.totals(self.params(), self.cfg.price)
        return (f"Кошелёк бота {addr}\n{money(ax, 'uaxm')}, хватит примерно "
                f"на {int(ax) // (total + 1500)} вопросов")

    def job_text(self, arg):
        if not arg.isdigit():
            return "Нужен номер: /job 7"
        p = self.params()
        job = self.chain.load_job(self.cfg.rest, self.cfg.contract, int(arg))
        checks = self.checks(job, int(arg))
        return f"Задание {arg}: {stage(job, p, checks)}"

    def checks(self, job, job_id):
        if str(job.get("state", "")).lower() not in ("answered", "settled"):
            return None
        try:
            return self.chain.checks_of(self.cfg.rest, self.cfg.contract,
                                        job_id)
        except chain.ChainUnavailable:
            return None

    def subscribe(self, chat, addr):
        if not re.fullmatch(r"axm1[02-9ac-hj-np-z]{38,}", addr):
            return "Нужен адрес узла: /node axm1…"
        rec = self.chain.load_executor(self.cfg.rest, self.cfg.contract,
                                       addr)
        if rec:
            self.store.q("INSERT OR REPLACE INTO subs(chat,address,last,down)"
                         " VALUES(?,?,?,0)", chat, addr, json.dumps(rec))
        card = node_card(addr, rec, self.params())
        return card + ("\n\nПодписка включена: сообщу об изменениях."
                       if rec else "")

    # --- вопрос -----------------------------------------------------------

    def ask(self, chat, prompt):
        if not prompt:
            self.api.send(chat, "Напишите вопрос текстом.")
            return
        if len(prompt) > self.cfg.max_chars:
            self.api.send(chat, f"Вопрос длиннее {self.cfg.max_chars} "
                                f"символов — сократите, пожалуйста.")
            return
        day = self.day()
        unlimited = chat in self.cfg.admins
        if not unlimited and not self.store.take(chat, day, self.cfg.daily):
            self.api.send(chat, f"На сегодня вопросы кончились "
                                f"({self.cfg.daily} в сутки). Завтра снова.")
            return
        msg = self.api.send(chat, "📨 Ставлю задание в сеть…")
        try:
            job_id, txhash = self.submit(prompt)
        except Exception as e:
            if not unlimited:
                self.store.give_back(chat, day)
            self.api.edit(chat, msg, f"Не удалось поставить задание: "
                                     f"{str(e)[:300]}")
            return
        self.store.q("INSERT INTO jobs(job_id,chat,msg,created,state) "
                     "VALUES(?,?,?,?,?)", job_id, chat, msg, self.now(),
                     "open")
        self.api.edit(chat, msg, f"Задание {job_id}: ⏳ ждёт исполнителя\n"
                                 f"сделка {txhash}")

    def submit(self, prompt):
        p = self.params()
        ph = chain.text_hash(prompt)
        # Текст выкладывается ДО сделки: задание возьмут сразу, и брать
        # текст узлу должно быть уже откуда.
        os.makedirs(self.cfg.prompts, exist_ok=True)
        with open(os.path.join(self.cfg.prompts, f"{ph}.txt"), "w",
                  encoding="utf-8", newline="") as f:
            f.write(prompt)
        msg, funds, to = client.submit_message(p, ph, self.cfg.price,
                                               self.cfg.prompt_uri)
        if to:
            msg["send"]["contract"] = self.cfg.contract
        with self.tx_lock:
            res = self.sender.send(msg, funds=funds, contract=to)
        job_id = tx.attribute(res.get("tx"), "job_id")
        if job_id is None:
            raise RuntimeError(f"сделка {res.get('txhash')} прошла, но "
                               f"номера задания в ней нет")
        return int(job_id), res.get("txhash")

    # --- присмотр ---------------------------------------------------------

    def tick_jobs(self):
        p = self.params()
        rows = self.store.q("SELECT job_id,chat,msg,created,state,answer "
                            "FROM jobs WHERE done=0")
        for job_id, chat, msg, created, old, answer in rows:
            try:
                job = self.chain.load_job(self.cfg.rest, self.cfg.contract,
                                          job_id)
            except chain.ChainUnavailable:
                continue
            st = str(job.get("state", "")).lower()
            checks = self.checks(job, job_id)
            if st != old or st == "answered":
                self.api.edit(chat, msg,
                              f"Задание {job_id}: {stage(job, p, checks)}")
                self.store.q("UPDATE jobs SET state=? WHERE job_id=?", st,
                             job_id)
            if job.get("answer_hash") and answer is None:
                b, src, why = self.fetch(
                    job, self.sources(self.cfg.rest, self.cfg.contract, job,
                                      ()), job_id)
                if b is not None:
                    answer = b["answer"]
                    self.store.q("UPDATE jobs SET answer=? WHERE job_id=?",
                                 answer, job_id)
            if st == "settled":
                self.finish(chat, job_id, job, answer)
            elif st == "open" and self.now() - created > self.cfg.open_timeout:
                self.cancel(chat, job_id)

    def finish(self, chat, job_id, job, answer):
        g = job.get("guilty")
        if g is False:
            if answer is None:
                text = (f"✅ Задание {job_id}: ответ подтверждён проверяющими, "
                        f"но забрать его у исполнителя не удалось. "
                        f"Попробую ещё раз.")
                self.api.send(chat, text)
                return  # done не ставим: следующий цикл попробует снова
            text = (f"✅ Ответ подтверждён двумя независимыми узлами "
                    f"(задание {job_id}, исполнитель {job.get('executor')})"
                    f"\n\n{answer}")
        elif g is True:
            text = (f"❌ Задание {job_id}: проверяющие признали ответ "
                    f"негодным, исполнитель наказан. Ответ не показываю. "
                    f"Попробуйте спросить ещё раз.")
        else:
            text = (f"⚠️ Задание {job_id} закрыто без вердикта: проверки не "
                    f"собрались в срок. Деньги вернулись, ответ не "
                    f"подтверждён — не показываю.")
        self.api.send(chat, text)
        self.store.q("UPDATE jobs SET done=1 WHERE job_id=?", job_id)

    def cancel(self, chat, job_id):
        try:
            with self.tx_lock:
                self.sender.send({"cancel_job": {"job_id": int(job_id)}})
        except tx.TxFailed as e:
            # Гонка: узел мог взять задание в ту же секунду. Тогда оно уже
            # не open, и следующий цикл увидит его состояние.
            print(f"cancel {job_id}: {e}", file=sys.stderr)
            return
        self.api.send(chat, f"Задание {job_id}: за "
                            f"{self.cfg.open_timeout // 60} мин его никто не "
                            f"взял — сейчас в сети нет свободных узлов. "
                            f"Задание снято, вопрос в лимите возвращён.")
        self.store.give_back(chat, self.day())
        self.store.q("UPDATE jobs SET done=1 WHERE job_id=?", job_id)

    def tick_nodes(self):
        p = self.params()
        rows = self.store.q("SELECT chat,address,last,down FROM subs")
        for chat, addr, last, down in rows:
            try:
                rec = self.chain.query_smart(
                    self.cfg.rest, self.cfg.contract,
                    {"executor": {"address": addr}})
            except chain.ChainUnavailable as e:
                if "not found" not in str(e).lower():
                    continue  # цепочка молчит — это не новость об узле
                rec = None
            old = json.loads(last) if last else None
            lines = node_changes(old, rec, p)
            ep = (rec or {}).get("endpoint")
            if ep:
                ok = self.ping(ep)
                if not ok and down == 1:
                    lines.append(f"🔴 адрес доставки {ep} не отвечает уже "
                                 f"две проверки подряд — задания узел "
                                 f"получит, а связки отдать не сможет")
                if ok and down >= 2:
                    lines.append(f"🟢 адрес доставки {ep} снова отвечает")
                down = 0 if ok else down + 1
            if lines:
                self.api.send(chat, f"Узел {addr}:\n" + "\n".join(lines))
            self.store.q("UPDATE subs SET last=?, down=? WHERE chat=? AND "
                         "address=?", json.dumps(rec) if rec else last, down,
                         chat, addr)


# --- запуск ---------------------------------------------------------------

def every(seconds, fn):
    def run():
        while True:
            try:
                fn()
            except Exception as e:
                print(f"{fn.__name__}: {e}", file=sys.stderr)
            time.sleep(seconds)
    threading.Thread(target=run, daemon=True).start()


def main():
    cfg = Config()
    api = Telegram(cfg.token)
    store = Store(cfg.db)
    sender = tx.Axmd(cfg.contract, cfg.key, chain_id=cfg.chain_id,
                     node=cfg.node, home=cfg.home, gas_prices=cfg.gas_prices)
    bot = Bot(cfg, api, store, sender)
    p = bot.params()
    print(f"кошелёк {sender.address()}, реестр {cfg.contract}, модель "
          f"{p.pinned_model}", file=sys.stderr)
    serve_prompts(cfg.prompts, cfg.port)
    print(f"запросы раздаются на :{cfg.port}, снаружи {cfg.prompt_uri}",
          file=sys.stderr)
    every(15, bot.tick_jobs)
    every(60, bot.tick_nodes)
    offset = None
    while True:
        try:
            for u in api.updates(offset):
                offset = u["update_id"] + 1
                # Вопрос держит поток до минуты (сделка ждёт блока), а
                # остальные сообщения ждать не должны.
                threading.Thread(target=bot.handle, args=(u,),
                                 daemon=True).start()
        except Exception as e:
            print(f"getUpdates: {e}", file=sys.stderr)
            time.sleep(5)


if __name__ == "__main__":
    main()
