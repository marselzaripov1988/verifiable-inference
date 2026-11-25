#!/usr/bin/env python3
"""Посредник для текстов запросов: агенту не нужен открытый порт.

Зачем. В цепочке лежит только хэш вопроса. Текст исполнитель забирает сам
по адресу `prompt_uri`, который заказчик назвал в задании. Значит,
заказчик обязан быть виден узлам снаружи. Боту на сервере это ничего не
стоит, а агенту за домашним роутером, в офисе или в облаке без белого
адреса узлы не дозвонятся, и задание никто не исполнит.

Посредник стоит там, где адрес есть (рядом с ботом и кабинетом):

  * агент выкладывает текст сюда ИСХОДЯЩИМ запросом (`PUT /<хэш>.txt`),
    подписанным своим ключом; исходящий запрос проходит из любой сети;
  * в задании `prompt_uri` указывает сюда;
  * узел берёт текст отсюда тем же подписанным `GET`, что и у бота, и
    получает его шифром для своего ключа.

Узлы не меняются: для них посредник — обычный `prompt_uri`.

Кому отдаётся текст — правило то же, что у бота и прокси
(`sign.Access.may_read`): исполнителю задания, а после сдачи ответа и
действующим узлам реестра. Задание ищется по цепочке: среди последних
заданий реестров — то, у которого этот хэш и заказчик — тот, кто текст
выложил. Чужой текст под своим заданием не раздать: заказчиком задания
должен быть тот же адрес, что подписал выкладку.

Что посредник НЕ делает:

  * не хранит деньги и ключи; он не подписывает ничего, кроме шифра
    ответа узлу (разовым ключом);
  * не держит тексты дольше нужного: невостребованный (задания с ним так
    и нет) стирается через `UNCLAIMED_TTL`, закрытый — сразу после
    вердикта, любой — через `MAX_AGE`;
  * не скрывает текст от себя самого: хозяин сервера может его прочесть,
    как может и хозяин сервера бота. Шифруется только путь до узла.

Запуск и настройка — README.md рядом.
"""

import http.server
import json
import os
import re
import sqlite3
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "daemon"))
import chain  # noqa: E402
import sign   # noqa: E402

DEFAULT_REGISTRIES = (
    "axm1fe5dsxm3vcnwjkvlgjk9mkvqnl6ggpay3y3r94hls7g3hz4s68tqcp547r",
    "axm1svd4ghyg8ku3s9al9atjxlsgclz8jgrnpa6g0day734ldzum3k6qyf8h36",
    "axm1k2v4cjqfl5mqjutnyg5g4cgtatvj835p2sen8myefy40h4pv3ttscnqtwg",
    "axm106jyr6sdutlwhpng0q9qclxfn5kz20e3np594fyqx38fyzuw0xfs0mm3fm",
)

#: `/<хэш>.txt`, можно с приставкой (`/relay/<хэш>.txt`), если посредник
#: висит на пути общего домена. Приставку прокси-сервер срезать НЕ должен:
#: узел подписывает полный путь, и подпись иначе не сойдётся.
NAME = re.compile(r"^(?:/[A-Za-z0-9._-]+)*/([0-9a-f]{64})\.txt$")
#: Задание ещё может потребовать текст: его исполняют или проверяют.
LIVE_STATES = ("open", "claimed", "answered")


class Config:
    def __init__(self, env=os.environ):
        self.rest = env.get("REST", "https://axm-lcd.trickle.pro")
        regs = env.get("REGISTRIES", "").strip()
        self.registries = tuple(r.strip() for r in regs.split(",")
                                if r.strip()) or DEFAULT_REGISTRIES
        self.dir = env.get("DIR", "./relay-prompts")
        self.db = env.get("DB", "./relay.sqlite")
        self.host = env.get("HOST", "127.0.0.1")
        self.port = int(env.get("PORT", "8097"))
        #: Предел текста. У прокси по умолчанию 8000 знаков; кириллица —
        #: два байта на знак, плюс шаблон диалога.
        self.max_bytes = int(env.get("MAX_BYTES", "65536"))
        #: Сколько текстов без задания может держать один адрес.
        self.max_pending = int(env.get("MAX_PENDING", "20"))
        #: Через сколько стирать текст, под который так и не появилось
        #: задание. Прокси выкладывает текст за секунды до сделки.
        self.unclaimed_ttl = int(env.get("UNCLAIMED_TTL", "3600"))
        #: Предел жизни любого текста: задание столько не живёт.
        self.max_age = int(env.get("MAX_AGE", str(3 * 86400)))
        #: Сколько последних заданий каждого реестра просматривать в поиске.
        self.scan = int(env.get("SCAN", "200"))


class Refused(Exception):
    def __init__(self, code, text):
        super().__init__(text)
        self.code = code


class Relay:
    def __init__(self, cfg, chainmod=chain, now=time.time, access=None):
        self.cfg, self.chain, self.now = cfg, chainmod, now
        os.makedirs(cfg.dir, exist_ok=True)
        self.db = sqlite3.connect(cfg.db, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS uploads (
                hash TEXT, owner TEXT, created REAL,
                PRIMARY KEY (hash, owner));
            CREATE TABLE IF NOT EXISTS jobs (
                hash TEXT, contract TEXT, job_id INTEGER, client TEXT,
                PRIMARY KEY (contract, job_id));
            CREATE TABLE IF NOT EXISTS scans (
                hash TEXT PRIMARY KEY, at REAL);
        """)
        self.access = access or {
            c: sign.Access(cfg.rest, c, chainmod, now=now)
            for c in cfg.registries}

    def q(self, sql, *a):
        with self.lock:
            cur = self.db.execute(sql, a)
            rows = cur.fetchall()
            self.db.commit()
            return rows

    def path(self, h):
        return os.path.join(self.cfg.dir, f"{h}.txt")

    # --- выкладка -----------------------------------------------------------

    def upload(self, h, body, owner):
        if len(body) > self.cfg.max_bytes:
            raise Refused(413, f"текст больше {self.cfg.max_bytes} байт")
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            raise Refused(400, "текст не в UTF-8")
        if self.chain.text_hash(text) != h:
            raise Refused(400, "хэш текста не совпал с именем")
        pending = self.q(
            "SELECT COUNT(*) FROM uploads u WHERE owner=? AND NOT EXISTS "
            "(SELECT 1 FROM jobs j WHERE j.hash=u.hash AND j.client=u.owner)",
            owner)[0][0]
        known = self.q("SELECT 1 FROM uploads WHERE hash=? AND owner=?",
                       h, owner)
        if not known and pending >= self.cfg.max_pending:
            raise Refused(429, f"у {owner} уже {pending} текстов без "
                               f"задания: дождитесь заданий или срока")
        tmp = self.path(h) + f".{os.getpid()}.{threading.get_ident()}"
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, self.path(h))
        self.q("INSERT OR REPLACE INTO uploads VALUES (?,?,?)",
               h, owner, self.now())

    # --- поиск задания ------------------------------------------------------

    def _scan(self, h):
        """Найти в последних заданиях реестров задания с этим хэшем от
        тех, кто его выложил. Не чаще раза в 5 с на хэш: каждый узел
        спрашивает текст несколько раз подряд."""
        last = self.q("SELECT at FROM scans WHERE hash=?", h)
        if last and self.now() - last[0][0] < 5:
            return
        self.q("INSERT OR REPLACE INTO scans VALUES (?,?)", h, self.now())
        owners = {r[0] for r in self.q(
            "SELECT owner FROM uploads WHERE hash=?", h)}
        for c in self.cfg.registries:
            try:
                _, end = self.chain.jobs_page(self.cfg.rest, c, limit=1)
                start = max(0, end - self.cfg.scan) - 1
                while True:
                    page, end = self.chain.jobs_page(
                        self.cfg.rest, c,
                        start_after=start if start >= 0 else None, limit=50)
                    for item in page:
                        job = item.get("job") or {}
                        if job.get("prompt_hash") == h and \
                                job.get("client") in owners:
                            self.q("INSERT OR REPLACE INTO jobs VALUES "
                                   "(?,?,?,?)", h, c, int(item["job_id"]),
                                   job["client"])
                    if not page or len(page) < 50:
                        break
                    start = int(page[-1]["job_id"])
            except chain.ChainUnavailable:
                continue

    def jobs_for(self, h, rescan=False):
        rows = self.q("SELECT contract, job_id FROM jobs WHERE hash=?", h)
        if not rows or rescan:
            self._scan(h)
            rows = self.q("SELECT contract, job_id FROM jobs WHERE hash=?",
                          h)
        return rows

    def _may_read(self, c, job_id, who):
        """None — читать можно, иначе (код, причина)."""
        acc = self.access[c]
        for fresh in (False, True):
            if fresh:
                # Узел мог взять задание секунду назад, а в кеше ещё `open`:
                # перечитать задание из цепочки один раз.
                acc.cache.pop(("job", int(job_id)), None)
            try:
                job = acc.job(job_id)
            except chain.ChainUnavailable:
                return 503, "chain unavailable"
            if acc.may_read("prompt", job, who)[0]:
                return None
        return 403, "not a job participant"

    def authorize(self, headers, path, h):
        """None — отдать, иначе (код, причина). Правило бота и прокси."""
        try:
            who = sign.requester(headers, path, self.now())
        except sign.Unsigned:
            return 401, "signature required"
        except sign.BadSignature:
            return 403, "bad signature"
        seen = set()
        for attempt in range(2):
            rows = [r for r in self.jobs_for(h, rescan=attempt > 0)
                    if r not in seen]
            if not rows and not seen:
                if attempt:
                    return 404, "no job with this prompt yet"
                continue
            for c, job_id in rows:
                seen.add((c, job_id))
                denied = self._may_read(c, job_id, who)
                if denied is None:
                    return None
                if denied[0] == 503:
                    return denied
        # Тот же вопрос мог прийти ещё раз новым заданием: поиск выше
        # повторён один раз (не чаще раза в 5 с на хэш).
        return 403, "not a job participant"

    # --- уборка -------------------------------------------------------------

    def sweep(self):
        """Стереть то, что больше не понадобится. Возвращает число стёртых."""
        now = self.now()
        gone = 0
        for (h,) in self.q("SELECT DISTINCT hash FROM uploads"):
            first = self.q("SELECT MIN(created) FROM uploads WHERE hash=?",
                           h)[0][0]
            jobs = self.q("SELECT contract, job_id FROM jobs WHERE hash=?",
                          h)
            drop = now - first > self.cfg.max_age
            if not jobs and now - first > self.cfg.unclaimed_ttl:
                drop = True
            if jobs and not drop:
                live = False
                for c, job_id in jobs:
                    try:
                        st = str(self.chain.load_job(
                            self.cfg.rest, c, job_id).get("state", "")).lower()
                    except chain.ChainUnavailable:
                        live = True       # не знаем — не трогаем
                        break
                    except Exception:
                        continue
                    if st in LIVE_STATES:
                        live = True
                        break
                drop = not live
            if drop:
                try:
                    os.remove(self.path(h))
                except FileNotFoundError:
                    pass
                self.q("DELETE FROM uploads WHERE hash=?", h)
                self.q("DELETE FROM jobs WHERE hash=?", h)
                self.q("DELETE FROM scans WHERE hash=?", h)
                gone += 1
        return gone


def make_handler(relay):
    class H(http.server.BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json; charset=utf-8",
                  extra=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _path(self):
            return self.path.split("?", 1)[0]

        def do_PUT(self):
            p = self._path()
            m = NAME.match(p)
            if not m:
                return self._send(404, {"error": "нужен PUT /<хэш>.txt"})
            try:
                who = sign.requester(self.headers, p, relay.now())
            except sign.Unsigned:
                return self._send(401, {"error": "нужна подпись (X-VI-*)"})
            except sign.BadSignature as e:
                return self._send(403, {"error": str(e)})
            n = int(self.headers.get("Content-Length") or 0)
            if n > relay.cfg.max_bytes:
                return self._send(413, {"error": "слишком длинный текст"})
            body = self.rfile.read(n)
            try:
                relay.upload(m.group(1), body, who)
            except Refused as e:
                return self._send(e.code, {"error": str(e)})
            self._send(201, {"ok": True, "hash": m.group(1), "owner": who})

        def do_GET(self):
            p = self._path()
            if p == "/healthz":
                n = relay.q("SELECT COUNT(DISTINCT hash) FROM uploads")[0][0]
                return self._send(200, {"ok": True, "prompts": n,
                                        "registries": relay.cfg.registries})
            m = NAME.match(p)
            if not m or not os.path.isfile(relay.path(m.group(1))):
                return self._send(404, {"error": "нет такого текста"})
            denied = relay.authorize(self.headers, p, m.group(1))
            if denied:
                return self._send(denied[0], {"error": denied[1]})
            with open(relay.path(m.group(1)), "rb") as f:
                body = f.read()
            # Подпись проверена: ключ — того, кто спросил.
            body = sign.seal(bytes.fromhex(self.headers[sign.H_KEY]), body)
            self._send(200, body, "application/octet-stream",
                       {sign.H_SEALED: "1"})

        def log_message(self, fmt, *a):
            print(f"{self.address_string()} {fmt % a}", file=sys.stderr)

    return H


def main():
    cfg = Config()
    relay = Relay(cfg)

    def sweeper():
        while True:
            time.sleep(300)
            try:
                n = relay.sweep()
                if n:
                    print(f"стёрто текстов: {n}", file=sys.stderr)
            except Exception as e:
                print(f"уборка не прошла: {e}", file=sys.stderr)

    print(f"стёрто при запуске: {relay.sweep()}", file=sys.stderr)
    threading.Thread(target=sweeper, daemon=True).start()
    srv = http.server.ThreadingHTTPServer((cfg.host, cfg.port),
                                          make_handler(relay))
    print(f"посредник: http://{cfg.host}:{cfg.port}/, реестры: "
          f"{', '.join(r[:16] + '…' for r in cfg.registries)}",
          file=sys.stderr)
    srv.serve_forever()


if __name__ == "__main__":
    main()
