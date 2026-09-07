"""Пять режимов и настройки, меняющиеся на ходу.

Режимы: выключен · лонг и шорт (переворот) · только лонг · только шорт ·
только закрытие. Переключение — без перезапуска, прямо посреди открытой
позиции.

Что проверяется и почему
------------------------
Таблица переходов из ТЗ §4.4 Б и DOMAIN.md §3:

=========================  ===============================================
Было → стало               Открытая позиция
=========================  ===============================================
Реверс → только лонг       открытый шорт закрывается на ближайшем сигнале,
                           новые шорты не открываются
Реверс → только шорт       симметрично
Любой → только закрытие    новых входов нет, позиция доводится до выхода
                           по обычным правилам
Любой → выключен           позиция **остаётся**, робот ею не управляет
=========================  ===============================================

⚠️ «Выключен» позицию **не закрывает** — намеренно: выключение не должно само
по себе приводить к сделке. Но программа обязана сказать об этом крупно,
и тихое выключение с оставленной позицией — дефект. Здесь проверяется, что
движок про это пишет; крупная надпись — дело `ui/`.

⚠️ И вторая половина той же строки, которой не было ни в одном документе
до Э1-8: **вооружённый тейк переживает выключение робота**. Он заявка
у брокера, а не расчёт внутри программы. Журнал обязан назвать и это.
"""

from __future__ import annotations

import asyncio
from dataclasses import fields, replace
from datetime import date, time

import pytest

from engine import (
    MAX_CLOSE_WAIT_BARS,
    DayMarks,
    Engine,
    EngineSettings,
    EngineState,
    Mode,
    OrderAction,
    PartialCandles,
    Reversal,
    Side,
    Step,
    TakeProfit,
    TradingWindow,
    process_closed_candle,
)
from strategies import Intent
from tests.engine_helpers import (
    NextOpenExecutor,
    ScriptedStrategy,
    bar,
    candle,
    decision,
    position,
)

WINDOW = TradingWindow(time(10, 5), time(11, 0))
WORKING = EngineSettings(mode=Mode.REVERSE, window=WINDOW, take_profit=False)
PRICE = 210_000.0
INSIDE = (10, 10)


def run(
    state: EngineState | None = None,
    *,
    settings: EngineSettings = WORKING,
    intent: Intent = Intent.LONG,
    when: tuple[int, int] = INSIDE,
):
    return process_closed_candle(
        EngineState() if state is None else state,
        bar(*when, close=PRICE),
        decision(intent, close=PRICE),
        settings,
    )


def actions(outcome) -> list[OrderAction]:
    return [order.action for order in outcome.orders]


# --------------------------------------------------------------------------
# Пять режимов: что каждый разрешает
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "mode,intent,opens",
    [
        (Mode.REVERSE, Intent.LONG, True),
        (Mode.REVERSE, Intent.SHORT, True),
        (Mode.LONG_ONLY, Intent.LONG, True),
        (Mode.LONG_ONLY, Intent.SHORT, False),
        (Mode.SHORT_ONLY, Intent.LONG, False),
        (Mode.SHORT_ONLY, Intent.SHORT, True),
        (Mode.CLOSE_ONLY, Intent.LONG, False),
        (Mode.CLOSE_ONLY, Intent.SHORT, False),
        (Mode.OFF, Intent.LONG, False),
        (Mode.OFF, Intent.SHORT, False),
    ],
)
def test_each_mode_opens_exactly_what_it_promises(
    mode: Mode, intent: Intent, opens: bool
) -> None:
    """Все пять режимов на пустом рынке, обе стороны сигнала."""
    outcome = run(settings=WORKING.replace(mode=mode), intent=intent)
    assert bool(outcome.orders) is opens, f"{mode.label}, намерение {intent.label}"


def test_only_long_closes_an_open_short_on_the_nearest_signal() -> None:
    """Реверс → только лонг: открытый шорт доживает до ближайшего сигнала.

    Смена режима не закрывает позицию сама — она меняет то, что робот
    откроет дальше.
    """
    settings = WORKING.replace(mode=Mode.LONG_ONLY)
    holding = run(
        EngineState(position=position(Side.SHORT, price=PRICE)),
        settings=settings, intent=Intent.SHORT,
    )
    assert holding.orders == (), "смена режима закрыла позицию сама по себе"

    closing = run(
        EngineState(position=position(Side.SHORT, price=PRICE)),
        settings=settings, intent=Intent.LONG,
    )
    assert actions(closing) == [OrderAction.CLOSE]


def test_only_long_does_not_open_a_short_after_the_exit() -> None:
    """И следующий шорт уже не откроется — а лонг откроется."""
    settings = WORKING.replace(mode=Mode.LONG_ONLY)
    blocked = run(settings=settings, intent=Intent.SHORT)
    assert blocked.orders == ()
    assert blocked.journal[0].event == "Вход не открыт: режим"
    assert "шорты не открываются" in blocked.journal[0].reason

    allowed = run(settings=settings, intent=Intent.LONG)
    assert actions(allowed) == [OrderAction.OPEN]


def test_only_short_is_the_mirror_image() -> None:
    settings = WORKING.replace(mode=Mode.SHORT_ONLY)
    closing = run(
        EngineState(position=position(Side.LONG, price=PRICE)),
        settings=settings, intent=Intent.SHORT,
    )
    assert actions(closing) == [OrderAction.CLOSE]

    blocked = run(settings=settings, intent=Intent.LONG)
    assert blocked.orders == ()
    assert "лонги не открываются" in blocked.journal[0].reason


def test_close_only_lets_the_position_out_by_the_usual_rules() -> None:
    """«Только закрытие»: новых входов нет, выход — как обычно.

    Шаг 8 стоит **после** шага 7, поэтому выход по обратному сигналу успевает
    произойти, а вход — уже нет.
    """
    settings = WORKING.replace(mode=Mode.CLOSE_ONLY)
    closing = run(
        EngineState(position=position(Side.LONG, price=PRICE)),
        settings=settings, intent=Intent.SHORT,
    )
    assert actions(closing) == [OrderAction.CLOSE]
    assert closing.last_step is Step.CLOSE_ONLY

    empty = run(settings=settings, intent=Intent.LONG)
    assert empty.orders == ()
    assert empty.journal[0].event == "Только закрытие"


def test_off_leaves_the_position_alone_and_says_so_loudly() -> None:
    """«Выключен» позицию не закрывает — и молчать об этом нельзя.

    Выключение не должно само по себе приводить к сделке. Но тихое выключение
    с оставленной позицией — дефект: владелец счёта обязан узнать, что позиция
    осталась без присмотра.
    """
    settings = WORKING.replace(mode=Mode.OFF)
    outcome = run(
        EngineState(position=position(Side.LONG, price=PRICE)),
        settings=settings, intent=Intent.SHORT,
    )
    assert outcome.orders == (), "выключение робота привело к сделке"
    assert outcome.state.position is not None
    assert outcome.last_step is Step.MODE_OFF
    entry = outcome.journal[0]
    assert entry.level.value == "warning"
    assert "не закрывается" in entry.reason
    assert "не управляет" in entry.reason


def test_off_says_out_loud_that_an_armed_take_stays_with_the_broker() -> None:
    """Вооружённый тейк переживает выключение робота — и это надо сказать.

    Тейк сделан **состоянием у брокера**, а не событием движка: в режиме
    «Выключен» шаг 1 возвращает до шага 5, уровень не переставляется,
    но уже вооружённый остаётся и может сработать.

    Умолчать дороже, чем сказать. Поверив, что тейка нет, владелец счёта
    поставит свой стоп и получит два; поверив обратному — не закроет позицию,
    считая, что уровень сработает сам.
    """
    settings = WORKING.replace(mode=Mode.OFF)
    guarded = replace(
        position(Side.LONG, price=PRICE),
        take=TakeProfit(percent=0.5, level=211_050.0),
    )
    outcome = run(EngineState(position=guarded), settings=settings)
    assert "остаётся у брокера" in outcome.journal[0].reason
    assert "211 050" in outcome.journal[0].reason

    bare = run(
        EngineState(position=position(Side.LONG, price=PRICE)), settings=settings,
    )
    assert "Сторожимого уровня у этой позиции нет" in bare.journal[0].reason


def test_off_does_not_cancel_the_armed_take() -> None:
    """И снятия тоже не подаётся: убрать защиту — это тоже действие.

    Снятие стопа меняет риск открытой позиции, а владелец счёта просил всего
    лишь выключить робота.
    """
    guarded = replace(
        position(Side.LONG, price=PRICE),
        take=TakeProfit(percent=0.5, level=211_050.0),
    )
    outcome = run(
        EngineState(position=guarded), settings=WORKING.replace(mode=Mode.OFF),
    )
    assert outcome.orders == ()
    assert outcome.state.position is not None
    assert outcome.state.position.take_profit == 211_050.0


def test_off_still_feeds_the_candle_to_the_trading_module() -> None:
    """Свечи подаются модулю ВСЕГДА, при любом режиме.

    У прототипа среднюю считает платформа, независимо от режима. Десять минут
    в «Выключен» не должны выбрасывать свечи из средней — иначе выключение
    робота меняет список сделок.
    """
    strategy = ScriptedStrategy([decision(Intent.LONG, close=PRICE)] * 2)
    engine = Engine(strategy, WORKING.replace(mode=Mode.OFF))
    executor = NextOpenExecutor()

    async def scenario() -> None:
        await engine.on_market_candle(candle(10, 5, close=PRICE), executor)
        await engine.on_market_candle(candle(10, 10, close=PRICE), executor)

    asyncio.run(scenario())
    assert len(strategy.seen) == 2, "свечи не дошли до модуля в режиме «Выключен»"


# --------------------------------------------------------------------------
# Переключение на ходу: журнал «прежнее → новое»
# --------------------------------------------------------------------------

def test_switching_the_mode_on_the_fly_writes_both_values() -> None:
    """ТЗ §4.4 А: изменение пишется с прежним и новым значением.

    «Настройка изменена» без чисел не даёт владельцу счёта восстановить, что
    именно он поменял и когда — а по журналу решений потом объясняют сделки.
    """
    engine = Engine(ScriptedStrategy([]), WORKING)
    lines = engine.apply_settings(
        WORKING.replace(mode=Mode.LONG_ONLY), at=bar(*INSIDE).closes_at,
    )
    assert len(lines) == 1
    assert "Лонг и шорт (переворот) → Только лонг" in lines[0].reason
    assert "со следующей сделки" in lines[0].reason
    assert engine.settings.mode is Mode.LONG_ONLY


#: Одна запись на поле `EngineSettings` — изменение и что искать в строке
#: журнала. Список сверяется с `fields(EngineSettings)` ниже, поэтому забытое
#: поле роняет прогон, а не проходит молча.
#:
#: ⚠️ Для `close_on_time_end` и `trade_in_weekend` ожидание — **устойчивое
#: словосочетание заголовка** («конце окна», «выходные»), а не вся
#: форматированная фраза с числами: полная фраза ломается от правки
#: пунктуации, и её «чинят» подгонкой ожидания, а не кода. Тем же способом
#: ловится и перестановка заголовков местами — мутация, забраковавшая
#: редакцию 1 миниплана Э1-0г: множество проверяемых полей не меняется,
#: значит сторож на множестве ключей её не видит, а этот — видит, потому
#: что после перестановки в строке о `close_on_time_end` стоит чужой
#: заголовок и «конце окна» в ней больше нет.
_SETTINGS_CHANGE_CASES: list[tuple[dict, str]] = [
    ({"mode": Mode.LONG_ONLY}, "Режим"),
    ({"window": TradingWindow(time(9, 0), time(9, 30))}, "Торговое окно"),
    ({"close_on_time_end": False}, "конце окна"),
    ({"trade_in_weekend": True}, "выходные"),
    ({"volume": 3.0}, "Объём: 1 → 3"),
    ({"reversal": Reversal.SAME_BAR}, "Момент переворота: Через свечу"),
    ({"take_profit": False}, "Тейк-профит: да → нет"),
    ({"take_profit_percent": 1.2}, "Тейк-профит, %: 0,5 → 1,2"),
    ({"trailing_take_profit": True}, "Скользящий тейк-профит: нет → да"),
    ({"trailing_start_percent": 0.6}, "порог включения"),
    ({"trailing_offset_percent": 0.3}, "Скользящий тейк, отступ, %: 0,2 → 0,3"),
    ({"trailing_step_percent": 0.1}, "Скользящий тейк, шаг подтяжки, %: 0,05 → 0,1"),
    ({"stop_after_take_profit": False}, "Стоп на день после тейка: да → нет"),
    ({"partial_candles": PartialCandles.SKIP}, "Неполные свечи: Принимать"),
    # Три предохранителя по деньгам. Умолчание у всех — «выключено», поэтому
    # в строке журнала стоит «нет → …»: владелец счёта обязан видеть, что
    # защита только что появилась, а не молча стояла всегда.
    ({"volume_cap": 5.0}, "Потолок объёма: нет → 5"),
    ({"daily_loss_limit_percent": 2.0}, "Дневной лимит убытка, %: нет → 2"),
    ({"free_funds_reserve_percent": 30.0}, "Минимальный запас средств, %: нет → 30"),
    ({"commission_per_side": 14.0}, "Комиссия за контракт на сторону: не задана → 14 ₽"),
    # ⚠️ Полей в окне настроек у этих двух нет — и именно поэтому они здесь.
    # `apply_settings` принимает `EngineSettings` целиком, то есть подменить
    # их может кто угодно, кто соберёт новое значение.
    ({"ruble_per_point": 5.0}, "Рублей в пункте цены: 1 ₽ → 5 ₽"),
    ({"close_wait_bars": 5}, "Ожидание исполнения выхода: 3 свечи → 5 свечей"),
    # Календарь: строка обязана нести дату и то, что с ней стало. «Календарь
    # изменён» без дня означало бы, что владелец счёта не может проверить,
    # тот ли день он пометил, — а помечает он его по деньгам.
    (
        {"calendar": DayMarks.of({date(2026, 6, 12): False})},
        "Календарь нерабочих дней: нет → 12.06.2026 не торгуем",
    ),
    # ⚠️ Дни биржи в окне не задаются вовсе, и тем важнее строка: набор
    # приходит от того, кто говорил с брокером, и меняет список сделок.
    (
        {"exchange_days": DayMarks.of({date(2026, 6, 12): False})},
        "Дни, названные биржей: нет → 12.06.2026 не торгуем",
    ),
]


def test_every_settings_field_has_a_change_case() -> None:
    """Список выше покрывает каждое поле `EngineSettings` ровно один раз.

    Список полей берётся у самого класса, а не переписан здесь от руки:
    новое поле без строки в списке роняет этот тест, а не проходит молча.
    """
    covered = [next(iter(changes)) for changes, _ in _SETTINGS_CHANGE_CASES]
    assert len(covered) == len(set(covered)), f"поле повторено дважды: {covered}"
    assert set(covered) == {field.name for field in fields(EngineSettings)}, (
        "поле EngineSettings без строки в списке или лишняя строка: "
        f"{set(covered) ^ {field.name for field in fields(EngineSettings)}}"
    )


@pytest.mark.parametrize("changes,expected", _SETTINGS_CHANGE_CASES)
def test_every_changed_setting_names_both_values(
    changes: dict, expected: str
) -> None:
    """Каждая настройка, меняющаяся на ходу, попадает в журнал своей строкой."""
    before = EngineSettings(mode=Mode.REVERSE, window=WINDOW)
    lines = before.replace(**changes).changes_from(before)
    assert len(lines) == 1, lines
    assert expected in lines[0]


def test_a_changed_setting_does_not_touch_the_open_position() -> None:
    """Новое значение действует со следующей сделки. Это ТЗ §4.4 А.

    Иначе робот начал бы «доливать» и «отрезать» на ровном месте: объём
    читается только в момент подачи заявки на вход, а план тейка заморожен
    на входе в позицию.
    """
    was = replace(
        position(Side.LONG, price=PRICE, volume=1.0),
        take=TakeProfit(percent=0.5, level=211_050.0),
    )
    engine = Engine(ScriptedStrategy([]), WORKING, state=EngineState(position=was))
    engine.apply_settings(
        WORKING.replace(volume=5.0, take_profit=True, take_profit_percent=2.0),
        at=bar(*INSIDE).closes_at,
    )
    assert engine.position is not None
    assert engine.position.volume == 1.0
    assert engine.position.take_profit == 211_050.0


def test_the_wait_before_a_halt_cannot_be_raised_quietly() -> None:
    """Ожидание выхода: выше границы нельзя, а внутри границы — не молча.

    ⚠️ **На этом числе стоит безопасность умолчания `Refusal.UNKNOWN`.**
    Не назвав причину, исполнитель заставляет движок считать сторожа живым
    и ждать сделку. Ошибка в эту сторону ограничена и громкая: позиция без
    управления считанные свечи, дальше счётчик и остановка. Ошибка в обратную —
    шорт на полный объём, и движок про него не узнает никогда.

    ⚠️ **Прежний сторож проверял обратное — и охранял ровно тот дефект,
    от которого ставился.** Он требовал `changes == []` со словами «попало
    в журнал — значит кто-то меняет на ходу». Причина и признак поменяны
    местами: строка журнала это **обнаружение** изменения, а не его источник.
    Менять настройку умеет `Engine.apply_settings`, который принимает
    `EngineSettings` целиком и это поле не проверяет; молчание же
    в `changes_from` означало ровно одно — подъём ожидания до ста свечей
    (восемь часов в рынке через вечернюю сессию) проходит незамеченным.
    Тот же тест сам и показывал, что `replace(close_wait_bars=10)`
    выполняется без ошибки.

    Теперь проверяется то, что нужно: поднять выше границы **нельзя**,
    а любое изменение внутри границы **видно**.
    """
    assert EngineSettings().close_wait_bars == 3
    # Пять минут на свечу: пятнадцать минут ожидания при умолчании.
    assert EngineSettings().close_wait_bars <= MAX_CLOSE_WAIT_BARS

    # Ноль останавливал бы робота на каждом штатном выходе.
    with pytest.raises(ValueError, match="хотя бы одну свечу"):
        EngineSettings(close_wait_bars=0)

    # Выше границы — отказ, а не молчаливое согласие. Сто свечей это восемь
    # часов в рынке со сторожем, которого движок считает живым.
    with pytest.raises(ValueError, match="дольше"):
        EngineSettings(close_wait_bars=MAX_CLOSE_WAIT_BARS + 1)
    with pytest.raises(ValueError, match="дольше"):
        EngineSettings(close_wait_bars=100)

    # А изменение внутри границы обязано быть видно в журнале.
    changes = EngineSettings().replace(
        close_wait_bars=MAX_CLOSE_WAIT_BARS
    ).changes_from(EngineSettings())
    assert changes == ["Ожидание исполнения выхода: 3 свечи → 12 свечей"], (
        "подъём ожидания прошёл молча — а на нём стоит граница ошибки "
        "умолчания `Refusal.UNKNOWN`"
    )


def test_settings_without_a_field_in_the_window_still_reach_the_journal() -> None:
    """Полное изменение настроек: журналируется **каждое** изменённое поле.

    ⚠️ Проверяется сквозь `Engine.apply_settings`, а не только
    `changes_from`: подменяет настройки движок, и ТЗ §4.4 А требует строку
    с прежним и новым значением от него, а не от функции сравнения.

    Оба поля здесь — те, у которых **нет поля в окне настроек**. Именно
    поэтому они и опасны: их не набирает человек, их подставляет код, и без
    строки в журнале подмена ничем себя не выдаёт.
    """
    lines: list = []
    engine = Engine(ScriptedStrategy([]), WORKING, journal=lines.append)
    engine.apply_settings(
        WORKING.replace(close_wait_bars=5, ruble_per_point=5.0),
        at=bar(*INSIDE).closes_at,
    )
    written = [entry.reason for entry in lines]
    assert any(
        "Ожидание исполнения выхода: 3 свечи → 5 свечей" in reason
        for reason in written
    ), written
    assert any(
        "Рублей в пункте цены: 1 ₽ → 5 ₽" in reason for reason in written
    ), written
    assert engine.settings.close_wait_bars == 5


def test_the_engine_refuses_to_date_a_settings_change_out_of_thin_air() -> None:
    """Своих часов у движка нет: «сейчас» — это время закрытия последней свечи.

    До первой свечи момент обязан назвать вызывающий. Подставить сюда
    системное время значило бы получить в журнале прогона по истории события
    из сегодняшнего дня.
    """
    engine = Engine(ScriptedStrategy([]), WORKING)
    with pytest.raises(ValueError, match="который час"):
        engine.apply_settings(WORKING.replace(volume=2.0))
