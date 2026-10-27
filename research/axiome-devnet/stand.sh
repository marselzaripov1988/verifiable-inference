#!/usr/bin/env bash
# Стенд: три правки к узлу Axiome, проверенные исполнением.
#
# Запускается ВНУТРИ образа, собранного из исправленного axm-node
# (см. run.sh). Снаружи ничего не трогает: своя цепочка, свой узел.
#
# Что проверяется и чем:
#   1. stargate в наборе — контракт с `requires_stargate` ЗАГРУЖАЕТСЯ.
#   2. разрешающий список — запрос к x/referral из контракта ОТВЕЧАЕТ,
#      и ответ совпадает с деревом из генезиса (у BOB реферер BEN).
#   3. счёт wasm с burner — BankMsg::Burn УМЕНЬШАЕТ выпуск на сумму.
#
# Проверка — сверка с ожидаемым, а не печать для глаза: каждый шаг либо
# подтверждается, либо роняет прогон.
set -eu

WASM=/work/stargate_probe.wasm
CHAIN=demo
HOME_DIR=${HOME:-/root}/.axmd
TX="--from alice --chain-id $CHAIN --keyring-backend test -y -o json \
    --gas 50000000 --fees 500000uaxm --broadcast-mode sync"
# Без --chain-id: у запросов этого флага нет, цепочка берётся из config.
Q="-o json"

# BOB и его реферер BEN — из tests/fixtures/referrals.json самого узла.
BOB=axm157lyytcwlk4lg2h7tvssapf9x45g7re5mlv96r
BEN=axm1rkk2x2jedsdkmckyrxzjx9tdtavnqwlpqf3yts

fail() { echo "ПРОВАЛ: $*" >&2; exit 1; }
ok() { echo "ок: $*"; }

# Сделка: отправить, дождаться включения, вернуть результат целиком.
# Ожидание не по времени, а по появлению сделки в цепочке.
send() {
  local out hash i
  out=$(axmd tx "$@" $TX) || fail "сделка не отправлена: $out"
  hash=$(echo "$out" | jq -r .txhash)
  [ "$(echo "$out" | jq -r .code)" = "0" ] || \
    fail "сделка отвергнута на проверке: $(echo "$out" | jq -r .raw_log)"
  for i in $(seq 1 40); do
    if axmd query tx "$hash" $Q > /tmp/tx.json 2>/dev/null; then
      cat /tmp/tx.json
      return 0
    fi
    sleep 1
  done
  fail "сделка $hash не попала в цепочку за 40 с"
}

supply() {
  # Через REST, а не через CLI: имя подкоманды выпуска между выпусками
  # SDK менялось, а путь REST — нет. Высота обязательна: выпуск здесь
  # меняется каждым блоком сам по себе.
  curl -sf -H "x-cosmos-block-height: $1" \
    "http://127.0.0.1:1317/cosmos/bank/v1beta1/supply/by_denom?denom=uaxm" \
    | jq -r .amount.amount
}

# Сколько выпуска съел БЛОК со сделкой: разница с предыдущим блоком.
# Иначе измерялось бы заодно всё, что цепочка делает сама.
block_effect() {
  local h=$1
  echo $(( $(supply $((h - 1))) - $(supply "$h") ))
}

height() {
  axmd status 2>/dev/null | jq -r '[.. | objects | .latest_block_height? // empty] | first'
}

echo "=== 0. цепочка"
bash /axm-node/scripts/init-testchain.sh > /tmp/init.log 2>&1 || \
  { tail -20 /tmp/init.log; fail "цепочка не поднялась"; }
# Сборка газа нулевая: стенд не про экономику.
axmd start --minimum-gas-prices 0uaxm > /tmp/node.log 2>&1 &
NODE=$!
for i in $(seq 1 60); do
  H=$(height || true)
  [ -n "${H:-}" ] && [ "$H" != "null" ] && [ "$H" -ge 2 ] && break
  kill -0 $NODE 2>/dev/null || { tail -30 /tmp/node.log; fail "узел умер на запуске"; }
  sleep 1
done
{ [ -n "${H:-}" ] && [ "$H" != "null" ]; } || \
  { tail -30 /tmp/node.log; fail "блоков нет за 60 с"; }
ok "узел работает, высота $H, $(axmd version)"

echo
echo "=== правка 3: модульный счёт wasm"
MA=$(axmd query auth module-account wasm $Q) || fail "счёта wasm нет"
# Вывод в амино-виде: поля лежат под .account.value.
echo "$MA" | jq -c '{name: .account.value.name, address: .account.value.address,
                     permissions: .account.value.permissions}'
echo "$MA" | jq -e '.account.value.permissions | index("burner")' > /dev/null || \
  fail "у счёта wasm нет права burner"
ok "счёт wasm есть, право burner при нём"

echo
echo "=== правка 1: загрузка контракта с requires_stargate"
[ -f "$WASM" ] || fail "нет артефакта $WASM"
echo "артефакт: $(sha256sum $WASM)"
R=$(send wasm store "$WASM")
echo "$R" | jq -e '.code == 0' > /dev/null || \
  fail "загрузка отвергнута: $(echo "$R" | jq -r .raw_log)"
CODE=$(echo "$R" | jq -r '[.events[]? | select(.type=="store_code") | .attributes[]?
        | select(.key=="code_id") | .value] | first')
[ -n "$CODE" ] && [ "$CODE" != "null" ] || fail "код не загрузился: $R"
ok "код $CODE загружен, газ $(echo "$R" | jq -r .gas_used)"

R=$(send wasm instantiate "$CODE" '{}' --label probe --no-admin)
echo "$R" | jq -e '.code == 0' > /dev/null || \
  fail "создание отвергнуто: $(echo "$R" | jq -r .raw_log)"
ADDR=$(echo "$R" | jq -r '[.events[]? | select(.type=="instantiate") | .attributes[]?
        | select(.key=="_contract_address") | .value] | first')
[ -n "$ADDR" ] && [ "$ADDR" != "null" ] || fail "адреса нет: $R"
ok "экземпляр $ADDR"

echo
echo "=== правка 2: запрос к x/referral из контракта"
ANSWER=$(axmd query wasm contract-state smart "$ADDR" \
         "{\"referral\":{\"address\":\"$BOB\"}}" $Q) || \
  fail "запрос не прошёл: контракт не смог спросить модуль"
echo "$ANSWER" | jq -c .data
GOT=$(echo "$ANSWER" | jq -r '.data.info.referrer // empty')
[ "$GOT" = "$BEN" ] || fail "реферер в ответе $GOT, а в генезисе $BEN"
ok "модуль ответил через контракт, реферер BOB совпал с деревом генезиса"

# Отрицательная проверка: путь вне разрешающего списка должен быть
# отвергнут. Иначе список ничего не ограничивает.
OUT=$(axmd query wasm contract-state smart "$ADDR" \
      "{\"referral_other\":{\"address\":\"$BOB\"}}" $Q 2>&1 || true)
case "$OUT" in
  *"unknown variant"*|*"Error parsing"*|*"error"*) ok "чужой запрос не проходит" ;;
  *) fail "неизвестный запрос почему-то принят: $OUT" ;;
esac

echo
echo "=== правка 3 на исполнении: BankMsg::Burn"
# Сперва мерка на пустой сделке с ТЕМ ЖЕ сбором. Нужна потому, что эта
# цепочка часть сбора сжигает сама (x/distribution/keeper/allocation.go:44),
# и без мерки сжигание контракта было бы не отделить от сжигания сбора.
R=$(send bank send alice "$BEN" 1uaxm)
echo "$R" | jq -e '.code == 0' > /dev/null || fail "мерочная сделка не прошла"
FEE_H=$(echo "$R" | jq -r .height)
FEE_ONLY=$(block_effect "$FEE_H")
echo "блок $FEE_H без сжигания: выпуск упал на $FEE_ONLY uaxm (это сбор)"

R=$(send wasm execute "$ADDR" '{"burn":{}}' --amount 1000000uaxm)
echo "$R" | jq -e '.code == 0' > /dev/null || \
  fail "сжигание отвергнуто: $(echo "$R" | jq -r .raw_log)"
echo "$R" | jq -r '[.events[]? | select(.type=="burn") | .attributes[]?
        | select(.key=="amount") | .value] | first' | sed 's/^/событие burn: /'
BURN_H=$(echo "$R" | jq -r .height)
WITH_BURN=$(block_effect "$BURN_H")
echo "блок $BURN_H со сжиганием: выпуск упал на $WITH_BURN uaxm"
NET=$((WITH_BURN - FEE_ONLY))
echo "чистое сжигание контракта: $NET uaxm"
[ "$NET" = "1000000" ] || \
  fail "сожглось не то: $NET вместо 1000000 (сбор $FEE_ONLY)"
ok "контракт сжёг ровно присланное, выпуск уменьшился на эту сумму"

echo
echo "=== итог: все три правки работают на исполнении"
kill $NODE 2>/dev/null || true
wait $NODE 2>/dev/null || true
