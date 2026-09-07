# Маржинальные показатели (WebSocket)

URL: https://trade-api.bcs.ru/websocket/marginal-indicators

WebSocket API позволяет получать **маржинальные показатели портфеля**, включая ГО,
вармаржу, уровень риска, задолженности и устойчивость счёта в реальном времени.

## 🔌 WebSocket URL

```text
wss://ws.broker.ru/trade-api-bff-marginal-indicators/api/v1/marginal-indicators/ws
```

Аутентификация — заголовок HTTP при подключении:

```text
Authorization: Bearer <ACCESS_TOKEN>
```

Подписки нет.

## 📥 Формат ответа

Все данные приходят **в одном объекте**: `agreementData`, `portfolioCurrentValue`,
`portfolioStability`, `fortsStability`, `marginParameters`, `cashByCurrency`,
`moneyDebt`, `marginSecurities`, `futuresParameters`.

### `agreementData` — данные о брокерском счёте
`number` номер счёта · `isIia` ИИС · `displayName` · `subaccountId` · `color` HEX‑код

### `portfolioCurrentValue` — стоимость портфеля
`currentValueRub` текущая стоимость · `dailyPortfolioChangeRub` дневное изменение (₽) ·
`dailyPortfolioChangePerc` дневное изменение (%) · `totalPortfolioChangeRub` ·
`totalPortfolioChangePerc` · **`collateralAvailable` доступное обеспечение** ·
`varMargin` вариационная маржа

### `portfolioStability` — устойчивость портфеля
`isMarginOn` включена ли маржинальная торговля · `borrowedFunds` все задолженности (₽) ·
`stabilityStatusId` ID статуса (0–5) · `stabilityStatusName` текст статуса ·
`refillSum` сумма для выхода из маржин-колла

### `fortsStability` — устойчивость по ФОРТС
`ownFunds` собственные средства · **`freeCash` свободные средства** ·
`stabilityStatusId` · `stabilityStatusName` · `refillSum` сумма пополнения ·
**`fortsMarginCall` уровень маржин-колла** · **`fortsForcedClosure` уровень
принудительного закрытия** · **`riskLevel` уровень риска (0–4)**

### `marginParameters` — параметры маржинального портфеля
`marginValue` стоимость маржинальных активов · **`collateral` ГО** ·
`marginCall` уровень маржин-колла · `forcedClosure` уровень принудительного закрытия

### `cashByCurrency` — свободные деньги по валютам
`currency` · `sum`

### `moneyDebt` — задолженности по валютам
`totalMoneyDebt` · `debtByCurrency[].currency` · `debtByCurrency[].sum`

### `marginSecurities` — маржинальные позиции
`totalMarginSecurity` · `marginBySecurity[]`: `ticker`, `board`, `displayName`,
`logoLink` *(deprecated)*, `quantity`, `currentValue`, `currency`, `discount` (%)

### `futuresParameters` — фьючерсные позиции
`futuresValue` общая стоимость · `futuresPortfolio[]`: `ticker`, `board`,
`currentValue`, `balanceValue`, **`futuresCollateral` ГО по позиции**, `discount`,
`displayName`, `logoLink` *(deprecated)*

## 📋 Пример ответа (сокращён до сути)

```json
{
  "agreementData": { "number": "1234567/25", "isIia": false, "displayName": "Основной" },
  "portfolioCurrentValue": {
    "currentValueRub": 8086.59, "dailyPortfolioChangeRub": -6.38,
    "dailyPortfolioChangePerc": -1.07, "collateralAvailable": 5018.23, "varMargin": 0
  },
  "portfolioStability": {
    "isMarginOn": false, "borrowedFunds": 0,
    "stabilityStatusId": 5, "stabilityStatusName": "Максимальная устойчивость", "refillSum": 0
  },
  "fortsStability": {
    "ownFunds": 0, "freeCash": 0, "stabilityStatusId": 0, "stabilityStatusName": "",
    "refillSum": 0, "fortsMarginCall": 0, "fortsForcedClosure": 0, "riskLevel": 0,
    "optionsPremium": 0
  },
  "marginParameters": { "marginValue": 5136.59, "collateral": 0, "marginCall": 118.36, "forcedClosure": 59.18 },
  "cashByCurrency": [ { "currency": "RUB", "sum": 4544.79 } ],
  "moneyDebt": { "totalMoneyDebt": 0, "debtByCurrency": [] },
  "futuresParameters": { "futuresValue": 0, "futuresPortfolio": [] }
}
```

Ошибки: `401 UNAUTHORIZED`, `400 BAD_REQUEST`, `404 NOT_FOUND`, `5xx`.

---

## Зачем это нам — источник для двух предохранителей из трёх

Три предохранителя обязаны быть до первого боевого запуска (`CLAUDE.md`):
потолок объёма, дневной лимит убытка, проверка свободных средств.
**Два последних получают здесь источник данных в реальном времени, без опроса.**

⬜ **«Свободные средства видны ДО подачи заявки, а не из отказа брокера»** —
   `fortsStability.freeCash` и `portfolioCurrentValue.collateralAvailable`.
   Это и есть требование `/risk`, которое до сегодня нечем было закрыть;
⛔ **ГО под контракт до входа этот сервис НЕ даёт — правка 05.09.2026.**
   Здесь стояло, что `futuresParameters.futuresPortfolio[].futuresCollateral`
   закрывает проверку ГО перед входом. Это неверно, и на этой записи стоял
   долг `D-046`. Официальный PDF о контейнере `futuresPortfolio` говорит
   дословно: «Массив данных по инструментам срочного рынка **в портфеле**.
   При отсутствии позиций приходит **пустой массив**». То есть поле даёт ГО
   **позиции**, а не контракта, и замкнутый круг «чтобы узнать ГО, нужна
   позиция; чтобы открыть позицию, нужно ГО» не разрывает. `marginParameters.collateral`
   — ГО портфеля целиком, тоже не по контракту.
   **Откуда ГО берётся на самом деле:** биржевое `INITIALMARGIN` от MOEX ISS
   с надбавкой на требования брокера — решение
   [`0043`](../decisions/0043-margin-per-contract-before-the-first-entry.md),
   разбор всех проверенных полей — в докстринге `broker/margin.py`;
⬜ **Предохранитель «после принудительного закрытия брокером — остановка, а не
   восстановление позиции»** — `fortsForcedClosure`, `fortsMarginCall`, `riskLevel` (0–4),
   `stabilityStatusName`. Раньше этот пункт `/risk` было нечем реализовать вовсе;
⬜ **Дневной лимит убытка** — `portfolioCurrentValue.dailyPortfolioChangeRub`
   и `dailyPortfolioChangePerc`. ⚠️ Осторожно: открытый вопрос №3 — считать ли
   бумажный убыток открытой позиции — **не закрыт**. Это поле включает переоценку
   открытых позиций, то есть отвечает на один из двух вариантов, а какой нужен —
   решает владелец счёта. Взять поле молча = принять решение за него.

⚠️ Лимит: **2 одновременных соединения** на этот сервис (`29-restrictions.md`).
Вместе с «Лимиты» (2) и «Портфель» (2) и «Заявки» (4) — соединений в программе
будет несколько, и каждое надо закрывать при переподключении.
