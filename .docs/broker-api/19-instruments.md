# Справочник инструментов — три метода, одна схема ответа

Три эндпоинта возвращают **один и тот же объект инструмента**, различаясь только
способом отбора. Схема выписана здесь один раз; у каждого метода — свой запрос.

Base URL у всех: `https://be.broker.ru/trade-api-information-service`, Bearer JWT.
Лимит 10 RPS. Ответ 429 — общая форма отказа (`03-limits.md`).

**Пагинация одинакова у всех трёх.** Path Parameters: `size` int64 `1…100`
**Default:** `50`; `page` int64 **Default:** `0`.
*Определение наличия последующих страниц осуществляется клиентом:* если количество
объектов в ответе меньше `size` — выборка завершена; при равенстве `size` следует
запросить `page + 1`.

---

## 1. Получить инструменты по тикеру

URL: https://trade-api.bcs.ru/http/information/get-instruments-by-tickers
`POST /api/v1/instruments/by-tickers`

Body: **`tickers`** string[] `>= 1`. **Example:** `["SBER","GAZP","ROSN"]`

## 2. Получить инструменты по ISIN

URL: https://trade-api.bcs.ru/http/information/get-instruments-by-isins
`POST /api/v1/instruments/by-isins`

Body: **`isins`** string[] `>= 1`. **Example:** `["RU0009029540","RU0007661625","RU000A0J2Q06"]`

## 3. Получить инструменты по типу

URL: https://trade-api.bcs.ru/http/information/get-instruments-by-type-and-base-asset-ticker
`GET /api/v1/instruments/by-type`

Query: **`type`** string **required** — `CURRENCY`, `STOCK`, `FOREIGN_STOCK`, `BONDS`,
`NOTES`, `DEPOSITARY_RECEIPTS`, `EURO_BONDS`, `MUTUAL_FUNDS`, `ETF`, **`FUTURES`**,
`OPTIONS`, `GOODS`, `INDICES`;
**`baseAssetTicker`** string — название базового актива, обязательно при `type=OPTIONS`.

---

## Схема объекта инструмента (общая для всех трёх)

`ticker` тикер · `boards[]` — `classCode` (**Example:** `TQBR`) и `exchange` (**Example:** `MOEX`) ·
`shortName` краткое наименование · `displayName` наименование для отображения ·
`type` / `instrumentType` тип инструмента (список выше) · `isin` · `registrationCode` ·
`issuerName` · `tradingCurrency` валюта инструмента · `faceValue` номинал ·
**`scale` точность** · **`minimumStep` минимальный шаг цены** · `accruedInt` НКД ·
`currencyStepPrice` валюта шага цены · `settleCode` код расчетов по умолчанию ·
`settlementCurrency` · `settlementDate` · **`maturityDate` дата погашения** ·
**`lotSize` размер лота** · `promoIdx` *(deprecated)* · `isQualifiedOnly` ·
**`isCanShort` признак доступности шорта** · **`baseAsset` название базового актива
для срочного контракта** · `qualifiedTestId` · `qualifiedTestIdTm` ·
`availableForUnqualified` · `currencyNominal` · **`stepPrice` стоимость шага цены** ·
`isBcsProduct` · `logoLink` *(deprecated)* · `couponsPerYear` · `couponRate` ·
`nextCoupon` · `complexProduct` · **`baseAssetFuture` тип базового актива
для фьючерсов** · `subType` · `percentTargetCurrent` · `businessSector` · `peNorm` ·
`priceTangible` · `epsGrowthRate` · `predictedDps` · `dividendYield` ·
`priceChangeYear` · `targetPrice` · `mktcap` · **`isBlocked` признак блокированного
класса** · `businessSectorId` · **`primaryBoard` первичный борд** ·
`secondaryBoards` string[] · **`isCanMargin` признак доступности для лонг** ·
`isReplacementBond` · `subTitle` · `couponTypeName` · `emissionDate` ·
`excludeTypeFlags` *(deprecated)* · `creditRating` · `liquidityRating` · `bcsScore` ·
`bcsScoreColor` · `cfi` *(deprecated)* · `nrdCode` · `strike` ·
`baseAssetSecuritySecCode` · `baseAssetSecurityClassCode` · `businessCountry` ·
`businessCountryCode` · `priceChangeHalfYear` · `priceChangeMonth` ·
`priceChangeEarlyYear` · `excludeTypes` *(deprecated)* · `displayNameSecond`
*(deprecated)* · `firstCurrCode` · `amortisedMty`

---

## Проверено живым запросом 04.09.2026

`POST /api/v1/instruments/by-tickers` с телом `{"tickers":["MXU6"]}` вернул
**массив** (не объект с полем `instruments`):

```json
[{ "ticker": "MXU6",
   "boards": [ { "classCode": "SPBFUT", "exchange": "MOEX" } ],
   "shortName": "MIX-9.26", "displayName": "MIX-9.26",
   "type": "Фьючерсы", "instrumentType": "FUTURES",
   "tradingCurrency": "RUB", "minimumStep": 25.0, "lotSize": 1.0,
   "settleCode": "T+n", "settlementDate": "2026-09-07T00:00:00.000Z",
   "maturityDate": "20260917", "isCanShort": true,
   "baseAsset": "Индекс МосБиржи", "currencyStepPrice": "RUB" }]
```

⚠️ **Официальный PDF описывает ответ так же, как сайт** — объект с обязательным
контейнером `instruments` («Массив инструментов»). То есть **оба документа
расходятся с живым сервером**, и прав живой сервер (порядок доверия —
`README.md` этого каталога).

**Сделано 05.09.2026** (`broker/instruments.py::_listed`): читаются **обе** формы,
а третья — отказ вслух. Прежде разбор ждал только документированной и на живом
ответе молча возвращал пустоту; читалось это как «нет такого тикера». Баг `B-014`.

⚠️ **`type` пришёл по-русски («Фьючерсы»), а в схеме объявлено перечисление
латиницей** (`FUTURES`). `instrumentType` при этом латиницей. Разбор по `type`
сломается; брать `instrumentType` — так и сделано.

⚠️ **`maturityDate` пришёл строкой `"20260917"`**, хотя в схеме объявлен `date-time`.
В схеме же на это есть намёк: пример показывает `"Unknown Type: date-time"`.

`fromisoformat` понимает обе записи, но компактная даёт **наивный** момент, а форма
с `Z` — момент с поясом. Смешанное поле роняло бы «сколько дней до экспирации»
вычитанием из `now_utc()` с `TypeError` ровно на рабочем контракте. С 05.09.2026
наивному значению приписывается UTC (`instruments._maturity`); точность поля —
**сутки**, на внутридневных решениях его использовать нельзя.

⚠️ **`stepPrice` в этом ответе отсутствует.** Значит стоимость шага цены отсюда
сегодня не берётся, а `ruble_per_point` остаётся умолчанием 1,0. Сокращён ли ответ
при снятии или брокер её действительно не отдаёт для фьючерса — не установлено.
Баг `B-015`.

---

## Зачем это нам

⬜ **`minimumStep` = 25,0 и `stepPrice`** — шаг цены и стоимость шага. `DOMAIN.md`
   и расчёт результата в рублях опираются на эту пару; сейчас в журнале есть
   колонка `ruble_per_point` со значением по умолчанию `1.0` — её надо заполнять
   отсюда, а не константой;
⬜ **`maturityDate` = 17.09.2026** — до экспирации MXU6 меньше двух недель.
   Открытый вопрос №8 (перенос позиции на следующий контракт) перестаёт быть
   отдалённым: неделя симуляции на живых данных упрётся в неё;
⬜ **`isCanShort`** — можно ли шортить. Реверсная система шортит обязательно;
   проверка обязана быть до старта, а не отказом заявки;
⬜ **`isBlocked`** — блокировка класса;
⬜ **`boards[]` массив** — у инструмента может быть несколько бордов
   (`primaryBoard` / `secondaryBoards`). Брать первый попавшийся нельзя.
