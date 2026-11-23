#!/usr/bin/env python3
"""Кабинет владельца узла — Telegram Mini App для «Кубика».

Что видит владелец видеокарты, открыв кабинет в Telegram:

  * **мои узлы** — адреса, которые он добавил: статус в каждом реестре,
    залог, проба, адрес доставки и жив ли он, сколько заработано как
    исполнителем и как проверяющим, балансы AXM и BARTR;
  * **подключить узел** — выбрать карту (реестр своего класса), записаться,
    внести залог, объявить адрес, запустить образ одной командой;
  * **действия** — донести и забрать залог, сменить адрес.

Ключей кабинет не видит и не хранит. Каждое действие — это готовая
строка `axiomesign://` для Axiome Wallet (Axiome Connect: вставить строку
и подтвердить), тот же формат, что у оплаты в боте, и рядом — та же
сделка командой `axmd`. Подписывает владелец, в своём кошельке.

Всё, что показывает кабинет, прочитано из цепочки: запись узла, задания,
проверки. Своей базы с деньгами нет — заработанное считается по
выплаченным заданиям так же, как их выплачивает контракт (`finalize`):
исполнителю цена, проверяющим `fee_total` поровну.

Кто открыл кабинет, узнаётся по `initData` Telegram — подпись проверяется
токеном бота. Список «моих узлов» — единственное, что кабинет хранит, и
если указан `BOT_DB`, он же становится подпиской бота: тот пишет
владельцу, когда у узла меняется статус, залог или пропадает адрес.

Запуск и настройка — README.md рядом, развёртывание в Telegram — РАЗВЁРТЫВАНИЕ.md.
"""

import base64
import hashlib
import hmac
import http.server
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
from decimal import Decimal, InvalidOperation

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "daemon"))
sys.path.insert(0, os.path.join(HERE, "..", "telegram"))
import chain  # noqa: E402
import sign   # noqa: E402
from bot import BARTR, NODE_IMAGE, GUIDE_URL, reachable  # noqa: E402

#: Карта по классу реестра. Класс — строка `pinned_arch` из цепочки, а
#: охват у неё ровно одна модель карты (см. УЗЕЛ.md, «Класс»).
CARDS = {"sm_89": "RTX 4090", "sm_86": "RTX 3090", "sm_120": "RTX 5090"}

#: Развёрнутые реестры (protocol/УЗЕЛ.md). Тестовый — с залогом 0,3 BARTR:
#: пройти весь путь дёшево, дохода там нет.
DEFAULT_REGISTRIES = (
    "axm1fe5dsxm3vcnwjkvlgjk9mkvqnl6ggpay3y3r94hls7g3hz4s68tqcp547r",
    "axm1svd4ghyg8ku3s9al9atjxlsgclz8jgrnpa6g0day734ldzum3k6qyf8h36",
    "axm1k2v4cjqfl5mqjutnyg5g4cgtatvj835p2sen8myefy40h4pv3ttscnqtwg",
    "axm106jyr6sdutlwhpng0q9qclxfn5kz20e3np594fyqx38fyzuw0xfs0mm3fm",
)
TEST_REGISTRIES = {
    "axm106jyr6sdutlwhpng0q9qclxfn5kz20e3np594fyqx38fyzuw0xfs0mm3fm"}

#: Что рабочему (горячему) ключу разрешено у реестра — раздел 9 УЗЕЛ.md.
#: Ни withdraw_stake, ни set_endpoint: укравший ключ не уведёт залог.
HOT_KEYS = ("claim_job,submit_answer,submit_check,finalize,release_job,"
            "expire_checks")

RPC = "https://api-chain.axiomechain.org"
CHAIN_ID = "axiome-1"


# --- адреса и суммы ---------------------------------------------------------

def valid_address(a, hrp="axm"):
    """bech32 с верной контрольной суммой и нужным префиксом.

    Опечатка в адресе здесь — не мелочь: подписка молча следила бы за
    чужим или несуществующим узлом, а строка залога ушла бы не туда."""
    if not isinstance(a, str) or a != a.lower() or len(a) > 90:
        return False
    if not a.startswith(hrp + "1"):
        return False
    data = a[len(hrp) + 1:]
    if len(data) < 6 or any(c not in sign._CHARSET for c in data):
        return False
    exp = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    return sign._polymod(exp + [sign._CHARSET.index(c) for c in data]) == 1


def to_micro(amount):
    """«1000» или «0,3» в целых токенах → микроединицы. Ошибка — ValueError.

    Пересчёт здесь, а не в странице: число с плавающей точкой в браузере
    дало бы 299999 вместо 300000, и залог не дотянул бы до минимума."""
    try:
        d = Decimal(str(amount).replace(",", ".").replace(" ", ""))
    except InvalidOperation:
        raise ValueError(f"не число: {amount!r}")
    m = d * 1_000_000
    if m <= 0 or m != m.to_integral_value():
        raise ValueError(f"сумма {amount!r}: больше нуля и не точнее "
                         f"шести знаков")
    return int(m)


def unit_label(unit):
    if unit == {"native": "uaxm"}:
        return "AXM"
    if unit == {"cw20": BARTR}:
        return "BARTR"
    return chain.settlement_label(unit)


# --- Telegram ---------------------------------------------------------------

class Unauthorized(Exception):
    pass


def tg_user(init_data, bot_token, now=None, max_age=86400):
    """Проверить `initData` мини-приложения и вернуть id пользователя.

    Правило Telegram: secret = HMAC_SHA256("WebAppData", токен бота),
    hash = HMAC_SHA256(secret, строки key=value без hash, по алфавиту,
    через перевод строки). Подделать без токена бота нельзя."""
    if not init_data:
        raise Unauthorized("нет initData: откройте кабинет из Telegram")
    pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
    got = dict(pairs)
    h = got.pop("hash", None)
    if not h:
        raise Unauthorized("в initData нет подписи")
    check = "\n".join(f"{k}={v}" for k, v in sorted(got.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(),
                      hashlib.sha256).digest()
    want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, h):
        raise Unauthorized("подпись initData не сошлась")
    now = time.time() if now is None else now
    if now - int(got.get("auth_date", 0)) > max_age:
        raise Unauthorized("initData устарел — откройте кабинет заново")
    try:
        return int(json.loads(got.get("user", "{}"))["id"])
    except (ValueError, KeyError, TypeError):
        raise Unauthorized("в initData нет пользователя")


# --- сделки -----------------------------------------------------------------

def axiomesign(contract, msg, memo="", chain_id=CHAIN_ID):
    """Строка Axiome Connect: исполнить `msg` у `contract`. Формат тот же,
    что у оплаты в боте (`bot.pay_link`)."""
    body = {"type": "cosmwasm_execute", "network": chain_id,
            "contract_addr": contract, "funds": [], "msg": msg,
            "memo": memo}
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    return "axiomesign://" + base64.b64encode(raw.encode()).decode()


def axmd_line(contract, msg, frm="$KEY"):
    j = json.dumps(msg, separators=(",", ":"), ensure_ascii=False)
    return (f"axmd tx wasm execute {contract} '{j}' --from {frm} "
            f"--chain-id {CHAIN_ID} --node {RPC} --gas auto "
            f"--gas-adjustment 1.3 --gas-prices 1uaxm -y")


def stake_msg(params, registry, amount):
    """(адрес, сообщение) внесения залога. BARTR — `send` на токен с
    вложенным `deposit_stake`; родная монета формой 5 отвергается."""
    if "cw20" in params.stake_unit:
        hook = base64.b64encode(b'{"deposit_stake":{}}').decode()
        return params.stake_unit["cw20"], {"send": {
            "contract": registry, "amount": str(amount), "msg": hook}}
    raise ValueError(f"залог в {unit_label(params.stake_unit)}: кабинет "
                     f"умеет только CW20 (BARTR)")


def action(params, registry, kind, args):
    """Готовая сделка для владельца: заголовок, строка, команда.

    Возвращает {"title", "axiomesign"?, "axmd", "note"?}. Неверные
    аргументы — ValueError с объяснением для человека."""
    if kind == "register":
        msg = {"register_executor": {
            "model": params.pinned_model, "runtime": params.pinned_runtime,
            "dtype": params.pinned_dtype, "card_class": params.pinned_arch,
            "kernel_cache_hash": params.kernel_cache_hash}}
        return {"title": "Записаться в реестр", "contract": registry,
                "msg": msg, "note": "Подписывайте кошельком, на который "
                "записан узел: на него же придут выплаты и залог."}
    if kind == "stake":
        amount = to_micro(args.get("amount") or
                          params.min_stake / 1_000_000)
        to, msg = stake_msg(params, registry, amount)
        return {"title": f"Внести залог {fmt(amount)} "
                         f"{unit_label(params.stake_unit)}",
                "contract": to, "msg": msg,
                "note": "Залог записывается на адрес, с которого отправлен, "
                        "и забирается обратно целиком, пока узел не пойман "
                        "на подлоге."}
    if kind == "withdraw":
        amount = to_micro(args.get("amount") or 0)
        return {"title": f"Забрать {fmt(amount)} "
                         f"{unit_label(params.stake_unit)} залога",
                "contract": registry,
                "msg": {"withdraw_stake": {"amount": str(amount)}},
                "note": "Пока по узлу есть незакрытые задания, контракт "
                        "откажет. Ниже минимума узел перестанет получать "
                        "задания (проверять сможет)."}
    if kind == "endpoint":
        ep = (args.get("endpoint") or "").strip()
        if ep and not re.fullmatch(r"https?://[^\s/]+(:\d+)?/?", ep):
            raise ValueError("адрес вида http://IP:порт")
        return {"title": "Объявить адрес доставки" if ep else
                         "Снять адрес доставки",
                "contract": registry,
                "msg": {"set_endpoint": {"endpoint": ep or None}},
                "note": "Образ узла объявляет адрес сам при запуске, если "
                        "ключ узла — этот же адрес. С рабочим ключом "
                        "объявите его здесь."}
    if kind == "hot":
        hot = (args.get("hot") or "").strip()
        if not valid_address(hot):
            raise ValueError("адрес рабочего ключа axm1… с верной "
                             "контрольной суммой")
        return {"title": "Разрешить рабочему ключу работать",
                "axmd": (f"axmd tx wasm grant contract {hot} execution "
                         f"{registry} --allow-msg-keys {HOT_KEYS} "
                         f"--max-calls 1000000 --no-token-transfer "
                         f"--expiration 1824000000 --from $KEY "
                         f"--chain-id {CHAIN_ID} --node {RPC} --gas auto "
                         f"--gas-adjustment 1.3 --gas-prices 1uaxm -y"),
                "note": "Axiome Connect такую сделку не принимает — только "
                        "axmd. Рабочему ключу не разрешены ни вывод "
                        "залога, ни смена адреса."}
    if kind == "docker":
        ep = (args.get("endpoint") or "").strip() or "http://ВАШ-IP:8081"
        return {"title": "Запустить узел",
                "axmd": (f"docker run -d --gpus all --restart unless-stopped "
                         f"--name node -p 8081:8081 \\\n"
                         f"  -e CONTRACT={registry} \\\n"
                         f"  -e ENDPOINT={ep} \\\n"
                         f"  -e KEY_HEX=<64 знака ключа узла> \\\n"
                         f"  {NODE_IMAGE}"),
                "note": "Признак, что узел поднялся: в docker logs -f node "
                        "восемь строк «ок:», проба с расхождением 0."}
    raise ValueError(f"неизвестное действие {kind!r}")


def render_action(a):
    """Дополнить действие строкой и командой из (contract, msg)."""
    out = {k: v for k, v in a.items() if k not in ("contract", "msg")}
    if "msg" in a:
        out["axiomesign"] = axiomesign(a["contract"], a["msg"])
        out["axmd"] = axmd_line(a["contract"], a["msg"])
    return out


def fmt(micro):
    s = f"{int(micro) / 1_000_000:,.6f}".rstrip("0").rstrip(".")
    return s.replace(",", " ")


# --- заработок --------------------------------------------------------------

class Ledger:
    """Задания реестра, разобранные на выплаты. Читается из цепочки.

    Выплаченное задание больше не меняется, поэтому разбирается один раз;
    перечитываются только незакрытые. Проверки запрашиваются только у
    выплаченных: там они окончательные."""

    def __init__(self, rest, contract, chainmod=chain):
        self.rest, self.contract, self.chain = rest, contract, chainmod
        self.final = {}    # job_id -> {executor, price, guilty, share, checkers}
        self.open = {}     # job_id -> {executor, state}
        self.low = None    # ниже этого номера всё окончательно
        self.lock = threading.Lock()
        self.at = 0

    def refresh(self):
        start = None if self.low is None else self.low - 1
        open_now, lowest_open = {}, None
        while True:
            jobs, nxt = self.chain.jobs_page(self.rest, self.contract,
                                             start_after=start)
            if not jobs:
                break
            for j in jobs:
                jid = int(j["job_id"])
                job = j.get("job", j)
                start = jid
                if jid in self.final:
                    continue
                st = str(job.get("state", "")).lower()
                if st == "settled":
                    self.final[jid] = self._settled(jid, job)
                else:
                    open_now[jid] = {"executor": job.get("executor"),
                                     "state": st}
                    if lowest_open is None:
                        lowest_open = jid
            if start is not None and start + 1 >= nxt:
                break
        with self.lock:
            self.open = open_now
            self.low = lowest_open if lowest_open is not None else (
                (start + 1) if start is not None else self.low)
            self.at = time.time()

    def _settled(self, jid, job):
        rec = {"executor": job.get("executor"), "guilty": job.get("guilty"),
               "price": int(job.get("price") or 0), "share": 0,
               "checkers": [], "paid": bool(job.get("paid"))}
        if rec["paid"]:
            try:
                checks = self.chain.checks_of(self.rest, self.contract,
                                                jid)
            except chain.ChainUnavailable:
                checks = []
            rec["checkers"] = [c.get("checker") for c in checks]
            if checks:
                rec["share"] = int(job.get("fee_total") or 0) // len(checks)
        return rec

    def summary(self, addr):
        with self.lock:
            final, open_ = dict(self.final), dict(self.open)
        s = {"executed": 0, "earned_exec": 0, "checked": 0,
             "earned_check": 0, "guilty": 0, "in_work": 0}
        for r in final.values():
            if r["executor"] == addr and r["paid"]:
                if r["guilty"] is False:
                    s["executed"] += 1
                    s["earned_exec"] += r["price"]
                elif r["guilty"] is True:
                    s["guilty"] += 1
            if addr in r["checkers"]:
                s["checked"] += 1
                s["earned_check"] += r["share"]
        s["in_work"] = sum(1 for r in open_.values()
                           if r["executor"] == addr)
        s["earned"] = s["earned_exec"] + s["earned_check"]
        return s


# --- хранилище --------------------------------------------------------------

class Store:
    """Единственное, что кабинет помнит: чьи узлы. Если задан `bot_db`,
    добавленный узел становится и подпиской бота (таблица `subs`)."""

    def __init__(self, path, bot_db=None):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.bot_db = bot_db
        self.q("CREATE TABLE IF NOT EXISTS nodes(user INTEGER, address TEXT,"
               " added REAL, PRIMARY KEY(user, address))")

    def q(self, sql, *args):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.fetchall()

    def _bot(self, sql, *args):
        if not self.bot_db:
            return
        try:
            db = sqlite3.connect(self.bot_db, timeout=10)
            db.execute("CREATE TABLE IF NOT EXISTS subs(chat INTEGER, "
                       "address TEXT, last TEXT, down INTEGER DEFAULT 0, "
                       "PRIMARY KEY(chat, address))")
            db.execute(sql, args)
            db.commit()
            db.close()
        except sqlite3.Error as e:
            print(f"подписка бота не записана: {e}", file=sys.stderr)

    def nodes(self, user):
        return [r[0] for r in self.q("SELECT address FROM nodes WHERE user=? "
                                     "ORDER BY added", user)]

    def add(self, user, addr):
        self.q("INSERT OR IGNORE INTO nodes VALUES(?,?,?)", user, addr,
               time.time())
        # В личке с ботом chat == id пользователя: подписка дойдёт.
        self._bot("INSERT OR IGNORE INTO subs(chat,address) VALUES(?,?)",
                  user, addr)

    def remove(self, user, addr):
        self.q("DELETE FROM nodes WHERE user=? AND address=?", user, addr)
        self._bot("DELETE FROM subs WHERE chat=? AND address=?", user, addr)


# --- кабинет ----------------------------------------------------------------

class Cabinet:
    def __init__(self, rest, registries, store, chainmod=chain,
                 ping=reachable, max_nodes=20):
        self.rest, self.registries = rest, list(registries)
        self.store, self.chain, self.ping = store, chainmod, ping
        self.max_nodes = max_nodes
        self.ledgers = {r: Ledger(rest, r, chainmod) for r in registries}
        self._params = {}

    def params(self, reg):
        hit = self._params.get(reg)
        if hit and time.time() - hit[0] < 300:
            return hit[1]
        p = self.chain.load_params(self.rest, reg)
        self._params[reg] = (time.time(), p)
        return p

    def refresh(self):
        for reg, led in self.ledgers.items():
            try:
                led.refresh()
            except chain.ChainUnavailable as e:
                print(f"задания {reg[:14]}… не прочитаны: {e}",
                      file=sys.stderr)

    def registry_info(self, reg):
        p = self.params(reg)
        return {"contract": reg, "card": CARDS.get(p.pinned_arch,
                                                   p.pinned_arch),
                "class": p.pinned_arch, "test": reg in TEST_REGISTRIES,
                "model": p.pinned_model, "runtime": p.pinned_runtime,
                "min_stake": p.min_stake,
                "min_stake_text": fmt(p.min_stake),
                "stake_unit": unit_label(p.stake_unit),
                "pay_unit": unit_label(p.settlement),
                "checks_required": p.checks_required,
                "verifier_fee_bps": p.verifier_fee_bps}

    def registries_info(self):
        out = []
        for reg in self.registries:
            try:
                out.append(self.registry_info(reg))
            except chain.ChainUnavailable as e:
                out.append({"contract": reg, "error": str(e)[:200]})
        return out

    def node(self, addr):
        """Всё об адресе во всех реестрах: запись, шаги, заработок."""
        regs = []
        for reg in self.registries:
            try:
                p = self.params(reg)
            except chain.ChainUnavailable as e:
                regs.append({"contract": reg, "error": str(e)[:200]})
                continue
            rec = self.chain.load_executor(self.rest, reg, addr)
            led = self.ledgers[reg]
            earn = led.summary(addr)
            if rec is None and not earn["checked"]:
                continue
            regs.append(self._registry_node(reg, p, rec, earn))
        return {"address": addr, "registries": regs,
                "balances": self.balances(addr)}

    def _registry_node(self, reg, p, rec, earn):
        rec = rec or {}
        stake = int(rec.get("stake") or 0)
        status = str(rec.get("status", "")).lower() or None
        conf_bad = list(chain.conformance_problems(p, rec)) if rec else [
            "не зарегистрирован"]
        ep = rec.get("endpoint")
        alive = self.ping(ep) if ep else None
        steps = [
            {"id": "register", "title": "Записан в реестр",
             "ok": bool(rec)},
            {"id": "stake", "title": f"Залог не меньше {fmt(p.min_stake)} "
                                     f"{unit_label(p.stake_unit)}",
             "ok": stake >= p.min_stake},
            {"id": "conformance", "title": "Проба на эталоне сдана",
             "ok": bool(rec) and not conf_bad,
             "why": "; ".join(conf_bad) if conf_bad else None},
            {"id": "endpoint", "title": "Адрес доставки объявлен",
             "ok": bool(ep)},
            {"id": "alive", "title": "Адрес доставки отвечает",
             "ok": alive is True},
            {"id": "status", "title": "Не забанен",
             "ok": status != "banned"},
        ]
        ready = all(s["ok"] for s in steps)
        return {"contract": reg, "card": CARDS.get(p.pinned_arch,
                                                   p.pinned_arch),
                "test": reg in TEST_REGISTRIES, "status": status,
                "registered": bool(rec), "ready": ready, "steps": steps,
                "stake": stake, "stake_text": fmt(stake),
                "stake_unit": unit_label(p.stake_unit),
                "min_stake": p.min_stake, "endpoint": ep,
                "jobs_done": rec.get("jobs_done"),
                "abandoned": rec.get("abandoned"),
                "convictions": rec.get("convictions"),
                "earn": dict(earn, earned_text=fmt(earn["earned"]),
                             unit=unit_label(p.settlement))}

    def balances(self, addr):
        out = {}
        try:
            out["AXM"] = fmt(self.chain.balance(self.rest, addr, "uaxm"))
        except chain.ChainUnavailable:
            out["AXM"] = None
        try:
            d = self.chain.query_smart(self.rest, BARTR,
                                       {"balance": {"address": addr}})
            out["BARTR"] = fmt(int(d.get("balance") or 0))
        except chain.ChainUnavailable:
            out["BARTR"] = None
        return out

    def action(self, reg, kind, args):
        if reg not in self.registries:
            raise ValueError("реестр не из списка кабинета")
        return render_action(action(self.params(reg), reg, kind, args))


# --- HTTP -------------------------------------------------------------------

def make_handler(cab, bot_token, dev=False):
    page_path = os.path.join(HERE, "index.html")

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code, body, ctype="application/json"):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _user(self):
            init = self.headers.get("X-Telegram-Init-Data", "")
            if dev and not init:
                return int(self.headers.get("X-Dev-User") or 1)
            return tg_user(init, bot_token)

        def _route(self, method):
            u = urllib.parse.urlsplit(self.path)
            path, qs = u.path, dict(urllib.parse.parse_qsl(u.query))
            if method == "GET" and path in ("/", "/index.html"):
                with open(page_path, "rb") as f:
                    return self._send(200, f.read(),
                                      "text/html; charset=utf-8")
            if not path.startswith("/api/"):
                return self._send(404, {"error": "нет такого пути"})
            user = self._user()
            if method == "GET" and path == "/api/registries":
                return self._send(200, {"registries": cab.registries_info(),
                                        "guide": GUIDE_URL})
            if method == "GET" and path == "/api/me":
                return self._send(200, {"nodes": cab.store.nodes(user)})
            m = re.fullmatch(r"/api/me/nodes/([a-z0-9]+)", path)
            if m and method == "PUT":
                a = m.group(1)
                if not valid_address(a):
                    return self._send(400, {"error": "адрес axm1… с ошибкой"
                                            " — проверьте, что скопирован "
                                            "целиком"})
                if len(cab.store.nodes(user)) >= cab.max_nodes and \
                        a not in cab.store.nodes(user):
                    return self._send(400, {"error": "слишком много узлов"})
                cab.store.add(user, a)
                return self._send(200, {"nodes": cab.store.nodes(user)})
            if m and method == "DELETE":
                cab.store.remove(user, m.group(1))
                return self._send(200, {"nodes": cab.store.nodes(user)})
            m = re.fullmatch(r"/api/node/([a-z0-9]+)", path)
            if m and method == "GET":
                if not valid_address(m.group(1)):
                    return self._send(400, {"error": "адрес с ошибкой"})
                return self._send(200, cab.node(m.group(1)))
            if method == "GET" and path == "/api/action":
                return self._send(200, cab.action(qs.get("registry"),
                                                  qs.get("kind"), qs))
            return self._send(404, {"error": "нет такого пути"})

        def _safe(self, method):
            try:
                self._route(method)
            except Unauthorized as e:
                self._send(401, {"error": str(e)})
            except ValueError as e:
                self._send(400, {"error": str(e)})
            except chain.ChainUnavailable as e:
                self._send(503, {"error": f"цепочка не ответила: "
                                          f"{str(e)[:200]}"})

        def do_GET(self):
            self._safe("GET")

        def do_PUT(self):
            self._safe("PUT")

        def do_DELETE(self):
            self._safe("DELETE")

        def log_message(self, fmt_, *a):
            print(f"{self.address_string()} {fmt_ % a}", file=sys.stderr)

    return H


def main():
    token = os.environ.get("TG_TOKEN", "").strip()
    dev = os.environ.get("DEV", "0") == "1"
    if not token and not dev:
        raise SystemExit("ОТКАЗ: не задан TG_TOKEN — токен бота, которым "
                         "открывается кабинет (им проверяется initData). "
                         "Для проверки без Telegram: DEV=1")
    if dev:
        print("ВНИМАНИЕ: DEV=1 — запросы без initData пускаются как "
              "пользователь X-Dev-User. Не для открытого адреса",
              file=sys.stderr)
    rest = os.environ.get("REST", "https://axm-lcd.trickle.pro")
    regs = [r.strip() for r in os.environ.get(
        "REGISTRIES", ",".join(DEFAULT_REGISTRIES)).split(",") if r.strip()]
    bad = [r for r in regs if not valid_address(r)]
    if bad:
        raise SystemExit(f"ОТКАЗ: в REGISTRIES адреса с ошибкой: {bad}")
    store = Store(os.environ.get("DB", "./cabinet.sqlite"),
                  os.environ.get("BOT_DB") or None)
    cab = Cabinet(rest, regs, store)
    for info in cab.registries_info():
        print(f"реестр {info['contract'][:16]}…: "
              f"{info.get('card', info.get('error'))}", file=sys.stderr)

    def loop():
        while True:
            cab.refresh()
            time.sleep(int(os.environ.get("REFRESH", "60")))
    threading.Thread(target=loop, daemon=True).start()

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8095"))
    srv = http.server.ThreadingHTTPServer((host, port),
                                          make_handler(cab, token, dev))
    print(f"кабинет: http://{host}:{port}/ — снаружи нужен HTTPS (Telegram "
          f"открывает мини-приложения только по https)", file=sys.stderr)
    srv.serve_forever()


if __name__ == "__main__":
    main()
