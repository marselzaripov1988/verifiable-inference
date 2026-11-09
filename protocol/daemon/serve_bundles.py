#!/usr/bin/env python3
"""Раздача своих связок — только участникам задания.

Прежде связки раздавал `python3 -m http.server`: `<номер>.json` с
вопросом и ответом отдавался любому, кто спросит, и хранился вечно.
Номера заданий идут подряд, так что перебором с узлов скачивались все
вопросы и ответы сети.

Теперь:

  * отдаётся только `<номер>.json` и `/` (ответ «жив» для проверок);
  * несуществующий файл — 404 ДО проверки подписи: так бот по-прежнему
    отличает живой сервер от снятой машины (`bot.reachable`);
  * без подписи — 401, не участнику задания — 403 (правило в sign.py);
  * связка закрытого задания хранится `--retain` секунд после ответа,
    чтобы опоздавший заказчик успел её забрать, и затем стирается —
    и при запросе, и обходом раз в `--sweep` секунд.

Запуск (его делает start.sh):
  python3 serve_bundles.py --dir БАНДЛЫ --port 8081 --rest … --contract …
"""

import argparse
import http.server
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chain  # noqa: E402
import sign  # noqa: E402

NAME = re.compile(r"^/(\d+)\.json$")


def expired(job, retain, now):
    """Закрыто и ответ сдан дольше `retain` секунд назад."""
    if str(job.get("state", "")).lower() != "settled":
        return False
    at = job.get("answered_at") or job.get("claimed_at") or 0
    return now - int(at) > retain


def make_handler(directory, access, retain, now=time.time,
                 log=lambda s: print(s, file=sys.stderr)):
    directory = os.path.abspath(directory)

    class H(http.server.BaseHTTPRequestHandler):
        def _send(self, code, body=b"", sealed=False):
            self.send_response(code)
            self.send_header("Content-Type",
                             "application/octet-stream" if sealed
                             else "text/plain; charset=utf-8")
            if sealed:
                self.send_header(sign.H_SEALED, "1")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                self._send(200, b"")
                return
            m = NAME.match(self.path)
            p = m and os.path.join(directory, f"{int(m.group(1))}.json")
            if not p or not os.path.isfile(p):
                self.send_error(404)
                return
            try:
                who = sign.requester(self.headers, self.path, now())
            except sign.Unsigned:
                self.send_error(401, "signature required")
                return
            except sign.BadSignature as e:
                log(f"отказ {self.path}: {e}")
                self.send_error(403, "bad signature")
                return
            try:
                job = access.job(int(m.group(1)))
            except chain.ChainUnavailable:
                self.send_error(503, "chain unavailable")
                return
            if expired(job, retain, now()):
                try:
                    os.remove(p)
                except FileNotFoundError:
                    pass
                self.send_error(404)
                return
            allowed, why = access.may_read("bundle", job, who)
            if not allowed:
                log(f"отказ {self.path}: {why}")
                self.send_error(403, "not a job participant")
                return
            with open(p, "rb") as f:
                body = f.read()
            # Ключ из запроса уже проверен подписью: шифр — для того, кто
            # спросил, и ни для кого больше.
            self._send(200, sign.seal(bytes.fromhex(self.headers[sign.H_KEY]),
                                      body), sealed=True)

        def log_message(self, *a):
            pass

    return H


def sweep(directory, access, retain, now=time.time):
    """Стереть связки закрытых заданий. Возвращает, сколько стёрто."""
    n = 0
    for name in os.listdir(directory):
        m = re.fullmatch(r"(\d+)\.json", name)
        if not m:
            continue
        try:
            job = access.chain.load_job(access.rest, access.contract,
                                        int(m.group(1)))
        except chain.ChainUnavailable:
            continue
        if expired(job, retain, now()):
            try:
                os.remove(os.path.join(directory, name))
                n += 1
            except FileNotFoundError:
                pass
    return n


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dir", required=True)
    p.add_argument("--port", type=int, default=8081)
    p.add_argument("--rest", required=True)
    p.add_argument("--contract", required=True)
    p.add_argument("--retain", type=int, default=86400,
                   help="сколько секунд держать связку закрытого задания")
    p.add_argument("--sweep", type=int, default=600,
                   help="как часто обходить каталог, секунды")
    a = p.parse_args()
    os.makedirs(a.dir, exist_ok=True)
    access = sign.Access(a.rest, a.contract, chain)

    def loop():
        while True:
            try:
                n = sweep(a.dir, access, a.retain)
                if n:
                    print(f"стёрто связок закрытых заданий: {n}",
                          file=sys.stderr)
            except Exception as e:
                print(f"обход связок: {e}", file=sys.stderr)
            time.sleep(a.sweep)
    threading.Thread(target=loop, daemon=True).start()
    srv = http.server.ThreadingHTTPServer(
        ("0.0.0.0", a.port), make_handler(a.dir, access, a.retain))
    print(f"связки раздаются на :{a.port} только участникам заданий",
          file=sys.stderr)
    srv.serve_forever()


if __name__ == "__main__":
    main()
