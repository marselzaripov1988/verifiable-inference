#!/usr/bin/env python3
"""Как часто два честных исполнителя выдают РАЗНЫЙ ТЕКСТ.

Это решающее число для избыточного исполнения.

ОГОВОРКА, добавленная после фазы 4: писалось это в уверенности, что
избыточное исполнение — единственный оставшийся способ, потому что фаза
2 якобы показала, что сверка вероятностей не ловит FP8 по весам (подлог
0,0326 против честных 0,0385). Тот вывод неверен: сравнивались максимум
одного распределения с минимумом другого. По средним запас вчетверо,
сверка вероятностей работает и обходится дешевле. Замеры ниже верны, но
«единственного способа» больше нет.

Избыточное исполнение сравнивает не вероятности, а ТЕКСТЫ. При жадном
декодировании текст меняется только если возмущение перевернуло порядок
двух ближайших кандидатов. Отсюда два вопроса, и оба мерятся здесь:

  1. Как часто текст расходится у ЧЕСТНЫХ узлов под разной нагрузкой.
     Если часто — схема не работает вовсе: спор будет постоянным, и
     отличить жулика от переворота argmax не выйдет.

  2. Как часто текст расходится у ПОДЛОГА против честного. Если не чаще,
     чем в первом случае, избыточное исполнение наследует провал фазы 2:
     возмущение подлога меньше честного, значит и argmax он переворачивает
     не чаще.

Эталон снимается БЕЗ нагрузки: там, по замерам фазы 1, ответ побитово
постоянен, и это надёжная точка отсчёта.

    python3 textdiff.py --base-url http://127.0.0.1:8000/v1 \\
        --model Qwen/Qwen2.5-7B-Instruct --label bf16 \\
        --levels 0,8,32,128 --repeats 40 --out textdiff-bf16.json

    # подлог: сверяется с ЧУЖИМ эталоном
    python3 textdiff.py ... --label w8 --reference textdiff-bf16.json \\
        --out textdiff-w8.json
"""

import argparse
import json
import statistics
import sys
import threading
import time
import urllib.request

PROMPTS = [
    "Объясни в двух предложениях, почему плавающая точка неассоциативна.",
    "Перечисли три состояния, в которых может находиться монета в сети Artery.",
    "What is the capital of France? Answer in one sentence.",
    "Write a single line of Python that reverses a list.",
]

NOISE = [
    "Summarize the history of the printing press in three sentences.",
    "Напиши рецепт борща.",
    "List seven prime numbers greater than one hundred.",
    "Опиши устройство велосипедной втулки.",
    "Translate 'good morning' into five languages.",
    "Что такое кессонная болезнь?",
    "Explain the difference between TCP and UDP.",
    "Сколько будет 17 умножить на 23? Покажи вычисление.",
]


def post(url, payload, timeout=600):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def generate(base, model, prompt, max_tokens):
    """Жадное декодирование. Возвращает текст и список токенов."""
    r = post(f"{base}/completions", {
        "model": model, "prompt": prompt, "max_tokens": max_tokens,
        "temperature": 0, "top_p": 1, "logprobs": 1})
    ch = r["choices"][0]
    return ch["text"], ch["logprobs"]["tokens"]


class Load:
    """Чужие запросы, держащие карту занятой.

    Поднимается один раз на серию, а не на каждое порождение: пауза на
    заполнение очереди нужна только при старте.
    """

    def __init__(self, base, model, n, max_tokens):
        self.base, self.model, self.n = base, model, n
        self.max_tokens = max_tokens
        self.stop = threading.Event()
        self.threads = []

    def __enter__(self):
        if self.n == 0:
            return self

        def noise(i):
            k = i
            while not self.stop.is_set():
                try:
                    generate(self.base, self.model,
                             NOISE[k % len(NOISE)], self.max_tokens)
                except Exception:
                    return
                k += self.n
        self.threads = [threading.Thread(target=noise, args=(i,), daemon=True)
                        for i in range(self.n)]
        for t in self.threads:
            t.start()
        time.sleep(1.5)
        return self

    def __exit__(self, *exc):
        self.stop.set()
        for t in self.threads:
            t.join(timeout=20)
        return False


def first_diff(a, b):
    """Индекс первого различающегося токена, или None если совпали."""
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            return i
    return None if len(a) == len(b) else min(len(a), len(b))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--label", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--levels", default="0,8,32,128")
    p.add_argument("--repeats", type=int, default=40)
    p.add_argument("--max-tokens", type=int, default=120)
    p.add_argument("--prompts-file", default=None,
                   help="JSON-список запросов вместо встроенного набора")
    p.add_argument("--reference", default=None,
                   help="сверяться с эталоном из этого файла, а не со своим")
    args = p.parse_args()

    base = args.base_url.rstrip("/")
    levels = [int(x) for x in args.levels.split(",")]
    prompts = PROMPTS
    if args.prompts_file:
        with open(args.prompts_file, encoding="utf-8") as f:
            prompts = json.load(f)
        print(f"запросов из файла: {len(prompts)}", file=sys.stderr)
    out = {"label": args.label, "model": args.model,
           "levels": levels, "repeats": args.repeats, "items": []}

    ref = None
    if args.reference:
        with open(args.reference, encoding="utf-8") as f:
            r = json.load(f)
        # Эталон — порождение БЕЗ нагрузки у честной модели.
        ref = {it["prompt"]: it["ref_tokens"] for it in r["items"]}
        print(f"эталон: {args.reference}", file=sys.stderr)

    for prompt in prompts:
        print(f"\n=== {prompt[:56]}", file=sys.stderr)
        if ref is not None:
            ref_tokens = ref.get(prompt)
            if ref_tokens is None:
                print("  нет в эталоне, пропускаю", file=sys.stderr)
                continue
            # Своё порождение без нагрузки — для справки, не как эталон.
            _, own = generate(base, args.model, prompt, args.max_tokens)
            d = first_diff(ref_tokens, own)
            print(f"  своё без нагрузки против чужого эталона: "
                  f"{'совпало' if d is None else f'разошлось с токена {d}'}",
                  file=sys.stderr)
        else:
            _, ref_tokens = generate(base, args.model, prompt, args.max_tokens)
            print(f"  эталон снят без нагрузки: {len(ref_tokens)} токенов",
                  file=sys.stderr)

        rec = {"prompt": prompt, "ref_tokens": ref_tokens, "by_level": {}}
        for N in levels:
            diffs, same = [], 0
            with Load(base, args.model, N, args.max_tokens):
                for _ in range(args.repeats):
                    try:
                        _, tok = generate(base, args.model, prompt, args.max_tokens)
                    except Exception as e:
                        diffs.append({"error": repr(e)[:80]})
                        continue
                    d = first_diff(ref_tokens, tok)
                    if d is None:
                        same += 1
                    else:
                        diffs.append({"at": d, "len": len(tok)})
            bad = len(diffs)
            rec["by_level"][str(N)] = {
                "same": same, "diff": bad, "total": same + bad,
                "first_diff": [x.get("at") for x in diffs if "at" in x],
            }
            ats = [x["at"] for x in diffs if "at" in x]
            print(f"  соседей {N:>3}: совпало {same}/{same+bad}"
                  + (f", первое различие на токене "
                     f"{min(ats)}–{max(ats)} (медиана {int(statistics.median(ats))})"
                     if ats else ""),
                  file=sys.stderr)
        out["items"].append(rec)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    print("\n############ ИТОГ ############", file=sys.stderr)
    print(f"{'соседей':>8} {'совпало':>10} {'разошлось':>11} {'доля расхождений':>18}",
          file=sys.stderr)
    for N in levels:
        s = sum(it["by_level"][str(N)]["same"] for it in out["items"])
        t = sum(it["by_level"][str(N)]["total"] for it in out["items"])
        if t:
            print(f"{N:>8} {s:>10} {t-s:>11} {100*(t-s)/t:>17.1f}%",
                  file=sys.stderr)
    print(f"\nзаписано: {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
