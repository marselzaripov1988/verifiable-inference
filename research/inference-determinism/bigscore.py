#!/usr/bin/env python3
"""Снять векторы на 6 000 текстах и на 34 текстах пробы. Один проход.

Зачем 6 000, а не 200. Прежний перемер порога стоял на выборке из 200
текстов, где худшее честное и минимум подлога — крайние значения двухсот
наблюдений. Зазор вышел 1,34x, и этого мало, чтобы менять число в
контракте: на большем наборе честное идёт вверх, подлог вниз.

Заодно снимается эталон пробы. Прежний снят 13 октября при ВКЛЮЧЁННОМ
выборе ядра по замеру, то есть привязан к одной удачной компиляции.

Вектор пишется целиком и сжатым: расхождение считается между картами,
значит нужны оба набора, а не их хэши.

    CFG=honest-3090 python3 -u bigscore.py
"""

import gzip
import json
import os
import sys
import time

sys.path.insert(0, "/root/protocol/daemon")
import canon  # noqa: E402
import vllm_client as vllm  # noqa: E402

BASE = "http://127.0.0.1:8000/v1"
MODEL = os.environ.get("MODEL", "Qwen/Qwen2.5-7B-Instruct")
CFG = os.environ.get("CFG", "cfg")
TEXTS = "/root/texts6k.json"
PROBE = "/root/protocol/daemon/probe/probe.json"
OUT = f"/root/vectors-{CFG}.json.gz"

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


def run(items, key):
    rows = {}
    for i, t in enumerate(items):
        try:
            rows[str(t[key])] = canon.to_micro(score(t["text"]))
        except Exception as e:
            stats["errors"] += 1
            if stats["errors"] <= 3:
                print(f"  ошибка на {t[key]}: {type(e).__name__}: {e}",
                      flush=True)
        if (i + 1) % 500 == 0:
            el = (time.time() - stats["started"]) / 60
            print(f"  {i+1} из {len(items)} за {el:.1f} мин, "
                  f"повторов {stats['retries']}", flush=True)
    return rows


def main():
    import subprocess
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
            with open(p, "rb") as f:
                c = f.read().replace(b"\0", b" ").decode(errors="replace")
        except Exception:
            continue
        if "vllm" in c and "serve" in c:
            stats["cmdline"] = c.strip()
            break
    print("окружение:", stats["gpu"], "|", stats["server_version"], flush=True)

    texts = json.load(open(TEXTS, encoding="utf-8"))
    print(f"=== 6 000 текстов", flush=True)
    main_rows = run(texts, "n")

    print("=== 34 текста пробы", flush=True)
    probe = json.load(open(PROBE, encoding="utf-8"))
    probe_items = [{"text": t, "i": i} for i, t in enumerate(probe["texts"])]
    probe_rows = run(probe_items, "i")

    stats["minutes"] = (time.time() - stats["started"]) / 60
    with gzip.open(OUT, "wt", encoding="utf-8") as f:
        json.dump({"stats": stats, "texts": main_rows, "probe": probe_rows}, f)
    print(json.dumps(stats, ensure_ascii=False), flush=True)
    print("записано:", OUT, flush=True)


if __name__ == "__main__":
    main()
