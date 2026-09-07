# Лимиты

URL: https://trade-api.bcs.ru/http/limits

```
GET
## https://be.broker.ru /api/v1/limits

```

Получение информации о вашем портфеле через сервис «Лимиты»

## Responses

- 200
- 429

OK

- application/json

- Schema
- Example (auto)

**Schema**

**depoLimit** object[] - Array [
**ticker** string Тикер инструмента

**classCode** string Код класса ценной бумаги

**exchange** string Код биржи

**averagePrice** number Средневзвешенная цена открытия позиции

**quantity** object **type** string Режим расчетов — срок, в течение которого произойдет расчет по сделке. [Подробнее](https://bcs.ru/faq/category/115/581)

**Possible values:** [ `T0` , `T1` , `T2` , `T365` ]

**value** number Количество этого актива в вашем портфеле в штуках

**quantityBatch** object **type** string Режим расчетов — срок, в течение которого произойдет расчет по сделке. [Подробнее](https://bcs.ru/faq/category/115/581)

**Possible values:** [ `T0` , `T1` , `T2` , `T365` ]

**value** number Количество этого актива в вашем портфеле в лотах

**instrumentType** string Тип инструмента (например, акция, облигация)

**loadDate** date-time Дата и время обновления данных

**lockedBuyValue** number Количество денег в активных заявках на покупку данной бумаги

**lockedSellValue** number Количество денег в активных заявках на продажу данной бумаги

**lockedBuyQuantity** number Количество заблокированных ценных бумаг под заявки на покупку в штуках

**lockedSellQuantity** number Количество заблокированных ценных бумаг под заявки на продажу в штуках

- ]

**futureHolding** object[] - Array [
**ticker** string Тикер инструмента

**classCode** string Код класса ценной бумаги

**exchange** string Код биржи

**cbplPlanned** number Стоимостная оценка планируемых позиций после ближайшего клиринга

**varMargin** number Оценка размера вариационной марки по позициям

**positionValue** number Стоимость позиций срочного рынка

**totalNet** number Текущие чистые позиции

**executionDate** date-time Дата исполнения

**totalVarMargin** number Суммарная вариационная маржа по итогам основного клиринга, начисленная по всем позициям

**realVarMargin** number Начисленная в ходе клиринга вариационная маржа

**averagePrice** number Средневзвешенная цена открытия позиции

**instrumentType** string Тип инструмента

**tradeDate** date-time Дата торговой сессии

- ]

**moneyLimits** object[] - Array [
**exchange** string Код биржи

**currencyCode** string Код валюты

**locked** number Количество денег, занятых в активных заявках

**averagePrice** number Средневзвешенная цена открытия позиции

**instrumentType** string Тип инструмента

**quantity** object **type** string Режим расчетов — срок, в течение которого произойдет расчет по сделке. [Подробнее](https://bcs.ru/faq/category/115/581)

**Possible values:** [ `T0` , `T1` , `T2` , `T365` ]

**value** number Количество этого актива в вашем портфеле в штуках

**loadDate** date-time Дата и время обновления данных

- ]

**futuresLimits** object[] - Array [
**currencyCode** string Код валюты

**exchange** string Код биржи

**accruedint** number Вариационная маржа на текущий день

**cbpLimit** number Текущий лимит открытых позиций

**cbplUsed** number Позиции после последнего клиринга

**cbplPlanned** number Стоимостная оценка планируемых позиций после ближайшего клиринга

**cbplUsedForOrders** number Величина ГО, зарезервированного под клиентские заявки

**cbplUsedForPositions** number Величина ГО, зарезервированного под открытые клиентские позиции

**optionsPremium** number Премии по опционам

**instrumentType** string Тип инструмента

**loadDate** date-time Дата и время обновления данных

**varMargin** number Оценка размера вариационной марки по позициям

**realVarMargin** number Начисленная в ходе клиринга вариационная маржа

- ]

```json
{
  "depoLimit": [
    {
      "ticker": "string",
      "classCode": "string",
      "exchange": "string",
      "averagePrice": 0,
      "quantity": {
        "type": "T0",
        "value": 0
      },
      "quantityBatch": {
        "type": "T0",
        "value": 0
      },
      "instrumentType": "string",
      "loadDate": "2024-07-29T15:51:28.071Z",
      "lockedBuyValue": 0,
      "lockedSellValue": 0,
      "lockedBuyQuantity": 0,
      "lockedSellQuantity": 0
    }
  ],
  "futureHolding": [
    {
      "ticker": "string",
      "classCode": "string",
      "exchange": "string",
      "cbplPlanned": 0,
      "varMargin": 0,
      "positionValue": 0,
      "totalNet": 0,
      "executionDate": "2024-07-29T15:51:28.071Z",
      "totalVarMargin": 0,
      "realVarMargin": 0,
      "averagePrice": 0,
      "instrumentType": "string",
      "tradeDate": "2024-07-29T15:51:28.071Z"
    }
  ],
  "moneyLimits": [
    {
      "exchange": "string",
      "currencyCode": "string",
      "locked": 0,
      "averagePrice": 0,
      "instrumentType": "string",
      "quantity": {
        "type": "T0",
        "value": 0
      },
      "loadDate": "2024-07-29T15:51:28.071Z"
    }
  ],
  "futuresLimits": [
    {
      "currencyCode": "string",
      "exchange": "string",
      "accruedint": 0,
      "cbpLimit": 0,
      "cbplUsed": 0,
      "cbplPlanned": 0,
      "cbplUsedForOrders": 0,
      "cbplUsedForPositions": 0,
      "optionsPremium": 0,
      "instrumentType": "string",
      "loadDate": "2024-07-29T15:51:28.071Z",
      "varMargin": 0,
      "realVarMargin": 0
    }
  ]
}
```

Too Many Requests

- application/json

- Schema
- Example (auto)

**Schema**

**timestamp** int64

**traceId** string

**type** string **Possible values:** [ `VALIDATION_ERROR` , `RESOURCE_EXHAUSTED` , `USER_BLOCKED` , `BAD_REQUEST` , `NOT_FOUND` , `UNAUTHORIZED` , `FORBIDDEN` , `CONFLICT` , `INTERNAL_SERVER_ERROR` , `SESSION_NOT_FOUND_ERROR` , `SESSION_EXPIRED_ERROR` , `SESSION_FAILED_ERROR` ]

**errors** object[] - Array [
**type** string

**field** string

**payload** object **property name*** object

- ]

**displayOptions** object **property name*** object

```json
{
  "timestamp": 0,
  "traceId": "string",
  "type": "VALIDATION_ERROR",
  "errors": [
    {
      "type": "string",
      "field": "string",
      "payload": {}
    }
  ],
  "displayOptions": {}
}
```

#### Authorization: http

```
**name:**[Bearer Authentication](/http/openapi-definition#authentication) **type:**http **scheme:** bearer **bearerFormat:** JWT
```

Base URL https://be.broker.ru — Auth Bearer Token

---

## Зачем это нам

Это источник для **предохранителя «свободные средства видны до подачи заявки»**.
Нужные поля — в `futuresLimits`:

* `cbpLimit` — текущий лимит открытых позиций;
* `cbplUsedForPositions` — ГО, зарезервированное под открытые позиции;
* `cbplUsedForOrders` — ГО, зарезервированное под заявки;
* `cbplPlanned` — оценка после ближайшего клиринга;
* `varMargin` / `realVarMargin` — вариационная маржа.

⚠️ **Имена контейнеров у сайта и PDF разные — единственное число против
множественного.** Страница сайта (с неё снята эта выписка) называет
`moneyLimits` и `futuresLimits`; официальный PDF — `moneyLimit` и
`futuresLimit`. Живьём не проверено ни одно написание, поэтому
`broker/account.py` читает оба. Промах по имени здесь не ломает ничего
громко — все числа просто остаются пустыми, снимок помечается неполным,
и робот не идёт в бой, — но выясняется это в торговое время.

⚠️ **Имя поля «текущий лимит открытых позиций» тоже разное:** сайт —
`cbpLimit`, PDF — `cbpl.init`. Рядом в PDF стоят `cbp1Planned` (цифра «1»
вместо буквы «l») и `cbplusedForOrders`, то есть таблица PDF набрана
с опечатками, и доверять её написаниям нельзя. Читаются все.

⚠️ **Контейнеры приходят по валютам.** `moneyLimits` и `futuresLimits` —
массивы: у каждого элемента свой `currencyCode` и `exchange`. «Взять первый
элемент» означает шанс прочитать доллары как рубли и подставить их в проверку
свободных средств перед заявкой. Разбор берёт рублёвый блок явно
(`RUB`/`RUR`/`SUR`/`643`), а при неоднозначности оставляет число пустым.

⚠️ **PDF не знает полей `lockedBuyQuantity` и `lockedSellQuantity`** — они
есть только на сайте. Нам они пока не нужны.

Отказ **429** несёт `type: RESOURCE_EXHAUSTED` — это и есть машинно-читаемый признак
превышения частоты запросов, вместе с `traceId` для разбора.
