"""Какие модели узел умеет и что каждой нужно на каждом классе карт.

Модель узел берёт из цепочки (`pinned_model`), а не из своих настроек:
так один образ работает и до смены модели в реестре, и после неё, и
владельцу карты не нужно знать, на какой модели сейчас сеть.

На каждую пару «модель — класс» нужны две вещи, и обе замеряются, а не
выводятся:

- **кеш ядер.** Сборка с нуля выбирает ядра замером и не повторяется
  от машины к машине, поэтому кеш прибит и везётся в образе;
- **эталон пробы.** Векторы тех же 34 текстов на этой модели и этом
  классе. Чужой эталон отвергается сверкой с обязательством в цепочке.

Пары, которой здесь нет, узел не поднимает: отказ лучше, чем сборка
ядер с нуля и проба, которая заведомо не сойдётся.
"""

MODELS = {
    "Qwen/Qwen2.5-7B-Instruct": {
        "revision": None,
        # Замер 11 ноября, 4090: честные 0/96, подделки 27–31/32 из 32.
        "greedy": {"gap": 0.5, "min_tokens": 3},
        "classes": {
            "sm_86": {"cache": "sm_86", "probe": "probe-v2-sm86.json"},
            "sm_89": {"cache": "sm_89", "probe": "probe-v2-sm89.json"},
            "sm_120": {"cache": "sm_120", "probe": "probe-v2-sm120.json"},
        },
    },
    # Без отказов (abliteration), Apache 2.0, только safetensors. Коммит
    # прибит: веса под тем же именем могут перезалить, а кеш ядер и
    # эталон пробы сняты на этих. Замер — zapisi/qwen3-8b-abl-sm86.
    "huihui-ai/Huihui-Qwen3-8B-abliterated-v2": {
        "revision": "7d89db76029281fd8f3e6698a8e30738608105a9",
        # Замер 12 ноября, 4090 и 3090: при «≥ 3» честные задеты 5 из 128 —
        # у Qwen3 чаще иное разбиение на токены, и разрывы идут парами в
        # соседних позициях, честный максимум 4 токена. При «≥ 5» честные
        # 0/128, выборка T=0,7 28/32, Qwen3-4B 30/32, Qwen3-1.7B 31/32, FP8 1/32.
        "greedy": {"gap": 0.5, "min_tokens": 5},
        "classes": {
            "sm_86": {"cache": "q3-sm_86", "probe": "probe-q3-sm86.json"},
            "sm_89": {"cache": "q3-sm_89", "probe": "probe-q3-sm89.json"},
        },
    },
}


#: Правило жадной проверки для модели, которой нет в таблице.
GREEDY_DEFAULT = {"gap": 0.5, "min_tokens": 3}


def greedy_rule(model):
    """Порог жадной проверки для модели: у каждой свой, он замеряется."""
    return dict((MODELS.get(model) or {}).get("greedy") or GREEDY_DEFAULT)


class Unsupported(Exception):
    """Этой пары модели и класса узел не умеет."""


def lookup(model, arch):
    """Что нужно модели на классе: revision, имя кеша, файл эталона."""
    m = MODELS.get(model)
    if m is None:
        raise Unsupported(f"модель {model!r} узлу неизвестна; известны: "
                          f"{sorted(MODELS)}")
    c = m["classes"].get(arch)
    if c is None:
        raise Unsupported(f"для {model} на классе {arch!r} нет кеша и "
                          f"эталона; есть: {sorted(m['classes'])}")
    return {"revision": m["revision"], "cache": c["cache"], "probe": c["probe"]}


if __name__ == "__main__":
    # start.sh: python3 models.py МОДЕЛЬ КЛАСС → «кеш revision» одной строкой
    import sys
    try:
        r = lookup(sys.argv[1], sys.argv[2])
    except Unsupported as e:
        print(e, file=sys.stderr)
        raise SystemExit(1)
    print(r["cache"], r["revision"] or "-")
