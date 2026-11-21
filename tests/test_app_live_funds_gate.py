"""Э4 2г: бой не включается, пока счёт у брокера не прочитан (`D-044`, Р-9).

Что стережёт файл — поведение порта и пары, а не проводку: проводку опроса
счёта в порт стережёт `tests/test_app_live_feed.py`
(`test_the_program_assembly_hands_the_account_snapshot_to_the_port`).

* без первого снимка счёта с подключения бой не включается, причина названа
  словами, к брокеру не уходит ни одной команды; после снимка — включается;
* отключение потока забывает снимок: «с подключения», а не «когда-то»;
* снимок другого договора, чем у исполнителя, — отказ сборки словами;
* снимок получают **оба** движка пары, строка «Размер счёта принят» — одна
  на дату у каждого, сколько бы раз опрос ни читал счёт;
* наблюдение (ход без боя) снимка не получает (решение 0037).

Стенд — пара движков 2в (`tests/test_app_live_pair.py`): настоящий порт,
настоящая сборка боевой стороны, исполнитель на `tests/fake_broker.py`.
Снимок кладётся тем вызовом, каким его кладёт опрос счёта в программе
(`HistoryPort.account_read`). Изоляция: своя база и свой `userdata`
во временном каталоге, сети нет.
"""

from __future__ import annotations

import pathlib
from datetime import timedelta

import pytest
import websockets

from engine import AccountFunds, JournalEntry
from tests.fake_broker import FakeBroker
from tests.test_app_live_pair import (
    LIVE_FROM,
    NO_TAKE,
    Bench,
    _bench,
    _close,
    _connect,
    _ride,
    account_read,
)
from ui.models import DecisionRow

#: Начало причины «счёт не прочитан» — так её видит человек в окне и в журнале.
UNREAD = "Счёт у брокера ещё не прочитан ни разу с подключения"
#: Строка движка о принятом размере счёта (`Engine.on_funds`).
TAKEN = "Размер счёта принят"


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> FakeBroker:
    fake = FakeBroker()
    monkeypatch.setattr(websockets, "connect", fake.connect)
    return fake


async def _stream_only(bench: Bench) -> None:
    """Поток включён, прогон прошёл — счёт **не** прочитан, бой не просили."""
    bench.exchange.now = LIVE_FROM - timedelta(seconds=58)
    bench.port.stream(True)
    await bench.port.wait()


def _refusals(bench: Bench) -> str:
    return " ".join(reason for event, reason in bench.notes if event == "Бой не включён")


def test_without_an_account_snapshot_live_stays_off_and_says_why_then_turns_on(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """Стережёт `D-044`: без первого снимка счёта — отказ словами, после — бой.

    Мутация молчания: убрать причину из `_live_blockers` — бой включится без
    снимка, и первая половина проверки упадёт. Мутация «всегда отказ» — упадёт
    вторая половина.
    """

    async def go() -> tuple[str, tuple[str, ...], bool, tuple[str, ...], bool]:
        bench, session = await _bench(tmp_path, broker, NO_TAKE)
        try:
            await _stream_only(bench)
            before = bench.port.live_status().why_not
            bench.port.set_live(True)
            await bench.port.wait()
            refused, off = _refusals(bench), not bench.port.live_status().on
            account_read(bench)
            after = bench.port.live_status().why_not
            bench.port.set_live(True)
            await bench.port.wait()
            return refused, before, off, after, bench.port.live_status().on
        finally:
            await _close(bench, session)

    refused, before, off, after, on = loop.run_until_complete(go())
    assert any(UNREAD in one for one in before), f"окно не видит причины: {before}"
    assert UNREAD in refused, f"отказ без причины «счёт не прочитан»: «{refused}»"
    assert off, "бой включился без снимка счёта"
    assert broker.commands == [], f"без снимка счёта ушли команды: {broker.commands}"
    assert not any(UNREAD in one for one in after), f"снимок пришёл, причина осталась: {after}"
    assert on, "снимок счёта пришёл, а бой так и не включился"


def test_turning_the_stream_off_forgets_the_snapshot(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """Стережёт «с подключения»: после отключения потока прежний снимок не в счёт.

    Мутация: не сбрасывать снимок при отключении — после нового подключения
    бой включался бы по снимку прошлой связи.
    """

    async def go() -> tuple[str, ...]:
        bench, session = await _bench(tmp_path, broker, NO_TAKE)
        try:
            await _stream_only(bench)
            account_read(bench)
            bench.port.stream(False)
            await bench.port.wait()
            bench.port.stream(True)
            await bench.port.wait()
            return bench.port.live_status().why_not
        finally:
            await _close(bench, session)

    why_not = loop.run_until_complete(go())
    assert any(UNREAD in one for one in why_not), (
        f"снимок прошлого подключения засчитан новому: {why_not}"
    )


def test_a_snapshot_of_another_account_keeps_live_off_in_words(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """Стережёт правку 9.3: снимок от другого договора — отказ сборки, команд нет.

    Мутация: убрать сверку договоров в `LiveDesks.open` — бой включится,
    а размер счёта у движков был бы от чужого счёта.
    """

    async def go() -> tuple[str, bool]:
        bench, session = await _bench(tmp_path, broker, NO_TAKE)
        try:
            await _stream_only(bench)
            bench.port.account_read(
                "ДРУГОЙ-ДОГОВОР",
                AccountFunds(at=bench.exchange.now, equity=1_000_000.0, free=900_000.0),
            )
            bench.port.set_live(True)
            await bench.port.wait()
            return _refusals(bench), bench.port.live_status().on
        finally:
            await _close(bench, session)

    refused, on = loop.run_until_complete(go())
    assert not on, "бой включился по снимку чужого договора"
    assert "ДРУГОЙ-ДОГОВОР" in refused and broker.account in refused, refused
    assert broker.commands == [], broker.commands


def test_both_engines_of_the_pair_take_the_snapshot_once_per_date(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """Стережёт правку 9.2: снимок — обоим движкам пары, строка — одна на дату.

    Опрос читает счёт раз в полминуты; строка на каждый снимок превратила бы
    журнал решений в поток данных. Мутации: отдавать снимок только «факту»
    (или только «прогнозу») — у второго строки нет; писать строку на каждый
    снимок — строк больше одной.
    """

    async def go() -> tuple[list[JournalEntry] | None, ...]:
        bench, session = await _bench(tmp_path, broker, NO_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, until=LIVE_FROM + timedelta(minutes=20))
            for _ in range(3):
                account_read(bench)
            await _ride(
                bench, since=LIVE_FROM + timedelta(minutes=21),
                until=LIVE_FROM + timedelta(minutes=40),
            )
            status = bench.port.live_status()
            return tuple(
                None if run is None else list(run.journal)
                for run in (status.fact, status.forecast)
            )
        finally:
            await _close(bench, session)

    fact, forecast = loop.run_until_complete(go())
    for name, journal in (("«факт»", fact), ("«прогноз»", forecast)):
        assert journal is not None, f"у {name} нет хода"
        taken = [row for row in journal if row.event == TAKEN]
        assert len(taken) == 1, f"у {name} строк «{TAKEN}»: {len(taken)}"


def test_observation_without_live_never_gets_the_snapshot(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """Стережёт решение 0037, следствие 1: ход без боя денег счёта не получает.

    Опрос счёта с проводкой 9.1 идёт при любом включённом потоке, но порт
    отдаёт снимок движкам только у пары. Мутация: отдавать и наблюдению —
    наблюдатель откажет вслух (`LiveObserver.funds`) либо в журнале хода
    появится строка о размере счёта.
    """
    journals: list[tuple[DecisionRow, ...]] = []

    async def go() -> None:
        bench, session = await _bench(tmp_path, broker, NO_TAKE)
        bench.port.decisions_replaced.connect(journals.append)
        try:
            await _connect(bench, live=False)
            account_read(bench)
            await _ride(bench, until=LIVE_FROM + timedelta(minutes=20))
            account_read(bench)
            await _ride(
                bench, since=LIVE_FROM + timedelta(minutes=21),
                until=LIVE_FROM + timedelta(minutes=30),
            )
        finally:
            await _close(bench, session)

    loop.run_until_complete(go())
    assert journals, "журнал хода окну не пришёл ни разу"
    assert not [row for row in journals[-1] if row.event == TAKEN], (
        "наблюдение получило размер счёта"
    )
