"""Границы слоя `ui/`, нарушение которых — дефект уровня архитектуры.

Три правила, каждое из которых ломается одной строкой и не падает при этом:

1. **В окне нет торговой логики.** Проверить это тестом полностью нельзя —
   «логика» не имеет синтаксиса. Но у неё есть надёжные следы: расчётные
   библиотеки. Числа приходят в окно готовыми (`engine/`, `backtest/`), и
   `pandas` в `ui/` означает, что кто-то начал считать здесь. Две скользящие
   средние — на графике и в стратегии — расходятся молча.
2. **Сети в окне нет.** Сетевой вызов в слоте замораживает интерфейс, и обрыв
   связи выглядит как зависшая программа (ARCHITECTURE.md §5).
3. **График изолирован.** Имя библиотеки отрисовки не встречается за пределами
   `ui/chart/`, иначе замена отрисовки по ТЗ §6 перестаёт быть заменой одного
   блока (ARCHITECTURE.md §4).

Проверка статическая: она ловит время, когда правило нарушено, а не когда
это выстрелит у владельца счёта.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import ast
import pathlib

import pytest

UI = pathlib.Path(__file__).resolve().parent.parent / "ui"
SOURCES = sorted(path for path in UI.rglob("*.py") if "__pycache__" not in path.parts)

# Сеть в слое окна не появляется ни в каком виде.
#
# ⚠️ Здесь стоят **имена модулей**, а не русские слова. С 31.08.2026 по
# 05.09.2026 в списке лежало «запросы» — след текстовой замены, прошедшей
# по коду вместе с текстами для человека (`D-000`). Модуля с таким именем
# не существует, а `requests` из списка выпал: `import requests` в `ui/`
# проходил молча все эти дни. Что список состоит из имён, а не из переводов,
# стережёт `test_the_forbidden_lists_name_modules_not_russian_words`.
#
# Чего здесь намеренно нет: `select`/`selectors` — блокируют поток, но сами
# по себе не сеть, и в окне ловятся проверкой на ожидание.
NETWORK = {
    # клиенты HTTP: свои и чужие
    "requests", "httpx", "aiohttp", "urllib", "urllib3", "http",
    # сокеты и то, что поверх них
    "socket", "socketserver", "ssl",
    # `websockets` и `websocket-client` — разные пакеты с разными именами
    "websockets", "websocket",
    # почта, файлы, вызов процедур
    "ftplib", "smtplib", "poplib", "imaplib", "telnetlib", "xmlrpc",
    # сеть самого Qt. Имя записано целиком: у `from PySide6.QtNetwork import …`
    # корень — `PySide6`, тот же, что у кнопок и надписей, и по корню сеть
    # от виджета не отличить.
    "PySide6.QtNetwork", "PySide6.QtWebSockets", "PySide6.QtNetworkAuth",
}

# Расчётные библиотеки — след того, что окно начало считать само.
CALCULATION = {"pandas", "numpy", "scipy", "statistics", "ta", "talib"}

# Имена библиотек отрисовки. Живут только в `ui/chart/`.
#
# ⚠️ С 09.09.2026 ни одного из них в дереве нет: веб-график удалён (решение
# 0056), а отрисовка на Qt не библиотека — она рисует `QPainter`'ом, который
# и так везде. Список от этого не устарел, а сменил роль: он **запрещает
# будущее**, а не описывает настоящее. Первый же `import pyqtgraph` в окне
# настроек или в журнале обязан упасть здесь, а не выясниться через месяц.
# Проверка на то, что список ещё что-то ловит, стоит отдельно и работает
# на подсунутом файле, а не на дереве: `test_isolation_check_is_not_blind`.
RENDERERS = ("QtWebEngine", "pyqtgraph", "LightweightCharts", "lightweight-charts", "QtCharts")


def _imports(tree: ast.AST) -> set[str]:
    """Имена модулей, которые файл втягивает: и корень, и полное имя.

    Корня хватает для `import socket` и для `from urllib.request import urlopen`.
    Полное имя нужно из-за Qt: сеть у него лежит в `PySide6.QtNetwork`, а корень
    там тот же `PySide6`, что у кнопок и надписей. По корню сеть Qt от виджета
    не отличить, а запретить корень нельзя — на нём стоит всё окно.

    `ast.walk` обходит и тело функций, поэтому отложенный импорт внутри метода
    (обычный приём для тяжёлых модулей) виден так же, как импорт в шапке.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
            names.add(node.module.split(".")[0])
    return names


def _offenders(parsed: list[tuple[pathlib.Path, ast.AST, str]], forbidden: set[str]) -> list[str]:
    """Файлы, втянувшие что-то из списка запретов, — с именами найденного."""
    return [
        f"{path.name}: {sorted(_imports(tree) & forbidden)}"
        for path, tree, _ in parsed
        if _imports(tree) & forbidden
    ]


@pytest.fixture(scope="module")
def parsed() -> list[tuple[pathlib.Path, ast.AST, str]]:
    result = []
    for path in SOURCES:
        text = path.read_text(encoding="utf-8")
        result.append((path, ast.parse(text, filename=str(path)), text))
    assert result, "в ui/ не разобрано ни одного файла — проверка вакуумна"
    return result


def test_ui_never_touches_the_network(parsed) -> None:
    guilty = _offenders(parsed, NETWORK)
    assert not guilty, (
        "в слое окна появилась сеть — обрыв связи заморозит интерфейс:\n  "
        + "\n  ".join(guilty)
    )


@pytest.mark.parametrize(
    ("title", "forbidden"),
    [("NETWORK", NETWORK), ("CALCULATION", CALCULATION)],
)
def test_the_forbidden_lists_name_modules_not_russian_words(
    title: str, forbidden: set[str],
) -> None:
    """В списке запретов стоят имена модулей, а не переведённые на русский слова.

    Ровно тот дефект, ради которого проверка написана (`D-000`): в наборе
    `NETWORK` пять дней лежало «запросы» вместо `requests`. Список сверяется
    с именами из `import`, поэтому русское слово в нём не совпадает ни с чем
    никогда — проверка остаётся зелёной при снятой защите и выглядит рабочей.

    Правило проекта 6 (PEP 672): имя пишется латиницей всегда. Здесь оно
    ещё и обязано быть именем модуля, то есть годиться после слова `import`.
    """
    broken = sorted(
        item for item in forbidden
        if not item.isascii() or not all(part.isidentifier() for part in item.split("."))
    )
    assert not broken, (
        f"в списке {title} не имена модулей, а слова: {broken}. Совпасть "
        "с настоящим импортом они не могут, и проверка ничего не проверяет"
    )
    assert forbidden, f"список {title} пуст — проверка стала вакуумной"


def _planted(source: str) -> list[tuple[pathlib.Path, ast.AST, str]]:
    """Подсунутый файл в том же виде, в каком проверка получает настоящие."""
    return [(pathlib.Path("ui/planted_probe.py"), ast.parse(source), source)]


def test_the_network_guard_catches_an_import_that_was_planted_for_it() -> None:
    """Канарейка: проверка сети ловит подсунутый ей импорт — все пять видов.

    Без неё опечатка в наборе делает предыдущую проверку вечно зелёной,
    и заметить это можно только тем, что сеть однажды окажется в окне.

    Проверяется **тот же путь**, которым идут настоящие файлы: `_offenders`
    целиком, а не одна `_imports` внутри него. Замер мутацией 05.09.2026:
    канарейка, звавшая `_imports` напрямую, оставалась зелёной при подмене
    тела `_offenders` на `return []` — то есть при полностью снятой защите.

    Проверка статическая, поэтому подсунутые модули не обязаны быть
    установлены: разбирается текст, а не выполняется импорт.
    """
    planted = {
        "прямой импорт": "import requests\n",
        "импорт с псевдонимом": "import httpx as client\n",
        "часть модуля": "from urllib.request import urlopen\n",
        "сеть самого Qt": "from PySide6.QtNetwork import QNetworkAccessManager\n",
        "отложенный внутри метода": "def load(self):\n    import socket\n    return socket\n",
    }
    missed = [
        title for title, source in planted.items()
        if not _offenders(_planted(source), NETWORK)
    ]
    assert not missed, f"проверка сети не замечает: {missed}"

    # И обратно: обычные импорты окна она не трогает. Проверка, ловящая всё,
    # была бы выключена в первый же день.
    clean = (
        "from PySide6.QtWidgets import QLabel\n"
        "from PySide6.QtWebEngineWidgets import QWebEngineView\n"
        "from ui.models import Candle\n"
    )
    assert not _offenders(_planted(clean), NETWORK), "проверка сети ловит обычный виджет окна"


def test_the_calculation_guard_catches_a_library_that_was_planted_for_it() -> None:
    """То же для расчётных библиотек: проверка ловит подсунутый `pandas`.

    Обе проверки ходят через один `_offenders`, и вакуумной становятся обе
    сразу. Здесь это названо отдельно, чтобы падение указало на список,
    а не на общий путь.
    """
    assert _offenders(_planted("import pandas as pd\n"), CALCULATION), (
        "расчётная библиотека в окне проходит мимо проверки"
    )
    assert not _offenders(_planted("from ui.models import Candle\n"), CALCULATION), (
        "проверка расчётов ловит обычный импорт окна"
    )


def test_ui_does_not_calculate(parsed) -> None:
    guilty = _offenders(parsed, CALCULATION)
    assert not guilty, (
        "в слое окна появилась расчётная библиотека. Числа приходят готовыми: "
        "средняя, прибыль, просадка и проскальзывание считаются в engine/ "
        "и backtest/, иначе на экране будет вторая, ничем не подтверждённая "
        "версия стратегии:\n  " + "\n  ".join(guilty)
    )


def test_ui_does_not_sleep(parsed) -> None:
    """`sleep` в потоке интерфейса — это замерший экран, а не пауза."""
    guilty = [
        path.name for path, _, text in parsed
        if "time.sleep(" in text or "QThread.sleep(" in text or "QThread.msleep(" in text
    ]
    assert not guilty, "ожидание в потоке интерфейса подвешивает окно: " + ", ".join(guilty)


def _named_renderers(sources: list[tuple[pathlib.Path, ast.AST, str]]) -> list[str]:
    """Файлы вне `ui/chart/`, в которых названа библиотека отрисовки.

    Вынесено функцией, чтобы канарейка ниже била **в этот же разбор**,
    а не в свою копию: проверка и её проверка, разойдясь, обе останутся
    зелёными, и узнать об этом будет не по чему.
    """
    guilty = []
    for path, _, text in sources:
        if path.parent.name == "chart":
            continue
        found = [name for name in RENDERERS if name in text]
        if found:
            guilty.append(f"{path}: {found}")
    return guilty


def test_chart_library_is_named_only_inside_ui_chart(parsed) -> None:
    """Замена отрисовщика обязана стоить один блок, а не всю программу."""
    named = [(path.relative_to(UI.parent), tree, text) for path, tree, text in parsed]
    guilty = _named_renderers(named)
    assert not guilty, (
        "имя библиотеки отрисовки вышло за пределы ui/chart/ — замена отрисовки "
        "по ТЗ §6 перестала быть заменой одного блока:\n  " + "\n  ".join(guilty)
    )


def test_isolation_check_is_not_blind() -> None:
    """Канарейка: сама проверка изоляции что-то да ловит.

    Без неё опечатка в списке имён превратила бы предыдущий тест в вечно
    зелёный — ровно тот случай, когда предохранитель есть, а защиты нет.

    ⚠️ Проверяется на **подсунутом** файле, а не на дереве. До 09.09.2026
    канарейка искала имя отрисовщика внутри `ui/chart/` и зеленела оттого,
    что там лежал веб-график. Веб-график удалён (решение 0056), чужих
    библиотек отрисовки в дереве не осталось ни одной — и канарейка в прежнем
    виде упала бы, хотя запрет исправен. Она проверяла наличие нарушителя,
    а должна была проверять зоркость проверки.
    """
    caught = _named_renderers(_planted("import pyqtgraph as pg\n"))
    assert caught, (
        "подсунутый в ui/ импорт отрисовщика прошёл мимо проверки изоляции: "
        "список RENDERERS или сам разбор перестали работать"
    )

    inside = [(pathlib.Path("ui/chart/planted_probe.py"), ast.parse(""), "import pyqtgraph\n")]
    assert not _named_renderers(inside), (
        "проверка ловит имя отрисовщика внутри ui/chart/ — там ему и место"
    )

    clean = _named_renderers(_planted("from PySide6.QtWidgets import QLabel\n"))
    assert not clean, "проверка изоляции ловит обычный виджет окна"


def test_every_code_directory_in_ui_is_a_package() -> None:
    """Каталог без `__init__.py` не попадёт в собранную поставку."""
    orphans = []
    for directory in UI.rglob("*"):
        if not directory.is_dir() or directory.name == "__pycache__":
            continue
        if any(item.suffix == ".py" for item in directory.iterdir()):
            if not (directory / "__init__.py").is_file():
                orphans.append(str(directory.relative_to(UI.parent)))
    assert not orphans, "каталоги с кодом без __init__.py: " + ", ".join(orphans)


def test_chart_factory_reports_why_it_chose_what_it_chose() -> None:
    """Выбор отрисовщика обязан быть видимым, а не молчаливым.

    ⚠️ Отрисовщик с 09.09.2026 один (решение 0056), и «>= 2» здесь стояло
    ровно до этого дня. Проверка осталась не ради счёта, а ради двух вещей,
    которые от числа не зависят: у отрисовщика есть **человеческое имя**
    (оно уезжает в подпись окна, в «О программе» и в журнал при старте —
    с него начинается разбор жалобы «график пустой»), и отказ, если он
    случился, назван **словами**, а не пустой строкой.
    """
    from ui.chart import available_surfaces

    surfaces = available_surfaces()
    assert surfaces, "фабрика не знает ни одного отрисовщика — графика не будет вовсе"
    assert any(ok for _, ok, _ in surfaces), (
        "ни один отрисовщик не доступен: окно откроется без графика"
    )
    for name, ok, reason in surfaces:
        assert name, "у отрисовщика нет человеческого имени"
        assert name != "неизвестный отрисовщик", (
            "отрисовщик не назвал себя — в журнале и в окне будет умолчание "
            "из ChartSurface.name"
        )
        if not ok:
            assert len(reason) > 20, f"отказ {name} без внятной причины: {reason!r}"


def test_every_port_signal_is_connected(qapp) -> None:
    """Ни один сигнал порта не уходит в пустоту.

    Сигнал, который окно объявило и забыло подключить, — это не падение,
    а молчание: движок отправляет данные, на экране их нет, и разбор начинается
    с подозрения на движок. Проверка перечисляет сигналы по мета-объекту Qt,
    поэтому новый сигнал попадает под неё сам, без правки теста.
    """
    from PySide6.QtCore import QMetaMethod

    from helpers import RecordingPort
    from market import redact
    from ui.main_window import MainWindow
    from ui.ports import TerminalPort

    port = RecordingPort()
    window = MainWindow(port=port, sanitize=redact)
    window._timer.stop()
    try:
        meta = TerminalPort.staticMetaObject
        forgotten = []
        for index in range(meta.methodOffset(), meta.methodCount()):
            method = meta.method(index)
            if method.methodType() != QMetaMethod.MethodType.Signal:
                continue
            signature = method.methodSignature().data().decode()
            if port.receivers("2" + signature) == 0:
                forgotten.append(signature)
        assert not forgotten, (
            "сигналы порта никуда не подключены — данные движка не дойдут "
            "до экрана: " + ", ".join(forgotten)
        )
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_market_candle_cannot_pass_into_the_window_silently() -> None:
    """Свеча слоя данных не подходит окну по имени поля — и это защита.

    В `market/candles.py` поле `time` — начало свечи, и там же есть свойство
    `close_time`. В окне поле называется `opens_at` — имени, которого у свечи
    слоя данных нет ни одного.

    Зачем: формы классов совпадают полностью, соответствие в проекте
    структурное (ARCHITECTURE.md §2), проверок типа нет ни в одном
    отрисовщике. Пока имя совпадало, свеча слоя данных проходила в график
    утиной типизацией — ничего не падало, а график вместе с метками сделок
    вставал на один бар в сторону. Заметить можно было только сравнением
    с чужим терминалом.

    ⚠️ Значение у полей теперь одно и то же — начало интервала (B-008,
    04.09.2026), и именно поэтому имя обязано остаться разным. Назвать поле
    окна `time` — значит впустить сюда свечу слоя данных вместе с её
    `timeframe`, `filled_minutes` и признаком незакрытости, о которых окно
    ничего не знает, и снять единственную защиту границы.

    Попытка назвать поле `close_time` защиты тоже не давала: такое свойство
    у свечи слоя данных уже есть.
    """
    from market.candles import Candle as MarketCandle
    from market.candles import Timeframe
    from ui.models import Candle as WindowCandle

    window_field = "opens_at"
    assert window_field in WindowCandle.__dataclass_fields__, (
        "поле окна переименовали — проверь, что новое имя отсутствует "
        "у market.candles.Candle, иначе защита исчезнет"
    )

    moment = datetime(2026, 6, 19, 10, 5, tzinfo=timezone(timedelta(hours=3)))
    stranger = MarketCandle(
        time=moment, open=1.0, high=2.0, low=0.5, close=1.5,
        volume=10.0, timeframe=Timeframe(5),
    )

    # Настоящая проверка: обращение к полю окна на свече слоя данных
    # обязано упасть. Заведомо несуществующее имя здесь ничего не докажет —
    # оно падает на любом объекте.
    with pytest.raises(AttributeError):
        getattr(stranger, window_field)

    # И обратно: у свечи окна нет полей слоя данных, поэтому подмена
    # в другую сторону тоже не пройдёт молча.
    ours = WindowCandle(opens_at=moment, open=1.0, high=2.0, low=0.5, close=1.5, volume=10.0)
    for foreign in ("time", "timeframe", "filled_minutes", "unsettled"):
        assert not hasattr(ours, foreign), (
            f"у свечи окна появилось поле слоя данных {foreign} — "
            "структурная подмена снова стала возможной"
        )
