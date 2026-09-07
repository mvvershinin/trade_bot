"""Свеча и таймфрейм: время закрытия, часовой пояс, допустимые размеры.

Отсюда движок берёт время закрытия свечи, а торговое окно считается именно
по нему и строгими неравенствами (ARCHITECTURE.md §6). Ошибка здесь
не падает — она даёт другой список сделок.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from market.candles import M5, MINUTE, MSK, Candle, Timeframe, ensure_msk, floor_to_minute

from market_helpers import ALLOWED_TIMEFRAMES


def test_msk_is_utc_plus_three() -> None:
    assert MSK.utcoffset(None) == timedelta(hours=3)


def test_msk_has_no_daylight_saving() -> None:
    """Смещение одно и то же в январе и в июле.

    Проверка стоит здесь ради того, чтобы подмена MSK на `zoneinfo` какой-нибудь
    зоны с переводом часов была видна сразу: в конце октября окно уехало бы
    на час и сделки стали бы другими.
    """
    winter = datetime(2026, 1, 15, 12, tzinfo=MSK)
    summer = datetime(2026, 7, 15, 12, tzinfo=MSK)
    autumn = datetime(2026, 10, 25, 12, tzinfo=MSK)
    assert winter.utcoffset() == summer.utcoffset() == autumn.utcoffset() == timedelta(hours=3)


def test_ensure_msk_rejects_naive_time() -> None:
    """Наивное время — отказ, а не догадка «наверное, это МСК»."""
    with pytest.raises(ValueError, match="без часового пояса"):
        ensure_msk(datetime(2026, 8, 26, 10, 5))


def test_ensure_msk_converts_from_utc() -> None:
    utc = datetime(2026, 8, 26, 7, 5, tzinfo=timezone.utc)
    assert ensure_msk(utc) == datetime(2026, 8, 26, 10, 5, tzinfo=MSK)
    assert ensure_msk(utc).hour == 10


#: Что валидатор обязан принимать — **список от руки, независимый оракул**.
#: Делители часа до 60 включительно и кратные часу делители суток.
#: `market_helpers.ALLOWED_TIMEFRAMES` выведен перебором **через сам
#: валидатор** и потому сам по себе ничего не доказывает; доказывает
#: их совпадение.
EXPECTED_TIMEFRAMES = (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60, 120, 180, 240, 360, 480, 720, 1440)


def test_the_set_of_allowed_timeframes_is_exactly_this() -> None:
    """Состав допустимых размеров свечи задан здесь и нигде больше.

    Списков «всех допустимых таймфреймов» в тестах было два, они не совпадали
    друг с другом (в одном не было 4, 6 и 12, в другом — 180, 360 и 480),
    и ни один не был выведен из валидатора. «Проверено на всех размерах»
    означало «проверено на тех, что вспомнились».
    """
    assert ALLOWED_TIMEFRAMES == EXPECTED_TIMEFRAMES


@pytest.mark.parametrize("minutes", ALLOWED_TIMEFRAMES)
def test_allowed_timeframes(minutes: int) -> None:
    assert Timeframe(minutes).minutes == minutes


def test_rejected_timeframes() -> None:
    """Размер, не делящий час или сутки нацело, отвергается.

    У такого размера нет однозначной границы бара: «начало» свечи начинает
    зависеть от того, с какой даты вести отсчёт. Проверяется **каждая**
    минута суток, а не выборка из десяти значений.
    """
    accepted_but_should_not_be = [
        minutes
        for minutes in range(1, 1441)
        if minutes not in EXPECTED_TIMEFRAMES and _is_accepted(minutes)
    ]
    assert not accepted_but_should_not_be, f"валидатор принял неделящие размеры: {accepted_but_should_not_be}"


def _is_accepted(minutes: int) -> bool:
    try:
        Timeframe(minutes)
    except ValueError:
        return False
    return True


@pytest.mark.parametrize("minutes", [0, -1, -5, 1441, 2880])
def test_timeframes_outside_the_day_are_rejected(minutes: int) -> None:
    with pytest.raises(ValueError):
        Timeframe(minutes)


def test_timeframe_default_names() -> None:
    assert Timeframe(5).name == "Min5"
    assert Timeframe(60).name == "Hour1"
    assert Timeframe(240).name == "Hour4"


def test_close_time_is_start_plus_timeframe() -> None:
    """Хранится начало свечи, закрытие = начало + таймфрейм.

    Пятиминутка 10:05 закрывается в 10:10. Движок считает окно по закрытию,
    и свеча, закрывшаяся ровно в 10:05, в окно 10:05–11:00 не попадает —
    но это уже его дело, здесь проверяется только само число.
    """
    candle = Candle(
        time=datetime(2026, 8, 26, 10, 5, tzinfo=MSK),
        open=1, high=1, low=1, close=1, volume=1,
        timeframe=M5, filled_minutes=5,
    )
    assert candle.close_time == datetime(2026, 8, 26, 10, 10, tzinfo=MSK)
    assert not candle.is_partial


def test_partial_flag_is_about_missing_minutes() -> None:
    candle = Candle(
        time=datetime(2026, 8, 26, 10, 5, tzinfo=MSK),
        open=1, high=1, low=1, close=1, volume=1,
        timeframe=M5, filled_minutes=3,
    )
    assert candle.is_partial
    assert not candle.unsettled


def test_minute_candle_is_full_by_default() -> None:
    candle = Candle(
        time=datetime(2026, 8, 26, 10, 5, tzinfo=MSK),
        open=1, high=1, low=1, close=1, volume=1,
    )
    assert candle.timeframe == MINUTE
    assert not candle.is_partial
    assert candle.close_time == datetime(2026, 8, 26, 10, 6, tzinfo=MSK)


# -- ключ минутки: минута, а не момент ---------------------------------------


@pytest.mark.parametrize(
    ("second", "microsecond"),
    [(0, 0), (7, 0), (47, 500_000), (59, 999_999)],
)
def test_floor_to_minute_drops_seconds(second: int, microsecond: int) -> None:
    """Секунды отбрасываются, минута не меняется.

    Биржа метит минутку `10:05:00`, поток брокера может прислать ту же минутку
    как `10:05:07`. Это одна минутка. Округление вверх сделало бы из свечи
    минуты 10:05 свечу минуты 10:06 — сдвиг всего ряда на бар.
    """
    moment = datetime(2026, 8, 26, 10, 5, second, microsecond, tzinfo=MSK)
    assert floor_to_minute(moment) == datetime(2026, 8, 26, 10, 5, tzinfo=MSK)


def test_floor_to_minute_converts_to_moscow() -> None:
    utc = datetime(2026, 8, 26, 7, 5, 30, tzinfo=timezone.utc)
    assert floor_to_minute(utc) == datetime(2026, 8, 26, 10, 5, tzinfo=MSK)


def test_floor_to_minute_rejects_naive_time() -> None:
    with pytest.raises(ValueError, match="без часового пояса"):
        floor_to_minute(datetime(2026, 8, 26, 10, 5, 7))


# -- полнота свечи не выдумывается -------------------------------------------

MARKET_DIR = pathlib.Path(__file__).resolve().parent.parent / "market"

#: Единственный модуль слоя, которому положено создавать свечи крупнее минуты:
#: он один считает `filled_minutes` из того, что реально собрал.
ASSEMBLER = "aggregate.py"

#: Сколько модулей в слое сейчас. Число нужно не само по себе: `rglob` по
#: несуществующему каталогу возвращает пустоту **без ошибки**, и при
#: перестройке раскладки сторож прошёл бы вхолостую, ничего не просмотрев.
MODULES_IN_LAYER_AT_LEAST = 8


def candle_names(module_tree: ast.Module) -> set[str]:
    """Как в этом модуле называется класс `Candle`, включая псевдонимы.

    `from market.candles import Candle as C` — законный питон и обход сторожа,
    ищущего строку «Candle».
    """
    found = {"Candle"}
    for element in ast.walk(module_tree):
        if isinstance(element, ast.ImportFrom):
            for alias in element.names:
                if alias.name == "Candle" and alias.asname:
                    found.add(alias.asname)
        elif isinstance(element, ast.Import):
            for alias in element.names:
                if alias.name.endswith(".Candle") and alias.asname:
                    found.add(alias.asname)
    return found


def is_literal_minute(expression: ast.expr | None) -> bool:
    """Записано ли в `timeframe` буквально `MINUTE`."""
    return expression is not None and (
        (isinstance(expression, ast.Name) and expression.id == "MINUTE")
        or (isinstance(expression, ast.Attribute) and expression.attr == "MINUTE")
    )


def test_only_the_assembler_builds_candles_bigger_than_a_minute() -> None:
    """Свечу с таймфреймом больше минуты в слое строит только сборка.

    Это и есть «расхождение невозможно по построению» (ARCHITECTURE.md §1).
    Любое другое место, создающее свечу нужного размера, обязано откуда-то
    взять её полноту — а взять её неоткуда, кроме как посчитав минутки.
    Так и появилось `filled_minutes = длина интервала` в разборе ответа ISS:
    скачанная пятиминутка всегда «полная», собранная — честная, и при
    настройке «пропускать неполные свечи» один отрезок давал два разных ряда.

    Проверка читает исходники, а не поведение: поведение здесь ничем
    не отличается, пока кто-нибудь не включит настройку.

    Сторож закрывает пять обходов, а не один:

    1. `Candle(...)` с `timeframe=` не-`MINUTE`;
    2. **позиционные** аргументы — `timeframe` седьмым по счёту;
    3. `Candle(**строка)` — что внутри словаря, неизвестно;
    4. **псевдоним** при импорте: `from ... import Candle as C`;
    5. `свеча.replace(timeframe=...)` — законный питон и обход по построению.

    Плюс сам факт, что каталог найден и файлы просмотрены: `rglob` по
    несуществующему пути возвращает пустоту без ошибки, и сторож
    прошёл бы вхолостую.
    """
    check_layer(MARKET_DIR)


def check_layer(directory: pathlib.Path) -> None:
    """Сама проверка. Вынесена отдельно, чтобы её можно было натравить
    на подсаженный «слой» с каждым известным обходом и убедиться, что она
    падает: «сторож есть» и «сторож работает» — разные утверждения."""
    assert directory.is_dir(), f"каталог слоя не найден: {directory}"
    files = sorted(directory.rglob("*.py"))
    assert len(files) >= MODULES_IN_LAYER_AT_LEAST, (
        f"просмотрено файлов: {len(files)}. Сторож ничего не проверил — "
        "скорее всего, раскладку слоя перестроили, а путь остался прежним"
    )

    culprits: list[str] = []
    inspected = 0
    for source in files:
        if source.name == ASSEMBLER:
            continue
        inspected += 1
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        candle_aliases = candle_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)

            if name in candle_aliases:
                if node.args:
                    culprits.append(
                        f"{source.name}:{node.lineno} (позиционные аргументы — "
                        "какой из них таймфрейм, по вызову не видно)"
                    )
                    continue
                if any(kw.arg is None for kw in node.keywords):
                    culprits.append(
                        f"{source.name}:{node.lineno} (Candle(**словарь) — "
                        "что внутри, неизвестно)"
                    )
                    continue
                timeframe = next(
                    (kw.value for kw in node.keywords if kw.arg == "timeframe"), None
                )
                # Явная минутка — единственная свеча, полноту которой может
                # назвать кто угодно: одна минута из одной.
                if timeframe is None or is_literal_minute(timeframe):
                    continue
                culprits.append(f"{source.name}:{node.lineno} (timeframe=…)")

            elif name == "replace":
                replaced_timeframe = next(
                    (kw.value for kw in node.keywords if kw.arg == "timeframe"), None
                )
                if replaced_timeframe is not None and not is_literal_minute(replaced_timeframe):
                    culprits.append(
                        f"{source.name}:{node.lineno} (replace(timeframe=…) — "
                        "свеча меняет размер, а полнота остаётся прежней)"
                    )

    assert inspected >= MODULES_IN_LAYER_AT_LEAST - 1, "просмотрено слишком мало файлов"
    assert not culprits, (
        "свечу крупнее минуты в слое собирает только "
        f"market/{ASSEMBLER}, а её строят ещё и здесь: {', '.join(culprits)}. "
        "Полноту такой свечи надо откуда-то взять, и взять её неоткуда"
    )


@pytest.mark.parametrize(
    ("way_around", "source_text"),
    [
        (
            "timeframe=",
            "from market.candles import Candle, M5\n"
            "c = Candle(time=t, open=1, high=1, low=1, close=1, volume=1, timeframe=M5)\n",
        ),
        (
            "позиционный",
            "from market.candles import Candle, M5\nc = Candle(t, 1, 1, 1, 1, 1, M5, 5)\n",
        ),
        (
            "словарь",
            "from market.candles import Candle\nc = Candle(**строка)\n",
        ),
        (
            "псевдоним",
            "from market.candles import Candle as C, M5\n"
            "c = C(time=t, open=1, high=1, low=1, close=1, volume=1, timeframe=M5)\n",
        ),
        (
            "replace",
            "c = свеча.replace(timeframe=M5)\n",
        ),
    ],
)
def test_the_guard_catches_every_way_around_it(
    way_around: str, source_text: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Каждый обход сторожа проверен подсадкой: сторож обязан на нём упасть.

    Без этого «сторож есть» и «сторож работает» — разные утверждения,
    и второе не проверено ничем.
    """
    fake_layer = tmp_path / "market"
    fake_layer.mkdir()
    for i in range(MODULES_IN_LAYER_AT_LEAST):
        (fake_layer / f"чистый{i}.py").write_text("x = 1\n", encoding="utf-8")
    (fake_layer / ASSEMBLER).write_text("y = 1\n", encoding="utf-8")
    (fake_layer / "нарушитель.py").write_text(source_text, encoding="utf-8")

    with pytest.raises(AssertionError, match="строят ещё и здесь"):
        check_layer(fake_layer)


def test_the_guard_fails_loudly_if_the_layer_moved(tmp_path: pathlib.Path) -> None:
    """Каталога нет — сторож падает, а не проходит вхолостую."""
    with pytest.raises(AssertionError, match="каталог слоя не найден"):
        check_layer(tmp_path / "нет-такого")


def test_the_guard_fails_if_it_saw_almost_nothing(tmp_path: pathlib.Path) -> None:
    """Каталог есть, а файлов в нём почти нет — тоже отказ.

    Так выглядит перестройка раскладки: путь цел, содержимое уехало.
    """
    almost_empty = tmp_path / "market"
    almost_empty.mkdir()
    (almost_empty / "один.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="Сторож ничего не проверил"):
        check_layer(almost_empty)
