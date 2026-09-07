"""Храповик против роста находок `ruff` и `mypy` — не разовая уборка.

Замер 07.09.2026 (задача «CI на GitHub», `.docs/packaging/packaging-expert-ci-…`):
`ruff` — 575 находок, `mypy` — 190. Это разобранный остаток с известными
причинами (см. `pyproject.toml`, комментарии у `[tool.ruff]`/`[tool.mypy]`),
а не мусор, и делать CI красным с первого дня хуже, чем не делать вовсе:
на красный перестают смотреть через неделю.

Приём тот же, что в `tests/test_function_size.py` — **не второй способ**.
Отличие одно: там список поимённый (функция → строки), здесь порог один
на инструмент (находок на весь проект), потому что у `ruff`/`mypy` нет
устойчивого «имени находки», по которому можно было бы вести список так же,
как по имени функции — одна и та же ошибка типа сдвигается на другую строку
от одной правки в соседнем месте файла.

Из этого отличия следует ассиметрия правил, и она — часть задачи, а не
недосмотр: прогон падает, если находок стало **больше** порога, и проходит
молча, если находок стало меньше или столько же. Требовать точного совпадения
(как для длины функции) было бы неверно: любая случайная попутная починка
одной находки в другом файле немедленно ломала бы этот тест до тех пор,
пока кто-то руками не поправит число — трение без пользы. Обратная сторона:
числа `KNOWN_*` ниже не сползают вниз сами и требуют ручного пересмотра,
когда разбор долга продвинется, — иначе храповик держит вчерашний уровень
вместо сегодняшнего.

Отдельно от храповика в CI есть два **справочных** шага (`ruff`/`mypy` без
проверки кода возврата) — они показывают текущий список находок в логе.
Годится ли число — решает этот тест, а не то, красный или зелёный тот шаг.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import subprocess
import sys
from typing import Final

import pytest

ROOT: Final[pathlib.Path] = pathlib.Path(__file__).resolve().parent.parent

#: Порог `ruff`. Замер 07.09.2026: `ruff check . --output-format=json`,
#: длина массива — 575. Совпадает с числом из `Found 575 errors.` в обычном
#: выводе и с записью в `.docs/TESTING.md`/`CLAUDE.md` на ту же дату.
KNOWN_RUFF_FINDINGS: Final[int] = 575

#: Порог `mypy`. Замер 07.09.2026: строка `Found 190 errors in 57 files`.
KNOWN_MYPY_FINDINGS: Final[int] = 190


def _tool_is_installed(module: str) -> bool:
    """`ruff`/`mypy` стоят отдельной группой `lint` (`pyproject.toml`).

    Кто-то мог поставить окружение без неё (`uv sync` без `--all-groups`).
    Это не повод падать: это повод сказать явно, что порог не проверен,
    а не притвориться, что находок ноль — тихий ноль здесь опаснее падения.
    """
    return importlib.util.find_spec(module) is not None


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _ruff_finding_count() -> int:
    """Число находок `ruff` по всему дереву, как его видит `ruff check .`.

    JSON, а не подсчёт строк текстового вывода: у находки бывает несколько
    строк контекста, а формат `--output-format=json` — документированный
    машиночитаемый контракт самого `ruff`, а не наш разбор его текста.
    """
    result = _run(["ruff", "check", ".", "--output-format=json"])
    if result.returncode not in (0, 1):
        pytest.fail(
            f"ruff завершился не находками, а сбоем (код {result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    try:
        return len(json.loads(result.stdout))
    except json.JSONDecodeError:
        pytest.fail(
            f"не удалось разобрать JSON от ruff:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )


_MYPY_ERROR_COUNT = re.compile(r"Found (\d+) error")


def _mypy_finding_count() -> int:
    """Число находок `mypy` по сводной строке его собственного вывода.

    У этой версии `mypy` нет устойчивого машиночитаемого счётчика (`--output`
    принимает произвольный формат, но не документированный `json` для сводки
    по числу ошибок), поэтому разбирается итоговая строка вида
    `Found N errors in M files` — тот же текст, что видит человек, гоняя
    `mypy` руками, и настолько же стабильный формат.
    """
    result = _run(["mypy"])
    if result.returncode not in (0, 1):
        pytest.fail(
            f"mypy завершился не находками, а сбоем (код {result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    if "Success: no issues found" in result.stdout:
        return 0
    match = _MYPY_ERROR_COUNT.search(result.stdout)
    if match is None:
        pytest.fail(f"не удалось разобрать сводку mypy:\n{result.stdout}")
    return int(match.group(1))


@pytest.mark.slow
def test_ruff_findings_have_not_grown_past_the_recorded_threshold() -> None:
    """Новых находок `ruff` сверх порога не появилось."""
    if not _tool_is_installed("ruff"):
        pytest.skip("ruff не установлен — окружение собрано без группы lint, порог не проверен")
    actual = _ruff_finding_count()
    assert actual <= KNOWN_RUFF_FINDINGS, (
        f"находок ruff стало больше: было {KNOWN_RUFF_FINDINGS}, стало {actual} "
        f"(+{actual - KNOWN_RUFF_FINDINGS}). Почини новую находку либо, если рост "
        "осознан, подними KNOWN_RUFF_FINDINGS в этом файле с объяснением — но не молча"
    )


@pytest.mark.slow
def test_mypy_findings_have_not_grown_past_the_recorded_threshold() -> None:
    """Новых находок `mypy` сверх порога не появилось."""
    if not _tool_is_installed("mypy"):
        pytest.skip("mypy не установлен — окружение собрано без группы lint, порог не проверен")
    actual = _mypy_finding_count()
    assert actual <= KNOWN_MYPY_FINDINGS, (
        f"находок mypy стало больше: было {KNOWN_MYPY_FINDINGS}, стало {actual} "
        f"(+{actual - KNOWN_MYPY_FINDINGS}). Почини новую находку либо, если рост "
        "осознан, подними KNOWN_MYPY_FINDINGS в этом файле с объяснением — но не молча"
    )
