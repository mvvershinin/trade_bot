# Получить текущие котировки

URL: https://trade-api.bcs.ru/http/market-data/get-quotes

```
POST
## https://be.broker.ru/trade-api-market-data-connector /api/v1/quotes
```

## Request — Body, required

**instruments** object[] **required** Список инструментов, `>= 1`, `<= 100`
* **ticker** string **required** `non-empty`. **Example:** `SBER`
* **classCode** string **required** `non-empty`. **Example:** `TQBR`

```json
{ "instruments": [ { "ticker": "SBER", "classCode": "TQBR" } ] }
```

## Responses — 200 OK

**records** object[]
* **ticker** string · **classCode** string
* **dateTime** date-time **Время обновления данных (UTC)**
* **securityTradingStatus** int32 **Статус торговли инструментом**
  `2` — Торги приостановлены
  `17` — Торги открыты
  `18` — Торги закрыты
  `100` — Закрытие торгов
  `101` — Открытие торгов
  `102` — Аукцион
  `103` — Аукцион закрытия
  `104` — Дискретный аукцион
  **Possible values:** [ `0` , `2` , `17` , `18` , `100` , `101` , `102` , `103` , `104` ]
* **currency** string Валюта котировки
* **bid** number Лучшая цена покупки
* **offer** number Лучшая цена продажи
* **open** number Цена открытия торговой сессии
* **close** number Цена закрытия предыдущей сессии
* **high** number Максимальная цена за день
* **low** number Минимальная цена за день
* **theoreticalPrice** number Цена расчетная (у опциона)
* **last** number Последняя цена сделки
* **bidYield** / **offerYield** number Доходность котировок (для облигаций)
* **change** number Изменение цены за текущую сессию, в валюте цены
* **changeRate** number Изменение цены за текущую сессию, в %

## 400 / 429 — общая форма отказа, см. `03-limits.md`

Base URL `https://be.broker.ru/trade-api-market-data-connector`, Bearer JWT.

---

## Зачем это нам — третий способ понять состояние рынка

`securityTradingStatus` — **машинный признак по конкретному инструменту**, в отличие
от расписания (`05-daily-schedule.md`) и статуса класса (`06-trading-status.md`),
которые говорят про класс целиком.

Прямое применение к задаче «тишина в потоке ≠ обрыв связи»:

| что видим | что это значит |
|---|---|
| `17` Торги открыты, а свечи не идут | подозрение на обрыв — проверять живость соединения |
| `2` Торги приостановлены | молчание законно, робот молчит и пишет причину в журнал |
| `18` Торги закрыты | то же |
| `102` / `103` / `104` аукцион | цены аукциона не годятся для решений |

⚠️ Это **HTTP-запрос**, то есть он тратит лимит частоты. Опрашивать его в цикле
вместо признака живости соединения — плохой способ; годится как разовая проверка
при подозрении.
