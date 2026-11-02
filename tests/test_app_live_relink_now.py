"""Сверка после связи — в момент связи, а не на закрытии бара (Z1 `/risk` по 2в).

Поток статусов переоткрылся посреди бара. До правки сверка шла на ближайшем
проходе — на закрытии бара, до 15 минут спустя: принудительное закрытие или
ручная сделка за обрыв узнавались с опозданием до бара, а окно всё это время
писало «БОЙ: заявки уходят брокеру». Теперь исполнитель на каждом
переоткрытии просит проход (`Wiring.on_news`), и сверка идёт сразу.

Стенд — пара движков 2в (`tests/test_app_live_pair.py`), переподключение без
пауз (`tests/test_app_live_ride.py`).
"""

from __future__ import annotations

import pathlib
from datetime import timedelta

import pytest
import websockets

from tests.fake_broker import FakeBroker
from tests.test_app_live_pair import (
    _SPIKES,
    BASE,
    FIXED_TAKE,
    LIVE_FROM,
    SYMBOL,
    _bench,
    _close,
    _connect,
    _ride,
)
from tests.test_app_live_ride import _fast_reconnect, _relink


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> FakeBroker:
    fake = FakeBroker()
    monkeypatch.setattr(websockets, "connect", fake.connect)
    return fake


def test_a_mismatch_made_during_the_outage_is_found_when_the_link_returns_not_at_the_bar(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Связь вернулась в 09:50, бар закрывается в 10:00 — расхождение найдено в 09:51.

    Лонг 1 с тейком у брокера; за обрыв у брокера проходит покупка, которую
    программа не видела (кадр потерян). Стережёт: на первой же минутке после
    связи робот остановлен словами «у брокера лонг 2, у робота лонг 1» —
    задолго до закрытия бара; новых заявок к брокеру нет.
    """
    monkeypatch.delitem(_SPIKES, LIVE_FROM + timedelta(minutes=45))

    async def go() -> tuple[str, int, int]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        _fast_reconnect(bench, session)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            sent = len(broker.commands)
            broker.muted = True
            broker.foreign_trade("1", 1, BASE, ticker=SYMBOL)
            reopened = await _relink(broker)
            broker.muted = False
            await _ride(
                bench, LIVE_FROM + timedelta(minutes=51), since=LIVE_FROM + timedelta(minutes=51)
            )
            created = [key for kind, key in broker.commands[sent:] if kind == "create"]
            return bench.port.halted, reopened, len(created)
        finally:
            await _close(bench, session)

    halted, reopened, created = loop.run_until_complete(go())
    assert reopened >= 1, "поток статусов не переоткрылся — проверять нечего"
    assert "у брокера лонг 2, у робота лонг 1" in halted, (
        f"к 09:51 сверки после связи не было: «{halted}»"
    )
    assert created == 0
