#!/bin/bash
# Поднять узел протокола. Внутри образа; оператор задаёт два значения.
#
#   docker run --gpus all -e KEY_HEX=… -e ENDPOINT=http://адрес:порт …
#
# У каждого шага положительный признак. Отсутствие ошибки признаком НЕ
# считается: в этом проекте на том горели трижды.
#
# Порядок не произволен. Кеш ядер ставится ДО первого запуска сервера:
# vLLM выбирает ядра замером на машине, и при другом выборе векторы
# расходятся, а вердикт выносится равенством хэшей.
set -uo pipefail

REST=${REST:-https://axm-lcd.trickle.pro}
RPC=${RPC:-https://api-chain.axiomechain.org}
CHAIN_ID=${CHAIN_ID:-axiome-1}
GAS_PRICES=${GAS_PRICES:-0.0025uaxm}
MODEL=${MODEL:-Qwen/Qwen2.5-7B-Instruct}
DTYPE=${DTYPE:-bfloat16}
MAX_TOKENS=${MAX_TOKENS:-200}
EVERY=${EVERY:-15}
MIN_LEFT=${MIN_LEFT:-120}
DAEMON=/opt/node/daemon
BUNDLES=$NODE_HOME/bundles
PROMPTS=$NODE_HOME/prompts

say() { echo; echo "===== $* ====="; }
fail() { echo "ОТКАЗ: $*" >&2; exit 1; }
ok() { echo "ок: $*"; }

say "0. что задано"
[ -n "${CONTRACT:-}" ] || fail "нужен CONTRACT=axm1… — адрес реестра своего класса"
[ -n "${ENDPOINT:-}" ] || fail "нужен ENDPOINT=http://ваш-адрес:порт, по которому ВИДНО снаружи.
  Изнутри его не определить: на арендованных машинах наружу смотрит другой порт,
  и узел не может достать даже сам себя по своему публичному адресу (NAT)."
case "$ENDPOINT" in
    http://*|https://*) : ;;
    *) fail "ENDPOINT должен начинаться с http:// или https://" ;;
esac
ok "реестр $CONTRACT, адрес доставки $ENDPOINT"

say "1. карта и класс"
command -v nvidia-smi >/dev/null || fail "нет nvidia-smi: запускайте с --gpus all"
CARD=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)
DRIVER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)
[ -n "$CARD" ] || fail "карта не определилась"
case "$CARD" in
    *4090*) CLASS=sm_89 ;;
    *3090*) CLASS=sm_86 ;;
    *5090*) CLASS=sm_120 ;;
    *) fail "карта «$CARD» не из трёх поддерживаемых моделей (RTX 4090, 3090, 5090).
  Класс — это МОДЕЛЬ карты, а не архитектура: кеш ядер с 4090 замерно не
  принимается ни на RTX 4500 Ada, ни на L40S." ;;
esac
ok "$CARD, драйвер $DRIVER → класс $CLASS"

say "2. класс сверяется с цепочкой"
CHAIN_CLASS=$(python3 - <<PY
import sys
sys.path.insert(0, "$DAEMON")
import chain
try:
    p = chain.load_params("$REST", "$CONTRACT")
except Exception as e:
    print("ОШИБКА", e, file=sys.stderr); raise SystemExit(1)
print(p.pinned_arch)
PY
) || fail "настройки из цепочки не прочитаны: проверьте CONTRACT и REST"
[ "$CHAIN_CLASS" = "$CLASS" ] || fail "у реестра класс $CHAIN_CLASS, а карта $CLASS.
  Возьмите адрес своего класса — см. таблицу в protocol/УЗЕЛ.md."
ok "реестр того же класса: $CHAIN_CLASS"

say "3. кеш ядер — до запуска сервера"
want=$(cut -d' ' -f1 "/opt/node/cache/$CLASS.sha256")
got=$(sha256sum "/opt/node/cache/$CLASS.tar.gz" | cut -d' ' -f1)
[ "$want" = "$got" ] || fail "архив кеша в образе испорчен: $got против $want"
rm -rf "$NODE_HOME/.cache/vllm"
mkdir -p "$NODE_HOME/.cache"
tar xzf "/opt/node/cache/$CLASS.tar.gz" -C "$NODE_HOME/.cache" --no-same-owner \
    || fail "кеш не распакован"
ok "кеш $CLASS на месте, $(du -sm "$NODE_HOME/.cache/vllm" | cut -f1) МБ, sha256 сошёлся"

say "4. vLLM с прибитыми флагами"
# Пусковая команда отдельным файлом, а не строкой: pkill -f по ssh иначе
# убивает собственную оболочку — проверено дважды.
cat > "$NODE_HOME/serve.sh" <<SH
#!/bin/bash
exec vllm serve "$MODEL" \
  --dtype "$DTYPE" --max-model-len 4096 --gpu-memory-utilization 0.92 \
  --no-enable-prefix-caching --seed 0 --host 127.0.0.1 --port "$VLLM_PORT" \
  --no-enable-flashinfer-autotune \
  --compilation-config '{"inductor_compile_config": {
      "enable_auto_functionalized_v2": false,
      "combo_kernels": false,
      "benchmark_combo_kernel": false}}'
SH
chmod +x "$NODE_HOME/serve.sh"
# Из каталога без питоновских файлов: vllm.py рядом затенит пакет.
( cd "$NODE_HOME" && setsid nohup "$NODE_HOME/serve.sh" > "$NODE_HOME/srv.log" 2>&1 < /dev/null & )

i=0
while [ $i -lt 120 ]; do
    sleep 5
    v=$(curl -s --max-time 5 "http://127.0.0.1:$VLLM_PORT/version" 2>/dev/null)
    case "$v" in *0.31.0*) break ;; esac
    i=$((i+1))
done
case "${v:-}" in
    *0.31.0*) ok "сервер отвечает: $v" ;;
    *) tail -20 "$NODE_HOME/srv.log"; fail "сервер не поднялся за 10 минут" ;;
esac
# Главный признак второго шага, и проверять его надо здесь: строки о
# сборке графа означают, что кеш не подошёл и узел вне класса.
n=$(grep -c "Compiling a graph" "$NODE_HOME/srv.log")
[ "$n" = "0" ] || fail "в журнале $n строк «Compiling a graph»: ядра собраны заново, узел вне класса"
ok "ядра не пересобирались — кеш принят"

say "5. проба на эталонном векторе"
mkdir -p "$BUNDLES" "$PROMPTS"
cd "$DAEMON" || fail "нет каталога демонов"
python3 conformance.py --base-url "http://127.0.0.1:$VLLM_PORT/v1" \
    --model "$MODEL" --dtype "$DTYPE" \
    --rest "$REST" --contract "$CONTRACT" > "$NODE_HOME/conf.json" \
    || { fail "проба не пройдена — смотрите вывод выше. Деньги вносить НЕ НАДО"; }
ok "проба пройдена, сообщение в $NODE_HOME/conf.json"

say "6. ключ"
[ -n "${KEY_HEX:-}" ] || fail "нужен KEY_HEX=<64 шестнадцатеричных знака> — ключ РАБОЧЕГО адреса.
  Ключ с залогом сюда везти не нужно: раздел 9 в protocol/УЗЕЛ.md —
  холодный адрес выдаёт рабочему право только на рабочие сообщения."
[ ${#KEY_HEX} -eq 64 ] || fail "KEY_HEX длиной ${#KEY_HEX}, нужно 64"
# Запись с таким именем могла остаться с прошлого запуска: связка живёт в
# $NODE_HOME, а он бывает томом. `import-hex` на существующем имени
# падает, и запуск вставал на этом месте — поймано прогоном 29 октября.
# Ключ всё равно приходит из KEY_HEX, поэтому прежнюю запись сносим.
axmd keys delete node --keyring-backend test -y >/dev/null 2>&1 || true
err=$(axmd keys import-hex node "$KEY_HEX" --keyring-backend test 2>&1) \
    || fail "ключ в связку не внесён: $err"
KEY_HEX=
ME=$(axmd keys show node -a --keyring-backend test) || fail "адрес из ключа не получен"
ok "рабочий адрес $ME"

say "7. раздача связок"
( cd "$BUNDLES" && setsid nohup python3 -m http.server "$BUNDLE_PORT" > "$NODE_HOME/http.log" 2>&1 < /dev/null & )
sleep 2
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$BUNDLE_PORT/")
[ "$code" = "200" ] || fail "раздача связок не поднялась (ответ $code)"
ok "связки раздаются на $BUNDLE_PORT; снаружи это должен быть $ENDPOINT"

say "8. петля"
echo "  дальше работает loop.py; журнал идёт сюда же"
exec python3 -u loop.py \
    --rest "$REST" --contract "$CONTRACT" \
    --base-url "http://127.0.0.1:$VLLM_PORT/v1" \
    --model "$MODEL" --dtype "$DTYPE" \
    --send --key node --binary axmd \
    --node "$RPC" --chain-id "$CHAIN_ID" --keyring-backend test \
    --gas-prices "$GAS_PRICES" \
    --endpoint "$ENDPOINT" \
    --prompts "$PROMPTS" --bundles "$BUNDLES" \
    --state "$NODE_HOME/loop-state.json" \
    --every "$EVERY" --max-tokens "$MAX_TOKENS" --min-time-left "$MIN_LEFT"
