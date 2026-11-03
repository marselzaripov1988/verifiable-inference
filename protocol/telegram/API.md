# Интерфейсы для своего бота

Всё, с чем говорит `bot.py`, — чтобы написать бота заново, на любом
языке. Рабочий образец каждого пункта — `bot.py` и `../daemon/`
(`chain.py`, `client.py`, `delivery.py`, `tx.py`); ожидаемое поведение
описано проверками в `test_bot.py`.

## 1. Цепочка, чтение (REST)

`REST = https://axm-lcd.trickle.pro`

Запросы к реестру:

```
GET {REST}/cosmwasm/wasm/v1/contract/{CONTRACT}/smart/{base64(JSON запроса)}
```

| Запрос | Что в ответе |
|---|---|
| `{"config":{}}` | модель, `verifier_fee_bps`, `settlement` (`{"native":"uaxm"}` или `{"cw20":"axm1…"}`), `checks_required`, `claim_ttl_seconds`, `check_ttl_seconds`, залог |
| `{"job":{"job_id":N}}` | `state` (`open` / `claimed` / `answered` / `settled`), `executor`, `prompt_hash`, `answer_hash`, `guilty` |
| `{"checks":{"job_id":N}}` | проверки, собранные по заданию |
| `{"executor":{"address":"axm1…"}}` | запись узла: статус, залог, `endpoint`, выполненные, брошенные, обвинения |
| `{"next_job":{}}` | номер, который получит следующее задание |
| `{"jobs":{"start_after":N,"limit":M}}` | страница заданий и `next_job` |

Чего нет в цепочке, того нет в протоколе: состояний «отменено» и
«просрочено» не бывает — снятое и истёкшее задание тоже `settled`, с
`guilty: null`.

Обычные запросы Cosmos:

- `GET {REST}/cosmos/bank/v1beta1/balances/{адрес}` — баланс кошелька бота;
- `GET {REST}/cosmos/base/tendermint/v1beta1/blocks/latest` — время
  цепочки, от него считаются сроки.

Тестовый реестр для 4090:
`CONTRACT = axm106jyr6sdutlwhpng0q9qclxfn5kz20e3np594fyqx38fyzuw0xfs0mm3fm`.
Боевые — в [../УЗЕЛ.md](../УЗЕЛ.md).

## 2. Цепочка, запись (RPC)

| | |
|---|---|
| `NODE` | `https://api-chain.axiomechain.org` |
| `chain-id` | `axiome-1` |
| `gas-prices` | `0.0025uaxm` |

Сделки подписывает кошелёк бота: `axmd tx wasm execute` или любой Cosmos
SDK (CosmJS, cosmpy).

**Задать вопрос:**

```json
{"submit_job": {"prompt_hash": "<sha256 текста, hex>",
                "prompt_uri": "http://<адрес бота>:8080",
                "price": "10000"}}
```

С монетами `price + fee`, где `fee = ceil(price · verifier_fee_bps / 10000)`.
Округление **вверх** и точное: на единицу больше или меньше — контракт
отвергнет сделку. На тестовом реестре это `10485uaxm`.

Если `settlement` — cw20, то же сообщение кладётся в base64 и
отправляется на адрес токена:

```json
{"send": {"contract": "<CONTRACT>", "amount": "10485", "msg": "<base64 submit_job>"}}
```

Номер задания — атрибут `job_id` события `wasm` в результате сделки.

**Снять невзятое задание:** `{"cancel_job": {"job_id": N}}`, без монет.
Деньги возвращаются на кошелёк.

## 3. Раздача вопроса — её держит сам бот

В цепочке лежит только хэш вопроса; текст узлы берут у бота:

```
GET {PROMPT_URI}/{prompt_hash}.txt
GET {PROMPT_URI}/{job_id}.txt
```

`prompt_hash = sha256(текст в UTF-8)`, hex. Файл выкладывается **до**
`submit_job`: задание, текст которого взять неоткуда, никто не выполнит,
а деньги оно займёт. Адрес должен быть виден из интернета. Отдавать
стоит только эти два вида имён — без списка каталога.

## 4. Ответ от исполнителя

`endpoint` исполнителя — из `{"executor":{"address": job.executor}}`.

```
GET {endpoint}/{job_id}.json
GET {endpoint}/{answer_hash}.json
```

В ответе связка `{"prompt": "…", "answer": "…", …}`.

**Обязательно перед показом:**

1. `sha256(prompt) == job.prompt_hash` и `sha256(answer) == job.answer_hash`.
   Связка не берётся на слово: бот — курьер, а не источник доверия.
2. Показывать ответ только при `state == "settled"` и `guilty == false`.
   `guilty: true` — проверяющие признали ответ негодным; `guilty: null` —
   проверки не собрались в срок. В обоих случаях ответ не показывается,
   деньги уже вернулись на кошелёк.

**Жив ли узел:** `GET {endpoint}/0.json`. Ответ 200 или 404 — жив
(узел раздаёт файлы обычным HTTP-сервером, на несуществующий файл 404).
Любой другой код или таймаут — нет: снятая машина отвечает с того же
порта 503 от прокси хоста.

## 5. Telegram

`https://api.telegram.org/bot{TOKEN}/` — `getUpdates`, `sendMessage`,
`editMessageText`. Токен даёт [@BotFather](https://t.me/BotFather).

## Что не передавать

- Токен своего бота: стороннему разработчику — его собственный.
- Ключ от кошелька заказчика или узла. Боту — отдельный кошелёк с
  небольшим остатком: ключ лежит на сервере без пароля.
