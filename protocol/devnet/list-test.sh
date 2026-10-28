#!/bin/sh
# РЕПЕТИЦИЯ ТЕСТОВОГО ЭКЗЕМПЛЯРА: развернуть ЛИСТОМ, который пойдёт в
# axiome-1, и провести по нему задание тремя узлами.
#
# Зачем. Трёх узлов на настоящих картах не проверял никто: залог
# 30 400 AXM — три такие суммы из своего кармана. Тестовый экземпляр с
# залогом 0,1 AXM это снимает, но стоит 0,45 AXM, и лист с опечаткой
# выяснился бы на живой цепочке и за газ. Здесь он разворачивается
# бесплатно, на той же паре wasmd 0.50.0 / libwasmvm 1.5.0, что у Axiome.
#
# Главное отличие от loop.sh: настройки НЕ пишутся в сценарии. Контракт
# разворачивается самим файлом deploy/instantiate-test.json, а узлы
# регистрируются полями, прочитанными ИЗ НЕГО же. Разойдись лист с
# контрактом — прогон встанет здесь, а не в цепочке.
#
# Модели здесь нет: обязательство пробы — это хэш, и контракт его не
# пересчитывает (пересчёт бывает только при споре). Поэтому проверяются
# ДЕНЬГИ, допуск и жизненный цикл задания с настоящей расчётной единицей
# uaxm и настоящим залогом из листа.
#
# У каждого шага положительный признак.
set -eu

CH=devnet-1
D=/root/.wasmd
B="wasmd --home $D"
# Плата за газ в stake и нулевая, чтобы не примешивалась к uaxm: расчёт и
# залог в листе идут в uaxm, и считать их надо чисто.
TX="--chain-id $CH --keyring-backend test --home $D --gas 3000000 --fees 0stake -y -o json"
Q="--home $D -o json"
LIST=/work/instantiate-test.json
PRICE=1000000
BPS=485
FEE=$(( (PRICE * BPS + 9999) / 10000 ))
TOTAL=$((PRICE + FEE))
SHARE=$((FEE / 2))
AAAAS=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
MINE=1111111111111111111111111111111111111111111111111111111111111111

say() { echo; echo "===== $* ====="; }
fail() { echo "ПРОВАЛ: $*" >&2; exit 1; }
ok() { echo "ок: $*"; }

command -v jq >/dev/null 2>&1 || fail "нет jq"
[ -f "$LIST" ] || fail "нет листа $LIST"

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

# Баланс В РАСЧЁТНОЙ ЕДИНИЦЕ ЛИСТА, а не в монете газа.
bal() {
    v=$($B query bank balances "$1" $Q | jq -r ".balances[]|select(.denom==\"$DENOM\").amount")
    echo "${v:-0}"
}

# Поля берутся ИЗ ЛИСТА: сценарий не знает их и знать не должен.
f() { jq -r "$1" "$LIST"; }

DENOM=$(f '.stake_denom')
MIN_STAKE=$(f '.min_stake')
MODEL=$(f '.pinned_model')
RUNTIME=$(f '.pinned_runtime')
DTYPE=$(f '.pinned_dtype')
ARCH=$(f '.pinned_arch')
CACHE=$(f '.kernel_cache_hash')

say "0. что в листе"
ok "класс $ARCH, залог $MIN_STAKE $DENOM, расчёт $(f '.settlement|keys[0]'):$(f '.settlement|to_entries[0].value')"
ok "кеш ядер $CACHE"
ok "проба: предел $(f '.conformance_limit_micro'), текстов $(f '.conformance_lens|length'), эталон $(f '.conformance_ref_hash')"
[ "$MIN_STAKE" = "100000" ] || fail "в листе залог $MIN_STAKE — это не тестовый лист"

say "1. поднять цепочку"
$B init devnet --chain-id $CH >/dev/null 2>&1
for n in admin client n1 n2 n3; do
    $B keys add $n --keyring-backend test --home $D >/dev/null 2>&1
done
sed -i 's/"ustake"/"stake"/g; s/"umlg"/"stake"/g' $D/config/genesis.json
# Две монеты у каждого: stake на газ и валидатора, uaxm — расчётная
# единица листа. Так прогон идёт в ТОЙ ЖЕ единице, что в axiome-1.
for n in admin client n1 n2 n3; do
    a=$($B keys show $n -a --keyring-backend test --home $D)
    $B genesis add-genesis-account "$a" 100000000000stake,100000000000$DENOM --home $D >/dev/null 2>&1
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
ok "высота $hh, в обороте две монеты: stake на газ и $DENOM на расчёт"

CLIENT=$($B keys show client -a --keyring-backend test --home $D)
A1=$($B keys show n1 -a --keyring-backend test --home $D)
A2=$($B keys show n2 -a --keyring-backend test --home $D)
A3=$($B keys show n3 -a --keyring-backend test --home $D)

say "2. развернуть ЛИСТОМ, а не настройками из сценария"
send wasm store /work/inference_registry.wasm --from admin
CODE=$($B query wasm list-code $Q | jq -r '.code_infos[-1].code_id')
[ -n "$CODE" ] && [ "$CODE" != "null" ] || fail "контракт не загрузился"
# Админ в листе — боевой адрес axm1…, в этой цепочке его нет. Для
# развёртывания это неважно: adrr_validate проверяет ФОРМУ, а префикс у
# wasmd другой, поэтому адрес подставляется местный. Единственное поле,
# которое прогон обязан заменить, и оно названо.
ADMIN=$($B keys show admin -a --keyring-backend test --home $D)
FORFEIT=$ADMIN
jq --arg a "$ADMIN" --arg f "$FORFEIT" \
   '.admin=$a | .forfeit={"to_address":$f}' "$LIST" > /tmp/msg.json
send wasm instantiate "$CODE" "$(cat /tmp/msg.json)" --label registry-test --admin "$ADMIN" --from admin
REG=$($B query wasm list-contract-by-code "$CODE" $Q | jq -r '.contracts[0]')
ok "развёрнут $REG"

# Признак: настройки в цепочке совпадают с листом ПОЛЕ ЗА ПОЛЕМ. Сверяются
# все, кроме двух заменённых адресов.
$B query wasm contract-state smart "$REG" '{"config":{}}' $Q | jq -S '.data' > /tmp/got.json
jq -S 'del(.admin,.forfeit) | .conformance_version=1' /tmp/msg.json > /tmp/want.json
jq -S 'del(.admin,.forfeit)' /tmp/got.json > /tmp/got-cmp.json
diff /tmp/want.json /tmp/got-cmp.json >/dev/null || {
    diff /tmp/want.json /tmp/got-cmp.json; fail "настройки в цепочке разошлись с листом"; }
ok "настройки в цепочке равны листу поле за полем (кроме двух местных адресов)"

say "3. три узла по полям ИЗ ЛИСТА, с залогом из листа"
for n in n1 n2 n3; do
    send wasm execute "$REG" "{\"register_executor\":{\"model\":\"$MODEL\",\"runtime\":\"$RUNTIME\",\"dtype\":\"$DTYPE\",\"card_class\":\"$ARCH\",\"kernel_cache_hash\":\"$CACHE\"}}" --from $n
    send wasm execute "$REG" '{"deposit_stake":{}}' --amount ${MIN_STAKE}${DENOM} --from $n
    send wasm execute "$REG" "{\"submit_conformance\":{\"divergence_micro\":0,\"logprobs_hash\":\"$AAAAS\"}}" --from $n
done
send wasm execute "$REG" '{"set_endpoint":{"endpoint":"http://127.0.0.1:8081"}}' --from n1
for a in $A1 $A2 $A3; do
    st=$($B query wasm contract-state smart "$REG" "{\"executor\":{\"address\":\"$a\"}}" $Q | jq -r '.data.status')
    sk=$($B query wasm contract-state smart "$REG" "{\"executor\":{\"address\":\"$a\"}}" $Q | jq -r '.data.stake')
    [ "$st" = "active" ] && [ "$sk" = "$MIN_STAKE" ] || fail "$a: статус $st, залог $sk"
done
ok "три узла активны, у каждого залог $MIN_STAKE $DENOM — это 0,1 AXM, а не 30 400"

say "4. задание проходит путь до вердикта"
b1=$(bal "$A1")
send wasm execute "$REG" "{\"submit_job\":{\"prompt_hash\":\"$MINE\",\"prompt_uri\":\"http://127.0.0.1:8080\",\"price\":\"$PRICE\"}}" --amount ${TOTAL}${DENOM} --from client
send wasm execute "$REG" '{"claim_job":{"job_id":1}}' --from n1
send wasm execute "$REG" "{\"submit_answer\":{\"job_id\":1,\"answer_hash\":\"$MINE\",\"logprobs_hash\":\"$MINE\",\"answer_tokens\":120}}" --from n1
for n in n2 n3; do
    send wasm execute "$REG" "{\"submit_check\":{\"job_id\":1,\"divergence_micro\":0,\"logprobs_hash\":\"$MINE\"}}" --from $n
done
send wasm execute "$REG" '{"finalize":{"job_id":1}}' --from client
g=$($B query wasm contract-state smart "$REG" '{"job":{"job_id":1}}' $Q | jq -r '.data.guilty')
[ "$g" = "false" ] || fail "вердикт guilty=$g"
ok "вердикт: не виновен — на тестовом залоге правило то же"

b2=$(bal "$A1")
[ "$((b2 - b1))" = "$PRICE" ] || fail "исполнителю пришло $((b2 - b1)), ожидалось $PRICE"
ok "исполнителю пришла цена $PRICE $DENOM"
for a in $A2 $A3; do
    v=$(bal "$a")
    want=$((100000000000 - MIN_STAKE + SHARE))
    [ "$v" = "$want" ] || fail "проверяющему $a: баланс $v, ожидался $want"
done
ok "каждому проверяющему доля $SHARE $DENOM"

say "5. залог возвращается — тот же путь, что на боевом"
cb=$(bal "$REG")
[ "$cb" = "$((MIN_STAKE * 3))" ] || fail "на контракте $cb, ожидались только залоги"
for n in n1 n2 n3; do
    send wasm execute "$REG" "{\"withdraw_stake\":{\"amount\":\"$MIN_STAKE\"}}" --from $n
done
cb=$(bal "$REG")
[ "$cb" = "0" ] || fail "на контракте осталось $cb"
ok "три залога вернулись, на контракте ноль"

say "ГОТОВО: лист разворачивается, и на нём проходит всё, кроме веса залога"
