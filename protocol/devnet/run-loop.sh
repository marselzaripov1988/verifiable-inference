#!/bin/sh
# Прогон ПЕТЛИ на местном девнете. Одна команда.
#
#   sh protocol/devnet/run-loop.sh
#
# Образ wasmd v0.50.0 — та же пара с libwasmvm 1.5.0, что у Axiome: на
# другой версии проверка была бы про другую цепочку.
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

WORK=${1:-$HERE/.stand}
mkdir -p "$WORK/daemon"
cp "$ART" "$WORK/"
cp "$HERE/loop.sh" "$HERE/fake-vllm.py" "$WORK/"
# Демоны берутся из репозитория как есть: проверяется то, что лежит в
# нём, а не копия с правками.
for f in loop.py tx.py chain.py executor.py checker.py canon.py vllm_client.py; do
    cp "$HERE/../daemon/$f" "$WORK/daemon/"
done

docker run --rm --entrypoint sh -v "$(dpath "$WORK"):/work" \
    cosmwasm/wasmd:v0.50.0 -c '
for i in 1 2 3; do apk add --no-cache jq python3 >/dev/null 2>&1 && break; sleep 2; done
command -v jq >/dev/null || { echo "ПРОВАЛ: jq не поставился" >&2; exit 1; }
command -v python3 >/dev/null || { echo "ПРОВАЛ: python3 не поставился" >&2; exit 1; }
sh /work/loop.sh'
