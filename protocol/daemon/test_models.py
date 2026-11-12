"""Таблица моделей: кеш, эталон пробы и порог жадной проверки на пару
модель — класс.

    python3 test_models.py
"""
import hashlib
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import conformance  # noqa: E402
import greedy  # noqa: E402
import models  # noqa: E402

fails = []
Q25 = "Qwen/Qwen2.5-7B-Instruct"
Q3 = "huihui-ai/Huihui-Qwen3-8B-abliterated-v2"


def ok(cond, what):
    print(f"  {'OK  ' if cond else 'ПЛОХО'} {what}")
    if not cond:
        fails.append(what)


def test_lookup():
    print("### пары модель — класс")
    r = models.lookup(Q3, "sm_86")
    ok(r == {"revision": "7d89db76029281fd8f3e6698a8e30738608105a9",
             "cache": "q3-sm_86", "probe": "probe-q3-sm86.json"},
       f"Qwen3 на 3090 ({r})")
    ok(models.lookup(Q25, "sm_89")["cache"] == "sm_89"
       and models.lookup(Q25, "sm_89")["revision"] is None,
       "Qwen2.5 на 4090 — прежний кеш, без revision")
    for bad in ((Q3, "sm_120"), ("x/y", "sm_89")):
        try:
            models.lookup(*bad)
            ok(False, f"{bad} должна отвергаться")
        except models.Unsupported:
            ok(True, f"{bad}: отказ, а не сборка ядер с нуля")


def test_cli():
    print("\n### строка для start.sh")
    out = subprocess.run([sys.executable, os.path.join(HERE, "models.py"), Q3, "sm_89"],
                         capture_output=True, text=True)
    ok(out.returncode == 0 and out.stdout.split() ==
       ["q3-sm_89", "7d89db76029281fd8f3e6698a8e30738608105a9"],
       f"кеш и revision одной строкой ({out.stdout.strip()!r})")
    out = subprocess.run([sys.executable, os.path.join(HERE, "models.py"), Q25, "sm_86"],
                         capture_output=True, text=True)
    ok(out.stdout.split() == ["sm_86", "-"], "без revision — прочерк")
    out = subprocess.run([sys.executable, os.path.join(HERE, "models.py"), Q3, "sm_120"],
                         capture_output=True, text=True)
    ok(out.returncode == 1, "неизвестная пара — ненулевой код")


def test_everything_shipped():
    print("\n### у каждой пары есть кеш и эталон, и они целы")
    for model, m in models.MODELS.items():
        for arch, c in m["classes"].items():
            d = os.path.join(HERE, "kernel-cache", c["cache"])
            tar = os.path.join(d, "kernel-cache.tar.gz")
            want = open(os.path.join(d, "SHA256SUMS")).read().split()[0]
            got = hashlib.sha256(open(tar, "rb").read()).hexdigest()
            ok(got == want, f"{model.split('/')[-1]} {arch}: кеш цел")
            p = conformance.load_probe(conformance.probe_path_for(arch, model))
            ok(len(p["texts"]) == 34 and sum(p["lens"]) == 3948
               and p["limit_micro"] == 0,
               f"{model.split('/')[-1]} {arch}: эталон 34 текста, 3 948 чисел, допуск 0")
            ok(got in p["kernel_cache"], f"{model.split('/')[-1]} {arch}: эталон снят на этом кеше")


def test_probes_differ():
    print("\n### эталоны разных пар различны")
    p86 = conformance.load_probe(conformance.probe_path_for("sm_86", Q3))
    p89 = conformance.load_probe(conformance.probe_path_for("sm_89", Q3))
    v2 = conformance.load_probe(conformance.probe_path_for("sm_89"))
    ok(p86["texts"] == p89["texts"] == v2["texts"], "тексты те же, что в probe-v2")
    ok(p86["ref_hash"] != p89["ref_hash"], "Qwen3: 3090 и 4090 — разные векторы")
    ok(p89["ref_hash"] != v2["ref_hash"], "Qwen3 и Qwen2.5 на 4090 — разные векторы")
    ok(conformance.probe_path_for("sm_89", Q25) == conformance.probe_path_for("sm_89"),
       "Qwen2.5 по модели и по классу — один и тот же файл")


def test_greedy_rule():
    print("\n### порог жадной проверки — у каждой модели свой")
    ok(models.greedy_rule(Q25) == {"gap": 0.5, "min_tokens": 3}, "Qwen2.5: ≥ 3")
    ok(models.greedy_rule(Q3) == {"gap": 0.5, "min_tokens": 5}, "Qwen3: ≥ 5")
    ok(models.greedy_rule("x/y") == models.GREEDY_DEFAULT, "неизвестная — по умолчанию")
    # Честный ответ Qwen3 из замера: 4 токена больше 0,5 — пары разрывов
    # от иного разбиения на токены.
    honest_q3 = [0.0] * 394 + [6.19, 2.13, 1.5, 0.63]
    ok(not greedy.verdict(honest_q3, models.greedy_rule(Q3))["suspect"],
       "честный Qwen3 с 4 разрывами — не подделка по своему порогу")
    ok(greedy.verdict(honest_q3, models.greedy_rule(Q25))["suspect"],
       "тот же ответ по порогу Qwen2.5 был бы подделкой — поэтому порог свой")


def main():
    test_lookup()
    test_cli()
    test_everything_shipped()
    test_probes_differ()
    test_greedy_rule()
    if fails:
        print(f"\nПРОВАЛ: {len(fails)}")
        sys.exit(1)
    print("\nтаблица моделей сходится с тем, что лежит в образе")


if __name__ == "__main__":
    main()
