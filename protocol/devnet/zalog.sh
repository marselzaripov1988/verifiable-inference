#!/bin/sh
# ЗАЛОГ НА НАСТОЯЩЕЙ ЦЕПОЧКЕ: внести, донести, забрать, потерять.
#
# Зачем отдельный прогон. Возврат залога был описан только тестами
# контракта и ни разу не проходил на настоящей цепочке — а инструкция
# владельцу карты обещает его человеку, который вносит 30 400 AXM.
# Писать такое с чужих слов нельзя.
#
# Прогон нашёл ошибку, из-за которой инструкцию и пришлось переписывать:
# `Pending` был дорогой в одну сторону. Узел, забравший хоть единицу ниже
# минимума, оставался непригоден НАВСЕГДА — `Active` ставилось только при
# регистрации, а второй раз зарегистрироваться нельзя. Шаг 5 ниже — тот
# самый признак, которого раньше не было.
#
# Демонов и модели здесь нет вовсе, и это нарочно: проверяются ДЕНЬГИ и
# состояния, а не инференс. Хэши ответов и проверок задаются прямо, чем и
# достигается нужный вердикт — совпали у всех или не совпали ни у кого.
#
# У каждого шага положительный признак. Отсутствие ошибки признаком НЕ
# считается.
set -eu

CH=devnet-1
D=/root/.wasmd
B="wasmd --home $D"
# Плата НОЛЬ: иначе комиссии примешиваются к залогу, и сверить деньги по
# балансам нельзя. Минимальная цена газа в app.toml тоже ноль.
TX="--chain-id $CH --keyring-backend test --home $D --gas 3000000 --fees 0stake -y -o json"
Q="--home $D -o json"
MIN_STAKE=50000000
PRICE=1000000
BPS=970
FEE=$((PRICE * BPS / 10000))
TOTAL=$((PRICE + FEE))
SHARE=$((FEE / 2))
ZEROS=0000000000000000000000000000000000000000000000000000000000000000
AAAAS=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
# Хэш ответа и обязательство исполнителя. Числа произвольны: вердикт
# выносится РАВЕНСТВОМ хэшей, а не их величиной.
MINE=1111111111111111111111111111111111111111111111111111111111111111
OTHER=2222222222222222222222222222222222222222222222222222222222222222

say() { echo; echo "===== $* ====="; }
fail() { echo "ПРОВАЛ: $*" >&2; exit 1; }
ok() { echo "ок: $*"; }

command -v jq >/dev/null 2>&1 || fail "нет jq"

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

# Сделка, которая ОБЯЗАНА быть отвергнута, и причина отказа. Код
# читается из ДОСТАВЛЕННОЙ транзакции: в ответе на отправку он всегда 0.
expect_fail() {
    want=$1; shift
    h=$($B tx "$@" $TX | jq -r '.txhash')
    [ -n "$h" ] && [ "$h" != "null" ] || fail "транзакция не отправлена"
    i=0
    while [ $i -lt 40 ]; do
        sleep 1
        out=$($B query tx --type=hash "$h" $Q 2>/dev/null) || { i=$((i+1)); continue; }
        code=$(echo "$out" | jq -r '.code')
        [ "$code" = "0" ] && fail "сделка прошла, а должна была быть отвергнута"
        echo "$out" | jq -r '.raw_log' | grep -qi "$want" && return 0
        echo "$out" | jq -r '.raw_log' >&2
        fail "отказ не тот: ждали «$want»"
    done
    fail "транзакция $h не попала в блок"
}

bal() {
    v=$($B query bank balances "$1" $Q | jq -r '.balances[]|select(.denom=="stake").amount')
    echo "${v:-0}"
}

node_field() {
    $B query wasm contract-state smart "$REG" \
        "{\"executor\":{\"address\":\"$1\"}}" $Q | jq -r ".data.$2"
}

say "1. поднять цепочку"
$B init devnet --chain-id $CH >/dev/null 2>&1
for n in admin client n1 n2 n3 konfiskat; do
    $B keys add $n --keyring-backend test --home $D >/dev/null 2>&1
done
sed -i 's/"ustake"/"stake"/g; s/"umlg"/"stake"/g' $D/config/genesis.json
for n in admin client n1 n2 n3 konfiskat; do
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
$B start --home $D >/tmp/node.log 2>&1 &
i=0
while [ $i -lt 60 ]; do
    sleep 1
    hh=$($B status --home $D 2>/dev/null | jq -r '.sync_info.latest_block_height' 2>/dev/null || echo "")
    [ -n "$hh" ] && [ "$hh" != "null" ] && [ "$hh" -gt 1 ] 2>/dev/null && break
    i=$((i+1))
done
[ -n "${hh:-}" ] && [ "$hh" -gt 1 ] 2>/dev/null || { tail -20 /tmp/node.log; fail "цепочка не поднялась"; }
ok "высота $hh — цепочка идёт"

ADMIN=$($B keys show admin -a --keyring-backend test --home $D)
CLIENT=$($B keys show client -a --keyring-backend test --home $D)
KONF=$($B keys show konfiskat -a --keyring-backend test --home $D)
A1=$($B keys show n1 -a --keyring-backend test --home $D)
A2=$($B keys show n2 -a --keyring-backend test --home $D)
A3=$($B keys show n3 -a --keyring-backend test --home $D)

say "2. развернуть реестр"
send wasm store /work/inference_registry.wasm --from admin
CODE=$($B query wasm list-code $Q | jq -r '.code_infos[-1].code_id')
[ -n "$CODE" ] && [ "$CODE" != "null" ] || fail "контракт не загрузился"
# Конфискат уходит по адресу, а не в сжигание: BankMsg::Burn на Axiome
# падает — модульного счёта wasm там нет. Здесь взят тот же вариант, что
# в живых контрактах, иначе прогон проверял бы не то устройство.
send wasm instantiate "$CODE" "{\"admin\":\"$ADMIN\",\"checks_required\":2,\
\"pinned_model\":\"Qwen/Qwen2.5-7B-Instruct\",\"pinned_runtime\":\"vllm-0.31.0\",\
\"pinned_dtype\":\"bfloat16\",\"pinned_arch\":\"sm_89\",\
\"kernel_cache_hash\":\"devnet-cache\",\"conformance_limit_micro\":0,\
\"conformance_ref_hash\":\"$ZEROS\",\"conformance_lens\":[3,3],\
\"claim_ttl_seconds\":600,\"check_ttl_seconds\":3600,\
\"settlement\":{\"native\":\"stake\"},\"stake_unit\":{\"native\":\"stake\"},\
\"min_stake\":\"$MIN_STAKE\",\"verifier_fee_bps\":$BPS,\
\"forfeit\":{\"to_address\":\"$KONF\"}}" --label registry --no-admin --from admin
REG=$($B query wasm list-contract-by-code "$CODE" $Q | jq -r '.contracts[0]')
ms=$($B query wasm contract-state smart "$REG" '{"config":{}}' $Q | jq -r '.data.min_stake')
[ "$ms" = "$MIN_STAKE" ] || fail "min_stake в цепочке $ms"
ok "реестр $REG, минимальный залог $ms"

say "3. регистрация без залога: задание не дадут"
for n in n1 n2 n3; do
    send wasm execute "$REG" '{"register_executor":{"model":"Qwen/Qwen2.5-7B-Instruct","runtime":"vllm-0.31.0","dtype":"bfloat16","card_class":"sm_89","kernel_cache_hash":"devnet-cache"}}' --from $n
    send wasm execute "$REG" "{\"submit_conformance\":{\"divergence_micro\":0,\"logprobs_hash\":\"$AAAAS\"}}" --from $n
done
send wasm execute "$REG" "{\"set_endpoint\":{\"endpoint\":\"http://127.0.0.1:8081\"}}" --from n1
st=$(node_field "$A1" status)
sk=$(node_field "$A1" stake)
[ "$st" = "active" ] && [ "$sk" = "0" ] || fail "после регистрации статус $st, залог $sk"
ok "зарегистрирован: статус $st, залог $sk — статус сам по себе ничего не даёт"

# Задание ставится заказчиком: оно понадобится на шагах 5-7.
send wasm execute "$REG" "{\"submit_job\":{\"prompt_hash\":\"$MINE\",\"prompt_uri\":\"http://127.0.0.1:8080\",\"price\":\"$PRICE\"}}" --amount ${TOTAL}stake --from client
expect_fail "залог 0, нужно не меньше $MIN_STAKE" \
    wasm execute "$REG" '{"claim_job":{"job_id":1}}' --from n1
ok "без залога задание не взять — отказ называет и сколько есть, и сколько нужно"

say "4. внести залог"
b0=$(bal "$A1")
send wasm execute "$REG" '{"deposit_stake":{}}' --amount ${MIN_STAKE}stake --from n1
b1=$(bal "$A1")
sk=$(node_field "$A1" stake)
[ "$sk" = "$MIN_STAKE" ] || fail "в записи залог $sk"
[ "$((b0 - b1))" = "$MIN_STAKE" ] || fail "со счёта ушло $((b0 - b1)), а залог $MIN_STAKE"
cb=$(bal "$REG")
[ "$cb" = "$((MIN_STAKE + TOTAL))" ] || fail "на контракте $cb"
ok "внесено $MIN_STAKE: со счёта ушло ровно столько, и столько же лежит на контракте"

say "5. частичный возврат ОБРАТИМ"
# Тот самый признак, которого не было до 28 октября: забрав единицу ниже
# минимума, узел выпадал в Pending НАВСЕГДА.
send wasm execute "$REG" '{"withdraw_stake":{"amount":"1000"}}' --from n1
b2=$(bal "$A1")
st=$(node_field "$A1" status)
sk=$(node_field "$A1" stake)
[ "$((b2 - b1))" = "1000" ] || fail "вернулось $((b2 - b1)) вместо 1000"
[ "$st" = "pending" ] || fail "статус $st, ожидался pending"
[ "$sk" = "$((MIN_STAKE - 1000))" ] || fail "залог $sk"
ok "забрано 1000: вернулось ровно столько, статус $st, залог $sk"

expect_fail "не допущен к заданиям" \
    wasm execute "$REG" '{"claim_job":{"job_id":1}}' --from n1
ok "в pending задания не дают — и отказ про статус, а не про подлог"

# Донести НЕ ДО минимума — остаться в pending. Иначе «донести» значило бы
# «положить сколько угодно».
send wasm execute "$REG" '{"deposit_stake":{}}' --amount 999stake --from n1
st=$(node_field "$A1" status)
sk=$(node_field "$A1" stake)
[ "$st" = "pending" ] || fail "залога не хватает, а статус уже $st"
[ "$sk" = "$((MIN_STAKE - 1))" ] || fail "залог $sk"
ok "донёс 999 из 1000 — статус всё ещё $st, залог $sk"

send wasm execute "$REG" '{"deposit_stake":{}}' --amount 1stake --from n1
st=$(node_field "$A1" status)
sk=$(node_field "$A1" stake)
b2=$(bal "$A1")
[ "$st" = "active" ] || fail "донёс до минимума, а статус $st"
[ "$sk" = "$MIN_STAKE" ] || fail "залог $sk"
[ "$b2" = "$b1" ] || fail "по счёту $b2 против $b1 до возврата: деньги разошлись"
ok "донёс недостающую единицу — статус снова $st, залог $sk, по счёту ноль разницы"

say "6. незакрытое задание держит залог"
send wasm execute "$REG" '{"claim_job":{"job_id":1}}' --from n1
expect_fail "залог нельзя забрать при 1 незакрытых заданиях" \
    wasm execute "$REG" '{"withdraw_stake":{"amount":"1"}}' --from n1
ok "взятое задание держит залог целиком — даже одну единицу не отдать"

send wasm execute "$REG" "{\"submit_answer\":{\"job_id\":1,\"answer_hash\":\"$MINE\",\"logprobs_hash\":\"$MINE\",\"answer_tokens\":120}}" --from n1
expect_fail "залог нельзя забрать при 1 незакрытых заданиях" \
    wasm execute "$REG" '{"withdraw_stake":{"amount":"1"}}' --from n1
ok "сданный ответ без вердикта — тоже держит"

say "7. вердикт отпускает залог, и возврат отдаёт ровно внесённое"
# Хэши проверяющих СОВПАДАЮТ с обязательством исполнителя: не виновен.
for n in n2 n3; do
    send wasm execute "$REG" "{\"submit_check\":{\"job_id\":1,\"divergence_micro\":0,\"logprobs_hash\":\"$MINE\"}}" --from $n
done
send wasm execute "$REG" '{"finalize":{"job_id":1}}' --from client
g=$($B query wasm contract-state smart "$REG" '{"job":{"job_id":1}}' $Q | jq -r '.data.guilty')
[ "$g" = "false" ] || fail "вердикт guilty=$g"
b3=$(bal "$A1")
[ "$((b3 - b2))" = "$PRICE" ] || fail "за задание пришло $((b3 - b2)), ожидалось $PRICE"
ok "вердикт: не виновен, исполнителю пришла цена $PRICE — целиком, долю внёс заказчик"

send wasm execute "$REG" "{\"withdraw_stake\":{\"amount\":\"$MIN_STAKE\"}}" --from n1
b4=$(bal "$A1")
sk=$(node_field "$A1" stake)
st=$(node_field "$A1" status)
[ "$((b4 - b3))" = "$MIN_STAKE" ] || fail "вернулось $((b4 - b3)) вместо $MIN_STAKE"
[ "$sk" = "0" ] || fail "в записи осталось $sk"
[ "$st" = "pending" ] || fail "статус $st"
ok "забран целиком: вернулось ровно $MIN_STAKE, залог 0, статус $st"
expect_fail "забрать 1 нельзя" \
    wasm execute "$REG" '{"withdraw_stake":{"amount":"1"}}' --from n1
ok "больше внесённого не отдаётся"

# Главная арифметика прогона: не считая задания, узел вернул себе всё.
[ "$((b4 - b0))" = "$PRICE" ] || fail "итого прирост $((b4 - b0)), ожидался ровно цена $PRICE"
ok "итог по счёту: прирост ровно $PRICE — залог вернулся без потерь"

say "8. проверяющему залог не нужен вовсе"
sk2=$(node_field "$A2" stake)
[ "$sk2" = "0" ] || fail "у проверяющего залог $sk2"
b2n=$(bal "$A2")
ok "n2 проверил задание с залогом $sk2 и получил долю: баланс $b2n"

say "9. подлог: залог изымается и не возвращается"
send wasm execute "$REG" '{"deposit_stake":{}}' --amount ${MIN_STAKE}stake --from n1
send wasm execute "$REG" "{\"submit_job\":{\"prompt_hash\":\"$MINE\",\"prompt_uri\":\"http://127.0.0.1:8080\",\"price\":\"$PRICE\"}}" --amount ${TOTAL}stake --from client
send wasm execute "$REG" '{"claim_job":{"job_id":2}}' --from n1
send wasm execute "$REG" "{\"submit_answer\":{\"job_id\":2,\"answer_hash\":\"$MINE\",\"logprobs_hash\":\"$MINE\",\"answer_tokens\":120}}" --from n1
k0=$(bal "$KONF")
# Хэши проверяющих НЕ СОВПАЛИ ни у одного: это и есть вердикт виновности.
for n in n2 n3; do
    send wasm execute "$REG" "{\"submit_check\":{\"job_id\":2,\"divergence_micro\":6,\"logprobs_hash\":\"$OTHER\"}}" --from $n
done
send wasm execute "$REG" '{"finalize":{"job_id":2}}' --from client
g=$($B query wasm contract-state smart "$REG" '{"job":{"job_id":2}}' $Q | jq -r '.data.guilty')
[ "$g" = "true" ] || fail "вердикт guilty=$g, ожидался true"
st=$(node_field "$A1" status)
sk=$(node_field "$A1" stake)
cn=$(node_field "$A1" convictions)
[ "$st" = "banned" ] || fail "статус $st"
[ "$sk" = "0" ] || fail "залог $sk — изъятие не состоялось"
[ "$cn" = "1" ] || fail "осуждений $cn"
k1=$(bal "$KONF")
[ "$((k1 - k0))" = "$MIN_STAKE" ] || fail "конфискат получил $((k1 - k0)) вместо $MIN_STAKE"
ok "вердикт: виновен. Статус $st, залог $sk, осуждений $cn, конфискат получил $MIN_STAKE"

expect_fail "забаненного исполнителя вернуть нельзя" \
    wasm execute "$REG" '{"withdraw_stake":{"amount":"1"}}' --from n1
ok "забаненный залога не заберёт"
expect_fail "забаненного исполнителя вернуть нельзя" \
    wasm execute "$REG" '{"deposit_stake":{}}' --amount 1000stake --from n1
ok "и взнос от него не принимается — иначе монеты заперло бы навсегда"

say "10. деньги сошлись у всех"
cb=$(bal "$REG")
[ "$cb" = "0" ] || fail "на контракте осталось $cb, а должно быть пусто"
ok "на контракте не осталось ничего: ни залогов, ни денег заданий"
for n in n2 n3; do
    eval "a=\$A$(echo $n | tr -d 'n')"
    v=$(bal "$a")
    ok "$n: баланс $v (две доли по $SHARE)"
done

say "ГОТОВО: залог внесён, возвращён частями и целиком, изъят за подлог"
