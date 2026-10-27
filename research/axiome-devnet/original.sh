#!/usr/bin/env bash
# Отрицательный контроль: ТОТ ЖЕ контракт на ОФИЦИАЛЬНОМ axmd 2.2.1.
#
# Без этого шага замер ничего не значит: «загрузилось на исправленном
# узле» доказывает правку только если на неисправленном не загружается.
# Берётся не своя сборка без правок, а выпущенный двоичный файл — тот
# самый, что работает на axiome-1, со сверкой sha256.
#
# Ожидание: загрузка ОТВЕРГНУТА из-за отсутствующей возможности.
set -eu

WASM=/work/stargate_probe.wasm
URL=https://github.com/axiome-pro/axm-node/releases/download/v2.2.1/axmd-linux-amd64-2.2.1
SUM=c8ef347f91b1378225f102c1124efcdc81fe6bb83ae2223fd8175df2041c0d4d
BIN=/tmp/axmd-original
export HOME=/tmp/original-home

fail() { echo "ПРОВАЛ: $*" >&2; exit 1; }
ok() { echo "ок: $*"; }

echo "=== 0. официальный двоичный файл"
curl -sfL -o "$BIN" "$URL" || fail "выпуск не скачался"
echo "$SUM  $BIN" | sha256sum -c - || fail "sha256 не тот: это не выпуск 2.2.1"
chmod +x "$BIN"
ok "$($BIN version) совпал по sha256 с выпуском v2.2.1"

echo
echo "=== 1. набор возможностей у выпуска и у стенда"
# grep -ao, а не по строкам: набор лежит в общем блоке строк на
# десятки килобайт, и целая «строка» была бы нечитаема.
caps() { grep -ao 'iterator,staking[a-z0-9_,]*cosmwasm_1_4' "$1" | head -1; }
echo "выпуск: $(caps "$BIN")"
echo "стенд:  $(caps /usr/local/bin/axmd)"

echo
echo "=== 2. своя цепочка на официальном узле"
# Генезис строится ИХ ЖЕ сценарием, чтобы цепочки отличались только
# двоичным файлом. Сценарий зовёт `which axmd`, поэтому выпуск
# подставляется в PATH под этим именем.
mkdir -p /tmp/origbin
ln -sf "$BIN" /tmp/origbin/axmd
export PATH=/tmp/origbin:$PATH
rm -rf "$HOME/.axmd"
bash /axm-node/scripts/init-testchain.sh > /tmp/original-init.log 2>&1 || \
  { tail -20 /tmp/original-init.log; fail "генезис не собрался"; }
[ "$(command -v axmd)" = "/tmp/origbin/axmd" ] || fail "в PATH не выпуск"
"$BIN" start --minimum-gas-prices 0uaxm > /tmp/original-node.log 2>&1 &
NODE=$!
for i in $(seq 1 60); do
  H=$("$BIN" status 2>/dev/null | jq -r '[.. | objects | .latest_block_height? // empty] | first')
  [ -n "${H:-}" ] && [ "$H" != "null" ] && [ "$H" -ge 2 ] && break
  kill -0 $NODE 2>/dev/null || { head -30 /tmp/original-node.log; fail "узел умер"; }
  sleep 1
done
ok "официальный узел работает, высота $H"

echo
echo "=== 3. загрузка того же контракта"
OUT=$("$BIN" tx wasm store "$WASM" --from alice --chain-id demo \
      --keyring-backend test -y -o json --gas 50000000 --fees 500000uaxm \
      --broadcast-mode sync 2>&1 || true)
# Только поля, а не целиком: в ответе лежит base64 всего артефакта.
echo "$OUT" | jq -c '{code, codespace, txhash, raw_log}' 2>/dev/null || echo "$OUT"
case "$OUT" in
  *stargate*)
    ok "загрузка отвергнута из-за отсутствующей возможности stargate" ;;
  *)
    HASH=$(echo "$OUT" | jq -r .txhash 2>/dev/null || echo "")
    if [ -n "$HASH" ] && [ "$HASH" != "null" ]; then
      sleep 6
      R=$("$BIN" query tx "$HASH" -o json 2>&1 || true)
      echo "$R" | jq -c '{height, code, codespace, gas_used, raw_log}' 2>/dev/null \
        || echo "$R" | head -c 500
      case "$R" in
        *stargate*) ok "загрузка отвергнута при исполнении: нет возможности stargate" ;;
        *) fail "контракт приняли на неисправленном узле — правка 1 ничего не меняет" ;;
      esac
    else
      fail "непонятный ответ официального узла" ;
    fi ;;
esac

kill $NODE 2>/dev/null || true
wait $NODE 2>/dev/null || true
echo
echo "=== итог: один и тот же контракт — на выпуске отказ, на стенде загрузка"
