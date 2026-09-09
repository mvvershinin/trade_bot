"""Переводы между слоями. Ни одного решения — только перекладка значений.

`app/` — сборка и проводка (ARCHITECTURE.md §2). Ответ на вопрос «войдёт ли
робот сейчас» здесь не живёт и жить не может: сюда приходят готовые сделки,
готовые строки журнала и готовые числа, а отсюда уходят объекты `ui.models`.

Почему перевод отдельным модулем и почему он **явный**
------------------------------------------------------
Три границы, на каждой из которых молчаливая подмена уже стоила проекту времени:

1. **Свеча.** Движок считает по времени **закрытия** свечи (граница торгового
   окна, ARCHITECTURE.md §6), а ось графика размечена **открытиями**: свеча
   подписывается началом своего интервала, как в терминале брокера. Перевод
   одного соглашения в другое живёт здесь и только здесь — `window_candle`
   для свечи, `chart_time` для времени, пришедшего из движка, `bar_open`
   для момента внутри бара. Пока перевода не было, весь график вместе
   с метками стоял на бар правее, чем у брокера (B-008).

   ⚠️ Сдвиг обязан быть **одинаковым для всех рядов графика** — свечей,
   средней, меток, линий сделок и полос затенения. Сдвинуть один ряд и забыть
   другой хуже, чем не сдвигать ничего: картинка останется цельной на вид,
   а метки встанут не на свои свечи. Поэтому в `ChartData` не попадает
   ни одно время, не прошедшее через функции этого модуля.

2. **Настройки.** `ui.models.Settings` и `engine.EngineSettings` — разные
   наборы полей с разными именами. Перевод собирается **перечислением полей**,
   а не `replace(**словарь)`: через `replace` в движок проходят поля, которых
   в окне нет (`close_wait_bars`, `ruble_per_point`), и меняют поведение, не
   попадая ни в одну строку журнала изменений.

3. **Строки окна настроек.** `SettingsDialog.values()` отдаёт таймфрейм
   произвольной строкой, а инструмент — хоть пустой строкой: поле ввода
   ничего не проверяет. Разбор здесь **громкий**: `SettingsRefused`
   с человеческой фразой. Молчаливая подмена умолчанием («не разобрали —
   возьмём пять минут») переезжает на слой ниже и всплывает как «робот
   торгует не тем инструментом».
"""

from __future__ import annotations

import dataclasses
import enum
import functools
import typing
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from backtest import Costs, Deal, HistoryRun, Pair, headline
from engine import (
    DayMarks,
    DayRule,
    EngineSettings,
    JournalEntry,
    JournalLevel,
    OrderAction,
    Reversal,
    TradingWindow,
    close_time,
    day_verdict,
)
from engine import Mode as EngineMode
from engine import Side as EngineSide
from market import Candle as MarketCandle
from market import DecisionLevel as StoredDecisionLevel
from market import DecisionRecord, StoredDecision, StoredTrade, Timeframe, TradeRecord, bar_start
from market import RunOrigin as StoredRunOrigin
from market import TradeSide as StoredTradeSide
from strategies import StrategySettings, registry
from ui.backend import SettingsDiff
from ui.formatting import (
    fmt_datetime,
    fmt_money,
    fmt_number,
    fmt_percent,
    fmt_price,
    fmt_volume,
    to_msk,
)
from ui.models import (
    AfterTakeProfit,
    AlgorithmOption,
    BacktestReport,
    BacktestRequest,
    Candle,
    DecisionLevel,
    DecisionRow,
    Layer,
    LinePoint,
    Marker,
    MarkerKind,
    Mode,
    ReversalMoment,
    RunAssumption,
    RunOrigin,
    Settings,
    Shade,
    ShadeKind,
    Side,
    TradePath,
    TradeRow,
    TradesSummary,
)

__all__ = [
    "MONEY_FIELDS",
    "SettingsRefused",
    "TIMEFRAMES",
    "timeframe_of",
    "instrument_of",
    "engine_settings",
    "strategy_settings",
    "strategy_rule",
    "rule_of",
    "rule_headline_of",
    "rule_changes",
    "algorithms",
    "chosen_algorithm",
    "strategy_title",
    "run_costs",
    "window_changes",
    "guard_changes",
    "all_changes",
    "settings_diff",
    "mode_of",
    "side_of",
    "average_label",
    "window_candle",
    "decision_row",
    "trade_row",
    "decision_record",
    "trade_record",
    "row_of_stored_decision",
    "row_of_stored_trade",
    "origin_of_stored",
    "stored_origin",
    "trades_summary",
    "run_days",
    "run_assumptions",
    "run_report",
    "average_line",
    "shades_of",
    "markers_and_paths",
    "bar_open",
    "chart_time",
]


class SettingsRefused(Exception):
    """Настройки окна не переводятся в настройки движка.

    Текст исключения показывается владельцу счёта как есть — значит он должен
    быть фразой, а не кодом, и должен говорить, что именно сделать.
    """


# ---------------------------------------------------------------------------
# Строки окна → значения слоёв
# ---------------------------------------------------------------------------

#: Подписи размеров свечи из окна настроек (`ui/settings_dialog.py`) и то,
#: что за ними стоит. Словарь здесь, а не в окне: окно про `market.Timeframe`
#: не знает, а движок про подписи — тем более.
TIMEFRAMES: dict[str, Timeframe] = {
    "1 минута": Timeframe(1),
    "5 минут": Timeframe(5),
    "15 минут": Timeframe(15),
    "30 минут": Timeframe(30),
    "1 час": Timeframe(60),
    "4 часа": Timeframe(240),
    "День": Timeframe(1440),
}


def timeframe_of(text: str) -> Timeframe:
    """Размер свечи по подписи из окна. Незнакомая подпись — отказ вслух."""
    try:
        return TIMEFRAMES[text.strip()]
    except KeyError:
        raise SettingsRefused(
            f"Размер свечи «{text}» программа не знает. Выберите один из: "
            + ", ".join(TIMEFRAMES)
        ) from None


def instrument_of(text: str) -> str:
    """Код инструмента. Пустое поле — отказ, а не «возьмём умолчание»."""
    value = text.strip()
    if not value:
        raise SettingsRefused(
            "Инструмент не заполнен. Впишите код фьючерса в настройках — "
            "например, MXU6."
        )
    return value


# ---------------------------------------------------------------------------
# Настройки окна → настройки движка и торгового модуля
# ---------------------------------------------------------------------------

_MODES: dict[Mode, EngineMode] = {
    Mode.OFF: EngineMode.OFF,
    Mode.REVERSE: EngineMode.REVERSE,
    Mode.LONG_ONLY: EngineMode.LONG_ONLY,
    Mode.SHORT_ONLY: EngineMode.SHORT_ONLY,
    Mode.CLOSE_ONLY: EngineMode.CLOSE_ONLY,
}

_REVERSALS: dict[ReversalMoment, Reversal] = {
    ReversalMoment.NEXT_BAR: Reversal.THROUGH_BAR,
    ReversalMoment.SAME_BAR: Reversal.SAME_BAR,
}

_SIDES: dict[EngineSide, Side] = {
    EngineSide.LONG: Side.LONG,
    EngineSide.SHORT: Side.SHORT,
}

_LEVELS: dict[JournalLevel, DecisionLevel] = {
    JournalLevel.INFO: DecisionLevel.INFO,
    JournalLevel.TRADE: DecisionLevel.TRADE,
    JournalLevel.WARNING: DecisionLevel.WARNING,
    JournalLevel.ERROR: DecisionLevel.ERROR,
}

# Уровень строки журнала и сторона сделки существуют в трёх слоях сразу:
# `engine/` их производит, `market/` хранит, `ui/` показывает. Значения строк
# у всех трёх совпадают дословно, но перевод всё равно идёт **по словарю**,
# а не через `Enum(value)`: словарь падает громко на первом же новом уровне,
# а конструктор по значению — только там, где новый уровень встретится
# в данных, то есть у владельца счёта.
_STORED_LEVELS: dict[JournalLevel, StoredDecisionLevel] = {
    JournalLevel.INFO: StoredDecisionLevel.INFO,
    JournalLevel.TRADE: StoredDecisionLevel.TRADE,
    JournalLevel.WARNING: StoredDecisionLevel.WARNING,
    JournalLevel.ERROR: StoredDecisionLevel.ERROR,
}

_LEVELS_OF_STORED: dict[StoredDecisionLevel, DecisionLevel] = {
    StoredDecisionLevel.INFO: DecisionLevel.INFO,
    StoredDecisionLevel.TRADE: DecisionLevel.TRADE,
    StoredDecisionLevel.WARNING: DecisionLevel.WARNING,
    StoredDecisionLevel.ERROR: DecisionLevel.ERROR,
}

_STORED_SIDES: dict[EngineSide, StoredTradeSide] = {
    EngineSide.LONG: StoredTradeSide.LONG,
    EngineSide.SHORT: StoredTradeSide.SHORT,
}

_SIDES_OF_STORED: dict[StoredTradeSide, Side] = {
    StoredTradeSide.LONG: Side.LONG,
    StoredTradeSide.SHORT: Side.SHORT,
}

# Происхождение прогона — третье, что живёт в трёх слоях сразу. Перевод по
# словарю, а не через `RunOrigin(value)`: словарь падает на первом же новом
# происхождении здесь, у разработчика, а конструктор по значению — там, где
# новое происхождение встретится в данных, то есть у владельца счёта.
_ORIGINS_OF_STORED: dict[StoredRunOrigin, RunOrigin] = {
    StoredRunOrigin.LIVE: RunOrigin.LIVE,
    StoredRunOrigin.PAPER: RunOrigin.PAPER,
    StoredRunOrigin.BACKTEST: RunOrigin.BACKTEST,
}


#: Обратный перевод — **выведенный**, а не написанный вторым списком.
#: Два списка одних и тех же трёх пар разошлись бы молча: происхождение,
#: заведённое завтра, попало бы в один и не попало в другой, и прогон
#: записался бы либо не тем видом, либо вовсе не записался. Что перевод
#: остался взаимно однозначным — проверяется тестом: два вида окна,
#: сведённые к одному виду базы, потеряли бы различие между боевым
#: прогоном и симуляцией.
_STORED_OF_ORIGINS: dict[RunOrigin, StoredRunOrigin] = {
    window: stored for stored, window in _ORIGINS_OF_STORED.items()
}


def origin_of_stored(origin: StoredRunOrigin) -> RunOrigin:
    """Происхождение прогона из базы → происхождение для окна и выгрузки."""
    return _ORIGINS_OF_STORED[origin]


def stored_origin(origin: RunOrigin) -> StoredRunOrigin:
    """Происхождение прогона окна → происхождение для записи в базу.

    Нужен записи прогона (`app/runs.py`): признак «боевой или по истории»
    живёт у порта как `ui.models.RunOrigin`, а колонка базы хранит
    `market.RunOrigin`. Утиной типизации между ними нет и не будет —
    у типов совпадают значения, но не тип (`market/journal.py`).
    """
    return _STORED_OF_ORIGINS[origin]


#: Откуда движок берёт каждое своё поле, если оно **приходит из окна**.
#:
#: Таблица, а не перечисление в конструкторе, и по той же причине, что
#: у торгового модуля ниже: поле, заведённое в `EngineSettings` завтра
#: и здесь не названное, молча вернулось бы к умолчанию при каждом
#: «Применить». Так уже было с `close_on_time_end`: настройка «закрывать
#: позицию по концу окна» существовала в движке, работала обеими ветками —
#: и не имела поля в окне, поэтому её тащили у прежних настроек.
_ENGINE_FROM_WINDOW: dict[str, Callable[[Settings, Mode], object]] = {
    "mode": lambda _values, mode: _MODES[mode],
    "window": lambda values, _mode: TradingWindow(
        start=values.window_start, end=values.window_end
    ),
    "close_on_time_end": lambda values, _mode: values.close_on_time_end,
    "volume": lambda values, _mode: float(values.volume),
    "reversal": lambda values, _mode: _REVERSALS[values.reversal_moment],
    "take_profit": lambda values, _mode: values.take_profit_enabled,
    "take_profit_percent": lambda values, _mode: values.take_profit_pct,
    "trailing_take_profit": lambda values, _mode: values.trailing_enabled,
    "trailing_start_percent": lambda values, _mode: values.trailing_start_pct,
    "trailing_offset_percent": lambda values, _mode: values.trailing_offset_pct,
    "trailing_step_percent": lambda values, _mode: values.trailing_step_pct,
    "stop_after_take_profit": lambda values, _mode: (
        values.after_take_profit is AfterTakeProfit.STOP_FOR_THE_DAY
    ),
    "commission_per_side": lambda values, _mode: values.commission_per_side_rub,
    # ⚠️ Стоимость пункта переехала сюда из «полей, которых в окне нет»
    # 05.09.2026 (`B-021`). Прежде она бралась у прежних настроек движка
    # и потому оставалась умолчанием 1,0 навсегда: положить её туда было
    # некому. Единица верна для одного контракта из шести, а ошибка тихая —
    # список сделок тот же, деньги другие.
    #
    # Само число окно **не выдумывает**: его спрашивают у биржи
    # (`market.iss.InstrumentSpec`) и подставляют в поле, а владелец счёта
    # вправе перебить. Здесь только перекладка.
    "ruble_per_point": lambda values, _mode: values.ruble_per_point,
    # Календарь владельца счёта: отметки окна → набор дат движка. Перевод,
    # а не правило: что значит отметка, решает `engine/window.py`.
    "calendar": lambda values, _mode: DayMarks.of(
        (mark.day, mark.trading) for mark in values.calendar
    ),
}

#: Поля движка, **которых в окне нет вовсе**. Берутся у прежних настроек,
#: а не у умолчания: умолчание молча отменило бы то, что кто-то поставил
#: осознанно (`close_wait_bars` подменяется целиком через `apply_settings`,
#: и молчаливая подмена там запрещена прямым текстом).
_ENGINE_FROM_BASE: frozenset[str] = frozenset({
    "trade_in_weekend",
    "partial_candles",
    "close_wait_bars",
    # ⚠️ Дни, названные биржей, — **не настройка**, и в окне их нет намеренно:
    # мышкой факт про мир не снимается (решение 0038). Берутся у прежних
    # настроек, потому что кладёт их туда тот, кто говорил с брокером, а любое
    # «Применить» из окна обязано их сохранить, а не стереть.
    #
    # ⚠️ Сегодня их туда не кладёт никто: расписание брокера отвечает только
    # про сегодняшний день, и проводка живого ответа в движок не сделана
    # (`F-002`). Набор всегда пуст, и это «не знаем», а не «биржа работает».
    "exchange_days",
})

@dataclass(frozen=True, slots=True)
class _Guard:
    """Один предохранитель по деньгам: выключатель, число и что значит «выключен».

    Три поля вместо двух не для симметрии. **Число без выключателя провести
    нельзя**: у потолка объёма в окне стоит 5, у дневного лимита 2 %, и
    проводка их «как есть» включила бы остановку торговли, которую владелец
    счёта не просил. Выключатель делает включение отдельным осознанным
    действием, а `off` держит обещание «пока не включили — ведём себя
    в точности как раньше».
    """

    #: Поле `ui.models.Settings` с галочкой.
    switch: str
    #: Поле `ui.models.Settings` с числом.
    number: str
    #: Что уходит движку при снятой галочке. **Обязано совпадать
    #: с умолчанием `EngineSettings`** — это проверяется тестом, а не
    #: обещанием: на умолчаниях стоит сверка с прототипом, 127 из 127.
    off: object


#: Три предохранителя по деньгам: ключ — поле `EngineSettings`.
#:
#: Проведены 05.09.2026 (`D-030`). До того числа из окна до движка
#: не доходили вовсе, и окно честно писало об этом красным. Теперь доходят —
#: но **только через галочку**, и по умолчанию все три галочки сняты:
#: проводка не имеет права ничего включить сама.
#:
#: ⚠️ Умолчания чисел (5 контрактов, 2 %, 30 %) остались прежними и остаются
#: **предложением**, а не значением. Цифры называет владелец счёта
#: (`CLAUDE.md`), и до того, как он их назвал, ни одна проверка не работает.
_GUARDS: dict[str, _Guard] = {
    "volume_cap": _Guard("volume_cap_enabled", "volume_cap", None),
    "daily_loss_limit_percent": _Guard(
        "daily_loss_limit_enabled", "daily_loss_limit_pct", 0.0
    ),
    "free_funds_reserve_percent": _Guard(
        "free_funds_reserve_enabled", "free_funds_reserve_pct", 0.0
    ),
}


def _guard_value(values: Settings, guard: _Guard) -> object:
    """Число предохранителя, если галочка стоит, иначе «выключено»."""
    if not getattr(values, guard.switch):
        return guard.off
    number = getattr(values, guard.number)
    # `volume_cap` у движка — `float | None`, а в окне это целые контракты.
    return float(number) if guard.off is None else number


# Три предохранителя дописываются в таблицу источников **циклом**, а не
# тремя строками руками: четвёртый, заведённый завтра, попадёт в перевод
# тем же движением, что и в окно. Значение по умолчанию у `lambda` —
# не украшение: без него все три замыкания смотрели бы на последний
# `guard` цикла, и потолок объёма получил бы процент запаса средств.
for _engine_field, _guard in _GUARDS.items():
    _ENGINE_FROM_WINDOW[_engine_field] = (
        lambda values, _mode, guard=_guard: _guard_value(values, guard)  # type: ignore[misc]  # значение по умолчанию связывает замыкание
    )


def _engine_gap() -> tuple[str, ...]:
    """Поля движка, не названные ни в одной из таблиц. Пусто — таблицы полны."""
    known = {field.name for field in dataclasses.fields(EngineSettings)}
    named = set(_ENGINE_FROM_WINDOW) | _ENGINE_FROM_BASE
    return tuple(sorted(known - named))


#: Считается один раз, при импорте: набор полей класса за время работы
#: не меняется.
_ENGINE_GAP: tuple[str, ...] = _engine_gap()


def engine_settings(
    values: Settings, mode: Mode, base: EngineSettings | None = None
) -> EngineSettings:
    """Настройки окна → настройки движка. **По таблицам, а не поимённо.**

    `mode` отдельным доводом, потому что в `Settings` его нет: режим живёт
    в панели управления окна, а не в диалоге настроек, и приходит от `set_mode`.

    `base` — прежние настройки движка. Из них берутся поля, которых в окне
    нет (`_ENGINE_FROM_BASE`): собрать их из `Settings` неоткуда, а потерять —
    значит молча вернуть умолчание.

    ⚠️ Три предохранителя по деньгам проводятся **через галочку** (`_GUARDS`).
    При снятой галочке движок получает своё умолчание, то есть выключенную
    проверку, и поведение робота остаётся ровно прежним. Провести число
    напрямую было нельзя: умолчания окна — потолок 5 контрактов и дневной
    лимит 2 % — включили бы остановку торговли, которой владелец счёта
    не просил.

    ⚠️ Поле движка, не названное ни в одной таблице, — **отказ вслух**.
    Прежде такое поле молча возвращалось к умолчанию при каждом «Применить»:
    так жила настройка «закрывать позицию по концу окна», у которой обе ветки
    в движке работали, а поля в окне не было. Тихое умолчание здесь означает
    торговлю с настройками, которых владелец счёта не выбирал.

    ⚠️ Третий вариант поведения после тейка («сразу восстановить позицию»)
    движком не выражен вовсе, и подменять его вторым нельзя: это другое
    поведение и другие сделки. Отказ громкий.
    """
    if _ENGINE_GAP:
        raise SettingsRefused(
            "Программа собрана несогласованно: у движка есть настройки, "
            f"которых окно не переносит — {', '.join(_ENGINE_GAP)}. Работать "
            "так нельзя: движок получил бы умолчания вместо того, что выбрано "
            "в окне. Обновите программу целиком."
        )
    if values.after_take_profit is AfterTakeProfit.RESTORE_AT_ONCE:
        raise SettingsRefused(
            "Вариант «Сразу восстановить позицию» после тейк-профита в движке "
            "пока не сделан: непонятно, в какую сторону и по какой цене "
            "восстанавливать, и замеров по нему нет. Выберите «Не входить "
            "до конца дня» или «Ждать нового сигнала средней»."
        )
    previous = base if base is not None else EngineSettings()
    fields: dict[str, Any] = {
        name: take(values, mode) for name, take in _ENGINE_FROM_WINDOW.items()
    }
    for name in _ENGINE_FROM_BASE:
        fields[name] = getattr(previous, name)
    return EngineSettings(**fields)


#: Что уходит модулю, когда галочка «Фильтр против пилы» снята. Имена —
#: **полей окна**, а не полей модуля: снятая галочка отменяет то, что стоит
#: в полях окна, а как эти поля зовутся у модуля — дело модуля.
#:
#: Таблица, а не два `if` внутри сборщика, и причина та же, по которой
#: таблицей стал сам сборщик: третье поле фильтра, заведённое завтра, здесь
#: не окажется — и снятая галочка перестанет означать «выключено» ровно
#: в том поле, о котором забыли. Полноту стережёт `_filter_off_gap`.
#:
#: ⚠️ Значения обязаны совпадать с умолчаниями модуля, и это проверяется
#: тестом, а не обещанием: на выключенном фильтре стоит сверка с прототипом
#: (127 сделок из 127), а у прототипа фильтра нет вовсе.
_FILTER_OFF: dict[str, object] = {
    "threshold_percent": 0.0,
    "confirm_bars": 1,
}


def _filter_off_gap() -> tuple[str, ...]:
    """Поля выключенного фильтра, которых у окна нет. Пусто — согласовано.

    Опечатка в таблице выше не отняла бы настройку, а завела бы
    несуществующее поле окна — и снятая галочка молча перестала бы
    что-либо выключать.
    """
    known = {field.name for field in dataclasses.fields(Settings)}
    return tuple(sorted(set(_FILTER_OFF) - known))


#: Считается один раз, при импорте: набор полей окна за время работы
#: не меняется, а платить за проверку на каждом прогоне незачем.
_FILTER_OFF_GAP: tuple[str, ...] = _filter_off_gap()


@functools.lru_cache(maxsize=None)
def _module_types(entry: registry.StrategyEntry) -> Mapping[str, Any]:
    """Объявленные типы полей настроек алгоритма. Считается раз на запись.

    Нужны, чтобы перевести значение поля окна в значение поля модуля, не зная
    имён его классов: тип поля называет сам модуль, а сборка только читает
    объявление. `get_type_hints`, а не `dataclasses.fields`, — у второго
    в `.type` лежит строка, потому что модули объявляют
    `from __future__ import annotations`.
    """
    return typing.get_type_hints(entry.settings_type)


def _module_value(value: object, wanted: object, *, said: str) -> object:
    """Значение поля окна → значение поля модуля. Мост между двумя слоями.

    Перечислений в программе два комплекта: свои у окна (`ui/models.py`)
    и свои у торговых модулей. Так и задумано — `ui/` торговые слои
    не импортирует и импортировать не будет (ARCHITECTURE.md §2), — но значит,
    между ними нужен мост, и он живёт здесь: `strategies/` про окно не знает,
    `ui/` про модули не знает, а сборка знает про обоих.

    ⚠️ **Мост по имени элемента, а не таблицей пар.** Таблица пар
    (`AverageKind.EMA → StrategyAverageKind.EMA`) требует, чтобы сборка
    называла классы модуля по имени, — то есть ровно того, от чего эта работа
    избавляется. Перечисление второго алгоритма в такую таблицу никто бы
    не дописал, и его настройка **молча** не доехала бы до движка: окно
    показывало бы «Простая (SMA)», а робот считал бы EMA.

    Правило «имена элементов совпадают» держит сторож, параметризованный
    по реестру (`tests/test_strategies_registry.py`), — то есть второй
    алгоритм получает эту проверку даром. Несовпадение здесь — **отказ
    вслух**, а не умолчание: умолчание означало бы торговлю с параметром,
    которого владелец счёта не выбирал.
    """
    if not (isinstance(wanted, type) and issubclass(wanted, enum.Enum)):
        return value
    if not isinstance(value, enum.Enum):
        raise SettingsRefused(
            f"настройка «{said}» окна не перечисление ({value!r}), а торговый "
            "алгоритм ждёт выбор из списка. Обновите программу целиком."
        )
    try:
        return wanted[value.name]
    except KeyError:
        raise SettingsRefused(
            f"выбранное в окне значение настройки «{said}» — {value.name} — "
            "торговому алгоритму неизвестно. Так бывает, когда окно и алгоритм "
            "собраны из разных версий программы: работать так нельзя, робот "
            "торговал бы не тем, что выбрано. Обновите программу целиком."
        ) from None


def _window_gap(entry: registry.StrategyEntry) -> tuple[str, ...]:
    """Поля окна, которых алгоритм просит, а окна у них нет. Пусто — сходится.

    Третья сторона той же сверки: `settings_gap` и `stray_fields` реестра
    сверяют таблицу полей с самим алгоритмом, а эта — с окном. Без неё
    алгоритм, просящий поле `momentum_period`, получил бы `AttributeError`
    в середине сборки настроек, а не отказ с объяснением.
    """
    known = {field.name for field in dataclasses.fields(Settings)}
    return tuple(sorted({one.outer for one in entry.fields} - known))


#: Чем настройки алгоритма могут оказаться несобираемыми — таблицей, а не
#: тремя `if` подряд. Каждая строка: проверка и фраза с двумя подстановками.
#:
#: ⚠️ Порядок значим и потому стал данными: сначала сверяется таблица полей
#: с самим алгоритмом (обе стороны), потом с окном. Обратный порядок называл
#: бы человеку окно виноватым там, где несогласована сама таблица.
_ALGORITHM_CHECKS: Final[
    tuple[tuple[Callable[[registry.StrategyEntry], tuple[str, ...]], str], ...]
] = (
    (
        registry.StrategyEntry.settings_gap,
        "у торгового алгоритма «{title}» есть настройки, которых таблица "
        "полей не называет — {names}. Работать так нельзя: алгоритм получил "
        "бы умолчания вместо того, что выбрано в окне.",
    ),
    (
        registry.StrategyEntry.stray_fields,
        "таблица полей торгового алгоритма «{title}» называет настройки, "
        "которых у него нет — {names}. Работать так нельзя: выбранное в окне "
        "уходило бы в никуда.",
    ),
    (
        _window_gap,
        "торговому алгоритму «{title}» нужны поля настроек, которых окно "
        "не показывает — {names}. Работать так нельзя: алгоритм получил бы "
        "умолчания вместо того, что выбрано в окне.",
    ),
)


@functools.lru_cache(maxsize=None)
def _algorithm_trouble(entry: registry.StrategyEntry) -> str:
    """Почему настройки этого алгоритма собрать нельзя. Пусто — можно.

    Считается один раз на запись реестра: таблица полей и набор полей окна
    за время работы не меняются, а `strategy_settings` зовётся на каждой
    свече живого хода.
    """
    for probe, phrase in _ALGORITHM_CHECKS:
        names = probe(entry)
        if names:
            return (
                phrase.format(title=entry.title, names=", ".join(names))
                + " Обновите программу целиком."
            )
    return ""


def chosen_algorithm(values: Settings) -> registry.StrategyEntry:
    """Запись выбранного алгоритма — или отказ вслух. Молчание здесь дороже.

    Два отказа, и оба про разное.

    **Незнакомое имя.** Файл настроек или шаблон сделаны более новой сборкой,
    в которой этот алгоритм есть. Подставить умолчание значило бы торговать
    правилом, которого владелец счёта не выбирал, при исправном виде окна.

    **Знакомое имя, но настройки не собираются.** Таблица полей алгоритма
    разошлась с ним самим или с окном — разбор в `_ALGORITHM_CHECKS`.

    ⚠️ Отказ ставится **на запись реестра, а не на класс настроек**, и это
    правка `D-098`. Прежняя редакция сверяла `entry.settings_type` с классом
    настроек алгоритма №1: второй алгоритм, **переиспользующий** тот же класс
    настроек — а он самый вероятный второй, — прошёл бы молча и получил бы
    настройки, собранные по чужой таблице полей.
    """
    try:
        entry = registry.find(values.strategy_id)
    except registry.UnknownStrategy as trouble:
        raise SettingsRefused(str(trouble)) from trouble
    trouble_said = _algorithm_trouble(entry)
    if trouble_said:
        raise SettingsRefused(trouble_said)
    return entry


def strategy_settings(values: Settings) -> StrategySettings:
    """Настройки окна → настройки выбранного алгоритма. По таблице реестра.

    ⚠️ **Имени класса настроек здесь нет и быть не должно.** Поля берутся
    по таблице записи реестра (`StrategyEntry.fields`): алгоритм называет
    своё поле, поле окна, из которого оно берётся, и подпись для человека.
    До 09.09.2026 таблица стояла здесь и перечисляла поля алгоритма №1 —
    то есть сборка знала, каким правилом торгует, и при выборе второго
    алгоритма собрала бы настройки первого.

    ⚠️ Поле алгоритма, которого нет в таблице, — **отказ вслух**, а не тихое
    умолчание. Тихое умолчание здесь означает торговлю с настройками, которых
    владелец счёта не выбирал: ровно это делал сборщик с фильтром против пилы
    (`D-029`). Отказ роняет применение настроек, а не программу, — его ловит
    `HistoryPort.apply_settings` и показывает фразой.

    ⚠️ **Снятая галочка фильтра сильнее полей.** При `filter_enabled=False`
    порог и подтверждение берутся из `_FILTER_OFF`, а не из полей окна.
    Это не молчаливая подмена, которая в этом модуле запрещена: подменяется
    значение выключенной настройки, выключатель стоит рядом с полями
    на экране, а строка про его переключение уходит в журнал (`_WINDOW_TOLD`).
    Обратное — уважать поля при снятой галочке — означало бы выключатель,
    который ничего не выключает.
    """
    if _FILTER_OFF_GAP:
        raise SettingsRefused(
            "Программа собрана несогласованно: выключенный фильтр против пилы "
            f"называет поля, которых у окна нет — {', '.join(_FILTER_OFF_GAP)}. "
            "Работать так нельзя: снятая галочка перестала бы означать "
            "«выключено». Обновите программу целиком."
        )
    entry = chosen_algorithm(values)
    wanted = _module_types(entry)
    # `Any` здесь честнее любой хитрости: значения полей разного типа, и
    # соответствие имени типу проверяет сам алгоритм в своём `__post_init__` —
    # громко и с фразой (`strategies/ema_reverse.py`).
    fields: dict[str, Any] = {}
    for one in entry.fields:
        off = not values.filter_enabled and one.outer in _FILTER_OFF
        raw = _FILTER_OFF[one.outer] if off else getattr(values, one.outer)
        fields[one.name] = _module_value(
            raw, wanted.get(one.name), said=one.title
        )
    made: StrategySettings = entry.settings_type(**fields)
    return made


# ---------------------------------------------------------------------------
# Правило робота словами: единственная дорога описания из модуля в окно
# ---------------------------------------------------------------------------
#
# ⚠️ `ui/` не импортирует `strategies/` и не будет: окно берёт у торговых
# слоёв готовые значения, а не считает торговое (ARCHITECTURE.md §2).
# Описание правила — такая же готовая строка, как `backtest.headline`:
# собирает её тот, кто знает правило, а сборка только перекладывает.
#
# ⚠️ Три функции, а не одна, потому что читателя три и им нужно разное:
# окно и снимок прогона читают абзацами, журнал решений — таблица, и
# многострочный абзац в ней не читается. Все три отрисовки собираются
# из **одной** таблицы утверждений модуля, поэтому разойтись между собой
# они не могут (`strategies/contracts.py`, `Description`).


def strategy_title(values: Settings) -> str:
    """Название выбранного алгоритма для человека. Незнакомое имя — как есть.

    Отказа здесь нет намеренно, в отличие от `chosen_algorithm`: подпись
    нужна журналу и снимку прогона, а строка журнала, роняющая запись, лишает
    разбора **и** того случая, ради которого её читают.
    """
    try:
        return registry.find(values.strategy_id).title
    except registry.UnknownStrategy:
        return values.strategy_id


def algorithms(values: Settings) -> tuple[AlgorithmOption, ...]:
    """Каталог торговых алгоритмов для окна выбора — готовыми строками.

    ⚠️ Ни одна строка каталога не пишется здесь: и название, и обе отрисовки
    правила приходят из реестра, то есть от самого алгоритма. Сборка их
    только перекладывает — тот же приём, что у `backtest.headline`.

    Чьи числа стоят в `details` — зависит от того, выбран алгоритм или нет,
    и это не небрежность. У выбранного числа настоящие: они лежат в полях
    окна. У остальных полей в окне нет вовсе (хранение настроек плоское,
    §7.6 миниплана), поэтому показываются **их собственные умолчания**,
    а окно говорит это вслух. Подставить туда чужие числа значило бы
    рассказать про правило, по которому робот не работает.
    """
    return tuple(_option(entry, values) for entry in registry.entries())


def _option(entry: registry.StrategyEntry, values: Settings) -> AlgorithmOption:
    """Одна строка каталога. Ничего не решает — перекладывает готовое.

    ⚠️ Настройки для обеих отрисовок берутся **одним** решением
    (`_algorithm_settings`), а не двумя порознь. Порознь они и разъехались:
    подробное описание считалось по применённым настройкам, а правило одной
    фразой — по умолчаниям алгоритма, и в одном окне стояли два разных
    правила (`B-039`). Одна развилка на обе строки означает, что разойтись
    им теперь негде.
    """
    chosen = entry.id == values.strategy_id
    settings, refusal = _algorithm_settings(entry, values, chosen=chosen)
    return AlgorithmOption(
        id=entry.id,
        title=entry.title,
        summary=_algorithm_summary(
            entry, settings, refusal=refusal, chosen=chosen
        ),
        details=_algorithm_details(entry, settings, refusal=refusal),
        chosen=chosen,
    )


def _algorithm_settings(
    entry: registry.StrategyEntry, values: Settings, *, chosen: bool
) -> tuple[object, str]:
    """Чьи настройки показывать в каталоге и почему не ваши. Одно решение.

    Отдаёт настройки и **причину отказа словами**; пустая причина означает
    «это ваши применённые числа».

    ⚠️ Отказ сборки здесь **не роняет каталог и не прячется**. Окно выбора —
    это место, куда человек приходит разобраться; окно, упавшее вместо
    ответа, лишает его и разбора тоже. Поэтому причина возвращается текстом,
    а настройками становятся умолчания алгоритма — и обе отрисовки говорят
    вслух, что числа не ваши.

    ⚠️ Ловятся **два** вида отказа, и оба настоящие. `SettingsRefused` —
    сборка несогласована либо выбран алгоритм, для которого окно полей
    не показывает. `ValueError` — числа из файла настроек не складываются
    в рабочий алгоритм (период средней 0 в файле, правленном руками):
    проверку делает сам алгоритм в своём `__post_init__`, и она бросает
    именно его. Общий `except Exception` здесь был бы шире правды: любая
    другая поломка обязана падать громко, а не превращаться в абзац.
    """
    if not chosen:
        return entry.defaults(), ""
    try:
        return strategy_settings(values), ""
    except (SettingsRefused, ValueError) as refusal:
        return entry.defaults(), str(refusal)


#: Оговорка для невыбранного алгоритма: числа в его описании — не ваши.
#:
#: ⚠️ Показывать умолчания чужого алгоритма честно (полей его в окне нет,
#: брать неоткуда), но молча — нет: правило читается как «вот что будет
#: у меня». До 09.09.2026 оговорка стояла только над подробным описанием
#: (`ui/algorithm_dialog.py::details_preamble`), а строка списка молчала.
_NOT_YOUR_NUMBERS: Final[str] = (
    "⚠️ Это правило с умолчаниями самого алгоритма: ваших настроек в нём нет — "
    "полей этого алгоритма окно пока не показывает."
)

#: Та же оговорка, когда настройки не собрались у **выбранного** алгоритма.
#: Причина названа отдельно в подробном описании, здесь — только факт.
_YOUR_NUMBERS_REFUSED: Final[str] = (
    "⚠️ Собрать ваши настройки не удалось, правило показано с умолчаниями "
    "алгоритма. Причина — в «Подробнее»."
)


def _algorithm_summary(
    entry: registry.StrategyEntry,
    settings: object,
    *,
    refusal: str,
    chosen: bool,
) -> str:
    """Правило одной фразой и без чисел — строка списка и всплывающая подсказка.

    Чисел в этой отрисовке нет, но **формулировки** зависят от настроек:
    включённый порог превращает «закрытие выше средней» в «закрытие выше
    полосы вокруг средней», подтверждение сигнала — в «несколько закрытий
    подряд выше…», а поведение на равенстве меняет намерение в третьем
    утверждении. Поэтому настройки сюда приходят те же, что в подробное
    описание, и той же дорогой (`B-039`).

    ⚠️ Оговорка про чужие числа — часть строки, а не украшение. Строка
    показывается в списке выбора и подсказкой на вкладке настроек; там,
    где она читается как «вот что будет у меня», умолчания чужого алгоритма
    обязаны быть названы умолчаниями.
    """
    said = entry.summary(settings)
    if refusal:
        return f"{said} {_YOUR_NUMBERS_REFUSED}"
    if not chosen:
        return f"{said} {_NOT_YOUR_NUMBERS}"
    return said


def _algorithm_details(
    entry: registry.StrategyEntry, settings: object, *, refusal: str
) -> str:
    """Правило абзацами: у выбранного — с вашими числами, у прочих — со своими.

    Развилку «чьи числа» решает `_algorithm_settings`; здесь только отрисовка
    и оговорка. Две развилки на две отрисовки — это и был `B-039`.
    """
    said = entry.description(settings).full()
    if refusal:
        return (
            "⚠️ Показать правило с вашими числами не удалось: программа "
            f"отказалась собрать настройки. {refusal}\n\n"
            "Ниже — то же правило с умолчаниями самого алгоритма. "
            "Ваши числа в нём не учтены.\n\n" + said
        )
    return said


def rule_of(entry: registry.StrategyEntry, module: object) -> str:
    """Правило выбранного алгоритма словами, абзацами, с нынешними числами.

    ⚠️ Описание берётся у **поданной записи**, а не у `default_entry()`, и это
    правка `D-098`. Прежняя редакция всегда спрашивала алгоритм по умолчанию:
    при выбранном втором алгоритме снимок прогона и журнал решений
    рассказывали бы правило первого — молча и убедительно.

    Настройки **алгоритма** на входе, а не окна: то же описание нужно снимку
    настроек прогона (`app/runs.py::settings_text`), а туда доезжают уже
    переведённые настройки, окна там нет. Чужие настройки запись отвергает
    сама (`StrategyEntry._own`).
    """
    return entry.description(module).full()


def rule_headline_of(entry: registry.StrategyEntry, module: object) -> str:
    """То же правило одной строкой — для журнала решений."""
    return entry.description(module).headline()


def strategy_rule(values: Settings) -> str:
    """Настройки окна → правило робота словами. То, что читает человек.

    ⚠️ Считается по **применённым** настройкам, а не по тому, что стоит
    в полях: пока «Применить» не нажато, робот работает по прежним, и
    описание, дорисованное на каждый щелчок, обещало бы поведение, которого
    сейчас нет. Дорисовывать его в окне значило бы завести вторую сборку
    настроек модуля рядом с этой — и разошлись бы они молча.
    """
    return rule_of(chosen_algorithm(values), strategy_settings(values))


def rule_changes(previous: Settings, now: Settings) -> list[str]:
    """Что сказать журналу про правило: строки «было → стало» и новое правило.

    ⚠️ **Строки изменений составляет сам алгоритм**, и только по однородному:
    `changes_from` принимает настройки того же алгоритма. При смене самого
    алгоритма прежние настройки принадлежат другому классу, и сравнивать их
    поле в поле нельзя — «период средней 15 → 15» про два разных правила
    было бы неправдой, а падение здесь лишило бы журнал строки ровно в тот
    день, когда её читают.

    ⚠️ Строка «правило теперь читается так» ставится и тогда, когда настройки
    совпали до поля: у двух алгоритмов настройки бывают одинаковыми по полям
    и разными по смыслу. Имя алгоритма своей строкой в журнал уже попало
    (`_WINDOW_TOLD`), но имя — это «что поменялось», а не «во что оно
    превратилось»: через месяц разбирают именно правило.
    """
    entry = chosen_algorithm(now)
    module = strategy_settings(now)
    same = previous.strategy_id == now.strategy_id
    said = module.changes_from(strategy_settings(previous)) if same else []
    if said or not same:
        said.append(f"Правило теперь читается так — {rule_headline_of(entry, module)}")
    return said


def run_costs(values: Settings) -> Costs:
    """Настройки окна → издержки прогона: комиссия, шаг цены, проскальзывание.

    Комиссия здесь та же, что уходит в движок: `replay` сверяет обе и отвергает
    прогон, если они разошлись. Проскальзывания у движка нет вовсе — оно
    величина только отчёта (`backtest/history.py`).

    ⚠️ Умолчание — **ноль шагов**, и за ним стоит сверка с прототипом. Ноль
    при этом не «мелочь»: замер 05.09.2026 на MXU6 — 0 шагов дают +9 908 ₽,
    1 шаг +4 394 ₽, 2 шага −153 ₽.

    ⚠️ Проскальзывание без шага цены — **отказ, а не ноль**. Иначе настройка
    выглядит заданной и не делает ничего: владелец счёта поставил бы поправку
    и продолжил смотреть на прибыль без неё. Условие ровно то же, что
    у `backtest.Costs.__post_init__`, — источник правды один, здесь только
    перевод отказа на человеческий язык.
    """
    if values.slippage_steps > 0 and values.price_step <= 0:
        raise SettingsRefused(
            f"Проскальзывание {values.slippage_steps} шагов задано, а шаг цены "
            "инструмента не назван. Так поправка не сработает вовсе, а отчёт "
            "выглядел бы посчитанным с ней. Впишите шаг цены в настройках — "
            "он есть в карточке инструмента на сайте биржи (у фьючерса "
            "на индекс МосБиржи это 25 ₽)."
        )
    try:
        return Costs(
            commission_per_side=values.commission_per_side_rub,
            price_step=values.price_step,
            slippage_steps=values.slippage_steps,
        )
    except ValueError as error:
        raise SettingsRefused(
            f"Издержки прогона не годятся: {error}. Прежние настройки "
            "остались в силе."
        ) from None


# ---------------------------------------------------------------------------
# Строки журнала об изменении настроек, о которых не расскажет никто другой
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _Told:
    """Как назвать поле окна в журнале и как показать его значение."""

    label: str
    #: `Any`, а не `object`: сюда подставляются готовые оформители чисел
    #: (`fmt_percent`, `fmt_volume`), и обёртка вокруг каждого ради подписи
    #: была бы третьим местом, где число превращается в текст.
    show: Callable[[Any], str]
    #: Поле предохранителя: строка уходит **предупреждением** и вместе с тем,
    #: что предохранитель ещё не включён. Тихое «принято» читалось бы как
    #: «ограничение поставлено», а цена ошибки здесь — деньги на счёте.
    guard: bool = False


def _as_text(value: object) -> str:
    """Строка как есть. Пустая — словом, а не пустым местом в журнале."""
    return str(value) if str(value) else "не задано"


def _as_steps(value: float) -> str:
    """Проскальзывание: число и единица, чтобы шаги не спутать с рублями."""
    return f"{fmt_number(value)} шагов цены"


def _as_days(value: object) -> str:
    """Глубина показа: ноль означает «вся история», а не «ноль дней»."""
    return "вся история" if value == 0 else f"{value} дн."


def _as_load_days(value: object) -> str:
    """Глубина загрузки: всегда число дней.

    Отдельно от `_as_days`, а не то же самое: у глубины **показа** ноль
    означает «вся история», а у глубины **загрузки** нуля нет вовсе —
    сколько именно отдаст биржа, до запроса неизвестно (`ui.models.Settings`).
    Общая функция превратила бы пришедший из старого файла ноль в обещание
    «скачаем всё», которого никто не давал.
    """
    return f"{value} дн."


def _as_source(value: object) -> str:
    """Происхождение стоимости пункта. Пусто — не подтверждено, и так и сказано."""
    return str(value) if str(value) else "не подтверждено биржей"


def _as_switch(value: object) -> str:
    """Выключатель словом. `True`/`False` в журнале решений читать невозможно."""
    return "включён" if value else "выключен"


def _as_algorithm(value: object) -> str:
    """Имя алгоритма латиницей → его название. Незнакомое — как есть, и вслух.

    Отказа здесь нет: строка журнала, роняющая запись, лишила бы разбора
    ровно тот случай, ради которого её читают. Незнакомое имя названо
    и помечено — оно и есть та новость, за которой пришли в журнал.
    """
    try:
        return f"«{registry.find(str(value)).title}»"
    except registry.UnknownStrategy:
        return f"«{value}» — алгоритма с таким именем в этой сборке нет"


#: Поля окна, о которых рассказывает **порт**, потому что больше некому.
#:
#: Таблица, а не цепочка `if`, и это не вкус. 05.09.2026 к настройкам разом
#: добавились шесть полей, и все шесть менялись бы в журнале молча: строка
#: «Значения совпали с прежними» после смены глубины, шага цены или
#: проскальзывания (ТЗ §4.4 А требует обратного — строку с прежним и новым
#: значением на каждое изменение).
_WINDOW_TOLD: dict[str, _Told] = {
    "instrument": _Told("Инструмент", _as_text),
    "timeframe": _Told("Размер свечи", _as_text),
    # ⚠️ Смена алгоритма — самое крупное изменение, какое здесь бывает:
    # меняется не число в правиле, а само правило. Строка обязана называть
    # прежнее и новое **название**, а не имя латиницей из файла настроек:
    # в журнале это читает человек (ТЗ §4.4 А).
    #
    # ⚠️ Само правило словами дописывает порт отдельной строкой
    # (`HistoryPort._apply` → `rule_headline_of`): здесь названо, ЧТО
    # поменялось, там — во что оно превратилось.
    "strategy_id": _Told("Торговый алгоритм", _as_algorithm),
    "depth_days": _Told("Глубина показа", _as_days),
    # ⚠️ Своя строка, а не общая с глубиной показа. Это разные числа: одно
    # говорит, сколько показывать из имеющегося, другое — сколько скачать
    # с биржи. Общая строка в журнале не дала бы ответить, что именно
    # владелец счёта поменял (`D-068`).
    "history_depth_days": _Told("Глубина загрузки истории", _as_load_days),
    "price_step": _Told("Шаг цены инструмента", fmt_number),
    # ⚠️ Само число рассказывает движок (`ruble_per_point` — его поле).
    # Здесь — только **происхождение**: «подсказано биржей» или «ввод руками».
    # Отдельная строка нужна потому, что величина плавающая: одно и то же
    # число, подтверждённое биржей вчера и введённое руками сегодня, —
    # разные основания доверять деньгам отчёта.
    "ruble_per_point_source": _Told("Стоимость пункта, источник", _as_source),
    "slippage_steps": _Told("Проскальзывание", _as_steps),
    "log_directory": _Told("Каталог технического журнала", _as_text),
    # ⚠️ Выключатель фильтра называет **себя**, а числа фильтра называет
    # торговый модуль (`_TOLD_BY_STRATEGY`) — и называет **действующие**:
    # при снятой галочке до модуля доходят его умолчания, а не то, что стоит
    # в полях. Отсюда читаемая пара строк на включение: «Фильтр против пилы:
    # выключен → включён; Порог пересечения: 0,00% → 0,04%». Правка числа
    # при снятой галочке строки не даёт, и это верно: поведение робота
    # от неё не меняется ничем.
    "filter_enabled": _Told("Фильтр против пилы", _as_switch),
    # Три предохранителя: у каждого галочка и число, и в журнал идут оба.
    # Одна галочка без числа читалась бы как «включил и всё» — а включение
    # без числа не значит ничего; одно число без галочки читалось бы как
    # «ограничение поставлено» — а оно не поставлено, пока галочка снята.
    "volume_cap_enabled": _Told("Потолок объёма", _as_switch, guard=True),
    "volume_cap": _Told("Потолок объёма, контрактов", fmt_volume, guard=True),
    "daily_loss_limit_enabled": _Told(
        "Дневной лимит убытка", _as_switch, guard=True
    ),
    "daily_loss_limit_pct": _Told(
        "Дневной лимит убытка, % от счёта", fmt_percent, guard=True
    ),
    "free_funds_reserve_enabled": _Told(
        "Запас свободных средств", _as_switch, guard=True
    ),
    "free_funds_reserve_pct": _Told(
        "Запас свободных средств, %", fmt_percent, guard=True
    ),
}

#: Поля окна, о которых расскажет **движок** (`EngineSettings.changes_from`):
#: они переводятся в его настройки, и строку пишет он.
_TOLD_BY_ENGINE: frozenset[str] = frozenset({
    "window_start", "window_end", "close_on_time_end", "volume",
    "reversal_moment", "take_profit_enabled", "take_profit_pct",
    "trailing_enabled", "trailing_start_pct", "trailing_offset_pct",
    "trailing_step_pct", "after_take_profit", "commission_per_side_rub",
    "ruble_per_point",
    # Календарь нерабочих дней: отметки уезжают в `EngineSettings.calendar`,
    # и строку «Календарь нерабочих дней: нет → 12.06.2026 не торгуем» пишет
    # движок. Своей строки в окне у него нет намеренно — двух строк об одном
    # изменении быть не должно.
    "calendar",
})

def _told_by_strategy() -> frozenset[str]:
    """Поля окна, о которых расскажет сам алгоритм (`changes_from`).

    ⚠️ Считается **по таблицам реестра**, а не перечисляется руками. Пять имён
    здесь были шестым списком тех же полей: список алгоритма, сборщик
    настроек, подписи снимка прогона, этот — и правые половины не сверяло
    ничто (`D-100`). Поле, добавленное алгоритму и забытое здесь, ловится
    теперь по построению.

    ⚠️ Берётся объединение по **всем** записям реестра, а не по выбранной,
    и цена названа вслух: поле, которое читает только второй алгоритм,
    при выбранном первом считается «рассказанным», хотя строки в журнале
    не будет — менять его при выбранном первом бессмысленно, робот его
    не читает. Правильный ответ на это — настройки алгоритма отдельным
    набором (§7.6 миниплана `strategy-modules-switchable.md`), а не список
    руками: список руками врал бы так же и вдобавок молча.
    """
    return frozenset(
        one.outer for entry in registry.entries() for one in entry.fields
    )


#: Считается один раз, при импорте: реестр за время работы не меняется.
_TOLD_BY_STRATEGY: Final[frozenset[str]] = _told_by_strategy()


def _as_confirm(value: object) -> str:
    """Подтверждение сигнала: свечей подряд, с оговоркой про единицу."""
    return "1 свеча (подтверждения нет)" if value == 1 else f"{value} свечей подряд"


#: Числа фильтра против пилы, **пока выключатель снят**.
#:
#: Про них рассказывает торговый модуль — но только когда фильтр включён:
#: при снятой галочке до модуля доходят его умолчания (`_FILTER_OFF`),
#: и модуль честно не видит никакой перемены. Строку в журнале это оставляло
#: без строки вовсе, а ТЗ §4.4 А требует строку на **каждое** изменение поля
#: окна — и сторож `test_a_change_of_any_settings_field_reaches_the_journal`
#: ловит ровно это.
#:
#: Молчание тут не безобидно: владелец счёта подбирает порог при снятой
#: галочке, потом включает её — и восстановить, что он крутил, нечем.
#: Поэтому строка есть, и она **сразу говорит, что число ни на что
#: не влияет**: иначе она читалась бы как включённый фильтр.
_TOLD_WHILE_THE_FILTER_IS_OFF: dict[str, _Told] = {
    "threshold_percent": _Told("Порог пересечения", fmt_percent),
    "confirm_bars": _Told("Подтверждение сигнала", _as_confirm),
}


def _silent_fields() -> tuple[str, ...]:
    """Поля окна, о которых не расскажет никто. Пусто — про все есть строка."""
    known = {field.name for field in dataclasses.fields(Settings)}
    named = set(_WINDOW_TOLD) | _TOLD_BY_ENGINE | _TOLD_BY_STRATEGY
    return tuple(sorted(known - named))


#: Считается один раз, при импорте.
_SILENT_FIELDS: tuple[str, ...] = _silent_fields()


def _changes(previous: Settings, now: Settings, *, guard: bool) -> list[str]:
    """Изменённые поля одной половины таблицы — строками «было → стало»."""
    lines = [
        f"{told.label}: {told.show(getattr(previous, name))} → "
        f"{told.show(getattr(now, name))}"
        for name, told in _WINDOW_TOLD.items()
        if told.guard is guard and getattr(previous, name) != getattr(now, name)
    ]
    return lines


def _muted_filter_changes(previous: Settings, now: Settings) -> list[str]:
    """Правка чисел фильтра при снятой галочке — строкой, и сразу с оговоркой.

    Пусто, если галочка была или стала поднятой: тогда перемену увидел
    и назвал сам торговый модуль, и вторая строка про то же была бы
    двойным учётом.
    """
    if previous.filter_enabled or now.filter_enabled:
        return []
    return [
        f"{told.label} (фильтр выключен, на робота не влияет): "
        f"{told.show(getattr(previous, name))} → {told.show(getattr(now, name))}"
        for name, told in _TOLD_WHILE_THE_FILTER_IS_OFF.items()
        if getattr(previous, name) != getattr(now, name)
    ]


def window_changes(previous: Settings, now: Settings) -> list[str]:
    """Изменения полей, о которых не расскажут ни движок, ни торговый модуль.

    Инструмент, размер свечи, глубина показа, шаг цены, проскальзывание
    и каталог лога не существуют ни в `EngineSettings`, ни
    в настройках алгоритма. Пока строки не было, смена инструмента давала
    в журнал «Значения совпали с прежними», после чего программа показывала
    другой инструмент.

    Формат — тот же, что у движка: «Название: было → стало» (ТЗ §4.4 А).

    ⚠️ Поле, не названное ни в одной из трёх таблиц, попадает в журнал
    **отдельной строкой про само себя**. Уронить прогон из-за отсутствующей
    подписи было бы несоразмерно, а промолчать нельзя: молчание здесь
    означает изменение параметра по деньгам, которого нет в журнале.
    """
    lines = _changes(previous, now, guard=False)
    lines += _muted_filter_changes(previous, now)
    if _SILENT_FIELDS and any(
        getattr(previous, name) != getattr(now, name) for name in _SILENT_FIELDS
    ):
        lines.append(
            "⚠️ Программа не умеет назвать изменения полей: "
            + ", ".join(_SILENT_FIELDS)
            + ". Они применены, но что именно поменялось — в журнале не видно"
        )
    return lines


def guard_changes(previous: Settings, now: Settings) -> list[str]:
    """Изменения полей предохранителей — отдельными строками.

    Отдельными потому, что читать их надо иначе, чем прочие настройки:
    это границы, за которые робот не выйдет, и цена ошибки здесь — деньги
    на счёте. Порт выводит их уровнем «предупреждение».

    ⚠️ Число само по себе ничего не ограничивает: предохранитель работает
    ровно тогда, когда стоит его галочка (`_GUARDS`). Поэтому строка
    про галочку идёт **вместе** со строкой про число, и обе — здесь.
    """
    return _changes(previous, now, guard=True)


#: Поля окна, за которыми стоят **деньги на счёте**: объём сделки, потолок
#: объёма, дневной лимит убытка, запас свободных средств, тариф комиссии.
#:
#: Отдельный список нужен ровно для одного: подтверждение «было → стало»
#: показывает эти строки первыми, крупнее и цветом. Смена периода средней
#: и смена объёма сделки читаются по-разному, и складывать их в один список
#: значит приучить человека жать «Да» не читая.
#:
#: ⚠️ Галочка предохранителя идёт вместе со своим числом: включённый потолок
#: без числа не значит ничего, а число при снятой галочке не ограничивает
#: ничего. Показать одно без другого — соврать.
MONEY_FIELDS: Final[frozenset[str]] = frozenset({
    "volume",
    "volume_cap_enabled",
    "volume_cap",
    "daily_loss_limit_enabled",
    "daily_loss_limit_pct",
    "free_funds_reserve_enabled",
    "free_funds_reserve_pct",
    "commission_per_side_rub",
})

#: Режим, на котором собираются обе стороны сравнения. Любой: он одинаков
#: у «было» и у «стало», поэтому строки про него не появится вовсе. Режим
#: живёт на панели управления, а не в окне настроек, и шаблон его не несёт.
_DIFF_MODE: Final[Mode] = Mode.REVERSE


def all_changes(previous: Settings, now: Settings) -> tuple[list[str], str]:
    """Все строки «было → стало» для пары наборов — из тех же трёх источников.

    Те же самые, что уходят в журнал решений (`HistoryPort.apply_settings`):
    окно (`window_changes`, `guard_changes`), движок и торговый модуль. Второе
    описание тех же изменений разошлось бы с первым молча, а расходиться ему
    ровно там, где человек подтверждает изменение параметра по деньгам.

    Вторым значением — причина, по которой перечислить не удалось. Настройки
    приходят из файла шаблона и могут быть негодны: подтверждение обязано
    сказать об этом, а не показать пустой список, читаемый как «ничего
    не меняется».
    """
    try:
        engine_was = engine_settings(previous, _DIFF_MODE)
        engine_now = engine_settings(now, _DIFF_MODE)
        module_was = strategy_settings(previous)
        module_now = strategy_settings(now)
    except (SettingsRefused, ValueError, TypeError) as error:
        return [], str(error)
    lines = window_changes(previous, now)
    lines += engine_now.changes_from(engine_was)
    lines += module_now.changes_from(module_was)
    lines += guard_changes(previous, now)
    return lines, ""


def settings_diff(previous: Settings, now: Settings) -> SettingsDiff:
    """Что изменится при применении: отдельно деньги, отдельно всё остальное.

    Разделение сделано **подменой значений, а не разбором строк**. Строку
    пишет тот, кто знает поле (движок, торговый модуль, окно), и вытаскивать
    имя поля обратно из готовой фразы значило бы завести третье знание
    о том, как эта фраза устроена.

    Приём: собираются два промежуточных набора — «изменились только деньги»
    и «изменилось всё, кроме денег», — и каждый сравнивается с исходным.
    Строка появляется тогда и только тогда, когда её поле отличается, значит
    две половины в сумме дают в точности полный перечень. Это проверяется
    тестом, а не обещанием.
    """
    money_now = previous.replace(
        **{name: getattr(now, name) for name in MONEY_FIELDS}
    )
    rest_now = now.replace(
        **{name: getattr(previous, name) for name in MONEY_FIELDS}
    )
    money, trouble = all_changes(previous, money_now)
    rest, rest_trouble = all_changes(previous, rest_now)
    return SettingsDiff(
        money=tuple(money),
        other=tuple(rest),
        trouble=trouble or rest_trouble,
    )


def side_of(side: EngineSide) -> Side:
    """Сторона сделки движка → сторона окна."""
    return _SIDES[side]


def average_label(values: Settings) -> str:
    """Подпись линии на графике: `EMA(15) — линия торгового модуля`.

    ⚠️ Подпись собирает **сам алгоритм** (`StrategySettings.label`), а не
    сборка: как называется линия, знает тот, кто её считает. Прежняя редакция
    складывала её здесь из полей окна и короткого имени средней — то есть
    сборка знала, что алгоритм считает именно среднюю. Алгоритм, рисующий
    не среднюю, подписал бы линию «EMA(15)» и не упал бы.

    Пустая подпись — законный ответ: у алгоритма без линии подписывать нечего,
    и окно её тогда не показывает. Отказ собрать настройки подписью тоже
    не является: график рисуется и без неё, а причина отказа человеку уже
    сказана окном настроек, и второй раз в углу графика она не нужна.
    """
    try:
        short = strategy_settings(values).label
    except (SettingsRefused, ValueError):
        return ""
    return f"{short} — линия торгового модуля" if short else ""


def mode_of(mode: EngineMode) -> Mode:
    """Режим движка → режим окна. Обратный перевод, тоже явный."""
    for window_mode, engine_mode in _MODES.items():
        if engine_mode is mode:
            return window_mode
    raise SettingsRefused(f"режим движка «{mode}» окну неизвестен")


# ---------------------------------------------------------------------------
# Данные → то, что рисует окно
# ---------------------------------------------------------------------------

def window_candle(candle: MarketCandle) -> Candle:
    """Свеча слоя данных → свеча окна. **Единственное место перевода.**

    На ось идёт **начало** интервала: биржевая свеча подписывается временем
    открытия, и терминал брокера рисует её так же. Пятиминутка 12:55–13:00
    стоит на отметке 12:55 в обеих программах — иначе сверка глазами,
    ради которой график и делается, невозможна.

    ⚠️ Время закрытия (`engine.close_time`) сюда не идёт и не должно: по нему
    движок решает, попала ли свеча в торговое окно, и это отдельное
    соглашение (ARCHITECTURE.md §6). Оно осталось нетронутым, переехала
    только подпись на оси.
    """
    return Candle(
        opens_at=candle.time,
        open=candle.open,
        high=candle.high,
        low=candle.low,
        close=candle.close,
        volume=candle.volume,
    )


def average_line(
    points: Sequence[tuple[datetime, float]], timeframe: Timeframe
) -> tuple[LinePoint, ...]:
    """Линия средней. Значения пришли от торгового модуля — здесь только форма.

    Время у точки — закрытие свечи, на которой значение посчитано
    (`backtest/history.py`): торговый модуль живёт в соглашении движка.
    На оси та же свеча стоит своим открытием, поэтому точка переводится
    `chart_time`. Без перевода линия средней сползла бы на бар вправо
    относительно свечей — и на глаз выглядела бы как опережение сигнала.
    """
    return tuple(
        LinePoint(time=chart_time(moment, timeframe), value=value)
        for moment, value in points
    )


def decision_row(entry: JournalEntry, *, origin: RunOrigin | None = None) -> DecisionRow:
    """Строка журнала движка → строка журнала окна. Текст не трогаем.

    Причина приходит готовой фразой из `engine/`. Окно её не сочиняет,
    не сокращает и не подменяет кодом — здесь тоже.

    ⚠️ `origin` называет тот, кто затеял прогон: движок про боевой режим
    и симуляцию не знает и знать не должен (ARCHITECTURE.md §1). Не назвали —
    строка так и уедет в выгрузку с пометкой «прогон не назван»: подставить
    сюда «боевой режим» по умолчанию значило бы соврать в отчёте, по которому
    выбирают объём.
    """
    return DecisionRow(
        time=entry.at,
        event=entry.event,
        reason=entry.reason,
        level=_LEVELS[entry.level],
        origin=origin,
    )


def trade_row(deal: Deal, *, origin: RunOrigin | None = None) -> TradeRow:
    """Сделка модели исполнения → строка журнала сделок.

    ⚠️ `profit_rub` — **чистый результат сделки**, если тариф комиссии задан,
    и валовый, если нет. Второе видно по пустой колонке «Комиссия»: `None`
    там означает «тариф не задан», а не «комиссия нулевая». Подставить ноль
    значило бы объявить любую сделку окупившей комиссию (DOMAIN.md §5).

    `profit_pct` — движение цены в процентах от цены входа. Комиссия в процент
    не входит: это движение рынка, а не результат кошелька.

    ⚠️ `origin` — см. `decision_row`: происхождение называет тот, кто затеял
    прогон, а не сделка. Не назвали — так и будет написано в выгрузке.
    """
    return TradeRow(
        entry_time=deal.entry_time,
        exit_time=deal.exit_time,
        side=_SIDES[deal.side],
        volume=deal.volume,
        entry_price=deal.entry_price,
        exit_price=deal.exit_price,
        exit_reason=deal.exit_reason.label,
        profit_rub=deal.net if deal.net is not None else deal.gross,
        profit_pct=deal.percent,
        commission_rub=deal.commission,
        trade_id=deal.entry_order_id,
        origin=origin,
    )


def decision_record(entry: JournalEntry) -> DecisionRecord:
    """Строка журнала движка → строка для базы. Текст не трогаем.

    ⚠️ Перекладка нужна не для красоты. `market/` не импортирует ни один слой
    проекта (ARCHITECTURE.md §2), поэтому `engine.JournalEntry` дойти до базы
    сам не может; типы у слоя данных свои, и совпадением форм здесь никто
    не пользуется — ровно как со свечой, чьё структурное соответствие уже
    один раз сдвинуло весь график на бар.
    """
    return DecisionRecord(
        at=entry.at,
        event=entry.event,
        reason=entry.reason,
        level=_STORED_LEVELS[entry.level],
    )


def trade_record(deal: Deal, symbol: str) -> TradeRecord:
    """Сделка прогона → запись журнала сделок.

    ⚠️ `gross` и `net` кладутся **посчитанными**, а не восстанавливаются
    при чтении. Журнал — свидетельство: он хранит то, что было посчитано
    в тот день, тем тарифом и тем весом пункта. Пересчёт при чтении менял бы
    прошлые сделки при смене тарифа, то есть переписывал бы журнал задним
    числом.

    `net is None` при незаданном тарифе — не ноль. Ноль объявил бы любую
    сделку окупившей комиссию (DOMAIN.md §5).

    `symbol` отдельным доводом: у `Deal` инструмента нет — прогон идёт
    по одному инструменту, и знает его тот, кто прогон затеял.
    """
    return TradeRecord(
        symbol=symbol,
        side=_STORED_SIDES[deal.side],
        volume=deal.volume,
        entry_time=deal.entry_time,
        entry_price=deal.entry_price,
        entry_order_id=deal.entry_order_id,
        exit_time=deal.exit_time,
        exit_price=deal.exit_price,
        exit_order_id=deal.exit_order_id,
        exit_reason=deal.exit_reason.label,
        gross=deal.gross,
        commission=deal.commission,
        net=deal.net,
        ruble_per_point=deal.ruble_per_point,
    )


def row_of_stored_decision(stored: StoredDecision) -> DecisionRow:
    """Строка журнала решений из базы → строка журнала окна.

    ⚠️ Происхождение прогона едет вместе со строкой и **не теряется здесь**.
    Прежде терялось: у строки окна такого поля не было, и гарантия «по строке
    видно, боевая ли она» умирала ровно на этой перекладке — в выгрузке,
    то есть в том самом отчёте за день, из числа сделок в котором владелец
    счёта выбирает объём (решение 0011, задача Э1-10б).

    Окно при этом по-прежнему показывает журнал **одного** прогона за раз:
    поле — про честность строки, а не разрешение мешать прогоны в одной
    таблице. Отбор делает `CandleStore.decisions`, и он же теперь без явной
    просьбы не смешивает.
    """
    return DecisionRow(
        time=stored.at,
        event=stored.event,
        reason=stored.reason,
        level=_LEVELS_OF_STORED[stored.level],
        origin=_ORIGINS_OF_STORED[stored.origin],
    )


def row_of_stored_trade(stored: StoredTrade) -> TradeRow:
    """Сделка из базы → строка журнала сделок окна.

    Числа берутся записанными, а не считаются заново, — по той же причине,
    по какой они записаны: тариф с тех пор мог измениться.

    Происхождение прогона едет со сделкой — разбор в `row_of_stored_decision`.
    """
    return TradeRow(
        entry_time=stored.entry_time,
        exit_time=stored.exit_time,
        side=_SIDES_OF_STORED[stored.side],
        volume=stored.volume,
        entry_price=stored.entry_price,
        exit_price=stored.exit_price,
        exit_reason=stored.exit_reason,
        profit_rub=stored.net if stored.net is not None else stored.gross,
        profit_pct=stored.percent,
        commission_rub=stored.commission,
        trade_id=stored.entry_order_id,
        origin=_ORIGINS_OF_STORED[stored.origin],
    )


def trades_summary(run: HistoryRun) -> TradesSummary:
    """Итог прогона → итог под таблицей. Ни одного числа заново.

    ⚠️ На вход идёт **прогон целиком**, а не его `Summary`, и это не удобство.
    Вместе с деньгами отсюда уезжает оговорка (`backtest.headline`): чем
    показанный итог отличается от выписки со счёта. Оговорка считается из
    самого прогона — из издержек, числа исполнений, выходов по уровню
    и отрезка дат, — и по одному `Summary` её не получить.
    Разъехаться с числом она теперь не может: место у неё то же поле того же
    объекта, который несёт прибыль (решение 0030).
    """
    summary = run.summary
    return TradesSummary(
        headline=headline(run),
        trades=summary.trades,
        profitable_share=summary.profitable_share,
        net_profit_rub=summary.net_profit,
        gross_profit_rub=summary.gross_profit,
        commission_rub=summary.commission,
        max_drawdown_rub=summary.max_drawdown,
        reversals=summary.reversals,
        profit_factor=summary.profit_factor,
    )


def shades_of(
    candles: Sequence[MarketCandle], settings: EngineSettings
) -> tuple[Shade, ...]:
    """Затенить время, в которое робот молчал.

    Вопрос «внутри ли окна» задаётся **самому движку** — `TradingWindow.contains`
    и `is_trading_day`, — а не решается здесь заново. Граница окна считается
    по времени закрытия свечи, неравенства строгие; вторая реализация того же
    правила разошлась бы с первой молча, и на графике была бы затенена
    не та половина (ARCHITECTURE.md §6).

    ⚠️ Здесь два времени одной свечи, и путать их нельзя. **Решает** движок
    по закрытию (`closes_at`), **ставится** полоса по открытию (`candle.time`),
    потому что так размечена ось (`window_candle`). Взять закрытие и для
    подписи — значит сдвинуть полосу на бар: она начнётся там, где стоит
    метка «Выход по концу окна», и накроет свечу, на которой робот ещё
    торговал. Взять открытие и для решения — значит сдвинуть на бар само
    торговое окно и разойтись с журналом решений.

    ⚠️ Следствие, которое здесь не лечится: молчание длиной **в одну свечу**
    даёт полосу нулевой ширины — обе границы совпадают, и на экране её не
    видно. Ширина свечи известна только рисующему слою (`ui/chart/`): он
    ставит свечу в её индекс на оси, и раздвинуть полосу на полсвечи в обе
    стороны может только он. Отсюда это чинить нельзя — получилась бы вторая
    разметка оси, живущая по своим правилам. Долг за `ui/chart/`.

    Причин молчать несколько — вне окна, выходной, отметка владельца счёта,
    закрытая биржа, — и полоса называет ту, которая была. Различает их движок
    (`day_verdict`), не мы: вторая реализация правила разошлась бы с первой
    молча, и на графике была бы затенена не та половина.
    """
    if not candles:
        return ()
    shades: list[Shade] = []
    start: datetime | None = None
    end: datetime | None = None
    outside = False
    rules: set[DayRule] = set()
    for candle in candles:
        closes_at = close_time(candle)
        # ⚠️ Окно «весь день» (начало равно концу) отдельной проверки не имеет
        # намеренно: `contains` для него возвращает True всегда, и единственной
        # причиной молчания остаётся нерабочий день. Ранний возврат по
        # `whole_day` стоял выше и оставлял график полностью незатенённым —
        # включая выходные, про которые журнал в тот же момент писал
        # «торговля в выходные выключена».
        inside = settings.window.contains(closes_at)
        verdict = day_verdict(
            closes_at,
            trade_in_weekend=settings.trade_in_weekend,
            calendar=settings.calendar,
            exchange=settings.exchange_days,
        )
        if not inside or not verdict.trading:
            # Решение — по закрытию (выше), отметка на оси — по открытию.
            start = candle.time if start is None else start
            end = candle.time
            outside = outside or not inside
            if not verdict.trading:
                rules.add(verdict.rule)
            continue
        if start is not None and end is not None:
            shades.append(Shade(
                start, end, ShadeKind.OUTSIDE_WINDOW, _quiet(settings, outside, rules)
            ))
        start = end = None
        outside = False
        rules = set()
    if start is not None and end is not None:
        shades.append(Shade(
            start, end, ShadeKind.OUTSIDE_WINDOW, _quiet(settings, outside, rules)
        ))
    return tuple(shades)


#: Почему день оказался нерабочим — словами для подписи полосы. Таблица,
#: а не тройка `if`: правило, заведённое в `engine/window.py` завтра, ловится
#: проверкой полноты (`tests/test_app_convert.py`), а не молчаливым «выходной»
#: под днём, который владелец счёта пометил сам.
_QUIET_OF_RULE: dict[DayRule, str] = {
    DayRule.WEEKEND: "торговля в выходные выключена",
    DayRule.OWNER_OFF: "вы пометили эти дни нерабочими",
    DayRule.EXCHANGE_CLOSED: "биржа в эти дни не работает",
}


def _quiet(settings: EngineSettings, outside: bool, rules: set[DayRule]) -> str:
    """Подпись полосы: почему робот здесь молчал.

    Про выход по концу окна говорится только когда он включён: строка
    «позиция закрывается по концу окна» при выключенном `close_on_time_end`
    обещала бы выход, которого не будет.

    Порядок причин в подписи — порядок таблицы, а не порядок обхода набора:
    одна и та же полоса обязана подписываться одинаково от прогона к прогону.
    """
    if rules and outside:
        return "Робот молчал: нерабочие дни и время вне торгового окна"
    if rules:
        why = ", ".join(
            text for rule, text in _QUIET_OF_RULE.items() if rule in rules
        )
        return f"Нерабочий день: {why}, входов нет"
    if settings.close_on_time_end:
        return "Вне торгового окна: входов нет, позиция закрывается по концу окна"
    return (
        "Вне торгового окна: входов нет. «Закрывать в конце окна» выключено — "
        "открытая позиция остаётся до сигнала средней"
    )


def bar_open(moment: datetime, timeframe: Timeframe) -> datetime:
    """Момент внутри бара → **метка этого бара на оси**, то есть его открытие.

    ⚠️ Без этого перевода слой «факт» уезжает на одну свечу, и уезжает молча.
    У сделки время — момент внутри бара (у модели исполнения прототипа это
    начало свечи исполнения, в бою — секунда исполнения где-то в середине).
    На оси у бара одна отметка, и метка обязана встать на неё, а не между
    свечами: иначе главный вопрос слоёв «факт правее по времени или нет»
    (DOMAIN.md §8) остаётся без ответа.

    Граница бара считается функцией слоя данных (`market.bar_start`), а не
    делением здесь: сетка баров одна на программу.
    """
    return bar_start(moment, timeframe)


def chart_time(closes_at: datetime, timeframe: Timeframe) -> datetime:
    """Время **закрытия** бара (соглашение движка) → метка этого бара на оси.

    Движок штампует свои решения закрытием свечи, на которой решение принято:
    `Plan.decided_at`, время строки журнала, границы торгового окна. Ось
    графика размечена открытиями. Разница ровно в один таймфрейм, и она
    касается **каждого** времени, приехавшего из движка на график.

    ⚠️ Направление легко перепутать местами: закрытие бара численно равно
    открытию **следующего**. Отсюда `бар, который закрылся в этот момент` —
    предыдущий, а не тот, что начинается. Вычитание сделано до `bar_open`,
    чтобы результат лёг на сетку баров даже если на вход пришло время,
    границей бара не являющееся.
    """
    return bar_open(closes_at - timeframe.delta, timeframe)


def markers_and_paths(
    run: HistoryRun,
    pairs: Sequence[Pair],
    reversals: frozenset[str],
    timeframe: Timeframe,
) -> tuple[tuple[Marker, ...], tuple[TradePath, ...]]:
    """Метки и линии обоих слоёв: «прогноз» и «факт» (DOMAIN.md §8).

    Слой определяется тем, откуда пришло число, а не оформлением: `plan` — то,
    что робот рассчитал, `fill` — то, что произошло. Пара связывается именем
    заявки, а не догадкой по времени и цене.

    Пять случаев таблицы DOMAIN.md §8 выражены так:

    * обе метки есть — пара, подсказка называет расхождение в пунктах и рублях;
    * **факта нет** — метка «сигнал пропущен» на слое прогноза;
    * **прогноза нет** — метка «сделка вне расчёта» на слое факта. Разбирается
      как дефект, поэтому у неё своя форма и свой цвет, а не общая.
    """
    markers: list[Marker] = []
    for pair in pairs:
        note = _divergence(pair)
        if pair.plan is not None:
            markers.append(Marker(
                time=chart_time(pair.plan.decided_at, timeframe),
                price=pair.plan.price,
                kind=_plan_kind(pair, reversals),
                layer=Layer.PLAN,
                tooltip=note,
                pair_id=pair.order_id,
            ))
        if pair.fill is not None:
            markers.append(Marker(
                time=bar_open(pair.fill.at, timeframe),
                price=pair.fill.price,
                kind=(
                    MarkerKind.UNPLANNED if pair.plan is None
                    else _fill_kind(pair, reversals)
                ),
                layer=Layer.FACT,
                tooltip=note,
                pair_id=pair.order_id,
            ))

    plans = {pair.order_id: pair.plan for pair in pairs}
    paths: list[TradePath] = []
    for deal in run.deals:
        side = _SIDES[deal.side]
        profitable = (deal.net if deal.net is not None else deal.gross) > 0
        paths.append(TradePath(
            entry_time=bar_open(deal.entry_time, timeframe),
            entry_price=deal.entry_price,
            exit_time=bar_open(deal.exit_time, timeframe),
            exit_price=deal.exit_price,
            side=side, layer=Layer.FACT, profitable=profitable,
        ))
        entry, exit_ = plans.get(deal.entry_order_id), plans.get(deal.exit_order_id)
        if entry is not None and exit_ is not None:
            paths.append(TradePath(
                entry_time=chart_time(entry.decided_at, timeframe),
                entry_price=entry.price,
                exit_time=chart_time(exit_.decided_at, timeframe),
                exit_price=exit_.price,
                side=side, layer=Layer.PLAN, profitable=None,
            ))
    return tuple(markers), tuple(paths)


def _entry_kind(side: EngineSide, order_id: str, reversals: frozenset[str]) -> MarkerKind:
    """Вход, переворот или выход. Что из этого — решено не здесь.

    Переворотом вход называет `backtest.reversal_entries`, и определение там
    одно на отчёт и на график.
    """
    if order_id in reversals:
        return MarkerKind.REVERSAL
    return MarkerKind.ENTRY_LONG if side is EngineSide.LONG else MarkerKind.ENTRY_SHORT


def _plan_kind(pair: Pair, reversals: frozenset[str]) -> MarkerKind:
    """Метка слоя «прогноз». Расчёт без сделки — «сигнал пропущен»."""
    assert pair.plan is not None
    if pair.fill is None:
        return MarkerKind.MISSED
    if pair.plan.action is OrderAction.OPEN:
        return _entry_kind(pair.plan.side, pair.order_id, reversals)
    return MarkerKind.EXIT


def _fill_kind(pair: Pair, reversals: frozenset[str]) -> MarkerKind:
    """Метка слоя «факт»."""
    assert pair.fill is not None
    if pair.fill.action is OrderAction.OPEN:
        return _entry_kind(pair.fill.side, pair.order_id, reversals)
    return MarkerKind.EXIT


def _divergence(pair: Pair) -> str:
    """Подсказка при наведении: расчёт, факт и расхождение (ТЗ §4.5).

    Оформление берётся у окна (`ui.formatting`), а не пишется здесь заново:
    цена в подсказке обязана выглядеть так же, как в таблице сделок.
    """
    if pair.plan is None:
        return (
            "Сделка, которой расчёт не предполагал. Разбирается как дефект: "
            "в журнале решений должна быть строка о ней."
        )
    what = "Вход" if pair.plan.action is OrderAction.OPEN else "Выход"
    if pair.fill is None:
        reason = (
            f" ({pair.plan.exit_reason.label})"
            if pair.plan.exit_reason is not None else ""
        )
        return (
            f"{what} по расчёту{reason}: {fmt_price(pair.plan.price)} — "
            "не состоялся. Причина — в журнале решений."
        )
    lines = [
        f"{what}. Расчётная цена: {fmt_price(pair.plan.price)}",
        f"Фактическая: {fmt_price(pair.fill.price)}, {fmt_datetime(pair.fill.at)} МСК",
    ]
    if pair.favour_points is not None and pair.favour_rubles is not None:
        better = "в пользу позиции" if pair.favour_points >= 0 else "против позиции"
        lines.append(
            f"Расхождение: {fmt_price(abs(pair.favour_points), 0)} пунктов, "
            f"{fmt_money(abs(pair.favour_rubles))} {better}"
        )
    return "\n".join(lines)



# ---------------------------------------------------------------------------
# Отчёт о прогоне на истории
# ---------------------------------------------------------------------------

def run_days(candles: Sequence[MarketCandle]) -> int:
    """Сколько разных дней с торгами в ряду **этого прогона**.

    Не «сколько календарных дней между границами». Разница не косметическая:
    отрезок 19.06–26.08.2026 — это 69 календарных дней и 49 торговых, а годовой
    отрезок даёт 365 против 280. Владелец счёта, читающий «прибыль за 365 дней»,
    считает её годовой; на самом деле торгов было 280.

    День берётся по **закрытию** свечи и по МСК — тем же соглашением, каким
    называет отрезок запись прогона (`app/runs.py::period_note`) и строка
    «Прогон по истории» в журнале решений. Разойдись они, сверять отчёт
    с журналом стало бы нельзя.

    ⚠️ Имя не `trading_days`, и это не вкусовщина. В проекте уже есть две
    функции с таким именем, и обе отвечают на **другие** вопросы:
    `market.CandleStore.trading_days` — дни с минутками по инструменту
    **целиком**, безотносительно отрезка и отбора; `backtest.trading_days` —
    дни, годные для перебора (будние и с полной историей часов перебора).
    Ни одна из них не даёт числа, которое стоит в этом отчёте: здесь считаются
    дни того ряда, который увидел движок, — уже обрезанного отрезком
    и уже прошедшего отбор `HistoryPort._trusted`. Третья функция того же
    имени рано или поздно была бы вызвана вместо нужной.
    """
    return len({to_msk(candle.close_time).date() for candle in candles})


def run_assumptions(run: HistoryRun) -> tuple[RunAssumption, ...]:
    """Допущения прогона в объектах окна. Ни одного слова здесь не сочиняется.

    Список приходит из `backtest/assumptions.py` и пустым не бывает: пустой
    означал бы «отчёт равен выписке со счёта», а это неправда при любых
    настройках.
    """
    return tuple(
        RunAssumption(
            name=item.name,
            short=item.short,
            text=item.text,
            beside_number=item.beside_number,
        )
        for item in run.assumptions
    )


def run_report(
    run: HistoryRun,
    candles: Sequence[MarketCandle],
    request: BacktestRequest,
    *,
    settings_text: str,
) -> BacktestReport:
    """Прогон → отчёт для окна. Ни одно число не считается заново.

    ⚠️ Происхождение строк здесь **постоянно** и равно «прогон по истории»,
    и это не упрощение. Отчёт собирается только там, где отрезок истории
    закреплён, а закреплённый отрезок уводит проход в `HistoryPort._replay`
    мимо живого хода движка — иначе прошлые бары попали бы в среднюю
    наблюдателя. Появится отчёт у живого хода — происхождение станет доводом,
    а не константой.

    ⚠️ Границы «просили» и «нашлось» — **разные поля** и разъезжаются штатно:
    в базе может не быть первых дней запрошенного отрезка, а последняя свеча
    может закрыться раньше правого края. Показывать одни вместо других значит
    сказать владельцу счёта, что посчитано на отрезке, которого не было.
    """
    return BacktestReport(
        instrument=instrument_of(request.settings.instrument),
        timeframe=request.settings.timeframe,
        asked_since=request.since,
        asked_until=request.until,
        first_bar=candles[0].close_time if candles else None,
        last_bar=candles[-1].close_time if candles else None,
        bars=run.bars,
        trading_days=run_days(candles),
        settings_source=request.settings_source,
        settings_text=settings_text,
        trades=tuple(
            trade_row(deal, origin=RunOrigin.BACKTEST) for deal in run.deals
        ),
        summary=trades_summary(run),
        assumptions=run_assumptions(run),
        halted=run.halted,
    )
