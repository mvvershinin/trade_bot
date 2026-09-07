"""Сторожа самой обвязки: сеть закрыта, экрана нет, лог уехал, списки не протухли.

Обвязка (`tests/conftest.py`) — это код, который никто не проверяет, потому
что он «просто настройка». Между тем от него зависит смысл всего прогона:
сторож сети, который не ловит, оставляет тесты, зависящие от биржи; подмена
папки лога, которая не сработала, возвращает мусор в файл владельца счёта.
Оба сторожа поэтому проверяются здесь поведением, а не чтением.

⚠️ Проверка сторожа в собственном кадре — законна **именно здесь** и только
здесь. Кадр этих тестов ничем не отличается от кадра любого другого теста:
фикстуры `no_internet` и `logs_go_to_a_temporary_folder` объявлены `autouse`,
то есть ставятся всем одинаково, и ни один тест ниже не готовит себе условие,
которое потом наблюдает. Ложной была бы обратная схема — тест, который сам
ставит подмену и сам же её видит; такой ничего не доказывает про соседей.

Чего эти проверки не доказывают, названо прямо: сторож сети живёт в подмене
`socket` **этого** процесса и до отдельного процесса не доезжает.
"""

from __future__ import annotations

import ast
import logging
import os
import pathlib
import socket
import subprocess
import sys
import threading

import pytest
from conftest import (
    INTERNET_ALLOWED,
    SCREENLESS_PLATFORMS,
    USERDATA_BOUND_IN,
    OpenedAWindow,
    WentOutside,
    named_in,
)

#: Куда ходить нельзя. Адрес настоящий и намеренно тот же, что увёл прогон
#: в интернет 05.09.2026 (`D-020`): сторож проверяется на своём случае.
OUTSIDE = ("iss.moex.com", 443)

REPO = pathlib.Path(__file__).resolve().parent.parent


# --------------------------------------------------- сеть закрыта (`D-020`)

def test_an_ordinary_test_cannot_open_a_socket_to_the_exchange() -> None:
    """Обычный тест, полезший на биржу, падает — а не скачивает данные.

    Это тот самый случай: `MarketWorker` без своего клиента собирает
    `IssClient()` с настоящим транспортом и молча качает минутки, пока
    прогон зелёный.
    """
    with pytest.raises(WentOutside, match="пошёл в настоящую сеть"):
        socket.create_connection(OUTSIDE, timeout=1)


def test_the_refusal_says_where_to_ask_for_permission() -> None:
    """Отказ объясняет, что делать, а не только что нельзя.

    Сторож, который отказывает без адреса, где просить, чинят обходом:
    следующий человек снимет фикстуру целиком.
    """
    with pytest.raises(WentOutside) as fallen:
        socket.getaddrinfo("iss.moex.com", 443)
    assert "INTERNET_ALLOWED" in str(fallen.value), (
        f"отказ не говорит, где выписывается разрешение: {fallen.value}"
    )


def test_resolving_a_name_falls_before_the_connection() -> None:
    """Разбор имени закрыт отдельно от соединения.

    Без этого отказ приходил бы из чужого кода — «сервер не отвечает», —
    и по нему не догадаться, что тест вообще куда-то шёл.
    """
    with pytest.raises(WentOutside, match="getaddrinfo"):
        socket.getaddrinfo("iss.moex.com", 443)


def test_the_guard_holds_in_a_worker_thread() -> None:
    """Поток работника данных закрыт тем же сторожем, что и главный.

    ⚠️ Не украшение: строки, из-за которых заведён `D-020`, написаны потоком
    `market_0`, а не главным. Сторож, поставленный только на свой поток,
    пропустил бы ровно тот случай, ради которого поставлен.
    """
    fallen: list[BaseException] = []

    def go() -> None:
        try:
            socket.create_connection(OUTSIDE, timeout=1)
        except BaseException as error:  # noqa: BLE001 — нужен сам тип, любой
            fallen.append(error)

    worker = threading.Thread(target=go, name="market_0")
    worker.start()
    worker.join(timeout=10)
    assert fallen and isinstance(fallen[0], WentOutside), (
        f"из потока работника данных сеть осталась открытой: {fallen}"
    )


def test_the_library_that_actually_goes_to_the_exchange_is_blocked() -> None:
    """Закрыт `httpx` — та самая библиотека, которой ходит `market.iss`.

    Проверка не на `socket` вообще, а на настоящем пути: сторож на одной
    точке ниже по стеку обязан ловить всё, что через неё проходит.

    Отказ доходит до вызывающего **как есть**, не завёрнутый в `ConnectError`
    библиотеки: `httpx` переводит в свои типы наследников `Exception`,
    а `WentOutside` — `BaseException` намеренно (разбор — у самого типа).
    Заворачивание превратило бы поход в интернет в «сервер не ответил».
    """
    httpx = pytest.importorskip("httpx")
    with pytest.raises(WentOutside):
        httpx.Client(timeout=1).get("https://iss.moex.com/iss/index.json")


def test_a_local_server_is_not_the_internet() -> None:
    """Канарейка: сторож закрывает интернет, а не сокеты вообще.

    Без неё «сеть закрыта» прошло бы и у фикстуры, которая ломает всё
    подряд, — а на локальном сокете стоят проверки разбора кадров потока
    котировок (`tests/test_broker_stream.py`). Тест, который никогда
    не запускается, ничего не стережёт.
    """
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        client = socket.create_connection(server.getsockname(), timeout=5)
        client.close()
    finally:
        server.close()


def test_nobody_is_allowed_outside_in_an_ordinary_run() -> None:
    """Список разрешённых пуст, пока его не открыли руками.

    Утверждение о прогоне, а не о словаре: «в интернет не ходит ни один
    тест» проверяемо только так. Открывается список переменной окружения
    `TERMINAL_ISS_NETWORK=1` и ровно на один файл.
    """
    import os

    if os.environ.get("TERMINAL_ISS_NETWORK") == "1":
        pytest.skip("сеть открыта руками под наполнение базы для сверки")
    assert INTERNET_ALLOWED == {}, (
        "кому-то выписано разрешение ходить в интернет молча: "
        f"{sorted(INTERNET_ALLOWED)}"
    )


def test_a_permission_for_one_file_does_not_cover_its_longer_named_neighbour() -> None:
    """Разрешение `tests/test_app_main.py` не накрывает `..._extra.py`.

    Находка ревью 06.09.2026. Сверка шла голым `nodeid.startswith(name)`,
    то есть разрешение растекалось на любой файл, чьё имя начинается так же,
    и на любой тест, чьё имя начинается так же (`::test_load` разрешал
    и `::test_load_twice`). Молча и в ту сторону, в которую ошибаться
    нельзя: список открывает доступ, а не закрывает.
    """
    allowed = {"tests/test_app_main.py": "зачем-то"}
    assert named_in("tests/test_app_main.py::test_x", allowed)
    assert not named_in("tests/test_app_main_extra.py::test_x", allowed), (
        "разрешение растеклось на соседний файл с более длинным именем"
    )

    one_test = {"tests/x.py::test_load": "зачем-то"}
    assert named_in("tests/x.py::test_load", one_test)
    assert not named_in("tests/x.py::test_load_twice", one_test), (
        "разрешение растеклось на тест с более длинным именем"
    )


def test_a_permission_for_one_test_covers_its_parametrised_cases() -> None:
    """`::test_y` разрешает и `::test_y[случай]`, иначе разрешение бесполезно.

    Обратная сторона той же правки: сравнение только на равенство закрыло бы
    доступ параметризованному тесту, ради которого разрешение и выдавали.
    """
    allowed = {"tests/x.py::test_y": "зачем-то"}
    assert named_in("tests/x.py::test_y[первый случай]", allowed), (
        "разрешение не покрыло параметризованный случай того же теста"
    )


def test_no_permission_list_is_checked_by_a_bare_prefix() -> None:
    """В обвязке не осталось сверки списка разрешений по началу строки.

    Проверка структурная и потому не заменяет две выше: те стерегут
    поведение `named_in`, эта — что сверяют именно им. Вернуть
    `nodeid.startswith(...)` обратно в фикстуру можно было бы молча,
    и обе проверки поведения остались бы зелёными.

    Мутация: заменить `named_in(nodeid, INTERNET_ALLOWED)` на
    `any(nodeid.startswith(name) for name in INTERNET_ALLOWED)` — тест
    обязан покраснеть.
    """
    source = (REPO / "tests" / "conftest.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    found = []
    for holder in ast.walk(tree):
        # Тело самого `named_in` — единственное законное место: сравнение
        # по границе там и написано, через `startswith` с разделителем.
        if not isinstance(holder, ast.FunctionDef) or holder.name == "named_in":
            continue
        for node in ast.walk(holder):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute) or node.func.attr != "startswith":
                continue
            if ast.unparse(node.func.value).endswith("nodeid"):
                found.append(
                    f"{holder.name}, строка {node.lineno}: {ast.unparse(node)}"
                )
    assert not found, (
        "разрешение сверяется по началу строки: оно накроет соседний файл "
        "с более длинным именем. Сверять надо `named_in`:\n  "
        + "\n  ".join(found)
    )


def test_every_permission_names_a_test_that_exists() -> None:
    """Протухшая строка списка — разрешение, выданное неизвестно кому.

    Файл переименовали или тест удалили, а строка осталась: она ничего
    не разрешает и создаёт впечатление, что случай разобран.
    """
    for name, why in INTERNET_ALLOWED.items():
        path, _, test = name.partition("::")
        assert (REPO / path).is_file(), f"разрешение выдано несуществующему файлу: {name}"
        assert why.strip(), f"разрешение без причины: {name}"
        if test:
            body = (REPO / path).read_text(encoding="utf-8")
            assert f"def {test}(" in body, f"разрешение выдано несуществующему тесту: {name}"


# ----------------------------------------- прогон без экрана (`B-033`)

#: Так выглядит окружение машины с монитором: платформа не названа, экран есть.
#: Ровно его отдавали ребёнку три вспомогательные функции до 06.09.2026.
WITH_A_SCREEN = {"PATH": os.environ.get("PATH", ""), "DISPLAY": ":0"}


def test_a_child_process_is_not_handed_a_screen() -> None:
    """Обычный тест не может запустить копию программы с настоящим экраном.

    Тот самый случай: `environment.pop("QT_QPA_PLATFORM", None)` со словами
    «режим снимка ставит его сам». Ребёнок стартовал с `DISPLAY` владельца
    счёта и рисовал окно ему на стол, а на докерхосте падал бы с
    `could not connect to display`.

    ⚠️ Отказ обязан прийти **до** запуска: сторож, замечающий окно после
    того, как оно открылось, задачу не решает.
    """
    with pytest.raises(OpenedAWindow, match="окружение с экраном"):
        subprocess.run(
            [sys.executable, "-c", "pass"], env=dict(WITH_A_SCREEN), check=False
        )


def test_the_screen_refusal_says_what_to_do_and_says_it_truthfully() -> None:
    """Отказ объясняет, что делать, и объяснение обязано быть выполнимым.

    ⚠️ Обе половины проверяются, и вторая — находка ревью 06.09.2026.
    Прежний текст советовал вписать тест в список `SCREEN_ALLOWED`,
    а совет вёл в тупик: `DISPLAY` удаляется из окружения безусловно,
    и вписавшийся экрана всё равно не получал. Отказ с негодным советом
    хуже молчащего — по нему чинят не то место (правило 13 `CLAUDE.md`).

    Мутация: вернуть в текст отказа обещание списка — тест обязан
    покраснеть.
    """
    with pytest.raises(OpenedAWindow) as fallen:
        subprocess.run(
            [sys.executable, "-c", "pass"], env=dict(WITH_A_SCREEN), check=False
        )
    said = str(fallen.value)
    assert "tests/conftest.py" in said, (
        "в отказе не сказано, где это чинится — сторож снесут вместо правки"
    )
    assert "как есть" in said, (
        "в отказе не сказано, что делать вместо: окружение уже без экрана "
        f"и отдавать его надо как есть. Сказано: {said}"
    )
    assert "SCREEN_ALLOWED" not in said, (
        "отказ снова обещает список разрешений. Списка нет, и работать он "
        "не может: DISPLAY убран из окружения на весь прогон, возвращать "
        "его некому — совет ведёт в тупик"
    )


def test_the_guard_also_watches_popen_called_directly() -> None:
    """Сторож стоит на `Popen`, а не на `run`.

    Половина запусков в дереве идёт через `subprocess.Popen` напрямую
    (`tests/test_app_single_instance.py::_launch`). Сторож на одном `run`
    пропустил бы её целиком.
    """
    with pytest.raises(OpenedAWindow, match="окружение с экраном"):
        subprocess.Popen([sys.executable, "-c", "pass"], env=dict(WITH_A_SCREEN))


def test_an_env_given_positionally_is_checked_too() -> None:
    """`env`, переданный без имени, сторож тоже видит.

    Так его не передаёт никто, но сторож, смотрящий только именованные
    аргументы, обходится случайно — а не осознанно.
    """
    with pytest.raises(OpenedAWindow, match="окружение с экраном"):
        subprocess.Popen(
            [sys.executable, "-c", "pass"],
            -1, None, None, None, None, None, True, False, None,
            dict(WITH_A_SCREEN),
        )


def test_an_ordinary_child_still_starts() -> None:
    """Сторож, отказывающий всем, не стережёт ничего — он просто ломает прогон.

    Обычный запуск копии программы обязан проходить: окружение прогона
    уже без экрана, и отдавать его ребёнку — правильный способ.
    """
    done = subprocess.run(
        [sys.executable, "-c", "print('жив')"],
        capture_output=True, text=True, check=False,
    )
    assert done.stdout.strip() == "жив", done.stderr


def test_a_deliberately_broken_plugin_passes_in_a_screenless_environment() -> None:
    """Негодное имя плагина в безэкранном окружении проходит и запускает ребёнка.

    На этом приёме стоят три проверки: отказ второй копии до касания Qt
    (`B-028`), ключ `--runs` и снимок экрана, который обязан **присвоить**
    себе `offscreen` поверх чужого значения. Сторож, отвергающий любую
    незнакомую платформу, снёс бы все три и заставил бы обходить себя.
    """
    environment = dict(os.environ)
    environment["QT_QPA_PLATFORM"] = "there-is-no-such-plugin"
    done = subprocess.run(
        [sys.executable, "-c", "print('жив')"], env=environment,
        capture_output=True, text=True, check=False,
    )
    assert done.stdout.strip() == "жив", done.stderr


def test_a_broken_plugin_does_not_excuse_a_live_screen() -> None:
    """А вместе с живым `DISPLAY` тот же плагин отвергается.

    Сторож смотрит на экран, а не на имя платформы: незнакомое имя —
    не пропуск. Иначе обойти сторожа можно было бы опечаткой.
    """
    environment = {**WITH_A_SCREEN, "QT_QPA_PLATFORM": "there-is-no-such-plugin"}
    with pytest.raises(OpenedAWindow, match="окружение с экраном"):
        subprocess.run(
            [sys.executable, "-c", "pass"], env=environment, check=False
        )


def test_the_run_itself_has_no_screen_in_its_environment() -> None:
    """У прогона нет `DISPLAY` — то есть стол ведёт себя как докерхост.

    Это и есть ответ на вопрос владельца счёта «как я при CI/CD буду
    запускать, там будет докерхост и там не будет монитора»: если проверка
    зелёная здесь, окружение прогона от контейнерного не отличается.
    """
    for name in ("DISPLAY", "WAYLAND_DISPLAY"):
        assert name not in os.environ, (
            f"{name}={os.environ[name]!r} остался в окружении прогона: "
            "прогон на этой машине и прогон в контейнере — разные прогоны"
        )


def test_qt_in_this_process_draws_into_memory(qapp) -> None:
    """Платформа поднятого Qt — безэкранная. Спрашивается сам Qt, не переменная.

    Переменную можно откатить, платформу — нет: у `QApplication` она
    выбирается один раз и живёт до конца процесса. Проверка по переменной
    зеленела бы при открытых окнах.

    Приложение берётся у фикстуры `qapp` — той же, что у всех тестов `ui/`:
    проверять надо ровно то, на чём они рисуют.
    """
    assert qapp.platformName() in SCREENLESS_PLATFORMS, (
        f"Qt поднят на {qapp.platformName()!r}: тесты рисуют на настоящем экране"
    )


def test_the_conftest_keeps_no_list_of_who_may_have_a_screen() -> None:
    """Списка «кому можно экран» в обвязке нет, и это проверяемо.

    Он был — пустой `SCREEN_ALLOWED`, — и сработать не мог: `DISPLAY`
    и `WAYLAND_DISPLAY` удаляются из окружения в шапке `tests/conftest.py`
    безусловно, вернуть их вписавшемуся некому. Пустой список, который
    не может сработать, хуже отсутствующего: он выглядит разобранным
    случаем и уводит следующего.

    Появится такой список снова — либо он умеет возвращать экран, либо
    его снова нет. Средний вариант этот тест и не пускает.
    """
    import conftest

    assert not hasattr(conftest, "SCREEN_ALLOWED"), (
        "в обвязке снова заведён список разрешений на экран. Он обязан "
        "уметь возвращать DISPLAY в окружение — иначе отказ, советующий "
        "в него вписаться, врёт человеку"
    )


def _drops_the_platform(node: ast.AST) -> bool:
    """Узел выбрасывает `QT_QPA_PLATFORM` из словаря: `.pop(...)` или `del`."""
    name = "QT_QPA_PLATFORM"
    if isinstance(node, ast.Call):
        called = node.func
        first = node.args[0] if node.args else None
        return (
            isinstance(called, ast.Attribute)
            and called.attr == "pop"
            and isinstance(first, ast.Constant)
            and first.value == name
        )
    if isinstance(node, ast.Delete):
        return any(
            isinstance(target, ast.Subscript)
            and isinstance(target.slice, ast.Constant)
            and target.slice.value == name
            for target in node.targets
        )
    return False


def test_no_helper_hands_a_child_an_environment_without_a_platform() -> None:
    """Ни одна вспомогательная функция не выкидывает платформу из окружения ребёнка.

    ⚠️ Проверка **по дереву разбора**, а не по поведению, и добавлена поверх
    сторожа осознанно. Сторож ловит запуск с экраном; `pop` платформы при уже
    убранном `DISPLAY` он пропустит — окна не будет. Но `pop` вернёт окна
    в тот день, когда `DISPLAY` из шапки `conftest` кто-нибудь уберёт, а до
    того выглядит безобидно. Именно так три таких строки и прожили в дереве
    (`B-033`).

    ⚠️ Разбор на токены, а не поиск подстроки, — правило проекта, и здесь оно
    поймано на себе: первая редакция этой проверки искала `"QT_QPA_PLATFORM"`
    и `.pop(` в тексте строки и упала **на собственном докстринге**, где эта
    строка приведена как пример.
    """
    guilty = []
    for source in sorted((REPO / "tests").glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        guilty += [
            f"{source.name}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, (ast.Call, ast.Delete)) and _drops_the_platform(node)
        ]
    assert not guilty, (
        "платформа Qt выкидывается из окружения — ребёнок выберет экранную "
        "сам, и на машине с монитором это окно на столе владельца счёта: "
        + ", ".join(guilty)
    )


# ------------------------- технический лог мимо папки владельца счёта

def _owners_log() -> pathlib.Path:
    """Боевой файл лога — тот, который читает владелец счёта.

    Путь собирается здесь руками, а не через `app.logs`: именно `app.logs`
    и подменён, спросить его значит спросить подмену.
    """
    return REPO / "userdata" / "logs" / "terminal.log"


def test_a_run_of_the_tests_does_not_write_into_the_owners_log() -> None:
    """Строка, написанная в прогоне, в боевом логе не появляется.

    ⚠️ Проверка по последствию, а не по пути: сравнивается размер боевого
    файла до и после настоящей записи. Сверка одного лишь пути прошла бы
    и в том случае, когда лог заводится вторым обработчиком где-то ещё.

    Разбирая запуск 05.09.2026, владелец счёта выуживал две настоящие строки
    из ста семидесяти килобайт тестового мусора: тестовые записи вытесняют
    настоящие ротацией, и по логу нельзя разобрать обрыв связи.
    """
    from app.logs import setup_logging

    owners = _owners_log()
    before = owners.stat().st_size if owners.exists() else None

    report = setup_logging()
    assert report.working, f"лог не завёлся вовсе: {report.trouble}"
    logging.getLogger("app.port").warning("КАНАРЕЙКА-ПРОГОНА-ТЕСТОВ-58201")
    for handler in logging.getLogger().handlers:
        handler.flush()

    after = owners.stat().st_size if owners.exists() else None
    assert after == before, (
        f"прогон тестов дописал в боевой лог владельца счёта {owners}: "
        f"было {before} Б, стало {after} Б"
    )
    written = report.path.read_text(encoding="utf-8") if report.path else ""
    assert "КАНАРЕЙКА-ПРОГОНА-ТЕСТОВ-58201" in written, (
        "строка не дошла и до временного файла — проверять было нечего, "
        f"и совпадение размеров ничего не значит: {report.path}"
    )


def test_the_default_log_folder_is_temporary() -> None:
    """Умолчание `setup_logging()` в прогоне ведёт во временную папку."""
    from app.logs import default_log_dir

    chosen = default_log_dir()
    assert REPO not in chosen.parents, (
        f"умолчание лога осталось в рабочей копии: {chosen}"
    )


def test_every_module_that_took_the_name_is_in_the_table() -> None:
    """Таблица подмены полна: модуль, взявший `userdata_dir`, в ней назван.

    Тот же приём, что у `_first_that_opens` в `app/logs.py`: перечень —
    это данные, а полноту данных проверяют, а не помнят. Модуль, взявший
    имя себе и не попавший в таблицу, продолжит писать владельцу счёта,
    и заметят это в день, когда лог понадобится.

    Проверяется только `app/` — слой, который заводит лог и журналы
    (`ARCHITECTURE.md` §2). `market/` эту папку подменять нельзя:
    по ней идёт сверка с эталоном, см. таблицу в `tests/conftest.py`.

    ⚠️ Имя ищется **разбором на токены**, а не поиском подстроки. Рядом
    живёт `ensure_userdata_dir`, и `«userdata_dir» in body` считает его
    совпадением — сторож с таким поиском требовал бы вписать в таблицу
    модуль, который этого имени не брал.
    """
    took: set[str] = set()
    for source in sorted((REPO / "app").rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if any(alias.name == "userdata_dir" for alias in node.names):
                took.add(f"app.{source.stem}")
    forgotten = took - set(USERDATA_BOUND_IN)
    assert not forgotten, (
        f"модули взяли себе `userdata_dir`, а в таблице подмены их нет: "
        f"{sorted(forgotten)}. Их записи уйдут в папку владельца счёта"
    )


# ------------------------------------------ отложенные удаления Qt (`D-083`)

def test_process_events_alone_leaves_a_widget_that_asked_to_be_deleted_alive(
    qapp,
) -> None:
    """`deleteLater()` + `processEvents()` виджет не убивает — это и был `D-083`.

    Проверяется не Qt, а **причина**, по которой уборка в полутора десятках
    файлов тестов ничего не убирала: там написано ровно это сочетание,
    и выглядит оно исправно. `deleteLater()` кладёт событие в очередь,
    `processEvents()` очередь `DeferredDelete` не разбирает; вдобавок
    `deleteLater()` снимает владение с обёртки Python, поэтому виджет
    переживает и конец теста, и сборку мусора.

    Замер 06.09.2026: **117 035 живых виджетов** в пике последовательного
    прогона. Если эта проверка однажды позеленеет наоборот — Qt поменял
    поведение, и всю уборку надо перечитывать, а не подгонять число.
    """
    from helpers import carry_out_deferred_deletions, live_widget_count
    from PySide6.QtWidgets import QWidget

    was = live_widget_count()
    doomed = QWidget()
    doomed.deleteLater()
    qapp.processEvents()
    assert live_widget_count() == was + 1, (
        "виджет умер от `processEvents()` — значит Qt поменял правила "
        "доставки `DeferredDelete`, и уборка в обвязке описана неверно"
    )

    carry_out_deferred_deletions()
    assert live_widget_count() == was, (
        "прокрутка очереди `DeferredDelete` виджет не убила: помощник "
        "обвязки перестал делать то единственное, ради чего написан"
    )


def test_the_harness_carries_out_the_deletions_a_test_left_behind(
    qapp, request: pytest.FixtureRequest,
) -> None:
    """Уборка стоит **в обвязке** и отрабатывает после теста, а не «где-то есть».

    Сторож соседа выше: тот проверяет помощника, этот — что помощника
    действительно зовут после каждого теста. Снять `autouse`, переименовать
    фикстуру, выпотрошить её тело — соседний сторож останется зелёным,
    этот покраснеет.

    Как устроено. Тест оставляет за собой десять виджетов, попросивших
    удалиться, и вешает на прогон обёртку вокруг `pytest_runtest_teardown`.
    Обёртка получает управление **после** всех прочих обработчиков, то есть
    после разбора фикстур, — там и считает. Иначе никак: финализаторы идут
    в обратном порядке, фикстура из `conftest` создаётся раньше всех
    фикстур теста и потому убирает последней, уже за спиной у своего теста.

    ⚠️ Отказ приходит как ошибка на разборе (`ERROR at teardown`), а не как
    упавшая проверка. Прогон от этого красный так же.
    """
    from helpers import live_widget_count
    from PySide6.QtWidgets import QWidget

    expected = live_widget_count()

    class CountsWidgetsAfterTeardown:
        """Считает живые виджеты после того, как обвязка убрала за тестом."""

        @pytest.hookimpl(wrapper=True)
        def pytest_runtest_teardown(self, item, nextitem):
            result = yield
            if item.nodeid != request.node.nodeid:
                return result
            item.config.pluginmanager.unregister(self)
            left = live_widget_count()
            assert left == expected, (
                f"после теста осталось живых виджетов {left} против "
                f"{expected} до него: обвязка перестала исполнять "
                "отложенные удаления, и виджеты снова копятся (`D-083`)"
            )
            return result

    request.config.pluginmanager.register(CountsWidgetsAfterTeardown())

    for _ in range(10):
        QWidget().deleteLater()
    qapp.processEvents()
    assert live_widget_count() == expected + 10, (
        "виджеты не пережили `processEvents()` — тесту нечего оставлять "
        "за собой, и проверка на разборе стала бы вакуумной"
    )
