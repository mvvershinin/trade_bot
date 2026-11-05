"""Дыры, найденные мутацией (test-expert, 05.11.2026): слова человеку, которых не стерёг никто.

Каждый тест назван поведением, которое ломала мутация; мутант с убранной
строкой раньше оставался зелёным.
"""

from __future__ import annotations

import pathlib
from datetime import timedelta

import pytest
import websockets

from broker.secret import Secret
from broker.token_store import TokenStore
from broker.tokens import TokenScope
from engine.contracts import LevelTouch, LevelUnfilled, Side
from engine.ports import ExecutionHalted
from tests.fake_broker import BookOrder, FakeBroker
from tests.test_app_live_executor import (
    BAR0,
    BAR1,
    BAR2,
    arm,
    bar,
    entry,
    heard,
    leave,
    long_open,
    make_rig,
    minute,
    run,
)
from tests.test_app_live_fok import killed, submit_on
from tests.test_broker_session import FAKE_REFRESH, START


@pytest.fixture
def store(tmp_path: pathlib.Path) -> TokenStore:
    directory = tmp_path / "userdata"
    directory.mkdir()
    keeper = TokenStore(directory)
    keeper.save(Secret(FAKE_REFRESH), TokenScope.TRADE, issued_at=START - timedelta(days=1))
    return keeper


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> FakeBroker:
    fake = FakeBroker()
    monkeypatch.setattr(websockets, "connect", fake.connect)
    return fake


@pytest.mark.parametrize(
    ("status", "said", "words"),
    [("8", "Цена заявки вне лимитов", "биржа её отклонила"), ("4", "", "биржа её сняла")],
    ids=["отклонена со словами биржи", "снята"],
)
def test_the_unfilled_level_event_tells_the_human_why_the_exchange_killed_the_fok(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker,
    *, status: str, said: str, words: str,
) -> None:
    """Р-10: событие и строка человеку несут причину снятия, слова биржи — как есть.

    Мутация молчания: убрать из причины события `«слова биржи»` или различие
    «отклонила / сняла» — движок получил бы безликое «не исполнился».
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(95.0, LevelTouch.FALL))
            rig.clock.now = BAR0 + timedelta(minutes=20)
            rig.minutes.append(minute(BAR0 + timedelta(minutes=2), low=94.0))
            rig.exchange.plans.append(killed(status, said))
            await rig.executor.watch()
            await heard()
            [event] = await rig.executor.fills_at(bar(BAR0 + timedelta(minutes=10)))
            assert isinstance(event, LevelUnfilled)
            assert words in event.reason, event.reason
            if said:
                assert f"«{said}»" in event.reason, event.reason
                assert f"«{said}»" in " ".join(rig.notices), rig.notices

    run(scenario())


def _partial_then_outside_cancel(fake: FakeBroker, order: BookOrder) -> None:
    """Исполнилась 1 из 2, остаток сняли руками в приложении брокера."""
    fake.execute(order.key, 1, 100.0)
    fake.cancel_outside(order.key)


def test_an_order_cancelled_outside_after_a_partial_fill_says_how_much_was_filled(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Остановка «снята не программой» называет, на сколько заявка успела исполниться."""

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            rig.exchange.plans.append(_partial_then_outside_cancel)
            with pytest.raises(ExecutionHalted) as halted:
                await submit_on(rig, entry(volume=2), close=100.0)
            text = halted.value.human
            assert "снята не программой" in text, text
            assert "успев исполниться на 1" in text, text

    run(scenario())


def test_a_limit_exit_is_waited_for_from_the_candle_after_the_signal_not_from_the_signal(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """§6 п. 18 (П2): ожидание выхода с пределом считается с T+1, а не с T.

    Выход решён по свече T (закрылась в `submitted_at`) и стоит у брокера живой
    DAY-заявкой. Свеча T, названная исполнителю ещё раз, — не ожидание: за ней
    ни одной новой свечи не было. Ждём `limit_exit_bars` (2) закрытых свечей
    T+1 и T+2: после T+1 робот не стоит, после T+2 — стоит, слова «2 свечи».
    Мутация: считать с T — остановка пришла бы на свече T+1.
    """
    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await submit_on(rig, leave(Side.LONG, at=BAR1, limit=98.0), close=100.0)
            assert rig.executor.halted is None
            await rig.executor.fills_at(bar(BAR0))
            assert rig.executor.halted is None, "свеча сигнала посчитана как ожидание"
            await rig.executor.fills_at(bar(BAR1))
            assert rig.executor.halted is None, rig.executor.halted
            with pytest.raises(ExecutionHalted):
                await rig.executor.fills_at(bar(BAR2))
            assert "2 свечи" in (rig.executor.halted or ""), rig.executor.halted

    run(scenario())


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_the_order_offset_of_the_executor_is_a_positive_finite_percent(bad: float) -> None:
    """Нулевой процент — это FOK по самой цене: не «отступ», и принять его молча нельзя.

    Мутация: пропустить ноль (`fok_percent < 0` вместо `<= 0`).
    """
    from app.live_executor import LiveLimits

    with pytest.raises(ValueError, match="процент"):
        LiveLimits(fok_percent=bad)
