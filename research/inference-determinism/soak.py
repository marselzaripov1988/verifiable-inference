#!/usr/bin/env python3
"""Долгий прогон: как часто карта считает неправильно сама по себе.

Пятый пункт фазы 1 — частота ложных срабатываний из-за железа, а не из-за
арифметики. У потребительских карт нет контроля памяти (ECC), и один
перевёрнутый бит меняет ответ. Отдельно: неустойчивый разгон даёт
неверные результаты, и домашний узел с перекрученной памятью будет
честно считать неправильно.

Замер опирается на результат предыдущих прогонов: **при одном запросе за
раз ответ побитово постоянен**, проверено сорока повторами с нулевым
расхождением. Значит в этом режиме любое отклонение — сбой железа по
определению, а не порядок суммирования в пачке. Детектор чистый, и
никакого порога не нужно: сравнение точное.

Чего этот замер НЕ даст: саму частоту. Сбои редки, и за несколько часов
почти наверняка будет ноль событий. Тогда результат — верхняя граница, и
она полезна ровно для одного вывода: сравнить с частотой расхождений от
батчёвки (от 1,5% до 97% прогонов в зависимости от нагрузки). Если
железные сбои на порядки реже, для протокола они неважны.

Выдержку берём объёмом, а не только временем: чем больше весов лежит в
памяти и перечитывается каждым проходом, тем выше вероятность поймать
переворот. Поэтому модель побольше, а не та же 1,5B.

    python3 soak.py --base-url http://127.0.0.1:8000/v1 \\
        --model Qwen/Qwen2.5-7B-Instruct --hours 6 --out soak.json
"""

import argparse
import json
import statistics
import subprocess
import sys
import time
import urllib.request

# Текст подлиннее: больше токенов за проход — больше арифметики на
# каждое обращение, и заодно длина, на которой предыдущие замеры дали
# устойчивый ноль.
TEXT = (
    "Опиши подробно, как работает алгоритм быстрой сортировки, "
    "с разбором выбора опорного элемента, поведения на отсортированных "
    "данных и оценкой сложности в среднем и худшем случае. "
    "Quicksort works by selecting a pivot element and partitioning the "
    "array around it, so that elements smaller than the pivot end up on "
    "one side and larger ones on the other. The procedure then recurses "
    "into both partitions. Average complexity is n log n, while the "
    "worst case degrades to n squared when the pivot is consistently "
    "the smallest or largest element, which is exactly what happens on "
    "already sorted input with a naive first-element pivot."
)


def post(url, payload, timeout=300):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def score(base, model, text):
    t0 = time.perf_counter()
    r = post(f"{base}/completions", {
        "model": model, "prompt": text, "max_tokens": 0,
        "echo": True, "logprobs": 1, "temperature": 0})
    dt = time.perf_counter() - t0
    lp = r["choices"][0]["logprobs"]
    return [x for x in lp["token_logprobs"] if x is not None], dt


def gpu_state():
    """Температура, частоты, питание и счётчики ошибок.

    Счётчики у потребительских карт почти наверняка вернут N/A — это и
    есть часть ответа: сбой на них не виден никак, ни софту, ни драйверу.
    Xid-ошибки драйвера тоже не поднимутся до контейнера, поэтому
    единственный доступный признак — расхождение самого результата.
    """
    q = ("temperature.gpu,clocks.current.graphics,clocks.current.memory,"
         "power.draw,utilization.gpu,ecc.errors.uncorrected.volatile.total,"
         "retired_pages.double_bit.count")
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=20).stdout.strip()
        return out
    except Exception as e:
        return f"недоступно: {e}"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--hours", type=float, default=6.0)
    p.add_argument("--out", required=True)
    p.add_argument("--state-every", type=int, default=200,
                   help="через сколько проходов записывать состояние карты")
    args = p.parse_args()

    base = args.base_url.rstrip("/")
    deadline = time.time() + args.hours * 3600

    print(f"состояние карты на старте:\n  {gpu_state()}", file=sys.stderr)

    ref, dt0 = score(base, args.model, TEXT)
    print(f"эталон снят: {len(ref)} токенов за {dt0:.3f} с", file=sys.stderr)
    if not ref:
        print("пустой ответ — проверьте модель и текст", file=sys.stderr)
        return 1

    out = {
        "model": args.model, "tokens": len(ref), "hours_requested": args.hours,
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "reference": ref, "deviations": [], "states": [],
    }

    n = 0
    times = []
    t_start = time.time()
    try:
        while time.time() < deadline:
            lp, dt = score(base, args.model, TEXT)
            n += 1
            times.append(dt)

            if lp != ref:
                k = min(len(lp), len(ref))
                d = [abs(lp[i] - ref[i]) for i in range(k)]
                ev = {
                    "iteration": n,
                    "seconds_in": round(time.time() - t_start, 1),
                    "len_ref": len(ref), "len_got": len(lp),
                    "max": max(d) if d else None,
                    "mean": statistics.fmean(d) if d else None,
                    "changed_tokens": sum(1 for x in d if x != 0.0),
                    "state": gpu_state(),
                }
                out["deviations"].append(ev)
                print(f"\n!!! ОТКЛОНЕНИЕ на проходе {n}: "
                      f"max {ev['max']}, токенов изменилось "
                      f"{ev['changed_tokens']}/{k}\n  {ev['state']}",
                      file=sys.stderr)

            if n % args.state_every == 0:
                st = gpu_state()
                out["states"].append({"iteration": n, "state": st})
                el = time.time() - t_start
                print(f"  проход {n}, прошло {el/60:.1f} мин, "
                      f"отклонений {len(out['deviations'])}, "
                      f"{statistics.fmean(times[-args.state_every:]):.3f} с/проход"
                      f"\n    {st}", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nпрервано вручную", file=sys.stderr)

    elapsed = time.time() - t_start
    out["iterations"] = n
    out["elapsed_seconds"] = round(elapsed, 1)
    out["token_passes"] = n * len(ref)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    print("\n############ ИТОГ ############", file=sys.stderr)
    print(f"проходов:            {n}", file=sys.stderr)
    print(f"токенов просчитано:  {n * len(ref)}", file=sys.stderr)
    print(f"время:               {elapsed/3600:.2f} ч", file=sys.stderr)
    print(f"ОТКЛОНЕНИЙ:          {len(out['deviations'])}", file=sys.stderr)
    if not out["deviations"] and n > 0:
        # Верхняя граница по правилу трёх: при нуле событий за n попыток
        # частота с 95% уверенностью ниже 3/n.
        print(f"частота ниже {3.0/n:.2e} на проход (правило трёх, 95%)",
              file=sys.stderr)
        print(f"то есть ниже {3.0/(elapsed/3600):.1f} событий в час",
              file=sys.stderr)
    print(f"\nзаписано: {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
