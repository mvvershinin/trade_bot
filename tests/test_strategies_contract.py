"""Границы слоя `strategies/`: что модуль принимает и чего он не знает.

Два правила, каждое из которых ломается одной строкой и при этом не падает.

1. **Свеча торгового модуля не несёт поля `time`.** В слое данных `time` —
   это **начало** свечи, а движок считает торговое окно по времени **закрытия**.
   Формы классов совпадают, соответствие в проекте структурное, проверок типа
   нет: свеча слоя данных прошла бы утиной типизацией молча. Один раз это уже
   произошло на графике — метки сделок уехали на один бар, и вылечено было
   переименованием поля плюс сторожевым тестом (`ui/models.py`, класс `Candle`;
   `tests/test_ui_no_trading_logic.py`). Здесь то же лечение и тот же сторож.

2. **Модуль не знает про деньги, окно, брокера и режим прогона.** «Не знает» —
   это не стиль, а условие, при котором тестер не может разойтись с боем
   (ARCHITECTURE.md §1). Проверка статическая и по именам в коде: полностью
   выразить «нет торговой логики движка» тестом нельзя, но у неё есть надёжные
   следы — понятия, которых в словаре модуля быть не может.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from strategies import (
    AverageKind,  # noqa: F401 — берётся через globals()[...] ниже
    Bar,
    Decision,
    EmaReverse,
    Intent,
    OnPriceEqualsAverage,  # noqa: F401 — берётся через globals()[...] ниже
    Strategy,
    check_bar,
)

LAYER = pathlib.Path(__file__).resolve().parent.parent / "strategies"
SOURCES = sorted(path for path in LAYER.rglob("*.py") if "__pycache__" not in path.parts)

MSK = timezone(timedelta(hours=3), "MSK")
MOMENT = datetime(2026, 6, 19, 10, 10, tzinfo=MSK)


# --------------------------------------------------------------------------
# Свеча: имя поля времени
# --------------------------------------------------------------------------

def test_bar_carries_the_closing_time_under_its_own_name() -> None:
    """Поле называется `closes_at`, и поля `time` у свечи модуля нет."""
    assert "closes_at" in Bar.__dataclass_fields__
    assert "time" not in Bar.__dataclass_fields__, (
        "поле `time` в контракте модуля означало бы, что свеча слоя данных "
        "проходит сюда молча — а там `time` это НАЧАЛО свечи"
    )

    ours = Bar(closes_at=MOMENT, open=1.0, high=2.0, low=0.5, close=1.5, volume=10.0)
    for foreign in ("time", "timeframe", "filled_minutes", "unsettled", "close_time"):
        assert not hasattr(ours, foreign), (
            f"у свечи модуля появилось поле слоя данных {foreign} — структурная "
            "подмена снова стала возможной"
        )


def test_market_candle_cannot_pass_into_the_module_silently() -> None:
    """Свеча слоя данных отвергается вслух, а не читается «как получится».

    Одного разного имени мало: модуль №1 читает из всей свечи только `close`,
    поэтому подмена сегодня ничего бы не сломала — и сломала бы завтра,
    в модуле, который посмотрит на время. Поэтому есть ещё и `check_bar`.
    """
    from market.candles import Candle as MarketCandle
    from market.candles import Timeframe

    stranger = MarketCandle(
        time=MOMENT, open=1.0, high=2.0, low=0.5, close=1.5,
        volume=10.0, timeframe=Timeframe(5),
    )

    # Ровно то, что делает подмену молчаливой: всё, что модуль читает, на месте.
    assert all(
        hasattr(stranger, name) for name in ("open", "high", "low", "close", "volume")
    ), "проверка выродилась: свеча слоя данных больше не похожа на свечу модуля"
    with pytest.raises(AttributeError):
        stranger.closes_at  # noqa: B018 — обращение и есть проверка

    with pytest.raises(TypeError, match="time"):
        check_bar(stranger)
    with pytest.raises(TypeError, match="time"):
        EmaReverse().on_closed_bar(stranger)  # type: ignore[arg-type]


def test_any_object_with_a_time_field_is_refused() -> None:
    """Отвергается признак, а не конкретный класс.

    Список знакомых типов закрывает то, что уже придумали; признак — то,
    что придумают завтра. Свеча со временем начала может приехать из чего
    угодно: из ответа брокера, из строки базы, из чужой библиотеки.
    """
    class ForeignCandle:
        time = MOMENT
        closes_at = MOMENT
        open = high = low = close = 1.0
        volume = 0.0

    with pytest.raises(TypeError, match="НАЧАЛО"):
        check_bar(ForeignCandle())


def test_a_well_formed_stand_in_is_accepted() -> None:
    """Соответствие структурное: подойдёт любой объект нужной формы.

    Канарейка на предыдущий тест: если бы `check_bar` начал отвергать всё
    подряд, тесты про свечу слоя данных остались бы зелёными, а движок
    перестал бы работать.
    """
    class Stand:
        closes_at = MOMENT
        open = high = low = close = 100.0
        volume = 1.0

    check_bar(Stand())
    assert EmaReverse().on_closed_bar(Stand()).close == 100.0  # type: ignore[arg-type]


def test_an_object_that_is_not_a_bar_is_refused() -> None:
    with pytest.raises(TypeError, match="не свеча"):
        check_bar(object())


# --------------------------------------------------------------------------
# Порт стратегии и совпадение значений с окном
# --------------------------------------------------------------------------

def test_module_one_satisfies_the_strategy_port() -> None:
    strategy = EmaReverse()
    assert isinstance(strategy, Strategy)
    assert strategy.title
    decision = strategy.on_closed_bar(
        Bar(closes_at=MOMENT, open=1.0, high=1.0, low=1.0, close=1.0)
    )
    assert isinstance(decision, Decision)
    assert isinstance(decision.intent, Intent)


@pytest.mark.parametrize(
    "ours, theirs",
    [("AverageKind", "AverageKind"), ("OnPriceEqualsAverage", "OnPriceEqualsAverage")],
)
def test_enum_values_match_the_window(ours: str, theirs: str) -> None:
    """Значения совпадают с `ui.models`, иначе `app/` переложит выбор мимо.

    Слои друг друга не импортируют (ARCHITECTURE.md §2), перечисления объявлены
    дважды — и разъехаться могут молча: окно покажет «Простая (SMA)», а модуль
    останется на экспоненциальной.
    """
    import ui.models

    mine = {member.name: member.value for member in globals()[ours]}
    window = {member.name: member.value for member in getattr(ui.models, theirs)}
    assert mine == window, (
        "перечисления слоя стратегий и слоя окна разошлись — выбор владельца "
        "счёта не доедет до расчёта"
    )


# --------------------------------------------------------------------------
# Чего в словаре модуля быть не может
# --------------------------------------------------------------------------

# Понятия движка. Проверяются по именам в коде — не по строкам и не по
# документации: в шапках модулей эти слова стоят намеренно, списком того,
# чего здесь нет.
#
# `volume` в список не входит: у свечи это биржевой объём торгов, часть OHLCV.
# Объём робота — это `size`, `qty`, `lots`, и они запрещены.
FORBIDDEN_WORDS = {
    "commission", "fee", "broker", "margin", "collateral", "slippage",
    "profit", "loss", "takeprofit", "stop", "trailing", "limit", "risk",
    "window", "session", "weekend", "holiday", "pause",
    "position", "order", "lot", "lots", "qty", "quantity", "size",
    "money", "cash", "rub", "rubles", "portfolio", "deposit", "leverage",
    "balance", "equity", "account", "drawdown",
    "live", "backtest", "tester", "simulation", "paper",
}

# То же по-русски: в проекте встречаются русские имена (`ui.formatting.EMPTY`),
# и проверка только по английским словам оставила бы дыру.
FORBIDDEN_PARTS = (
    "объём", "объем", "комисс", "брокер", "тейк", "заявк", "позици",
    "окно", "окна", "лимит", "депозит", "рубл", "тестер", "гарантийн",
    "проскальзыван", "просадк", "счёт", "счет", "риск",
)


def _identifiers(tree: ast.AST) -> set[str]:
    """Имена в коде: переменные, поля, функции, классы, аргументы.

    Строковые константы и комментарии сюда не попадают намеренно — иначе
    проверка сработала бы на шапке модуля, где перечислено, чего здесь нет.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name)
    return names


def _guilty(names: set[str]) -> list[str]:
    found = []
    for name in sorted(names):
        lowered = name.lower()
        if set(lowered.split("_")) & FORBIDDEN_WORDS:
            found.append(name)
        elif any(part in lowered for part in FORBIDDEN_PARTS):
            found.append(name)
    return found


@pytest.fixture(scope="module")
def parsed() -> list[tuple[pathlib.Path, ast.AST]]:
    result = [
        (path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for path in SOURCES
    ]
    assert result, "в strategies/ не разобрано ни одного файла — проверка вакуумна"
    return result


def test_the_module_does_not_speak_about_money_window_or_broker(parsed) -> None:
    guilty = [
        f"{path.name}: {_guilty(_identifiers(tree))}"
        for path, tree in parsed
        if _guilty(_identifiers(tree))
    ]
    assert not guilty, (
        "в слое стратегий появилось понятие движка. Объём, комиссия, ГО, "
        "торговое окно, тейк-профит, брокер и режим прогона живут в engine/ "
        "и в каждом модуле не дублируются (ARCHITECTURE.md §3):\n  "
        + "\n  ".join(guilty)
    )


def test_that_check_is_not_blind() -> None:
    """Канарейка: сама проверка ловит, и ловит **каждое** слово словаря.

    Выборочная канарейка — половина защиты: она подтверждает, что механизм
    жив, и ничего не говорит про конкретную запись. Опечатка в одном слове
    из сорока не проявилась бы никак, а слово в словаре есть ровно потому,
    что понятие опасное.
    """
    blind = [word for word in FORBIDDEN_WORDS if not _guilty({word})]
    assert not blind, f"слова словаря не срабатывают: {sorted(blind)}"
    blind = [part for part in FORBIDDEN_PARTS if not _guilty({part})]
    assert not blind, f"русские подстроки не срабатывают: {sorted(blind)}"

    # И то же целиком: от разбора кода до вердикта.
    planted = ast.parse(
        "def take_profit(order_size):\n"
        "    комиссия = order_size\n"
        "    return комиссия\n"
    )
    assert sorted(_guilty(_identifiers(planted))) == [
        "order_size", "take_profit", "комиссия",
    ]

    # Обычные имена модуля мимо словаря не проходят: иначе тест начнут
    # отключать, и он перестанет защищать что-либо вообще.
    clean = ast.parse(
        "def on_closed_bar(bar):\n"
        "    average = bar.close\n"
        "    return average\n"
    )
    assert _guilty(_identifiers(clean)) == []


def test_the_module_imports_nothing_but_the_standard_library(parsed) -> None:
    """Ни одного чужого слоя и ни одной внешней библиотеки.

    Направление зависимостей проверяет `tests/test_layers.py`; здесь — жёстче:
    сменный торговый модуль не должен тянуть за собой ни pandas, ни Qt, иначе
    его нельзя будет прогнать в оптимизаторе в отдельном процессе дёшево.
    """
    allowed_own = {"strategies"}
    stdlib = {
        "__future__", "enum", "dataclasses", "datetime", "typing", "math",
        "collections", "collections.abc",
    }
    foreign: list[str] = []
    for path, tree in parsed:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            else:
                continue
            for module in modules:
                head = module.split(".")[0]
                if head not in stdlib and head not in allowed_own:
                    foreign.append(f"{path.name}:{node.lineno} → {module}")
    assert not foreign, (
        "в слое стратегий появился посторонний импорт:\n  " + "\n  ".join(foreign)
    )


# --------------------------------------------------------------------------
# Непригодное закрытие: nan не падает, а расползается
# --------------------------------------------------------------------------

@pytest.mark.parametrize("close", [float("nan"), float("inf"), float("-inf")])
def test_a_non_number_close_is_refused(close: float) -> None:
    """`nan` в закрытии обязан останавливать модуль на входе.

    Иначе он расползается: сравнения с `nan` ложны, поэтому свеча уходит
    в ветку «закрытие ровно на средней», а сама средняя становится `nan`
    навсегда. При настройке «считать сигналом на лонг» это сигнал на покупку
    на каждой свече до перезапуска программы.
    """
    bar = Bar(closes_at=MOMENT, open=1.0, high=1.0, low=1.0, close=close)
    with pytest.raises(ValueError, match="не число"):
        check_bar(bar)
    with pytest.raises(ValueError, match="не число"):
        EmaReverse().on_closed_bar(bar)


def test_a_non_number_close_does_not_poison_the_average() -> None:
    """Отказ происходит до того, как свеча попала в расчёт."""
    strategy = EmaReverse()
    first = Bar(closes_at=MOMENT, open=1.0, high=1.0, low=1.0, close=100.0)
    spoiled = Bar(
        closes_at=MOMENT + timedelta(minutes=5),
        open=1.0, high=1.0, low=1.0, close=float("nan"),
    )
    third = Bar(
        closes_at=MOMENT + timedelta(minutes=10),
        open=1.0, high=1.0, low=1.0, close=101.0,
    )

    strategy.on_closed_bar(first)
    with pytest.raises(ValueError):
        strategy.on_closed_bar(spoiled)
    assert strategy.bars == 1, "непригодная свеча не должна попадать в историю"
    assert strategy.on_closed_bar(third).bars == 2


def test_a_close_that_is_not_a_number_at_all_is_refused() -> None:
    """Строка, которая числом не притворяется, — отказ.

    ⚠️ Числовая строка (`"285000"`) при этом принимается: `float()` переводит
    её без потерь, и запрещать это значило бы вводить свою политику типов
    заодно с `Decimal`. Настоящая защита здесь — от `nan`, а не от `str`.
    """
    class Textual:
        closes_at = MOMENT
        open = high = low = 1.0
        close = "триста"
        volume = 0.0

    with pytest.raises(TypeError, match="не число"):
        check_bar(Textual())

    class Numeric(Textual):
        close = "285000"

    check_bar(Numeric())
