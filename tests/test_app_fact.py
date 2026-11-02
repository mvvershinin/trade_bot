"""День из базы → значение движка (`app/fact.py::day_record`): перекладка полная.

Решение 0066, Р7: перезапуск в день с тейком не снимает «стоп после тейка».
Значение дня движку собирается перекладкой `market.TradingDay` →
`engine.DayRecord` по таблице (`DAY_FIELDS`). Поле, забытое в таблице, —
это движок, получивший после перезапуска пустую дату тейка: молча, без
единой ошибки. Стережёт это не текст таблицы, а значения: день со всеми
полями, отличными от умолчаний, обязан доехать до движка целиком.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime

from app.fact import DAY_FIELDS, DAY_LEFT_OUT, FUNDS_FIELDS, day_record
from engine import AccountFunds, DayRecord
from market import MSK, OpeningFunds, TradingDay

#: День, у которого каждое поле отлично от умолчания — и от соседних.
OPENING = OpeningFunds(
    account="FAKE-ACCOUNT-1",
    at=datetime(2026, 6, 19, 9, 1, tzinfo=MSK),
    equity=1_000_000.0,
    free=640_000.0,
    margin_per_contract=31_250.0,
)
TRADING = TradingDay(
    account="FAKE-ACCOUNT-1",
    day=date(2026, 6, 19),
    opening=OPENING,
    gross=1_275.0,
    commission=28.0,
    unknown_commissions=2,
    take_profit_date=date(2026, 6, 19),
    last_exit_date=date(2026, 6, 18),
    foreign=("FAKE-EXEC-7", "FAKE-EXEC-9"),
)


def test_every_field_of_the_day_reaches_the_engine() -> None:
    """Значения, а не имена: каждое поле дня — в своём поле значения движка."""
    record = day_record(TRADING)
    assert record == DayRecord(
        day=date(2026, 6, 19),
        opening=AccountFunds(
            at=datetime(2026, 6, 19, 9, 1, tzinfo=MSK),
            equity=1_000_000.0,
            free=640_000.0,
            margin_per_contract=31_250.0,
        ),
        realized=1_247.0,
        take_profit_date=date(2026, 6, 19),
        last_exit_date=date(2026, 6, 18),
        unknown_commissions=2,
        foreign=("FAKE-EXEC-7", "FAKE-EXEC-9"),
    )
    defaults = DayRecord(day=record.day)
    missed = [
        field.name for field in dataclasses.fields(DayRecord)
        if field.name != "day" and getattr(record, field.name) == getattr(defaults, field.name)
    ]
    assert not missed, f"до движка не доехали поля дня: {missed}"


def test_the_day_table_covers_both_classes() -> None:
    """Полнота таблицы: новое поле дня — либо в перекладке, либо названо с причиной.

    Новое поле `DayRecord` без строки таблицы — отказ сборки значения;
    новое поле `TradingDay` — молча потерянный факт дня. Оба ловятся здесь,
    до первого перезапуска.
    """
    record = {field.name for field in dataclasses.fields(DayRecord)}
    trading = {field.name for field in dataclasses.fields(TradingDay)}
    assert set(DAY_FIELDS) == record
    assert trading <= set(DAY_FIELDS) | set(DAY_LEFT_OUT), trading - set(DAY_FIELDS)
    assert not set(DAY_LEFT_OUT) & set(DAY_FIELDS)
    funds = {field.name for field in dataclasses.fields(AccountFunds)}
    opening = {field.name for field in dataclasses.fields(OpeningFunds)}
    assert set(FUNDS_FIELDS) == funds
    assert opening == funds | {"account"}


def test_a_day_without_a_morning_snapshot_gives_no_opening() -> None:
    """Снимка счёта за дату нет — у движка его нет тоже, а не нулевой счёт."""
    record = day_record(dataclasses.replace(TRADING, opening=None))
    assert record.opening is None
