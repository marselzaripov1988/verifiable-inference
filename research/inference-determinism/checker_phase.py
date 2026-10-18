#!/usr/bin/env python3
"""Фаза проверяющих: оценить записанные за сутки задания.

Что здесь мерится и почему это последнее непроверенное место протокола.

Вердикт выносится так: виновен, если ВСЕ четыре проверки превысили порог
36 000. Честное расхождение между картами замерено (13 297 в среднем,
20 137 худшее среди пригодных заданий), но замерено на СПОКОЙНЫХ узлах.
А в работе исполнитель занят, проверяющий — почти свободен, и та же
модель на разной загрузке расходится сама с собой до 0,04466 в среднем
по паре. Это ВЫШЕ порога. Спасает только конъюнктивное правило, а
честное расхождение коррелировано по запросам (r = 0,76).

Поэтому: исполнитель отработал сутки под нагрузкой 16 одновременных
запросов, его задания записаны вместе с векторами. Проверяющий здесь
оценивает их ПОСЛЕДОВАТЕЛЬНО, то есть почти без нагрузки — ровно та
асимметрия, которая опасна.

Выход — расхождение по каждому заданию. Сводит их вызывающий: ложное
обвинение, это когда все четыре проверяющих превысили порог на одном
задании.

Проверяющий делает то же, что демон: сверяет вектор исполнителя с
обязательством и отказывается, если он не тот.
"""

import json
import os
import sys
import time

sys.path.insert(0, "/root/protocol/daemon")
import canon  # noqa: E402
import vllm_client as vllm  # noqa: E402

BASE = "http://127.0.0.1:8000/v1"
MODEL = "Qwen/Qwen2.5-7B-Instruct"
JOBS = "/root/jobs.jsonl"
NAME = os.environ.get("CHECKER_NAME", "checker")
OUT = f"/root/checks-{NAME}.jsonl"
LIMIT = int(os.environ.get("LIMIT", "0"))      # 0 = все
MIN_TOKENS = 32                                 # контракт отклоняет короче

stats = {"done": 0, "skipped_short": 0, "commit_mismatch": 0,
         "score_retries": 0, "score_failed": 0, "errors": 0,
         "started": time.time()}


def score_counted(text):
    """Оценка с подсчётом повторов.

    На простаивающей карте отказ с NaN съедал 34 текста из 40, а под
    нагрузкой не случился ни разу на 21 585 вызовах. Проверяющий работает
    почти без нагрузки, то есть в тех самых условиях — считаем.
    """
    import urllib.error
    for _ in range(vllm.SCORE_ATTEMPTS):
        try:
            r = vllm._post(BASE, "/completions", {
                "model": MODEL, "prompt": text, "max_tokens": 0,
                "echo": True, "logprobs": 1,
                "temperature": vllm.TEMPERATURE, "top_p": vllm.TOP_P,
            })
            return r["choices"][0]["logprobs"]["token_logprobs"]
        except urllib.error.HTTPError as e:
            body = b""
            try:
                body = e.read()
            except Exception:
                pass
            if e.code == 400 and b"nan" in body.lower():
                stats["score_retries"] += 1
                continue
            raise
    stats["score_failed"] += 1
    raise vllm.ScoringFailed("не отдал за все попытки")


def main():
    out = open(OUT, "a", encoding="utf-8")
    with open(JOBS, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if LIMIT and stats["done"] >= LIMIT:
                break
            job = json.loads(line)
            if job["answer_tokens"] < MIN_TOKENS:
                stats["skipped_short"] += 1
                continue
            producer = job["vector"]
            # То же, что делает демон: без сверки с обязательством
            # расхождение считалось бы по чужим данным.
            if canon.commit_hash(producer) != job["commit"]:
                stats["commit_mismatch"] += 1
                continue
            try:
                mine = canon.to_micro(
                    score_counted(job["prompt"] + job["answer"]))
                if not mine:
                    stats["errors"] += 1
                    continue
                d = canon.divergence_micro(producer, mine)
                out.write(json.dumps({
                    "n": job["n"], "divergence_micro": d,
                    "len_producer": len(producer), "len_mine": len(mine),
                    "answer_tokens": job["answer_tokens"],
                    "commit": canon.commit_hash(mine),
                }) + "\n")
                stats["done"] += 1
                if stats["done"] % 200 == 0:
                    out.flush()
                    el = time.time() - stats["started"]
                    print(f"  {stats['done']} за {el/60:.1f} мин, "
                          f"повторов {stats['score_retries']}, "
                          f"отказов {stats['score_failed']}", flush=True)
            except Exception as e:
                stats["errors"] += 1
                if stats["errors"] <= 5:
                    print(f"  ошибка на {job['n']}: {type(e).__name__}: {e}",
                          flush=True)
    out.close()
    stats["minutes"] = (time.time() - stats["started"]) / 60
    with open(f"/root/checks-{NAME}-stats.json", "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    print(json.dumps(stats, ensure_ascii=False, indent=1), flush=True)


if __name__ == "__main__":
    main()
