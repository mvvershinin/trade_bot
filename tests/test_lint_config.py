"""Настройка линтера и дерево не разъезжаются — проверяется без линтера.

Зачем этот файл существует
--------------------------

В `pyproject.toml` записано, какие правила `ruff` проверяет. В коде записано,
какие правила подавлены (`# noqa: КОД`). Между этими двумя списками бывает
ровно две беды, и обе тихие:

1. **Подавление без правила.** Код заглушен, но правило не выбрано или гасится
   в `per-file-ignores`. Подавление мертво: оно ничего не подавляет и создаёт
   впечатление, что место разобрано. Именно так в проекте появились 56 `# noqa`
   и 51 `# type: ignore` для инструментов, которых не было ни одного.
2. **Инвентарь в комментарии.** Список кодов, записанный словами рядом
   с настройкой, устаревает при первой же правке — и устарел он однажды внутри
   той самой правки, которая его меняла.

Поэтому здесь не комментарий, а прогон: инвентарь считается по дереву каждый
раз заново и сверяется с настройкой. `ruff` для этого не нужен — и это важно,
потому что на 31.08.2026 его в окружении нет.

Чего этот файл не делает
------------------------

Он **не** отвечает на вопрос «какое подавление лишнее». Мёртвое подавление —
это то, чьё правило не проверяется; лишнее — это то, чьё правило проверяется,
но не срабатывает. Второе умеет только сам `ruff` (`RUF100`), и до его
установки ответа нет.
"""

from __future__ import annotations

import io
import pathlib
import re
import tokenize
import tomllib
from typing import Final

import pytest

REPO_ROOT: Final = pathlib.Path(__file__).resolve().parent.parent
PYPROJECT: Final = REPO_ROOT / "pyproject.toml"

SKIP_DIRS: Final = frozenset({
    ".git", ".venv", "venv", "env", "reference", "userdata",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "terminal.egg-info", "dist", "build", "AppDir", "node_modules",
})

NOQA: Final = re.compile(r"#\s*noqa:\s*([A-Z]+[0-9]+)")


def _sources() -> list[pathlib.Path]:
    return sorted(
        path
        for path in REPO_ROOT.rglob("*.py")
        if not SKIP_DIRS.intersection(path.relative_to(REPO_ROOT).parts)
    )


def suppressions() -> list[tuple[str, int, str]]:
    """Все `# noqa: КОД`, которые `ruff` действительно увидит.

    Считаются только те, что лежат в токене-комментарии. Подавление внутри
    строкового литерала — а такие в дереве есть: питоновский исходник,
    уезжающий дочернему процессу, — до линтера не доходит никогда, и считать
    его в инвентаре значит завышать защищённость.
    """
    found: list[tuple[str, int, str]] = []
    for path in _sources():
        src = path.read_text(encoding="utf-8")
        comments = {
            t.start[0]: t.string
            for t in tokenize.generate_tokens(io.StringIO(src).readline)
            if t.type == tokenize.COMMENT
        }
        for line, text in comments.items():
            match = NOQA.search(text)
            if match:
                found.append((str(path.relative_to(REPO_ROOT)), line, match.group(1)))
    return found


def ruff_lint_config() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["ruff"]["lint"]


def is_checked(code: str, path: str, config: dict) -> tuple[bool, str]:
    """Проверяется ли `code` в файле `path` при этой настройке."""
    select = config["select"]
    if not any(code.startswith(prefix) for prefix in select):
        return False, "правило не выбрано в select"
    if code in config.get("ignore", ()):
        return False, "правило в глобальном ignore"
    for pattern, codes in config.get("per-file-ignores", {}).items():
        if path.startswith(pattern.removesuffix("/**")) and code in codes:
            return False, f"правило гасится per-file-ignores {pattern}"
    return True, ""


def test_no_suppression_is_dead() -> None:
    """Каждое подавление глушит правило, которое действительно проверяется.

    Мёртвое подавление хуже отсутствующего: оно выглядит как разобранное место.
    """
    config = ruff_lint_config()
    dead = [
        f"{path}:{line}: {code} — {why}"
        for path, line, code in suppressions()
        if not is_checked(code, path, config)[0]
        for why in [is_checked(code, path, config)[1]]
    ]
    assert not dead, (
        "подавление, которое ничего не подавляет — правило не проверяется "
        "в этом пути. Либо снести подавление, либо включить правило:\n  "
        + "\n  ".join(dead)
    )


def test_the_inventory_is_not_empty() -> None:
    """Сторож обязан что-то видеть: пустой обход прошёл бы вхолостую."""
    found = suppressions()
    assert len(found) >= 20, (
        f"найдено подавлений: {len(found)}. Либо их правда столько, "
        "и порог надо опустить осознанно, либо обход сломался"
    )


def test_the_guard_notices_a_rule_that_is_not_selected() -> None:
    """Подсадка: код вне `select` обязан быть опознан как мёртвый."""
    config = {"select": ["E", "F"], "ignore": [], "per-file-ignores": {}}
    assert is_checked("F401", "app/main.py", config)[0]
    ok, why = is_checked("SLF001", "app/main.py", config)
    assert not ok and "select" in why


def test_the_guard_notices_a_globally_ignored_rule() -> None:
    config = {"select": ["D"], "ignore": ["D203"], "per-file-ignores": {}}
    ok, why = is_checked("D203", "ui/models.py", config)
    assert not ok and "ignore" in why


def test_the_guard_notices_a_per_file_ignore() -> None:
    config = {"select": ["PL"], "ignore": [], "per-file-ignores": {"tests/**": ["PLR2004"]}}
    assert is_checked("PLR2004", "engine/runner.py", config)[0]
    ok, why = is_checked("PLR2004", "tests/test_x.py", config)
    assert not ok and "per-file-ignores" in why


def test_a_suppression_inside_a_string_literal_is_not_counted() -> None:
    """Подавление внутри исходника-строки до `ruff` не доходит.

    Такое в дереве есть: `# noqa: F401` живёт внутри `IMPORT_PROBE`
    в `tests/test_broker_no_leak.py` — питоновского исходника, который уезжает
    дочернему процессу через `python -c`. Если считать его в инвентаре,
    получится, что `F401` в дереве подавлен, а на деле линтер его не видит.
    """
    probe = REPO_ROOT / "tests" / "test_broker_no_leak.py"
    text = probe.read_text(encoding="utf-8")
    assert "# noqa: F401" in text, "подсадка исчезла — тест потерял предмет"
    counted = {code for path, _, code in suppressions() if path.endswith("test_broker_no_leak.py")}
    assert "F401" not in counted, (
        "подавление из строкового литерала попало в инвентарь: "
        "защищённость дерева посчитана выше фактической"
    )


@pytest.mark.parametrize("section", ["select", "ignore", "per-file-ignores"])
def test_the_config_has_the_sections_this_file_reads(section: str) -> None:
    """Настройку переименовали — тест обязан упасть, а не пройти вхолостую."""
    assert section in ruff_lint_config()
