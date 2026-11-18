#!/usr/bin/env python3
"""Сторож машин vast.ai: пишет админам бота в Telegram, что с машинами.

Запускается cron'ом раз в 5 минут (storozh.cron) и смотрит ВСЕ машины
аккаунта vast.ai — и узлы расписания, и арендованные вручную (чат).

О чём он пишет:
- ▶️ машина поднялась; для машины с веб-чатом — когда чат ответил;
- ⏹ машина остановилась или исчезла: сколько работала и сколько стоила;
- по расписанию (время московское, из storozh в vecher.json):
  в up_check — поднялись ли все узлы (✅ или ⚠️ с перечнем),
  в down_check — остановились ли (✅ или ⚠️ «не остановилась»);
- ⚠️ веб-чат не ответил за ready_min минут после запуска;
- ⏰ машина работает дольше long_hours часов — и повторяет каждые
  repeat_min минут, пока она работает.

Только стандартная библиотека; vast.ai и Telegram — через vecher.py.
Своё состояние — storozh.json рядом с vecher.json: первый запуск
только запоминает машины и ничего не шлёт.
"""

import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import vecher

MSK = timezone(timedelta(hours=3))
DEFAULTS = {
    "up_check": "19:05",      # к этому времени узлы должны работать
    "down_check": "20:40",    # к этому — быть уничтожены
    "long_hours": 3,
    "repeat_min": 60,
    "ready_min": 40,          # сколько ждать ответа веб-чата
    "chat_port": 8080,        # порт Open WebUI внутри машины
}


def settings(conf):
    s = dict(DEFAULTS)
    s.update(conf.get("storozh") or {})
    return s


def state_path():
    return vecher.HOME / "storozh.json"


def load():
    p = state_path()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def save(st):
    tmp = state_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(state_path())


# ------------------------------------------------------------------ машины

def instances(conf):
    """Все машины аккаунта: {id(str): описание}. Ошибка vast.ai — исключение."""
    d = vecher.vast_json(conf, "show", "instances")
    if isinstance(d, dict):
        d = d.get("instances", [])
    return {str(i["id"]): i for i in d if i.get("id")}


def node_names(conf):
    """{id машины: имя узла} по state.json расписания."""
    return {str(v["instance"]): k for k, v in vecher.load_state().items()
            if isinstance(v, dict) and v.get("instance")}


def is_node(inst, conf, nodes):
    return str(inst["id"]) in nodes or inst.get("image_uuid") == conf.get("image")


def name_of(inst, nodes):
    i = str(inst["id"])
    if i in nodes:
        return f"{nodes[i]} ({i})"
    return f"{inst.get('label') or 'машина'} ({i})"


def hours_up(inst, now):
    t = inst.get("start_date")
    return max(0.0, (now - float(t)) / 3600) if t else 0.0


def cost(inst, hours):
    return float(inst.get("dph_total") or 0) * hours


def chat_url(inst, s):
    ip = (inst.get("public_ipaddr") or "").strip()
    port = vecher.host_port(inst, s["chat_port"])
    return f"http://{ip}:{port}" if ip and port else None


def chat_ok(url):
    try:
        with urllib.request.urlopen(url + "/health", timeout=10) as r:
            return r.status == 200
    except Exception:
        return False


def has_chat(inst, s):
    return vecher.host_port(inst, s["chat_port"]) is not None


# ------------------------------------------------------------------ проход

def today_at(now_msk, hhmm):
    h, m = map(int, hhmm.split(":"))
    return now_msk.replace(hour=h, minute=m, second=0, microsecond=0)


def tick(conf, insts, st, now, chat_check=chat_ok):
    """Один проход. Возвращает (новое состояние, список сообщений)."""
    s = settings(conf)
    nodes = node_names(conf)
    msgs = []
    first = st is None
    st = st or {"seen": {}, "done": {}}
    seen = st["seen"]
    now_msk = datetime.fromtimestamp(now, MSK)
    day = now_msk.strftime("%Y-%m-%d")

    # 1. появились, поднялись, остановились
    for i, inst in insts.items():
        run = inst.get("actual_status") == "running"
        prev = seen.get(i)
        rec = prev or {"name": name_of(inst, nodes), "since": now,
                       "running": False, "ready": False,
                       "dph": inst.get("dph_total") or 0}
        rec["name"] = name_of(inst, nodes)
        rec["dph"] = inst.get("dph_total") or rec.get("dph", 0)
        rec["start"] = inst.get("start_date") or rec.get("start")
        if run and not rec["running"]:
            rec["running"], rec["up_at"] = True, now
            if not first:
                msgs.append(f"▶️ Поднялась {rec['name']}, ${rec['dph']:.3f}/ч")
        elif not run and rec["running"]:
            rec["running"] = False
            if not first:
                h = hours_up(inst, now)
                msgs.append(f"⏹ Остановилась {rec['name']} (статус "
                            f"{inst.get('actual_status')}), работала {h:.1f} ч, "
                            f"≈${cost(inst, h):.2f}. Диск остановленной машины "
                            f"оплачивается — уничтожьте, если не нужна")
        # веб-чат: ответил / не ответил
        if run and has_chat(inst, s) and not is_node(inst, conf, nodes) \
                and not rec.get("ready"):
            url = chat_url(inst, s)
            if url and chat_check(url):
                rec["ready"] = True
                if not first:
                    msgs.append(f"✅ Чат отвечает: {url} ({rec['name']})")
            elif not rec.get("not_ready_told") and \
                    now - rec.get("up_at", now) > s["ready_min"] * 60:
                rec["not_ready_told"] = True
                msgs.append(f"⚠️ {rec['name']} работает {s['ready_min']} мин, "
                            f"а чат {url or ''} не отвечает — модель не поднялась?")
        # долгая работа
        if run:
            h = hours_up(inst, now)
            last = rec.get("long_told", 0)
            if h >= s["long_hours"] and now - last >= s["repeat_min"] * 60 - 30:
                rec["long_told"] = now
                msgs.append(f"⏰ {rec['name']} работает уже {h:.1f} ч, "
                            f"≈${cost(inst, h):.2f} (${rec['dph']:.3f}/ч)")
        seen[i] = rec

    for i in list(seen):
        if i not in insts:
            rec = seen.pop(i)
            if not first:
                h = (now - float(rec["start"])) / 3600 if rec.get("start") else 0
                msgs.append(f"⏹ Уничтожена {rec['name']}, работала {h:.1f} ч, "
                            f"≈${float(rec.get('dph') or 0) * h:.2f}")

    # 2. расписание узлов
    done = st["done"]
    up_t, down_t = today_at(now_msk, s["up_check"]), today_at(now_msk, s["down_check"])
    if up_t <= now_msk < down_t and done.get("up") != day:
        done["up"] = day
        want = [n["name"] for n in conf.get("nodes", [])]
        ok = [nodes[i] for i, inst in insts.items()
              if i in nodes and inst.get("actual_status") == "running"]
        miss = [n for n in want if n not in ok]
        msgs.append(f"✅ По расписанию поднялись все узлы: {len(ok)} из {len(want)}"
                    if not miss else
                    f"⚠️ По расписанию поднялись {len(ok)} из {len(want)}, "
                    f"нет: {', '.join(miss)}. Журнал: /var/log/vecher.log")
    if now_msk >= down_t and done.get("down") != day:
        done["down"] = day
        left = [name_of(inst, nodes) + f" — {inst.get('actual_status')}"
                for inst in insts.values() if is_node(inst, conf, nodes)]
        msgs.append("✅ Узлы по расписанию остановлены" if not left else
                    "⚠️ Узлы не остановились по расписанию: " + "; ".join(left)
                    + ". Проверьте vecher.py down")
    if first:
        msgs = []
    return st, msgs


def main(argv=None):
    conf = vecher.load_conf()
    try:
        insts = instances(conf)
    except Exception as e:
        vecher.log("сторож", f"vast.ai не ответил: {e}")
        return 1
    st, msgs = tick(conf, insts, load(), time.time())
    for m in msgs:
        vecher.log("сторож", m)
        vecher.notify(conf, m)
    save(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
