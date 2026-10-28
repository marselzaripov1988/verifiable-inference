#!/bin/sh
# Прогон ЗАЛОГА на местном девнете. Одна команда.
#
#   sh protocol/devnet/run-zalog.sh
#
# Образ wasmd v0.50.0 — та же пара с libwasmvm 1.5.0, что у Axiome.
# Демонов и модели здесь нет: проверяются деньги и состояния узла.
#
# Нужны docker и собранный артефакт (protocol/contract/artifacts/).
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ART="$HERE/../contract/artifacts/inference_registry.wasm"
[ -f "$ART" ] || { echo "нет артефакта: собрать cosmwasm/optimizer" >&2; exit 1; }

# Путь для docker: на Windows он принимает C:/…, а /tmp/… понимает как
# путь внутри своей виртуальной машины и молча монтирует пустоту.
dpath() {
    case "$(uname -s)" in
        MINGW*|MSYS*|CYGWIN*) (cd "$1" && pwd -W) ;;
        *) printf '%s\n' "$1" ;;
    esac
}

WORK=${1:-$HERE/.stand-zalog}
mkdir -p "$WORK"
cp "$ART" "$WORK/"
cp "$HERE/zalog.sh" "$WORK/"

docker run --rm --entrypoint sh -v "$(dpath "$WORK"):/work" \
    cosmwasm/wasmd:v0.50.0 -c '
for i in 1 2 3; do apk add --no-cache jq >/dev/null 2>&1 && break; sleep 2; done
command -v jq >/dev/null || { echo "ПРОВАЛ: jq не поставился" >&2; exit 1; }
sh /work/zalog.sh'
