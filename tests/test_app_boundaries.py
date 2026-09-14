"""Границы слоя `app/` и порядок запуска, который нельзя нарушить молча.

`app/` — сборка и проводка. Ему разрешено импортировать всё, и именно поэтому
его границы приходится сторожить отдельно: слой, которому можно всё, — самое
удобное место, куда логика утекает из движка.

Проверки статические. Они ловят момент, когда правило нарушено, а не когда
это выстрелит у владельца счёта.
"""

from __future__ import annotations

import ast
import copy
import pathlib

import pytest

APP = pathlib.Path(__file__).resolve().parent.parent / "app"
SOURCES = sorted(path for path in APP.rglob("*.py") if "__pycache__" not in path.parts)

#: Расчётные библиотеки. След того, что сборка начала считать сама. Числа
#: приходят из `engine/` и `backtest/` готовыми: посчитанные ещё раз здесь,
#: они разойдутся с журналом робота и разойдутся молча.
CALCULATION = {"pandas", "numpy", "scipy", "statistics", "ta", "talib"}

#: Сеть. Ходить в сеть — работа `broker/` и `market/`; `app/` их собирает,
#: но не разговаривает с брокером сам.
NETWORK = {"httpx", "websockets", "aiohttp", "urllib", "urllib3", "socket", "requests"}

#: Имена библиотек отрисовки. Живут только в `ui/chart/`, иначе замена
#: отрисовки по ТЗ §6 перестаёт быть заменой одного блока. Ни одного из них
#: в дереве сегодня нет — веб-график удалён 09.09.2026 (решение 0056),
#: а отрисовка на Qt библиотекой не является. Список запрещает будущее,
#: а не описывает настоящее; зоркость самой проверки стережёт канарейка
#: в `tests/test_ui_no_trading_logic.py`.
RENDERERS = ("QtWebEngine", "pyqtgraph", "LightweightCharts", "QtCharts")

#: Типы сигнала торгового модуля. Их появление в `app/` означало бы, что
#: сборка начала разбирать намерения — то есть принимать решения.
SIGNALS = {"Bar", "Decision", "Intent"}

#: Внутренности разбора свечи. Движок вызывается целиком (`Engine`, `replay`),
#: а не по шагам: собранная из шагов вторая линия обработки разойдётся
#: с первой и не упадёт при этом.
INTERNALS = {"process_closed_candle", "apply_fill", "after_submit", "after_refused_exit"}

#: ЧТО `app/` ВПРАВЕ ВЗЯТЬ У ТОРГОВЫХ СЛОЁВ. Разрешительный список, а не
#: запретительный, и это принципиально: `engine/__init__.py` экспортирует
#: полсотни имён, среди которых `exit_signal`, `to_bar`, `intake`,
#: `take_order_id`, `fixed_level`, `round_level`, `target_profit`,
#: `covers_commission`. `from engine import exit_signal` — это и есть правило
#: входа и выхода; перечислить всё, чего нельзя, невозможно, а список того,
#: что нужно, короткий и объяснимый.
#:
#: Каждое имя здесь стоит по причине, и причина одна и та же: значение или
#: правило, второй реализации которого в программе быть не должно.
ALLOWED: dict[str, set[str]] = {
    "engine": {
        # настройки и их перевод
        "EngineSettings", "Mode", "Reversal",
        # значения, которые перекладываются в объекты окна
        "Side", "PositionState", "JournalEntry", "JournalLevel", "OrderAction",
        # ⚠️ `Engine` — движок ЦЕЛИКОМ, и это ровно та форма, которую
        # разрешает комментарий у `INTERNALS`: живой ход (`app/observe.py`)
        # создаёт движок один раз и подаёт ему свечи, а не собирает свою
        # обработку из его шагов. Шаги по-прежнему запрещены.
        "Engine",
        # `OrderRequest` — значение заявки в подписи исполнителя-тени.
        # Такое же значение, как `JournalEntry` выше: сборка его не читает
        # и не толкует, а только называет тип в подписи.
        "OrderRequest",
        # ⚠️ `AccountFunds` — деньги счёта, которые кладут в движок
        # (`app/live_feed.py::funds_of`, решения 0035 и 0040). Это **значение**,
        # а не расчёт: сборка перекладывает в него три числа из ответа брокера
        # и ни одного не считает. Второй реализации у него быть не может
        # по построению — `broker/` про движок не знает, движок про брокера
        # не знает, и место встречи ровно одно, здесь.
        "AccountFunds",
        # правила времени: одно на программу, и берутся они у движка.
        # `in_moscow` — приведение к МСК, которым сборка считает «день»
        # в панели «за день»: голый `.date()` брал бы день того пояса,
        # в котором момент пришёл, а наивный момент проходил бы молча.
        # Второй реализации приведения в программе быть не должно.
        "MSK", "TradingWindow", "close_time", "is_trading_day", "in_moscow",
        # ⚠️ Календарь нерабочих дней (`F-002`, решение 0038). `day_verdict` —
        # то же правило торгового дня, что и `is_trading_day`, но с причиной:
        # затенение графика обязано подписать полосу тем же словом, каким
        # движок объяснил молчание в журнале. `DayRule` — ключи этих причин,
        # `DayMarks` — форма набора дат, которую движок принимает. Ни одно
        # из трёх имён правил не содержит: правила живут в `engine/window.py`.
        "DayMarks", "DayRule", "DayVerdict", "day_verdict",
    },
    "strategies": {
        # ⚠️ **Имени ни одного торгового алгоритма здесь нет, и это главный
        # результат З2** миниплана `strategy-modules-switchable.md`. До
        # 09.09.2026 список содержал `EmaReverse`, `EmaReverseSettings`,
        # `AverageKind` и `OnPriceEqualsAverage` — то есть сборка знала,
        # каким правилом торгует. Второй алгоритм при этом не падал бы:
        # окно показывало бы его название, а решения считал бы первый.
        #
        # `Strategy` — только имя типа в подписи живого хода: он принимает
        # уже настроенный модуль и внутрь него не смотрит.
        "Strategy",
        # `StrategySettings` — порт настроек модуля: подпись линии
        # на графике и строки журнала «было → стало». Обе — текст, который
        # посчитал сам модуль; сборка его перекладывает и не толкует.
        "StrategySettings",
        # ⚠️ `registry` — таблица торговых модулей ЦЕЛИКОМ. Сборка берёт
        # у него всё, что зависит от алгоритма: сам модуль (`entry.build`),
        # таблицу полей настроек, подписи этих полей и правило робота
        # словами. Считает всё это сам модуль — окно торговых слоёв
        # не импортирует и правило посчитать не может, а написанное руками
        # в `ui/` описание разошлось бы с кодом при первой правке модуля
        # и разошлось бы молча.
        "registry",
    },
    "backtest": {
        # прогон целиком и его готовые числа — ни одного шага по отдельности
        "replay", "HistoryRun", "Deal", "Pair", "Summary",
        "pairs", "reversal_entries", "summarise",
        # ⚠️ `HistoryExecutor` — исполнитель живого хода (`app/observe.py`).
        # Взят целиком и намеренно: наблюдение обязано считать сделки ТОЙ ЖЕ
        # моделью, что прогон по истории, иначе расхождение между ними
        # означало бы не данные, а две разные модели исполнения — и сверять
        # живой ход с историей стало бы нечем. Своей модели в `app/` при этом
        # не появляется: наследник добавляет счётчик поданного и ничего больше.
        "HistoryExecutor",
        # `Plan` — «где робот собирался войти или выйти», готовое значение
        # прогона. Живой ход складывает такие же, чтобы окно рисовало слой
        # прогноза одним кодом на обоих путях.
        "Plan",
        # `Costs` — издержки прогона одним значением. Комиссия и
        # проскальзывание считаются в `backtest/`, сборка их только
        # переносит; второй реализации издержек в программе быть не должно.
        "Costs",
        # `BREATHE_EVERY` — через сколько свечей прогон отдаёт управление
        # циклу событий. Число про живость окна, и оно одно на программу:
        # своё в `app/` разошлось бы с прогоном молча.
        "BREATHE_EVERY",
        # `headline` — оговорка, которая едет рядом с числом (решение 0030).
        # Текст принадлежит тому, кто число посчитал; своя формулировка
        # в сборке разошлась бы с отчётом.
        "headline",
        # ⚠️ `ForeignStrategy` — отказ перебора чужому алгоритму (ловушка 14
        # миниплана `strategy-modules-switchable.md`, З5). Сетка перебора
        # написана под поля **одного** алгоритма и объявляет это сама
        # (`backtest/sweep.py::GRID_STRATEGY_ID`); сборка ловит отказ,
        # чтобы `python3 -m app.leaders` сказал причину фразой и вернул
        # код возврата, а не трассировку. Взято **имя исключения** —
        # ни правила отбора, ни сетки в `app/` от этого не появляется.
        "ForeignStrategy",
        # ------------------------------------------------- перебор лидеров
        # ⚠️ Шесть имён `backtest.leaders`, и ни одно из них не шаг расчёта.
        # Разбор 06.09.2026: первая редакция держала весь перебор в `app/`
        # и брала у слоя девятнадцать имён — сетку, деление истории, поправки
        # на подгонку. Это была вторая линия расчёта в слое проводки, и сторож
        # её поймал. Расчёт переехал в `backtest/leaders.py`; здесь осталось
        # то, чего у торгового слоя быть не может: библиотека шаблонов
        # и журнал прогонов.
        #
        # `prepare` — собрать условия замера; деление истории на подбор
        # и проверку считает торговый слой, а не сборка: правило «подбор
        # не заходит на проверку» стережёт `backtest.split.Fold`, и обойти
        # его параметром нельзя.
        "prepare",
        # `study` — ВЕСЬ замер одним вызовом. Та же форма, что `Engine`
        # и `replay` выше: сборка зовёт расчёт целиком, а не собирает его
        # из шагов. Замер, собранный в `app/` из кусков, разошёлся бы
        # с торговым слоем молча.
        "study",
        # `Study`, `Stretch` — готовые ЗНАЧЕНИЯ замера: что вышло и какие
        # отрезки прогнаны. Сборка их не считает и не толкует, а переносит
        # в свод, в шаблоны и в журнал.
        "Study", "Stretch",
        # `Recipe` — сочетание настроек в простых величинах: время, число,
        # флаг. Сетка описана ОДИН раз в торговом слое и разворачивается
        # двумя слоями: там — в настройки движка, здесь — в набор окна,
        # который ложится в шаблон. Обратного перевода в программе нет
        # и заводить его нельзя (`D-051`); согласие двух разворотов
        # проверяется тестом по всей сетке из 9 744 сочетаний.
        "Recipe",
        # `summary_text` — свод текстом. Та же причина, что у `headline`:
        # текст принадлежит тому, кто числа посчитал, и своя формулировка
        # в сборке разошлась бы с таблицей.
        "summary_text",
        # `bars_of`, `days_of`, `snapshot` — чтение ряда свечей и отбор
        # торговых дней. Правило «какой день торговый» живёт
        # в `backtest.split.trading_days`, и второй его реализации быть
        # не должно: сборка спрашивает готовый ответ, чтобы подать его
        # в `prepare`.
        "bars_of", "days_of", "snapshot",
        # `LEADERS_WANTED` — сколько лидеров просил владелец счёта.
        # Умолчание ключа командной строки; число одно на программу.
        "LEADERS_WANTED",
    },
}


@pytest.fixture(scope="module")
def parsed() -> list[tuple[pathlib.Path, ast.AST, str]]:
    result = []
    for path in SOURCES:
        text = path.read_text(encoding="utf-8")
        result.append((path, ast.parse(text, filename=str(path)), text))
    assert result, "в app/ не разобрано ни одного файла — проверка вакуумна"
    return result


def _imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def _code(tree: ast.AST) -> str:
    """Исходник без докстрингов и комментариев.

    Поиск по тексту иначе ловит сам себя: в `app/main.py` написано, почему
    `qasync.run()` не используется, — и проверка «этой строки нет» падает
    на объяснении, а не на нарушении.
    """
    tree = copy.deepcopy(tree)
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = node.body
        if (
            body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _used_names(tree: ast.AST) -> dict[str, set[str]]:
    """Что взято у каждого слоя: `{"engine": {"close_time", ...}}`.

    Считаются **два** способа, а не один. Прежняя проверка смотрела только
    `ImportFrom`, и обойти её было нечем: `import strategies` с последующим
    `strategies.Intent` проходил мимо сторожа целиком. Здесь имя, взятое
    через точку у импортированного модуля, — такое же взятие, как и в списке
    `from ... import`.
    """
    used: dict[str, set[str]] = {}
    modules: dict[str, str] = {}  # локальное имя → настоящее имя модуля
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                modules[alias.asname or root] = root
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            root = node.module.split(".")[0]
            used.setdefault(root, set()).update(alias.name for alias in node.names)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in modules
        ):
            used.setdefault(modules[node.value.id], set()).add(node.attr)
    return used


def _taken(tree: ast.AST) -> set[str]:
    """Все имена, взятые у торговых слоёв, одним множеством."""
    used = _used_names(tree)
    return set().union(*(used.get(layer, set()) for layer in ALLOWED)) or set()


def test_the_assembly_computes_nothing(parsed) -> None:
    culprits = [
        f"{path.name}: {sorted(_imports(tree) & CALCULATION)}"
        for path, tree, _ in parsed if _imports(tree) & CALCULATION
    ]
    assert not culprits, (
        "в app/ появился расчёт — числа обязаны приходить готовыми:\n  "
        + "\n  ".join(culprits)
    )


def test_the_assembly_never_goes_to_the_network_itself(parsed) -> None:
    culprits = [
        f"{path.name}: {sorted(_imports(tree) & NETWORK)}"
        for path, tree, _ in parsed if _imports(tree) & NETWORK
    ]
    assert not culprits, (
        "сеть в app/ мимо broker/ и market/:\n  " + "\n  ".join(culprits)
    )


def test_the_assembly_does_not_know_what_draws_the_chart(parsed) -> None:
    culprits = [
        f"{path.name}: {name}"
        for path, tree, _ in parsed for name in RENDERERS if name in _code(tree)
    ]
    assert not culprits, (
        "имя библиотеки отрисовки за пределами ui/chart/:\n  " + "\n  ".join(culprits)
    )


def test_the_assembly_does_not_interpret_the_strategy_intents(parsed) -> None:
    culprits = [
        f"{path.name}: {sorted(_taken(tree) & SIGNALS)}"
        for path, tree, _ in parsed if _taken(tree) & SIGNALS
    ]
    assert not culprits, (
        "app/ взял типы сигнала торгового модуля — решения принимает движок:\n  "
        + "\n  ".join(culprits)
    )


def test_the_assembly_never_drives_the_engine_step_by_step(parsed) -> None:
    culprits = [
        f"{path.name}: {sorted(_taken(tree) & INTERNALS)}"
        for path, tree, _ in parsed if _taken(tree) & INTERNALS
    ]
    assert not culprits, (
        "app/ собрал свою обработку свечи из шагов движка:\n  " + "\n  ".join(culprits)
    )


def test_only_the_allowed_names_are_taken_from_the_engine(parsed) -> None:
    """Разрешительный список: что не названо в `ALLOWED`, то нарушение.

    Запретительный список тут не работает по устройству: движок экспортирует
    полсотни имён, и половина из них — само торговое правило. `from engine
    import exit_signal` — это правило входа и выхода, `fixed_level`
    и `target_profit` — расчёт уровня тейка, `to_bar` и `intake` — приём
    свечи. Ни одно из них не попадало ни в один запретительный список,
    и все они проходили сторожа насквозь.

    Появилось новое имя — либо оно здесь по названной причине, либо его
    в `app/` быть не должно.
    """
    culprits = []
    for path, tree, _ in parsed:
        used = _used_names(tree)
        for layer, allowed_names in ALLOWED.items():
            extra = used.get(layer, set()) - allowed_names
            if extra:
                culprits.append(f"{path.name}: из {layer} взято {sorted(extra)}")
    assert not culprits, (
        "app/ взял у торгового слоя то, чего ему не положено. Либо это утечка "
        "логики в сборку, либо имя надо внести в ALLOWED — с причиной:\n  "
        + "\n  ".join(culprits)
    )


def test_the_assembly_does_not_import_on_the_fly(parsed) -> None:
    """Динамический импорт обходит все проверки выше разом.

    `importlib.import_module("engine").exit_signal` статически не виден
    никак — и разрешительный список превращается в украшение.
    """
    culprits = [
        path.name for path, tree, _ in parsed
        if "importlib" in _imports(tree) or "__import__" in _code(tree)
    ]
    assert not culprits, (
        "динамический импорт в app/ — сторожа границ после него ничего "
        f"не значат: {culprits}"
    )


# ------------------------------------------------- порядок запуска (решение 0005)

def test_the_qt_binding_is_chosen_before_qasync_is_imported() -> None:
    """`QT_API` ставится **до** первого импорта qasync.

    qasync перебирает привязки в порядке PyQt5 → PyQt6 → PySide2 → PySide6
    и берёт первую найденную. На машине, где рядом стоит PyQt5, программа
    получит два разных Qt в одном процессе — и упадёт не там, где причина.
    """
    text = (APP / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    assignments = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Subscript)
            and isinstance(target.slice, ast.Constant)
            and target.slice.value == "QT_API"
            for target in node.targets
        )
    ]
    imports = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        and any(alias.name == "qasync" for alias in node.names)
    ]
    assert assignments, "QT_API не ставится вовсе"
    assert imports, "qasync не импортируется — цикл событий собран не так"
    assert max(assignments) < min(imports), (
        "QT_API ставится после импорта qasync — переменная уже ни на что не влияет"
    )


def test_the_loop_is_started_once_and_by_hand() -> None:
    """`qasync.run()` не используется нигде.

    Это `asyncio.run`, а он после остановки цикла входит в него ещё раз ради
    `shutdown_asyncgens`. Замер, на котором правило принято, сделан
    на программе с QtWebEngine внутри: повторный вход давал SIGSEGV
    или зависание (PYSIDE-3451, решение 0005).

    ⚠️ QtWebEngine ушёл из программы 09.09.2026 вместе с веб-графиком
    (решение 0056). Правило осталось, и замера, разрешающего его снять,
    никто не делал. Вторая половина причины от WebEngine не зависела:
    завершение — дозапись журнала, закрытие базы, снятие заявок — живёт
    внутри одной корутины, и второй вход в цикл рвёт его посередине.
    """
    for path in SOURCES:
        code = _code(ast.parse(path.read_text(encoding="utf-8")))
        assert "qasync.run(" not in code, f"{path.name}: qasync.run() запрещён"


def _calls(node: ast.AST) -> list[str]:
    """Вызовы в порядке появления: `объект.метод(...)` и `функция(...)`.

    Голые вызовы функций считаются с 04.09.2026. Прежде собирались только
    `имя.метод(...)`, и `await _close_live(live)` в `finally` главной корутины
    был для проверки невидим: порядок «поток → прогон → база» не стерёг никто,
    хотя докстринг `_close_live` обещал защиту (ворота `/validate`).
    """
    found: list[str] = []
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Call):
            continue
        if isinstance(inner.func, ast.Attribute):
            owner = inner.func.value
            name = owner.id if isinstance(owner, ast.Name) else "?"
            found.append(f"{name}.{inner.func.attr}")
        elif isinstance(inner.func, ast.Name):
            found.append(inner.func.id)
    return found


def _awaited(node: ast.AST) -> list[str]:
    """Имена функций, вызов которых стоит прямо под `await`."""
    return [
        inner.value.func.id
        for inner in ast.walk(node)
        if isinstance(inner, ast.Await)
        and isinstance(inner.value, ast.Call)
        and isinstance(inner.value.func, ast.Name)
    ]


def _shutdown_body(tree: ast.AST) -> list[ast.stmt]:
    """Тело последнего `finally` главной корутины — само завершение."""
    finally_blocks = [
        node for node in ast.walk(_function(tree, "_run"))
        if isinstance(node, ast.Try) and node.finalbody
    ]
    assert finally_blocks, "в _run нет `finally` — завершение не гарантировано ничем"
    return finally_blocks[-1].finalbody


def _function(tree: ast.AST, name: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"в app/main.py нет функции {name} — проверка вакуумна")


def test_closing_the_window_does_not_stop_the_loop() -> None:
    """Иначе завершение обрывается на середине: журнал не дозаписан, база открыта.

    Проверяется **вызов**, а не наличие строки в файле. Прежняя проверка
    искала подстроку в сыром тексте — и находила её в шапке модуля, где эта
    же строка приведена как цитата в объяснении порядка запуска. Удаление
    самого вызова из `main()` проверку не роняло.
    """
    tree = ast.parse((APP / "main.py").read_text(encoding="utf-8"))
    calls = [
        node for node in ast.walk(_function(tree, "main"))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "setQuitOnLastWindowClosed"
    ]
    assert calls, (
        "setQuitOnLastWindowClosed в main() не вызывается: закрытие окна "
        "остановит цикл на середине завершения"
    )
    arguments = [
        arg.value for call in calls for arg in call.args
        if isinstance(arg, ast.Constant)
    ]
    assert arguments == [False], (
        f"setQuitOnLastWindowClosed вызван не с False, а с {arguments}"
    )


def test_shutdown_closes_the_database_and_cancels_the_run() -> None:
    """Порядок остановки — часть решения 0005, а не мелочь уборки.

    Сначала снимается прогон, потом закрывается база. В обратном порядке
    незаконченный прогон обращается к уже закрытой базе — и завершение
    программы падает исключением из потока данных.

    Проверяется **порядок**, а не наличие двух подстрок: перестановка строк
    местами прежнюю проверку не роняла, а докстринг обещал именно порядок.
    """
    tree = ast.parse((APP / "main.py").read_text(encoding="utf-8"))
    calls = [name for statement in _shutdown_body(tree) for name in _calls(statement)]
    assert "port.aclose" in calls, f"прогон не снимается при выходе: {calls}"
    assert "worker.close" in calls, f"поток данных не закрывается: {calls}"
    assert calls.index("port.aclose") < calls.index("worker.close"), (
        "база закрывается раньше, чем снят прогон: незаконченный прогон "
        f"обратится к закрытой базе. Порядок вызовов: {calls}"
    )


def test_shutdown_takes_the_live_stream_down_before_the_run_and_the_database() -> None:
    """Поток котировок пишет в базу — значит снимается раньше прогона и базы.

    Ворота `/validate` 04.09.2026: `await _close_live(live)` был невидим
    прежней проверке, и заявленный порядок «поток → прогон → база» не стерёг
    никто. Здесь проверяется порядок, то, что `_close_live` ждут (`await`:
    незаждавшаяся корутина не исполнится), и то, что она действительно
    закрывает звено, а не обещает.
    """
    tree = ast.parse((APP / "main.py").read_text(encoding="utf-8"))
    body = _shutdown_body(tree)
    calls = [name for statement in body for name in _calls(statement)]
    assert "_close_live" in calls, f"живой поток не снимается при выходе: {calls}"
    assert calls.index("_close_live") < calls.index("port.aclose") < calls.index("worker.close"), (
        "поток котировок снимается не первым: запись пойдёт в снятый прогон "
        f"или в закрывающуюся базу. Порядок вызовов: {calls}"
    )
    awaited = [name for statement in body for name in _awaited(statement)]
    assert "_close_live" in awaited, "`_close_live` вызвана без `await` — корутина не исполнится"
    assert "live.aclose" in _calls(_function(tree, "_close_live")), (
        "`_close_live` не закрывает звено: обещание в докстринге без действия"
    )
