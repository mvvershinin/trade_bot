"""Лидеры перебора → шаблоны: посчитано ровно то, что записано в шаблон.

Слой сборки здесь ничего не считает. Он разворачивает рецепт сетки в набор
окна, кладёт набор в библиотеку шаблонов и пишет два прогона в журнал.
Каждый тест стережёт одну вещь из этого списка и прогнан против испорченного
кода — мутационная проба лежит в отчёте методолога.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from app import convert
from app.leaders import (
    MACHINE_MARK,
    NAME_ROOM,
    origin_of,
    record,
    settings_of,
    template_name,
    template_of,
)
from backtest.history import HistoryRun
from backtest.leaders import Stretch, point_of, recipes, reference_recipes
from backtest.split import Period
from market.candles import M5, Candle
from market.storage import CandleStore
from ui.formatting import MSK
from ui.models import Mode, Settings
from ui.settings_codec import field_codecs
from ui.templates import EXAMPLES_DIR_NAME, Library

BASE = Settings(instrument="MXU6", price_step=25.0)


# ------------------------------------------- посчитано то же, что записано

def test_the_two_expansions_of_a_recipe_agree_over_the_whole_grid():
    """Набор окна и настройки движка, полученные из одного рецепта, совпадают.

    ⚠️ Это главный сторож задачи после отбора лидеров. Сетку разворачивают
    два слоя: торговый — в настройки движка, сборка — в набор окна, который
    ляжет в шаблон. Разойдись развороты хоть в одном поле — шаблон обещал бы
    одно, а посчитано было бы другое, и заметить это было бы нечем: обе
    таблицы выглядят одинаково.

    Проверяется **вся** сетка, а не образец: развороты трогают шесть полей,
    и ошибка в одном сочетании из девяти тысяч тоже ошибка.
    """
    engine = convert.engine_settings(BASE, Mode.REVERSE)
    strategy = convert.strategy_settings(BASE)
    for recipe in recipes():
        values = settings_of(recipe, BASE)
        point = point_of(recipe, engine, strategy)
        assert convert.engine_settings(values, Mode.REVERSE) == point.engine, recipe.label
        assert convert.strategy_settings(values) == point.strategy, recipe.label


def test_reference_rows_agree_too():
    """Опорные строки свода разворачиваются так же, как строки сетки."""
    engine = convert.engine_settings(BASE, Mode.REVERSE)
    strategy = convert.strategy_settings(BASE)
    for recipe, _title in reference_recipes():
        values = settings_of(recipe, BASE)
        assert convert.engine_settings(values, Mode.REVERSE) == point_of(
            recipe, engine, strategy
        ).engine, recipe.label


def test_the_base_settings_are_not_touched_by_the_grid():
    """Сетка меняет шесть полей и ни одного больше.

    Всё, чего сетка не касается — инструмент, объём, комиссия, календарь,
    предохранители, — обязано доехать до шаблона таким, каким пришло.
    Иначе применение шаблона поменяло бы владельцу счёта то, чего он
    не выбирал и что в своде не мерялось.
    """
    touched = {
        "window_start", "window_end", "average_period",
        "take_profit_enabled", "take_profit_pct",
        "reversal_moment", "after_take_profit",
    }
    values = settings_of(recipes()[4242], BASE)
    changed = {
        name for name in field_codecs()
        if getattr(values, name) != getattr(BASE, name)
    }
    assert changed <= touched, changed - touched


# ------------------------------------------------------------------ шаблоны

ORIGIN = "@MX 06.25–02.26"


def test_an_example_is_marked_numbered_and_names_its_origin():
    """Пример отличим в списке и говорит, на чём он отобран.

    Происхождение обязательно (решение 0051): без «на чём отобран» набор
    читается как рекомендация, а подбор в этом проекте дважды проиграл
    бездействию.
    """
    name = template_name(3, recipes()[0], origin=ORIGIN)
    assert name.startswith(MACHINE_MARK)
    assert "03" in name
    assert ORIGIN in name


def test_the_origin_names_the_instrument_and_the_period():
    """Короткое происхождение — инструмент и границы отрезка подбора."""
    said = origin_of("@MX", Period(date(2025, 6, 19), date(2026, 2, 14)))
    assert "@MX" in said
    assert "06.25" in said
    assert "02.26" in said


def test_every_example_name_fits_the_library_limit():
    """Имя не обрезается библиотекой: обрезка съела бы происхождение.

    Проверяется вся сетка, а не образец: длина растёт от самого длинного окна,
    самой длинной средней и самого длинного тейка сразу, и угадать это
    сочетание глазами нельзя.
    """
    for place, recipe in enumerate(recipes(), start=1):
        name = template_name(min(place, 99), recipe, origin=ORIGIN)
        assert len(name) <= NAME_ROOM, f"{len(name)}: {name}"


def test_a_name_too_long_is_refused_loudly():
    """Слишком длинное имя — отказ вслух, а не молча обрезанный хвост."""
    with pytest.raises(ValueError, match="не поместится"):
        template_name(1, recipes()[0], origin="я" * NAME_ROOM)


def test_an_example_carries_its_origin_in_a_field_too(tmp_path):
    """Происхождение лежит и в имени, и в поле: имя переживёт старую сборку."""
    one = template_of(
        1, recipes()[0], BASE, saved_at=datetime.now(tz=MSK),
        origin=ORIGIN, full_origin="отобран перебором 9744 сочетаний на @MX",
    )
    assert ORIGIN in one.name
    assert one.origin.startswith("отобран перебором")


def test_a_template_carries_no_money(tmp_path):
    """В файле примеров нет ни одного поля с деньгами прогона.

    `ui/templates.py` отказывается их хранить намеренно: цифры берутся
    из записанных прогонов, а считать их отдельно значило бы завести вторую
    правду о деньгах. Соблазн велик именно здесь — свод уже посчитан.
    """
    library = Library(tmp_path)
    library.write((template_of(
        1, recipes()[0], BASE, saved_at=datetime.now(tz=MSK), origin=ORIGIN,
    ),))
    stored = json.loads(library.path.read_text(encoding="utf-8"))
    known = set(field_codecs())
    for item in stored["templates"]:
        assert set(item["settings"]) <= known, set(item["settings"]) - known
        assert set(item) <= {"name", "saved_at", "settings", "origin"}


# ------------------------------------------------------- запись прогонов

def a_bar(day: date) -> Candle:
    """Одна пятиминутка названного дня. Числа в проверке не участвуют."""
    return Candle(
        time=datetime(day.year, day.month, day.day, 10, 0, tzinfo=MSK),
        open=100.0, high=101.0, low=99.0, close=100.5, volume=1.0,
        timeframe=M5, filled_minutes=5,
    )


def a_stretch(since: date, until: date) -> Stretch:
    """Отрезок с прогоном по нему.

    ⚠️ Свечи обязаны лежать внутри отрезка: запись называет период
    по **первой и последней свече**, а не по границам `Period`. В работе это
    так и есть — свечи отбирает `part`, — и тест повторяет то же условие.
    """
    return Stretch(
        period=Period(since, until),
        bars=(a_bar(since), a_bar(until)),
        run=HistoryRun(),
    )


TUNING = (date(2026, 5, 26), date(2026, 7, 17))
CHECKING = (date(2026, 7, 20), date(2026, 9, 4))


def test_the_checking_run_is_written_last(tmp_path):
    """Проверочный отрезок записывается последним — и потому виден в списке.

    Окно шаблонов показывает строкой **последний** записанный прогон
    (`ui/templates_dialog.py`: `runs[0]`, прогоны идут свежими первыми).
    Поменяй порядок — и в списке встанут пятнадцать зелёных чисел подбора,
    то есть ровно то, против чего заведено разделение подбора и проверки.
    """
    values = settings_of(recipes()[0], BASE)
    with CandleStore(tmp_path / "candles.sqlite3") as store:
        written = record(
            store, values, (a_stretch(*TUNING), a_stretch(*CHECKING)),
            timeframe=values.timeframe,
        )
        page = store.journal_sessions(limit=10)
    assert len(written) == 2
    assert written[0] < written[1]
    # ⚠️ Проверяется не «последний записан последним» — это верно при любом
    # порядке, — а **какой именно отрезок** оказался наверху списка.
    assert page.rows[0].id == written[1]
    assert "20.07.2026" in page.rows[0].note, page.rows[0].note
    assert "26.05.2026" in page.rows[1].note, page.rows[1].note


def test_every_template_finds_its_own_recorded_runs(tmp_path):
    """У каждого шаблона находятся оба его прогона, и первым — проверочный.

    Полный оборот: рецепт → набор окна → шаблон → запись прогонов → поиск
    прогонов набора тем же кодом, которым их ищет окно. Разойдись снимок
    настроек при записи и при поиске — шаблон показал бы «не запускался»,
    и вся затея потеряла бы смысл.
    """
    from app.runs import matching_runs

    database = tmp_path / "candles.sqlite3"
    sets = [settings_of(recipe, BASE) for recipe in recipes()[:3]]
    with CandleStore(database) as store:
        for values in sets:
            record(
                store, values, (a_stretch(*TUNING), a_stretch(*CHECKING)),
                timeframe=values.timeframe,
            )
    stats = matching_runs(database, sets)
    assert stats.trouble == ""
    for found in stats.runs:
        assert len(found) == 2, found
        assert found[0].period.startswith("20.07.2026")


# --------------------------------------- куда кладутся примеры (решение 0051)

def test_examples_go_beside_the_program_not_into_the_account_folder():
    """Файл примеров лежит в папке примеров, а не в `userdata/`.

    ⚠️ Рабочий список — собственность владельца счёта. Пятнадцать машинных
    наборов вперемешку с его собственными через месяц не разделить, а
    следующая поставка не смогла бы обновить примеры, не споря с его
    правками (решение 0051).
    """
    from app.leaders import EXAMPLES_FILE_NAME, arguments, examples_path

    where = examples_path(arguments([]))
    assert where.parent.name == EXAMPLES_DIR_NAME
    assert where.name == EXAMPLES_FILE_NAME
    assert "userdata" not in where.parts


def test_the_module_never_takes_the_account_data_folder():
    """`app/leaders.py` не берёт себе `userdata_dir` — ни под каким именем.

    Это сторож не стиля, а обещания: пока имя не взято, записать что-либо
    в папку владельца счёта модуль не может по построению. Проверяется
    разбором на токены, а не поиском подстроки: рядом живёт
    `ensure_userdata_dir`.
    """
    import ast
    import pathlib as _pathlib

    source = _pathlib.Path(__file__).resolve().parent.parent / "app" / "leaders.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    taken = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "userdata_dir" not in taken


def test_the_examples_file_holds_exactly_what_was_written():
    """Файл примеров пишется целиком: чужих записей в нём не появляется.

    Он принадлежит поставке, а не человеку, поэтому повторный прогон
    заменяет его, а не дописывает. Проверяется тем, что список, поданный
    на запись, и есть весь список.
    """
    made = tuple(
        template_of(place, recipe, BASE, saved_at=datetime.now(tz=MSK), origin=ORIGIN)
        for place, recipe in enumerate(recipes()[:3], start=1)
    )
    assert len({one.name for one in made}) == len(made)


# ---------------------------------------------------------------------------
# Перебор чужому алгоритму отказывает фразой, а не трассировкой
# ---------------------------------------------------------------------------


def test_a_foreign_algorithm_stops_the_sweep_with_words_and_a_return_code(
    monkeypatch, capsys
) -> None:
    """`python3 -m app.leaders` при чужом алгоритме говорит причину и уходит.

    ⚠️ Трассировка в консоль здесь была бы тем самым молчанием, которое
    дороже поломки (`CLAUDE.md` №13): человек видит стену Python и не видит
    ответа на свой вопрос «почему не подобралось».

    Мутация, обязанная ронять проверку: убрать `except ForeignStrategy`
    из `app/leaders.py::main` — тогда отказ вылетит трассировкой,
    а код возврата станет 1 от самого интерпретатора.
    """
    from app import leaders as app_leaders
    from backtest.sweep import ForeignStrategy

    async def refuse(_args) -> int:
        raise ForeignStrategy(
            "сетка перебора написана под торговый алгоритм «Реверс по "
            "скользящей средней» (ema_reverse), а выбран «second». "
            "Перебор не запущен."
        )

    monkeypatch.setattr(app_leaders, "work", refuse)
    code = app_leaders.main(["--db", "нет.sqlite3"])
    said = capsys.readouterr().err
    assert code == 2, f"отказ вернул код {code}, а не отдельный код отказа"
    assert "Перебор не запущен" in said, "отказ не доехал до человека"
    assert "second" in said, "отказ не называет, какой алгоритм выбран"
