"""Сторож на класс отказа: виджет Qt, построенный без `QApplication`.

`QWidget`, созданный до `QApplication`, для Qt не ошибка, а `qFatal`:
процесс уходит в `abort()` с кодом 134, и `pytest` не успевает сообщить
ни об одном тесте. Пока такой тест бежит в компании, приложение успевает
создать сосед, и всё зелено. В одиночку — дамп ядра.

⚠️ Отказ невидим ровно до тех пор, пока никто не запускает тест отдельно.
Так он и просидел два дня: тринадцать узлов `tests/test_ui_settings.py`
роняли процесс поодиночке и проходили вместе (правило 14 `CLAUDE.md`,
часть «состояние»).

Проверка **разбирает** файлы, а не выполняет их, и виджетов не строит:
строила бы — падала бы ровно тем способом, который стережёт.

Что было исправлено после ревью 06.09.2026
------------------------------------------
Первая редакция жила внутри `tests/test_ui_settings.py` и была слепа втройне:

* смотрела **только свой файл** (`Path(__file__)`) — нарушителя в соседнем
  не остановило бы ничто;
* считала постройкой только `ast.Name` — вызов `ui.main_window.MainWindow(...)`
  в её же файле (строка 627) проходил мимо, это `ast.Attribute`;
* собирала имена только из `ast.ImportFrom` — `import ui.main_window`
  не давал ей вовсе ничего.

Все три промаха проверены мутацией: с прежней редакцией храповик оставался
зелёным, а одиночный прогон подсунутого теста ронял процесс с дампом ядра.

Чего сторож не закрывает, сказано вслух
---------------------------------------
Виджет, построенный **чужим** кодом по строке или по имени из настроек
(`getattr`, фабрика по названию класса), разбором не виден: в файле нет
ни имени класса, ни вызова. Такого в дереве нет, и заводить проверку
впрок значит стеречь то, чего нет.

Соседний тестовый модуль разбирается **на один уровень**: если он сам берёт
виджет из третьего тестового модуля, цепочка дальше не прослеживается.
Так сделано намеренно — полная рекурсия по кругу импортов повисла бы,
а второго уровня в дереве сегодня нет.
"""

from __future__ import annotations

import ast
import importlib
import pathlib
import types
from typing import Final

#: Каталог с тестами. Разбираются **все** файлы `test_*.py`, а не свой.
TESTS: Final[pathlib.Path] = pathlib.Path(__file__).resolve().parent

#: Где вообще может лежать класс-виджет: слой интерфейса и сам Qt.
#: Модули других слоёв не импортируются вовсе — и потому, что виджету
#: там не место, и потому, что разбор не должен тащить за собой пол-проекта.
WIDGET_HOMES: Final[tuple[str, ...]] = ("ui", "PySide6")

#: Узлы-функции обоих видов. `async def` попадает сюда наравне с обычным:
#: правило «виджет требует приложения» от вида функции не зависит.
#: Пара называется без ручной аннотации намеренно: так `isinstance`
#: сужает тип узла, и разбор его полей проверяется, а не проходит как `Any`.
FUNCTIONS: Final = (ast.FunctionDef, ast.AsyncFunctionDef)

#: Имя, с которого начинается доставка `QApplication` в тест.
#: Фикстура живёт в `tests/conftest.py`.
THE_APPLICATION: Final[str] = "qapp"


def _is_widget(value: object) -> bool:
    """За значением стоит класс-виджет Qt.

    Разрешение имени в объект, а не догадка по написанию: `Settings`,
    `CalendarDay` и `RecordingPort` тоже пишутся с большой буквы, и правило
    «CamelCase значит виджет» отметило бы половину дерева.
    """
    from PySide6.QtWidgets import QWidget

    return isinstance(value, type) and issubclass(value, QWidget)


def _module(name: str) -> types.ModuleType | None:
    """Модуль по имени, если он из тех, где живут виджеты. Иначе — ничего."""
    if name.split(".", maxsplit=1)[0] not in WIDGET_HOMES:
        return None
    return importlib.import_module(name)


def _submodule(name: str) -> types.ModuleType | None:
    """То же, но для проверки «а не модуль ли это».

    `from ui.models import Settings` и `from ui import main_window` выглядят
    в разборе одинаково, различает их только попытка импорта. Отсутствие
    модуля здесь — обычный ответ «нет», а не беда, и глушится ровно оно:
    любая другая ошибка импорта пройдёт наверх.
    """
    try:
        return _module(name)
    except ModuleNotFoundError:
        return None


def _dotted(node: ast.AST) -> str | None:
    """`ui.main_window.MainWindow` из узла обращения. Не обращение — `None`.

    Точечное имя разбирается целиком, а не только последнее звено: первая
    редакция смотрела на `ast.Name` и пропускала ровно те вызовы, которыми
    в дереве и строят главное окно.
    """
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _from_a_module(bound: str, module: types.ModuleType) -> set[str]:
    """Точечные имена всех виджетов модуля под тем именем, каким его связали.

    `dir()`, а не `vars()`, и это не вкусовщина: PySide6 наполняет
    пространство имён модуля лениво, и `vars(PySide6.QtWidgets)` на свежем
    импорте отдаёт **один** класс из семидесяти трёх. Замер 06.09.2026:
    редакция на `vars()` не нашла даже `QLabel`.
    """
    return {
        f"{bound}.{attr}"
        for attr in dir(module)
        if _is_widget(getattr(module, attr, None))
    }


def widget_names(path: pathlib.Path, *, follow: bool = True) -> set[str]:
    """Имена, которыми в этом файле зовут классы-виджеты Qt.

    Учитываются все четыре способа завести такое имя:

    * `from ui.settings_dialog import SettingsDialog` — прямое имя;
    * `import ui.main_window` — точечное `ui.main_window.MainWindow`;
    * `from ui import main_window` — точечное `main_window.MainWindow`;
    * `class Instant(SettingsDialog)` — наследник, объявленный на месте.

    Плюс пятый, межфайловый: `from test_ui_chart import Probe`, где `Probe`
    объявлен виджетом в соседнем тестовом модуле. Соседа разбираем без
    дальнейшего перехода (`follow=False`) — круг импортов повесил бы разбор.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = _module(alias.name)
                if module is not None:
                    names |= _from_a_module(alias.asname or alias.name, module)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names |= _named_imports(node, path, follow=follow)

    _add_local_heirs(tree, names)
    return names


def _named_imports(
    node: ast.ImportFrom, path: pathlib.Path, *, follow: bool
) -> set[str]:
    """Имена-виджеты, которые приносит одно `from ... import ...`."""
    names: set[str] = set()
    module = _module(node.module or "")
    if module is not None:
        for alias in node.names:
            value = getattr(module, alias.name, None)
            if _is_widget(value):
                names.add(alias.asname or alias.name)
                continue
            inner = _submodule(f"{node.module}.{alias.name}")
            if inner is not None:
                names |= _from_a_module(alias.asname or alias.name, inner)
        return names

    neighbour = TESTS / f"{node.module}.py"
    if follow and neighbour.is_file() and neighbour != path:
        borrowed = widget_names(neighbour, follow=False)
        for alias in node.names:
            if alias.name in borrowed:
                names.add(alias.asname or alias.name)
    return names


def _add_local_heirs(tree: ast.AST, names: set[str]) -> None:
    """Наследники виджетов, объявленные в самом файле, — тоже виджеты.

    Считается до неподвижной точки: наследник наследника тоже строит окно.
    """
    growing = True
    while growing:
        growing = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name in names:
                continue
            if any(_dotted(base) in names for base in node.bases):
                names.add(node.name)
                growing = True


def _is_fixture(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any("fixture" in ast.unparse(item) for item in node.decorator_list)


def carriers(tree: ast.AST, seed: set[str]) -> set[str]:
    """Имена в подписи, через которые до тела доезжает `QApplication`.

    Считается замыканием, а не списком руками: `dialog` берёт `make_dialog`,
    `make_dialog` берёт `qapp`, и цепочка завтра станет длиннее. Список
    руками на третьем звене устареет молча — ровно то, за чем этот сторож
    и поставлен.
    """
    got = set(seed)
    growing = True
    while growing:
        growing = False
        for node in ast.walk(tree):
            if not isinstance(node, FUNCTIONS) or node.name in got:
                continue
            if not _is_fixture(node):
                continue
            if {argument.arg for argument in node.args.args} & got:
                got.add(node.name)
                growing = True
    return got


def _builds_a_widget(node: ast.AST, targets: set[str]) -> list[str]:
    """Что за виджеты строит эта функция, включая вложенные в неё."""
    return sorted({
        name
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and (name := _dotted(call.func)) in targets
    })


def helpers_that_build(tree: ast.AST, widgets: set[str]) -> set[str]:
    """Функции файла, вызов которых строит виджет, — тоже постройка.

    Обход только по телу теста пропускал бы `def _made(): return QLabel()`
    рядом с ним: в самом тесте виден лишь вызов `_made()`. Считается
    до неподвижной точки — помощник помощника тоже строит.

    Фикстуры сюда не входят: они приходят в тест **значением** через подпись,
    и за их приложение отвечает цепочка `carriers`.
    """
    made: set[str] = set()
    growing = True
    while growing:
        growing = False
        for node in ast.walk(tree):
            if not isinstance(node, FUNCTIONS) or node.name in made:
                continue
            if node.name.startswith("test_") or _is_fixture(node):
                continue
            if _builds_a_widget(node, widgets | made):
                made.add(node.name)
                growing = True
    return made


def offenders_in(path: pathlib.Path, seed: set[str]) -> list[str]:
    """Тесты и фикстуры файла, которые строят виджет, не попросив приложения."""
    widgets = widget_names(path)
    if not widgets:
        return []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    targets = widgets | helpers_that_build(tree, widgets)
    got = carriers(tree, seed)

    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, FUNCTIONS):
            continue
        if not node.name.startswith("test_") and not _is_fixture(node):
            continue
        built = _builds_a_widget(node, targets)
        if built and not {argument.arg for argument in node.args.args} & got:
            found.append(f"{path.name}::{node.name} строит {', '.join(built)}")
    return found


def _seed() -> set[str]:
    """Несущие имена из `conftest.py`: сам `qapp` и всё, что его берёт."""
    tree = ast.parse((TESTS / "conftest.py").read_text(encoding="utf-8"))
    return carriers(tree, {THE_APPLICATION})


def test_no_test_builds_a_qt_widget_without_asking_for_the_application() -> None:
    """Тест или фикстура, строящие виджет, обязаны просить `qapp`.

    Сам или через цепочку фикстур — но обязаны. Иначе одиночный прогон
    этого теста не краснеет, а роняет процесс с кодом 134, и `pytest`
    не сообщает ни о чём.

    Мутация: дописать в любой файл `tests/test_*.py` тест, строящий виджет
    без `qapp`, — проверка обязана назвать его по имени. Проверено
    06.09.2026 на трёх видах постройки: прямое имя, точечное
    (`ui.main_window.MainWindow`) и через вспомогательную функцию модуля.
    """
    seed = _seed()
    found: list[str] = []
    for path in sorted(TESTS.glob("test_*.py")):
        found += offenders_in(path, seed)

    assert not found, (
        "тест строит виджет Qt, не попросив приложения, — в одиночку он "
        "не упадёт, а уронит процесс с кодом 134:\n  " + "\n  ".join(found)
    )


def test_the_guard_looks_at_every_test_file_not_only_its_own() -> None:
    """Разбираются все файлы тестов, а не тот, в котором живёт проверка.

    Первая редакция брала `Path(__file__)` и потому стерегла ровно один
    файл из ста с лишним. Нарушитель в соседнем не остановило бы ничто.
    """
    files = sorted(TESTS.glob("test_*.py"))
    # Замер 06.09.2026: в дереве 85 файлов `test_*.py`. Порог взят с запасом
    # вниз — стережётся сломанная маска («ноль файлов» или «только свой»),
    # а не точное число, которое меняется каждым новым файлом.
    assert len(files) > 50, (
        f"разбору достался {len(files)} файл(ов) — маска сломалась, "
        "и проверка молча смотрит в пустоту"
    )
    for named in ("test_ui_settings.py", "test_ui_backtest.py", "test_ui_hover.py"):
        assert TESTS / named in files, f"{named} выпал из разбора"


def test_a_plain_import_of_a_widget_is_recognised() -> None:
    """`from ui.settings_dialog import SettingsDialog` — имя разрешено в класс.

    Самый простой из четырёх способов. Не работает он — проверка ослепла
    целиком и пропустит что угодно.
    """
    names = widget_names(TESTS / "test_ui_settings.py")
    assert "SettingsDialog" in names, (
        f"прямое имя виджета не разрешилось: {sorted(names)[:20]}"
    )


def test_a_dotted_call_through_an_imported_module_is_recognised() -> None:
    """`import ui.main_window` плюс `ui.main_window.MainWindow(...)` — постройка.

    Ровно тот случай, который первая редакция пропускала дважды: имя
    приходит через `ast.Import`, а вызов выглядит как `ast.Attribute`.
    Строка 627 её собственного файла проходила мимо.
    """
    names = widget_names(TESTS / "test_ui_settings.py")
    assert "ui.main_window.MainWindow" in names, (
        "точечное имя главного окна не разрешилось — а именно им его "
        "и строят в дереве"
    )


def test_a_widget_imported_from_a_neighbouring_test_module_is_recognised() -> None:
    """`from test_ui_chart import Probe`, где `Probe` — наследник виджета.

    Виджет, объявленный в одном тестовом модуле и построенный в другом,
    роняет процесс так же, как любой другой.
    """
    names = widget_names(TESTS / "test_ui_chart_gaps.py")
    assert "Probe" in names, (
        "виджет, взятый из соседнего тестового модуля, не разрешился"
    )


def test_a_widget_imported_inside_a_function_body_is_recognised() -> None:
    """Импорт внутри тела теста виден так же, как импорт в шапке файла.

    Половина файлов дерева заводит виджеты именно так — чтобы не тащить
    Qt в модули, которым он не нужен.
    """
    names = widget_names(TESTS / "test_ui_backtest.py")
    assert "BacktestDialog" in names, (
        f"имя, заведённое импортом внутри функции, потеряно: {sorted(names)[:20]}"
    )


def test_the_chain_of_fixtures_down_to_the_application_is_followed() -> None:
    """Тест, просящий `dialog`, приложение получает — через два звена.

    Список несущих имён руками устарел бы на третьем звене молча. Здесь
    проверяется, что он считается замыканием и цепочка прослеживается.
    """
    tree = ast.parse((TESTS / "test_ui_settings.py").read_text(encoding="utf-8"))
    got = carriers(tree, _seed())
    assert {"qapp", "make_dialog", "dialog"} <= got, (
        f"цепочка фикстур до `qapp` не прослеживается: {sorted(got)}"
    )


def test_the_loop_fixture_of_the_conftest_counts_as_a_carrier() -> None:
    """`loop` из `conftest.py` тоже приносит приложение, и это учтено.

    Фикстура `loop` берёт `qapp`, значит тест, попросивший один только
    `loop`, приложение получил. Считать иначе — назвать нарушителями
    два десятка исправных тестов.
    """
    assert "loop" in _seed(), (
        "несущие имена из conftest не прослеживаются: тесты на `loop` "
        "будут объявлены нарушителями"
    )
