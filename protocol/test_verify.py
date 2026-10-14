#!/usr/bin/env python3
"""Проверка правила на НАСТОЯЩИХ векторах из замеров.

Смысл не в том, чтобы покрыть ветки кода, а в том, чтобы правило давало
на реальных данных те же числа, что опубликованы. Выдуманные векторы
этого не покажут: они проверят арифметику и пропустят ошибку в самой
постановке — ровно то, на чём сгорела фаза 2, где сравнивались хвосты
двух разных распределений.

Данные берутся из research/inference-determinism/results/:
  phase4/bf16.json       — эталон 4090, его тексты и оценки
  phase4/bf16-vary.json  — честные оценки при меняющемся составе пачки
  phase4/w8.json         — подлог FP8 по весам
  phase4/kv8.json        — подлог KV-кеша
  crosscard/*.json       — те же тексты на 3090 и A100

Запуск:  python protocol/test_verify.py
"""

import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify  # noqa: E402

R = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "..", "research", "inference-determinism", "results")

failures = []


def check(name, got, want, tol):
    ok = abs(got - want) <= tol
    print(f"  {'OK  ' if ok else 'ПЛОХО'} {name:<46} "
          f"{got:.5f} против {want:.5f}")
    if not ok:
        failures.append(f"{name}: {got:.5f} != {want:.5f}")


def load(*p):
    with open(os.path.join(R, *p), encoding="utf-8") as f:
        return json.load(f)


def lp(rec, i=0):
    s = rec.get("scores") or []
    if len(s) <= i:
        return None
    v = [x for x in s[i]["logprobs"] if x is not None]
    return v or None


def main():
    ref = load("phase4", "bf16.json")
    vary = load("phase4", "bf16-vary.json")
    w8 = load("phase4", "w8.json")
    kv8 = load("phase4", "kv8.json")
    g3090 = load("crosscard", "3090-vs-4090.json")
    a100 = load("crosscard", "a100-vs-4090.json")
    byp = {r["prompt"]: r for r in ref["prompts"]}

    def against_ref(d):
        out = []
        for r in d["prompts"]:
            o = byp.get(r["prompt"])
            a, b = (lp(o), lp(r)) if o else (None, None)
            if a and b:
                out.append(verify.divergence(a, b))
        return out

    def own(d):
        return [s["mean"] for s in d.get("summary", {}).get("solo_vs_batched", [])]

    print("### расхождение на настоящих данных воспроизводит замер")
    honest = own(vary) + own(g3090) + own(a100) + against_ref(g3090) + against_ref(a100)
    fraud = against_ref(w8)
    kvf = against_ref(kv8)
    check("честное среднее", statistics.fmean(honest), verify.HONEST_MEAN, 5e-5)
    check("честный максимум", max(honest), verify.HONEST_MAX, 5e-5)
    check("подлог FP8 минимум", min(fraud), verify.FRAUD_FP8_MIN, 5e-5)
    check("подлог FP8 среднее", statistics.fmean(fraud), verify.FRAUD_FP8_MEAN, 5e-5)
    print(f"  (честных наблюдений {len(honest)}, запросов подлога {len(fraud)})")

    print("\n### порог разделяет так, как заявлено")
    ph = sum(1 for x in honest if x >= verify.THRESHOLD) / len(honest)
    pf = sum(1 for x in fraud if x >= verify.THRESHOLD) / len(fraud)
    check("доля честных выше порога", ph, 0.002632, 1e-5)
    check("доля подлога выше порога", pf, 1.0, 1e-9)
    check("ложных обвинений при четырёх проверках", ph ** verify.CHECKS,
          4.8e-11, 1e-11)

    print("\n### KV-кеш ловится без порога вовсе")
    check("KV-кеш минимум против худшего честного",
          min(kvf) / max(honest), 7.1, 0.05)

    print("\n### вердикт: честный чист, подлог виновен")
    # честный: четыре наблюдения из честного множества
    hon4 = sorted(honest)[len(honest) // 2:][:verify.CHECKS]
    v = verify.Verdict(tuple(hon4), sum(1 for d in hon4 if d >= verify.THRESHOLD),
                       all(d >= verify.THRESHOLD for d in hon4))
    print(f"  честный:  {v}")
    if v.guilty:
        failures.append("честный признан виновным")
    fr4 = sorted(fraud)[:verify.CHECKS]
    v = verify.Verdict(tuple(fr4), sum(1 for d in fr4 if d >= verify.THRESHOLD),
                       all(d >= verify.THRESHOLD for d in fr4))
    print(f"  подлог:   {v}")
    if not v.guilty:
        failures.append("подлог не пойман на четырёх самых незаметных запросах")

    print("\n### короткие ответы и чужой рантайм отклоняются")
    for args, why in (
        (([0] * 10, verify.MEASURED_MODEL, verify.MEASURED_RUNTIME, verify.MEASURED_DTYPE),
         "ответ 10 токенов"),
        (([0] * 100, verify.MEASURED_MODEL, "vllm-0.32.0", verify.MEASURED_DTYPE),
         "другая версия рантайма"),
        (([0] * 100, "meta-llama/Llama-3-8B", verify.MEASURED_RUNTIME, verify.MEASURED_DTYPE),
         "другая модель"),
    ):
        try:
            verify.check_eligible(*args)
            print(f"  ПЛОХО {why}: пропущено, а должно быть отклонено")
            failures.append(f"не отклонено: {why}")
        except verify.Rejected as e:
            print(f"  OK   {why}: {str(e)[:60]}")

    print()
    if failures:
        print(f"ПРОВАЛ, {len(failures)} расхождений:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("всё сошлось")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
