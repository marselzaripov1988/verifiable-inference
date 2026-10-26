#!/usr/bin/env python3
"""Демон проверяющего: оценить ту же пару и обязаться своим вектором.

Проверяющий НЕ порождает ответ заново — это было бы избыточным
исполнением с множителем два. Он оценивает готовый текст одним проходом,
что дешевле порождения в 41 раз, и сравнивает свои вероятности с
вероятностями исполнителя.

Обязательство своим вектором нужно не для красоты: без него расхождение
было бы словом проверяющего против слова исполнителя. С обязательством
контракт при споре пересчитывает расхождение сам и ловит того, кто
назвал число, не сходящееся с его же вектором.

ЧТО НУЖНО ДОСТАВИТЬ ПРОВЕРЯЮЩЕМУ, а что нет.

**Вектор исполнителя не нужен.** При правиле «равенство хэшей» он не
участвует в вердикте вовсе: проверяющий оценивает текст и сдаёт СВОЙ
хэш, а сравнивает их контракт. Прежде вектор был обязателен, потому что
демон считал расхождение и по нему решалось дело. Теперь расхождение —
только свидетельство: дали вектор — считается, не дали — сдаётся ноль.
При споре оба вектора раскрываются НА ЦЕПОЧКЕ, то есть доставку там
берёт на себя она.

**А вот запрос и ответ нужны, и их обязательно сверять с цепочкой.**
Текст приходит вне цепочки, и подменить его может кто угодно на пути
доставки. Проверяющий оценил бы не то, его хэш не совпал бы с хэшем
исполнителя, и при нулевом допуске это обвинило бы ЧЕСТНОГО — причём
тихо, выглядело бы как подлог. Двум проверяющим можно подсунуть одну и
ту же подмену, так что правило «не совпало у всех» от этого не спасает.

Поэтому демон СВЕРЯЕТ полученный текст с `prompt_hash` и `answer_hash`
из задания и отказывается работать, если текст не тот. Это и делает
доставку недоверенной: брать текст можно откуда угодно, в том числе у
самого исполнителя. Остаётся вопрос живости — если текст не отдаёт
никто, задание просто не проверить, — но это не вопрос правильности.
"""

import argparse
import json
import sys

import canon
import chain
import vllm_client as vllm


def load_bundle(src):
    """Связка от исполнителя: файл или http(s)-ссылка.

    Откуда взята — неважно, и это главное свойство. Подмена ловится
    сверкой с `prompt_hash` и `answer_hash` из задания, поэтому отдавать
    связку может сам исполнитель, зеркало, сосед — кто угодно. Доверия
    доставка не требует; требует только того, чтобы кто-то отдавал.
    """
    if src.startswith(("http://", "https://")):
        import urllib.request
        req = urllib.request.Request(
            src, headers={"User-Agent": "verifiable-inference-checker/0.1"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    with open(src, encoding="utf-8") as f:
        return json.load(f)


def deadline_note(job, params, now):
    """Что сказать проверяющему про срок сбора проверок.

    Отказа здесь по умолчанию НЕТ, и это разница с исполнителем. У
    исполнителя истёкший срок означает почти верную потерю работы:
    задание выбьют из `Claimed`, и ответ принять будет некуда. У
    проверяющего — нет: пока никто не вызвал `expire_checks`, сданная
    проверка засчитывается, а если она добирает кворум, задание
    закрывается вердиктом и платят всем как обычно. Запрещать такую
    работу значило бы запрещать доводить задания до вердикта.

    Но и молчать нельзя: риск реален — кто угодно может закрыть задание
    сроком, и тогда работа не оплачена. Поэтому предупреждение с числом,
    а отказ — по требованию узла (`--skip-expired`).
    """
    d = chain.deadline_of(job, params, now)
    if d["left"] is None:
        return None, f"задание в состоянии {d['phase'] or '?'}: сроком не ограничено"
    if d["expired"]:
        return True, (f"срок сбора проверок вышел {-d['left']} с назад: "
                      f"работа может не оплатиться — задание закроют "
                      f"возвратом заказчику. Но пока не закрыли, проверка "
                      f"засчитывается и может добрать кворум")
    return False, f"срок сбора проверок: осталось {d['left']} с из {params.check_ttl_seconds}"


def run(base, model, job_id, prompt, answer, producer_vector=None,
        producer_hash=None, params=None, dtype="bfloat16", record=None,
        job=None, now=None, skip_expired=False):
    # Сверка ТЕКСТА с цепочкой — до всякой работы и без исключений:
    # подменённый текст даёт несовпадение хэшей, а это обвинение честного.
    if job is not None:
        bad = chain.job_problems(job, prompt, answer)
        if bad:
            for b in bad:
                print(f"ОТКАЗ: {b}", file=sys.stderr)
            return None
    else:
        print("ВНИМАНИЕ: текст не сверен с цепочкой — только для проверки "
              "самого демона, не для сети", file=sys.stderr)

    # Срок сбора проверок: предупреждение, а не отказ — см.
    # deadline_note. Отказ только если узел сам этого потребовал.
    if job is not None and params is not None and now is not None:
        expired, note = deadline_note(job, params, now)
        if note:
            print(("ВНИМАНИЕ: " if expired else "") + note, file=sys.stderr)
        if expired and skip_expired:
            print("ОТКАЗ: срок вышел, а узел просил такие не брать",
                  file=sys.stderr)
            return None

    # Вектор исполнителя необязателен: в вердикте он не участвует. Но
    # если дан, обязан сходиться со своим обязательством — иначе
    # расхождение считалось бы про чужие данные.
    if producer_vector is not None:
        got = canon.commit_hash(producer_vector)
        if producer_hash and got != producer_hash:
            print(f"ОТКАЗ: вектор исполнителя не сходится с обязательством\n"
                  f"  получен:  {got}\n  обязался: {producer_hash}", file=sys.stderr)
            return None

    if params is not None:
        try:
            actual = chain.normalize_runtime(vllm.server_version(base))
            bad = chain.check_node_matches(params, model, actual, dtype)
        except vllm.VersionUnknown as e:
            bad = [f"версию рантайма определить не удалось: {e}"]
        # Проба нужна и проверяющему: контракт её с него требует. Узел в
        # неверном режиме исполнения завышает расхождение, и это не
        # подлог исполнителя, а ложное обвинение честного.
        if record is not None:
            bad += chain.conformance_problems(params, record)
        if bad:
            for b in bad:
                print(f"ОТКАЗ: {b}", file=sys.stderr)
            return None
    for p in vllm.self_check_problems(base, model):
        print(f"ОТКАЗ: узел проверяющего негоден: {p}", file=sys.stderr)
        return None

    logprobs = vllm.score(base, model, prompt + answer)
    mine = canon.require_nonempty(canon.to_micro(logprobs))
    # Расхождение — свидетельство, а не вердикт. Без вектора исполнителя
    # его не посчитать, и это не мешает: контракт сравнивает хэши.
    d = canon.divergence_micro(producer_vector, mine) if producer_vector else 0

    return {
        "msg": {
            "submit_check": {
                "job_id": job_id,
                "divergence_micro": d,
                "logprobs_hash": canon.commit_hash(mine),
            }
        },
        "divergence_micro": d,
        "vector_len": len(mine),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", required=True)
    p.add_argument("--model", default="Qwen/Qwen2.5-7B-Instruct")
    p.add_argument("--job-id", type=int, required=True)
    p.add_argument("--prompt", required=True)
    p.add_argument("--answer", required=True)
    p.add_argument("--bundle",
                   help="связка от исполнителя: файл или http(s)-ссылка. "
                        "Откуда угодно: подмена ловится сверкой с цепочкой")
    p.add_argument("--producer-vector",
                   help="только вектор, без текста. Необязателен: в вердикте "
                        "вектор не участвует, он нужен лишь для свидетельства")
    p.add_argument("--producer-hash",
                   help="обязательство из цепочки; без него сверка не делается")
    p.add_argument("--rest", help="REST цепочки; с ним порог берётся оттуда")
    p.add_argument("--contract", help="адрес контракта")
    p.add_argument("--dtype", default="bfloat16")
    p.add_argument("--address", help="свой адрес: с ним сверяется состояние пробы")
    p.add_argument("--skip-expired", action="store_true",
                   help="не браться за задания, у которых срок сбора проверок "
                        "уже вышел. По умолчанию демон за них берётся, но "
                        "предупреждает: проверка ещё может добрать кворум")
    a = p.parse_args()

    params = None
    if a.rest and a.contract:
        try:
            params = chain.load_params(a.rest, a.contract)
        except chain.ChainUnavailable as e:
            print(f"ОТКАЗ: параметры из цепочки не получены: {e}", file=sys.stderr)
            return 1

    prompt, answer, pv = a.prompt, a.answer, None
    if a.bundle:
        b = load_bundle(a.bundle)
        # Текст из связки, а не из командной строки: иначе сверять с
        # цепочкой было бы нечего — сверялось бы то, что сам и подставил.
        prompt = b.get("prompt", prompt)
        answer = b.get("answer", answer)
        pv = b.get("vector")
    elif a.producer_vector:
        with open(a.producer_vector, encoding="utf-8") as f:
            pv = json.load(f)["vector"]

    record = job = None
    if params is not None and a.address:
        record = chain.load_executor(a.rest, a.contract, a.address)
    if a.rest and a.contract:
        # Задание из цепочки: в нём хэши текста, без которых доставку
        # нечем проверить.
        try:
            job = chain.load_job(a.rest, a.contract, a.job_id)
        except chain.ChainUnavailable as e:
            print(f"ОТКАЗ: задание из цепочки не получено: {e}",
                  file=sys.stderr)
            return 1
    now = None
    if a.rest and params is not None:
        # Время ЦЕПОЧКИ, не местные часы: срок считает контракт по
        # времени блока.
        try:
            now = chain.chain_time(a.rest)
        except chain.ChainUnavailable as e:
            print(f"ВНИМАНИЕ: время цепочки не получено, срок не проверен: {e}",
                  file=sys.stderr)
    r = run(a.base_url, a.model, a.job_id, prompt, answer, pv,
            a.producer_hash or (job or {}).get("logprobs_hash"),
            params=params, dtype=a.dtype, record=record, job=job, now=now,
            skip_expired=a.skip_expired)
    if r is None:
        return 1
    # Вердикт выносит контракт равенством хэшей, а не демон порогом.
    # Расхождение печатается как свидетельство: по нему видно, НАСКОЛЬКО
    # разошлись, а решает совпадение хэша с обязательством исполнителя.
    d = r["divergence_micro"]
    if a.producer_hash:
        same = r["msg"]["submit_check"]["logprobs_hash"] == a.producer_hash
    else:
        same = None
    verdict = ("хэш совпал" if same else
               "ХЭШ НЕ СОВПАЛ" if same is False else
               "обязательство не передано, сверить нечем")
    print(f"расхождение {d} микродолей; {verdict}", file=sys.stderr)
    print(json.dumps(r["msg"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
