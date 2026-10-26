#!/bin/sh
# Можно ли вернуться с нового кода на code_id 339 — проверка на девнете.
#
# Девнет wasmd v0.50.0 / libwasmvm 1.5.0 — та же пара, что у Axiome.
# Старый код здесь ТОТ САМЫЙ: его контрольная сумма 2461a00b… равна
# data_hash кода 339 в axiome-1.
#
# Признаки успеха положительные и внешние, отсутствие ошибки за успех не
# считается.
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
        echo "$out"
        return 0
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

# Ждём ОТКАЗА. Успех здесь — именно неуспех сделки.
send_expect_fail() {
    h=$($B tx "$@" $TX | jq -r '.txhash')
    out=$(wait_tx "$h")
    code=$(echo "$out" | jq -r '.code')
    [ "$code" = "0" ] && fail "сделка ПРОШЛА, а должна была отказать"
    echo "  код ошибки: $code"
    echo "  журнал: $(echo "$out" | jq -r '.raw_log')"
}

say "1. поднять цепочку"
$B init devnet --chain-id $CH >/dev/null 2>&1
for n in admin forfeit attestor; do
    $B keys add $n --keyring-backend test --home $D >/dev/null 2>&1
done
sed -i 's/"ustake"/"stake"/g; s/"umlg"/"stake"/g' $D/config/genesis.json
for n in admin forfeit attestor; do
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
ATT=$($B keys show attestor -a --keyring-backend test --home $D)

say "2. загрузить ОБА кода"
out=$(send_ok wasm store /work/old.wasm --from admin)
OLD=$(echo "$out" | jq -r '.events[]|select(.type=="store_code")|.attributes[]|select(.key=="code_id")|.value')
out=$(send_ok wasm store /work/new.wasm --from admin)
NEW=$(echo "$out" | jq -r '.events[]|select(.type=="store_code")|.attributes[]|select(.key=="code_id")|.value')
echo "старый код (2461a00b…, он же 339 в axiome-1): code_id=$OLD"
echo "новый код (68562c30…): code_id=$NEW"
$B query wasm code-info "$OLD" $Q | jq -r '"  старый data_hash: " + (.data_hash|ascii_downcase)'
$B query wasm code-info "$NEW" $Q | jq -r '"  новый  data_hash: " + (.data_hash|ascii_downcase)'

say "3. развернуть СТАРЫМ кодом, как в axiome-1"
LEASE=$(jq -c --arg a "$ADMIN" --arg t "$ATT" --arg f "$FORF" '
    .admin=$a | .attestor=$t | .forfeit={"to_address":$f}
    | .settlement={"native":"stake"} | .stake_denom="stake"' /work/lease-old.json)
out=$(send_ok wasm instantiate "$OLD" "$LEASE" --label reg --admin "$ADMIN" --from admin)
C=$(echo "$out" | jq -r '.events[]|select(.type=="instantiate")|.attributes[]|select(.key=="_contract_address")|.value')
echo "контракт: $C"
HAS=$($B query wasm contract-state smart "$C" '{"config":{}}' $Q | jq -r '.data.attestor // "нет"')
[ "$HAS" = "$ATT" ] || fail "в настройках нет аттестора, а должен быть"
echo "  аттестор в настройках: есть ($HAS)"

say "4. перенос ВПЕРЁД, на новый код"
out=$(send_ok wasm migrate "$C" "$NEW" '{}' --from admin)
echo "$out" | jq -r '.events[]|select(.type=="wasm")|.attributes[]|select(.key=="from" or .key=="to" or .key=="changed")|"  " + .key + ": " + .value'
CFG=$($B query wasm contract-state smart "$C" '{"config":{}}' $Q | jq -c '.data')
echo "$CFG" | jq -e 'has("attestor")' >/dev/null && fail "аттестор остался"
echo "  аттестора в настройках больше нет"
echo "$CFG" | jq -r '"  класс " + .pinned_arch + ", предел " + (.conformance_limit_micro|tostring) + ", залог " + .min_stake + ", доля " + (.verifier_fee_bps|tostring) + ", проверок " + (.checks_required|tostring) + ", версия пробы " + (.conformance_version|tostring)'
CID=$($B query wasm contract "$C" $Q | jq -r '.contract_info.code_id')
echo "  code_id контракта теперь: $CID"

say "5. перенос НАЗАД, на тот самый старый code_id"
send_expect_fail wasm migrate "$C" "$OLD" '{}' --from admin

say "6. а если загрузить тот же старый код ещё раз, под новым номером?"
out=$(send_ok wasm store /work/old.wasm --from admin)
OLD2=$(echo "$out" | jq -r '.events[]|select(.type=="store_code")|.attributes[]|select(.key=="code_id")|.value')
echo "тот же код, code_id=$OLD2"
send_expect_fail wasm migrate "$C" "$OLD2" '{}' --from admin

say "7. контракт после двух отказов жив и на новом коде"
CID=$($B query wasm contract "$C" $Q | jq -r '.contract_info.code_id')
[ "$CID" = "$NEW" ] || fail "code_id сбился: $CID"
$B query wasm contract-state smart "$C" '{"config":{}}' $Q | jq -r '"  code_id " + "'"$CID"'" + ", аттестор: " + (if .data|has("attestor") then "есть" else "нет" end)'

say "ИТОГ"
echo "вперёд — проходит, назад на код без migrate — нет, и это свойство КОДА,"
echo "а не номера: тот же код под другим номером отказал так же."
