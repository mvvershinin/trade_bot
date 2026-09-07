# Получение списка всех сделок

URL: https://trade-api.bcs.ru/http/trades/get-trades

```
POST
## https://be.broker.ru/trade-api-bff-operations /api/v1/trades/search
```

> ⚠️ **Внимание!** В списке сделок отображаются сделки, совершенные начиная
> с **26.01.2026**. Сделки, совершенные ранее этой даты, в список не включены.

## Request

### Query Parameters
`page` integer `>= 0` **Default:** `0` · `size` integer `>= 1`, `<= 100` **Default:** `50` ·
`sort` string[] — [ `tradeDateTime,asc|desc` , `ticker,asc|desc` , `classCode,asc|desc` , `side,asc|desc` ]

### Body
`side` string [`1` Покупка, `2` Продажа] · `tradeNums` int64[] список номеров сделок ·
`tickers` string[] · `classCodes` string[] · `startDateTime` date-time · `endDateTime` date-time

## Responses — 200 OK

**records** object[] — список сделок:

| Поле | Тип | Описание |
|---|---|---|
| `orderNum` | int64 | Номер заявки |
| `ticker` | string | Тикер инструмента |
| **`tradeNum`** | int64 | **Номер сделки** |
| `clientCode` | string | Код клиента |
| `classCode` | string | Код класса инструмента |
| `settlementCurrency` | string | Валюта расчетов |
| `baseCurrency` | string | Базовая валюта |
| `priceCurrency` | string | Валюта цены |
| `side` | string | `1` Покупка, `2` Продажа |
| `instrumentType` | string | `CURRENCY`, `STOCK`, `FOREIGN_STOCK`, `BONDS`, `NOTES`, `DEPOSITARY_RECEIPTS`, `EURO_BONDS`, `MUTUAL_FUNDS`, `ETF`, **`FUTURES`**, `OPTIONS`, `GOODS`, `INDICES` |
| `dealType` | int32 | Тип сделки |
| `tradeDateTime` | date-time | Дата и время совершения сделки |
| `price` | double | Цена сделки |
| `volume` | double | Объем сделки |
| **`go`** | double | **Гарантийное обеспечение** |
| `contractAmount` | double | Сумма контракта |
| `settleDate` | date | Дата расчетов |
| **`tradeQuantity`** | double | **Количество в штуках** |
| `tradeQuantityLots` | double | Количество в лотах |

**totalRecords** int64 · **totalPages** int32

## 429 — общая форма отказа, см. `03-limits.md`

Base URL `https://be.broker.ru/trade-api-bff-operations`, Bearer JWT. Лимит 10 RPS.

---

## Зачем это нам

⬜ **`tradeNum` — номер сделки от биржи.** Второй кандидат в ключ уникальности
   журнала, рядом с `executionId` из потока статусов (`24-ws-order-status.md`).
   Который из двух правильный — решать при разборе схемы журнала, но **важно,
   что теперь есть из чего выбирать**: до сегодня ключа не было вовсе;
⬜ **`go` — гарантийное обеспечение по конкретной сделке.** Прямой источник
   для проверки ГО, факт вместо оценки;
⬜ **`tradeQuantity` в штуках и `volume` отдельно** — снова подтверждение,
   что у брокера это разные величины;
⬜ **сверка журнала с брокером после обрыва связи.** Открытый вопрос №4 —
   что делать при расхождении позиции; этот метод даёт материал для сравнения
   «что записали мы» против «что видит брокер».

⚠️ **Ограничение 26.01.2026** — сделок раньше этой даты через API не получить.
Для сверки истории это потолок, и его надо знать заранее.
