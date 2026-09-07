"""Файл с токеном: место, права, атомарность записи, поведение при поломке.

Проверяется то, что в решении 0003 и ТЗ §4.1 записано словами: папка
`userdata/`, права только владельцу, внятный отказ на каждый вид поломки.

Настоящий токен здесь не участвует: только приметные подделки.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import stat
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from broker.errors import BrokerError, TokenFileError
from broker.secret import Secret
from broker.token_store import (
    FILE_MODE,
    LEGACY_KEYS,
    TOKEN_FILE_NAME,
    USERDATA_DIR_NAME,
    TokenStore,
    windows,
)
from broker.tokens import TokenScope

FAKE = "FAKE-store-0000-1111-2222-NOT-A-REAL-TOKEN"
OTHER = "FAKE-store-9999-8888-7777-NOT-A-REAL-TOKEN"

ISSUED = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def in_previous_format(fields: dict[str, Any]) -> dict[str, Any]:
    """Тот же набор полей именами до 06.09.2026 — по-русски.

    Имена берутся из таблицы слоя, а не переписываются здесь руками: иначе
    забытое поле молча выпало бы из проверки прежнего формата.
    """
    return {LEGACY_KEYS[name]: value for name, value in fields.items()}


#: Как записать одно и то же тело файла двумя форматами. Каждая проверка
#: разбора гоняется по обоим: файл прежнего формата лежит у владельца счёта
#: на диске прямо сейчас, и его пути обязаны быть покрыты не хуже нынешних.
FORMATS: dict[str, Any] = {
    "нынешний": lambda fields: fields,
    "прежний": in_previous_format,
}

#: Целый и правильный файл нынешнего формата.
WHOLE_FILE: dict[str, Any] = {
    "version": 1,
    "scope": "trade-api-write",
    "issued": ISSUED.isoformat(),
    "account": "БКС-777",
    "token": FAKE,
}


def body(fields: dict[str, Any], shape: str) -> str:
    """Тело файла нужным форматом."""
    return json.dumps(FORMATS[shape](fields), ensure_ascii=False)


#: Файл, где остались **оба** имени поля: ручная правка поверх старого.
#: Значение под нынешним именем — другое, чтобы было видно, какое из двух
#: взято. Константы модуля, а не локальные переменные тестов: локальная
#: попала бы в переменные кадра, а кадр теста входит в трассировку отказа —
#: проверка искала бы собственный мусор вместо утечки слоя.
MIXED_FILE: str = json.dumps(
    {**in_previous_format(WHOLE_FILE), "token": OTHER, "scope": "trade-api-read"},
    ensure_ascii=False,
)

#: То же, но под нынешним именем лежит негодное значение, а под прежним —
#: годное. Разбор обязан отказать: приоритет имён задан кодом, а не удачей.
MIXED_EMPTY_FILE: str = json.dumps(
    {**in_previous_format(WHOLE_FILE), "token": "   "},
    ensure_ascii=False,
)

#: Файл токена, записанный не в UTF-8: русская Windows, «Блокнот», кодировка
#: ANSI, битая копия. Русские имена полей в cp1251 — это и есть байты,
#: на которых спотыкается разбор UTF-8.
#:
#: ⚠️ Формат здесь **прежний**, и это не забывчивость: файл нынешнего формата
#: с токеном-подделкой состоит из одних символов ASCII, то есть в cp1251
#: и в UTF-8 он одинаков и `UnicodeDecodeError` на нём не случается вовсе.
#: Проверять эту ветку можно только на файле с кириллицей — то есть на том
#: самом, который лежит у владельца счёта.
#:
#: Константа модуля, а не локальная переменная теста: локальная попала бы
#: в переменные кадра, а кадр теста входит в трассировку отказа — проверка
#: искала бы собственный мусор вместо утечки слоя.
CP1251_FILE = json.dumps(
    {
        "версия": 1,
        "права": "trade-api-write",
        "выпущен": ISSUED.isoformat(),
        "токен": FAKE,
    },
    ensure_ascii=False,
).encode("cp1251")

on_posix = pytest.mark.skipif(windows(), reason="права POSIX; на Windows нужны ACL")


@pytest.fixture
def pretend_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ветка Windows на любой системе.

    Подменяется два свойства среды, и оба обязательны.

    1. Ответ на вопрос «мы на Windows».
    2. **Отсутствие `os.fchmod`.** Это не косметика: на Windows этой функции
       нет — «Availability: Unix», поддержка появилась только в Python 3.13,
       а программа собрана под 3.12 (`pyproject.toml`:
       `requires-python = ">=3.12,<3.13"`). Фикстура, подменявшая только
       `windows()`, проверяла ветку, которой на настоящей Windows нет:
       `os.fchmod` на POSIX существует, вызов проходил, и тест зеленел
       при коде, который на Windows падает `AttributeError` и не сохраняет
       токен вообще никогда.

    Подменять `os.name` нельзя: его читают `pathlib`, `tempfile` и половина
    стандартной библиотеки — тест начал бы проверять их поведение, а не наше.
    Проверяется здесь **отчёт** о правах и обход `os.fchmod`; сами права
    на Windows задают списки ACL, которых Python не трогает, и на живой
    машине с Windows это не проверялось — её нет.
    """
    monkeypatch.setattr("broker.token_store.windows", lambda: True)
    monkeypatch.delattr(os, "fchmod", raising=False)


@pytest.fixture
def userdata(tmp_path: pathlib.Path) -> pathlib.Path:
    directory = tmp_path / USERDATA_DIR_NAME
    directory.mkdir()
    return directory


def mode(path: pathlib.Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --- место ---


def test_directory_must_be_userdata(tmp_path: pathlib.Path) -> None:
    """Каталог с другим именем не принимается: решение 0003.

    Правило `/userdata/` в `.gitignore` привязано к месту, а не к имени файла.
    Файл, положенный мимо, `git add -A` унесёт в историю репозитория.
    """
    with pytest.raises(TokenFileError) as caught:
        TokenStore(tmp_path / "settings")
    assert USERDATA_DIR_NAME in caught.value.technical
    assert "0003" in caught.value.technical


def test_file_name_is_fixed(userdata: pathlib.Path) -> None:
    keeper = TokenStore(userdata)
    assert keeper.path == userdata / TOKEN_FILE_NAME


def test_directory_is_created_on_save(tmp_path: pathlib.Path) -> None:
    """Папки может не быть: при первом запуске программа создаёт её сама."""
    target = tmp_path / "куда-то" / USERDATA_DIR_NAME
    keeper = TokenStore(target)
    assert not target.exists()
    keeper.save(Secret(FAKE), TokenScope.TRADE)
    assert keeper.path.is_file()


# --- права ---


@on_posix
def test_saved_file_is_readable_only_by_owner(userdata: pathlib.Path) -> None:
    """ТЗ §4.1: отдельный файл с правами только для владельца."""
    keeper = TokenStore(userdata)
    keeper.save(Secret(FAKE), TokenScope.TRADE)
    assert mode(keeper.path) == FILE_MODE, oct(mode(keeper.path))


@on_posix
def test_directory_is_closed_too(userdata: pathlib.Path) -> None:
    """Права на файл бесполезны, если каталог открыт на чтение всем."""
    keeper = TokenStore(userdata)
    keeper.save(Secret(FAKE), TokenScope.TRADE)
    assert mode(userdata) == 0o700, oct(mode(userdata))


@on_posix
def test_umask_does_not_widen_the_file(userdata: pathlib.Path) -> None:
    """При щедрой umask файл всё равно создаётся закрытым.

    `open()` с обычным режимом получил бы `0o666 & ~umask` — при umask 0
    это файл, читаемый всеми. Здесь права выставляются на дескрипторе.
    """
    previous = os.umask(0)
    try:
        keeper = TokenStore(userdata)
        keeper.save(Secret(FAKE), TokenScope.TRADE)
        assert mode(keeper.path) == FILE_MODE, oct(mode(keeper.path))
    finally:
        os.umask(previous)


@on_posix
def test_loose_permissions_are_tightened_and_reported(userdata: pathlib.Path) -> None:
    """Разъехавшиеся права сужаются, и об этом говорится, а не молчится.

    Отказ работать был бы хуже: причина обычно безобидная — копирование
    папки, распаковка архива. Но и молчать нельзя: если файл действительно
    читал кто-то ещё, токен надо перевыпускать, и решает это владелец счёта.
    """
    keeper = TokenStore(userdata)
    keeper.save(Secret(FAKE), TokenScope.TRADE)
    os.chmod(keeper.path, 0o644)

    stored = keeper.load()
    assert stored.permissions_were_loose is True
    assert mode(keeper.path) == FILE_MODE, oct(mode(keeper.path))

    again = keeper.load()
    assert again.permissions_were_loose is False


@on_posix
def test_permissions_are_reported_as_enforced_on_posix(userdata: pathlib.Path) -> None:
    keeper = TokenStore(userdata)
    stored = keeper.save(Secret(FAKE), TokenScope.TRADE)
    assert stored.permissions_enforced is True


@on_posix
def test_posix_reports_the_same_on_save_and_on_load(userdata: pathlib.Path) -> None:
    """На POSIX обе стороны отвечают «защита выставлена» — и это правда.

    Парная проверка к ветке Windows ниже: она держит на месте то, что
    исправление честности на Windows не перевернуло ответ на POSIX.
    """
    keeper = TokenStore(userdata)
    saved = keeper.save(Secret(FAKE), TokenScope.TRADE)
    loaded = keeper.load()
    assert saved.permissions_enforced is True
    assert loaded.permissions_enforced is True
    assert loaded.permissions_were_loose is False


# --- права: ветка Windows ---


def test_windows_save_admits_that_permissions_are_not_set(
    userdata: pathlib.Path, pretend_windows: None
) -> None:
    """`os.chmod` на Windows правами других пользователей не заведует.

    Там их задают списки ACL, и Python их не выставляет. ТЗ §4.1 требует
    «права только для владельца» — на Windows это требование программой
    не выполнено, и говорить об этом обязана она сама.
    """
    keeper = TokenStore(userdata)
    stored = keeper.save(Secret(FAKE), TokenScope.TRADE)
    assert stored.permissions_enforced is False


def test_windows_load_admits_it_too(
    userdata: pathlib.Path, pretend_windows: None
) -> None:
    """Чтение рапортует о защите ровно то же, что и запись.

    Было наоборот: `save()` честно отвечал «права не выставлены», `load()` —
    всегда «выставлены». Расхождение одностороннее и потому опасное: токен
    вводят один раз, а программа запускается сотни раз, и после первого
    запуска предупреждение не появилось бы уже никогда.
    """
    keeper = TokenStore(userdata)
    saved = keeper.save(Secret(FAKE), TokenScope.TRADE)
    loaded = keeper.load()

    assert saved.permissions_enforced is False
    assert loaded.permissions_enforced is False, (
        "чтение сообщает о защите, которой на Windows нет"
    )
    assert loaded.permissions_enforced == saved.permissions_enforced


def test_windows_does_not_claim_it_tightened_anything(
    userdata: pathlib.Path, pretend_windows: None
) -> None:
    """«Права были шире и сужены» на Windows — тоже неправда.

    Сузить их программа там не может, значит и сообщать о сужении нечего:
    владелец счёта прочитал бы «программа всё поправила» и успокоился.
    """
    keeper = TokenStore(userdata)
    keeper.save(Secret(FAKE), TokenScope.TRADE)
    if not windows():
        os.chmod(keeper.path, 0o644)

    loaded = keeper.load()
    assert loaded.permissions_were_loose is False
    assert loaded.permissions_enforced is False


def test_windows_round_trip_still_works(
    userdata: pathlib.Path, pretend_windows: None
) -> None:
    """Токен на Windows сохраняется и читается — без `os.fchmod`.

    Главное здесь — что запись вообще доходит до конца. `os.fchmod` фикстурой
    убран, потому что на Windows его нет: до правки `save()` падал там
    `AttributeError`, владелец счёта видел голое исключение вместо текста,
    а токен не сохранялся никогда — при том что ТЗ требует одну программу
    под обе системы.

    ⚠️ Проверено на POSIX с убранным `os.fchmod` и по документации Python;
    на живой машине с Windows не проверялось — её нет. Списки ACL, которыми
    Windows на самом деле заведует правами, здесь не выставляются и не
    проверяются: программа их не трогает и об этом говорит.
    """
    keeper = TokenStore(userdata)
    saved = keeper.save(Secret(FAKE), TokenScope.TRADE, issued_at=ISSUED, account="123456")
    assert saved.permissions_enforced is False

    stored = keeper.load()
    assert stored.token.secret == Secret(FAKE)
    assert stored.token.scope is TokenScope.TRADE
    assert stored.account == "123456"
    assert [path.name for path in userdata.iterdir()] == [TOKEN_FILE_NAME], (
        "после записи остался временный файл"
    )


def test_token_saves_on_a_filesystem_without_posix_permissions(
    userdata: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Флешка с exFAT или NTFS: токен сохраняется, о правах говорится правда.

    Поставка портативная, и на Linux такой носитель монтируется через udisks2
    без прав POSIX: `os.fchmod` отвечает `EPERM`. Необёрнутый вызов превращал
    это в отказ сохранить токен с текстом «проверьте свободное место и право
    записи» — при том что место есть, папка доступна, а файл только что создан
    в ней же `mkstemp`-ом. В том же файле `_ensure_directory` и `_restrict`
    этот отказ ловят и деградируют с предупреждением; `save()` не ловил.
    """
    def no_permissions(*args: Any, **kwargs: Any) -> None:
        raise PermissionError(1, "Operation not permitted")

    keeper = TokenStore(userdata)
    monkeypatch.setattr(os, "fchmod", no_permissions, raising=False)
    monkeypatch.setattr(os, "chmod", no_permissions)

    with caplog.at_level(logging.WARNING, logger="broker"):
        stored = keeper.save(Secret(FAKE), TokenScope.TRADE, issued_at=ISSUED)

    assert keeper.path.is_file(), "токен не сохранён на носителе без прав POSIX"
    assert stored.permissions_enforced is False, "программа сочла права выставленными"
    messages = [record.getMessage() for record in caplog.records]
    assert any("не выставлены" in line for line in messages), messages
    assert not any(FAKE in line for line in messages)

    monkeypatch.undo()
    assert keeper.load().token.secret == Secret(FAKE)


def test_no_file_descriptor_leaks_when_writing_fails(
    userdata: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отказ до `os.fdopen` не оставляет открытый дескриптор.

    `unlink` убирает имя, но не дескриптор, а `ResourceWarning` на сыром fd
    не выдаётся — настройка `filterwarnings = ["error"]` такую утечку
    не поймает. Считаем дескрипторы напрямую.
    """
    if not pathlib.Path("/proc/self/fd").is_dir():
        pytest.skip("счёт дескрипторов через /proc — только Linux")

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError(28, "No space left on device")

    keeper = TokenStore(userdata)
    keeper.save(Secret(FAKE), TokenScope.TRADE)  # каталог уже создан
    monkeypatch.setattr(os, "fdopen", boom)

    before = len(os.listdir("/proc/self/fd"))
    for _ in range(20):
        with pytest.raises(TokenFileError):
            keeper.save(Secret(FAKE), TokenScope.TRADE)
    after = len(os.listdir("/proc/self/fd"))

    assert after <= before + 1, (
        f"дескрипторы утекают: было {before}, стало {after} после 20 отказов"
    )


def test_windows_says_in_the_log_that_permissions_are_not_set(
    userdata: pathlib.Path, pretend_windows: None, caplog: pytest.LogCaptureFixture
) -> None:
    """О невыставленных правах говорится **на обеих** ветках, а не на одной.

    Поле `permissions_enforced` сегодня не читает никто: проброс в окно —
    слой `ui/`, он вне этой правки. Пока предупреждения на экране нет,
    единственное место, где это видно, — технический лог. До правки записи
    не было ни одной: `log().warning` стоял только в ветке «права разъехались
    и сужены», то есть на Windows программа молчала всегда.
    """
    keeper = TokenStore(userdata)

    with caplog.at_level(logging.WARNING, logger="broker"):
        keeper.save(Secret(FAKE), TokenScope.TRADE)
        on_save = [record.getMessage() for record in caplog.records]
        caplog.clear()
        keeper.load()
        on_load = [record.getMessage() for record in caplog.records]

    assert any("права файла токена не выставлены" in line for line in on_save), on_save
    assert any("права файла токена не выставлены" in line for line in on_load), on_load
    assert not any(FAKE in line for line in on_save + on_load)


@on_posix
def test_posix_does_not_cry_wolf_about_permissions(
    userdata: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """На POSIX права выставляются по-настоящему — предупреждения быть не должно.

    Предупреждение, появляющееся всегда, читается как шум и перестаёт
    работать ровно тогда, когда оно нужно.
    """
    keeper = TokenStore(userdata)
    with caplog.at_level(logging.WARNING, logger="broker"):
        keeper.save(Secret(FAKE), TokenScope.TRADE)
        keeper.load()
    assert not [
        record for record in caplog.records if "не выставлены" in record.getMessage()
    ], [record.getMessage() for record in caplog.records]


# --- запись и чтение ---


def test_round_trip(userdata: pathlib.Path) -> None:
    keeper = TokenStore(userdata)
    keeper.save(Secret(FAKE), TokenScope.TRADE, issued_at=ISSUED, account="123456")

    stored = keeper.load()
    assert stored.token.secret == Secret(FAKE)
    assert stored.token.scope is TokenScope.TRADE
    assert stored.token.issued_at == ISSUED
    assert stored.account == "123456"


def test_replacing_the_token_overwrites(userdata: pathlib.Path) -> None:
    """Кнопка «заменить токен» — это та же запись поверх старого файла."""
    keeper = TokenStore(userdata)
    keeper.save(Secret(FAKE), TokenScope.READ_ONLY)
    keeper.save(Secret(OTHER), TokenScope.TRADE)

    stored = keeper.load()
    assert stored.token.secret == Secret(OTHER)
    assert stored.token.scope is TokenScope.TRADE
    body = keeper.path.read_text(encoding="utf-8")
    assert FAKE not in body, "прежний токен остался в файле"


def test_write_leaves_no_temporary_files(userdata: pathlib.Path) -> None:
    """Временный файл записи не переживает вызов: в нём тот же токен."""
    keeper = TokenStore(userdata)
    keeper.save(Secret(FAKE), TokenScope.TRADE)
    assert [path.name for path in userdata.iterdir()] == [TOKEN_FILE_NAME]


def test_failure_to_create_the_file_keeps_the_token_out_of_the_frame(
    userdata: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отказ на создании файла: человеческий текст и ни следа токена в кадре.

    Путь — «владелец счёта первый раз вставил токен», причины будничные:
    папка только на чтение, кончилось место, файл держит антивирус.
    `_ensure_directory` их не ловит: `mkdir(exist_ok=True)` на готовом
    каталоге успешен и записываемость не проверяет.

    Отказ подставлен, а не устроен по-настоящему, и это вынужденно: сузить
    права каталогу для проверки нельзя — `_ensure_directory` возвращает ему
    `0700` первым же действием, — а кончиться месту на диске машины
    по требованию теста нечем.

    До правки `payload` и `body` — то есть весь JSON с токеном строкой —
    собирались **до** `try`, а `del` стоял в `finally`. Отказ `mkstemp`
    уходил необёрнутым, `finally` не отрабатывал, и `capture_locals=True`
    печатал значение целиком.
    """
    def refuse(**kwargs: Any) -> Any:
        raise PermissionError(13, "Permission denied")

    keeper = TokenStore(userdata)
    monkeypatch.setattr("broker.token_store.tempfile.mkstemp", refuse)

    with pytest.raises(TokenFileError) as caught:
        keeper.save(Secret(FAKE), TokenScope.TRADE)

    error = caught.value
    assert "право записи" in error.human
    assert "mkstemp" in error.technical
    rendered = "".join(
        traceback.TracebackException.from_exception(error, capture_locals=True).format()
    )
    assert FAKE not in rendered, f"значение токена в переменных кадра:\n{rendered}"
    assert FAKE[:12] not in rendered, f"начало значения в переменных кадра:\n{rendered}"
    assert not keeper.exists()


def test_write_failure_speaks_human_and_leaves_no_temporary_file(
    userdata: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отказ на самой записи — тоже человеческий текст, а не `OSError` наружу.

    `load()` отказы ввода-вывода оборачивал, `save()` — нет. А место на диске
    кончается ровно в тот момент, когда владелец счёта вставляет токен:
    «ENOSPC» в окне вместо объяснения — это отказ программы говорить с ним
    на его языке.

    Отказ подставлен в `os.fsync`: он случается после того, как значение
    токена уже превращено в строку, то есть проверяет заодно и очистку
    переменных кадра на пути отказа.
    """
    def full_disk(descriptor: int) -> None:
        raise OSError(28, "No space left on device")

    keeper = TokenStore(userdata)
    monkeypatch.setattr(os, "fsync", full_disk)

    with pytest.raises(TokenFileError) as caught:
        keeper.save(Secret(FAKE), TokenScope.TRADE)

    error = caught.value
    assert "свободное место" in error.human
    assert "OSError" in error.technical
    assert list(userdata.iterdir()) == [], "временный файл с токеном пережил отказ"

    rendered = "".join(
        traceback.TracebackException.from_exception(error, capture_locals=True).format()
    )
    assert FAKE not in rendered, f"значение токена в переменных кадра:\n{rendered}"
    assert FAKE[:12] not in rendered, f"начало значения в переменных кадра:\n{rendered}"


def test_issue_date_must_have_a_timezone(userdata: pathlib.Path) -> None:
    """Наивное время дало бы отсчёт срока, зависящий от зоны машины."""
    keeper = TokenStore(userdata)
    with pytest.raises(ValueError):
        keeper.save(Secret(FAKE), TokenScope.TRADE, issued_at=datetime(2026, 6, 1))


# --- поломки ---


def test_missing_file_says_what_to_do(userdata: pathlib.Path) -> None:
    keeper = TokenStore(userdata)
    with pytest.raises(TokenFileError) as caught:
        keeper.load()
    text = caught.value.human
    assert "настройк" in text.lower()
    assert "БКС" in text


def test_directory_in_place_of_the_token_file(userdata: pathlib.Path) -> None:
    """На месте файла каталог: отказ понятный, права каталогу не правятся.

    `Path.exists()` истинно и для каталога. До правки программа считала его
    файлом с «разъехавшимися правами», выставляла **каталогу** `0600`
    и только потом спотыкалась о `IsADirectoryError` при чтении.
    Публичный `exists()` этого класса спрашивает `is_file()` — внутри `load`
    условие обязано быть тем же.
    """
    keeper = TokenStore(userdata)
    keeper.path.mkdir()
    before = mode(keeper.path)

    with pytest.raises(TokenFileError) as caught:
        keeper.load()

    assert "не файл" in caught.value.human
    assert "не обычный файл" in caught.value.technical
    if not windows():
        assert mode(keeper.path) == before, "программа выставила права каталогу"


@on_posix
def test_symlink_in_place_of_the_token_file(
    userdata: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """Ссылка на месте файла: правка прав не должна уходить наружу `userdata/`.

    `stat()` идёт по ссылке, поэтому «права разъехались» считалось по цели,
    и `chmod` сужал права **чужому файлу** вне папки программы. Читается
    как мелочь ровно до того дня, когда ссылка укажет на что-то нужное.
    """
    outsider = tmp_path / "outsider.txt"
    outsider.write_text("это не файл токена", encoding="utf-8")
    os.chmod(outsider, 0o644)

    keeper = TokenStore(userdata)
    keeper.path.symlink_to(outsider)

    with pytest.raises(TokenFileError) as caught:
        keeper.load()

    assert "не файл" in caught.value.human
    assert mode(outsider) == 0o644, "программа поправила права файлу вне userdata/"


def test_unreadable_file_properties_are_reported_humanly(
    userdata: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Файл есть, а свойства не читаются — человеческий отказ, а не голый `OSError`.

    Это же место закрывает гонку «файл исчез между проверкой и чтением»:
    прежде `exists()` и `stat()` были двумя разными обращениями к диску,
    и между ними файл успевал пропасть — замена токена из другого окна,
    чистильщик диска. `Path.exists()` при отказе прав молча отвечает «нет»,
    и владелец счёта читал «токен ещё не введён» на введённом токене.
    """
    keeper = TokenStore(userdata)
    keeper.save(Secret(FAKE), TokenScope.TRADE)

    def refuse(self: pathlib.Path) -> Any:
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(pathlib.Path, "lstat", refuse)

    with pytest.raises(TokenFileError) as caught:
        keeper.load()

    assert "Не удалось прочитать" in caught.value.human
    assert "PermissionError" in caught.value.technical


def test_broken_json(userdata: pathlib.Path) -> None:
    keeper = TokenStore(userdata)
    keeper.path.write_text("{это не json", encoding="utf-8")
    with pytest.raises(TokenFileError) as caught:
        keeper.load()
    assert "испорчен" in caught.value.human


def test_file_in_another_encoding_is_refused_like_any_other_breakage(
    userdata: pathlib.Path,
) -> None:
    """Файл токена не в UTF-8 — штатный отказ, а не падение стартового пути.

    `UnicodeDecodeError` — подкласс `ValueError`, а не `OSError`: прежний
    перехват его не видел, и на первом же запуске программы исключение
    уходило наружу необёрнутым, мимо всей чистки. Путь стартовый: файл
    в чужой кодировке появляется от правки вручную в «Блокноте» на русской
    Windows и от битой копии.
    """
    keeper = TokenStore(userdata)
    keeper.path.write_bytes(CP1251_FILE)

    with pytest.raises(TokenFileError) as caught:
        keeper.load()

    error = caught.value
    assert "кодировке" in error.human
    assert "БКС" in error.human
    assert str(userdata) not in error.human
    for word in ("Error", "Traceback", "JSON", "None", "codec", "utf"):
        assert word not in error.human, f"техническое слово «{word}»: {error.human}"

    assert "UnicodeDecodeError" in error.technical
    assert str(keeper.path) in error.technical


def test_encoding_failure_carries_no_chained_exception(userdata: pathlib.Path) -> None:
    """Цепочка исключений пуста — иначе она тащит за собой весь файл.

    `raise ... from None` обнуляет `__cause__`, но `__context__` при этом
    продолжает указывать на исходное исключение, а у `UnicodeDecodeError`
    в `object` и в `args` лежит **весь прочитанный буфер**, то есть файл
    с токеном целиком. Поэтому отказ поднимается за пределами `except`.
    """
    keeper = TokenStore(userdata)
    keeper.path.write_bytes(CP1251_FILE)

    with pytest.raises(TokenFileError) as caught:
        keeper.load()

    assert caught.value.__cause__ is None, "исходное исключение осталось причиной"
    assert caught.value.__context__ is None, "исходное исключение осталось контекстом"


@pytest.mark.parametrize("shape", sorted(FORMATS))
def test_unknown_format_version(userdata: pathlib.Path, shape: str) -> None:
    """Файл от будущей версии не разбирается наполовину."""
    keeper = TokenStore(userdata)
    keeper.path.write_text(body({**WHOLE_FILE, "version": 99}, shape), encoding="utf-8")
    with pytest.raises(TokenFileError) as caught:
        keeper.load()
    assert "версией программы" in caught.value.human
    assert "99" in caught.value.technical


@pytest.mark.parametrize("shape", sorted(FORMATS))
def test_unknown_scope(userdata: pathlib.Path, shape: str) -> None:
    keeper = TokenStore(userdata)
    keeper.path.write_text(
        body({**WHOLE_FILE, "scope": "какие-то другие"}, shape), encoding="utf-8"
    )
    with pytest.raises(TokenFileError) as caught:
        keeper.load()
    assert "тип токена" in caught.value.human


@pytest.mark.parametrize("shape", sorted(FORMATS))
def test_empty_token_value(userdata: pathlib.Path, shape: str) -> None:
    keeper = TokenStore(userdata)
    keeper.path.write_text(body({**WHOLE_FILE, "token": "   "}, shape), encoding="utf-8")
    with pytest.raises(TokenFileError) as caught:
        keeper.load()
    assert "нет токена" in caught.value.human


@pytest.mark.parametrize("shape", sorted(FORMATS))
def test_broken_issue_date(userdata: pathlib.Path, shape: str) -> None:
    """Без даты выпуска нельзя сказать, когда кончится срок."""
    keeper = TokenStore(userdata)
    keeper.path.write_text(
        body({**WHOLE_FILE, "issued": "позавчера"}, shape), encoding="utf-8"
    )
    with pytest.raises(TokenFileError) as caught:
        keeper.load()
    assert "дата выпуска" in caught.value.human


def test_delete_is_idempotent(userdata: pathlib.Path) -> None:
    keeper = TokenStore(userdata)
    keeper.delete()
    keeper.save(Secret(FAKE), TokenScope.TRADE)
    keeper.delete()
    keeper.delete()
    assert not keeper.exists()


def test_delete_of_a_directory_speaks_human(userdata: pathlib.Path) -> None:
    """Кнопка «заменить токен» объясняет отказ, а не роняет окно.

    `delete()` оборачивал только `FileNotFoundError`. Каталог на месте файла
    даёт `IsADirectoryError`, занятый файл на Windows — `PermissionError`:
    тот самый случай, ради которого чинили `load`.
    """
    keeper = TokenStore(userdata)
    keeper.path.mkdir()

    with pytest.raises(TokenFileError) as caught:
        keeper.delete()

    assert "не смогла убрать" in caught.value.human
    assert "unlink" in caught.value.technical


def test_every_failure_speaks_human(userdata: pathlib.Path) -> None:
    """Сообщение владельцу счёта не содержит английских кодов и путей.

    Проверка грубая, но ловит главное: сообщение, склеенное из текста
    исключения Python, здесь не пройдёт.
    """
    keeper = TokenStore(userdata)
    cases = ["{сломано", '{"version": 42}', '{"версия": 42}']
    for body in cases:
        keeper.path.write_text(body, encoding="utf-8")
        with pytest.raises(BrokerError) as caught:
            keeper.load()
        human = caught.value.human
        assert str(userdata) not in human, f"path в сообщении: {human}"
        for word in ("Error", "Traceback", "JSON", "None"):
            assert word not in human, f"техническое слово «{word}» в сообщении: {human}"


def test_technical_text_keeps_the_path(userdata: pathlib.Path) -> None:
    """Путь нужен технику — он и уходит в технический текст."""
    keeper = TokenStore(userdata)
    with pytest.raises(TokenFileError) as caught:
        keeper.load()
    assert str(keeper.path) in caught.value.technical


def test_expiry_is_counted_from_the_saved_date(userdata: pathlib.Path) -> None:
    """Срок считается от даты выпуска, а не от даты чтения файла."""
    keeper = TokenStore(userdata)
    keeper.save(Secret(FAKE), TokenScope.TRADE, issued_at=ISSUED)
    stored = keeper.load()
    assert stored.token.declared_expires_at == ISSUED + timedelta(days=90)


# --- имена полей в файле ---


def test_the_table_of_previous_names_covers_every_field() -> None:
    """Каждому полю файла отвечает имя прежнего формата, и лишних нет.

    Таблица `LEGACY_KEYS` — единственное место, где перечислены прежние имена:
    по ней читается файл владельца счёта и по ней же пишется строка в журнал.
    Забытое поле означало бы, что оно молча не прочитается из старого файла,
    а прогон при этом останется зелёным — ровно тот дефект, ради которого
    ветвление и сводят в таблицу.
    """
    saved = TokenStore.__module__  # имя слоя нужен только для внятного текста
    fields = set(WHOLE_FILE)
    assert set(LEGACY_KEYS) == fields, (
        f"{saved}: таблица прежних имён разошлась с составом файла: "
        f"нет имени для {sorted(fields - set(LEGACY_KEYS))}, "
        f"лишние {sorted(set(LEGACY_KEYS) - fields)}"
    )
    for name, previous in LEGACY_KEYS.items():
        assert name.isascii(), f"нынешнее имя поля не латиницей: {name!r}"
        assert not previous.isascii(), f"прежнее имя поля не русское: {previous!r}"


def test_saved_file_uses_latin_field_names(userdata: pathlib.Path) -> None:
    """Правило 6: имена полей в файле — латиницей. Смотрим файл, не константы.

    Проверка идёт по тому, что легло на диск: тест на константы модуля прошёл
    бы и в случае, когда `save` пишет что-то своё.
    """
    keeper = TokenStore(userdata)
    keeper.save(Secret(FAKE), TokenScope.TRADE, issued_at=ISSUED, account="БКС-777")

    written = json.loads(keeper.path.read_text(encoding="utf-8"))
    assert sorted(written) == ["account", "issued", "scope", "token", "version"]
    for name in written:
        assert name.isascii(), f"имя поля не латиницей: {name!r}"
    for previous in LEGACY_KEYS.values():
        assert previous not in written, f"в новом файле осталось имя {previous!r}"


def test_a_file_in_the_previous_format_is_still_read(userdata: pathlib.Path) -> None:
    """⛔ Главный сторож правки: файл с русскими именами полей читается целиком.

    Такой файл лежит у владельца счёта на диске **прямо сейчас**, и в нём
    боевой токен. Отказ его прочитать — это не «программа не встала», а поход
    в личный кабинет БКС за новым токеном: показывают его там один раз.

    Проверяются все пять полей, а не факт «не упало»: половина полей,
    прочитанная как `None`, дала бы отказ уже дальше по пути — с текстом,
    в котором про формат файла ничего не сказано.
    """
    keeper = TokenStore(userdata)
    keeper.path.write_text(body(WHOLE_FILE, "прежний"), encoding="utf-8")

    stored = keeper.load()

    assert stored.token.secret == Secret(FAKE)
    assert stored.token.scope is TokenScope.TRADE
    assert stored.token.issued_at == ISSUED
    assert stored.account == "БКС-777"


def test_reading_the_previous_format_does_not_touch_the_file(
    userdata: pathlib.Path,
) -> None:
    """Чтение чужого файла его не переписывает — ни содержимым, ни временем.

    Правка на чтении означала бы, что чтение умеет отказать (носитель только
    для чтения, флешка, антивирус), а испорченный файл токена стоит владельцу
    счёта нового токена. Нынешний формат появляется при следующей записи.
    """
    keeper = TokenStore(userdata)
    keeper.path.write_text(body(WHOLE_FILE, "прежний"), encoding="utf-8")
    before = keeper.path.read_bytes()

    keeper.load()

    assert keeper.path.read_bytes() == before, "файл владельца счёта переписан"
    assert [path.name for path in userdata.iterdir()] == [TOKEN_FILE_NAME], (
        "после чтения в папке появился лишний файл"
    )


def test_reading_the_previous_format_is_said_in_the_journal(
    userdata: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Переход формата виден человеку строкой в журнале — и без значений."""
    keeper = TokenStore(userdata)
    keeper.path.write_text(body(WHOLE_FILE, "прежний"), encoding="utf-8")

    with caplog.at_level(logging.INFO, logger="broker"):
        keeper.load()

    said = [record.getMessage() for record in caplog.records if "формате" in record.getMessage()]
    assert said, f"о прежнем формате не сказано ничего: {caplog.text}"
    told = "\n".join(said)
    assert "токен" in told, "не названо имя поля, по которому файл опознан"
    assert FAKE not in told, f"значение токена ушло в журнал: {told}"


def test_the_current_format_says_nothing_about_the_previous_one(
    userdata: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """На обычном файле сообщения о прежнем формате нет.

    Иначе строка появлялась бы в журнале при каждом запуске и перестала бы
    что-либо значить.
    """
    keeper = TokenStore(userdata)
    keeper.save(Secret(FAKE), TokenScope.TRADE, issued_at=ISSUED)

    with caplog.at_level(logging.INFO, logger="broker"):
        keeper.load()

    assert "прежнем формате" not in caplog.text, caplog.text


def test_saving_over_a_previous_format_file_writes_latin_names(
    userdata: pathlib.Path,
) -> None:
    """Замена токена переводит файл в нынешний формат целиком, без остатка."""
    keeper = TokenStore(userdata)
    keeper.path.write_text(body(WHOLE_FILE, "прежний"), encoding="utf-8")

    keeper.save(Secret(OTHER), TokenScope.READ_ONLY, issued_at=ISSUED, account="БКС-777")

    written = json.loads(keeper.path.read_text(encoding="utf-8"))
    assert sorted(written) == ["account", "issued", "scope", "token", "version"]
    assert keeper.load().token.secret == Secret(OTHER)


def test_both_names_at_once_prefer_the_current_one(userdata: pathlib.Path) -> None:
    """Оба имени в одном файле: верным считается нынешнее.

    Так выглядит ручная правка поверх старого файла. Разбор обязан быть
    определённым, а не зависеть от порядка ключей в JSON.
    """
    keeper = TokenStore(userdata)
    keeper.path.write_text(MIXED_FILE, encoding="utf-8")

    stored = keeper.load()

    assert stored.token.secret == Secret(OTHER)
    assert stored.token.scope is TokenScope.READ_ONLY


def test_the_current_name_wins_even_when_it_is_the_broken_one(
    userdata: pathlib.Path,
) -> None:
    """Приоритет имён задан кодом, а не тем, какое значение разобралось.

    В файле оба имени: под нынешним — пустое значение, под прежним — годное.
    Разбор обязан отказать. Иначе правило «нынешнее имя главнее» на деле
    означало бы «главнее то, что получилось прочитать», и один и тот же файл
    читался бы по-разному в зависимости от того, что в нём испорчено.

    Заодно проверяется, что ни одно из двух значений не уехало в отказ:
    словарь разобранного файла живёт в кадрах `load` и `_build`, и оба
    попадают в трассировку.
    """
    keeper = TokenStore(userdata)
    keeper.path.write_text(MIXED_EMPTY_FILE, encoding="utf-8")

    with pytest.raises(TokenFileError) as caught:
        keeper.load()

    assert "нет токена" in caught.value.human
    printed = "".join(
        traceback.TracebackException.from_exception(
            caught.value, capture_locals=True
        ).format()
    )
    for leftover in (FAKE, OTHER):
        assert leftover not in printed, f"значение осталось в кадре:\n{printed}"
