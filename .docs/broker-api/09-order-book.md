# Получить данные по стакану

URL: https://trade-api.bcs.ru/http/market-data/get-order-book

```
GET
## https://be.broker.ru/trade-api-market-data-connector /api/v1/order-book
```

## Request — Query Parameters

**ticker** string **required** `non-empty` — Тикер. **Example:** `SBER`
**classCode** string **required** `non-empty` — Код класса. **Example:** `TQBR`
**depth** int32 `>= 1` и `<= 20` — Глубина стакана. **Default:** `20`

## Responses — 200 OK

**depth** int32 Глубина стакана
**dateTime** date-time Дата и время обновления стакана
**ticker** string · **classCode** string
**bidVolume** int32 Объем покупателей в шт.
**askVolume** int32 Объем продавцов в шт.
**bids** object[] Предложения покупки — **price** number, **quantity** int64 (в шт.)
**asks** object[] Предложения продажи — **price** number, **quantity** int64 (в шт.)

```json
{
  "depth": 0,
  "dateTime": "2024-07-29T15:51:28.071Z",
  "ticker": "string",
  "classCode": "string",
  "bidVolume": 0,
  "askVolume": 0,
  "bids": [ { "price": 0, "quantity": 0 } ],
  "asks": [ { "price": 0, "quantity": 0 } ]
}
```

## 400 / 429 — общая форма отказа, см. `03-limits.md`

Base URL `https://be.broker.ru/trade-api-market-data-connector`, Bearer JWT.

---

## Зачем это нам

Открытый вопрос №7 — «ликвидность и проскальзывание»: нужен ли автоматический
переход на лимитные заявки выше порога объёма. Стакан — единственный источник,
по которому это можно решить не на глаз: `bidVolume` / `askVolume` в штуках прямо
говорят, съест ли рыночная заявка нужного размера весь первый уровень.

Для этапа 1 не нужен: объём предполагается небольшой, заявки рыночные.
Понадобится, когда заказчик ответит на №7.
