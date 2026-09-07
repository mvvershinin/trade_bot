"""Файл с токеном: где лежит, какие права, как читается и пишется.

Место — папка `userdata/` рядом с программой, [решение 0003](
../.docs/decisions/0003-runtime-data-location.md). Основную ветку задаёт
ТЗ §4.1: «токен хранится в отдельном файле настроек рядом с программой».
Опция системного хранилища ОС — открытый вопрос заказчика, здесь её нет.

**Почему каталог обязан называться `userdata`.** Решение 0003 принято после
прогона `git check-ignore`: правила по имени файла пропускают пять
правдоподобных имён файла настроек, правила по подстроке без якоря прячут
шесть путей продуктового кода. Работает ровно одно — якорное правило
`/userdata/` на каталог. Файл с токеном, положенный мимо этого каталога,
`git add -A` унесёт в историю репозитория, и вычистить его оттуда после
`push` надёжно нельзя: останется только перевыпуск токена в кабинете.
Поэтому имя каталога проверяется здесь, а не оставляется на дисциплину.

**Права.** ТЗ §4.1: «отдельный файл с правами только для владельца».
Файл создаётся сразу с `0600` через `os.open`, а не создаётся и потом
исправляется: между созданием и `chmod` есть окно, в котором файл читает
кто угодно.

**Имена полей — латиницей, а файл прежнего формата читается по-прежнему.**
До 06.09.2026 ключи в файле были русскими (`версия`, `токен`, `права`).
Это ломало чистку логов: образцы `broker/redaction.py` искали английские
имена полей и ключа нашего собственного файла не находили — единственное
место, где токен лежит открытым текстом, было единственным, чьё имя поля
чистка не знала. Ключи переименованы, `LEGACY_KEYS` держит прежние имена,
и файл, записанный раньше, читается без единой правки на диске. Нынешний
формат появляется при следующей записи, то есть при замене токена.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from broker.errors import TokenFileError
from broker.redaction import log
from broker.secret import Secret
from broker.tokens import RefreshToken, TokenScope, now_utc

#: Имя каталога рантайм-данных. Решение 0003.
USERDATA_DIR_NAME: Final[str] = "userdata"

#: Имя файла с токеном внутри этого каталога.
TOKEN_FILE_NAME: Final[str] = "broker-token.json"

FILE_MODE: Final[int] = 0o600
DIR_MODE: Final[int] = 0o700

#: Версия формата файла. Читатель обязан уметь сказать «формат новее меня»,
#: а не молча разобрать половину полей.
FORMAT_VERSION: Final[int] = 1

# Имена полей — латиницей, правило 6 `CLAUDE.md`. Причина не в букве правила:
# русский ключ в нашем собственном файле не находили образцы чистки логов
# (`redaction._BARE_FIELDS` искал английские имена). То есть единственный файл,
# где токен лежит открытым текстом, был единственным, чьё имя поля чистка
# не знала, — и одна отладочная строка с содержимым файла вынесла бы токен.
_KEY_VERSION: Final[str] = "version"
_KEY_TOKEN: Final[str] = "token"
_KEY_SCOPE: Final[str] = "scope"
_KEY_ISSUED: Final[str] = "issued"
_KEY_ACCOUNT: Final[str] = "account"

#: Те же поля в формате до 06.09.2026: имена по-русски. **Читаются навсегда.**
#:
#: ⚠️ Версия формата при переименовании намеренно **не поднята**. Содержимое
#: полей то же самое, а `load` на незнакомой версии отказывается работать
#: целиком — «файл сделан другой версией программы». Подними мы версию, файл
#: владельца счёта, лежащий на диске прямо сейчас, перестал бы читаться,
#: и токен пришлось бы выпускать заново в личном кабинете.
#:
#: Таблица одна на чтение и на запись в журнал: имена прежнего формата больше
#: нигде руками не перечисляются. Полноту стережёт тест
#: `test_every_field_has_a_name_in_the_previous_format`.
LEGACY_KEYS: Final[dict[str, str]] = {
    _KEY_VERSION: "версия",
    _KEY_TOKEN: "токен",
    _KEY_SCOPE: "права",
    _KEY_ISSUED: "выпущен",
    _KEY_ACCOUNT: "счёт",
}


def _read(payload: dict[str, Any], key: str) -> object:
    """Значение поля: сперва нынешнее имя, затем имя прежнего формата.

    Файл, записанный до 06.09.2026, обязан читаться как есть — иначе
    переименование ключей означало бы для владельца счёта поход в кабинет
    за новым токеном. Порядок именно такой: если в файле оказались оба имени
    (правка вручную поверх старого), верным считается нынешнее.
    """
    if key in payload:
        return payload[key]
    return payload.get(LEGACY_KEYS[key])


def _legacy_names(payload: dict[str, Any]) -> tuple[str, ...]:
    """Имена полей прежнего формата, встреченные в файле.

    Имена, а не значения: строка отсюда уходит в журнал.
    """
    return tuple(name for name in LEGACY_KEYS.values() if name in payload)


@dataclass(frozen=True, slots=True)
class StoredToken:
    """Что лежит в файле.

    `account` — номер брокерского счёта, к которому привязан токен. Секретом
    он не является (владелец видит его в кабинете), но нужен, чтобы заметить
    подмену файла: токен привязан к одному счёту, и смена номера означает,
    что подключились не туда.
    """

    token: RefreshToken
    account: str | None = None
    #: Права файла были шире `0600` и исправлены при чтении.
    permissions_were_loose: bool = False
    #: На этой системе права файла средствами Python не сужаются (Windows).
    permissions_enforced: bool = True


def windows() -> bool:
    return os.name == "nt"


class TokenStore:
    """Чтение и запись файла с токеном. Значение наружу не печатает никогда."""

    def __init__(self, directory: Path | str, *, filename: str = TOKEN_FILE_NAME) -> None:
        directory = Path(directory)
        if directory.name != USERDATA_DIR_NAME:
            raise TokenFileError(
                "Программа не смогла сохранить токен: папка для рабочих данных "
                "названа неверно.",
                technical=(
                    f"каталог токена {directory} должен называться "
                    f"'{USERDATA_DIR_NAME}' — решение 0003; иначе файл не закрыт "
                    "правилом .gitignore и попадёт в историю репозитория"
                ),
            )
        self._directory = directory
        self._path = directory / filename

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.is_file()

    # --- запись ---

    def save(
        self,
        secret: Secret,
        scope: TokenScope,
        *,
        issued_at: datetime | None = None,
        account: str | None = None,
    ) -> StoredToken:
        """Записать токен. Замена токена — это тот же вызов поверх старого файла.

        Запись атомарная: временный файл в том же каталоге плюс `os.replace`.
        Обрыв питания на середине оставит либо прежний токен, либо новый,
        но не половину файла — иначе владельцу счёта пришлось бы выпускать
        токен заново из-за выключенного света.
        """
        issued_at = issued_at or now_utc()
        if issued_at.tzinfo is None:
            raise ValueError("дата выпуска токена должна быть с часовым поясом")

        self._ensure_directory()

        # Временный файл создаётся ДО того, как значение токена превращается
        # в строку: порядок выбран так, чтобы отказ случался в кадре, где
        # ни `payload`, ни `body` ещё не существует — печатать в трассировке
        # нечего.
        #
        # Отказ здесь настоящий, а не гипотетический: диск полон (ENOSPC),
        # исчерпаны дескрипторы (EMFILE), файл держит антивирус, каталог
        # лежит на носителе, смонтированном только для чтения (EROFS).
        # А вот «папка только на чтение» из-за прав — не тот случай:
        # `_ensure_directory` строкой выше возвращает каталогу `0700`,
        # и владелец всегда вернёт себе запись.
        #
        # Отказ поднимается прямо из обработчика: в `OSError` лежат errno
        # и путь, содержимого файла в нём нет — в отличие от
        # `UnicodeDecodeError` в `load`, ради которого там пляска с `refusal`.
        try:
            handle, temporary = tempfile.mkstemp(
                dir=self._directory, prefix=".", suffix=".tmp"
            )
        except OSError as error:
            raise TokenFileError(
                "Программа не смогла сохранить токен: не получилось создать "
                "файл в папке с программой. Проверьте, что у вас есть право "
                "записи в эту папку и что на диске есть свободное место.",
                technical=f"{type(error).__name__}: mkstemp в {self._directory}",
            ) from None

        # Обе переменные заводятся до `try`, чтобы `del` в `finally` отработал
        # и на отказе, случившемся раньше их наполнения.
        payload: dict[str, Any] = {}
        body = ""
        try:
            if not windows():
                # ⚠️ `os.fchmod` на Windows не существует: «Availability: Unix»,
                # поддержка добавлена только в Python 3.13, а программа собрана
                # под 3.12 (`pyproject.toml`: `requires-python = ">=3.12,<3.13"`).
                # Вызов там давал `AttributeError` — не `BrokerError`, — то есть
                # на Windows токен не сохранялся вообще никогда, а владелец
                # счёта получал голое исключение вместо человеческого текста.
                # Правами файлов в Windows заведуют списки ACL, программа их
                # не трогает, и `_restrict` ниже честно отвечает «не выставлены».
                # Вывод сделан по документации и по прогону с удалённым
                # `os.fchmod`; на живой машине с Windows не проверялся.
                try:
                    os.fchmod(handle, FILE_MODE)
                except OSError:
                    # Отказ прав — не повод не сохранить токен. Поставка
                    # портативная и живёт на флешке: exFAT и NTFS прав POSIX
                    # не знают, udisks2 монтирует их без них, и `fchmod`
                    # отвечает `EPERM`. Без этой ветки владелец счёта получал
                    # «проверьте свободное место и право записи» — при том что
                    # место есть, папка доступна, а файл только что создан
                    # в ней же. Деградируем так же, как `_ensure_directory`
                    # и `_restrict`: файл пишем, о правах говорим правду.
                    # `_restrict` ниже на том же носителе ответит «нет»,
                    # и отчёт сойдётся сам.
                    log().warning(
                        "не удалось сузить права нового файла токена: %s",
                        self._directory,
                    )
            payload = {
                _KEY_VERSION: FORMAT_VERSION,
                _KEY_SCOPE: scope.value,
                _KEY_ISSUED: issued_at.astimezone(timezone.utc).isoformat(),
                _KEY_ACCOUNT: account,
                # Значение — последним полем и одним коротким выражением:
                # длинная строка рядом с именем поля читается детектором секретов
                # в tests/test_layers.py как секрет в исходнике.
                _KEY_TOKEN: secret.reveal(),
            }
            body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            stream = os.fdopen(handle, "w", encoding="utf-8")
            handle = -1  # владение дескриптором перешло к потоку
            with stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        except BaseException as error:
            if handle != -1:
                # До `os.fdopen` дело не дошло — дескриптор наш, и закрыть его
                # больше некому. `unlink` убирает имя, но не дескриптор,
                # а `ResourceWarning` на сыром fd не выдаётся: настройка
                # `filterwarnings = ["error"]` такую утечку не поймает.
                try:
                    os.close(handle)
                except OSError:
                    pass
            # Временный файл с токеном не должен пережить отказ записи.
            try:
                os.unlink(temporary)
            except OSError:
                pass
            failure = ""
            if isinstance(error, UnicodeEncodeError):
                # Единственный отказ записи, который несёт с собой сам текст:
                # в `object` лежит `body`, то есть файл с токеном целиком.
                # Зеркало `UnicodeDecodeError` в `load`.
                error.object = ""
                error.args = ()
                failure = f"UnicodeEncodeError: запись {self._path}"
            elif isinstance(error, OSError):
                # Отказ записи — не дефект программы, а состояние машины:
                # кончилось место, файл занят антивирусом, папка стала только
                # на чтение. Владелец счёта в этот момент вставляет токен
                # и обязан прочитать, что делать, а не текст исключения Python.
                failure = f"{type(error).__name__}: запись {self._path}"
            if failure:
                raise TokenFileError(
                    "Программа не смогла сохранить токен: файл не записался. "
                    "Проверьте, что на диске есть свободное место и что папка "
                    "с программой доступна на запись.",
                    technical=failure,
                ) from None
            raise
        finally:
            body = ""
            payload = {}
            del body, payload

        enforced = self._restrict(self._path)
        self._warn_if_unprotected(enforced)
        log().info("токен сохранён в файл %s", self._path)
        return StoredToken(
            token=RefreshToken(secret=secret, scope=scope, issued_at=issued_at),
            account=account,
            permissions_enforced=enforced,
        )

    def delete(self) -> None:
        """Убрать файл. Нужно кнопке «заменить токен» и тестам."""
        try:
            self._path.unlink()
        except FileNotFoundError:
            return
        except OSError as error:
            # Тот же случай, ради которого чинили `load`: каталог на месте
            # файла даёт `IsADirectoryError`, занятый файл на Windows —
            # `PermissionError`. Кнопка «заменить токен» обязана объяснять,
            # а не ронять окно голым исключением.
            raise TokenFileError(
                "Программа не смогла убрать старый файл с токеном. Закройте "
                "программы, которые могли его открыть, и попробуйте ещё раз.",
                technical=f"{type(error).__name__}: unlink {self._path}",
            ) from None
        log().info("файл токена удалён: %s", self._path)

    # --- чтение ---

    def load(self) -> StoredToken:
        """Прочитать токен.

        Права шире `0600` не считаются поводом отказать в работе: программа
        сужает их и сообщает об этом. Отказ здесь означал бы, что робот
        не встаёт из-за прав файла — а причина, по которой права разъехались,
        обычно безобидная (копирование папки, распаковка архива). Но молчать
        тоже нельзя: если файл действительно читал кто-то ещё, токен надо
        перевыпускать, и это решение владельца счёта, а не программы.
        """
        # Один `lstat` вместо `exists()` плюс `stat()` порознь. Так закрываются
        # сразу три вещи:
        #
        # * файл, исчезнувший между проверкой и чтением (замена токена из
        #   другого окна, чистильщик диска), давал голый `FileNotFoundError`
        #   мимо `TokenFileError` и мимо человеческого текста;
        # * `exists()` истинно и для каталога: на месте файла токена каталог
        #   означал бы `chmod 0600` **каталогу** и `IsADirectoryError` при
        #   чтении. Публичный `exists()` этого класса спрашивает `is_file()` —
        #   внутри `load` условие обязано быть тем же;
        # * `lstat`, а не `stat`: ссылка на месте файла увела бы правку прав
        #   на чужой файл вне `userdata/`.
        refusal: tuple[str, str] | None = None
        mode: int | None = None
        try:
            info = self._path.lstat()
        except FileNotFoundError:
            refusal = (
                "Токен брокера ещё не введён. Откройте настройки, вставьте "
                "токен из личного кабинета БКС и укажите его тип.",
                f"нет файла {self._path}",
            )
        except OSError as error:
            refusal = (
                "Не удалось прочитать файл настроек с токеном. Проверьте, "
                "что папка с программой доступна на запись и чтение.",
                f"{type(error).__name__}: чтение свойств {self._path}",
            )
        else:
            if stat.S_ISREG(info.st_mode):
                mode = stat.S_IMODE(info.st_mode)
            else:
                refusal = (
                    "На месте файла настроек с токеном оказался не файл, "
                    "а папка или ссылка. Уберите её и вставьте токен заново "
                    "в настройках программы.",
                    f"{self._path}: не обычный файл",
                )
        if refusal is not None:
            raise TokenFileError(refusal[0], technical=refusal[1])

        loose = self._mode_is_loose(mode)
        # На Windows права файла средствами Python не сужаются (см. `_restrict`),
        # а `_mode_is_loose` там ничего и не проверяет. Ответить «защита
        # выставлена» значит соврать: `save()` в тех же условиях честно
        # отвечает «нет». Расхождение молчаливое и одностороннее — токен вводят
        # один раз, а программа стартует сотни раз, и после первого запуска
        # владелец счёта не увидел бы предупреждения уже никогда.
        enforced = not windows()
        if loose:
            enforced = self._restrict(self._path)
            log().warning(
                "права файла токена были шире 0600 и сужены: %s", self._path
            )
        self._warn_if_unprotected(enforced)

        raw = ""
        # Отказ собирается здесь, а поднимается ниже — **за** пределами `except`.
        # `raise ... from None` внутри обработчика обнуляет `__cause__`, но
        # `__context__` всё равно продолжает указывать на исходное исключение,
        # а через него — на прочитанный буфер. Отказ, поднятый снаружи, ссылок
        # на исходное исключение не имеет вовсе: ни `__cause__`, ни `__context__`,
        # ни чужих кадров в трассировке.
        refusal: tuple[str, str] | None = None
        try:
            raw = self._path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            # Файл не в UTF-8: правка вручную в блокноте на русской Windows,
            # битая копия, склейка двух файлов. `UnicodeDecodeError` — подкласс
            # `ValueError`, а не `OSError`, и мимо перехвата ниже он проходил
            # насквозь, минуя всю чистку и унося **весь файл целиком**: буфер
            # лежит у него в `object`, в `args` и в переменных кадра `codecs`,
            # который попадает в трассировку. Проверено 30.08.2026: файл
            # с токеном в cp1251 печатался в трассировке побайтно.
            # Полезная нагрузка гасится сразу, до выхода из обработчика.
            error.object = b""
            error.args = ()
            refusal = (
                "Файл настроек с токеном не читается: он записан не в той "
                "кодировке. Так бывает, если файл правили вручную в текстовом "
                "редакторе. Выпустите новый токен в личном кабинете БКС "
                "и вставьте его в настройках программы.",
                f"{type(error).__name__} при чтении {self._path}",
            )
        except OSError as error:
            refusal = (
                "Не удалось прочитать файл настроек с токеном. Проверьте, "
                "что папка с программой доступна на запись и чтение.",
                f"{type(error).__name__} при чтении {self._path}",
            )
        if refusal is not None:
            raise TokenFileError(refusal[0], technical=refusal[1])

        payload: Any = None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            # `json.JSONDecodeError` держит **весь текст файла** в атрибуте
            # `doc`. `raise ... from None` обнуляет `__cause__`, но `__context__`
            # продолжает указывать сюда, и токен достаётся одним обращением
            # к атрибуту. Реестр чистки помочь не может: `Secret` на этом пути
            # ещё не создан. Образцы знают оба имени поля, но файл бывает
            # оборван прямо в значении — закрывающей кавычки нет, образец
            # не совпадает. Поэтому нагрузка гасится здесь, а отказ снаружи.
            error.doc = ""
            refusal = (
                "Файл настроек с токеном испорчен и не читается. Выпустите "
                "новый токен в личном кабинете БКС и вставьте его в настройках.",
                f"{self._path}: разбор JSON не удался в позиции {error.pos}",
            )
        finally:
            raw = ""
            del raw
        if refusal is not None:
            raise TokenFileError(refusal[0], technical=refusal[1])

        # Разобранный словарь чистится на выходе в любом случае, включая
        # успешный. Значение токена лежит в нём строкой, а кадры `load`
        # и `_build` попадают в трассировку каждого отказа разбора: «версия
        # формата», «неизвестный тип прав», «битая дата выпуска». Через
        # `capture_locals=True` — отладчик, `pytest -l`, сборщик отчётов
        # об ошибках — печатался сырой токен. `_build` получает тот же объект,
        # поэтому чистка здесь закрывает оба кадра сразу.
        try:
            if not isinstance(payload, dict):
                raise TokenFileError(
                    "Файл настроек с токеном испорчен и не читается. Выпустите "
                    "новый токен в личном кабинете БКС и вставьте его в настройках.",
                    technical=f"{self._path}: ожидался объект JSON",
                )

            version = _read(payload, _KEY_VERSION)
            if version != FORMAT_VERSION:
                raise TokenFileError(
                    "Файл настроек с токеном сделан другой версией программы. "
                    "Вставьте токен заново в настройках.",
                    technical=(
                        f"{self._path}: версия формата {version!r}, "
                        f"ожидалась {FORMAT_VERSION}"
                    ),
                )

            return self._build(payload, loose=loose, enforced=enforced)
        finally:
            if isinstance(payload, dict):
                payload.clear()
            payload = None
            del payload

    def _build(self, payload: dict[str, Any], *, loose: bool, enforced: bool) -> StoredToken:
        self._note_previous_format(payload)
        # Значение вынимается из словаря, а не читается из него: словарь живёт
        # в кадре вызывающего до конца разбора, а `pop` оставляет сырую строку
        # ровно в одной переменной — и та стирается в `finally` ниже.
        #
        # Вынимаются **оба** имени, и всегда: файл с ручной правкой поверх
        # старого держит их одновременно, а невынутое значение осталось бы
        # в словаре и уехало бы в кадры `load` и `_build`. Нынешнее имя
        # вынимается вторым и перебивает прежнее — порядок задан здесь,
        # а не тем, какое из двух значений случайно разобралось.
        value = payload.pop(LEGACY_KEYS[_KEY_TOKEN], None)
        value = payload.pop(_KEY_TOKEN, value)
        present = value is not None
        try:
            secret = Secret(value) if isinstance(value, str) else None
        except ValueError:
            secret = None
        finally:
            value = ""
            del value
        if secret is None:
            raise TokenFileError(
                "В файле настроек нет токена. Вставьте токен из личного "
                "кабинета БКС в настройках программы.",
                technical=(
                    f"{self._path}: поле {_KEY_TOKEN!r} "
                    + ("не прочиталось как значение токена" if present else "отсутствует")
                ),
            )

        raw_scope = _read(payload, _KEY_SCOPE)
        try:
            scope = TokenScope(raw_scope)
        except ValueError:
            raise TokenFileError(
                "В файле настроек не указан тип токена — «только для чтения» "
                "или «для торговли и чтения». Укажите его в настройках.",
                technical=f"{self._path}: неизвестный тип прав {raw_scope!r}",
            ) from None

        issued_raw = _read(payload, _KEY_ISSUED)
        try:
            issued_at = datetime.fromisoformat(str(issued_raw))
        except (TypeError, ValueError):
            raise TokenFileError(
                "В файле настроек испорчена дата выпуска токена. Вставьте "
                "токен заново, чтобы программа знала, когда кончится его срок.",
                technical=f"{self._path}: дата выпуска {issued_raw!r} не разбирается",
            ) from None
        if issued_at.tzinfo is None:
            issued_at = issued_at.replace(tzinfo=timezone.utc)

        account = _read(payload, _KEY_ACCOUNT)
        return StoredToken(
            token=RefreshToken(secret=secret, scope=scope, issued_at=issued_at),
            account=account if isinstance(account, str) else None,
            permissions_were_loose=loose,
            permissions_enforced=enforced,
        )

    def _note_previous_format(self, payload: dict[str, Any]) -> None:
        """Сказать в журнал, что файл записан в формате до 06.09.2026.

        Файл при этом **не переписывается**. Правка чужого файла на чтении
        означала бы, что чтение умеет отказать: носитель только для чтения,
        флешка, антивирус. Испорченный файл токена стоит владельцу счёта
        похода в личный кабинет за новым, и рисковать этим ради имён полей
        нечем: нынешний формат появится сам при следующей замене токена.

        В журнал уходят **имена** полей и путь. Значений здесь нет и быть
        не может — `payload` сюда не печатается ни целиком, ни по частям.
        """
        legacy = _legacy_names(payload)
        if not legacy:
            return
        log().info(
            "файл токена записан в прежнем формате, имена полей по-русски "
            "(%s): %s. Прочитан как есть; в нынешнем формате будет "
            "перезаписан при следующей замене токена в настройках",
            ", ".join(legacy),
            self._path,
        )

    # --- права и каталог ---

    def _ensure_directory(self) -> None:
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise TokenFileError(
                "Программа не смогла создать папку для своих данных рядом "
                "с собой. Перенесите программу туда, где у вас есть право "
                "записи — например, в папку пользователя.",
                technical=f"{type(error).__name__}: mkdir {self._directory}",
            ) from None
        if not windows():
            try:
                os.chmod(self._directory, DIR_MODE)
            except OSError:
                log().warning("не удалось сузить права папки %s", self._directory)

    @staticmethod
    def _mode_is_loose(mode: int | None) -> bool:
        """Права шире владельца. Режим берётся из уже сделанного `lstat`.

        Второй `stat` внутри `load` был бы вторым обращением к файлу, который
        мог за это время исчезнуть, — и голым `FileNotFoundError` мимо
        человеческого текста.
        """
        if windows() or mode is None:
            return False
        return bool(mode & 0o077)

    def _warn_if_unprotected(self, enforced: bool) -> None:
        """Сказать в журнал, что права файла с токеном не выставлены.

        Поле `StoredToken.permissions_enforced` сегодня не читает никто:
        проброс в окно — это слой `ui/`, он вне этой правки. Пока предупреждения
        на экране нет, единственное место, где владелец счёта и техник могут
        это увидеть, — технический лог, и запись обязана появляться на обеих
        ветках. До правки её не было ни одной: `log().warning` стоял только
        в ветке «права разъехались и сужены», то есть на Windows молчали всегда.
        """
        if enforced:
            return
        log().warning(
            "права файла токена не выставлены программой: %s. Доступ к файлу "
            "определяют настройки системы (на Windows — списки ACL). Требование "
            "«права только для владельца» на этой системе не выполнено",
            self._path,
        )

    @staticmethod
    def _restrict(path: Path) -> bool:
        """Сузить права до владельца. Возвращает, получилось ли это по-настоящему.

        ⚠️ **Windows.** `os.chmod` там переключает единственный бит «только для
        чтения» и доступ других пользователей не ограничивает: правами файлов
        в NTFS заведуют списки контроля доступа, и Python их не трогает.
        ТЗ §4.1 говорит «права только для владельца», для Windows это означает
        ACL, а ACL здесь **не выставляется**. Функция возвращает `False`, чтобы
        программа сказала об этом владельцу счёта, а не делала вид, что защита
        стоит на обеих системах.
        """
        if windows():
            return False
        try:
            os.chmod(path, FILE_MODE)
        except OSError:
            log().warning("не удалось сузить права файла %s", path)
            return False
        return True
