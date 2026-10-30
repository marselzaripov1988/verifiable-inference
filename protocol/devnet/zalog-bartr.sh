#!/bin/sh
# ЗАЛОГ В BARTR НА НАСТОЯЩЕЙ ЦЕПОЧКЕ: перенос, смена единицы, работа.
#
# Две части, и порядок у них тот же, что будет на живой цепочке.
#
# A. Перенос. Реестр разворачивается ПРЕЖНИМ кодом (343, залог строкой
#    `stake_denom`), узел вносит залог родной монетой, и реестр
#    переносится на новый код. Перенос обязан оставить залог родным:
#    внесённое возвращается тем, чем внесено. Сменить единицу при
#    лежащем залоге нельзя; когда залог забран — можно.
#
# B. Работа на BARTR. Залог вносится `send` на контракт токена с
#    вложенным `deposit_stake`, возвращается переводом BARTR, донесение
#    возвращает допуск, и за подлог BARTR уходит по адресу конфиската.
#    Расчёт заданий при этом остаётся родной монетой — единицы порознь.
#
# BARTR здесь — cw20_base. У живого BARTR (code_id 144) тот же
# интерфейс: запрос разбирается в `cw20_base::msg::QueryMsg`, а в
# байткоде есть `send` и `Cw20ReceiveMsg`. Но `send` на живом BARTR не
# вызывал никто ни разу — 1412 переводов и 0 `send` на 30 октября, — так
# что первая настоящая проверка будет в axiome-1.
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
BPS=485
FEE=$((PRICE * BPS / 10000))
TOTAL=$((PRICE + FEE))
ZEROS=0000000000000000000000000000000000000000000000000000000000000000
AAAAS=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
MINE=1111111111111111111111111111111111111111111111111111111111111111
OTHER=2222222222222222222222222222222222222222222222222222222222222222
# {"deposit_stake":{}} в base64 — вложенное сообщение для `send`.
HOOK=$(printf '{"deposit_stake":{}}' | base64 | tr -d '\n')

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

bartr() {
    $B query wasm contract-state smart "$BARTR" "{\"balance\":{\"address\":\"$1\"}}" $Q \
        | jq -r '.data.balance'
}

node_field() {
    $B query wasm contract-state smart "$REG" \
        "{\"executor\":{\"address\":\"$1\"}}" $Q | jq -r ".data.$2"
}

cfg_field() {
    $B query wasm contract-state smart "$REG" '{"config":{}}' $Q | jq -c ".data.$1"
}

# Правка одной настройки. Пропущенные поля читаются как None, и
# UpdateConfig их не трогает.
upd() {
    echo "{\"update_config\":{$1}}"
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

say "2. BARTR и оба кода"
send wasm store /work/cw20_base.wasm --from admin
CW20_CODE=$($B query wasm list-code $Q | jq -r '.code_infos[-1].code_id')
send wasm instantiate "$CW20_CODE" "{\"name\":\"BARTR-test\",\"symbol\":\"BARTR\",\"decimals\":6,\
\"initial_balances\":[{\"address\":\"$A1\",\"amount\":\"$((MIN_STAKE * 3))\"}]}" \
    --label bartr --no-admin --from admin
BARTR=$($B query wasm list-contract-by-code "$CW20_CODE" $Q | jq -r '.contracts[0]')
[ "$(bartr "$A1")" = "$((MIN_STAKE * 3))" ] || fail "BARTR у n1: $(bartr "$A1")"
ok "BARTR $BARTR, у n1 $(bartr "$A1")"

send wasm store /work/inference_registry-343.wasm --from admin
OLD_CODE=$($B query wasm list-code $Q | jq -r '.code_infos[-1].code_id')
send wasm store /work/inference_registry.wasm --from admin
NEW_CODE=$($B query wasm list-code $Q | jq -r '.code_infos[-1].code_id')
[ "$OLD_CODE" != "$NEW_CODE" ] || fail "коды не различаются"
ok "прежний код $OLD_CODE (343 в axiome-1), новый $NEW_CODE"

say "A1. реестр прежним кодом, залог строкой"
# --admin, а не --no-admin: иначе переносить было бы некому. Так же
# развёрнуты живые экземпляры.
send wasm instantiate "$OLD_CODE" "{\"admin\":\"$ADMIN\",\"checks_required\":2,\
\"pinned_model\":\"Qwen/Qwen2.5-7B-Instruct\",\"pinned_runtime\":\"vllm-0.31.0\",\
\"pinned_dtype\":\"bfloat16\",\"pinned_arch\":\"sm_89\",\
\"kernel_cache_hash\":\"devnet-cache\",\"conformance_limit_micro\":0,\
\"conformance_ref_hash\":\"$ZEROS\",\"conformance_lens\":[3,3],\
\"claim_ttl_seconds\":600,\"check_ttl_seconds\":3600,\
\"settlement\":{\"native\":\"stake\"},\"stake_denom\":\"stake\",\
\"min_stake\":\"$MIN_STAKE\",\"verifier_fee_bps\":$BPS,\
\"forfeit\":{\"to_address\":\"$KONF\"}}" --label registry --admin "$ADMIN" --from admin
REG=$($B query wasm list-contract-by-code "$OLD_CODE" $Q | jq -r '.contracts[0]')
[ "$(cfg_field stake_denom)" = '"stake"' ] || fail "в прежней форме залог $(cfg_field stake_denom)"
ok "реестр $REG, залог строкой $(cfg_field stake_denom)"

for n in n1 n2 n3; do
    send wasm execute "$REG" '{"register_executor":{"model":"Qwen/Qwen2.5-7B-Instruct","runtime":"vllm-0.31.0","dtype":"bfloat16","card_class":"sm_89","kernel_cache_hash":"devnet-cache"}}' --from $n
    send wasm execute "$REG" "{\"submit_conformance\":{\"divergence_micro\":0,\"logprobs_hash\":\"$AAAAS\"}}" --from $n
done
send wasm execute "$REG" "{\"set_endpoint\":{\"endpoint\":\"http://127.0.0.1:8081\"}}" --from n1
b0=$(bal "$A1")
send wasm execute "$REG" '{"deposit_stake":{}}' --amount ${MIN_STAKE}stake --from n1
[ "$(node_field "$A1" stake)" = "$MIN_STAKE" ] || fail "залог $(node_field "$A1" stake)"
ok "n1 внёс $MIN_STAKE родной монетой прежним кодом"

say "A2. перенос оставляет залог родным"
send wasm migrate "$REG" "$NEW_CODE" '{}' --from admin
u=$(cfg_field stake_unit)
[ "$u" = '{"native":"stake"}' ] || fail "после переноса единица $u"
[ "$(cfg_field stake_denom)" = "null" ] || fail "строка залога осталась в настройках"
[ "$(node_field "$A1" stake)" = "$MIN_STAKE" ] || fail "залог после переноса $(node_field "$A1" stake)"
[ "$(node_field "$A1" status)" = "active" ] || fail "статус $(node_field "$A1" status)"
ok "форма 5: единица $u, залог n1 $MIN_STAKE, статус active — перенос чужих денег не тронул"

say "A3. сменить единицу при лежащем залоге нельзя"
expect_fail "единицу залога нельзя сменить" \
    wasm execute "$REG" "$(upd "\"stake_unit\":{\"cw20\":\"$BARTR\"}")" --from admin
[ "$(cfg_field stake_unit)" = '{"native":"stake"}' ] || fail "единица сменилась"
ok "отказ: внесённые stake иначе вернулись бы BARTR"

send wasm execute "$REG" "{\"withdraw_stake\":{\"amount\":\"$MIN_STAKE\"}}" --from n1
b1=$(bal "$A1")
[ "$b1" = "$b0" ] || fail "по счёту $b1 против $b0: родной залог вернулся не целиком"
ok "n1 забрал залог: по счёту ровно как до взноса"

say "A4. залог пуст — единица меняется на BARTR"
send wasm execute "$REG" "$(upd "\"stake_unit\":{\"cw20\":\"$BARTR\"}")" --from admin
u=$(cfg_field stake_unit)
[ "$u" = "{\"cw20\":\"$BARTR\"}" ] || fail "единица $u"
[ "$(cfg_field settlement)" = '{"native":"stake"}' ] || fail "расчёт сменился вместе с залогом"
ok "залог в $u, расчёт по-прежнему $(cfg_field settlement)"

expect_fail "сжечь залог в CW20 нельзя" \
    wasm execute "$REG" "$(upd '"forfeit":"burn"')" --from admin
ok "сжигание BARTR настройкой не принимается: это заявка на выкуп, а не стока"

say "B1. залог в BARTR вносится только через send"
expect_fail "залог вносится в CW20" \
    wasm execute "$REG" '{"deposit_stake":{}}' --amount ${MIN_STAKE}stake --from n1
ok "родная монета залогом больше не становится"

t0=$(bartr "$A1")
send wasm execute "$BARTR" "{\"send\":{\"contract\":\"$REG\",\"amount\":\"$MIN_STAKE\",\"msg\":\"$HOOK\"}}" --from n1
t1=$(bartr "$A1")
[ "$((t0 - t1))" = "$MIN_STAKE" ] || fail "у n1 ушло $((t0 - t1)) BARTR"
[ "$(bartr "$REG")" = "$MIN_STAKE" ] || fail "на реестре $(bartr "$REG") BARTR"
[ "$(node_field "$A1" stake)" = "$MIN_STAKE" ] || fail "в записи $(node_field "$A1" stake)"
[ "$(node_field "$A1" status)" = "active" ] || fail "статус $(node_field "$A1" status)"
ok "send $MIN_STAKE BARTR: у n1 ушло ровно столько, на реестре лежит столько же, записано на n1"

say "B2. честное задание, частичный возврат и донесение в BARTR"
send wasm execute "$REG" "{\"submit_job\":{\"prompt_hash\":\"$MINE\",\"prompt_uri\":\"http://127.0.0.1:8080\",\"price\":\"$PRICE\"}}" --amount ${TOTAL}stake --from client
send wasm execute "$REG" '{"claim_job":{"job_id":1}}' --from n1
send wasm execute "$REG" "{\"submit_answer\":{\"job_id\":1,\"answer_hash\":\"$MINE\",\"logprobs_hash\":\"$MINE\",\"answer_tokens\":120}}" --from n1
expect_fail "залог нельзя забрать при 1 незакрытых заданиях" \
    wasm execute "$REG" '{"withdraw_stake":{"amount":"1"}}' --from n1
for n in n2 n3; do
    send wasm execute "$REG" "{\"submit_check\":{\"job_id\":1,\"divergence_micro\":0,\"logprobs_hash\":\"$MINE\"}}" --from $n
done
p0=$(bal "$A1")
send wasm execute "$REG" '{"finalize":{"job_id":1}}' --from client
p1=$(bal "$A1")
[ "$((p1 - p0))" = "$PRICE" ] || fail "за задание пришло $((p1 - p0)) stake"
ok "не виновен: цена $PRICE пришла родной монетой, залог в BARTR при этом не тронут"

send wasm execute "$REG" '{"withdraw_stake":{"amount":"1000"}}' --from n1
[ "$(($(bartr "$A1") - t1))" = "1000" ] || fail "вернулось $(($(bartr "$A1") - t1)) BARTR"
[ "$(node_field "$A1" status)" = "pending" ] || fail "статус $(node_field "$A1" status)"
ok "забрано 1000: вернулось 1000 BARTR, статус pending"
send wasm execute "$BARTR" "{\"send\":{\"contract\":\"$REG\",\"amount\":\"1000\",\"msg\":\"$HOOK\"}}" --from n1
[ "$(node_field "$A1" status)" = "active" ] || fail "донёс, а статус $(node_field "$A1" status)"
[ "$(bartr "$A1")" = "$t1" ] || fail "BARTR у n1 $(bartr "$A1") против $t1"
ok "донёс 1000 BARTR — снова active, по счёту BARTR ноль разницы"

say "B3. подлог: залог в BARTR уходит конфискату"
send wasm execute "$REG" "{\"submit_job\":{\"prompt_hash\":\"$MINE\",\"prompt_uri\":\"http://127.0.0.1:8080\",\"price\":\"$PRICE\"}}" --amount ${TOTAL}stake --from client
send wasm execute "$REG" '{"claim_job":{"job_id":2}}' --from n1
send wasm execute "$REG" "{\"submit_answer\":{\"job_id\":2,\"answer_hash\":\"$MINE\",\"logprobs_hash\":\"$MINE\",\"answer_tokens\":120}}" --from n1
for n in n2 n3; do
    send wasm execute "$REG" "{\"submit_check\":{\"job_id\":2,\"divergence_micro\":6,\"logprobs_hash\":\"$OTHER\"}}" --from $n
done
k0=$(bartr "$KONF")
send wasm execute "$REG" '{"finalize":{"job_id":2}}' --from client
g=$($B query wasm contract-state smart "$REG" '{"job":{"job_id":2}}' $Q | jq -r '.data.guilty')
[ "$g" = "true" ] || fail "вердикт guilty=$g"
[ "$(node_field "$A1" status)" = "banned" ] || fail "статус $(node_field "$A1" status)"
[ "$(node_field "$A1" stake)" = "0" ] || fail "залог $(node_field "$A1" stake)"
k1=$(bartr "$KONF")
[ "$((k1 - ${k0:-0}))" = "$MIN_STAKE" ] || fail "конфискат получил $((k1 - ${k0:-0})) BARTR"
ok "виновен: конфискат получил $MIN_STAKE BARTR, n1 забанен"

expect_fail "забаненного исполнителя вернуть нельзя" \
    wasm execute "$BARTR" "{\"send\":{\"contract\":\"$REG\",\"amount\":\"1000\",\"msg\":\"$HOOK\"}}" --from n1
ok "и BARTR от забаненного не принимается — иначе заперло бы навсегда"

say "B4. деньги сошлись"
[ "$(bartr "$REG")" = "0" ] || fail "на реестре осталось $(bartr "$REG") BARTR"
[ "$(bal "$REG")" = "0" ] || fail "на реестре осталось $(bal "$REG") stake"
ok "на реестре ни BARTR, ни родной монеты"

say "ГОТОВО: перенос оставил залог родным, единица сменилась только при пустом залоге, залог в BARTR внесён, возвращён и изъят"
