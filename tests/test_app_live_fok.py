"""Цена боевых заявок: FOK «текущая ± %», планка биржи, касание после обрыва (решение 0067).

Фаза 2.1 плана координатора `.docs/coordination/coordinator-2026-11-04-1233.md`
(Р-2, Р-4, Р-6, Р-7, Р-10 — сторона исполнителя) и разборы гейтов
`architecture-validator-2026-11-04-1912.md`, `architecture-validator-r10-2026-11-04-1925.md`.
Пункты §6 плана — в докстринге каждого теста: их ломает мутацией test-expert.

Оснастка — та же, что у `tests/test_app_live_executor.py`: подставной брокер
(`tests/fake_broker.py`), журнал команд в настоящей базе во временной папке.
Счёт — по книге подделки: что ушло к брокеру, с какой ценой и каким сроком.
Цены — на сетке `STEP` = 0,5; процент — умолчание `LiveLimits`, 2 %.
"""

from __future__ import annotations

import asyncio
import contextlib
import pathlib
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
import websockets

from app.live_executor import Instrument, LiveLimits
from broker.secret import Secret
from broker.token_store import TokenStore
from broker.tokens import TokenScope
from engine import close_time
from engine.contracts import (
    ExitReason,
    LevelTouch,
    LevelUnfilled,
    OrderAction,
    OrderRequest,
    Side,
)
from engine.ports import ExecutionHalted, ExecutionRefused, Refusal
from market import DecisionLevel, PriceBand
from market.candles import M5, Candle
from tests.fake_broker import ANSWERS, BookOrder, FakeBroker
from tests.test_app_live_executor import (
    ACCOUNT,
    BAR0,
    BAR1,
    BAR2,
    DAY,
    FAST,
    FOK,
    STEP,
    TICKER,
    Plan,
    Rig,
    arm,
    bar,
    disarm,
    entry,
    filled,
    heard,
    leave,
    long_open,
    make_rig,
    minute,
    only_fill,
    resting,
    run,
    take_name,
)
from tests.test_broker_session import FAKE_REFRESH, START

#: Порог свежести исполнителя при `FAST`: свеча 5 мин плюс `stale_after` 60 с.
FRESH = timedelta(minutes=5, seconds=60)

#: Минутка касания уровня 95 у лонга — после обрыва или вовремя, смотря по часам.
TOUCH = BAR0 + timedelta(minutes=1)


@pytest.fixture
def store(tmp_path: pathlib.Path) -> TokenStore:
    """Торговый токен в файле — как у `tests/test_app_live_executor.py`."""
    directory = tmp_path / "userdata"
    directory.mkdir()
    keeper = TokenStore(directory)
    keeper.save(Secret(FAKE_REFRESH), TokenScope.TRADE, issued_at=START - timedelta(days=1))
    return keeper


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> FakeBroker:
    """Подставной брокер: HTTP — книгой заявок, поток статусов — подменой `websockets`."""
    fake = FakeBroker()
    monkeypatch.setattr(websockets, "connect", fake.connect)
    return fake


def closed(start: datetime, close: float) -> Candle:
    """Закрытая пятиминутка с заданным закрытием — свеча сигнала."""
    return Candle(
        time=start, open=close, high=close + 5.0, low=close - 5.0, close=close,
        volume=10.0, timeframe=M5, filled_minutes=5,
    )


async def submit_on(rig: Rig, order: OrderRequest, close: float) -> None:
    """Заявка движка после `fills_at` своей свечи сигнала с закрытием `close` (Р-2, путь А)."""
    given = await rig.executor.fills_at(closed(order.submitted_at - timedelta(minutes=5), close))
    assert list(given) == [], given
    await rig.executor.submit(order)


def killed(status: str, reason: str = "") -> Plan:
    """Биржа сняла FOK целиком: статус `4` без исполнения или `8` со словами."""

    def act(broker: FakeBroker, order: BookOrder) -> None:
        broker.kill(order.key, status, reason)

    return act


def immediate(rig: Rig) -> list[tuple[object, object]]:
    """(сторона, цена) всех заявок «исполнить целиком или снять», ушедших к брокеру."""
    bodies = rig.creates()
    return [(body["side"], body.get("price")) for body in bodies if body["timeInForce"] == FOK]


def market_orders(rig: Rig) -> list[dict[str, object]]:
    """Рыночные заявки, ушедшие к брокеру: после 0067 их не бывает."""
    return [body for body in rig.creates() if body["orderType"] == "1"]


def band(low: float | None, high: float | None, told: str = "MOEX ISS") -> Callable[
    [], Awaitable[PriceBand]
]:
    """Источник планки, отвечающий сразу."""

    async def ask() -> PriceBand:
        return PriceBand(TICKER, high=high, low=low, told=told)

    return ask


# --------------------------------------------------------------- §6 п. 1, 9


@pytest.mark.parametrize(
    ("side", "percent", "price", "deal_side"),
    [
        (Side.LONG, 2.0, 102.5, "1"),   # 100,3 × 1,02 = 102,306 → вверх к 102,5
        (Side.SHORT, 2.0, 98.0, "2"),   # 100,3 × 0,98 = 98,294 → вниз к 98,0
        (Side.LONG, 3.0, 103.5, "1"),   # 100,3 × 1,03 = 103,309 → вверх к 103,5
    ],
)
def test_an_entry_is_a_fok_limit_at_the_signal_close_plus_percent_away_from_the_market(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker,
    *, side: Side, percent: float, price: float, deal_side: str,
) -> None:
    """§6 п. 1 и п. 9: вход — лимитная `timeInForce 2` по «закрытие свечи сигнала ± p %».

    Закрытие свечи сигнала 100,3, шаг 0,5. Покупка — вверх к сетке, продажа —
    вниз: цена не ближе к рынку, чем процент. Процент — из `LiveLimits`
    (окно), 3 % дают другую цену. Мутации: знак, DAY вместо FOK, округление
    к рынку, процент не из пределов.
    """

    async def scenario() -> None:
        limits = replace(FAST, fok_percent=percent)
        async with make_rig(tmp_path, store, broker, limits=limits) as rig:
            rig.exchange.plans.append(filled((1, 100.5)))
            await submit_on(rig, entry(side), close=100.3)
            [body] = rig.creates()
            assert (body["side"], body["orderType"], body["timeInForce"], body["price"]) == (
                deal_side, "2", FOK, price,
            )

    run(scenario())


# --------------------------------------------------------------- §6 п. 2


async def _entry(rig: Rig) -> None:
    rig.exchange.plans.append(filled((1, 100.0)))
    await submit_on(rig, entry(), close=100.0)


async def _signal_exit(rig: Rig) -> None:
    await long_open(rig)
    rig.exchange.plans.append(filled((1, 99.0)))
    await submit_on(rig, leave(), close=100.0)


async def _reversal(rig: Rig) -> None:
    await long_open(rig)
    rig.exchange.plans.extend([filled((1, 99.0)), filled((1, 99.0))])
    await submit_on(rig, leave(), close=100.0)
    await rig.executor.submit(entry(Side.SHORT, at=BAR1))


async def _take_rest(rig: Rig) -> None:
    await long_open(rig, volume=2)
    await rig.executor.submit(arm(110.0, LevelTouch.RISE, volume=2))
    rig.broker.execute(str(rig.creates()[-1]["clientOrderId"]), 1, 110.0)
    await heard()
    rig.exchange.plans.append(filled((1, 109.0)))
    with pytest.raises(ExecutionRefused) as refused:
        await rig.executor.submit(disarm(volume=2))
    assert refused.value.reason is Refusal.ALREADY_TRIGGERED


async def _trailing_touch(rig: Rig) -> None:
    await long_open(rig)
    await rig.executor.submit(arm(95.0, LevelTouch.FALL))
    rig.minutes.append(minute(TOUCH, low=94.0))
    rig.exchange.plans.append(filled((1, 94.0)))
    await rig.executor.watch()


async def _close_all(rig: Rig) -> None:
    await long_open(rig)
    rig.exchange.plans.append(filled((1, 99.0)))
    await rig.executor.close_everything("Проверка: закрыть всё.")


#: Шесть мест Р-2 → что ушло к брокеру немедленными заявками: (сторона, цена).
#: Вход лонга `long_open` — закрытие свечи 100 → покупка 102. Выход по сигналу
#: и переворот — закрытие 100 → продажа 98. Остаток тейка и «закрыть всё» —
#: закрытие последней минутки (09:59, 100) → 98. Касание — уровень 95 → 93,1 → 93.
SIX_POINTS: dict[str, tuple[Callable[[Rig], Awaitable[None]], list[tuple[object, object]]]] = {
    "вход": (_entry, [("1", 102.0)]),
    "выход по сигналу": (_signal_exit, [("1", 102.0), ("2", 98.0)]),
    "переворот": (_reversal, [("1", 102.0), ("2", 98.0), ("2", 98.0)]),
    "остаток тейка": (_take_rest, [("1", 102.0), ("2", 98.0)]),
    "касание трейлинга": (_trailing_touch, [("1", 102.0), ("2", 93.0)]),
    "закрыть всё": (_close_all, [("1", 102.0), ("2", 98.0)]),
}


@pytest.mark.parametrize("point", list(SIX_POINTS))
def test_no_point_of_the_six_sends_a_market_order(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker, point: str
) -> None:
    """§6 п. 2: ни одна из шести точек Р-2 не подаёт рыночную; цена — «текущая ± 2 %».

    Каждая точка — свой сценарий; к брокеру не уходит ни одной заявки
    `orderType 1`, а немедленные — FOK по цене от своей «текущей»: закрытие
    свечи сигнала, уровень касания, закрытие последней минутки. Мутация:
    вернуть рыночную в любую одну точку или взять «текущую» не оттуда.
    """
    scenario_of, expected = SIX_POINTS[point]

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await scenario_of(rig)
            assert market_orders(rig) == [], f"{point}: ушла рыночная заявка"
            assert immediate(rig) == expected, f"{point}: {rig.creates()}"

    run(scenario())


# --------------------------------------------------------------- §6 п. 3


@pytest.mark.parametrize(
    ("status", "reason"), [("4", ""), ("8", "Цена заявки вне лимитов")]
)
@pytest.mark.parametrize("closing", [False, True], ids=["вход", "выход"])
def test_a_fok_killed_whole_halts_in_its_status_words_with_the_fok_tail(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker,
    *, status: str, reason: str, closing: bool,
) -> None:
    """§6 п. 3: FOK снята целиком (`4` без исполнения и отдельно `8`) — остановка словами.

    В словах: «FOK», цена поданной заявки, «позиция у брокера та, что была
    до неё»; у выхода — «открыта без тейка» (6.2); у `8` — слова биржи как
    есть (6.1). Позиция у брокера не изменилась. Мутации молчания: общие слова
    вместо хвоста, отсутствие остановки.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            if closing:
                await long_open(rig)
            before = rig.broker.position(TICKER)
            rig.exchange.plans.append(killed(status, reason))
            order = leave() if closing else entry()
            with pytest.raises(ExecutionHalted) as halted:
                await submit_on(rig, order, close=100.0)
            text = halted.value.human
            price = "98" if closing else "102"
            assert "FOK" in text and f"по {price}" in text, text
            assert "позиция у брокера та, что была до неё" in text, text
            assert ("открыта без тейка" in text) is closing, text
            if reason:
                assert f"«{reason}»" in text, text
            assert rig.executor.halted == text
            assert rig.broker.position(TICKER) == before

    run(scenario())


# --------------------------------------------------------------- §6 п. 4, Р-10 граница


@pytest.mark.parametrize(
    ("answer", "who"),
    [(killed("4"), "биржа её сняла"), (resting, "программа её сняла")],
    ids=["снята биржей", "без ответа, снята программой"],
)
@pytest.mark.parametrize(
    ("shift", "late"),
    [(-timedelta(seconds=1), False), (timedelta(seconds=1), True)],
    ids=["вовремя", "после обрыва"],
)
def test_a_killed_trailing_fok_halts_in_time_and_becomes_an_event_after_the_freshness_limit(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker,
    *, shift: timedelta, late: bool, answer: Plan, who: str,
) -> None:
    """§6 п. 4 и §3 «граница порога свежести»: касание судится по обе стороны порога.

    Касание уровня 95 в минутке 10:01, выход — FOK от уровня (93). Подан
    раньше порога свежести от минутки касания и снят — остановка Р-6 со
    словами «FOK» и «открыта без тейка», события нет. Подан позже порога
    и снят — остановки нет, движку событие `LevelUnfilled`, сделки касания
    нет. Мутация: принять касание вовремя за «после обрыва» и наоборот.

    «Снята» — двумя путями: биржа сняла сама (`4`), или ответа нет за
    `fill_wait` и программа сняла её сама, ни контракта не исполнив (ревью
    10.11, находка 3). Второй путь судится так же: вовремя — остановка,
    после обрыва — событие, и его слова называют, кто снял (`/risk` 10.11, 4).
    Мутации (test-expert 10.11): снятая нами «пощажённая» — остановка; снятая
    нами обычная — событие без остановки; «биржа сняла» о снятой нами.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(95.0, LevelTouch.FALL))
            rig.minutes.append(minute(TOUCH, low=94.0))
            rig.clock.now = TOUCH + FRESH + shift
            rig.exchange.plans.append(answer)
            await rig.executor.watch()
            assert immediate(rig)[-1] == ("2", 93.0)
            assert not [order for order in rig.broker.orders if order.live], rig.broker.orders
            if not late:
                assert rig.executor.halted is not None
                assert "FOK" in rig.executor.halted and "открыта без тейка" in rig.executor.halted
                with pytest.raises(ExecutionHalted) as halted:
                    await rig.executor.fills_at(bar(BAR0))
                assert halted.value.unfilled == ()
                return
            assert rig.executor.halted is None, rig.executor.halted
            [event] = await rig.executor.fills_at(bar(BAR0))
            assert isinstance(event, LevelUnfilled), event
            assert (event.order_id, event.at) == (take_name(), TOUCH)
            assert who in event.reason, event.reason
            assert rig.broker.position(TICKER) == 1

    run(scenario())


# --------------------------------------------------------------- Р-10: событие, §6 п. 14


async def _killed_after_outage(rig: Rig) -> None:
    """Лонг, сторож 95; минутка касания 10:02 видна в 10:20 — выход после обрыва снят."""
    await long_open(rig)
    await rig.executor.submit(arm(95.0, LevelTouch.FALL))
    rig.clock.now = BAR0 + timedelta(minutes=20)
    rig.minutes.append(minute(BAR0 + timedelta(minutes=2), low=94.0))
    rig.exchange.plans.append(killed("4"))
    await rig.executor.watch()
    await heard()


def test_the_event_waits_for_a_fresh_closed_candle_and_the_guard_answers_already_triggered(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """§6 п. 14 и Р-10 п. 1, 3: от снятия FOK до отдачи события сторож держит имя.

    * вооружение и снятие этого имени — `ALREADY_TRIGGERED`, к брокеру ничего;
    * по незакрытой свече события нет (Н4), по закрытой несвежей — тоже:
      рождённый движком выход по ней был бы отвергнут как несвежий;
    * по первой свежей закрытой — событие, и в том же ответе нет сделки касания;
    * человеку — строка исполнителя: уровень, время касания, цена FOK, решение
      за роботом;
    * после отдачи сторожа нет: вооружение того же имени принимается.

    Мутации: принять вооружение; убрать сторожа из `_takes` раньше отдачи;
    отдать сделку касания; отдать событие по незакрытой свече.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await _killed_after_outage(rig)
            assert rig.executor.halted is None, rig.executor.halted
            assert rig.executor.undelivered == (), "сделка касания ждёт отдачи движку"
            said = " ".join(rig.notices)
            assert "95" in said and "10:02" in said, said
            assert "по цене 93: текущая 95 (уровень касания) − 2 %" in said, said
            sent = len(rig.creates())
            later = BAR0 + timedelta(minutes=15)
            for order in (arm(96.0, LevelTouch.FALL, at=later), disarm(at=later)):
                with pytest.raises(ExecutionRefused) as refused:
                    await rig.executor.submit(order)
                assert refused.value.reason is Refusal.ALREADY_TRIGGERED
            assert len(rig.creates()) == sent
            assert await rig.executor.fills_at(bar(later, unsettled=True)) == []
            assert await rig.executor.fills_at(bar(BAR0)) == [], "событие по несвежей свече"
            given = await rig.executor.fills_at(bar(BAR0 + timedelta(minutes=10)))
            assert [type(item) for item in given] == [LevelUnfilled], given
            [event] = given
            assert isinstance(event, LevelUnfilled)
            assert (event.order_id, event.at) == (take_name(), BAR0 + timedelta(minutes=2))
            assert "по цене 93" in event.reason
            assert await rig.executor.fills_at(bar(BAR0 + timedelta(minutes=10))) == []
            await rig.executor.submit(arm(96.0, LevelTouch.FALL, at=later))
            assert len(rig.creates()) == sent
            assert rig.broker.position(TICKER) == 1

    run(scenario())


def test_a_halt_while_the_event_waits_carries_it_to_the_engine(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Р-10 п. 1: остановка, пока событие не отдано, уносит его в `ExecutionHalted.unfilled`.

    Позиция у брокера открыта без сторожа — движок обязан назвать это строкой
    журнала, а не потерять. Мутация: не класть неотданные события в остановку.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await _killed_after_outage(rig)
            rig.broker.foreign_trade("1", 1, 100.0)
            await heard()
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.fills_at(bar(BAR0))
            assert halted.value.fills == ()
            assert [event.order_id for event in halted.value.unfilled] == [take_name()]

    run(scenario())


def test_the_exit_born_by_the_event_goes_by_the_band_as_a_day_limit(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """§6 п. 12, сторона исполнителя: рождённый выход — путём `limit_price`, по планке, DAY.

    Движок рождает выход со временем закрытия свечи обмена (Н1): сверка
    свечи сигнала проходит, планка годна — продажа по `LOWLIMIT`, срок «до
    конца дня». Мутация: время рождённой — момент события (отказ «нет цены
    свечи сигнала»); рыночная или FOK вместо дневной.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker, band=band(90.0, 110.0)) as rig:
            await _killed_after_outage(rig)
            candle = bar(BAR0 + timedelta(minutes=10))
            [event] = await rig.executor.fills_at(candle)
            born = OrderRequest(
                action=OrderAction.CLOSE, side=Side.LONG, volume=1,
                submitted_at=close_time(candle), reason="уровень не исполнился",
                order_id=f"level-day:{take_name()}", exit_reason=ExitReason.LEVEL_UNFILLED,
                limit_price=98.0,
            )
            await rig.executor.submit(born)
            body = rig.creates()[-1]
            assert (body["side"], body["timeInForce"], body["price"]) == ("2", DAY, 90.0)
            assert rig.executor.halted is None

    run(scenario())


# --------------------------------------------------------------- §6 п. 6: планка


@pytest.mark.parametrize(
    ("side", "deal_side", "price", "words"),
    [
        (Side.LONG, "2", 90.5, "по цене 90.5: по нижней планке биржи"),     # 90,2 → вверх
        (Side.SHORT, "1", 109.5, "по цене 109.5: по верхней планке биржи"),  # 109,8 → вниз
    ],
)
def test_the_window_end_exit_goes_by_the_band_moved_inward_to_the_grid(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker,
    *, side: Side, deal_side: str, price: float, words: str,
) -> None:
    """§6 п. 6 и правки 4.2, 4.3: биржа ответила — продажа по `LOWLIMIT`, покупка по `HIGHLIMIT`.

    Границы вне сетки (90,2 и 109,8 при шаге 0,5) кладутся на сетку **внутрь**
    коридора: округление от рынка вывело бы цену за коридор. Срок — DAY.
    Человеку — строка «подана по планке, предел движка не подан». Остановка
    по неисполнению называет поданную цену, а не предел движка. Мутации:
    перепутать границы, округлить от рынка, молча подменить.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker, band=band(90.2, 109.8)) as rig:
            rig.exchange.plans.append(filled((1, 100.0)))
            await submit_on(rig, entry(side), close=100.0)
            assert only_fill(await rig.executor.fills_at(bar(BAR0, unsettled=True)))
            limit = 98.0 if side is Side.LONG else 102.0
            await submit_on(rig, leave(side, limit=limit), close=100.0)
            body = rig.creates()[-1]
            assert (body["side"], body["orderType"], body["timeInForce"], body["price"]) == (
                deal_side, "2", DAY, price,
            )
            await heard()
            said = " ".join(rig.notices)
            assert words in said and f"предел движка {limit:g} не подан" in said, said
            assert await rig.executor.fills_at(bar(BAR1)) == []
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.fills_at(bar(BAR2))
            assert words in halted.value.human, halted.value.human
            assert str(body["clientOrderId"]) in halted.value.human

    run(scenario())


async def _silent() -> PriceBand:
    await asyncio.Event().wait()
    raise AssertionError("молчащая биржа ответила")


async def _closed_worker() -> PriceBand:
    raise RuntimeError("поток данных закрыт")


@pytest.mark.parametrize(
    ("source", "words"),
    [
        (_silent, "биржа не ответила за 0.3 с"),
        (band(100.5, 110.0), "не охватывает текущую цену 100"),
        (band(None, None, "биржа не дала планку MXZ6: нет строки RFUD"), "нет строки RFUD"),
        (_closed_worker, "планку спросить не удалось (RuntimeError)"),
        (None, "источник планки не подключён"),
    ],
    ids=["молчит", "по ту сторону", "негодна", "сбой", "нет источника"],
)
def test_without_a_usable_band_the_engine_limit_goes_in_time_with_its_line(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker,
    *, source: Callable[[], Awaitable[PriceBand]] | None, words: str,
) -> None:
    """§6 п. 6: планки нет — подан предел движка, не позже `band_wait`, и строка «откуда».

    Биржа молчит дольше `band_wait`, планка по ту сторону от закрытия, планки
    нет в ответе, источник упал, источника нет — во всех случаях к брокеру
    уходит дневная по пределу движка (99), а человеку — строка с причиной.
    Мутации: ждать без срока; подать планку по ту сторону; подменить молча.
    """

    async def scenario() -> None:
        limits = replace(FAST, band_wait=0.3)
        async with make_rig(tmp_path, store, broker, limits=limits, band=source) as rig:
            await long_open(rig)
            loop = asyncio.get_running_loop()
            started = loop.time()
            # Срок — и у самой проверки: исполнитель, ждущий планку без срока,
            # повесил бы тест, а не уронил его.
            await asyncio.wait_for(submit_on(rig, leave(limit=99.0), close=100.0), 0.3 + 2.0)
            waited = loop.time() - started
            body = rig.creates()[-1]
            assert (body["side"], body["timeInForce"], body["price"]) == ("2", DAY, 99.0)
            assert waited < 0.3 + 1.0, f"выход ждал планку {waited:.2f} с"
            await heard()
            said = " ".join(rig.notices)
            assert "по цене 99: предел движка" in said and words in said, said

    run(scenario())


# --------------------------------------------------------------- Р-2: свеча сигнала


@pytest.mark.parametrize("unsettled", [False, True], ids=["свеча раньше", "незакрытая"])
def test_an_order_without_its_signal_candle_is_not_sent(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker, unsettled: bool
) -> None:
    """§3 «текущая не от свечи сигнала»: заявка свечи T при последнем `fills_at(T−1)` — не подана.

    Заявка решена по свече, закрывшейся в 10:00, а последняя закрытая свеча
    у исполнителя закрылась в 09:55 — цены свечи сигнала нет: отказ словами
    с обоими моментами, к брокеру ничего. Незакрытая свеча сигналом не
    становится. Мутация: взять закрытие любой последней свечи.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await rig.executor.fills_at(closed(BAR0 - timedelta(minutes=10), 100.0))
            if unsettled:
                await rig.executor.fills_at(bar(BAR0 - timedelta(minutes=5), unsettled=True))
            with pytest.raises(ExecutionRefused) as refused:
                await rig.executor.submit(entry())
            text = str(refused.value)
            assert "Нет цены свечи сигнала" in text and "10:00" in text and "09:55" in text
            assert rig.creates() == []
            assert rig.executor.halted is None

    run(scenario())


# --------------------------------------------------------------- §6 п. 5: «закрыть всё»


def test_close_all_prices_from_the_last_recorded_minute(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """§6 п. 5: «закрыть всё» — FOK от закрытия последней записанной минутки.

    Последняя минутка 10:03 закрылась на 101,3: продажа 101,3 × 0,98 = 99,274 →
    вниз к 99,0. Мутация: взять не последнюю минутку или закрытие свечи.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            rig.minutes.append(minute(BAR0 + timedelta(minutes=2), low=99.0))
            last = minute(BAR0 + timedelta(minutes=3), low=99.0)
            rig.minutes.append(replace(last, close=101.3, high=101.5))
            rig.clock.now = BAR0 + timedelta(minutes=4)
            rig.exchange.plans.append(filled((1, 101.0)))
            text = await rig.executor.close_everything("Проверка.")
            assert immediate(rig)[-1] == ("2", 99.0)
            assert "закрыта заявкой FOK" in text, text

    run(scenario())


#: Подставная планка: 90,2–109,8 при шаге 0,5 — продажа по 90,5, покупка по 109,5.
GOOD_BAND = band(90.2, 109.8)


async def _stale_close_all(rig: Rig) -> str:
    """Лонг; последняя минутка 09:59 к 10:07 несвежая (ей 8 мин) — «закрыть всё»."""
    await long_open(rig)
    rig.clock.now = BAR0 + timedelta(minutes=7)
    rig.exchange.plans.append(filled((1, 99.0)))
    return await rig.executor.close_everything("Проверка.")


async def _stale_take_rest(rig: Rig) -> str:
    """Тейк на 2 исполнен на 1; к снятию в 10:07 минутка 09:59 несвежая — остаток FOK."""
    await long_open(rig, volume=2)
    await rig.executor.submit(arm(110.0, LevelTouch.RISE, volume=2))
    rig.broker.execute(str(rig.creates()[-1]["clientOrderId"]), 1, 110.0)
    await heard()
    rig.clock.now = BAR0 + timedelta(minutes=7)
    rig.exchange.plans.append(filled((1, 99.0)))
    with pytest.raises(ExecutionRefused) as refused:
        await rig.executor.submit(disarm(volume=2, at=BAR1))
    assert refused.value.reason is Refusal.ALREADY_TRIGGERED
    return ""


@pytest.mark.parametrize("scenario_of", [_stale_close_all, _stale_take_rest],
                         ids=["закрыть всё", "остаток тейка"])
@pytest.mark.parametrize(
    ("source", "price", "words"),
    [
        (GOOD_BAND, 90.5, "по цене 90.5: по нижней планке биржи (LOWLIMIT, MOEX ISS), FOK"),
        (None, 98.0, "текущая 100 (закрытие минутки 09:59, несвежей — ей 8 мин) − 2 %, FOK — "
                     "планки нет: источник планки не подключён"),
        (band(None, None, "биржа не дала планку"), 98.0,
         "ей 8 мин) − 2 %, FOK — планки нет: биржа не дала"),
    ],
    ids=["по планке", "планки нет", "планка негодна"],
)
def test_a_stale_minute_still_sends_the_fok_by_the_band_or_by_the_stale_price(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker,
    *, scenario_of: Callable[[Rig], Awaitable[str]],
    source: Callable[[], Awaitable[PriceBand]] | None, price: float, words: str,
) -> None:
    """0067 «Последняя минутка несвежая» (владелец 04.11, `/risk` 22:35 находка 1).

    «Закрыть всё» и остаток тейка при минутке старше порога свежести заявку
    **подают**: FOK по планке биржи к сетке внутрь коридора; планки нет или
    она негодна — FOK от несвежей минутки ± %, и в словах её возраст.
    Строка «подана по цене…» в журнале решений говорит, откуда цена.
    Мутации: не подавать при несвежей минутке; подать по несвежей минутке,
    когда планка есть; промолчать о возрасте или о планке.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker, band=source) as rig:
            await scenario_of(rig)
            await heard()
            assert immediate(rig)[-1] == ("2", price), rig.creates()
            told = [note for note in rig.told_orders() if f"по цене {price:g}" in note]
            assert told and words in told[-1], rig.notices
            # Возраст минутки назван в строке при любом источнике цены (мутация:
            # причина «несвежая» выпала из слов планки).
            assert "ей 8 мин" in told[-1], told[-1]
            assert rig.broker.position(TICKER) == 0

    run(scenario())


def test_close_all_without_any_price_sends_nothing_and_the_button_works_again(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Цены нет вовсе — ни минуток за сутки, ни планки: заявки нет, «закрыть всё» не сгорает.

    Слова говорят, почему заявки нет и что закрывать придётся самому.
    Флаг «закрыть всё уже пыталась» ставится только поданной заявкой
    (`/risk` 22:35, находка 1): минутки появились — следующий вызов кнопки
    «Закрыть всё» подаёт FOK, а не отвечает «уже пыталась». Мутация: поднять
    флаг до попытки; молча не подать.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            sent = len(rig.creates())
            rig.minutes.clear()
            first = await rig.executor.close_everything("Проверка.")
            assert len(rig.creates()) == sent, "заявка ушла без цены"
            assert "не записано ни одной минутки" in first and "Заявка не подана" in first
            assert "Закройте её сами" in first, first
            # Слова называют и то, почему нет планки (мутация: причина выпала).
            assert "источник планки не подключён" in first, first
            rig.minutes.append(minute(BAR0 + timedelta(minutes=1), low=99.0))
            rig.exchange.plans.append(filled((1, 99.0)))
            again = await rig.executor.close_everything("Проверка ещё раз.")
            assert "уже пыталась" not in again, again
            assert immediate(rig)[-1] == ("2", 98.0)
            assert rig.broker.position(TICKER) == 0
            third = await rig.executor.close_everything("И ещё раз.")
            assert "уже пыталась" in third, "после поданной заявки закрыть всё подано снова"
            assert len(immediate(rig)) == 2

    run(scenario())


def test_a_close_all_refused_by_the_broker_does_not_burn_the_button(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Заявка записана, но брокер её отверг (400): она «не ушла» — «закрыть всё» не сгорает.

    Флаг «уже пыталась» ставится только принятой заявкой. Мутация: ставить его
    и от отвергнутой — второе нажатие ответит «уже пыталась» и позицию не закроет.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            rig.broker.create_answers.append(ANSWERS["не принята, 400"])
            first = await rig.executor.close_everything("Проверка.")
            assert "закрыть не удалось" in first, first
            assert rig.broker.position(TICKER) == 1
            rig.exchange.plans.append(filled((1, 99.0)))
            again = await rig.executor.close_everything("Проверка ещё раз.")
            assert "уже пыталась" not in again, again
            assert rig.broker.position(TICKER) == 0, again

    run(scenario())


# --------------------------------------------------------------- строка о каждой заявке


async def _every_kind(rig: Rig) -> None:
    """Все виды боевых заявок подряд: вход, тейк, выход по концу окна, вход, сторож, закрыть всё."""
    await long_open(rig)
    await rig.executor.submit(arm(110.0, LevelTouch.RISE))
    await rig.executor.submit(disarm())
    await submit_on(rig, leave(limit=98.0), close=100.0)
    exit_key = str(rig.creates()[-1]["clientOrderId"])
    rig.broker.execute(exit_key, 1, 99.0)
    await heard()
    assert only_fill(await rig.executor.fills_at(bar(BAR1)))
    rig.clock.now = BAR1 + timedelta(seconds=10)
    rig.exchange.plans.append(filled((1, 100.0)))
    await submit_on(rig, entry(at=BAR2), close=100.0)
    await rig.executor.submit(arm(95.0, LevelTouch.FALL, at=BAR2))
    rig.minutes.append(minute(BAR2 + timedelta(minutes=1), low=94.0))
    rig.clock.now = BAR2 + timedelta(minutes=1, seconds=10)
    rig.exchange.plans.append(filled((1, 94.0)))
    await rig.executor.watch()
    await heard()


def test_every_live_order_has_its_line_with_price_and_source_in_the_decision_journal(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """0067, решение владельца 04.11: на **каждую** боевую заявку — строка в журнал решений.

    «Заявка (что) подана по цене X: откуда цена» — FOK по планке биржи,
    тейк по уровню движка, выход по концу окна по планке биржи. Строк ровно
    столько, сколько заявок ушло к брокеру, и цена в строке — цена в теле
    заявки. Технический лог сохраняет ту же строку уровнем INFO, журнал
    решений — тоже INFO: штатная строка не мигает предупреждением на каждом
    перевороте (решение владельца 10.11). Мутации молчания: убрать строку
    у любого вида заявки; назвать цену не ту, что ушла; поднять уровень.
    """

    async def scenario() -> None:
        caplog.set_level("INFO", logger="app.live_executor")
        async with make_rig(tmp_path, store, broker, band=GOOD_BAND) as rig:
            await _every_kind(rig)
            prices = [body["price"] for body in rig.creates()]
            told = rig.told_orders()
            assert len(told) == len(prices), (prices, told)
            journal = [level for text, level in rig.levels if " подана по цене " in text]
            assert journal == [DecisionLevel.INFO] * len(prices), rig.levels
            for price, line in zip(prices, told, strict=True):
                assert f"подана по цене {price:g}:" in line, (price, line)
            sources = (
                "по верхней планке биржи (HIGHLIMIT, MOEX ISS; текущая 100 — закрытие свечи "
                "сигнала), FOK",
                "уровень тейка движка",
                "по нижней планке биржи",
                "по нижней планке биржи (LOWLIMIT, MOEX ISS; текущая 95 — уровень касания), FOK",
            )
            for source in sources:
                assert any(source in line for line in told), (source, told)
            logged = [
                record for record in caplog.records
                if " подана по цене " in record.getMessage()
            ]
            assert len(logged) == len(prices)
            assert {record.levelname for record in logged} == {"INFO"}

    run(scenario())


def test_an_instrument_without_a_price_step_is_refused() -> None:
    """Шаг цены не больше нуля — отказ сразу: цену FOK не положить на сетку (Р-3)."""
    for step in (0.0, -0.5, float("nan")):
        with pytest.raises(ValueError, match="шаг цены"):
            Instrument(TICKER, "SPBFUT", 5, ACCOUNT, step)
    assert Instrument(TICKER, "SPBFUT", 5, ACCOUNT, STEP).price_step == STEP
    assert LiveLimits().fok_percent == 2.0


# ------------------------------------------- FOK по планке биржи (владелец 10.11.2026)


@pytest.mark.parametrize(
    ("source", "price", "words"),
    [
        (band(90.0, 101.5), 101.5, "по цене 101.5: по верхней планке биржи (HIGHLIMIT"),
        (band(90.2, 101.8), 101.5, "по цене 101.5: по верхней планке биржи (HIGHLIMIT"),
        (band(80.0, 95.0), 102.5, "+ 2 %, FOK — планка биржи не годится: планка 80–95 не "
                                  "охватывает"),
        (None, 102.5, "+ 2 %, FOK — планка биржи не годится: источник планки не подключён"),
        (_silent, 102.5, "+ 2 %, FOK — планка биржи не годится: биржа не ответила за 0.3 с"),
    ],
    ids=["у края планки", "край вне сетки", "планка не охватывает", "планки нет", "молчит"],
)
def test_an_entry_fok_goes_by_the_band_and_never_beyond_it(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker,
    *, source: Callable[[], Awaitable[PriceBand]] | None, price: float, words: str,
) -> None:
    """Вход FOK — по верхней планке к сетке внутрь, а не «текущая + 2 %» за ней (ревью 10.11, 1).

    Закрытие 100,3: «+ 2 %» — 102,5, за верхней планкой 101,5 биржа заявку
    не примет, и в трендовый день робот вставал бы на каждом выходе против
    движения. По планке — 101,5; планка 101,8 вне сетки 0,5 — внутрь, 101,5.
    Планка, не охватывающая «текущую», или её нет, или биржа молчит дольше
    `band_wait` — «текущая ± %», и слова говорят почему. Мутации: подать «± %»
    при годной планке; округлить край от рынка; подать по планке, не
    охватывающей «текущую»; промолчать о причине; ждать планку без срока.
    """

    async def scenario() -> None:
        limits = replace(FAST, band_wait=0.3)
        async with make_rig(tmp_path, store, broker, limits=limits, band=source) as rig:
            rig.exchange.plans.append(filled((1, 100.5)))
            loop = asyncio.get_running_loop()
            started = loop.time()
            # Срок — у самой проверки: ждущий планку без срока повесил бы тест.
            await asyncio.wait_for(submit_on(rig, entry(Side.LONG), close=100.3), 0.3 + 2.0)
            waited = loop.time() - started
            assert waited < 0.3 + 1.0, f"вход ждал планку {waited:.2f} с"
            assert immediate(rig) == [("1", price)], rig.creates()
            await heard()
            [told] = rig.told_orders()
            assert words in told, told

    run(scenario())


def test_a_trailing_exit_fok_goes_by_the_lower_band_not_below_it(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Выход по касанию трейлинга 95 при планке 94–110: продажа по 94, а не «− 2 %» = 93.

    93 ниже LOWLIMIT — отказ биржи вовремя и остановка с открытой позицией
    без сторожа (ревью 10.11, находка 1). Мутация: выход сторожа мимо планки.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker, band=band(94.0, 110.0)) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(95.0, LevelTouch.FALL))
            rig.minutes.append(minute(TOUCH, low=94.5))
            rig.exchange.plans.append(filled((1, 94.5)))
            await rig.executor.watch()
            assert immediate(rig)[-1] == ("2", 94.0), rig.creates()
            assert rig.broker.position(TICKER) == 0
            assert rig.executor.halted is None, rig.executor.halted

    run(scenario())


#: Шесть мест Р-2 при годной планке 90,2–109,8 (`GOOD_BAND`): покупка — 109,5,
#: продажа — 90,5, край на сетку 0,5 внутрь коридора. «± 2 %» дало бы 102 и 98,
#: касание — 93 (`SIX_POINTS`): точка мимо планки видна ценой в теле заявки.
BY_THE_BAND: dict[str, list[tuple[object, object]]] = {
    "вход": [("1", 109.5)],
    "выход по сигналу": [("1", 109.5), ("2", 90.5)],
    "переворот": [("1", 109.5), ("2", 90.5), ("2", 90.5)],
    "остаток тейка": [("1", 109.5), ("2", 90.5)],
    "касание трейлинга": [("1", 109.5), ("2", 90.5)],
    "закрыть всё": [("1", 109.5), ("2", 90.5)],
}


@pytest.mark.parametrize("point", list(SIX_POINTS))
def test_every_fok_of_the_six_points_goes_by_a_usable_band(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker, point: str
) -> None:
    """Решение владельца 10.11 (0067): **каждая** FOK — по планке, не только вход и касание.

    Те же шесть сценариев, что у `test_no_point_of_the_six_sends_a_market_order`,
    но планка годна: «текущая» каждой точки (закрытие свечи сигнала 100,
    закрытие свежей минутки 100, уровень касания 95) лежит внутри коридора.
    Каждая немедленная заявка — по своему краю, и строка о ней называет
    планку. Мутация (test-expert 10.11): любая одна точка — выход по сигналу,
    переворот, остаток тейка, «закрыть всё» при свежей минутке — мимо планки.
    """
    scenario_of, _ = SIX_POINTS[point]

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker, band=GOOD_BAND) as rig:
            await scenario_of(rig)
            assert immediate(rig) == BY_THE_BAND[point], f"{point}: {rig.creates()}"
            await heard()
            immediate_lines = [line for line in rig.told_orders() if ", FOK" in line]
            assert len(immediate_lines) == len(BY_THE_BAND[point]), rig.told_orders()
            for line in immediate_lines:
                assert "планке биржи" in line and "%" not in line, line

    run(scenario())


class _ShiftingBand:
    """Планка 90–110; пока её ждут — позиция у брокера сдвигается мимо программы.

    Сдвиг — `outside` подделки: сделка руками или принудительное закрытие,
    о которых поток статусов молчит. Один раз, на первом вопросе о планке
    после взвода (`armed`): вход, открывающий позицию, планку спрашивает тоже.
    """

    def __init__(self, broker: FakeBroker, by: int) -> None:
        self.broker = broker
        self.by = by
        self.armed = False

    async def __call__(self) -> PriceBand:
        if self.armed:
            self.armed = False
            self.broker.outside += self.by
        return PriceBand(TICKER, high=110.0, low=90.0, told="MOEX ISS")


async def _race_entry(rig: Rig, shift: _ShiftingBand) -> None:
    shift.armed = True
    rig.exchange.plans.append(filled((1, 99.0)))
    await submit_on(rig, entry(), close=100.0)


async def _race_signal_exit(rig: Rig, shift: _ShiftingBand) -> None:
    await long_open(rig)
    shift.armed = True
    rig.exchange.plans.append(filled((1, 99.0)))
    await submit_on(rig, leave(), close=100.0)


async def _race_trailing_touch(rig: Rig, shift: _ShiftingBand) -> None:
    await long_open(rig)
    await rig.executor.submit(arm(95.0, LevelTouch.FALL))
    rig.minutes.append(minute(TOUCH, low=94.0))
    rig.exchange.plans.append(filled((1, 94.0)))
    shift.armed = True
    await rig.executor.watch()


async def _race_take_rest(rig: Rig, shift: _ShiftingBand) -> None:
    await long_open(rig, volume=2)
    await rig.executor.submit(arm(110.0, LevelTouch.RISE, volume=2))
    rig.broker.execute(str(rig.creates()[-1]["clientOrderId"]), 1, 110.0)
    await heard()
    rig.exchange.plans.append(filled((1, 109.0)))
    shift.armed = True
    await rig.executor.submit(disarm(volume=2))


async def _race_close_all(rig: Rig, shift: _ShiftingBand) -> None:
    await long_open(rig)
    rig.exchange.plans.append(filled((1, 99.0)))
    shift.armed = True
    await rig.executor.close_everything("Проверка: закрыть всё.")


#: Пять мест, где FOK ждёт планку: сценарий, сдвиг позиции мимо программы
#: и позиция у брокера после него — та, которую робот обязан оставить.
RACES: dict[str, tuple[Callable[[Rig, _ShiftingBand], Awaitable[None]], int, int]] = {
    "вход": (_race_entry, +1, 0),
    "выход по сигналу": (_race_signal_exit, -1, 0),
    "касание трейлинга": (_race_trailing_touch, -1, 0),
    "остаток тейка": (_race_take_rest, -1, 0),
    "закрыть всё": (_race_close_all, -1, 0),
}


@pytest.mark.parametrize("point", list(RACES))
def test_a_position_changed_while_the_band_is_awaited_is_never_traded_on_the_old_reading(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker, point: str
) -> None:
    """`/risk` 10.11, находка 1: планка спрашивается **до** чтения позиции, а не после.

    Ожидание планки — до `band_wait` (5 с). Между чтением портфеля и подачей
    оно оставляло окно, в которое позиция менялась руками, — и заявка уходила
    на старый объём: выход из лонга, проданного руками, становился шортом,
    вход поверх купленного руками — двойным лонгом. Порядок «планка → позиция
    → подача» видит сдвиг: заявка на старый объём не уходит, робот стоит.
    Мутация (test-expert 10.11): в любой одной точке планка — после проверки
    позиции.
    """
    scenario_of, by, left = RACES[point]

    async def scenario() -> None:
        shift = _ShiftingBand(broker, by)
        async with make_rig(tmp_path, store, broker, band=shift) as rig:
            with contextlib.suppress(ExecutionHalted, ExecutionRefused):
                await scenario_of(rig, shift)
            assert not shift.armed, "сценарий ни разу не спросил планку после взвода"
            assert rig.broker.position(TICKER) == left, (point, rig.creates())
            assert rig.executor.halted is not None, point

    run(scenario())


async def _flat_by_the_fok(rig: Rig) -> str:
    """«Закрыть всё» закроет позицию своей FOK."""
    rig.exchange.plans.append(filled((1, 99.0)))
    return "закрыта заявкой FOK"


async def _flat_by_hand(rig: Rig) -> str:
    """Позицию уже закрыли руками в приложении брокера: поток принёс чужую сделку."""
    rig.broker.foreign_trade("2", 1, 99.0)
    await heard()
    return "Позиции у брокера нет — закрывать нечего"


@pytest.mark.parametrize("flat", [_flat_by_the_fok, _flat_by_hand], ids=["заявкой", "руками"])
def test_close_everything_after_an_unfilled_level_hands_no_open_position_event(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker,
    flat: Callable[[Rig], Awaitable[str]],
) -> None:
    """Выход после обрыва снят, событие ждёт; «закрыть всё» нашло позицию нулевой — события нет.

    Иначе с остановкой движок пишет ERROR «позиция открыта и без сторожа —
    закройте её» рядом со словами «закрыта заявкой FOK» (ревью 10.11, 2).
    Ноль — двумя путями: закрыла FOK «закрыть всё» или позицию уже закрыли
    руками, и закрывать нечего. Мутация: не забыть событие после закрытия;
    не забыть его, когда позиции нет (test-expert 10.11).
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await _killed_after_outage(rig)
            words = await flat(rig)
            text = await rig.executor.close_everything("Проверка: закрыть всё.")
            assert rig.broker.position(TICKER) == 0, text
            assert words in text, text
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.fills_at(bar(BAR0 + timedelta(minutes=10)))
            assert halted.value.unfilled == (), halted.value.unfilled

    run(scenario())


def test_close_everything_that_did_not_close_keeps_the_open_position_event(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """«Закрыть всё» не закрыло (FOK снята) — событие «уровень не исполнился» уходит с остановкой.

    Позиция открыта и без сторожа — это правда, и движок обязан сказать её
    строкой журнала. Мутация: забывать событие при любом «закрыть всё».
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await _killed_after_outage(rig)
            rig.exchange.plans.append(killed("4"))
            text = await rig.executor.close_everything("Проверка: закрыть всё.")
            assert rig.broker.position(TICKER) == 1, text
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.fills_at(bar(BAR0 + timedelta(minutes=10)))
            [event] = halted.value.unfilled
            assert event.order_id == take_name(), event

    run(scenario())


def test_an_unanswered_fok_cancelled_by_the_program_says_the_position_is_unchanged(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """FOK без ответа за `fill_wait`, снята программой, ни контракта: «позиция та же», не «не та».

    Ревью 10.11, находка 3: прежде остановка говорила «исполнена на 0 из 1…
    позиция у брокера не та, что у робота» — и человек шёл искать
    расхождение, которого нет. Мутация: слова частичной при нуле исполненных.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            rig.exchange.plans.append(resting)
            with pytest.raises(ExecutionHalted) as halted:
                await submit_on(rig, entry(Side.LONG), close=100.0)
            words = halted.value.human
            assert "не исполнившись ни на контракт" in words, words
            assert "позиция у брокера та, что была до заявки" in words, words
            assert "не та, что у робота" not in words, words
            assert rig.broker.position(TICKER) == 0

    run(scenario())


# ------------------------------- исполнение против «текущей» (владелец 10.11.2026)


@pytest.mark.parametrize(
    ("side", "fill", "words"),
    [
        (Side.LONG, 100.5, "исполнена по 100.5: текущая была 100, хуже текущей на 0.5 (0.50 %)"),
        (Side.LONG, 99.5, "исполнена по 99.5: текущая была 100, лучше текущей на 0.5 (0.50 %)"),
        (Side.SHORT, 99.0, "исполнена по 99: текущая была 100, хуже текущей на 1 (1.00 %)"),
        (Side.SHORT, 101.0, "исполнена по 101: текущая была 100, лучше текущей на 1 (1.00 %)"),
        (Side.LONG, 100.0, "исполнена по 100: текущая была 100, по текущей."),
    ],
    ids=["покупка хуже", "покупка лучше", "продажа хуже", "продажа лучше", "по текущей"],
)
def test_every_filled_fok_tells_its_price_against_the_current_one(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker,
    *, side: Side, fill: float, words: str,
) -> None:
    """FOK по планке на пустом стакане исполнится до края коридора (`/risk` 10.11, 2).

    Поэтому у каждой исполненной заявки — строка INFO «исполнена по X:
    текущая была Y, хуже/лучше текущей на N (p %)»: покупка хуже — дороже,
    продажа хуже — дешевле. Мутации: перепутать «хуже» и «лучше» по стороне;
    промолчать об исполнении; поднять уровень до предупреждения.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker, band=GOOD_BAND) as rig:
            rig.exchange.plans.append(filled((1, fill)))
            await submit_on(rig, entry(side), close=100.0)
            await heard()
            [told] = rig.told_fills()
            assert words in told, told
            assert [level for text, level in rig.levels if text == told] == [DecisionLevel.INFO]

    run(scenario())


def test_a_take_filled_at_its_level_tells_no_price_against_the_current_one(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Тейк — лимитная по уровню движка: «текущей» у него нет, строки об отклонении нет.

    Строка об исполнении — одна, у входа FOK. Мутация: строка у заявки без «текущей».
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE))
            rig.broker.execute(str(rig.creates()[-1]["clientOrderId"]), 1, 110.0)
            await heard()
            fills = rig.told_fills()
            assert len(fills) == 1 and "вход в лонг" in fills[0], fills

    run(scenario())


async def _limited_exit(rig: Rig) -> None:
    """Лонг; выход с пределом 98 (конец окна) по свече сигнала с закрытием 100, исполнен по 99."""
    await long_open(rig)
    rig.exchange.plans.append(filled((1, 99.0)))
    await submit_on(rig, leave(limit=98.0), close=100.0)


#: Строка о входе лонга `long_open`: FOK исполнена по закрытию свечи сигнала, 100.
_LONG_IN = ("вход в лонг", "исполнена по 100: текущая была 100, по текущей.")

#: Каждая точка исполнения → строки «исполнена по X: текущая была Y…» по порядку:
#: (что за заявка — начало скобок, слова об исполнении). Без планки — FOK «± %».
#: «Текущая» — у каждой своя: закрытие свечи сигнала, уровень касания (95),
#: закрытие свежей минутки 09:59 (100), у выхода с пределом — закрытие свечи сигнала.
FILL_LINES: dict[str, tuple[Callable[[Rig], Awaitable[object]], list[tuple[str, str]]]] = {
    "вход": (_entry, [_LONG_IN]),
    "выход по сигналу": (_signal_exit, [
        _LONG_IN,
        ("выход из лонга", "исполнена по 99: текущая была 100, хуже текущей на 1 (1.00 %)"),
    ]),
    "переворот": (_reversal, [
        _LONG_IN,
        ("выход из лонга", "исполнена по 99: текущая была 100, хуже текущей на 1 (1.00 %)"),
        ("вход в шорт", "исполнена по 99: текущая была 100, хуже текущей на 1 (1.00 %)"),
    ]),
    "остаток тейка": (_take_rest, [
        _LONG_IN,
        ("закрытие остатка тейка лонга",
         "исполнена по 109: текущая была 100, лучше текущей на 9 (9.00 %)"),
    ]),
    "касание трейлинга": (_trailing_touch, [
        _LONG_IN,
        ("выход из лонга по трейлингу 95",
         "исполнена по 94: текущая была 95, хуже текущей на 1 (1.05 %)"),
    ]),
    "закрыть всё": (_close_all, [
        _LONG_IN,
        ("закрыть всё: лонг 1", "исполнена по 99: текущая была 100, хуже текущей на 1 (1.00 %)"),
    ]),
    "выход с пределом": (_limited_exit, [
        _LONG_IN,
        ("выход из лонга — ", "исполнена по 99: текущая была 100, хуже текущей на 1 (1.00 %)"),
    ]),
}


@pytest.mark.parametrize("point", list(FILL_LINES))
def test_each_point_of_execution_tells_the_fill_against_its_own_current(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker, point: str
) -> None:
    """Строка об исполнении — у каждой точки, где «текущая» известна, и от **её** «текущей».

    Вход, выход по сигналу, переворот, остаток тейка при свежей минутке,
    касание трейлинга, «закрыть всё» — FOK; выход с пределом — дневная, его
    «текущая» — закрытие свечи сигнала (владелец 10.11.2026). Строк ровно
    столько, сколько исполненных заявок с «текущей», каждая INFO, каждая
    называет свою заявку. Тест входа выше этого не видит: он ходит одной
    точкой. Мутации (test-expert 10.11): «текущая» не доехала до билета
    в одной точке — трейлинг, «закрыть всё», остаток тейка, выход по сигналу,
    выход с пределом (`_close_limited` без `close`), FOK «± %» без планки.
    """
    scenario_of, expected = FILL_LINES[point]

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await scenario_of(rig)
            await heard()
            fills = rig.told_fills()
            assert len(fills) == len(expected), f"{point}: {fills}"
            for line, (what, words) in zip(fills, expected, strict=True):
                assert line.startswith(f"Заявка ({what}"), f"{point}: {line}"
                assert words in line, f"{point}: {line}"
            levels = [level for text, level in rig.levels if text in fills]
            assert levels == [DecisionLevel.INFO] * len(fills), f"{point}: {rig.levels}"

    run(scenario())


@pytest.mark.parametrize("scenario_of", [_stale_close_all, _stale_take_rest],
                         ids=["закрыть всё", "остаток тейка"])
@pytest.mark.parametrize(("source", "price"), [(GOOD_BAND, 90.5), (None, 98.0)],
                         ids=["по планке", "без планки"])
def test_a_fok_by_the_band_at_a_stale_minute_tells_no_price_against_a_current_one(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker,
    *, scenario_of: Callable[[Rig], Awaitable[str]],
    source: Callable[[], Awaitable[PriceBand]] | None, price: float,
) -> None:
    """Несвежая минутка: «текущей» нет — строки «текущая была» нет, с планкой и без неё.

    Без планки FOK уходит «± %» от несвежей минутки, но судить по ней
    исполнение — та же ошибка (координатор 10.11, мутация: строка от
    несвежей минутки у FOK без планки).

    Цена несвежей минутки — цена, которой уже нет (0067): сравнивать с ней
    исполнение по планке значило бы назвать «хуже на 9» то, что хуже только
    давно ушедшей цены. Строка одна — у входа; продажа исполнена (позиция 0),
    то есть строки нет не потому, что нечего было исполнять. Мутация
    (test-expert 10.11): FOK по планке при несвежей минутке судится от неё.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker, band=source) as rig:
            await scenario_of(rig)
            await heard()
            assert immediate(rig)[-1] == ("2", price), rig.creates()
            assert rig.broker.position(TICKER) == 0
            fills = rig.told_fills()
            assert len(fills) == 1 and fills[0].startswith("Заявка (вход в лонг"), fills

    run(scenario())


def test_a_fok_filled_at_two_prices_averaging_the_current_tells_it_went_by_the_current(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """FOK исполнена по двум ценам стакана, средняя ровно равна «текущей» — «по текущей».

    Сетка 0,05, как у живых фьючерсов: (2900,15 + 2900,05) / 2 в плавающей
    точке — 2900,1000000000004, а не 2900,1. Без округления пунктов строка
    сказала бы «хуже текущей на 4.54747e-13 (0.00 %)» — шум вместо слов.
    Мутация (test-expert 10.11): разность цен без округления.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            rig.exchange.plans.append(filled((1, 2900.15), (1, 2900.05)))
            await submit_on(rig, entry(volume=2), close=2900.1)
            await heard()
            [told] = rig.told_fills()
            assert "исполнена по 2900.1: текущая была 2900.1, по текущей." in told, told

    run(scenario())
