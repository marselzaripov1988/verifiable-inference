#!/bin/sh
# Прогон полного цикла протокола на местном девнете.
#
# Образ wasmd v0.50.0 выбран намеренно: у него libwasmvm 1.5.0 — та же
# пара, что у Axiome. На другой версии проверка была бы про другую
# цепочку.
#
# Нужны: docker и собранный артефакт (protocol/contract/artifacts/).
#
#   sh protocol/devnet/run.sh
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ART="$HERE/../contract/artifacts/inference_registry.wasm"
[ -f "$ART" ] || { echo "нет артефакта: собрать cosmwasm/optimizer" >&2; exit 1; }
WORK=$(mktemp -d)
cp "$ART" "$WORK/"
cp "$HERE/cycle.sh" "$WORK/"
# Расчётная единица для прогона — эталонный cw20_base, не наш код.
[ -f "$HERE/cw20_base.wasm" ] || \
  curl -sL -o "$HERE/cw20_base.wasm" \
    https://github.com/CosmWasm/cw-plus/releases/download/v1.1.2/cw20_base.wasm
cp "$HERE/cw20_base.wasm" "$WORK/"
docker run --rm --entrypoint sh -v "$WORK:/work" cosmwasm/wasmd:v0.50.0 -c '
for i in 1 2 3; do apk add --no-cache jq >/dev/null 2>&1 && break; sleep 2; done
command -v jq >/dev/null || { echo "ПРОВАЛ: jq не поставился" >&2; exit 1; }
sh /work/cycle.sh'
