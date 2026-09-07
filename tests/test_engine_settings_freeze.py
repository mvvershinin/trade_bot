"""Заморозочная матрица `EngineSettings.__post_init__` — поведение как есть.

Миниплан `.docs/plans/E1-0g-phase-1-engine-settings.md` §3(г). Пишется **до**
разбора функции на методы (шаг 3 плана) и обязана быть зелёной на нынешнем,
нетронутом коде: её работа — заметить, что рефакторинг **стёр** проверку или
**ослабил** её, а не проверить, что проверка правильная. Сверка с прототипом
этого не видит: она ловит ужесточение (эталон гоняет `take_profit_percent=0.0`
и упал бы, отвергни код ноль), но не ослабление — отвергающая ветка,
переставшая отвергать, прототипу ничем не мешает.

Почему не все 136 клеток (17 полей × 8 категорий значений)
------------------------------------------------------------
Осмысленно различных исходов меньше, чем клеток:

* у четырёх перечислений (`mode`, `window`, `reversal`, `partial_candles`) —
  два: верный тип и чужой. `nan`, `inf`, 0, отрицательное дают одну и ту же
  ветку («не инстанс класса»), и клетка на каждое из них ничего нового
  не проверяет;
* у пяти булевых — три: верный, чужой тип, целое `1` (похоже на булево,
  но `isinstance(1, bool)` — `False`);
* полный разброс нужен только восьми числовым полям: `volume`,
  `take_profit_percent`, `trailing_start_percent`, `trailing_offset_percent`,
  `trailing_step_percent`, `commission_per_side`, `ruble_per_point`,
  `close_wait_bars` — у них шесть РАЗНЫХ наборов правил, и различия денежные
  (см. `engine/settings.py`, комментарии у `__post_init__`).

Почему два базовых ряда
------------------------
`trailing_offset_percent` и `trailing_step_percent` проверяются на знак
**только** внутри `if self.trailing_take_profit:` — при выключенном
трейлинге (умолчание) отрицательные значения проходят молча, потому что поле
ни на что не влияет. Матрица «одно поле варьируем, остальные — умолчания»
эту ветку не задевает **никогда**: клетки «трейлинг включён, отступ
отрицательный» и «трейлинг включён, шаг отрицательный» при одном базовом
ряду структурно недостижимы, и рефакторинг мог бы стереть эти две проверки
незамеченным. Отсюда второй базовый ряд: `trailing_take_profit=True`.

Известный долг помечен, а не спрятан
--------------------------------------
`EngineSettings(volume=float("nan"))` и `volume=float("inf")` **проходят**:
у `volume`, в отличие от четырёх процентов, нет проверки `math.isfinite`.
Это заведённый долг (`ROADMAP.md` §9.10, найден при проверке этого же
миниплана), и чинить его в этой фазе нельзя — это правка поведения под
видом сохраняющего рефакторинга. Клетки помечены `# ДОЛГ` в тексте `label`,
чтобы не читаться как решение, — в отличие от соседней клетки
`take_profit_percent` × «отрицательное», которая тоже проходит, но по
обоснованному правилу (`PROTOTYPE.md` §5: «процент <= 0 — тейка нет»).

Что не открытие, а норма
--------------------------
Клетки «`trailing_start_percent`/`trailing_offset_percent`/
`trailing_step_percent` отрицательные **на умолчаниях**» тоже проходят —
но при выключенном трейлинге эти поля ни на что не влияют, и это не долг,
а корректное отражение того, что поле не задействовано.
"""

from __future__ import annotations

from datetime import time
from typing import NamedTuple

import pytest

from engine import (
    MAX_CLOSE_WAIT_BARS,
    EngineSettings,
    Mode,
    PartialCandles,
    Reversal,
    TradingWindow,
)

NAN = float("nan")
INF = float("inf")
NEG_INF = float("-inf")

#: Написанная фраза, общая для всех отказов на чужом типе поля: `... получено
#: {value!r}`. Мутация «проверить значение раньше типа» на нетипизированном
#: значении обрушивает сравнение раньше, чем до этой фразы доходит очередь, —
#: и вместо неё видна фраза из `TypeError` самого Python, а не наша.
GOT = "получено"


class Cell(NamedTuple):
    """Одна клетка матрицы: что подать, что должно случиться."""

    label: str
    kwargs: dict[str, object]
    outcome: type[Exception] | None  # None — не должно упасть
    match: str | None = None  # подстрока в тексте исключения


def _enum_cells() -> list[Cell]:
    """Четыре перечисления: верный тип проходит, чужой — отказ с фразой."""
    cells: list[Cell] = []
    for name, valid in (
        ("mode", Mode.LONG_ONLY),
        ("window", TradingWindow(time(9, 0), time(9, 30))),
        ("reversal", Reversal.SAME_BAR),
        ("partial_candles", PartialCandles.SKIP),
    ):
        cells.append(Cell(f"{name}=верный тип", {name: valid}, None))
        cells.append(Cell(f"{name}=чужой тип", {name: "чужое"}, TypeError, GOT))
    return cells


def _bool_cells() -> list[Cell]:
    """Пять булевых: верный, чужой тип, целое `1` (похоже, но не булево)."""
    cells: list[Cell] = []
    for name in (
        "close_on_time_end", "trade_in_weekend", "stop_after_take_profit",
        "take_profit", "trailing_take_profit",
    ):
        cells.append(Cell(f"{name}=верный тип", {name: False}, None))
        cells.append(Cell(f"{name}=чужой тип", {name: "да"}, TypeError, GOT))
        cells.append(Cell(f"{name}=целое 1", {name: 1}, TypeError, GOT))
    return cells


#: `volume` — единственное числовое поле без `math.isfinite`: `nan`/`inf`
#: проходят молча, это заведённый долг (см. докстринг модуля), а не образец
#: для остальных семи полей.
_VOLUME_CELLS: list[Cell] = [
    Cell("volume=чужой тип", {"volume": "чужое"}, TypeError, GOT),
    Cell("volume=булево", {"volume": True}, TypeError, GOT),
    Cell("volume=None", {"volume": None}, TypeError, GOT),
    Cell("volume=nan (ДОЛГ ROADMAP 9.10)", {"volume": NAN}, None),
    Cell("volume=inf (ДОЛГ, тот же нет-isfinite)", {"volume": INF}, None),
    Cell("volume=-inf", {"volume": NEG_INF}, ValueError, "положительным"),
    Cell("volume=0", {"volume": 0}, ValueError, "положительным"),
    Cell("volume=отрицательное", {"volume": -1.0}, ValueError, "положительным"),
    Cell("volume=валидное", {"volume": 0.5}, None),
]

#: `take_profit_percent` — `isfinite` проверяется, знак — **нет**, намеренно:
#: `PROTOTYPE.md` §5, «процент <= 0 — тейка нет» — рабочая конфигурация,
#: а не вырожденная.
_TAKE_PROFIT_PERCENT_CELLS: list[Cell] = [
    Cell("take_profit_percent=чужой тип", {"take_profit_percent": "чужое"}, TypeError, GOT),
    Cell("take_profit_percent=булево", {"take_profit_percent": True}, TypeError, GOT),
    Cell("take_profit_percent=None", {"take_profit_percent": None}, TypeError, GOT),
    Cell("take_profit_percent=nan", {"take_profit_percent": NAN}, ValueError, "не число"),
    Cell("take_profit_percent=inf", {"take_profit_percent": INF}, ValueError, "не число"),
    Cell("take_profit_percent=-inf", {"take_profit_percent": NEG_INF}, ValueError, "не число"),
    Cell("take_profit_percent=0 (по правилу — тейка нет)", {"take_profit_percent": 0}, None),
    Cell("take_profit_percent=отрицательное (по правилу)", {"take_profit_percent": -1.0}, None),
    Cell("take_profit_percent=валидное", {"take_profit_percent": 0.5}, None),
]


def _trailing_field_cells(name: str, title: str) -> list[Cell]:
    """Общая часть трёх полей трейлинга: тип и `isfinite` — на обоих рядах.

    Знак и парность проверяются только при `trailing_take_profit=True` —
    это добавляется отдельно вызывающим кодом, не здесь.
    """
    return [
        Cell(f"{name}=чужой тип", {name: "чужое"}, TypeError, GOT),
        Cell(f"{name}=булево", {name: True}, TypeError, GOT),
        Cell(f"{name}=nan", {name: NAN}, ValueError, "не число"),
        Cell(f"{name}=inf", {name: INF}, ValueError, "не число"),
        Cell(
            f"{name}=отрицательное на умолчаниях (трейлинг выключен — поле не влияет)",
            {name: -5.0}, None,
        ),
        Cell(f"{name}=валидное на умолчаниях", {name: 0.5}, None),
    ]


_TRAILING_START_CELLS = [
    *_trailing_field_cells("trailing_start_percent", "порог включения"),
    # Второй базовый ряд: трейлинг включён, парная проверка против отступа.
    # ⚠️ Клетка ниже — не про значение, про ПОРЯДОК проверок. Тип и парность
    # проверяются РАЗНЫМИ методами (`_check_percent_fields` и
    # `_check_trailing_pair`); на базовом ряду 1 трейлинг выключен,
    # и `_check_trailing_pair` возвращается сразу — порядок вызовов
    # методов там ни на что не влияет. Только здесь, при включённом
    # трейлинге, чужой тип доходит до сравнения `<=` в парной проверке,
    # если её вызвать раньше проверки типа: `"чужое" <= 0.2` — естественная
    # ошибка Python без фразы «получено», а не написанный отказ.
    Cell(
        "trailing_start_percent=чужой тип, трейлинг включён (порядок проверок)",
        {"trailing_take_profit": True, "trailing_start_percent": "чужое"}, TypeError, GOT,
    ),
    Cell(
        "trailing_start_percent=0, трейлинг включён (<= отступа 0,2 умолчания)",
        {"trailing_take_profit": True, "trailing_start_percent": 0}, ValueError, "больше отступа",
    ),
    Cell(
        "trailing_start_percent=отрицательное, трейлинг включён",
        {"trailing_take_profit": True, "trailing_start_percent": -1.0},
        ValueError, "больше отступа",
    ),
    Cell(
        "trailing_start_percent=валидное, трейлинг включён",
        {"trailing_take_profit": True, "trailing_start_percent": 0.5}, None,
    ),
]

_TRAILING_OFFSET_CELLS = [
    *_trailing_field_cells("trailing_offset_percent", "отступ"),
    # ⚠️ Слепая клетка из §3 плана: без второго базового ряда «отступ
    # отрицательный + трейлинг включён» структурно недостижима, и удаление
    # этой проверки не уронило бы ничего.
    Cell(
        "trailing_offset_percent=чужой тип, трейлинг включён (порядок проверок)",
        {"trailing_take_profit": True, "trailing_offset_percent": "чужое"}, TypeError, GOT,
    ),
    Cell(
        "trailing_offset_percent=0, трейлинг включён",
        {"trailing_take_profit": True, "trailing_offset_percent": 0}, ValueError, "положительным",
    ),
    Cell(
        "trailing_offset_percent=отрицательное, трейлинг включён",
        {"trailing_take_profit": True, "trailing_offset_percent": -1.0},
        ValueError, "положительным",
    ),
    Cell(
        "trailing_offset_percent=равен порогу умолчания (0,5, не строго больше)",
        {"trailing_take_profit": True, "trailing_offset_percent": 0.5},
        ValueError, "больше отступа",
    ),
    Cell(
        "trailing_offset_percent=валидное, трейлинг включён",
        {"trailing_take_profit": True, "trailing_offset_percent": 0.1}, None,
    ),
]

_TRAILING_STEP_CELLS = [
    *_trailing_field_cells("trailing_step_percent", "шаг подтяжки"),
    # ⚠️ Вторая слепая клетка из §3 плана.
    Cell(
        "trailing_step_percent=чужой тип, трейлинг включён (порядок проверок)",
        {"trailing_take_profit": True, "trailing_step_percent": "чужое"}, TypeError, GOT,
    ),
    Cell(
        "trailing_step_percent=0, трейлинг включён (разрешён, `< 0`, не `<= 0`)",
        {"trailing_take_profit": True, "trailing_step_percent": 0}, None,
    ),
    Cell(
        "trailing_step_percent=отрицательное, трейлинг включён",
        {"trailing_take_profit": True, "trailing_step_percent": -1.0},
        ValueError, "отрицательным",
    ),
    Cell(
        "trailing_step_percent=валидное, трейлинг включён",
        {"trailing_take_profit": True, "trailing_step_percent": 0.5}, None,
    ),
]

#: `commission_per_side` — `None` допущен явно («тариф не задан»), явный
#: `0.0` — тоже допущен явно (разобрано 03.09.2026, граничный тест
#: `tests/test_engine_take_profit.py:1947`). Единственное поле с обоими
#: допущениями сразу.
_COMMISSION_CELLS: list[Cell] = [
    Cell("commission_per_side=чужой тип", {"commission_per_side": "чужое"}, TypeError, GOT),
    Cell("commission_per_side=булево", {"commission_per_side": True}, TypeError, GOT),
    Cell(
        "commission_per_side=nan", {"commission_per_side": NAN}, ValueError, "не может быть такой",
    ),
    Cell(
        "commission_per_side=inf", {"commission_per_side": INF}, ValueError, "не может быть такой",
    ),
    Cell(
        "commission_per_side=-inf",
        {"commission_per_side": NEG_INF}, ValueError, "не может быть такой",
    ),
    Cell("commission_per_side=0 (явный ноль допущен)", {"commission_per_side": 0}, None),
    Cell(
        "commission_per_side=отрицательное",
        {"commission_per_side": -1.0}, ValueError, "не может быть такой",
    ),
    Cell("commission_per_side=валидное", {"commission_per_side": 0.5}, None),
    Cell("commission_per_side=None (тариф не задан)", {"commission_per_side": None}, None),
]

#: `ruble_per_point` — строго больше нуля, `None` не допущен (в отличие
#: от комиссии: это не «тариф не задан», а масштаб самого расчёта).
_RUBLE_PER_POINT_CELLS: list[Cell] = [
    Cell("ruble_per_point=чужой тип", {"ruble_per_point": "чужое"}, TypeError, GOT),
    Cell("ruble_per_point=булево", {"ruble_per_point": True}, TypeError, GOT),
    Cell("ruble_per_point=None", {"ruble_per_point": None}, TypeError, GOT),
    Cell("ruble_per_point=nan", {"ruble_per_point": NAN}, ValueError, "больше нуля"),
    Cell("ruble_per_point=inf", {"ruble_per_point": INF}, ValueError, "больше нуля"),
    Cell("ruble_per_point=-inf", {"ruble_per_point": NEG_INF}, ValueError, "больше нуля"),
    Cell("ruble_per_point=0", {"ruble_per_point": 0}, ValueError, "больше нуля"),
    Cell("ruble_per_point=отрицательное", {"ruble_per_point": -1.0}, ValueError, "больше нуля"),
    Cell("ruble_per_point=валидное", {"ruble_per_point": 0.5}, None),
]

#: `close_wait_bars` — целое, [1; `MAX_CLOSE_WAIT_BARS`]. Единственное
#: числовое поле без `float`: `bool` исключён так же явно, как у остальных.
_CLOSE_WAIT_BARS_CELLS: list[Cell] = [
    Cell("close_wait_bars=чужой тип", {"close_wait_bars": "чужое"}, TypeError, GOT),
    Cell("close_wait_bars=булево", {"close_wait_bars": True}, TypeError, GOT),
    Cell("close_wait_bars=None", {"close_wait_bars": None}, TypeError, GOT),
    Cell("close_wait_bars=дробное", {"close_wait_bars": 0.5}, TypeError, GOT),
    Cell("close_wait_bars=0", {"close_wait_bars": 0}, ValueError, "хотя бы одну свечу"),
    Cell(
        "close_wait_bars=отрицательное",
        {"close_wait_bars": -1}, ValueError, "хотя бы одну свечу",
    ),
    Cell("close_wait_bars=1 (нижняя граница)", {"close_wait_bars": 1}, None),
    Cell(
        "close_wait_bars=MAX_CLOSE_WAIT_BARS (верхняя граница)",
        {"close_wait_bars": MAX_CLOSE_WAIT_BARS}, None,
    ),
    Cell(
        "close_wait_bars=MAX_CLOSE_WAIT_BARS + 1",
        {"close_wait_bars": MAX_CLOSE_WAIT_BARS + 1}, ValueError, "дольше",
    ),
]

CELLS: list[Cell] = [
    *_enum_cells(),
    *_bool_cells(),
    *_VOLUME_CELLS,
    *_TAKE_PROFIT_PERCENT_CELLS,
    *_TRAILING_START_CELLS,
    *_TRAILING_OFFSET_CELLS,
    *_TRAILING_STEP_CELLS,
    *_COMMISSION_CELLS,
    *_RUBLE_PER_POINT_CELLS,
    *_CLOSE_WAIT_BARS_CELLS,
]


def test_the_matrix_is_not_empty() -> None:
    """Сторож на сам список: пустой список параметризации проходит тихо."""
    assert len(CELLS) > 80, len(CELLS)


@pytest.mark.parametrize("cell", CELLS, ids=lambda cell: cell.label)
def test_frozen_validation_outcome(cell: Cell) -> None:
    """Клетка матрицы: сегодняшний код обязан вести себя ровно так.

    Не тест «это правильно» — тест «это не изменилось незаметно». Отказ
    здесь после правки `__post_init__` означает: разбор на методы стёр
    или ослабил проверку, а не то, что сама проверка была неверной.
    """
    if cell.outcome is None:
        EngineSettings(**cell.kwargs)  # type: ignore[arg-type]  # матрица — гетерогенные kwargs
        return
    with pytest.raises(cell.outcome) as excinfo:
        EngineSettings(**cell.kwargs)  # type: ignore[arg-type]  # см. выше
    if cell.match is not None:
        assert cell.match in str(excinfo.value), (
            f"{cell.label}: ожидали подстроку {cell.match!r}, получили "
            f"{excinfo.value!r} — похоже на естественную ошибку Python, "
            "а не на написанную фразу (проверка значения встала раньше "
            "проверки типа)"
        )


# --------------------------------------------------------------------------
# Порядок между группами: клетки выше варьируют не больше одного поля разом
# и слепы к перестановке двух соседних вызовов _check_* в __post_init__.
# --------------------------------------------------------------------------

#: Одна пара на каждую смежную границу между восемью группами проверок
#: `EngineSettings.__post_init__`, в документированном порядке: перечисления
#: → булевы → объём → проценты → парная → комиссия → рубль в пункте →
#: ожидание выхода. В каждой паре **оба** поля негодны одновременно;
#: подстрока — фраза ИЗ ЛЕВОЙ (более ранней по порядку) группы. Переставь
#: два соседних вызова местами — первой сработает правая группа, и подстрока
#: перестанет встречаться в тексте отказа.
#:
#: Найдено ревью 04.09.2026: ни одна клетка `CELLS` выше не варьирует больше
#: одного поля разом, и перестановка `_check_bool_fields`/`_check_enum_fields`
#: проходила молча (1804 passed что со здоровым, что с перепутанным
#: порядком) — сторожа на ПОРЯДОК при нескольких негодных полях не было.
_ADJACENT_ORDER_BOUNDARIES: tuple[tuple[str, dict[str, object], str], ...] = (
    (
        "перечисления -> булевы",
        {"mode": "чужое", "close_on_time_end": "чужое"},
        "engine.Mode",
    ),
    (
        "булевы -> объём",
        {"trailing_take_profit": "чужое", "volume": -5.0},
        "булево",
    ),
    (
        "объём -> проценты",
        {"volume": -5.0, "take_profit_percent": "чужое"},
        "положительным",
    ),
    (
        "проценты -> парная",
        {
            "take_profit_percent": "чужое",
            "trailing_take_profit": True, "trailing_offset_percent": -1.0,
        },
        "число",
    ),
    (
        "парная -> комиссия",
        {
            "trailing_take_profit": True, "trailing_offset_percent": -1.0,
            "commission_per_side": -5.0,
        },
        "положительным",
    ),
    (
        "комиссия -> рубль в пункте",
        {"commission_per_side": -5.0, "ruble_per_point": -5.0},
        "не может быть такой",
    ),
    (
        "рубль в пункте -> ожидание выхода",
        {"ruble_per_point": -5.0, "close_wait_bars": 0},
        "больше нуля",
    ),
)


@pytest.mark.parametrize(
    "boundary,kwargs,expected",
    _ADJACENT_ORDER_BOUNDARIES,
    ids=[item[0] for item in _ADJACENT_ORDER_BOUNDARIES],
)
def test_the_order_of_checks_wins_on_the_earlier_group(
    boundary: str, kwargs: dict[str, object], expected: str
) -> None:
    """При двух негодных полях разом побеждает более ранняя по порядку группа.

    Не про значение — про ПОРЯДОК вызовов внутри `__post_init__`. Отказ
    здесь после правки означает: относительный порядок двух соседних
    `_check_*` поменялся, и владелец счёта получит текст про не то поле —
    то есть починит не то, на что ему указали.
    """
    with pytest.raises((TypeError, ValueError)) as excinfo:
        EngineSettings(**kwargs)  # type: ignore[arg-type]  # см. Cell выше
    assert expected in str(excinfo.value), (
        f"{boundary}: ожидали подстроку {expected!r} (более ранняя по "
        f"порядку группа), получили {excinfo.value!r} — похоже, порядок "
        "проверок в __post_init__ поменялся"
    )
