# Лимиты (WebSocket)

URL: https://trade-api.bcs.ru/websocket/limits

WebSocket API предоставляет **портфельные данные клиента в реальном времени**:
позиции, денежные лимиты, фьючерсные данные, ГО, долговые нагрузки и др.

## 🔌 WebSocket URL

```text
wss://ws.broker.ru/trade-api-bff-limit/api/v1/limits/ws
```

Аутентификация — заголовок HTTP при подключении:

```text
Authorization: Bearer <ACCESS_TOKEN>
```

Подписки нет.

## 📥 Формат server → client

Сервер отправляет **единый объект**: `depoLimit`, `futureHolding`, `moneyLimits`,
`futuresLimits`. Состав полей — тот же, что у HTTP-версии (`03-limits.md`).

### `depoLimit` — позиции по инструментам
`ticker` · `classCode` · `exchange` · `averagePrice` · `quantity.type` / `.value` (шт.) ·
`quantityBatch.type` / `.value` (лоты) · `instrumentType` · `loadDate` ·
`lockedBuyValue` · `lockedSellValue` · `lockedBuyQuantity` · `lockedSellQuantity`

### `futureHolding` — фьючерсные позиции
`ticker` · `classCode` · `exchange` · `cbplPlanned` · `varMargin` · `positionValue` ·
**`totalNet` текущие чистые позиции** · `executionDate` · `totalVarMargin` ·
`realVarMargin` · `averagePrice` · `instrumentType` · `tradeDate`

### `moneyLimits` — остатки по валютам
`exchange` · `currencyCode` · `locked` · `averagePrice` · `instrumentType` ·
`quantity.type` / `.value` · `loadDate`

### `futuresLimits` — лимиты по срочному рынку
`currencyCode` · `exchange` · `accruedint` вариационная маржа на текущий день ·
**`cbpLimit` текущий лимит открытых позиций** · `cbplUsed` позиции после последнего
клиринга · `cbplPlanned` оценка после ближайшего клиринга ·
**`cbplUsedForOrders` ГО под заявки** · **`cbplUsedForPositions` ГО под позиции** ·
`optionsPremium` · `instrumentType` · `loadDate` · `varMargin` · `realVarMargin`

### Пример (сокращён)

```json
{
  "depoLimit": [ { "ticker": "SBER", "classCode": "TQBR", "exchange": "MOEX",
    "averagePrice": 300.526666, "quantity": { "type": "T365", "value": 2 },
    "instrumentType": "STOCK", "loadDate": "2025-11-13T21:00:00.000Z",
    "lockedBuyValue": 0, "lockedSellValue": 0 } ],
  "futureHolding": [],
  "moneyLimits": [ { "exchange": "MOEX", "currencyCode": "RUB", "locked": 0,
    "instrumentType": "MONEY", "quantity": { "type": "T365", "value": 4544.79 } } ],
  "futuresLimits": [ { "currencyCode": "RUB", "exchange": "FORTS", "accruedint": 0,
    "cbpLimit": -0.26, "cbplUsed": 0, "cbplPlanned": 0, "cbplUsedForOrders": 0,
    "cbplUsedForPositions": 0, "instrumentType": "MONEY", "varMargin": 0 } ]
}
```

Ошибки: `401 UNAUTHORIZED`, `500 INTERNAL SERVER ERROR`.
⚠️ Лимит: **2 одновременных соединения**, 8 кб объём (`29-restrictions.md`).

---

## Зачем это нам

**`futureHolding[].totalNet` — «текущие чистые позиции» у брокера, в реальном
времени.** Это источник для **сверки позиции после обрыва связи** — открытый
вопрос №4 и пункт приёмки «программа пережила разрыв связи и восстановилась сама».

До сегодня в плане стояло, что сверка позиции — этап 2 и авансом её помечать нельзя.
Данные для неё, оказывается, приходят сами, потоком, без опроса.

`exchange: "FORTS"` в `futuresLimits` — наш рынок; `cbpLimit`, `cbplUsedForPositions`
и `cbplUsedForOrders` дают ГО раздельно под позиции и под заявки. То же есть
и в маржинальных показателях (`27-ws-marginal-indicators.md`) — два источника
одного числа, и **они могут разойтись**; какой считать главным, надо решить
до кода, а не после.
