"""Технический лог: файл, права, ротация и молчащая консоль.

Заведено по `D-021`: владелец счёта запустил программу и получил в консоли
шесть предложений текста, написанного для человека, — потому что места
для логов в программе не было настроено ни одного, и записи уходили
в аварийный вывод Python.

Каждая проверка здесь стережёт **одно** поведение, и оно названо в имени
и первой строке докстринга. Проверки прогонные, а не по чтению кода:
файл создаётся по-настоящему, права снимаются `os.stat`, ротация
запускается настоящими записями, консоль читается из отдельного процесса.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import pathlib
import stat
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Iterator

import pytest

from app.logs import (
    BACKUPS,
    DIR_MODE,
    FILE_MODE,
    LOG_DIR_NAME,
    LOG_FILE_NAME,
    MAX_BYTES,
    SilentSink,
    TechnicalLog,
    close_logging,
    default_log_dir,
    setup_logging,
)
from broker.redaction import FOREIGN_LOGGERS, LOGGER_NAME, RedactingFilter, RedactingSink

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Приметная строка, которая секретом не является. Ею проверяется, что запись
#: до файла вообще доходит: без такой канарейки «токена в файле нет» зеленеет
#: и на пустом файле.
CANARY = "КАНАРЕЙКА-В-ЛОГЕ-0192837465"

WINDOWS = os.name == "nt"
POSIX_ONLY = pytest.mark.skipif(WINDOWS, reason="права POSIX; на Windows их ставит ACL")


@pytest.fixture(autouse=True)
def pristine_logging() -> Iterator[None]:
    """Дерево логгеров возвращается в исходное состояние после каждой проверки.

    `setup_logging` правит **корневой** логгер процесса и ставит стоки чистки
    на чужие ветки. Без уборки соседний файл тестов зеленел бы или краснел
    по порядку запуска, а не по коду.
    """
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    level_before = root.level
    watched = (LOGGER_NAME, *FOREIGN_LOGGERS)
    before = {
        name: (
            list(logging.getLogger(name).filters),
            list(logging.getLogger(name).handlers),
        )
        for name in watched
    }
    try:
        yield
    finally:
        close_logging()
        for handler in list(root.handlers):
            if handler not in handlers_before:
                root.removeHandler(handler)
        for handler in handlers_before:
            if handler not in root.handlers:
                root.addHandler(handler)
        root.setLevel(level_before)
        for name in watched:
            logger = logging.getLogger(name)
            filters_before, sinks_before = before[name]
            for item in list(logger.filters):
                if isinstance(item, RedactingFilter) and item not in filters_before:
                    logger.removeFilter(item)
            for sink in list(logger.handlers):
                if isinstance(sink, RedactingSink) and sink not in sinks_before:
                    logger.removeHandler(sink)


@pytest.fixture
def userdata(tmp_path: pathlib.Path) -> pathlib.Path:
    directory = tmp_path / "userdata"
    directory.mkdir()
    return directory


def _mode(path: pathlib.Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _flush() -> None:
    for handler in logging.getLogger().handlers:
        handler.flush()


# --------------------------------------------------------------- где лежит файл


def test_the_log_is_written_to_a_file_without_any_setting(userdata: pathlib.Path) -> None:
    """Без единой настройки лог пишется в файл рядом с базой свечей."""
    report = setup_logging(userdata=userdata)
    assert report.working, f"лога нет: {report.trouble}"
    assert report.path == userdata / LOG_DIR_NAME / LOG_FILE_NAME
    logging.getLogger("app.проба").warning(CANARY)
    _flush()
    assert CANARY in report.path.read_text(encoding="utf-8")


def test_the_file_exists_before_anything_is_logged(userdata: pathlib.Path) -> None:
    """Файл заводится сразу, а не при первой записи: его должно быть где искать.

    Уровень взят выше того, на котором пишется первая строка о самом логе:
    иначе проверка держалась бы на ней, а не на настройке обработчика,
    и «ленивое» открытие файла прошло бы молча. Замер мутацией 05.09.2026:
    с `level=INFO` подмена `delay=False` на `delay=True` не ловилась ничем.
    """
    report = setup_logging(userdata=userdata, level=logging.ERROR)
    assert report.path is not None
    assert report.path.is_file(), "файла нет — владельцу счёта нечего прислать"
    assert report.path.stat().st_size == 0, "на этом уровне писать было нечего"


def test_the_named_directory_wins_over_the_default(
    userdata: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """Папка, названная в настройках, побеждает умолчание."""
    chosen = tmp_path / "куда-сказали"
    report = setup_logging(chosen, userdata=userdata)
    assert report.path == chosen / LOG_FILE_NAME
    assert report.directory == chosen
    assert report.asked == chosen
    assert not report.trouble
    assert not default_log_dir(userdata).exists(), "умолчание завели зря"


def test_an_unusable_named_directory_falls_back_to_the_default_and_says_so(
    userdata: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """Негодная папка из настроек не оставляет программу без лога, и это сказано.

    Порядок проб — данные, а не ветвление: названная папка, потом умолчание.
    Молчаливое падение назад было бы хуже отказа: владелец счёта считал бы,
    что лог пишется туда, куда он указал.
    """
    obstacle = tmp_path / "это-файл-а-не-папка"
    obstacle.write_text("", encoding="utf-8")
    report = setup_logging(obstacle, userdata=userdata)
    assert report.path == default_log_dir(userdata) / LOG_FILE_NAME
    assert report.asked == obstacle
    assert str(obstacle) in report.trouble, f"о подмене папки не сказано: {report.trouble}"
    assert str(default_log_dir(userdata)) in report.trouble
    logging.getLogger("app.проба").warning(CANARY)
    _flush()
    assert CANARY in report.path.read_text(encoding="utf-8")


def test_when_no_directory_works_at_all_the_console_still_stays_silent(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Лога нет — консоль всё равно молчит, и об отсутствии сказано вслух.

    Без обработчика на корне Python включает `logging.lastResort`, и всё
    написанное летит в stderr. Отсутствие лога не имеет права превращаться
    в засорённый экран.
    """
    obstacle = tmp_path / "занято"
    obstacle.write_text("", encoding="utf-8")
    report = setup_logging(obstacle, userdata=obstacle)
    assert not report.working
    assert report.path is None
    assert "не удалось" in report.trouble
    assert any(isinstance(h, SilentSink) for h in logging.getLogger().handlers), (
        "на корне не осталось обработчика — заработает lastResort и экран засорится"
    )
    capsys.readouterr()
    logging.getLogger("broker.session").error(CANARY)
    printed = capsys.readouterr()
    assert printed.err == "", f"в консоль ушло: {printed.err}"
    assert printed.out == "", f"в консоль ушло: {printed.out}"


def test_calling_setup_twice_leaves_one_handler(userdata: pathlib.Path) -> None:
    """Повторный вызов не плодит обработчиков: строка не удвоится в файле."""
    setup_logging(userdata=userdata)
    report = setup_logging(userdata=userdata)
    ours = [h for h in logging.getLogger().handlers if isinstance(h, TechnicalLog)]
    assert len(ours) == 1
    logging.getLogger("app.проба").warning(CANARY)
    _flush()
    assert report.path is not None
    assert report.path.read_text(encoding="utf-8").count(CANARY) == 1


# ------------------------------------------------------------------------ права


@POSIX_ONLY
def test_the_log_file_is_no_wider_than_the_token_lying_next_to_it(
    userdata: pathlib.Path,
) -> None:
    """Права лога не шире прав файла с токеном по соседству.

    Лог с более широкими правами в том же каталоге — приглашение прочитать
    его целиком. Сравнение с настоящим соседом, а не с числом: изменись
    правило у токена, разойдутся оба.
    """
    from broker.secret import Secret
    from broker.token_store import TokenStore
    from broker.tokens import TokenScope

    keeper = TokenStore(userdata)
    keeper.save(
        Secret("FAKE-refresh-NEIGHBOUR-NOT-A-REAL-TOKEN"),
        TokenScope.TRADE,
        issued_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    report = setup_logging(userdata=userdata)
    assert report.path is not None
    assert report.permissions_enforced
    assert _mode(report.path) == FILE_MODE
    assert _mode(report.path) == _mode(keeper.path), "лог шире соседа-токена"
    assert _mode(report.path) & 0o077 == 0


@POSIX_ONLY
def test_the_directory_we_created_ourselves_is_closed_to_others(
    userdata: pathlib.Path,
) -> None:
    """Папку лога, которую завели мы, никто, кроме владельца, не откроет."""
    report = setup_logging(userdata=userdata)
    assert report.path is not None
    assert _mode(report.path.parent) == DIR_MODE
    assert _mode(report.path.parent) & 0o077 == 0


@POSIX_ONLY
def test_a_log_file_left_with_wide_permissions_is_narrowed_when_opened(
    userdata: pathlib.Path,
) -> None:
    """Разъехавшиеся права старого файла сужаются при открытии, а не остаются.

    Так бывает после копирования папки, распаковки архива и переноса
    с флешки: файл уже есть, и права у него чужие.
    """
    folder = default_log_dir(userdata)
    folder.mkdir(parents=True)
    stale = folder / LOG_FILE_NAME
    stale.write_text("старое\n", encoding="utf-8")
    os.chmod(stale, 0o644)
    assert _mode(stale) == 0o644

    report = setup_logging(userdata=userdata)
    assert report.path == stale
    assert _mode(stale) == FILE_MODE


@POSIX_ONLY
def test_a_directory_that_already_existed_is_not_re_permissioned(
    tmp_path: pathlib.Path, userdata: pathlib.Path
) -> None:
    """Чужую папку из настроек программа не сужает — решение, а не забывчивость.

    Владелец счёта вправе указать «Документы» или сетевой диск. Молча менять
    права каталога, который завёл не ты, — видимое действие над чужим.
    Замок на самом логе от этого не зависит: файл всё равно `0600`.
    """
    existing = tmp_path / "мои-документы"
    existing.mkdir(mode=0o755)
    os.chmod(existing, 0o755)
    report = setup_logging(existing, userdata=userdata)
    assert report.path == existing / LOG_FILE_NAME
    assert _mode(existing) == 0o755, "программа сузила права чужой папке"
    assert _mode(report.path) == FILE_MODE, "а вот сам лог обязан быть закрыт"


# --------------------------------------------------------------------- ротация


def test_rotation_cuts_at_the_named_size_and_keeps_the_named_count(
    userdata: pathlib.Path,
) -> None:
    """Файл режется по названному размеру, копий хранится названное число.

    Без этого лог растёт неограниченно на машине, которую владелец счёта
    не обслуживает: поток котировок пишет строку в минуту круглый день.
    """
    limit = 4096
    report = setup_logging(userdata=userdata, max_bytes=limit, backups=2)
    assert report.path is not None
    log = logging.getLogger("app.проба")
    for number in range(400):
        log.warning("строка номер %d, набивка: %s", number, "x" * 80)
    _flush()

    folder = report.path.parent
    files = sorted(folder.iterdir())
    assert [path.name for path in files] == [
        LOG_FILE_NAME,
        f"{LOG_FILE_NAME}.1",
        f"{LOG_FILE_NAME}.2",
    ], f"на диске оказалось: {[p.name for p in files]}"
    for path in files:
        assert path.stat().st_size <= limit, f"{path.name} перерос предел: {path.stat().st_size}"


def test_the_ceiling_of_the_log_on_disk_is_the_named_one(userdata: pathlib.Path) -> None:
    """Потолок всего лога на диске — `MAX_BYTES × (BACKUPS + 1)`, и он проверяем."""
    limit = 2048
    report = setup_logging(userdata=userdata, max_bytes=limit, backups=BACKUPS)
    assert report.path is not None
    log = logging.getLogger("app.проба")
    for number in range(2000):
        log.warning("строка номер %d, набивка: %s", number, "x" * 80)
    _flush()
    folder = report.path.parent
    total = sum(path.stat().st_size for path in folder.iterdir())
    assert total <= limit * (BACKUPS + 1), f"лог занял {total} Б"
    assert len(list(folder.iterdir())) == BACKUPS + 1


@POSIX_ONLY
def test_a_rotated_file_is_as_closed_as_the_current_one(userdata: pathlib.Path) -> None:
    """Отложенная копия закрыта так же, как текущий файл.

    Ротация переименовывает файл, и права при `rename` наследуются, — но
    проверяется именно это: копия с широкими правами свела бы защиту на нет,
    ведь в ней лежит ровно то же самое.
    """
    report = setup_logging(userdata=userdata, max_bytes=2048, backups=2)
    assert report.path is not None
    log = logging.getLogger("app.проба")
    for number in range(200):
        log.warning("строка номер %d, набивка: %s", number, "x" * 80)
    _flush()
    rotated = report.path.parent / f"{LOG_FILE_NAME}.1"
    assert rotated.is_file(), "ротации не случилось — проверка вакуумна"
    assert _mode(rotated) == FILE_MODE


def test_the_default_numbers_are_the_measured_ones() -> None:
    """Умолчания ротации — те, под которые сделан замер в шапке модуля.

    Смена числа без пересчёта дневного объёма означает, что глубина лога
    перестала быть известной величиной.
    """
    assert MAX_BYTES == 2 * 1024 * 1024
    assert BACKUPS == 5


# --------------------------------------------------------------------- консоль


#: Проба к проверке ниже. Отдельный процесс обязателен: `capsys` подменяет
#: `sys.stdout`/`sys.stderr` объектами pytest, а проверяется настоящая консоль,
#: включая аварийный вывод Python и вывод чужих библиотек при импорте.
CONSOLE_PROBE = """
import logging
import pathlib
import sys

from app.logs import setup_logging

report = setup_logging(userdata=pathlib.Path(sys.argv[1]))
assert report.working, report.trouble

logging.getLogger("broker.session").warning("отказ брокера: недостаточно средств")
logging.getLogger("app.port").info("прогон по истории закончен")
logging.getLogger("совсем.чужой.логгер").error("КАНАРЕЙКА-В-ЛОГЕ-0192837465")

# Так выглядит отладочная строка, забытая в коде: после настройки лога
# `basicConfig` не делает ничего — его тело целиком под `if not root.handlers`.
logging.basicConfig(level=logging.DEBUG)
logging.getLogger("совсем.чужой.логгер").error("вторая строка после basicConfig")

logging.shutdown()
"""


def test_the_console_stays_silent_while_the_file_gets_everything(
    userdata: pathlib.Path,
) -> None:
    """Программа пишет три строки — консоль пуста, а файл полон.

    Ровно тот отказ, из-за которого заведён `D-021`: владелец счёта получил
    в консоли текст, ему не адресованный. Проверяется настоящими stdout
    и stderr отдельного процесса, а не подменой pytest.
    """
    probe = subprocess.run(
        [sys.executable, "-c", CONSOLE_PROBE, str(userdata)],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr[-2000:]
    assert probe.stderr == "", f"в консоль ушло: {probe.stderr}"
    assert probe.stdout == "", f"в консоль ушло: {probe.stdout}"

    written = (default_log_dir(userdata) / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert "отказ брокера" in written, "запись слоя брокера не дошла до файла"
    assert "прогон по истории закончен" in written, "запись сборки не дошла до файла"
    assert CANARY in written, "запись чужого логгера не дошла до файла"
    assert "вторая строка после basicConfig" in written


def test_setup_leaves_nothing_that_writes_to_the_screen(userdata: pathlib.Path) -> None:
    """На корне нет ни одного обработчика, пишущего в stdout или stderr."""
    setup_logging(userdata=userdata)
    screen = [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, logging.StreamHandler)
        and getattr(handler, "stream", None) in (sys.stdout, sys.stderr)
    ]
    assert not screen, f"на корне висит вывод на экран: {screen}"


def test_basic_config_after_setup_adds_nothing(userdata: pathlib.Path) -> None:
    """`logging.basicConfig`, забытый в коде, после настройки лога бессилен."""
    setup_logging(userdata=userdata)
    before = list(logging.getLogger().handlers)
    logging.basicConfig(level=logging.DEBUG)
    assert logging.getLogger().handlers == before
    assert logging.getLogger().level == logging.INFO, "уровень сбило basicConfig"


class _Broken:
    """Поток, который отказывается писать: диск полон, носитель выдернут."""

    closed = False

    def write(self, text: str) -> int:
        raise OSError("на устройстве не осталось места")

    def tell(self) -> int:
        return 0

    def seek(self, offset: int, whence: int = 0) -> int:
        return 0

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None


def _record_with_a_secret() -> logging.LogRecord:
    return logging.LogRecord(
        "совсем.чужой.логгер",
        logging.WARNING,
        "/x.py",
        1,
        "заголовок %s: %s",
        ("Authorization", "Bearer " + CANARY),
        None,
    )


def test_a_failed_write_says_nothing_to_the_console_and_carries_no_record(
    userdata: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Отказ записи в файл не выводит в консоль ни строки и ни куска записи.

    Штатный `logging.Handler.handleError` печатает в stderr трассировку,
    стек **и** `record.msg` с `record.args` в сыром виде — то есть текст
    до чистки. Для файла с логом это разом и шум на экране, и утечка.
    """
    report = setup_logging(userdata=userdata)
    assert report.path is not None
    handler = next(h for h in logging.getLogger().handlers if isinstance(h, TechnicalLog))
    # Подмена через monkeypatch, а не присваиванием: тип поля задан `logging`,
    # и подавление типов ради подставного потока — цена выше пользы. Настоящий
    # поток при этом **не закрывается**: monkeypatch вернёт его на место
    # раньше, чем уборка позовёт `close_logging`, и закрытый заранее поток
    # уронил бы завершение `ValueError` уже в самой уборке.
    monkeypatch.setattr(handler, "stream", _Broken())

    capsys.readouterr()
    logging.getLogger("совсем.чужой.логгер").warning(
        "заголовок %s: %s", "Authorization", "Bearer " + CANARY
    )
    printed = capsys.readouterr()

    assert printed.err == "", f"в консоль ушло: {printed.err}"
    assert printed.out == "", f"в консоль ушло: {printed.out}"
    assert handler.failures == 1, "отказ записи потерялся молча"
    assert "OSError" in handler.last_failure
    assert "совсем.чужой.логгер" in handler.last_failure, "чья строка пропала — неизвестно"
    assert CANARY not in handler.last_failure


def test_the_standard_error_handler_would_have_printed_the_record(
    userdata: pathlib.Path,
) -> None:
    """Канарейка: штатный `handleError` печатает запись — значит, есть что чинить.

    Без неё проверка выше зеленела бы и в мире, где Python ничего не печатает,
    и переопределение `handleError` выглядело бы лишним.
    """
    report = setup_logging(userdata=userdata)
    assert report.path is not None
    handler = next(h for h in logging.getLogger().handlers if isinstance(h, TechnicalLog))
    buffer = io.StringIO()
    try:
        raise OSError("на устройстве не осталось места")
    except OSError:
        with contextlib.redirect_stderr(buffer):
            logging.Handler.handleError(handler, _record_with_a_secret())
    printed = buffer.getvalue()
    assert "--- Logging error ---" in printed
    assert CANARY in printed, (
        "штатный handleError не вынес запись в stderr — проверка выше ничего не доказывает"
    )


# ------------------------------------------ чистка секретов ставится этим же вызовом


#: Проба отдельным процессом: проверяется эффект **одного** вызова на чистом
#: дереве логгеров. В общем прогоне по нему уже прошлись соседние файлы,
#: и проверка в том же процессе зеленела бы по порядку запуска, а не по коду.
SCRUBBING_PROBE = """
import logging
import pathlib
import sys

from broker.redaction import FOREIGN_LOGGERS, LOGGER_NAME, RedactingFilter, RedactingSink

naked = []
for name in FOREIGN_LOGGERS:
    logger = logging.getLogger(name)
    if [item for item in logger.filters if isinstance(item, RedactingFilter)]:
        naked.append(name + ": чистка стояла ДО настройки лога")
    if [item for item in logger.handlers if isinstance(item, RedactingSink)]:
        naked.append(name + ": сток стоял ДО настройки лога")

from app.logs import setup_logging

setup_logging(userdata=pathlib.Path(sys.argv[1]))

missing = []
for name in (LOGGER_NAME, *FOREIGN_LOGGERS):
    logger = logging.getLogger(name)
    if not [item for item in logger.filters if isinstance(item, RedactingFilter)]:
        missing.append(name + ": нет фильтра")
    if not [item for item in logger.handlers if isinstance(item, RedactingSink)]:
        missing.append(name + ": нет стока")

logging.shutdown()
if naked:
    print("КАНАРЕЙКА МЕРТВА: " + ", ".join(naked))
elif missing:
    print("не накрыто: " + ", ".join(missing))
else:
    print("накрыто всё")
"""


def test_setting_up_the_log_is_what_installs_the_scrubbing_everywhere(
    userdata: pathlib.Path,
) -> None:
    """Чистка чужих веток встаёт тем же вызовом, которым заводится лог.

    Вчерашний остаток: гарантия «`httpx`, `httpcore`, `websockets` накрыты»
    держалась на одной строке в `app/main.py::_live_feed` — то есть на сборке
    потока котировок, — и сторожа на неё не было. Процесс, собранный мимо
    этой строки, писал бы заголовок рукопожатия с токеном в лог молча.
    Теперь она держится на настройке лога, а лог программа заводит всегда.

    Канарейка внутри пробы: до вызова чужие ветки обязаны быть **голыми**.
    Иначе проверка ничего не доказывает — их мог накрыть импорт слоя.
    """
    probe = subprocess.run(
        [sys.executable, "-c", SCRUBBING_PROBE, str(userdata)],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr[-2000:]
    assert probe.stdout.strip() == "накрыто всё", probe.stdout + probe.stderr


def test_the_file_handler_carries_the_scrubbing_formatter(userdata: pathlib.Path) -> None:
    """На файловом обработчике стоит чистящий форматтер, а не обычный.

    Это второй рубеж и единственный, который работает для записи, сделанной
    на логгере вне накрытых поддеревьев: до стока она не доходит вовсе.
    Утечка проверяется прогоном в `tests/test_broker_no_leak.py`; здесь —
    что рубеж на месте, потому что снять его можно одной строкой.
    """
    from broker.redaction import RedactingFormatter

    setup_logging(userdata=userdata)
    handler = next(h for h in logging.getLogger().handlers if isinstance(h, TechnicalLog))
    assert isinstance(handler.formatter, RedactingFormatter)


def test_the_level_lets_the_ordinary_lines_through(userdata: pathlib.Path) -> None:
    """Уровень по умолчанию — INFO: обычные строки слоёв доходят до файла.

    Корень по умолчанию стоит на WARNING, и без явной установки уровня
    `log.info(...)` любого слоя гасился бы ещё до обработчика — лог был бы
    и заведён, и пуст.
    """
    report = setup_logging(userdata=userdata)
    assert report.path is not None
    assert logging.getLogger().level == logging.INFO
    logging.getLogger("broker.session").info("рабочий токен получен: %s", CANARY)
    _flush()
    assert CANARY in report.path.read_text(encoding="utf-8")


def test_a_debug_level_can_be_asked_for_and_reaches_the_file(
    userdata: pathlib.Path,
) -> None:
    """Подробный уровень включается явно и доходит до файла.

    Тем же путём проверяется утечка: подробный лог — единственный режим,
    в котором библиотеки печатают заголовки запроса.
    """
    report = setup_logging(userdata=userdata, level=logging.DEBUG)
    assert report.path is not None
    logging.getLogger("websockets.client").debug("> %s: %s", "X-Request-Id", CANARY)
    _flush()
    assert CANARY in report.path.read_text(encoding="utf-8")


def test_close_logging_takes_our_handlers_off(userdata: pathlib.Path) -> None:
    """Завершение снимает наши обработчики и не трогает чужие."""
    stranger: Any = logging.NullHandler()
    root = logging.getLogger()
    root.addHandler(stranger)
    try:
        setup_logging(userdata=userdata)
        close_logging()
        assert not [h for h in root.handlers if isinstance(h, (TechnicalLog, SilentSink))]
        assert stranger in root.handlers, "снят чужой обработчик"
    finally:
        root.removeHandler(stranger)


def test_the_log_can_be_re_pointed_after_the_settings_are_read(
    userdata: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """Второй вызов переносит лог в папку из настроек и не оставляет хвостов.

    Так лог проводится в программе: первый вызов стоит сразу после разбора
    ключей — до него настроек ещё нет, и писать надо уже сейчас, — второй
    приходит с прочитанной папкой владельца счёта. Обработчик на корне
    обязан остаться ровно один, иначе строка задвоится, а старый файл
    останется открытым до конца работы.
    """
    first = setup_logging(userdata=userdata)
    logging.getLogger("app.старт").warning("до чтения настроек")
    chosen = tmp_path / "куда-сказал-владелец"
    second = setup_logging(chosen, userdata=userdata)
    logging.getLogger("app.окно").warning(CANARY)
    _flush()

    assert first.path is not None
    assert second.path == chosen / LOG_FILE_NAME
    assert len([h for h in logging.getLogger().handlers if isinstance(h, TechnicalLog)]) == 1
    assert "до чтения настроек" in first.path.read_text(encoding="utf-8")
    assert CANARY in second.path.read_text(encoding="utf-8")
    assert CANARY not in first.path.read_text(encoding="utf-8"), "старый файл ещё пишется"
