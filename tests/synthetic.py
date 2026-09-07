"""Синтетические данные для проверки окна: свечи, метки, журналы, итог.

Данные придуманные и детерминированные — генератор с фиксированным зерном.
В сеть тесты не ходят, к базе не обращаются, `reference/` не читают.

Важно, чем это **не** является: правилами торговли. Здесь нет входов, выходов
и границ окна — есть заранее записанные метки и строки журнала, изображающие
то, что в готовой программе присылает `engine/`. Это подставные данные, а не
вторая реализация стратегии.
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import datetime, timedelta

from ui.formatting import MSK
from ui.models import (
    Candle,
    ChartData,
    DecisionLevel,
    DecisionRow,
    Layer,
    LevelKind,
    LinePoint,
    Marker,
    MarkerKind,
    Position,
    PriceLevel,
    RobotState,
    Shade,
    ShadeKind,
    Side,
    TakeGuard,
    TradePath,
    TradeRow,
    TradesSummary,
    Connection,
    Mode,
)

DAY = datetime(2026, 6, 19, 9, 30, tzinfo=MSK)
STEP = timedelta(minutes=5)


def candles(count: int = 60, start: datetime = DAY, seed: int = 20260619) -> list[Candle]:
    """Случайное блуждание с фиксированным зерном — одинаковое от запуска к запуску."""
    rng = random.Random(seed)
    price = 285_000.0
    result: list[Candle] = []
    for index in range(count):
        drift = rng.uniform(-260, 260) + (120 if 6 <= index <= 16 else -40)
        open_ = price
        close = price + drift
        high = max(open_, close) + rng.uniform(20, 200)
        low = min(open_, close) - rng.uniform(20, 200)
        result.append(Candle(
            opens_at=start + STEP * index,  # время ОТКРЫТИЯ свечи — как на оси
            open=round(open_), high=round(high), low=round(low), close=round(close),
            volume=rng.uniform(100, 900),
        ))
        price = close
    return result


def average(source: list[Candle], period: int = 15) -> list[LinePoint]:
    """Линия средней для картинки.

    Считается здесь, потому что в готовой программе её присылает стратегия,
    а тесту нужно чем-то заполнить график. Никакого отношения к тому, как
    средняя считается в `strategies/`, эта функция не имеет и иметь не должна.
    """
    points: list[LinePoint] = []
    k = 2 / (period + 1)
    value = source[0].close
    for candle in source:
        value = candle.close * k + value * (1 - k)
        points.append(LinePoint(time=candle.opens_at, value=value))
    return points


def chart_data(count: int = 60) -> ChartData:
    bars = candles(count)
    line = average(bars)
    # Не `def`: короткая выборка с зажимом индекса, нужная только ниже по телу.
    pick = lambda index: bars[min(index, len(bars) - 1)]  # noqa: E731
    entry = pick(8)
    turn = pick(11)
    exit_ = pick(16)
    stray = pick(24)
    missed = pick(30)

    markers = [
        Marker(entry.opens_at, entry.low, MarkerKind.ENTRY_LONG, Layer.FACT,
               "Лонг 1", "Факт: вход в лонг по 285 400\nРасчёт: 285 380\nРасхождение: 20 п. (20 ₽)",
               pair_id="t1"),
        Marker(entry.opens_at, entry.low, MarkerKind.ENTRY_LONG, Layer.PLAN,
               "Расчёт", "Расчёт: вход в лонг по 285 380", pair_id="t1"),
        Marker(turn.opens_at, turn.close, MarkerKind.REVERSAL, Layer.FACT,
               "Переворот", "Позиция не закрылась, а сменила сторону: лонг → шорт",
               pair_id="t2"),
        Marker(exit_.opens_at, exit_.high, MarkerKind.EXIT, Layer.FACT,
               "Тейк", "Тейк-профит сработал: +0,50%", pair_id="t3"),
        Marker(exit_.opens_at, exit_.high, MarkerKind.EXIT, Layer.PLAN,
               "Расчёт", "Расчётный выход по тейку", pair_id="t3"),
        Marker(stray.opens_at, stray.high, MarkerKind.UNPLANNED, Layer.FACT,
               "Вне расчёта", "Сделка, которой алгоритм не предполагал. Разбирается как дефект"),
        Marker(missed.opens_at, missed.low, MarkerKind.MISSED, Layer.PLAN,
               "Пропущен", "Сигнал был, сделки нет: не было связи с брокером"),
    ]
    paths = [
        TradePath(entry.opens_at, entry.close, turn.opens_at, turn.close, Side.LONG, Layer.FACT, True),
        TradePath(turn.opens_at, turn.close, exit_.opens_at, exit_.close, Side.SHORT, Layer.FACT, False),
    ]
    levels = [
        PriceLevel(exit_.close, LevelKind.ENTRY, "Цена входа"),
        PriceLevel(exit_.close * 1.005, LevelKind.TAKE, "Тейк +0,5%"),
        PriceLevel(exit_.close * 1.003, LevelKind.TRAILING, "Скользящий тейк"),
    ]
    shades = [
        Shade(bars[0].opens_at - STEP, pick(6).opens_at, ShadeKind.OUTSIDE_WINDOW, "Вне окна"),
        Shade(pick(17).opens_at, bars[-1].opens_at + STEP, ShadeKind.OUTSIDE_WINDOW, "Вне окна"),
    ]
    return ChartData(
        instrument="MXU6",
        timeframe="5 минут",
        candles=tuple(bars),
        average=tuple(line),
        average_label="EMA(15)",
        markers=tuple(markers),
        paths=tuple(paths),
        levels=tuple(levels),
        shades=tuple(shades),
    )


def trades() -> list[TradeRow]:
    bars = candles()
    return [
        TradeRow(bars[8].opens_at, bars[11].opens_at, Side.LONG, 1, bars[8].close, bars[11].close,
                 "Обратный сигнал средней — переворот", 1180.0, 0.41, 28.0, "1"),
        TradeRow(bars[11].opens_at, bars[16].opens_at, Side.SHORT, 1, bars[11].close, bars[16].close,
                 "Тейк-профит +0,5%", 1425.0, 0.50, 28.0, "2"),
        TradeRow(bars[18].opens_at, bars[22].opens_at, Side.LONG, 1, bars[18].close, bars[22].close,
                 "Конец торгового окна — закрытие в деньги", -640.0, -0.22, 28.0, "3"),
    ]


def summary() -> TradesSummary:
    # Профит-фактор — из тех же трёх строк, чистыми:
    # (1180 − 28 + 1425 − 28) / (640 + 28).
    return TradesSummary(
        trades=3, profitable_share=2 / 3, net_profit_rub=1881.0,
        gross_profit_rub=1965.0, commission_rub=84.0, max_drawdown_rub=-640.0, reversals=1,
        profit_factor=2549 / 668,
    )


def decisions() -> list[DecisionRow]:
    base = DAY.replace(hour=10, minute=5)
    rows = [
        (base, "Торговое окно", "Начало торгового окна. Позиции нет, ждём первого закрытия свечи", DecisionLevel.INFO),
        (base + timedelta(minutes=5), "Сигнал", "Закрытие выше средней EMA(15) → покупка 1 контракта", DecisionLevel.TRADE),
        (base + timedelta(minutes=5), "Заявка", "Заявка по рынку отправлена и исполнена. Комиссия 14 ₽ учтена", DecisionLevel.TRADE),
        (base + timedelta(minutes=5), "Сверка", "Расчётная цена входа 285 380, фактическая 285 400: расхождение 20 пунктов (20 ₽)", DecisionLevel.INFO),
        (base + timedelta(minutes=5), "Тейк-профит", "Выставлена цель прибыли +0,50% от цены входа", DecisionLevel.INFO),
        (base + timedelta(minutes=20), "Переворот", "Обратный сигнал средней: лонг закрыт, открыт шорт — рынок не покидали", DecisionLevel.TRADE),
        (base + timedelta(minutes=45), "Тейк-профит", "Цель прибыли достигнута: +1 425 ₽ чистыми, комиссия 28 ₽ учтена", DecisionLevel.TRADE),
        (base + timedelta(minutes=45), "Правило дня", "Сделок сегодня больше не будет: включено правило «стоп на день после тейка»", DecisionLevel.INFO),
        (base + timedelta(minutes=52), "Связь", "Связь с брокером потеряна. Ждём восстановления, заявки не подаются", DecisionLevel.WARNING),
        (base + timedelta(minutes=54), "Связь", "Связь восстановлена. Сверка позиции: у брокера шорт 1, у робота шорт 1 — расхождений нет", DecisionLevel.INFO),
        (base + timedelta(minutes=58), "Отказ брокера", "Брокер отклонил заявку: недостаточно свободных средств для гарантийного обеспечения", DecisionLevel.ERROR),
        (base + timedelta(minutes=59), "Дневной лимит", "Достигнут дневной лимит убытка. Счёт на утро 1 000 000 ₽, убыток 20 400 ₽, порог 2,0% (20 000 ₽). Робот остановлен", DecisionLevel.ERROR),
        (base + timedelta(minutes=55), "Торговое окно", "Конец торгового окна. Позиции нет", DecisionLevel.INFO),
    ]
    return [DecisionRow(time=t, event=e, reason=r, level=lvl) for t, e, r, lvl in rows]


def state(**changes: object) -> RobotState:
    bars = candles()
    base = RobotState(
        instrument="MXU6",
        timeframe="5 минут",
        connection=Connection.ONLINE,
        mode=Mode.REVERSE,
        running=True,
        simulation=True,
        # ⚠️ `take_trailing=False` задан явно, хотя это и значение по смыслу
        # умолчательное: у поля умолчание — `None`, «движок не сказал».
        # Фикстура изображает заполненное состояние, а не забытое; забытое
        # проверяется отдельно в `test_ui_notices.py`.
        #
        # ⚠️ Уровень тейка — просто правдоподобное число для картинки, а не
        # расчёт. Считает его движок (решение 0008): формула здесь была бы
        # второй, ничем не подтверждённой реализацией правила про деньги.
        # Умолчание — «уровень выставлен у брокера»: это то состояние,
        # в котором позиция живёт большую часть времени, и именно про него
        # окно раньше говорило неправду.
        position=Position(
            Side.SHORT, 1, bars[16].close, bars[16].opens_at, -240.0,
            take=TakeGuard.ARMED, take_level=284_400.0, take_trailing=False,
        ),
        day_profit_rub=1881.0,
        day_profit_pct=0.19,
        day_commission_rub=84.0,
        day_trades=3,
        token_read_only=False,
        token_days_left=6,
    )
    return replace(base, **changes) if changes else base
