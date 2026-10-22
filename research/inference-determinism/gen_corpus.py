#!/usr/bin/env python3
"""Породить ответы на корпусе и записать задания. Первая половина замера.

Корпус — 662 РАЗЛИЧНЫХ запроса против прежних сорока. Ответы берутся при
ДВУХ лимитах токенов (48 и 128): запрос один, а текст получается разный
по длине, и полоса длин вектора раздвигается без подтасовки — лимит
токенов это обычный параметр задания.

Пишется всё, что нужно второй половине замера: запрос, ответ, вектор и
его обязательство. Проверяющие потом сверяют вектор с обязательством, как
делает демон, и считают расхождение по той же `canon.divergence_micro`.

    python3 -u gen_corpus.py
"""

import json
import os
import sys
import time

sys.path.insert(0, "/root/protocol/daemon")
import canon  # noqa: E402
import vllm_client as vllm  # noqa: E402

BASE = "http://127.0.0.1:8000/v1"
MODEL = "Qwen/Qwen2.5-7B-Instruct"
CORPUS = "/root/prompts-corpus.json"
CAPS = [int(x) for x in os.environ.get("CAPS", "48,128").split(",")]
JOBS = "/root/corpus-jobs.jsonl"
TEXTS = "/root/corpus-texts.json"

stats = {"generated": 0, "retries": 0, "errors": 0, "started": time.time()}


def score(text):
    import urllib.error
    for _ in range(vllm.SCORE_ATTEMPTS):
        try:
            r = vllm._post(BASE, "/completions", {
                "model": MODEL, "prompt": text, "max_tokens": 0,
                "echo": True, "logprobs": 1,
                "temperature": vllm.TEMPERATURE, "top_p": vllm.TOP_P,
            })
            return r["choices"][0]["logprobs"]["token_logprobs"]
        except urllib.error.HTTPError as e:
            body = b""
            try:
                body = e.read()
            except Exception:
                pass
            if e.code == 400 and b"nan" in body.lower():
                stats["retries"] += 1
                continue
            raise
    raise vllm.ScoringFailed("не отдал за все попытки")


def main():
    prompts = json.load(open(CORPUS, encoding="utf-8"))
    print(f"запросов в корпусе: {len(prompts)}, различных: "
          f"{len(set(prompts))}; лимиты токенов: {CAPS}", flush=True)

    jobs = open(JOBS, "w", encoding="utf-8")
    texts = {}
    n = 0
    for cap in CAPS:
        for p in prompts:
            try:
                answer, toks = vllm.generate(BASE, MODEL, p, cap)
                text = p + answer
                vec = canon.to_micro(score(text))
                if not vec:
                    stats["errors"] += 1
                    continue
                jobs.write(json.dumps({
                    "n": n, "max_tokens": cap, "prompt": p, "answer": answer,
                    "answer_tokens": len(toks), "vector_len": len(vec),
                    "vector": vec, "commit": canon.commit_hash(vec),
                }, ensure_ascii=False) + "\n")
                texts[str(n)] = text
                n += 1
                stats["generated"] += 1
            except Exception as e:
                stats["errors"] += 1
                if stats["errors"] <= 3:
                    print(f"  ошибка: {type(e).__name__}: {e}", flush=True)
            if stats["generated"] % 200 == 0 and stats["generated"]:
                el = (time.time() - stats["started"]) / 60
                print(f"  {stats['generated']} за {el:.1f} мин", flush=True)
    jobs.close()
    json.dump(texts, open(TEXTS, "w", encoding="utf-8"), ensure_ascii=False)

    # Признак того, ради чего брались два лимита: полоса длин вектора.
    lens = sorted(json.loads(l)["vector_len"]
                  for l in open(JOBS, encoding="utf-8"))
    bands = {}
    for L in lens:
        b = min(L // 25 * 25, 250)
        bands[b] = bands.get(b, 0) + 1
    stats["minutes"] = (time.time() - stats["started"]) / 60
    stats["vector_len_min"] = lens[0]
    stats["vector_len_max"] = lens[-1]
    stats["vector_len_median"] = lens[len(lens) // 2]
    stats["vector_len_bands"] = dict(sorted(bands.items()))
    stats["distinct_texts"] = len(set(texts.values()))
    print(json.dumps(stats, ensure_ascii=False, indent=1), flush=True)
    json.dump(stats, open("/root/gen-stats.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
