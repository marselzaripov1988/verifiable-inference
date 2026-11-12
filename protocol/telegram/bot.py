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

**Пополнение.** Сверх бесплатных вопросов человек платит сам: переводит
AXM или BARTR на кошелёк бота с личным кодом в заметке (`/pay`). Задание
всё равно ставит бот от своего кошелька: ответ шифруется ключом заказчика,
и заплати человек в реестр напрямую, бот не смог бы ему ответ показать.

Запуск и настройка — README.md рядом.
"""

import base64
import http.server
import json
import os
import re
import secrets
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
import delivery  # noqa: E402
import sign    # noqa: E402
import tx      # noqa: E402

BARTR = "axm1uzldhz4dvv7k7gtc7jl5ahlj84eghc52p0r6zhnjca0d20dxcfkshsd8uz"
TG_LIMIT = 4000  # у Telegram 4096; запас на заголовок

# Узел не ведёт диалог, а продолжает текст (`/v1/completions`): голый вопрос
# модель принимает за заголовок и дописывает к нему статью. Обёртка в шаблон
# диалога Qwen превращает продолжение в ответ, и модель сама кончает его
# `<|im_end|>`. Узлам и проверке это всё равно: хэш и пересчёт — от того же
# текста, что выложен.
CHAT_TEMPLATES = {
    "qwen": "<|im_start|>user\n{q}<|im_end|>\n<|im_start|>assistant\n",
    # Qwen3 без рассуждений: так шаблон модели собирает ответ при
    # enable_thinking=False — пустым блоком <think>. Без него Qwen3 сначала
    # рассуждает вслух, и ответ съедает весь лимит токенов.
    "qwen3": ("<|im_start|>user\n{q}<|im_end|>\n<|im_start|>assistant\n"
              "<think>\n\n</think>\n\n"),
    "none": "{q}",
}


def template_for(name, model):
    """Шаблон по настройке; `auto` — по модели, которую прибил реестр.

    `auto` нужен для смены модели в реестре: бот подхватывает её сам,
    без перезапуска с другим CHAT_TEMPLATE."""
    if name != "auto":
        return CHAT_TEMPLATES[name]
    return CHAT_TEMPLATES["qwen3" if "qwen3" in (model or "").lower()
                          else "qwen"]


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
        self.explorer = env.get("EXPLORER", "https://axiomechain.org")
        self.template = env.get("CHAT_TEMPLATE", "auto")
        if self.template != "auto" and self.template not in CHAT_TEMPLATES:
            raise SystemExit(f"CHAT_TEMPLATE: одно из "
                             f"auto, {', '.join(CHAT_TEMPLATES)}, а не "
                             f"{self.template!r}")
        self.open_timeout = int(env.get("OPEN_TIMEOUT", "900"))
        self.prompts = env.get("PROMPTS_DIR", "./prompts")
        self.port = int(env.get("PROMPT_PORT", "8080"))
        #: Ключ кошелька бота (64 hex) для подписи запросов связок: узлы
        #: отдают связку только заказчику задания. Без него ответы с узлов
        #: нового образа не забрать.
        self.sign_key = env.get("SIGN_KEY")
        #: Отдавать вопрос и без подписи — только для узлов СТАРОГО образа,
        #: которые не подписывают запросов. Тогда вопрос открытого задания
        #: может скачать любой, кто следит за цепочкой.
        self.allow_unsigned = env.get("ALLOW_UNSIGNED", "0") == "1"
        self.db = env.get("DB", "./bot.sqlite")
        self.admins = {int(x) for x in re.findall(r"-?\d+",
                                                   env.get("ADMINS", ""))}
        self.allowed = {int(x) for x in re.findall(r"-?\d+",
                                                   env.get("ALLOWED", ""))}
        #: Часы работы сети по Москве, «19-21». Пусто — круглосуточно.
        #: Узлы поднимает расписание (protocol/raspisanie), и вопрос вне
        #: часов никто бы не взял: бот снял бы его через OPEN_TIMEOUT.
        self.hours = parse_hours(env.get("HOURS", ""))
        #: Сверх цены задания с платного вопроса берётся на газ бота, uaxm.
        self.pay_gas = int(env.get("PAY_GAS", "3000"))
        #: Цена вопроса в микро-BARTR. 0 — оплата в BARTR выключена: у BARTR
        #: нет пула, и курс к AXM задаёт владелец бота.
        self.price_bartr = int(env.get("PRICE_BARTR", "0"))


MSK = 3 * 3600  # Москва — UTC+3 круглый год


def parse_hours(s):
    """«19-21» → (19, 21). Пусто → None (круглосуточно)."""
    s = (s or "").strip()
    if not s:
        return None
    m = re.fullmatch(r"(\d{1,2})(?::00)?\s*-\s*(\d{1,2})(?::00)?", s)
    if not m or not (0 <= int(m[1]) < int(m[2]) <= 24):
        raise SystemExit(f"HOURS: нужно вроде «19-21», а не {s!r}")
    return int(m[1]), int(m[2])


def is_open(hours, now):
    if not hours:
        return True
    h = time.gmtime(now + MSK).tm_hour
    return hours[0] <= h < hours[1]


def hours_text(hours, now):
    if not hours:
        return "Сеть работает круглосуточно."
    a, b = hours
    head = (f"🕖 Тестовый режим: сеть работает ежедневно с {a}:00 до {b}:00 "
            f"по Москве.")
    if is_open(hours, now):
        return f"{head}\nСейчас открыто — задавайте вопрос."
    t = time.gmtime(now + MSK)
    left = ((a - t.tm_hour) % 24) * 60 - t.tm_min
    return (f"{head}\nСейчас закрыто, откроется через "
            f"{left // 60} ч {left % 60} мин. Вне этих часов узлы выключены, "
            f"и вопрос никто не возьмёт.")


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


TX_LABELS = {
    "submit_job": "объявление и оплата",
    "claim_job": "взято узлом",
    "submit_answer": "ответ сдан",
    "submit_check": "проверка",
    "finalize": "вердикт и выплата",
    "cancel_job": "задание снято",
}


def job_txs(node, contract, job_id, timeout=20):
    """Сделки задания по порядку: [(действие, хэш)].

    Обозреватель по номеру задания не ищет, а без хэшей человеку нечем
    проверить, что вердикт записан в цепочке. Поиск — по событиям `wasm`
    реестра в индексе узла; если индекс молчит, список просто пуст.
    """
    q = (f"\"wasm._contract_address='{contract}' AND "
         f"wasm.job_id={int(job_id)}\"")
    url = (f"{node.rstrip('/')}/tx_search?query={urllib.parse.quote(q)}"
           f"&per_page=50&order_by=%22asc%22")
    # Cloudflare перед RPC отвечает 403 на подпись urllib по умолчанию.
    req = urllib.request.Request(url, headers={"User-Agent": "vi-bot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        txs = json.loads(r.read()).get("result", {}).get("txs") or []
    out = []
    for t in txs:
        for e in t.get("tx_result", {}).get("events", []):
            at = {a.get("key"): a.get("value") for a in e.get("attributes", [])}
            if (e.get("type") == "wasm" and at.get("_contract_address") == contract
                    and at.get("action") in TX_LABELS):
                out.append((at["action"], t["hash"]))
                break
    return out


def links_text(txs, explorer):
    """Блок ссылок на обозреватель; пустая строка, если сделок нет."""
    if not txs:
        return ""
    lines, n = ["🔗 В блокчейне:"], 0
    for action, h in txs:
        label = TX_LABELS[action]
        if action == "submit_check":
            n += 1
            label = f"{label} {n}"
        lines.append(f"{label} — {explorer.rstrip('/')}/transactions/{h}")
    return "\n".join(lines)


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
            CREATE TABLE IF NOT EXISTS codes(chat INTEGER PRIMARY KEY,
                code TEXT UNIQUE);
            CREATE TABLE IF NOT EXISTS credit(chat INTEGER, unit TEXT,
                amount INTEGER, PRIMARY KEY(chat, unit));
            CREATE TABLE IF NOT EXISTS deposits(hash TEXT PRIMARY KEY,
                chat INTEGER, unit TEXT, amount INTEGER, at REAL);
            """)
            # Хэш запроса — чтобы знать, какой файл раздавать и какой
            # стереть. Столбец добавлен 9 ноября; старые базы дополняются.
            cols = [r[1] for r in self.db.execute("PRAGMA table_info(jobs)")]
            if "prompt_hash" not in cols:
                self.db.execute("ALTER TABLE jobs ADD COLUMN prompt_hash TEXT")
            # Чем оплачен вопрос сверх лимита: вернуть ровно это, если
            # ответа человек не получил. Добавлено 10 ноября.
            if "paid_unit" not in cols:
                self.db.execute("ALTER TABLE jobs ADD COLUMN paid_unit TEXT")
                self.db.execute("ALTER TABLE jobs ADD COLUMN paid INTEGER")
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

    # --- пополнение ---

    def code(self, chat):
        """Личный код для заметки перевода: по нему платёж находит чат."""
        while True:
            r = self.q("SELECT code FROM codes WHERE chat=?", chat)
            if r:
                return r[0][0]
            c = "vi-" + "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789")
                                for _ in range(6))
            try:
                self.q("INSERT INTO codes VALUES(?,?)", chat, c)
            except sqlite3.IntegrityError:
                pass  # код занят или чат успел получить свой — ещё круг

    def chat_of(self, code):
        r = self.q("SELECT chat FROM codes WHERE code=?", code)
        return r[0][0] if r else None

    def credit(self, chat):
        return {u: a for u, a in self.q(
            "SELECT unit, amount FROM credit WHERE chat=?", chat)}

    def add_credit(self, chat, unit, amount):
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO credit VALUES(?,?,0)",
                            (chat, unit))
            self.db.execute("UPDATE credit SET amount=amount+? WHERE chat=? "
                            "AND unit=?", (int(amount), chat, unit))
            self.db.commit()

    def charge(self, chat, unit, amount):
        """Списать, если хватает. Под замком: два вопроса разом не
        спишут одни и те же деньги дважды."""
        with self.lock:
            row = self.db.execute("SELECT amount FROM credit WHERE chat=? AND "
                                  "unit=?", (chat, unit)).fetchone()
            if not row or row[0] < amount:
                return False
            self.db.execute("UPDATE credit SET amount=amount-? WHERE chat=? "
                            "AND unit=?", (int(amount), chat, unit))
            self.db.commit()
            return True

    def known_deposits(self):
        return {r[0] for r in self.q("SELECT hash FROM deposits")}

    def add_deposit(self, h, chat, unit, amount, at):
        """Записать сделку один раз. False — уже была."""
        with self.lock:
            try:
                self.db.execute("INSERT INTO deposits VALUES(?,?,?,?,?)",
                                (h, chat, unit, int(amount), at))
            except sqlite3.IntegrityError:
                return False
            self.db.commit()
            return True


# --- раздача запросов -----------------------------------------------------

NAME = re.compile(r"^/([0-9a-f]{64})\.txt$")


def serve_prompts(directory, port, allowed=None, auth=None):
    """Отдавать узлам тексты запросов: только `<хэш>.txt`.

    Ничего кроме: ни списка каталога, ни других файлов. Доверия к этому
    адресу узлам не нужно — текст они сверяют с хэшем из задания.

    `allowed` — функция, возвращающая хэши заданий В РАБОТЕ. Хэш запроса
    лежит в цепочке открыто, поэтому без этого ограничения любой читатель
    цепочки скачал бы все вопросы, и закрытые тоже: так и было до 9
    ноября. Имени `<номер>.txt` больше нет — номера идут подряд, и по ним
    вопросы перебирались бы ещё проще.

    `auth(headers, path, хэш)` — None, если отдавать, иначе (код,
    причина): пускает только участников задания по подписи запроса (см.
    daemon/sign.py). Несуществующий и закрытый — 404 до проверки подписи.
    """
    directory = os.path.abspath(directory)

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            m = NAME.match(self.path)
            p = m and os.path.join(directory, m.group(0)[1:])
            if m and allowed is not None and m.group(1) not in allowed():
                p = None
            if not p or not os.path.isfile(p):
                self.send_error(404)
                return
            if auth is not None:
                denied = auth(self.headers, self.path, m.group(1))
                if denied:
                    self.send_error(*denied)
                    return
            with open(p, "rb") as f:
                body = f.read()
            key = self.headers.get(sign.H_KEY)
            sealed = auth is not None and bool(key)
            if sealed:
                # Подпись уже проверена в auth: ключ — того, кто спросил.
                body = sign.seal(bytes.fromhex(key), body)
            self.send_response(200)
            if sealed:
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header(sign.H_SEALED, "1")
            else:
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
    if unit == {"cw20": BARTR} or unit == "bartr":
        return f"{s} BARTR"
    return f"{amount} {chain.settlement_label(unit)}"


CODE = re.compile(r"\bvi-[a-z0-9]{6}\b")


def pay_link(chain_id, token, recipient, amount, memo):
    """Строка Axiome Connect: перевод CW20 на кошелёк бота с кодом в
    заметке. Тот же формат, что у награды BARTR Distributor."""
    body = {"type": "cosmwasm_execute", "network": chain_id,
            "contract_addr": token, "funds": [],
            "msg": {"transfer": {"recipient": recipient,
                                 "amount": str(int(amount))}},
            "memo": memo}
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    return "axiomesign://" + base64.b64encode(raw.encode()).decode()


def parse_deposit(txj, addr, token):
    """Сделка с LCD → (заметка, единица, сумма). Считается только то, что
    пришло на `addr`: перевод AXM или transfer CW20 токена `token`.
    Возвраты реестра сюда не попадают: это не MsgSend."""
    resp = txj.get("tx_response") or {}
    if int(resp.get("code") or 0) != 0:
        return None, None, 0
    body = (txj.get("tx") or {}).get("body") or {}
    memo = body.get("memo") or ""
    unit, total = None, 0
    for m in body.get("messages") or []:
        t = m.get("@type", "")
        if t.endswith("bank.v1beta1.MsgSend") and m.get("to_address") == addr:
            for c in m.get("amount") or []:
                if c.get("denom") == "uaxm":
                    unit, total = "uaxm", total + int(c.get("amount") or 0)
        elif t.endswith("wasm.v1.MsgExecuteContract") \
                and m.get("contract") == token:
            inner = m.get("msg")
            if isinstance(inner, str):
                try:
                    inner = json.loads(base64.b64decode(inner))
                except Exception:
                    inner = {}
            tr = (inner or {}).get("transfer") or {}
            if tr.get("recipient") == addr:
                unit, total = "bartr", total + int(tr.get("amount") or 0)
    return memo, unit, total


def find_deposits(node, rest, addr, token, skip, timeout=20):
    """Новые входящие сделки кошелька бота: [(хэш, заметка, единица,
    сумма)]. Поиск по индексу RPC, разбор — по JSON с LCD."""
    out = []
    for q in (f"transfer.recipient='{addr}'",
              f"wasm._contract_address='{token}' AND wasm.to='{addr}'"):
        url = (f"{node.rstrip('/')}/tx_search?query="
               f"{urllib.parse.quote(chr(34) + q + chr(34))}"
               f"&per_page=50&order_by=%22desc%22")
        req = urllib.request.Request(url, headers={"User-Agent": "vi-bot/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txs = json.loads(r.read()).get("result", {}).get("txs") or []
        for t in txs:
            h = t.get("hash")
            if not h or h in skip:
                continue
            skip.add(h)
            with urllib.request.urlopen(
                    f"{rest.rstrip('/')}/cosmos/tx/v1beta1/txs/{h}",
                    timeout=timeout) as r:
                memo, unit, amount = parse_deposit(json.loads(r.read()),
                                                   addr, token)
            out.append((h, memo, unit, amount))
    return out


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
/pay — оплатить вопросы сверх бесплатных
/hours — часы работы сети
/registry — настройки реестра

Для владельцев видеокарт:
/gpu — как подключить свою карту и зарабатывать
/node <адрес> — запись узла и подписка на изменения
/unnode <адрес> — отписаться
/nodes — мои подписки"""

#: Образ узла по digest: тег можно перевесить, digest — нет. Это v3,
#: проверенная на 4090 9–10 ноября (protocol/УЗЕЛ.md).
NODE_IMAGE = ("ghcr.io/marselzaripov1988/verifiable-inference-node@sha256:"
              "6a0d4c7906e59dd7faac0e7e5e6f06b54653bde045084634262cbb3e504757a5")
GUIDE_URL = ("https://github.com/marselzaripov1988/verifiable-inference/"
             "blob/main/protocol/%D0%A3%D0%97%D0%95%D0%9B.md")

GPU = """🖥 Как подключить свою видеокарту

Узел отвечает на вопросы и проверяет чужие ответы, за это ему платят из каждого вопроса. Залог держит узел честным: за подменённый ответ он сгорает.

Что нужно:
• RTX 4090 (24 ГБ). Именно эта модель: у этого реестра класс {arch}, кеш ядер и эталон сняты на ней. Для 3090 и 5090 свои реестры — в инструкции.
• Linux, Docker и драйвер NVIDIA; один TCP-порт, открытый наружу.
• Кошелёк Axiome (тип монеты 546, не 118), немного AXM на газ и {stake} на залог.

1. Скачать образ:
docker pull {image}

2. Записаться в реестр и внести залог — команды axmd в разделах 5 и 8 инструкции. Реестр:
{contract}

3. Запустить узел:
docker run -d --gpus all --name node -p 8081:8081 \\
  -e CONTRACT={contract} \\
  -e ENDPOINT=http://ВАШ-IP:8081 \\
  -e KEY_HEX=<ключ рабочего адреса> \\
  {image}
Образ сам проверит карту, кеш ядер и пробу на эталоне. Если что-то не так — откажется работать до всяких денег.

4. Проверить: /node <ваш адрес> — запись узла и уведомления о его заданиях.

Залог забирается обратно (withdraw_stake), пока узел не пойман на подмене. Ключ с залогом на арендованную машину везти не нужно — раздел 9 инструкции.
{hours}
Полная инструкция: {guide}"""


# --- бот ------------------------------------------------------------------

class Bot:
    def __init__(self, cfg, api, store, sender, chainmod=chain,
                 fetch=client.fetch_answer, sources=client.answer_sources,
                 ping=reachable, now=time.time, txs=job_txs,
                 deposits=None):
        self.cfg, self.api, self.store = cfg, api, store
        self.sender, self.chain = sender, chainmod
        self.fetch, self.sources, self.ping, self.now = (fetch, sources,
                                                         ping, now)
        self.txs = txs
        self.deposits = deposits or (lambda addr, skip: find_deposits(
            cfg.node, cfg.rest, addr, BARTR, skip))
        # Одна сделка за раз: у кошелька один номер последовательности, и
        # две одновременные отправки — отказ одной из них.
        self.tx_lock = threading.Lock()
        self._params, self._params_at = None, 0
        #: Хэши, выложенные до сделки и ещё не записанные в базу: между
        #: файлом и строкой в базе проходит сделка, и раздавать текст в
        #: это время уже нужно.
        self.pending = set()
        self.access = sign.Access(cfg.rest, cfg.contract, chainmod, now=now)

    def authorize(self, headers, path, ph):
        """Кому отдавать вопрос: None — отдать, иначе (код, причина)."""
        try:
            who = sign.requester(headers, path, self.now(), self.access.hrp)
        except sign.Unsigned:
            if getattr(self.cfg, "allow_unsigned", False):
                return None
            return 401, "signature required"
        except sign.BadSignature as e:
            return 403, "bad signature"
        rows = self.store.q("SELECT job_id FROM jobs WHERE prompt_hash=? AND "
                            "done=0", ph)
        why = "задание ещё не в цепочке"
        for (job_id,) in rows:
            try:
                job = self.access.job(job_id)
            except chain.ChainUnavailable:
                return 503, "chain unavailable"
            allowed, why = self.access.may_read("prompt", job, who)
            if allowed:
                return None
        print(f"вопрос {ph[:12]}… не отдан: {why}", file=sys.stderr)
        return 403, "not a job participant"

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
        # Любой пробел, а не только ASCII: Telegram на телефоне присылает
        # «/job 9» с неразрывным пробелом, и partition(" ") видел команду
        # «/job\xa09», которой нет.
        parts = text.split(None, 1)
        cmd = parts[0].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        try:
            if cmd in ("/start", "/help"):
                hours = getattr(self.cfg, "hours", None)
                self.api.send(chat, HELP + ("\n\n" + hours_text(hours, self.now())
                                            if hours else ""))
            elif cmd == "/hours":
                self.api.send(chat, hours_text(getattr(self.cfg, "hours", None),
                                               self.now()))
            elif cmd == "/gpu":
                self.api.send(chat, self.gpu_text())
            elif cmd == "/limits":
                left = self.cfg.daily - self.store.used(chat, self.day())
                self.api.send(chat, f"Сегодня осталось бесплатных вопросов: "
                                    f"{max(left, 0)} из {self.cfg.daily}."
                                    + self.credit_line(chat))
            elif cmd == "/pay":
                self.api.send(chat, self.pay_text(chat, arg))
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
            f"{money(fee, p.settlement)}) — бесплатные платит бот, "
            f"сверх них /pay",
        ])

    def gpu_text(self):
        try:
            p = self.params()
            arch, stake = p.pinned_arch, money(p.min_stake, p.stake_unit)
        except chain.ChainUnavailable:
            arch, stake = "sm_89", "залог реестра (/registry)"
        hours = getattr(self.cfg, "hours", None)
        return GPU.format(
            arch=arch, stake=stake, image=NODE_IMAGE,
            contract=self.cfg.contract, guide=GUIDE_URL,
            hours=(f"\nЗадания в тестовом режиме идут с {hours[0]}:00 до "
                   f"{hours[1]}:00 по Москве.\n" if hours else ""))

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
        links = self.links(int(arg))
        text = f"Задание {arg}: {stage(job, p, checks)}"
        return f"{text}\n\n{links}" if links else text

    def links(self, job_id):
        try:
            return links_text(self.txs(self.cfg.node, self.cfg.contract,
                                       job_id), self.cfg.explorer)
        except Exception as e:
            # Ссылки — довесок: без них состояние всё равно сообщается.
            print(f"сделки задания {job_id}: {e}", file=sys.stderr)
            return ""

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

    # --- пополнение -------------------------------------------------------

    def prices(self):
        """Цена платного вопроса: {единица: микро}. AXM — цена задания в
        реестре плюс газ бота; BARTR — по курсу владельца, если задан."""
        out = {}
        p = self.params()
        if p.settlement == {"native": "uaxm"}:
            _, _, total = client.totals(p, self.cfg.price)
            out["uaxm"] = total + getattr(self.cfg, "pay_gas", 3000)
        if getattr(self.cfg, "price_bartr", 0) > 0:
            out["bartr"] = self.cfg.price_bartr
        return out

    def charge(self, chat):
        """Списать один вопрос: сначала AXM, потом BARTR. None — нечем."""
        for unit, price in self.prices().items():
            if self.store.charge(chat, unit, price):
                return unit, price
        return None

    def give_back(self, chat, day, unlimited, paid):
        if paid:
            self.store.add_credit(chat, *paid)
        elif not unlimited:
            self.store.give_back(chat, day)

    def refund(self, job_id):
        """Ответа человек не получил — вернуть оплату вопроса, если была.
        Один раз: сумма в строке задания обнуляется."""
        r = self.store.q("SELECT chat, paid_unit, paid FROM jobs WHERE "
                         "job_id=?", job_id)
        if not r or not r[0][1] or not r[0][2]:
            return None
        chat, unit, amount = r[0]
        self.store.q("UPDATE jobs SET paid=0 WHERE job_id=?", job_id)
        self.store.add_credit(chat, unit, amount)
        return unit, amount

    def credit_line(self, chat):
        c = {u: a for u, a in self.store.credit(chat).items() if a > 0}
        if not c:
            return "\nОплаченных вопросов нет: /pay"
        parts, n = [], 0
        prices = self.prices()
        for unit, a in c.items():
            parts.append(money(a, unit))
            if prices.get(unit):
                n += a // prices[unit]
        return (f"\nНа счёте: {', '.join(parts)} — это {n} вопросов "
                f"сверх лимита.")

    def pay_text(self, chat, arg):
        prices = self.prices()
        if not prices:
            return "Оплата вопросов сейчас не принимается."
        n = int(arg) if arg.isdigit() and 0 < int(arg) <= 1000 else 10
        code = self.store.code(chat)
        addr = self.sender.address()
        lines = [f"💳 Вопросы сверх бесплатных {self.cfg.daily} в сутки",
                 "Цена вопроса: " + " или ".join(
                     money(v, u) for u, v in prices.items()) + ".",
                 f"Ваш код: {code} — он идёт в заметку (memo) перевода. "
                 f"Без кода платёж не найдёт ваш чат."]
        if "bartr" in prices:
            lines += ["", f"BARTR одной строкой, на {n} вопросов "
                      f"({money(n * prices['bartr'], 'bartr')}): Axiome "
                      f"Wallet → Axiome Connect → вставить строку и "
                      f"подтвердить.",
                      pay_link(getattr(self.cfg, "chain_id", "axiome-1"),
                               BARTR, addr, n * prices["bartr"], code)]
        if "uaxm" in prices:
            lines += ["", f"AXM — обычным переводом на адрес бота с заметкой "
                      f"{code}:", addr,
                      f"на {n} вопросов — {money(n * prices['uaxm'], 'uaxm')}."]
        lines += ["", "Деньги зачислятся через минуту-две после перевода, "
                  "я напишу. Если ответ на оплаченный вопрос не пришёл, "
                  "оплата возвращается на счёт." + self.credit_line(chat)]
        return "\n".join(lines)

    def tick_deposits(self, initial=False):
        """Входящие переводы кошелька бота → счёт чата по коду в заметке.

        `initial` — первый проход на базе без единой записи о переводах:
        всё, что уже лежит в истории кошелька (пополнения владельцем,
        возвраты реестра), помечается увиденным без зачисления и писем.
        """
        addr = self.sender.address()
        skip = self.store.known_deposits()
        fresh = initial and not skip
        for h, memo, unit, amount in self.deposits(addr, skip):
            m = CODE.search((memo or "").lower())
            chat = self.store.chat_of(m.group(0)) if m and unit else None
            if fresh:
                chat, amount = None, 0
            if not self.store.add_deposit(h, chat, unit, amount, self.now()):
                continue
            if chat is not None and amount > 0:
                self.store.add_credit(chat, unit, amount)
                self.api.send(chat, f"✅ Пришло {money(amount, unit)}."
                                    + self.credit_line(chat))
            elif unit and amount > 0:
                for a in self.cfg.admins:
                    self.api.send(a, f"Пришло {money(amount, unit)} без "
                                     f"кода в заметке (сделка {h}, заметка "
                                     f"{(memo or '—')[:60]!r}). Не зачислено.")

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
        hours = getattr(self.cfg, "hours", None)
        if not unlimited and not is_open(hours, self.now()):
            # Отказ ДО лимита и сделки: вопрос никто бы не взял, а бот
            # заплатил бы газ за постановку и снятие.
            self.api.send(chat, hours_text(hours, self.now()))
            return
        paid = None  # (единица, сумма), если вопрос сверх лимита
        if not unlimited and not self.store.take(chat, day, self.cfg.daily):
            paid = self.charge(chat)
            if paid is None:
                self.api.send(chat, f"Бесплатные вопросы на сегодня кончились "
                                    f"({self.cfg.daily} в сутки). Завтра снова "
                                    f"— или оплатите вопросы: /pay")
                return
        msg = self.api.send(chat, "📨 Ставлю задание в сеть…" + (
            f" (оплачено {money(paid[1], paid[0])})" if paid else ""))
        try:
            job_id, txhash, ph = self.submit(prompt)
        except Exception as e:
            self.give_back(chat, day, unlimited, paid)
            self.api.edit(chat, msg, f"Не удалось поставить задание: "
                                     f"{str(e)[:300]}")
            return
        self.store.q("INSERT INTO jobs(job_id,chat,msg,created,state,"
                     "prompt_hash,paid_unit,paid) VALUES(?,?,?,?,?,?,?,?)",
                     job_id, chat, msg, self.now(), "open", ph,
                     paid[0] if paid else None, paid[1] if paid else None)
        self.pending.discard(ph)
        self.api.edit(chat, msg, f"Задание {job_id}: ⏳ ждёт исполнителя\n"
                                 f"сделка {txhash}")

    def submit(self, question):
        p = self.params()
        prompt = template_for(self.cfg.template,
                              p.pinned_model).format(q=question)
        ph = chain.text_hash(prompt)
        # Текст выкладывается ДО сделки: задание возьмут сразу, и брать
        # текст узлу должно быть уже откуда.
        os.makedirs(self.cfg.prompts, exist_ok=True)
        path = os.path.join(self.cfg.prompts, f"{ph}.txt")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(prompt)
        self.pending.add(ph)
        msg, funds, to = client.submit_message(p, ph, self.cfg.price,
                                               self.cfg.prompt_uri)
        if to:
            msg["send"]["contract"] = self.cfg.contract
        try:
            with self.tx_lock:
                res = self.sender.send(msg, funds=funds, contract=to)
        except Exception:
            # Задания нет — и текст лежать не должен.
            self.pending.discard(ph)
            if ph not in self.active_hashes():
                self._remove(path)
            raise
        job_id = tx.attribute(res.get("tx"), "job_id")
        if job_id is None:
            raise RuntimeError(f"сделка {res.get('txhash')} прошла, но "
                               f"номера задания в ней нет")
        return int(job_id), res.get("txhash"), ph

    # --- тексты вопросов --------------------------------------------------

    def active_hashes(self):
        """Хэши запросов, которые ещё нужны узлам: задание не закрыто."""
        rows = self.store.q("SELECT prompt_hash FROM jobs WHERE done=0 "
                            "AND prompt_hash IS NOT NULL")
        return {r[0] for r in rows} | set(self.pending)

    @staticmethod
    def _remove(path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    def forget(self, job_id):
        """Задание закрыто: стереть текст вопроса и сохранённый ответ.

        Ответ человеку уже отправлен, а держать вопросы и ответы впрок —
        значит держать то, что можно украсть с сервера. Файл стирается,
        только если тот же вопрос не задан в другом открытом задании.
        """
        rows = self.store.q("SELECT prompt_hash FROM jobs WHERE job_id=?",
                            job_id)
        self.store.q("UPDATE jobs SET answer=NULL WHERE job_id=?", job_id)
        ph = rows[0][0] if rows else None
        if ph and ph not in self.active_hashes():
            self._remove(os.path.join(self.cfg.prompts, f"{ph}.txt"))

    def purge(self):
        """При запуске: стереть всё, что не нужно заданиям в работе.

        У строк, записанных до появления столбца `prompt_hash`, хэш берётся
        из цепочки; не вышло — файлы не трогаются, чтобы не оставить узел
        без текста посреди задания.
        """
        unknown = self.store.q("SELECT job_id FROM jobs WHERE done=0 AND "
                               "prompt_hash IS NULL")
        for (job_id,) in unknown:
            try:
                job = self.chain.load_job(self.cfg.rest, self.cfg.contract,
                                          job_id)
            except chain.ChainUnavailable:
                print(f"чистка: задание {job_id} не прочитано — файлы "
                      f"не трогаю", file=sys.stderr)
                return 0
            self.store.q("UPDATE jobs SET prompt_hash=? WHERE job_id=?",
                         job.get("prompt_hash"), job_id)
        self.store.q("UPDATE jobs SET answer=NULL WHERE done=1")
        keep = self.active_hashes()
        n = 0
        if os.path.isdir(self.cfg.prompts):
            for name in os.listdir(self.cfg.prompts):
                m = re.fullmatch(r"([0-9a-f]{64}|\d+)\.txt", name)
                if m and m.group(1) not in keep:
                    self._remove(os.path.join(self.cfg.prompts, name))
                    n += 1
        return n

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
        if g is not False:
            back = self.refund(job_id)
            if back:
                text += f" Оплата {money(back[1], back[0])} возвращена на счёт."
        links = self.links(job_id)
        if links:
            text = f"{text}\n\n{links}"
        self.api.send(chat, text)
        self.store.q("UPDATE jobs SET done=1 WHERE job_id=?", job_id)
        self.forget(job_id)

    def cancel(self, chat, job_id):
        try:
            with self.tx_lock:
                self.sender.send({"cancel_job": {"job_id": int(job_id)}})
        except tx.TxFailed as e:
            # Гонка: узел мог взять задание в ту же секунду. Тогда оно уже
            # не open, и следующий цикл увидит его состояние.
            print(f"cancel {job_id}: {e}", file=sys.stderr)
            return
        back = self.refund(job_id)
        self.api.send(chat, f"Задание {job_id}: за "
                            f"{self.cfg.open_timeout // 60} мин его никто не "
                            f"взял — сейчас в сети нет свободных узлов. "
                            f"Задание снято, "
                            + (f"{money(back[1], back[0])} возвращены на счёт."
                               if back else "вопрос в лимите возвращён."))
        if not back:
            self.store.give_back(chat, self.day())
        self.store.q("UPDATE jobs SET done=1 WHERE job_id=?", job_id)
        self.forget(job_id)

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
    n = bot.purge()
    if n:
        print(f"стёрто текстов закрытых заданий: {n}", file=sys.stderr)
    if cfg.sign_key:
        key = sign.load_key(cfg.sign_key)
        mine = sign.address(sign.pubkey(key))
        if mine != sender.address():
            raise SystemExit(f"SIGN_KEY даёт {mine}, а кошелёк бота "
                             f"{sender.address()}: узлы не отдадут связки")
        delivery.set_sign_key(key)
    else:
        print("ВНИМАНИЕ: SIGN_KEY не задан — связки с узлов нового образа "
              "не забрать", file=sys.stderr)
    if cfg.allow_unsigned:
        print("ВНИМАНИЕ: ALLOW_UNSIGNED=1 — вопрос открытого задания отдаётся "
              "без подписи", file=sys.stderr)
    serve_prompts(cfg.prompts, cfg.port, allowed=bot.active_hashes,
                  auth=bot.authorize)
    print(f"запросы раздаются на :{cfg.port}, снаружи {cfg.prompt_uri}",
          file=sys.stderr)
    every(15, bot.tick_jobs)
    try:
        bot.tick_deposits(initial=True)
    except Exception as e:
        print(f"пополнения: {e}", file=sys.stderr)
    every(30, bot.tick_deposits)
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
