#!/usr/bin/env python3
"""Подставной vLLM для прогона ПЕТЛИ на местном девнете.

Чего он НЕ доказывает и доказывать не может: ни детерминизма инференса,
ни порога, ни пробы. Это замерено на настоящих картах и лежит в
`research/inference-determinism/`.

Что он даёт: возможность прогнать петлю на НАСТОЯЩЕЙ цепочке без GPU.
Проверяется ровно то, чего нет в подставных тестах, — что узлы сами
находят задание, берут его, сдают ответ, обмениваются связками, сдают
проверки и сводят вердикт, и что цепочка всё это принимает.

Ответ и вероятности — чистые функции ТЕКСТА. Поэтому три узла, глядя на
один и тот же текст, выдают один и тот же вектор, как и обязаны узлы
одного класса, и вердикт получается `guilty=false`. Это и есть свойство
класса, вокруг которого построено правило; настоящие карты дают его
побитово, здесь оно получается арифметикой.

Маршруты — те, которыми пользуется daemon/vllm_client.py:

    GET  /version          — версия, иначе демон откажется работать
    POST /v1/completions   — порождение (max_tokens>0) и оценка (echo)
"""

import hashlib
import http.server
import json
import sys

VERSION = "0.31.0"

#: Длина «токена» в символах. Разбивка одинаковая у всех узлов — иначе
#: векторы были бы разной длины и сравнивать их было бы нельзя.
TOKEN = 4


def tokens(text):
    return [text[i:i + TOKEN] for i in range(0, len(text), TOKEN)] or [""]


def logprobs(text):
    """Вероятности как функция текста и только текста.

    Каждое значение зависит от номера токена и его содержимого, поэтому
    общий префикс у двух текстов даёт общее начало вектора — так же, как
    у настоящей модели.
    """
    out = []
    for i, t in enumerate(tokens(text)):
        d = hashlib.sha256(f"{i}:{t}".encode()).digest()
        out.append(-(int.from_bytes(d[:4], "big") % 2_000_000) / 1e6)
    return out


def answer_for(prompt, max_tokens):
    """Ответ как функция запроса. Длинный: короткие ответы протоколу
    неинтересны, а вектор должен быть заметной длины."""
    h = hashlib.sha256(prompt.encode()).hexdigest()
    body = " ".join(f"{h[i:i + 8]}" for i in range(0, 64, 8))
    text = ""
    while len(text) < max_tokens * TOKEN:
        text += f" {body}"
    return text[:max_tokens * TOKEN]


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") == "/version":
            return self._json(200, {"version": VERSION})
        if self.path.startswith("/v1/models"):
            return self._json(200, {"data": [{"id": "подставной"}]})
        self._json(404, {"error": self.path})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        if not self.path.startswith("/v1/completions"):
            return self._json(404, {"error": self.path})

        prompt = req.get("prompt") or ""
        max_tokens = int(req.get("max_tokens") or 0)
        if max_tokens == 0 and req.get("echo"):
            # Оценка готового текста: один проход, вероятности на всю
            # длину. Именно так проверяющий и работает.
            lp = logprobs(prompt)
            return self._json(200, {"choices": [{
                "text": "", "logprobs": {"tokens": tokens(prompt),
                                         "token_logprobs": lp}}]})
        text = answer_for(prompt, max_tokens or 120)
        return self._json(200, {"choices": [{
            "text": text,
            "logprobs": {"tokens": tokens(text),
                         "token_logprobs": logprobs(text)}}]})


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"подставной vLLM {VERSION} на 127.0.0.1:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
