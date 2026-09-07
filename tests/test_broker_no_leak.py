"""Доказательство, что токен не утекает через слой `broker/`.

Требование ТЗ §4.1, `DOMAIN.md` §6 и `CLAUDE.md`: токен не попадает
в технический лог, журнал решений, журнал сделок, экспортируемые отчёты,
текст исключения и трассировку — ни целиком, ни частично, ни первыми
символами.

Проверяется это здесь **прогоном**, а не чтением кода. Каждый тест
устроен одинаково: сквозь слой прогоняется поддельный токен с приметным
значением, после чего вся продукция слоя — текст исключения, трассировка,
записи журнала, `repr` объектов — обыскивается на это значение.

Отдельно стоит `test_search_finds_a_planted_leak`: он подкладывает утечку
нарочно и требует, чтобы поиск её нашёл. Без него весь файл мог бы
выродиться в зелёный прогон, ничего не проверяющий, — и это ровно тот
отказ, который в проекте страшнее отсутствия теста.

Настоящий токен здесь не участвует ни в каком виде: только приметные
поддельные строки, заведомо не похожие на выданные брокером.
"""

from __future__ import annotations

import ast
import asyncio
import gc
import io
import json
import logging
import os
import pathlib
import subprocess
import sys
import traceback
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Iterator

import httpx
import pytest
import websockets.exceptions

from broker import stream
from broker.errors import BrokerError, NoConnection, from_status
from broker.redaction import (
    FOREIGN_LOGGERS,
    LOGGER_NAME,
    MASK,
    REGISTRY,
    RedactingFilter,
    RedactingFormatter,
    install,
    log,
    scrub,
)
from broker.secret import Secret
from broker.session import AUTH_URL, PORTFOLIO_PATH, BrokerSession
from broker.stream import StreamRejected, candle_stream, snapshot_of
from broker.token_store import LEGACY_KEYS, TokenStore
from broker.tokens import TokenScope

# Приметные подделки. Ни одна из них не является токеном брокера: они
# нарочно написаны так, чтобы их нельзя было принять за настоящий,
# и при этом состоят из тех же символов, что настоящий, — иначе проверка
# шла бы по значению, которое до HTTP-заголовка вообще не доходит.
FAKE_REFRESH = "FAKE-refresh-ZZZZ-0000-1111-2222-NOT-A-REAL-TOKEN"
FAKE_ACCESS = "FAKE-access-YYYY-3333-4444-5555-NOT-A-REAL-TOKEN"

#: Файл токена, записанный не в UTF-8: русская Windows, «Блокнот», кодировка
#: ANSI, битая копия. Русские имена полей в cp1251 — это и есть байты,
#: на которых спотыкается разбор UTF-8.
#:
#: Константа модуля, а не локальная переменная теста: кадр теста входит
#: в трассировку отказа, и локальная нашлась бы там сама по себе — проверка
#: ловила бы собственный мусор вместо утечки слоя.
CP1251_TOKEN_FILE = json.dumps(
    {
        "версия": 1,
        "права": "trade-api-write",
        "выпущен": "2026-06-01T12:00:00+00:00",
        "токен": FAKE_REFRESH,
    },
    ensure_ascii=False,
).encode("cp1251")

#: Не строка, а байты — ровно то, что придёт из файла, прочитанного до
#: декодирования. Тоже константа модуля и по той же причине.
FAKE_BYTES_TOKEN = b"FAKE-bytes-6666-7777-NOT-A-REAL-TOKEN"


def _token_file(previous: bool = False, *, value: str = FAKE_REFRESH, **broken: Any) -> str:
    """Тело файла токена с приметным значением и одним испорченным полем.

    `previous=True` даёт формат до 06.09.2026 — имена полей по-русски. Ровно
    такой файл лежит у владельца счёта на диске, и утечки на его путях разбора
    стоят столько же, сколько на нынешних. Имена берутся из таблицы слоя,
    а не переписываются здесь: забытое поле выпало бы из проверки молча.
    """
    fields: dict[str, Any] = {
        "version": 1,
        "scope": "trade-api-write",
        "issued": "2026-06-01T12:00:00+00:00",
        "token": value,
    }
    fields.update(broken)
    if previous:
        fields = {LEGACY_KEYS[name]: value for name, value in fields.items()}
    return json.dumps(fields, ensure_ascii=False)


#: Файлы, на которых разбор спотыкается **после** успешного разбора JSON.
#: На каждом из них значение токена уже лежит в разобранном словаре, а словарь
#: живёт в кадрах `load` и `_build` — то есть в трассировке отказа.
#: Константы модуля: кадр теста тоже входит в трассировку.
#:
#: Каждый случай — в обоих форматах файла: нынешнем и прежнем.
BROKEN_TOKEN_FILES: dict[str, str] = {
    f"{case}, формат {shape}": body
    for shape, previous in (("нынешний", False), ("прежний", True))
    for case, body in {
        "чужой тип прав": _token_file(previous, scope="какие-то другие"),
        "битая дата выпуска": _token_file(previous, issued="позавчера"),
        "чужая версия формата": _token_file(previous, version=99),
        "тело не объект": json.dumps([_token_file(previous)], ensure_ascii=False),
    }.items()
}

#: Файл, на котором спотыкается сам разбор JSON. `json.JSONDecodeError` держит
#: **весь текст файла** в атрибуте `doc`. Формат прежний: именно на нём образец
#: чистки обязан узнать русское имя поля.
BROKEN_JSON_FILE: str = _token_file(previous=True) + " и лишний мусор в конце"

#: То же нынешним форматом.
BROKEN_JSON_FILE_NOW: str = _token_file() + " и лишний мусор в конце"

#: Оба под именами. ⚠️ Перебор идёт по **имени**, а тело берётся из словаря:
#: параметр теста — такая же локальная переменная кадра, как любая другая,
#: и файл, отданный в `parametrize` телом, уехал бы и в переменные кадра,
#: и в имя теста в отчёте прогона.
BROKEN_JSON_FILES: dict[str, str] = {
    "прежний формат": BROKEN_JSON_FILE,
    "нынешний формат": BROKEN_JSON_FILE_NOW,
}

#: Имена поля с токеном в нашем файле: нынешнее и прежнее. Пара выписана
#: здесь, а не выведена из слоя, потому что проверять надо именно её —
#: образцы чистки не умеют спрашивать у `token_store`, как называется поле.
#: Связь с таблицей слоя стережёт `test_the_token_field_names_are_the_layers_own`.
TOKEN_FIELD_NAMES: tuple[str, str] = ("token", "токен")

#: Подделка **только для проверки образцов**. Она никогда не оборачивается
#: в `Secret`, поэтому реестр чистки её не знает: текст, из которого она
#: пропала, вычищен именно образцом, а не по значению.
#:
#: ⚠️ Без этого сторож умирает молча. Проверено мутацией 06.09.2026: с русским
#: именем поля, убранным из `redaction._BARE_FIELDS`, прогон файла целиком
#: оставался **зелёным**, а тот же тест в одиночку падал. Причина — общий
#: реестр процесса: соседние тесты держат живой `Secret(FAKE_REFRESH)`,
#: реестр затирает это значение по значению, и проверка образцов зеленеет
#: при выключенных образцах.
PATTERN_ONLY: str = "FAKE-pattern-8888-9999-0000-NOT-A-REAL-TOKEN"

#: Битый JSON с этой подделкой — обоими форматами файла.
PATTERN_ONLY_FILES: dict[str, str] = {
    "прежний формат": _token_file(True, value=PATTERN_ONLY) + " и лишний мусор",
    "нынешний формат": _token_file(value=PATTERN_ONLY) + " и лишний мусор",
}

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BROKER = REPO_ROOT / "broker"


def moment(**shift: float) -> datetime:
    return datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc) + timedelta(**shift)


@pytest.fixture(autouse=True)
def secret_registry_is_not_the_weather() -> Iterator[None]:
    """Состав реестра чистки к началу теста — постановка, а не погода.

    **Зачем.** Половина проверок этого файла спрашивает не «текст чистый»,
    а «текст вычищен **образцом**»: `Bearer …`, имя поля, форма JWT. Реестр
    чистки (`broker.redaction.REGISTRY`) вырезает текст **по значению**,
    и чужое значение, задержавшееся в нём, зеленит такую проверку при
    полностью снятом образце. Ровно этим два сторожа файла зеленели
    в компании и падали в одиночку (`D-078`, 06.09.2026).

    Значение берётся под охрану в `Secret.__init__` и снимается финализатором
    слабой ссылки — то есть когда объект умер. Умирает он не сразу: `Secret`
    живёт внутри `BrokerSession`, сессия — в кадре, кадр — в трассировке
    отказа. Такой узел развязывает только сборщик мусора, а он приходит
    когда захочет. Замер 06.09.2026 на этом файле: реестр непуст к началу
    **24 тестов из 87**, и после сборки — ни одного.

    **Что делает фикстура, двумя раздельными действиями.**

    1. Зовёт сборку, если реестру есть что отдать. Цена замерена самим
       pytest по времени установки фикстуры: **0,31 с на 90 вызовов**,
       три повтора дали 0,305, 0,317 и 0,304. Один `gc.collect()` — 10 мс
       в лёгком процессе и 17 мс в воркере, куда уже загружен Qt.
       **Заявленных 6,7 с не подтвердилось, разница двадцатикратная**;
       разбор замера — в отчёте задачи.
    2. Проверяет, что под охраной нет значений, которые этот файл обязан
       чистить **образцом**. Их два, и оба существуют только здесь:
       `PATTERN_ONLY` и JWT из `tests/fixtures/`. Заверни любое из них
       в `Secret` — и восемь проверок образцов начнут зеленеть от реестра,
       ничего не проверяя.

    ⚠️ **Почему проверяется не «реестр пуст».** Строгая форма написана,
    прогнана и работает — она ловит `D-078` пробой (без неё прогон
    с подложенным контейнером уровня модуля даёт «89 passed» и молчит,
    с ней — одну громкую ошибку). Ставить её нельзя по двум замерам:

    * она валит **чужие** файлы в свой цвет. `tests/test_broker_candles.py`
      и `tests/test_broker_stream.py` оставляют по значению под охраной
      до конца процесса — тот же механизм, что в `D-078`. Под `-n auto`
      с раздачей по тестам это красный прогон по жребию, а причина
      в файлах, которые правит не эта задача;
    * при **любой** настоящей поломке она превращает прогон в лавину:
      «1 failed, 72 errors» вместо «6 failed», и пять настоящих поломок
      из шести исчезают под собственным сторожем. Упавший тест держит
      секрет своей трассировкой, трассировку держит отчёт pytest.

    Узкая форма от обоих замеров свободна по построению: чужой файл
    этих двух значений не знает, а лавины нет, потому что своих `Secret`
    с ними не заводит никто.
    """
    from broker.redaction import REGISTRY

    if REGISTRY.known():
        gc.collect()

    guarded = [
        name
        for name, value in (
            ("PATTERN_ONLY", PATTERN_ONLY),
            ("JWT из фикстуры", _answer_samples()["ответ_авторизации"]["access_token"]),
        )
        if value in REGISTRY.known()
    ]
    # В сообщении — имена, а не значения: реестр по построению держит то,
    # что показывать нельзя (правило 7). Здесь подделки, но приём,
    # печатающий содержимое реестра, скопируют туда, где значения настоящие.
    assert not guarded, (
        f"под охраной реестра значения, которые обязан вырезать образец: {guarded}. "
        "Проверки образцов в этом файле зеленели бы по значению, а не "
        "по образцу, — то есть не проверяли бы ничего (D-078)"
    )
    yield


@pytest.fixture
def userdata(tmp_path: pathlib.Path) -> pathlib.Path:
    directory = tmp_path / "userdata"
    directory.mkdir()
    return directory


@pytest.fixture
def store(userdata: pathlib.Path) -> TokenStore:
    keeper = TokenStore(userdata)
    keeper.save(Secret(FAKE_REFRESH), TokenScope.TRADE, issued_at=moment(days=-1))
    return keeper


@pytest.fixture
def journal() -> Iterator[tuple[logging.Handler, io.StringIO]]:
    """Обработчик журнала, собирающий всё, что слой пишет, включая трассировки.

    Форматтер — тот же `RedactingFormatter`, которым собирается технический
    лог программы. Фильтр слоя ставится в `BrokerSession.__init__`.
    """
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(RedactingFormatter("%(levelname)s %(message)s"))
    handler.setLevel(logging.DEBUG)

    logger = log()
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield handler, buffer
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


#: Негодный рабочий токен: пробел внутри. Константа модуля, а не локальная
#: переменная теста — иначе проверка находит значение в собственном кадре.
FAKE_BAD_ACCESS = "FAKE-access ПРОБЕЛ-NOT-A-REAL-TOKEN"


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def leaked(haystack: str) -> list[str]:
    """Куски подделок, найденные в тексте.

    Ищется не только целое значение, но и **начало** — «ни первыми символами»
    из требования. Двенадцати символов достаточно, чтобы отличить утечку
    от случайного совпадения и поймать маску вида `FAKE-refresh…`.
    """
    found: list[str] = []
    for secret in (FAKE_REFRESH, FAKE_ACCESS):
        if secret in haystack:
            found.append(f"целиком: {secret[:6]}…")
        elif secret[:12] in haystack:
            found.append(f"первые символы: {secret[:6]}…")
    return found


def leaked_file_content(haystack: str) -> list[str]:
    """Куски файла токена в чужой кодировке, найденные в тексте.

    Ищется двумя способами. Первый — обычный `leaked`: значение токена лежит
    в файле латиницей и в cp1251 выглядит теми же байтами, что в UTF-8.
    Второй — escape-запись начала файла: так буфер выглядит, когда его
    печатает `repr` байтовой строки, а печатался он именно так — в `repr`
    исключения, в его `args` и в переменных кадра `codecs`, попадающих
    в трассировку.
    """
    found = leaked(haystack)
    escaped = repr(CP1251_TOKEN_FILE)[2:-1]  # содержимое без обрамления b'…'
    for length in (32, 16):
        piece = escaped[:length]
        if piece in haystack:
            found.append(f"байты файла: {piece[:16]}…")
            break
    return found


# --- канарейка: поиск действительно ищет ---


def test_search_finds_a_planted_leak() -> None:
    """Подложенная утечка обязана находиться — иначе весь файл вакуумен."""
    assert leaked(f"в журнале оказалось {FAKE_REFRESH}"), "целое значение не найдено"
    assert leaked(f"замаскировали как {FAKE_ACCESS[:12]}…"), "начало значения не найдено"
    assert not leaked("обычная строка журнала без секретов"), "ложное срабатывание"


def test_file_content_search_finds_a_planted_leak() -> None:
    """То же для поиска по байтам файла: он видит напечатанный буфер.

    Без этой канарейки проверка ниже зеленела бы и на слое, который печатает
    файл с токеном целиком: искомого просто не нашлось бы по опечатке в поиске.
    """
    assert leaked_file_content("data = " + repr(CP1251_TOKEN_FILE)), "буфер не найден"
    assert leaked_file_content(f"в файле лежит {FAKE_REFRESH}"), "значение не найдено"
    assert not leaked_file_content("обычная строка журнала без секретов")


# --- сам тип ---


def test_secret_never_prints() -> None:
    """Ни один способ превратить объект в текст не выдаёт значение."""
    secret = Secret(FAKE_REFRESH)
    renderings = {
        "str": str(secret),
        "repr": repr(secret),
        "f-строка": f"{secret}",
        "формат с шириной": f"{secret:>40}",
        "%s": "%s" % secret,
        "%r": "%r" % secret,
        "format()": format(secret),
        "в списке": str([secret]),
        "в словаре": str({"токен": secret}),
        "в кортеже": repr((secret,)),
    }
    for how, rendered in renderings.items():
        assert not leaked(rendered), f"утечка через {how}: {rendered}"
        assert MASK in rendered, f"{how} не показал маску: {rendered}"


def test_secret_does_not_serialise() -> None:
    """`pickle` унёс бы значение в файл или в очередь между процессами."""
    import copy
    import pickle

    secret = Secret(FAKE_REFRESH)
    with pytest.raises(TypeError):
        pickle.dumps(secret)
    # Копирование разрешено и значение не размножает: копия — тот же объект.
    assert copy.copy(secret) is secret
    assert copy.deepcopy(secret) is secret


def test_secret_hides_its_length() -> None:
    """Длина токена — тоже сведение о нём, и она не нужна ни разу."""
    with pytest.raises(TypeError):
        len(Secret(FAKE_REFRESH))


def test_scrub_masks_unknown_tokens_from_server_answer() -> None:
    """Токен, который прислал сервер, реестру неизвестен — чистится по образцу.

    Образцы вынесены в `tests/fixtures/`: строка вида «имя поля, а следом
    длинное значение» в исходнике теста неотличима от настоящей утечки —
    и для детектора секретов в `tests/test_layers.py`, и для человека,
    читающего diff. Каталог фикстур для того и заведён.
    """
    samples = json.loads(
        (REPO_ROOT / "tests" / "fixtures" / "broker-answer-samples.json").read_text(
            encoding="utf-8"
        )
    )
    answer = json.dumps(samples["ответ_авторизации"], ensure_ascii=False)
    cleaned = scrub(answer)

    assert "FAKE-unknown-to-registry" not in cleaned, cleaned
    assert "eyJhbGciOiJSUzI1NiJ9" not in cleaned, cleaned
    assert "bearer" in cleaned, "чистка съела безобидные поля"
    assert "openid profile" in cleaned, "чистка съела права доступа"

    header = samples["заголовок_запроса"]
    assert "FAKE-header-abcdef" not in scrub(header), scrub(header)


def _answer_samples() -> dict[str, Any]:
    """Поддельные ответы сервера из `tests/fixtures/`.

    Образцы вынесены в файл, а не написаны здесь: строка вида «имя поля,
    а следом длинное значение» в исходнике теста неотличима от настоящей
    утечки — и для детектора секретов в `tests/test_layers.py`, и для
    человека, читающего правку. Каталог фикстур для того и заведён.
    """
    body = (REPO_ROOT / "tests" / "fixtures" / "broker-answer-samples.json").read_text(
        encoding="utf-8"
    )
    return dict(json.loads(body))


def test_scrub_masks_a_bare_jwt_the_shape_a_real_broker_token_has() -> None:
    """Токен формы JWT исчезает из текста, где нет ни имени поля, ни слова `Bearer`.

    Настоящий токен БКС — JWT (`bearerFormat: JWT` в таблицах документации
    брокера), и в текст он попадает не только полем ответа: из сообщения
    чужой библиотеки, из обрывка файла, из тела, которое сервер прислал
    прозой. Тогда работает единственный образец, узнающий **само значение**,
    а не его обрамление.

    ⚠️ Проверка стоит здесь, а не только в журнальном файле, и это
    не дублирование. Аудит 06.09.2026 снял строку `_JWT.sub` в
    `broker/redaction.py` и прогнал 3432 теста: покраснел ровно один,
    и тот в `tests/test_app_journal_records.py`. Файл, существующий затем,
    чтобы доказывать «из `broker/` токен не выходит», сноса целого рубежа
    не замечал ни одним из своих 81 теста.

    ⚠️ Значение берётся из фикстуры и **никем не оборачивается в `Secret`**:
    реестр чистки его не знает, вырезать текст может только образец.
    Опора на реестр дала бы зелёный цвет от соседей по файлу (`D-078`).
    """
    jwt: str = _answer_samples()["ответ_авторизации"]["access_token"]

    # Канарейка формы: подмени в фикстуре значение на обычную строку —
    # и весь перебор ниже проверял бы не тот образец, зеленея молча.
    parts = jwt.split(".")
    assert len(parts) == 3, f"в фикстуре не JWT, частей {len(parts)}: {jwt}"
    assert jwt.startswith("eyJ"), f"в фикстуре не JWT: {jwt}"
    assert all(len(part) >= 9 for part in parts), f"части JWT короче образца: {jwt}"
    assert jwt not in REGISTRY.known(), (
        "значение попало в реестр чистки — текст затрётся по значению "
        "и при выключенном образце, а образец проверять станет нечем"
    )

    naked = {
        "проза ответа": f"брокер ответил: {jwt} — разобрать не удалось",
        "слова чужой библиотеки": f"поток свечей: InvalidHandshake: sent {jwt}",
        "значение само по себе": jwt,
        "поле с чужим именем": f'{{"hint": "{jwt}"}}',
    }
    for case in sorted(naked):
        cleaned = scrub(naked[case])
        assert jwt[:12] not in cleaned, f"{case}: JWT уцелел: {cleaned}"
        assert MASK in cleaned, f"{case}: маски нет вовсе: {cleaned}"


# --- исключения и трассировки ---


def test_the_technical_text_of_a_failure_is_scrubbed_by_the_exception_itself() -> None:
    """Секрет, попавший в технический текст отказа, вырезается самим отказом.

    `technical` доезжает до человека двумя дорогами сразу: техническим логом
    и пояснением отчёта о загрузке — `app/backfill` кладёт `failure.technical`
    в базу, а база уходит в выгрузку. Собирают эту строку вызывающие,
    подставляя в неё слова чужих библиотек (`broker/stream.py::_transport`
    делает это дословно), и чистка на всём пути одна — в `BrokerError.__init__`.

    ⚠️ Аудит 06.09.2026 заменил `self.technical = scrub(technical)` на
    `self.technical = technical` и прогнал 3432 теста: **не упал ни один**.
    Этот тест — ответ на ту мутацию.

    ⚠️ Значение — `PATTERN_ONLY`: в `Secret` его не заворачивает никто,
    и вырезать его может только образец. Реестр чистки — состояние процесса,
    и опора на него дала бы зелёный цвет от соседей по файлу (`D-078`).
    """
    assert PATTERN_ONLY not in REGISTRY.known(), (
        "значение попало в реестр чистки — текст затрётся по значению "
        "и у отказа, который не чистит ничего"
    )

    error = BrokerError(
        "Нет связи с брокером.",
        "рукопожатие отклонено, заголовок Authorization: Bearer " + PATTERN_ONLY,
    )

    assert PATTERN_ONLY[:12] not in error.technical, (
        f"секрет уцелел в техническом тексте: {error.technical}"
    )
    assert MASK in error.technical, f"маски нет вовсе: {error.technical}"
    assert "рукопожатие отклонено" in error.technical, (
        f"чистка съела сам текст отказа — разбирать станет нечего: {error.technical}"
    )


def test_the_human_text_of_a_failure_is_scrubbed_by_the_exception_itself() -> None:
    """То же для текста, который читает владелец счёта.

    `human` — фраза журнала решений: владелец счёта видит её в окне, вывозит
    в отчёте и присылает скриншотом в переписку. `str` и `repr` отказа
    возвращают её же, а `args` собраны из неё — поэтому проверяются четыре
    поверхности разом: чистка одна, дорог наружу четыре.

    ⚠️ Мутация аудита 06.09.2026 `self.human = human` не уронила ни одного
    из 3432 тестов.

    ⚠️ Значение — `PATTERN_ONLY`, по той же причине, что и в проверке
    `technical` выше: чистит его образец, а не реестр.
    """
    assert PATTERN_ONLY not in REGISTRY.known(), (
        "значение попало в реестр чистки — текст затрётся по значению "
        "и у отказа, который не чистит ничего"
    )

    error = BrokerError(
        "Брокер отказал в подписке: Authorization: Bearer " + PATTERN_ONLY,
        "поток свечей: без секрета",
    )

    surfaces = {
        "human": error.human,
        "str": str(error),
        "repr": repr(error),
        "args": repr(error.args),
    }
    for where in sorted(surfaces):
        assert PATTERN_ONLY[:12] not in surfaces[where], (
            f"секрет уцелел в {where}: {surfaces[where]}"
        )
    assert MASK in error.human, f"маски нет вовсе: {error.human}"
    assert "Брокер отказал в подписке" in error.human, (
        f"чистка съела сам текст отказа: {error.human}"
    )


def transport(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="https://example.invalid", transport=httpx.MockTransport(handler)
    )


@pytest.mark.parametrize(
    ("name", "reply"),
    [
        ("401 неверный токен", lambda request: httpx.Response(401, json={"error": "UNAUTHORIZED"})),
        ("400 валидация", lambda request: httpx.Response(400, json={"error": "VALIDATION_ERROR"})),
        ("404 не найдено", lambda request: httpx.Response(404, json={"error": "NOT_FOUND"})),
        ("500 сервер", lambda request: httpx.Response(500, text="internal")),
        ("429 частота", lambda request: httpx.Response(429, text="too many")),
    ],
)
def test_no_token_in_exception_from_http_failure(
    store: TokenStore, journal: Any, name: str, reply: Any
) -> None:
    """Отказ брокера по коду ответа не выносит токен ни в текст, ни в трассировку."""
    handler, buffer = journal

    async def scenario() -> BrokerError:
        async with BrokerSession(
            store,
            client=transport(reply),
            clock=moment,
            attempts=2,
            sleep=_no_sleep,
        ) as session:
            with pytest.raises(BrokerError) as caught:
                await session.access_token()
            return caught.value

    error = run(scenario())
    surfaces = {
        "str": str(error),
        "repr": repr(error),
        "args": repr(error.args),
        "human": error.human,
        "technical": error.technical,
        "трассировка": "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        ),
        "журнал": buffer.getvalue(),
    }
    for where, body in surfaces.items():
        assert not leaked(body), f"{name}: утечка в {where}:\n{body}"


def test_no_token_when_server_echoes_it_back(store: TokenStore, journal: Any) -> None:
    """Сервер вернул наш же токен в теле отказа — в лог он не попадает.

    Это не выдумка: сервис авторизации вполне может отдать в тексте ошибки
    то, что ему передали. Тело отказа мы кладём в технический лог — с 05.09.2026
    отдельным полем `details` и отдельной записью уровня DEBUG, а не строкой
    внутри `technical`.
    """
    handler, buffer = journal

    def reply(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": f"token {FAKE_REFRESH} is not valid",
            },
        )

    async def scenario() -> BrokerError:
        async with BrokerSession(
            store, client=transport(reply), clock=moment, attempts=1, sleep=_no_sleep
        ) as session:
            with pytest.raises(BrokerError) as caught:
                await session.access_token()
            return caught.value

    error = run(scenario())
    assert not leaked(error.technical), f"эхо токена в техническом тексте: {error.technical}"
    assert not leaked(error.details), f"эхо токена в теле ответа: {error.details}"
    assert not leaked(str(error))
    assert not leaked(buffer.getvalue())
    assert MASK in error.details, "тело отказа не прошло через чистку"
    # Тело живёт **только** в `details`. Проверка не косметическая: `technical`
    # читает владелец счёта, пока технического лога нет, — см. 05.09.2026
    # в шапке `broker/errors.py`. Код отказа переносится, текст отказа — нет.
    assert "invalid_grant" in error.technical, "код отказа брокера потерян для разбора"
    for piece in ("error_description", "is not valid", "token "):
        assert piece not in error.technical, (
            f"кусок тела ответа просочился в технический текст: {error.technical}"
        )


def frames_with_secret(error: BaseException) -> list[str]:
    """Строки локальных переменных всех кадров трассировки, где нашлась подделка.

    `traceback.format_exception` переменные кадра не печатает — и потому
    все прежние проверки этот путь не видели. Печатают их отладчики,
    сборщики отчётов об ошибках и `pytest -l`. Кадр живёт ровно столько,
    сколько живёт объект трассировки, то есть до конца обработки отказа.
    """
    rendered = "".join(
        traceback.TracebackException.from_exception(error, capture_locals=True).format()
    )
    return [line.strip() for line in rendered.splitlines() if leaked(line)]


@pytest.mark.parametrize(
    ("name", "reply"),
    [
        ("обрыв связи", lambda request: _raise(httpx.ConnectError("нет сети", request=request))),
        ("таймаут", lambda request: _raise(httpx.ReadTimeout("долго", request=request))),
        ("отказ 401", lambda request: httpx.Response(401, json={"error": "UNAUTHORIZED"})),
        ("отказ 500", lambda request: httpx.Response(500, text="internal")),
        ("ответ без токена", lambda request: httpx.Response(200, json={"token_type": "bearer"})),
        ("ответ не JSON", lambda request: httpx.Response(200, text="<html>")),
        # Тело «200» без поля токена, но с нашим токеном в постороннем поле.
        # Прежние случаи этой ветки не проверяли: тело не содержало ни одной
        # подделки, и проверка проходила при любой реализации. Сервис,
        # возвращающий наш токен в чужом поле, — не выдумка: тем же занимается
        # его сообщение об отказе, на что есть отдельная проверка выше.
        (
            "200 без токена, но с эхом токена",
            lambda request: httpx.Response(
                200, json={"token_type": "bearer", "hint": f"got {FAKE_REFRESH}"}
            ),
        ),
    ],
)
def test_no_token_in_frame_locals_of_any_traceback(
    store: TokenStore, name: str, reply: Any
) -> None:
    """Ни в одной локальной переменной ни одного кадра нет сырого значения.

    Проверка общая, а не про конкретную переменную: следующая переменная
    с сырым значением обязана валить этот тест, не дожидаясь отдельного
    разбора. Найдено аудитом 30.08.2026: словарь тела запроса авторизации
    очищался после `await`, то есть на пути исключения — никогда,
    и обрыв связи в время обмена выносил 90-дневный токен целиком.
    """

    async def scenario() -> BrokerError:
        async with BrokerSession(
            store,
            client=transport(reply),
            clock=moment,
            attempts=2,
            sleep=_no_sleep,
        ) as session:
            with pytest.raises(BrokerError) as caught:
                await session.access_token()
            return caught.value

    error = run(scenario())
    offenders = frames_with_secret(error)
    assert not offenders, f"{name}: значение токена в переменных кадра:\n  " + "\n  ".join(
        offenders
    )


def _auth_then(failure: BaseException) -> Any:
    """Обмен токена проходит, а следующий запрос падает не-httpx отказом."""
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(
                200, json={"access_token": FAKE_ACCESS, "expires_in": 86400}
            )
        raise failure

    return handler


#: Отказы, летящие мимо `httpx`. ⚠️ **Заводятся по требованию, а не разом
#: при разборе файла**, и это условие изоляции, а не стиль. Готовый объект
#: исключения, положенный в `parametrize`, живёт до конца прогона — а `raise`
#: вешает на него `__traceback__`, и вместе с трассировкой в живых остаются
#: все кадры: `BrokerSession`, его рабочий токен и `Secret` вокруг значения.
#: Замер 06.09.2026: `FAKE_ACCESS` появлялся в реестре чистки на этом тесте
#: и **не уходил оттуда до конца процесса**, то есть каждый следующий тест
#: файла — и любой тест, доставшийся тому же воркеру, — чистился по значению
#: там, где обязан был чиститься образцом. Ровно этим зеленел
#: `test_scrub_does_not_damage_what_it_already_masked` (`D-078`).
NON_HTTPX_FAILURES: tuple[tuple[str, Callable[[], BaseException]], ...] = (
    ("отмена задачи", asyncio.CancelledError),
    ("нехватка памяти", MemoryError),
    ("чужой RuntimeError", lambda: RuntimeError("что-то пошло не так внутри")),
)


@pytest.mark.parametrize(("name", "make_failure"), NON_HTTPX_FAILURES)
def test_no_access_token_in_frame_locals_of_a_non_httpx_failure(
    store: TokenStore, name: str, make_failure: Callable[[], BaseException]
) -> None:
    """Заголовок с рабочим токеном не остаётся в кадре при чужом отказе.

    `_with_retries` ловит только исключения httpx. Всё остальное проходит кадр
    `_authorized` насквозь и уносит с собой словарь заголовков со строкой
    `Bearer <рабочий токен>` — сутки полного доступа к счёту.

    Главный случай здесь — **отмена задачи**: владелец счёта закрывает окно
    или жмёт «Стоп» во время запроса портфеля, Qt и asyncio отменяют задачу,
    `CancelledError` летит через этот кадр, и asyncio штатно печатает
    непойманное исключение задачи через `loop.call_exception_handler`.
    То есть утечка случается не в редком отказе, а при обычном выходе
    из программы.
    """
    failure: BaseException = make_failure()

    async def scenario() -> BaseException:
        async with BrokerSession(
            store,
            client=transport(_auth_then(failure)),
            clock=moment,
            attempts=1,
            sleep=_no_sleep,
        ) as session:
            try:
                await session.read(PORTFOLIO_PATH)
            except BaseException as error:  # noqa: BLE001 — ловим любой
                return error
            raise AssertionError("подставленный отказ не долетел")

    error = run(scenario())
    assert type(error) is type(failure), f"{name}: отказ подменился на {error!r}"
    offenders = frames_with_secret(error)
    assert not offenders, f"{name}: значение рабочего токена в переменных кадра:\n  " + (
        "\n  ".join(offenders)
    )


def test_bad_access_token_from_the_server_leaves_the_layer_as_broker_error(
    store: TokenStore,
) -> None:
    """Негодный рабочий токен в ответе — отказ слоя, а не голый `ValueError`.

    `Secret` отвергает значение с посторонним символом, и до правки этот
    `ValueError` уходил из слоя `broker/` как есть. Разбор отказов выше
    ловит `BrokerError`; всё прочее читается как дефект программы и валит
    работу вместо того, чтобы сказать «сервис ответил не тем».

    Сценарий не выдуманный: шлюз между брокером и клиентом правит тело,
    а сервис при внутренней ошибке отвечает 200 с текстом вместо токена.
    """
    def reply(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"access_token": FAKE_BAD_ACCESS, "expires_in": 86400}
        )

    async def scenario() -> BrokerError:
        async with BrokerSession(
            store, client=transport(reply), clock=moment, attempts=1, sleep=_no_sleep
        ) as session:
            with pytest.raises(BrokerError) as caught:
                await session.access_token()
            return caught.value

    error = run(scenario())
    assert "сервис авторизации" in error.human or "брокер" in error.human.lower()
    assert FAKE_BAD_ACCESS not in error.technical
    assert FAKE_BAD_ACCESS[:12] not in error.technical


def test_frame_scan_would_catch_a_planted_leak() -> None:
    """Канарейка: сам способ поиска работает.

    Без неё тест выше зеленел бы и на слое, из которого убрали всю защиту.
    """

    def carrier() -> None:
        bait = FAKE_REFRESH  # noqa: F841 — она здесь ровно для того, чтобы её нашли
        raise RuntimeError("отказ ради проверки")

    try:
        carrier()
    except RuntimeError as error:
        assert frames_with_secret(error), "поиск по переменным кадра ничего не видит"


def _raise(error: Exception) -> Any:
    raise error


def test_no_token_in_transport_failure(store: TokenStore, journal: Any) -> None:
    """Обрыв связи: исключение httpx в наш текст не переносится."""
    handler, buffer = journal

    def reply(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async def scenario() -> BrokerError:
        async with BrokerSession(
            store, client=transport(reply), clock=moment, attempts=2, sleep=_no_sleep
        ) as session:
            with pytest.raises(BrokerError) as caught:
                await session.access_token()
            return caught.value

    error = run(scenario())
    trace = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    assert not leaked(trace), trace
    assert not leaked(buffer.getvalue())


def test_httpx_itself_does_not_print_the_header() -> None:
    """Проверяем не свой код, а библиотеку: она печатает заголовки или нет.

    httpx 0.28.1 заменяет значение заголовка `authorization` на `[secure]`
    в `repr`. Это его внутреннее свойство, а не наша заслуга: смена версии
    может его отменить, и утечка появится там, где её никто не писал.
    Тест держит это допущение на виду.
    """
    request = httpx.Request(
        "POST", "https://example.invalid/x", headers={"Authorization": f"Bearer {FAKE_ACCESS}"}
    )
    assert not leaked(repr(request))
    assert not leaked(repr(request.headers))
    assert not leaked(str(request.headers))

    response = httpx.Response(401, request=request, json={"error": "UNAUTHORIZED"})
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        assert not leaked(str(error))
        assert not leaked(repr(error))
        assert not leaked(
            "".join(traceback.format_exception(type(error), error, error.__traceback__))
        )


# --- поток свечей: слова чужой библиотеки в наших текстах ---

#: Токен подписки на поток. Отдельное значение, и не для красоты: оно
#: **под охраной реестра**, как в бою, поэтому работу образцов на нём
#: проверять нельзя — для этого в текст отказа подкладывается `PATTERN_ONLY`.
FAKE_HANDSHAKE = "FAKE-handshake-4444-5555-NOT-A-REAL-TOKEN"


async def _handshake_token() -> SimpleNamespace:
    """Подставной `session.access_token()`: слой берёт из ответа `secret.reveal()`."""
    return SimpleNamespace(secret=Secret(FAKE_HANDSHAKE))


class _RefusingHandshake:
    """Контекст, отказывающий на входе, — так выглядит несостоявшееся рукопожатие.

    Классом, а не генератором с `@asynccontextmanager`: у настоящей библиотеки
    отказ поднимается из `__aenter__`, а `yield` после `raise` — мёртвая строка.
    """

    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def __aenter__(self) -> None:
        raise self._error

    async def __aexit__(self, *unused: object) -> bool:
        return False


def test_words_of_the_websockets_library_reach_our_text_without_the_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Текст отказа `websockets` подставляется дословно — и чистится по дороге.

    `broker/stream.py::_transport` собирает технический текст так:
    `f"поток свечей: {type(error).__name__}: {error}"`, то есть слова чужой
    библиотеки попадают в нашу строку **как есть**. Рукопожатие сокета
    котировок несёт токен заголовком `Authorization: Bearer …`, и библиотека
    вправе положить отправленные заголовки в текст своего отказа: путь утечки
    здесь не выдуманный, он проложен.

    ⚠️ Дыры на нём нет, и это проверено прогоном, а не выведено: чистка
    работает. Держит её единственная строка — `scrub` в `BrokerError.__init__`;
    аудит 06.09.2026 снял её и не уронил ни одного из 3432 тестов. Тест
    закрепляет поведение, которое сегодня уже верно, а не чинит дыру.

    Сокет подставной, в сеть тест не ходит.
    """
    assert PATTERN_ONLY not in REGISTRY.known(), (
        "значение попало в реестр чистки — текст затрётся по значению "
        "и у отказа, который не чистит ничего"
    )

    spoken = "opening handshake failed; sent headers: Authorization: Bearer " + PATTERN_ONLY

    def connect(
        url: str, *, additional_headers: dict[str, str], logger: logging.Logger
    ) -> _RefusingHandshake:
        return _RefusingHandshake(websockets.exceptions.InvalidHandshake(spoken))

    monkeypatch.setattr(stream.websockets, "connect", connect)
    session: Any = SimpleNamespace(access_token=_handshake_token)

    async def scenario() -> NoConnection:
        with pytest.raises(NoConnection) as caught:
            async with candle_stream(session, ticker="MXU6", class_code="SPBFUT"):
                pass
        return caught.value

    error = run(scenario())
    assert "InvalidHandshake" in error.technical, (
        f"слова библиотеки до нашего текста не дошли — проверка вакуумна: {error.technical}"
    )
    assert PATTERN_ONLY[:12] not in error.technical, (
        f"заголовок рукопожатия уцелел в техническом тексте: {error.technical}"
    )
    assert MASK in error.technical, f"маски нет вовсе: {error.technical}"
    assert PATTERN_ONLY[:12] not in error.human, f"секрет уехал владельцу счёта: {error.human}"


def test_a_candle_of_the_wrong_shape_does_not_echo_its_value_to_the_owner() -> None:
    """Значение из свечи попадает в **человеческий** текст — и чистится.

    `broker/stream.py::_snapshot` собирает фразу владельцу счёта как
    `f"Брокер прислал свечу в незнакомом виде: {error}."`, а `{error}` — это
    `ValueError` разбора, который цитирует **само значение поля**. Пришли
    брокер в поле цены строку формы JWT — и она уехала бы в журнал решений,
    то есть в окно и в выгрузку.

    Пара к проверке `human` выше: там чистка проверяется на самом отказе,
    здесь — на настоящем пути слоя. ⚠️ Красный цвет здесь значит одно
    из двух: перестал чиститься `human` либо снят образец `_JWT`. Каждое
    из двух стережётся отдельно, эта проверка — про их стык.
    """
    jwt: str = _answer_samples()["ответ_авторизации"]["access_token"]
    assert jwt not in REGISTRY.known(), (
        "значение попало в реестр чистки — текст затрётся по значению "
        "и при выключенных образцах"
    )

    body = {
        "responseType": "CandleStick",
        "ticker": "MXU6",
        "classCode": "SPBFUT",
        "timeFrame": "M1",
        "dateTime": "2026-09-04T05:38:00Z",
        "open": jwt,
        "high": 1.0,
        "low": 1.0,
        "close": 1.0,
        "volume": 1.0,
    }
    with pytest.raises(StreamRejected) as caught:
        snapshot_of(json.dumps(body))

    human = caught.value.human
    assert "незнакомом виде" in human, f"отказ подменился, проверять нечего: {human}"
    assert jwt[:12] not in human, f"значение поля уехало владельцу счёта: {human}"
    assert MASK in human, f"маски нет вовсе: {human}"


# --- журнал ---


def _record_with_a_cached_traceback(message: str) -> logging.LogRecord:
    """Запись журнала, у которой трассировка **уже собрана** в `record.exc_text`.

    Так её собирает первый же обработчик с обычным форматтером:
    `logging.Formatter.format` кладёт готовый текст трассировки в `exc_text`
    и дальше берёт его из кэша, не рисуя заново. Следующий обработчик
    печатает уже этот кэш — вот почему фильтру мало вычистить `record.msg`.

    Отказ поднимается здесь, а не в теле теста, намеренно: в трассировку
    входит кадр того, кто поднял, и подделка из кадра теста нашлась бы
    в выводе сама по себе, без всякой утечки через `exc_text`.
    """
    try:
        raise RuntimeError(message)
    except RuntimeError:
        record = logging.LogRecord(
            LOGGER_NAME,
            logging.ERROR,
            __file__,
            0,
            "разбор ответа брокера не удался",
            (),
            sys.exc_info(),
        )
    logging.Formatter("%(message)s").format(record)  # так кэш и наполняется
    return record


def test_a_traceback_cached_on_the_record_leaves_the_filter_masked() -> None:
    """Секрет из текста исключения не уезжает дальше фильтра журнала.

    ⚠️ Проверяется **фильтр**, а не файл лога, и это не упрощение, а условие
    работоспособности сторожа. Файл сегодня чистит `RedactingFormatter`
    целиком, поэтому проверка по файлу зеленела бы и с выключенной чисткой
    `exc_text`: аудит 06.09.2026 снял строку `record.exc_text =
    scrub(record.exc_text)` и не уронил ни одного из 3432 тестов.

    Красным этот путь становится там, где запись уходит **второму**
    обработчику — консоли, журналу в окне, `QueueHandler`, — у которого
    своего `RedactingFormatter` нет. Второго обработчика в программе
    сегодня нет; появится — сторож уже стоит.

    ⚠️ Значение — `PATTERN_ONLY`: чистит его образец `Bearer …`, а не реестр.
    """
    assert PATTERN_ONLY not in REGISTRY.known(), (
        "значение попало в реестр чистки — трассировка затрётся по значению "
        "и при фильтре, который `exc_text` не трогает"
    )

    record = _record_with_a_cached_traceback(
        "рукопожатие отклонено, заголовок Authorization: Bearer " + PATTERN_ONLY
    )
    assert record.exc_text and PATTERN_ONLY in record.exc_text, (
        "утечка не подложена — фильтру нечего вырезать, проверка вакуумна"
    )

    RedactingFilter().filter(record)

    # Так эту запись напечатает обработчик без чистки: он возьмёт готовый
    # `exc_text` из кэша и допишет его к сообщению.
    printed = logging.Formatter("%(message)s").format(record)
    assert "RuntimeError" in printed, f"трассировки в выводе нет — проверка вакуумна:\n{printed}"
    assert PATTERN_ONLY[:12] not in printed, f"секрет уехал вторым обработчиком:\n{printed}"
    assert MASK in printed, f"маски нет вовсе:\n{printed}"


def test_filter_masks_a_direct_leak_into_the_log(journal: Any) -> None:
    """Кто-то написал в журнал сам токен — фильтр обязан его вырезать.

    Это проверка второго рубежа. Первый рубеж — `Secret` — работает, пока
    в коде пишут `%s` от объекта. Здесь нарочно пишется «сырое» значение,
    как это выйдет у того, кто по невнимательности вызвал `reveal()`.
    """
    handler, buffer = journal
    secret = Secret(FAKE_ACCESS)  # значение попадает в реестр
    # Фильтр здесь не ставится: он уже стоит, потому что слой импортирован.
    # Тест нарочно работает на боевой настройке журнала, а не на своей.
    logger = log()
    assert any(isinstance(item, RedactingFilter) for item in logger.filters)

    logger.warning("отладка: токен %s", secret.reveal())
    logger.error("и ещё раз: " + secret.reveal())
    logger.info("в заголовке: Authorization: Bearer %s", secret.reveal())

    written = buffer.getvalue()
    assert not leaked(written), written
    assert written.count(MASK) >= 3, written


def _log_and_capture(logger_name: str) -> str:
    """Написать в логгер строку с токеном и вернуть то, что дошло до вывода."""
    buffer = io.StringIO()
    sink = logging.StreamHandler(buffer)
    sink.setFormatter(logging.Formatter("%(name)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(sink)
    previous = root.level
    root.setLevel(logging.DEBUG)
    try:
        logging.getLogger(logger_name).warning(
            "запрос с заголовком Authorization: Bearer %s", FAKE_ACCESS
        )
    finally:
        root.removeHandler(sink)
        root.setLevel(previous)
    return buffer.getvalue()


@pytest.fixture
def foreign_scrubbing() -> Iterator[tuple[str, ...]]:
    """Чистка чужих веток — руками теста, как её поставит `app/` на Э1-18.

    Слой брокера её не ставит и ставить не должен: `httpx` — общая библиотека
    процесса, через неё ходит и загрузка истории в `market/`. Фикстура
    убирает за собой, иначе соседний тест, требующий чужие ветки нетронутыми,
    зеленел бы или краснел в зависимости от порядка запуска.
    """
    from broker.redaction import FOREIGN_LOGGERS, RedactingSink

    # Запоминается состояние ДО, и снимается ровно добавленное. Простое
    # «убрать всё наше» скрыло бы нарушение: если слой брокера снова начнёт
    # настраивать чужие ветки на импорте, уборка вычистит и это, и соседний
    # тест, стоящий на страже, зазеленеет из-за порядка запуска.
    before = {
        name: (
            list(logging.getLogger(name).filters),
            list(logging.getLogger(name).handlers),
        )
        for name in FOREIGN_LOGGERS
    }
    install(FOREIGN_LOGGERS)
    try:
        yield FOREIGN_LOGGERS
    finally:
        for name in FOREIGN_LOGGERS:
            logger = logging.getLogger(name)
            filters_before, handlers_before = before[name]
            for item in list(logger.filters):
                if isinstance(item, RedactingFilter) and item not in filters_before:
                    logger.removeFilter(item)
            for handler in list(logger.handlers):
                if isinstance(handler, RedactingSink) and handler not in handlers_before:
                    logger.removeHandler(handler)


@pytest.mark.parametrize("logger_name", ["broker", "broker.session", "broker.a.b"])
def test_scrubbing_covers_the_whole_broker_subtree(logger_name: str) -> None:
    """Своё поддерево слой закрывает сам, одним фактом импорта.

    Фильтр, поставленный на логгер, срабатывает только на записях, сделанных
    на нём самом — модуль сам это правило и формулирует. Поэтому в списке
    стоят **дети**: проверка на одном `broker` была бы зелёной и при полностью
    открытой утечке из `broker.session`, а первый такой логгер в слое появится
    молча и чиститься перестанет тоже молча.

    Ставится это на импорте, а не в конструкторе сессии: `TokenStore` пишет
    в журнал раньше, чем существует `BrokerSession`.
    """
    written = _log_and_capture(logger_name)
    assert written.strip(), f"{logger_name}: запись вообще не дошла до обработчика"
    assert not leaked(written), f"{logger_name}: утечка в журнале:\n{written}"
    assert MASK in written, f"{logger_name}: маски нет:\n{written}"


@pytest.mark.parametrize(
    "logger_name",
    ["httpx", "httpx._client", "httpcore", "httpcore.connection", "httpcore.http11"],
)
def test_scrubbing_is_ready_for_the_foreign_subtrees(
    logger_name: str, foreign_scrubbing: tuple[str, ...]
) -> None:
    """Чужие ветки закрываются тем же приёмом — когда их закроет `app/`.

    Заголовок `Authorization: Bearer …` пишут не `httpx` и `httpcore`, а их
    дети: `httpx._client`, `httpcore.connection`, `httpcore.http11`. Проверка
    на родителе была бы зелёной при полностью открытой утечке из ребёнка,
    поэтому в списке стоят именно дети.

    Тест держит механизм готовым и проверенным к моменту Э1-18. Он **не**
    утверждает, что чистка чужих веток стоит: сегодня она не стоит, и это
    осознанный долг — см. докстринг `redaction.install`.
    """
    written = _log_and_capture(logger_name)
    assert written.strip(), f"{logger_name}: запись вообще не дошла до обработчика"
    assert not leaked(written), f"{logger_name}: утечка в журнале:\n{written}"
    assert MASK in written, f"{logger_name}: маски нет:\n{written}"


def _handshake_line_as_logged(logger_name: str) -> str:
    """Строка рукопожатия `websockets` на DEBUG — как её увидел бы файл лога."""
    buffer = io.StringIO()
    sink = logging.StreamHandler(buffer)
    sink.setFormatter(logging.Formatter("%(name)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(sink)
    previous = root.level
    root.setLevel(logging.DEBUG)
    try:
        # Точная форма `websockets/client.py:297` (17.0.1): ключ и значение порознь.
        logging.getLogger(logger_name).debug(
            "> %s: %s", "Authorization", f"Bearer {FAKE_ACCESS}"
        )
    finally:
        root.removeHandler(sink)
        root.setLevel(previous)
    return buffer.getvalue()


@pytest.mark.parametrize("logger_name", ["websockets", "websockets.client"])
def test_websockets_handshake_headers_are_masked_by_the_foreign_scrubbing(
    logger_name: str, foreign_scrubbing: tuple[str, ...]
) -> None:
    """`websockets` печатает каждый заголовок рукопожатия на DEBUG — с токеном.

    Проверено по исходнику 17.0.1: `client.py:297`, `logger.debug("> %s: %s",
    key, value)`, логгер `websockets.client`. Подписка на поток котировок
    передаёт токен именно заголовком `Authorization`. Уровень по умолчанию
    INFO — то есть без строки в `FOREIGN_LOGGERS` дыра закрыта случайностью
    настройки, а не решением.
    """
    assert "websockets" in foreign_scrubbing, (
        "websockets выпал из FOREIGN_LOGGERS — заголовок рукопожатия уйдёт в лог на DEBUG"
    )
    written = _handshake_line_as_logged(logger_name)
    assert written.strip(), f"{logger_name}: запись вообще не дошла до обработчика"
    assert not leaked(written), f"{logger_name}: утечка в журнале:\n{written}"
    assert MASK in written, f"{logger_name}: маски нет:\n{written}"


#: Проба к тесту ниже. Отдельный процесс обязателен: проверяется эффект
#: **импорта**, а он одноразовый. В общем прогоне к моменту теста по дереву
#: логгеров уже прошлись соседние тесты, и проверка в том же процессе
#: зеленела бы или краснела по порядку запуска, а не по коду.
IMPORT_PROBE = """
import logging

import broker  # noqa: F401 — импорт и есть проверяемое действие
from broker.redaction import FOREIGN_LOGGERS, LOGGER_NAME, RedactingFilter, RedactingSink

ours = logging.getLogger(LOGGER_NAME)
assert [f for f in ours.filters if isinstance(f, RedactingFilter)], "своё поддерево не закрыто"
assert [h for h in ours.handlers if isinstance(h, RedactingSink)], "своё поддерево не закрыто"

dirty = []
for name in FOREIGN_LOGGERS:
    logger = logging.getLogger(name)
    if [f for f in logger.filters if isinstance(f, RedactingFilter)]:
        dirty.append(name + ": фильтр")
    if [h for h in logger.handlers if isinstance(h, RedactingSink)]:
        dirty.append(name + ": обработчик")
print("чужие ветки не тронуты" if not dirty else "слой настроил чужое: " + ", ".join(dirty))
"""


def test_importing_the_layer_does_not_touch_foreign_logger_branches() -> None:
    """Слой брокера не настраивает чужие ветки дерева логгеров процесса.

    `httpx` — общая библиотека: через неё ходит и загрузка истории с биржи
    в `market/`. `RedactingFilter` схлопывает запись — `record.args` после
    него пустой кортеж, — и структурный обработчик, который поставит `app/`,
    получил бы записи чужого слоя без параметров. Причина при этом невидима:
    она срабатывает в момент `import broker`, за тысячу строк от места,
    где смотрят.

    Второе следствие того же: в процессе-работнике оптимизатора (решение 0005
    п. 18) `broker` не импортируется, и одна и та же догрузка с биржи давала бы
    в работнике другой лог, чем в главном процессе.

    По `ARCHITECTURE.md` §2 журналирование настраивает `app/`.
    """
    probe = subprocess.run(
        [sys.executable, "-c", IMPORT_PROBE],
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
    assert probe.stdout.strip() == "чужие ветки не тронуты", probe.stdout + probe.stderr


def test_the_token_field_names_are_the_layers_own() -> None:
    """Пара имён выше — та же, что записана в слое, а не копия наугад.

    Переименуют поле в `token_store` и не поправят образцы — падает здесь,
    а не молчаливой утечкой в журнал.
    """
    current, previous = TOKEN_FIELD_NAMES
    assert current in LEGACY_KEYS, f"в слое нет поля {current!r}: {sorted(LEGACY_KEYS)}"
    assert LEGACY_KEYS[current] == previous


def test_scrub_knows_every_name_of_our_own_file() -> None:
    """Образцы знают **оба** имени поля нашего файла — нынешнее и прежнее.

    Момент разбора файла токена единственный, когда оба рубежа опущены разом:
    `Secret` ещё не создан, реестр значения не знает, а образцы знали только
    английские составные имена (`access_token` и подобные). Одна строка
    `log().debug("файл: %s", raw)`, добавленная при отладке, вынесла бы файл
    с токеном целиком.

    ⚠️ Прежнее имя проверяется наравне с нынешним и после 06.09.2026: файлы
    того формата лежат в резервных копиях папки данных, а копия папки — способ
    перенести портативную программу на другую машину (решение 0001).

    ⚠️ Значение здесь — `PATTERN_ONLY`, а не `FAKE_REFRESH`, и это условие
    работоспособности сторожа, а не стиль: реестр чистки знает значения,
    прошедшие через `Secret`, и затирал бы текст сам, независимо от образцов.
    """
    assert PATTERN_ONLY not in REGISTRY.known(), (
        "значение попало в реестр чистки — проверка образцов больше ничего "
        "не проверяет: текст затрётся по значению при любых образцах"
    )

    for shape in sorted(PATTERN_ONLY_FILES):
        cleaned = scrub(PATTERN_ONLY_FILES[shape])
        assert PATTERN_ONLY not in cleaned, f"{shape}: {cleaned}"
        assert MASK in cleaned, f"{shape}: маски нет вовсе: {cleaned}"

    for name in TOKEN_FIELD_NAMES:
        as_json = scrub(f'{{"{name}": "{PATTERN_ONLY}"}}')
        assert PATTERN_ONLY not in as_json, f"поле {name!r} в JSON: {as_json}"
        as_form = scrub(f"{name}={PATTERN_ONLY}")
        assert PATTERN_ONLY not in as_form, f"поле {name!r} в form: {as_form}"

    # Безобидные соседи не съедаются: `token_type` — это не токен.
    assert "bearer" in scrub('{"token_type": "bearer"}')


def test_scrub_does_not_damage_what_it_already_masked() -> None:
    """Чистка идемпотентна: один и тот же текст проходит через неё дважды.

    Фильтр чистит запись, форматтер — готовую строку вместе с трассировкой
    (до трассировки фильтр не достаёт, поэтому оба нужны). Без оговорки
    в образцах `\\S+` откусывал у уже подставленной маски первое слово,
    и `Bearer <токен скрыт>` превращалось в `Bearer <токен скрыт> скрыт>`:
    маска ломала сообщение, в которое её подставили.

    ⚠️ Последняя строка перебора — «голое» значение: ни имени поля, ни
    `Bearer`, ни формы JWT. Её вырезает не образец, а **реестр**, и живой
    `Secret` заводится здесь же именно поэтому. Реестр — состояние процесса:
    до 06.09.2026 строка зеленела от чужих `Secret`, которые соседние тесты
    файла оставляли в живых внутри `BrokerSession` и `StoredToken`, а в
    одиночку тест падал (`D-078`). Своё состояние тест готовит сам.

    ⚠️ Обратное условие — на первой строке: заголовок чистит **образец**
    `Bearer …`, и значение там `PATTERN_ONLY`, которое в `Secret` не заворачивает
    никто. Стой там значение под охраной реестра — образец перестал бы
    участвовать вовсе, и снос `_BEARER` целиком прошёл бы мимо проверки.
    Оба состояния реестра объявлены здесь же, поэтому прогон в одиночку
    от прогона в компании не отличается ничем.

    ⚠️ Почему не `FAKE_ACCESS`: он попадает в реестр у соседей по файлу
    и уходит оттуда не сразу, а на ближайшей сборке мусора — трассировка
    чужого отказа держит `BrokerSession` в цикле ссылок. Проверка «его
    в реестре нет» зеленела бы или краснела по тому, успел ли пройти
    сборщик, то есть по погоде.
    """
    keeper = Secret(FAKE_REFRESH)  # значение под охраной реестра, как в бою
    assert FAKE_REFRESH in REGISTRY.known(), (
        "реестр не взял подделку под охрану — последняя строка перебора "
        "проверяла бы не идемпотентность, а пустое место"
    )
    assert PATTERN_ONLY not in REGISTRY.known(), (
        "значение из заголовка попало в реестр чистки — первая строка "
        "перебора проверяет тогда реестр во второй раз, а образец `Bearer …` "
        "не проверяет никто"
    )

    for text in (
        f"Authorization: Bearer {PATTERN_ONLY}",
        f"refresh_token={FAKE_REFRESH}&grant_type=refresh_token",
        '{"токен": "' + FAKE_REFRESH + '"}',
        f"в журнале оказалось {FAKE_REFRESH}",
    ):
        once = scrub(text)
        assert once == scrub(once) == scrub(scrub(once)), (
            f"повторная чистка меняет текст:\n{once}\n{scrub(once)}"
        )
        assert not leaked(once)
        assert PATTERN_ONLY[:12] not in once, f"образец не сработал: {once}"

    # Ссылка держится до конца проверки намеренно: смерть `Secret` снимает
    # охрану с значения, и перебор выше остался бы без первого рубежа.
    assert str(keeper) == MASK


def test_registry_survives_forget_then_remember_again() -> None:
    """Забыли, снова запомнили, разобрали очередь — значение под охраной.

    Отложенное забывание разъезжается с повторной регистрацией, если очередь
    разбирают не в том порядке: значение вернулось, а отложенный `forget`
    его вычёркивает. Отказ молчаливый — токен просто перестаёт затираться.
    """
    import gc

    from broker.redaction import REGISTRY

    value = "FAKE-again-0000-1111-NOT-A-REAL-TOKEN"
    first = Secret(value)
    assert value in REGISTRY.known()

    del first
    gc.collect()
    assert value not in REGISTRY.known()

    second = Secret(value)
    assert value in REGISTRY.known(), "повторная регистрация не восстановила охрану"
    assert scrub(f"в журнале {value}") == "в журнале " + MASK

    # Ещё раз, не давая очереди разобраться между смертью и рождением.
    third = Secret(value)
    del second
    gc.collect()
    assert value in REGISTRY.known(), "смерть одной копии сняла охрану с живой"
    assert scrub(f"в журнале {value}") == "в журнале " + MASK

    del third
    gc.collect()
    assert value not in REGISTRY.known()


def test_traceback_in_the_log_is_masked(journal: Any) -> None:
    """Трассировку рисует форматтер — она чистится там же."""
    handler, buffer = journal
    secret = Secret(FAKE_REFRESH)

    try:
        raise RuntimeError(f"чужая библиотека напечатала {secret.reveal()}")
    except RuntimeError:
        log().exception("отказ при обмене токена")

    written = buffer.getvalue()
    assert "RuntimeError" in written, "трассировка вообще не попала в журнал"
    assert not leaked(written), written


#: Программа-проба для теста ниже. Запускается отдельным процессом: если
#: взаимная блокировка вернётся, зависнет она, а не весь прогон.
#: `dump_traceback_later(..., exit=True)` печатает стек и убивает пробу через
#: 20 секунд — тест падает с готовым стеком, а не с молчаливым таймаутом.
#:
#: ⚠️ Пороги сборщика мусора **не трогаются**. Подкрутка `gc.set_threshold(1,1,1)`
#: дефект прячет: при агрессивной сборке мусор умирает раньше, чем критическая
#: секция успевает аллоцировать, и проба зеленеет на сломанном коде. Ровно
#: на этом прежняя версия пробы и обманула — она проверяла свой финализатор
#: `forget`, а не чужой.
DEADLOCK_PROBE = """
import faulthandler, logging
from broker.redaction import install, log
from broker.secret import Secret

install()
log().setLevel(logging.DEBUG)
faulthandler.dump_traceback_later(20, exit=True)


class Foreign:
    '''Чужой объект, который пишет в журнал, когда его убирает сборщик.

    Это не выдумка ради теста: так ведёт себя `asyncio.Future.__del__`
    у отменённой задачи — штатное событие при закрытии окна во время
    запроса портфеля.
    '''

    def __del__(self):
        log().debug("чужой объект убран сборщиком")


for number in range(30000):
    secret = Secret(f"FAKE-loop-{number:05d}-NOT-A-REAL-TOKEN")
    foreign = Foreign()
    cycle = {"foreign": foreign}
    cycle["self"] = cycle          # цикл: refcount не освободит, освободит сборщик
    del secret, foreign, cycle
    log().warning("строка журнала номер %d", number)
print("проба дошла до конца")
"""


def test_log_scrubbing_does_not_deadlock_on_a_foreign_finalizer() -> None:
    """Чистка журнала не вешает поток, когда в неё приходит чужой финализатор.

    Дефект: `known()` держала неперевходимую блокировку и **под ней выполняла
    чужой код**. Любая аллокация под блокировкой запускает сборщик мусора,
    сборщик зовёт финализаторы поколения, а финализатор — чей угодно `__del__`,
    в том числе `asyncio.Future.__del__` отменённой задачи — пишет в журнал.
    Дальше: фильтр журнала → `scrub` → `known()` → та же блокировка → futex,
    навсегда.

    Своего финализатора `forget` мало: закрыть один известный вход
    не значит закрыть вход. Поэтому проверяется именно **чужой**.

    Последствие в бою: робот держит позицию, поток замирает молча — без
    исключения, без записи в журнал, без нагрузки на процессор. Позиция
    остаётся неуправляемой до конца сессии.

    Проба идёт отдельным процессом намеренно: вернувшаяся блокировка повесила
    бы прогон целиком, и вместо красного теста был бы вечный таймаут.
    `encoding`/`PYTHONIOENCODING` заданы явно — под `LC_ALL=C` (контейнер,
    systemd) `print` с кириллицей дал бы `UnicodeEncodeError`, и тест
    сообщил бы «реестр встал», показав не тот стек.
    """
    probe = subprocess.run(
        [sys.executable, "-c", DEADLOCK_PROBE],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(REPO_ROOT),
            "PYTHONIOENCODING": "utf-8",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert probe.returncode == 0, (
        "чистка журнала встала: под блокировкой реестра выполнился чужой код.\n"
        f"код возврата {probe.returncode}\n{probe.stderr[-2500:]}"
    )
    assert "проба дошла до конца" in probe.stdout, probe.stdout


def test_forgotten_secret_leaves_the_registry() -> None:
    """Умерший токен перестаёт вырезаться: реестр не растёт бесконечно."""
    import gc

    from broker.redaction import REGISTRY

    value = "FAKE-temporary-0000-NOT-A-REAL-TOKEN"
    secret = Secret(value)
    assert value in REGISTRY.known()
    del secret
    gc.collect()
    assert value not in REGISTRY.known()


# --- файл токена ---


def test_token_value_is_only_in_its_own_file(store: TokenStore, journal: Any) -> None:
    """В `userdata/` значение лежит ровно в одном файле и больше нигде."""
    handler, buffer = journal
    store.load()

    directory = store.path.parent
    files = [path for path in directory.rglob("*") if path.is_file()]
    assert files == [store.path], f"в папке появились лишние файлы: {files}"

    carriers = [
        path
        for path in files
        if FAKE_REFRESH in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert carriers == [store.path], f"значение найдено не только в своём файле: {carriers}"
    assert not leaked(buffer.getvalue()), buffer.getvalue()


def test_token_file_in_another_encoding_leaks_nothing(
    userdata: pathlib.Path, journal: Any
) -> None:
    """Файл токена не в UTF-8: наружу не выходит ни одного его байта.

    Стартовый путь программы. `Path.read_text` бросает `UnicodeDecodeError`,
    а это подкласс `ValueError`, а не `OSError` — прежний перехват его
    не видел, и исключение уходило наружу необёрнутым, минуя всю чистку.
    Уносило оно при этом **весь файл целиком**: буфер лежит у него в `object`,
    в `args` и в переменных кадра `codecs`. Проверено прогоном 30.08.2026:
    файл с токеном в cp1251 печатался в трассировке побайтно — три раза
    в одной трассировке.

    Одного перехвата мало: `raise ... from None` внутри обработчика обнуляет
    `__cause__`, но `__context__` продолжает указывать на исходное исключение,
    а через него — на тот же буфер. Поэтому цепочка проверяется отдельно.
    """
    handler, buffer = journal
    keeper = TokenStore(userdata)
    keeper.path.write_bytes(CP1251_TOKEN_FILE)

    with pytest.raises(BrokerError) as caught:
        keeper.load()
    error = caught.value

    surfaces = {
        "str": str(error),
        "repr": repr(error),
        "args": repr(error.args),
        "human": error.human,
        "technical": error.technical,
        "трассировка": "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        ),
        "трассировка с переменными кадра": "".join(
            traceback.TracebackException.from_exception(
                error, capture_locals=True
            ).format()
        ),
        "цепочка исключений": repr((error.__cause__, error.__context__)),
        "журнал": buffer.getvalue(),
    }
    for where, body in surfaces.items():
        assert not leaked_file_content(body), f"утечка в {where}:\n{body}"

    assert error.__cause__ is None, "исходное исключение осталось причиной"
    assert error.__context__ is None, "исходное исключение осталось контекстом"


@pytest.mark.parametrize("case", sorted(BROKEN_TOKEN_FILES))
def test_broken_field_does_not_leave_the_token_in_frame_locals(
    userdata: pathlib.Path, case: str
) -> None:
    """Битое поле в файле: сырое значение не остаётся в переменных кадра.

    Это те пути, где JSON разобрался, а дальше что-то не сошлось. Значение
    токена к этому моменту уже лежит строкой в разобранном словаре, а словарь
    живёт сразу в двух кадрах — `load` и `_build`, — и оба попадают
    в трассировку отказа. Через `capture_locals=True` (отладчик, `pytest -l`,
    будущий сборщик отчётов об ошибках) печатался 90-дневный токен целиком.

    Проверка общая, а не про конкретную переменную: следующая переменная
    с сырым значением обязана валить этот тест, не дожидаясь отдельного разбора.
    """
    keeper = TokenStore(userdata)
    keeper.path.write_text(BROKEN_TOKEN_FILES[case], encoding="utf-8")

    with pytest.raises(BrokerError) as caught:
        keeper.load()
    error = caught.value

    surfaces = {
        "human": error.human,
        "technical": error.technical,
        "трассировка": "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        ),
        "трассировка с переменными кадра": "".join(
            traceback.TracebackException.from_exception(
                error, capture_locals=True
            ).format()
        ),
        "цепочка исключений": repr((error.__cause__, error.__context__)),
    }
    for where, body in surfaces.items():
        assert not leaked(body), f"{case}: утечка в {where}:\n{body}"


@pytest.mark.parametrize("shape", sorted(BROKEN_JSON_FILES))
def test_broken_json_does_not_keep_the_whole_file_in_the_chain(
    userdata: pathlib.Path, shape: str
) -> None:
    """Битый JSON: файл не уезжает наружу в цепочке исключений.

    `json.JSONDecodeError` держит **весь текст файла** в атрибуте `doc`.
    `raise ... from None` обнуляет `__cause__`, но `__context__` продолжает
    указывать на исходное исключение, и токен достаётся одним обращением
    к атрибуту. Подчистить это задним числом нечем: реестр чистки знает
    только значения, прошедшие через `Secret`, а на этом пути `Secret` ещё
    не создан. Образцы знают оба имени поля, но файл бывает оборван прямо
    в значении — закрывающей кавычки нет, и образец не совпадает.
    """
    keeper = TokenStore(userdata)
    keeper.path.write_text(BROKEN_JSON_FILES[shape], encoding="utf-8")

    with pytest.raises(BrokerError) as caught:
        keeper.load()
    error = caught.value

    assert error.__cause__ is None, "исходное исключение осталось причиной"
    assert error.__context__ is None, "исходное исключение осталось контекстом"
    assert not leaked(error.technical), error.technical
    assert not leaked(str(error))
    assert not leaked(
        "".join(
            traceback.TracebackException.from_exception(
                error, capture_locals=True
            ).format()
        )
    )


@pytest.mark.parametrize("name", TOKEN_FIELD_NAMES)
def test_broken_file_message_has_no_content(userdata: pathlib.Path, name: str) -> None:
    """Испорченный файл: в сообщении нет ни куска содержимого.

    Оба имени поля: файл прежнего формата рвётся ровно так же, как нынешний.
    """
    keeper = TokenStore(userdata)
    keeper.path.write_text(
        '{"' + name + '": "' + FAKE_REFRESH + '", сломано', encoding="utf-8"
    )
    with pytest.raises(BrokerError) as caught:
        keeper.load()
    assert not leaked(str(caught.value)), str(caught.value)
    assert not leaked(caught.value.technical), caught.value.technical


# --- статическая проверка исходников ---


#: Каталоги, где живёт код продукта и инструментов. Обход идёт по всем,
#: а не только по `broker/`: прежний обход одного каталога пропустил пятый
#: `reveal()` в `tools/` (ворота `/risk` 04.09.2026).
PRODUCT_DIRS = ("app", "broker", "engine", "market", "strategies", "backtest", "ui", "tools")

#: Где значение токена вправе выйти наружу — и сколько раз в каждом файле.
#: Числа по дереву 04.09.2026. Лишний вызов в разрешённом файле ловится так же,
#: как вызов в чужом: и то и другое — разговор, а не правка таблицы.
REVEAL_SITES: dict[str, int] = {
    # тело запроса авторизации (`_exchange`) и заголовок рабочего запроса (`_authorized`)
    "broker/session.py": 2,
    # запись токена в свой файл с правами 0600
    "broker/token_store.py": 1,
    # заголовок рукопожатия сокета котировок — добавлен 04.09.2026 разговором
    "broker/stream.py": 1,
}


def _reveal_lines(source: pathlib.Path) -> list[int]:
    """Строки, где к чему-то обращаются как к `.reveal` — вызов или голая ссылка.

    Считается по дереву разбора, а не по тексту: докстринг `secret.py`
    показывает `>>> s.reveal()` примером, и текст ловил бы его как вызов;
    зато `f = secret.reveal` без скобок текст пропустил бы, а дерево — нет.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "reveal"
    )


def test_the_reveal_scan_sees_a_planted_call(tmp_path: pathlib.Path) -> None:
    """Канарейка: обход видит и вызов, и голую ссылку на `reveal`, а докстринг — нет."""
    probe = tmp_path / "probe.py"
    probe.write_text("x = secret.reveal()\n\n\ndef f(s):\n    return s.reveal\n", encoding="utf-8")
    assert _reveal_lines(probe) == [1, 5]
    quiet = tmp_path / "quiet.py"
    quiet.write_text('"""Здесь `reveal()` только в докстринге."""\nreveal = 1\n', encoding="utf-8")
    assert _reveal_lines(quiet) == []


def test_reveal_lives_where_declared() -> None:
    """`reveal()` — ровно там, где объявлено, ровно столько раз, и нигде больше.

    Четыре вызова в трёх файлах слоя брокера: тело запроса авторизации
    и заголовок рабочего запроса (`session.py`), запись в свой файл
    (`token_store.py`), заголовок рукопожатия сокета (`stream.py`). Пятое
    место — повод для разговора, а не для правки таблицы задним числом.

    ⚠️ Обход — **по всем каталогам продукта и инструментов**, а не только
    по `broker/`. Прежний сторож обходил один каталог и был зелёным при живом
    пятом вызове в `tools/` — проверено прогоном 04.09.2026 (ворота `/risk`).
    И число вызовов в разрешённом файле считается: третий `reveal()`
    в `session.py` прежде прошёл бы молча, файл-то разрешён.

    ⚠️ `stream.py` добавлен 04.09.2026 **разговором, а не молча**: сторож упал,
    вопрос был задан владельцу счёта, ответ — «добавь stream.py в список».
    Брокер принимает подписку на поток котировок только с токеном в заголовке
    рукопожатия HTTP — другого способа нет (`.docs/broker-api/20-ws-last-candle.md`).
    Провести это через `session.py` дало бы ту же строку с токеном, только
    длиннее путь.

    Чем место закрыто — правка 04.09.2026, прежняя запись здесь была неверна
    («значение живёт одной локальной переменной» и было дырой):

    * значение уходит в **словарь заголовков, затираемый в `finally`**. Голой
      локальной переменной оно жить не может: оба отказа рукопожатия
      поднимаются внутри кадра `candle_stream`, кадр уезжает в трассировку,
      и `pytest -l` печатал `Bearer <рабочий токен>` целиком. Сторож —
      `tests/test_broker_stream.py::test_no_handshake_token_in_frame_locals_of_any_failure`;
    * подробный лог библиотеки уходит на логгер `broker.stream.socket`
      (`broker/stream.py::SOCKET_LOGGER`), то есть в поддерево, на которое
      чистка ставится при импорте слоя. `websockets` остаётся в
      `FOREIGN_LOGGERS` ради чужих сокетов и версий, где логгер не задать.
    """
    found: dict[str, list[int]] = {}
    scanned = 0
    for directory in PRODUCT_DIRS:
        root = REPO_ROOT / directory
        assert root.is_dir(), f"каталога {directory}/ нет — обход стал бы вакуумным молча"
        for source in sorted(root.rglob("*.py")):
            if "__pycache__" in source.parts:
                continue
            scanned += 1
            lines = _reveal_lines(source)
            if lines:
                found[source.relative_to(REPO_ROOT).as_posix()] = lines
    assert scanned > 40, f"обход увидел всего {scanned} файлов — проверка вакуумна"
    strangers = {path: lines for path, lines in found.items() if path not in REVEAL_SITES}
    assert not strangers, (
        "значение токена достаётся вне объявленных мест (файл: строки): "
        f"{strangers}. Новое место обсуждается, а не добавляется в таблицу"
    )
    counts = {path: len(lines) for path, lines in found.items()}
    assert counts == REVEAL_SITES, (
        f"число вызовов разошлось с таблицей: в дереве {found}, в таблице {REVEAL_SITES}. "
        "Лишний вызов в разрешённом файле — такой же разговор, как новый файл; "
        "исчезнувший — протухшая строка таблицы"
    )


def test_broker_does_not_print() -> None:
    """`print` в слое нет: он пишет мимо журнала и мимо чистки."""
    offenders = [
        source.relative_to(REPO_ROOT).as_posix()
        for source in sorted(BROKER.rglob("*.py"))
        if "__pycache__" not in source.parts
        and any(
            line.lstrip().startswith("print(")
            for line in source.read_text(encoding="utf-8").splitlines()
        )
    ]
    assert not offenders, f"print() в слое broker: {offenders}"


def test_auth_url_is_the_documented_one() -> None:
    """Адрес авторизации — из документации БКС, а не собран по памяти."""
    assert AUTH_URL.startswith("https://be.broker.ru/trade-api-keycloak/")
    assert AUTH_URL.endswith("/protocol/openid-connect/token")


def test_from_status_puts_the_code_only_into_technical_text() -> None:
    """Код брокера — в технический текст, человеку — человеческий."""
    error = from_status(401, where="портфель", body="UNAUTHORIZED")
    assert "401" in error.technical
    assert "401" not in error.human
    assert "UNAUTHORIZED" not in error.human
    assert "токен" in error.human.lower()


def test_from_status_scrubs_the_body_it_is_handed() -> None:
    """`from_status` чистит тело **сам**, а не надеется на вызывающего.

    Сегодня тело приходит уже чистым: `session._interpret` зовёт `scrub`
    до вызова. Но `from_status` — публичная функция слоя, её зовёт и
    `broker/stream.py`, и снять чистку из неё значит открыть дыру, которую
    никакой прогон через сессию не заметит: проверено мутацией 05.09.2026 —
    убранный `scrub` в `body_details` не уронил ни одного теста.

    ⚠️ Значение — `PATTERN_ONLY`, а не `FAKE_REFRESH`, и оно стоит в поле
    с именем: тело обязано вычиститься **образцом**, без всякой опоры
    на реестр процесса. Прежняя редакция клала «голое» `FAKE_REFRESH`
    в поле `hint`, где ни один образец его не видит, и зеленела ровно
    потому, что соседние тесты файла держали живой `Secret` с тем же
    значением; в одиночку она падала (`D-078`). Проверяется поведение
    этой функции, а не состояние процесса.

    ⚠️ **Проверяется одно поле — `details`. Правка 06.09.2026.** Здесь
    стояли ещё две строки, `beginning not in error.technical` и
    `beginning not in error.human`, и обе **не стерегли ничего**: тело
    в эти поля не переносится вовсе, а значит, снятие защиты их не роняет.
    Проверено мутацией в отдельной копии дерева, а не выведено чтением:
    `self.technical = technical` и `self.human = human` в
    `BrokerError.__init__` — тест **проходил**; `return body[:MAX_DETAILS]`
    в `body_details` — падал. Строка, зелёная при снятой защите, хуже
    отсутствующей: она создаёт вид проверки.

    Обе дороги, которые те строки будто бы стерегли, разобраны и стерегутся
    отдельно, каждая на своей мутации, — три следующих теста файла:

    * `technical` тело всё-таки видит, и ровно одним куском: кодом отказа
      из `errors.code_of`. Стережёт тест про код отказа в техническом тексте;
    * ничего кроме этого кода из тела в `technical` не попадает — тест
      про то, что в технический текст не переносится ни символа тела;
    * `human` тела не видит вовсе, и проверяется это равенством с фразой
      того же отказа, собранного без тела.
    """
    assert PATTERN_ONLY not in REGISTRY.known(), (
        "значение попало в реестр чистки — тело затрётся по значению "
        "и при `from_status`, которая не чистит ничего"
    )

    echo = f'{{"error": "invalid_grant", "refresh_token": "{PATTERN_ONLY}"}}'
    error = from_status(400, where="портфель", body=echo)

    # «Ни целиком, ни частично, ни первыми символами»: ищется начало значения.
    assert PATTERN_ONLY[:12] not in error.details, f"токен уцелел в теле: {error.details}"
    assert MASK in error.details, "тело не прошло через чистку вовсе"
    assert "invalid_grant" in error.details, (
        f"чистка съела тело целиком — разбирать отказ станет нечем: {error.details}"
    )


def test_the_broker_code_from_the_body_reaches_the_technical_text_masked() -> None:
    """Код отказа — единственный кусок тела в `technical`, и он тоже чистится.

    Дорога существует, и она одна. `from_status` берёт из тела код
    (`errors.code_of`) и дописывает его в технический текст. Форма кода —
    «буква, дальше буквы, цифры, `_`, `.`, `-`, не длиннее 64»
    (`errors._CODE_SHAPE`), и **короткий JWT под неё подходит**, а настоящий
    токен БКС — именно JWT (`bearerFormat: JWT` в таблицах документации
    брокера). То есть сервис, положивший токен в поле `error` или `code`,
    отправляет его прямиком в технический текст, и держит эту дорогу
    один `scrub` в `BrokerError.__init__`.

    ⚠️ `technical` — не только технический лог. `app/backfill` кладёт его
    в пояснение отчёта о загрузке, отчёт живёт в базе и уходит в выгрузку
    (шапка `broker/errors.py`, запись 05.09.2026). Это поле читает человек.

    ⚠️ Значение из фикстуры и в `Secret` не заворачивается никем: вырезает
    его образец `_JWT`, а не реестр процесса (`D-078`).
    """
    jwt: str = _answer_samples()["ответ_авторизации"]["access_token"]
    assert jwt not in REGISTRY.known(), (
        "значение попало в реестр чистки — код затрётся по значению "
        "и при отказе, который не чистит ничего"
    )

    error = from_status(401, where="портфель", body=json.dumps({"error": jwt}))

    # Канарейка дороги. Ужмут `_CODE_SHAPE`, переименуют поле, перестанут
    # дописывать код — и проверка ниже станет вакуумной. Тогда падает здесь.
    assert "код " in error.technical, (
        f"код из тела до технического текста не дошёл — стеречь нечего: {error.technical}"
    )
    assert jwt[:12] not in error.technical, (
        f"токен приехал в технический текст кодом отказа: {error.technical}"
    )
    assert MASK in error.technical, f"маски нет вовсе: {error.technical}"


#: Приметная фраза внутри тела ответа. Секретом не является: ею проверяется,
#: что в `technical` не попадает **никакой** символ тела, а не только токен.
BODY_CANARY = "КАНАРЕЙКА-ТЕЛА-4471"


def test_nothing_but_the_broker_code_of_the_body_reaches_the_technical_text() -> None:
    """Кроме кода отказа, из тела в `technical` не переносится ни символа.

    Это возврат уже случившейся поломки, а не выдумка. До 05.09.2026 слой
    собирал `technical = f"{technical}; тело: {body[:500]}"`, и владелец
    счёта прочитал в окне страницу 404 от nginx брокера целиком (шапка
    `broker/errors.py`). Разделение восстановлено «по построению»: в текст
    идут код и **описание** тела — вид и длина. Сторож на то, чтобы
    построение не разобрали обратно.

    Тело здесь — страница шлюза: кода отказа в ней нет вовсе, то есть
    любой символ тела в `technical` означает возврат прежней сборки.
    """
    body = f"<html><body><h1>502 Bad Gateway</h1>{BODY_CANARY} {PATTERN_ONLY}</body></html>"
    error = from_status(502, where="свечи", body=body)

    # Канарейка: описание тела обязано в тексте быть, иначе проверять нечего.
    assert "разметка" in error.technical, (
        f"описание тела из текста исчезло — стеречь нечего: {error.technical}"
    )
    assert str(len(body)) in error.technical, (
        f"длина тела из текста исчезла — стеречь нечего: {error.technical}"
    )
    for piece in (BODY_CANARY, "502 Bad Gateway", "<html", PATTERN_ONLY[:12]):
        assert piece not in error.technical, (
            f"кусок тела вернулся в технический текст ({piece!r}): {error.technical}"
        )


def test_the_owner_text_of_a_failure_by_status_does_not_depend_on_the_body() -> None:
    """Фраза владельцу счёта собирается вообще без тела ответа.

    Утверждение строже, чем «токена в ней нет»: текст обязан быть **тем же
    самым**, что у отказа без тела. Проверка «подделки в `human` нет» здесь
    была бы вакуумной — тело в `human` не переносит ни одна строка слоя,
    и снятие чистки её не роняло (мутация 06.09.2026). Равенство роняет
    любую дорогу тела наружу: и токен, и код брокера, и длину, и слово
    «объект JSON».

    Зачем это стеречь: `human` — фраза журнала решений. Владелец счёта
    видит её в окне, вывозит в отчёте и присылает снимком экрана
    в переписку. Ровно так 05.09.2026 к нему и приехала страница 404
    от nginx брокера.
    """
    echo = f'{{"error": "invalid_grant", "refresh_token": "{PATTERN_ONLY}"}}'
    for status in (400, 401, 404, 429, 500):
        with_body = from_status(status, where="портфель", body=echo)
        without_body = from_status(status, where="портфель", body="")
        # Канарейка: пустая фраза совпала бы с пустой, ничего не проверив.
        assert with_body.human.strip(), f"HTTP {status}: фразы владельцу счёта нет вовсе"
        assert with_body.human == without_body.human, (
            f"HTTP {status}: тело ответа изменило фразу владельцу счёта\n"
            f"  с телом:  {with_body.human}\n"
            f"  без тела: {without_body.human}"
        )


def test_token_with_stray_characters_is_refused() -> None:
    """Токен с пробелом или кириллицей не принимается, и в отказе его нет.

    Найдено прогоном 30.08.2026. Значение уходит в HTTP-заголовок, httpx
    кодирует заголовки в ASCII и на постороннем символе бросает
    `UnicodeEncodeError`. Это исключение не наследует `httpx.HTTPError`,
    разбор отказов его не ловит, а в кадре лежит переменная со строкой
    `Bearer <значение>` — то есть отчёт об ошибке уносит токен целиком.
    Отказ на входе закрывает и это, и известную граблю подключения к БКС:
    «токен вставлен с переносом строки».
    """
    for bad in (
        f"{FAKE_REFRESH} с кириллицей",
        f"{FAKE_REFRESH} с пробелом",
        f"{FAKE_REFRESH}\nи переносом",
    ):
        with pytest.raises(ValueError) as caught:
            Secret(bad)
        assert not leaked(str(caught.value)), str(caught.value)

    # Обрамляющие пробелы и переносы — обычное следствие копирования,
    # они срезаются, а не приводят к отказу.
    assert Secret(f"  {FAKE_REFRESH}\n").reveal() == FAKE_REFRESH


def _secret_from_bytes() -> Secret:
    """Отдельный кадр: у кадра теста ссылки на значение нет вовсе.

    Иначе проверка нашла бы байты в собственных переменных и упала бы
    на своём же мусоре — при полностью исправном слое.
    """
    return Secret(FAKE_BYTES_TOKEN)  # type: ignore[arg-type]


def test_secret_refuses_bytes_and_does_not_show_them() -> None:
    """Третья ветка `Secret.__init__`: значение вообще не строка.

    Байты приходят сюда естественным путём — содержимое файла, прочитанное
    до декодирования. Сегодня оба места вызова проверяют тип заранее
    (`token_store._build` делает `isinstance(value, str)`), то есть ветка
    закрыта, но не проверена ничем: две ветки одной функции покрыты,
    третья — нет.

    Проверяется не только тип отказа. Затирание аргумента в этой ветке
    написано ровно ради `capture_locals=True`: без него сырое значение
    осталось бы в переменных кадра и уехало бы в отладчик, в `pytest -l`
    и в будущий сборщик отчётов об ошибках.
    """
    from broker.redaction import REGISTRY

    with pytest.raises(TypeError) as caught:
        _secret_from_bytes()

    error = caught.value
    assert "строка" in str(error), str(error)
    assert "FAKE-bytes" not in str(error), str(error)

    rendered = "".join(
        traceback.TracebackException.from_exception(error, capture_locals=True).format()
    )
    assert "FAKE-bytes" not in rendered, f"значение осталось в переменных кадра:\n{rendered}"

    # Отвергнутое значение не запоминается: реестр чистки — не свалка всего,
    # что когда-либо передали в конструктор.
    assert not [known for known in REGISTRY.known() if "FAKE-bytes" in known]


@pytest.mark.parametrize(
    "wrong",
    [None, 0, 12345, 3.14, ["FAKE"], {"токен": "FAKE"}, bytearray(b"FAKE"), Secret],
)
def test_secret_refuses_anything_that_is_not_a_string(wrong: object) -> None:
    """Отказ — `TypeError`, и это не придирка к букве.

    `ValueError` в этом слое значит «значение испорчено, скопируйте токен
    заново», и выше его ловят именно так: `token_store._build` превращает
    его в «в файле настроек нет токена». Не-строка — не испорченное значение,
    а дефект вызывающего кода, и он обязан быть виден отдельно.
    """
    with pytest.raises(TypeError):
        Secret(wrong)  # type: ignore[arg-type]


def test_two_copies_of_one_token_do_not_disable_masking() -> None:
    """Смерть одной копии токена не выключает затирание для живой.

    Реестр ведёт учёт по числу владельцев, а не по факту наличия. Без этого
    отказ молчаливый: токен просто начинает попадать в лог.
    """
    import gc

    from broker.redaction import REGISTRY

    value = "FAKE-two-owners-0000-NOT-A-REAL-TOKEN"
    alive = Secret(value)
    doomed = Secret(value)
    assert value in REGISTRY.known()

    del doomed
    gc.collect()
    assert value in REGISTRY.known(), "живая копия осталась без затирания"
    assert scrub(f"в журнале {value}") == "в журнале " + MASK

    del alive
    gc.collect()
    assert value not in REGISTRY.known()


async def _no_sleep(seconds: float) -> None:
    """Пауза между попытками в тестах не ждёт по-настоящему."""
    return None


def test_bad_access_token_does_not_stay_in_frame_locals(store: TokenStore) -> None:
    """Негодный рабочий токен из ответа сервера не остаётся в переменных кадра.

    `Secret()` отвергает значение с посторонним символом и бросает `ValueError`.
    Такое значение приходит не только от кривых рук: шлюз между брокером
    и клиентом может поправить тело, а сервис при внутренней ошибке ответить
    200 с текстом ошибки вместо токена. На этом пути очистка не выполнялась,
    и сырой рабочий токен — доступ к счёту на сутки — оставался в кадре,
    видимый через `capture_locals=True`.

    Это та же дыра, что была у токена личного кабинета, только этажом ниже:
    её починили для одной переменной и не обобщили на вторую.
    """
    def reply(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": FAKE_BAD_ACCESS, "expires_in": 86400})

    async def scenario() -> None:
        async with BrokerSession(
            store, client=transport(reply), clock=moment, attempts=1, sleep=_no_sleep
        ) as session:
            await session.access_token()

    try:
        run(scenario())
    except BaseException as error:  # noqa: BLE001 — ловим любой, дальше ищем утечку
        rendered = "".join(
            traceback.TracebackException.from_exception(error, capture_locals=True).format()
        )
    else:
        pytest.fail("негодное значение приняли молча — Secret() обязан его отвергнуть")

    assert FAKE_BAD_ACCESS not in rendered, "значение рабочего токена осталось в кадре"
    assert FAKE_BAD_ACCESS[:12] not in rendered, "начало значения осталось в кадре"


# --- технический лог на диске (D-021) ---
#
# Файл на диске — ровно тот случай утечки, ради которого написан модуль
# чистки. Проверки ниже прогоняют записи через настоящий обработчик,
# заведённый `app/logs.py`, и обыскивают **содержимое файла**.


#: Приметная строка, которая секретом не является. Ею доказывается, что
#: запись до файла доходит: «токена в файле нет» зеленеет и на пустом файле.
FILE_CANARY = "КАНАРЕЙКА-ФАЙЛА-5647382910"

#: Логгер вне любого накрытого поддерева. Записи с него до стоков чистки
#: не доходят вовсе — их чистит только форматтер файлового обработчика.
#: Имя приметное, а не `websockets.client`: то накрывается стоком, и проверка
#: доказывала бы работу другого рубежа.
OUTSIDER = "ни-разу-не-брокер.чужая.ветка"


@pytest.fixture
def technical_log(tmp_path: pathlib.Path) -> Iterator[pathlib.Path]:
    """Технический лог, заведённый так же, как его заводит программа.

    Уровень — самый подробный: заголовки запроса библиотеки печатают только
    на нём, и именно этот режим включает техник при разборе обрыва.
    Уборка обязательна — `setup_logging` правит корневой логгер процесса.
    """
    from app.logs import close_logging, setup_logging
    from broker.redaction import RedactingSink

    root = logging.getLogger()
    handlers_before = list(root.handlers)
    level_before = root.level
    # ⚠️ Снимается и состояние **чужих** веток: `setup_logging` ставит чистку
    # на `httpx`, `httpcore` и `websockets`, а `test_broker_session.py`
    # проверяет, что слой брокера их не трогает. Без уборки тот сторож
    # краснел бы от порядка запуска — поймано полным прогоном 05.09.2026.
    watched = (LOGGER_NAME, *FOREIGN_LOGGERS)
    before = {
        name: (
            list(logging.getLogger(name).filters),
            list(logging.getLogger(name).handlers),
        )
        for name in watched
    }
    home = tmp_path / "userdata"
    home.mkdir()
    report = setup_logging(userdata=home, level=logging.DEBUG)
    assert report.path is not None, report.trouble
    try:
        yield report.path
    finally:
        close_logging()
        for handler in list(root.handlers):
            if handler not in handlers_before:
                root.removeHandler(handler)
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


def _written(path: pathlib.Path) -> str:
    for handler in logging.getLogger().handlers:
        handler.flush()
    return path.read_text(encoding="utf-8")


def test_the_technical_log_file_gets_the_line_at_all(technical_log: pathlib.Path) -> None:
    """Канарейка: строка с чужого логгера доходит до файла целиком.

    Без неё все проверки ниже вакуумны: «подделки в файле нет» верно и тогда,
    когда в файл не попадает вообще ничего — не тот уровень, не тот логгер,
    обработчик не встал.
    """
    logging.getLogger(OUTSIDER).debug("> %s: %s", "X-Request-Id", FILE_CANARY)
    written = _written(technical_log)
    assert FILE_CANARY in written, f"до файла не дошло ничего:\n{written}"


def test_no_token_in_the_log_file_at_the_most_verbose_level(
    technical_log: pathlib.Path,
) -> None:
    """Ни заголовок рукопожатия, ни тело ответа брокера не попадают в файл.

    Три пути разом, и все три настоящие: строка `websockets` формы
    `debug("> %s: %s", key, value)` (исходник 17.0.1, `client.py:297`),
    тело ответа сервиса авторизации с полем `access_token` и живое значение
    под охраной реестра.
    """
    keeper = Secret(FAKE_REFRESH)  # значение под охраной реестра, как в бою
    assert str(keeper) == MASK

    logging.getLogger("websockets.client").debug(
        "> %s: %s", "Authorization", f"Bearer {FAKE_ACCESS}"
    )
    logging.getLogger("broker.session").debug(
        "тело ответа брокера: %s",
        json.dumps({"access_token": FAKE_ACCESS, "expires_in": 86400}),
    )
    logging.getLogger("broker.token_store").debug("файл: %s", _token_file())
    logging.getLogger("httpcore.http11").debug(
        "send_request_headers.complete Authorization: Bearer %s", FAKE_ACCESS
    )

    written = _written(technical_log)
    assert "Authorization" in written, "записи не дошли до файла — проверка вакуумна"
    assert not leaked(written), f"утечка в файле технического лога:\n{written}"
    assert written.count(MASK) >= 4, f"масок меньше, чем мест утечки:\n{written}"


def test_the_file_is_clean_even_for_a_logger_no_sink_covers(
    technical_log: pathlib.Path,
) -> None:
    """Запись с логгера вне накрытых поддеревьев тоже приходит в файл с маской.

    Это и есть второй рубеж: сток чистки висит на `broker`, `httpx`,
    `httpcore` и `websockets`, а до записи с чужой ветки он не достаёт —
    `Logger.callHandlers` зовёт обработчики предков, и предок здесь только
    корень. Чистит её форматтер файлового обработчика. Снять его можно
    одной строкой, поэтому проверка прогонная, а не по типу форматтера.
    """
    logging.getLogger(OUTSIDER).warning(
        "запрос с заголовком Authorization: Bearer %s", FAKE_ACCESS
    )
    written = _written(technical_log)
    assert "Bearer" in written, "запись не дошла до файла — проверка вакуумна"
    assert not leaked(written), f"с чужой ветки подделка ушла в файл:\n{written}"
    assert MASK in written


def test_a_traceback_written_to_the_file_is_masked(technical_log: pathlib.Path) -> None:
    """Трассировка в файле — тоже без подделки.

    До неё фильтр не достаёт: её рисует форматтер. Единственный рубеж
    здесь — `RedactingFormatter` на файловом обработчике.
    """
    try:
        raise RuntimeError(f"отказ на заголовке Bearer {FAKE_ACCESS}")
    except RuntimeError:
        logging.getLogger(OUTSIDER).exception("разбор ответа не удался")

    written = _written(technical_log)
    assert "RuntimeError" in written, "трассировки в файле нет — проверка вакуумна"
    assert not leaked(written), f"подделка уехала в файл вместе с трассировкой:\n{written}"
    assert MASK in written
