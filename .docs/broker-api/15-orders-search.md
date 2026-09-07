# Получение списка всех заявок

URL: https://trade-api.bcs.ru/http/operations/all-order-list

```
POST
## https://be.broker.ru/trade-api-market-data-connector /api/v1/orders/search
```

> ⚠️ **Внимание!** В списке заявок отображаются заявки, поданные начиная
> с **26.01.2026**. Заявки, созданные ранее этой даты, в список не включены.

## Request

### Query Parameters
`page` integer `>= 0` **Default:** `0` · `size` integer `1…100` **Default:** `50` ·
`sort` string[] — [ `orderDateTime,asc|desc`, `updateDateTime,asc|desc`, `ticker,asc|desc`,
`classCode,asc|desc`, `orderType,asc|desc`, `side,asc|desc` ]

### Body
`startDateTime` / `endDateTime` date-time · `side` integer [`1` Покупка, `2` Продажа] ·
`orderStatus` integer[] — `1` Снята, `2` Исполнена, `3` Активна ·
`orderTypes` integer[] — `1` Рыночная, `2` Лимитная, `3` Айсберг, `4` Стоп-лимит,
`5` Тейк-профит (порождает лимитную заявку), `6` Стоп-лосс, `7` Тейк-профит и стоп-лосс,
`10` Лимитная на 30 дней, `11` Тейк-профит ·
`tickers` string[] · `classCodes` string[]

## Responses — 200 OK

**records** object[] — список заявок:

`orderNum` int64 номер заявки · `orderId` string уникальный идентификатор заявки ·
`clientCode` string · `executionDateTime` date-time дата и время исполнения ·
`executedValue` double исполненный объем в валюте · `orderDateTime` date-time дата подачи ·
`tradeDate` date торговая дата · `updateDateTime` date-time последнее обновление ·
`ticker` / `classCode` string · **`takePrice`** double цена тейк-профита ·
**`stopPrice`** double цена стоп-лосса · `price` double цена заявки ·
`settlementCurrency` string · `orderQuantity` double количество (шт.) ·
`remainedQuantity` double оставшееся · `executedQuantity` double исполненное ·
**`rejectReason`** string причина отклонения · `averagePrice` double средняя цена исполнения ·
`calculationVolume` double расчетный объем · `contractSum` double сумма контракта ·
`orderStatus` int32 (`1` Снята, `2` Исполнена, `3` Активна) ·
`orderType` int32 (список выше) · `side` int32 ·
`orderQuantityLots` / `remainedQuantityLots` / `executedQuantityLots` double — в лотах ·
`linkedOrder` string номер связанной заявки · `stopOrder` string номер стоп-заявки ·
`visible` double видимая часть айсберга ·
`marketTakeProfit` integer (`1` рыночная, `2` лимитная) ·
`marketStopLoss` integer (`1` рыночная, `2` лимитная) ·
`positionPriceStop` double · `positionPriceLimit` double

**totalRecords** int64 · **totalPages** int32

## 429 — общая форма отказа, см. `03-limits.md`

Base URL `https://be.broker.ru/trade-api-market-data-connector`, Bearer JWT. 10 RPS.

---

## Зачем это нам

⬜ **Сверка после перезапуска программы и после обрыва связи.** `orderStatus = 3`
   (Активна) даёт список того, что у брокера сейчас живёт — включая **выставленный
   тейк**. Это единственный способ узнать, что уровень ещё стоит, не полагаясь
   на собственную память: `ui/notices.py` прямо говорит, что снятую руками заявку
   движок не заметит;
⬜ **`rejectReason`** — причина отклонения человеческим языком от брокера.
   В журнал решений идёт она, а не код (правило слоя `ui/`);
⬜ ⚠️ **типы `5`, `7`, `11` — тейк-профит; `6` — стоп-лосс.** Заявка типа `5`
   «порождает лимитную заявку»: то есть у одного нашего тейка может оказаться
   **две записи** у брокера — условная и порождённая ею лимитная. Считать
   выставленные уровни простым подсчётом строк нельзя;
⬜ ограничение **26.01.2026** — заявок раньше через API не видно.
