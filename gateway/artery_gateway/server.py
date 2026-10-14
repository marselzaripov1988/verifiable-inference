"""Шлюз: приём запроса, оплата, отдача ответа. Шаг 5 плана.

Что здесь есть и чего нет. Проверки ответов нет вовсе — карты свои, и
проверять себя бессмысленно. Это этап 1: он отвечает на единственный
вопрос, который сейчас имеет значение, — платит ли кто-нибудь. Протокол
(контракт, демоны, порог, проба) к этому не подключён и не нужен.

Что НЕ сделано «как проще», хотя соблазн был:

  * параметры модели прибиты теми, при которых измерен порог. Иначе
    ответы шлюза нельзя будет проверить на этапе 2, и этап 1 пришлось бы
    переделывать;
  * платёж опознаётся по памятке, а не по адресу отправителя. Адрес
    публичен, и по адресу любой тратил бы чужой остаток;
  * перед работой резервируется максимум, после — списывается
    использованное. Списание после ответа означало бы ответы в долг.

HTTP на stdlib: шлюз должен запускаться на арендованной машине без
установки зависимостей, рядом с vLLM.

    python gateway/scripts/serve.py --treasury artr1... --price 1000000
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import inference
from .chain import ArteryRPC
from .ledger import (
    InsufficientFunds,
    Ledger,
    NoFreeAddress,
    UnknownAccount,
)
from .pricing import Pricing

MAX_BODY = 256 * 1024


class Gateway:
    """Состояние шлюза: учёт, цены, узел, казна."""

    def __init__(self, ledger: Ledger, pricing: Pricing, treasury: str,
                 rpc: ArteryRPC, base_url: str,
                 model: str = inference.PINNED_MODEL,
                 per_account: bool = False):
        self.ledger = ledger
        self.pricing = pricing
        self.treasury = treasury
        self.rpc = rpc
        self.base_url = base_url
        self.model = model
        #: Выдавать ли каждому счёту свой адрес приёма. С ним памятка не
        #: нужна, и кошелёк, который её не умеет, перестаёт мешать.
        self.per_account = per_account
        #: Минимальная единица сети — из приёмника, а не из настроек:
        #: иначе подсказка об оплате и зачисление могли бы разойтись.
        self.denom = rpc.denom
        self.node_version: str | None = None
        self.last_poll: float = 0.0
        self.poll_error: str | None = None

    # --- платежи ----------------------------------------------------------

    def poll_payments(self) -> int:
        """Прочитать новые платежи. Возвращает число зачисленных.

        Высота двигается даже для неопознанных платежей, иначе шлюз
        перечитывал бы их вечно. Ошибку чтения он ЗАПОМИНАЕТ: молчащий
        приёмник выглядит как «платежей нет», а это ровно та картина,
        которой нельзя доверять.
        """
        try:
            n = 0
            # Опрос по диапазону высот, когда адресов больше одного: один
            # запрос за цикл независимо от числа счетов. По индексу на
            # каждый адрес их было бы по запросу на счёт.
            watched = {self.treasury} | set(self.ledger.by_deposit)
            if len(watched) > 1:
                found = self.rpc.scan_range(watched, self.ledger.height + 1)
            else:
                found = self.rpc.scan_payments(self.treasury,
                                               self.ledger.height + 1)
            for p in found:
                if self.ledger.credit(p.tx_hash, p.height, p.sender,
                                      p.amount_micro, p.memo, p.recipient):
                    n += 1
            self.poll_error = None
            self.last_poll = time.time()
            return n
        except Exception as e:  # noqa: BLE001 — причина попадёт в /healthz
            self.poll_error = f"{type(e).__name__}: {e}"
            raise

    def watch(self, every: float = 10.0, stop: threading.Event | None = None):
        """Опрос в отдельном потоке."""
        while not (stop and stop.is_set()):
            try:
                self.poll_payments()
            except Exception:
                traceback.print_exc()
            time.sleep(every)

    # --- работа -----------------------------------------------------------

    def complete(self, key: str, prompt: str, max_tokens: int) -> dict:
        """Оплаченный запрос к модели."""
        if not prompt:
            raise ValueError("запрос пустой")
        if max_tokens <= 0 or max_tokens > self.pricing.max_answer_tokens:
            raise ValueError(
                f"max_tokens от 1 до {self.pricing.max_answer_tokens}")

        self.ledger.account(key)  # есть ли счёт — до всякой работы
        prompt_tokens = inference.count_tokens(self.base_url, self.model, prompt)
        if prompt_tokens > self.pricing.max_prompt_tokens:
            raise ValueError(
                f"запрос {prompt_tokens} токенов, предел "
                f"{self.pricing.max_prompt_tokens}")

        reserved = self.pricing.reserve_for(prompt_tokens, max_tokens)
        self.ledger.reserve(key, reserved)
        try:
            r = inference.complete(self.base_url, self.model, prompt, max_tokens)
        except BaseException:
            # Работа не состоялась — резерв снимается без списания.
            self.ledger.release(key, reserved)
            raise
        charge = self.pricing.cost(r["total_tokens"])
        self.ledger.settle(key, reserved, charge, r["total_tokens"])
        a = self.ledger.account(key)
        return {
            "answer": r["answer"],
            "usage": {
                "prompt_tokens": r["prompt_tokens"],
                "answer_tokens": r["answer_tokens"],
                "total_tokens": r["total_tokens"],
            },
            "charged_micro": charge,
            "reserved_micro": reserved,
            "balance_micro": a.balance,
        }


def make_handler(gw: Gateway):
    class Handler(BaseHTTPRequestHandler):
        server_version = "artery-gateway"

        def log_message(self, fmt, *args):
            print(f"{self.address_string()} {fmt % args}")

        # --- помощники ---
        def _send(self, code: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            if n > MAX_BODY:
                raise ValueError("тело запроса слишком велико")
            return json.loads(self.rfile.read(n))

        def _key(self) -> str:
            k = self.headers.get("X-API-Key") or ""
            if not k:
                raise UnknownAccount("нужен заголовок X-API-Key")
            return k

        # --- маршруты ---
        def do_GET(self):
            try:
                if self.path == "/healthz":
                    return self._send(200, {
                        "treasury": gw.treasury,
                        "height": gw.ledger.height,
                        "node_version": gw.node_version,
                        "last_poll": gw.last_poll,
                        "poll_error": gw.poll_error,
                        "accounts": len(gw.ledger.accounts),
                        "free_addresses": len(gw.ledger.free),
                        "watched_addresses": 1 + len(gw.ledger.by_deposit),
                        "unclaimed": len(gw.ledger.unclaimed),
                    })
                if self.path == "/v1/account":
                    a = gw.ledger.account(self._key())
                    return self._send(200, {
                        "code": a.code, "deposit": a.deposit,
                        "balance_micro": a.balance,
                        "reserved_micro": a.reserved, "spent_micro": a.spent,
                        "tokens": a.tokens,
                    })
                return self._send(404, {"error": "нет такого пути"})
            except UnknownAccount as e:
                return self._send(401, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})

        def do_POST(self):
            try:
                if self.path == "/v1/account":
                    a = gw.ledger.open_account(need_deposit=gw.per_account)
                    how = (f"перевод на {a.deposit} в {gw.denom}; "
                           f"памятка не нужна — адрес ваш" if a.deposit else
                           f"перевод на {gw.treasury} в {gw.denom}, "
                           f"в памятке укажите {a.code}")
                    return self._send(201, {
                        "api_key": a.key,
                        "code": a.code,
                        "deposit": a.deposit or gw.treasury,
                        "treasury": gw.treasury,
                        "price_micro_per_million":
                            gw.pricing.price_micro_per_million,
                        "как_платить": how,
                    })
                if self.path == "/v1/completions":
                    b = self._body()
                    r = gw.complete(self._key(), b.get("prompt") or "",
                                    int(b.get("max_tokens") or 256))
                    return self._send(200, r)
                return self._send(404, {"error": "нет такого пути"})
            except UnknownAccount as e:
                return self._send(401, {"error": str(e)})
            except NoFreeAddress as e:
                # 503, а не 500: это нехватка запаса, а не поломка.
                return self._send(503, {"error": str(e)})
            except InsufficientFunds as e:
                return self._send(402, {
                    "error": str(e), "have_micro": e.have, "need_micro": e.need,
                })
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except inference.NodeUnfit as e:
                return self._send(503, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    return Handler


def serve(gw: Gateway, host: str = "127.0.0.1", port: int = 8080,
          poll_every: float = 10.0) -> ThreadingHTTPServer:
    """Поднять сервис и приёмник платежей."""
    srv = ThreadingHTTPServer((host, port), make_handler(gw))
    stop = threading.Event()
    t = threading.Thread(target=gw.watch, args=(poll_every, stop), daemon=True)
    t.start()
    srv._stop_watch = stop  # type: ignore[attr-defined]
    return srv
