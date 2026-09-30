"""Раздел версии для страницы выпуска берётся из `CHANGELOG.md` и не молчит."""

from __future__ import annotations

import subprocess
import sys

from tools.changelog_section import CHANGELOG, section

SAMPLE = """# Что нового

## [0.3.0] — 01.10.2026

### Добавлено
- новое

## [0.2.0] — 30.09.2026

### Исправлено
- старое
"""


def test_the_section_stops_at_the_next_version() -> None:
    assert section(SAMPLE, "0.3.0") == "### Добавлено\n- новое"
    assert section(SAMPLE, "0.2.0") == "### Исправлено\n- старое"


def test_a_missing_or_empty_section_is_none() -> None:
    assert section(SAMPLE, "9.9.9") is None
    assert section("## [1.0.0] — пусто\n\n## [0.9.0]\n- было\n", "1.0.0") is None


def test_the_version_is_matched_exactly() -> None:
    assert section("## [10.2.0]\n- чужое\n", "0.2.0") is None


def test_the_command_refuses_out_loud_without_a_section() -> None:
    done = subprocess.run(
        [sys.executable, "tools/changelog_section.py", "9.9.9"],
        capture_output=True, text=True, check=False,
    )
    assert done.returncode == 1
    assert "9.9.9" in done.stderr
    assert done.stdout == ""


def test_the_released_version_has_its_section() -> None:
    """Выпущенная 0.2.0 обязана иметь заметки — иначе CI не опубликовал бы её."""
    assert section(CHANGELOG.read_text(encoding="utf-8"), "0.2.0")
