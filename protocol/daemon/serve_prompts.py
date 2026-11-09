#!/usr/bin/env python3
"""Раздача текстов запросов для заказчика без бота — только участникам.

Бот в `protocol/telegram` раздаёт свои запросы сам. Заказчику, который
ставит задания через `client.py submit --prompts КАТАЛОГ`, нужно то же
самое: хэш запроса лежит в цепочке открыто, и простой `http.server`
отдал бы текст любому её читателю.

Правило то же, что у бота (sign.Access.may_read): запрос получает
исполнитель взятого задания, а после сдачи ответа — действующие узлы
реестра. Ответ шифруется ключом спросившего. Задание по хэшу ищется в
списке заданий цепочки; текст закрытого задания стирается.

  python3 serve_prompts.py --dir prompts --port 8080 --rest … --contract …
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

NAME = re.compile(r"^/([0-9a-f]{64})\.txt$")
CLOSED = ("settled", "cancelled", "canceled")


class Index:
    """Задания по хэшу запроса, из списка заданий цепочки."""

    def __init__(self, rest, contract, every=3, now=time.time):
        self.rest, self.contract, self.every, self.now = (rest, contract,
                                                          every, now)
        self.by_hash, self.at, self.lock = {}, 0, threading.Lock()

    def refresh(self):
        jobs, after = {}, None
        while True:
            page, border = chain.jobs_page(self.rest, self.contract, after)
            for e in page:
                jobs[int(e["job_id"])] = e["job"]
            if not page or int(page[-1]["job_id"]) + 1 >= border:
                break
            after = page[-1]["job_id"]
        by = {}
        for jid, j in jobs.items():
            by.setdefault(j.get("prompt_hash"), []).append(j)
        self.by_hash, self.at = by, self.now()

    def jobs(self, ph):
        with self.lock:
            if self.now() - self.at > self.every:
                self.refresh()
            return list(self.by_hash.get(ph, []))


def make_handler(directory, index, access, now=time.time,
                 log=lambda s: print(s, file=sys.stderr)):
    directory = os.path.abspath(directory)

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            m = NAME.match(self.path)
            p = m and os.path.join(directory, m.group(0)[1:])
            if not p or not os.path.isfile(p):
                self.send_error(404)
                return
            try:
                who = sign.requester(self.headers, self.path, now(),
                                     access.hrp)
            except sign.Unsigned:
                self.send_error(401, "signature required")
                return
            except sign.BadSignature as e:
                log(f"отказ {self.path}: {e}")
                self.send_error(403, "bad signature")
                return
            try:
                jobs = index.jobs(m.group(1))
            except chain.ChainUnavailable:
                self.send_error(503, "chain unavailable")
                return
            why = "задания с таким запросом нет"
            for j in jobs:
                allowed, why = access.may_read("prompt", j, who)
                if allowed:
                    with open(p, "rb") as f:
                        body = sign.seal(bytes.fromhex(
                            self.headers[sign.H_KEY]), f.read())
                    self.send_response(200)
                    self.send_header("Content-Type",
                                     "application/octet-stream")
                    self.send_header(sign.H_SEALED, "1")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
            log(f"отказ {self.path}: {why}")
            self.send_error(403, "not a job participant")

        def log_message(self, *a):
            pass

    return H


def sweep(directory, index):
    """Стереть тексты, все задания которых закрыты. Сколько стёрто."""
    with index.lock:
        index.refresh()
        by = dict(index.by_hash)
    n = 0
    for name in os.listdir(directory):
        m = re.fullmatch(r"([0-9a-f]{64})\.txt", name)
        if not m or m.group(1) not in by:
            continue  # задание ещё не в цепочке — текст нужен
        if all(str(j.get("state", "")).lower() in CLOSED
               for j in by[m.group(1)]):
            try:
                os.remove(os.path.join(directory, name))
                n += 1
            except FileNotFoundError:
                pass
    return n


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dir", required=True)
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--rest", required=True)
    p.add_argument("--contract", required=True)
    p.add_argument("--sweep", type=int, default=60)
    a = p.parse_args()
    os.makedirs(a.dir, exist_ok=True)
    index = Index(a.rest, a.contract)
    access = sign.Access(a.rest, a.contract, chain)

    def loop():
        while True:
            try:
                n = sweep(a.dir, index)
                if n:
                    print(f"стёрто текстов закрытых заданий: {n}",
                          file=sys.stderr)
            except Exception as e:
                print(f"обход текстов: {e}", file=sys.stderr)
            time.sleep(a.sweep)
    threading.Thread(target=loop, daemon=True).start()
    srv = http.server.ThreadingHTTPServer(
        ("0.0.0.0", a.port), make_handler(a.dir, index, access))
    print(f"запросы раздаются на :{a.port} только участникам заданий",
          file=sys.stderr)
    srv.serve_forever()


if __name__ == "__main__":
    main()
