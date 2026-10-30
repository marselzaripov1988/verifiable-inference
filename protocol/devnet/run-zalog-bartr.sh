#!/bin/sh
# Прогон ЗАЛОГА В BARTR на местном девнете. Одна команда.
#
#   sh protocol/devnet/run-zalog-bartr.sh
#
# Образ wasmd v0.50.0 — та же пара с libwasmvm 1.5.0, что у Axiome.
# BARTR здесь — cw20_base: у живого BARTR (code_id 144) запрос отвечает
# разбором `cw20_base::msg::QueryMsg`, и в байткоде есть `send` и
# `Cw20ReceiveMsg`.
#
# Нужны docker, новый артефакт (protocol/contract/artifacts/) и прежний,
# с кодом 343, рядом под именем inference_registry-343.wasm — перенос
# репетируется с него.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ART="$HERE/../contract/artifacts/inference_registry.wasm"
OLD="$HERE/../contract/artifacts/inference_registry-343.wasm"
[ -f "$ART" ] || { echo "нет артефакта: собрать cosmwasm/optimizer" >&2; exit 1; }
[ -f "$OLD" ] || { echo "нет прежнего артефакта (код 343): $OLD" >&2; exit 1; }

# Путь для docker: на Windows он принимает C:/…, а /tmp/… понимает как
# путь внутри своей виртуальной машины и молча монтирует пустоту.
dpath() {
    case "$(uname -s)" in
        MINGW*|MSYS*|CYGWIN*) (cd "$1" && pwd -W) ;;
        *) printf '%s\n' "$1" ;;
    esac
}

WORK=${1:-$HERE/.stand-zalog-bartr}
mkdir -p "$WORK"
cp "$ART" "$WORK/"
cp "$OLD" "$WORK/"
cp "$HERE/cw20_base.wasm" "$WORK/"
cp "$HERE/zalog-bartr.sh" "$WORK/"

docker run --rm --entrypoint sh -v "$(dpath "$WORK"):/work" \
    cosmwasm/wasmd:v0.50.0 -c '
for i in 1 2 3; do apk add --no-cache jq >/dev/null 2>&1 && break; sleep 2; done
command -v jq >/dev/null || { echo "ПРОВАЛ: jq не поставился" >&2; exit 1; }
sh /work/zalog-bartr.sh'
