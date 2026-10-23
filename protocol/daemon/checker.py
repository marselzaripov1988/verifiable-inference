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

Вектор исполнителя берётся вне цепочки — в цепочке только его хэш.
Поэтому первым делом демон СВЕРЯЕТ полученный вектор с обязательством и
отказывается работать, если он не тот.
"""

import argparse
import json
import sys

import canon
import chain
import vllm_client as vllm


def run(base, model, job_id, prompt, answer, producer_vector, producer_hash,
        params=None, dtype="bfloat16", record=None):
    # Сверка до всякой работы: если вектор не тот, которым обязались,
    # считать по нему нечего — результат был бы про чужие данные.
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
    d = canon.divergence_micro(producer_vector, mine)

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
    p.add_argument("--producer-vector", required=True,
                   help="файл от исполнителя: {job_id, vector}")
    p.add_argument("--producer-hash",
                   help="обязательство из цепочки; без него сверка не делается")
    p.add_argument("--rest", help="REST цепочки; с ним порог берётся оттуда")
    p.add_argument("--contract", help="адрес контракта")
    p.add_argument("--dtype", default="bfloat16")
    p.add_argument("--address", help="свой адрес: с ним сверяется состояние пробы")
    a = p.parse_args()

    params = None
    if a.rest and a.contract:
        try:
            params = chain.load_params(a.rest, a.contract)
        except chain.ChainUnavailable as e:
            print(f"ОТКАЗ: параметры из цепочки не получены: {e}", file=sys.stderr)
            return 1

    with open(a.producer_vector, encoding="utf-8") as f:
        pv = json.load(f)["vector"]

    record = None
    if params is not None and a.address:
        record = chain.load_executor(a.rest, a.contract, a.address)
    r = run(a.base_url, a.model, a.job_id, a.prompt, a.answer, pv,
            a.producer_hash, params=params, dtype=a.dtype, record=record)
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
