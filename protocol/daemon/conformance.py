#!/usr/bin/env python3
"""Проба на эталонном векторе: сверка связки ПО ПОВЕДЕНИЮ, а не по именам.

Зачем она. Контракт прибивает тройку модель/рантайм/dtype и отклоняет
чужую при регистрации. Замер на живом 4090 показал, что этого мало: на
одной и той же карте, с той же моделью, той же версией vLLM и тем же
dtype, `--enforce-eager` против пути по умолчанию дал ни одного
побитового совпадения из 40 векторов и расхождение до 413 488 микродолей
— внутри полосы подлога (минимум подлога FP8 37 780). То есть честный
узел в неверном режиме исполнения выглядит жуликом, и все три имени у
него при этом правильные. Сверять имена значит проверять то, что узел
о себе СКАЗАЛ.

Как устроена проба. В цепочке лежит обязательство эталонным набором
(хэш склейки эталонных векторов), длины текстов и предел. Сами векторы
вне цепочки — 3 948 чисел — тем же приёмом, что и вектор задания. Узел
оценивает те же тексты, считает расхождение с эталоном и публикует
максимум вместе с обязательством СВОИМИ векторами. Соврать можно —
расхождение он считает сам, — но обязательство делает ложь
опровержимой: любой раскрывает оба набора, контракт пересчитывает и
банит совравшего.

Почему максимум по текстам, а не среднее по склейке. Неверный режим
портит РЕДКИЕ тексты: у eager 29 векторов из 40 совпали с собой
побитово, а остальные разошлись до 413 488. Среднее по склейке такую
порчу размывает — замерено, что зазор падает с 20x до 1,5x, и жулик
пробу проходит.

Предел 60 000, и он выведен из данных, а не выбран. Честные максимумы по
текстам: A100 против 4090 — 19 656, 3090 против 4090 — 20 137, ДРУГОЙ
4090 против этого эталона — 22 421. Последнее число и заставило поднять
предел: прежние 25 000 оставляли запас 1,1x, и четвёртая карта могла бы
получить отказ, будучи честной. Неверный режим превышает 60 000 на
четырёх текстах из 34 при максимуме 410 012, то есть обнаружение не
ослабляется.

Чего проба по-прежнему не знает: карт, кроме замеренных. Узел на H100
может честно разойтись сильнее. Поэтому предел — параметр цепочки, а не
константа здесь, и с новыми картами его надо выводить заново.
"""

import argparse
import json
import os
import sys

import canon
import chain
import vllm_client as vllm

#: Эталон рядом с демоном. Это не источник истины: истина — обязательство
#: в цепочке, и файл сверяется с ним перед работой.
#:
#: Эталон У КАЖДОГО КЛАССА СВОЙ, и это необходимость: между Ampere и Ada
#: те же 334 текста не совпали ни на одном, среднее расхождение 15 178.
#: Проверять Ampere эталоном Ada бессмысленно. Какой брать — определяет
#: архитектура узла; сверка с цепочкой всё равно отвергнет чужой.
PROBE_BY_ARCH = {
    "sm_86": "probe-v2-sm86.json",
    "sm_89": "probe-v2-sm89.json",
}
PROBE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe")
PROBE_PATH = os.path.join(PROBE_DIR, "probe-v2-sm89.json")


def probe_path_for(arch):
    """Эталон класса. Неизвестная архитектура — отказ, а не умолчание:
    умолчание здесь означало бы сверку с эталоном чужого класса."""
    name = PROBE_BY_ARCH.get(arch)
    if name is None:
        raise ProbeMismatch(
            f"для архитектуры {arch!r} эталона нет; известны: "
            f"{sorted(PROBE_BY_ARCH)}")
    return os.path.join(PROBE_DIR, name)


class ProbeMismatch(Exception):
    """Эталонный набор не тот, которым обязалась цепочка."""


def load_probe(path=PROBE_PATH, params=None):
    """Прочитать эталон и СВЕРИТЬ его с обязательством из цепочки.

    Без сверки подменённый файл эталона давал бы любую пробу: узел
    сравнивал бы себя с собой. Поэтому расхождение здесь — отказ.
    """
    with open(path, encoding="utf-8") as f:
        probe = json.load(f)
    ref = probe["ref"]
    lens = [len(v) for v in ref]
    if lens != list(probe["lens"]):
        raise ProbeMismatch(
            f"длины в файле не сходятся с векторами: {probe['lens']} против {lens}")
    glued = [x for v in ref for x in v]
    got = canon.commit_hash(glued)
    if got != probe["ref_hash"]:
        raise ProbeMismatch(
            f"файл эталона испорчен: хэш {got}, в файле {probe['ref_hash']}")
    if params is not None:
        if got != params.conformance_ref_hash:
            raise ProbeMismatch(
                "эталон не тот, которым обязалась цепочка:\n"
                f"  в файле:  {got}\n  в цепочке: {params.conformance_ref_hash}")
        if tuple(lens) != tuple(params.conformance_lens):
            raise ProbeMismatch(
                f"границы текстов не те: в файле {lens}, "
                f"в цепочке {list(params.conformance_lens)}")
    return probe


def max_per_text(reference, mine, lens):
    """Максимум среднего расхождения ПО ТЕКСТАМ.

    Считается так же, как в контракте: тот же порядок, то же деление
    вниз, та же нарезка по длинам. Расходиться им нельзя — контракт
    пересчитает при споре.
    """
    want = sum(lens)
    for name, v in (("эталон", reference), ("свой", mine)):
        if len(v) != want:
            raise canon.EmptyVector(
                f"{name} набор длиной {len(v)}, эталон требует {want}")
    worst, at, per = 0, 0, []
    for n in lens:
        d = canon.divergence_micro(reference[at:at + n], mine[at:at + n])
        per.append(d)
        worst = max(worst, d)
        at += n
    return worst, per


def run(base, model, params=None, dtype="bfloat16", probe_path=PROBE_PATH):
    """Пройти пробу. Возвращает None, если узел работать не должен."""
    if params is not None:
        try:
            actual = chain.normalize_runtime(vllm.server_version(base))
            bad = chain.check_node_matches(params, model, actual, dtype)
        except vllm.VersionUnknown as e:
            bad = [f"версию рантайма определить не удалось: {e}"]
        if bad:
            for b in bad:
                print(f"ОТКАЗ: {b}", file=sys.stderr)
            return None
    # Узел, не повторяющий сам себя, пробу проходить не должен: его
    # собственные числа ничего не значат.
    for p in vllm.self_check_problems(base, model):
        print(f"ОТКАЗ: {p}", file=sys.stderr)
        return None

    probe = load_probe(probe_path, params)
    lens = [len(v) for v in probe["ref"]]
    reference = [x for v in probe["ref"] for x in v]

    mine = []
    for i, text in enumerate(probe["texts"]):
        v = canon.require_nonempty(canon.to_micro(vllm.score(base, model, text)))
        if len(v) != lens[i]:
            # Оценивается готовый текст, обрываться ему негде. Другая
            # длина значит, что оценивалось не то: другой токенизатор,
            # другая модель. Это отказ, а не повод сравнивать общую часть.
            print(f"ОТКАЗ: текст {i} дал {len(v)} чисел, эталон {lens[i]}. "
                  "Оценивается не то — модель или токенизатор другие.",
                  file=sys.stderr)
            return None
        mine += v

    worst, per = max_per_text(reference, mine, lens)
    # Предел НОЛЬ: внутри класса честный узел даёт расхождение ровно 0 —
    # замерено на второй карте, другой машине и другом драйвере, 334
    # текста из 334. Всё, что меняет арифметику, не совпадает ни на
    # одном тексте. Прежний предел 60 000 неверный режим пропускал.
    limit = params.conformance_limit_micro if params else None
    if limit is not None and worst > limit:
        over = [i for i, d in enumerate(per) if d > limit]
        print(f"ОТКАЗ: проба не пройдена. Максимум {worst} при пределе "
              f"{limit}, разошлось на текстах {over}.", file=sys.stderr)
        print("  Это НЕ обвинение: несовпадение связки означает, что узел "
              "НЕСРАВНИМ с остальными, а не что он жулик. Вердикт выносится "
              "только тем, кто пробу прошёл.", file=sys.stderr)
        print("  Чаще всего причина — не тот кеш ядер: поставьте раздаваемый "
              "архив своего класса ДО первого запуска сервера и сверьте его "
              "sha256.", file=sys.stderr)
        return None

    return {
        "msg": {
            "submit_conformance": {
                "divergence_micro": worst,
                "logprobs_hash": canon.commit_hash(mine),
            }
        },
        "divergence_micro": worst,
        "per_text": per,
        "texts": len(lens),
        "ref_version": params.conformance_version if params else None,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", required=True)
    p.add_argument("--model", default="Qwen/Qwen2.5-7B-Instruct")
    p.add_argument("--dtype", default="bfloat16")
    p.add_argument("--probe", default=PROBE_PATH)
    p.add_argument("--rest", help="REST цепочки")
    p.add_argument("--contract", help="адрес контракта")
    a = p.parse_args()

    params = None
    if a.rest and a.contract:
        try:
            params = chain.load_params(a.rest, a.contract)
        except chain.ChainUnavailable as e:
            print(f"ОТКАЗ: параметры из цепочки не получены: {e}", file=sys.stderr)
            return 1
    else:
        print("ВНИМАНИЕ: без параметров из цепочки предел неизвестен — "
              "проба считается, но не оценивается", file=sys.stderr)

    try:
        r = run(a.base_url, a.model, params=params, dtype=a.dtype,
                probe_path=a.probe)
    except ProbeMismatch as e:
        print(f"ОТКАЗ: {e}", file=sys.stderr)
        return 1
    if r is None:
        return 1
    print(f"проба пройдена: максимум {r['divergence_micro']} микродолей по "
          f"{r['texts']} текстам, эталон версии {r['ref_version']}",
          file=sys.stderr)
    print(json.dumps(r["msg"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
