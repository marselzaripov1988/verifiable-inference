#!/usr/bin/env python3
"""Вечерние часы сети: три узла на RTX 4090 с 19:00 до 21:00 по Москве.

Запускается по расписанию на сервере (cron, см. vecher.cron):

    vecher.py up              поднять узлы: запустить остановленные машины
                              vast.ai или арендовать новые, отвезти ключ,
                              запустить start.sh и дождаться петли
    vecher.py down            дождаться, пока незакрытые задания закроются
                              (не дольше drain_min), стереть ключи с машин
                              и ОСТАНОВИТЬ машины (диск с образом остаётся)
    vecher.py down --no-drain то же без ожидания — страховка в 21:30
    vecher.py status          что с машинами и залогами
    vecher.py stake           внести залог тем узлам, у которых его нет.
                              Подпись на каждый узел; вручную, не по cron

Только стандартная библиотека и CLI vastai. Ключи узлов лежат на сервере
(keys/nodeN.key, права 600) и уезжают на машину по stdin ssh — в
командной строке и в журнале их нет. При `down` ключ стирается с машины
ДО остановки: у остановленной машины диск остаётся у хозяина карты.

Почему так, а не иначе:
- Остановка, а не уничтожение: следующий запуск не качает образ заново.
  Если карту за это время заняли, машина не стартует — тогда она
  уничтожается и арендуется другая 4090 (не дороже max_dph).
- Залог не снимается: выключенный узел заданий не берёт, а брошенное
  задание контракт возвращает в очередь без наказания (release_job).
- Перед остановкой ждём незакрытые задания: исполнитель без проверяющих
  своё не получит, и заказчику вернут деньги только через час.
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

HOME = Path(os.environ.get("VECHER_HOME", "/opt/vecher"))
SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
    "-o", "ServerAliveInterval=30",
]
LOOP_MARK = "===== 8. петля"
_print_lock = threading.Lock()
_rent_lock = threading.Lock()


def log(name, msg):
    with _print_lock:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{name}] {msg}", flush=True)


# ---------------------------------------------------------------- настройки

def load_conf():
    conf = json.loads((HOME / "vecher.json").read_text(encoding="utf-8"))
    for k in ("contract", "image", "nodes", "offer_query"):
        if k not in conf:
            raise SystemExit(f"в vecher.json нет «{k}»")
    conf.setdefault("rest", "https://axm-lcd.trickle.pro")
    conf.setdefault("vastai", "vastai")
    conf.setdefault("max_dph", 0.6)
    conf.setdefault("disk", 60)
    conf.setdefault("start_wait_min", 6)
    conf.setdefault("rent_wait_min", 25)
    conf.setdefault("ready_wait_min", 25)
    conf.setdefault("drain_min", 10)
    conf.setdefault("rent_tries", 3)
    return conf


def load_state():
    p = HOME / "state.json"
    return json.loads(p.read_text()) if p.exists() else {}


_state_lock = threading.Lock()


def save_instance(name, inst_id):
    with _state_lock:
        st = load_state()
        if inst_id is None:
            st.pop(name, None)
        else:
            st[name] = {"instance": int(inst_id)}
        tmp = HOME / "state.json.tmp"
        tmp.write_text(json.dumps(st, indent=1))
        tmp.replace(HOME / "state.json")


def key_hex(path):
    """Ключ из файла: только шестнадцатеричные знаки, ровно 64.

    Файлы сохранялись в Windows: BOM, CR, UTF-16 — всё это не hex и
    отбрасывается. Неверная длина — отказ, а не попытка угадать.
    """
    raw = Path(path).read_bytes().decode("latin-1").replace("\x00", "")
    # Файл ключа — строки «имя=значение»; ключ в строке privkey_hex=.
    # Файл из одной строки hex тоже принимается.
    m = re.search(r"^\W*privkey_hex=(.*)$", raw, re.M)
    h = re.sub(r"[^0-9a-fA-F]", "", m.group(1) if m else raw)
    if len(h) != 64:
        raise RuntimeError(f"{path}: после очистки {len(h)} знаков, нужно 64")
    return h


# ------------------------------------------------------------------ vast.ai

def vast(conf, *args, timeout=120):
    """Вызов CLI. Возвращает (код, вывод)."""
    r = subprocess.run(
        [conf["vastai"], *map(str, args)], capture_output=True, text=True,
        timeout=timeout, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    return r.returncode, (r.stdout + r.stderr).strip()


def vast_json(conf, *args):
    code, out = vast(conf, *args, "--raw")
    try:
        return json.loads(out)
    except ValueError:
        raise RuntimeError(f"vastai {' '.join(map(str, args[:2]))}: {out[-300:]}")


def show(conf, inst_id):
    """Описание машины или None, если её больше нет."""
    code, out = vast(conf, "show", "instance", inst_id, "--raw")
    try:
        d = json.loads(out)
    except ValueError:
        return None
    if isinstance(d, dict) and d.get("id"):
        return d
    return None


def stale_image(inst, conf):
    """Образ машины, если он не тот, что в настройках, иначе None.

    Остановленная машина при запуске остаётся на образе, с которым её
    создали: сменить digest в настройках мало, прежние машины так и
    работали бы на старой сборке. Такую машину не запускают, а заменяют.
    """
    img = inst.get("image_uuid")
    if img and img != conf["image"]:
        return img
    return None


def host_port(inst, container_port):
    ports = inst.get("ports") or {}
    for m in ports.get(f"{container_port}/tcp") or []:
        if m.get("HostPort"):
            return int(m["HostPort"])
    return None


def addr(inst):
    """(ip, порт ssh, порт связок) прямого доступа или None."""
    ip = (inst.get("public_ipaddr") or "").strip()
    p22, p8081 = host_port(inst, 22), host_port(inst, 8081)
    if ip and p22 and p8081:
        return ip, p22, p8081
    return None


def wait_running(conf, name, inst_id, minutes):
    end = time.time() + minutes * 60
    last = None
    while time.time() < end:
        inst = show(conf, inst_id)
        if inst is None:
            log(name, f"машина {inst_id} исчезла")
            return None
        st = inst.get("actual_status")
        if st != last:
            log(name, f"машина {inst_id}: {st}")
            last = st
        if st == "running" and addr(inst):
            return inst
        time.sleep(15)
    log(name, f"машина {inst_id} не запустилась за {minutes} мин")
    return None


def destroy(conf, name, inst_id):
    code, out = vast(conf, "destroy", "instance", inst_id, "-y")
    log(name, f"уничтожена {inst_id}: {out[-120:]}")


def pick_offer(offers, max_dph, avoid_machines, avoid_offers):
    for o in offers:
        if o.get("dph_total", 99) > max_dph:
            return None  # отсортированы по цене: дальше только дороже
        if o.get("machine_id") in avoid_machines or o.get("id") in avoid_offers:
            continue
        return o
    return None


def rent(conf, name, avoid_machines, tried):
    with _rent_lock:
        offers = vast_json(conf, "search", "offers", conf["offer_query"],
                           "-o", "dph_total")
        o = pick_offer(offers, conf["max_dph"], avoid_machines, tried)
        if o is None:
            log(name, f"нет предложений 4090 не дороже ${conf['max_dph']}/ч")
            return None
        tried.add(o["id"])
        avoid_machines.add(o.get("machine_id"))
        log(name, f"арендую предложение {o['id']}: ${o['dph_total']:.3f}/ч, "
                  f"{o.get('geolocation', '?')}")
        d = vast_json(conf, "create", "instance", o["id"], "--image", conf["image"],
                      "--disk", conf["disk"], "--ssh", "--direct",
                      "--env", "-p 8081:8081", "--onstart-cmd", "sleep infinity")
    if not d.get("success") or not d.get("new_contract"):
        log(name, f"аренда не удалась: {json.dumps(d)[:200]}")
        return None
    inst_id = int(d["new_contract"])
    save_instance(name, inst_id)
    return inst_id


# ---------------------------------------------------------------------- ssh

def ssh(ip, port, cmd, stdin=None, timeout=120):
    r = subprocess.run(["ssh", *SSH_OPTS, "-p", str(port), f"root@{ip}", cmd],
                       input=stdin, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def wait_ssh(ip, port, minutes=5):
    end = time.time() + minutes * 60
    while time.time() < end:
        try:
            if ssh(ip, port, "true", timeout=30)[0] == 0:
                return True
        except subprocess.TimeoutExpired:
            pass
        time.sleep(10)
    return False


def launch_cmd(conf, endpoint):
    """Команда запуска на машине. Ключ приходит первой строкой stdin."""
    env = (f"CONTRACT={conf['contract']} ENDPOINT={endpoint} NODE_HOME=/root "
           f"VLLM_PORT=8000 BUNDLE_PORT=8081 PYTHONUNBUFFERED=1 "
           f"MAX_TOKENS={int(conf.get('max_tokens', 2048))}")
    return ("read -r KEY_HEX; KEY_HEX=$(printf %s \"$KEY_HEX\" | tr -cd 0-9a-fA-F); "
            f"export KEY_HEX {env}; cd /root; "
            "nohup setsid /opt/node/start.sh > /root/node.log 2>&1 < /dev/null & "
            "echo запущено")


# Скобки в шаблоне — чтобы pgrep не нашёл собственную оболочку ssh:
# в её командной строке стоит «[l]oop.py», а это под шаблон не подходит.
RUNNING = "pgrep -f '[l]oop.py' >/dev/null && echo да || echo нет"
WIPE = ("for p in $(pgrep -f '[l]oop.py|[v]llm serve|[s]erve_bundles|opt/node/[s]tart'); "
        "do kill $p 2>/dev/null; done; sleep 2; "
        "rm -f /root/sign.key; "
        "axmd keys delete node --keyring-backend test -y >/dev/null 2>&1; "
        "test -e /root/sign.key && echo ключ-остался || echo ключ-стёрт")


def node_up(conf, node, avoid_machines, tried, results):
    name = node["name"]
    try:
        key = key_hex(node["key"])
    except Exception as e:
        log(name, f"ОТКАЗ: {e}")
        results[name] = False
        return
    inst = None
    inst_id = (load_state().get(name) or {}).get("instance")
    if inst_id:
        cur = show(conf, inst_id)
        if cur is not None and stale_image(cur, conf):
            log(name, f"машина {inst_id} на прежнем образе "
                      f"{stale_image(cur, conf)[-12:]} — заменяю")
            destroy(conf, name, inst_id)
            save_instance(name, None)
        elif cur is None:
            log(name, f"прежней машины {inst_id} нет")
            save_instance(name, None)
        else:
            if cur.get("actual_status") != "running":
                code, out = vast(conf, "start", "instance", inst_id)
                log(name, f"запускаю остановленную {inst_id}: {out[-120:]}")
            inst = wait_running(conf, name, inst_id, conf["start_wait_min"])
            if inst is None:
                destroy(conf, name, inst_id)
                save_instance(name, None)
            else:
                avoid_machines.add(inst.get("machine_id"))
    # Машина, к которой не пускает ssh, бесполезна весь вечер, а деньги
    # за неё идут: её уничтожают и берут другую в счёт тех же попыток.
    # Её хост уже в avoid_machines — второй раз его не предложат.
    tries = 0
    while True:
        while inst is None and tries < conf["rent_tries"]:
            tries += 1
            new_id = rent(conf, name, avoid_machines, tried)
            if new_id is None:
                break
            inst = wait_running(conf, name, new_id, conf["rent_wait_min"])
            if inst is None:
                destroy(conf, name, new_id)
                save_instance(name, None)
        if inst is None:
            log(name, "ОТКАЗ: машины нет")
            results[name] = False
            return
        ip, p22, p8081 = addr(inst)
        if wait_ssh(ip, p22):
            break
        log(name, f"ssh {ip}:{p22} не пускает — уничтожаю и беру другую")
        destroy(conf, name, inst["id"])
        save_instance(name, None)
        inst = None

    endpoint = f"http://{ip}:{p8081}"
    if ssh(ip, p22, RUNNING)[1].endswith("да"):
        log(name, "петля уже работает — не трогаю")
        results[name] = True
        return
    code, out = ssh(ip, p22, launch_cmd(conf, endpoint), stdin=key + "\n")
    key = None
    log(name, f"start.sh: {out[-80:]}; адрес доставки {endpoint}")
    end = time.time() + conf["ready_wait_min"] * 60
    while time.time() < end:
        time.sleep(20)
        try:
            code, out = ssh(ip, p22,
                            f"grep -q '{LOOP_MARK}' /root/node.log && echo петля; "
                            "grep -m1 'ОТКАЗ' /root/node.log; "
                            "grep '^=====' /root/node.log | tail -1", timeout=60)
        except subprocess.TimeoutExpired:
            continue
        if "ОТКАЗ" in out:
            log(name, f"ОТКАЗ на машине: {out[-300:]}")
            results[name] = False
            return
        if out.startswith("петля"):
            log(name, "узел работает")
            results[name] = ensure_conformance(conf, name, ip, p22)
            return
    log(name, "ОТКАЗ: петля не поднялась вовремя, см. /root/node.log на машине")
    results[name] = False


#: Сдать пробу, если в цепочке её нет или она к старой версии эталона.
#: start.sh пробу СЧИТАЕТ (/root/conf.json), но не сдаёт: сдача — сделка,
#: а при каждом запуске она не нужна. Петля в это время шлёт свои сделки,
#: отсюда повторы при несовпавшем номере последовательности.
CONF_SH = r"""set -u
C={contract}; N="--node {rpc}"
A=$(axmd keys show node -a --keyring-backend test)
need() {{ python3 - "$1" "$2" <<'PY'
import json, sys
e = json.loads(sys.argv[1])["data"]; c = json.loads(sys.argv[2])["data"]
p = e.get("conformance") or {{}}
print("да" if p.get("ref_version") != c["conformance_version"] else "нет")
PY
}}
for i in 1 2 3 4; do
  E=$(axmd q wasm contract-state smart $C "{{\"executor\":{{\"address\":\"$A\"}}}}" $N -o json)
  G=$(axmd q wasm contract-state smart $C '{{"config":{{}}}}' $N -o json)
  [ "$(need "$E" "$G")" = нет ] && {{ echo "проба в цепочке"; exit 0; }}
  [ -s /root/conf.json ] || {{ echo "ОТКАЗ: нет /root/conf.json"; exit 1; }}
  out=$(axmd tx wasm execute $C "$(cat /root/conf.json)" --from node --keyring-backend test $N --chain-id axiome-1 --gas auto --gas-adjustment 1.3 --gas-prices 0.0025uaxm -y -o json 2>&1 | grep '^{{')
  echo "попытка $i: $(echo "$out" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["code"],d["txhash"][:10],d.get("raw_log","")[:120])' 2>/dev/null)"
  sleep 12
done
echo "ОТКАЗ: проба не сдана"; exit 1
"""


def ensure_conformance(conf, name, ip, p22):
    sh = CONF_SH.format(contract=conf["contract"],
                        rpc=conf.get("rpc", "https://api-chain.axiomechain.org"))
    try:
        code, out = ssh(ip, p22, "bash -s", stdin=sh, timeout=240)
    except subprocess.TimeoutExpired:
        log(name, "ОТКАЗ: сдача пробы не ответила")
        return False
    log(name, out.splitlines()[-1] if out else f"проба: код {code}")
    return code == 0


def cmd_up(conf):
    avoid, tried, results = set(), set(), {}
    threads = [threading.Thread(target=node_up, args=(conf, n, avoid, tried, results))
               for n in conf["nodes"]]
    for t in threads:
        t.start()
        time.sleep(3)  # аренда под замком, но вывод так читается легче
    for t in threads:
        t.join()
    ok = sum(results.values())
    try:
        need = int(smart(conf, {"config": {}})["checks_required"]) + 1
    except Exception:
        need = 3
    log("итог", f"работает {ok} из {len(conf['nodes'])}"
                + ("" if ok >= need else
                   f" — меньше {need}: ответы закроются, только если "
                   f"подключены чужие узлы"))
    return 0 if ok == len(conf["nodes"]) else 1


# ------------------------------------------------------------------ цепочка

def smart(conf, query):
    q = base64.b64encode(json.dumps(query).encode()).decode()
    url = f"{conf['rest'].rstrip('/')}/cosmwasm/wasm/v1/contract/{conf['contract']}/smart/{q}"
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read())["data"]


def unfinished(jobs):
    """Номера заданий, которые остановка оставила бы незакрытыми.

    Взятое, но без ответа, и отвеченное, но не оплаченное. Открытое не
    в счёт: его никто не взял, оно дождётся следующего вечера.
    """
    out = []
    for rec in jobs:
        j = rec.get("job") or {}
        st = str(j.get("state", "")).lower()
        if st == "claimed" or (st == "answered" and not j.get("paid")):
            out.append(rec.get("job_id"))
    return out


def recent_jobs(conf, n=30):
    nxt = int(smart(conf, {"next_job": {}})["next_job"])
    q = {"jobs": {"limit": n}}
    if nxt - n - 1 > 0:
        q["jobs"]["start_after"] = nxt - n - 1
    return smart(conf, q)["jobs"]


def drain(conf):
    end = time.time() + conf["drain_min"] * 60
    while True:
        try:
            left = unfinished(recent_jobs(conf))
        except Exception as e:
            log("сток", f"цепочка не ответила: {e}")
            left = ["?"]
        if not left:
            log("сток", "незакрытых заданий нет")
            return
        if time.time() > end:
            log("сток", f"не дождались: {left}. Взятые вернутся в очередь через "
                        "10 мин, неоплаченные — заказчику через час")
            return
        log("сток", f"ждём закрытия: {left}")
        time.sleep(20)


def node_down(conf, node):
    name = node["name"]
    inst_id = (load_state().get(name) or {}).get("instance")
    if not inst_id:
        log(name, "машины в записи нет")
        return
    inst = show(conf, inst_id)
    if inst is None:
        log(name, f"машины {inst_id} уже нет")
        save_instance(name, None)
        return
    if inst.get("actual_status") == "running" and addr(inst):
        ip, p22, _ = addr(inst)
        try:
            code, out = ssh(ip, p22, WIPE, timeout=90)
            log(name, out[-40:] or f"ssh код {code}")
        except subprocess.TimeoutExpired:
            log(name, "ssh не ответил — ключ на машине мог остаться")
    code, out = vast(conf, "stop", "instance", inst_id)
    log(name, f"остановка {inst_id}: {out[-120:]}")


def cmd_down(conf, no_drain):
    if not no_drain:
        drain(conf)
    threads = [threading.Thread(target=node_down, args=(conf, n)) for n in conf["nodes"]]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return 0


def cmd_status(conf):
    st = load_state()
    for n in conf["nodes"]:
        inst_id = (st.get(n["name"]) or {}).get("instance")
        inst = show(conf, inst_id) if inst_id else None
        if inst is None:
            print(f"{n['name']}: машины нет")
            continue
        a = addr(inst)
        print(f"{n['name']}: {inst_id} {inst.get('actual_status')} "
              f"${inst.get('dph_total', 0):.3f}/ч {a[0] + ':' + str(a[1]) if a else ''}")
    try:
        left = unfinished(recent_jobs(conf))
        print("незакрытые задания:", left or "нет")
    except Exception as e:
        print("цепочка не ответила:", e)
    return 0


STAKE_SH = r"""set -eu
T={contract}
B={bartr}
N="--node {rpc}"; K="--keyring-backend test --home /root/.axmd"
A=$(axmd keys show node -a $K)
S=$(axmd q wasm contract-state smart $T "{{\"executor\":{{\"address\":\"$A\"}}}}" $N -o json | python3 -c 'import json,sys;print(json.load(sys.stdin)["data"]["stake"])')
if [ "$S" -ge {amount} ]; then echo "$A залог уже $S"; exit 0; fi
HOOK=$(printf '{{"deposit_stake":{{}}}}' | base64 -w0)
h=$(axmd tx wasm execute $B "{{\"send\":{{\"contract\":\"$T\",\"amount\":\"{amount}\",\"msg\":\"$HOOK\"}}}}" --from node $K $N --chain-id axiome-1 --gas auto --gas-adjustment 1.3 --gas-prices 0.0025uaxm -y -o json 2>&1 | grep '^{{' | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["txhash"])')
for i in $(seq 1 30); do R=$(axmd q tx $h $N -o json 2>/dev/null) && break; sleep 3; done
echo "$R" | python3 -c 'import json,sys;d=json.load(sys.stdin);print("код",d["code"],d.get("raw_log","")[:150])'
axmd q wasm contract-state smart $T "{{\"executor\":{{\"address\":\"$A\"}}}}" $N -o json | python3 -c 'import json,sys;d=json.load(sys.stdin)["data"];print(d["status"],"залог",d["stake"])'
"""


def cmd_stake(conf):
    st = load_state()
    sh = STAKE_SH.format(contract=conf["contract"], bartr=conf["bartr"],
                         rpc=conf.get("rpc", "https://api-chain.axiomechain.org"),
                         amount=int(conf.get("stake", 300000)))
    for n in conf["nodes"]:
        inst_id = (st.get(n["name"]) or {}).get("instance")
        inst = show(conf, inst_id) if inst_id else None
        if not inst or not addr(inst):
            log(n["name"], "машина не запущена — сначала up")
            continue
        ip, p22, _ = addr(inst)
        code, out = ssh(ip, p22, "bash -s", stdin=sh, timeout=240)
        log(n["name"], out[-300:])
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("what", choices=("up", "down", "status", "stake"))
    p.add_argument("--no-drain", action="store_true")
    a = p.parse_args(argv)
    conf = load_conf()
    log("vecher", a.what + (" --no-drain" if a.no_drain else ""))
    if a.what == "up":
        return cmd_up(conf)
    if a.what == "down":
        return cmd_down(conf, a.no_drain)
    if a.what == "stake":
        return cmd_stake(conf)
    return cmd_status(conf)


if __name__ == "__main__":
    sys.exit(main())
