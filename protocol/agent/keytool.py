#!/usr/bin/env python3
"""Ключ агента файлом: создать, взять из фразы, показать адрес.

    python3 keytool.py new agent.key        # новый ключ
    python3 keytool.py mnemonic agent.key   # из фразы кошелька (спросит её)
    python3 keytool.py address agent.key    # адрес axm1… ключа

Файл — 64 шестнадцатеричных знака, права 600. Его и ждёт прокси
(`SIGN_KEY`): им подписываются сделки, запросы к узлам и расшифровываются
ответы.

Фраза читается со стандартного входа, а не из аргументов, чтобы не
осесть в истории оболочки. Путь по умолчанию — Axiome, m/44'/546'/0'/0/0.
**Сверьте напечатанный адрес с адресом в Axiome Wallet.** Опечатка во
фразе даёт не ошибку, а другой, пустой кошелёк.
"""

import getpass
import os
import secrets
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "daemon"))
import sign    # noqa: E402
import wallet  # noqa: E402


def write_key(path, key):
    if os.path.exists(path):
        raise SystemExit(f"ОТКАЗ: {path} уже есть. Чужой ключ не "
                         f"перезаписываю: деньги на нём пропали бы")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key.hex() + "\n")


def main(argv):
    if len(argv) < 3 or argv[1] not in ("new", "mnemonic", "address"):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    cmd, path = argv[1], argv[2]
    if cmd == "address":
        key = sign.load_key(path)
    elif cmd == "new":
        while True:
            key = secrets.token_bytes(32)
            if 0 < int.from_bytes(key, "big") < sign.N:
                break
        write_key(path, key)
    else:
        if sys.stdin.isatty():
            words = getpass.getpass("фраза (12 или 24 слова, не видна): ")
        else:
            words = sys.stdin.readline()
        n = len(words.split())
        if n not in (12, 15, 18, 21, 24):
            raise SystemExit(f"ОТКАЗ: во фразе {n} слов, а бывает 12–24")
        path_ = os.environ.get("HD_PATH", wallet.AXIOME_PATH)
        key = wallet.key_from_mnemonic(
            words, os.environ.get("PASSPHRASE", ""), path_)
        write_key(path, key)
    addr = sign.address(sign.pubkey(key))
    print(addr)
    if cmd != "address":
        print(f"ключ записан в {path}. Пополните {addr} монетами AXM — "
              f"на них агент платит за ответы и газ", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
