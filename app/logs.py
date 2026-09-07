"""Технический лог: куда пишется, с какими правами, чего в консоли не бывает.

Единственное место программы, где настраивается журналирование
(`ARCHITECTURE.md` §2: это обязанность `app/`). До 05.09.2026 такого места
не было вовсе, и следствий было два, оба видел владелец счёта: сперва любая
запись любого слоя уходила в аварийный вывод Python — то есть в консоль
сырым текстом, — а после того, как перекидывание убрали
(`broker/redaction.py::RedactingSink.emit`), записей не стало видно никому.

Что здесь решено
----------------
**Файл, а не консоль.** Программа пишет технический лог в файл сразу после
установки, без единой настройки. Место — `userdata/logs/terminal.log`, рядом
с базой свечей и файлом токена ([решение 0003](
../.docs/decisions/0003-runtime-data-location.md) прямо называет технический
лог среди того, что живёт в `userdata/`): папка переносится и копируется
целиком, и лог едет вместе с базой, к которой относится.

**Консоль остаётся чистой.** На корневой логгер вешается ровно один
обработчик — файловый. Сам факт его присутствия отменяет `logging.lastResort`,
аварийный вывод Python в stderr; ничего пишущего в stdout или stderr здесь
не заводится. Побочно и полезно: после этой настройки
`logging.basicConfig(level=DEBUG)` не делает **ничего** — его тело целиком
стоит под `if len(root.handlers) == 0`, — то есть отладочная строка,
забытая в коде, не выведет владельцу счёта поток котировок на экран.

**Токен.** Файл на диске — ровно тот случай утечки, ради которого написана
`broker/redaction.py`. Рубежей здесь два, и они закрывают разные дыры:

1. `install_redaction((LOGGER_NAME, *FOREIGN_LOGGERS))` зовётся отсюда,
   а не из сборки окна. `Logger.callHandlers` идёт от логгера записи **вверх**
   по предкам, поэтому сток на `broker` (и на `httpx`, `httpcore`,
   `websockets`) срабатывает раньше корневого обработчика — чистка стоит
   до файла по построению `logging`, а не по порядку, в котором мы добавляли
   обработчики.
2. На самом файловом обработчике стоит `RedactingFormatter`. Это и есть
   гарантия для файла: запись, сделанная на логгере вне любого накрытого
   поддерева, до стока не доходит вовсе, и без форматтера ушла бы в файл
   как есть. Первый рубеж чистит запись, второй — готовую строку вместе
   с трассировкой, до которой фильтр не достаёт.

**Отказ записи не идёт в консоль и не несёт с собой запись.** Штатный
`logging.Handler.handleError` печатает в stderr трассировку, стек вызова
и `record.msg` с `record.args` **в сыром виде** — то есть текст до чистки.
Переопределён, см. `TechnicalLog.handleError`.

Ротация: откуда числа
---------------------
Замер длины строки на настоящих сообщениях проекта (`broker.session`,
`app.live_feed`, `app.port`, `httpx._client`, кадр `websockets` на DEBUG),
формат — тот же, что ниже: медиана **159 Б**, максимум 189 Б. Считаем 160 Б.

* обычный уровень (INFO): поток котировок пишет строку в минуту, сессия
  10:00–23:50 МСК — это 830 минут, то есть **~133 КБ в день**;
* самый подробный (DEBUG со стоком сокета): библиотека печатает каждый
  принятый кадр, а снимков брокер шлёт девять в минуту (замер `B-007`);
  с нашими строками — десять в минуту, то есть **~1,3 МБ в день**.

Отсюда `MAX_BYTES = 2 МБ` и `BACKUPS = 5`, потолок — **12 МБ на шесть файлов**:

* на INFO один файл держит 15 торговых дней, все шесть — около 90; это ровно
  срок жизни токена личного кабинета, то есть в логе всегда лежит вся жизнь
  текущего токена;
* на DEBUG один файл держит полтора дня — подробный лог включают утром
  ради разбора, и утро не должно уехать за край к вечеру; все шесть — 9 дней;
* 12 МБ на машине, которую владелец счёта не обслуживает, — величина, о
  которой не думают, а один файл в 2 МБ ужимается примерно в 200 КБ и
  отправляется письмом, когда его просят прислать.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import pathlib
import sys
from dataclasses import dataclass
from io import TextIOWrapper
from typing import Any, Final, cast

from broker.redaction import FOREIGN_LOGGERS, LOGGER_NAME, RedactingFormatter, scrub
from broker.redaction import install as install_redaction
from market.paths import userdata_dir

__all__ = [
    "BACKUPS",
    "DIR_MODE",
    "FILE_MODE",
    "LINE_FORMAT",
    "LOG_DIR_NAME",
    "LOG_FILE_NAME",
    "MAX_BYTES",
    "LogSetup",
    "close_logging",
    "default_log_dir",
    "setup_logging",
]

#: Папка лога внутри `userdata/`. Отдельная, а не корень `userdata/`: рядом
#: лежит база с файлами `-wal` и `-shm`, и шесть файлов лога в той же куче
#: мешали бы отличить рабочие данные от разбора неполадок.
LOG_DIR_NAME: Final[str] = "logs"

#: Имя файла латиницей — правило проекта: кириллица в имени ломает пути
#: в консоли, в путях сборки Nuitka под Windows и в любой выдаче, где имя
#: показывается escape-последовательностями.
LOG_FILE_NAME: Final[str] = "terminal.log"

#: Права файла и папки. Те же, что у файла с токеном по соседству
#: (`broker/token_store.py`): лог с более широкими правами в том же каталоге —
#: приглашение прочитать его целиком.
FILE_MODE: Final[int] = 0o600
DIR_MODE: Final[int] = 0o700

#: Потолок одного файла и число хранимых копий. Обоснование — в шапке модуля.
MAX_BYTES: Final[int] = 2 * 1024 * 1024
BACKUPS: Final[int] = 5

#: Имя потока в строке не для красоты: по решению 0005 в программе живут
#: цикл событий с портами и отдельный поток работника данных, и «кто написал»
#: — первый вопрос при разборе гонки между догрузкой и живым потоком.
LINE_FORMAT: Final[str] = "%(asctime)s %(levelname)-7s %(threadName)s %(name)s: %(message)s"


def _windows() -> bool:
    return os.name == "nt"


def default_log_dir(userdata: pathlib.Path | None = None) -> pathlib.Path:
    """Папка лога по умолчанию — `userdata/logs`."""
    return (userdata or userdata_dir()) / LOG_DIR_NAME


@dataclass(frozen=True, slots=True)
class LogSetup:
    """Что получилось: куда пишем, выставлены ли права, о чём сказать человеку.

    Возвращается наружу, потому что решать, показывать ли это в окне, — дело
    сборки, а не журналирования. Молча съесть «лог вести не удалось» нельзя:
    владелец счёта узнает об этом в день, когда лог понадобится.
    """

    #: Файл, в который пишется лог. `None` — лога нет, причина в `trouble`.
    path: pathlib.Path | None
    #: Папка, которая в итоге подошла.
    directory: pathlib.Path | None
    #: Папка, которую назвал владелец счёта. `None` — умолчание.
    asked: pathlib.Path | None
    #: Права `0600` выставлены программой по-настоящему. На Windows правами
    #: заведуют списки ACL, Python их не трогает — там всегда `False`,
    #: и это говорится вслух, а не выдаётся за защиту.
    permissions_enforced: bool
    #: Что пошло не так — человеческим языком. Пусто — всё в порядке.
    trouble: str

    @property
    def working(self) -> bool:
        """Лог пишется в файл."""
        return self.path is not None


class TechnicalLog(logging.handlers.RotatingFileHandler):
    """Файл лога: права `0600` с рождения, отказ записи не идёт в консоль."""

    def __init__(self, path: pathlib.Path, *, max_bytes: int, backups: int) -> None:
        # Оба поля заводятся ДО `super().__init__`: он с `delay=False` сразу
        # зовёт `_open`, а тот их пишет.
        self.permissions_enforced: bool = not _windows()
        self.failures: int = 0
        self.last_failure: str = ""
        super().__init__(
            os.fspath(path),
            maxBytes=max_bytes,
            backupCount=backups,
            encoding="utf-8",
            delay=False,
        )

    def _open(self) -> TextIOWrapper[Any]:  # имя задано logging.FileHandler
        """Открыть файл сразу с правами `0600`, а не открыть и потом исправить.

        Между `open` и `chmod` есть окно, в котором файл читает кто угодно —
        то же рассуждение, что в `broker/token_store.py`. `os.open` даёт
        права при создании, `fchmod` поверх нужен для двух случаев: файл
        уже существовал с широкими правами (папку скопировали, распаковали
        из архива) и `umask` сузил режим создания.

        Зовётся не только при сборке, но и на каждой ротации: `doRollover`
        переименовывает текущий файл (права при `rename` сохраняются)
        и открывает новый этим же методом.
        """
        if _windows():
            return super()._open()
        descriptor = os.open(
            self.baseFilename, os.O_CREAT | os.O_WRONLY | os.O_APPEND, FILE_MODE
        )
        try:
            os.fchmod(descriptor, FILE_MODE)
        except OSError:
            # Не повод не вести лог: поставка портативная и живёт на флешке,
            # exFAT и NTFS прав POSIX не знают. Деградируем так же, как
            # `token_store`: файл пишем, о правах говорим правду.
            self.permissions_enforced = False
        stream = os.fdopen(
            descriptor, self.mode, encoding=self.encoding, errors=self.errors
        )
        # `self.mode` — обычная строка, и по ней typeshed не выбирает
        # текстовую ветку `open`. Подписи это приведение не меняет: режим
        # у `FileHandler` текстовый всегда.
        return cast("TextIOWrapper[Any]", stream)

    def handleError(  # noqa: N802 — имя метода задано logging.Handler
        self, record: logging.LogRecord
    ) -> None:
        """Запомнить отказ записи и **ничего не выводить**.

        ⚠️ Штатный `logging.Handler.handleError` пишет в stderr трассировку,
        стек вызова и отдельной строкой `record.msg` вместе с `record.args`
        (`logging/__init__.py`, «Issue 18671»). Здесь это два нарушения разом:

        * консоль владельца счёта перестаёт быть чистой ровно в тот момент,
          когда с диском что-то не так, — то есть когда он и без того
          растерян;
        * `record.msg` и `record.args` — это текст **до** чистки. Запись,
          сделанная на логгере вне накрытого поддерева, чистится нашим
          форматтером в момент записи в файл; в `handleError` она приходит
          сырой, и заголовок рукопожатия ушёл бы в консоль вместе с рабочим
          токеном.

        Причина не теряется: она копится здесь и попадает в `LogSetup`
        следующей сборки, а сам счётчик читается тестом. Текст причины
        всё равно прогоняется через `scrub` — принцип «не доверяй тому,
        что кладёшь в строку» дешевле разбора, какие исключения несут с собой
        данные, а какие нет.
        """
        self.failures += 1
        error = sys.exc_info()[1]
        why = f"{type(error).__name__}: {error}" if error else "отказ без исключения"
        # Имя логгера — не секрет и единственное, что берётся из записи:
        # по нему видно, чья строка не записалась. Ни `msg`, ни `args`
        # отсюда не читаются намеренно, см. выше.
        self.last_failure = scrub(f"{why} (запись логгера {record.name})")


class SilentSink(logging.Handler):
    """Обработчик, который ничего не пишет: он держит консоль чистой.

    Ставится, когда файл лога завести не удалось. Без него на корневом
    логгере не остаётся ни одного обработчика, а тогда Python включает
    `logging.lastResort` — и всё, что программа пишет, снова летит владельцу
    счёта в консоль. То есть отсутствие лога превращалось бы в худшее
    из двух зол: и файла нет, и экран засорён.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)

    def emit(self, record: logging.LogRecord) -> None:  # имя задано logging
        """Ничего не делает намеренно."""


def _prepare_directory(path: pathlib.Path) -> str:
    """Создать папку лога. Возвращает причину отказа; пусто — всё вышло.

    Права `0700` ставятся **только на папку, которую создали мы**. Владелец
    счёта вправе указать в настройках существующий каталог — свои «Документы»,
    сетевой диск, — и молча сужать права чужой папки программа не должна:
    это её видимое действие над тем, чего она не заводила. Замок на самом
    логе от этого не зависит — файл всегда `0600`, а он и есть защита.
    """
    try:
        path.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        if not path.is_dir():
            return f"{path} — не папка"
        return ""
    except OSError as error:
        return f"{type(error).__name__}: {error}"
    if not _windows():
        try:
            os.chmod(path, DIR_MODE)
        except OSError:
            # Права не сузились — папка при этом есть и писать в неё можно,
            # а замок на самом логе от прав папки не зависит: файл `0600`.
            pass
    return ""


def _open_log(path: pathlib.Path, *, max_bytes: int, backups: int) -> TechnicalLog:
    """Открыть файл лога в готовой папке. Отказ поднимается как `OSError`."""
    handler = TechnicalLog(path, max_bytes=max_bytes, backups=backups)
    handler.setFormatter(RedactingFormatter(LINE_FORMAT))
    return handler


def _first_that_opens(
    candidates: tuple[pathlib.Path, ...], *, max_bytes: int, backups: int
) -> tuple[TechnicalLog | None, list[str]]:
    """Первая папка списка, в которой файл лога удалось открыть.

    Цепочка обязанностей: звенья идут списком, и **порядок — это данные**,
    а не последовательность вложенных `if`. Поэтому порядок проверяется
    тестом, а добавление третьего места (скажем, временной папки системы)
    стоит одной строки в списке и ни одной здесь.

    Отказы копятся все: сказать владельцу счёта «указанная папка не подошла,
    пишу в запасную» можно, только помня, что именно не вышло.
    """
    troubles: list[str] = []
    for candidate in candidates:
        refusal = _prepare_directory(candidate)
        if not refusal:
            try:
                return _open_log(
                    candidate / LOG_FILE_NAME, max_bytes=max_bytes, backups=backups
                ), troubles
            except OSError as error:
                refusal = f"{type(error).__name__}: {error}"
        troubles.append(f"{candidate}: {refusal}")
    return None, troubles


def close_logging() -> None:
    """Снять наши обработчики с корневого логгера. Для завершения и для тестов."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, (TechnicalLog, SilentSink)):
            root.removeHandler(handler)
            handler.close()


def setup_logging(
    directory: pathlib.Path | str | None = None,
    *,
    userdata: pathlib.Path | None = None,
    level: int = logging.INFO,
    max_bytes: int = MAX_BYTES,
    backups: int = BACKUPS,
) -> LogSetup:
    """Завести технический лог. Зовётся один раз при запуске, повтор безвреден.

    `directory` — папка, названная владельцем счёта в настройках; пусто
    означает умолчание. Порядок проб — **данные, а не ветвление**: сначала
    названная папка, потом умолчание, и первая, которая открылась, побеждает.
    Отсюда и поведение при негодной настройке: лог не пропадает, а уезжает
    в `userdata/logs`, и об этом говорится в `LogSetup.trouble`.

    Чистка секретов ставится здесь же и на все ветки сразу. Это не украшение
    порядка: вызов из сборки окна накрывал только тот процесс, который окно
    поднимает, и гарантия «чужие логгеры накрыты» держалась на одной строке
    в `app/main.py`. Теперь она держится на том же вызове, которым программа
    заводит лог, — а лог она заводит всегда и первым делом.
    """
    install_redaction((LOGGER_NAME, *FOREIGN_LOGGERS))

    close_logging()
    root = logging.getLogger()
    root.setLevel(level)

    asked = pathlib.Path(directory).expanduser() if directory else None
    fallback = default_log_dir(userdata)
    # `dict.fromkeys` вместо `set`: порядок проб — часть поведения, и он же
    # проверяется тестом. Совпадающие пути схлопываются, чтобы не жаловаться
    # дважды на одну и ту же папку.
    candidates = tuple(dict.fromkeys(path for path in (asked, fallback) if path))

    handler, troubles = _first_that_opens(candidates, max_bytes=max_bytes, backups=backups)
    if handler is not None:
        root.addHandler(handler)
        chosen = pathlib.Path(handler.baseFilename)
        report = LogSetup(
            path=chosen,
            directory=chosen.parent,
            asked=asked,
            permissions_enforced=handler.permissions_enforced,
            trouble=_fallback_note(troubles, chosen=chosen.parent),
        )
        _say_where(report, max_bytes=max_bytes, backups=backups)
        return report

    root.addHandler(SilentSink())
    return LogSetup(
        path=None,
        directory=None,
        asked=asked,
        permissions_enforced=False,
        trouble=(
            "Технический лог вести не удалось: "
            + "; ".join(troubles)
            + ". Программа работает, но разбирать неполадки будет не по чему."
        ),
    )


def _fallback_note(troubles: list[str], *, chosen: pathlib.Path) -> str:
    """Фраза о том, что названная папка не подошла и лог уехал в запасную."""
    if not troubles:
        return ""
    return (
        f"Технический лог не удалось вести в указанной папке ({troubles[0]}). "
        f"Программа пишет его в {chosen}."
    )


def _say_where(report: LogSetup, *, max_bytes: int, backups: int) -> None:
    """Первая строка нового лога: где он лежит и когда начнёт стираться.

    Пишется после того, как обработчик встал, — иначе строка ушла бы в никуда.
    Заодно доказывает при разборе, что запись до файла доходит: пустой файл
    и отсутствие настройки выглядят одинаково.
    """
    log = logging.getLogger(__name__)
    log.info(
        "технический лог: %s, до %d КБ на файл, копий %d",
        report.path,
        max_bytes // 1024,
        backups,
    )
    if not report.permissions_enforced:
        log.warning(
            "права файла лога не выставлены программой: %s. Доступ к файлу "
            "определяют настройки системы (на Windows — списки ACL)",
            report.path,
        )
    if report.trouble:
        log.warning("%s", report.trouble)
