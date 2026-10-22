#!/usr/bin/env python3
"""Свести перемер порога на 6 000 текстах.

Все наборы снимались с ВЫКЛЮЧЕННЫМ выбором ядра по замеру: иначе
пересборка ядер сама уводит вектор на 10 789 в среднем, и мерить нечего.
"""

import glob
import gzip
import json
import math
import os
import sys

SP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SP)
import canon  # noqa: E402

BIG = os.path.join(SP, "big")


def load(cfg):
    p = os.path.join(BIG, f"vectors-{cfg}.json.gz")
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return json.load(f)


def q(s, p):
    return s[min(len(s) - 1, int(round(p * (len(s) - 1))))]


def stat(X, Y, label, lens=None, show=True):
    keys = sorted(set(X) & set(Y))
    ds, ident = [], 0
    for k in keys:
        a, b = X[k], Y[k]
        if len(a) != len(b):
            continue
        ds.append(canon.divergence_micro(a, b))
        ident += a == b
    s = sorted(ds)
    if show:
        print(f"{label}")
        print(f"   текстов {len(ds)}  побитово {ident}  среднее "
              f"{sum(ds)/len(ds):>7.0f}  медиана {q(s,.5):>6}  "
              f"p99 {q(s,.99):>6}  худшее {max(ds):>7}  минимум {min(ds):>6}")
    return {k: v for k, v in zip(keys, ds)}, ds


have = sorted(os.path.basename(p)[len("vectors-"):-len(".json.gz")]
              for p in glob.glob(os.path.join(BIG, "vectors-*.json.gz")))
print("наборы:", ", ".join(have), "\n")
D = {c: load(c) for c in have}
for c in have:
    st = D[c]["stats"]
    print(f"  {c}: {st['gpu']}, vLLM {st['server_version']}, "
          f"{st['minutes']:.1f} мин, повторов {st['retries']}, "
          f"ошибок {st['errors']}")
print()

T = {c: D[c]["texts"] for c in have}
P = {c: D[c]["probe"] for c in have}
lens = {k: len(v) for k, v in T[have[0]].items()}

print("=== ЧЕСТНОЕ")
honest = {}
if "a3090" in T and "b3090" in T:
    honest["3090 vs 3090"] = stat(T["a3090"], T["b3090"],
                                  " 3090 против 3090 (разные машины)")[1]
if "a3090" in T and "honest4090" in T:
    cross_map, cross = stat(T["a3090"], T["honest4090"],
                            " 3090 против 4090 (разные архитектуры)")
    honest["3090 vs 4090"] = cross

print("\n=== ПОДЛОГ И НЕВЕРНЫЙ РЕЖИМ (против честной 4090)")
frauds = {}
for cfg, label in (("fp8", " FP8"), ("awq", " AWQ4"),
                   ("eager", " eager (неверный режим, не подлог)")):
    if cfg in T:
        m, ds = stat(T["honest4090"], T[cfg], label)
        frauds[cfg] = (m, ds)

print("\n=== ГДЕ МОЖЕТ СТОЯТЬ ПОРОГ")
wh = max(honest.get("3090 vs 4090", [0]))
real = [c for c in frauds if c != "eager"]
if real:
    mf = min(min(frauds[c][1]) for c in real)
    print(f"   худшее честное (между архитектурами): {wh}")
    print(f"   минимум подлога:                      {mf}")
    print(f"   зазор {mf - wh}, отношение {mf/wh:.2f}x")
    if mf > wh:
        print(f"   середина по логарифму: {round(math.sqrt(wh*mf))}")
    print(f"   порог 36 000 безопасен: {'да' if mf > 36000 else 'НЕТ'}")

    print("\n   если отсечь короткие векторы:")
    for cut in (0, 70, 80, 90, 100, 110, 120, 130):
        ks = [k for k in cross_map if lens.get(k, 0) >= cut]
        if len(ks) < 100:
            continue
        w = max(cross_map[k] for k in ks)
        f = min(min(frauds[c][0].get(k, 10**9) for k in ks) for c in real)
        print(f"     >= {cut:>3}: осталось {len(ks):>4}  честное <= {w:>6}  "
              f"подлог >= {f:>6}  отношение {f/w:.2f}x  "
              f"середина {round(math.sqrt(w*f))}")

print("\n=== ПРОБА (34 текста)")
if "a3090" in P and "honest4090" in P:
    _, pc = stat(P["a3090"], P["honest4090"], " 3090 против 4090 на пробе")
if "eager" in P:
    _, pe = stat(P["honest4090"], P["eager"], " eager против честной на пробе")
if "fp8" in P:
    _, pf = stat(P["honest4090"], P["fp8"], " FP8 против честной на пробе")
