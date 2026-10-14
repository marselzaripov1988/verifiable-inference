"""Цена запроса.

Цена ЗАДАЁТСЯ, а не выводится. Замеры дают порог безубыточности в
долларах — $0,138 за миллион токенов при $0,34 в час за 4090, — но
перевод этого в AXM требует курса токена, а курса у меня нет. Поэтому
`price_micro_per_million` обязателен в настройках и не имеет значения по
умолчанию: умолчание здесь означало бы выдуманный курс в коде.

Счёт по токенам, а не по запросам: запросы различаются в сотни раз, и
плата за запрос означала бы, что короткие платят за длинные.
"""

from __future__ import annotations

from dataclasses import dataclass

MILLION = 1_000_000


class PriceNotSet(ValueError):
    pass


@dataclass(frozen=True)
class Pricing:
    #: Сколько минимальных единиц за миллион токенов (запрос плюс ответ).
    price_micro_per_million: int
    #: Предел длины запроса в токенах. Без него резерв может превысить
    #: любой остаток, а узел — уйти в своп на чужом запросе.
    max_prompt_tokens: int = 2048
    #: Предел длины ответа.
    max_answer_tokens: int = 1024

    def __post_init__(self):
        if self.price_micro_per_million <= 0:
            raise PriceNotSet(
                "price_micro_per_million должен быть задан: курс монеты "
                "в коде не выдумывается")

    def cost(self, tokens: int) -> int:
        """Стоимость в минимальных единицах. Деление ВВЕРХ — в пользу шлюза.

        Вниз означало бы, что запрос в несколько токенов бесплатен, и
        тогда бесплатен любой поток таких запросов.
        """
        if tokens <= 0:
            return 0
        return -(-tokens * self.price_micro_per_million // MILLION)

    def reserve_for(self, prompt_tokens: int, answer_tokens: int) -> int:
        """Сколько резервировать до работы.

        Длина запроса известна точно (токенизатор сервера), длина ответа
        — сверху. Резерв по сумме: иначе клиент с нулевым остатком
        получил бы ответ в долг.
        """
        return self.cost(prompt_tokens + answer_tokens)
