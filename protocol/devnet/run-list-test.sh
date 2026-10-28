#!/bin/sh
# Репетиция ТЕСТОВОГО ЭКЗЕМПЛЯРА на местном девнете. Одна команда.
#
#   sh protocol/devnet/run-list-test.sh
#
# Разворачивает контракт тем самым листом, который пойдёт в axiome-1
# (contract/deploy/instantiate-test.json), и проводит по нему задание
# тремя узлами с залогом из листа. Бесплатно — лист с опечаткой иначе
# выяснился бы в живой цепочке и за 0,45 AXM.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ART="$HERE/../contract/artifacts/inference_registry.wasm"
LIST="$HERE/../contract/deploy/instantiate-test.json"
[ -f "$ART" ] || { echo "нет артефакта: собрать cosmwasm/optimizer" >&2; exit 1; }
[ -f "$LIST" ] || { echo "нет листа: $LIST" >&2; exit 1; }

dpath() {
    case "$(uname -s)" in
        MINGW*|MSYS*|CYGWIN*) (cd "$1" && pwd -W) ;;
        *) printf '%s\n' "$1" ;;
    esac
}

WORK=${1:-$HERE/.stand-list}
mkdir -p "$WORK"
cp "$ART" "$WORK/"
cp "$LIST" "$WORK/"
cp "$HERE/list-test.sh" "$WORK/"

docker run --rm --entrypoint sh -v "$(dpath "$WORK"):/work" \
    cosmwasm/wasmd:v0.50.0 -c '
for i in 1 2 3; do apk add --no-cache jq >/dev/null 2>&1 && break; sleep 2; done
command -v jq >/dev/null || { echo "ПРОВАЛ: jq не поставился" >&2; exit 1; }
sh /work/list-test.sh'
