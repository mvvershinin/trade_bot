"""Переводы между слоями: то, что ломается молча.

Три вещи проверяются здесь, и каждая уже стоила проекту времени или могла бы:

1. **Свеча не съезжает на бар.** Ось графика размечена **открытиями**
   (`opens_at`), движок считает по **закрытиям**. Проверяется равенство
   с точностью до минуты, а не «поле скопировалось», и проверяется, что
   на одну ось не попали два соглашения сразу.
2. **Настройки собираются поимённо.** Ни одно поле `EngineSettings` не остаётся
   без источника, и поля, которых в окне нет, не сбрасываются в умолчание.
3. **Разбор строк окна громкий.** Пустой инструмент, незнакомый размер свечи
   и невыразимый вариант поведения дают отказ с фразой, а не тихую подмену.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import date, datetime, time, timedelta

import pytest

from engine import EngineSettings, ExitReason, Fill, JournalEntry, JournalLevel, OrderAction
from engine import Mode as EngineMode
from engine import Side as EngineSide
from engine import DayMarks, TradingWindow
from engine import window as engine_window
from market import Candle, MSK, Timeframe
from strategies import AverageKind as StrategyAverageKind
from strategies import registry
from ui.models import (
    AfterTakeProfit,
    AverageKind,
    DecisionLevel,
    Layer,
    Mode,
    OnPriceEqualsAverage,
    ReversalMoment,
    Settings,
    Side,
)

from app import convert
from backtest import Deal, HistoryRun, Pair, Plan, Summary


def candle(hour: int, minute: int, minutes: int = 5) -> Candle:
    return Candle(
        time=datetime(2026, 6, 19, hour, minute, tzinfo=MSK),
        open=100.0, high=101.0, low=99.0, close=100.5, volume=7.0,
        timeframe=Timeframe(minutes), filled_minutes=minutes,
    )


# --------------------------------------------------------------------- свеча

def test_a_window_candle_is_stamped_with_the_start_of_its_interval() -> None:
    """`opens_at` — **начало** интервала, как в терминале брокера.

    Проверка от 04.09.2026 (B-008): в график уходило закрытие, и пятиминутка
    12:55–13:00 стояла у нас на 13:00, а у брокера на 12:55. Весь график
    вместе с метками сделок был сдвинут на бар вправо, и сверка глазами —
    то, ради чего график и делается, — была невозможна.
    """
    window = convert.window_candle(candle(12, 55))
    assert window.opens_at == datetime(2026, 6, 19, 12, 55, tzinfo=MSK), (
        "свеча подписана не началом интервала: весь график и все метки "
        f"сдвинуты на бар ({window.opens_at})"
    )
    assert (window.open, window.high, window.low, window.close, window.volume) == (
        100.0, 101.0, 99.0, 100.5, 7.0
    )


@pytest.mark.parametrize("minutes", [1, 5, 15, 30, 60])
def test_the_candle_label_does_not_depend_on_the_timeframe(minutes: int) -> None:
    """Отметка на оси — начало интервала при любом размере свечи.

    Прежняя редакция проверяла обратное: что расстояние до отметки равно
    таймфрейму. Именно эта проверка держала ошибку B-008 зелёной.
    """
    window = convert.window_candle(candle(10, 0, minutes))
    assert window.opens_at - datetime(2026, 6, 19, 10, 0, tzinfo=MSK) == timedelta(0)


def test_a_fill_lands_on_the_candle_of_execution_not_of_the_decision() -> None:
    """Сделка прототипа помечена **началом** свечи исполнения.

    Решение принято на закрытии свечи 10:05–10:10, то есть в 10:10; исполнено
    на открытии следующей, тоже в 10:10. На оси, размеченной открытиями,
    это разные свечи: 10:05 у решения и 10:10 у исполнения. Без перевода
    метка факта встала бы на ту же свечу, где стоит метка расчёта, и вопрос
    «факт правее по времени или нет» (DOMAIN.md §8) остался бы без ответа.
    """
    decision_close = datetime(2026, 6, 19, 10, 10, tzinfo=MSK)   # закрытие свечи решения
    fill_time = decision_close                                       # начало свечи исполнения
    plan_at = convert.chart_time(decision_close, Timeframe(5))
    fill_at = convert.bar_open(fill_time, Timeframe(5))
    assert plan_at == datetime(2026, 6, 19, 10, 5, tzinfo=MSK)
    assert fill_at == datetime(2026, 6, 19, 10, 10, tzinfo=MSK)
    assert fill_at - plan_at == timedelta(minutes=5), (
        "метки расчёта и факта встали на одну свечу — наложение слоёв "
        "перестало показывать разницу"
    )


def test_a_fill_inside_a_bar_lands_on_that_bars_start() -> None:
    """В бою время сделки — не граница бара, а момент внутри него."""
    moment = datetime(2026, 6, 19, 10, 11, 37, tzinfo=MSK)
    assert convert.bar_open(moment, Timeframe(5)) == datetime(
        2026, 6, 19, 10, 10, tzinfo=MSK
    )


def test_an_engine_moment_lands_on_the_bar_that_closed_at_it() -> None:
    """`chart_time` берёт бар, который этим моментом **закрылся**.

    Численно закрытие бара равно открытию следующего, и перепутать их
    местами — ошибка на один бар, которую видно только рядом с чужим
    терминалом. 10:10 — это закрытие свечи 10:05–10:10, значит её отметка.
    """
    assert convert.chart_time(
        datetime(2026, 6, 19, 10, 10, tzinfo=MSK), Timeframe(5)
    ) == datetime(2026, 6, 19, 10, 5, tzinfo=MSK)


def test_every_chart_series_shares_one_axis_convention() -> None:
    """Все ряды графика сдвинуты одинаково — иначе метки уедут со свечей.

    Проверка на цельность оси, а не на отдельный перевод: свеча, точка
    средней и метка расчёта, относящиеся к одному бару, обязаны получить
    одну и ту же отметку. Починить один ряд и забыть другой хуже, чем
    не чинить ничего: картинка останется цельной на вид.
    """
    frame = Timeframe(5)
    bar = candle(10, 5)                       # свеча 10:05–10:10
    closes_at = bar.time + frame.delta        # 10:10 — соглашение движка
    on_axis = convert.window_candle(bar).opens_at
    line = convert.average_line(((closes_at, 285_000.0),), frame)
    assert on_axis == datetime(2026, 6, 19, 10, 5, tzinfo=MSK)
    assert line[0].time == on_axis, "средняя стоит не на своей свече"
    assert convert.chart_time(closes_at, frame) == on_axis, (
        "метка расчёта стоит не на той свече, на которой принято решение"
    )


def test_the_markers_of_a_trade_stand_on_the_candles_of_the_chart() -> None:
    """Метки обоих слоёв встают на отметки свечей, и на свои.

    Проверка не на «перевод вызван», а на результат: решение принято
    на закрытии свечи 10:05–10:10, исполнено на открытии следующей.
    На оси, размеченной открытиями, это 10:05 у расчёта и 10:10 у факта —
    соседние свечи, и факт правее (DOMAIN.md §8).

    ⚠️ Проверять членством в наборе отметок здесь нельзя, и это ловушка,
    из-за которой дыра держалась: закрытие бара численно равно открытию
    следующего, поэтому непереведённая метка расчёта тоже попадает
    на законную отметку оси — просто на чужую. Ловится только расстоянием
    между парой.
    """
    frame = Timeframe(5)
    decided_at = datetime(2026, 6, 19, 10, 10, tzinfo=MSK)   # закрытие свечи 10:05
    fill_at = decided_at                                     # открытие свечи 10:10
    plan = Plan(
        order_id="open:long:тест", decided_at=decided_at, price=100.0,
        side=EngineSide.LONG, action=OrderAction.OPEN,
    )
    fill = Fill(
        action=OrderAction.OPEN, side=EngineSide.LONG, volume=1.0, price=100.5,
        at=fill_at, order_id="open:long:тест",
    )
    markers, _ = convert.markers_and_paths(
        HistoryRun(), (Pair("open:long:тест", plan, fill),), frozenset(), frame
    )
    plan_marker = next(m for m in markers if m.layer is Layer.PLAN)
    fact_marker = next(m for m in markers if m.layer is Layer.FACT)
    assert plan_marker.time == datetime(2026, 6, 19, 10, 5, tzinfo=MSK), (
        f"метка расчёта уехала со своей свечи: {plan_marker.time}"
    )
    assert fact_marker.time == datetime(2026, 6, 19, 10, 10, tzinfo=MSK), (
        f"метка факта уехала со своей свечи: {fact_marker.time}"
    )
    assert fact_marker.time - plan_marker.time == frame.delta, (
        "расчёт и факт встали не на соседние свечи — наложение слоёв "
        "перестало отвечать на вопрос «факт правее по времени или нет»"
    )


def test_the_trade_paths_of_both_layers_are_drawn_on_the_axis_of_the_candles() -> None:
    """Линии «вход → выход» обоих слоёв размечены той же осью, что свечи.

    Слой факта берёт время сделки — момент внутри бара; слой прогноза берёт
    время решения — закрытие бара. Соглашения разные, ось одна, и оба конца
    каждой линии обязаны стоять на отметках свечей.
    """
    frame = Timeframe(5)
    entry_decided = datetime(2026, 6, 19, 10, 10, tzinfo=MSK)   # закрытие свечи 10:05
    exit_decided = datetime(2026, 6, 19, 10, 25, tzinfo=MSK)    # закрытие свечи 10:20
    deal = Deal(
        side=EngineSide.LONG, volume=1.0,
        entry_time=entry_decided,                               # открытие свечи 10:10
        entry_price=100.0, entry_order_id="open:long:тест",
        exit_time=datetime(2026, 6, 19, 10, 27, 41, tzinfo=MSK),  # внутри бара 10:25
        exit_price=101.0, exit_order_id="close:long:тест",
        exit_reason=ExitReason.SIGNAL,
    )
    plans = (
        Pair("open:long:тест", Plan(
            order_id="open:long:тест", decided_at=entry_decided, price=100.0,
            side=EngineSide.LONG, action=OrderAction.OPEN,
        )),
        Pair("close:long:тест", Plan(
            order_id="close:long:тест", decided_at=exit_decided, price=101.0,
            side=EngineSide.LONG, action=OrderAction.CLOSE,
            exit_reason=ExitReason.SIGNAL,
        )),
    )
    _, paths = convert.markers_and_paths(
        HistoryRun(deals=(deal,)), plans, frozenset(), frame
    )
    fact = next(p for p in paths if p.layer is Layer.FACT)
    assert fact.entry_time == datetime(2026, 6, 19, 10, 10, tzinfo=MSK)
    assert fact.exit_time == datetime(2026, 6, 19, 10, 25, tzinfo=MSK), (
        f"конец линии сделки стоит не на отметке своей свечи: {fact.exit_time}"
    )

    plan_path = next(p for p in paths if p.layer is Layer.PLAN)
    assert plan_path.entry_time == datetime(2026, 6, 19, 10, 5, tzinfo=MSK), (
        f"начало расчётной линии уехало со своей свечи: {plan_path.entry_time}"
    )
    assert plan_path.exit_time == datetime(2026, 6, 19, 10, 20, tzinfo=MSK), (
        f"конец расчётной линии уехал со своей свечи: {plan_path.exit_time}"
    )
    assert fact.entry_time - plan_path.entry_time == frame.delta, (
        "расчётная линия и линия факта совпали по времени входа — наложение "
        "слоёв перестало показывать задержку исполнения"
    )


# ----------------------------------------------------------------- настройки

def test_every_engine_field_has_a_source() -> None:
    """Ни одного поля `EngineSettings` без явного источника.

    Тест смотрит на список полей самого движка, а не на список, записанный
    здесь от руки: новое поле в `engine/settings.py` обязано либо появиться
    в окне, либо быть названным в переводе — иначе оно молча уедет
    в умолчание, и никто об этом не узнает.
    """
    from_the_window = {
        "mode", "window", "volume", "reversal", "take_profit",
        "take_profit_percent", "trailing_take_profit", "trailing_start_percent",
        "trailing_offset_percent", "trailing_step_percent",
        "stop_after_take_profit", "commission_per_side",
        # ⚠️ Переехало из `from_the_previous` 05.09.2026 (`B-021`): величину
        # спрашивают у биржи и кладут в поле окна, а не тащат у прежних
        # настроек, где она навсегда оставалась умолчанием 1,0.
        "ruble_per_point",
        # Календарь владельца счёта: он ставит отметки мышкой, значит источник
        # — окно (`F-002`).
        "calendar",
    }
    from_the_previous = {
        "close_on_time_end", "trade_in_weekend", "partial_candles",
        "close_wait_bars",
        # ⚠️ Дни, названные биржей, поля в окне не имеют и иметь не должны:
        # факт про мир мышкой не снимается (решение 0038). Кладёт их тот, кто
        # говорил с брокером; окно обязано их сохранить, а не стереть.
        "exchange_days",
    }
    # Три предохранителя по деньгам. Проведены 05.09.2026 (`D-030`), но
    # **через галочку**: числа доезжают до движка только при своём
    # выключателе. Здесь они в «из окна» именно поэтому — источник у них
    # окно, а не прежние настройки.
    from_the_window |= {
        "volume_cap", "daily_loss_limit_percent", "free_funds_reserve_percent",
    }
    every_field = {field.name for field in fields(EngineSettings)}
    named = from_the_window | from_the_previous
    assert every_field == named, (
        "в EngineSettings появилось поле, для которого не назван источник: "
        f"{sorted(every_field - named)}"
    )


def test_the_guards_are_wired_but_switched_off_by_default() -> None:
    """Проводка предохранителей ничего не включила сама по себе.

    **Главное условие задачи `D-030`.** В окне стоят потолок 5 контрактов
    и дневной лимит 2 %; провести их «как есть» значило бы включить
    остановку торговли, которой владелец счёта не просил, и сломать сверку
    с прототипом — у неё предохранителей нет ни одного.

    Проверяется сравнением с умолчанием движка целиком по трём полям,
    а не «примерно тем же»: цена ошибки здесь — другой список сделок.
    """
    default = EngineSettings()
    produced = convert.engine_settings(
        Settings(volume_cap=5, daily_loss_limit_pct=2.0, free_funds_reserve_pct=30.0),
        Mode.REVERSE,
    )
    assert produced.volume_cap is default.volume_cap is None
    assert produced.daily_loss_limit_percent == default.daily_loss_limit_percent == 0.0
    assert produced.free_funds_reserve_percent == default.free_funds_reserve_percent == 0.0


def test_a_switched_on_guard_reaches_the_engine() -> None:
    """Поставленная галочка доводит число до движка — иначе поле бутафория."""
    produced = convert.engine_settings(
        Settings(
            volume_cap_enabled=True, volume_cap=7,
            daily_loss_limit_enabled=True, daily_loss_limit_pct=3.5,
            free_funds_reserve_enabled=True, free_funds_reserve_pct=25.0,
        ),
        Mode.REVERSE,
    )
    assert produced.volume_cap == pytest.approx(7.0)
    assert produced.daily_loss_limit_percent == pytest.approx(3.5)
    assert produced.free_funds_reserve_percent == pytest.approx(25.0)


@pytest.mark.parametrize("switch", [
    "volume_cap_enabled", "daily_loss_limit_enabled", "free_funds_reserve_enabled",
])
def test_each_guard_has_its_own_switch(switch: str) -> None:
    """Галочки не одна на троих: включение одного не включает остальных.

    Иначе владелец счёта, поставивший потолок объёма, молча получил бы
    остановку торговли по дневному лимиту.
    """
    produced = convert.engine_settings(
        Settings().replace(**{switch: True}), Mode.REVERSE
    )
    default = EngineSettings()
    off = [
        name for name, value in (
            ("volume_cap", produced.volume_cap),
            ("daily_loss_limit_percent", produced.daily_loss_limit_percent),
            ("free_funds_reserve_percent", produced.free_funds_reserve_percent),
        )
        if value != getattr(default, name)
    ]
    assert len(off) == 1, (
        f"галочка «{switch}» включила не только своё: {off}"
    )


def test_the_off_value_of_every_guard_is_the_default_of_the_engine() -> None:
    """Таблица `_GUARDS` согласована с умолчаниями движка — по полю, а не на глаз.

    Канарейка: тесты выше сравнивают **результат** перевода, этот — саму
    таблицу. Разъехавшись, они бы дали «выключено», которое что-то включает.
    """
    from app.convert import _GUARDS

    default = EngineSettings()
    for engine_field, guard in _GUARDS.items():
        assert getattr(default, engine_field) == guard.off, (
            f"«выключено» для {engine_field} разошлось с умолчанием движка"
        )


def test_switching_a_guard_is_a_journal_line() -> None:
    """Включение предохранителя называется словом, а не `True`/`False`."""
    lines = convert.guard_changes(
        Settings(), Settings(volume_cap_enabled=True)
    )
    assert lines == ["Потолок объёма: выключен → включён"], lines


def test_fields_absent_from_the_window_are_taken_from_the_previous_settings() -> None:
    """`close_wait_bars` и торговля в выходные не сбрасываются в умолчание.

    Через `replace(**словарь)` они бы прошли молча в обе стороны; здесь
    проверяется, что перевод их сохраняет, а не изобретает.

    ⚠️ Список редеет по мере того, как поля получают своё место в окне:
    `close_on_time_end` ушёл отсюда 05.09.2026, `ruble_per_point` — тогда же
    (`B-021`). Проверки на них — `test_closing_at_the_window_end_comes_from_the_window`
    и `test_the_cost_of_a_point_comes_from_the_window_not_from_the_previous_run`.
    """
    previous = EngineSettings(close_wait_bars=7, trade_in_weekend=True)
    produced = convert.engine_settings(Settings(), Mode.REVERSE, previous)
    assert produced.close_wait_bars == 7
    assert produced.trade_in_weekend is True


def test_the_cost_of_a_point_comes_from_the_window_not_from_the_previous_run() -> None:
    """Стоимость пункта берётся из настроек окна, а не у прежнего прогона.

    Пока она бралась у прежних настроек, положить туда настоящую величину
    было некому: она навсегда оставалась умолчанием 1,0 (`B-021`). Единица
    верна для фьючерса на индекс МосБиржи и занижает результат по РТС
    в 1,74 раза, по Бренту — в 869.
    """
    previous = EngineSettings(ruble_per_point=100.0)
    produced = convert.engine_settings(
        Settings(ruble_per_point=1.73774), Mode.REVERSE, previous
    )
    assert produced.ruble_per_point == pytest.approx(1.73774)


def test_the_default_cost_of_a_point_is_the_one_parity_stands_on() -> None:
    """Умолчание окна даёт движку ровно 1,0 — на этом стоит сверка 127 из 127.

    Проверка выглядит тривиальной и таковой не является: она стережёт
    единственное число, при котором деньги эталонного прогона остаются
    прежними. Любая подстановка «с биржи», добравшаяся до умолчания,
    сдвинет весь отчёт молча.
    """
    assert convert.engine_settings(Settings(), Mode.REVERSE).ruble_per_point == 1.0


def test_where_the_cost_of_a_point_came_from_is_a_journal_line() -> None:
    """Смена происхождения стоимости пункта — строка в журнале.

    Само число называет движок. Но число, подтверждённое биржей, и то же
    число, введённое руками, — разные основания доверять деньгам отчёта,
    и переход между ними обязан быть виден.
    """
    lines = convert.window_changes(
        Settings(ruble_per_point_source=""),
        Settings(ruble_per_point_source="биржа, MXU6, 04.09.2026 07:00"),
    )
    assert lines == [
        "Стоимость пункта, источник: не подтверждено биржей → "
        "биржа, MXU6, 04.09.2026 07:00"
    ], lines


def test_an_unchanged_pair_produces_no_journal_lines() -> None:
    """Пара без изменений собрана переводом, а не `dataclasses.replace`.

    `engine_settings()` пересобирает `volume=float(values.volume)`
    (`app/convert.py:277`) — новый объект `float` при каждом вызове, и в бою
    настройки идут именно этим путём (`app/port.py:258`). Таблица
    `changes_from`, перепутавшая `!=` на `is not` для строки объёма, увидела
    бы «Объём: 1 → 1» там, где ничего не менялось: два равных, но разных
    объекта `float` неотличимы по тождеству. Пара, собранная через
    `dataclasses.replace`, этот дефект **структурно не видит** — у
    нетронутых полей там один и тот же объект (миниплан Э1-0г §3), поэтому
    здесь пара собрана двумя независимыми вызовами перевода.
    """
    values = Settings()
    first = convert.engine_settings(values, Mode.REVERSE)
    second = convert.engine_settings(values, Mode.REVERSE)
    assert first == second
    assert first.volume is not second.volume, (
        "пара обязана быть из двух разных объектов float — иначе тест "
        "не отличается от сравнения объекта с самим собой и не проверяет "
        "то, ради чего написан"
    )
    assert first.changes_from(second) == []


def test_the_window_values_reach_the_engine() -> None:
    values = Settings(
        window_start=time(10, 0), window_end=time(12, 30),
        volume=3, take_profit_enabled=False, take_profit_pct=1.25,
        trailing_enabled=True, trailing_start_pct=0.7, trailing_offset_pct=0.3,
        trailing_step_pct=0.1, reversal_moment=ReversalMoment.SAME_BAR,
        after_take_profit=AfterTakeProfit.WAIT_FOR_SIGNAL,
        commission_per_side_rub=14.0,
    )
    settings = convert.engine_settings(values, Mode.LONG_ONLY)
    assert settings.mode is EngineMode.LONG_ONLY
    assert (settings.window.start, settings.window.end) == (time(10, 0), time(12, 30))
    assert settings.volume == 3.0
    assert settings.take_profit is False
    assert settings.take_profit_percent == 1.25
    assert settings.trailing_take_profit is True
    assert settings.reversal.value == "same_bar"
    assert settings.stop_after_take_profit is False
    assert settings.commission_per_side == 14.0


def test_the_strategy_settings_are_assembled_field_by_field() -> None:
    values = Settings(
        average_period=9, average_kind=AverageKind.SMA,
        on_price_equals_average=OnPriceEqualsAverage.TREAT_AS_SHORT,
    )
    from strategies import EmaReverseSettings

    module = convert.strategy_settings(values)
    # ⚠️ Сужение — часть проверки, а не поклон системе типов. Сборщик
    # объявлен через порт настроек и класс алгоритма не называет; убедиться,
    # что он собрал настройки **выбранного** алгоритма, а не какие-нибудь, —
    # ровно то, ради чего проверка и стоит.
    assert isinstance(module, EmaReverseSettings)
    assert module.period == 9
    assert module.kind is StrategyAverageKind.SMA
    assert module.on_equal.value == "short"


def test_a_switched_off_filter_gives_the_module_exactly_its_own_defaults() -> None:
    """Снятая галочка фильтра = умолчания торгового модуля, поле в поле.

    **Это сторож сверки с прототипом.** Она сходится 127 сделок из 127 на
    выключенном фильтре, у прототипа фильтра нет вовсе, и любое ненулевое
    значение, просочившееся сюда, сломает её молча: список сделок другой,
    а ошибки нигде нет.

    Сравнение целиком, а не по двум полям: третье поле фильтра, заведённое
    завтра и забытое в `_FILTER_OFF`, обошло бы поимённую проверку.
    """
    from strategies import EmaReverseSettings

    values = Settings(
        filter_enabled=False, threshold_percent=0.4, confirm_bars=5
    )
    produced = convert.strategy_settings(values)
    assert isinstance(produced, EmaReverseSettings)
    default = EmaReverseSettings(
        period=values.average_period,
        kind=StrategyAverageKind.EMA,
        on_equal=produced.on_equal,
    )
    assert produced == default, (
        "выключённый фильтр отдал модулю не его умолчания — сверка "
        "с прототипом поедет молча"
    )


def test_a_switched_on_filter_carries_both_numbers_to_the_module() -> None:
    """Включённая галочка отдаёт модулю то, что стоит в полях."""
    from strategies import EmaReverseSettings

    produced = convert.strategy_settings(
        Settings(filter_enabled=True, threshold_percent=0.04, confirm_bars=3)
    )
    assert isinstance(produced, EmaReverseSettings)
    assert produced.threshold_percent == pytest.approx(0.04)
    assert produced.confirm_bars == 3


def test_the_off_values_of_the_filter_are_the_defaults_of_the_module() -> None:
    """`_FILTER_OFF` совпадает с умолчаниями модуля — по таблице, а не на глаз.

    Канарейка к тесту выше: тот сравнивает результат перевода, этот —
    саму таблицу. Разъехавшись, они бы дали «выключено», которое выключает
    не туда.
    """
    from app.convert import _FILTER_OFF
    from strategies import EmaReverseSettings

    default = EmaReverseSettings()
    for name, value in _FILTER_OFF.items():
        assert getattr(default, name) == value, (
            f"«выключено» для поля {name} разошлось с умолчанием модуля"
        )


def test_a_field_of_the_filter_missing_from_the_assembler_is_refused(
    monkeypatch,
) -> None:
    """Поле «выключенного фильтра», которого нет у сборщика, — отказ вслух.

    Проверка полноты считается один раз при импорте, поэтому подменяется
    её результат, а не таблица: иначе тест проверял бы не сторожа,
    а собственную арифметику.
    """
    monkeypatch.setattr(convert, "_FILTER_OFF_GAP", ("выдуманное_поле",))
    with pytest.raises(convert.SettingsRefused, match="выдуманное_поле"):
        convert.strategy_settings(Settings())


def test_switching_the_filter_is_a_line_in_the_journal() -> None:
    """Переключение фильтра называется словом, а не `True`/`False`.

    Строку пишет порт (`_WINDOW_TOLD`): сам выключатель в настройки
    торгового модуля не переводится, и рассказать о нём больше некому.
    """
    lines = convert.window_changes(
        Settings(filter_enabled=False), Settings(filter_enabled=True)
    )
    assert lines == ["Фильтр против пилы: выключен → включён"], lines


def test_a_muted_filter_number_is_journalled_and_says_it_is_muted() -> None:
    """Правка числа при снятой галочке — строка, и в ней сказано, что она инертна.

    ТЗ §4.4 А требует строку на **каждое** изменение поля окна. Молчать
    нельзя: владелец счёта подбирает порог при снятой галочке, а потом
    восстановить, что он крутил, будет нечем. Врать «включено» тоже нельзя —
    отсюда оговорка прямо в строке.
    """
    lines = convert.window_changes(
        Settings(filter_enabled=False, threshold_percent=0.0),
        Settings(filter_enabled=False, threshold_percent=0.5),
    )
    assert len(lines) == 1, lines
    assert "фильтр выключен" in lines[0] and "0,50" in lines[0], lines


def test_a_number_of_a_working_filter_is_not_journalled_twice() -> None:
    """При включённом фильтре про число говорит только торговый модуль."""
    assert convert.window_changes(
        Settings(filter_enabled=True, threshold_percent=0.0),
        Settings(filter_enabled=True, threshold_percent=0.5),
    ) == []


def test_the_numbers_of_the_filter_are_journalled_as_they_act() -> None:
    """Журнал пишет **действующее** значение фильтра, а не то, что в поле.

    Включение галочки при заранее набранных числах обязано дать строки
    модуля «0,00% → 0,04%»: именно это изменение поведения и произошло.
    """
    off = Settings(filter_enabled=False, threshold_percent=0.04, confirm_bars=3)
    on = off.replace(filter_enabled=True)
    lines = convert.strategy_settings(on).changes_from(
        convert.strategy_settings(off)
    )
    joined = " ".join(lines)
    assert "0,04" in joined and "3" in joined, joined


@pytest.mark.parametrize("mode", list(Mode))
def test_the_modes_translate_both_ways(mode: Mode) -> None:
    settings = convert.engine_settings(Settings(), mode)
    assert convert.mode_of(settings.mode) is mode


@pytest.mark.parametrize("mode", list(EngineMode))
def test_every_engine_mode_is_known_to_the_window(mode: EngineMode) -> None:
    """Обратный перевод обязан знать **все** режимы движка, а не наоборот.

    Порт показывает в панели режим того прогона, чей результат на экране,
    и берёт его через `mode_of`. Режим, появившийся в движке без пары в окне,
    свалит весь кадр в «Прогон не удался» — вместо графика будет отказ.
    """
    assert convert.mode_of(mode) in set(Mode)


def test_an_inexpressible_behaviour_after_the_take_is_refused_out_loud() -> None:
    """«Сразу восстановить позицию» движком не выражено — значит отказ.

    Подмена вторым вариантом дала бы другие сделки под тем же названием.
    """
    values = Settings(after_take_profit=AfterTakeProfit.RESTORE_AT_ONCE)
    with pytest.raises(convert.SettingsRefused) as refusal:
        convert.engine_settings(values, Mode.REVERSE)
    assert "восстановить" in str(refusal.value)


def test_an_empty_instrument_is_refused_out_loud() -> None:
    with pytest.raises(convert.SettingsRefused):
        convert.instrument_of("   ")


def test_an_unknown_timeframe_is_refused_out_loud() -> None:
    with pytest.raises(convert.SettingsRefused) as refusal:
        convert.timeframe_of("семь минут")
    assert "5 минут" in str(refusal.value), "отказ обязан называть допустимые значения"


@pytest.mark.parametrize("label", list(convert.TIMEFRAMES))
def test_every_timeframe_from_the_window_is_known_to_the_data_layer(label: str) -> None:
    """Список в окне и список здесь обязаны совпадать.

    Размер, который окно предлагает, а программа не понимает, — это отказ
    в ответ на обычный выбор мышкой.
    """
    from ui.settings_dialog import TIMEFRAMES as IN_THE_WINDOW

    assert label in IN_THE_WINDOW
    assert convert.timeframe_of(label).minutes > 0


def test_the_window_offers_nothing_extra() -> None:
    from ui.settings_dialog import TIMEFRAMES as IN_THE_WINDOW

    assert set(IN_THE_WINDOW) == set(convert.TIMEFRAMES)


# ----------------------------------------------------------------- затенение

def _series(at_hour: int, at_minute: int, count: int, day: int = 19) -> list[Candle]:
    """Непрерывный ряд пятиминуток с указанного момента."""
    first_moment = datetime(2026, 6, day, at_hour, at_minute, tzinfo=MSK)
    return [
        Candle(
            time=first_moment + timedelta(minutes=5 * step),
            open=100.0, high=101.0, low=99.0, close=100.5, volume=1.0,
            timeframe=Timeframe(5), filled_minutes=5,
        )
        for step in range(count)
    ]


def test_the_shading_decides_by_closes_but_is_laid_out_by_starts() -> None:
    """Два времени одной свечи: решает движок по закрытию, ось — по открытию.

    Последняя торговавшая свеча — 10:50–10:55: её закрытие 10:55 попадает
    в окно 10:05–11:00, неравенства строгие. Следующая, 10:55–11:00, уже
    молчит: 11:00 в окно не входит. Полоса молчания начинается с её отметки
    на оси, то есть с 10:55. Число совпало с закрытием предыдущей свечи —
    ровно поэтому здесь и путают одно с другим.
    """
    candles = _series(9, 45, 20)  # 09:45 … 11:20, окно 10:05–11:00
    configuration = EngineSettings(window=TradingWindow(start=time(10, 5), end=time(11, 0)))
    bands = convert.shades_of(candles, configuration)
    assert len(bands) == 2, f"ожидались полосы до окна и после: {bands}"

    # Первая свеча ряда 09:45–09:50 стоит на оси в 09:45.
    assert bands[0].start == datetime(2026, 6, 19, 9, 45, tzinfo=MSK)
    # Последняя молчащая до окна — 10:00–10:05: её закрытие 10:05 в окно
    # 10:05–11:00 не попадает, неравенства строгие.
    assert bands[0].end == datetime(2026, 6, 19, 10, 0, tzinfo=MSK)
    assert bands[1].start == datetime(2026, 6, 19, 10, 55, tzinfo=MSK), (
        "полоса молчания начата не с той свечи: либо накрыла свечу, на которой "
        "робот торговал, либо оставила незатенённой свечу, на которой он молчал"
    )

    starts = {each_candle.time for each_candle in candles}
    for band in bands:
        assert band.start in starts and band.end in starts, (
            "граница полосы не совпадает ни с одной отметкой свечи на оси: "
            "полоса стоит между свечами"
        )


def test_a_non_trading_day_is_shaded_even_without_a_trading_window() -> None:
    """Окно «весь день» — законная конфигурация, и она не отменяет выходных.

    Прежде здесь стоял ранний возврат по `whole_day`: при не заданном окне
    график оставался незатенённым целиком, включая субботу и воскресенье,
    про которые журнал в тот же момент писал «торговля в выходные выключена».
    Проверка `is_trading_day` стояла внутри цикла, до которого возврат
    не доходил.
    """
    saturday = _series(10, 0, 12, day=20)  # 20.06.2026 — суббота
    configuration = EngineSettings(
        window=TradingWindow(start=time(10, 0), end=time(10, 0)),  # весь день
        trade_in_weekend=False,
    )
    assert configuration.window.whole_day, "конфигурация теста не та, что задумана"
    bands = convert.shades_of(saturday, configuration)
    assert bands, "выходной день не затенён — не видно, где робот молчал"
    assert "Нерабочий день" in bands[0].note, bands[0].note
    assert bands[0].start == datetime(2026, 6, 20, 10, 0, tzinfo=MSK)
    assert bands[0].end == datetime(2026, 6, 20, 10, 55, tzinfo=MSK)


def test_trading_on_weekends_removes_the_weekend_shading() -> None:
    """Обратная проверка: затенение спрашивает движок, а не календарь само."""
    saturday = _series(10, 0, 12, day=20)
    configuration = EngineSettings(
        window=TradingWindow(start=time(10, 0), end=time(10, 0)),
        trade_in_weekend=True,
    )
    assert convert.shades_of(saturday, configuration) == ()


def test_the_band_label_never_promises_an_exit_that_will_not_happen() -> None:
    """При выключенном «закрывать в конце окна» полоса говорит другое."""
    candles = _series(9, 45, 20)
    trading_window = TradingWindow(start=time(10, 5), end=time(11, 0))
    with_exit = convert.shades_of(candles, EngineSettings(window=trading_window))
    without_exit = convert.shades_of(
        candles, EngineSettings(window=trading_window, close_on_time_end=False)
    )
    assert "закрывается по концу окна" in with_exit[0].note
    assert "закрывается по концу окна" not in without_exit[0].note


# -------------------------------------------------------------------- журнал

def test_a_journal_line_is_carried_over_verbatim() -> None:
    entry = JournalEntry(
        at=datetime(2026, 6, 19, 10, 10, tzinfo=MSK),
        event="Вход в лонг",
        reason="Закрытие 100 выше EMA(15) 99",
        level=JournalLevel.TRADE,
    )
    row = convert.decision_row(entry)
    assert (row.time, row.event, row.reason) == (entry.at, entry.event, entry.reason)
    assert row.level is DecisionLevel.TRADE


def _deal(commission: float | None) -> Deal:
    return Deal(
        side=EngineSide.LONG, volume=1.0,
        entry_time=datetime(2026, 6, 19, 10, 10, tzinfo=MSK), entry_price=100000.0,
        entry_order_id="open:long:1",
        exit_time=datetime(2026, 6, 19, 10, 30, tzinfo=MSK), exit_price=100500.0,
        exit_order_id="close:long:1", exit_reason=ExitReason.TAKE_PROFIT,
        commission=commission, ruble_per_point=1.0,
    )


def test_without_a_tariff_the_commission_stays_unknown() -> None:
    """`None` — это не ноль. Подставленный ноль объявил бы результат чистым."""
    row = convert.trade_row(_deal(None))
    assert row.commission_rub is None
    assert row.profit_rub == 500.0          # валовая, и это видно по пустой комиссии
    assert row.exit_reason == "тейк-профит"
    assert row.side is Side.LONG


def test_with_a_tariff_the_deal_result_is_net() -> None:
    row = convert.trade_row(_deal(28.0))
    assert row.commission_rub == 28.0
    assert row.profit_rub == 472.0
    assert row.profit_pct == pytest.approx(0.5)


def test_the_summary_is_carried_as_numbers_not_recomputed() -> None:
    summary = Summary(
        trades=10, profitable=6, profitable_share=0.6, gross_profit=1000.0,
        commission=280.0, net_profit=720.0, max_drawdown=150.0, reversals=4,
        profit_factor=1.8,
    )
    carried = convert.trades_summary(HistoryRun(summary=summary))
    assert (carried.trades, carried.gross_profit_rub, carried.commission_rub) == (10, 1000.0, 280.0)
    assert (carried.net_profit_rub, carried.max_drawdown_rub, carried.reversals) == (720.0, 150.0, 4)
    assert carried.profitable_share == 0.6
    assert carried.profit_factor == 1.8, "профит-фактор пересчитан или потерян по дороге"
    assert carried.headline, (
        "итог приехал без оговорки: число снова показывается как выписка "
        "со счёта, хотя проскальзывание в нём нулевое (решение 0030)"
    )


def test_the_band_names_the_owner_mark_and_not_the_weekend() -> None:
    """Полоса под помеченным днём говорит про отметку, а не про выходные.

    Две причины молчать читаются по-разному: «вы пометили эти дни нерабочими»
    и «торговля в выходные выключена» — разные события, и подменять одно
    другим значит объяснять владельцу счёта его собственное решение чужой
    причиной (решение 0038).
    """
    friday = _series(10, 0, 12, day=19)  # 19.06.2026 — пятница, обычный будний
    marked = EngineSettings(
        window=TradingWindow(start=time(10, 0), end=time(10, 0)),  # весь день
        calendar=DayMarks.of({date(2026, 6, 19): False}),
    )
    bands = convert.shades_of(friday, marked)
    assert bands, "помеченный день не затенён — не видно, где робот молчал"
    assert "вы пометили эти дни нерабочими" in bands[0].note, bands[0].note


def test_the_band_names_a_closed_exchange_in_its_own_words() -> None:
    """День, закрытый биржей, подписан биржей, а не выходными и не отметкой."""
    friday = _series(10, 0, 12, day=19)
    closed = EngineSettings(
        window=TradingWindow(start=time(10, 0), end=time(10, 0)),
        exchange_days=DayMarks.of({date(2026, 6, 19): False}),
    )
    bands = convert.shades_of(friday, closed)
    assert bands
    assert "биржа в эти дни не работает" in bands[0].note, bands[0].note


def test_every_non_trading_rule_has_words_for_the_band() -> None:
    """У каждого правила «день нерабочий» есть подпись для полосы.

    Проверка полноты, а не трёх случаев: правило, заведённое в движке завтра
    и здесь не названное, подписало бы полосу пустым местом — «Нерабочий
    день: , входов нет».
    """
    silent = {
        rule for rule, trading, _ in engine_window._DAY_RULES if not trading
    }
    assert silent == set(convert._QUIET_OF_RULE)


# ---------------------------------------------------------------------------
# Выбор торгового алгоритма: строка журнала, каталог для окна, громкий отказ
# ---------------------------------------------------------------------------
#
# Задача З4 миниплана `strategy-modules-switchable.md`, редакция 08.09.2026.
# Здесь проверяется дорога от поля окна до журнала и до окна выбора; само
# описание правила проверяется у алгоритма (`tests/test_strategies_description.py`),
# а окно — в `tests/test_ui_algorithm_dialog.py`.


def test_the_change_of_algorithm_is_named_in_the_journal() -> None:
    """Сменили алгоритм — в журнале строка с прежним и новым **названием**.

    ТЗ §4.4 А: изменение настройки пишется с прежним и новым значением.
    Смена алгоритма — самое крупное изменение, какое бывает: меняется
    не число в правиле, а само правило.

    ⚠️ Значения здесь не проверяются на принадлежность реестру намеренно:
    `window_changes` — сборщик строк журнала, и строка обязана получиться
    и на имени, которого в сборке нет. Иначе разбор «что случилось» пропадал
    бы ровно в том случае, ради которого его читают.

    Мутация, обязанная ронять проверку: убрать `strategy_id` из `_WINDOW_TOLD`.
    """
    said = convert.window_changes(
        Settings(),
        Settings().replace(strategy_id="atr_channel"),
    )
    line = next((row for row in said if row.startswith("Торговый алгоритм")), "")
    assert line, f"смена алгоритма не попала в журнал ни одной строкой: {said}"
    assert "→" in line, "строка не показывает прежнее и новое значение"
    before, _, after = line.partition("→")
    assert "Реверс" in before, (
        "прежний алгоритм назван именем латиницей, а не названием для человека"
    )
    assert "atr_channel" in after and "нет" in after, (
        "новое имя не названо либо не сказано, что такого алгоритма в сборке нет"
    )


def test_a_field_without_a_journal_line_cannot_appear() -> None:
    """Канарейка предыдущей проверки: поле без подписи роняет сборку строк.

    Строка про безымянные поля собирается один раз при импорте
    (`_SILENT_FIELDS`), поэтому проверяется она, а не текст журнала.
    """
    assert convert._SILENT_FIELDS == (), (  # noqa: SLF001 — сторож на внутреннюю таблицу
        "поля окна остались без строки в журнале: "
        f"{convert._SILENT_FIELDS}"  # noqa: SLF001 — то же
    )


def test_an_unknown_algorithm_is_refused_out_loud() -> None:
    """Незнакомый алгоритм — отказ с фразой, а не подстановка умолчания.

    Подстановка означала бы торговлю правилом, которого владелец счёта
    не выбирал, при исправном виде окна. Отказ ловит `HistoryPort`
    и показывает «Настройки не приняты», прежние остаются в силе.
    """
    with pytest.raises(convert.SettingsRefused) as refusal:
        convert.strategy_settings(Settings().replace(strategy_id="atr_channel"))
    said = str(refusal.value)
    assert "atr_channel" in said, "отказ не называет, какой алгоритм требуется"
    assert registry.DEFAULT_ID in said, "отказ не называет, какие алгоритмы есть"


def test_the_window_default_algorithm_is_the_registry_default() -> None:
    """Умолчание окна и умолчание реестра — одно и то же имя.

    Окно реестра не видит (ARCHITECTURE.md §2), поэтому имя стоит в нём
    строкой. Разъехавшись, эти два умолчания дали бы сборку, которая торгует
    не тем правилом, о котором договаривались, и заметить это по экрану
    нельзя.
    """
    assert Settings().strategy_id == registry.DEFAULT_ID


def test_the_catalogue_carries_the_algorithms_own_words() -> None:
    """Каталог для окна собран из реестра, а не написан в `app/`.

    ⚠️ Сверяется с тем, что говорит о себе сам алгоритм. Второй текст рядом
    с первым разошёлся бы при первой правке алгоритма — молча, текст не падает.
    """
    values = Settings()
    catalogue = convert.algorithms(values)
    assert catalogue, "каталог алгоритмов пуст — выбирать человеку не из чего"
    assert [item.id for item in catalogue] == list(registry.known_ids())
    chosen = next(item for item in catalogue if item.chosen)
    assert chosen.id == values.strategy_id
    entry = registry.find(chosen.id)
    assert chosen.title == entry.title
    assert chosen.summary == entry.summary(convert.strategy_settings(values))
    assert chosen.details == convert.strategy_rule(values)


def test_the_catalogue_shows_the_numbers_that_are_applied() -> None:
    """Описание выбранного алгоритма считается по применённым настройкам.

    Мутация, обязанная ронять проверку: собирать описание по умолчаниям
    алгоритма вместо полей окна. Человек читал бы правило про период 15,
    имея в настройках 40.
    """
    chosen = convert.algorithms(Settings().replace(average_period=40))[0]
    assert "40" in chosen.details, "описание не увидело нынешнего периода"
    assert not any(sign.isdigit() for sign in chosen.summary), (
        "правило одной фразой обязано читаться без чисел: оно показывается "
        "до всякого «Применить»"
    )


def test_the_summary_of_the_chosen_algorithm_follows_the_applied_settings() -> None:
    """Правило одной фразой считается по вашим настройкам, а не по умолчаниям.

    ⚠️ Чисел в этой отрисовке нет — и на этом держался дефект. Довод «`brief()`
    цифр не содержит, значит от настроек не зависит» неверен: включённый порог
    меняет **формулировку**. Владелец счёта, поставивший фильтр против пилы,
    читал «закрытие выше средней», а на деле нужно несколько закрытий подряд
    за полосой — и на вкладке, и в окне выбора (`B-039`).

    Мутация, обязанная ронять проверку: собирать `summary` из умолчаний
    алгоритма вместо применённых настроек.
    """
    values = Settings().replace(
        filter_enabled=True, threshold_percent=0.04, confirm_bars=3
    )
    chosen = next(item for item in convert.algorithms(values) if item.chosen)
    entry = registry.find(chosen.id)
    assert chosen.summary == entry.summary(convert.strategy_settings(values)), (
        "краткое правило выбранного алгоритма собрано не из применённых настроек"
    )
    assert "подряд" in chosen.summary, (
        "подтверждение сигнала включено, а правило одной фразой говорит про "
        f"одно закрытие: «{chosen.summary}»"
    )
    assert "полосы" in chosen.summary, (
        "порог включён, а правило одной фразой говорит про саму среднюю: "
        f"«{chosen.summary}»"
    )
    assert chosen.summary != entry.summary(entry.defaults()), (
        "правило с фильтром совпало с правилом без фильтра — значит считается "
        "по умолчаниям"
    )


def test_an_algorithm_that_is_not_chosen_says_whose_numbers_it_shows() -> None:
    """У невыбранного числа свои, и строка списка обязана сказать это.

    Показывать умолчания чужого алгоритма честно — его полей в окне нет,
    брать неоткуда. Молчать об этом нельзя: правило читается как «вот что
    будет у меня». До 09.09.2026 оговорка стояла только над подробным
    описанием (`ui/algorithm_dialog.py::details_preamble`), а строка списка
    и подсказка на вкладке молчали.

    ⚠️ Ветка «алгоритм не выбран» при одной записи в реестре достижима
    единственным честным способом: в настройках стоит имя, которого в этой
    сборке нет, — файл настроек от более новой сборки. Подстраивать реестр
    ради проверки нельзя, подделка досталась бы соседям (`D-078`).
    """
    values = Settings().replace(strategy_id="atr_channel")
    catalogue = convert.algorithms(values)
    assert catalogue, "каталог пуст — проверять нечего"
    assert not any(item.chosen for item in catalogue), (
        "в настройках стоит алгоритм, которого в сборке нет, — выбранным "
        "не может оказаться ни один"
    )
    for option in catalogue:
        assert "умолчаниями" in option.summary, (
            f"алгоритм «{option.id}» не выбран, а строка списка не говорит, "
            f"что числа в нём не ваши: «{option.summary}»"
        )


def test_the_summary_of_the_chosen_algorithm_carries_no_disclaimer() -> None:
    """У выбранного числа ваши — оговорка про умолчания читалась бы как отказ."""
    chosen = next(item for item in convert.algorithms(Settings()) if item.chosen)
    assert "умолчаниями" not in chosen.summary, (
        f"строка выбранного алгоритма говорит про чужие числа: «{chosen.summary}»"
    )


def test_the_catalogue_does_not_fall_over_a_refusal() -> None:
    """Настройки собрать не удалось — каталог всё равно приезжает и говорит это.

    Окно выбора — место, куда человек приходит разобраться. Окно, упавшее
    вместо ответа, лишает его и разбора тоже. Причина отказа обязана быть
    в тексте, а не в трассировке.
    """
    # Период 0 приходит не из окна (там поле начинается с 5), а из файла
    # настроек, правленного руками, — и проверку на него делает сам алгоритм.
    catalogue = convert.algorithms(Settings().replace(average_period=0))
    assert catalogue, "каталог не приехал вовсе"
    assert "Показать правило с вашими числами не удалось" in catalogue[0].details
    assert "период средней" in catalogue[0].details, (
        "причина отказа названа не словами алгоритма, а общей фразой"
    )
    assert "умолчаниями" in catalogue[0].details, (
        "не сказано, что числа в показанном правиле не принадлежат человеку"
    )
