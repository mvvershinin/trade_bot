# Котировки (WebSocket)

URL: https://trade-api.bcs.ru/websocket/market-data/quotes

WebSocket API позволяет получать **котировки инструмента** в реальном времени.

## 🔌 WebSocket URL

```text
wss://ws.broker.ru/trade-api-market-data-connector/api/v1/market-data/ws
```

Тот же сокет, что свечи и обезличенные сделки, — различает `dataType`.

## 📤 Подписка

```json
{
  "subscribeType": 0,
  "dataType": 3,
  "instruments": [ { "ticker": "SBER", "classCode": "TQBR" } ]
}
```

`subscribeType`: `0` Подписка, `1` Отписка. `dataType`: **`3` — Котировка**.

## 📥 Ответ

```json
{
  "responseType": "Quotes",
  "ticker": "SBER", "classCode": "TQBR", "type": "refresh",
  "dateTime": "2024-11-01T10:00:00.000Z",
  "securityTradingStatus": 17,
  "currency": "RUB",
  "bid": 305.86, "offer": 305.96,
  "open": 304.76, "close": 304.75, "high": 307.24, "low": 304.75,
  "theoreticalPrice": 0, "last": 305.97,
  "bidYield": 0, "offerYield": 0,
  "change": 1.05, "changeRate": 0.34
}
```

| Поле | Тип | Описание |
|---|---|---|
| `responseType` | string (enum) | `Quotes` — данные котировки |
| `ticker` / `classCode` | string | Инструмент и класс |
| `type` | string | Тип объекта котировки |
| `dateTime` | date-time | Время обновления данных (UTC) |
| **`securityTradingStatus`** | number (enum) | `2` Торги приостановлены · **`17` Торги открыты** · `18` Торги закрыты · `100` Закрытие торгов · `101` Открытие торгов · `102` Аукцион · `103` Аукцион закрытия · `104` Дискретный аукцион |
| `currency` | string | Валюта котировки |
| `last` | number | Последняя цена сделки |
| `bid` / `offer` | number | Лучшие цены покупки / продажи |
| `open` / `close` | number | Цена открытия сессии / закрытия предыдущей |
| `high` / `low` | number | Максимум / минимум за день |
| `theoreticalPrice` | number | Расчетная цена (у опциона) |
| `bidYield` / `offerYield` | number | Доходности (для облигаций) |
| `change` / `changeRate` | number | Изменение за сессию в валюте цены / в % |

Коды ошибок: `NO_DATE`, `NOT_FOUND`, `INCORRECT_JSON`, `BAD_REQUEST`, `UNAUTHORIZED`.

---

## Зачем это нам — второй признак «рынок жив», без HTTP

`securityTradingStatus` **потоком**, а не запросом. Это важно: документация прямо
советует «для потоковых данных предпочитайте WebSocket, а не периодические
HTTP-запросы» (`29-restrictions.md`), а HTTP-версия котировок тратит лимит 10 RPS.

Разбор молчания потока свечей становится трёхслойным и не требует ни одного
HTTP-запроса:

| слой | источник | что различает |
|---|---|---|
| соединение живо? | ping каждые 30 с (`28-ping-pong.md`) | обрыв связи |
| торги идут? | `securityTradingStatus` потоком | пауза, аукцион, закрытие |
| сделки есть? | приход свечей | тишина на рынке |

⚠️ Подписка идёт **в тот же сокет**, что и свечи (`dataType: 1`), в пределах лимита
100 инструментов на соединение. То есть отдельного соединения не нужно —
и это дешевле, чем кажется.
