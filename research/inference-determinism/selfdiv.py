#!/usr/bin/env python3
"""Насколько далеко уводит пересборка ядер. Решающее число.

Уже замерено: на одной карте пересборка ядер даёт другие вероятности на
всех 200 текстах из 200. Остался вопрос величины. Порог протокола —
36 000 миллионных. Если пересборка сдвигает вектор меньше, это помеха;
если больше — честный узел, пересобравший ядра, выглядит жуликом, и
правило надо менять.

Поэтому здесь хранятся ПОЛНЫЕ векторы, а не хэши: расхождение считается
тем же `canon.divergence_micro`, которым его считает контракт.

    GEN=a python3 -u selfdiv.py    # до пересборки
    GEN=b python3 -u selfdiv.py    # после
"""

import json
import os
import sys

sys.path.insert(0, "/root/protocol/daemon")
import canon  # noqa: E402
import vllm_client as vllm  # noqa: E402

BASE = "http://127.0.0.1:8000/v1"
MODEL = "Qwen/Qwen2.5-7B-Instruct"
GEN = os.environ.get("GEN", "a")
OUT = f"/root/selfdiv-{GEN}.json"
N = int(os.environ.get("N", "200"))


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
                continue
            raise
    raise vllm.ScoringFailed("не отдал за все попытки")


texts = json.load(open("/root/texts.json", encoding="utf-8"))[:N]
rows = []
for i, t in enumerate(texts):
    rows.append({"n": t["n"], "v": canon.to_micro(score(t["text"]))})
    if (i + 1) % 50 == 0:
        print(f"  {i+1} из {len(texts)}", flush=True)
json.dump(rows, open(OUT, "w", encoding="utf-8"))
print("записано:", OUT, flush=True)
