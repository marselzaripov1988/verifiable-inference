#!/usr/bin/env python3
"""Демон исполнителя: взять задание, породить ответ, обязаться вектором.

Что он публикует и почему именно так:

  * хэш ответа — чтобы ответ нельзя было подменить после проверок;
  * хэш ВЕКТОРА вероятностей — то же для вектора. Сам вектор на цепочку
    не кладётся (~120 чисел на ответ), он отдаётся проверяющим вне её,
    а хэш позволяет любому убедиться, что получен именно обязавшийся;
  * длину ответа в токенах — контракт отклонит короче 32, где способ не
    работает.

Порядок безопасен сам собой: исполнитель публикует ПЕРВЫМ и результата
проверок не знает, поэтому подогнать вектор под них не может.

Сообщения в цепочку этот демон собирает, но не отправляет сам: он
печатает готовый JSON, а отправка — делом вызывающего (axmd tx wasm
execute). Так демон остаётся проверяемым без живой цепочки.
"""

import argparse
import hashlib
import json
import sys

import canon
import chain
import vllm_client as vllm


def preflight(base, params, model, dtype, record=None):
    """Соответствует ли узел тому, что записано в цепочке.

    Версия берётся У СЕРВЕРА, а не со слов исполнителя: при регистрации
    он её только заявляет, и до этой проверки заявленное с
    действительным никто не сверял. Определить не удалось — отказ, а не
    предположение.

    Сверки имён НЕДОСТАТОЧНО: замерено, что при той же модели, той же
    версии и том же dtype неверный режим исполнения расходится с
    эталоном до 413 488 микродолей — внутри полосы подлога. Поэтому
    сюда же входит состояние пробы по данным цепочки, если запись об
    узле передана. Саму пробу считает conformance.py.
    """
    try:
        actual = chain.normalize_runtime(vllm.server_version(base))
    except vllm.VersionUnknown as e:
        return [f"версию рантайма определить не удалось: {e}"]
    bad = chain.check_node_matches(params, model, actual, dtype)
    if record is not None:
        bad += chain.conformance_problems(params, record)
    return bad


def run(base, model, job_id, prompt, max_tokens, out_vector,
        params=None, dtype="bfloat16", record=None):
    if params is not None:
        bad = preflight(base, params, model, dtype, record)
        if bad:
            for b in bad:
                print(f"ОТКАЗ: {b}", file=sys.stderr)
            return None
    for p in vllm.self_check_problems(base, model):
        print(f"ОТКАЗ: {p}", file=sys.stderr)
        return None

    answer, tokens = vllm.generate(base, model, prompt, max_tokens)
    # Оценивается ЦЕЛИКОМ prompt+answer, как в замерах: проверяющий будет
    # оценивать то же самое, и расходиться в составе текста нельзя.
    logprobs = vllm.score(base, model, prompt + answer)
    v = canon.require_nonempty(canon.to_micro(logprobs))

    if out_vector:
        # Связка для проверяющих: ЗАПРОС, ОТВЕТ и вектор в одном файле.
        #
        # Текст тут главное, а вектор — нет. Проверяющему, чтобы работать,
        # нужны именно запрос и ответ: он оценивает их сам и сдаёт свой
        # хэш, а вердикт выносит контракт сравнением хэшей. Вектор лежит
        # рядом только для свидетельства — по нему видно, НАСКОЛЬКО
        # разошлись, — и при споре раскрывается на цепочке.
        #
        # Отдавать связку можно откуда угодно и кому угодно: подмена
        # ловится сверкой с `prompt_hash` и `answer_hash` из задания,
        # которую проверяющий делает до всякой работы. Поэтому доставка
        # не требует доверия — только того, чтобы кто-то её отдавал.
        with open(out_vector, "w", encoding="utf-8") as f:
            json.dump({
                "job_id": job_id,
                "prompt": prompt,
                "answer": answer,
                "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest(),
                "answer_hash": hashlib.sha256(answer.encode()).hexdigest(),
                "logprobs_hash": canon.commit_hash(v),
                "vector": v,
            }, f, ensure_ascii=False)

    msg = {
        "submit_answer": {
            "job_id": job_id,
            "answer_hash": hashlib.sha256(answer.encode()).hexdigest(),
            "logprobs_hash": canon.commit_hash(v),
            "answer_tokens": len(tokens),
        }
    }
    return {"msg": msg, "answer": answer, "vector_len": len(v)}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", required=True)
    p.add_argument("--model", default="Qwen/Qwen2.5-7B-Instruct")
    p.add_argument("--job-id", type=int, required=True)
    p.add_argument("--prompt", required=True)
    p.add_argument("--max-tokens", type=int, default=120)
    p.add_argument("--out-vector", help="куда сложить вектор для проверяющих")
    p.add_argument("--rest", help="REST цепочки; с ним параметры берутся оттуда")
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
        print(f"параметры из цепочки: проверок {params.checks_required}, "
              f"класс {params.pinned_model}/{params.pinned_runtime}/"
              f"{params.pinned_dtype}/{params.pinned_arch}, "
              f"кеш ядер {params.kernel_cache_hash[:16]}…", file=sys.stderr)
    else:
        print("ВНИМАНИЕ: работа без параметров из цепочки — только для "
              "проверки самого демона, не для сети", file=sys.stderr)

    record = None
    if params is not None and a.address:
        record = chain.load_executor(a.rest, a.contract, a.address)
    r = run(a.base_url, a.model, a.job_id, a.prompt, a.max_tokens, a.out_vector,
            params=params, dtype=a.dtype, record=record)
    if r is None:
        return 1
    print(f"ответ: {len(r['answer'])} символов, вектор {r['vector_len']} элементов",
          file=sys.stderr)
    print(json.dumps(r["msg"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
