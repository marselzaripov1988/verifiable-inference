#!/usr/bin/env python3
"""Петля узла: опросить цепочку, взять задание, сдать ответ, проверить чужой.

Шаг 15. Чего не хватало до неё. Контракт развёрнут, демоны считают
правильно, сообщения собираются те, что он ждёт, — но крутить протокол
было некому. `executor.py` и `checker.py` работают по ОДНОМУ заданию,
номер которого им назвали, и печатают сообщение, которое кто-то должен
отправить руками. Сеть из таких узлов не работает: заказчик ставит
задание, и его никто не берёт.

Петля закрывает ровно это: сама находит задания, сама решает, что с ними
делать, и — с `--send` — сама отправляет.

ЧТО ЗДЕСЬ ВАЖНО ЗНАТЬ.

**Задания берутся запросом списка, а при старом контракте — перебором.**
Запрос `Jobs` появился 27 октября; до него в `QueryMsg` был только
`Job{job_id}`, счётчик `NEXT_JOB` снаружи не виден, и петля шла по
номерам от своей отметки, останавливаясь на отсутствующем. Перебор
остался — не из вежливости к старому коду, а потому что перенос трёх
живых адресов делается не мгновенно, и в это время узел обязан работать.
Какой путь взят, петля говорит вслух. Номера сплошные в обоих случаях —
`CancelJob` запись не удаляет, а переводит в `Settled`.

**Отсутствие задания и недоступность цепочки — разные вещи.** Если их
смешать, упавший REST выглядит концом списка, и петля тихо простоит всю
неисправность, считая, что заданий нет. Разбор — в `chain.job_or_none`.

**Текст приходит вне цепочки и ОБЯЗАТЕЛЬНО сверяется с ней.** В цепочке
только хэши. Запрос исполнителю нужен, чтобы работать; запрос и ответ
проверяющему — чтобы оценивать. Берутся они откуда угодно: из каталога,
у соседа, у самого исполнителя. Подмена ловится сверкой с `prompt_hash` и
`answer_hash`, и поэтому доставка не требует доверия — требует лишь того,
чтобы кто-то отдавал. Отсюда и `--prompts`/`--peers` списком: пробуется
каждый, берётся первый сошедшийся с цепочкой.

**Где искать — теперь говорит сама цепочка.** У задания есть
`prompt_uri` (назвал заказчик), у записи узла — `endpoint` (назвал узел).
Оба спрашиваются первыми, а `--prompts` и `--peers` остаются за ними: до
27 октября адреса передавались людьми, и узел, которому не сказали адрес
соседа, не мог проверить его ответ. Доверия это не прибавляет ни на
сколько — всё так же решает хэш; это лишь первое место, куда смотреть.

**Один залог — одно задание за раз.** Карта одна. Взяв второе, узел
просрочит оба, и оба у него отберут.

**Сроки закрывает любой.** `release_job` и `expire_checks` вправе
вызвать кто угодно, нарочно: иначе срок был бы усмотрением админа с
таймером. «Кто угодно» значит, что должен кто-то, и место этому — здесь,
в петле узла. Выгоды от вызова нет, кроме той, что задания возвращаются
в оборот.

Запуск (сперва без отправки — видно, что узел собрался делать):

    python3 loop.py --rest https://axm-lcd.trickle.pro \\
        --contract axm1… --base-url http://127.0.0.1:8000/v1 \\
        --key node1 --bundles ./bundles \\
        --endpoint http://мой-адрес:8081 --once

С отправкой добавить `--send`. Без `--once` крутится с паузой
`--every` секунд.
"""

import argparse
import json
import os
import sys
import time

import chain
import checker
import delivery
import executor
import tx


# --- снимок цепочки -------------------------------------------------------

def scan_jobs(rest, contract, from_id, max_scan=200, miss_streak=2):
    """Задания подряд от `from_id`. Возвращает {номер: задание}.

    Останавливается после `miss_streak` подряд отсутствующих — это и есть
    конец списка. Недоступность цепочки НЕ считается отсутствием и летит
    наружу: иначе неисправность выглядела бы пустой очередью.

    `max_scan` ограничивает цикл по числу запросов: при длинной истории
    заданий без отметки `from_id` один проход съедал бы тысячи запросов к
    REST.
    """
    jobs, misses = {}, 0
    job_id = int(from_id)
    for _ in range(int(max_scan)):
        job = chain.job_or_none(rest, contract, job_id)
        if job is None:
            misses += 1
            if misses >= miss_streak:
                break
        else:
            misses = 0
            jobs[job_id] = job
        job_id += 1
    return jobs


def jobs_listed(rest, contract, from_id, max_scan=200, page=50):
    """Задания от `from_id` ЗАПРОСОМ СПИСКА. (задания, граница).

    Страницами, потому что страница ограничена сверху контрактом (50): за
    одну историю в тысячу заданий не вытащить, а узел, у которого отметка
    потерялась, читает с начала.

    `QueryUnknown` летит наружу: решать, чем заменить незнакомый запрос,
    не здесь.
    """
    jobs, border = {}, None
    after = int(from_id) - 1
    after = after if after > 0 else None
    while len(jobs) < int(max_scan):
        want = min(int(page), int(max_scan) - len(jobs))
        got, border = chain.jobs_page(rest, contract, after, want)
        for e in got:
            jobs[int(e["job_id"])] = e["job"]
        if not got:
            break
        after = int(got[-1]["job_id"])
        if after + 1 >= border:
            break
    return jobs, border


def snapshot(rest, contract, from_id, max_scan=200):
    """Снимок заданий от отметки: (задания, граница, чем добыто).

    Граница None означает, что контракт запроса списка не знает и задания
    найдены перебором. Это не ошибка и не повод останавливаться: перенос
    трёх живых адресов не мгновенный, и узел обязан работать всё это
    время.
    """
    try:
        jobs, border = jobs_listed(rest, contract, from_id, max_scan)
        return jobs, border, "список"
    except chain.QueryUnknown:
        return scan_jobs(rest, contract, from_id, max_scan), None, "перебор"


def state_of(job):
    return str(job.get("state", "")).lower()


def next_watermark(jobs, current):
    """Откуда начинать в следующий раз.

    Отметка двигается только по подряд ЗАКРЫТЫМ заданиям с начала
    просмотра. Задание в любом другом состоянии останавливает её: по нему
    ещё будет работа — у нас или у кого-то, — и пропустить его нельзя.
    """
    nxt = int(current)
    for job_id in sorted(jobs):
        if job_id != nxt:
            break
        if state_of(jobs[job_id]) != "settled":
            break
        nxt = job_id + 1
    return nxt


def load_state(path):
    if not path or not os.path.exists(path):
        return {"from_id": 1}
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return {"from_id": int(d.get("from_id", 1))}
    except Exception:
        # Испорченный файл состояния — не повод стоять: отметка всего лишь
        # оптимизация, без неё петля верна, просто читает больше.
        return {"from_id": 1}


def save_state(path, state):
    if not path:
        return
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


# --- что делать в этом цикле ---------------------------------------------

def plan(jobs, checks, me, params, now, roles=("executor", "checker"),
         min_left=0, max_checks=1):
    """Решение по снимку цепочки. ЧИСТАЯ функция: ни сети, ни отправки.

    Так она проверяема целиком — а проверять тут есть что: порядок
    действий в цикле выбран не произвольно.

    `checks` — {номер задания: список сданных проверок} для состояния
    `answered`.

    Порядок:

    1. сроки — чужое брошенное возвращается в оборот раньше, чем мы ищем
       себе работу;
    2. свой ответ — за взятие задания уже заплачено, и срок идёт;
    3. чужие проверки — работа, за которую платят долю;
    4. сведение — закрывает задание, собравшее кворум;
    5. взятие нового — последним, и только если своего нет.
    """
    acts = []
    busy = any(state_of(j) == "claimed" and j.get("executor") == me
               for j in jobs.values())

    # 1. сроки
    for job_id in sorted(jobs):
        job = jobs[job_id]
        n = len(checks.get(job_id, [])) if state_of(job) == "answered" else None
        msg = chain.due_message(job_id, job, params, now, n)
        if msg:
            acts.append({"do": next(iter(msg)), "job_id": job_id, "msg": msg,
                         "why": f"срок вышел в состоянии {state_of(job)}"})

    # 2. свой ответ
    if "executor" in roles:
        for job_id in sorted(jobs):
            job = jobs[job_id]
            if state_of(job) != "claimed" or job.get("executor") != me:
                continue
            bad = executor.deadline_problems(job, params, now, min_left)
            if bad:
                # Отказ, а не работа: за просроченное задание уже не
                # заплатят, а инференс стоит денег. Сказать почему —
                # обязательно, иначе узел выглядит простаивающим.
                acts.append({"do": "skip", "job_id": job_id, "msg": None,
                             "why": bad[0]})
                continue
            acts.append({"do": "answer", "job_id": job_id, "msg": None,
                         "why": "своё взятое задание без ответа"})

    # 3. чужие проверки
    if "checker" in roles:
        taken = 0
        for job_id in sorted(jobs):
            if taken >= int(max_checks):
                break
            job = jobs[job_id]
            if state_of(job) != "answered" or job.get("executor") == me:
                continue
            got = checks.get(job_id, [])
            if any(c.get("checker") == me for c in got):
                continue
            if len(got) >= int(params.checks_required):
                continue
            acts.append({"do": "check", "job_id": job_id, "msg": None,
                         "why": f"проверок {len(got)} из "
                                f"{params.checks_required}"})
            taken += 1

    # 4. сведение
    for job_id in sorted(jobs):
        job = jobs[job_id]
        if state_of(job) != "answered":
            continue
        if len(checks.get(job_id, [])) < int(params.checks_required):
            continue
        acts.append({"do": "finalize", "job_id": job_id,
                     "msg": {"finalize": {"job_id": int(job_id)}},
                     "why": "кворум собран"})

    # 5. взятие нового
    if "executor" in roles and not busy:
        for job_id in sorted(jobs):
            if state_of(jobs[job_id]) == "open":
                acts.append({"do": "claim_job", "job_id": job_id,
                             "msg": {"claim_job": {"job_id": int(job_id)}},
                             "why": "свободное задание, своего нет"})
                break
    return acts


def allowed_roles(params, record, roles):
    """Какие роли узлу СЕЙЧАС доступны, и почему остальные — нет.

    Проверяется до отправки, потому что контракт проверит то же самое, но
    за газ, а причина из отвергнутой сделки не видна.

    Роли отбираются по отдельности, и это не формальность:

      * проба и бан закрывают обе роли — `submit_check` контракт требует
        от прошедшего пробу так же, как `claim_job`;
      * недобор залога закрывает только исполнителя: с проверяющего
        контракт залога не требует вовсе, и запрещать ему работать значило
        бы терять проверки на узле, которому просто нечего ставить;
      * необъявленный адрес доставки — тоже только исполнителя, и ровно
        по той же причине: связка нужна ОТ исполнителя. Контракт с 27
        октября отвергает `claim_job` без адреса, так что без этой
        проверки узел платил бы газ за отказ каждый цикл.

    Сроки и сведение остаются доступны при любом исходе: их вправе
    вызвать кто угодно, и ничего из проверенного здесь для них не нужно.
    """
    bad = list(chain.conformance_problems(params, record))
    status = str((record or {}).get("status", "")).lower()
    if status == "banned":
        bad.append("узел забанен: заданий не будет")
    if bad:
        return (), bad

    have = int((record or {}).get("stake", 0) or 0)
    if have < int(params.min_stake):
        return (tuple(r for r in roles if r != "executor"),
                [f"залог {have} меньше нужного {params.min_stake} "
                 f"{params.stake_denom}: заданий не взять, проверять можно"])
    # Адрес доставки проверяется ТАК ЖЕ, как залог, и по той же причине:
    # контракт его требует при взятии, а причина отвергнутой сделки
    # оператору не видна — видна только списанная плата за газ.
    if not (record or {}).get("endpoint"):
        return (tuple(r for r in roles if r != "executor"),
                ["адрес доставки не объявлен: связку взять негде, заданий "
                 "не дадут. Отправьте set_endpoint — проверять можно и так"])
    return tuple(roles), []


# --- выполнение -----------------------------------------------------------

class Loop:
    """Один узел: цепочка, свой vLLM, отправитель и источники текста."""

    def __init__(self, rest, contract, sender, base_url, model,
                 dtype="bfloat16", roles=("executor", "checker"),
                 bundles=None, prompts=(), peers=(), state_path=None,
                 min_left=0, max_checks=1, max_scan=200, max_tokens=200,
                 skip_expired=False, endpoint=None, out=None):
        self.rest = rest
        self.contract = contract
        self.sender = sender
        self.base_url = base_url
        self.model = model
        self.dtype = dtype
        self.roles = tuple(roles)
        self.bundles = bundles
        self.prompts = tuple(prompts)
        self.peers = tuple(peers)
        self.state_path = state_path
        self.min_left = min_left
        self.max_checks = max_checks
        self.max_scan = max_scan
        self.max_tokens = max_tokens
        self.skip_expired = skip_expired
        self.endpoint = endpoint
        self.out = out if out is not None else sys.stderr
        self.state = load_state(state_path)
        #: Адреса доставки чужих узлов, прочитанные в этом цикле. Кеш на
        #: цикл, а не навсегда: адрес меняется, и узел, переехавший между
        #: циклами, иначе остался бы недостижимым до перезапуска.
        self.endpoints = {}

    def say(self, s):
        print(s, file=self.out)

    def bundle_path(self, job_id):
        if not self.bundles:
            return None
        return os.path.join(self.bundles, f"{int(job_id)}.json")

    # --- источники --------------------------------------------------------

    def prompt_sources(self, job=None):
        """Где искать запрос: адрес из задания, свои каталоги, соседи.

        Адрес из задания первым — его назвал заказчик, он и обязался
        отдавать. Остальные за ним: запрос сверяется с `prompt_hash`, и
        отдать его может кто угодно, так что молчащий адрес заказчика не
        обязан стоить задания.
        """
        mine = tuple(self.prompts) + tuple(self.peers)
        return delivery.sources_with((job or {}).get("prompt_uri"), mine)

    def bundle_sources(self, job=None):
        """Где искать связку: адрес исполнителя, соседи, свой каталог.

        Адрес исполнителя спрашивается у цепочки по его записи — до 27
        октября его негде было взять, и узел, которому не назвали соседа,
        не мог проверить чужой ответ.

        Свой каталог последним и не напрасно: по заданию, которое мы же
        исполняли, связка лежит у нас, и проверять её нам всё равно
        нельзя — контракт отвергнет самопроверку. Но каталог бывает общим
        на несколько узлов.
        """
        mine = tuple(self.peers) + ((self.bundles,) if self.bundles else ())
        return delivery.sources_with(self.endpoint_of(job), mine)

    def announce_endpoint(self, record):
        """Привести адрес в цепочке к своему. Возвращает свежую запись.

        Зачем это петле, а не только оператору. Адрес меняется чаще
        всего прочего: карта переарендована, машина переехала, порт
        другой. Узел, у которого в цепочке записан прежний адрес, выглядит
        исправным и берёт задания — а связку у него не забрать, и его
        ответ не проверят. Петля знает свой адрес из флага, то есть видит
        расхождение сразу, и сделка уходит ТОЛЬКО при расхождении.

        Молчание тоже законно: без `--endpoint` петля ничего не объявляет
        и не переписывает объявленное оператором.
        """
        if not self.endpoint:
            return None
        have = (record or {}).get("endpoint")
        if have == self.endpoint:
            return None
        if record is None:
            self.say("адрес не объявить: узел не зарегистрирован в контракте")
            return None
        self.say(f"адрес доставки: в цепочке {have or 'нет'}, у нас "
                 f"{self.endpoint} — объявляем")
        try:
            self.sender.send({"set_endpoint": {"endpoint": self.endpoint}})
        except tx.TxFailed as e:
            self.say(f"  адрес не объявлен: {str(e).splitlines()[0][:160]}")
            return None
        return chain.load_executor(self.rest, self.contract,
                                   self.sender.address())

    def endpoint_of(self, job):
        """Адрес исполнителя задания. Спрашивается раз за цикл на узел.

        Недоступность цепочки здесь НЕ останавливает проверку: адрес
        всего лишь первый источник, а остальные никуда не делись.
        """
        who = (job or {}).get("executor")
        if not who:
            return None
        if who not in self.endpoints:
            try:
                self.endpoints[who] = chain.endpoint_of(
                    self.rest, self.contract, who)
            except chain.ChainUnavailable as e:
                self.say(f"  адрес узла {who} не прочитан: {e}")
                self.endpoints[who] = None
        return self.endpoints[who]

    # --- действия ---------------------------------------------------------

    def do_answer(self, job_id, job, params, now, record=None):
        """Породить ответ и собрать `submit_answer`.

        Готовая связка ПЕРЕИСПОЛЬЗУЕТСЯ, если сходится с заданием. Это не
        кеш ради скорости: между порождением и отправкой узел может
        упасть, и повторный инференс стоил бы денег второй раз. Сошлась
        связка с `prompt_hash` — значит она по этому заданию.
        """
        path = self.bundle_path(job_id)
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    b = json.load(f)
                if (b.get("prompt_hash") == job.get("prompt_hash")
                        and b.get("answer_hash") and b.get("logprobs_hash")):
                    self.say(f"  {job_id}: связка уже есть, инференс не "
                             f"повторяется")
                    return {"submit_answer": {
                        "job_id": int(job_id),
                        "answer_hash": b["answer_hash"],
                        "logprobs_hash": b["logprobs_hash"],
                        "answer_tokens": int(b.get("answer_tokens") or 0),
                    }}
            except Exception as e:
                self.say(f"  {job_id}: своя связка не прочитана ({e}), "
                         f"считаем заново")

        raw, src, why = delivery.fetch_verified(
            self.prompt_sources(job),
            (f"{job.get('prompt_hash')}.txt", f"{int(job_id)}.txt"),
            delivery.prompt_check(job))
        if raw is None:
            self.say(f"  {job_id}: запрос не получен ни откуда — работать "
                     f"нечем")
            for w in why:
                self.say(f"    {w}")
            return None
        prompt = delivery.prompt_text(raw, job)
        self.say(f"  {job_id}: запрос получен из {src}, сошёлся с цепочкой")

        r = executor.run(self.base_url, self.model, int(job_id), prompt,
                         self.max_tokens, path, params=params,
                         dtype=self.dtype, record=record, job=job, now=now,
                         min_left=self.min_left)
        if r is None:
            return None
        # Длина ответа в токенах лежит в сообщении, а в связке её не было:
        # дописываем, иначе переиспользование после падения не соберёт
        # сообщение целиком.
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    b = json.load(f)
                b["answer_tokens"] = r["msg"]["submit_answer"]["answer_tokens"]
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(b, f, ensure_ascii=False)
            except Exception as e:
                self.say(f"  {job_id}: связку не дописали ({e})")
        return r["msg"]

    def do_check(self, job_id, job, params, now, record=None):
        """Достать связку, оценить ту же пару и собрать `submit_check`."""
        raw, src, why = delivery.fetch_verified(
            self.bundle_sources(job), f"{int(job_id)}.json",
            delivery.bundle_check(job))
        if raw is None:
            self.say(f"  {job_id}: связка не получена ни откуда — проверять "
                     f"нечего")
            for w in why:
                self.say(f"    {w}")
            return None
        b = json.loads(raw)
        self.say(f"  {job_id}: связка получена из {src}, текст сошёлся с "
                 f"цепочкой")
        r = checker.run(self.base_url, self.model, int(job_id),
                        b["prompt"], b["answer"], b.get("vector"),
                        job.get("logprobs_hash"), params=params,
                        dtype=self.dtype, record=record, job=job, now=now,
                        skip_expired=self.skip_expired)
        if r is None:
            return None
        same = (r["msg"]["submit_check"]["logprobs_hash"]
                == job.get("logprobs_hash"))
        self.say(f"  {job_id}: расхождение {r['divergence_micro']}, "
                 f"хэш {'совпал' if same else 'НЕ СОВПАЛ'}")
        return r["msg"]

    # --- цикл -------------------------------------------------------------

    def cycle(self):
        """Один проход. Возвращает сводку: что сделано и что не вышло."""
        params = chain.load_params(self.rest, self.contract)
        now = chain.chain_time(self.rest)
        me = self.sender.address()
        record = chain.load_executor(self.rest, self.contract, me)
        self.endpoints = {}
        fresh = self.announce_endpoint(record)
        if fresh is not None:
            record = fresh

        roles, blocked = allowed_roles(params, record, self.roles)
        for b in blocked:
            self.say(f"роль ограничена: {b}")
        if not roles:
            # Работать нельзя, но сроки и сведение — можно: их вправе
            # вызвать кто угодно. Поэтому цикл продолжается, а не выходит.
            self.say("своей работы не будет; остаются сроки и сведение")

        from_id = self.state["from_id"]
        jobs, border, how = snapshot(self.rest, self.contract, from_id,
                                     self.max_scan)
        if border is not None and from_id > border:
            # Отметка ЗА границей списка. С запросом `NextJob` это видно
            # прямо, а не по косвенным признакам: такого номера контракт
            # ещё не выдавал, значит файл состояния приехал с другой сети
            # или с соседа.
            self.say(f"отметка {from_id} за границей списка ({border}) — "
                     f"смотрим с начала")
            from_id = 1
            self.state["from_id"] = 1
            jobs, border, how = snapshot(self.rest, self.contract, 1,
                                         self.max_scan)
        if border is None and from_id > 1 and not jobs:
            # То же самое, но для контракта БЕЗ запроса списка: границы
            # нет, и приходится судить по косвенному признаку.
            #
            # Отметка может указывать ЗА КРАЙ цепочки: файл состояния
            # переехал с другой сети, был восстановлен из копии или
            # скопирован с соседа. Тогда узел слеп ко всем заданиям ниже
            # отметки и молчит об этом — ровно это и случилось на прогоне
            # стенда, где рабочий каталог переиспользовали.
            #
            # Признак положительный: задание под `from_id - 1` обязано
            # существовать И БЫТЬ ЗАКРЫТЫМ — отметку двигает только
            # `next_watermark`, и только по закрытым. Один лишний
            # запрос, и только когда работы нет.
            #
            # Проверять одно существование НЕ ГОДИТСЯ, и это стоило
            # второго прогона: на новой цепочке под номером 1 оказалось
            # свежее открытое задание, существование подтвердилось, и
            # узел так и остался слепым.
            prev = chain.job_or_none(self.rest, self.contract, from_id - 1)
            if prev is None or state_of(prev) != "settled":
                self.say(f"отметка {from_id} указывает за край цепочки — "
                         f"смотрим с начала")
                from_id = 1
                self.state["from_id"] = 1
                jobs = scan_jobs(self.rest, self.contract, 1, self.max_scan)
        checks = {}
        for job_id, job in jobs.items():
            if state_of(job) == "answered":
                checks[job_id] = chain.checks_of(self.rest, self.contract,
                                                 job_id)
        edge = f", граница {border}" if border is not None else ""
        self.say(f"заданий с {from_id}: {len(jobs)} ({how}{edge}), "
                 f"время цепочки {now}")

        acts = plan(jobs, checks, me, params, now, roles,
                    self.min_left, self.max_checks)
        sent, skipped, fatal = [], [], None
        for a in acts:
            job_id = a["job_id"]
            if a["do"] == "skip":
                self.say(f"  {job_id}: пропуск — {a['why']}")
                skipped.append(a)
                continue
            self.say(f"  {job_id}: {a['do']} — {a['why']}")
            msg = a["msg"]
            if msg is None:
                msg = (self.do_answer(job_id, jobs[job_id], params, now,
                                      record)
                       if a["do"] == "answer"
                       else self.do_check(job_id, jobs[job_id], params, now,
                                          record))
                if msg is None:
                    skipped.append(a)
                    continue
            try:
                res = self.sender.send(msg)
                sent.append({"do": a["do"], "job_id": job_id, "res": res})
            except tx.TxFailed as e:
                kind = tx.classify(str(e))
                if kind == "race":
                    # Обычное дело, а не поломка: задание успели занять,
                    # проверку успели сдать. Петля идёт дальше.
                    self.say(f"    опоздали: {str(e).splitlines()[0][:120]}")
                    skipped.append(a)
                    continue
                if kind == "retry":
                    self.say(f"    отправка не удалась, повтор в следующем "
                             f"цикле: {str(e).splitlines()[0][:120]}")
                    skipped.append(a)
                    continue
                # Непонятный отказ: дальше в этом цикле не отправляем —
                # остальные сообщения могли зависеть от этого, — но и не
                # падаем сразу. Решает `main` по числу подряд: один
                # неразобранный отказ не должен убивать узел насовсем, а
                # три подряд значат, что нужен оператор.
                self.say(f"    ОТКАЗ, не разобран: "
                         f"{str(e).splitlines()[0][:200]}")
                fatal = str(e)
                break

        self.state["from_id"] = next_watermark(jobs, from_id)
        save_state(self.state_path, self.state)
        return {"sent": sent, "skipped": skipped, "blocked": blocked,
                "fatal": fatal}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--rest", required=True, help="REST цепочки")
    p.add_argument("--contract", required=True)
    p.add_argument("--base-url", required=True, help="свой vLLM")
    p.add_argument("--model", default="Qwen/Qwen2.5-7B-Instruct")
    p.add_argument("--dtype", default="bfloat16")
    p.add_argument("--role", default="both",
                   choices=("both", "executor", "checker"))
    p.add_argument("--send", action="store_true",
                   help="отправлять сделки. Без него петля только читает и "
                        "печатает, что собиралась отправить")
    p.add_argument("--key", help="ключ в связке axmd; нужен с --send")
    p.add_argument("--node", default="https://api-chain.axiomechain.org",
                   help="RPC для отправки")
    p.add_argument("--chain-id", default="axiome-1")
    p.add_argument("--keyring-backend", default="test")
    p.add_argument("--home", help="--home для axmd")
    p.add_argument("--binary", default="axmd",
                   help="чем отправлять. На местном девнете это wasmd")
    p.add_argument("--gas-prices", default="1uaxm",
                   help="цена газа. На девнете единица другая")
    p.add_argument("--address", help="свой адрес. Нужен без --send: без "
                                     "ключа взять его негде")
    p.add_argument("--prompts", action="append", default=[],
                   help="где искать запросы: каталог или http(s). Можно "
                        "несколько раз")
    p.add_argument("--peers", action="append", default=[],
                   help="где искать связки соседей: каталог или http(s)")
    p.add_argument("--bundles", help="куда класть свои связки. Их же отдают "
                                     "соседям: python3 -m http.server")
    p.add_argument("--endpoint", help="свой адрес доставки, http(s). Если в "
                                      "цепочке записан другой, петля объявит "
                                      "этот")
    p.add_argument("--state", help="файл с отметкой просмотра")
    p.add_argument("--every", type=int, default=30,
                   help="пауза между циклами, секунды")
    p.add_argument("--once", action="store_true", help="один цикл и выход")
    p.add_argument("--max-tokens", type=int, default=200)
    p.add_argument("--max-checks", type=int, default=1,
                   help="сколько чужих заданий проверять за цикл")
    p.add_argument("--max-scan", type=int, default=200)
    p.add_argument("--min-time-left", type=int, default=0, metavar="СЕК",
                   help="запас срока, при котором ещё браться за работу")
    p.add_argument("--max-fatal", type=int, default=3,
                   help="сколько неразобранных отказов подряд терпеть, "
                        "прежде чем выйти")
    p.add_argument("--skip-expired", action="store_true",
                   help="не проверять задания с вышедшим сроком сбора")
    a = p.parse_args()

    roles = ("executor", "checker") if a.role == "both" else (a.role,)

    if a.send:
        if not a.key:
            print("ОТКАЗ: --send без --key: подписывать нечем", file=sys.stderr)
            return 2
        try:
            sender = tx.Axmd(a.contract, a.key, chain_id=a.chain_id,
                             node=a.node, home=a.home,
                             keyring=a.keyring_backend, binary=a.binary,
                             gas_prices=a.gas_prices)
            me = sender.address()
        except tx.TxFailed as e:
            print(f"ОТКАЗ: {e}", file=sys.stderr)
            return 2
    else:
        if not a.address:
            print("ОТКАЗ: без --send нужен --address: петля обязана знать, "
                  "чьи задания её", file=sys.stderr)
            return 2
        sender = tx.DryRun()
        sender.address = lambda: a.address
        me = a.address
        print("без отправки: сделки не уходят, сообщения печатаются",
              file=sys.stderr)

    if a.bundles:
        os.makedirs(a.bundles, exist_ok=True)
    elif "executor" in roles:
        # Без каталога связок ответ некуда положить, а значит проверяющим
        # нечего забрать: задание дойдёт до `Answered` и там и останется,
        # пока его не закроют сроком. Это предупреждение, а не отказ —
        # узел может отдавать связки чем-то своим.
        print("ВНИМАНИЕ: --bundles не задан: связку негде взять "
              "проверяющим, задание закроется сроком", file=sys.stderr)
    if "executor" in roles and not a.prompts and not a.peers:
        # Уже не отказ и даже не беда: адрес текста лежит в самом задании
        # (`prompt_uri`), и его назвал заказчик. Свои источники остаются
        # запасом на случай, когда его адрес молчит.
        print("без --prompts и --peers: запрос будет браться только по "
              "адресу из задания", file=sys.stderr)
    if "executor" in roles and not a.endpoint:
        print("ВНИМАНИЕ: --endpoint не задан: петля не объявит адрес "
              "доставки, и если он не объявлен в цепочке, заданий не дадут",
              file=sys.stderr)

    loop = Loop(a.rest, a.contract, sender, a.base_url, a.model,
                dtype=a.dtype, roles=roles, bundles=a.bundles,
                prompts=a.prompts, peers=a.peers, state_path=a.state,
                min_left=a.min_time_left, max_checks=a.max_checks,
                max_scan=a.max_scan, max_tokens=a.max_tokens,
                skip_expired=a.skip_expired, endpoint=a.endpoint)
    print(f"узел {me}, роли {'+'.join(roles)}", file=sys.stderr)

    # Подряд неразобранных отказов. Один такой не должен убивать узел:
    # прогон на девнете показал, чем это кончается — два узла проиграли
    # гонку за заданием, отказ не был разобран, оба вышли, и задание
    # осталось без проверок. Но и крутиться вечно, сжигая газ на том же
    # отказе, нельзя: три подряд значат, что нужен оператор.
    fatals = 0
    while True:
        r = {}
        try:
            r = loop.cycle()
            print(f"цикл: отправлено {len(r['sent'])}, пропущено "
                  f"{len(r['skipped'])}", file=sys.stderr)
            if r.get("fatal"):
                fatals += 1
                if fatals >= a.max_fatal:
                    print(f"ОТКАЗ отправки {fatals} раз подряд, последний: "
                          f"{r['fatal']}", file=sys.stderr)
                    return 1
            else:
                fatals = 0
        except chain.ChainUnavailable as e:
            # Недоступная цепочка — не повод падать: узел сдаёт работу
            # циклами, и следующий цикл может пройти. Но и не повод
            # делать выводы: ни одной отметки не двигается.
            print(f"цепочка недоступна, ждём: {e}", file=sys.stderr)
        except tx.TxFailed as e:
            # Сюда попадает только то, что не прошло через разбор в
            # цикле: сам отправитель не собрался, ключа нет, бинаря нет.
            print(f"ОТКАЗ: отправлять нечем: {e}", file=sys.stderr)
            return 1
        if a.once:
            return 1 if r.get("fatal") else 0
        time.sleep(max(1, a.every))


if __name__ == "__main__":
    raise SystemExit(main())
