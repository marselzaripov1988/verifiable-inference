#!/usr/bin/env python3
"""Отправка сообщений в контракт. Шаг 15.

До этого демоны собирали сообщения и ПЕЧАТАЛИ их: отправлял человек
руками, `axmd tx wasm execute`. Свойство это полезное — демон остаётся
проверяемым без живой цепочки, — и оно здесь сохранено: отправитель
подставляется, и `DryRun` печатает ровно то же, что печатали демоны.
Петля без `--send` работает именно так.

Чего отправка требует обязательно.

**Положительного признака.** «Отправлено» не значит «исполнено»:
`axmd tx` возвращает txhash до попадания в блок, и сделка в блоке может
вернуть ненулевой код. Поэтому каждая отправка ждёт сделку и ТРЕБУЕТ
`code=0`; иначе петля пошла бы дальше, считая сделанным то, что цепочка
отвергла. На этом в этом проекте горели дважды — см. devnet/cycle.sh.

**Разбора отказа.** Отказ контракта — это не сбой: `Open` могли занять
за секунду до нас, проверку мог сдать кто-то третий. Такие отказы петля
обязана отличать от настоящих поломок, иначе узел встанет на первой же
гонке. Разбор в `classify`.

**Ключ лежит на узле.** Иначе петля не петля: ждать человека с подписью
нечего. Это осознанная цена, и риск ограничен — на узле ключ только с
залогом, а не ключ админа контракта. Ключ развёртывания держится отдельно
и подписывает вне узла (РАЗВЁРТЫВАНИЕ.md).
"""

import base64
import json
import re
import shutil
import subprocess
import sys
import time


class TxFailed(Exception):
    """Сделка не прошла. Текст — то, что сказала цепочка."""


#: Отказы, при которых повторять НЕ надо и ругаться не на что: кто-то
#: оказался быстрее, либо задание ушло дальше по состояниям. Для петли это
#: обычный ход дела.
#:
#: Узнаются ПО РУССКОМУ ТЕКСТУ, потому что цепочка отдаёт именно его:
#: `error.rs` объявляет сообщения через `#[error("…")]`, и в сделке
#: приходит отрендеренная строка, а не имя варианта. Сперва здесь стояли
#: имена вариантов — и это стоило прогона: два узла проиграли гонку за
#: заданием, отказ попал в «непонятный», и оба демона вышли, оставив
#: задание без проверок. Имена вариантов оставлены рядом: если текст
#: когда-нибудь поменяют, разбор не рассыплется молча.
RACE = (
    "задание в состоянии",          # WrongJobState: заняли раньше нас
    "уже сдал проверку",            # DuplicateCheck
    "проверок уже достаточно",      # ChecksComplete, ChecksEnough
    "уже выплачено",                # AlreadyPaid
    "проверок из",                  # NotEnoughChecks: проверку сдали не все
    "задание не найдено",           # JobUnknown
    "срок не вышел",                # NotExpired: срок закрыл кто-то другой
    "вердикт уже вынесен",          # AlreadySettled
    "WrongJobState",
    "DuplicateCheck",
    "ChecksComplete",
    "AlreadyPaid",
    "NotEnoughChecks",
    "JobUnknown",
    "NotExpired",
    "AlreadySettled",
)

#: Отказы узла, а не правила: подпись не села на последовательность, сделка
#: не поместилась в очередь. Повтор уместен.
RETRY = (
    "account sequence mismatch",
    "mempool is full",
    "tx already in mempool",
    "connection",
    "timeout",
    "EOF",
)


def classify(text):
    """Что это за отказ: `race`, `retry` или `fatal`.

    Разбор по тексту, а не по коду: коды ошибок контракта в сделке не
    различимы — wasm отдаёт все как code 5 с текстом внутри. Текст же
    приходит ровно тот, что в error.rs.
    """
    t = text or ""
    for r in RACE:
        if r in t:
            return "race"
    low = t.lower()
    for r in RETRY:
        if r.lower() in low:
            return "retry"
    return "fatal"


def attribute(res, key, event="wasm"):
    """Значение атрибута из событий сделки. None — нет такого.

    Нужно затем, что номер задания присваивает контракт: заказчик платит,
    а какой номер вышел — узнаёт только из события `wasm`. Угадывать его
    по счётчику нельзя, между чтением и отправкой задание мог поставить
    кто-то другой.

    Ключи в cosmos-sdk 0.50 приходят простыми строками, но в более старых
    ответах они бывают в base64. Разбираются оба вида: разойдись это с
    версией узла, заказчик молча не узнал бы свой номер.
    """
    if not isinstance(res, dict):
        return None
    for ev in res.get("events") or []:
        if event and ev.get("type") != event:
            continue
        for a in ev.get("attributes") or []:
            k, v = a.get("key"), a.get("value")
            if k == key:
                return v
            try:
                if base64.b64decode(k or "", validate=True).decode() == key:
                    return base64.b64decode(v or "", validate=True).decode()
            except Exception:
                pass
    return None


class DryRun:
    """Печатает сообщения вместо отправки.

    Нужен не только для тестов. Первый запуск петли на живой цепочке
    разумно сделать именно так: видно, что узел собрался делать, и ни одна
    сделка не ушла.
    """

    def __init__(self, out=None):
        self.out = out if out is not None else sys.stdout
        self.sent = []

    def address(self):
        return "axm1dryrun"

    def send(self, msg, funds=None, contract=None):
        self.sent.append({"msg": msg, "funds": funds, "contract": contract})
        print(json.dumps(msg, ensure_ascii=False), file=self.out)
        return {"txhash": None, "dry": True}


class Axmd:
    """Отправка через `axmd`. Ключ берётся из связки узла.

    Пароль связки не спрашивается и не хранится здесь: для петли связка
    должна быть `--keyring-backend test` либо разблокированная `os`.
    Запрашивать пароль было бы ложью про автономность — петля работает без
    человека, и человека рядом нет.
    """

    def __init__(self, contract, key, chain_id="axiome-1",
                 node="https://api-chain.axiomechain.org",
                 home=None, keyring="test", gas_prices="1uaxm",
                 gas_adjustment="1.3", binary="axmd", wait=90):
        if shutil.which(binary) is None:
            raise TxFailed(f"{binary} не найден в PATH: отправлять нечем")
        self.contract = contract
        self.key = key
        self.chain_id = chain_id
        self.node = node
        self.home = home
        self.keyring = keyring
        self.gas_prices = gas_prices
        self.gas_adjustment = gas_adjustment
        self.binary = binary
        self.wait = wait
        self._address = None

    # --- служебное --------------------------------------------------------
    #
    # Набор флагов РАЗНЫЙ у трёх команд, и это не придирка: запрос не
    # принимает флагов связки, а связка — флага узла, и лишний флаг
    # означает «unknown flag» вместо работы. На родственной ошибке
    # (--chain-id у команды запроса) в этом проекте уже горели — см.
    # devnet/cycle.sh.

    def _home(self):
        return ["--home", self.home] if self.home else []

    def _keys_flags(self):
        return ["--keyring-backend", self.keyring] + self._home()

    def _query_flags(self):
        return ["--node", self.node] + self._home()

    def _tx_flags(self):
        return (["--keyring-backend", self.keyring, "--node", self.node]
                + self._home())

    def _run(self, args, timeout=120):
        p = subprocess.run([self.binary] + args, capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr

    def address(self):
        """Свой адрес из связки. Нужен затем, что петля обязана знать,
        её ли это задание: `submit_answer` принимается только от взявшего,
        а `submit_check` от исполнителя контракт отвергает как самопроверку.
        """
        if self._address:
            return self._address
        rc, out, err = self._run(
            ["keys", "show", self.key, "-a"] + self._keys_flags())
        a = (out or "").strip()
        # Приставка НЕ прибита к `axm1`: тот же код гоняется на местном
        # девнете wasmd, где адреса начинаются с `wasm1`. Проверяется вид
        # bech32, а не имя цепочки.
        if rc != 0 or not re.fullmatch(r"[a-z]{2,10}1[02-9ac-hj-np-z]{38,}", a):
            raise TxFailed(f"адрес ключа {self.key!r} не получен: "
                           f"{(err or out or '').strip()[:200]}")
        self._address = a
        return a

    # --- отправка ---------------------------------------------------------

    def send(self, msg, funds=None, contract=None):
        """Отправить сообщение и ДОЖДАТЬСЯ `code=0`.

        Возврат — разобранная сделка. Любой иной исход — исключение:
        петля не должна продолжать, считая сделанным отвергнутое.

        `contract` переопределяет адрес. Нужно для расчёта CW20: там
        заказчик платит не реестру, а токену — `Send` на токен с
        вложенным сообщением, — и это единственный путь для тех денег.
        """
        args = ["tx", "wasm", "execute", contract or self.contract,
                json.dumps(msg, ensure_ascii=False, separators=(",", ":")),
                "--from", self.key, "--chain-id", self.chain_id,
                "--gas", "auto", "--gas-adjustment", self.gas_adjustment,
                "--gas-prices", self.gas_prices, "-y", "-o", "json"]
        if funds:
            args += ["--amount", funds]
        rc, out, err = self._run(args + self._tx_flags())
        text = f"{out}\n{err}"
        # Отказ на РАЗБОРЕ, до блока: симуляция газа исполняет сообщение, и
        # отказ контракта виден уже здесь. Это обычный путь для гонки за
        # заданием, поэтому текст сохраняется как есть.
        if rc != 0:
            raise TxFailed(text.strip()[:400])
        try:
            res = json.loads(out)
        except ValueError:
            raise TxFailed(f"ответ axmd не разобран: {text.strip()[:300]}")
        if int(res.get("code", 0)) != 0:
            raise TxFailed(f"code={res.get('code')}: "
                           f"{res.get('raw_log', '')[:400]}")
        h = res.get("txhash")
        if not h:
            raise TxFailed(f"в ответе нет txhash: {out[:200]}")
        return self.wait_tx(h)

    def wait_tx(self, txhash):
        """Дождаться сделки в блоке и потребовать `code=0`.

        Пока сделки нет, запрос отвечает ошибкой — это не отказ, а «ещё
        не в блоке». Отличается по тексту: `not found`.
        """
        deadline = time.time() + self.wait
        last = ""
        while time.time() < deadline:
            time.sleep(2)
            rc, out, err = self._run(
                ["query", "tx", "--type=hash", txhash, "-o", "json"]
                + self._query_flags())
            if rc != 0:
                last = f"{out}\n{err}".strip()
                if re.search(r"not found|no such tx", last, re.I):
                    continue
                # Недоступный узел — тоже ждём: сделка уже ушла, и
                # считать её провалившейся нельзя.
                continue
            try:
                res = json.loads(out)
            except ValueError:
                last = out[:200]
                continue
            code = int(res.get("code", 0))
            if code != 0:
                raise TxFailed(f"{txhash}: code={code}: "
                               f"{res.get('raw_log', '')[:400]}")
            # Сама сделка отдаётся целиком: в её событиях лежит номер
            # созданного задания, и заказчику больше взять его негде —
            # номер присваивает контракт, а не тот, кто платил.
            return {"txhash": txhash, "height": res.get("height"), "code": 0,
                    "tx": res}
        raise TxFailed(f"{txhash}: за {self.wait} с не попала в блок. "
                       f"Последнее: {last[:200]}")
