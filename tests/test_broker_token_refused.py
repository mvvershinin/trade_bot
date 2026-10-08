"""Отказ `invalid_grant` на обмене токена (`B-062`, показ 05.10.2026).

На Windows сервис авторизации отвечал `HTTP 400 invalid_grant`, а программа
выбрасывала `error_description`, называла отказ «ошибкой программы» и не давала
установить причину. Здесь стерегутся четыре поведения, вход подставной
(`httpx.MockTransport`, к брокеру ничего не уходит):

* описание брокера доходит до технического лога уровнем WARNING — то есть
  виден на штатном уровне лога, а не только в отладочном;
* описание, в котором есть **кусок** нашего токена (не целиком и не по
  образцу), снимается целиком — мутация «описание без проверки» роняет тест;
* человеку — «Брокер не принял токен: …», а не «ошибка программы»;
* после отказа сессия к сервису авторизации больше не ходит, пока человек
  не нажмёт «Подключиться» (`allow_token_retry`).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Iterator

import httpx
import pytest

from broker.errors import AuthFailed, BadRequest, TokenRefused, from_status
from broker.redaction import FRAGMENT_HIDDEN, RedactingFormatter, log
from broker.secret import Secret
from broker.session import AUTH_URL, BrokerSession
from broker.token_store import TokenStore
from broker.tokens import TokenScope

#: Подделка токена кабинета. Кусок из её середины ниже — не JWT, без `Bearer`,
#: короче маски длинных кусков: целиком его не узнаёт ни реестр, ни образцы.
FAKE_REFRESH = "FAKE-rfsd-QQQQ-7777-8888-9999-NOT-A-REAL-TOKEN"
FRAGMENT = FAKE_REFRESH[5:19]


def moment() -> datetime:
    return datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path: pathlib.Path) -> TokenStore:
    directory = tmp_path / "userdata"
    directory.mkdir()
    keeper = TokenStore(directory)
    keeper.save(Secret(FAKE_REFRESH), TokenScope.TRADE, issued_at=moment() - timedelta(days=1))
    return keeper


@pytest.fixture
def warnings_log() -> Iterator[io.StringIO]:
    """Технический лог на **штатном** уровне WARNING, тем же форматтером."""
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(RedactingFormatter("%(levelname)s %(message)s"))
    handler.setLevel(logging.WARNING)
    logger = log()
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        yield buffer
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def refusing(description: str, posts: list[httpx.Request]) -> httpx.AsyncClient:
    def reply(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(
            400, json={"error": "invalid_grant", "error_description": description}
        )

    return httpx.AsyncClient(
        base_url="https://example.invalid", transport=httpx.MockTransport(reply)
    )


async def _no_sleep(seconds: float) -> None:
    return None


def exchange_refused(store: TokenStore, description: str) -> TokenRefused:
    posts: list[httpx.Request] = []

    async def scenario() -> TokenRefused:
        async with BrokerSession(
            store, client=refusing(description, posts), clock=moment, sleep=_no_sleep
        ) as session:
            with pytest.raises(TokenRefused) as caught:
                await session.access_token()
            return caught.value

    return asyncio.run(scenario())


def test_the_broker_description_reaches_the_technical_log(
    store: TokenStore, warnings_log: io.StringIO
) -> None:
    """Стережёт: `error_description` дословно в `technical` и в логе уровня WARNING."""
    error = exchange_refused(store, "Token is not active")
    assert "Token is not active" in error.technical, error.technical
    assert "invalid_grant" in error.technical
    written = warnings_log.getvalue()
    assert "Token is not active" in written, f"описания нет в техническом логе: {written}"


def test_a_piece_of_our_token_in_the_description_hides_the_description(
    store: TokenStore, warnings_log: io.StringIO
) -> None:
    """Стережёт: кусок живого токена в описании — описание снято целиком.

    Кусок выбран так, что ни реестр (знает значение только целиком), ни образцы
    (`Bearer`, JWT, поля, длинные куски) его не узнают: тест держится только
    на проверке частей (`redaction._holds_fragment`).
    """
    error = exchange_refused(store, f"Invalid refresh token {FRAGMENT} rejected")
    surfaces = {
        "technical": error.technical,
        "human": error.human,
        "лог": warnings_log.getvalue(),
    }
    for where, text in surfaces.items():
        assert FRAGMENT not in text, f"кусок токена в {where}: {text}"
    assert FRAGMENT_HIDDEN in error.technical, error.technical


def test_the_whole_token_echoed_in_the_description_is_masked(store: TokenStore) -> None:
    """Стережёт: эхо токена целиком в описании не выходит ни в один текст."""
    error = exchange_refused(store, f"token {FAKE_REFRESH} is not valid")
    for text in (error.technical, error.human, str(error), repr(error)):
        assert FAKE_REFRESH not in text
        assert FAKE_REFRESH[:12] not in text


def test_the_owner_reads_that_the_token_was_not_accepted() -> None:
    """Стережёт: окно говорит «брокер не принял токен», а не «ошибка программы».

    Узнанное описание Keycloak переводится, неузнанное показывается дословно.
    """
    known = from_status(
        400,
        where="авторизация",
        body='{"error":"invalid_grant","error_description":"Session not active"}',
    )
    assert isinstance(known, TokenRefused)
    assert isinstance(known, AuthFailed)
    assert not isinstance(known, BadRequest)
    assert known.human.startswith("Брокер не принял токен: сессия токена у брокера закрыта")
    assert "Проверьте токен в кабинете БКС" in known.human
    for wrong in ("ошибка программы", "неверные параметры"):
        assert wrong not in known.human

    unknown = from_status(
        400,
        where="авторизация",
        body='{"error":"invalid_grant","error_description":"Something new happened"}',
    )
    assert "Брокер не принял токен: Something new happened." in unknown.human


def test_a_refused_token_is_not_exchanged_again_until_the_owner_presses_connect(
    store: TokenStore,
) -> None:
    """Стережёт: после `invalid_grant` нет повторных обменов до действия человека.

    Опрос счёта, расписание и поток спрашивают `access_token` каждый на своём
    круге; без запомненного отказа каждый круг — запрос к сервису авторизации.
    """
    posts: list[httpx.Request] = []

    async def scenario() -> None:
        async with BrokerSession(
            store, client=refusing("Token is not active", posts), clock=moment, sleep=_no_sleep
        ) as session:
            for _ in range(3):
                with pytest.raises(TokenRefused):
                    await session.access_token()
            assert len(posts) == 1, f"отказанный токен обменивали снова: {len(posts)}"
            session.allow_token_retry()
            with pytest.raises(TokenRefused) as again:
                await session.access_token()
            assert len(posts) == 2, "кнопка не дала обмену пойти к брокеру снова"
            assert "Token is not active" in again.value.technical

    asyncio.run(scenario())
    assert all(str(request.url) == AUTH_URL for request in posts)


# --- чужой секрет и подделка строки лога в описании ------------------------

#: Короткое чужое значение: реестр его не знает, маска длинных кусков до него
#: не дотягивается (меньше 32 знаков). Узнаёт его только образец `scrub`.
FOREIGN_SHORT = "FAKE-other-77"
#: Длинное чужое значение без точек и без `Bearer`: ни реестр, ни образцы
#: `scrub` его не узнают — только маска длинных сплошных кусков.
FOREIGN_LONG = "FAKEforeign" + "0" * 30


def _refused_with(description: str) -> TokenRefused:
    error = from_status(
        400,
        where="авторизация",
        body=json.dumps({"error": "invalid_grant", "error_description": description}),
    )
    assert isinstance(error, TokenRefused), error
    return error


@pytest.mark.parametrize(
    "description",
    [f"refresh_token={FOREIGN_SHORT} rejected", f"Bearer {FOREIGN_SHORT} rejected"],
)
def test_a_foreign_secret_by_pattern_does_not_leave_with_the_description(
    description: str,
) -> None:
    """Стережёт: значение поля токена и `Bearer …` в описании вырезаются образцом.

    Мутация «описание без `scrub`» роняет тест: реестр значение не знает,
    а для маски длинных кусков оно короткое. ⚠️ `technical` и `human` второй
    раз чистит `BrokerError`, и на них одних мутация зеленела бы; падает она
    на `description` — поле, которое сессия хранит и поднимает снова
    (`BrokerSession._refused`).
    """
    error = _refused_with(description)
    surfaces = {
        "technical": error.technical, "human": error.human, "description": error.description,
    }
    for where, text in surfaces.items():
        assert FOREIGN_SHORT not in text, f"чужое значение в {where}: {text}"
    assert "rejected" in error.technical, "описание снято целиком, а не вычищено"


def test_a_foreign_long_value_in_the_description_is_masked() -> None:
    """Стережёт: длинный сплошной кусок base64url (чужой токен) — маской.

    Мутация «без маски длинных кусков» роняет тест: значение не JWT, не поле
    и не в реестре.
    """
    error = _refused_with(f"Token {FOREIGN_LONG} is not active")
    for where, text in {"technical": error.technical, "human": error.human}.items():
        assert FOREIGN_LONG[:32] not in text, f"чужое длинное значение в {where}: {text}"
    assert "is not active" in error.technical, "описание снято целиком, а не вычищено"


def test_a_newline_in_the_description_cannot_forge_a_log_line(
    store: TokenStore, warnings_log: io.StringIO
) -> None:
    """Стережёт: перенос строки и управляющие знаки описания — пробелом.

    Иначе брокер (или кто-то посередине) дописал бы в технический лог строку,
    которую человек принял бы за нашу. Мутация «без замены переносов» роняет тест.
    """
    forged = "WARNING связь с брокером восстановлена"
    error = exchange_refused(store, f"Token is not active\r\n{forged}\x1b[2K")
    for where, text in {"technical": error.technical, "human": error.human}.items():
        for char in ("\n", "\r", "\x1b"):
            assert char not in text, f"управляющий знак {char!r} в {where}: {text!r}"
    lines = warnings_log.getvalue().splitlines()
    assert not any(line.startswith(forged) for line in lines), (
        "описание брокера подделало отдельную строку технического лога:\n"
        + "\n".join(lines)
    )
