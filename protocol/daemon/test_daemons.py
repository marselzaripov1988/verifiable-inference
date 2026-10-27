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
    test_delivery()
    test_sroki_v_demonah()
    test_server_nedostupen()

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
        "checks_required": 2,
        "pinned_model": "Qwen/Qwen2.5-7B-Instruct",
        "pinned_runtime": "vllm-0.31.0", "pinned_dtype": "bfloat16",
        "pinned_arch": "sm_89", "kernel_cache_hash": "3753554454f2c9b8",
        "settlement": {"cw20": "axm1uz…"}, "stake_denom": "uaxm",
        "min_stake": "150000000", "verifier_fee_bps": 970,
        "conformance_limit_micro": "0",
        "conformance_ref_hash": "0" * 64,
        "conformance_lens": [3, 3],
        "conformance_version": 1,
        "claim_ttl_seconds": 600, "check_ttl_seconds": 3600,
    }
    chain.query_smart = lambda rest, c, q, timeout=20: cfg
    p = chain.load_params("http://rest", "axm1c")
    ok(p.checks_required == 2, "число проверок из цепочки")
    ok(p.pinned_arch == "sm_89", "архитектура класса из цепочки")
    ok(p.kernel_cache_hash == "3753554454f2c9b8", "хэш кеша ядер из цепочки")
    ok(p.stake_denom == "uaxm", "денom залога из цепочки")
    ok((p.claim_ttl_seconds, p.check_ttl_seconds) == (600, 3600),
       "сроки заданий из цепочки")
    # Без срока в настройках демон не работает: это не мелочь, которую
    # можно подставить по умолчанию. Умолчание разошлось бы с цепочкой, а
    # по сроку решается, успеет ли узел и когда задание у него отберут.
    bez = {k: v for k, v in cfg.items() if k != "claim_ttl_seconds"}
    chain.query_smart = lambda rest, c, q, timeout=20: bez
    try:
        chain.load_params("http://rest", "axm1c")
        ok(False, "настройки без срока должны отвергаться")
    except chain.ChainUnavailable as e:
        ok("claim_ttl_seconds" in str(e), "сказано, какого поля нет")
    chain.query_smart = lambda rest, c, q, timeout=20: cfg
    ok(chain.settlement_label(p.settlement) == "cw20:axm1uz…",
       "вид расчёта прочитан")
    ok(chain.settlement_label({"native": "uaxm"}) == "uaxm",
       "родной расчёт назван своим денomom")
    try:
        chain.settlement_label({"бред": 1})
        ok(False, "неизвестный вид расчёта должен отвергаться")
    except chain.ChainUnavailable:
        ok(True, "неизвестный вид расчёта — отказ, а не умолчание")
    ok(p.conformance_limit_micro == 0,
       "предел пробы из цепочки, и ноль — не отсутствие значения")
    ok(p.conformance_lens == (3, 3), "границы текстов пробы из цепочки")

    print("\n### неполные настройки — отказ, а не умолчание")
    chain.query_smart = lambda rest, c, q, timeout=20: {"checks_required": 2}
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
    """Эталоны классов, лежащие в репозитории, сходятся сами с собой.

    У каждого класса свой: между Ampere и Ada те же 334 текста не совпали
    ни на одном, среднее расхождение 15 178. Эталоном одного класса
    проверять другой бессмысленно, и спутать их нельзя.
    """
    import conformance

    for arch in ("sm_86", "sm_89", "sm_120"):
        print(f"### эталон класса {arch}")
        path = conformance.probe_path_for(arch)
        p = conformance.load_probe(path)
        ok(len(p["texts"]) == len(p["ref"]) == len(p["lens"]),
           f"{arch}: текстов, векторов и границ одинаково")
        ok(len(p["texts"]) == 34, f"{arch}: 34 текста (вышло {len(p['texts'])})")
        ok(sum(p["lens"]) == 3948,
           f"{arch}: всего 3 948 чисел (вышло {sum(p['lens'])})")
        # Предел НОЛЬ, а не запас. Прежние 60 000 выбирались как запас к
        # честному разбросу между картами — и неверный режим пропускали
        # целиком: на этих же текстах eager даёт худшее 28 025.
        ok(p["limit_micro"] == 0, f"{arch}: предел нулевой, а не запас")
        ok(p["arch"].startswith(arch), f"{arch}: класс назван в эталоне")
        ok("sha256" in p["kernel_cache"],
           f"{arch}: назван кеш ядер, на котором снят эталон")

    print("### эталоны классов различны")
    probes = {arch: conformance.load_probe(conformance.probe_path_for(arch))
              for arch in ("sm_86", "sm_89", "sm_120")}
    pairs = (("sm_86", "sm_89"), ("sm_86", "sm_120"), ("sm_89", "sm_120"))
    for x, y in pairs:
        a, b = probes[x], probes[y]
        ok(a["texts"] == b["texts"], f"{x} и {y}: тексты одни и те же")
        ok(a["ref_hash"] != b["ref_hash"],
           f"{x} и {y}: эталонные векторы разные — между классами равенства "
           "не бывает")
        ok(sum(1 for u, v in zip(a["ref"], b["ref"]) if u == v) == 0,
           f"{x} и {y}: не совпал ни один текст")

    print("### чужой класс не подсунуть")
    try:
        conformance.probe_path_for("sm_90")
        ok(False, "неизвестная архитектура должна отвергаться")
    except conformance.ProbeMismatch:
        ok(True, "неизвестная архитектура — отказ, а не эталон чужого класса")


def test_delivery():
    """Доставка текста проверяющему: подмена обязана отвергаться.

    Это не удобство, а защита от ложного обвинения. Текст приходит вне
    цепочки; подмени его — проверяющий оценит не то, его хэш не совпадёт
    с хэшем исполнителя, и при нулевом допуске контракт обвинит ЧЕСТНОГО.
    Двум проверяющим можно подсунуть одну и ту же подмену, так что
    правило «не совпало у всех» здесь не помогает.
    """
    import chain as ch
    import checker as chk

    print("### сверка текста с хэшами задания")
    prompt, answer = "Назови пять простых чисел.", "  2, 3, 5, 7, 11"
    job = {"prompt_hash": ch.text_hash(prompt),
           "answer_hash": ch.text_hash(answer),
           "logprobs_hash": "неважно"}
    ok(ch.job_problems(job, prompt, answer) == [], "тот самый текст проходит")
    bad = ch.job_problems(job, prompt + " ", answer)
    ok(len(bad) == 1 and "запрос не тот" in bad[0],
       "подменённый запрос отвергнут, и сказано какой именно")
    bad = ch.job_problems(job, prompt, answer + ".")
    ok(len(bad) == 1 and "ответ не тот" in bad[0], "подменённый ответ отвергнут")
    ok(len(ch.job_problems({}, prompt, answer)) == 2,
       "задания без хэшей недостаточно: сверять нечем, и это отказ")
    ok(ch.job_problems({"prompt_hash": ch.text_hash(prompt)}, prompt, answer)
       != [], "ответ ещё не сдан — работать не с чем")

    print("\n### проверяющий отказывается от подменённого текста")
    honest = [-0.5 - i * 0.001 for i in range(100)]
    patch(FakeVllm(honest))
    r = chk.run("http://x/v1", "m", 7, prompt, answer + " лишнее",
                job=job)
    ok(r is None, "подмена ловится ДО оценки, а не после")
    r = chk.run("http://x/v1", "m", 7, prompt, answer, job=job)
    ok(r is not None, "тот самый текст — работа идёт")

    print("\n### вектор исполнителя больше не обязателен")
    patch(FakeVllm(honest))
    r = chk.run("http://x/v1", "m", 7, prompt, answer, job=job)
    ok(r is not None and r["divergence_micro"] == 0,
       "без вектора расхождение нулевое, и это не мешает: вердикт по хэшу")
    ok("logprobs_hash" in r["msg"]["submit_check"],
       "свой хэш сдаётся всё равно — им и выносится вердикт")
    mine = [x - 0.0127 for x in honest]
    patch(FakeVllm(mine))
    pv = canon.to_micro(honest)
    r = chk.run("http://x/v1", "m", 7, prompt, answer, producer_vector=pv,
                producer_hash=canon.commit_hash(pv), job=job)
    ok(r is not None and r["divergence_micro"] == 12_700,
       f"с вектором считается свидетельство (вышло {r and r['divergence_micro']})")

    print("\n### связка исполнителя читается и из файла")
    import json as _json, os as _os, tempfile
    bundle = {"job_id": 7, "prompt": prompt, "answer": answer,
              "prompt_hash": ch.text_hash(prompt),
              "answer_hash": ch.text_hash(answer),
              "logprobs_hash": canon.commit_hash(pv), "vector": pv}
    fd, path = tempfile.mkstemp(suffix=".json")
    _os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        _json.dump(bundle, f, ensure_ascii=False)
    got = chk.load_bundle(path)
    ok(got["prompt"] == prompt and got["vector"] == pv,
       "связка прочитана целиком: текст и вектор вместе")
    ok(ch.job_problems(job, got["prompt"], got["answer"]) == [],
       "и она сходится с хэшами задания")
    _os.unlink(path)


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


def test_sroki_v_demonah():
    """Сроки заданий: демоны их учитывают.

    Сроки появились в контракте 26 октября. Демонам они нужны не для
    порядка: исполнитель, начавший работу по заданию с вышедшим сроком,
    платит за инференс и остаётся без оплаты — задание отберут.
    """
    import chain
    import checker
    import deadlines
    import executor

    P = chain.Params(
        checks_required=2, pinned_model="m", pinned_runtime="r",
        pinned_dtype="bfloat16", pinned_arch="sm_89", kernel_cache_hash="k",
        settlement={"native": "uaxm"}, stake_denom="uaxm",
        min_stake=150_000_000, verifier_fee_bps=970,
        conformance_limit_micro=0, conformance_ref_hash="x" * 64,
        conformance_lens=(3,), conformance_version=1,
        claim_ttl_seconds=600, check_ttl_seconds=3600)

    print("### граница срока совпадает с контрактной")
    j = {"state": "claimed", "claimed_at": 1000}
    ok(chain.deadline_of(j, P, 1500)["left"] == 100, "остаток считается")
    # В контракте условие строгое: `passed <= ttl` — отказ. Значит ровно
    # на границе срок ЕЩЁ не вышел, и демон обязан считать так же,
    # иначе он отказался бы от задания, которое контракт ещё держит.
    ok(not chain.deadline_of(j, P, 1600)["expired"],
       "ровно на границе срок не вышел — как в контракте")
    ok(chain.deadline_of(j, P, 1601)["expired"], "секундой позже — вышел")

    print("### без отметки времени срок считается давно вышедшим")
    # Записи до появления сроков отметки не имеют. В контракте такие
    # отбираемы немедленно (claimed_at.unwrap_or(0)), и демон не должен
    # считать их вечно живыми.
    ok(chain.deadline_of({"state": "claimed"}, P, 10_000)["expired"],
       "задание без отметки отбираемо")

    print("### у невзятого и закрытого срока нет")
    for st in ("open", "settled"):
        d = chain.deadline_of({"state": st}, P, 10_000)
        ok(d["left"] is None and not d["expired"], f"{st}: сроком не ограничено")

    print("### исполнитель отказывается от задания с вышедшим сроком")
    bad = executor.deadline_problems({"state": "claimed", "claimed_at": 0}, P, 10_000)
    ok(bad and "отберёт" in bad[0], "сказано, что работа пропадёт")
    ok(not executor.deadline_problems({"state": "claimed", "claimed_at": 9_000},
                                      P, 9_100),
       "по живому заданию работа идёт")
    print("### и от задания, которое не взято им")
    bad = executor.deadline_problems({"state": "open"}, P, 100)
    ok(bad and "claimed" in bad[0], "ответ принимается только по взятому")
    print("### запас узла уважается")
    bad = executor.deadline_problems({"state": "claimed", "claimed_at": 9_000},
                                     P, 9_500, min_left=200)
    ok(bad and "не успеть" in bad[0], "осталось 100 с при запросе 200 — отказ")

    print("### проверяющий предупреждает, но не отказывается")
    # Разница с исполнителем не в мягкости: пока никто не вызвал
    # expire_checks, сданная проверка засчитывается и может добрать
    # кворум, то есть довести задание до вердикта. Запрет такой работы
    # запрещал бы закрывать задания.
    expired, note = checker.deadline_note(
        {"state": "answered", "answered_at": 0}, P, 10_000)
    ok(expired is True, "срок распознан как вышедший")
    ok("засчитывается" in note, "сказано, что работа ещё может оплатиться")
    expired, note = checker.deadline_note(
        {"state": "answered", "answered_at": 9_000}, P, 9_100)
    ok(expired is False and "осталось" in note, "по живому заданию — остаток")

    print("### сообщения, которые пора отправить")
    ok(chain.due_message(7, {"state": "claimed", "claimed_at": 0}, P, 10_000)
       == {"release_job": {"job_id": 7}}, "брошенное задание возвращается")
    ok(chain.due_message(7, {"state": "answered", "answered_at": 0}, P,
                         10_000, checks=1)
       == {"expire_checks": {"job_id": 7}}, "несобранные проверки закрываются")
    # При собранном кворуме закрывать надо вердиктом: контракт отвергнет
    # закрытие сроком (ChecksEnough), и предлагать его нельзя.
    ok(chain.due_message(7, {"state": "answered", "answered_at": 0}, P,
                         10_000, checks=2) is None,
       "при собранном кворуме сроком не закрывают")
    # Без числа проверок предлагать нечего: вызов мог бы быть отвергнут.
    ok(chain.due_message(7, {"state": "answered", "answered_at": 0}, P,
                         10_000, checks=None) is None,
       "без числа проверок сроком не закрывают")
    ok(chain.due_message(7, {"state": "claimed", "claimed_at": 9_000}, P, 9_100)
       is None, "по живому заданию закрывать нечего")

    print("### обход заданий")
    jobs = {
        1: {"state": "settled"},
        2: {"state": "claimed", "claimed_at": 0},
        3: {"state": "answered", "answered_at": 0},
        4: {"state": "open"},
        5: {"state": "answered", "answered_at": 9_000},
    }
    def fake(rest, contract, q, timeout=20):
        if "job" in q:
            jid = q["job"]["job_id"]
            if jid not in jobs:
                raise chain.ChainUnavailable("нет такого задания")
            return jobs[jid]
        if "checks" in q:
            return {"checks": [{"checker": "c0"}], "mismatched": 0}
        raise AssertionError(q)
    saved = chain.query_smart
    chain.query_smart = fake
    try:
        rows = deadlines.scan("http://rest", "axm1c", P, 10_000, range(1, 9))
    finally:
        chain.query_smart = saved
    ok(len(rows) == 5, f"найдено 5 заданий, не больше (вышло {len(rows)})")
    due = [r["msg"] for r in rows if r["msg"]]
    ok(due == [{"release_job": {"job_id": 2}},
               {"expire_checks": {"job_id": 3}}],
       f"пора закрыть ровно два, и нужными сообщениями (вышло {due})")
    # Пятое задание свежее: срок у него не вышел, и в выдачу не попало.
    ok(not [r for r in rows if r["job_id"] == 5 and r["expired"]],
       "свежее задание не закрывается")

    print("### время берётся у цепочки, а не с местных часов")
    # Часы арендованной машины уходят, а срок проверяет контракт по
    # времени блока. Разбор формата проверяется прямо: наносекунды
    # отбрасываются, Z — зона UTC.
    import io as _io
    import json as _json
    import urllib.request as _ur
    class R:
        def __init__(self, t): self.t = t
        def read(self): return _json.dumps(
            {"block": {"header": {"time": self.t}}}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False
    saved = _ur.urlopen
    _ur.urlopen = lambda url, timeout=20: R("2026-10-26T12:00:00.123456789Z")
    try:
        t = chain.chain_time("http://rest")
    finally:
        _ur.urlopen = saved
    # Ожидаемое считается из даты, а не вписано числом: число я один раз
    # уже написал неверное, и тест поймал разбор вместо моей ошибки.
    import datetime as _dt
    want = int(_dt.datetime(2026, 10, 26, 12, 0, 0,
                            tzinfo=_dt.timezone.utc).timestamp())
    ok(t == want, f"время блока разобрано (вышло {t}, ждали {want})")
    _ur.urlopen = lambda url, timeout=20: R("не время")
    try:
        chain.chain_time("http://rest")
        ok(False, "неразборчивое время должно отвергаться")
    except chain.ChainUnavailable:
        ok(True, "неразборчивое время — отказ, а не догадка")
    finally:
        _ur.urlopen = saved


def test_server_nedostupen():
    """Недоступный сервер — ОТКАЗ, а не трассировка.

    Случай обычный: сервер ещё не поднялся после перезапуска, слушает
    другой порт, упал под нагрузкой. Демон запускают из сценария, и
    трассировка там не говорит ни адреса, ни что делать — а код возврата
    при падении и при отказе одинаков, так что сценарий не отличит
    «узла нет» от «узел жульничает».
    """
    import urllib.error

    import checker
    import conformance
    import executor
    import vllm_client as vllm

    print("### что считается недоступностью")
    # HTTPError и URLError — подклассы OSError, поэтому перечислять их
    # отдельно не нужно; а разбор ответа в этот набор НЕ входит нарочно.
    ok(issubclass(urllib.error.HTTPError, OSError), "HTTPError это OSError")
    ok(not isinstance(KeyError("choices"), vllm.NODE_FAILURES),
       "KeyError на разборе ответа недоступностью НЕ считается: это своя "
       "ошибка, и прятать её нельзя")
    ok(isinstance(vllm.ScoringFailed("x"), vllm.NODE_FAILURES),
       "исчерпанные попытки оценки — тоже отказ узла")

    print("### в отказе назван адрес, а не только код")
    e = urllib.error.HTTPError("http://узел:8000/v1/completions", 503,
                               "Service Unavailable", {}, None)
    ok("http://узел:8000/v1/completions" in vllm.why(e) and "503" in vllm.why(e),
       f"сказано, куда стучались и что ответили: {vllm.why(e)}")
    e = urllib.error.URLError("отказ в подключении")
    ok("не соединиться" in vllm.why(e), f"обрыв связи назван: {vllm.why(e)}")

    print("### демоны отказывают, а не падают")
    boom = lambda *a, **k: (_ for _ in ()).throw(
        urllib.error.HTTPError("http://x/v1/completions", 503,
                               "Service Unavailable", {}, None))
    saved = (vllm.self_check_problems, vllm.generate, vllm.score)
    vllm.self_check_problems = boom
    try:
        ok(executor.run("http://x/v1", "m", 1, "p", 10, None) is None,
           "исполнитель отказался, а не упал")
        ok(checker.run("http://x/v1", "m", 1, "p", "a") is None,
           "проверяющий отказался, а не упал")
    finally:
        vllm.self_check_problems = saved[0]

    print("### и когда сервер умер ПОСЛЕ самопроверки")
    # Так бывает чаще всего: самопроверка прошла, а на длинном тексте
    # сервер отвалился. Этот случай раньше тоже давал трассировку.
    vllm.self_check_problems = lambda *a, **k: []
    vllm.generate = boom
    vllm.score = boom
    try:
        ok(executor.run("http://x/v1", "m", 1, "p", 10, None) is None,
           "исполнитель отказался на порождении")
        ok(checker.run("http://x/v1", "m", 1, "p", "a") is None,
           "проверяющий отказался на оценке")
    finally:
        (vllm.self_check_problems, vllm.generate, vllm.score) = saved


if __name__ == "__main__":
    raise SystemExit(main())


