"""Проверка адреса казны. Bech32 по BIP-173, без зависимостей.

Зачем это отдельной проверкой. Шлюз принимал адрес казны как строку и
никак её не проверял. Опечатка в одном знаке означала бы адрес, на
который никто ничего не переведёт: шлюз исправно опрашивал бы цепочку,
исправно не находил платежей и показывал бы «платежей нет».

Это худший исход из возможных, потому что выглядел бы он не как ошибка
настройки, а как ОТСУТСТВИЕ СПРОСА — то есть дал бы неверный ответ на
единственный вопрос, ради которого существует этап 1. Контрольная сумма
bech32 ловит ровно такие опечатки: один изменённый знак её рушит.

Чего эта проверка НЕ делает: не проверяет, что ключ у вас. Этого из
цепочки не видно, и никакая проверка адреса этого не покажет.
"""

from __future__ import annotations

CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
GENERATOR = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)


class BadAddress(ValueError):
    """Адрес не похож на адрес этой сети."""


def _polymod(values) -> int:
    chk = 1
    for v in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i, g in enumerate(GENERATOR):
            if (top >> i) & 1:
                chk ^= g
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def decode(addr: str) -> tuple[str, list[int]]:
    """Префикс и данные. Бросает BadAddress при любой несообразности."""
    if not addr or len(addr) > 90:
        raise BadAddress(f"длина адреса {len(addr)}: допустимо от 8 до 90")
    if addr != addr.lower() and addr != addr.upper():
        raise BadAddress("смешанный регистр: bech32 так не бывает")
    a = addr.lower()
    pos = a.rfind("1")
    if pos < 1 or pos + 7 > len(a):
        raise BadAddress("разделителя «1» нет или он не на месте")
    hrp, data = a[:pos], a[pos + 1:]
    if any(not (33 <= ord(c) <= 126) for c in hrp):
        raise BadAddress("в префиксе непечатный знак")
    try:
        values = [CHARSET.index(c) for c in data]
    except ValueError as e:
        bad = next(c for c in data if c not in CHARSET)
        raise BadAddress(f"знак {bad!r} не из алфавита bech32 — похоже на "
                         f"опечатку") from e
    if _polymod(_hrp_expand(hrp) + values) != 1:
        raise BadAddress(
            "контрольная сумма не сходится: адрес набран с ошибкой. "
            "Это та самая опечатка, из-за которой шлюз молча не собирал бы "
            "платежей и это выглядело бы отсутствием спроса")
    return hrp, values[:-6]


def check(addr: str, want_hrp: str) -> str:
    """Проверить адрес и его сеть. Возвращает адрес в нижнем регистре."""
    hrp, data = decode(addr)
    if hrp != want_hrp:
        raise BadAddress(
            f"адрес сети {hrp!r}, а шлюз настроен на {want_hrp!r}: "
            f"платежи ждут не там, где их переведут")
    # Адрес счёта в Cosmos — 20 байт, то есть 32 знака данных bech32.
    # Контракты дают 32 байта (52 знака), и казной такой адрес быть
    # может, поэтому допускаются оба.
    if len(data) not in (32, 52):
        raise BadAddress(
            f"{len(data)} знаков данных: у адреса счёта 32, у контракта 52")
    return addr.lower()
