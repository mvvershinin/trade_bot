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

3. **Порт соблюдают все модули реестра, а не один написанный первым.** Набор
   проверок внизу файла параметризован по `strategies.registry`: добавили
   модуль — проверки поехали на него сами, забыть их некуда. Пока модуль один,
   это самое удобное место спрятать дефект «работает только для первого»,
   поэтому сам набор доказан **поддельным сломанным модулем**: каждая поломка
   обязана уронить свою проверку поимённо, а исправный подставной модуль —
   пройти все. Без этой пары набор был бы вакуумным, и вакуумность не видна
   по зелёному прогону.
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

import pytest

from strategies import (
    AverageKind,  # noqa: F401 — берётся через globals()[...] ниже
    Bar,
    Claim,
    Decision,
    Description,
    EmaReverse,
    Intent,
    OnPriceEqualsAverage,  # noqa: F401 — берётся через globals()[...] ниже
    Strategy,
    StrategyEntry,
    check_bar,
    registry,
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


# --------------------------------------------------------------------------
# Контрактный набор: каждый модуль реестра — против каждой проверки порта
# --------------------------------------------------------------------------
#
# Почему набор, а не отдельные тесты на модуль №1: сверка с прототипом
# (127 сделок построчно) **не ловит** нарушения этого порта. Разбор в
# `strategies/contracts.py`: прогрев эталонного отрезка приходится на
# 08:55–10:10, позиции ещё нет, шаги 5 и 6 `PROTOTYPE.md` §2 пустые — модуль,
# перепутавший `skip_bar` с «намерение NONE», даст те же самые 127 сделок.
# Вылезет это на прогоне, начатом внутри открытой позиции, там, где сверять
# уже не с чем.

#: Сколько свечей отводится модулю на прогрев. У модуля №1 при периоде 15
#: прогрев кончается на 16-й свече, у подставного — на 4-й. Шестьдесят — это
#: запас, а не измерение: модуль, которому мало, назовёт себя сам отказом.
WARMUP_LIMIT = 60


def _series(count: int) -> list[Bar]:
    """Ряд закрытых свечей: время идёт вперёд, закрытие ходит пилой вокруг 100.

    Пила, а не прямая: на прямой закрытие всё время по одну сторону средней,
    и проверки не увидели бы разницы между «модуль отдаёт намерение» и
    «модуль отдаёт одно и то же».
    """
    bars: list[Bar] = []
    for step in range(count):
        close = 100.0 + float(step % 7) - 3.0
        bars.append(
            Bar(
                closes_at=MOMENT + timedelta(minutes=5 * step),
                open=close,
                high=close + 1.0,
                low=close - 1.0,
                close=close,
                volume=1.0,
            )
        )
    return bars


def _trace(module: object, bars: list[Bar]) -> tuple[tuple[object, ...], ...]:
    """Что модуль ответил на ряд — построчно, полями решения.

    Через `getattr`, а не по типу: набор проверяет **форму** ответа, и разбор
    «это вообще `Decision`?» — отдельная проверка со своим именем.
    """
    answers = []
    for bar in bars:
        # Модуль здесь `object` намеренно: разбор «это вообще модуль порта?» —
        # отдельная проверка со своим именем, отсюда подавление на строке ниже.
        decision = module.on_closed_bar(bar)  # type: ignore[attr-defined]
        answers.append(
            tuple(
                getattr(decision, name, "<нет поля>")
                for name in ("intent", "warmed_up", "skip_bar", "close", "average", "bars")
            )
        )
    return tuple(answers)


def _warm(entry: StrategyEntry) -> tuple[Strategy, list[Bar], Decision]:
    """Прогретый модуль, поданный ему ряд и последнее решение."""
    module = entry.build(entry.defaults())
    bars = _series(WARMUP_LIMIT)
    fed: list[Bar] = []
    for bar in bars:
        decision = module.on_closed_bar(bar)
        fed.append(bar)
        if getattr(decision, "warmed_up", False):
            return module, fed, decision
    raise AssertionError(
        f"модуль «{entry.title}» не прогрелся за {WARMUP_LIMIT} свечей — "
        "прогрев либо не кончается, либо про него не сообщается"
    )


class ForeignCandleShape:
    """Свеча со временем НАЧАЛА: поле `time` есть, всё остальное на месте.

    Именно такая подмена молчалива: у объекта есть всё, что модуль читает.
    """

    time = MOMENT
    closes_at = MOMENT
    open = high = low = close = 100.0
    volume = 0.0


# ----------------------------------------------------------- сами проверки

def _check_port_shape(entry: StrategyEntry) -> None:
    """Собранный модуль удовлетворяет порту `Strategy`."""
    module = entry.build(entry.defaults())
    assert isinstance(module, Strategy), (
        f"модуль «{entry.title}» не удовлетворяет порту Strategy: движок зовёт "
        "у него `title`, `reset()` и `on_closed_bar()`"
    )


def _check_title_is_not_empty(entry: StrategyEntry) -> None:
    """Название модуля не пустое: оно уходит в окно, в журнал и в базу прогонов."""
    module = entry.build(entry.defaults())
    title = getattr(module, "title", "")
    assert isinstance(title, str) and title.strip(), (
        f"у модуля {entry.id} пустое название — в списке выбора и в журнале "
        "решений будет пустое место"
    )


def _check_title_matches_the_entry(entry: StrategyEntry) -> None:
    """Название в реестре и название модуля — одно и то же.

    Разошлись — в списке выбора одно, а в журнале решений и в снимке прогона
    другое, и разбор «почему в тот день робот повёл себя иначе» ломается.
    """
    module = entry.build(entry.defaults())
    assert getattr(module, "title", None) == entry.title, (
        f"реестр называет модуль {entry.id} «{entry.title}», а сам модуль — "
        f"«{getattr(module, 'title', None)}»"
    )


def _check_the_decision_has_the_declared_type(entry: StrategyEntry) -> None:
    """Ответ — `Decision` с намерением `Intent` и непустой причиной.

    Причина уходит в журнал решений человеческим языком (ТЗ §4.6): пустая
    строка означает действие робота без строки в журнале, то есть дефект.
    """
    module = entry.build(entry.defaults())
    decision = module.on_closed_bar(_series(1)[0])
    assert isinstance(decision, Decision), (
        f"модуль {entry.id} вернул {type(decision).__name__}, а не Decision: "
        "движок читает поля решения по имени и молча получил бы не то"
    )
    assert isinstance(decision.intent, Intent)
    assert decision.reason.strip(), "решение без причины — строка в журнале будет пустой"


def _check_warmup_skips_the_bar(entry: StrategyEntry) -> None:
    """Пока прогрев не кончился, свеча в обработку не идёт (`skip_bar`).

    Шаг 2 `PROTOTYPE.md` §2: выход из обработки свечи **целиком**, до
    перестановки тейка и до закрытия по концу окна. Это и есть та ловушка,
    которую сверка с прототипом не поймает.
    """
    module = entry.build(entry.defaults())
    for number, bar in enumerate(_series(WARMUP_LIMIT), start=1):
        decision = module.on_closed_bar(bar)
        if getattr(decision, "warmed_up", False):
            assert decision.skip_bar is False, (
                f"модуль {entry.id} пропускает свечу №{number}, хотя прогрев "
                "уже кончился: движок не переставит тейк и не закроет по окну"
            )
            return
        assert decision.skip_bar is True, (
            f"модуль {entry.id} на прогреве (свеча №{number}) не выставил "
            "skip_bar: движок примет непрогретую свечу за рабочую"
        )
    raise AssertionError(f"модуль {entry.id} не прогрелся за {WARMUP_LIMIT} свечей")


def _check_reset_returns_the_module_to_warmup(entry: StrategyEntry) -> None:
    """`reset()` — это новый ряд с начала, а не «очистка кэша».

    Прогрев отсчитывается от начала поданного ряда, предыстории у модуля нет
    (`PROTOTYPE.md` §3): тот же файл с другой начальной даты даёт другой
    список сделок целиком. Поэтому сброшенный модуль обязан ответить на ряд
    ровно то же, что отвечает только что созданный.
    """
    bars = _series(WARMUP_LIMIT)
    fresh = _trace(entry.build(entry.defaults()), bars)

    reused = entry.build(entry.defaults())
    _trace(reused, bars)
    reused.reset()
    try:
        again = _trace(reused, bars)
    except (TypeError, ValueError) as error:
        raise AssertionError(
            f"после reset() модуль {entry.id} не принял тот же ряд заново "
            f"({error!r}) — сброс не вернул его к прогреву"
        ) from error
    assert again == fresh, (
        f"после reset() модуль {entry.id} отвечает не так, как только что "
        "созданный: прогрев или накопленный ряд остались от прошлой жизни"
    )


def _check_a_repeated_bar_is_skipped_not_raised(entry: StrategyEntry) -> None:
    """Повтор последней свечи — штатное поведение потока, а не сбой.

    После переподключения брокер присылает последнюю свечу ещё раз. Принятая
    молча, она входит в расчёт дважды и сдвигает прогрев; ставшая исключением,
    она роняет цикл обработки у робота с открытой позицией.
    """
    module, fed, before = _warm(entry)
    again = module.on_closed_bar(fed[-1])
    assert again.skip_bar is True, (
        f"модуль {entry.id} принял повторно поданную свечу как новую — "
        "она войдёт в расчёт дважды"
    )
    assert again.bars == before.bars, (
        f"модуль {entry.id} посчитал повторную свечу второй раз"
    )


def _check_a_bar_from_the_past_is_refused(entry: StrategyEntry) -> None:
    """Свеча раньше предыдущей — порча ряда, прогон обязан остановиться."""
    module, fed, _ = _warm(entry)
    backwards = dataclasses.replace(
        fed[-1], closes_at=fed[0].closes_at - timedelta(minutes=5)
    )
    with pytest.raises(ValueError):
        module.on_closed_bar(backwards)


def _check_a_foreign_candle_is_refused(entry: StrategyEntry) -> None:
    """Свеча со временем НАЧАЛА (`time`) — отказ вслух, а не «как получится»."""
    module = entry.build(entry.defaults())
    with pytest.raises(TypeError):
        # Подставная свеча не `Bar` намеренно: проверяется ровно то, что модуль
        # отвергает объект чужой формы, отсюда подавление на строке ниже.
        module.on_closed_bar(ForeignCandleShape())  # type: ignore[arg-type]


def _check_a_nan_close_is_refused(entry: StrategyEntry) -> None:
    """`nan` в закрытии не падает, а расползается: он обязан быть отвергнут."""
    module = entry.build(entry.defaults())
    spoiled = dataclasses.replace(_series(1)[0], close=float("nan"))
    with pytest.raises(ValueError):
        module.on_closed_bar(spoiled)


#: Контрактный набор. Имя проверки попадает в имя теста — отказ читается
#: без открывания файла.
CHECKS: dict[str, Callable[[StrategyEntry], None]] = {
    "port_shape": _check_port_shape,
    "title_is_not_empty": _check_title_is_not_empty,
    "title_matches_the_entry": _check_title_matches_the_entry,
    "decision_has_the_declared_type": _check_the_decision_has_the_declared_type,
    "warmup_skips_the_bar": _check_warmup_skips_the_bar,
    "reset_returns_the_module_to_warmup": _check_reset_returns_the_module_to_warmup,
    "a_repeated_bar_is_skipped": _check_a_repeated_bar_is_skipped_not_raised,
    "a_bar_from_the_past_is_refused": _check_a_bar_from_the_past_is_refused,
    "a_foreign_candle_is_refused": _check_a_foreign_candle_is_refused,
    "a_nan_close_is_refused": _check_a_nan_close_is_refused,
}


@pytest.mark.parametrize("check", sorted(CHECKS))
@pytest.mark.parametrize("entry", registry.entries(), ids=lambda item: item.id)
def test_every_module_of_the_registry_keeps_the_port(
    entry: StrategyEntry, check: str
) -> None:
    """Каждый модуль реестра против каждой проверки порта.

    Параметризация по реестру, а не по списку руками: добавили модуль —
    проверки поехали на него сами, и забыть их некуда.
    """
    CHECKS[check](entry)


# --------------------------------------------------------------------------
# Поддельный модуль: чем доказывается, что набор выше не вакуумен
# --------------------------------------------------------------------------
#
# ⚠️ Поддельный модуль **не попадает в настоящий реестр** ни на одну проверку.
# Запись собирается тут же, локально, и никуда не сохраняется: тест, дописавший
# строку в общую таблицу, оставил бы её соседям — это ровно тот дефект
# изоляции, из-за которого `test_from_status_scrubs_the_body_it_is_handed`
# зеленел от чужой работы и не проверял ничего (`D-078`). Сторожит это
# автоматическая фикстура ниже.

STAND_IN_TITLE = "Подставной модуль для проверки набора"


@dataclasses.dataclass(frozen=True, slots=True)
class StandInSettings:
    """Настройки подставного модуля. Своя, отдельная от модуля №1, форма."""

    period: int = 3


class SoundStandIn:
    """Подставной модуль, написанный **по контракту**. Эталон для поломок.

    Написан отдельно от модуля №1 нарочно: если бы поломки делались из
    `EmaReverse`, набор проверял бы сам себя. Здесь другая арифметика (простая
    средняя), другой прогрев (3) и другие настройки — общее только то, что
    объявлено портом.
    """

    title = STAND_IN_TITLE

    def __init__(self, settings: StandInSettings | None = None) -> None:
        self._settings = StandInSettings() if settings is None else settings
        self._closes: list[float] = []
        self._last_at: object | None = None

    def reset(self) -> None:
        self._closes.clear()
        self._last_at = None

    def _check(self, bar: object) -> None:
        """Разбор входа отдельным методом: поломка «без проверки» гасит его."""
        check_bar(bar)

    def _is_warm(self) -> bool:
        return len(self._closes) > self._settings.period

    def _average(self) -> float | None:
        period = self._settings.period
        if len(self._closes) < period:
            return None
        return sum(self._closes[-period:]) / period

    def on_closed_bar(self, bar: Bar) -> Decision:
        self._check(bar)
        if self._last_at is not None:
            if bar.closes_at == self._last_at:
                return Decision(
                    intent=Intent.NONE,
                    reason="Свеча подана повторно — уже учтена, в обработку не идёт",
                    close=float(bar.close),
                    average=self._average(),
                    bars=len(self._closes),
                    warmed_up=self._is_warm(),
                    skip_bar=True,
                )
            if bar.closes_at < self._last_at:  # type: ignore[operator]
                raise ValueError("свеча из прошлого: ряд повреждён")
        self._closes.append(float(bar.close))
        self._last_at = bar.closes_at
        average = self._average()
        if not self._is_warm() or average is None:
            return Decision(
                intent=Intent.NONE,
                reason=f"Прогрев: накоплено свечей {len(self._closes)} — сигналов нет",
                close=float(bar.close),
                average=average,
                bars=len(self._closes),
                warmed_up=False,
                skip_bar=True,
            )
        close = float(bar.close)
        intent = Intent.NONE
        if close > average:
            intent = Intent.LONG
        elif close < average:
            intent = Intent.SHORT
        return Decision(
            intent=intent,
            reason=f"Закрытие {close} против средней {average}",
            close=close,
            average=average,
            bars=len(self._closes),
            warmed_up=True,
        )


class WithoutTheClosedBarMethod:
    """Поломка: метода порта нет вовсе. Движку нечего позвать."""

    title = STAND_IN_TITLE

    def __init__(self, settings: StandInSettings | None = None) -> None:
        self._sound = SoundStandIn(settings)

    def reset(self) -> None:
        self._sound.reset()


class WithoutReset:
    """Поломка: нет `reset()`. Прогон с другой даты пошёл бы поверх прошлого."""

    title = STAND_IN_TITLE

    def __init__(self, settings: StandInSettings | None = None) -> None:
        self._sound = SoundStandIn(settings)

    def on_closed_bar(self, bar: Bar) -> Decision:
        return self._sound.on_closed_bar(bar)


@dataclasses.dataclass(frozen=True, slots=True)
class DecisionLookalike:
    """Двойник решения: те же поля, другой класс.

    Реалистичнее подмены на строку: модуль, объявивший свой `Decision`,
    прошёл бы утиной типизацией и разошёлся бы с движком в первом же поле,
    которое решат добавить.
    """

    intent: Intent
    reason: str
    close: float
    average: float | None = None
    bars: int = 0
    warmed_up: bool = False
    skip_bar: bool = False


class ADecisionOfAForeignType(SoundStandIn):
    """Поломка: метод есть, ответ той же формы, но не `Decision`."""

    def on_closed_bar(self, bar: Bar) -> Decision:
        real = super().on_closed_bar(bar)
        return DecisionLookalike(  # type: ignore[return-value]
            **{f.name: getattr(real, f.name) for f in dataclasses.fields(real)}
        )


class AResetThatForgetsNothing(SoundStandIn):
    """Поломка: `reset()` есть и ничего не делает."""

    def reset(self) -> None:
        return None


class AnEmptyTitle(SoundStandIn):
    """Поломка: названия нет. В списке выбора и в журнале — пустое место."""

    title = ""


class ATitleThatDriftedFromTheEntry(SoundStandIn):
    """Поломка: модуль переименовали, а реестр остался со старым названием."""

    title = "Модуль, о котором реестр не знает"


class WarmupWithoutSkippingTheBar(SoundStandIn):
    """Поломка ловушки №10: прогрев без `skip_bar`.

    Сверка с прототипом на эту поломку **не реагирует** — 127 сделок остаются
    теми же самыми (разбор в `strategies/contracts.py`). Кроме этого набора
    её не ловит ничто.
    """

    def on_closed_bar(self, bar: Bar) -> Decision:
        real = super().on_closed_bar(bar)
        if not real.warmed_up:
            return dataclasses.replace(real, skip_bar=False)
        return real


class ARepeatTakenAsNew(SoundStandIn):
    """Поломка: повторно поданная свеча объявлена рабочей."""

    def on_closed_bar(self, bar: Bar) -> Decision:
        real = super().on_closed_bar(bar)
        if real.skip_bar and real.warmed_up:
            return dataclasses.replace(real, skip_bar=False)
        return real


class WithoutTheInputCheck(SoundStandIn):
    """Поломка: вход не проверяется. Чужая свеча и `nan` проходят молча."""

    def _check(self, bar: object) -> None:
        return None


class ABarFromThePastSwallowed(SoundStandIn):
    """Поломка: ход времени назад проглочен и выдан за обычную свечу."""

    def on_closed_bar(self, bar: Bar) -> Decision:
        check_bar(bar)  # `nan` и чужая свеча по-прежнему отвергаются
        try:
            return super().on_closed_bar(bar)
        except ValueError:
            return Decision(
                intent=Intent.NONE,
                reason="ход времени назад проглочен",
                close=float(bar.close),
                skip_bar=True,
            )


class Breakage(NamedTuple):
    """Поломка подставного модуля и проверки, которые она обязана уронить."""

    factory: type
    title: str
    drops: tuple[str, ...]


#: Таблица поломок. `title` — то, что о модуле говорит **реестр**: у поломки
#: с пустым названием реестр честно повторяет пустое, иначе упали бы две
#: проверки вместо одной и стало бы непонятно, которая работает.
BREAKAGES: dict[str, Breakage] = {
    "no_closed_bar_method": Breakage(
        WithoutTheClosedBarMethod, STAND_IN_TITLE, ("port_shape",)
    ),
    "no_reset_method": Breakage(
        WithoutReset, STAND_IN_TITLE,
        ("port_shape", "reset_returns_the_module_to_warmup"),
    ),
    "a_decision_of_a_foreign_type": Breakage(
        ADecisionOfAForeignType, STAND_IN_TITLE, ("decision_has_the_declared_type",)
    ),
    "a_reset_that_forgets_nothing": Breakage(
        AResetThatForgetsNothing, STAND_IN_TITLE,
        ("reset_returns_the_module_to_warmup",),
    ),
    "an_empty_title": Breakage(AnEmptyTitle, "", ("title_is_not_empty",)),
    "a_title_that_drifted": Breakage(
        ATitleThatDriftedFromTheEntry, STAND_IN_TITLE, ("title_matches_the_entry",)
    ),
    "warmup_without_skipping_the_bar": Breakage(
        WarmupWithoutSkippingTheBar, STAND_IN_TITLE, ("warmup_skips_the_bar",)
    ),
    "a_repeat_taken_as_new": Breakage(
        ARepeatTakenAsNew, STAND_IN_TITLE, ("a_repeated_bar_is_skipped",)
    ),
    "without_the_input_check": Breakage(
        WithoutTheInputCheck, STAND_IN_TITLE,
        ("a_foreign_candle_is_refused", "a_nan_close_is_refused"),
    ),
    "a_bar_from_the_past_swallowed": Breakage(
        ABarFromThePastSwallowed, STAND_IN_TITLE,
        ("a_bar_from_the_past_is_refused",),
    ),
}


def _stand_in_description(settings: StandInSettings) -> Description:
    """Описание подставного модуля — настоящее, а не заглушка.

    Настоящее потому, что набор проверок описания (`test_strategies_description`)
    ходит по записям реестра, а этот файл собирает записи **мимо** реестра:
    заглушка здесь означала бы, что механизм описания на подделке никогда
    не проверялся. Утверждения повторяют арифметику `SoundStandIn` —
    простая средняя, строгие неравенства, равенство без сигнала.
    """
    label = f"SMA({settings.period})"
    return Description(
        title=STAND_IN_TITLE,
        lead=f"Подставной модуль сравнивает закрытие с простой средней {label}.",
        claims=(
            Claim(
                relation="закрытие выше средней",
                detail=f"закрытие выше {label}",
                intent=Intent.LONG,
                probe=lambda average: average + max(abs(average) * 0.02, 0.02),
            ),
            Claim(
                relation="закрытие ниже средней",
                detail=f"закрытие ниже {label}",
                intent=Intent.SHORT,
                probe=lambda average: average - max(abs(average) * 0.02, 0.02),
            ),
            Claim(
                relation="закрытие ровно на средней",
                detail=f"закрытие ровно на {label}",
                intent=Intent.NONE,
                probe=lambda average: average,
            ),
        ),
    )


def _stand_in_entry(factory: type, title: str) -> StrategyEntry:
    """Запись реестра для подставного модуля — **локальная**, мимо таблицы."""
    return StrategyEntry(
        id="stand_in",
        title=title,
        settings_type=StandInSettings,
        factory=factory,
        fields=(("period", "average_period"),),
        describe=_stand_in_description,
    )


def _failing_checks(entry: StrategyEntry) -> frozenset[str]:
    """Какие проверки набора падают на этой записи — множеством имён.

    ⚠️ `pytest.fail.Exception` в списке не для полноты: `pytest.raises`,
    не дождавшийся исключения, роняет **не** `Exception`, а потомка
    `BaseException`. Без этого имени проверки вида «модуль обязан отвергнуть»
    считались бы пройденными на модуле, который ничего не отвергает, —
    то есть таблица поломок молча перестала бы что-либо доказывать.
    """
    failed = set()
    for name, check in CHECKS.items():
        try:
            check(entry)
        except (Exception, pytest.fail.Exception):  # noqa: BLE001 — ловится ЛЮБОЙ отказ проверки
            failed.add(name)
    return frozenset(failed)


@pytest.fixture(autouse=True)
def the_registry_is_left_alone():
    """Ни один тест этого файла не дописывает подделку в настоящий реестр.

    Проверяется после каждого теста, а не один раз в конце: иначе виновника
    пришлось бы искать по всему файлу.
    """
    before = registry.known_ids()
    yield
    assert registry.known_ids() == before, (
        "тест изменил состав настоящего реестра — подделка досталась соседям"
    )


def test_the_stand_in_is_not_a_module_of_this_build() -> None:
    """Подставной модуль в реестре не значится и значиться не должен."""
    assert "stand_in" not in registry.known_ids()


def test_the_sound_stand_in_passes_every_check() -> None:
    """Канарейка набора: исправный подставной модуль проходит все проверки.

    Без неё «поломка роняет проверку» ничего не значило бы: набор, падающий
    на чём угодно, тоже роняет каждую поломку.
    """
    entry = _stand_in_entry(SoundStandIn, STAND_IN_TITLE)
    assert _failing_checks(entry) == frozenset(), (
        "исправный подставной модуль не прошёл набор — значит проверки ловят "
        "не нарушение контракта, а особенности модуля №1"
    )


@pytest.mark.parametrize("name", sorted(BREAKAGES))
def test_each_breakage_drops_its_own_check(name: str) -> None:
    """Каждая поломка роняет свою проверку — поимённо.

    Проверяется вхождение, а не совпадение множеств: поломка вроде «нет метода
    порта» роняет заодно всё, что этот метод зовёт, и требовать точного списка
    значило бы переписывать таблицу при добавлении любой новой проверки.
    Строгость даёт пара с канарейкой выше: набор не падает на исправном.
    """
    breakage = BREAKAGES[name]
    entry = _stand_in_entry(breakage.factory, breakage.title)
    failed = _failing_checks(entry)
    missed = sorted(set(breakage.drops) - failed)
    assert not missed, (
        f"поломка «{name}» прошла мимо проверок {missed}: они зелёные "
        f"на сломанном модуле. Упало только это: {sorted(failed)}"
    )


def test_no_check_of_the_set_is_dead_weight() -> None:
    """У каждой проверки набора есть поломка, которая её роняет.

    Проверка, которую не роняет ни одна поломка, не доказана ничем: она может
    быть зелёной всегда — ровно тот класс дефекта, который в проекте ловили
    семнадцать раз за трое суток.
    """
    named = {check for breakage in BREAKAGES.values() for check in breakage.drops}
    orphans = sorted(set(CHECKS) - named)
    assert not orphans, (
        f"проверки набора не доказаны ни одной поломкой: {orphans}. "
        "Заведите поломку либо снимите проверку"
    )


def test_every_breakage_names_a_check_of_the_set() -> None:
    """Обратная сторона: имя проверки в таблице поломок не протухло."""
    unknown = sorted(
        {check for breakage in BREAKAGES.values() for check in breakage.drops}
        - set(CHECKS)
    )
    assert not unknown, f"таблица поломок называет несуществующие проверки: {unknown}"
