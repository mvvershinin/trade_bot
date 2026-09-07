# Последняя свеча (WebSocket)

URL: https://trade-api.bcs.ru/websocket/market-data/last-candle

Этот метод WebSocket API позволяет получать **последнюю свечу** по инструменту
в режиме реального времени для заданного таймфрейма.

## 🔌 WebSocket URL

```text
wss://ws.broker.ru/trade-api-market-data-connector/api/v1/market-data/ws
```

## 🔐 Аутентификация

```text
Authorization: Bearer <ACCESS_TOKEN>
```

## 🧱 Общий формат сообщений

Все сообщения в WebSocket передаются в формате **JSON**.

- Клиент → сервер: команды подписки / отписки.
- Сервер → клиент: подтверждения подписки, данные свечей, ошибки.

## 📤 Сообщения client → server

### Подписка на последнюю свечу

```json
{
  "subscribeType": 0,
  "dataType": 1,
  "timeFrame": "M1",
  "instruments": [ { "classCode": "TQBR", "ticker": "SBER" } ]
}
```

| Поле | Тип | Обяз. | Описание |
|---|---|---|---|
| `subscribeType` | number (enum) | да | `0` — Подписка, `1` — Отписка |
| `dataType` | number (enum) | да | `1` — Свечи |
| `timeFrame` | string (enum) | да | `M1`, `M5`, `M15`, `M30`, `H1`, `H4`, `D`, `W`, `MN` |
| `instruments` | array | да | Список инструментов для подписки |
| `instruments[].ticker` | string | да | Биржевой тикер |
| `instruments[].classCode` | string | да | Код класса ценной бумаги |

### Отписка — то же сообщение с `subscribeType = 1`.

## 📥 Сообщения server → client

### Успешный ответ о подписке

```json
{
  "responseType": "CandleStickSuccess",
  "subscribeType": 0,
  "ticker": "SBER",
  "classCode": "TQBR",
  "timeFrame": "M1",
  "dateTime": "2024-11-10T10:30:00.000Z"
}
```

### Успешный ответ с данными свечи

```json
{
  "responseType": "CandleStick",
  "ticker": "SBER",
  "classCode": "TQBR",
  "timeFrame": "M1",
  "open": 244.20,
  "close": 244.50,
  "high": 244.70,
  "low": 243.90,
  "volume": 3200,
  "dateTime": "2024-11-10T10:30:00.000Z"
}
```

| Поле | Тип | Обяз. | Описание |
|---|---|---|---|
| `responseType` | string (enum) | да | `CandleStick` — получение данных |
| `ticker` / `classCode` | string | да | Инструмент и класс |
| `timeFrame` | string (enum) | да | Таймфрейм свечи |
| `open` / `close` / `high` / `low` | number | да | Цены |
| `volume` | number | да | **Объём торгов в валюте** |
| `dateTime` | string (datetime) | да | Дата и время свечи (UTC) |

### Ответ с ошибкой

```json
{
  "responseType": "CandleStick",
  "errors": [
    { "message": "Input JSON structure does not match structure, 'timeFrame' field is undefined.",
      "code": "INCORRECT_JSON" }
  ]
}
```

Коды ошибок: `NO_DATE` — нет данных · `NOT_FOUND` — инструмент не найден ·
`INCORRECT_JSON` — невалидный json · `BAD_REQUEST` — ошибка выполнения ·
`UNAUTHORIZED` — клиент не авторизован.

HTTP-ошибки: `400 BadRequest` — неверные параметры; `500` — внутренняя ошибка сервера.

---

## Сверка с живым прогоном 04.09.2026 — сайт прав, PDF врёт

Прогон на настоящем токене чтения, MXU6/SPBFUT, M1:

| что | сайт (эта страница) | PDF `trade-api-docs.pdf` | сервер |
|---|---|---|---|
| поле подписки | `subscribeType` ✅ | `subscriberType` ❌ | `subscribeType` |
| поле времени | `dateTime` ✅ | `dateTimeUtc` ❌ | `dateTime` |
| форма ответа | плоская ✅ | вложенные `candleStick`/`instrument` ❌ | плоская |
| единица `volume` | «в валюте» ✅ | «Объем торгов» (без единицы) | в валюте |

**Вывод: страница сайта верна, PDF устарел.** Правило `CLAUDE.md` «документация
на сайте главнее» подтвердилось замером.

## Что документация по-прежнему не говорит — и что показал замер

⬜ **Признака «свеча закрыта» нет ни в одном поле.** Подтверждено и документацией,
   и прогоном. Закрытие определяется только приходом свечи с бо́льшим `dateTime`
   либо по часам с запасом.
⬜ **Одна минута приходит многократно, объём нарастающим итогом.** Замер: минута
   `05:38` пришла 9 раз, `volume` монотонно рос 224 900 → 30 579 800, `low`
   монотонно падал, `high` держался. Значит это **снимок минуты целиком**,
   а не дельта, и `market.storage.put_minutes` (схлопывание последним значением)
   **верен как есть**.
⬜ **Задержка первой свечи новой минуты: 3–17 с, среднее 8,6 с** (9 минут записи,
   спокойный утренний рынок). Первое значение 68 с отброшено — это свеча предыдущей
   минуты, присланная сразу после подписки.
⬜ **`volume` в валюте, а у нас в базе — контракты** (ISS, значения 1…43 146).
   Пересчёт: 30 579 800 ÷ 224 875 = 136,0. Точное количество даёт канал
   обезличенных сделок (`23-ws-trades.md`), где есть отдельное поле `quantity`.
