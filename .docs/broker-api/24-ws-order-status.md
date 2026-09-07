# Статус заявок (WebSocket)

URL: https://trade-api.bcs.ru/websocket/operations/status

WebSocket API предоставляет обновления о статусе заявок в реальном времени.

## 🔌 WebSocket URL

```text
wss://ws.broker.ru/trade-api-bff-operations/api/v1/orders/events/ws
```

## 🔐 Аутентификация

```text
Authorization: Bearer <ACCESS_TOKEN>
```

Подписки нет — сервер шлёт события сам после подключения.

## 📥 Формат ответа

```json
{
  "originalClientOrderId": "517661df-d051-461d-9389-988abf24de4d",
  "clientOrderId": "",
  "data": {
    "messageType": "8",
    "orderStatus": "2",
    "executionType": "2",
    "orderQuantity": 100,
    "executedQuantity": 100,
    "lastQuantity": 50,
    "remainedQuantity": 0,
    "ticker": "SBER",
    "classCode": "TQBR",
    "side": "1",
    "orderType": "2",
    "averagePrice": 244.5,
    "orderId": "20241030-TQBR-12345678910",
    "executionId": "TQBR-Z3fE7c-S-1-1-N",
    "price": 244.5,
    "currency": "RUB",
    "clientCode": "123456",
    "transactionTime": "2024-10-30T09:01:00.000Z",
    "tradeDate": "2024-10-30",
    "orderNumber": "12345678910",
    "accruedCoupon": 0,
    "executionValue": 24450,
    "commission": 12.3,
    "securityExchange": "TQBR"
  }
}
```

### Поля верхнего уровня

| Поле | Тип | Описание |
|---|---|---|
| `originalClientOrderId` | uuid | Уникальный идентификатор изменённой заявки |
| `clientOrderId` | uuid | Уникальный идентификатор заявки |
| `data` | object | Детали исполнения |

### Поля `data`

| Поле | Тип | Описание |
|---|---|---|
| `messageType` | string | Тип сообщения |
| `orderStatus` | string (enum) | `0` Новая · `1` Частично исполнена · `2` Полностью исполнена · `4` Отменена · `5` Заменена · `6` Отменяется · `8` Отклонена · `9` Заменяется · `10` Ожидает подтверждения новой |
| `executionType` | string (enum) | `0` Новая · `1` Частично исполнена · `2` Исполнена · `4` Отменена · `6` Ожидает отмены · `5` Заменена · `8` Отклонена · `9` Приостановлена · `10` Ожидает подтверждения новой · **`11` Сделка** · `12` Статус заявки · `13` Исправлено |
| `orderQuantity` | number | Количество в заявке (шт.) |
| `executedQuantity` | number | Исполненное количество (шт.) |
| `lastQuantity` | number | Количество в текущей сделке (шт.) |
| `remainedQuantity` | number | Оставшееся количество (шт.) |
| `ticker` / `classCode` | string | Инструмент и класс |
| `side` | string (enum) | `1` Покупка, `2` Продажа |
| `orderType` | string (enum) | `1` Рыночная, `2` Лимитная |
| `averagePrice` | number | Средняя цена исполнения |
| `orderId` | string | Уникальный идентификатор заявки |
| **`executionId`** | string | **Уникальный идентификатор сделки** |
| `price` | number | Цена заявки |
| `currency` | string | Валюта |
| `clientCode` | string | Код клиента |
| `transactionTime` | date-time | Дата и время транзакции |
| `tradeDate` | date-time | Дата сделки. Вечерняя сессия FORTS — дата следующей сессии |
| `orderNumber` | string | Номер заявки |
| `accruedCoupon` | number | НКД |
| `executionValue` | number | Объем сделки |
| **`commission`** | number | **Комиссия** |
| `securityExchange` | string | Идентификатор биржи |

Ошибки: `401 UNAUTHORIZED`, `500 INTERNAL SERVER ERROR`.

---

## Зачем это нам — вторая ловушка Э1-5 закрыта

Долг `/risk` от 03.09.2026: сторож уникальности сделки в журнале работает по ключу
`(session_id, symbol, side, entry_ts, exit_ts)`; на живом потоке он **разойдётся** —
повтор, собранный заново из ответа брокера, отличается на секунды и ляжет второй
сделкой, а «прибыль за день» удвоится.

**Лечится `executionId`** — собственный идентификатор исполнения от брокера,
приходящий в реальном времени, без опроса.

⚠️ Три вещи, которые надо сказать честно:

1. **`executionId` — идентификатор исполнения, а не «сделки» нашего журнала.**
   Наша сделка = вход + выход, то есть минимум два исполнения; при частичном
   исполнении (`orderStatus = 1`, `lastQuantity`) их больше. Значит правка схемы —
   не колонка в `journal_trade`, а **отдельная таблица исполнений**, из которой
   сделка собирается. Это работа боевого исполнителя (Э1-9 боевой), не Э1-5.
2. **`commission` приходит от брокера фактом.** Правило 4 проекта требует комиссию
   отдельной строкой; здесь есть чем сверить нашу модель (14 ₽ за контракт
   на сторону) с реальностью — и это первый случай, когда сверка возможна.
3. **`executionType = 11` («Сделка») отличает факт исполнения от смены статуса
   заявки.** Писать в журнал по любому событию — значит писать шум; фильтр
   по этому полю обязан быть назван в контракте.

⚠️ Лимит: **4 одновременных соединения** на сервис «Заявки» (`29-restrictions.md`).
