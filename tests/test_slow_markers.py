"""Быстрый прогон обязан что-то отсекать. Сторож против возврата фикции.

В `CLAUDE.md` стояла ежедневная команда `pytest -m "not slow"`, и фикцией она
была с самого начала. Собирала **3432** теста, полный прогон — те же **3432**: маркеров
в дереве не было ни одного, секции `markers` в `pyproject.toml` не было вовсе,
и обещание `.docs/TESTING.md` «уложиться в 5-10 секунд» было невыполнимо
по построению. Команда была, выглядела фильтром и не фильтровала ничего
(`BACKLOG.md`, `D-081`).

Это не опечатка и не забывчивость — это класс дефекта, который в проекте уже
называли по имени: **проверка, которая зеленеет, ничего не проверяя**. Правится
он не разметкой, а сторожем на разметку: разметка без сторожа исчезнет при
первом же массовом рефакторинге тестов, и никто этого не заметит — прогон
останется зелёным, просто станет медленным, а потом кто-нибудь снова напишет
в документе «быстрый прогон».

Что здесь проверяется и чего каждая проверка НЕ доказывает
-----------------------------------------------------------

* **Маркер объявлен.** Слабая проверка, и сказано прямо: `--strict-markers`
  уронил бы прогон и без неё. Держится ради читаемости отказа — «маркер
  не объявлен» вместо «unknown marker» на случайном тесте.
* **Разметка не усохла** — разбор `tests/**` через `ast`. Смотрит всё дерево,
  идёт за миллисекунды. Не доказывает, что pytest этой разметкой пользуется:
  это утверждение об исходниках, а не о прогоне.
* **Фильтр действительно отбрасывает** — один подпроцесс `--collect-only`
  на одном файле. Доказывает ровно обратное первому: что `-m` работает
  и что отобранное совпадает с размеченным. Не смотрит всё дерево — за это
  отвечает проверка выше.
* **Сторожа дерева остались в быстром наборе.** Разметить их было бы дешевле
  всего и вреднее всего: они ловят нарушение ровно в тот момент, когда его
  вносят.

Замер, по которому выбран порог 0,5 с, — `.docs/quality/test-durations-2026-09-06.md`.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys
import tomllib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
PYPROJECT = ROOT / "pyproject.toml"

#: Нижняя граница числа размеченных функций. На 06.09.2026 их 29 — порог 0,5 с
#: по замеру `.docs/quality/test-durations-2026-09-06.md`. Взято 20, а не 29:
#: удалить пару долгих тестов — законная работа, и она не должна красить прогон.
#: Обвал ниже двадцати законной работой не бывает — это снос разметки.
MARKED_AT_LEAST = 20

#: Файлы, без которых разметка теряет смысл: на них приходится 58 из 74 секунд,
#: снимаемых с повседневного прогона (сверка с прототипом и запуск команды
#: подпроцессом). Число здесь не записано намеренно — оно уехало бы при первой
#: правке; проверяется факт «размечено хоть что-то», а не количество.
ANCHORS = (
    "test_engine_reference_parity.py",
    "test_backtest_main.py",
)

#: Долгие, но остаются в быстром наборе — с причиной, а не по недосмотру.
#: Все четыре читают дерево целиком и ловят нарушение правила проекта в тот
#: момент, когда его вносят: латиница в именах, границы слоя `app`, дисциплина
#: фикстур Qt, окружение дочернего процесса. Вместе они стоят 3,0 с из 163 —
#: 1,9 % прогона за четыре правила, которые иначе не стережёт ничто.
STAY_FAST = {
    "test_ascii_identifiers.py": "test_no_identifier_in_the_tree_is_written_in_another_script",
    "test_app_boundaries.py": "test_the_assembly_does_not_know_what_draws_the_chart",
    "test_qt_needs_the_application.py": (
        "test_no_test_builds_a_qt_widget_without_asking_for_the_application"
    ),
    "test_conftest_guards.py": "test_no_helper_hands_a_child_an_environment_without_a_platform",
    # Пятая строка про этот же файл. Сквозная проверка стоит 0,8 с — то есть
    # сама перешагивает порог, — и всё равно обязана идти в быстром прогоне:
    # сторож против фикции, выключенный из прогона, где фикция и живёт, —
    # это ровно та шутка, ради которой заведён `D-081`.
    "test_slow_markers.py": "test_the_filter_deselects_exactly_the_tests_that_carry_the_mark",
}

#: Файл для сквозной проверки фильтра. Выбран не наугад: в нём шесть размеченных
#: тестов из двадцати двух, он не тянет Qt (сбор идёт за 0,15 с) и ни один из его
#: размеченных тестов не параметризован — значит «функций» и «узлов» здесь одно
#: и то же, и сравнение имён однозначно.
PROBE = "test_backtest_main.py"


def _slow_functions(path: pathlib.Path) -> set[str]:
    """Имена функций файла, помеченных `@pytest.mark.slow`.

    Модульный `pytestmark` тоже учитывается: он помечает весь файл, и тогда
    возвращаются все функции `test_*`. В дереве такого сегодня нет, но приём
    законный, и разбор, его не знающий, однажды соврал бы в пользу «размечено».

    ⚠️ Отбор по подстроке до разбора — не небрежность, а цена. Первая редакция
    разбирала все 90 файлов подряд и стоила 1,5 с на вызов: сторож быстрого
    прогона сам оказался бы в десятке самых долгих. Пропуск файла, где строки
    `slow` нет вовсе, безопасен: написать декоратор, не написав слова, можно
    только через вычисляемое имя (`getattr(pytest.mark, ...)`), а такой записи
    в дереве нет и быть не должно — она непроверяема ни `ruff`, ни этим тестом.
    """
    text = path.read_text(encoding="utf-8")
    if "slow" not in text:
        return set()
    tree = ast.parse(text)

    def is_slow(node: ast.expr) -> bool:
        return isinstance(node, ast.Attribute) and node.attr == "slow"

    whole_file = False
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in statement.targets
        ):
            continue
        value = statement.value
        marks = value.elts if isinstance(value, ast.Tuple | ast.List) else [value]
        whole_file = whole_file or any(is_slow(mark) for mark in marks)

    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.name.startswith("test_"):
            continue
        if whole_file or any(is_slow(dec) for dec in node.decorator_list):
            found.add(node.name)
    return found


def _all_slow() -> dict[str, set[str]]:
    """Разметка по всему дереву тестов: файл → имена размеченных функций."""
    return {
        path.name: names
        for path in sorted(TESTS.rglob("test_*.py"))
        if (names := _slow_functions(path))
    }


def test_the_marker_is_declared_where_pytest_looks_for_it() -> None:
    """`slow` объявлен в `pyproject.toml` и объяснён словами, а не одним именем.

    Объявление без описания — половина работы: `--strict-markers` перестанет
    ругаться, а человек, увидевший `@pytest.mark.slow` в чужом файле,
    по-прежнему не узнает, чем тест заслужил метку.
    """
    settings = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    declared = settings["tool"]["pytest"]["ini_options"].get("markers", [])
    named = [line for line in declared if line.split(":", 1)[0].strip() == "slow"]
    assert named, (
        "маркер `slow` не объявлен в [tool.pytest.ini_options].markers — "
        f"объявлено: {declared or 'ничего'}"
    )
    (description,) = (line.split(":", 1)[1].strip() for line in named)
    assert len(description) > 20, (
        f"маркер объявлен без внятного описания: {description!r}. "
        "Строка обязана говорить, за что тест получает метку"
    )


def test_the_fast_run_still_drops_something() -> None:
    """Разметка на месте: без неё отбор «кроме slow» снова станет фикцией.

    Проверки на снос не было вовсе — потому фикцию и не заметили. Падает,
    когда разметку выносят массовой правкой тестов, а документ про быстрый
    прогон остаётся.
    """
    marked = _all_slow()
    count = sum(len(names) for names in marked.values())
    assert count >= MARKED_AT_LEAST, (
        f"размечено {count} функций в {len(marked)} файлах, нижняя граница "
        f"{MARKED_AT_LEAST}. Быстрый прогон снова перестал что-либо отсекать — "
        "либо разметку снесли, либо порог 0,5 с надо пересчитать заново "
        "(.docs/quality/test-durations-2026-09-06.md)"
    )


@pytest.mark.parametrize("anchor", ANCHORS)
def test_the_longest_files_keep_their_marks(anchor: str) -> None:
    """Два самых дорогих файла размечены: на них 58 из 74 снимаемых секунд.

    Общего счёта мало: двадцать размеченных мелочей и снятая метка со сверки
    оставили бы прогон формально «фильтрующим» и фактически прежним по времени.
    """
    assert (TESTS / anchor).exists(), (
        f"{anchor} в дереве нет — строка в ANCHORS протухла, её надо убрать "
        "или заменить, а не оставлять сторожа сторожить несуществующее"
    )
    assert _slow_functions(TESTS / anchor), (
        f"в {anchor} не осталось ни одного `@pytest.mark.slow`. Это самый "
        "дорогой файл набора; без его разметки быстрый прогон почти не быстрее"
    )


def test_the_filter_deselects_exactly_the_tests_that_carry_the_mark() -> None:
    """Сквозная проверка: `-m slow` отбирает ровно размеченное, и не пусто.

    Единственная здесь проверка, которая трогает сам pytest, а не исходники.
    Разбор `ast` доказывает, что декоратор написан; эта — что от него что-то
    зависит. Одного подпроцесса хватает: строка итога сбора называет и
    отобранное, и отброшенное.
    """
    marked = _slow_functions(TESTS / PROBE)
    assert marked, f"в {PROBE} нет размеченных тестов — проверка выродилась"

    done = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            "--collect-only", "-q", "-p", "no:cacheprovider",
            "-m", "slow", f"tests/{PROBE}",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, f"сбор не удался: {done.stdout}\n{done.stderr}"

    picked = {
        line.split("::", 1)[1].split("[", 1)[0].strip()
        for line in done.stdout.splitlines()
        if line.startswith(f"tests/{PROBE}::")
    }
    assert picked == marked, (
        "фильтр отобрал не то, что размечено в исходнике.\n"
        f"  отобрал pytest: {sorted(picked)}\n"
        f"  размечено в файле: {sorted(marked)}"
    )
    assert "deselected" in done.stdout, (
        "в итоге сбора нет ни одного отброшенного теста — значит размечен весь "
        f"файл целиком, и `-m \"not slow\"` на нём не оставит ничего:\n{done.stdout}"
    )


@pytest.mark.parametrize("file_name, test_name", sorted(STAY_FAST.items()))
def test_the_guards_that_read_the_whole_tree_stay_in_the_fast_run(
    file_name: str, test_name: str
) -> None:
    """Долгий сторож дерева метку не получает — это решение, а не недосмотр.

    Все четыре перешагивают порог 0,5 с, и все четыре обязаны идти в каждом
    повседневном прогоне: они ловят нарушение правила проекта ровно тогда,
    когда его вносят. Убрать их из быстрого набора — сэкономить 1,9 % времени
    и потерять единственный запускаемый механизм за четырьмя правилами.
    """
    path = TESTS / file_name
    assert path.exists(), (
        f"{file_name} в дереве нет — строка в STAY_FAST протухла. Список "
        "исключений обязан описывать существующее, иначе он не проверяет ничего"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert test_name in names, (
        f"{file_name}::{test_name} не найден — тест переименовали или удалили, "
        "а исключение осталось стеречь пустоту"
    )
    assert test_name not in _slow_functions(path), (
        f"{file_name}::{test_name} размечен как `slow`. Это сторож, читающий "
        "дерево целиком; вне повседневного прогона он бесполезен. Если метка "
        "поставлена намеренно — вычеркнуть строку из STAY_FAST и объяснить чем"
    )
