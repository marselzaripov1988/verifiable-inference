#!/usr/bin/env python3
"""Оценить записанные на корпусе тексты. Вторая половина замера.

Векторы пишутся целиком и сжатыми: расхождение считается между картами,
значит нужны оба набора, а не их хэши.

    CFG=<имя> MODEL=<модель> python3 -u score_corpus.py
"""

import gzip
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, "/root/protocol/daemon")
import canon  # noqa: E402
import vllm_client as vllm  # noqa: E402

BASE = "http://127.0.0.1:8000/v1"
MODEL = os.environ.get("MODEL", "Qwen/Qwen2.5-7B-Instruct")
CFG = os.environ.get("CFG", "cfg")
TEXTS = "/root/corpus-texts.json"
OUT = f"/root/corpus-vectors-{CFG}.json.gz"

stats = {"cfg": CFG, "model": MODEL, "retries": 0, "failed": 0,
         "errors": 0, "started": time.time()}


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
    stats["failed"] += 1
    raise vllm.ScoringFailed("не отдал за все попытки")


def main():
    stats["gpu"] = subprocess.run(
        ["nvidia-smi", "--query-gpu=name,driver_version",
         "--format=csv,noheader"], capture_output=True, text=True).stdout.strip()
    try:
        stats["server_version"] = vllm.server_version(BASE)
    except Exception as e:
        stats["server_version"] = f"неизвестна: {e}"
    import glob
    for p in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            c = open(p, "rb").read().replace(b"\0", b" ").decode(errors="replace")
        except Exception:
            continue
        if "vllm" in c and "serve" in c:
            stats["cmdline"] = c.strip()
            break
    print("окружение:", stats["gpu"], "|", stats["server_version"], flush=True)

    texts = json.load(open(TEXTS, encoding="utf-8"))
    rows = {}
    for i, (k, t) in enumerate(sorted(texts.items(), key=lambda kv: int(kv[0]))):
        try:
            v = canon.to_micro(score(t))
            if v:
                rows[k] = v
            else:
                stats["errors"] += 1
        except Exception as e:
            stats["errors"] += 1
            if stats["errors"] <= 3:
                print(f"  ошибка на {k}: {type(e).__name__}: {e}", flush=True)
        if (i + 1) % 300 == 0:
            el = (time.time() - stats["started"]) / 60
            print(f"  {i+1} из {len(texts)} за {el:.1f} мин, "
                  f"повторов {stats['retries']}", flush=True)

    stats["minutes"] = (time.time() - stats["started"]) / 60
    stats["scored"] = len(rows)
    with gzip.open(OUT, "wt", encoding="utf-8") as f:
        json.dump({"stats": stats, "texts": rows}, f)
    print(json.dumps(stats, ensure_ascii=False), flush=True)
    print("записано:", OUT, flush=True)


if __name__ == "__main__":
    main()
