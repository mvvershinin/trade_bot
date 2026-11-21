"""Дыры 2г, найденные мутацией (test-expert, 05.11.2026): снимок счёта и окно."""

from __future__ import annotations

import pathlib
from datetime import timedelta

import pytest
import websockets

from app.fact import FactRefused
from app.live_trading import _same_account
from engine import AccountFunds
from tests.fake_broker import FakeBroker
from tests.test_app_live_funds_gate import UNREAD, _stream_only
from tests.test_app_live_pair import (
    LIVE_FROM,
    NO_TAKE,
    _bench,
    _close,
    _connect,
    _ride,
    account_read,
)


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> FakeBroker:
    fake = FakeBroker()
    monkeypatch.setattr(websockets, "connect", fake.connect)
    return fake


def test_the_window_is_told_the_moment_the_first_snapshot_removes_the_unread_reason(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """Причина «счёт не прочитан» снимается в окне сразу, а не когда-нибудь потом.

    Окно узнаёт о боевом состоянии сигналом `live_changed`; без него кнопка
    «Включить бой» оставалась бы серой, пока что-то другое не обновит снимок.
    Мутация: убрать публикацию при первом снимке (`HistoryPort.account_read`).
    """
    told: list[object] = []

    async def go() -> None:
        bench, session = await _bench(tmp_path, broker, NO_TAKE)
        bench.port.live_changed.connect(told.append)
        try:
            await _stream_only(bench)
            told.clear()
            account_read(bench)
            await bench.port.wait()
        finally:
            await _close(bench, session)

    loop.run_until_complete(go())
    assert told, "окну не сказали, что счёт прочитан"
    assert not any(UNREAD in one for one in told[-1].why_not), told[-1].why_not  # type: ignore[attr-defined]


def test_an_empty_account_number_is_refused_as_not_read_not_as_another_account() -> None:
    """Пустой номер договора — «счёт не прочитан», а не «договор разошёлся».

    Человеку нужна верная причина: с пустым номером нечего сверять, и слова
    «прочитано по договору … а заявки пошли бы по …» увели бы не туда.
    """
    with pytest.raises(FactRefused) as refused:
        _same_account("", "ACC-1")
    assert "ещё не прочитан" in str(refused.value), str(refused.value)


def test_a_later_snapshot_reaches_both_engines_of_a_running_pair(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """Снимок после включения пары доходит до обоих движков, а не только первый.

    Свободные средства движок берёт из последнего снимка (`Engine.on_funds`);
    если пара получает деньги лишь при включении, проверка средств работала бы
    по устаревшим числам. Мутация: не отдавать снимок паре в `account_read`.
    """

    async def go() -> tuple[float, float]:
        bench, session = await _bench(tmp_path, broker, NO_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, until=LIVE_FROM + timedelta(minutes=5))
            bench.port.account_read(
                broker.account,
                AccountFunds(
                    at=bench.exchange.now, equity=1_000_000.0, free=123_456.0
                ),
            )
            await _ride(
                bench, since=LIVE_FROM + timedelta(minutes=6),
                until=LIVE_FROM + timedelta(minutes=20),
            )
            observer = bench.port._watch.observer  # noqa: SLF001
            assert observer is not None and observer._fact is not None  # noqa: SLF001
            fact = observer._fact.engine.state.funds  # noqa: SLF001
            forecast = observer._engine.state.funds  # noqa: SLF001
            assert fact is not None and forecast is not None
            return fact.free, forecast.free
        finally:
            await _close(bench, session)

    assert loop.run_until_complete(go()) == (123_456.0, 123_456.0)
