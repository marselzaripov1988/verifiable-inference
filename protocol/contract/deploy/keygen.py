#!/usr/bin/env python3
"""Породить ключ и адрес axm1…, не показывая секрет.

Секрет пишется в файл и НЕ печатается: в переписку он попасть не должен.
Печатается только адрес — он публичен.

Код проверяется против эталона BIP-173 ДО того, как что-то породит.
Проверка не формальная: она ловит ошибку и в кодировании bech32, и в
выводе адреса из открытого ключа. Эталон взят из спецификации: закрытому
ключу 1 отвечает открытый 0279BE66…81798, его hash160 —
751e76e8199196d454941c45d1b3a323f1433bd6, а адрес segwit —
bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4. Если моя сборка воспроизводит
эту строку знак в знак, значит и bech32, и hash160 у меня верные.

Чего этот способ НЕ даёт: мнемоники из 24 слов. Она требует BIP39 и
BIP32, а подходящей библиотеки на машине нет, и писать их руками для
ключа, который распоряжается контрактом, — плохая мысль. Поэтому ключ
сырой, в hex, и в кошелёк он вносится как «приватный ключ», а не как
сид-фраза.
"""

from __future__ import annotations

import hashlib
import os
import sys

from Crypto.Hash import RIPEMD160
from cryptography.hazmat.primitives.asymmetric import ec

CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
GEN = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)


def polymod(values):
    chk = 1
    for v in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i, g in enumerate(GEN):
            if (top >> i) & 1:
                chk ^= g
    return chk


def hrp_expand(hrp):
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def checksum(hrp, data):
    v = polymod(hrp_expand(hrp) + data + [0, 0, 0, 0, 0, 0]) ^ 1
    return [(v >> 5 * (5 - i)) & 31 for i in range(6)]


def encode(hrp, data):
    return hrp + "1" + "".join(CHARSET[d] for d in data + checksum(hrp, data))


def convertbits(data, frm, to, pad=True):
    acc = bits = 0
    out = []
    maxv = (1 << to) - 1
    for b in data:
        acc = (acc << frm) | b
        bits += frm
        while bits >= to:
            bits -= to
            out.append((acc >> bits) & maxv)
    if pad and bits:
        out.append((acc << (to - bits)) & maxv)
    return out


def hash160(b: bytes) -> bytes:
    h = RIPEMD160.new()
    h.update(hashlib.sha256(b).digest())
    return h.digest()


def compressed_pubkey(priv) -> bytes:
    nums = priv.public_key().public_numbers()
    return bytes([2 + (nums.y & 1)]) + nums.x.to_bytes(32, "big")


def selftest() -> None:
    """Эталон BIP-173. Расхождение — отказ, ничего не порождаем."""
    priv = ec.derive_private_key(1, ec.SECP256K1())
    pub = compressed_pubkey(priv)
    want_pub = ("0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b"
                "16f81798")
    assert pub.hex() == want_pub, f"открытый ключ не тот: {pub.hex()}"
    h = hash160(pub)
    want_h = "751e76e8199196d454941c45d1b3a323f1433bd6"
    assert h.hex() == want_h, f"hash160 не тот: {h.hex()}"
    addr = encode("bc", [0] + convertbits(h, 8, 5))
    want_addr = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
    assert addr == want_addr, f"адрес не тот: {addr}"
    print("  эталон BIP-173 воспроизведён знак в знак:")
    print(f"    открытый ключ {pub.hex()[:16]}… hash160 {h.hex()[:16]}…")
    print(f"    {addr}")

    # И обратная сверка с разбором, которым пользуется шлюз.
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))


def check_against_real(known: str, hrp: str) -> None:
    """Пересобрать НАСТОЯЩИЙ адрес из его же данных.

    Берём известный адрес сети, разбираем его чужим кодом (разбор шлюза),
    и собираем обратно своим. Совпало — кодирование верно для этой сети,
    а не только для эталона Bitcoin.
    """
    sys.path.insert(0, r"C:\Users\x\Desktop\verifiable-inference\gateway")
    from artery_gateway.bech32 import decode as gw_decode

    got_hrp, data = gw_decode(known)
    assert got_hrp == hrp, got_hrp
    again = encode(hrp, data)
    assert again == known, f"пересборка дала {again}"
    print(f"  настоящий адрес сети пересобран знак в знак: {known[:18]}…")


def generate(hrp: str, path: str) -> str:
    priv = ec.generate_private_key(ec.SECP256K1())
    pub = compressed_pubkey(priv)
    addr = encode(hrp, convertbits(hash160(pub), 8, 5))
    secret = priv.private_numbers().private_value.to_bytes(32, "big").hex()
    # Секрет пишется и НЕ возвращается наружу: в переписку он не попадёт.
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"address={addr}\n")
        f.write(f"pubkey_compressed_hex={pub.hex()}\n")
        f.write(f"privkey_hex={secret}\n")
        f.write("# Вносится в кошелёк как ПРИВАТНЫЙ КЛЮЧ (не сид-фраза).\n")
        f.write("# После внесения сверьте адрес и удалите этот файл.\n")
    os.chmod(path, 0o600)
    return addr


if __name__ == "__main__":
    print("### самопроверка до порождения")
    selftest()
    check_against_real("axm1l0a927clmz7wqmls2737ctwlxzhxeqmxaf55w7", "axm")
    out_dir = sys.argv[1]
    os.makedirs(out_dir, exist_ok=True)
    print("\n### порождение")
    for name in ("admin-attestor", "forfeit"):
        p = os.path.join(out_dir, f"{name}.key")
        a = generate("axm", p)
        print(f"  {name:16s} {a}")
        print(f"    секрет: {p} (не читался и не печатался)")
