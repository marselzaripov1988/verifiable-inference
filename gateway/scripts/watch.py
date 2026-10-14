"""Показывает входящие платежи на адрес казны.

    python scripts/watch.py <адрес_казны> [--rpc URL] [--from-height N]

Проверка приёмника на живой цепочке. На девнете адресом казны удобно
взять любой аккаунт из генезиса.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from artery_gateway.chain import ArteryRPC, AxiomeRPC  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("treasury", help="адрес казны (axm1... или artr1...)")
    ap.add_argument("--rpc", default="https://api-chain.axiomechain.org")
    ap.add_argument("--chain", choices=("axiome", "artery"), default="axiome")
    ap.add_argument("--from-height", type=int, default=1)
    args = ap.parse_args()

    rpc = (AxiomeRPC if args.chain == "axiome" else ArteryRPC)(args.rpc)
    print(f"цепочка на высоте {rpc.latest_height()}")
    print(f"платежи на {args.treasury} с высоты {args.from_height}:\n")

    total = 0
    count = 0
    for p in rpc.scan_payments(args.treasury, args.from_height):
        count += 1
        total += p.amount_micro
        print(f"  {p.height:>7}  {p.amount_whole:>12,.6f} монет от {p.sender}  {p.tx_hash[:16]}…")

    if not count:
        print("  (платежей нет)")
    else:
        print(f"\n  итого: {count} платеж(ей), {total / 1_000_000:,.6f} монет")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
