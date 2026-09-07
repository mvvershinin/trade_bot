"""Замер распределения размера и сложности функций по продуктовому коду.

Метрики берутся у самого ruff (порог 0 — он называет число по каждой функции),
кроме длины тела: такого правила в ruff нет, её считаем по AST.
"""

from __future__ import annotations

import ast
import pathlib
import re
import subprocess
import tomllib

RUFF = ".venv/bin/ruff"


def product_layers() -> list[str]:
    """Слои продукта — из `pyproject.toml`, а не списком здесь.

    ⚠️ Список слоёв в этом проекте уже размножен: `tests/test_layers.py`,
    `tests/test_function_size.py` и маски пакетов в `pyproject.toml`. Четвёртая
    рукописная копия — ровно то, за чем велено следить правилом 9 `CLAUDE.md`:
    перечисление одного и того же руками в нескольких местах. Найдено
    `/validate` 03.09.2026 в этом самом файле, в день, когда правило заводили.

    Цена копии здесь не косметическая: восьмой слой или переименование —
    и замер размера кода молча перестанет его видеть, а по этому замеру
    выбраны пороги линтера.

    Берётся `[tool.setuptools.packages.find] include`: это состав поставки,
    то есть определение «слой продукта» по самому строгому счёту.
    """
    config = tomllib.loads(pathlib.Path("pyproject.toml").read_text(encoding="utf-8"))
    masks = config["tool"]["setuptools"]["packages"]["find"]["include"]
    return [mask.removesuffix("*") for mask in masks]


#: Ключи латиницей — правило 6 `CLAUDE.md`: имя есть имя, даже если оно ключ
#: словаря. Русский остаётся в подписи, которую читает человек.
RULES = {
    "complexity": ("C901", "lint.mccabe.max-complexity = 0", "сложность (C901)"),
    "statements": ("PLR0915", "lint.pylint.max-statements = 0", "операторы (PLR0915)"),
    "branches": ("PLR0912", "lint.pylint.max-branches = 0", "ветвления (PLR0912)"),
    "arguments": ("PLR0913", "lint.pylint.max-args = 0", "аргументы (PLR0913)"),
    "returns": ("PLR0911", "lint.pylint.max-returns = 0", "возвраты (PLR0911)"),
}

VALUE = re.compile(r"\((\d+) > \d+\)")


def measured(rule: str, config: str) -> list[int]:
    out = subprocess.run(
        [RUFF, "check", "--no-cache", "--isolated", "--select", rule,
         "--config", config, *product_layers()],
        capture_output=True, text=True,
        check=False,  # ruff выходит с кодом 1, когда нашёл нарушения, — это норма
    ).stdout
    return sorted(int(m.group(1)) for m in VALUE.finditer(out))


def body_lengths() -> list[tuple[int, str]]:
    """Длина тела функции в строках: от первого оператора до последнего."""
    found: list[tuple[int, str]] = []
    for folder in product_layers():
        for path in pathlib.Path(folder).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                body = [n for n in node.body if not (
                    isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str)
                )]  # докстринг за размер не считаем
                if not body:
                    continue
                length = (body[-1].end_lineno or 0) - body[0].lineno + 1
                found.append((length, f"{path}:{node.lineno} {node.name}"))
    return sorted(found, reverse=True)


def report(title: str, values: list[int], candidates: list[int]) -> None:
    n = len(values)
    if not n:
        print(f"{title}: пусто")
        return
    def pct(p: float) -> int:
        return values[min(n - 1, int(n * p))]
    print(f"\n{title}: функций {n}, медиана {pct(0.5)}, "
          f"p90 {pct(0.9)}, p95 {pct(0.95)}, p99 {pct(0.99)}, максимум {values[-1]}")
    for limit in candidates:
        over = sum(1 for v in values if v > limit)
        print(f"    порог {limit:>3}  →  срабатываний {over:>3}  "
              f"({over / n * 100:.1f}% функций)")


def main() -> None:
    for rule, config, title in RULES.values():
        values = measured(rule, config)
        top = values[-1] if values else 0
        candidates = sorted({x for x in (5, 8, 10, 12, 15, 20, 25, 30, 40, 50, 60, 75)
                             if x <= top} | {top})
        report(title, values, candidates[:8])

    lengths = body_lengths()
    values = sorted(x for x, _ in lengths)
    report("длина тела, строк (AST)", values, [30, 40, 50, 60, 75, 100, 150, 200])
    print("\n    самые длинные:")
    for length, where in lengths[:12]:
        print(f"      {length:>4}  {where}")


if __name__ == "__main__":
    main()
