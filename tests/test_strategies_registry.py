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
import enum
import pathlib
from typing import Any

import pytest

from strategies import MaReverseAlways, MaReverseAlwaysSettings, StrategyEntry, registry

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
    """Умолчание — «Реверс с постоянной позицией», и это проверяется сборкой.

    Решение владельца счёта 05.10.2026 (решение 0063): алгоритм единственный.
    Мутация `DEFAULT_ID` роняет этот тест.
    """
    assert registry.DEFAULT_ID in registry.known_ids()
    entry = registry.default_entry()
    assert entry is registry.find(registry.DEFAULT_ID)
    assert registry.DEFAULT_ID == "ma_reverse_always"
    assert entry.settings_type is MaReverseAlwaysSettings
    assert type(entry.build(entry.defaults())) is MaReverseAlways, (
        "умолчание реестра собирает не тот модуль, которым торгуют сегодня"
    )


def test_the_build_is_exactly_the_modules_it_declares() -> None:
    """Состав сборки назван поимённо: новый модуль — осознанная правка списка.

    ⚠️ Порядок значим: в этом же порядке алгоритмы стоят в окне выбора,
    и первый в списке — не умолчание. Умолчание отдельным полем
    (`DEFAULT_ID`), и его стережёт проверка выше.
    """
    assert registry.known_ids() == ("ma_reverse_always",), (
        "состав реестра изменился. Это не запрет: впишите новый модуль сюда "
        "вместе с проверками, которые он обязан пройти"
    )


def test_the_removed_algorithm_is_named_but_not_built() -> None:
    """Убранный «Реверс по скользящей средней» назван, но не собирается.

    Стережёт: `ema_reverse` есть в таблице убранных (по ней старый файл
    и шаблон читаются как алгоритм по умолчанию), а `find()` по нему
    по-прежнему отказывает — молча подставить другое правило реестр не вправе.
    """
    assert registry.retired_title("ema_reverse") == "Реверс по скользящей средней"
    assert registry.retired_title(registry.DEFAULT_ID) is None
    assert "ema_reverse" not in registry.known_ids()
    with pytest.raises(registry.UnknownStrategy):
        registry.find("ema_reverse")


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
def test_the_title_of_the_record_is_what_the_module_says_about_itself(
    entry: StrategyEntry,
) -> None:
    """Название записи и название модуля — одна строка, а не две похожих.

    ⚠️ Проверки на это не было вовсе до 14.09.2026, и дыра не теоретическая:
    в реестре появился алгоритм, собранный из наследника первого. Запись
    с `factory=EmaReverse` при собственном `title` прошла бы все прежние
    проверки — уникальность названий и непустоту описания, — а в журнал
    решений и в снимок прогона писала бы «Реверс по скользящей средней»
    при выбранной в окне другой строке. Разобрать потом, каким правилом
    шла торговля, было бы нечем.

    Мутация, обязанная ронять проверку: `factory=EmaReverse` у записи,
    чьё `title` от `EmaReverse.title` отличается.
    """
    built = entry.build(entry.defaults())
    assert built.title == entry.title, (
        f"запись реестра «{entry.id}» называется «{entry.title}», а собранный "
        f"ею модуль говорит о себе «{built.title}». В окне будет одно, "
        "в журнале другое"
    )
    assert entry.description(entry.defaults()).title == entry.title, (
        f"описание правила модуля «{entry.id}» озаглавлено не так, как запись"
    )


@pytest.mark.parametrize("entry", registry.entries(), ids=lambda item: item.id)
def test_every_setting_of_the_module_gets_a_line_in_the_journal(
    entry: StrategyEntry,
) -> None:
    """Сменили любое поле настроек — журнал сказал об этом строкой.

    ТЗ §4.4 А: изменение настройки пишется с прежним и новым значением.
    Строки составляет сам модуль (`changes_from`), и составляет их
    перечислением полей руками — то есть поле, заведённое завтра и забытое
    в перечислении, молча не попадёт в журнал. Владелец счёта поменял
    параметр, по которому идут деньги, а в журнале этого нет.

    Мутация, обязанная ронять проверку: убрать одну ветку из `changes_from`
    любого из алгоритмов.
    """
    defaults = entry.defaults()
    silent = [
        field.name
        for field in dataclasses.fields(entry.settings_type)
        if not _another_value(defaults, field.name).changes_from(defaults)
    ]
    assert not silent, (
        f"модуль {entry.id} меняет настройки {silent} молча — в журнале "
        "решений этого изменения не будет"
    )


def _another_value(settings: Any, name: str) -> Any:  # noqa: ANN401 — разбор ниже
    """Те же настройки с одним изменённым полем. Значение — заведомо другое.

    Правило замены общее и про модуль ничего не знает: `bool` разбирается
    раньше `int` (в Python `True` — целое), перечисление меняется на соседа,
    число сдвигается. Тип, для которого правила нет, — отказ вслух:
    молчаливый пропуск поля означал бы проверку, которая его не смотрит.

    ⚠️ `Any` здесь по существу, а не от лени: реестр отдаёт настройки как
    `object` — он не знает, какой у модуля класс, — а проверке нужен
    датакласс, по полям которого она ходит. Признание этого одной строкой
    честнее россыпи подавлений по файлу (тот же довод, что у
    `tests/test_strategies_description.py::defaults_of`).
    """
    value = getattr(settings, name)
    if isinstance(value, bool):
        other: object = not value
    elif isinstance(value, enum.Enum):
        others = [item for item in type(value) if item is not value]
        assert others, f"у перечисления {type(value).__name__} одно значение"
        other = others[0]
    elif isinstance(value, int):
        other = value + 5
    elif isinstance(value, float):
        other = value + 0.04
    else:
        raise AssertionError(
            f"проверка не знает, чем заменить значение поля {name} "
            f"типа {type(value).__name__} — допишите правило"
        )
    return dataclasses.replace(settings, **{name: other})


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

    ⚠️ **С 14.09.2026 на нынешнем составе реестра эта проверка пуста, и это
    сказано здесь нарочно.** Записи две, классы настроек у них разные
    (`EmaReverseSettings` и `MaReverseAlwaysSettings`), общей пары нет
    ни одной — значит `_tables_that_disagree` не находит ничего при любом
    состоянии таблиц. Мёртвой проверка от этого не стала: она падает на том,
    ради чего заведена, — на **третьей** записи, делящей класс настроек
    с первой и объявившей свою таблицу полей (проверено мутацией того же дня).
    Подставлять сюда близнеца ради непустоты нельзя: его таблицу пишет этот
    же файл, расходиться ей не с чем, и проверка стала бы согласием с самой
    собой. Что сама сверка жива, стережёт канарейка ниже.
    """
    assert not _tables_that_disagree(registry.entries()), (
        "\n  ".join(["алгоритмы разошлись в таблице полей:",
                     *_tables_that_disagree(registry.entries())])
    )


def test_that_agreement_check_is_not_blind() -> None:
    """Канарейка: подмена одной колонки в таблице второго модуля обязана ловиться.

    Мутация ставится на **локальных** записях: настоящий реестр не трогается
    ни на одну проверку (`D-078`).

    ⚠️ Пара для опыта строится **здесь**, а не берётся из реестра, и это
    правка 14.09.2026. Прежняя редакция брала первую и последнюю настоящие
    записи и требовала, чтобы они делили класс настроек: пока таких записей
    было две с общим классом, канарейка работала, а при первом же алгоритме
    со своим классом настроек она падала — не поймав ни одной подмены,
    то есть сообщая о поломке там, где её нет. Проверяемое свойство
    («две записи, делящие класс настроек, обязаны делить и таблицу полей»)
    от состава реестра не зависит вовсе, и опираться на него незачем.
    """
    first = registry.default_entry()
    second = dataclasses.replace(first, id=f"{first.id}_twin")
    # ⚠️ Утверждения «записи делят класс настроек» здесь больше нет, и это
    # правка 14.09.2026. При паре, построенной `replace` по одному полю `id`,
    # оно тождественно истинно: подменить класс настроек `replace` тут нечем.
    # Утверждение, которое не может не выполниться, читается как сторож
    # и сторожем не является — а именно так извещатель об уснувшей проверке
    # однажды уснул сам.
    assert second.id != first.id, "пара для опыта не сложилась: это одна запись"
    swapped = dataclasses.replace(
        second,
        fields=(
            registry.SettingsField("period", "take_profit_pct", "Период средней"),
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
