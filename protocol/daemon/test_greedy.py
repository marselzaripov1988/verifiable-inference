"""Жадная проверка: подсчёт разрывов, вердикт, журнал, безвредность сбоя.

    python3 test_greedy.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import greedy  # noqa: E402
import loop  # noqa: E402
import vllm_client as vllm  # noqa: E402

fails = []


def ok(cond, what):
    print(f"  {'OK  ' if cond else 'ПЛОХО'} {what}")
    if not cond:
        fails.append(what)


def fake_echo(prompt_len, rows):
    """echo: промпт из prompt_len токенов, потом ответ — пары (выбранный, лучший)."""
    def echo(base, model, text, k):
        n = prompt_len + (len(rows) if text != "P" else 0)
        lp = [None] + [-0.1] * (prompt_len - 1)
        top = [None] + [{"a": -0.1}] * (prompt_len - 1)
        if text != "P":
            for chosen, best in rows:
                lp.append(chosen)
                top.append({"x": best, "y": chosen})
        return {"tokens": ["t"] * n, "token_logprobs": lp, "top_logprobs": top}
    return echo


def test_gaps_and_verdict():
    print("### разрывы считаются только по ответу")
    saved = vllm.echo
    vllm.echo = fake_echo(4, [(-0.2, -0.2), (-1.0, -0.3), (-2.0, -0.5)])
    try:
        g = greedy.gaps("http://x/v1", "m", "P", "A")
    finally:
        vllm.echo = saved
    ok(len(g) == 3, f"три токена ответа, промпт не в счёт ({len(g)})")
    ok(abs(g[0]) < 1e-9 and abs(g[1] - 0.7) < 1e-9 and abs(g[2] - 1.5) < 1e-9,
       f"разрыв = лучший − выбранный ({g})")

    print("\n### вердикт: число «не своих» токенов, а не наибольший разрыв")
    honest = [0.0] * 300 + [4.3, 2.4]   # два одиночных разрыва, как в замере
    v = greedy.verdict(honest)
    ok(not v["suspect"], "два больших разрыва на 302 токена — не подделка")
    ok(v["over_gap"] == 2 and v["max_gap"] == 4.3, f"сводка верна ({v})")
    v = greedy.verdict([0.0] * 100 + [0.6, 0.7, 0.8])
    ok(v["suspect"], "три токена с разрывом больше 0,5 — подозрение")
    v = greedy.verdict([0.25] * 50)
    ok(not v["suspect"], "мелкие разрывы шага bf16 не в счёт")
    ok(greedy.verdict([])["tokens"] == 0, "пустой ответ не роняет")


def test_check_never_raises():
    print("\n### сбой жадной проверки не мешает основной")
    saved = vllm.echo

    def down(*a, **k):
        raise OSError("connection refused")
    vllm.echo = down
    try:
        ok(greedy.check("http://x/v1", "m", "P", "A") is None,
           "сервер недоступен — None, а не исключение")
        vllm.echo = lambda *a, **k: {"tokens": []}
        ok(greedy.check("http://x/v1", "m", "P", "A") is None,
           "ответ без top_logprobs — None")
    finally:
        vllm.echo = saved


def test_score_payload_unchanged():
    print("\n### score для хэша — прежний запрос, logprobs=1")
    seen = []
    saved = vllm._post

    def cap(base, path, payload, timeout=600):
        seen.append(payload)
        return {"choices": [{"logprobs": {"token_logprobs": [None, -0.5],
                                          "tokens": ["a", "b"]}}]}
    vllm._post = cap
    try:
        got = vllm.score("http://x/v1", "m", "текст")
        vllm.echo("http://x/v1", "m", "текст", 5)
    finally:
        vllm._post = saved
    ok(got == [None, -0.5], "score возвращает только token_logprobs")
    ok(seen[0] == {"model": "m", "prompt": "текст", "max_tokens": 0,
                   "echo": True, "logprobs": 1, "temperature": 0, "top_p": 1},
       f"запрос score байт в байт прежний ({seen[0]})")
    ok(seen[1]["logprobs"] == 5, "жадная проверка просит top-5 отдельным вызовом")


def test_journal():
    print("\n### журнал вердиктов")
    d = tempfile.mkdtemp()
    path = os.path.join(d, "greedy.jsonl")
    out = []

    class L(loop.Loop):
        def __init__(self):
            self.greedy_log = path
            self.clock = lambda: 1700000000
            self.out = None

        def say(self, s):
            out.append(s)

    lp = L()
    v = greedy.verdict([0.0] * 10 + [1.0] * 3)
    lp.note_greedy(12, {"executor": "axm1x"}, v, True)
    lp.note_greedy(13, {"executor": "axm1y"}, None, True)
    rows = [json.loads(x) for x in open(path, encoding="utf-8")]
    ok(len(rows) == 1, "в файл попал только выполненный вердикт")
    r = rows[0]
    ok(r["job_id"] == 12 and r["executor"] == "axm1x" and r["suspect"]
       and r["hash_same"] is True and r["at"] == 1700000000,
       f"строка журнала полная ({r})")
    ok(any("ЧУЖУЮ МОДЕЛЬ" in s for s in out), "подозрение видно в журнале узла")
    ok(any("не выполнена" in s for s in out), "невыполненная проверка названа")


def main():
    test_gaps_and_verdict()
    test_check_never_raises()
    test_score_payload_unchanged()
    test_journal()
    if fails:
        print(f"\nПРОВАЛ: {len(fails)}")
        sys.exit(1)
    print("\nжадная проверка ведёт себя как задумано")


if __name__ == "__main__":
    main()
