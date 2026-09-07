# Получить историю неторговых операций

URL: https://trade-api.bcs.ru/http/get-operation-history

```
POST
## https://be.broker.ru/trade-api-bff-marginal-indicators /api/v1/operations/search
```

## Request

### Path Parameters
`size` int64 `1…100` **Default:** `50` · `page` int64 **Default:** `0`

### Body, required

`operationTypes` string[] — тип операции:
`PayOut` Вывод денежных средств · `PayIn` Пополнение счета · `PayTransfer` Перевод ·
`Dividend` Выплата дивидендов · `DividendDebt` Выплата дивидендов РЕПО ·
`BondPayingOff` Выплата купона · **`Commission` Комиссия** · `IncomeTax` НДФЛ ·
`SecurityOut` Списание ценных бумаг · `SecurityIn` Зачисление ценных бумаг ·
`BondPartRepayment` · `BondFullRepayment` · `StructuralProductRepayment` ·
`InvestmentShareRepayment` · `PayOutFx` · `PayOutDfa` · `PayOutDu` · `PayInFx` ·
`PayInDfa` · **`VarMargin` Начисление/списание вариационной маржи**

`statuses` string[] — [ `Approved` , `InProgress` , `Rejected` ]
`startDateTime` / `endDateTime` date-time · `isins` string[] · `tickers` string[] ·
`currencies` string[]

## Responses — 200 OK

**records** object[]:

`id` uuid идентификатор операции · `date` date-time · `isIia` признак ИИС ·
`ticker` · `classCode` · `type` (список выше) · `status` [`Approved`, `InProgress`, `Rejected`] ·
**`sum` сумма операции** · `currency` · `isRestsPayOut` · `isin` · `issuerName` ·
`replenishmentType` · `executionPeriod` ·
**`balanceChange`** — как операция меняет баланс: `Neutral` не изменяет ·
`Negative` уменьшает · `Positive` увеличивает ·
`rejectionReason` причина отклонения

**pageSize** int32

## 429 — общая форма отказа, см. `03-limits.md`

Base URL `https://be.broker.ru/trade-api-bff-marginal-indicators`, Bearer JWT.
⚠️ **Лимит 3 RPS** — самый жёсткий среди всех методов (`29-restrictions.md`).

---

## Зачем это нам

⬜ **`Commission`** — фактические списания комиссии. Правило 4 проекта: комиссия
   всегда отдельной строкой, валовая прибыль без неё результатом не считается.
   Здесь можно **сверить нашу модель (14 ₽ за контракт на сторону) с фактом**
   на реальном счёте, а не принимать на веру;
⬜ **`VarMargin`** — начисление и списание вариационной маржи. На срочном рынке
   деньги двигаются не только сделками: клиринг в 19:05 пересчитывает позиции
   (`01-special-info.md`), и без этих строк дневной результат не сойдётся
   с выпиской;
⬜ **`PayIn` / `PayOut`** — пополнение и вывод. Дневной лимит убытка считается
   от счёта **на утро** (требование `/risk`); если владелец счёта пополнил счёт
   среди дня, а лимит пересчитался — предохранитель испортился. Эти строки
   позволяют заметить такое, а не гадать.

⚠️ 3 RPS — это метод для разбора «раз в день», а не для опроса.
