# Авторизация

URL: https://trade-api.bcs.ru/http/authorization

```
POST
## /trade-api-keycloak/realms/tradeapi/protocol/openid-connect/token

```

Сервис предназначен для предоставления клиенту access-токена. Перед использованием необходимо получить refresh-токен в веб-версии БКС Мир инвестиций. [Инструкция по получению](/#%D1%81-%D1%87%D0%B5%D0%B3%D0%BE-%D0%BD%D0%B0%D1%87%D0%B0%D1%82%D1%8C)

## Request

- application/x-www-form-urlencoded

- Body
- Example (auto)

### Body **required**

**client_id** stringrequired Идентификатор типа токена
`trade-api-read` — токен для чтения
`trade-api-write` – токен для торговли

**Possible values:** [ `trade-api-read` , `trade-api-write` ]

**refresh_token** stringrequired Refresh-токен, полученный в веб-версии.

**grant_type** stringrequired Тип grant. Для обновления токена — `refresh_token` .

**Possible values:** [ `refresh_token` ]

```json
{
  "client_id": "trade-api-read",
  "refresh_token": "string",
  "grant_type": "refresh_token"
}
```

## Responses

- 200
- 400

OK

- application/json

- Schema
- Example (auto)

**Schema**

**access_token** string Токен с запрошенными правами

**expires_in** number Время жизни токена в секундах (всего 24 часа)

**refresh_expires_in** number Время жизни refresh-токена (всего 90 суток)

**refresh_token** string Параметр обновления токена. Используется для обновления пары токенов access/refresh

**token_type** string Тип токена, определяющий способ его использования. Всегда принимает значение «bearer»

**not-before-policy** string Дата, с которой начнут действовать токены системы (по UNIX-времени). По умолчанию 0 (т.е. сразу)

**session_state** string GUID сессии

**scope** string Права доступа согласно переданному в запросе токену

```json
{
  "access_token": "string",
  "expires_in": 0,
  "refresh_expires_in": 0,
  "refresh_token": "string",
  "token_type": "string",
  "not-before-policy": "string",
  "session_state": "string",
  "scope": "string"
}
```

Invalid grant

- application/json

- Schema
- Example (auto)

**Schema**

**error** string Тип ошибки

**Possible values:** [ `invalid_grant` ]

**error_description** string Описание ошибки

```json
{
  "error": "invalid_grant",
  "error_description": "string"
}
```

---

## Проверено живым запросом 04.09.2026

Обмен работает ровно как написано. Ответ на настоящем токене чтения:
`expires_in=86400`, `refresh_expires_in=7775555`, `scope=openid`,
длина `access_token` 1247 символов.
