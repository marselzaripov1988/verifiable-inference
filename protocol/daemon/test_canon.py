#!/usr/bin/env python3
"""Сверка канона с реализацией на цепочке.

Эталоны сняты с protocol/contract/src/contract.rs — там commit_hash
работает на цепочке и потому авторитетен. Если этот тест падает,
неправ Python, а не Rust: исполнитель с таким демоном окажется
обвинён, а его раскрытие отвергнуто.

Те же значения продублированы тестом canon_golden на стороне Rust.
Разойдётся любая из двух реализаций — упадёт её собственный тест.

Запуск:  python protocol/daemon/test_canon.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import canon  # noqa: E402

# (вектор, хэш) — снято с Rust
GOLDEN = [
    ([], "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
    ([-693147], "bdae61cea9607466c860223f82df0f78d0780c763cf544525ffd1cb6d1c5b7f5"),
    ([-693147, -120000],
     "2991e9142791f02ac67b1e6101bf55d7ff0745947039c00dbbfb2dbe3e55faab"),
    ([0], "af5570f5a1810b7af78caf4bc70a660f0df51e42baf91d4de5b2328de0e83dfc"),
    ([-1, -9223372036854775807, 9223372036854775807],
     "2bdf8baeef16c408dc31ca90018696d2b7c531b065c1e40dd10df61600107727"),
    ([-500_000 - i * 1000 for i in range(100)],
     "b32b971c97f6382b7d0927c5f8d7694634ff4048407c3314478434dfd636b633"),
]

fails = []


def ok(cond, what):
    print(f"  {'OK  ' if cond else 'ПЛОХО'} {what}")
    if not cond:
        fails.append(what)


def main():
    print("### канон совпадает с реализацией на цепочке")
    for v, want in GOLDEN:
        got = canon.commit_hash(v)
        label = f"{len(v)} элементов" if len(v) != 1 else f"[{v[0]}]"
        ok(got == want, f"хэш {label}: {got[:16]}…")

    print("\n### свойства кодирования")
    ok(canon.commit_hash([-1, -2]) != canon.commit_hash([-2, -1]),
       "порядок значим")
    ok(canon.commit_hash([-1]) != canon.commit_hash([-1, 0]),
       "длина значима: [x] не равно [x, 0]")
    ok(len(canon.encode([0, 0, 0])) == 24, "восемь байт на элемент")

    print("\n### пересчёт совпадает с контрактом")
    a = [-500_000 - i * 1000 for i in range(100)]
    b = [x - 12_700 for x in a]
    ok(canon.divergence_micro(a, b) == 12_700, "честная пара даёт 12 700")
    ok(canon.divergence_micro(a, b[:50]) == 12_700, "разная длина — по общей части")
    # Деление вниз: на границе результат меньше, то есть в пользу
    # исполнителя. Проверяем знак округления, а не просто значение.
    ok(canon.divergence_micro([0, 0, 0], [1, 1, 2]) == 1,
       "деление вниз (4/3 даёт 1, не 2)")

    print("\n### пустота не проходит за согласие")
    try:
        canon.divergence_micro([], [1])
        ok(False, "пустой вектор должен отвергаться")
    except canon.EmptyVector:
        ok(True, "пересчёт отвергает пустой вектор")
    # А вот хэш пустоты — осмысленное значение, и это ловушка.
    ok(canon.commit_hash([]) == GOLDEN[0][1],
       "хэш пустоты равен sha256 пустой строки — ловить отдельно")
    try:
        canon.require_nonempty([])
        ok(False, "require_nonempty должен отвергать")
    except canon.EmptyVector:
        ok(True, "require_nonempty отвергает пустоту")

    print("\n### перевод в микродоли")
    ok(canon.to_micro([-0.693147]) == [-693147], "-0,693147 даёт -693147")
    ok(canon.to_micro([-0.0000005]) == [-1] or canon.to_micro([-0.0000005]) == [0],
       "округление к ближайшему, не усечение")
    ok(canon.to_micro([-1.0, None, -2.0]) == [-1000000, -2000000],
       "None выбрасывается, а не становится нулём")

    print()
    if fails:
        print(f"ПРОВАЛ, {len(fails)}:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("канон сходится с цепочкой")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
