"""Тип токена определяется по самому токену — полю `azp` JWT (показ 05.10.2026).

Брокер обменивает refresh-токен только с `client_id`, для которого тот выпущен,
а программа брала `client_id` из поля `scope` («права») файла. Поле не совпало
с токеном — `400 invalid_grant` «Token client and authorized client don't
match», и человек не понимал почему. Вход подставной: файл в `tmp_path`,
`httpx.MockTransport`, токены — самодельные JWT с подписью-заглушкой
(подделка-для-теста: ни одного настоящего значения в файле нет).

Что стережётся (каждый тест называет своё словами в докстринге):

* `client_id` в **настоящей форме обмена** равен `azp` токена, а не полю файла;
* расхождение не молчит: WARNING в техническом логе и строка человеку;
* не-JWT и незнакомый `azp` — по полю файла, как раньше;
* предохранитель боевого режима опирается на `azp`;
* разобранная нагрузка (там `sub`, `sid`) не выходит ни в лог, ни в исключения.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import pathlib
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import httpx
import pytest

from broker.errors import ReadOnlyToken, TokenFileError, TokenRefused, from_status
from broker.redaction import RedactingFormatter, log
from broker.session import AUTH_URL, BrokerSession
from broker.token_store import TOKEN_FILE_NAME, TokenStore
from broker.tokens import TokenScope, issued_for

#: Метки внутри нагрузки, которых нет больше нигде: короткие, без признаков
#: секрета — чистка лога их не тронет, и утечку они покажут, а не спрячут.
SUB_MARKER = "subj-QX7-mark"
SID_MARKER = "sess-ZW4-mark"

READ = "trade-api-read"
WRITE = "trade-api-write"


def moment() -> datetime:
    return datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)


def _segment(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def fake_jwt(azp: object, **extra: object) -> str:
    """JWT формы БКС: заголовок, нагрузка, подпись-заглушка (её никто не проверяет)."""
    claims: dict[str, object] = {"typ": "Refresh", "sub": SUB_MARKER, "sid": SID_MARKER}
    if azp is not None:
        claims["azp"] = azp
    claims.update(extra)
    return ".".join(
        (
            _segment(b'{"alg":"HS512","typ":"JWT"}'),
            _segment(json.dumps(claims).encode()),
            "FAKEsignatureFAKEsignature",
        )
    )


def token_file(
    tmp_path: pathlib.Path, token: str, scope: object, *, issued: object = None
) -> TokenStore:
    """Файл токена руками, как его кладёт владелец счёта, — мимо `save`."""
    directory = tmp_path / "userdata"
    directory.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "version": 1,
        "token": token,
        "issued": issued if issued is not None else (moment() - timedelta(days=1)).isoformat(),
        "account": None,
    }
    if scope is not None:
        payload["scope"] = scope
    path = directory / TOKEN_FILE_NAME
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return TokenStore(directory)


@pytest.fixture
def journal() -> Iterator[io.StringIO]:
    """Технический лог на уровне DEBUG — чтобы утечку не спрятал уровень."""
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(RedactingFormatter("%(levelname)s %(message)s"))
    handler.setLevel(logging.DEBUG)
    logger = log()
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield buffer
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


async def _no_sleep(seconds: float) -> None:
    return None


def exchanged_form(store: TokenStore) -> str:
    """Тело настоящего POST обмена, ушедшего в подставной сервис авторизации."""
    posts: list[httpx.Request] = []

    def reply(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(
            200, json={"access_token": "ACCESS-fake-0000-1111-2222", "expires_in": 86400}
        )

    client = httpx.AsyncClient(
        base_url="https://example.invalid", transport=httpx.MockTransport(reply)
    )

    async def scenario() -> None:
        async with BrokerSession(store, client=client, clock=moment, sleep=_no_sleep) as session:
            await session.access_token()
        await client.aclose()

    asyncio.run(scenario())
    assert [str(request.url) for request in posts] == [AUTH_URL]
    return posts[0].content.decode()


# --- client_id по токену ---------------------------------------------------


def test_read_token_with_trade_field_exchanges_as_read_and_says_so(
    tmp_path: pathlib.Path, journal: io.StringIO
) -> None:
    """Стережёт: токен read с полем «торговля» — обмен с `client_id=trade-api-read`.

    Ровно сценарий показа 05.10.2026. Плюс расхождение не молчит: WARNING
    в логе с обоими типами и строка человеку, где названы оба.
    """
    store = token_file(tmp_path, fake_jwt(READ), WRITE)
    form = exchanged_form(store)
    assert f"client_id={READ}" in form, form
    assert WRITE not in form

    written = journal.getvalue()
    assert "WARNING" in written and READ in written and WRITE in written, written

    notice = store.load().scope_notice
    assert notice is not None
    assert "«для торговли и чтения данных»" in notice and "«только для чтения»" in notice
    assert "боевой режим не включится" in notice


def test_trade_token_with_read_field_exchanges_as_trade_and_says_so(
    tmp_path: pathlib.Path, journal: io.StringIO
) -> None:
    """Стережёт обратное направление: токен write с полем «чтение» — `client_id` write."""
    store = token_file(tmp_path, fake_jwt(WRITE), READ)
    form = exchanged_form(store)
    assert f"client_id={WRITE}" in form, form
    assert "WARNING" in journal.getvalue()
    notice = store.load().scope_notice
    assert notice is not None and "«для торговли и чтения данных»" in notice
    assert "боевой режим не включится" not in notice


def test_matching_field_is_silent(tmp_path: pathlib.Path, journal: io.StringIO) -> None:
    """Стережёт: совпадение не даёт ни WARNING, ни строки человеку — шум не ложный."""
    store = token_file(tmp_path, fake_jwt(WRITE), WRITE)
    assert f"client_id={WRITE}" in exchanged_form(store)
    assert "WARNING" not in journal.getvalue()
    stored = store.load()
    assert stored.scope_from_token and stored.scope_notice is None


def test_known_azp_with_missing_field_still_connects(tmp_path: pathlib.Path) -> None:
    """Стережёт: известный `azp` при пустом поле файла — по токену, а не отказ файла."""
    store = token_file(tmp_path, fake_jwt(READ), None)
    stored = store.load()
    assert stored.token.scope is TokenScope.READ_ONLY
    assert stored.scope_notice is not None and "тип не указан" in stored.scope_notice


# --- запасной путь: поле файла ---------------------------------------------


@pytest.mark.parametrize("scope", [READ, WRITE])
def test_a_token_that_is_not_jwt_goes_by_the_file_field(
    tmp_path: pathlib.Path, journal: io.StringIO, scope: str
) -> None:
    """Стережёт обратную совместимость: не-JWT — `client_id` из поля файла, тихо."""
    store = token_file(tmp_path, "FAKE-rfsd-not-a-jwt-0000-1111-2222", scope)
    assert f"client_id={scope}" in exchanged_form(store)
    stored = store.load()
    assert not stored.scope_from_token and stored.scope_notice is None
    assert "WARNING" not in journal.getvalue()


def test_unknown_azp_goes_by_the_file_field_and_is_logged(
    tmp_path: pathlib.Path, journal: io.StringIO
) -> None:
    """Стережёт: незнакомый клиент в токене — по полю файла и строка в лог.

    Значение незнакомого `azp` с посторонними символами в лог не попадает.
    """
    store = token_file(tmp_path, fake_jwt("other client <x>"), WRITE)
    assert f"client_id={WRITE}" in exchanged_form(store)
    written = journal.getvalue()
    assert "незнакомого клиента <нечитаемое значение>" in written, written
    assert "other client" not in written


def test_without_azp_and_without_field_the_file_is_refused(tmp_path: pathlib.Path) -> None:
    """Стережёт: тип неизвестен ни из токена, ни из файла — прежний отказ файла."""
    with pytest.raises(TokenFileError, match="не указан тип токена"):
        token_file(tmp_path, "FAKE-rfsd-not-a-jwt-0000-1111-2222", "nonsense").load()


@pytest.mark.parametrize(
    "value",
    [
        "",
        "a.b",
        "a.b.c.d",
        "a.!!!.c",
        "a." + _segment(b"\xff\xfe") + ".c",
        "a." + _segment(b"[1, 2]") + ".c",
        "a." + _segment(b'{"azp": 5}') + ".c",
        "a." + _segment(b"[" * 100000) + ".c",
    ],
)
def test_garbage_is_not_jwt(value: str) -> None:
    """Стережёт: любой не-JWT — `None`, без исключения наружу."""
    assert issued_for(value) is None


# --- предохранитель боевого режима ------------------------------------------


def _session(store: TokenStore) -> BrokerSession:
    return BrokerSession(store, client=httpx.AsyncClient(), clock=moment, sleep=_no_sleep)


def test_read_token_with_trade_field_does_not_open_live_mode(tmp_path: pathlib.Path) -> None:
    """Стережёт: поле «торговля» на токене для чтения больше не даёт «можно торговать»."""
    session = _session(token_file(tmp_path, fake_jwt(READ), WRITE))
    assert session.can_trade() is False
    with pytest.raises(ReadOnlyToken) as refused:
        session.require_trade_rights()
    assert "по самому токену" in refused.value.technical
    asyncio.run(session.close())


def test_trade_token_with_read_field_is_not_blocked(tmp_path: pathlib.Path) -> None:
    """Стережёт: поле «чтение» на токене для торговли боевой режим не запирает."""
    session = _session(token_file(tmp_path, fake_jwt(WRITE), READ))
    assert session.can_trade() is True
    session.require_trade_rights()
    asyncio.run(session.close())


# --- текст отказа -----------------------------------------------------------


def test_client_mismatch_refusal_names_the_token_type() -> None:
    """Стережёт: «client don't match» человеку — про тип токена, а не общий отказ."""
    error = from_status(
        400,
        where="авторизация",
        body=json.dumps(
            {
                "error": "invalid_grant",
                "error_description": (
                    "Invalid refresh token. Token client and authorized client don't match"
                ),
            }
        ),
    )
    assert isinstance(error, TokenRefused)
    assert "тип токена в файле не совпадает с тем, для которого он выпущен" in error.human


# --- нагрузка не утекает ----------------------------------------------------


def _all_text(error: BaseException) -> str:
    frames = traceback.TracebackException.from_exception(error, capture_locals=True)
    return "".join(frames.format()) + str(error) + repr(error) + getattr(error, "technical", "")


def test_the_payload_reaches_neither_the_log_nor_exceptions(
    tmp_path: pathlib.Path, journal: io.StringIO
) -> None:
    """Стережёт правило 7: из нагрузки JWT наружу выходит только `azp`.

    Проверяются лог (DEBUG) на загрузке с расхождением и на обмене, строка
    человеку, и отказ файла, поднятый **после** разбора токена (испорченная
    дата выпуска): его трассировка с переменными кадров — там, где разобранная
    нагрузка осталась бы, держи её `_build` в своей переменной.
    """
    store = token_file(tmp_path, fake_jwt(READ), WRITE)
    exchanged_form(store)
    notice = store.load().scope_notice or ""

    broken = token_file(tmp_path / "other", fake_jwt(READ), WRITE, issued="испорчено")
    with pytest.raises(TokenFileError) as caught:
        broken.load()

    for where, text in {
        "лог": journal.getvalue(),
        "строка человеку": notice,
        "отказ файла": _all_text(caught.value),
    }.items():
        for marker in (SUB_MARKER, SID_MARKER, "Refresh"):
            assert marker not in text, f"кусок нагрузки JWT ({marker}) в: {where}"
