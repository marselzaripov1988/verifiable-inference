#!/usr/bin/env python3
"""Сравнение замеров и вывод: можно ли ставить порог.

На вход — файлы от measure.py с разных машин или с разных моделей.
Первый считается образцом (честный исполнитель), остальные
сравниваются с ним.

    python3 compare.py --honest a100.json --honest 4090.json \\
                       --fraud q4.json --fraud small-model.json

Вывод отвечает на единственный вопрос фазы 1: **есть ли чистый зазор**
между разбросом у честных исполнителей и разбросом при подлоге. Если
зазора нет, нежёсткое сравнение отпадает, и остаётся либо избыточное
исполнение, либо доверенная среда на серверных картах.
"""

import argparse
import json
import statistics
import sys


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def first_logprobs(rec):
    """Вероятности для сравнения, без пустых.

    Берутся ОЦЕНКИ готового текста, а не порождение. Это принципиально:
    разные модели порождают разные тексты, и сравнивать их вероятности
    потокенно бессмысленно — расхождение будет стопроцентным просто
    потому, что это разные слова. Проверяющий в протоколе тоже сверяет
    не свой ответ, а чужой.

    На порождение откат оставлен для старых файлов, снятых до того, как
    появился режим --reference.
    """
    if rec.get("scores"):
        return [x for x in rec["scores"][0]["logprobs"] if x is not None]
    return [x for x in rec["runs"][0]["logprobs"] if x is not None]


def spread(a, b):
    n = min(len(a), len(b))
    if n == 0:
        return None
    d = [abs(a[i] - b[i]) for i in range(n)]
    return {"n": n, "max": max(d), "mean": statistics.fmean(d),
            "len_mismatch": len(a) != len(b)}


def compare_files(ref, other):
    """Сравнение по запросам, одинаковым в обоих файлах."""
    by_prompt = {r["prompt"]: r for r in other["prompts"]}
    out = []
    for rec in ref["prompts"]:
        o = by_prompt.get(rec["prompt"])
        if not o:
            continue
        s = spread(first_logprobs(rec), first_logprobs(o))
        if s:
            out.append(s)
    return out


def describe(name, spreads):
    """Печатает обе меры и возвращает (max, худшее среднее).

    Худшее среднее — не среднее средних, а максимум по запросам: порог
    обязан покрывать самый неудобный запрос, а не типичный.
    """
    if not spreads:
        print(f"  {name}: нет сопоставимых запросов")
        return None
    mx = max(s["max"] for s in spreads)
    worst_mean = max(s["mean"] for s in spreads)
    mism = sum(1 for s in spreads if s["len_mismatch"])
    note = f", расхождений по длине: {mism}" if mism else ""
    print(f"  {name}: среднее до {worst_mean:.3e}, max {mx:.3e}{note}")
    return mx, worst_mean


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--honest", action="append", required=True,
                   help="замеры честных исполнителей; первый — образец")
    p.add_argument("--fraud", action="append", default=[],
                   help="замеры подлога: другое квантование, модель поменьше, мусор")
    args = p.parse_args()

    ref = load(args.honest[0])
    print(f"образец: {ref['label']} ({ref['model']})\n")

    print("ЧЕСТНЫЙ РАЗБРОС")
    honest_max = []
    honest_mean = []
    # Повторы на самой образцовой машине — нижняя граница шума.
    rr = ref.get("summary", {}).get("run_to_run", [])
    if rr:
        mx = max(s["max"] for s in rr)
        ident = all(s.get("identical") for s in rr)
        print(f"  повтор на той же карте: max {mx:.3e}"
              f"{'  (побитово одинаково)' if ident else ''}")
        honest_max.append(mx)
        honest_mean.append(max(s["mean"] for s in rr))

    # Разброс от состава пачки. Он и есть настоящая честная граница:
    # одиночный повтор на свободной карте даёт ноль, но исполнитель в
    # сети работает под нагрузкой, а проверяющий состав его очереди
    # повторить не может. Считать границу по одиночному повтору —
    # значит поставить порог, который честные будут пробивать.
    sb = ref.get("summary", {}).get("solo_vs_batched", [])
    if sb:
        mx = max(s["max"] for s in sb)
        bad = [s for s in sb if not s.get("identical")]
        print(f"  от состава пачки:       среднее до "
              f"{max(s['mean'] for s in sb):.3e}, max {mx:.3e}"
              f"  (разошлось {len(bad)} из {len(sb)},"
              f" {100*len(bad)/len(sb):.1f}%)")
        honest_max.append(mx)
        honest_mean.append(max(s["mean"] for s in sb))
    for path in args.honest[1:]:
        o = load(path)
        r = describe(f"против {o['label']}", compare_files(ref, o))
        if r is not None:
            honest_max.append(r[0])
            honest_mean.append(r[1])

    print("\nРАЗБРОС ПРИ ПОДЛОГЕ")
    fraud_min = []
    fraud_min_mean = []
    for path in args.fraud:
        o = load(path)
        sp = compare_files(ref, o)
        if sp:
            describe(f"против {o['label']}", sp)
            # Минимум по запросам: жулика надо поймать на самом удобном
            # для него запросе, а не на среднем.
            fraud_min.append(min(s["max"] for s in sp))
            fraud_min_mean.append(min(s["mean"] for s in sp))

    print("\nВЕРДИКТ")
    if not honest_max:
        print("  нечего сравнивать: нужен хотя бы один честный замер сверх образца")
        return 1
    if not fraud_min:
        print("  нет замеров подлога — верхняя граница зазора неизвестна")
        return 1

    # Считаем по СРЕДНЕМУ. Замеры дважды показали, что максимум даёт
    # неверный вердикт: он определяется одиночными редкими токенами, на
    # которых честная вариация скачет не меньше подлога. Пример — версия
    # рантайма: по максимуму 0,5384 против 0,3865 у подлога, то есть
    # «зазора нет», а по среднему 0,0277 против 0,0739, то есть запас
    # почти втрое. Максимум остаётся как диагностика.
    hi_mx = max(honest_max)
    lo_mx = min(fraud_min)
    print(f"  по максимуму:  честные до {hi_mx:.3e}, подлог от {lo_mx:.3e}"
          f"{'  — перекрываются' if lo_mx <= hi_mx else ''}")

    hi = max(honest_mean)
    lo = min(fraud_min_mean)
    print(f"  ПО СРЕДНЕМУ:   честные до {hi:.3e}, подлог от {lo:.3e}")

    if hi == 0.0:
        # Бывает, если честный замер снят только одиночными запросами.
        # Отношение тогда бесконечно, и это не хорошая новость, а признак
        # того, что честная граница просто не измерена.
        print("\n  честная граница вышла нулевой — значит замер снят без"
              " нагрузки.")
        print("  Прогоните measure.py с --batch-neighbours, иначе порог"
              " ставить не от чего:")
        print("  на свободной карте ноль получается всегда.")
        return 1

    if lo > hi * 10:
        thr = (hi * lo) ** 0.5  # середина по логарифму
        print(f"\n  ЗАЗОР ЧИСТЫЙ, отношение {lo/hi:.0f}x")
        print(f"  порог допуска: {thr:.3e}")
        print("  нежёсткое сравнение с выборочными перепроверками работает.")
        return 0
    if lo > hi:
        print(f"\n  ЗАЗОР УЗКИЙ, отношение всего {lo/hi:.1f}x")
        print("  порог поставить можно, но ложные срабатывания неизбежны.")
        print("  Наказывать надо за долю расхождений на окне, а не за единичное.")
        return 0
    print("\n  ЗАЗОРА НЕТ: честные расходятся сильнее, чем подлог.")
    print("  Нежёсткое сравнение по вероятностям не годится. Остаются")
    print("  избыточное исполнение либо доверенная среда на серверных картах.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
