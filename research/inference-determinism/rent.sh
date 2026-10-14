#!/bin/bash
# Аренда карты на Vast.ai под замеры фазы 1.
#
# По умолчанию НИЧЕГО НЕ АРЕНДУЕТ: ищет предложения, считает цену
# и печатает. Трата денег — только с явным --rent.
#
# Ключ берётся из VAST_API_KEY. Пароль от личного кабинета здесь не
# нужен и не используется.
#
#   VAST_API_KEY=... ./rent.sh RTX_4090            # посмотреть цены
#   VAST_API_KEY=... ./rent.sh RTX_4090 --rent     # арендовать
#
# Флаги сверены с vastai 1.8.2 по `vastai <команда> --help`.
set -uo pipefail

GPU="${1:-RTX_4090}"
RENT=no
[ "${2:-}" = "--rent" ] && RENT=yes

# Образ с уже собранным vLLM. Свой venv не собираем осознанно: в
# прошлый раз это ломалось трижды подряд — на python3.12-venv, на
# python3.12-dev для Triton и на ninja, не найденном FlashInfer.
IMAGE="vllm/vllm-openai:latest"

# Весам BF16-семёрки нужно ~15 ГБ, той же модели в 4 битах ~5 ГБ,
# обе держим одновременно, чтобы не качать заново. Плюс образ.
DISK=60

: "${VAST_API_KEY:?нужен ключ API: VAST_API_KEY=... $0 $GPU}"

VASTAI="${VASTAI:-vastai}"
command -v "$VASTAI" >/dev/null || { echo "нет клиента vastai (pip install vastai)"; exit 1; }
export VAST_API_KEY

# rentable=true      — свободно прямо сейчас
# reliability>0.99   — машина не отваливается посреди прогона
# cuda_max_good>=13  — ГЛАВНЫЙ фильтр. В образе vLLM стоит CUDA 13, ей
#                      нужен драйвер от 580. На хосте с драйвером 535
#                      контейнер запускается, nvidia-smi отвечает, а
#                      инициализация CUDA падает с ошибкой 804
#                      «forward compatibility ... on non supported HW»:
#                      слой прямой совместимости на GeForce не работает,
#                      он только для серверных карт. Поле cuda_vers для
#                      этого не годится — фильтровать надо по потолку.
# inet_down>500      — хост с медленной сетью не докачает веса вовсе.
#                      Один такой попался: 12 МБ за двенадцать минут.
QUERY="gpu_name=$GPU num_gpus=1 rentable=true reliability>0.99 disk_space>=$DISK cuda_max_good>=13.0 inet_down>500"

echo "ищу: $GPU, диск $DISK ГБ"
OFFERS=$("$VASTAI" search offers "$QUERY" -o 'dph+' --raw 2>/dev/null)

if [ -z "$OFFERS" ] || [ "$OFFERS" = "[]" ]; then
    echo "предложений нет. Проверьте имя карты — оно пишется как RTX_4090, A100_PCIE, RTX_3090."
    exit 1
fi

python3 - "$OFFERS" <<'PY'
import json, sys
offers = json.loads(sys.argv[1])[:5]
print(f"\n{'id':>10}  {'$/час':>7}  {'карта':<16} {'ОЗУ ГБ':>7}  {'надёжн':>7}  страна")
for o in offers:
    print(f"{o['id']:>10}  {o['dph_total']:>7.3f}  {o['gpu_name'][:16]:<16} "
          f"{o.get('gpu_ram', 0)/1024:>7.0f}  {o.get('reliability2', 0):>7.3f}  "
          f"{o.get('geolocation', '?')}")
best = offers[0]
# dph_total в поиске посчитан для 5 ГБ хранилища. Мы заказываем больше,
# и по счёту выходит заметно дороже: на замере 29.09 поиск показывал
# 0,139 $/час, а списывалось 0,206.
print(f"\nдешевейшее: id {best['id']}, {best['dph_total']:.3f} $/час "
      f"(без учёта диска, по факту выйдет дороже)")
open('/tmp/vast-best-id', 'w').write(str(best['id']))
PY

BEST=$(cat /tmp/vast-best-id 2>/dev/null)

if [ "$RENT" != yes ]; then
    echo
    echo "ничего не арендовано. Чтобы арендовать id $BEST:"
    echo "  VAST_API_KEY=... $0 $GPU --rent"
    exit 0
fi

echo
echo "арендую id $BEST…"
"$VASTAI" create instance "$BEST" \
    --image "$IMAGE" \
    --disk "$DISK" \
    --ssh --direct \
    --env '-p 8000:8000' \
    --raw

echo
echo "жду, пока машина поднимется…"
for i in $(seq 1 60); do
    ST=$("$VASTAI" show instances --raw 2>/dev/null \
         | python3 -c "import json,sys; d=json.load(sys.stdin); print(d[0]['actual_status'] if d else '')" 2>/dev/null)
    echo "  состояние: ${ST:-неизвестно}"
    [ "$ST" = "running" ] && break
    sleep 10
done

echo
echo "адрес для входа:"
"$VASTAI" show instances --raw 2>/dev/null | python3 -c "
import json, sys
d = json.load(sys.stdin)
if d:
    i = d[0]
    print(f\"  ssh -p {i.get('ssh_port')} root@{i.get('ssh_host')}\")
    print(f\"  id инстанса: {i['id']}\")
    print()
    print('ПОСЛЕ ЗАМЕРОВ ОБЯЗАТЕЛЬНО:')
    print(f\"  {'vastai'} destroy instance {i['id']}\")
"
