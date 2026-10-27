#!/usr/bin/env python3
"""Петля узла на подставной цепочке.

Ни модели, ни узла здесь нет: подменяются цепочка и отправитель.
Проверяется то, из-за чего петля либо работает, либо тихо делает глупость.

Что именно проверяется и почему это стоило проверять:

  * **недоступность цепочки не выглядит концом списка.** Если смешать «нет
    задания с таким номером» с «REST не ответил», упавший шлюз читается
    как пустая очередь, и узел простоит всю неисправность, считая, что
    работы нет. Самая дорогая ошибка из возможных здесь: она ничего не
    ломает и ничего не печатает;
  * **отметка просмотра не перескакивает незакрытое.** Перескочила —
    задание пропущено навсегда: назад петля не смотрит;
  * **одно задание за раз.** Карта одна, и второе взятое означает два
    просроченных;
  * **подменённый текст не берётся в работу.** Доставка недоверенная по
    замыслу, и держится это ровно на сверке с хэшами из цепочки;
  * **чужая связка берётся у того, кто отдал верную**, а не у первого
    попавшегося;
  * **своё задание не проверяется**, а дважды проверенное — не
    перепроверяется: и то и другое контракт отвергнет за газ;
  * **опоздание — не поломка.** Задание, занятое другим, обязано
    оставлять петлю в работе.

Запуск:  python protocol/daemon/test_loop.py
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import canon  # noqa: E402
import chain  # noqa: E402
import loop  # noqa: E402
import tx  # noqa: E402

fails = []


def ok(cond, what):
    print(f"  {'OK  ' if cond else 'ПЛОХО'} {what}")
    if not cond:
        fails.append(what)


ME = "axm1me"
HIM = "axm1him"

#: Ответ ЖИВОГО REST на запрос несуществующего задания, снятый 27 октября
#: с развёрнутого контракта axm1fe5…547r. Переписан дословно, вместе с
#: именем ящика (`inference_registry`, а не имя каталога) и кодом
#: `codespace wasm code 9`: разбор держится на этом тексте, и
#: придуманный мной «похожий» ничего бы не доказывал.
MISSING = (
    "https://axm-lcd.trickle.pro/cosmwasm/wasm/v1/contract/axm1fe5…/smart/"
    "eyJqb2IiOiB7ImpvYl9pZCI6IDF9fQ==: HTTP Error 500: Internal Server "
    'Error: {"code":2,"message":"codespace wasm code 9: query wasm contract '
    "failed: type: inference_registry::state::Job; key: [00, 04, 6A, 6F, 62, "
    '73, 00, 00, 00, 00, 00, 00, 00, 01] not found","details":[]}')

#: Настройки, какие отдаёт развёрнутый контракт.
CFG = {
    "checks_required": 2,
    "pinned_model": "Qwen/Qwen2.5-7B-Instruct",
    "pinned_runtime": "vllm-0.31.0", "pinned_dtype": "bfloat16",
    "pinned_arch": "sm_89", "kernel_cache_hash": "3753554454f2c9b8",
    "settlement": {"native": "uaxm"}, "stake_denom": "uaxm",
    "min_stake": "150000000",
    "conformance_limit_micro": "0",
    "conformance_ref_hash": "0" * 64,
    "conformance_lens": [3, 3],
    "conformance_version": 1,
    "claim_ttl_seconds": 600, "check_ttl_seconds": 3600,
}

def params():
    """Params из тех же настроек, что отдаёт контракт."""
    saved = chain.query_smart
    chain.query_smart = lambda rest, c, q, timeout=20: CFG
    try:
        return chain.load_params("http://rest", "axm1c")
    finally:
        chain.query_smart = saved


def job(state="open", executor=None, prompt="Назови первые пять простых.",
        answer=None, claimed_at=None, answered_at=None, logprobs_hash=None):
    j = {
        "client": "axm1client",
        "prompt_hash": chain.text_hash(prompt),
        "executor": executor,
        "state": state,
        "answer_hash": chain.text_hash(answer) if answer is not None else None,
        "logprobs_hash": logprobs_hash,
        "answer_tokens": 120 if answer is not None else None,
        "guilty": None, "reveal_verified": None,
        "price": "1000", "fee_total": "97", "paid": False,
        "claimed_at": claimed_at, "answered_at": answered_at,
    }
    return j


def record(stake="150000000", status="active", version=1):
    return {"model": CFG["pinned_model"], "runtime": CFG["pinned_runtime"],
            "dtype": "bfloat16", "card_class": "sm_89",
            "kernel_cache_hash": CFG["kernel_cache_hash"],
            "status": status, "jobs_done": 0, "convictions": 0,
            "abandoned": 0, "stake": stake,
            "conformance": {"divergence_micro": "0",
                            "logprobs_hash": "a" * 64,
                            "ref_version": version}}


class FakeChain:
    """Цепочка, которой можно задать задания и сломать связь.

    Подменяется САМЫЙ НИЖНИЙ слой — `query_smart`, — а не разбор над ним.
    Так проверяется настоящий `job_or_none` с настоящим разбором тела
    ошибки, то есть ровно то место, где отсутствие задания отличается от
    неисправности. Подмени я `job_or_none`, это различение осталось бы
    непроверенным.

    `broken` — номера, на которых REST отвечает ошибкой, не связанной с
    отсутствием задания.
    """

    def __init__(self, jobs, checks=None, broken=()):
        self.jobs = dict(jobs)
        self.checks = dict(checks or {})
        self.broken = set(broken)
        self.asked = []

    def query_smart(self, rest, contract, q, timeout=20):
        kind = next(iter(q))
        if kind == "config":
            return CFG
        if kind == "checks":
            n = int(q["checks"]["job_id"])
            return {"checks": list(self.checks.get(n, [])), "mismatched": 0}
        if kind == "executor":
            return record()
        n = int(q["job"]["job_id"])
        self.asked.append(n)
        if n in self.broken:
            raise chain.ChainUnavailable(
                "http://rest/...: HTTP Error 502: Bad Gateway: ")
        if n in self.jobs:
            return self.jobs[n]
        raise chain.ChainUnavailable(MISSING)


REAL_QUERY_SMART = chain.query_smart


def install(fake):
    chain.query_smart = fake.query_smart


def restore():
    chain.query_smart = REAL_QUERY_SMART


# --- сам разбор отсутствия задания ---------------------------------------

def test_otsutstvie_protiv_nedostupnosti():
    print("### нет задания — это не то же, что нет цепочки")
    calls = []

    def missing(rest, contract, q, timeout=20):
        calls.append(q)
        raise chain.ChainUnavailable(MISSING)

    def broken(rest, contract, q, timeout=20):
        raise chain.ChainUnavailable("http://rest/x: HTTP Error 502: Bad Gateway: ")

    saved = chain.query_smart
    try:
        chain.query_smart = missing
        ok(chain.job_or_none("http://rest", "axm1c", 5) is None,
           "отсутствующее задание — None")
        chain.query_smart = broken
        try:
            chain.job_or_none("http://rest", "axm1c", 5)
            ok(False, "недоступность должна пробрасываться")
        except chain.ChainUnavailable:
            ok(True, "недоступность — исключение, а не пустота")
        # Самое важное: 404 по ДРУГОЙ причине (не то задание, другой
        # контракт) тоже не должен читаться как конец очереди.
        chain.query_smart = lambda r, c, q, t=20: (_ for _ in ()).throw(
            chain.ChainUnavailable("http://rest/x: HTTP Error 404: Not Found: "
                                   "contract: not found"))
        try:
            chain.job_or_none("http://rest", "axm1c", 5)
            ok(False, "чужой not found должен пробрасываться")
        except chain.ChainUnavailable:
            ok(True, "not found не про задание — недоступность, а не конец")
    finally:
        chain.query_smart = saved


def test_obhod_i_otmetka():
    print("\n### обход номеров и отметка просмотра")
    p = params()
    f = FakeChain({1: job("settled"), 2: job("settled"),
                   3: job("open"), 4: job("open")})
    install(f)
    try:
        jobs = loop.scan_jobs("http://rest", "axm1c", 1, max_scan=50)
        ok(sorted(jobs) == [1, 2, 3, 4], "найдены все четыре")
        ok(max(f.asked) <= 6, f"лишних запросов нет (дошли до {max(f.asked)})")
        ok(loop.next_watermark(jobs, 1) == 3,
           "отметка встала на первом незакрытом")
        jobs = {1: job("settled"), 2: job("settled"), 3: job("settled")}
        ok(loop.next_watermark(jobs, 1) == 4,
           "все закрыты — отметка за последним")
        jobs = {1: job("answered"), 2: job("settled")}
        ok(loop.next_watermark(jobs, 1) == 1,
           "незакрытое первым — отметка не двигается вовсе")

        print("\n### обрыв связи посреди обхода не двигает отметку")
        f = FakeChain({1: job("settled"), 2: job("open")}, broken=[2])
        install(f)
        try:
            loop.scan_jobs("http://rest", "axm1c", 1, max_scan=50)
            ok(False, "обрыв должен прерывать обход")
        except chain.ChainUnavailable:
            ok(True, "обход прерван, а не признан концом очереди")
    finally:
        restore()
    return p


def test_plan_poryadok():
    print("\n### что петля решает по снимку цепочки")
    p = params()
    now = 10_000

    # Своё взятое задание и чужое свободное: сперва ответ, браться за
    # новое нельзя.
    jobs = {1: job("claimed", executor=ME, claimed_at=now - 10),
            2: job("open")}
    acts = loop.plan(jobs, {}, ME, p, now)
    kinds = [a["do"] for a in acts]
    ok(kinds == ["answer"],
       f"при своём взятом — только ответ, не взятие (вышло {kinds})")

    # Своего нет — берётся самое раннее свободное, и только одно.
    jobs = {1: job("open"), 2: job("open")}
    acts = loop.plan(jobs, {}, ME, p, now)
    ok([a["do"] for a in acts] == ["claim_job"], "берётся ровно одно задание")
    ok(acts[0]["job_id"] == 1, "берётся самое раннее")

    # Чужое сданное — проверка. Своё сданное — нет: контракт отвергнет
    # самопроверку.
    jobs = {1: job("answered", executor=HIM, answered_at=now, answer="a",
                   logprobs_hash="b" * 64),
            2: job("answered", executor=ME, answered_at=now, answer="a",
                   logprobs_hash="c" * 64)}
    acts = loop.plan(jobs, {1: [], 2: []}, ME, p, now)
    ok([(a["do"], a["job_id"]) for a in acts] == [("check", 1)],
       "проверяется чужое, своё — нет")

    # Уже сдавали — второй раз нельзя (DuplicateCheck за газ).
    acts = loop.plan({1: jobs[1]}, {1: [{"checker": ME}]}, ME, p, now)
    ok([a["do"] for a in acts] == [], "своя сданная проверка не повторяется")

    # Кворум собран — сведение, и проверять больше нечего.
    acts = loop.plan({1: jobs[1]},
                     {1: [{"checker": HIM}, {"checker": "axm1third"}]},
                     ME, p, now)
    ok([a["do"] for a in acts] == ["finalize"],
       "при кворуме — сведение, а не ещё проверка")

    # Срок взятого вышел: отбираем чужое, своё не доделываем.
    jobs = {1: job("claimed", executor=HIM, claimed_at=now - 10_000)}
    acts = loop.plan(jobs, {}, ME, p, now)
    ok([a["do"] for a in acts] == ["release_job"],
       "просроченное чужое отбирается")
    jobs = {1: job("claimed", executor=ME, claimed_at=now - 10_000)}
    acts = loop.plan(jobs, {}, ME, p, now)
    ok([a["do"] for a in acts] == ["release_job", "skip"],
       "своё просроченное не доделывается: инференс уже не оплатят")

    # Срок сбора проверок вышел, кворума нет — закрываем сроком.
    jobs = {1: job("answered", executor=HIM, answered_at=now - 10_000,
                   answer="a", logprobs_hash="b" * 64)}
    acts = loop.plan(jobs, {1: []}, ME, p, now)
    ok(acts[0]["do"] == "expire_checks", "просроченный сбор закрывается сроком")

    print("\n### роли ограничиваются по состоянию узла")
    ok(loop.allowed_roles(p, record(), ("executor", "checker"))[0]
       == ("executor", "checker"), "исправный узел — обе роли")
    r, why = loop.allowed_roles(p, record(stake="1"), ("executor", "checker"))
    ok(r == ("checker",), "недобор залога оставляет только проверку")
    ok(why and "залог" in why[0], "сказано про залог")
    r, why = loop.allowed_roles(p, record(status="banned"),
                                ("executor", "checker"))
    ok(r == () and any("забанен" in w for w in why), "бан закрывает обе роли")
    r, why = loop.allowed_roles(p, record(version=0), ("executor", "checker"))
    ok(r == () and any("эталона" in w or "проба" in w for w in why),
       "просроченная проба закрывает обе роли")
    r, why = loop.allowed_roles(p, None, ("executor", "checker"))
    ok(r == (), "незарегистрированный узел не работает")


def test_dostavka_teksta():
    print("\n### текст берётся откуда угодно, но сверяется с цепочкой")
    prompt = "Назови первые пять простых."
    j = job("claimed", executor=ME, prompt=prompt)
    with tempfile.TemporaryDirectory() as d:
        good = os.path.join(d, "good")
        bad = os.path.join(d, "bad")
        os.makedirs(good)
        os.makedirs(bad)
        with open(os.path.join(bad, "1.txt"), "w", encoding="utf-8") as f:
            f.write("Назови первые пять составных.")
        with open(os.path.join(good, "1.txt"), "w", encoding="utf-8") as f:
            f.write(prompt)

        raw, src, why = loop.fetch_verified([bad], "1.txt", loop.prompt_check(j))
        ok(raw is None, "подменённый запрос не берётся")
        ok(why and "хэш запроса не тот" in why[0], "сказано, что хэш не тот")

        raw, src, why = loop.fetch_verified([bad, good], "1.txt",
                                           loop.prompt_check(j))
        ok(raw is not None and src == good,
           "при нескольких источниках берётся сошедшийся")
        ok(loop.prompt_text(raw, j) == prompt, "текст тот самый")

        # Хвостовой перевод строки: редактор добавляет его сам, и решает
        # всё равно хэш из цепочки, а не мы.
        with open(os.path.join(good, "2.txt"), "w", encoding="utf-8") as f:
            f.write(prompt + "\n")
        raw, src, why = loop.fetch_verified([good], "2.txt",
                                           loop.prompt_check(j))
        ok(raw is not None, "файл с переводом строки на конце принят")
        ok(loop.prompt_text(raw, j) == prompt,
           "в работу идёт тот вид, что сошёлся с цепочкой")

        print("\n### связка проверяющего — тоже только сошедшаяся")
        answer = "  2, 3, 5, 7, 11"
        vec = canon.to_micro([-0.5 - i * 0.001 for i in range(100)])
        ja = job("answered", executor=HIM, prompt=prompt, answer=answer,
                 logprobs_hash=canon.commit_hash(vec))
        full = {"job_id": 1, "prompt": prompt, "answer": answer,
                "prompt_hash": chain.text_hash(prompt),
                "answer_hash": chain.text_hash(answer),
                "logprobs_hash": canon.commit_hash(vec), "vector": vec}
        with open(os.path.join(good, "1.json"), "w", encoding="utf-8") as f:
            json.dump(full, f, ensure_ascii=False)
        podmena = dict(full, answer="  2, 3, 5, 7, 13")
        with open(os.path.join(bad, "1.json"), "w", encoding="utf-8") as f:
            json.dump(podmena, f, ensure_ascii=False)
        with open(os.path.join(bad, "2.json"), "w", encoding="utf-8") as f:
            f.write("{не json")

        raw, src, why = loop.fetch_verified([bad], "1.json", loop.bundle_check(ja))
        ok(raw is None, "связка с подменённым ответом отвергнута")
        ok(why and "ответ не тот" in why[0], "названа причина: ответ не тот")
        raw, src, why = loop.fetch_verified([bad, good], "1.json",
                                           loop.bundle_check(ja))
        ok(src == good, "берётся у того, кто отдал верную")
        raw, src, why = loop.fetch_verified([bad], "2.json", loop.bundle_check(ja))
        ok(raw is None and "JSON" in why[0], "испорченный файл — отказ с причиной")
        raw, src, why = loop.fetch_verified([os.path.join(d, "нет")], "1.json",
                                            loop.bundle_check(ja))
        ok(raw is None and "не прочитано" in why[0],
           "отсутствие источника названо отдельно от подмены")


def test_otpravka_i_gonka():
    print("\n### отправка: опоздание — не поломка")
    # Текст дословно из прогона на девнете 27 октября: цепочка отдаёт
    # РУССКОЕ сообщение из error.rs, а не имя варианта. Разбор по именам
    # вариантов стоил прогона — два узла вышли, проиграв гонку.
    ok(tx.classify(
        "68A1…: code=5: failed to execute message; message index: 0: "
        "задание в состоянии Claimed, ожидалось Open: execute wasm "
        "contract failed") == "race",
       "занятое задание — гонка (по русскому тексту цепочки)")
    ok(tx.classify("этот проверяющий уже сдал проверку по заданию") == "race",
       "повторная проверка — гонка")
    ok(tx.classify("проверок уже достаточно") == "race",
       "добранный кворум — гонка")
    ok(tx.classify("по заданию уже выплачено") == "race",
       "сведённое задание — гонка")
    ok(tx.classify("срок не вышел: прошло 10 с из 600") == "race",
       "срок закрыл кто-то другой — гонка")
    ok(tx.classify("WrongJobState") == "race",
       "имя варианта тоже узнаётся: текст могут поменять")
    ok(tx.classify("account sequence mismatch, expected 42, got 41")
       == "retry", "сбой последовательности — повтор")
    ok(tx.classify("залог 1, нужно не меньше 150000000") == "fatal",
       "недобор залога — не гонка, это к оператору")
    ok(tx.classify("исполнитель не допущен к заданиям (статус Banned)")
       == "fatal", "бан — не гонка")
    ok(tx.classify("исполнитель не может проверять своё задание") == "fatal",
       "самопроверка — наша ошибка в расчёте, а не гонка")


def test_tsikl_cherez_podstavnuyu_tsepochku():
    print("\n### цикл целиком, без сети и без модели")
    now = 10_000
    real_time = chain.chain_time
    f = FakeChain({1: job("settled"),
                   2: job("open"),
                   3: job("answered", executor=HIM, answered_at=now,
                          answer="a", logprobs_hash="b" * 64)},
                  checks={3: [{"checker": "axm1x"}, {"checker": "axm1y"}]})
    install(f)
    # Время — единственное, что идёт не через query_smart: оно читается у
    # tendermint напрямую.
    chain.chain_time = lambda rest, timeout=20: now
    try:
        with tempfile.TemporaryDirectory() as d:
            st = os.path.join(d, "state.json")
            sender = tx.DryRun(out=open(os.devnull, "w"))
            sender.address = lambda: ME
            lp = loop.Loop("http://rest", "axm1c", sender,
                           "http://vllm/v1", CFG["pinned_model"],
                           bundles=os.path.join(d, "bundles"),
                           state_path=st,
                           out=open(os.devnull, "w"))
            r = lp.cycle()
            dos = [s["do"] for s in r["sent"]]
            # Задание 3 собрало кворум — его надо свести; задание 2
            # свободно — взять. Проверять 3 уже нельзя.
            ok(dos == ["finalize", "claim_job"],
               f"сведение и взятие, в этом порядке (вышло {dos})")
            ok(json.load(open(st, encoding="utf-8"))["from_id"] == 2,
               "отметка сдвинулась на первое незакрытое")

            print("\n### недоступная цепочка не двигает отметку и не врёт")
            f2 = FakeChain({2: job("open")}, broken=[2])
            install(f2)
            try:
                lp.cycle()
                ok(False, "цикл должен прерваться")
            except chain.ChainUnavailable:
                ok(True, "недоступность пробрасывается в main, где пауза")
            ok(json.load(open(st, encoding="utf-8"))["from_id"] == 2,
               "отметка осталась на месте")
    finally:
        restore()
        chain.chain_time = real_time


def main():
    test_otsutstvie_protiv_nedostupnosti()
    test_obhod_i_otmetka()
    test_plan_poryadok()
    test_dostavka_teksta()
    test_otpravka_i_gonka()
    test_tsikl_cherez_podstavnuyu_tsepochku()
    print()
    if fails:
        print(f"ПРОВАЛ, {len(fails)}:")
        for x in fails:
            print(f"  - {x}")
        return 1
    print("петля ведёт себя как задумано")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
