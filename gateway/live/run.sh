#!/bin/bash
# Шлюз против НАСТОЯЩЕЙ модели и НАСТОЯЩЕЙ цепочки.
#
# Подставлен ровно один шаг, и он помечен: платёж. Своего адреса казны с
# AXM нет, посторонний плательщик тоже не из воздуха. Остальное живое:
# модель на карте, узел axiome-1 из реестра цепочек, учёт на диске.
#
# У каждого шага положительный признак, а не отсутствие ошибки.
set -u
cd /root/crypto
RPC=https://api-chain.axiomechain.org
# Адрес ТОЛЬКО на чтение: чужой адрес с настоящими платежами, нужен
# затем, чтобы приёмник работал против живой цепочки, а не подставной.
TREASURY=axm1l0a927clmz7wqmls2737ctwlxzhxeqmxaf55w7
FROM_HEIGHT=14400000
PRICE=1000000          # 1 AXM за миллион токенов — число для прогона, не курс
LEDGER=/root/live-ledger.json
BASE=http://127.0.0.1:8000/v1
PORT=18080
rm -f "$LEDGER" /root/gw.log

say() { echo; echo "===== $* ====="; }
fail() { echo "ПРОВАЛ: $*" >&2; exit 1; }

# Шаблон в скобках обязателен: иначе pkill -f находит собственную
# командную строку и убивает эту же оболочку.
stop_gw() { pkill -f "[s]erve.py --chain" 2>/dev/null; sleep 2; }
# Шлюз снимается при любом выходе, включая провал: оставленный процесс
# продолжает слушать порт и портит следующий прогон.
trap stop_gw EXIT

start_gw() {
    # Сначала убрать прежний. Иначе новый процесс падает на занятом
    # порту, а /healthz отвечает СТАРЫЙ — и признак успеха оказывается
    # выполнен мёртвым прогоном. Ровно так здесь уже выглядели
    # успешными две пустые репетиции отката.
    stop_gw
    for i in $(seq 1 15); do
        ss -ltn 2>/dev/null | grep -q ":$PORT " || break
        sleep 1
    done
    ss -ltn 2>/dev/null | grep -q ":$PORT " && fail "порт $PORT занят"
    # -u обязателен: вывод в файл буферизуется, сервер работает дальше,
    # и признак успеха остался бы в буфере непрочитанным.
    nohup python3 -u gateway/scripts/serve.py \
        --chain axiome --rpc "$RPC" --treasury "$TREASURY" --price "$PRICE" \
        --base-url "$BASE" --ledger "$LEDGER" --port "$PORT" \
        --from-height "$FROM_HEIGHT" --poll 3600 >> /root/gw.log 2>&1 &
    for i in $(seq 1 60); do
        sleep 2
        curl -s -m 5 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && return 0
    done
    tail -20 /root/gw.log
    fail "шлюз не поднялся"
}

say "1. узел проверяется ДО приёма запросов"
start_gw
grep -q "узел годен" /root/gw.log || { tail -10 /root/gw.log; fail "узел не признан годным"; }
grep "узел годен" /root/gw.log

say "2. шлюз прочитал НАСТОЯЩУЮ цепочку"
curl -s -m 15 "http://127.0.0.1:$PORT/healthz" > /root/h.json
python3 - <<'PY' || fail "состояние шлюза не то"
import json
d = json.load(open("/root/h.json", encoding="utf-8"))
assert d["poll_error"] is None, f'ошибка чтения платежей: {d["poll_error"]}'
# Высота двигается только когда платёж УВИДЕН. Значит это признак того,
# что приёмник читал цепочку, а не молчал в ответ.
assert d["height"] >= 14_400_000, f'платежей не увидено, высота {d["height"]}'
assert d["unclaimed"] > 0, "настоящих платежей не увидено вовсе"
print(f'  высота по последнему увиденному платежу: {d["height"]}')
print(f'  версия узла модели: {d["node_version"]}')
print(f'  НАСТОЯЩИХ платежей увидено: {d["unclaimed"]} — у них нет нашего кода')
print('  в памятке, поэтому они ушли в неразобранные, а не на счёт')
PY

say "3. счёт открывается, оплата объясняется"
curl -s -m 10 -XPOST "http://127.0.0.1:$PORT/v1/account" > /root/acc.json
python3 - <<'PY' || fail "счёт не открыт"
import json
d = json.load(open("/root/acc.json", encoding="utf-8"))
assert d["code"].startswith("ag-"), d
print("  код счёта:", d["code"])
print("  ", d["как_платить"])
open("/root/key", "w").write(d["api_key"])
open("/root/code", "w").write(d["code"])
PY
KEY=$(cat /root/key); CODE=$(cat /root/code)

say "4. без денег ответа нет"
C=$(curl -s -m 30 -o /root/r402 -w "%{http_code}" -XPOST "http://127.0.0.1:$PORT/v1/completions" \
    -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
    -d '{"prompt":"Столица Франции? Ответь одним предложением.","max_tokens":64}')
[ "$C" = "402" ] || { cat /root/r402; fail "ожидался 402, вышло $C"; }
echo "  402: $(cat /root/r402)"

say "5. ПОДСТАВНОЙ ШАГ: платёж (своей казны с AXM нет)"
stop_gw
CODE="$CODE" LEDGER="$LEDGER" python3 - <<'PY' || fail "подставной платёж не зачислен"
import os, sys
sys.path.insert(0, "/root/crypto/gateway")
from artery_gateway.ledger import Ledger
l = Ledger(os.environ["LEDGER"])
a = l.credit("ПОДСТАВНОЙ-ПЛАТЁЖ-1", 14_440_600, "axm1плательщик", 2_000_000,
             f'оплата инференса {os.environ["CODE"]}')
assert a is not None, "платёж не зачтён"
print(f"  зачислено {a.balance} uaxm на счёт {a.code}")
PY
start_gw
curl -s -m 10 "http://127.0.0.1:$PORT/v1/account" -H "X-API-Key: $KEY" > /root/bal.json
python3 - <<'PY' || fail "остаток не сохранился"
import json
d = json.load(open("/root/bal.json", encoding="utf-8"))
assert d["balance_micro"] == 2_000_000, d
print(f'  остаток после ПЕРЕЗАПУСКА шлюза: {d["balance_micro"]} uaxm —')
print('  учёт лежит на диске и переживает перезапуск')
PY

say "6. ОПЛАЧЕННЫЙ ЗАПРОС К НАСТОЯЩЕЙ МОДЕЛИ"
curl -s -m 300 -XPOST "http://127.0.0.1:$PORT/v1/completions" \
    -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
    -d '{"prompt":"Столица Франции? Ответь одним предложением.","max_tokens":64}' \
    > /root/ans1.json
python3 - <<'PY' || fail "оплаченный запрос не прошёл"
import json
d = json.load(open("/root/ans1.json", encoding="utf-8"))
assert "answer" in d, d
u = d["usage"]
print("  ответ модели:", repr(d["answer"]))
print(f'  токенов: запрос {u["prompt_tokens"]}, ответ {u["answer_tokens"]}, '
      f'всего {u["total_tokens"]}')
print(f'  зарезервировано {d["reserved_micro"]}, списано {d["charged_micro"]}, '
      f'остаток {d["balance_micro"]}')
assert u["total_tokens"] == u["prompt_tokens"] + u["answer_tokens"]
assert d["charged_micro"] <= d["reserved_micro"], "списано больше резерва"
assert d["balance_micro"] == 2_000_000 - d["charged_micro"], "остаток не сходится"
print("  списано по ДЕЙСТВИТЕЛЬНОМУ расходу, а не по резерву — сошлось")
PY

say "7. тот же запрос дважды — ответ побитово тот же"
curl -s -m 300 -XPOST "http://127.0.0.1:$PORT/v1/completions" \
    -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
    -d '{"prompt":"Столица Франции? Ответь одним предложением.","max_tokens":64}' \
    > /root/ans2.json
python3 - <<'PY' || fail "ответы разошлись"
import json
a = json.load(open("/root/ans1.json", encoding="utf-8"))["answer"]
b = json.load(open("/root/ans2.json", encoding="utf-8"))["answer"]
print("  побитово одинаково:", a == b)
assert a == b, "узел не повторяет сам себя — порог 0,0360 для него недействителен"
PY

say "8. длинный запрос: резерв по max_tokens, списание по факту"
curl -s -m 300 -XPOST "http://127.0.0.1:$PORT/v1/completions" \
    -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
    -d '{"prompt":"Перечисли по шагам, что происходит при отправке транзакции в блокчейне. Отвечай подробно.","max_tokens":200}' \
    > /root/ans3.json
python3 - <<'PY' || fail "длинный запрос не прошёл"
import json
d = json.load(open("/root/ans3.json", encoding="utf-8"))
u = d["usage"]
print(f'  токенов: запрос {u["prompt_tokens"]}, ответ {u["answer_tokens"]}, '
      f'всего {u["total_tokens"]}')
print(f'  зарезервировано {d["reserved_micro"]} (по max_tokens), '
      f'списано {d["charged_micro"]}')
assert d["charged_micro"] <= d["reserved_micro"], "списано больше резерва"
if u["answer_tokens"] >= 200:
    assert d["charged_micro"] == d["reserved_micro"]
    print("  ответ занял max_tokens целиком: резерв и списание совпали —")
    print("  это верхняя граница, больше резерва не списывается никогда")
else:
    assert d["reserved_micro"] > d["charged_micro"]
    print("  резерв больше списанного — ответ в долг невозможен")
PY

say "9. расход учтён, резервов не висит"
python3 - <<'PY' || fail "расход не учтён"
import json, urllib.request
key = open("/root/key").read()
req = urllib.request.Request("http://127.0.0.1:18080/v1/account",
                             headers={"X-API-Key": key})
d = json.loads(urllib.request.urlopen(req, timeout=10).read())
print(f'  потрачено {d["spent_micro"]} uaxm за {d["tokens"]} токенов, '
      f'остаток {d["balance_micro"]}')
assert d["spent_micro"] > 0 and d["tokens"] > 0
assert d["reserved_micro"] == 0, "резерв не снят"
print("  резервов не висит")
PY

say "ГОТОВО: шлюз отработал против настоящей модели и настоящей цепочки"
