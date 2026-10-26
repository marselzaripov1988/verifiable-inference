#!/usr/bin/env python3
"""Сроки заданий: что уже пора закрыть.

Зачем отдельный демон. Сроки в контракте **никто не исполняет сам**:
`release_job` и `expire_checks` вызывает кто угодно — нарочно, чтобы
право не досталось админу и не стало тем же усмотрением, что убранный
`SetSuspended`, только с таймером. Но «кто угодно» означает, что вызвать
должен кто-то. Пока этого не делает ни один узел, механизм записан в
коде и не работает: брошенные задания так и висят, а деньги заказчика
так и лежат в контракте.

Этот демон обходит задания и печатает сообщения, которые пора отправить.
Отправку, как и остальные демоны, он не делает: печатает готовый JSON, а
вызывающий отправляет `axmd tx wasm execute`. Так он остаётся
проверяемым без живой цепочки.

Выгоды от вызова нет никакой — ни платы, ни доли. Звать его стоит по
двум причинам: заказчику он возвращает деньги, а узлам возвращает в
оборот задания, которые иначе заперты за молчащим исполнителем. Поэтому
естественное место — у того, кто держит узел, рядом с проверяющим.

    python3 deadlines.py --rest https://axm-lcd.trickle.pro \\
        --contract axm1… --from-id 1 --to-id 50

Время берётся У ЦЕПОЧКИ, а не с местных часов: срок проверяет контракт
по времени блока, и ушедшие часы узла дали бы либо напрасный вызов, либо
пропущенный срок.
"""

import argparse
import json
import sys

import chain


def scan(rest, contract, params, now, ids):
    """Пройти задания и собрать, что по ним пора.

    Задания, которых нет, пропускаются молча: нумерация сплошная, но
    верхнюю границу вызывающий задаёт с запасом, и отсутствие задания —
    это конец списка, а не ошибка.
    """
    rows = []
    for job_id in ids:
        try:
            job = chain.load_job(rest, contract, job_id)
        except chain.ChainUnavailable:
            continue
        if not isinstance(job, dict) or "state" not in job:
            continue
        d = chain.deadline_of(job, params, now)
        checks = None
        # Число проверок нужно только для `answered`: при собранном
        # кворуме закрывать надо вердиктом, и сроком контракт не даст.
        if d["phase"] == "answered":
            try:
                checks = chain.load_checks(rest, contract, job_id)
            except chain.ChainUnavailable:
                checks = None
        msg = chain.due_message(job_id, job, params, now, checks)
        rows.append({
            "job_id": job_id,
            "state": d["phase"],
            "left": d["left"],
            "expired": d["expired"],
            "checks": checks,
            "msg": msg,
        })
    return rows


def describe(row, params):
    """Строка для человека. Нужна затем, что по одному JSON не видно,
    почему срок вышел, а сообщения нет."""
    j, st, left = row["job_id"], row["state"], row["left"]
    if left is None:
        why = {"open": "никем не взято, сроком не ограничено",
               "settled": "закрыто"}.get(st, "сроком не ограничено")
        return f"{j}: {st} — {why}"
    if not row["expired"]:
        return f"{j}: {st} — осталось {left} с"
    if row["msg"]:
        return f"{j}: {st} — срок вышел {-left} с назад, пора закрывать"
    if st == "answered" and row["checks"] is None:
        return (f"{j}: {st} — срок вышел {-left} с назад, но число проверок "
                f"не прочитано: без него закрывать нельзя")
    if st == "answered":
        return (f"{j}: {st} — срок вышел {-left} с назад, но проверок уже "
                f"{row['checks']} из {params.checks_required}: закрывать "
                f"надо finalize, а не сроком")
    return f"{j}: {st} — срок вышел {-left} с назад"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--rest", required=True)
    p.add_argument("--contract", required=True)
    p.add_argument("--from-id", type=int, default=1)
    p.add_argument("--to-id", type=int, required=True)
    a = p.parse_args()

    try:
        params = chain.load_params(a.rest, a.contract)
        now = chain.chain_time(a.rest)
    except chain.ChainUnavailable as e:
        print(f"ОТКАЗ: {e}", file=sys.stderr)
        return 1
    print(f"время цепочки {now}, сроки {params.claim_ttl_seconds}/"
          f"{params.check_ttl_seconds} с", file=sys.stderr)

    rows = scan(a.rest, a.contract, params, now,
                range(a.from_id, a.to_id + 1))
    for r in rows:
        print(describe(r, params), file=sys.stderr)
    due = [r["msg"] for r in rows if r["msg"]]
    print(f"заданий просмотрено {len(rows)}, пора закрыть {len(due)}",
          file=sys.stderr)
    for m in due:
        print(json.dumps(m, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
