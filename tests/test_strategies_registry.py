"""Реестр торговых модулей: таблица, умолчание и громкий отказ.

Пять свойств, каждое из которых ломается одной строкой и при этом не падает
ни на одном другом прогоне.

1. **Реестр — данные, а не ветвление.** `if id == "ema": … elif …` пришлось бы
   повторить в сборке модуля, в подписи для окна, в таблице полей и в снимке
   прогона; забытая ветка — это молча не тот модуль. Разбор `ast`: ни одно
   условие в `strategies/registry.py` не сравнивается с именем модуля.

2. **Ни одного динамического импорта.** Nuitka линкует то, что видит
   статически. Реестр, собранный `importlib`/`pkgutil`/точками входа, дал бы
   **пустой список модулей в собранной программе** при полностью зелёном
   прогоне тестов из исходников — дефект, невидимый до запуска на машине
   владельца счёта. В `app/` такой сторож уже стоит
   (`tests/test_app_boundaries.py::test_the_assembly_does_not_import_on_the_fly`),
   здесь — такой же на слой стратегий.

3. **Умолчание — модуль, которым торгуют сегодня.** ТЗ §4.8 («по умолчанию
   всегда выбрана скользящая средняя») и слова владельца счёта 08.09.2026.
   Уехавшее умолчание — это сборка, торгующая не тем правилом, о котором
   договаривались, и по экрану это не видно.

4. **Незнакомый `id` — отказ вслух, а не умолчание.** Правило 13 `CLAUDE.md`:
   молчание — самостоятельный дефект. Отказ обязан называть, какого модуля
   нет, какие есть и что делать дальше.

5. **Таблица полей полна и не выдумана.** Забытое поле молча получит
   умолчание вместо выбранного в окне; лишнее — уронит сборщик настроек
   не там, где причина.

⚠️ Записей с 09.09.2026 две, и свойства **механизма** по-прежнему проверяются
на подделках, собираемых тут же и в настоящий реестр не попадающих:
настоящий алгоритм ломать нельзя, а подделку — нужно. На настоящих записях
проверяются **данные**.

⚠️ `D-094` со вторым алгоритмом закрыт не тем, чем задумывался, и это
записано здесь, а не только в `BACKLOG.md`. Задумана была проверка
«множества полей окна, которые читает каждый модуль, попарно
не пересекаются». Второй алгоритм показал, что она бы **запрещала верное**:
он переиспользует класс настроек первого (владелец счёта: «все условия
те же»), и множества полей у них совпадают целиком — по замыслу, а не
по недосмотру. Настоящая опасность оказалась другой и живёт ниже:
**два алгоритма делят класс настроек и получают разные таблицы полей**.
Тогда одно и то же поле окна доезжает у них в разные поля настроек —
молча, при исправном виде окна.
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

from strategies import EmaReverse, EmaReverseSettings, StrategyEntry, registry

LAYER = pathlib.Path(__file__).resolve().parent.parent / "strategies"
REGISTRY_SOURCE = LAYER / "registry.py"
SOURCES = sorted(path for path in LAYER.rglob("*.py") if "__pycache__" not in path.parts)


@pytest.fixture(autouse=True)
def the_registry_is_left_alone():
    """Ни один тест этого файла не меняет состав настоящего реестра."""
    before = registry.known_ids()
    yield
    assert registry.known_ids() == before, (
        "тест изменил состав настоящего реестра — подделка досталась соседям"
    )


# --------------------------------------------------------------------------
# Реестр — таблица, а не ветвление
# --------------------------------------------------------------------------

def _tree(path: pathlib.Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _ids_named_in_conditions(tree: ast.AST, names: set[str]) -> list[str]:
    """Имена модулей, попавшие внутрь условия. Пусто — ветвления по имени нет."""
    found: list[str] = []
    for node in ast.walk(tree):
        tests: list[ast.AST] = []
        if isinstance(node, (ast.If, ast.IfExp, ast.While)):
            tests = [node.test]
        elif isinstance(node, ast.match_case):
            tests = [node.pattern]
        for test in tests:
            for inner in ast.walk(test):
                if isinstance(inner, ast.Constant) and inner.value in names:
                    found.append(f"строка {inner.lineno}: {inner.value!r}")
    return found


def test_the_registry_does_not_branch_on_the_name_of_a_module() -> None:
    """Ни одно условие реестра не сравнивается с именем модуля.

    Мутация, которую это ловит: `if strategy_id == "ema_reverse": return …`
    вместо прохода по таблице. Такая правка не падает ни на одном прогоне
    и всплывает при добавлении второго модуля — то есть тогда, когда её уже
    успели повторить в трёх местах.
    """
    guilty = _ids_named_in_conditions(_tree(REGISTRY_SOURCE), set(registry.known_ids()))
    assert not guilty, (
        "в реестре появилось ветвление по имени модуля:\n  " + "\n  ".join(guilty)
    )


def test_that_check_is_not_blind() -> None:
    """Канарейка: разбор действительно находит ветвление по имени."""
    planted = ast.parse(
        'def build(strategy_id):\n'
        '    if strategy_id == "ema_reverse":\n'
        '        return 1\n'
        '    return 0\n'
    )
    assert _ids_named_in_conditions(planted, {"ema_reverse"})
    innocent = ast.parse('def build(entry):\n    if entry.id:\n        return entry\n')
    assert not _ids_named_in_conditions(innocent, {"ema_reverse"})


#: Ветвление на функцию. Проход по таблице — это `for` плюс `if` плюс единица,
#: то есть три; всё, что выше, означает вернувшееся ветвление. Сборка модуля
#: считается отдельно: у неё один отказ и ничего больше.
COMPLEXITY_LIMIT = 3
NAMED_LIMITS = {"build": 2}

BRANCHING = (
    ast.If, ast.IfExp, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler,
    ast.match_case,
)


def _complexity(node: ast.AST) -> int:
    """Цикломатическая сложность тела: ветвления плюс единица.

    Включения (`[x for x in y]`) не считаются: это проход по таблице, а не
    ветвление, и наказывать за него значило бы толкать обратно к циклам с `if`.
    """
    total = 1
    for inner in ast.walk(node):
        if inner is node:
            continue
        if isinstance(inner, BRANCHING):
            total += 1
        elif isinstance(inner, ast.BoolOp):
            total += len(inner.values) - 1
    return total


def test_every_function_of_the_registry_stays_a_table_walk() -> None:
    """Сложность каждой функции реестра — не выше прохода по таблице."""
    tree = _tree(REGISTRY_SOURCE)
    functions = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert functions, "в реестре не разобрано ни одной функции — проверка вакуумна"
    too_complex = [
        f"{node.name}: {_complexity(node)} > {NAMED_LIMITS.get(node.name, COMPLEXITY_LIMIT)}"
        for node in functions
        if _complexity(node) > NAMED_LIMITS.get(node.name, COMPLEXITY_LIMIT)
    ]
    assert not too_complex, (
        "в реестр вернулось ветвление — таблица перестала быть таблицей:\n  "
        + "\n  ".join(too_complex)
    )


# --------------------------------------------------------------------------
# Ни одного динамического импорта во всём слое
# --------------------------------------------------------------------------

#: Приёмы, которыми принято собирать реестры модулей и которых здесь быть
#: не должно ни в одном виде: под Nuitka каждый из них даёт пустой список
#: модулей в собранной программе при зелёном прогоне из исходников.
LOADERS = {
    "importlib", "pkgutil", "__import__", "import_module", "find_spec",
    "iter_modules", "walk_packages", "entry_points", "load_module",
    "exec_module", "SourceFileLoader", "module_from_spec", "getattr_static",
}


def _loaders_used(tree: ast.AST) -> set[str]:
    """Имена загрузчиков в коде: импорты и обращения. Строки не смотрим.

    Строки не смотрим намеренно: в шапке реестра эти имена стоят списком
    того, чего там нет, и проверка по тексту сработала бы на объяснении.
    """
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            used |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            used.add(node.module.split(".")[0])
            used |= {alias.name for alias in node.names}
        elif isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
    return used & LOADERS


def test_the_layer_never_loads_a_module_by_name() -> None:
    """Реестр — таблица явных импортов; динамической загрузки нет во всём слое."""
    guilty = [
        f"{path.name}: {sorted(_loaders_used(_tree(path)))}"
        for path in SOURCES
        if _loaders_used(_tree(path))
    ]
    assert not guilty, (
        "в слое стратегий появилась загрузка модуля по имени. Под Nuitka такой "
        "реестр окажется ПУСТЫМ в собранной программе, а прогон тестов из "
        "исходников останется зелёным:\n  " + "\n  ".join(guilty)
    )


def test_the_loader_check_is_not_blind() -> None:
    """Канарейка: разбор ловит и импорт, и обращение по атрибуту."""
    assert _loaders_used(ast.parse("import importlib\n")) == {"importlib"}
    assert _loaders_used(ast.parse("from importlib import import_module\n")) == {
        "importlib", "import_module",
    }
    assert _loaders_used(ast.parse("x = __import__('strategies')\n")) == {"__import__"}
    assert _loaders_used(ast.parse("import dataclasses\n")) == set()
    # Имя загрузчика в строке — это объяснение, а не вызов.
    assert _loaders_used(ast.parse('"""importlib здесь запрещён"""\n')) == set()


# --------------------------------------------------------------------------
# Умолчание
# --------------------------------------------------------------------------

def test_the_default_is_the_module_that_trades_today() -> None:
    """Умолчание — реверс по скользящей средней, и это проверяется сборкой.

    ТЗ §4.8: «по умолчанию всегда выбрана скользящая средняя». Мутация
    `DEFAULT_ID` роняет этот тест; она же уронила бы сверку с прототипом,
    но сверка идёт на архиве, которого в свежем клоне нет, — здесь проверка
    работает всегда.
    """
    assert registry.DEFAULT_ID in registry.known_ids()
    entry = registry.default_entry()
    assert entry is registry.find(registry.DEFAULT_ID)
    assert entry.settings_type is EmaReverseSettings
    assert isinstance(entry.build(entry.defaults()), EmaReverse), (
        "умолчание реестра собирает не тот модуль, которым торгуют сегодня"
    )


def test_the_build_is_exactly_the_modules_it_declares() -> None:
    """Состав сборки назван поимённо: новый модуль — осознанная правка списка.

    ⚠️ Порядок значим: в этом же порядке алгоритмы стоят в окне выбора,
    и первый в списке — не умолчание. Умолчание отдельным полем
    (`DEFAULT_ID`), и его стережёт проверка выше.
    """
    assert registry.known_ids() == ("ema_reverse", "ma_crossing"), (
        "состав реестра изменился. Это не запрет: впишите новый модуль сюда "
        "вместе с проверками, которые он обязан пройти"
    )


# --------------------------------------------------------------------------
# Незнакомый id — отказ вслух
# --------------------------------------------------------------------------

def test_an_unknown_id_is_refused_out_loud() -> None:
    """Отказ называет, чего нет, что есть и что делать. Не `None` и не умолчание.

    ⚠️ Это сторож против **молчания**, а не против ошибки. Мутация «вернуть
    умолчание вместо отказа» оставила бы программу работающей и торгующей
    не тем правилом, что записано в настройках; мутация «вернуть `None`»
    уронила бы её в другом месте и без причины. Обе роняют этот тест.
    """
    with pytest.raises(registry.UnknownStrategy) as refusal:
        registry.find("ema_reverce")
    said = str(refusal.value)
    assert "ema_reverce" in said, "отказ не называет, какой модуль не найден"
    for known in registry.known_ids():
        assert known in said, f"отказ не называет модуль {known}, который есть"
    assert "настройк" in said.lower() or "обнови" in said.lower(), (
        "отказ не называет выход из положения — это молчаливый запрет "
        "(правило 13 CLAUDE.md)"
    )


@pytest.mark.parametrize("wrong", ["", " ", "EMA_REVERSE", "ema-reverse", "нет такого"])
def test_a_near_miss_does_not_quietly_become_the_default(wrong: str) -> None:
    """Пустое имя, чужой регистр и опечатка — отказ, а не подстановка модуля №1."""
    with pytest.raises(registry.UnknownStrategy):
        registry.find(wrong)


# --------------------------------------------------------------------------
# Данные записей
# --------------------------------------------------------------------------

def test_ids_and_titles_are_unique() -> None:
    """Два модуля с одинаковым `id` или названием неразличимы в журнале прогонов.

    Колонка `strategy` в базе прогонов текстовая (решение 0032): совпавшие
    названия делают разбор прошлого невозможным.
    """
    ids = [entry.id for entry in registry.entries()]
    titles = [entry.title for entry in registry.entries()]
    assert len(set(ids)) == len(ids), f"повторяющийся id модуля: {ids}"
    assert len(set(titles)) == len(titles), f"повторяющееся название: {titles}"


@pytest.mark.parametrize("entry", registry.entries(), ids=lambda item: item.id)
def test_the_id_is_latin_and_fit_for_a_settings_file(entry: StrategyEntry) -> None:
    """`id` уезжает в файл настроек и в шаблоны — только латиница (правило 6)."""
    assert entry.id.isascii(), f"нелатинский id модуля: {entry.id!r}"
    assert entry.id.isidentifier(), (
        f"id модуля не годится в имя ключа настроек: {entry.id!r}"
    )
    assert entry.id.islower(), f"id модуля пишется строчными: {entry.id!r}"


@pytest.mark.parametrize("entry", registry.entries(), ids=lambda item: item.id)
def test_the_field_table_covers_every_setting_of_the_module(
    entry: StrategyEntry,
) -> None:
    """Каждая настройка модуля названа в таблице полей, и лишних имён нет.

    Обобщение `app/convert.py::_strategy_gap()` на реестр: поле, заведённое
    завтра и забытое в таблице, молча получит умолчание вместо того, что
    выбрано в окне.
    """
    assert entry.settings_gap() == (), (
        f"у модуля {entry.id} есть настройки, которых нет в таблице полей: "
        f"{entry.settings_gap()}"
    )
    assert entry.stray_fields() == (), (
        f"таблица полей модуля {entry.id} называет несуществующие настройки: "
        f"{entry.stray_fields()}"
    )
    names = [one.name for one in entry.fields]
    assert len(set(names)) == len(names), f"поле названо дважды: {names}"


@pytest.mark.parametrize("entry", registry.entries(), ids=lambda item: item.id)
def test_the_field_table_names_real_fields_of_the_window(entry: StrategyEntry) -> None:
    """Правая половина таблицы — настоящие поля общих настроек программы.

    Слои друг друга не импортируют (ARCHITECTURE.md §2), и связь здесь по
    имени: опечатка разъехалась бы молча — сборщик настроек `app/convert.py`
    получил бы имя, которого нет, и упал бы не там, где причина.
    """
    import ui.models

    missing = [
        one.outer for one in entry.fields
        if not hasattr(ui.models.Settings(), one.outer)
    ]
    assert not missing, (
        f"таблица полей модуля {entry.id} называет поля общих настроек, "
        f"которых нет: {missing}"
    )


@pytest.mark.parametrize("entry", registry.entries(), ids=lambda item: item.id)
def test_the_gap_check_is_not_blind(entry: StrategyEntry) -> None:
    """Канарейка: проверка полноты действительно ловит забытое поле."""
    shortened = dataclasses.replace(entry, fields=entry.fields[1:])
    assert shortened.settings_gap(), (
        "из таблицы убрано поле, а разрыв не найден — проверка полноты мертва"
    )
    invented = dataclasses.replace(
        entry,
        fields=(
            *entry.fields,
            registry.SettingsField(
                "нет_такой_настройки", "average_period", "Выдуманная настройка"
            ),
        ),
    )
    assert invented.stray_fields() == ("нет_такой_настройки",)


def _tables_that_disagree(entries: tuple[StrategyEntry, ...]) -> list[str]:
    """Записи, делящие класс настроек и не делящие таблицу полей.

    Пусто — согласовано. Сравниваются сами записи полей: имя у модуля, имя
    в окне и подпись; разойтись любой из трёх колонок достаточно, чтобы
    один и тот же выбор человека доехал до двух алгоритмов по-разному.
    """
    seen: dict[type, StrategyEntry] = {}
    trouble: list[str] = []
    for entry in entries:
        first = seen.setdefault(entry.settings_type, entry)
        if first is not entry and first.fields != entry.fields:
            trouble.append(
                f"{first.id} и {entry.id} делят настройки "
                f"{entry.settings_type.__name__}, а таблицы полей у них разные: "
                f"{first.fields} против {entry.fields}"
            )
    return trouble


def test_modules_sharing_a_settings_class_share_its_field_table() -> None:
    """Один класс настроек — одна таблица полей на всех, кто им пользуется.

    ⚠️ Это **замена** задуманной проверки `D-094`, а не она сама. Задумано
    было «поля двух модулей не пересекаются»; второй алгоритм показал, что
    такая проверка запрещала бы верное — он переиспользует класс настроек
    первого по прямой просьбе владельца счёта («все условия те же»), и поля
    у них совпадают целиком.

    Опасность оказалась другой стороной того же: **общий класс настроек
    и две разные таблицы полей**. Тогда «Период средней» из окна у одного
    алгоритма едет в `period`, а у другого — в `confirm_bars`, и заметить
    это можно только по чужим сделкам. Отдельная проверка нужна потому, что
    полнота таблицы (`settings_gap`, `stray_fields`) обе такие таблицы
    считает исправными: каждая по отдельности полна.

    ⚠️ Разные классы настроек, читающие одно и то же поле окна, — **норма**,
    а не нарушение: хранение настроек плоское, «Период средней» в окне один
    (`D-096`). Проверять там нечего.
    """
    assert not _tables_that_disagree(registry.entries()), (
        "\n  ".join(["алгоритмы разошлись в таблице полей:",
                     *_tables_that_disagree(registry.entries())])
    )


def test_that_agreement_check_is_not_blind() -> None:
    """Канарейка: подмена одной колонки в таблице второго модуля обязана ловиться.

    Мутация ставится на **локальных** записях: настоящий реестр не трогается
    ни на одну проверку (`D-078`).
    """
    first, second = registry.entries()[0], registry.entries()[-1]
    assert first.settings_type is second.settings_type, (
        "канарейка потеряла смысл: записи больше не делят класс настроек"
    )
    swapped = dataclasses.replace(
        second,
        fields=(
            registry.SettingsField("period", "confirm_bars", "Период средней"),
            *second.fields[1:],
        ),
    )
    assert _tables_that_disagree((first, swapped)), (
        "у второго алгоритма поле окна подменено, а проверка молчит"
    )
    assert not _tables_that_disagree((first, second)), (
        "проверка падает на согласованных таблицах — она ловит не то"
    )


@pytest.mark.parametrize("entry", registry.entries(), ids=lambda item: item.id)
def test_the_summary_says_something(entry: StrategyEntry) -> None:
    """Описание правила словами есть у каждой записи.

    ⚠️ Здесь проверяется только наличие. Что описание **не врёт** — отдельные
    проверки в `tests/test_strategies_description.py`: исполнение утверждений
    и фактов, полнота по настройкам и граница честности.

    ⚠️ Настройки называются явно, потому что от них зависит **формулировка**:
    при включённом пороге «закрытие выше средней» становится «закрытие выше
    полосы вокруг средней» без единой цифры. Свойства без аргумента здесь
    больше нет — оно молча подставляло умолчания и врало в окне (`B-039`).
    """
    said = entry.summary(entry.defaults())
    assert said.strip(), f"у модуля {entry.id} нет описания словами"


@pytest.mark.parametrize("entry", registry.entries(), ids=lambda item: item.id)
def test_settings_of_another_module_are_refused(entry: StrategyEntry) -> None:
    """Чужие настройки — отказ вслух, а не торговля с чужими параметрами."""
    class SettingsOfSomeoneElse:
        period = 15

    with pytest.raises(TypeError) as refusal:
        entry.build(SettingsOfSomeoneElse())
    said = str(refusal.value)
    assert entry.settings_type.__name__ in said, (
        "отказ не называет, каких настроек модуль ждал"
    )
    assert "SettingsOfSomeoneElse" in said, "отказ не называет, что ему подали"


def test_the_table_cannot_be_edited_through_the_accessor() -> None:
    """`entries()` отдаёт кортеж: дописать модуль на ходу нечем.

    Общая таблица, которую можно поправить из любого места, однажды будет
    поправлена из теста — и достанется соседям (`D-078`).
    """
    assert isinstance(registry.entries(), tuple)
    assert registry.entries() is registry.entries()
    entry = registry.default_entry()
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.id = "другой"  # type: ignore[misc]
    assert isinstance(entry.fields, tuple)
