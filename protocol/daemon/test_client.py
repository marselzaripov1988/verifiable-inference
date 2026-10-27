#!/usr/bin/env python3
"""Заказчик на подставной цепочке.

Проверяется то, из-за чего заказчик либо получает ответ, либо платит зря:

  * **сумма считается точно.** Контракт требует `price + доля` ни больше
    ни меньше, доля округляется ВВЕРХ. Ошибка на единицу — отвергнутая
    сделка и потерянный газ, и ошибка эта незаметная: на «круглых» ценах
    округление не проявляется вовсе;
  * **запрос выложен ДО отправки.** Номер заданию присваивает контракт;
    выложи запрос после — и задание возьмут раньше, чем появится текст;
  * **CW20 идёт на токен, а не в реестр.** Напрямую реестр такие деньги
    не примет, и сообщение другой формы;
  * **ответ не берётся на слово.** Он приходит вне цепочки, и подменённый
    обязан быть отвергнут до того, как заказчик его увидит;
  * **«ответ сдан» и «ответ подтверждён» — не одно и то же.** Склей их, и
    заказчик примет непроверенное за проверенное;
  * **взятое задание не отменяется.** Иначе заказчик забирал бы деньги у
    работающего узла.

Запуск:  python protocol/daemon/test_client.py
"""

import argparse
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import canon  # noqa: E402
import chain  # noqa: E402
import client  # noqa: E402

fails = []


def ok(cond, what):
    print(f"  {'OK  ' if cond else 'ПЛОХО'} {what}")
    if not cond:
        fails.append(what)


CFG = {
    "checks_required": 2,
    "pinned_model": "Qwen/Qwen2.5-7B-Instruct",
    "pinned_runtime": "vllm-0.31.0", "pinned_dtype": "bfloat16",
    "pinned_arch": "sm_89", "kernel_cache_hash": "k",
    "settlement": {"native": "uaxm"}, "stake_denom": "uaxm",
    "min_stake": "30400000000", "verifier_fee_bps": 485,
    "conformance_limit_micro": "0", "conformance_ref_hash": "0" * 64,
    "conformance_lens": [3, 3], "conformance_version": 1,
    "claim_ttl_seconds": 600, "check_ttl_seconds": 3600,
}

REAL_QUERY = chain.query_smart


def params(cfg=None):
    chain.query_smart = lambda rest, c, q, timeout=20: cfg or CFG
    try:
        return chain.load_params("http://rest", "axm1c")
    finally:
        chain.query_smart = REAL_QUERY


def test_summa():
    print("### сумма: округление вверх, как в контракте")
    p = params()
    # Доля 485 bps. 1000000 * 485 / 10000 = 48500 ровно — здесь ошибку в
    # округлении не увидеть, и ровно поэтому проверяются ещё и «неровные»
    # цены.
    ok(client.totals(p, 1_000_000) == (1_000_000, 48_500, 1_048_500),
       "ровная цена: 1000000 + 48500")
    # 7 * 485 / 10000 = 0,3395 -> контракт берёт 1, а не 0.
    ok(client.totals(p, 7) == (7, 1, 8),
       f"малая цена округляется вверх (вышло {client.totals(p, 7)})")
    ok(client.totals(p, 1) == (1, 1, 2), "цена 1 стоит 2: доля не бывает нулём")
    # Та же арифметика, что у цепочки: chain.fee_for — один код на всех.
    ok(chain.fee_for(1_000_001, 485) == 48_501,
       "на единицу больше цены — на единицу больше доли")
    ok(chain.fee_for(0, 485) == 0, "нулевая цена — нулевая доля")
    p970 = params(dict(CFG, verifier_fee_bps=970))
    ok(client.totals(p970, 1_000_000)[1] == 97_000,
       "доля берётся ИЗ ЦЕПОЧКИ, а не из представления о ней")


def test_soobshenie():
    print("\n### сообщение: родной расчёт и CW20 — разные пути")
    p = params()
    msg, funds, to = client.submit_message(p, "ph", 1_000_000)
    ok(set(msg) == {"submit_job"}, "родной расчёт: submit_job реестру")
    ok(msg["submit_job"] == {"prompt_hash": "ph", "price": "1000000"},
       "поля ровно те, что ждёт контракт")
    ok(funds == "1048500uaxm", f"монеты идут с транзакцией (вышло {funds})")
    ok(to is None, "получатель — сам реестр")

    pc = params(dict(CFG, settlement={"cw20": "axm1token"}))
    msg, funds, to = client.submit_message(pc, "ph", 1_000_000)
    ok(to == "axm1token", "CW20: сообщение идёт НА ТОКЕН")
    ok(funds is None, "при CW20 родных монет с транзакцией нет")
    ok(set(msg) == {"send"} and msg["send"]["amount"] == "1048500",
       "это send, и сумма в нём")
    import base64
    hook = json.loads(base64.b64decode(msg["send"]["msg"]))
    ok(hook == {"submit_job": {"prompt_hash": "ph", "price": "1000000"}},
       "вложенное сообщение — то же submit_job")

    try:
        client.submit_message(params(dict(CFG, settlement={"бред": 1})),
                              "ph", 1)
        ok(False, "неизвестный расчёт должен отвергаться")
    except chain.ChainUnavailable:
        ok(True, "неизвестный вид расчёта — отказ, а не догадка")


def test_vykladka_do_otpravki():
    print("\n### запрос выложен ДО отправки, под именем своего хэша")
    p = params()
    prompt = "Опиши по шагам, что происходит при отправке транзакции."
    ph = chain.text_hash(prompt)
    seen = {}

    class Sender:
        """Отправитель, который смотрит, что уже лежит на диске."""

        def address(self):
            return "axm1client"

        def send(self, msg, funds=None, contract=None):
            seen["был_файл"] = os.path.exists(os.path.join(d, f"{ph}.txt"))
            seen["msg"] = msg
            return {"txhash": "AB", "code": 0, "tx": {"events": [
                {"type": "wasm", "attributes": [
                    {"key": "action", "value": "submit_job"},
                    {"key": "job_id", "value": "7"}]}]}}

    with tempfile.TemporaryDirectory() as d:
        pf = os.path.join(d, "вопрос.txt")
        with open(pf, "w", encoding="utf-8", newline="") as f:
            f.write(prompt)
        a = argparse.Namespace(
            prompt_file=pf, prompt=None, price=1_000_000, prompts=d,
            contract="axm1reg")
        rc = client.cmd_submit(a, p, Sender())
        ok(rc == 0, "submit отработал")
        ok(seen.get("был_файл") is True,
           "к моменту отправки запрос УЖЕ выложен")
        ok(seen["msg"]["submit_job"]["prompt_hash"] == ph,
           "в сделке хэш именно выложенного текста")
        with open(os.path.join(d, f"{ph}.txt"), encoding="utf-8",
                  newline="") as f:
            ok(f.read() == prompt, "файл побайтово тот же: хвост не дописан")
        ok(os.path.exists(os.path.join(d, "7.txt")),
           "после отправки появилось и имя по номеру — для человека")


def test_otvet_ne_na_slovo():
    print("\n### ответ сверяется с цепочкой, а не берётся на слово")
    prompt = "Назови первые пять простых."
    answer = "  2, 3, 5, 7, 11"
    vec = canon.to_micro([-0.5 - i * 0.001 for i in range(100)])
    job = {"state": "settled", "guilty": False,
           "prompt_hash": chain.text_hash(prompt),
           "answer_hash": chain.text_hash(answer),
           "logprobs_hash": canon.commit_hash(vec)}
    good = {"job_id": 1, "prompt": prompt, "answer": answer,
            "prompt_hash": job["prompt_hash"],
            "answer_hash": job["answer_hash"],
            "logprobs_hash": job["logprobs_hash"], "vector": vec}

    with tempfile.TemporaryDirectory() as d:
        liar, honest = os.path.join(d, "liar"), os.path.join(d, "honest")
        os.makedirs(liar)
        os.makedirs(honest)
        with open(os.path.join(liar, "1.json"), "w", encoding="utf-8") as f:
            json.dump(dict(good, answer="  2, 3, 5, 7, 13"), f,
                      ensure_ascii=False)
        with open(os.path.join(honest, "1.json"), "w", encoding="utf-8") as f:
            json.dump(good, f, ensure_ascii=False)

        b, src, why = client.fetch_answer(job, [liar], 1)
        ok(b is None, "подменённый ответ отвергнут")
        ok(any("ответ не тот" in w for w in why), "сказано, что ответ не тот")
        b, src, why = client.fetch_answer(job, [liar, honest], 1)
        ok(b is not None and b["answer"] == answer,
           "из двух источников взят сошедшийся с цепочкой")
        # Ответ на ДРУГОЙ запрос: хэш ответа сойдётся, а запроса — нет.
        chuzhoy = dict(job, prompt_hash=chain.text_hash("другой вопрос"))
        b, src, why = client.fetch_answer(chuzhoy, [honest], 1)
        ok(b is None and any("запрос не тот" in w for w in why),
           "ответ на другой запрос не принимается")


def test_verdikt_otdelno_ot_otveta():
    print("\n### «сдан» и «подтверждён» — разные вещи")
    p = params()
    ok("никто не взял" in client.verdict_line({"state": "open"}, p),
       "объявленное")
    ok("ответа ещё нет" in client.verdict_line(
        {"state": "claimed", "executor": "axm1n"}, p), "взятое")
    line = client.verdict_line({"state": "answered"}, p, checks=[{"c": 1}])
    ok("вердикта ещё нет" in line and "1 из 2" in line,
       f"сданный ответ без кворума (вышло {line!r})")
    ok("подтверждён" in client.verdict_line(
        {"state": "settled", "guilty": False}, p), "вердикт: не виновен")
    ok("виновным" in client.verdict_line(
        {"state": "settled", "guilty": True}, p), "вердикт: виновен")
    # Закрытое сроком: вердикта НЕТ, и это не «не виновен».
    line = client.verdict_line({"state": "settled", "guilty": None}, p)
    ok("БЕЗ вердикта" in line,
       f"закрытое сроком не выдаётся за подтверждённое (вышло {line!r})")


def test_otmena():
    print("\n### взятое задание отменить нельзя")
    p = params()
    saved = chain.load_job
    try:
        class Sender:
            def __init__(self):
                self.sent = []

            def address(self):
                return "axm1client"

            def send(self, msg, funds=None, contract=None):
                self.sent.append(msg)
                return {"txhash": "AB", "code": 0, "tx": {}}

        chain.load_job = lambda r, c, j: {"state": "claimed",
                                          "executor": "axm1n"}
        s = Sender()
        a = argparse.Namespace(rest="http://r", contract="axm1c", job_id=1)
        ok(client.cmd_cancel(a, p, s) == 1, "взятое задание — отказ")
        ok(s.sent == [], "и сделка не отправлена вовсе")

        chain.load_job = lambda r, c, j: {"state": "open"}
        s = Sender()
        ok(client.cmd_cancel(a, p, s) == 0, "невзятое забирается")
        ok(s.sent == [{"cancel_job": {"job_id": 1}}], "сообщение то самое")
    finally:
        chain.load_job = saved


def main():
    test_summa()
    test_soobshenie()
    test_vykladka_do_otpravki()
    test_otvet_ne_na_slovo()
    test_verdikt_otdelno_ot_otveta()
    test_otmena()
    print()
    if fails:
        print(f"ПРОВАЛ, {len(fails)}:")
        for x in fails:
            print(f"  - {x}")
        return 1
    print("заказчик ведёт себя как задумано")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
