#!/usr/bin/env python3
"""Где проходит граница длины ответа.

Замеры фазы 1 показали, что нежёсткое сравнение работает на ответах
в 131–143 токена и не работает на 19: на коротком ответе честный скачок
от состава пачки догоняет подлог. Отсюда правило «ввести нижнюю границу
длины», но сама граница не измерена — известно только, что она между
19 и 131.

Здесь она измеряется напрямую. Один и тот же ответ обрезается до
нескольких длин, и для каждой считается отношение подлога к честному
разбросу. Содержание при этом не меняется — меняется ровно длина, иначе
мы мерили бы разницу между запросами, а не влияние длины.

Порядок: сначала на честной модели (BF16), она порождает ответы и
замеряет свой разброс от пачки; потом на подложной (FP8) с
--reference — она оценивает ТЕ ЖЕ обрезки.

    # честная
    python3 measure_length.py --base-url http://127.0.0.1:8000/v1 \\
        --model Qwen/Qwen2.5-1.5B-Instruct --label bf16 --out honest.json

    # подлог: те же тексты
    python3 measure_length.py --base-url http://127.0.0.1:8000/v1 \\
        --model Qwen/Qwen2.5-1.5B-Instruct --label fp8 \\
        --reference honest.json --out fraud.json

    python3 compare_length.py --honest honest.json --fraud fraud.json
"""

import argparse
import json
import statistics
import sys
import threading
import time
import urllib.request

# Запросы выбраны так, чтобы ответ был заведомо длиннее самой большой
# проверяемой длины: обрезать можно только то, что порождено.
PROMPTS = [
    "Опиши подробно, как работает алгоритм быстрой сортировки.",
    "Explain in detail how a hash table handles collisions.",
    "Расскажи по шагам, что происходит при отправке транзакции в блокчейн.",
]

# Соседи по пачке. Не похожи на PROMPTS: общий префикс движок мог бы
# переиспользовать, и вместо соседства мы намерили бы работу кеша.
NOISE_PROMPTS = [
    "Summarize the history of the printing press in three sentences.",
    "Напиши рецепт борща.",
    "List seven prime numbers greater than one hundred.",
    "Опиши устройство велосипедной втулки.",
    "Translate 'good morning' into five languages.",
    "Что такое кессонная болезнь?",
    "Explain the difference between TCP and UDP.",
    "Сколько будет 17 умножить на 23? Покажи вычисление.",
]

LENGTHS = [8, 16, 32, 64, 128, 224]


def post(url, payload, timeout=600):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def generate(base, model, prompt, max_tokens):
    """Жадное порождение. Возвращает и текст, и список токенов.

    Токены нужны, чтобы резать ответ по токенам, а не по символам:
    граница измеряется в токенах, и резать по символам означало бы
    получать на каждой модели разную длину.
    """
    r = post(f"{base}/completions", {
        "model": model, "prompt": prompt, "max_tokens": max_tokens,
        "temperature": 0, "top_p": 1, "logprobs": 1})
    ch = r["choices"][0]
    return ch["text"], ch["logprobs"]["tokens"]


def score(base, model, text):
    """Вероятности готового текста: один проход, ничего не дописывая."""
    t0 = time.perf_counter()
    r = post(f"{base}/completions", {
        "model": model, "prompt": text, "max_tokens": 0,
        "echo": True, "logprobs": 1, "temperature": 0})
    dt = time.perf_counter() - t0
    lp = r["choices"][0]["logprobs"]
    return {"logprobs": [x for x in lp["token_logprobs"] if x is not None],
            "seconds": dt}


class Load:
    """Чужие запросы, держащие карту занятой.

    Шум поднимается один раз на всю серию оценок одной длины, а не на
    каждую: пауза на заполнение очереди нужна только при старте, и
    поднимать потоки на каждую оценку означало бы тратить секунду
    впустую триста раз.
    """

    def __init__(self, base, model, neighbours, max_tokens):
        self.base, self.model = base, model
        self.n, self.max_tokens = neighbours, max_tokens
        self.stop = threading.Event()
        self.threads = []

    def __enter__(self):
        def noise(i):
            while not self.stop.is_set():
                try:
                    generate(self.base, self.model,
                             NOISE_PROMPTS[i % len(NOISE_PROMPTS)],
                             self.max_tokens)
                except Exception:
                    return
        self.threads = [threading.Thread(target=noise, args=(i,), daemon=True)
                        for i in range(self.n)]
        for t in self.threads:
            t.start()
        time.sleep(1.0)  # дать очереди наполниться
        return self

    def __exit__(self, *exc):
        self.stop.set()
        for t in self.threads:
            t.join(timeout=15)
        return False


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--label", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--reference", default=None,
                   help="оценивать обрезки из этого файла, а не порождать свои")
    p.add_argument("--loaded-repeats", type=int, default=20,
                   help="сколько раз оценить под нагрузкой на каждой длине")
    p.add_argument("--batch-neighbours", type=int, default=8)
    args = p.parse_args()

    base = args.base_url.rstrip("/")
    out = {"label": args.label, "model": args.model, "items": []}

    if args.reference:
        with open(args.reference, encoding="utf-8") as f:
            ref = json.load(f)
        # Берём готовые обрезки: подлог обязан оценивать ровно тот текст,
        # что предъявил честный исполнитель.
        #
        # Запрос приклеивается обратно: в файле лежит только ответ, без
        # него. Оценить обрезок без его запроса — значит оценить другой
        # текст в другом контексте; в первом прогоне это завысило подлог
        # на порядок, и выдало себя тем, что число токенов не совпало с
        # честным замером.
        texts = [(it["prompt"], it["length"], it["prompt"] + it["text"])
                 for it in ref["items"]]
        print(f"эталон: {args.reference}, обрезков {len(texts)}", file=sys.stderr)
    else:
        texts = []
        for prompt in PROMPTS:
            print(f"\n=== порождаю: {prompt[:50]}", file=sys.stderr)
            _, tokens = generate(base, args.model, prompt, max(LENGTHS) + 20)
            print(f"  получено {len(tokens)} токенов", file=sys.stderr)
            for L in LENGTHS:
                if len(tokens) < L:
                    print(f"  {L}: не хватило токенов, пропускаю", file=sys.stderr)
                    continue
                texts.append((prompt, L, prompt + "".join(tokens[:L])))

    for prompt, L, full in texts:
        rec = {"prompt": prompt, "length": L, "text": full[len(prompt):],
               "loaded": []}
        rec["solo"] = score(base, args.model, full)
        n_tok = len(rec["solo"]["logprobs"])

        if args.loaded_repeats > 0:
            with Load(base, args.model, args.batch_neighbours, 120):
                for _ in range(args.loaded_repeats):
                    rec["loaded"].append(score(base, args.model, full))

        # Разброс от пачки на этой длине — по среднему: максимум дважды
        # давал ложную тревогу, он определяется одиночными редкими
        # токенами, на которых честная вариация скачет не меньше подлога.
        worst = 0.0
        diverged = 0
        for ld in rec["loaded"]:
            a, b = rec["solo"]["logprobs"], ld["logprobs"]
            k = min(len(a), len(b))
            if k == 0:
                continue
            d = [abs(a[i] - b[i]) for i in range(k)]
            m = statistics.fmean(d)
            worst = max(worst, m)
            if m > 0:
                diverged += 1
        rec["batch_worst_mean"] = worst
        rec["batch_diverged"] = diverged
        out["items"].append(rec)
        print(f"  длина {L:>3} ({n_tok:>3} токенов всего): "
              f"пачка до {worst:.5f}, разошлось {diverged}/{len(rec['loaded'])}",
              file=sys.stderr)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"\nзаписано: {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
