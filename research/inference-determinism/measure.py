#!/usr/bin/env python3
"""Измерения фазы 1: насколько воспроизводим инференс.

Задача — не «проверить модель», а получить четыре величины, от которых
зависит форма протокола проверки результатов:

  1. разброс на ОДНОЙ карте от запуска к запуску;
  2. разброс от состава батча (соседи в очереди меняют арифметику);
  3. разброс при подлоге — другое квантование, модель поменьше, мусор;
  4. отношение стоимости проверки к стоимости работы.

Пятая величина — разброс между РАЗНЫМИ картами — этим скриптом не
берётся: нужно запустить его на каждой машине отдельно и сравнить
результаты через compare.py.

Ключевая мысль замера: сравниваются не тексты, а вероятности, которые
модель присваивает уже готовому ответу. Тексты расходятся лавинообразно
(один токен иначе — дальше другая ветка), а вероятности близки
численно даже на разном железе.

Работает с любым сервером, совместимым с OpenAI API, — vLLM, TGI,
llama.cpp server.

    python3 measure.py --base-url http://127.0.0.1:8000/v1 \\
        --model qwen38-uncensored --label a100-fp8 --out a100.json
"""

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

# Запросы подобраны так, чтобы ответы были содержательными, но
# короткими: длинные только удлиняют прогон, не добавляя точности.
PROMPTS = [
    "Объясни в двух предложениях, почему плавающая точка неассоциативна.",
    "Перечисли три состояния, в которых может находиться монета в сети Artery.",
    "What is the capital of France? Answer in one sentence.",
    "Write a single line of Python that reverses a list.",
]

# Чужие запросы для замера батчёвки. Нарочно не похожи на PROMPTS: общий
# префикс движок мог бы переиспользовать, и вместо соседства по пачке мы
# намерили бы работу кеша.
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


def post(url, payload, timeout=600):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def generate(base, model, prompt, max_tokens, seed=None):
    """Порождение ответа. Жадное декодирование, без случайности.

    temperature=0 и top_p=1 — обязательны: при сэмплировании ответ
    случаен по определению, и сравнивать нечего. seed передаётся на
    случай, если сервер всё же уйдёт в сэмплирование.
    """
    body = {
        "model": model,
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": 0,
        "top_p": 1,
        "logprobs": 1,
    }
    if seed is not None:
        body["seed"] = seed
    t0 = time.perf_counter()
    r = post(f"{base}/completions", body)
    dt = time.perf_counter() - t0
    ch = r["choices"][0]
    return {
        "text": ch["text"],
        "tokens": ch["logprobs"]["tokens"],
        "logprobs": ch["logprobs"]["token_logprobs"],
        "seconds": dt,
    }


def score(base, model, text, timeout=600):
    """Оценка ГОТОВОГО текста — один проход вместо последовательного.

    Здесь и прячется вся экономика проверки: порождение ста токенов это
    сто последовательных проходов через модель, а оценка готового ответа
    считается за один, параллельно на всю длину. Проверяющий тратит
    заметно меньше исполнителя — без этого никто бы не проверял.

    echo + max_tokens=0 просит сервер вернуть вероятности самого
    промпта, ничего не дописывая.
    """
    body = {
        "model": model,
        "prompt": text,
        "max_tokens": 0,
        "echo": True,
        "logprobs": 1,
        "temperature": 0,
    }
    t0 = time.perf_counter()
    r = post(f"{base}/completions", body, timeout=timeout)
    dt = time.perf_counter() - t0
    lp = r["choices"][0]["logprobs"]
    vals = [x for x in lp["token_logprobs"] if x is not None]
    return {"logprobs": vals, "tokens": lp["tokens"], "seconds": dt}


def score_under_load(base, model, text, neighbours, max_tokens, vary=False):
    """Та же оценка, но пока сервер занят чужими запросами.

    Это и есть настоящий вопрос к протоколу. Одиночный запрос на
    свободной карте воспроизводится тривиально; исполнитель же в сети
    обслуживает много сразу, и состав пачки меняет порядок суммирования
    в ядрах. Проверяющий этот состав повторить не может — у него своя
    очередь. Если от соседей вероятности едут, сверять их бессмысленно
    независимо от того, как хорошо карта повторяет сама себя.

    Шум держится потоками, целевой запрос уходит в середину нагрузки.

    vary=True меняет СОСТАВ пачки от вызова к вызову: число соседей, какие
    именно чужие запросы и когда они приходят. Без этого повторы не дают
    новых сведений — замерено: тридцать оценок при одинаковом составе
    вышли побитово одинаковыми, то есть сдвиг от пачки систематический, а
    не случайный. Вопрос протокола именно в разбросе ПО СОСТАВАМ: у
    проверяющего очередь своя, повторить чужую он не может.
    """
    stop = threading.Event()
    n = neighbours
    if vary and neighbours:
        n = random.randint(max(1, neighbours // 4), neighbours)
    order = list(range(len(NOISE_PROMPTS)))
    if vary:
        random.shuffle(order)

    def noise(i):
        k = order[i % len(order)]
        if vary:
            time.sleep(random.uniform(0, 0.4))  # разнобой в приходе
        while not stop.is_set():
            try:
                generate(base, model, NOISE_PROMPTS[k % len(NOISE_PROMPTS)],
                         max_tokens)
            except Exception:
                return
            if vary:
                k += 1

    threads = [threading.Thread(target=noise, args=(i,), daemon=True)
               for i in range(n)]
    for t in threads:
        t.start()
    time.sleep(1.0)  # дать очереди наполниться, иначе замер выйдет одиночным
    try:
        return score(base, model, text)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=15)


def spread(a, b):
    """Расхождение двух наборов вероятностей.

    Максимум важнее среднего: наказание срабатывает по худшему токену,
    а не по типичному. Средний разброс полезен для понимания картины,
    порог ставится по максимуму.
    """
    n = min(len(a), len(b))
    if n == 0:
        return None
    d = [abs(a[i] - b[i]) for i in range(n)]
    return {
        "n": n,
        "max": max(d),
        "mean": statistics.fmean(d),
        "median": statistics.median(d),
        "identical": all(x == 0.0 for x in d),
        "len_mismatch": len(a) != len(b),
    }


def environment(base):
    """Чем и на чём мерили. Пишется В РЕЗУЛЬТАТ, а не в память.

    Зачем это понадобилось. Порог 0,0360 действителен только для одной
    связки, и правило протокола требует перезамера при смене версии
    рантайма. Но ни один прежний файл замеров версию НЕ записывал — она
    существовала только в тексте отчёта. Значит `MEASURED_RUNTIME` в
    verify.py ничем в самих данных не подтверждался, и сверить замер с
    тем, что крутится у исполнителя, было нечем.

    Командная строка сервера берётся из /proc, а не со слов
    запускавшего: 14 октября замерено, что `--enforce-eager` при той же
    модели, версии и dtype сдвигает расхождение до 49 424 микродолей —
    внутрь полосы подлога. То есть режим исполнения определяет числа не
    меньше версии, и в результате он обязан быть виден.
    """
    env = {}
    try:
        url = f"{base.rstrip('/').removesuffix('/v1')}/version"
        with urllib.request.urlopen(url, timeout=15) as r:
            env["vllm_version"] = json.loads(r.read()).get("version")
    except Exception as e:
        env["vllm_version"] = None
        env["vllm_version_error"] = f"{type(e).__name__}: {e}"

    env["server_cmdline"] = None
    try:
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open(f"/proc/{d}/cmdline", "rb") as f:
                    parts = f.read().split(b"\0")
            except OSError:
                continue
            argv = [x.decode("utf-8", "replace") for x in parts if x]
            if any("vllm" in a for a in argv) and "serve" in argv:
                env["server_cmdline"] = argv
                break
    except OSError:
        pass

    try:
        env["gpu"] = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        env["gpu"] = None

    # Условия, без которых замер недействителен. Проверяются по
    # командной строке сервера, потому что из клиента их не видно.
    argv = env.get("server_cmdline") or []
    line = " ".join(argv)
    bad = []
    if argv:
        if "--no-enable-prefix-caching" not in argv:
            bad.append("префикс-кеш НЕ отключён: повторы перестают быть "
                       "побитовыми, и сравнивать вероятности бессмысленно")
        if "--enforce-eager" in argv:
            bad.append("--enforce-eager: узел теряет воспроизводимость "
                       "(замерено: 29 совпадений из 40), порог для него "
                       "недействителен")
        if "--dtype" in argv:
            i = argv.index("--dtype")
            if i + 1 < len(argv):
                env["dtype"] = argv[i + 1]
    else:
        bad.append("командную строку сервера прочитать не удалось: "
                   "режим исполнения в результате не зафиксирован")
    env["invalidating"] = bad
    return env


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", required=True, help="например http://127.0.0.1:8000/v1")
    p.add_argument("--model", required=True)
    p.add_argument("--label", required=True, help="как назвать эту машину/модель")
    p.add_argument("--out", required=True)
    p.add_argument("--repeats", type=int, default=5)
    p.add_argument("--max-tokens", type=int, default=120)
    p.add_argument("--batch-neighbours", type=int, default=8,
                   help="сколько чужих запросов подмешать для замера батчёвки")
    p.add_argument("--reference", default=None,
                   help="файл от прошлого запуска: оценивать ЕГО ответы, а не "
                        "свои. Нужен для замера подлога — проверяющий сверяет "
                        "чужой ответ, а не тот, что сам бы породил")
    p.add_argument("--loaded-repeats", type=int, default=2,
                   help="сколько раз оценить под нагрузкой; расхождение "
                        "возникает не в каждой пачке, нужна частота, а не факт")
    p.add_argument("--vary-load", action="store_true",
                   help="менять состав пачки между повторами: число соседей, "
                        "какие запросы и когда приходят. Без этого повторы "
                        "бесполезны — при одинаковом составе тридцать оценок "
                        "вышли побитово одинаковыми")
    p.add_argument("--prompts-file", default=None,
                   help="JSON-список запросов вместо встроенных четырёх. "
                        "Встроенный набор мал, и фаза 3б показала, что два "
                        "из четырёх ведут себя нетипично в разные стороны: "
                        "вывод фазы 2 на нём держаться не должен")
    args = p.parse_args()

    base = args.base_url.rstrip("/")
    prompts = PROMPTS
    if args.prompts_file:
        with open(args.prompts_file, encoding="utf-8") as f:
            prompts = json.load(f)
        print(f"запросов из файла: {len(prompts)}", file=sys.stderr)
    env = environment(args.base_url)
    out = {"label": args.label, "model": args.model, "env": env, "prompts": []}
    print(f"рантайм: vLLM {env.get('vllm_version')}, карта: {env.get('gpu')}",
          file=sys.stderr)
    for b in env["invalidating"]:
        print(f"ЗАМЕР НЕДЕЙСТВИТЕЛЕН: {b}", file=sys.stderr)

    # Тексты для оценки: свои или чужие. Во втором случае порождение
    # пропускается — оно измеряло бы работу подозреваемого, а нас
    # интересует его оценка чужого ответа.
    reference = None
    if args.reference:
        with open(args.reference, encoding="utf-8") as f:
            ref = json.load(f)
        reference = {r["prompt"]: r["runs"][0]["text"] for r in ref["prompts"]}
        print(f"эталон: {args.reference}, запросов {len(reference)}", file=sys.stderr)

    for prompt in prompts:
        print(f"\n=== {prompt[:60]}", file=sys.stderr)
        rec = {"prompt": prompt, "runs": [], "scores": []}

        # 1. Повторяемость на одной карте: один и тот же запрос N раз.
        if reference is None:
            for i in range(args.repeats):
                g = generate(base, args.model, prompt, args.max_tokens, seed=42)
                rec["runs"].append(g)
                print(f"  порождение {i+1}/{args.repeats}: "
                      f"{len(g['tokens'])} токенов за {g['seconds']:.2f} с",
                      file=sys.stderr)
            answer = rec["runs"][0]["text"]
        else:
            answer = reference.get(prompt)
            if answer is None:
                print("  нет в эталоне, пропускаю", file=sys.stderr)
                continue
            print(f"  оцениваю чужой ответ, {len(answer)} символов", file=sys.stderr)

        # 2. Оценка готового ответа — она же замер стоимости проверки.
        full = prompt + answer
        for i in range(2):
            s = score(base, args.model, full)
            rec["scores"].append(s)
            print(f"  оценка {i+1}/2: {len(s['logprobs'])} токенов "
                  f"за {s['seconds']:.2f} с", file=sys.stderr)

        # 3. Та же оценка под нагрузкой: меняет ли состав пачки ответ.
        if args.batch_neighbours > 0:
            rec["scores_loaded"] = []
            for i in range(args.loaded_repeats):
                s = score_under_load(base, args.model, full,
                                     args.batch_neighbours, args.max_tokens,
                                     vary=args.vary_load)
                rec["scores_loaded"].append(s)
                print(f"  оценка под нагрузкой {i+1}/{args.loaded_repeats}: "
                      f"{len(s['logprobs'])} токенов за {s['seconds']:.2f} с",
                      file=sys.stderr)

        out["prompts"].append(rec)

    # --- сводка по этой машине ---
    summary = {"run_to_run": [], "score_to_score": [], "cost_ratio": [],
               "solo_vs_batched": []}
    for rec in out["prompts"]:
        if rec["runs"]:
            base_lp = [x for x in rec["runs"][0]["logprobs"] if x is not None]
            for r in rec["runs"][1:]:
                lp = [x for x in r["logprobs"] if x is not None]
                s = spread(base_lp, lp)
                if s:
                    summary["run_to_run"].append(s)
        if len(rec["scores"]) >= 2:
            s = spread(rec["scores"][0]["logprobs"], rec["scores"][1]["logprobs"])
            if s:
                summary["score_to_score"].append(s)
        # Одиночная оценка против оценки в пачке — по той же строке.
        for s_loaded in rec.get("scores_loaded", []):
            s = spread(rec["scores"][0]["logprobs"], s_loaded["logprobs"])
            if s:
                summary["solo_vs_batched"].append(s)
        if rec["runs"] and rec["scores"]:
            gen_t = statistics.fmean(r["seconds"] for r in rec["runs"])
            sc_t = statistics.fmean(s["seconds"] for s in rec["scores"])
            if sc_t > 0:
                summary["cost_ratio"].append(gen_t / sc_t)

    out["summary"] = summary
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print("\n############ ИТОГ ############", file=sys.stderr)
    if summary["run_to_run"]:
        mx = max(s["max"] for s in summary["run_to_run"])
        ident = all(s["identical"] for s in summary["run_to_run"])
        print(f"повтор на одной карте: max |Δlogprob| = {mx:.3e}"
              f"{'  (побитово одинаково)' if ident else ''}", file=sys.stderr)
        if not ident and mx > 1e-3:
            print("  ВНИМАНИЕ: одна и та же карта не повторяет сама себя.", file=sys.stderr)
            print("  Сверка хэшей невозможна в принципе, даже внутри класса.", file=sys.stderr)
    if summary["solo_vs_batched"]:
        tot = len(summary["solo_vs_batched"])
        bad = [s for s in summary["solo_vs_batched"] if not s["identical"]]
        mx = max(s["max"] for s in summary["solo_vs_batched"])
        print(f"одиночно против пачки:  max |Δlogprob| = {mx:.3e}", file=sys.stderr)
        print(f"  разошлось {len(bad)} из {tot} прогонов"
              f" ({100*len(bad)/tot:.0f}%)", file=sys.stderr)
        if bad:
            print(f"  среди разошедшихся: max {max(s['max'] for s in bad):.3e},"
                  f" среднее {statistics.fmean(s['mean'] for s in bad):.3e}",
                  file=sys.stderr)
            print("  Состав пачки меняет ответ, но не в каждой пачке. Проверяющий", file=sys.stderr)
            print("  состав не повторит, поэтому порог обязан перекрывать максимум,", file=sys.stderr)
            print("  а наказывать следует за долю расхождений, а не за одно.", file=sys.stderr)
    if summary["cost_ratio"]:
        print(f"порождение дороже проверки в "
              f"{statistics.fmean(summary['cost_ratio']):.1f} раза", file=sys.stderr)
    print(f"\nзаписано: {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
