# Портфель

URL: https://trade-api.bcs.ru/http/portfolio

```
GET
## https://be.broker.ru/trade-api-bff-limit /api/v1/portfolio
```

Получение информации о вашем портфеле через сервис «Портфель»

## Responses — 200 OK, application/json

Ответ — **массив** позиций.

⚠️ **Здесь сайт и PDF расходятся, и это третий такой случай.** Официальный PDF
описывает ответ **объектом с контейнером `positions`** (строка таблицы:
«positions · контейнер · Нет · Массив · Да · Позиции в вашем портфеле»);
страница сайта, с которой снята эта выписка, обещает голый массив.
У справочника инструментов ровно это расхождение кончилось багом `B-014`:
живой сервер прислал массив, разбор ждал контейнер, и программа молча решила,
что тикера нет.

Живьём портфель не запрашивался ни разу. Поэтому `broker/account.py`
**читает оба вида**, а третий вид — отказ, а не пустой список: «позиций нет»
здесь означает разрешение войти поверх уже открытой позиции.

**type** string Тип позиции
**Possible values:** [ `moneyLimit` , `depoLimit` , `futuresLimit` , `futuresHolding` , `otcLimit` ]

**subAccountId** uuid *deprecated* ID субсчета
**agreementId** uuid *deprecated* ID генерального соглашения
**account** string Торговый счет
**exchange** string Код биржи
**ticker** string Тикер инструмента
**displayName** string Наименование инструмента
**baseAssetTicker** string Тикер базового актива
**currency** string Валюта цены
**upperType** string Верхнеуровневый тип инструмента — [ `CURRENCY` , `RUSSIA` , `FOREIGN` , `OTC` ]
**instrumentType** string Тип инструмента
**term** string Код срока расчетов — [ `T0` , `T1` , `T2` , `T365` ]
**quantity** number Количество (шт.)
**locked** number Количество активов, занятых под активные заявки или ГО
**balancePrice** number Балансовая цена (цена открытия позиции) в валюте цены
**currentPrice** number Текущая цена
**balanceValue** number Балансовая стоимость (цена открытия позиции) в валюте цены
**balanceValueRub** number Балансовая стоимость в рублях
**balanceValueUsd** number Балансовая стоимость в долларах
**balanceValueEur** number Балансовая стоимость в евро
**currentValue** number Текущая стоимость в валюте цены
**currentValueRub** number Текущая стоимость в рублях
**currentValueUsd** number Текущая стоимость в долларах
**currentValueEur** number Текущая стоимость в евро
**unrealizedPL** number Изменение цены с момента открытия позиции
**unrealizedPercentPL** number Изменение цены с момента открытия позиции, %
**dailyPL** number Дневное изменение цены с момента открытия позиции
**dailyPercentPL** number Дневное изменение цены с момента открытия позиции, %
**portfolioShare** number Доля в портфеле, % (от рублевой стоимости портфеля)
**scale** int32 Количество знаков
**minimumStep** number Минимальный шаг цены
**board** string Код класса ценной бумаги
**priceUnit** string Единица цены
**faceValue** number Номинал (для облигаций)
**accruedIncome** number Накопленный купонный доход (для облигаций)
**logoLink** string Ссылка на логотип инструмента
**isBlocked** boolean Признак блокированного класса
**isBlockedTradeAccount** boolean Заблокирован ли торговый счет
**lockedForFutures** number Занято под ГО по фьючерсам
**ratioQuantity** number Количество в лоте
**expireDate** string Дата экспирации

```json
[
  {
    "type": "moneyLimit",
    "account": "string",
    "exchange": "string",
    "ticker": "string",
    "displayName": "string",
    "baseAssetTicker": "string",
    "currency": "string",
    "upperType": "CURRENCY",
    "instrumentType": "string",
    "term": "T0",
    "quantity": 0,
    "locked": 0,
    "balancePrice": 0,
    "currentPrice": 0,
    "balanceValue": 0,
    "balanceValueRub": 0,
    "balanceValueUsd": 0,
    "balanceValueEur": 0,
    "currentValue": 0,
    "currentValueRub": 0,
    "currentValueUsd": 0,
    "currentValueEur": 0,
    "unrealizedPL": 0,
    "unrealizedPercentPL": 0,
    "dailyPL": 0,
    "dailyPercentPL": 0,
    "portfolioShare": 0,
    "scale": 0,
    "minimumStep": 0,
    "board": "string",
    "priceUnit": "string",
    "faceValue": 0,
    "accruedIncome": 0,
    "logoLink": "string",
    "isBlocked": true,
    "isBlockedTradeAccount": true,
    "lockedForFutures": 0,
    "ratioQuantity": 0,
    "expireDate": "string"
  }
]
```

## 429 Too Many Requests

Общая форма отказа (одинакова для всех эндпоинтов) — см. `03-limits.md`:
`timestamp`, `traceId`, `type` ∈ [ `VALIDATION_ERROR`, `RESOURCE_EXHAUSTED`, `USER_BLOCKED`,
`BAD_REQUEST`, `NOT_FOUND`, `UNAUTHORIZED`, `FORBIDDEN`, `CONFLICT`, `INTERNAL_SERVER_ERROR`,
`SESSION_NOT_FOUND_ERROR`, `SESSION_EXPIRED_ERROR`, `SESSION_FAILED_ERROR` ],
`errors[]` (`type`, `field`, `payload`), `displayOptions`.

#### Authorization

Bearer Authentication, scheme `bearer`, bearerFormat `JWT`.
Base URL `https://be.broker.ru/trade-api-bff-limit`.

---

## Зачем это нам

Второй источник для предохранителей, рядом с «Лимитами» (`03-limits.md`).
Прямо относящееся к деньгам:

* `lockedForFutures` — занято под ГО по фьючерсам;
* `locked` — занято под активные заявки или ГО;
* `dailyPL` / `dailyPercentPL` — дневной результат по позиции; это кандидат
  в источник **дневного лимита убытка**, но осторожно: открытый вопрос №3
  («считать ли бумажный убыток открытой позиции») ещё не закрыт заказчиком;
* `isBlockedTradeAccount` — торговый счёт заблокирован. Это состояние обязано
  гасить кнопку «Старт» с объяснением, а не выясняться отказом заявки;
* `expireDate` — дата экспирации, для переноса позиции на следующий контракт
  (открытый вопрос №8).

**Размер счёта здесь только суммой.** Дневной лимит убытка считается от размера
счёта (решение 0035), а прямого поля «стоимость портфеля» в этом ответе нет:
оно есть в «Маржинальных показателях» (`portfolioCurrentValue.currentValueRub`),
а тот сервис — **только вебсокет**. По HTTP размер счёта складывается из
`currentValueRub` всех записей: деньги приходят такой же записью, типа
`moneyLimit`. Сумма **живьём не сверялась**; одной записи без `currentValueRub`
достаточно, чтобы отказаться от всей суммы, — заниженный размер счёта означает
заниженный лимит убытка и раннюю остановку без объяснения.

**ГО под контракт — тоже вывод, а не поле.** `lockedForFutures`, делённое на
`quantity`, даёт ГО одного контракта, и только когда позиция открыта. Прямое
поле `futuresCollateral` живёт в «Маржинальных показателях», то есть опять
в вебсокете.

**Мелкие расхождения PDF со страницей сайта** (все — в пользу сайта, живьём
не проверено ничего): PDF описывает `isBlocked` словами `locked`
(«деньги, занятые в активных заявках или под ГО») при типе «логический»;
`priceUnit` у PDF «Число», у сайта `string`; `expireDate` у PDF «Дата»,
у сайта `string`. В разборе это не используется.
