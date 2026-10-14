"""Вызов модели с прибитыми параметрами.

Шлюз НЕ вызывает vLLM как попало. Он берёт того же клиента, что демоны
протокола (`protocol/daemon/vllm_client.py`), и по одной причине: ответы
шлюза — это те самые ответы, расхождение которых на этапе 2 будет
сравниваться с порогом 0,0360. Порог измерен для связки
Qwen2.5-7B-Instruct / vllm-0.31.0 / bfloat16 при отключённом
префикс-кеше и жадном декодировании. Ответ, выданный при других
параметрах, проверить этим порогом нельзя — и значит этап 1, сделанный
«как проще», пришлось бы переделывать на этапе 2.

Поэтому же здесь есть проверка узла перед работой: узел, не повторяющий
сам себя, порог делает недействительным. На этапе 1 проверяющих нет и
поймать это некому, кроме самого шлюза.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

_DAEMON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "..", "..", "protocol", "daemon")
sys.path.insert(0, os.path.abspath(_DAEMON))

import vllm_client  # noqa: E402

PINNED_MODEL = "Qwen/Qwen2.5-7B-Instruct"


class NodeUnfit(Exception):
    """Узел не годен для работы."""


def count_tokens(base: str, model: str, text: str, timeout: float = 30) -> int:
    """Длина запроса в токенах — у самого сервера.

    Считать по символам нельзя: резерв вышел бы либо завышенным (клиент
    не может позволить себе запрос, который может), либо заниженным
    (ответ в долг). Токенизатор сервера даёт точное число.
    """
    url = base.rstrip("/").removesuffix("/v1") + "/tokenize"
    body = json.dumps({"model": model, "prompt": text}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return int(json.loads(r.read())["count"])


def check_node(base: str, model: str) -> None:
    """Готов ли узел. Бросает NodeUnfit, а не возвращает False.

    Молча отработать на негодном узле хуже, чем отказать: ответы такого
    узла нельзя будет проверить, а узнается это на этапе 2.
    """
    try:
        version = vllm_client.server_version(base)
    except vllm_client.VersionUnknown as e:
        raise NodeUnfit(f"версию рантайма определить не удалось: {e}") from e
    if not vllm_client.self_check(base, model):
        raise NodeUnfit(
            "узел не повторяет сам себя: префикс-кеш не отключён или "
            "включена выборка. Порог для такого узла недействителен.")
    return version


def complete(base: str, model: str, prompt: str, max_tokens: int) -> dict:
    """Ответ и действительный расход токенов."""
    answer, tokens = vllm_client.generate(base, model, prompt, max_tokens)
    prompt_tokens = count_tokens(base, model, prompt)
    return {
        "answer": answer,
        "prompt_tokens": prompt_tokens,
        "answer_tokens": len(tokens),
        "total_tokens": prompt_tokens + len(tokens),
    }
