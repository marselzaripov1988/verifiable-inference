#!/usr/bin/env python3
"""Отношение подлога к честному разбросу в зависимости от длины ответа.

Отвечает на один вопрос: с какой длины ответа нежёсткое сравнение
начинает работать. Ниже этой длины проверять вероятностями нельзя —
останутся избыточное исполнение либо запрет батчёвки.

Обе величины считаются ПО СРЕДНЕМУ расхождению, а не по максимуму:
максимум в замерах фазы 1 дважды дал ложную тревогу, потому что
определяется одиночными редкими токенами, на которых честная вариация
скачет не меньше подлога.
"""

import argparse
import json
import statistics
import sys


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def key(it):
    return (it["prompt"], it["length"])


def mean_spread(a, b):
    n = min(len(a), len(b))
    if n == 0:
        return None
    return statistics.fmean(abs(a[i] - b[i]) for i in range(n))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--honest", required=True)
    p.add_argument("--fraud", action="append", required=True)
    args = p.parse_args()

    h = load(args.honest)
    by_len = {}
    for it in h["items"]:
        by_len.setdefault(it["length"], []).append(it)

    frauds = [load(p) for p in args.fraud]
    fmaps = [{key(it): it for it in f["items"]} for f in frauds]

    print(f"честный: {h['label']} ({h['model']})")
    for f in frauds:
        print(f"подлог:  {f['label']} ({f['model']})")
    print()
    head = f"{'длина':>6} {'честно':>9} {'разошлось':>10}"
    for f in frauds:
        head += f" {f['label'][:10]:>11}"
    head += f" {'запас':>7}"
    print(head)

    rows = []
    for L in sorted(by_len):
        items = by_len[L]
        # Честная граница на этой длине — худший запрос, не типичный:
        # порог обязан покрывать самый неудобный случай.
        honest = max(it["batch_worst_mean"] for it in items)
        div = sum(it["batch_diverged"] for it in items)
        tot = sum(len(it["loaded"]) for it in items)

        fr = []
        for fm in fmaps:
            vals = []
            for it in items:
                o = fm.get(key(it))
                if o:
                    s = mean_spread(it["solo"]["logprobs"], o["solo"]["logprobs"])
                    if s is not None:
                        vals.append(s)
            # Минимум по запросам: жулика надо ловить на самом удобном
            # для него запросе.
            fr.append(min(vals) if vals else None)

        line = f"{L:>6} {honest:>9.5f} {div:>4}/{tot:<5}"
        for v in fr:
            line += f" {v:>11.5f}" if v is not None else f" {'—':>11}"
        ok = [v for v in fr if v is not None]
        ratio = (min(ok) / honest) if (ok and honest > 0) else None
        line += f" {ratio:>6.1f}x" if ratio else f" {'∞':>7}"
        print(line)
        rows.append((L, honest, ratio))

    print()
    ok = [(L, r) for L, _, r in rows if r is not None]
    if not ok:
        print("честный разброс вышел нулевым на всех длинах — замер снят"
              " без нагрузки либо соседей слишком мало")
        return 1

    # Не пороговый приговор по произвольной константе, а две величины,
    # которые действительно нужны протоколу: где запас перестаёт быть
    # никаким и где он перестаёт расти. Вторая важнее: если запас выходит
    # на полку, требовать ответы ещё длиннее бессмысленно.
    best = max(r for _, r in ok)
    plateau = min(L for L, r in ok if r >= best * 0.9)
    worst_L, worst_r = min(ok, key=lambda x: x[1])

    print(f"хуже всего на длине {worst_L}: запас {worst_r:.1f}x")
    print(f"потолок запаса {best:.1f}x, выходит на полку с {plateau} токенов")
    print()
    print("То есть нижняя граница длины осмысленна, но сверху запас ограничен:")
    print(f"ответы длиннее {plateau} токенов не добавляют различимости, а")
    print(f"потолок {best:.1f}x означает, что наказывать можно только за долю")
    print("расхождений на окне — одиночное расхождение при таком запасе")
    print("ничего не доказывает.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
