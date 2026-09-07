# Стакан (WebSocket)

URL: https://trade-api.bcs.ru/websocket/market-data/order-book

WebSocket API позволяет получать **стакан котировок (Order Book)** в реальном времени
с глубиной до 20 уровней.

## 🔌 WebSocket URL

```text
wss://ws.broker.ru/trade-api-market-data-connector/api/v1/market-data/ws
```

Тот же сокет, что свечи, котировки и обезличенные сделки, — различает `dataType`.

## 📤 Подписка

```json
{
  "subscribeType": 0,
  "dataType": 0,
  "depth": 20,
  "instruments": [ { "ticker": "SBER", "classCode": "TQBR" } ]
}
```

| Поле | Тип | Обяз. | Описание |
|---|---|---|---|
| `subscribeType` | number (enum) | да | `0` Подписка, `1` Отписка |
| `dataType` | number | да | **`0` — Стакан** |
| `depth` | number | нет | Глубина стакана 1–20. **По умолчанию: 20** |
| `instruments[].ticker` / `.classCode` | string | да | Инструмент и класс |

## 📥 Ответы

### Подтверждение подписки
```json
{ "responseType": "OrderBookSuccess", "subscribeType": 0, "ticker": "SBER",
  "classCode": "TQBR", "depth": 20, "dateTime": "2024-10-30T09:01:00.000Z" }
```

### Данные
```json
{
  "responseType": "OrderBook",
  "ticker": "SBER", "classCode": "TQBR", "depth": 20,
  "dateTime": "2024-10-30T09:01:00.000Z",
  "bidVolume": "59851", "askVolume": "90339",
  "bids": [ { "price": 244.30, "quantity": 100 }, { "price": 244.25, "quantity": 90 } ],
  "asks": [ { "price": 244.35, "quantity": 120 }, { "price": 244.40, "quantity": 50 } ]
}
```

`bidVolume` / `askVolume` — объём покупателей / продавцов в шт.;
`bids[]` / `asks[]` — `price`, `quantity`.

Коды ошибок: `NO_DATE`, `NOT_FOUND`, `INCORRECT_JSON`, `BAD_REQUEST`, `UNAUTHORIZED`.

---

## Зачем это нам

Для этапа 1 не нужен. Понадобится по открытому вопросу №7 (ликвидность
и проскальзывание): нужен ли автоматический переход на лимитные заявки выше порога
объёма и что делать с неисполнившейся лимитной заявкой.

⚠️ В примере `bidVolume` — **строка** (`"59851"`), хотя в таблице полей значится
`number`. Мелочь, но разбор на строгих типах на ней падает; проверять пробой.
