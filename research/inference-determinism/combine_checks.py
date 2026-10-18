#!/usr/bin/env python3
"""Пересчитать итог фазы проверяющих из записанных расхождений.

Считает по `results/checkers/divergences.tsv` — построчной записи того,
насколько каждый проверяющий разошёлся с исполнителем на каждом задании.
Все задания в ней честные, поэтому любое обвинение — ложное.

    python3 combine_checks.py [путь/к/divergences.tsv] [порог]
"""

import math
import os
import sys

DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "results", "checkers", "divergences.tsv")


def read(path):
    names, rows = [], []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("#"):
                parts = line.lstrip("# ").split("\t")
                if len(parts) > 2 and parts[0].startswith("задание"):
                    names = parts[2:]
                continue
            if not line:
                continue
            p = line.split("\t")
            rows.append((int(p[0]), int(p[1]), [int(x) for x in p[2:]]))
    return names, rows


def q(xs, p):
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(p * (len(s) - 1))))]


def pearson(a, b):
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    sa = math.sqrt(sum((x - ma) ** 2 for x in a))
    sb = math.sqrt(sum((x - mb) ** 2 for x in b))
    if sa == 0 or sb == 0:
        return float("nan")
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    thr = int(sys.argv[2]) if len(sys.argv) > 2 else 36000
    names, rows = read(path)
    if not rows:
        print("пусто:", path)
        return 1
    k = len(rows[0][2])
    names = names or [f"проверяющий {i+1}" for i in range(k)]
    cols = [[r[2][i] for r in rows] for i in range(k)]
    print(f"заданий {len(rows)}, проверяющих {k}, порог {thr}\n")

    for i, nm in enumerate(names):
        xs = cols[i]
        print(f"  {nm}: среднее {sum(xs)/len(xs):>8.0f}  медиана "
              f"{q(xs,.5):>7}  p99 {q(xs,.99):>7}  худшее {max(xs):>8}  "
              f"выше порога {sum(1 for x in xs if x >= thr)}")

    guilty = [r for r in rows if all(x >= thr for x in r[2])]
    print(f"\nвиновен, если превысили ВСЕ: ложных обвинений "
          f"{len(guilty)} из {len(rows)}")
    hist = {}
    for r in rows:
        c = sum(1 for x in r[2] if x >= thr)
        hist[c] = hist.get(c, 0) + 1
    for c in sorted(hist):
        print(f"  превысили {c} из {k}: {hist[c]} заданий")

    mins = [min(r[2]) for r in rows]
    print(f"\nхудшее у мягчайшего проверяющего: {max(mins)} — "
          f"запас порога {thr/max(mins):.2f}x")
    print(f"худшее вообще: {max(max(r[2]) for r in rows)}")

    print("\nсвязь между проверяющими:")
    for i in range(k):
        for j in range(i + 1, k):
            print(f"  r({names[i]}, {names[j]}) = "
                  f"{pearson(cols[i], cols[j]):.3f}")

    print("\nдлина ответа и расхождение:")
    for lo, hi in ((32, 48), (48, 64), (64, 96), (96, 127), (127, 10 ** 9)):
        sel = [r for r in rows if lo <= r[1] < hi]
        if not sel:
            continue
        worst = [max(r[2]) for r in sel]
        least = [min(r[2]) for r in sel]
        print(f"  токенов {lo:>3}..{hi if hi < 10**9 else 999:>3}: "
              f"заданий {len(sel):>6}  худший в среднем "
              f"{sum(worst)/len(worst):>8.0f}  мягчайший "
              f"{sum(least)/len(least):>8.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
