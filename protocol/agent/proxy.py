#!/usr/bin/env python3
"""Прокси для автономного агента: OpenAI-совместимый API, оплата в AXM.

Агент говорит с прокси как с любым OpenAI-совместимым сервером
(`/v1/chat/completions`), а прокси превращает каждый запрос в задание
реестра и **платит кошельком самого агента**: `submit_job` с монетой
`uaxm` уходит в контракт от его ключа. Посредника с деньгами нет — ни
счёта у шлюза, ни баланса в базе: деньги лежат на адресе агента и
уходят прямо в реестр, а возвраты (снятое, непроверенное, негодное)
контракт шлёт туда же.

Чем это отличается от Telegram-бота (`../telegram/bot.py`), с которым у
прокси общая механика:

  * бот платит СВОИМ кошельком за людей — прокси стоит рядом с агентом
    и платит ЕГО кошельком; один прокси — один агент — один ключ;
  * вместо чата — HTTP, который понимает любой клиент OpenAI;
  * вместо дневного лимита на человека — потолок трат в сутки: агент
    автономен, и без потолка ошибка в его цикле опустошит кошелёк.

Что сохранено от бота без изменений, потому что это и есть протокол:

  * текст выкладывается ДО сделки и раздаётся узлам по `PROMPT_URI`
    только участникам задания (подпись запроса, ответ шифром);
  * ответ берётся у исполнителя и сверяется с обоими хэшами задания —
    тем же `client.fetch_answer`; прокси курьер, а не источник доверия;
  * «ответ получен» и «ответ подтверждён» не склеиваются: в режиме
    `settled` (по умолчанию) агент получает ответ только с вердиктом
    «подтверждён», а негодный и непроверенный не получает вовсе.

Запуск и настройка — README.md рядом.
"""

import http.server
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "daemon"))
sys.path.insert(0, os.path.join(HERE, "..", "telegram"))
import chain     # noqa: E402
import client    # noqa: E402
import delivery  # noqa: E402
import sign      # noqa: E402
import tx        # noqa: E402
import wallet    # noqa: E402
# Раздача текстов узлам и шаблоны диалога — те же, что у бота: две копии
# одного правила допуска разошлись бы, и одна из них отдавала бы лишнее.
from bot import CHAT_TEMPLATES, serve_prompts, template_for  # noqa: E402

#: Агенты платят в AXM. Реестр с другим расчётом прокси не обслуживает:
#: потолок трат и цена заданы в uaxm, и молча платить чем-то другим
#: значило бы тратить не те деньги.
DENOM = "uaxm"

#: Чем Qwen кончает реплику. Узел продолжает текст, и маркер конца
#: реплики может остаться в ответе — агенту он не нужен.
STOP_MARKERS = ("<|im_end|>", "<|endoftext|>")


# --- настройки ------------------------------------------------------------

class Config:
    """Всё из окружения. Без реестра, ключа и адреса раздачи — отказ при
    запуске, а не при первом запросе агента."""

    def __init__(self, env=os.environ):
        def need(k):
            v = env.get(k, "").strip()
            if not v:
                raise SystemExit(f"ОТКАЗ: не задан {k} (см. README.md)")
            return v
        self.contract = need("CONTRACT")
        #: Файл с ключом агента (64 hex, `keytool.py`): подпись сделок,
        #: подпись запросов к узлам и расшифровка ответов.
        self.sign_key = need("SIGN_KEY")
        #: Чем подписывать сделки: `python` — сам прокси, ключом SIGN_KEY,
        #: через LCD (ничего ставить не нужно); `axmd` — утилитой цепочки,
        #: ключом KEY из её связки.
        self.signer = env.get("SIGNER", "python")
        if self.signer not in SIGNERS:
            raise SystemExit(f"SIGNER: одно из {', '.join(SIGNERS)}, а не "
                             f"{self.signer!r}")
        #: Имя ключа в связке axmd. Нужно только при SIGNER=axmd и должно
        #: давать тот же адрес, что SIGN_KEY.
        self.key = need("KEY") if self.signer == "axmd" else env.get("KEY")
        #: Посредник для текстов (`protocol/relay`): прокси выкладывает
        #: текст туда исходящим запросом, и узлы берут его там. Тогда
        #: агенту не нужен ни белый адрес, ни открытый порт.
        self.relay = env.get("RELAY", "").strip().rstrip("/") or None
        #: Адрес, по которому УЗЛЫ возьмут текст запроса. Снаружи, а не
        #: localhost: исполнитель стоит на чужой машине. С посредником —
        #: адрес посредника.
        self.prompt_uri = (self.relay if self.relay and not
                           env.get("PROMPT_URI", "").strip()
                           else need("PROMPT_URI").rstrip("/"))
        self.prompt_port = int(env.get("PROMPT_PORT", "8090"))
        self.prompts = env.get("PROMPTS_DIR", "./prompts")
        #: API для агента. По умолчанию только с этой машины: кто достучится
        #: до порта, тратит деньги агента.
        self.api_host = env.get("API_HOST", "127.0.0.1")
        self.api_port = int(env.get("API_PORT", "8088"))
        self.api_token = env.get("API_TOKEN", "").strip() or None
        if self.api_host not in ("127.0.0.1", "localhost", "::1") \
                and not self.api_token:
            raise SystemExit("ОТКАЗ: API открыт наружу (API_HOST="
                             f"{self.api_host}) без API_TOKEN — любой, кто "
                             "достучится, потратит кошелёк агента")
        self.rest = env.get("REST", "https://axm-lcd.trickle.pro")
        self.node = env.get("NODE", "https://api-chain.axiomechain.org")
        self.chain_id = env.get("CHAIN_ID", "axiome-1")
        self.gas_prices = env.get("GAS_PRICES", "0.0025uaxm")
        self.home = env.get("AXMD_HOME") or None
        self.keyring = env.get("KEYRING_BACKEND", "test")
        #: Цена задания узлу, uaxm. Доля проверяющих — сверху, по цепочке.
        self.price = int(env.get("PRICE", "10000"))
        #: Потолок трат в сутки (UTC), uaxm: цена + доля, без газа.
        self.max_spend = int(env.get("MAX_SPEND_DAY", "1000000"))
        self.max_chars = int(env.get("MAX_CHARS", "8000"))
        #: Сколько ждать, пока задание возьмут; потом снять и вернуть деньги.
        self.open_timeout = int(env.get("OPEN_TIMEOUT", "900"))
        #: Сколько держать HTTP-запрос агента. Дальше — 504 с номером
        #: задания: задание живёт, ответ забирается `GET /v1/jobs/<номер>`.
        self.wait = int(env.get("WAIT_TIMEOUT", "1800"))
        self.poll = float(env.get("POLL", "5"))
        #: settled — отдавать ответ только с вердиктом «подтверждён»;
        #: answer — как только узел сдал ответ и он сошёлся с хэшами, с
        #: пометкой verified=false. Агент может выбрать на запрос.
        self.mode = env.get("MODE", "settled")
        if self.mode not in MODES:
            raise SystemExit(f"MODE: одно из {', '.join(MODES)}, а не "
                             f"{self.mode!r}")
        self.template = env.get("CHAT_TEMPLATE", "auto")
        if self.template != "auto" and self.template not in CHAT_TEMPLATES:
            raise SystemExit(f"CHAT_TEMPLATE: одно из auto, "
                             f"{', '.join(CHAT_TEMPLATES)}, а не "
                             f"{self.template!r}")
        self.db = env.get("DB", "./agent.sqlite")


MODES = ("settled", "answer")
SIGNERS = ("python", "axmd")


# --- хранилище ------------------------------------------------------------

class Store:
    """Задания агента. Не баланс: деньги лежат на кошельке, а здесь —
    что поставлено, что с ним и сколько потрачено за сутки."""

    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.q("""CREATE TABLE IF NOT EXISTS jobs(
            job_id INTEGER PRIMARY KEY, prompt_hash TEXT, created REAL,
            day TEXT, total INTEGER, txhash TEXT, state TEXT,
            done INTEGER DEFAULT 0, guilty TEXT, executor TEXT,
            answer TEXT)""")

    def q(self, sql, *args):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.fetchall()

    def spent(self, day):
        """Потрачено за сутки — по поставленным, вместе с возвращёнными.

        Возвраты не вычитаются нарочно: потолок защищает от сбоя в цикле
        агента, а сбой как раз и даёт поток снятых заданий."""
        r = self.q("SELECT COALESCE(SUM(total),0) FROM jobs WHERE day=?", day)
        return int(r[0][0])


# --- текст запроса --------------------------------------------------------

class BadRequest(Exception):
    pass


def render_chat(messages, template):
    """Диалог OpenAI → текст, который продолжит узел.

    Узел не ведёт диалог, а продолжает текст (`/v1/completions`), поэтому
    диалог собирается здесь в шаблон модели. Шаблон один на реестр:
    его задаёт модель, прибитая в цепочке. Хэш задания — от этого текста,
    и узлы с проверяющими считают ровно его.
    """
    if not isinstance(messages, list) or not messages:
        raise BadRequest("messages: нужен непустой список")
    if template == CHAT_TEMPLATES["none"]:
        parts = []
        for m in messages:
            parts.append(_content(m))
        return "\n\n".join(parts)
    out = []
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else None
        if role not in ("system", "user", "assistant"):
            raise BadRequest(f"роль {role!r} не поддерживается: только "
                             f"system, user, assistant")
        out.append(f"<|im_start|>{role}\n{_content(m)}<|im_end|>\n")
    if messages[-1].get("role") == "assistant":
        raise BadRequest("последняя реплика должна быть не assistant")
    # Хвост шаблона — то, с чего начинается ответ модели: у qwen3 там ещё
    # пустой блок рассуждений.
    tail = template.split("{q}<|im_end|>\n", 1)[1]
    return "".join(out) + tail


def _content(m):
    c = m.get("content") if isinstance(m, dict) else None
    if isinstance(c, list):
        # Формат частей OpenAI: берётся только текст. Картинок модель
        # реестра не понимает, и отказ честнее молчаливого пропуска.
        texts = []
        for part in c:
            if not isinstance(part, dict) or part.get("type") != "text":
                raise BadRequest("поддерживаются только текстовые части")
            texts.append(str(part.get("text", "")))
        c = "".join(texts)
    if not isinstance(c, str):
        raise BadRequest("content: нужна строка")
    return c


def clean_answer(text):
    for s in STOP_MARKERS:
        i = text.find(s)
        if i >= 0:
            text = text[:i]
    return text.strip()


# --- работа с реестром ----------------------------------------------------

class Refused(Exception):
    """Запрос не поставлен — деньги не тронуты. `code` — HTTP-код."""

    def __init__(self, code, text):
        super().__init__(text)
        self.code = code


class Proxy:
    def __init__(self, cfg, store, sender, chainmod=chain,
                 fetch=client.fetch_answer, sources=client.answer_sources,
                 now=time.time, sleep=time.sleep, upload=None):
        self.cfg, self.store, self.sender, self.chain = (cfg, store, sender,
                                                         chainmod)
        self.fetch, self.sources, self.now, self.sleep = (fetch, sources,
                                                          now, sleep)
        #: Выкладка текста посреднику: `upload(text)`. None — посредника
        #: нет, текст раздаёт сам прокси.
        self.upload = upload
        #: Одна сделка за раз: у кошелька один номер последовательности.
        #: Под тем же замком — проверка потолка, иначе два запроса разом
        #: оба прошли бы под ним и вместе его превысили.
        self.tx_lock = threading.Lock()
        self._params, self._params_at = None, 0
        #: Хэши, выложенные до сделки: между файлом и строкой в базе
        #: проходит сделка, и раздавать текст в это время уже нужно.
        self.pending = set()
        self.access = sign.Access(cfg.rest, cfg.contract, chainmod, now=now)

    def params(self):
        if not self._params or self.now() - self._params_at > 300:
            p = self.chain.load_params(self.cfg.rest, self.cfg.contract)
            if p.settlement != {"native": DENOM}:
                raise Refused(503, f"реестр берёт оплату в "
                                   f"{chain.settlement_label(p.settlement)},"
                                   f" а агент платит в {DENOM}")
            self._params, self._params_at = p, self.now()
        return self._params

    def day(self):
        return time.strftime("%Y-%m-%d", time.gmtime(self.now()))

    def template(self):
        return template_for(self.cfg.template, self.params().pinned_model)

    # --- раздача текста узлам ---------------------------------------------

    def active_hashes(self):
        rows = self.store.q("SELECT prompt_hash FROM jobs WHERE done=0")
        return {r[0] for r in rows} | set(self.pending)

    def authorize(self, headers, path, ph):
        """Кому отдавать текст: None — отдать, иначе (код, причина)."""
        try:
            who = sign.requester(headers, path, self.now(), self.access.hrp)
        except sign.Unsigned:
            return 401, "signature required"
        except sign.BadSignature:
            return 403, "bad signature"
        for (job_id,) in self.store.q("SELECT job_id FROM jobs WHERE "
                                      "prompt_hash=? AND done=0", ph):
            try:
                job = self.access.job(job_id)
            except chain.ChainUnavailable:
                return 503, "chain unavailable"
            if self.access.may_read("prompt", job, who)[0]:
                return None
        return 403, "not a job participant"

    def _path(self, ph):
        return os.path.join(self.cfg.prompts, f"{ph}.txt")

    def forget(self, job_id):
        """Задание закрыто: стереть текст, если он не нужен другому."""
        rows = self.store.q("SELECT prompt_hash FROM jobs WHERE job_id=?",
                            job_id)
        ph = rows[0][0] if rows else None
        if ph and ph not in self.active_hashes():
            try:
                os.remove(self._path(ph))
            except FileNotFoundError:
                pass

    def purge(self):
        """При запуске: стереть тексты, не нужные заданиям в работе."""
        keep = self.active_hashes()
        n = 0
        if os.path.isdir(self.cfg.prompts):
            for name in os.listdir(self.cfg.prompts):
                m = re.fullmatch(r"([0-9a-f]{64})\.txt", name)
                if m and m.group(1) not in keep:
                    os.remove(os.path.join(self.cfg.prompts, name))
                    n += 1
        return n

    # --- постановка -------------------------------------------------------

    def submit(self, prompt):
        """Поставить задание. (номер, сделка, сколько заплачено)."""
        if not prompt.strip():
            raise Refused(400, "запрос пустой")
        if len(prompt) > self.cfg.max_chars:
            raise Refused(400, f"запрос длиннее {self.cfg.max_chars} "
                               f"символов")
        p = self.params()
        _, _, total = client.totals(p, self.cfg.price)
        ph = chain.text_hash(prompt)
        os.makedirs(self.cfg.prompts, exist_ok=True)
        with self.tx_lock:
            day = self.day()
            spent = self.store.spent(day)
            if spent + total > self.cfg.max_spend:
                raise Refused(402, f"потолок трат на сутки: потрачено "
                                   f"{spent}, задание стоит {total}, потолок "
                                   f"{self.cfg.max_spend} {DENOM}")
            # Текст — ДО сделки: задание возьмут сразу.
            with open(self._path(ph), "w", encoding="utf-8",
                      newline="") as f:
                f.write(prompt)
            self.pending.add(ph)
            if self.upload is not None:
                try:
                    self.upload(prompt)
                except Exception as e:
                    self.pending.discard(ph)
                    if ph not in self.active_hashes():
                        os.remove(self._path(ph))
                    raise Refused(503, f"посредник не принял текст, сделка "
                                       f"не отправлена: {str(e)[:300]}")
            msg, funds, _ = client.submit_message(p, ph, self.cfg.price,
                                                  self.cfg.prompt_uri)
            try:
                res = self.sender.send(msg, funds=funds)
            except Exception as e:
                self.pending.discard(ph)
                if ph not in self.active_hashes():
                    os.remove(self._path(ph))
                raise Refused(502, f"сделка не прошла: {str(e)[:300]}")
            job_id = tx.attribute(res.get("tx"), "job_id")
            if job_id is None:
                # Деньги, возможно, ушли — потолок считает их потраченными.
                self.store.q("INSERT INTO jobs(job_id,prompt_hash,created,"
                             "day,total,txhash,state,done) "
                             "VALUES(?,?,?,?,?,?,?,1)",
                             -int(self.now() * 1000), ph, self.now(), day,
                             total, res.get("txhash"), "unknown")
                self.pending.discard(ph)
                raise Refused(502, f"сделка {res.get('txhash')} прошла, но "
                                   f"номера задания в ней нет")
            job_id = int(job_id)
            self.store.q("INSERT INTO jobs(job_id,prompt_hash,created,day,"
                         "total,txhash,state) VALUES(?,?,?,?,?,?,?)",
                         job_id, ph, self.now(), day, total,
                         res.get("txhash"), "open")
            self.pending.discard(ph)
        return job_id, res.get("txhash"), total

    # --- присмотр за заданием ---------------------------------------------

    def step(self, job_id):
        """Один шаг по заданию: прочитать цепочку, забрать ответ, снять
        невзятое по сроку. Возвращает состояние для агента."""
        rows = self.store.q("SELECT created,total,txhash,done,answer,state,"
                            "guilty,executor FROM jobs WHERE job_id=?",
                            job_id)
        if not rows:
            raise Refused(404, f"задание {job_id} ставил не этот прокси")
        created, total, txhash, done, answer, state, guilty, executor = rows[0]
        if not done:
            try:
                job = self.chain.load_job(self.cfg.rest, self.cfg.contract,
                                          job_id)
            except chain.ChainUnavailable:
                job = None
            if job is not None:
                state = str(job.get("state", "")).lower()
                executor = job.get("executor")
                g = job.get("guilty")
                guilty = None if g is None else ("1" if g else "0")
                if job.get("answer_hash") and answer is None:
                    b, _, why = self.fetch(
                        job, self.sources(self.cfg.rest, self.cfg.contract,
                                          job, ()), job_id)
                    if b is not None:
                        answer = b["answer"]
                if state == "open" and \
                        self.now() - created > self.cfg.open_timeout:
                    if self.cancel(job_id):
                        state, guilty = "settled", None
                # Закрыто, а ответ подтверждён, но не забран — держать
                # задание открытым: следующий шаг попробует снова.
                done = int(state == "settled" and not (
                    guilty == "0" and answer is None))
                self.store.q("UPDATE jobs SET state=?,guilty=?,executor=?,"
                             "answer=?,done=? WHERE job_id=?", state, guilty,
                             executor, answer, done, job_id)
                if done:
                    if guilty != "0":
                        # Негодный и непроверенный ответ не хранится:
                        # отдавать его агенту нельзя, а держать — незачем.
                        answer = None
                        self.store.q("UPDATE jobs SET answer=NULL WHERE "
                                     "job_id=?", job_id)
                    self.forget(job_id)
        verdict = {"1": "guilty", "0": "confirmed"}.get(
            guilty, "no_verdict" if state == "settled" else None)
        return {"job_id": job_id, "state": state, "verdict": verdict,
                "executor": executor, "txhash": txhash, "paid": total,
                "denom": DENOM, "verified": verdict == "confirmed",
                "answer": answer if (verdict == "confirmed" or
                                     verdict is None) else None}

    def cancel(self, job_id):
        try:
            with self.tx_lock:
                self.sender.send({"cancel_job": {"job_id": int(job_id)}})
            return True
        except tx.TxFailed as e:
            # Гонка: узел мог взять задание в ту же секунду.
            print(f"cancel {job_id}: {e}", file=sys.stderr)
            return False

    def wait(self, job_id, mode, timeout):
        """Ждать, пока задание дойдёт до того, что нужно агенту."""
        end = self.now() + timeout
        while True:
            s = self.step(job_id)
            if s["state"] == "settled" and (s["verdict"] != "confirmed"
                                            or s["answer"] is not None):
                return s
            if mode == "answer" and s["answer"] is not None:
                return s
            if self.now() >= end:
                return s
            self.sleep(self.cfg.poll)


# --- HTTP для агента --------------------------------------------------------

def completion_body(kind, s, model, created):
    """Ответ в форме OpenAI плюс поле `verification` — что с заданием."""
    text = clean_answer(s["answer"] or "")
    verification = {k: s[k] for k in ("job_id", "state", "verdict",
                                       "executor", "txhash", "paid",
                                       "denom")}
    verification["verified"] = s["verified"]
    base = {"id": f"vi-{s['job_id']}", "created": int(created),
            "model": model, "verification": verification,
            "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                      "total_tokens": 0}}
    if kind == "chat":
        base.update(object="chat.completion", choices=[{
            "index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": text}}])
    else:
        base.update(object="text_completion", choices=[{
            "index": 0, "finish_reason": "stop", "text": text,
            "logprobs": None}])
    return base


def failure(s):
    """Задание закрыто без годного ответа: (код, текст)."""
    if s["verdict"] == "guilty":
        return 502, (f"задание {s['job_id']}: проверяющие признали ответ "
                     f"негодным, цена возвращена на кошелёк агента")
    if s["verdict"] == "no_verdict":
        return 504, (f"задание {s['job_id']}: закрыто без вердикта (не взяли "
                     f"или проверки не собрались в срок), деньги возвращены")
    return 504, (f"задание {s['job_id']} ещё в работе ({s['state']}): "
                 f"ответ заберите GET /v1/jobs/{s['job_id']}")


def make_handler(proxy):
    cfg = proxy.cfg

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, code, text, extra=None):
            err = {"message": text, "type": "vi_error", "code": code}
            if extra:
                err.update(extra)
            self._send(code, {"error": err})

        def _authed(self):
            if not cfg.api_token:
                return True
            got = self.headers.get("Authorization", "")
            if secrets.compare_digest(got, f"Bearer {cfg.api_token}"):
                return True
            self._error(401, "нужен заголовок Authorization: Bearer <API_TOKEN>")
            return False

        def _json(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 4 * cfg.max_chars + 65536:
                raise BadRequest("тело запроса слишком велико")
            try:
                d = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                raise BadRequest("тело — не JSON")
            if not isinstance(d, dict):
                raise BadRequest("тело — не объект JSON")
            return d

        def do_GET(self):
            if self.path == "/healthz":
                return self._health()
            if not self._authed():
                return
            if self.path == "/v1/models":
                p = proxy.params()
                return self._send(200, {"object": "list", "data": [{
                    "id": p.pinned_model, "object": "model",
                    "owned_by": "verifiable-inference"}]})
            m = re.fullmatch(r"/v1/jobs/(\d+)", self.path)
            if m:
                return self._send(200, proxy.step(int(m.group(1))))
            self._error(404, "нет такого пути")

        def _health(self):
            try:
                p = proxy.params()
                _, fee, total = client.totals(p, cfg.price)
                self._send(200, {
                    "ok": True, "agent": proxy.sender.address(),
                    "contract": cfg.contract, "model": p.pinned_model,
                    "price": cfg.price, "verifier_fee": fee,
                    "per_request": total, "denom": DENOM,
                    "spent_today": proxy.store.spent(proxy.day()),
                    "max_spend_day": cfg.max_spend})
            except Exception as e:
                self._send(503, {"ok": False, "error": str(e)[:300]})

        def do_POST(self):
            if not self._authed():
                return
            try:
                d = self._json()
                if self.path == "/v1/chat/completions":
                    prompt = render_chat(d.get("messages"), proxy.template())
                    kind = "chat"
                elif self.path == "/v1/completions":
                    prompt = d.get("prompt")
                    if isinstance(prompt, list) and len(prompt) == 1:
                        prompt = prompt[0]
                    if not isinstance(prompt, str):
                        raise BadRequest("prompt: нужна строка")
                    kind = "text"
                elif self.path == "/v1/jobs":
                    # Без ожидания: агент сам спрашивает GET /v1/jobs/N.
                    if "messages" in d:
                        prompt = render_chat(d["messages"], proxy.template())
                    else:
                        prompt = d.get("prompt")
                        if not isinstance(prompt, str):
                            raise BadRequest("нужен prompt или messages")
                    job_id, txhash, total = proxy.submit(prompt)
                    return self._send(202, {"job_id": job_id,
                                            "txhash": txhash, "paid": total,
                                            "denom": DENOM})
                else:
                    return self._error(404, "нет такого пути")
                if d.get("stream"):
                    raise BadRequest("stream не поддерживается: ответ "
                                     "отдаётся целиком, после сверки")
                mode = d.get("verification", cfg.mode)
                if mode not in MODES:
                    raise BadRequest(f"verification: одно из "
                                     f"{', '.join(MODES)}")
                job_id, txhash, total = proxy.submit(prompt)
                created = proxy.now()
                s = proxy.wait(job_id, mode, cfg.wait)
                if s["answer"] is None or s["verdict"] in ("guilty",
                                                           "no_verdict"):
                    code, text = failure(s)
                    return self._error(code, text, {"job_id": job_id})
                self._send(200, completion_body(
                    kind, s, proxy.params().pinned_model, created))
            except BadRequest as e:
                self._error(400, str(e))
            except Refused as e:
                self._error(e.code, str(e))
            except chain.ChainUnavailable as e:
                self._error(503, f"цепочка не ответила: {e}")

        def log_message(self, fmt, *a):
            print(f"{self.address_string()} {fmt % a}", file=sys.stderr)

    return H


def make_sender(cfg, key):
    """Отправитель сделок. По умолчанию — свой, без axmd: агенту хватает
    ключа и Python."""
    if cfg.signer == "python":
        return wallet.Wallet(cfg.contract, key, rest=cfg.rest,
                             chain_id=cfg.chain_id,
                             gas_prices=cfg.gas_prices)
    return tx.Axmd(cfg.contract, cfg.key, chain_id=cfg.chain_id,
                   node=cfg.node, home=cfg.home, keyring=cfg.keyring,
                   gas_prices=cfg.gas_prices)


def main():
    cfg = Config()
    store = Store(cfg.db)
    key = sign.load_key(cfg.sign_key)
    mine = sign.address(sign.pubkey(key))
    sender = make_sender(cfg, key)
    if mine != sender.address():
        raise SystemExit(f"ОТКАЗ: SIGN_KEY даёт {mine}, а ключ {cfg.key} — "
                         f"{sender.address()}. Узлы отдают ответ только "
                         f"заказчику, а заказчик — адрес KEY")
    delivery.set_sign_key(key)
    proxy = Proxy(cfg, store, sender)
    p = proxy.params()
    _, fee, total = client.totals(p, cfg.price)
    print(f"агент {mine}, реестр {cfg.contract}, модель {p.pinned_model}; "
          f"запрос стоит {cfg.price} + {fee} = {total} {DENOM}, потолок "
          f"{cfg.max_spend} {DENOM} в сутки", file=sys.stderr)
    try:
        print(f"на кошельке {chain.balance(cfg.rest, mine, DENOM)} {DENOM}",
              file=sys.stderr)
    except chain.ChainUnavailable as e:
        print(f"баланс не прочитан: {e}", file=sys.stderr)
    n = proxy.purge()
    if n:
        print(f"стёрто текстов закрытых заданий: {n}", file=sys.stderr)
    if cfg.relay:
        proxy.upload = lambda text: delivery.upload_prompt(cfg.relay, key,
                                                           text)
        print(f"тексты узлам: через посредника {cfg.relay}; открытый порт "
              f"не нужен", file=sys.stderr)
    else:
        serve_prompts(cfg.prompts, cfg.prompt_port,
                      allowed=proxy.active_hashes, auth=proxy.authorize)
        print(f"тексты узлам: :{cfg.prompt_port}, снаружи {cfg.prompt_uri}",
              file=sys.stderr)
    srv = http.server.ThreadingHTTPServer((cfg.api_host, cfg.api_port),
                                          make_handler(proxy))
    print(f"API агента: http://{cfg.api_host}:{cfg.api_port}/v1",
          file=sys.stderr)
    srv.serve_forever()


if __name__ == "__main__":
    main()
