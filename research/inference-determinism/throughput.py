#!/usr/bin/env python3
"""Сколько пропускной способности стоит запрет батчёвки.

Замеры детерминизма показали: при обработке строго по одному запросу за
раз ответ побитово постоянен, и сверка хэшей работает без порогов, без
доли расхождений на окне и без нижней границы длины ответа. Протокол
становится втрое проще.

Цена этой простоты — пропускная способность, и здесь она измеряется, а
не прикидывается. «Запретить батчёвку» означает, что исполнитель
обслуживает один запрос за раз; клиенты при этом продолжают присылать
много, то есть встают в очередь. Значит нужное число — отношение
пропускной способности на насыщении к пропускной способности при
одновременности 1.

Считаются порождённые токены, а не запросы: запросы могут отличаться
длиной ответа, и делить надо на работу, а не на их число.

    python3 throughput.py --base-url http://127.0.0.1:8000/v1 \\
        --model Qwen/Qwen2.5-7B-Instruct --out throughput.json
"""

import argparse
import json
import statistics
import sys
import threading
import time
import urllib.request

# Запросы РАЗНЫЕ и без общего начала: одинаковые дали бы движку
# переиспользовать кеш префиксов, и замер показал бы работу кеша, а не
# пропускную способность. Кеш вдобавок выключен флагом при запуске.
PROMPTS = [
    "Опиши подробно устройство асинхронного двигателя.",
    "Explain in detail how TCP congestion control works.",
    "Расскажи по шагам, как заквасить капусту.",
    "Describe the architecture of a modern CPU cache hierarchy.",
    "Объясни, почему небо голубое, с разбором рассеяния.",
    "Walk through how a mortgage amortisation schedule is computed.",
    "Расскажи об истории книгопечатания в Европе.",
    "Explain the difference between a B-tree and an LSM tree.",
    "Опиши процесс обжарки кофейных зёрен.",
    "Describe how GPS receivers determine position.",
    "Объясни принцип работы холодильника.",
    "Explain how vaccines train the immune system.",
    "Расскажи, как устроена канализация в городе.",
    "Describe the lifecycle of a Kubernetes pod.",
    "Объясни, что происходит при компиляции программы на C.",
    "Explain how noise-cancelling headphones work.",
    "Опиши, как работает гидроэлектростанция.",
    "Describe the stages of wine fermentation.",
    "Объясни устройство парового двигателя.",
    "Explain how a blockchain reaches consensus.",
    "Расскажи о строении глаза человека.",
    "Describe how a jet engine produces thrust.",
    "Объясни, как работает поисковый индекс.",
    "Explain what happens during a TLS handshake.",
    "Опиши процесс выплавки стали.",
    "Describe how solar panels convert light to electricity.",
    "Объясни, как устроен лифт.",
    "Explain how DNS resolution works end to end.",
    "Опиши, как делают бумагу из древесины.",
    "Describe how an MRI scanner forms an image.",
    "Объясни принцип работы микроволновой печи.",
    "Explain how a relational database plans a join.",
]


def post(url, payload, timeout=600):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def generate(base, model, prompt, max_tokens):
    """Возвращает (число порождённых токенов, секунды)."""
    t0 = time.perf_counter()
    r = post(f"{base}/completions", {
        "model": model, "prompt": prompt, "max_tokens": max_tokens,
        "temperature": 0, "top_p": 1})
    dt = time.perf_counter() - t0
    n = r.get("usage", {}).get("completion_tokens")
    if n is None:
        n = max_tokens  # запасной путь, если сервер не вернул учёт
    return n, dt


def run_level(base, model, concurrency, seconds, max_tokens):
    """Гонит нагрузку заданной плотности и считает, что успела сделать.

    Первые ответы отбрасываются вместе со всем прогоном только если он
    пустой: в остальном берётся всё, потому что установившийся режим
    наступает быстро — модель уже загружена, графы захвачены.
    """
    stop = threading.Event()
    lock = threading.Lock()
    tokens = [0]
    lat = []
    errors = [0]

    def worker(i):
        k = i
        while not stop.is_set():
            try:
                n, dt = generate(base, model,
                                 PROMPTS[k % len(PROMPTS)], max_tokens)
            except Exception:
                with lock:
                    errors[0] += 1
                continue
            k += len(PROMPTS)  # каждому потоку свой набор запросов
            with lock:
                tokens[0] += n
                lat.append(dt)

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(concurrency)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    time.sleep(seconds)
    stop.set()
    for t in threads:
        t.join(timeout=120)
    elapsed = time.perf_counter() - t0

    if not lat:
        return None
    return {
        "concurrency": concurrency,
        "elapsed": round(elapsed, 2),
        "requests": len(lat),
        "tokens": tokens[0],
        "errors": errors[0],
        "tokens_per_sec": tokens[0] / elapsed,
        "latency_mean": statistics.fmean(lat),
        "latency_p95": sorted(lat)[int(len(lat) * 0.95) - 1] if len(lat) > 1 else lat[0],
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--seconds", type=int, default=30,
                   help="сколько держать каждый уровень нагрузки")
    p.add_argument("--max-tokens", type=int, default=128,
                   help="длина ответа; выше замеренной границы 32 токена")
    p.add_argument("--levels", default="1,2,4,8,16,32")
    args = p.parse_args()

    base = args.base_url.rstrip("/")
    levels = [int(x) for x in args.levels.split(",")]
    out = {"model": args.model, "max_tokens": args.max_tokens,
           "seconds_per_level": args.seconds, "levels": []}

    print(f"{'одновр':>7} {'запросов':>9} {'токенов':>9} {'токен/с':>9} "
          f"{'задержка':>9} {'p95':>8} {'ошибок':>7}", file=sys.stderr)
    for c in levels:
        r = run_level(base, args.model, c, args.seconds, args.max_tokens)
        if r is None:
            print(f"{c:>7}   уровень не дал ни одного ответа", file=sys.stderr)
            continue
        out["levels"].append(r)
        print(f"{c:>7} {r['requests']:>9} {r['tokens']:>9} "
              f"{r['tokens_per_sec']:>9.1f} {r['latency_mean']:>9.2f} "
              f"{r['latency_p95']:>8.2f} {r['errors']:>7}", file=sys.stderr)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    if len(out["levels"]) >= 2:
        one = next((x for x in out["levels"] if x["concurrency"] == 1), None)
        best = max(out["levels"], key=lambda x: x["tokens_per_sec"])
        print("\n############ ИТОГ ############", file=sys.stderr)
        if one:
            print(f"один запрос за раз:  {one['tokens_per_sec']:.1f} токен/с",
                  file=sys.stderr)
            print(f"лучшее (одновр. {best['concurrency']}): "
                  f"{best['tokens_per_sec']:.1f} токен/с", file=sys.stderr)
            print(f"\nЗАПРЕТ БАТЧЁВКИ СТОИТ "
                  f"{best['tokens_per_sec']/one['tokens_per_sec']:.1f}x "
                  f"пропускной способности", file=sys.stderr)
            print(f"то есть себестоимость запроса растёт во столько же раз",
                  file=sys.stderr)
    print(f"\nзаписано: {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
