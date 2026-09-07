# Получить статусы торговли

URL: https://trade-api.bcs.ru/http/information/get-trading-status

```
GET
## https://be.broker.ru/trade-api-information-service /api/v1/trading-schedule/status
```

## Request — Query Parameters

**classCode** string **required** `non-empty` — Код класса ценной бумаги. **Example:** `TQBR`

## Responses — 200 OK

**tradingSessionTypeId** int32 ID сессии — ⚠️ в PDF `tradingSessionTypeID`,
разница только в регистре (`parsing.pick` ищет без учёта регистра)
**tradingSessionType** string Название текущей сессии
**tradingSessionStatus** string Статус сессии — [ `OPEN` , `CLOSE` ]
**nextSessionDate** date-time Дата и время следующего изменения статуса

⚠️ **Пример в PDF — `2021-01-19T09:41:03`, без `Z` и без смещения**, пример
на сайте — `2024-07-29T15:51:28.071Z`. Момент без пояса `broker/schedule.py`
**отбрасывает**, а не толкует: истолкованный как местный, он сдвинул бы срок
на три часа молча. Потеря не стоит ничего — срок жизни ответа всё равно
ограничен сверху.

```json
{
  "tradingSessionTypeId": 0,
  "tradingSessionType": "string",
  "tradingSessionStatus": "OPEN",
  "nextSessionDate": "2024-07-29T15:51:28.071Z"
}
```

## 429 — общая форма отказа, см. `03-limits.md`

⚠️ PDF называет у этого метода ещё **404 NOT_FOUND** «Данные не найдены»,
которого на странице сайта нет. Значит 404 здесь двузначен так же, как
у свечей, и адрес стоит в `session.ADDRESS_SENSITIVE`.

Base URL `https://be.broker.ru/trade-api-information-service`, Bearer JWT.

---

## Зачем это нам

Дешевле, чем полное расписание (`05-daily-schedule.md`): один запрос отвечает
«идут торги сейчас или нет» и **когда состояние изменится** — `nextSessionDate`.

`nextSessionDate` снимает необходимость опрашивать статус по кругу: программа
знает, до какого момента можно не спрашивать. Это прямо относится к лимиту
частоты запросов (`RESOURCE_EXHAUSTED`).

**Что из этого сделано 05.09.2026** (`broker/schedule.py`):

⬜ ответ живёт в памяти до `nextSessionDate`, но **не дольше пяти минут
   и не короче пятнадцати секунд**. Потолок обязателен: момент, ошибочно
   отнесённый вперёд (в том числе из-за неверно понятой зоны), стоил бы
   пропущенного открытия торгов. Пол обязателен по обратной причине;
⬜ `OPEN`/`CLOSE` — да/нет; **любое другое слово даёт «не знаем», а не
   «закрыто»**: остановить торговлю по непонятому полю чужого сервиса нельзя;
⬜ отказ метода означает «не знаем» и не поднимается наружу — программа
   ведёт себя так, как вела себя без расписания.
