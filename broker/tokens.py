"""Модель токенов БКС: сроки, права, обратный отсчёт, предупреждение за 10 дней.

Факты — из официальной документации БКС Trade API (раздел «Схема авторизации.
Токены refresh и access», прочитан 30.08.2026) и из `DOMAIN.md` §6:

* токен из личного кабинета (**refresh**) живёт **90 суток**;
* рабочий токен (**access**) живёт **24 часа**, программа обновляет его сама;
* токен бывает «только для чтения» и «для торговли и чтения данных»;
* тип задаётся при обмене полем `client_id`: `trade-api-read` либо
  `trade-api-write`;
* токен показывается один раз и привязан к одному брокерскому счёту.

Предупреждение за **10 дней** — требование ТЗ §4.1 и пункт приёмки
(`QUALITY.md` §4): «За 10 дней до конца срока токена показано предупреждение».
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final

from broker.secret import Secret

#: Срок токена личного кабинета. Документация БКС: «Refresh-токен имеет срок
#: жизни 90 суток».
REFRESH_LIFETIME: Final[timedelta] = timedelta(days=90)

#: Срок рабочего токена. Документация БКС: «Access-токен имеет срок жизни
#: 24 часа, затем его необходимо получить заново».
ACCESS_LIFETIME: Final[timedelta] = timedelta(hours=24)

#: За сколько предупреждать о конце срока токена личного кабинета. ТЗ §4.1.
WARN_BEFORE: Final[timedelta] = timedelta(days=10)

#: За сколько до конца срока обновлять рабочий токен.
#:
#: Полчаса, а не минута. Требование роли: «обновление рабочего токена не должно
#: приводить к паузе в потоке котировок посреди торгового окна». Торговое окно
#: по умолчанию — 10:05–11:00, то есть 55 минут; запас в полчаса даёт обмену
#: случиться заведомо вне окна при любом моменте выпуска токена, а при неудаче
#: остаётся ещё несколько попыток до того, как рабочий токен станет негодным.
ACCESS_REFRESH_MARGIN: Final[timedelta] = timedelta(minutes=30)


def now_utc() -> datetime:
    """Текущее время с зоной. Часы вынесены в функцию ради тестов."""
    return datetime.now(timezone.utc)


class TokenScope(enum.Enum):
    """Права токена личного кабинета."""

    READ_ONLY = "trade-api-read"
    TRADE = "trade-api-write"

    @property
    def client_id(self) -> str:
        """Значение поля `client_id` в запросе авторизации."""
        return self.value

    @property
    def can_trade(self) -> bool:
        return self is TokenScope.TRADE

    @property
    def human(self) -> str:
        if self is TokenScope.TRADE:
            return "для торговли и чтения данных"
        return "только для чтения"


class NoticeLevel(enum.Enum):
    """Насколько срочно дело с токеном."""

    CALM = "спокойно"
    WARNING = "предупреждение"
    EXPIRED = "истёк"


@dataclass(frozen=True, slots=True)
class TokenNotice:
    """Что сказать владельцу счёта о сроке токена."""

    level: NoticeLevel
    days_left: int
    expires_at: datetime
    text: str

    @property
    def urgent(self) -> bool:
        return self.level is not NoticeLevel.CALM


def _ru_days(count: int) -> str:
    """«1 день», «2 дня», «5 дней» — иначе текст в окне читается как машинный."""
    tail = count % 100
    if 11 <= tail <= 14:
        return f"{count} дней"
    match count % 10:
        case 1:
            return f"{count} день"
        case 2 | 3 | 4:
            return f"{count} дня"
        case _:
            return f"{count} дней"


@dataclass(frozen=True, slots=True)
class RefreshToken:
    """Токен личного кабинета: значение, права и срок.

    Про два срока. `declared_expires_at` считается от даты выпуска, которую
    называет владелец счёта (по умолчанию — момент сохранения файла).
    `reported_expires_at` приходит от сервера полем `refresh_expires_in`
    при каждом обмене.

    Действующим считается **более ранний** из двух. Направление выбрано
    сознательно: ошибиться в сторону «предупредили раньше» — значит один раз
    зря показать напоминание, ошибиться в другую — значит промолчать до дня,
    когда робот встанет посреди торгов. Расхождение больше суток само по себе
    достойно строки в журнале: оно означает, что дата выпуска указана неверно
    либо сервер считает срок иначе, чем документация.
    """

    secret: Secret
    scope: TokenScope
    issued_at: datetime
    reported_expires_at: datetime | None = None

    @property
    def declared_expires_at(self) -> datetime:
        return self.issued_at + REFRESH_LIFETIME

    @property
    def expires_at(self) -> datetime:
        if self.reported_expires_at is None:
            return self.declared_expires_at
        return min(self.declared_expires_at, self.reported_expires_at)

    @property
    def sources_disagree(self) -> bool:
        """Дата выпуска и ответ сервера расходятся больше чем на сутки."""
        if self.reported_expires_at is None:
            return False
        return abs(self.declared_expires_at - self.reported_expires_at) > timedelta(days=1)

    def expired(self, now: datetime) -> bool:
        return now >= self.expires_at

    def time_left(self, now: datetime) -> timedelta:
        return self.expires_at - now

    def notice(self, now: datetime) -> TokenNotice:
        """Что показать в окне и записать в журнал про срок токена."""
        left = self.time_left(now)
        when = self.expires_at.astimezone().strftime("%d.%m.%Y")

        if left <= timedelta(0):
            return TokenNotice(
                level=NoticeLevel.EXPIRED,
                days_left=0,
                expires_at=self.expires_at,
                text=(
                    f"Срок токена брокера истёк {when}. Робот торговать не может: "
                    "брокер не примет ни один запрос. Выпустите новый токен "
                    "в личном кабинете БКС — Профиль, ваш счёт, «Токены API» — "
                    "и вставьте его в настройках программы."
                ),
            )

        # Дней осталось — с округлением вниз: «осталось 10 дней» при 10 днях
        # и 5 часах честнее, чем «11». Предупреждение включается на десятом дне.
        days = left.days
        if left <= WARN_BEFORE:
            return TokenNotice(
                level=NoticeLevel.WARNING,
                days_left=days,
                expires_at=self.expires_at,
                text=(
                    f"Токен брокера действует ещё {_ru_days(days)} — до {when}. "
                    "Когда срок кончится, робот остановится: брокер перестанет "
                    "отвечать. Выпустите новый токен в личном кабинете БКС — "
                    "Профиль, ваш счёт, «Токены API» — и вставьте его "
                    "в настройках программы."
                ),
            )

        return TokenNotice(
            level=NoticeLevel.CALM,
            days_left=days,
            expires_at=self.expires_at,
            text=f"Токен брокера действует ещё {_ru_days(days)} — до {when}.",
        )


@dataclass(frozen=True, slots=True)
class AccessToken:
    """Рабочий токен: сутки жизни, обновляется программой без участия человека."""

    secret: Secret
    expires_at: datetime
    scope: TokenScope
    #: Строка `scope` из ответа сервера. Документация обязательность поля
    #: заявляет, но состав значений не описывает — поэтому строка хранится
    #: как есть, для журнала, и ни на какое решение не влияет.
    scope_reported: str | None = None

    def expired(self, now: datetime) -> bool:
        return now >= self.expires_at

    def stale(self, now: datetime, margin: timedelta = ACCESS_REFRESH_MARGIN) -> bool:
        """Пора обновлять: срок кончится раньше, чем через запас."""
        return now >= self.expires_at - margin
