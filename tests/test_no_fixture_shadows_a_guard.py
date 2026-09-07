"""Сторож на класс отказа: файл тестов молча отменяет общего сторожа.

`pytest` разрешает файлу объявить фикстуру с тем же именем, что у фикстуры
из `conftest.py`, и местная побеждает. Для обычной фикстуры это приём:
файл подставляет своё значение вместо общего. Для **автоматической**
(`autouse=True`) — это отмена целого рубежа защиты, и происходит она молча:
ни предупреждения, ни строки в выводе. Прогон остаётся зелёным.

Так и было найдено `B-034`. Четыре файла — `test_app_fetch.py`,
`test_app_backfill.py`, `test_ui_history_load.py`, `test_market_history.py` —
объявляли свою фикстуру `no_internet`, обезвреживающую транспорт биржи,
и тем самым снимали с себя **сокетного** сторожа `tests/conftest.py::
no_internet`. Мимо него шли 135 тестов из 3449, а `conftest` при этом
объявлял обход невозможным: «нужен настоящий обмен — впишите тест поимённо
в `INTERNET_ALLOWED`». Документ врал про собственную защиту.

Замер 06.09.2026, чтобы не преувеличивать: **до сокета не доходил ни один
из 135**. Транспорт биржи был подставлен всюду, и местная фикстура работала.
Цена перекрытия — не утёкший трафик, а снятый второй рубеж: с местного
сторожа сняли одну строку (мутация в отдельном рабочем дереве) — и
`test_the_real_transport_cannot_leave_this_file` ушёл разрешать `iss.moex.com`
по-настоящему, потому что ловить его стало нечем.

Почему автоматические, а не все подряд
--------------------------------------
Перекрытие обычной фикстуры — законный приём `pytest`, и в дереве он
используется по делу: `tests/test_app_observe.py::loop` подменяет общий
`loop` (тот собирает цикл поверх приложения Qt) простым циклом `asyncio`,
и это написано в его докстринге. Запрещать такое значило бы объявить
нарушением исправный код.

Чего сторож не закрывает, сказано вслух
---------------------------------------
* **Вложенных `conftest.py` в дереве нет** (проверено `find`, 06.09.2026),
  и цепочка каталогов здесь не прослеживается. Появится второй `conftest` —
  проверку надо расширять, сама она об этом не скажет.
* **Отмена через `pytest_plugins` и подмену объекта на ходу** разбором
  не видна: в файле нет ни `def`, ни декоратора. Такого в дереве нет.
* Сторож смотрит на **имя**, а не на смысл. Фикстура, снявшая защиту
  изнутри — например, вернувшая `socket.getaddrinfo` на место, — пройдёт
  мимо: это уже не перекрытие.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Final

#: Каталог с тестами. Разбираются **все** `*.py`, кроме самого `conftest.py`.
TESTS: Final[pathlib.Path] = pathlib.Path(__file__).resolve().parent

#: Известные перекрытия, оставленные намеренно: имя и причина на каждое.
#: Список работает храповиком в обе стороны — строка, переставшая быть
#: правдой, роняет прогон так же, как новое перекрытие. Иначе он превратится
#: в кладбище разрешений, выданных однажды и никем не снятых.
SHADOWS_ALLOWED: Final[dict[str, str]] = {
    "tests/test_app_logs.py::pristine_logging": (
        "дословная копия общего сторожа: тела совпали посимвольно "
        "(разбор `ast`, 06.09.2026), поэтому уборка логгеров сегодня "
        "делается ровно та же. Вред отложенный — общий сторож обрастёт "
        "новой обязанностью, а копия молча останется прежней. "
        "Файл не входил в границы задачи B-034; снимать копию тому, "
        "кто правит `test_app_logs.py`"
    ),
}


def _dotted(node: ast.AST) -> str:
    """Точечное имя из узла: `pytest.fixture` или `fixture`. Иначе — пусто."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    parts.append(node.id)
    return ".".join(reversed(parts))


def _keyword(call: ast.Call, name: str) -> object:
    """Значение именованного аргумента вызова, если оно записано константой."""
    for item in call.keywords:
        if item.arg == name and isinstance(item.value, ast.Constant):
            return item.value.value
    return None


def _fixture_of(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, bool] | None:
    """Функция объявлена фикстурой — вернуть её имя и признак автоматической.

    ⚠️ Имя берётся из `name=`, если он задан, а не из имени функции:
    `@pytest.fixture(name="no_internet")` над `def whatever()` перекрывает
    сторожа ровно так же, а по имени функции не виден вовсе. Проверено
    мутацией — редакция без разбора `name=` оставалась зелёной.

    ⚠️ Проверяется **последнее звено** точечного имени, а не вхождение
    подстроки: `pytest.mark.usefixtures("qapp")` содержит «fixture»
    и по подстроке был бы объявлен фикстурой.
    """
    for decorator in node.decorator_list:
        call = decorator if isinstance(decorator, ast.Call) else None
        dotted = _dotted(call.func if call is not None else decorator)
        if not dotted or dotted.rsplit(".", maxsplit=1)[-1] != "fixture":
            continue
        named = _keyword(call, "name") if call is not None else None
        automatic = bool(_keyword(call, "autouse")) if call is not None else False
        return (named if isinstance(named, str) else node.name, automatic)
    return None


def fixtures_in(source: str) -> dict[str, tuple[int, bool]]:
    """Фикстуры файла: имя → (строка объявления, автоматическая ли).

    Берёт текст, а не путь, намеренно: так проверку самой проверки можно
    поставить на образце из трёх строк, не заводя файла в дереве.
    """
    found: dict[str, tuple[int, bool]] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        fixture = _fixture_of(node)
        if fixture is not None:
            name, automatic = fixture
            found[name] = (node.lineno, automatic)
    return found


def guards() -> dict[str, int]:
    """Автоматические фикстуры `conftest.py`: имя → строка объявления.

    Ровно они и есть рубежи, снимаемые перекрытием молча: сеть, экран,
    уборка логгеров, временная папка под технический лог.
    """
    source = (TESTS / "conftest.py").read_text(encoding="utf-8")
    return {
        name: line
        for name, (line, automatic) in fixtures_in(source).items()
        if automatic
    }


def shadows() -> dict[str, str]:
    """Все перекрытия сторожей в дереве: `файл::имя` → место с номером строки."""
    watched = guards()
    found: dict[str, str] = {}
    for path in sorted(TESTS.glob("*.py")):
        if path.name == "conftest.py":
            continue
        source = path.read_text(encoding="utf-8")
        for name, (line, _) in fixtures_in(source).items():
            if name in watched:
                where = f"tests/{path.name}"
                found[f"{where}::{name}"] = (
                    f"{where}:{line} объявляет `{name}`, "
                    f"а это сторож tests/conftest.py:{watched[name]}"
                )
    return found


def test_no_test_file_shadows_an_autouse_guard_of_the_conftest() -> None:
    """Файл тестов не объявляет фикстуру с именем автоматического сторожа.

    Перекрыл — значит снял защиту со всего файла, и `pytest` об этом
    не скажет ни слова. Так `B-034` и просидел незамеченным: 135 тестов
    шли без сокетного сторожа при зелёном прогоне.

    Мутация 06.09.2026 (отдельное рабочее дерево): в `test_market_history.py`
    дописана `@pytest.fixture(autouse=True) def no_internet()` — проверка
    упала и назвала файл, строку и имя сторожа. Второй заход, через
    `@pytest.fixture(name="no_internet")` над функцией с другим именем, —
    упала так же.
    """
    found = shadows()
    unexpected = sorted(set(found) - set(SHADOWS_ALLOWED))

    assert not unexpected, (
        "фикстура файла отменяет автоматического сторожа из conftest — "
        "весь файл идёт без защиты, и прогон об этом молчит:\n  "
        + "\n  ".join(found[key] for key in unexpected)
        + "\nПереименуйте местную фикстуру по смыслу того, что она делает "
        "(она почти наверняка подменяет один вызов, а не отменяет рубеж), "
        "либо впишите её в SHADOWS_ALLOWED с причиной."
    )


def test_a_permission_that_stopped_being_true_fails_the_run() -> None:
    """Строка `SHADOWS_ALLOWED`, под которой уже нет перекрытия, — красная.

    Храповик работает в обе стороны. Разрешение, выданное однажды, иначе
    остаётся навсегда и через месяц читается как «здесь так и надо»,
    хотя перекрытия давно нет.
    """
    found = shadows()
    stale = sorted(set(SHADOWS_ALLOWED) - set(found))

    assert not stale, (
        "разрешение выдано, а перекрытия под ним нет — снимите строку "
        f"из SHADOWS_ALLOWED: {stale}"
    )


def test_the_socket_guard_is_among_the_watched_names() -> None:
    """`no_internet` из `conftest` считается сторожем — ради него всё и заведено.

    Сломается разбор `conftest` (переименуют декоратор, обернут фикстуру
    в свой помощник) — список сторожей опустеет, и обе проверки выше
    станут зелёными навсегда, ничего не проверяя.
    """
    watched = guards()
    assert "no_internet" in watched, (
        f"сокетный сторож не опознан как автоматическая фикстура: {sorted(watched)}"
    )
    assert len(watched) >= 3, (
        f"автоматических сторожей найдено {len(watched)} — разбор conftest "
        f"сломался и проверка смотрит в пустоту: {sorted(watched)}"
    )


def test_an_ordinary_fixture_of_the_conftest_may_be_overridden() -> None:
    """`loop` и `qapp` сторожами не считаются: их перекрытие — законный приём.

    `tests/test_app_observe.py` подменяет `loop` простым циклом `asyncio`
    осознанно и с объяснением в докстринге. Проверка, объявляющая это
    нарушением, заставила бы чинить исправное — а такую проверку отключают
    целиком, вместе с той частью, ради которой она стоит.
    """
    watched = guards()
    assert "loop" not in watched, "обычная фикстура попала в сторожа"
    assert "qapp" not in watched, "обычная фикстура попала в сторожа"


def test_a_fixture_renamed_through_the_name_argument_is_still_seen() -> None:
    """`@pytest.fixture(name="no_internet")` — то же перекрытие, другая запись.

    Самый дешёвый способ обойти проверку по имени функции, и он же —
    обычная опечатка в чужом стиле. Разбирается на образце, а не на файле
    дерева: в дереве такой записи сегодня нет, и проверка без образца
    была бы пустой.
    """
    sample = (
        "import pytest\n"
        "\n"
        "@pytest.fixture(name='no_internet', autouse=True)\n"
        "def whatever():\n"
        "    yield\n"
    )
    found = fixtures_in(sample)
    assert "no_internet" in found, f"имя из `name=` потеряно: {found}"
    assert found["no_internet"][1] is True, "признак autouse потерян"
    assert "whatever" not in found, "фикстура учтена под именем функции"


def test_a_usefixtures_marker_is_not_mistaken_for_a_fixture() -> None:
    """`pytest.mark.usefixtures` содержит слово «fixture» и фикстурой не является.

    Разбор по вхождению подстроки объявил бы фикстурой каждый тест
    с этим маркером — а такие в дереве есть, и звали бы их по именам
    тестов. Проверка бы не покраснела, но стала бы врать про содержимое.
    """
    sample = (
        "import pytest\n"
        "\n"
        "@pytest.mark.usefixtures('qapp')\n"
        "def test_something():\n"
        "    pass\n"
    )
    assert fixtures_in(sample) == {}, "маркер принят за объявление фикстуры"


def test_the_guard_reads_every_test_file_not_only_its_own() -> None:
    """Разбору достаётся весь каталог, а не файл, в котором живёт проверка.

    Тот же промах, на котором обожглись 06.09.2026 в
    `test_qt_needs_the_application.py`: первая редакция брала
    `Path(__file__)` и стерегла один файл из ста с лишним.
    """
    files = [path for path in sorted(TESTS.glob("*.py")) if path.name != "conftest.py"]
    # Замер 06.09.2026: в каталоге 86 файлов `test_*.py` и четыре модуля
    # оснастки. Порог взят с запасом вниз — стережётся сломанная маска,
    # а не точное число, которое меняется с каждым новым файлом.
    assert len(files) > 50, (
        f"разбору достался {len(files)} файл(ов) — маска сломалась, "
        "и проверка молча смотрит в пустоту"
    )
    for named in (
        "test_app_fetch.py",
        "test_app_backfill.py",
        "test_ui_history_load.py",
        "test_market_history.py",
        "helpers.py",
    ):
        assert TESTS / named in files, f"{named} выпал из разбора"


def test_the_four_files_of_the_bug_no_longer_shadow_the_socket_guard() -> None:
    """Именно те четыре файла, с которых начался `B-034`, — чистые.

    Проверка по имени, а не по общему счётчику: общий список мог бы
    зазеленеть оттого, что кто-то вписал их в `SHADOWS_ALLOWED`, и
    исправление молча откатилось бы обратно.
    """
    for named in (
        "test_app_fetch.py",
        "test_app_backfill.py",
        "test_ui_history_load.py",
        "test_market_history.py",
    ):
        source = (TESTS / named).read_text(encoding="utf-8")
        assert "no_internet" not in fixtures_in(source), (
            f"tests/{named} снова объявляет `no_internet` и снимает с себя "
            "сокетного сторожа — это возврат B-034"
        )
