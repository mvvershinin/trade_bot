"""Сроки токенов, права и предупреждение за 10 дней.

Числа — из документации БКС и `DOMAIN.md` §6: токен личного кабинета 90 суток,
рабочий 24 часа. Предупреждение за 10 дней — ТЗ §4.1 и пункт приёмки
`QUALITY.md` §4: «За 10 дней до конца срока токена показано предупреждение».
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from broker.secret import Secret
from broker.tokens import (
    ACCESS_LIFETIME,
    ACCESS_REFRESH_MARGIN,
    REFRESH_LIFETIME,
    WARN_BEFORE,
    AccessToken,
    NoticeLevel,
    RefreshToken,
    TokenScope,
)

FAKE = "FAKE-lifetime-0000-NOT-A-REAL-TOKEN"
ISSUED = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def token(**kwargs: object) -> RefreshToken:
    base = {
        "secret": Secret(FAKE),
        "scope": TokenScope.TRADE,
        "issued_at": ISSUED,
    }
    base.update(kwargs)
    return RefreshToken(**base)  # type: ignore[arg-type]


# --- документированные числа ---


def test_documented_lifetimes() -> None:
    """Если эти числа поедут, поедет и весь обратный отсчёт."""
    assert REFRESH_LIFETIME == timedelta(days=90)
    assert ACCESS_LIFETIME == timedelta(hours=24)
    assert WARN_BEFORE == timedelta(days=10)


def test_client_id_matches_the_documentation() -> None:
    """Тип токена передаётся брокеру полем `client_id` — значения из документации."""
    assert TokenScope.READ_ONLY.client_id == "trade-api-read"
    assert TokenScope.TRADE.client_id == "trade-api-write"
    assert TokenScope.TRADE.can_trade is True
    assert TokenScope.READ_ONLY.can_trade is False


# --- обратный отсчёт ---


def test_expiry_is_ninety_days_from_issue() -> None:
    assert token().expires_at == ISSUED + timedelta(days=90)


@pytest.mark.parametrize(
    ("days_before_expiry", "level"),
    [
        (90, NoticeLevel.CALM),
        (30, NoticeLevel.CALM),
        (11, NoticeLevel.CALM),
        (10, NoticeLevel.WARNING),
        (9, NoticeLevel.WARNING),
        (1, NoticeLevel.WARNING),
        (0, NoticeLevel.EXPIRED),
    ],
)
def test_warning_switches_on_at_ten_days(days_before_expiry: int, level: NoticeLevel) -> None:
    """Пункт приёмки: за 10 дней до конца срока предупреждение показано."""
    item = token()
    now = item.expires_at - timedelta(days=days_before_expiry)
    notice = item.notice(now)
    assert notice.level is level, f"за {days_before_expiry} дн.: {notice.text}"


def test_warning_text_is_for_a_human() -> None:
    """Текст читает владелец счёта: что случится и что делать."""
    item = token()
    notice = item.notice(item.expires_at - timedelta(days=7))
    assert notice.level is NoticeLevel.WARNING
    assert notice.urgent is True
    assert notice.days_left == 7
    assert "7 дней" in notice.text
    assert "робот остановится" in notice.text.lower()
    assert "личном кабинете" in notice.text
    assert "Токены API" in notice.text
    assert FAKE not in notice.text


def test_expired_text_says_the_robot_cannot_trade() -> None:
    item = token()
    notice = item.notice(item.expires_at + timedelta(hours=1))
    assert notice.level is NoticeLevel.EXPIRED
    assert notice.days_left == 0
    assert "истёк" in notice.text
    assert "не может" in notice.text


@pytest.mark.parametrize(
    ("days", "expected"),
    [(1, "1 день"), (2, "2 дня"), (4, "4 дня"), (5, "5 дней"), (11, "11 дней")],
)
def test_days_are_declined(days: int, expected: str) -> None:
    """«1 дней» в окне у владельца счёта читается как поломка программы."""
    item = token()
    notice = item.notice(item.expires_at - timedelta(days=days, seconds=1))
    assert expected in notice.text, notice.text


def test_calm_notice_still_says_the_date() -> None:
    """Обратный отсчёт виден всегда, а не только в последние десять дней."""
    item = token()
    notice = item.notice(ISSUED)
    assert notice.level is NoticeLevel.CALM
    assert notice.urgent is False
    assert "действует ещё" in notice.text


# --- два источника срока ---


def test_server_answer_shortens_the_countdown() -> None:
    """Сервер сказал, что срок кончится раньше, — верим ему.

    Ошибиться в сторону «предупредили раньше» — один зря показанный
    экран. Ошибиться в другую — робот встаёт посреди торгов.
    """
    early = ISSUED + timedelta(days=40)
    item = token(reported_expires_at=early)
    assert item.expires_at == early
    assert item.sources_disagree is True


def test_server_answer_does_not_extend_the_countdown() -> None:
    """Обратное направление не работает: продлевать срок сервер не может."""
    late = ISSUED + timedelta(days=200)
    item = token(reported_expires_at=late)
    assert item.expires_at == ISSUED + REFRESH_LIFETIME
    assert item.sources_disagree is True


def test_matching_sources_do_not_disagree() -> None:
    item = token(reported_expires_at=ISSUED + timedelta(days=90, hours=2))
    assert item.sources_disagree is False


# --- рабочий токен ---


def test_access_token_is_refreshed_before_it_dies() -> None:
    """Обновление заранее: пауза в потоке посреди торгового окна недопустима."""
    issued = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)
    access = AccessToken(
        secret=Secret(FAKE), expires_at=issued + ACCESS_LIFETIME, scope=TokenScope.TRADE
    )
    assert access.stale(issued) is False
    assert access.stale(access.expires_at - ACCESS_REFRESH_MARGIN - timedelta(minutes=1)) is False
    assert access.stale(access.expires_at - ACCESS_REFRESH_MARGIN) is True
    assert access.expired(access.expires_at - timedelta(minutes=1)) is False
    assert access.expired(access.expires_at) is True


def test_refresh_margin_covers_the_trading_window() -> None:
    """Запас обязан быть не меньше торгового окна 10:05–11:00 по умолчанию."""
    assert ACCESS_REFRESH_MARGIN >= timedelta(minutes=30)
