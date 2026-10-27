#!/bin/sh
# Весь стенд с нуля: клон узла, три правки, сборка, два прогона.
#
#   sh research/axiome-devnet/run.sh [каталог-для-клона, по умолчанию .stand рядом]
#
# На Windows запускать с MSYS_NO_PATHCONV=1, иначе пути в -v ломаются.
# Нужны: docker, git, python3. Сеть нужна (клон, модули Go, образы).
#
# Прогона два, и второй обязателен: «загрузилось на исправленном узле»
# доказывает правку только вместе с «не загрузилось на выпущенном».
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
# Каталог сборки — рядом со стендом, а не в /tmp: во временном каталоге
# клон может не дожить до сборки, если оболочка запускает команды в
# песочнице (у нас так и вышло — каталог оставался, а содержимое нет).
WORK=${1:-$HERE/.stand}
SRC="$WORK/src"
TAG=axmd-stand:v2.2.1-patched
ART="$HERE/stargate-probe/artifacts/stargate_probe.wasm"

# Путь для `docker -v`. На Windows docker — не часть этой оболочки: он
# принимает пути вида C:/..., а `/tmp/...` понимает как путь ВНУТРИ своей
# виртуальной машины и молча подсовывает пустой каталог. Проверено:
# прогон доходил до protobuf и падал на «can't open ./scripts/protocgen.sh».
dpath() {
  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) (cd "$1" && pwd -W) ;;
    *) printf '%s\n' "$1" ;;
  esac
}

echo "== 1. исходники узла (Apache-2.0, ничего не просим у площадки)"
if [ ! -d "$SRC" ]; then
  mkdir -p "$WORK"
  # core.autocrlf=false обязателен: с CRLF их же сценарии не работают.
  git -c core.autocrlf=false clone --depth 1 --branch v2.2.1 \
    https://github.com/axiome-pro/axm-node.git "$SRC"
fi

echo "== 2. protobuf: в репозитории нет .pb.go, без них узел не собрать"
if [ ! -f "$SRC/x/referral/types/query.pb.go" ]; then
  # Последняя строка их сценария падает на повторном rm, это не беда.
  docker run --rm -v "$(dpath "$SRC"):/workspace" -w /workspace \
    ghcr.io/cosmos/proto-builder:0.14.0 sh ./scripts/protocgen.sh || true
  [ -f "$SRC/x/referral/types/query.pb.go" ] || \
    { echo "ОТКАЗ: protobuf не сгенерировался" >&2; exit 1; }
fi

echo "== 3. три правки"
# Какой python тут настоящий. На Windows `python3` бывает заглушкой из
# Microsoft Store: она печатает «Python» и уходит, ничего не выполнив, —
# поэтому не «есть ли команда», а «считает ли она».
PY=""
for c in python3 python py; do
  if [ "$("$c" -c "print(42)" 2>/dev/null)" = "42" ]; then PY=$c; break; fi
done
[ -n "$PY" ] || { echo "ОТКАЗ: python не найден" >&2; exit 1; }
# Пути через dpath: под MSYS_NO_PATHCONV=1 оболочка не переводит их для
# родных программ, и windows-python принял бы /c/... за путь от корня.
"$PY" "$(dpath "$HERE")/apply.py" "$(dpath "$SRC")"

echo "== 4. сборка узла"
cp "$HERE/Dockerfile" "$WORK/Dockerfile"
docker build -t "$TAG" "$(dpath "$WORK")"

echo "== 5. контракт-пробник"
if [ ! -f "$ART" ]; then
  docker run --rm -v "$(dpath "$HERE/stargate-probe"):/code" \
    -v stargate_probe_cache:/target \
    -v stargate_probe_registry:/usr/local/cargo/registry \
    cosmwasm/optimizer:0.16.0
fi
sha256sum "$ART"

echo "== 6. прогон на исправленном узле"
docker run --rm -v "$(dpath "$HERE/stargate-probe/artifacts"):/work:ro" \
  -v "$(dpath "$HERE"):/stand:ro" --entrypoint bash "$TAG" /stand/stand.sh

echo "== 7. отрицательный контроль на выпущенном двоичном файле"
docker run --rm -v "$(dpath "$HERE/stargate-probe/artifacts"):/work:ro" \
  -v "$(dpath "$HERE"):/stand:ro" --entrypoint bash "$TAG" /stand/original.sh
