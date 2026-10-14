"""Запустить шлюз.

    python gateway/scripts/serve.py \
        --treasury artr1... \
        --price 1000000 \
        --rpc http://127.0.0.1:26657 \
        --base-url http://127.0.0.1:8000/v1

Узел проверяется ДО приёма запросов: узел, не повторяющий сам себя,
делает порог 0,0360 недействительным, и ответы такого узла на этапе 2
проверить будет нечем. Отказ лучше, чем работа, которую придётся
переделывать.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from artery_gateway import inference, server  # noqa: E402
from artery_gateway.bech32 import BadAddress, check  # noqa: E402
from artery_gateway.chain import ArteryRPC, AxiomeRPC  # noqa: E402
from artery_gateway.ledger import Ledger  # noqa: E402
from artery_gateway.pricing import Pricing  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--treasury", required=True,
                    help="адрес казны: axm1... для axiome, artr1... для artery")
    ap.add_argument("--price", type=int, required=True,
                    help="минимальных единиц (uaxm) за миллион токенов; "
                         "курса монеты в коде нет")
    ap.add_argument("--chain", choices=("axiome", "artery"), default="axiome",
                    help="где принимать платежи; решено 14 октября — axiome")
    ap.add_argument("--rpc", default="https://api-chain.axiomechain.org",
                    help="RPC цепочки. Умолчание — публичный узел axiome-1 "
                         "из реестра цепочек Cosmos. ВНИМАНИЕ: индексатор "
                         "api-idx.axiomechain.pro сюда не годится — он "
                         "отдаёт контракты и заголовки блоков, но не сделки")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    ap.add_argument("--model", default=inference.PINNED_MODEL)
    ap.add_argument("--ledger", default="gateway-ledger.json")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--poll", type=float, default=10.0,
                    help="как часто читать платежи, секунды")
    ap.add_argument("--from-height", type=int, default=0,
                    help="с какой высоты читать платежи при первом запуске")
    ap.add_argument("--addresses",
                    help="файл с адресами приёма, по одному на строку: "
                         "каждому счёту выдаётся свой, и памятка не нужна. "
                         "Адреса берутся из ВАШЕГО кошелька — шлюз ключей "
                         "не держит и адресов не выводит")
    ap.add_argument("--allow-unfit-node", action="store_true",
                    help="поднять шлюз, даже если узел не годен: только "
                         "чтобы проверить приём платежей без карты")
    a = ap.parse_args()

    rpc_class = AxiomeRPC if a.chain == "axiome" else ArteryRPC
    # Адрес казны проверяется ДО всего остального. Опечатка в нём дала бы
    # адрес, на который никто не переводит: шлюз исправно опрашивал бы
    # цепочку, исправно не находил платежей — и это выглядело бы
    # отсутствием спроса, а не ошибкой настройки.
    try:
        treasury = check(a.treasury, rpc_class.hrp)
    except BadAddress as e:
        print(f"АДРЕС КАЗНЫ НЕ ГОДИТСЯ: {e}", file=sys.stderr)
        return 1

    # Адреса приёма проверяются так же, как казна, и по той же причине:
    # опечатка дала бы адрес, на который клиент переведёт, а шлюз не
    # увидит. Деньги при этом уйдут не в никуда, а туда, где их никто не
    # ждёт — хуже, чем отказ.
    pool = []
    if a.addresses:
        try:
            with open(a.addresses, encoding="utf-8") as f:
                raw = [x.strip() for x in f if x.strip()
                       and not x.strip().startswith("#")]
        except OSError as e:
            print(f"ФАЙЛ АДРЕСОВ НЕ ПРОЧИТАН: {e}", file=sys.stderr)
            return 1
        for i, line in enumerate(raw, 1):
            try:
                pool.append(check(line, rpc_class.hrp))
            except BadAddress as e:
                print(f"АДРЕС ПРИЁМА В СТРОКЕ {i} НЕ ГОДИТСЯ: {e}",
                      file=sys.stderr)
                return 1
        if not pool:
            print("ФАЙЛ АДРЕСОВ ПУСТ: выдавать клиентам нечего",
                  file=sys.stderr)
            return 1
        if treasury in pool:
            print("АДРЕС КАЗНЫ ЕСТЬ В ПУЛЕ: платежи на него считались бы "
                  "и по получателю, и по памятке", file=sys.stderr)
            return 1

    led = Ledger(a.ledger)
    if pool:
        added = led.add_addresses(pool)
        print(f"адресов приёма: добавлено {added}, свободно {len(led.free)}, "
              f"выдано {len(led.by_deposit)}")
    if a.from_height and not led.height:
        led.height = a.from_height - 1
        led._save()

    gw = server.Gateway(
        ledger=led,
        pricing=Pricing(price_micro_per_million=a.price),
        treasury=treasury,
        rpc=rpc_class(a.rpc),
        base_url=a.base_url,
        model=a.model,
        per_account=bool(a.addresses),
    )

    try:
        gw.node_version = inference.check_node(a.base_url, a.model)
        print(f"узел годен: vLLM {gw.node_version}, модель {a.model}")
    except Exception as e:  # noqa: BLE001
        print(f"УЗЕЛ НЕ ГОДЕН: {e}", file=sys.stderr)
        if not a.allow_unfit_node:
            print("шлюз не поднят. С --allow-unfit-node можно проверить "
                  "только приём платежей.", file=sys.stderr)
            return 1
        print("ВНИМАНИЕ: работаю без годного узла — запросы будут падать",
              file=sys.stderr)

    # Первое чтение платежей до открытия порта: иначе клиент, уже
    # заплативший, получил бы отказ по остатку.
    try:
        n = gw.poll_payments()
        print(f"платежи прочитаны до высоты {led.height}, зачислено {n}")
    except Exception as e:  # noqa: BLE001
        print(f"ПЛАТЕЖИ НЕ ПРОЧИТАНЫ: {e}", file=sys.stderr)
        print("это не «платежей нет», а неизвестно — видно в /healthz",
              file=sys.stderr)

    srv = server.serve(gw, a.host, a.port, a.poll)
    print(f"шлюз на http://{a.host}:{a.port}, сеть {a.chain}, "
          f"казна {a.treasury}, цена {a.price} {gw.denom} за миллион токенов")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлен")
    finally:
        srv.shutdown()
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
