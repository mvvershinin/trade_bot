# Портфель (WebSocket)

URL: https://trade-api.bcs.ru/websocket/portfolio

WebSocket API предоставляет **данные о позициях портфеля**, стоимости активов, P/L,
валютных остатках и блокировках в режиме реального времени.

## 🔌 WebSocket URL

```text
wss://ws.broker.ru/trade-api-bff-portfolio/api/v1/portfolio/ws
```

Аутентификация — заголовок HTTP при подключении:

```text
Authorization: Bearer <ACCESS_TOKEN>
```

Подписки нет.

## 📥 Формат server → client

Сервер отправляет **массив объектов** — каждая запись описывает одну позицию.
Состав полей совпадает с HTTP-версией (`04-portfolio.md`):

`type` · `subAccountId` *(deprecated)* · `agreementId` *(deprecated)* · `account` ·
`exchange` · `ticker` · `displayName` · `baseAssetTicker` · `currency` ·
**`expireDate` дата экспирации** · `upperType` · `instrumentType` · `term` ·
`quantity` (шт.) · **`locked` занято под активные заявки или ГО** · `balancePrice` ·
`currentPrice` · `balanceValue` · `balanceValueRub` / `Usd` / `Eur` · `currentValue` ·
`currentValueRub` / `Usd` / `Eur` · `unrealizedPL` · `unrealizedPercentPL` ·
**`dailyPL`** · **`dailyPercentPL`** · `portfolioShare` · `scale` · `minimumStep` ·
`board` · `priceUnit` · `faceValue` · `accruedIncome` · `logoLink` *(deprecated)* ·
`isBlocked` · **`isBlockedTradeAccount`** · **`lockedForFutures` занято под ГО
по фьючерсам** · `ratioQuantity` количество в лоте

### Пример (сокращён до двух записей)

```json
[
  { "type": "moneyLimit", "account": "1234567/25", "exchange": "MOEX",
    "ticker": "RUB", "displayName": "RUB", "currency": "RUB",
    "upperType": "CURRENCY", "instrumentType": "CURRENCY", "term": "T0",
    "quantity": 4544.79, "locked": 0, "balancePrice": 1, "currentPrice": 1,
    "currentValueRub": 4544.79, "unrealizedPL": 0, "dailyPL": 0,
    "isBlocked": false, "isBlockedTradeAccount": false, "lockedForFutures": 0 },
  { "type": "depoLimit", "account": "3534991/25", "exchange": "MOEX",
    "ticker": "SBER", "displayName": "Сбербанк", "currency": "RUB",
    "upperType": "RUSSIA", "instrumentType": "STOCK", "term": "T0",
    "quantity": 2, "locked": 0, "balancePrice": 300.5267, "currentPrice": 296,
    "currentValue": 592, "unrealizedPL": -9.0533, "unrealizedPercentPL": -1.5062,
    "dailyPL": 0, "board": "TQBR", "minimumStep": 0.01, "scale": 2 }
]
```

Ошибки: `401 UNAUTHORIZED`, `400 VALIDATION_ERROR`, `404 NOT_FOUND`, `500`.
⚠️ Лимит: **2 одновременных соединения**, 8 кб объём.

---

## Зачем это нам

⬜ **`isBlockedTradeAccount`** — торговый счёт заблокирован. Это состояние обязано
   гасить «Старт» с объяснением, а не выясняться отказом заявки. Приходит потоком,
   то есть блокировка **посреди торгового дня** тоже будет замечена;
⬜ **`lockedForFutures`** и `locked` — сколько занято под ГО и заявки;
⬜ **`expireDate`** — экспирация. Открытый вопрос №8 (перенос позиции на следующий
   контракт) без этого поля не решается; сейчас MXU6 = MIX-9.26, экспирация 17.09.2026,
   то есть меньше двух недель;
⬜ `dailyPL` / `dailyPercentPL` — дневной результат. ⚠️ Включает переоценку открытой
   позиции, а открытый вопрос №3 («считать ли бумажный убыток») не закрыт.
   Брать это поле под дневной лимит убытка молча — значит решить за владельца счёта.

⚠️ **`account` в примере разный у двух записей одного ответа** (`1234567/25`
и `3534991/25`). Значит поток отдаёт **несколько торговых счетов сразу**, а токен
привязан к одному счёту (`00-quickstart.md`). Отбор по счёту обязан быть явным,
иначе программа посчитает чужие позиции своими.
