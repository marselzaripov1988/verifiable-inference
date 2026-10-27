#!/bin/sh
# ПРОТОКОЛ ЦЕЛИКОМ НА НАСТОЯЩЕЙ ЦЕПОЧКЕ, без человека в середине.
#
# Заказчик ставит задание своим же кодом (client.py), три узла работают
# своими петлями (loop.py), заказчик забирает ответ и сверяет его с
# цепочкой. Сценарий после запуска не правит никем и ничем.
#
# Чего не проверяют подставные тесты. Там подменена цепочка, и проверяется
# решение петли по снимку. Здесь цепочка настоящая — wasmd 0.50.0 с
# libwasmvm 1.5.0, та же пара, что у Axiome, — и проверяется то, ради чего
# петля написана: что задание проходит путь от объявления до вердикта САМО,
# без человека с командной строкой.
#
# Три узла запускаются демонами и дальше не трогаются вовсе. Всё, что
# происходит после запуска, — их собственные решения: кто успел взять
# задание, кто отдал связку, кто сдал проверку, кто свёл вердикт.
#
# Инференса здесь нет: вместо vLLM подставной сервер, у которого ответ и
# вероятности — функции текста (fake-vllm.py). Детерминизм карт замерен
# отдельно и к петле отношения не имеет; смешивать два вопроса в одном
# прогоне значило бы не проверить ни одного.
#
# У каждого шага положительный признак. Отсутствие ошибки признаком НЕ
# считается — на этом в этом проекте горели дважды.
set -eu

CH=devnet-1
D=/root/.wasmd
B="wasmd --home $D"
# Цена газа НОЛЬ, и это не лень: иначе комиссии узлов примешиваются к
# выплатам, и сверить деньги по балансам нельзя. Минимальная цена в
# app.toml тоже ноль.
TX="--chain-id $CH --keyring-backend test --home $D --gas 3000000 --fees 0stake -y -o json"
Q="--home $D -o json"
NODES="n1 n2 n3"
MIN_STAKE=50000000
PRICE=1000000
BPS=970
FEE=$((PRICE * BPS / 10000))
TOTAL=$((PRICE + FEE))
SHARE=$((FEE / 2))
#: Эталон пробы на девнете не поверяется: предел нулевой, и сверка идёт
#: по числу, а не по векторам. Раскрытие эталона — отдельный прогон
#: (cycle.sh), и смешивать его с петлёй значило бы не проверить ни того,
#: ни другого.
ZEROS=0000000000000000000000000000000000000000000000000000000000000000
AAAAS=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa

say() { echo; echo "===== $* ====="; }
fail() { echo "ПРОВАЛ: $*" >&2; exit 1; }
ok() { echo "ок: $*"; }

command -v jq >/dev/null 2>&1 || fail "нет jq"
command -v python3 >/dev/null 2>&1 || fail "нет python3"

wait_tx() {
    h=$1; i=0
    while [ $i -lt 40 ]; do
        sleep 1
        out=$($B query tx --type=hash "$h" $Q 2>/dev/null) || { i=$((i+1)); continue; }
        code=$(echo "$out" | jq -r '.code')
        [ "$code" = "0" ] && { echo "$out"; return 0; }
        echo "$out" | jq -r '.raw_log' >&2
        fail "транзакция $h вернула code=$code"
    done
    fail "транзакция $h не попала в блок"
}

send() {
    h=$($B tx "$@" $TX | jq -r '.txhash')
    [ -n "$h" ] && [ "$h" != "null" ] || fail "транзакция не отправлена"
    wait_tx "$h" >/dev/null
}

bal() {
    v=$($B query bank balances "$1" $Q | jq -r '.balances[]|select(.denom=="stake").amount')
    echo "${v:-0}"
}

#: Номер задания присваивает контракт, а не сценарий: заказчик узнаёт его
#: из события сделки. До постановки он неизвестен.
JOB=""

job_field() {
    $B query wasm contract-state smart "$REG" \
        "{\"job\":{\"job_id\":${JOB:-1}}}" $Q | jq -r ".data.$1"
}

checks_field() {
    $B query wasm contract-state smart "$REG" \
        "{\"checks\":{\"job_id\":${JOB:-1}}}" $Q | jq -r "$1"
}

say "1. поднять цепочку"
$B init devnet --chain-id $CH >/dev/null 2>&1
for n in admin client $NODES; do
    $B keys add $n --keyring-backend test --home $D >/dev/null 2>&1
done
sed -i 's/"ustake"/"stake"/g; s/"umlg"/"stake"/g' $D/config/genesis.json
for n in admin client $NODES; do
    a=$($B keys show $n -a --keyring-backend test --home $D)
    $B genesis add-genesis-account "$a" 100000000000stake --home $D >/dev/null 2>&1
done
$B genesis gentx admin 70000000000stake --chain-id $CH --keyring-backend test --home $D >/dev/null 2>&1
$B genesis collect-gentxs --home $D >/dev/null 2>&1
jq '.app_state.mint.minter.inflation="0.000000000000000000"
    | .app_state.mint.params.inflation_min="0.000000000000000000"
    | .app_state.mint.params.inflation_max="0.000000000000000000"
    | .app_state.distribution.params.community_tax="0.000000000000000000"' \
    $D/config/genesis.json > /tmp/g.json && mv /tmp/g.json $D/config/genesis.json
sed -i 's/^minimum-gas-prices = .*/minimum-gas-prices = "0stake"/' $D/config/app.toml
sed -i 's/^timeout_commit = .*/timeout_commit = "1s"/' $D/config/config.toml
# REST обязателен: петля читает цепочку через него, а не через RPC. По
# умолчанию он выключен. Правка только внутри раздела [api] — иначе
# попадёт в [grpc-web] и в телеметрию.
sed -i '/^\[api\]/,/^\[[a-z]/ s/^enable = false/enable = true/' $D/config/app.toml
sed -i '/^\[api\]/,/^\[[a-z]/ s|^address = .*|address = "tcp://127.0.0.1:1317"|' $D/config/app.toml
$B start --home $D >/tmp/node.log 2>&1 &
i=0
while [ $i -lt 60 ]; do
    sleep 1
    hh=$($B status --home $D 2>/dev/null | jq -r '.sync_info.latest_block_height' 2>/dev/null || echo "")
    [ -n "$hh" ] && [ "$hh" != "null" ] && [ "$hh" -gt 1 ] 2>/dev/null && break
    i=$((i+1))
done
[ -n "${hh:-}" ] && [ "$hh" -gt 1 ] 2>/dev/null || { tail -20 /tmp/node.log; fail "цепочка не поднялась"; }
# REST проверяется отдельным признаком: без него петля не прочитает ни
# настроек, ни заданий, а узнать об этом лучше здесь, чем из трёх
# одинаковых отказов в логах узлов.
i=0
while [ $i -lt 30 ]; do
    t=$(wget -qO- http://127.0.0.1:1317/cosmos/base/tendermint/v1beta1/blocks/latest 2>/dev/null \
        | jq -r '.block.header.time' 2>/dev/null || echo "")
    [ -n "$t" ] && [ "$t" != "null" ] && break
    sleep 1; i=$((i+1))
done
[ -n "${t:-}" ] && [ "$t" != "null" ] || { tail -20 /tmp/node.log; fail "REST не отвечает"; }
ok "высота $hh — цепочка идёт, REST отдаёт время $t"

ADMIN=$($B keys show admin -a --keyring-backend test --home $D)
CLIENT=$($B keys show client -a --keyring-backend test --home $D)

say "2. развернуть реестр"
send wasm store /work/inference_registry.wasm --from admin
CODE=$($B query wasm list-code $Q | jq -r '.code_infos[-1].code_id')
[ -n "$CODE" ] && [ "$CODE" != "null" ] || fail "контракт не загрузился"
# Расчёт РОДНОЙ монетой: ровно то, что собирает шлюз этапа 1. Предел
# пробы ноль — как в развёрнутом контракте.
send wasm instantiate "$CODE" "{\"admin\":\"$ADMIN\",\"checks_required\":2,\
\"pinned_model\":\"Qwen/Qwen2.5-7B-Instruct\",\"pinned_runtime\":\"vllm-0.31.0\",\
\"pinned_dtype\":\"bfloat16\",\"pinned_arch\":\"sm_89\",\
\"kernel_cache_hash\":\"devnet-cache\",\"conformance_limit_micro\":0,\
\"conformance_ref_hash\":\"$ZEROS\",\"conformance_lens\":[3,3],\
\"claim_ttl_seconds\":600,\"check_ttl_seconds\":3600,\
\"settlement\":{\"native\":\"stake\"},\"stake_denom\":\"stake\",\
\"min_stake\":\"$MIN_STAKE\",\"verifier_fee_bps\":$BPS,\
\"forfeit\":{\"to_address\":\"$ADMIN\"}}" --label registry --no-admin --from admin
REG=$($B query wasm list-contract-by-code "$CODE" $Q | jq -r '.contracts[0]')
cr=$($B query wasm contract-state smart "$REG" '{"config":{}}' $Q | jq -r '.data.checks_required')
[ "$cr" = "2" ] || fail "настройки не те: checks_required=$cr"
ok "реестр $REG, проверок нужно $cr"

say "3. три узла: регистрация, залог, проба"
for n in $NODES; do
    send wasm execute "$REG" '{"register_executor":{"model":"Qwen/Qwen2.5-7B-Instruct","runtime":"vllm-0.31.0","dtype":"bfloat16","card_class":"sm_89","kernel_cache_hash":"devnet-cache"}}' --from $n
    send wasm execute "$REG" "{\"deposit_stake\":{}}" --amount ${MIN_STAKE}stake --from $n
    # Предел нулевой, значит и проба нулевая: узел класса расходится с
    # эталоном ровно на ноль.
    send wasm execute "$REG" "{\"submit_conformance\":{\"divergence_micro\":0,\"logprobs_hash\":\"$AAAAS\"}}" --from $n
done
for n in $NODES; do
    a=$($B keys show $n -a --keyring-backend test --home $D)
    st=$($B query wasm contract-state smart "$REG" "{\"executor\":{\"address\":\"$a\"}}" $Q | jq -r '.data.status')
    [ "$st" = "active" ] || fail "узел $n в состоянии $st"
done
ok "три узла активны, с залогом и пробой"

say "4. подставной vLLM и раздача связок"
mkdir -p /work/prompts /work/b-n1 /work/b-n2 /work/b-n3 /work/state
python3 /work/fake-vllm.py 8000 >/tmp/vllm.log 2>&1 &
# Связки отдаются по http — тем же, чем их отдавал бы узел в сети.
( cd /work/b-n1 && python3 -m http.server 8081 >/tmp/h1.log 2>&1 & )
( cd /work/b-n2 && python3 -m http.server 8082 >/tmp/h2.log 2>&1 & )
i=0
while [ $i -lt 20 ]; do
    v=$(wget -qO- http://127.0.0.1:8000/version 2>/dev/null | jq -r '.version' 2>/dev/null || echo "")
    [ "$v" = "0.31.0" ] && break
    sleep 1; i=$((i+1))
done
[ "$v" = "0.31.0" ] || { cat /tmp/vllm.log; fail "подставной vLLM не поднялся"; }
ok "vLLM отвечает версией $v, связки раздаются на 8081 и 8082"

say "5. заказчик ставит задание — своим же кодом, не сценарием"
# Это client.py, а не `wasmd tx` из сценария: проверяется в том числе то,
# что заказчик сам считает сумму (цена плюс доля, округление вверх) и
# сам выкладывает запрос до отправки.
PROMPT="Опиши по шагам, что происходит при отправке транзакции в блокчейне."
printf '%s' "$PROMPT" > /work/вопрос.txt
JOB=$(python3 /work/daemon/client.py submit \
    --rest http://127.0.0.1:1317 --contract "$REG" \
    --prompt-file /work/вопрос.txt --price "$PRICE" \
    --prompts /work/prompts \
    --send --key client --binary wasmd --home "$D" \
    --node tcp://127.0.0.1:26657 --chain-id "$CH" --gas-prices 0stake \
    2>/tmp/submit.err) || { cat /tmp/submit.err; fail "заказчик не поставил задание"; }
grep -E "цена|хэш" /tmp/submit.err | sed 's/^/  /'
[ "$JOB" = "1" ] || { cat /tmp/submit.err; fail "номер задания $JOB, ожидался 1"; }
PH=$(python3 -c "import hashlib;print(hashlib.sha256(open('/work/вопрос.txt','rb').read()).hexdigest())")
[ -f "/work/prompts/$PH.txt" ] || fail "запрос не выложен под именем хэша"
stt=$(job_field state)
[ "$stt" = "open" ] || fail "задание в состоянии $stt"
cb=$(bal "$REG")
[ "$cb" = "$((TOTAL + MIN_STAKE * 3))" ] || fail "на контракте $cb, ожидалось $((TOTAL + MIN_STAKE * 3))"
ok "задание $JOB объявлено заказчиком, состояние $stt, на контракте $cb"
ok "запрос выложен под именем хэша: $PH.txt"

say "6. запустить три петли и больше ничего не делать"
for n in $NODES; do
    eval "B_$n=\$(bal \$($B keys show $n -a --keyring-backend test --home $D))"
done
# У каждого узла свои соседи, и нарочно разного вида: один по http,
# другой каталогом. Оба пути доставки проверяются одним прогоном.
peers_of() {
    case $1 in
        n1) echo "--peers http://127.0.0.1:8082 --peers /work/b-n3" ;;
        n2) echo "--peers http://127.0.0.1:8081 --peers /work/b-n3" ;;
        n3) echo "--peers http://127.0.0.1:8081 --peers http://127.0.0.1:8082" ;;
    esac
}
for n in $NODES; do
    pp=$(peers_of "$n")
    # shellcheck disable=SC2086
    python3 /work/daemon/loop.py \
        --rest http://127.0.0.1:1317 --contract "$REG" \
        --base-url http://127.0.0.1:8000/v1 \
        --model Qwen/Qwen2.5-7B-Instruct --dtype bfloat16 \
        --send --key "$n" --binary wasmd --home "$D" \
        --node tcp://127.0.0.1:26657 --chain-id "$CH" \
        --gas-prices 0stake \
        --prompts /work/prompts --bundles "/work/b-$n" $pp \
        --state "/work/state/$n.json" --every 3 --max-tokens 120 \
        >"/tmp/$n.log" 2>&1 &
    eval "PID_$n=$!"
    echo "  $n запущен"
done

say "7. ждать, пока задание закроется само"
i=0
while [ $i -lt 100 ]; do
    sleep 3
    stt=$(job_field state 2>/dev/null || echo "?")
    [ "$stt" = "settled" ] && break
    i=$((i+1))
done
# Петли больше не нужны: дальше только сверка.
for n in $NODES; do eval "kill \$PID_$n 2>/dev/null" || true; done
if [ "$stt" != "settled" ]; then
    echo "--- последнее состояние: $stt"
    for n in $NODES; do echo "--- $n:"; tail -25 "/tmp/$n.log"; done
    fail "задание не дошло до вердикта само"
fi
ok "задание закрыто без единой команды извне, состояние $stt"
# Если рабочий каталог переиспользован, у узлов осталась отметка
# просмотра с прошлой цепочки. Она обязана была исправиться сама —
# иначе узлы не увидели бы задания вовсе. Признак не обязательный:
# на свежем каталоге отметки нет.
for n in $NODES; do
    if grep -q "указывает за край" "/tmp/$n.log"; then
        echo "  $n: $(grep -m1 'указывает за край' /tmp/$n.log)"
    fi
done

say "8. сверить, ЧТО именно сделали узлы"
g=$(job_field guilty)
[ "$g" = "false" ] || fail "вердикт guilty=$g, ожидался false"
ok "вердикт: не виновен — узлы одного класса сошлись хэшами"

nchecks=$(checks_field '.data.checks|length')
[ "$nchecks" = "2" ] || fail "проверок $nchecks, ожидалось 2"
mis=$(checks_field '.data.mismatched')
[ "$mis" = "0" ] || fail "несовпавших хэшей $mis, ожидалось 0"
ok "проверок $nchecks, несовпавших хэшей $mis"

EX=$(job_field executor)
[ "$EX" != "null" ] && [ -n "$EX" ] || fail "исполнитель не записан"
# Кто взял задание — решили сами узлы, не мы. Поэтому ищем его по адресу.
WHO=""
for n in $NODES; do
    a=$($B keys show $n -a --keyring-backend test --home $D)
    [ "$a" = "$EX" ] && WHO=$n
done
[ -n "$WHO" ] || fail "исполнитель $EX не из наших узлов"
ok "задание взял $WHO — выбор его собственный"

# Деньги. Комиссии нулевые, поэтому прирост баланса — ровно выплата.
for n in $NODES; do
    a=$($B keys show $n -a --keyring-backend test --home $D)
    eval "was=\$B_$n"
    now=$(bal "$a")
    got=$((now - was))
    if [ "$n" = "$WHO" ]; then
        [ "$got" = "$PRICE" ] || fail "исполнителю $n пришло $got, ожидалось $PRICE"
        ok "исполнителю $n пришло $got — цена задания"
    else
        [ "$got" = "$SHARE" ] || fail "проверяющему $n пришло $got, ожидалось $SHARE"
        ok "проверяющему $n пришло $got — доля"
    fi
done
left=$(bal "$REG")
[ "$left" = "$((MIN_STAKE * 3))" ] || fail "на контракте осталось $left, ожидались только залоги $((MIN_STAKE * 3))"
ok "на контракте остались только залоги: $left"

say "9. связки действительно ходили между узлами"
[ -f "/work/b-$WHO/$JOB.json" ] || fail "исполнитель не положил связку"
python3 - "$WHO" "$PH" "$JOB" <<'PY' || fail "связка не та"
import hashlib, json, sys
who, ph, job = sys.argv[1], sys.argv[2], sys.argv[3]
b = json.load(open(f"/work/b-{who}/{job}.json", encoding="utf-8"))
assert b["prompt_hash"] == ph, "в связке не тот запрос"
assert hashlib.sha256(b["answer"].encode()).hexdigest() == b["answer_hash"]
print(f"  связка: запрос сошёлся, ответ {len(b['answer'])} символов, "
      f"вектор {len(b['vector'])} элементов")
PY
# Проверяющие обязаны были взять её у исполнителя: своей по этому
# заданию у них нет.
for n in $NODES; do
    [ "$n" = "$WHO" ] && continue
    grep -q "связка получена из" "/tmp/$n.log" || fail "$n не брал связку"
    echo "  $n: $(grep -m1 'связка получена из' /tmp/$n.log)"
done
ok "связка ушла от исполнителя к обоим проверяющим"

say "10. заказчик забирает ответ и сверяет его с цепочкой"
python3 /work/daemon/client.py collect \
    --rest http://127.0.0.1:1317 --contract "$REG" --job-id "$JOB" \
    --peers http://127.0.0.1:8081 --peers http://127.0.0.1:8082 \
    --peers /work/b-n3 --out /work/ответ.txt \
    >/dev/null 2>/tmp/collect.err || { cat /tmp/collect.err; fail "заказчик не забрал ответ"; }
grep -E "ВЕРДИКТ|получен из" /tmp/collect.err | sed 's/^/  /'
grep -q "ВЕРДИКТ: ответ подтверждён" /tmp/collect.err \
    || { cat /tmp/collect.err; fail "заказчику не сказали про вердикт"; }
# Независимая сверка: заказчик сверяет сам, но признак должен быть и у
# сценария — иначе проверяется только то, что он себе не противоречит.
AH=$(job_field answer_hash)
GOT=$(python3 -c "import hashlib;print(hashlib.sha256(open('/work/ответ.txt','rb').read()).hexdigest())")
[ "$GOT" = "$AH" ] || fail "записанный ответ не тот: $GOT против $AH в цепочке"
ok "ответ записан и сошёлся с хэшем из цепочки: $(wc -c < /work/ответ.txt) байт"

say "11. гонка за заданием не ломает петлю"
# Двое из трёх обязаны были опоздать: задание одно. Петля должна была
# пойти дальше, а не встать.
raced=0
for n in $NODES; do
    grep -q "опоздали" "/tmp/$n.log" && raced=$((raced + 1))
done
[ "$raced" -ge 1 ] || fail "ни один узел не опоздал: гонки не было, проверять нечего"
ok "опоздавших узлов $raced — отказ цепочки принят как обычный ход дела"

say "ГОТОВО: заказчик поставил, узлы сделали, заказчик забрал — без человека"
