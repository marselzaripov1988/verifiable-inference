#!/usr/bin/env python3
"""Повторяет ли ОДНА карта сама себя — в одном процессе и после перезапуска.

Зачем. В фазе проверяющих две RTX 4090 на разных машинах, с разными
драйверами и разницей в пять часов выдали побитово одинаковые векторы на
всех 18 320 текстах. Две RTX 3090 — ни на одном. Прежде чем искать
разницу между машинами, надо исключить более простое объяснение: что
3090 не воспроизводит сама себя.

Опыт устроен так, что различает три случая:

  * V1 против V2 (один процесс, подряд) — ловит недетерминизм внутри
    запуска: атомарные свёртки, split-K, выбор ядра на ходу;
  * V1 против V3 (после перезапуска сервера) — ловит то, что зависит от
    состояния при старте: размер KV-кеша считается от СВОБОДНОЙ памяти,
    а она между запусками разная;
  * если V1 = V2 = V3, карта детерминирована, и разницу между двумя
    3090 надо искать в настройках машины, а не в карте.

Писать в вывод обязательно окружение: ни один файл замеров фазы 1 не
записывал версию vLLM, и это уже один раз делало замеры несравнимыми.

    PHASE=1 python3 -u selfrepro.py     # V1 и V2 до перезапуска
    PHASE=2 python3 -u selfrepro.py     # V3 после перезапуска
"""

import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/root/protocol/daemon")
import canon  # noqa: E402
import vllm_client as vllm  # noqa: E402

BASE = "http://127.0.0.1:8000/v1"
MODEL = "Qwen/Qwen2.5-7B-Instruct"
TEXTS = "/root/texts.json"
PHASE = os.environ.get("PHASE", "1")
OUT = f"/root/selfrepro-{PHASE}.json"


def score(text):
    """Оценка с повторами: отказ с NaN это помеха доступности, не
    правильности — полученный вектор тот же самый."""
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


def run(texts):
    """Один проход по набору. Возвращает хэш вектора на каждый текст:
    сравнивать потом надо побитово, а не по среднему."""
    rows = []
    for i, t in enumerate(texts):
        v = canon.to_micro(score(t["text"]))
        rows.append({"n": t["n"], "len": len(v),
                     "hash": canon.commit_hash(v),
                     "raw": hashlib.sha256(
                         json.dumps(v).encode()).hexdigest()[:16]})
        if (i + 1) % 50 == 0:
            print(f"  {i+1} из {len(texts)}", flush=True)
    return rows


def environment():
    """Всё, что делает замер сравнимым или несравнимым."""
    import subprocess
    env = {}
    try:
        env["server_version"] = vllm.server_version(BASE)
    except Exception as e:
        env["server_version_error"] = f"{type(e).__name__}: {e}"
    for key, cmd in (
        ("gpu", ["nvidia-smi", "--query-gpu=name,driver_version,memory.total,"
                 "memory.used,clocks.max.sm,ecc.mode.current",
                 "--format=csv,noheader"]),
        ("vllm_version", ["python3", "-c", "import vllm;print(vllm.__version__)"]),
        ("flash_attn", ["python3", "-c",
                        "import flash_attn;print(flash_attn.__version__)"]),
        ("torch", ["python3", "-c", "import torch;print(torch.__version__)"]),
    ):
        try:
            env[key] = subprocess.run(cmd, capture_output=True, text=True,
                                      timeout=60).stdout.strip()
        except Exception as e:
            env[key] = f"ошибка: {e}"
    # Командная строка сервера: параметры должны быть теми самыми.
    try:
        import glob
        for p in glob.glob("/proc/[0-9]*/cmdline"):
            with open(p, "rb") as f:
                c = f.read().replace(b"\0", b" ").decode(errors="replace")
            if "vllm" in c and "serve" in c:
                env["server_cmdline"] = c.strip()
                break
    except Exception as e:
        env["server_cmdline"] = f"ошибка: {e}"
    return env


def main():
    texts = json.load(open(TEXTS, encoding="utf-8"))
    env = environment()
    print("окружение:", json.dumps(env, ensure_ascii=False)[:400], flush=True)

    res = {"phase": PHASE, "at": time.time(), "env": env,
           "texts": len(texts)}

    print(f"=== проход A ({len(texts)} текстов)", flush=True)
    a = run(texts)
    res["A"] = a

    if PHASE == "1":
        print("=== проход B, тот же процесс сервера", flush=True)
        b = run(texts)
        res["B"] = b
        same = sum(1 for x, y in zip(a, b) if x["hash"] == y["hash"])
        res["A_vs_B_identical"] = same
        print(f"\nодин процесс: побитово совпало {same} из {len(a)}",
              flush=True)

    json.dump(res, open(OUT, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("записано:", OUT, flush=True)


if __name__ == "__main__":
    main()
