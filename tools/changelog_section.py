"""Раздел версии из `CHANGELOG.md` — для текста страницы выпуска.

`python tools/changelog_section.py 0.2.0` печатает тело раздела `## [0.2.0]`
без заголовка. Раздела нет или он пуст — код возврата 1 и строка в stderr:
выпуск без заметок о нём не должен выйти молча (правило 13).
"""

from __future__ import annotations

import pathlib
import re
import sys

CHANGELOG = pathlib.Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def section(text: str, version: str) -> str | None:
    """Тело раздела `## [version]` до следующего `## [`; `None`, если его нет."""
    head = re.compile(rf"^## \[{re.escape(version)}\][^\n]*\n", re.MULTILINE)
    found = head.search(text)
    if found is None:
        return None
    rest = text[found.end():]
    following = re.search(r"^## \[", rest, re.MULTILINE)
    body = rest[: following.start()] if following else rest
    return body.strip() or None


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("нужен ровно один довод — версия, например 0.2.0", file=sys.stderr)
        return 2
    body = section(CHANGELOG.read_text(encoding="utf-8"), argv[0])
    if body is None:
        print(
            f"в {CHANGELOG.name} нет раздела «## [{argv[0]}]» или он пуст — "
            "допишите его до тега",
            file=sys.stderr,
        )
        return 1
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
