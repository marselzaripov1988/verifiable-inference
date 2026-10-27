#!/usr/bin/env python3
"""Заказчик: поставить задание, забрать ответ, вернуть деньги.

Шаг 16, последнее место, где протокол требовал человека. Петля узла
крутится сама, но крутить ей было нечего: задание ставилось руками через
`axmd tx wasm execute`, а ответ заказчик добывал как мог.

Что здесь по существу.

**Запрос выкладывается ДО отправки, под именем своего хэша.** Номер
заданию присваивает контракт, то есть узнать его заранее нельзя. Выложи
запрос после — и между объявлением и выкладкой задание берут, а работать
исполнителю нечем. Имя `<prompt_hash>.txt` снимает это: оно известно до
отправки, а подмену всё равно ловит сверка с цепочкой.

**Сумма считается ТОЧНО.** Контракт требует `price + доля` ни больше ни
меньше, доля округляется вверх, и значение `verifier_fee_bps` берётся из
цепочки, а не из представления о нём. Ошибись на единицу — сделка
отвергнута целиком, за газ.

**Ответ не берётся на слово.** Он приходит вне цепочки, и заказчик
сверяет его с `answer_hash` из задания прежде, чем показать. Источников
список: исполнитель, сосед, зеркало — доверия не требует ни один.

**Вердикт показывается отдельно от ответа.** «Ответ получен» и «ответ
подтверждён проверяющими» — разные вещи: до `settled` ответ есть, а
подтверждения ещё нет. Смешай их, и заказчик решит, что непроверенное
проверено.

Отправка, как у петли, по флагу. Без `--send` печатается готовое
сообщение — тем же JSON, который примет `axmd tx wasm execute`.

    # поставить задание
    python3 client.py submit --rest … --contract axm1… \\
        --prompt-file вопрос.txt --price 1000000 \\
        --prompts ./prompts --send --key заказчик

    # забрать ответ, когда он появится
    python3 client.py collect --rest … --contract axm1… --job-id 7 \\
        --peers http://узел:8081 --wait 600
"""

import argparse
import base64
import json
import os
import sys
import time

import chain
import delivery
import tx


def totals(params, price):
    """Цена, доля проверяющих и сколько всего платить.

    Доля считается `chain.fee_for` — тем же округлением вверх, что в
    контракте. Отдельной арифметики здесь нет нарочно: две реализации
    одного округления разойдутся на единице, и это будет отказ сделки.
    """
    price = int(price)
    fee = chain.fee_for(price, params.verifier_fee_bps)
    return price, fee, price + fee


def submit_message(params, prompt_hash, price):
    """Сообщение, деньги и КУДА его отправлять.

    Возвращает (сообщение, монеты, адрес-получатель или None).

    Два пути, потому что расчёт двух видов и это настройка цепочки:

      * родной монетой — `submit_job` реестру, монеты идут с транзакцией;
      * CW20 — `send` НА ТОКЕН с вложенным сообщением. Напрямую реестр
        такие деньги не примет: два пути для одних денег означали бы два
        места, где их можно потерять.
    """
    _, _, total = totals(params, price)
    job = {"submit_job": {"prompt_hash": prompt_hash, "price": str(int(price))}}
    s = params.settlement
    if "native" in s:
        return job, f"{total}{s['native']}", None
    if "cw20" in s:
        hook = base64.b64encode(
            json.dumps(job, ensure_ascii=False).encode()).decode()
        return ({"send": {"contract": None, "amount": str(total),
                          "msg": hook}}, None, s["cw20"])
    raise chain.ChainUnavailable(f"неизвестный вид расчёта: {s!r}")


def verdict_line(job, params, checks=None):
    """Что с заданием, одной строкой — и без склейки разных вещей.

    «Ответ сдан» и «ответ подтверждён» разделены нарочно: до вердикта
    ответ есть, а подтверждения нет, и заказчику это решать, а не нам.
    """
    st = str(job.get("state", "")).lower()
    if st == "open":
        return "объявлено, никто не взял"
    if st == "claimed":
        return f"взято узлом {job.get('executor')}, ответа ещё нет"
    if st == "answered":
        n = len(checks) if checks is not None else "?"
        return (f"ответ сдан, собрано проверок {n} из "
                f"{params.checks_required} — вердикта ещё нет")
    if st == "settled":
        g = job.get("guilty")
        if g is True:
            return ("ВЕРДИКТ: исполнитель признан виновным — ответ считать "
                    "негодным, деньги вернулись заказчику")
        if g is False:
            return "ВЕРДИКТ: ответ подтверждён проверяющими"
        return ("закрыто БЕЗ вердикта: проверки не собрались в срок, "
                "деньги вернулись заказчику")
    return f"состояние {st or '?'}"


def fetch_answer(job, sources, job_id):
    """Ответ из связки, сверенный с цепочкой. (ответ, откуда, причины).

    Сверяется и запрос, и ответ: `chain.job_problems` требует оба. Иначе
    заказчику можно было бы подсунуть ответ на другой запрос.
    """
    raw, src, why = delivery.fetch_verified(
        sources,
        (f"{int(job_id)}.json", f"{job.get('answer_hash')}.json"),
        delivery.bundle_check(job))
    if raw is None:
        return None, None, why
    return json.loads(raw), src, why


# --- команды --------------------------------------------------------------

def cmd_submit(a, params, sender):
    if a.prompt_file:
        with open(a.prompt_file, encoding="utf-8") as f:
            prompt = f.read()
    else:
        prompt = a.prompt
    if not prompt:
        print("ОТКАЗ: запрос пустой", file=sys.stderr)
        return 2
    ph = chain.text_hash(prompt)
    price, fee, total = totals(params, a.price)
    unit = chain.settlement_label(params.settlement)
    print(f"запрос {len(prompt)} символов, хэш {ph}", file=sys.stderr)
    print(f"цена {price} + доля проверяющих {fee} = {total} {unit}",
          file=sys.stderr)

    # Выкладка ДО отправки: номер задания ещё не известен, а запрос уже
    # должен быть доступен — иначе задание возьмут, а работать нечем.
    if a.prompts:
        os.makedirs(a.prompts, exist_ok=True)
        path = os.path.join(a.prompts, f"{ph}.txt")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(prompt)
        print(f"запрос выложен: {path}", file=sys.stderr)
    else:
        print("ВНИМАНИЕ: --prompts не задан — запрос взять негде, задание "
              "никто не выполнит", file=sys.stderr)

    msg, funds, to = submit_message(params, ph, price)
    if to:
        # Реестр узнаёт о задании из вложенного сообщения, поэтому его
        # адрес подставляется здесь, а не в submit_message: там он не
        # известен.
        msg["send"]["contract"] = a.contract
    res = sender.send(msg, funds=funds, contract=to)
    if res.get("dry"):
        print("сделка не отправлена: без --send", file=sys.stderr)
        return 0
    job_id = tx.attribute(res.get("tx"), "job_id")
    if job_id is None:
        print(f"сделка прошла ({res.get('txhash')}), но номера задания в "
              f"событиях нет — посмотрите сделку", file=sys.stderr)
        return 1
    print(f"задание {job_id} объявлено, сделка {res.get('txhash')}",
          file=sys.stderr)
    # Второе имя — по номеру. Нужно не протоколу, а человеку: по каталогу
    # видно, какой запрос каким заданием стал.
    if a.prompts:
        with open(os.path.join(a.prompts, f"{job_id}.txt"), "w",
                  encoding="utf-8", newline="") as f:
            f.write(prompt)
    print(job_id)
    return 0


def cmd_status(a, params, sender):
    job = chain.load_job(a.rest, a.contract, a.job_id)
    checks = None
    if str(job.get("state", "")).lower() in ("answered", "settled"):
        try:
            checks = chain.checks_of(a.rest, a.contract, a.job_id)
        except chain.ChainUnavailable:
            checks = None
    now = chain.chain_time(a.rest)
    d = chain.deadline_of(job, params, now)
    print(f"задание {a.job_id}: {verdict_line(job, params, checks)}")
    if d["left"] is not None:
        print(f"срок: осталось {d['left']} с"
              if not d["expired"] else
              f"срок вышел {-d['left']} с назад")
    print(f"цена {job.get('price')}, доля {job.get('fee_total')}, "
          f"выплачено: {job.get('paid')}")
    return 0


def cmd_collect(a, params, sender):
    deadline = time.time() + max(0, a.wait)
    while True:
        job = chain.load_job(a.rest, a.contract, a.job_id)
        st = str(job.get("state", "")).lower()
        checks = None
        if st in ("answered", "settled"):
            try:
                checks = chain.checks_of(a.rest, a.contract, a.job_id)
            except chain.ChainUnavailable:
                checks = None
        line = verdict_line(job, params, checks)
        if job.get("answer_hash"):
            break
        print(f"задание {a.job_id}: {line}", file=sys.stderr)
        if time.time() >= deadline:
            # Ответа нет — и это не ошибка заказчика. Невзятое задание
            # можно забрать вместе с деньгами (cancel).
            print("ответа пока нет", file=sys.stderr)
            return 1
        time.sleep(max(1, a.every))

    print(f"задание {a.job_id}: {line}", file=sys.stderr)
    b, src, why = fetch_answer(job, a.peers, a.job_id)
    if b is None:
        print("ответ не получен ни откуда — или получен не тот:",
              file=sys.stderr)
        for w in why:
            print(f"  {w}", file=sys.stderr)
        return 1
    print(f"ответ получен из {src}, сошёлся с цепочкой по обоим хэшам",
          file=sys.stderr)
    if a.out:
        with open(a.out, "w", encoding="utf-8", newline="") as f:
            f.write(b["answer"])
        print(f"записан: {a.out}", file=sys.stderr)
    else:
        print(b["answer"])
    return 0


def cmd_cancel(a, params, sender):
    job = chain.load_job(a.rest, a.contract, a.job_id)
    st = str(job.get("state", "")).lower()
    if st != "open":
        # Взятое задание отбирают СРОКОМ, а не отменой: иначе заказчик
        # забирал бы деньги у работающего узла.
        print(f"ОТКАЗ: задание в состоянии {st}, забрать можно только "
              f"невзятое. Взятое освобождается по истечении срока — это "
              f"делает любой узел", file=sys.stderr)
        return 1
    res = sender.send({"cancel_job": {"job_id": int(a.job_id)}})
    if res.get("dry"):
        print("сделка не отправлена: без --send", file=sys.stderr)
        return 0
    print(f"задание {a.job_id} забрано, деньги вернулись. Сделка "
          f"{res.get('txhash')}", file=sys.stderr)
    return 0


CMDS = {"submit": cmd_submit, "status": cmd_status,
        "collect": cmd_collect, "cancel": cmd_cancel}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=sorted(CMDS))
    p.add_argument("--rest", required=True)
    p.add_argument("--contract", required=True)
    p.add_argument("--prompt", help="текст запроса")
    p.add_argument("--prompt-file", help="файл с запросом; берётся как есть")
    p.add_argument("--price", type=int, help="сколько получит исполнитель")
    p.add_argument("--prompts", help="каталог, из которого запрос заберут "
                                     "узлы. Отдавать его можно чем угодно")
    p.add_argument("--peers", action="append", default=[],
                   help="где искать связку с ответом: каталог или http(s)")
    p.add_argument("--job-id", type=int)
    p.add_argument("--out", help="куда записать ответ")
    p.add_argument("--wait", type=int, default=0,
                   help="сколько секунд ждать ответа")
    p.add_argument("--every", type=int, default=10)
    p.add_argument("--send", action="store_true")
    p.add_argument("--key")
    p.add_argument("--node", default="https://api-chain.axiomechain.org")
    p.add_argument("--chain-id", default="axiome-1")
    p.add_argument("--keyring-backend", default="test")
    p.add_argument("--home")
    p.add_argument("--binary", default="axmd")
    p.add_argument("--gas-prices", default="1uaxm")
    a = p.parse_args()

    if a.cmd == "submit" and not a.price:
        print("ОТКАЗ: submit без --price", file=sys.stderr)
        return 2
    if a.cmd in ("collect", "status", "cancel") and not a.job_id:
        print(f"ОТКАЗ: {a.cmd} без --job-id", file=sys.stderr)
        return 2

    try:
        params = chain.load_params(a.rest, a.contract)
    except chain.ChainUnavailable as e:
        print(f"ОТКАЗ: параметры из цепочки не получены: {e}", file=sys.stderr)
        return 1

    if a.send:
        if not a.key:
            print("ОТКАЗ: --send без --key: подписывать нечем", file=sys.stderr)
            return 2
        try:
            sender = tx.Axmd(a.contract, a.key, chain_id=a.chain_id,
                             node=a.node, home=a.home,
                             keyring=a.keyring_backend, binary=a.binary,
                             gas_prices=a.gas_prices)
            sender.address()
        except tx.TxFailed as e:
            print(f"ОТКАЗ: {e}", file=sys.stderr)
            return 2
    else:
        sender = tx.DryRun()

    try:
        return CMDS[a.cmd](a, params, sender)
    except chain.ChainUnavailable as e:
        print(f"ОТКАЗ: цепочка не ответила: {e}", file=sys.stderr)
        return 1
    except tx.TxFailed as e:
        print(f"ОТКАЗ: сделка не прошла: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
