#!/usr/bin/env python3
"""Логика демонов на подставном vLLM.

Модель здесь не нужна: проверяется, что демоны правильно обязуются,
правильно отказываются и что сообщения, которые они собирают, — те,
которые ждёт контракт.

Один сценарий особенно важен: проверяющий обязан ОТКАЗАТЬСЯ, если
вектор исполнителя не сходится с обязательством из цепочки. Без этого он
посчитал бы расхождение по чужим данным и обвинил честного.

Запуск:  python protocol/daemon/test_daemons.py
"""

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import canon  # noqa: E402
import checker  # noqa: E402
import executor  # noqa: E402
import vllm_client as vllm  # noqa: E402

#: Настоящая оценка, снятая до того, как тесты начнут её подменять:
#: ниже проверяются её собственные повторы, а не подставная.
REAL_SCORE = vllm.score

#: Настоящая проверка узла. Один из тестов подменяет её заглушкой, и без
#: возврата к настоящей заглушка протекала бы в следующие тесты.
REAL_SELF_CHECK = vllm.self_check_problems

fails = []


def ok(cond, what):
    print(f"  {'OK  ' if cond else 'ПЛОХО'} {what}")
    if not cond:
        fails.append(what)


class FakeVllm:
    """Подставной сервер.

    `deterministic=False` меняет ХВОСТ ответа от вызова к вызову. Это не
    имитация выборки: честное жадное порождение под нагрузкой ведёт себя
    ровно так же (все 52 разошедшиеся пары суточного прогона совпали на
    44% ответа и разошлись дальше), и узел с таким поведением обязан
    работать.

    `sampling=True` меняет НАЧАЛО — вот это выборка, и её узел проходить
    не должен.

    `score_jitter` сдвигает оценку от вызова к вызову, в микродолях: так
    выглядит узел, оценка которого не повторяется.
    """

    def __init__(self, logprobs, deterministic=True, answer="  2, 3, 5, 7, 11",
                 sampling=False, score_jitter=0):
        self.logprobs = logprobs
        self.deterministic = deterministic
        self.answer = answer
        self.sampling = sampling
        self.score_jitter = score_jitter
        self.calls = 0
        self.scores = 0

    def generate(self, base, model, prompt, max_tokens):
        self.calls += 1
        if self.sampling:
            return f"{self.calls} {self.answer}", ["t"] * 120
        suffix = "" if self.deterministic else f" {self.calls}"
        return self.answer + suffix, ["t"] * 120

    def score(self, base, model, text):
        self.scores += 1
        if not self.score_jitter:
            return self.logprobs
        shift = self.score_jitter * self.scores / 1e6
        return [None if x is None else x + shift for x in self.logprobs]


def patch(fake):
    vllm.generate = fake.generate
    vllm.score = fake.score
    # self_check_problems НЕ подменяется: он пользуется и generate, и
    # score, то есть на подставном сервере работает настоящий.
    vllm.self_check_problems = REAL_SELF_CHECK


def main():
    honest = [-0.5 - i * 0.001 for i in range(100)]

    print("### исполнитель обязуется и собирает сообщение контракта")
    patch(FakeVllm(honest))
    r = executor.run("http://x/v1", "m", 7, "Назови числа.", 120, None)
    ok(r is not None, "исполнитель отработал")
    msg = r["msg"]["submit_answer"]
    ok(msg["job_id"] == 7, "номер задания на месте")
    ok(len(msg["logprobs_hash"]) == 64, "обязательство вектором — sha256")
    ok(msg["answer_tokens"] == 120, "длина ответа в токенах")
    ok(set(msg) == {"job_id", "answer_hash", "logprobs_hash", "answer_tokens"},
       "поля ровно те, что ждёт контракт")
    producer_vector = canon.to_micro(honest)
    ok(msg["logprobs_hash"] == canon.commit_hash(producer_vector),
       "хэш считается по канону")

    print("\n### узел, расходящийся в ХВОСТЕ порождения, работать ОБЯЗАН")
    # Раньше здесь требовался отказ, и это было ошибкой: под нагрузкой
    # два жадных порождения совпали 44 раза из 96, то есть честный узел
    # отказывался работать именно тогда, когда зарабатывает. Все 52
    # разошедшиеся пары суточного прогона совпали на 44% ответа.
    patch(FakeVllm(honest, deterministic=False))
    ok(executor.run("http://x/v1", "m", 8, "p", 120, None) is not None,
       "исполнитель не отказался из-за хвоста")

    print("\n### узел с ВЫБОРКОЙ не работает")
    patch(FakeVllm(honest, sampling=True))
    ok(executor.run("http://x/v1", "m", 8, "p", 120, None) is None,
       "исполнитель отказался при выборке")

    print("\n### узел, у которого не повторяется ОЦЕНКА, не работает")
    patch(FakeVllm(honest, score_jitter=30_000))
    ok(executor.run("http://x/v1", "m", 8, "p", 120, None) is None,
       "исполнитель отказался при неповторяющейся оценке")

    print("\n### сдвиг оценки в пределах допуска работать не мешает")
    patch(FakeVllm(honest, score_jitter=1_000))
    ok(executor.run("http://x/v1", "m", 8, "p", 120, None) is not None,
       "сдвиг 1 000 при пределе 24 000 — не повод отказывать")

    print("\n### честный проверяющий: расхождение около замеренного")
    # Его вектор отличается от вектора исполнителя на 0,0127 — это
    # честное среднее из замеров.
    mine = [x - 0.0127 for x in honest]
    patch(FakeVllm(mine))
    r = checker.run("http://x/v1", "m", 9, "p", "a", producer_vector,
                    canon.commit_hash(producer_vector))
    ok(r is not None, "проверяющий отработал")
    ok(r["divergence_micro"] == 12_700, f"расхождение 12 700 (вышло {r and r['divergence_micro']})")
    cm = r["msg"]["submit_check"]
    ok(set(cm) == {"job_id", "divergence_micro", "logprobs_hash"},
       "поля ровно те, что ждёт контракт")
    ok(cm["divergence_micro"] < 36_000, "ниже порога — честного не обвиняет")

    print("\n### подлог превышает порог")
    # FP8 по весам: минимум из измеренных, 0,03778
    fraud = [x - 0.03778 for x in honest]
    patch(FakeVllm(fraud))
    r = checker.run("http://x/v1", "m", 10, "p", "a", producer_vector,
                    canon.commit_hash(producer_vector))
    ok(r["divergence_micro"] == 37_780, "расхождение 37 780")
    ok(r["divergence_micro"] >= 36_000, "порог превышен")

    print("\n### проверяющий отказывается от чужого вектора")
    patch(FakeVllm(mine))
    podmena = list(producer_vector)
    podmena[0] += 50_000
    r = checker.run("http://x/v1", "m", 11, "p", "a", podmena,
                    canon.commit_hash(producer_vector))
    ok(r is None, "вектор не сошёлся с обязательством — отказ, а не подсчёт")

    test_chain_params()
    test_score_retries()
    test_conformance()
    test_shipped_probe()
    test_checker_needs_probe()

    print()
    if fails:
        print(f"ПРОВАЛ, {len(fails)}:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("демоны ведут себя как задумано")
    return 0




def test_chain_params():
    """Шаг 12: параметры из цепочки, а узел сверяется с ними.

    Раньше связка жила константой в коде демона — договорённостью между
    людьми. Обновил один исполнитель рантайм, и порог для него молча
    перестал быть действительным, а сеть об этом не знала.
    """
    import chain

    print("### параметры читаются из цепочки")
    cfg = {
        "threshold_micro": "36000", "checks_required": 4,
        "min_answer_tokens": 32,
        "pinned_model": "Qwen/Qwen2.5-7B-Instruct",
        "pinned_runtime": "vllm-0.31.0", "pinned_dtype": "bfloat16",
        "settlement": {"cw20": "axm1uz…"}, "stake_denom": "uaxm",
        "conformance_limit_micro": "25000",
        "conformance_ref_hash": "0" * 64,
        "conformance_lens": [3, 3],
        "conformance_version": 1,
    }
    chain.query_smart = lambda rest, c, q, timeout=20: cfg
    p = chain.load_params("http://rest", "axm1c")
    ok(p.threshold_micro == 36000, "порог из цепочки, строка приведена к числу")
    ok(p.checks_required == 4, "число проверок из цепочки")
    ok(p.stake_denom == "uaxm", "денom залога из цепочки")
    ok(chain.settlement_label(p.settlement) == "cw20:axm1uz…",
       "вид расчёта прочитан")
    ok(chain.settlement_label({"native": "uaxm"}) == "uaxm",
       "родной расчёт назван своим денomom")
    try:
        chain.settlement_label({"бред": 1})
        ok(False, "неизвестный вид расчёта должен отвергаться")
    except chain.ChainUnavailable:
        ok(True, "неизвестный вид расчёта — отказ, а не умолчание")
    ok(p.conformance_limit_micro == 25000, "предел пробы из цепочки")
    ok(p.conformance_lens == (3, 3), "границы текстов пробы из цепочки")

    print("\n### неполные настройки — отказ, а не умолчание")
    chain.query_smart = lambda rest, c, q, timeout=20: {"threshold_micro": "36000"}
    try:
        chain.load_params("http://rest", "axm1c")
        ok(False, "неполные настройки должны отвергаться")
    except chain.ChainUnavailable as e:
        ok("pinned_model" in str(e), "названы отсутствующие поля")

    print("\n### версия рантайма приводится к одному виду")
    ok(chain.normalize_runtime("0.31.0") == "vllm-0.31.0", "0.31.0 -> vllm-0.31.0")
    ok(chain.normalize_runtime("vllm-0.31.0") == "vllm-0.31.0", "уже приведённое не портится")

    print("\n### расхождения узла с цепочкой перечисляются все")
    bad = chain.check_node_matches(p, "Qwen/Qwen2.5-7B-Instruct", "vllm-0.31.0", "bfloat16")
    ok(bad == [], "совпадающий узел проходит")
    bad = chain.check_node_matches(p, "meta-llama/Llama-3-8B", "vllm-0.32.0", "float16")
    ok(len(bad) == 3, f"три расхождения названы все, не первое (вышло {len(bad)})")

    print("\n### исполнитель с чужой версией рантайма не работает")
    honest = [-0.5 - i * 0.001 for i in range(100)]
    patch(FakeVllm(honest))
    vllm.server_version = lambda base, timeout=15: "0.32.0"
    ok(executor.run("http://x/v1", "Qwen/Qwen2.5-7B-Instruct", 1, "p", 120, None,
                    params=p) is None,
       "версия не та — отказ")

    print("\n### неопределимая версия тоже отказ, а не предположение")
    def boom(base, timeout=15):
        raise vllm.VersionUnknown("нет маршрута /version")
    vllm.server_version = boom
    ok(executor.run("http://x/v1", "Qwen/Qwen2.5-7B-Instruct", 2, "p", 120, None,
                    params=p) is None,
       "версию не определить — отказ")

    print("\n### совпадающий узел работает")
    vllm.server_version = lambda base, timeout=15: "0.31.0"
    r = executor.run("http://x/v1", "Qwen/Qwen2.5-7B-Instruct", 3, "p", 120, None,
                     params=p)
    ok(r is not None, "узел, сходящийся с цепочкой, отработал")


def test_score_retries():
    """Повторы оценки: vLLM 0.31.0 изредка давится своим же NaN.

    Замерено на живом 4090: с первой попытки отказ на 34 текстах из 40,
    а повторы сходятся к тому же вектору. Без повторов проверяющий не
    работал бы на большинстве заданий, и это выяснилось только на
    настоящем сервере — подставной такого не делает.
    """
    import urllib.error

    print("### оценка повторяется, пока сервер давится своим NaN")
    nan_err = lambda: urllib.error.HTTPError(
        "u", 400, "Bad Request", {},
        io.BytesIO(b'{"error":{"message":"Out of range float values are '
                   b'not JSON compliant: nan"}}'))

    calls = []
    def flaky(base, path, payload, timeout=600):
        calls.append(1)
        if len(calls) < 3:
            raise nan_err()
        return {"choices": [{"logprobs": {"token_logprobs": [None, -0.5]}}]}
    vllm._post = flaky
    got = REAL_SCORE("http://x/v1", "m", "текст")
    ok(got == [None, -0.5], "вектор получен после повторов")
    ok(len(calls) == 3, f"повторов ровно столько, сколько нужно ({len(calls)})")

    print("\n### если не отдаёт совсем — отказ, а не пустой вектор")
    vllm._post = lambda *a, **k: (_ for _ in ()).throw(nan_err())
    try:
        REAL_SCORE("http://x/v1", "m", "текст")
        ok(False, "должен был отказать")
    except vllm.ScoringFailed as e:
        ok(str(vllm.SCORE_ATTEMPTS) in str(e), "в отказе названо число попыток")

    print("\n### чужую ошибку сервера не повторяет")
    other = lambda *a, **k: (_ for _ in ()).throw(
        urllib.error.HTTPError("u", 400, "Bad Request", {},
                               io.BytesIO(b'{"error":{"message":"model not found"}}')))
    n = []
    def counting(*a, **k):
        n.append(1); other()
    vllm._post = counting
    try:
        REAL_SCORE("http://x/v1", "m", "текст")
        ok(False, "должен был пробросить")
    except urllib.error.HTTPError:
        ok(len(n) == 1, "попытка одна: повторять нечего")


def test_conformance():
    """Проба на эталонном векторе.

    Правило обязано совпадать с контрактным до последнего знака: контракт
    пересчитывает его при споре, и расхождение демона с контрактом
    означало бы, что честный узел сам себя подводит под бан.
    """
    import json
    import tempfile

    import conformance

    print("### эталонный набор, которым обязалась цепочка")
    ref = [[-500_000, -500_000, -500_000], [-400_000, -400_000, -400_000]]
    glued = [x for v in ref for x in v]
    probe = {"lens": [3, 3], "texts": ["текст один", "текст два"],
             "ref": ref, "ref_hash": canon.commit_hash(glued),
             "limit_micro": 25_000}
    path = tempfile.mktemp(suffix=".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(probe, f, ensure_ascii=False)

    class P:
        conformance_limit_micro = 25_000
        conformance_ref_hash = probe["ref_hash"]
        conformance_lens = (3, 3)
        conformance_version = 7
        pinned_model = "m"
        pinned_runtime = "vllm-0.31.0"
        pinned_dtype = "bfloat16"

    got = conformance.load_probe(path, P)
    ok(got["ref_hash"] == probe["ref_hash"], "эталон сошёлся с цепочкой")

    print("\n### чужой эталон — отказ, а не работа по нему")
    class Other(P):
        conformance_ref_hash = "f" * 64
    try:
        conformance.load_probe(path, Other)
        ok(False, "должен был отказать")
    except conformance.ProbeMismatch as e:
        ok("цепочк" in str(e), "названо расхождение с цепочкой")

    print("\n### испорченный файл эталона — отказ")
    bad_path = tempfile.mktemp(suffix=".json")
    spoiled = dict(probe)
    spoiled["ref"] = [[-1, -1, -1], [-2, -2, -2]]
    with open(bad_path, "w", encoding="utf-8") as f:
        json.dump(spoiled, f)
    try:
        conformance.load_probe(bad_path, P)
        ok(False, "должен был отказать")
    except conformance.ProbeMismatch:
        ok(True, "подмена содержимого при том же хэше не проходит")

    print("\n### максимум ПО ТЕКСТАМ, а не среднее по склейке")
    # Первый текст сходится точно, второй расходится на 48 000. Среднее по
    # склейке 24 000 — ниже предела 25 000; максимум 48 000 — выше.
    # Именно так выглядит неверный режим: он портит РЕДКИЕ тексты.
    mine = list(glued)
    for i in range(3, 6):
        mine[i] -= 48_000
    ok(canon.divergence_micro(glued, mine) == 24_000,
       "среднее по склейке ниже предела — потому оно и не годится")
    worst, per = conformance.max_per_text(glued, mine, [3, 3])
    ok(worst == 48_000, f"максимум по текстам 48 000 (вышло {worst})")
    ok(per == [0, 48_000], f"расхождения по текстам названы по отдельности ({per})")

    print("\n### честный узел проходит и собирает сообщение контракта")
    honest = [x - 12_700 for x in glued]
    patch(FakeVllm(None))
    vllm.server_version = lambda base, timeout=15: "0.31.0"
    vllm.score = lambda b, m, text: (
        [None] + [x / 1e6 for x in (honest[:3] if text == "текст один" else honest[3:])])
    r = conformance.run("http://x/v1", "m", params=P, probe_path=path)
    ok(r is not None, "проба пройдена")
    msg = r["msg"]["submit_conformance"]
    ok(set(msg) == {"divergence_micro", "logprobs_hash"},
       "поля ровно те, что ждёт контракт")
    ok(msg["divergence_micro"] == 12_700,
       f"расхождение 12 700 (вышло {msg['divergence_micro']})")
    ok(r["ref_version"] == 7, "версия эталона из цепочки")

    print("\n### выше предела — отказ работать, а не молчаливая работа")
    over = [x - 200_000 for x in glued]
    vllm.score = lambda b, m, text: (
        [None] + [x / 1e6 for x in (over[:3] if text == "текст один" else over[3:])])
    ok(conformance.run("http://x/v1", "m", params=P, probe_path=path) is None,
       "узел отказался")

    print("\n### другая длина вектора — отказ: оценивалось не то")
    vllm.score = lambda b, m, text: [None] + [-0.5, -0.5]
    ok(conformance.run("http://x/v1", "m", params=P, probe_path=path) is None,
       "длина не та — отказ, а не сравнение общей части")

    print("\n### узел, не повторяющий сам себя, пробу не проходит")
    vllm.score = lambda b, m, text: (
        [None] + [x / 1e6 for x in (honest[:3] if text == "текст один" else honest[3:])])
    vllm.self_check_problems = lambda b, m, **kw: ["узел негоден"]
    ok(conformance.run("http://x/v1", "m", params=P, probe_path=path) is None,
       "его собственные числа ничего не значат")

    print("\n### состояние пробы по данным цепочки")
    import chain
    ok(chain.conformance_problems(P, None) != [], "незарегистрированный отсеян")
    ok(chain.conformance_problems(P, {"conformance": None}) != [],
       "без пробы отсеян")
    ok(chain.conformance_problems(P, {"conformance": {"ref_version": 6}}) != [],
       "просроченная проба отсеяна")
    ok(chain.conformance_problems(P, {"conformance": {"ref_version": 7}}) == [],
       "действующая проба проходит")


def test_shipped_probe():
    """Эталон, который лежит в репозитории, сходится сам с собой.

    Он снят на живом 4090 (vLLM 0.31.0, bfloat16) и воспроизводим: два
    прогона через перезапуск сервера дали 40 побитовых совпадений из 40.
    Если файл поехал, узлы начнут сравниваться с мусором.
    """
    import conformance

    print("### эталон из репозитория")
    p = conformance.load_probe(conformance.PROBE_PATH)
    ok(len(p["texts"]) == len(p["ref"]) == len(p["lens"]),
       "текстов, векторов и границ одинаково")
    ok(len(p["texts"]) == 34, f"34 текста (вышло {len(p['texts'])})")
    ok(all(len(v) >= 32 for v in p["ref"]),
       "каждый текст не короче 32 чисел: ниже этой длины способ не работает")
    ok(p["limit_micro"] == 60_000, "предел 60 000 — выведен из замеров")
    obs = p["honest_observations"]
    ok(max(v for k, v in obs.items() if "wrong" not in k) == 22_421,
       "худшее честное наблюдение записано рядом с пределом")
    ok(p["limit_micro"] > 22_421 * 2,
       "запас к худшему честному не меньше двойного")
    ok(obs["wrong_execution_mode_max"] > p["limit_micro"] * 5,
       "неверный режим превышает предел с кратным запасом")
    ok(sum(p["lens"]) == 3948, f"всего 3 948 чисел (вышло {sum(p['lens'])})")


def test_checker_needs_probe():
    """Проверяющий без действующей пробы не работает.

    Контракт её с него требует, и правильно: его завышенное расхождение
    это не подлог исполнителя, а ложное обвинение честного.
    """
    print("### проверяющий со просроченной пробой отказывается")
    honest = [-0.5 - i * 0.001 for i in range(100)]
    pv = canon.to_micro(honest)
    patch(FakeVllm([x - 0.0127 for x in honest]))
    vllm.server_version = lambda base, timeout=15: "0.31.0"

    class P:
        threshold_micro = 36_000
        pinned_model = "m"
        pinned_runtime = "vllm-0.31.0"
        pinned_dtype = "bfloat16"
        conformance_version = 7

    r = checker.run("http://x/v1", "m", 1, "p", "a", pv,
                    canon.commit_hash(pv), params=P,
                    record={"conformance": {"ref_version": 6}})
    ok(r is None, "просроченная проба — отказ")
    r = checker.run("http://x/v1", "m", 2, "p", "a", pv,
                    canon.commit_hash(pv), params=P,
                    record={"conformance": {"ref_version": 7}})
    ok(r is not None, "с действующей пробой работает")


if __name__ == "__main__":
    raise SystemExit(main())
