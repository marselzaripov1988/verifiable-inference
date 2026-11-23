#!/usr/bin/env python3
"""Кабинет узла на подставной цепочке, через настоящий HTTP.

Проверяется то, из-за чего владелец либо видит правду и подписывает
верную сделку, либо теряет деньги:

  * `initData` Telegram принимается только с верной подписью и свежий;
  * адрес с опечаткой не принимается — ни в «мои узлы», ни в права;
  * суммы пересчитываются в микроединицы точно, без плавающей точки;
  * сделки — те же, что в УЗЕЛ.md: запись с полями класса из цепочки,
    залог — `send` на BARTR с вложенным `deposit_stake`, рабочему ключу
    без вывода залога и смены адреса;
  * заработок считается как выплачивает контракт: исполнителю цена,
    проверяющим `fee_total` поровну, виновному ничего;
  * шаги готовности узла отражают запись в цепочке;
  * добавленный узел становится подпиской бота.

Запуск:  python protocol/cabinet/test_cabinet.py
"""

import base64
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402
import chain   # noqa: E402
import sign    # noqa: E402

BARTR = server.BARTR
REG = server.DEFAULT_REGISTRIES[0]
TEST = server.DEFAULT_REGISTRIES[3]
NODE = sign.address(sign.pubkey(bytes.fromhex("11" * 32)))
CHK = sign.address(sign.pubkey(bytes.fromhex("22" * 32)))
OTHER = sign.address(sign.pubkey(bytes.fromhex("33" * 32)))
TOKEN = "123456:TEST-token"

PARAMS = chain.Params(
    checks_required=1, pinned_model="Qwen/Qwen2.5-7B-Instruct",
    pinned_runtime="vllm-0.31.0", pinned_dtype="bfloat16",
    pinned_arch="sm_89", kernel_cache_hash="ab" * 32,
    settlement={"native": "uaxm"}, stake_unit={"cw20": BARTR},
    min_stake=1_000_000_000, verifier_fee_bps=485,
    conformance_limit_micro=0, conformance_ref_hash="cd" * 32,
    conformance_lens=(1,), claim_ttl_seconds=600, check_ttl_seconds=3600,
    conformance_version=1)

FAILS = []


def ok(cond, what):
    print(("ок   " if cond else "ПРОВАЛ ") + what)
    if not cond:
        FAILS.append(what)


def init_data(user, token=TOKEN, auth_date=None):
    fields = {"auth_date": str(int(auth_date or time.time())),
              "query_id": "AAH", "user": json.dumps({"id": user,
                                                       "first_name": "В"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(),
                              hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(fields)


class Chain:
    ChainUnavailable = chain.ChainUnavailable

    def __init__(self):
        self.execs = {}
        self.jobs = {}     # id -> job
        self.checks = {}   # id -> [checker]
        self.page = 2
        self.check_calls = 0

    def load_params(self, rest, contract):
        return PARAMS

    def load_executor(self, rest, contract, addr):
        return self.execs.get((contract, addr))

    def jobs_page(self, rest, contract, start_after=None):
        ids = sorted(i for i in self.jobs
                     if start_after is None or i > start_after)[:self.page]
        nxt = max(self.jobs, default=0) + 1
        return [{"job_id": i, "job": self.jobs[i]} for i in ids], nxt

    def checks_of(self, rest, contract, jid):
        self.check_calls += 1
        return [{"checker": c} for c in self.checks.get(jid, [])]

    def balance(self, rest, addr, denom):
        return 2_500_000

    def query_smart(self, rest, contract, q):
        return {"balance": "1000000000"}


def job(state, executor, price=10000, fee=485, guilty=None, paid=False):
    return {"state": state, "executor": executor, "price": str(price),
            "fee_total": str(fee), "guilty": guilty, "paid": paid}


def main():
    tmp = tempfile.mkdtemp()
    try:
        run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if FAILS:
        print(f"\nпровалов: {len(FAILS)}")
        return 1
    print("\nкабинет ведёт себя как задумано")
    return 0


def run(tmp):
    # --- Telegram ---------------------------------------------------------
    ok(server.tg_user(init_data(42), TOKEN) == 42,
       "initData с верной подписью даёт пользователя")
    for bad, why in ((init_data(42, token="0:other"), "чужой токен"),
                     (init_data(42).replace("42", "43"), "подменён id"),
                     (init_data(42, auth_date=time.time() - 90000),
                      "устаревший"),
                     ("", "пустой")):
        try:
            server.tg_user(bad, TOKEN)
            ok(False, f"initData отвергнут: {why}")
        except server.Unauthorized:
            ok(True, f"initData отвергнут: {why}")

    # --- адреса и суммы ---------------------------------------------------
    ok(server.valid_address(NODE) and server.valid_address(REG),
       "верные адреса принимаются")
    typo = NODE[:-1] + ("q" if NODE[-1] != "q" else "p")
    ok(not server.valid_address(typo), "опечатка в последнем знаке ловится")
    ok(not server.valid_address(NODE.upper())
       and not server.valid_address("cosmos1" + NODE[4:]),
       "чужой префикс и верхний регистр не принимаются")
    ok(server.to_micro("0,3") == 300000 and server.to_micro("1000")
       == 1_000_000_000 and server.to_micro("76 000") == 76_000_000_000,
       "0,3 / 1000 / 76 000 → точные микроединицы")
    for bad in ("0", "-1", "abc", "0.0000001"):
        try:
            server.to_micro(bad)
            ok(False, f"сумма {bad!r} отвергнута")
        except ValueError:
            ok(True, f"сумма {bad!r} отвергнута")

    # --- сделки -----------------------------------------------------------
    a = server.render_action(server.action(PARAMS, REG, "register", {}))
    body = json.loads(base64.b64decode(a["axiomesign"][len("axiomesign://"):]))
    msg = body["msg"]["register_executor"]
    ok(body["contract_addr"] == REG and body["type"] == "cosmwasm_execute"
       and body["network"] == "axiome-1" and body["funds"] == [],
       "запись: строка Axiome Connect на реестр")
    ok(msg == {"model": "Qwen/Qwen2.5-7B-Instruct", "runtime": "vllm-0.31.0",
               "dtype": "bfloat16", "card_class": "sm_89",
               "kernel_cache_hash": "ab" * 32},
       "запись: все пять полей класса — из цепочки")
    a = server.render_action(server.action(PARAMS, REG, "stake", {}))
    body = json.loads(base64.b64decode(a["axiomesign"][len("axiomesign://"):]))
    send = body["msg"]["send"]
    ok(body["contract_addr"] == BARTR and send["contract"] == REG
       and send["amount"] == "1000000000"
       and json.loads(base64.b64decode(send["msg"])) == {"deposit_stake": {}},
       "залог: send на BARTR, по умолчанию минимум, внутри deposit_stake")
    ok(f"axmd tx wasm execute {BARTR}" in a["axmd"],
       "залог: команда axmd на контракт токена")
    a = server.render_action(server.action(PARAMS, REG, "withdraw",
                                           {"amount": "500"}))
    ok('"withdraw_stake":{"amount":"500000000"}' in a["axmd"],
       "вывод: 500 BARTR → 500000000")
    a = server.render_action(server.action(PARAMS, REG, "hot", {"hot": NODE}))
    ok("axiomesign" not in a and "withdraw_stake" not in a["axmd"]
       and "set_endpoint" not in a["axmd"] and f"contract {NODE} execution "
       f"{REG}" in a["axmd"],
       "права рабочему ключу: только axmd, без вывода залога и смены адреса")
    try:
        server.action(PARAMS, REG, "hot", {"hot": typo})
        ok(False, "права на адрес с опечаткой не выдаются")
    except ValueError:
        ok(True, "права на адрес с опечаткой не выдаются")
    try:
        server.action(PARAMS, REG, "endpoint", {"endpoint": "1.2.3.4; rm"})
        ok(False, "адрес доставки без http:// отвергнут")
    except ValueError:
        ok(True, "адрес доставки без http:// отвергнут")
    a = server.action(PARAMS, REG, "docker", {"endpoint": "http://1.2.3.4:40001"})
    ok(f"CONTRACT={REG}" in a["axmd"] and "ENDPOINT=http://1.2.3.4:40001"
       in a["axmd"] and "@sha256:" in a["axmd"],
       "команда запуска: реестр, адрес и образ по digest")

    # --- заработок --------------------------------------------------------
    ch = Chain()
    ch.jobs = {
        1: job("settled", NODE, guilty=False, paid=True),       # ответ
        2: job("settled", OTHER, guilty=False, paid=True),      # проверка
        3: job("settled", NODE, guilty=True, paid=True),        # пойман
        4: job("settled", OTHER, guilty=None),                  # снято
        5: job("answered", NODE),                               # в работе
        6: job("settled", OTHER, fee=970, guilty=False, paid=True),
    }
    ch.checks = {1: [CHK], 2: [NODE], 3: [CHK], 6: [NODE, CHK]}
    led = server.Ledger("rest", REG, ch)
    led.refresh()
    s = led.summary(NODE)
    ok(s["executed"] == 1 and s["earned_exec"] == 10000,
       f"исполнитель: одно подтверждённое, цена 10000 ({s})")
    ok(s["checked"] == 2 and s["earned_check"] == 485 + 485,
       "проверяющий: 485 целиком и половина от 970")
    ok(s["guilty"] == 1 and s["in_work"] == 1,
       "пойманное и незакрытое учтены отдельно, без денег")
    calls = ch.check_calls
    ch.jobs[5] = job("settled", NODE, guilty=False, paid=True)
    ch.checks[5] = [CHK]
    ch.jobs[7] = job("open", None)
    led.refresh()
    s = led.summary(NODE)
    ok(s["executed"] == 2 and s["in_work"] == 0,
       "второй проход дочитал закрывшееся задание")
    ok(ch.check_calls - calls == 1,
       "выплаченные задания второй раз не перечитываются")

    # --- узел -------------------------------------------------------------
    store = server.Store(os.path.join(tmp, "c.sqlite"),
                         bot_db=os.path.join(tmp, "bot.sqlite"))
    ch.execs[(REG, NODE)] = {"status": "active", "stake": "1000000000",
                             "endpoint": "http://1.2.3.4:8081",
                             "conformance": {"ref_version": 1,
                                             "divergence_micro": 0},
                             "jobs_done": 2, "abandoned": 0,
                             "convictions": 1}
    pinged = []
    cab = server.Cabinet("rest", [REG, TEST], store, chainmod=ch,
                         ping=lambda ep: pinged.append(ep) or True)
    cab.ledgers[REG] = led
    d = cab.node(NODE)
    regs = d["registries"]
    ok(len(regs) == 1 and regs[0]["contract"] == REG,
       "узел показан только в реестре, где он есть")
    steps = {x["id"]: x["ok"] for x in regs[0]["steps"]}
    ok(steps["register"] and steps["stake"] and steps["endpoint"]
       and steps["alive"] and steps["status"],
       f"шаги готовности по записи из цепочки ({steps})")
    ok(pinged == ["http://1.2.3.4:8081"], "адрес доставки проверен на жизнь")
    ok(d["balances"] == {"AXM": "2.5", "BARTR": "1 000"},
       f"балансы AXM и BARTR ({d['balances']})")
    ch.execs[(REG, NODE)]["stake"] = "999999999"
    steps = {x["id"]: x["ok"] for x in cab.node(NODE)["registries"][0]
             ["steps"]}
    ok(not steps["stake"], "залог на единицу меньше минимума — шаг не пройден")
    ok(cab.node(OTHER)["registries"] == [],
       "незаписанный адрес без проверок — реестров нет")

    # --- HTTP -------------------------------------------------------------
    srv = ThreadingHTTPServer(("127.0.0.1", 0),
                              server.make_handler(cab, TOKEN))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(method, path, user=7):
        req = urllib.request.Request(base + path, method=method)
        if user is not None:
            req.add_header("X-Telegram-Init-Data", init_data(user))
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if r.headers.get(
                    "Content-Type", "").startswith("application/json")
                    else raw)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    code, page = call("GET", "/", user=None)
    ok(code == 200 and b"telegram-web-app.js" in page,
       "страница мини-приложения отдаётся без initData")
    code, r = call("GET", "/api/me", user=None)
    ok(code == 401, "API без initData — 401")
    code, r = call("PUT", f"/api/me/nodes/{typo}")
    ok(code == 400, "адрес с опечаткой не добавлен")
    code, r = call("PUT", f"/api/me/nodes/{NODE}")
    ok(code == 200 and r["nodes"] == [NODE], "узел добавлен")
    code, r = call("GET", "/api/me", user=8)
    ok(r["nodes"] == [], "у другого пользователя свой список")
    db = sqlite3.connect(os.path.join(tmp, "bot.sqlite"))
    ok(db.execute("SELECT chat,address FROM subs").fetchall() == [(7, NODE)],
       "добавленный узел — подписка бота в личке пользователя")
    code, r = call("GET", f"/api/node/{NODE}")
    ok(code == 200 and r["registries"][0]["earn"]["earned"] == 20970,
       f"заработок по HTTP: 2 ответа + 2 проверки = 20970 uaxm")
    code, r = call("GET", f"/api/action?registry={REG}&kind=stake&amount=0,3")
    ok(code == 200 and r["axiomesign"].startswith("axiomesign://"),
       "действие по HTTP — строка Axiome Connect")
    code, r = call("GET", f"/api/action?registry={OTHER}&kind=stake")
    ok(code == 400, "реестр не из списка — отказ")
    code, r = call("DELETE", f"/api/me/nodes/{NODE}")
    ok(r["nodes"] == [] and db.execute("SELECT COUNT(*) FROM subs")
       .fetchone()[0] == 0, "узел убран — и подписка бота тоже")
    code, r = call("GET", "/api/registries")
    ok(code == 200 and r["registries"][0]["card"] == "RTX 4090"
       and r["registries"][1]["test"] is True,
       "реестры: карта по классу, тестовый помечен")
    srv.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
