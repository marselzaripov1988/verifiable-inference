"""Учёт: счета, остатки, зачисления и списания.

Три свойства, без которых шлюз теряет деньги, и ради которых этот
модуль существует отдельно от HTTP:

  * **Идемпотентность.** Один платёж зачисляется один раз. Приёмник
    отдаёт платежи страницами и без гарантии порядка, перезапуск
    начинает чтение заново — без защиты по хэшу один перевод зачислился
    бы дважды. Ровно эту задачу решает is_mint_processed у
    BARTR-DISTRIBUTOR, и по той же причине.

  * **Долговечность.** Остатки и высота последнего прочитанного блока
    переживают перезапуск. Иначе перезапуск либо зачисляет всё заново,
    либо теряет деньги клиента.

  * **Резерв под ответ.** Списывать после ответа нельзя: длину ответа
    заранее не знает никто, и клиент с нулевым остатком получил бы
    ответ в долг. Поэтому перед работой резервируется максимум, а после
    — списывается действительно использованное.

Хранилище — один файл JSON, запись через временный файл и os.replace:
при обрыве остаётся прежнее целое состояние, а не половина нового.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field


class LedgerError(Exception):
    """Отказ учёта."""


class InsufficientFunds(LedgerError):
    def __init__(self, have: int, need: int):
        super().__init__(f"на счету {have}, нужно {need} (минимальные единицы)")
        self.have = have
        self.need = need


class UnknownAccount(LedgerError):
    pass


@dataclass
class Account:
    key: str          #: секрет клиента, им он доказывает право на остаток
    code: str         #: код для памятки платежа, публичный
    balance: int = 0  #: в минимальных единицах сети (uaxm либо uartr)
    reserved: int = 0
    spent: int = 0
    tokens: int = 0
    created: float = field(default_factory=time.time)

    @property
    def available(self) -> int:
        return self.balance - self.reserved


class Ledger:
    """Состояние шлюза. Потокобезопасен: HTTP-сервер многопоточный."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self.accounts: dict[str, Account] = {}
        self.by_code: dict[str, str] = {}
        self.processed: set[str] = set()
        self.unclaimed: list[dict] = []
        self.height = 0
        self._load()

    # --- хранение ---------------------------------------------------------

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, encoding="utf-8") as f:
            d = json.load(f)
        self.accounts = {k: Account(**v) for k, v in d.get("accounts", {}).items()}
        self.by_code = {a.code: a.key for a in self.accounts.values()}
        self.processed = set(d.get("processed", []))
        self.unclaimed = d.get("unclaimed", [])
        self.height = int(d.get("height", 0))

    def _save(self) -> None:
        d = {
            "accounts": {k: asdict(a) for k, a in self.accounts.items()},
            "processed": sorted(self.processed),
            "unclaimed": self.unclaimed,
            "height": self.height,
        }
        # Временный файл рядом с целевым: os.replace атомарен только в
        # пределах одной файловой системы.
        dirn = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(dirn, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dirn, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # --- счета ------------------------------------------------------------

    def open_account(self) -> Account:
        """Новый счёт. Код короткий — его придётся вводить руками в кошельке."""
        with self._lock:
            while True:
                code = "ag-" + secrets.token_hex(3)
                if code not in self.by_code:
                    break
            a = Account(key=secrets.token_urlsafe(24), code=code)
            self.accounts[a.key] = a
            self.by_code[code] = a.key
            self._save()
            return a

    def account(self, key: str) -> Account:
        a = self.accounts.get(key)
        if a is None:
            raise UnknownAccount("счёт не найден")
        return a

    # --- деньги -----------------------------------------------------------

    def credit(self, tx_hash: str, height: int, sender: str, amount: int,
               memo: str) -> Account | None:
        """Зачислить платёж. None — счёт по памятке не опознан.

        Повторный вызов с тем же хэшем ничего не делает: платёж уже
        зачтён. Высота двигается всегда, в том числе для неопознанного —
        иначе шлюз перечитывал бы его вечно.
        """
        with self._lock:
            if tx_hash in self.processed:
                return None
            code = _find_code(memo, self.by_code)
            self.processed.add(tx_hash)
            self.height = max(self.height, height)
            if code is None:
                # Деньги не теряются и не зачисляются наугад: платёж
                # остаётся видимым, и его разбирает человек.
                self.unclaimed.append({
                    "tx_hash": tx_hash, "height": height, "sender": sender,
                    "amount": amount, "memo": memo[:120], "at": time.time(),
                })
                self._save()
                return None
            a = self.accounts[self.by_code[code]]
            a.balance += amount
            self._save()
            return a

    def reserve(self, key: str, amount: int) -> None:
        with self._lock:
            a = self.account(key)
            if a.available < amount:
                raise InsufficientFunds(a.available, amount)
            a.reserved += amount
            self._save()

    def settle(self, key: str, reserved: int, actual: int, tokens: int) -> None:
        """Снять резерв и списать действительно использованное.

        actual больше reserved быть не может: резерв считался по
        max_tokens, и ответ длиннее не бывает. Но если когда-нибудь
        станет — списывается не больше резерва, чтобы остаток не ушёл в
        минус незаметно.
        """
        with self._lock:
            a = self.account(key)
            charge = min(actual, reserved)
            a.reserved = max(0, a.reserved - reserved)
            a.balance -= charge
            a.spent += charge
            a.tokens += tokens
            self._save()

    def release(self, key: str, reserved: int) -> None:
        """Снять резерв, ничего не списав: работа не состоялась."""
        with self._lock:
            a = self.account(key)
            a.reserved = max(0, a.reserved - reserved)
            self._save()


def _find_code(memo: str, codes: dict[str, str]) -> str | None:
    """Код счёта в памятке.

    Поиск подстрокой, а не равенством: кошельки добавляют к памятке своё,
    а человек — пробелы и слова. Код достаточно случаен (три байта на
    счёт плюс префикс), чтобы случайное совпадение не стоило обсуждения.
    """
    if not memo:
        return None
    low = memo.lower()
    for code in codes:
        if code in low:
            return code
    return None
