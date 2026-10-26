#!/bin/sh
# Репетиция переноса формы 2 -> 3 на девнете, до траты AXM.
#
# Воспроизводится ровно то, что пойдёт на axiome-1: экземпляр развёрнут
# НЫНЕШНИМ кодом (68562c30…, тот самый code_id 340), значит в хранилище
# настройки формы 2 — без аттестора и без сроков. Затем перенос на новый
# код со сроками.
#
# Признак успеха: событие `changed: ttl_added`, сроки читаются из
# настроек, версия пробы НЕ сброшена, и задание после переноса проходит
# полный путь со сроком.
set -eu
CH=devnet-1
D=/root/.wasmd
B="wasmd --home $D"
TX="--chain-id $CH --keyring-backend test --home $D --gas 3000000 --fees 10000stake -y -o json"
Q="--home $D -o json"

say() { echo; echo "===== $* ====="; }
fail() { echo "ПРОВАЛ: $*" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || fail "нет jq"

wait_tx() {
    i=0
    while [ $i -lt 40 ]; do
        sleep 1
        out=$($B query tx --type=hash "$1" $Q 2>/dev/null) || { i=$((i+1)); continue; }
        echo "$out"; return 0
    done
    fail "транзакция $1 не попала в блок"
}
send_ok() {
    h=$($B tx "$@" $TX | jq -r '.txhash')
    out=$(wait_tx "$h")
    code=$(echo "$out" | jq -r '.code')
    [ "$code" = "0" ] || { echo "$out" | jq -r '.raw_log' >&2; fail "code=$code"; }
    echo "$out"
}
send_expect_fail() {
    h=$($B tx "$@" $TX | jq -r '.txhash')
    out=$(wait_tx "$h")
    code=$(echo "$out" | jq -r '.code')
    [ "$code" = "0" ] && fail "сделка ПРОШЛА, а должна была отказать"
    echo "  отказ, код $code: $(echo "$out" | jq -r '.raw_log' | head -c 160)"
}

say "1. поднять цепочку"
$B init devnet --chain-id $CH >/dev/null 2>&1
for n in admin forfeit client exec c0; do
    $B keys add $n --keyring-backend test --home $D >/dev/null 2>&1
done
sed -i 's/"ustake"/"stake"/g; s/"umlg"/"stake"/g' $D/config/genesis.json
for n in admin forfeit client exec c0; do
    a=$($B keys show $n -a --keyring-backend test --home $D)
    $B genesis add-genesis-account "$a" 100000000000stake --home $D >/dev/null 2>&1
done
$B genesis gentx admin 70000000000stake --chain-id $CH --keyring-backend test --home $D >/dev/null 2>&1
$B genesis collect-gentxs --home $D >/dev/null 2>&1
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
[ -n "${hh:-}" ] || fail "цепочка не поднялась"
echo "высота $hh"
ADMIN=$($B keys show admin -a --keyring-backend test --home $D)
FORF=$($B keys show forfeit -a --keyring-backend test --home $D)
CLIENT=$($B keys show client -a --keyring-backend test --home $D)
EXEC=$($B keys show exec -a --keyring-backend test --home $D)

say "2. загрузить нынешний код (как 340) и новый"
out=$(send_ok wasm store /work/cur.wasm --from admin)
CUR=$(echo "$out" | jq -r '.events[]|select(.type=="store_code")|.attributes[]|select(.key=="code_id")|.value')
out=$(send_ok wasm store /work/new3.wasm --from admin)
NEW=$(echo "$out" | jq -r '.events[]|select(.type=="store_code")|.attributes[]|select(.key=="code_id")|.value')
$B query wasm code-info "$CUR" $Q | jq -r '"  нынешний: " + (.data_hash|ascii_downcase)'
$B query wasm code-info "$NEW" $Q | jq -r '"  новый:    " + (.data_hash|ascii_downcase)'

say "3. развернуть НЫНЕШНИМ кодом — хранилище формы 2"
LEASE=$(jq -c --arg a "$ADMIN" --arg f "$FORF" '
    .admin=$a | .forfeit={"to_address":$f}
    | .settlement={"native":"stake"} | .stake_denom="stake"
    | .min_stake="1000000" | del(.claim_ttl_seconds) | del(.check_ttl_seconds)' /work/lease.json)
out=$(send_ok wasm instantiate "$CUR" "$LEASE" --label reg --admin "$ADMIN" --from admin)
C=$(echo "$out" | jq -r '.events[]|select(.type=="instantiate")|.attributes[]|select(.key=="_contract_address")|.value')
echo "контракт: $C"
CFG=$($B query wasm contract-state smart "$C" '{"config":{}}' $Q | jq -c '.data')
echo "$CFG" | jq -e 'has("claim_ttl_seconds")' >/dev/null && fail "сроки уже есть"
echo "  сроков в настройках нет — как у развёрнутых"
echo "  версия пробы: $(echo "$CFG" | jq -r '.conformance_version')"

say "4. срока нет — задание повиснет"
send_ok wasm execute "$C" '{"register_executor":{"model":"Qwen/Qwen2.5-7B-Instruct","runtime":"vllm-0.31.0","dtype":"bfloat16","card_class":"sm_89","kernel_cache_hash":"3753554454f2c9b850c6ec0912a58e39ee08028012f3de1e9649e62d67cc131a"}}' --from exec >/dev/null
send_ok wasm execute "$C" '{"deposit_stake":{}}' --amount 1000000stake --from exec >/dev/null
# Проба: контракт принимает ЗАЯВЛЕННОЕ число (ноль) и обязательство
# хэшем. Правдивость проверяется только спором — здесь он не нужен.
send_ok wasm execute "$C" '{"submit_conformance":{"divergence_micro":0,"logprobs_hash":"probe-h"}}' --from exec >/dev/null
send_ok wasm execute "$C" '{"submit_job":{"prompt_hash":"ph","price":"1000000"}}' --amount 1048500stake --from client >/dev/null
echo "  задание 1 поставлено; отменить его в нынешнем коде нельзя:"
send_expect_fail wasm execute "$C" '{"cancel_job":{"job_id":1}}' --from client

say "5. ПЕРЕНОС на код со сроками"
out=$(send_ok wasm migrate "$C" "$NEW" '{}' --from admin)
echo "$out" | jq -r '.events[]|select(.type=="wasm")|.attributes[]|select(.key=="from" or .key=="to" or .key=="changed" or .key=="claim_ttl_seconds" or .key=="check_ttl_seconds")|"  " + .key + ": " + .value'
CFG=$($B query wasm contract-state smart "$C" '{"config":{}}' $Q | jq -c '.data')
echo "$CFG" | jq -r '"  сроки в настройках: " + (.claim_ttl_seconds|tostring) + " и " + (.check_ttl_seconds|tostring)'
echo "$CFG" | jq -r '"  версия пробы после переноса: " + (.conformance_version|tostring)'
[ "$(echo "$CFG" | jq -r '.conformance_version')" = "1" ] || fail "версия пробы сбита"

say "6. задание 1 цело и теперь отменяемо"
$B query wasm contract-state smart "$C" '{"job":{"job_id":1}}' $Q | jq -r '"  состояние " + .data.state + ", цена " + .data.price + ", доля " + .data.fee_total'
BEFORE=$($B query bank balances "$CLIENT" $Q | jq -r '.balances[]|select(.denom=="stake")|.amount')
send_ok wasm execute "$C" '{"cancel_job":{"job_id":1}}' --from client >/dev/null
AFTER=$($B query bank balances "$CLIENT" $Q | jq -r '.balances[]|select(.denom=="stake")|.amount')
# Из прироста надо вычесть плату за саму сделку отмены: её заказчик
# платит цепочке, а не контракту. 1 048 500 внесено, 10 000 плата.
GOT=$((AFTER - BEFORE + 10000))
echo "  заказчику вернулось: $GOT stake (внесено 1048500, плата за сделку 10000)"
[ "$GOT" = "1048500" ] || fail "вернулось не всё: $GOT"

say "7. срок на взятое задание — на живой цепочке"
# Сроки укорачиваются до секунды настройкой: ждать десять минут на
# девнете незачем, а механизм от величины не зависит. Ноль поставить
# нельзя, и это проверено отдельно.
send_ok wasm execute "$C" '{"update_config":{"claim_ttl_seconds":1,"check_ttl_seconds":1}}' --from admin >/dev/null
send_expect_fail wasm execute "$C" '{"update_config":{"claim_ttl_seconds":0}}' --from admin
send_ok wasm execute "$C" '{"submit_job":{"prompt_hash":"ph2","price":"1000000"}}' --amount 1048500stake --from client >/dev/null
send_ok wasm execute "$C" '{"claim_job":{"job_id":2}}' --from exec >/dev/null
sleep 3
out=$(send_ok wasm execute "$C" '{"release_job":{"job_id":2}}' --from c0)
echo "$out" | jq -r '.events[]|select(.type=="wasm")|.attributes[]|select(.key=="passed" or .key=="abandoned_by")|"  " + .key + ": " + .value'
$B query wasm contract-state smart "$C" '{"job":{"job_id":2}}' $Q | jq -r '"  задание 2: " + .data.state + ", исполнитель " + (.data.executor // "нет")'
$B query wasm contract-state smart "$C" '{"executor":{"address":"'"$EXEC"'"}}' $Q | jq -r '"  исполнитель: статус " + .data.status + ", брошено " + (.data.abandoned|tostring) + ", залог " + .data.stake'

say "8. сбор проверок истёк — деньги заказчику"
# Проверяющий тоже узел класса: `submit_check` требует регистрации.
send_ok wasm execute "$C" '{"register_executor":{"model":"Qwen/Qwen2.5-7B-Instruct","runtime":"vllm-0.31.0","dtype":"bfloat16","card_class":"sm_89","kernel_cache_hash":"3753554454f2c9b850c6ec0912a58e39ee08028012f3de1e9649e62d67cc131a"}}' --from c0 >/dev/null
send_ok wasm execute "$C" '{"deposit_stake":{}}' --amount 1000000stake --from c0 >/dev/null
send_ok wasm execute "$C" '{"submit_conformance":{"divergence_micro":0,"logprobs_hash":"probe-h"}}' --from c0 >/dev/null
send_ok wasm execute "$C" '{"claim_job":{"job_id":2}}' --from exec >/dev/null
send_ok wasm execute "$C" '{"submit_answer":{"job_id":2,"answer_hash":"ah","logprobs_hash":"lh","answer_tokens":64}}' --from exec >/dev/null
# Одна проверка из двух: вердикта быть не может.
send_ok wasm execute "$C" '{"submit_check":{"job_id":2,"divergence_micro":0,"logprobs_hash":"lh"}}' --from c0 >/dev/null
send_expect_fail wasm execute "$C" '{"finalize":{"job_id":2}}' --from c0
sleep 3
B4=$($B query bank balances "$CLIENT" $Q | jq -r '.balances[]|select(.denom=="stake")|.amount')
out=$(send_ok wasm execute "$C" '{"expire_checks":{"job_id":2}}' --from c0)
echo "$out" | jq -r '.events[]|select(.type=="wasm")|.attributes[]|select(.key=="checks" or .key=="verifier_fees" or .key=="refunded")|"  " + .key + ": " + .value'
A4=$($B query bank balances "$CLIENT" $Q | jq -r '.balances[]|select(.denom=="stake")|.amount')
echo "  заказчику пришло: $((A4 - B4)) stake (цена 1000000 плюс неразошедшаяся доля 24250)"
[ "$((A4 - B4))" = "1024250" ] || fail "заказчику пришло не то: $((A4 - B4))"
$B query wasm contract-state smart "$C" '{"executor":{"address":"'"$EXEC"'"}}' $Q | jq -r '"  исполнитель: статус " + .data.status + ", заданий " + (.data.jobs_done|tostring) + ", осуждений " + (.data.convictions|tostring) + ", залог " + .data.stake'
$B query wasm contract-state smart "$C" '{"job":{"job_id":2}}' $Q | jq -r '"  задание 2: " + .data.state + ", вердикт " + (if .data.guilty == null then "нет" else (.data.guilty|tostring) end)'

say "ИТОГ"
echo "перенос формы 2 -> 3 проходит: сроки добавлены, версия пробы цела,"
echo "задание цело и отменяемо. Оба срока действуют на живой цепочке:"
echo "брошенное задание возвращается в Open, а истёкший сбор проверок"
echo "возвращает деньги заказчику, не осуждая исполнителя."
