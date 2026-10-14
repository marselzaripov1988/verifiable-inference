#!/bin/sh
# Полный цикл протокола на местном девнете wasmd 0.50.0 / libwasmvm 1.5.0 —
# той же паре, что у Axiome.
#
# Проверяется то, чего не проверяют тесты на подставном окружении:
#   * контракт ЗАГРУЖАЕТСЯ в настоящую цепочку (а не просто собрался);
#   * путь Receive от настоящего CW20 работает;
#   * BankMsg::Burn действительно исполняется, а не только компилируется;
#   * балансы после выплат те, что обещаны;
#   * проба на эталонном векторе: ложная опровергается на настоящей
#     цепочке, со сверкой хэшей и нарезкой склейки по границам текстов.
#
# У каждого шага положительный признак успеха. Отсутствие ошибки в выводе
# признаком НЕ считается: на этом я уже обжигался дважды.
set -eu

CH=devnet-1
D=/root/.wasmd
B="wasmd --home $D"
TX="--chain-id $CH --keyring-backend test --home $D --gas 3000000 --fees 10000stake -y -o json"
# ВАЖНО: без --chain-id. Команды запроса его не принимают, и это ровно
# та ошибка, на которой сгорели две репетиции отката на Artery: там её
# съел tail, здесь она проявилась, потому что wait_tx требует
# ПОЛОЖИТЕЛЬНЫЙ признак, а не отсутствие ошибки в выводе.
Q="--home $D -o json"

say() { echo; echo "===== $* ====="; }
# jq обязателен. Без него каждый разбор вернёт пустоту, и проверки
# начнут считать пустоту неуспехом цепочки — ровно это и случилось,
# когда apk add молча не прошёл.
command -v jq >/dev/null 2>&1 || { echo "ПРОВАЛ: нет jq" >&2; exit 1; }
fail() { echo "ПРОВАЛ: $*" >&2; exit 1; }

# Ждём, пока транзакция попадёт в блок, и ТРЕБУЕМ code=0.
# Без этого «отправлено» принималось бы за «исполнено».
wait_tx() {
    h=$1
    i=0
    while [ $i -lt 40 ]; do
        sleep 1
        out=$($B query tx --type=hash "$h" $Q 2>/tmp/qerr) || { i=$((i+1)); continue; }
        code=$(echo "$out" | jq -r '.code')
        [ "$code" = "0" ] && { echo "$out"; return 0; }
        echo "$out" | jq -r '.raw_log' >&2
        fail "транзакция $h вернула code=$code"
    done
    echo "последняя ошибка запроса:" >&2; head -3 /tmp/qerr >&2
    fail "транзакция $h не попала в блок"
}

send() {
    h=$($B tx "$@" $TX | jq -r '.txhash')
    [ -n "$h" ] && [ "$h" != "null" ] || fail "транзакция не отправлена"
    wait_tx "$h" >/dev/null
    echo "$h"
}

say "1. поднять цепочку"
$B init devnet --chain-id $CH >/dev/null 2>&1
for n in admin attestor executor client c0 c1 c2 c3 liar; do
    $B keys add $n --keyring-backend test --home $D >/dev/null 2>&1
done
# Правим денom на stake во всём генезисе: иначе половина полей останется
# в umlg и узел не поднимется.
sed -i 's/"ustake"/"stake"/g; s/"umlg"/"stake"/g' $D/config/genesis.json
for n in admin attestor executor client c0 c1 c2 c3 liar; do
    a=$($B keys show $n -a --keyring-backend test --home $D)
    $B genesis add-genesis-account "$a" 100000000000stake --home $D >/dev/null 2>&1
done
$B genesis gentx admin 70000000000stake --chain-id $CH --keyring-backend test --home $D >/dev/null 2>&1
$B genesis collect-gentxs --home $D >/dev/null 2>&1
# Инфляция выключается, иначе замер сожжённого сбит: цепочка печатает
# награды в те же блоки, и чистое уменьшение выпуска вышло 49 703 415
# вместо 50 000 000. Правильнее убрать помеху, чем расширить допуск.
jq '.app_state.mint.minter.inflation="0.000000000000000000"
    | .app_state.mint.params.inflation_min="0.000000000000000000"
    | .app_state.mint.params.inflation_max="0.000000000000000000"
    | .app_state.mint.params.inflation_rate_change="0.000000000000000000"
    | .app_state.distribution.params.community_tax="0.000000000000000000"'     $D/config/genesis.json > /tmp/g.json && mv /tmp/g.json $D/config/genesis.json
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
echo "высота $hh — цепочка идёт"

ADMIN=$($B keys show admin -a --keyring-backend test --home $D)
ATT=$($B keys show attestor -a --keyring-backend test --home $D)
EXEC=$($B keys show executor -a --keyring-backend test --home $D)
CLIENT=$($B keys show client -a --keyring-backend test --home $D)

say "2. загрузить оба контракта"
send wasm store /work/cw20_base.wasm --from admin >/dev/null
send wasm store /work/inference_registry.wasm --from admin >/dev/null
CW20_CODE=$($B query wasm list-code $Q | jq -r '.code_infos[0].code_id')
REG_CODE=$($B query wasm list-code $Q | jq -r '.code_infos[1].code_id')
[ "$REG_CODE" = "2" ] || fail "контракт не загрузился (code_id=$REG_CODE)"
echo "cw20 code_id=$CW20_CODE, реестр code_id=$REG_CODE — ЗАГРУЗИЛСЯ в настоящую цепочку"

say "3. развернуть расчётную единицу"
send wasm instantiate "$CW20_CODE" "{\"name\":\"BARTR-test\",\"symbol\":\"BARTR\",\"decimals\":6,\"initial_balances\":[{\"address\":\"$CLIENT\",\"amount\":\"10000000\"}]}" --label bartr --no-admin --from admin >/dev/null
CW20=$($B query wasm list-contract-by-code "$CW20_CODE" $Q | jq -r '.contracts[0]')
bal=$($B query wasm contract-state smart "$CW20" '{"balance":{"address":"'$CLIENT'"}}' $Q | jq -r '.data.balance')
[ "$bal" = "10000000" ] || fail "у заказчика нет расчётной единицы: $bal"
echo "$CW20, у заказчика $bal"

say "4. развернуть реестр"
MIN_STAKE=50000000
# Эталон пробы для девнета: два текста по три числа. Хэши сняты каноном
# демона (protocol/daemon/canon.py). Разойдись они с контрактом —
# опровержение в шаге 11 не сошлось бы, и это было бы видно.
#   эталон   [-500000 x3, -400000 x3]
#   честный  каждый элемент -12700   -> максимум по текстам 12700
#   лжец     второй текст  -48000    -> максимум 48000, склейка 24000
CONF_LIMIT=25000
CONF_REF_HASH=651ad68e9d54554b787f1949fddbdbc35786f796848e12d817988468b9890225
CONF_HONEST_HASH=788a30dc15564183ef6f40e9f4dab26dc344555c1b2f1dd37e5d9e69bc47c42f
CONF_LIAR_HASH=a65355a94a1f443529e6db09861a235ee896070ad3d311aaee036fd668cfb37f
REF_VEC=[-500000,-500000,-500000,-400000,-400000,-400000]
HONEST_VEC=[-512700,-512700,-512700,-412700,-412700,-412700]
LIAR_VEC=[-500000,-500000,-500000,-448000,-448000,-448000]
send wasm instantiate "$REG_CODE" "{\"admin\":\"$ADMIN\",\"attestor\":\"$ATT\",\"threshold_micro\":36000,\"checks_required\":4,\"min_answer_tokens\":32,\"pinned_model\":\"Qwen/Qwen2.5-7B-Instruct\",\"pinned_runtime\":\"vllm-0.31.0\",\"pinned_dtype\":\"bfloat16\",\"settlement_denom\":\"cw20\",\"stake_denom\":\"stake\",\"settlement_cw20\":\"$CW20\",\"min_stake\":\"$MIN_STAKE\",\"verifier_fee_bps\":970,\"forfeit\":\"burn\",\"conformance_limit_micro\":$CONF_LIMIT,\"conformance_ref_hash\":\"$CONF_REF_HASH\",\"conformance_lens\":[3,3]}" --label registry --no-admin --from admin >/dev/null
REG=$($B query wasm list-contract-by-code "$REG_CODE" $Q | jq -r '.contracts[0]')
thr=$($B query wasm contract-state smart "$REG" '{"config":{}}' $Q | jq -r '.data.threshold_micro')
[ "$thr" = "36000" ] || fail "порог в цепочке $thr, ожидался 36000"
echo "$REG, порог из цепочки $thr"

say "5. исполнитель и проверяющие: регистрация, проба, залог, допуск"
# Проба требуется и от ПРОВЕРЯЮЩИХ: их расхождение сравнивается с тем
# же порогом, значит настройка у них должна быть той же. Поэтому
# c0..c3 регистрируются и проходят пробу здесь, а не берутся из
# воздуха, как было до пробы.
for n in executor c0 c1 c2 c3; do
    send wasm execute "$REG" '{"register_executor":{"model":"Qwen/Qwen2.5-7B-Instruct","runtime":"vllm-0.31.0","dtype":"bfloat16","card_class":"rtx4090"}}' --from $n >/dev/null
    send wasm execute "$REG" '{"submit_conformance":{"divergence_micro":12700,"logprobs_hash":"'$CONF_HONEST_HASH'"}}' --from $n >/dev/null
done
send wasm execute "$REG" '{"deposit_stake":{}}' --amount ${MIN_STAKE}stake --from executor >/dev/null
send wasm execute "$REG" "{\"attest\":{\"executor\":\"$EXEC\",\"eligible\":true}}" --from attestor >/dev/null
st=$($B query wasm contract-state smart "$REG" '{"executor":{"address":"'$EXEC'"}}' $Q | jq -r '.data.status + " " + .data.stake')
[ "$st" = "active $MIN_STAKE" ] || fail "исполнитель в состоянии [$st]"
cv=$($B query wasm contract-state smart "$REG" '{"executor":{"address":"'$EXEC'"}}' $Q | jq -r '(.data.conformance.divergence_micro|tostring) + " v" + (.data.conformance.ref_version|tostring)')
[ "$cv" = "12700 v1" ] || fail "проба исполнителя [$cv], ожидалась 12700 v1"
echo "состояние: $st, проба: $cv"

say "6. заказчик платит через Receive настоящего CW20"
PRICE=1000000
HOOK=$(echo -n '{"submit_job":{"prompt_hash":"ph1"}}' | base64 -w0)
send wasm execute "$CW20" "{\"send\":{\"contract\":\"$REG\",\"amount\":\"$PRICE\",\"msg\":\"$HOOK\"}}" --from client >/dev/null
price=$($B query wasm contract-state smart "$REG" '{"job":{"job_id":1}}' $Q | jq -r '.data.price')
[ "$price" = "$PRICE" ] || fail "цена задания $price, ожидалась $PRICE"
echo "задание 1 создано через Receive, цена $price — путь CW20 РАБОТАЕТ"

say "7. исполнитель берёт задание и сдаёт ответ"
send wasm execute "$REG" '{"claim_job":{"job_id":1}}' --from executor >/dev/null
send wasm execute "$REG" '{"submit_answer":{"job_id":1,"answer_hash":"ah1","logprobs_hash":"lh1","answer_tokens":120}}' --from executor >/dev/null
jst=$($B query wasm contract-state smart "$REG" '{"job":{"job_id":1}}' $Q | jq -r '.data.state')
[ "$jst" = "answered" ] || fail "задание в состоянии $jst"
echo "состояние задания: $jst"

say "8. четыре проверяющих, все ниже порога"
for n in c0 c1 c2 c3; do
    send wasm execute "$REG" '{"submit_check":{"job_id":1,"divergence_micro":12700,"logprobs_hash":"ch-'$n'"}}' --from $n >/dev/null
done
ex=$($B query wasm contract-state smart "$REG" '{"checks":{"job_id":1}}' $Q | jq -r '.data.exceeded')
[ "$ex" = "0" ] || fail "превысило $ex, ожидалось 0"
echo "собрано 4 проверки, превысило $ex"

say "9. свести вердикт и сверить ДЕНЬГИ"
before_exec=$($B query wasm contract-state smart "$CW20" '{"balance":{"address":"'$EXEC'"}}' $Q | jq -r '.data.balance')
send wasm execute "$REG" '{"finalize":{"job_id":1}}' --from admin >/dev/null
g=$($B query wasm contract-state smart "$REG" '{"job":{"job_id":1}}' $Q | jq -r '.data.guilty')
[ "$g" = "false" ] || fail "вердикт guilty=$g, ожидался false"
# 970 bps от 1 000 000 = 97 000, на четверых по 24 250 = 97 000 ровно
SHARE=24250
after_exec=$($B query wasm contract-state smart "$CW20" '{"balance":{"address":"'$EXEC'"}}' $Q | jq -r '.data.balance')
got=$((after_exec - before_exec))
want=$((PRICE - SHARE * 4))
[ "$got" = "$want" ] || fail "исполнителю пришло $got, ожидалось $want"
c0a=$($B keys show c0 -a --keyring-backend test --home $D)
cb=$($B query wasm contract-state smart "$CW20" '{"balance":{"address":"'$c0a'"}}' $Q | jq -r '.data.balance')
[ "$cb" = "$SHARE" ] || fail "проверяющему пришло $cb, ожидалось $SHARE"
echo "исполнителю $got, каждому проверяющему $cb — ВЫПЛАТЫ СОШЛИСЬ"

say "10. виновный: изъятие залога через BankMsg::Burn"
# total-supply-of, а не total --denom: этого флага в 0.50 нет, и пустой
# результат прочитался бы как «сожжено 0» — то есть провал выглядел бы
# успехом. Поэтому результат ещё и проверяется на непустоту.
SUP_BEFORE=$($B query bank total-supply-of stake $Q | jq -r '.amount.amount')
[ -n "$SUP_BEFORE" ] && [ "$SUP_BEFORE" != "null" ] || fail "выпуск до не прочитан"
# c3 зарегистрирован и прошёл пробу в шаге 5 как проверяющий; здесь
# ему нужны только залог и допуск, чтобы брать задания.
send wasm execute "$REG" '{"deposit_stake":{}}' --amount ${MIN_STAKE}stake --from c3 >/dev/null
C3=$($B keys show c3 -a --keyring-backend test --home $D)
send wasm execute "$REG" "{\"attest\":{\"executor\":\"$C3\",\"eligible\":true}}" --from attestor >/dev/null
send wasm execute "$CW20" "{\"send\":{\"contract\":\"$REG\",\"amount\":\"$PRICE\",\"msg\":\"$HOOK\"}}" --from client >/dev/null
send wasm execute "$REG" '{"claim_job":{"job_id":2}}' --from c3 >/dev/null
send wasm execute "$REG" '{"submit_answer":{"job_id":2,"answer_hash":"ah2","logprobs_hash":"lh2","answer_tokens":120}}' --from c3 >/dev/null
for n in c0 c1 c2 executor; do
    send wasm execute "$REG" '{"submit_check":{"job_id":2,"divergence_micro":37780,"logprobs_hash":"x-'$n'"}}' --from $n >/dev/null
done
send wasm execute "$REG" '{"finalize":{"job_id":2}}' --from admin >/dev/null
g2=$($B query wasm contract-state smart "$REG" '{"job":{"job_id":2}}' $Q | jq -r '.data.guilty')
[ "$g2" = "true" ] || fail "вердикт по подлогу guilty=$g2"
st3=$($B query wasm contract-state smart "$REG" '{"executor":{"address":"'$C3'"}}' $Q | jq -r '.data.status + " " + .data.stake')
[ "$st3" = "banned 0" ] || fail "виновный в состоянии [$st3]"
SUP_AFTER=$($B query bank total-supply-of stake $Q | jq -r '.amount.amount')
[ -n "$SUP_AFTER" ] && [ "$SUP_AFTER" != "null" ] || fail "выпуск после не прочитан"
BURNED=$((SUP_BEFORE - SUP_AFTER))
echo "вердикт: виновен, состояние: $st3"
echo "выпуск stake: $SUP_BEFORE -> $SUP_AFTER, сожжено $BURNED"
[ "$BURNED" = "$MIN_STAKE" ] || fail "сожжено $BURNED, залог $MIN_STAKE — не сходится"
echo "BankMsg::Burn РАБОТАЕТ: выпуск уменьшился РОВНО на залог"

say "11. ложная проба опровергается"
# Самое важное свойство правила пробы: статистика — МАКСИМУМ по текстам,
# а не среднее по склейке. Лжец называет 24 000 (среднее по склейке, ниже
# предела 25 000), а максимум по текстам у него 48 000.
#
# Среднее не годится потому, что неверный режим исполнения портит РЕДКИЕ
# тексты: замерено, что у eager 29 векторов из 40 совпали с собой
# побитово, а остальные разошлись до 413 488. Среднее такую порчу
# размывает — зазор падает с 20x до 1,5x.
send wasm execute "$REG" '{"register_executor":{"model":"Qwen/Qwen2.5-7B-Instruct","runtime":"vllm-0.31.0","dtype":"bfloat16","card_class":"rtx4090"}}' --from liar >/dev/null
send wasm execute "$REG" '{"submit_conformance":{"divergence_micro":24000,"logprobs_hash":"'$CONF_LIAR_HASH'"}}' --from liar >/dev/null
LIAR=$($B keys show liar -a --keyring-backend test --home $D)
cl=$($B query wasm contract-state smart "$REG" '{"executor":{"address":"'$LIAR'"}}' $Q | jq -r '.data.conformance.divergence_micro|tostring')
[ "$cl" = "24000" ] || fail "проба лжеца принята как [$cl], ожидалось 24000"
echo "лжец заявил $cl — принято, предел $CONF_LIMIT не превышен"
h=$(send wasm execute "$REG" '{"dispute_conformance":{"executor":"'$LIAR'","reference_logprobs":'$REF_VEC',"executor_logprobs":'$LIAR_VEC'}}' --from admin)
ev=$($B query tx --type=hash "$h" $Q)
lied=$(echo "$ev" | jq -r '[.events[].attributes[]?|select(.key=="lied").value]|first')
actual=$(echo "$ev" | jq -r '[.events[].attributes[]?|select(.key=="actual_micro").value]|first')
[ "$lied" = "true" ] || fail "опровержение дало lied=[$lied], ожидалось true"
[ "$actual" = "48000" ] || fail "пересчитано [$actual], ожидалось 48000 — максимум по текстам"
stl=$($B query wasm contract-state smart "$REG" '{"executor":{"address":"'$LIAR'"}}' $Q | jq -r '.data.status')
[ "$stl" = "banned" ] || fail "лжец в состоянии [$stl], ожидалось banned"
echo "пересчёт на цепочке: $actual, а не 24000 — ЛОЖЬ ПОЙМАНА, статус $stl"

say "12. честная проба опровержение подтверждает"
# Иначе опровержением можно было бы валить честных.
h=$(send wasm execute "$REG" '{"dispute_conformance":{"executor":"'$EXEC'","reference_logprobs":'$REF_VEC',"executor_logprobs":'$HONEST_VEC'}}' --from admin)
lied2=$($B query tx --type=hash "$h" $Q | jq -r '[.events[].attributes[]?|select(.key=="lied").value]|first')
[ "$lied2" = "false" ] || fail "честная проба дала lied=[$lied2]"
ste=$($B query wasm contract-state smart "$REG" '{"executor":{"address":"'$EXEC'"}}' $Q | jq -r '.data.status')
[ "$ste" = "active" ] || fail "честный исполнитель в состоянии [$ste]"
echo "честного опровержение не тронуло: lied=$lied2, статус $ste"

say "ГОТОВО: полный цикл пройден на настоящей цепочке"
