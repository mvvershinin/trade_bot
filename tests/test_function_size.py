"""Предел длины функции: храповик, а не запрет задним числом.

Правила «функция не длиннее N строк» в `ruff` нет — ни в одном префиксе.
Ближайшее, `PLR0915`, считает **операторы**, а не строки, и длинная функция
из простых присваиваний ему незаметна. Поэтому проверка живёт здесь: тест
разбирает дерево и меряет тело от первого оператора до последнего.

Почему храповик, а не голый запрет
-----------------------------------
На 03.09.2026 предел нарушают десять функций, самая длинная — 285 строк.
Запрет «упасть, если длиннее 75» сделал бы прогон красным навсегда, а красный
прогон перестают читать через день: он не отличает новую беду от известной.
Подавление же (`# noqa`, список исключений «чтобы позеленело») прячет список
на рефакторинг с глаз — ровно то, чего делать нельзя.

Храповик решает обе задачи сразу. Известные нарушители перечислены **поимённо
с их размером**, и тест падает в трёх случаях:

* появилась новая функция длиннее предела — новая беда видна сразу;
* известная функция **выросла** — деградация видна, даже если она уже в списке;
* известная функция стала короче предела, но осталась в списке — заставляет
  вычеркнуть строку, иначе список гниёт и перестаёт быть планом работ.

Третий случай — не педантизм. Список без него превращается в кладбище имён,
по которому нельзя сказать, сколько работы осталось.

⚠️ Уменьшать числа в `KNOWN` руками, не тронув код, запрещено: это то же
подавление, только записанное цифрой. Число меняется вместе с функцией.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

#: Предел длины тела в строках. Замер 03.09.2026 по продуктовым слоям:
#: медиана 5, p90 24, p95 41, p99 84, максимум 285. Порог 75 даёт 10
#: нарушителей (1.2% функций), порог 100 — семь, порог 50 — двадцать восемь.
#: Взято 75: чуть ниже p99, то есть граница настоящего хвоста, и список
#: из десяти имён — обозримая работа, а не разбор четверти дерева.
LIMIT = 75

#: Слои продукта. `tests/`, `tools/` и `mockups/` не проверяются: у теста
#: длина бывает оправдана перечислением случаев, и это не тот код, который
#: читают под давлением.
LAYERS = ("engine", "market", "backtest", "broker", "strategies", "ui", "app")

#: Известные нарушители на 03.09.2026 и их размер. Список — план работ
#: по разбору длинных функций; порядок по убыванию размера.
#:
#: ⚠️ `process_closed_candle` — десять шагов обработки свечи в одном теле
#: (`PROTOTYPE.md` §2). Его разбор трогает ядро, считающее деньги, поэтому
#: делается только с перегоном сверки с прототипом после каждого выделенного
#: куска: 127 сделок из 127 построчно.
#: ⚠️ Разобрано 05.09.2026: `ui/settings_dialog.py::SettingsDialog._build_fields`
#: (было 124). Разошлось на пять строителей по группам окна плюс два
#: помощника, задающих диапазон и знаки после запятой данными, а не пятёркой
#: вызовов на каждое поле.
KNOWN: dict[str, int] = {
    "engine/pipeline.py::process_closed_candle": 285,
    "market/sync.py::sync_minutes": 137,
    "broker/token_store.py::TokenStore.load": 136,
    "broker/token_store.py::TokenStore.save": 134,
    "ui/notices.py::guard_note": 109,
    "broker/session.py::BrokerSession._exchange": 95,
    "ui/status_panel.py::StatusPanel.apply_state": 83,
}

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _functions(tree: ast.Module) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Все функции файла с полным именем: `Класс.метод`, `внешняя.вложенная`."""
    found: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                found.append((f"{prefix}{child.name}", child))
                walk(child, f"{prefix}{child.name}.")
            else:
                walk(child, prefix)

    walk(tree, "")
    return found


def _body_length(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Длина тела в строках. Докстринг не считается — он не логика.

    Считается от первой строки первого оператора до последней строки
    последнего: так многострочный вызов виден целиком, а не как один оператор.
    """
    body = [
        item for item in node.body
        if not (
            isinstance(item, ast.Expr)
            and isinstance(item.value, ast.Constant)
            and isinstance(item.value.value, str)
        )
    ]
    if not body:
        return 0
    return (body[-1].end_lineno or body[-1].lineno) - body[0].lineno + 1


def _measured() -> dict[str, int]:
    """Все функции продуктовых слоёв длиннее предела."""
    over: dict[str, int] = {}
    for layer in LAYERS:
        for path in sorted((ROOT / layer).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            key_path = path.relative_to(ROOT).as_posix()
            for name, node in _functions(tree):
                size = _body_length(node)
                if size > LIMIT:
                    over[f"{key_path}::{name}"] = size
    return over


def test_no_new_function_is_longer_than_the_limit() -> None:
    """Новых длинных функций не появилось."""
    over = _measured()
    fresh = {key: size for key, size in over.items() if key not in KNOWN}
    assert not fresh, (
        f"функций длиннее {LIMIT} строк стало больше: "
        + ", ".join(f"{key} — {size}" for key, size in sorted(fresh.items()))
        + ". Разнести по методам либо, если это осознанное исключение, "
        "добавить в KNOWN с объяснением — но не молча"
    )


def test_known_long_functions_did_not_grow() -> None:
    """Известные длинные функции не выросли."""
    over = _measured()
    grown = {
        key: (KNOWN[key], size)
        for key, size in over.items()
        if key in KNOWN and size > KNOWN[key]
    }
    assert not grown, (
        "длинная функция стала длиннее: "
        + ", ".join(f"{key} — было {was}, стало {now}" for key, (was, now) in sorted(grown.items()))
        + ". Правка в такую функцию либо укорачивает её, либо выносится наружу"
    )


def test_the_list_of_long_functions_is_not_stale() -> None:
    """В списке нет строк, которые уже разобраны.

    Падает, когда функцию укоротили, а строку в `KNOWN` не убрали. Иначе
    список перестаёт быть планом работ: по нему нельзя сказать, сколько
    осталось.
    """
    over = _measured()
    done = sorted(key for key in KNOWN if key not in over)
    assert not done, (
        "разобрано, но осталось в списке KNOWN: "
        + ", ".join(done)
        + ". Убрать строки — это и есть отметка о выполненной работе"
    )


@pytest.mark.parametrize("key", sorted(KNOWN))
def test_every_known_entry_points_at_a_real_function(key: str) -> None:
    """Каждая строка списка указывает на существующую функцию.

    Переименовали или перенесли, а список не поправили — и храповик перестал
    стеречь эту функцию, ничего об этом не сказав.
    """
    path, _, name = key.partition("::")
    source = ROOT / path
    assert source.exists(), f"{key}: файла нет, список устарел"
    names = {found for found, _ in _functions(ast.parse(source.read_text(encoding="utf-8")))}
    assert name in names, f"{key}: функции с таким именем в файле нет, список устарел"
