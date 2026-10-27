#!/usr/bin/env python3
"""Доставка текста вне цепочки — и сверка его С цепочкой.

В цепочке лежат только хэши: `prompt_hash` у задания и `answer_hash` со
`logprobs_hash` после сдачи. Сами тексты ходят мимо неё, и это
осознанный выбор — вектор на ответ это ~120 чисел, а запрос с ответом
могут быть любой длины.

Отсюда главное свойство, на котором всё держится: **доставка не требует
доверия**. Подмена ловится сверкой с хэшем из задания, поэтому брать
текст можно откуда угодно — из каталога, у соседа, у самого
исполнителя, — и пробовать можно всех подряд. Если бы доставке
приходилось доверять, протокол нуждался бы в доверенном складе, а это
ровно то, от чего он уходит.

Вынесено в свой модуль затем, что этим пользуются ОБЕ стороны:
`loop.py` — узел, которому нужны запрос и чужая связка, и `client.py` —
заказчик, которому нужен свой ответ. Разойдись у них сверка, одна из
сторон считала бы верным то, что другая отвергает.
"""

import json
import os
import urllib.request

import chain


def read_source(src, name, timeout=30):
    """Один источник: каталог на диске либо http(s)-адрес."""
    if src.startswith(("http://", "https://")):
        url = f"{src.rstrip('/')}/{name}"
        req = urllib.request.Request(
            url, headers={"User-Agent": "verifiable-inference/0.1"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8")
    with open(os.path.join(src, name), encoding="utf-8") as f:
        return f.read()


def fetch_verified(sources, names, check, timeout=30):
    """Взять один из `names` из любого источника и проверить ПО ЦЕПОЧКЕ.

    Возвращает (данные, откуда, причины отказов). Данные None — ни один
    источник не дал того, что сходится с цепочкой; причины перечислены
    все, потому что «не найдено» и «найдено, но не то» требуют от
    человека разного.

    Имён несколько по делу. Номер заданию присваивает контракт, то есть
    заказчик узнаёт его ТОЛЬКО ПОСЛЕ отправки, а запрос надо выложить до
    того — иначе между объявлением и выкладкой задание берут, и работать
    исполнителю нечем. Поэтому запрос лежит под именем своего хэша, а
    `<номер>.txt` остаётся как второе имя. Выбор имени ничего не решает:
    решает сверка с хэшем из задания.

    Порядок источников — порядок предпочтения, но доверия нет ни к
    одному.
    """
    if isinstance(names, str):
        names = (names,)
    why = []
    for src in sources:
        for name in names:
            try:
                raw = read_source(src, name, timeout)
            except Exception as e:
                why.append(f"{src}/{name}: не прочитано: {e}")
                continue
            bad = check(raw)
            if bad:
                why.append(f"{src}/{name}: {bad}")
                continue
            return raw, f"{src}/{name}", why
    return None, None, why


def prompt_check(job):
    """Проверка запроса: его хэш обязан совпасть с хэшем из задания.

    Хвостовой перевод строки пробуется ОТДЕЛЬНО, и это не догадка:
    решает хэш из цепочки, а не мы. Нужно затем, что редактор и `echo`
    добавляют его сами, и совпадающий по смыслу файл давал бы отказ.
    """
    def check(raw):
        want = job.get("prompt_hash")
        if not want:
            return "в задании нет хэша запроса: сверять нечем"
        if chain.text_hash(raw) == want:
            return None
        if raw.endswith("\n") and chain.text_hash(raw[:-1]) == want:
            return None
        return f"хэш запроса не тот: цепочка ждёт {want}"
    return check


def prompt_text(raw, job):
    """Тот из двух видов запроса, который сошёлся с цепочкой."""
    if chain.text_hash(raw) == job.get("prompt_hash"):
        return raw
    return raw[:-1]


def bundle_check(job):
    """Проверка связки: запрос И ответ обязаны сойтись с заданием."""
    def check(raw):
        try:
            b = json.loads(raw)
        except ValueError as e:
            return f"не разобрано как JSON: {e}"
        if not isinstance(b, dict) or "prompt" not in b or "answer" not in b:
            return "в связке нет запроса или ответа"
        bad = chain.job_problems(job, b["prompt"], b["answer"])
        return "; ".join(bad) if bad else None
    return check
