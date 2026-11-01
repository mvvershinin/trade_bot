"""Сторожа правок по `/risk` 2в, которых не было: поведение, найденное мутацией живым.

Решение 0066, строки от 01.11.2026; разбор мутаций — `.docs/tests/`. Каждая
проверка здесь стережёт **поведение**, которое выжило при поломке кода
(мутант прошёл весь набор), а не текст:

* отказ включения боя **после** разбора журнала говорит, что разбор сделал
  с заявками, строками «Сверка с брокером» — в каждой ветке отказа, — и не
  говорит «команд брокеру не было», раз они были (БЛОК 1, правило 13);
* позиция у брокера после разбора журнала читается снова: заявка прошлого
  запуска, исполнившаяся вместо снятия, бой не включает (БЛОК 1);
* отказ включения при позиции у брокера позицию в окне не снимает (R4);
* остановка, после которой бой выключили, строкой «Бой остановлен» не висит,
  а позиция у брокера видна (R4);
* «Применить» в бою отказывает под **тем** полем, которое сменилось: порог
  проверки по минуткам, размер свечи, объём; вне боя то же принимается (R1);
* «Позиция без управления» при отложенной пересборке сказана однажды (БЛОК 2);
* «Закрыть позицию» при позиции «факта» называет её и советует «Выключить
  бой»; «Старт» без боя говорит, как бой включается (замечания);
* сверка после связи — одной операцией под замком: сторож, продавший, пока
  сверка читала портфель, не даёт «закрыть всё» (R2);
* сверка после связи не идёт, пока поток закрыт (замечание к `relinked`);
* пределы ожидания из файла — словами окна, без записи Python (замечание).

Стенды — `tests/test_app_live_pair.py` (порт, пара, подставной брокер)
и `tests/test_app_live_executor.py` (исполнитель). Изоляция — их же: своя
база и свой `userdata` во временном каталоге, сети нет.
"""

from __future__ import annotations

import asyncio
import dataclasses
import pathlib
import sqlite3
from collections.abc import Awaitable, Callable, Coroutine
from datetime import timedelta

import httpx
import pytest
import websockets

from app import live_trading
from app.fact import LiveStatus
from app.port import HistoryPort
from app.reconcile import Reconciliation, reconcile
from broker.session import PORTFOLIO_PATH
from broker.tokens import TokenScope
from engine.contracts import LevelTouch, OrderAction
from market import CandleStore, MarketWorker
from market.commands import BrokerCommand, CommandAction, CommandSide
from market.journal import RunOrigin, SessionRecord
from tests.fake_broker import CANCEL, CREATE, STATUS, FakeBroker, _unavailable
from tests.test_app_live_executor import (
    BAR0,
    TICKER,
    _gated,
    _market_sells,
    _reconnect,
    arm,
    bar,
    filled,
    heard,
    long_open,
    make_rig,
    minute,
    only_fill,
    run,
    take_name,
)
from tests.test_app_live_gates import GONE_TARGET, LIVE_TAKE, LONE_CANCEL, _build
from tests.test_app_live_pair import (
    _SPIKES,
    BASE,
    DAY,
    ENTRY_SPIKE,
    FIXED_TAKE,
    LIVE_FROM,
    NO_TAKE,
    OWNER,
    SMALL,
    SYMBOL,
    Bench,
    _bench,
    _close,
    _connect,
    _ride,
    _takes,
    _token,
)
from tests.test_app_live_ride import _fast_reconnect, _relink
from tests.test_app_live_trading import _desk, _Executor
from ui.models import FIELD_CAPTIONS, MinuteBarLimit, Mode
from ui.models import Settings as UiSettings

#: Ответ подделки на один адрес (`FakeBroker.replace_route`).
Route = Callable[[httpx.Request], httpx.Response]

#: Цена и имя тейка прошлого запуска — того, что разбор журнала снимает.
OLD_TAKE_PRICE = 251_000.0
OLD_TAKE_NAME = "take:long:прошлый"
#: Вход прошлого запуска, о котором брокер не отвечает. Ключ выдуманный, в форме UUID.
MUTE_ENTRY = "00000000-0000-4000-8000-0000000000e1"


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> FakeBroker:
    fake = FakeBroker()
    monkeypatch.setattr(websockets, "connect", fake.connect)
    return fake


# --------------------------------------------------------------- БЛОК 1: отказ после разбора


@dataclasses.dataclass(frozen=True)
class After:
    """Что не пускает бой уже после разбора журнала и чем это названо."""

    words: str
    lone_cancel: bool = False
    mute_entry: bool = False
    spoil: Callable[[pytest.MonkeyPatch, FakeBroker], None] | None = None


def _plant(broker: FakeBroker, after: After) -> Callable[[MarketWorker], Awaitable[None]]:
    """Журнал прошлого запуска: тейк, живой у брокера, и то, что просит случай."""

    async def plant(worker: MarketWorker) -> None:
        def write(base: CandleStore) -> None:
            run = base.open_journal_session(
                SessionRecord(RunOrigin.LIVE, symbol=SYMBOL), now=DAY.replace(hour=8)
            )
            base.record_broker_command(run.id, BrokerCommand(
                client_order_id=LIVE_TAKE, action=CommandAction.CREATE,
                order_id=OLD_TAKE_NAME, symbol=SYMBOL, side=CommandSide.SELL,
                volume=1, price=OLD_TAKE_PRICE, account=broker.account,
            ))
            if after.lone_cancel:
                base.record_broker_command(run.id, BrokerCommand(
                    client_order_id=LONE_CANCEL, action=CommandAction.CANCEL,
                    order_id="take:long:позапрошлый", symbol=SYMBOL,
                    target_client_order_id=GONE_TARGET, account=broker.account,
                ))
            if after.mute_entry:
                base.record_broker_command(run.id, BrokerCommand(
                    client_order_id=MUTE_ENTRY, action=CommandAction.CREATE,
                    order_id="open:long:прошлый", symbol=SYMBOL, side=CommandSide.BUY,
                    volume=1, account=broker.account,
                ))

        await worker.call(write)

    return plant


async def _old_take_at_the_broker(broker: FakeBroker) -> None:
    """Тейк прошлого запуска — в книге подделки тем же путём, каким его подала бы программа."""
    async with broker.client() as client:
        await client.post(CREATE[1], json={
            "clientOrderId": LIVE_TAKE, "side": "2", "orderType": "2",
            "orderQuantity": 1, "ticker": SYMBOL, "classCode": "SPBFUT",
            "price": OLD_TAKE_PRICE,
        })
    order = broker.order(LIVE_TAKE)
    assert order is not None and order.live, "подделка не приняла заявку прошлого запуска"


def _fail_day(monkeypatch: pytest.MonkeyPatch, broker: FakeBroker) -> None:
    async def broken(self: MarketWorker, *args: object, **kwargs: object) -> object:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(MarketWorker, "executions_day", broken)


def _fail_portfolio_after_a_cancel(monkeypatch: pytest.MonkeyPatch, broker: FakeBroker) -> None:
    """Портфель отвечает, пока разбор не снял заявку; после снятия — 503."""
    previous: list[Route] = []

    def route(request: httpx.Request) -> httpx.Response:
        if any(kind == "cancel" for kind, _ in broker.commands):
            return _unavailable(request, "")
        return previous[0](request)

    previous.append(broker.replace_route(("GET", PORTFOLIO_PATH), route))


def _status_down_for_the_entry(monkeypatch: pytest.MonkeyPatch, broker: FakeBroker) -> None:
    """О входе прошлого запуска брокер не отвечает: судьба команды не выясняется."""
    previous: list[Route] = []

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.params["orderId"] == MUTE_ENTRY:
            return _unavailable(request, MUTE_ENTRY)
        return previous[0](request)

    previous.append(broker.replace_route(STATUS, route))


def _reject_instead_of_cancel(monkeypatch: pytest.MonkeyPatch, broker: FakeBroker) -> None:
    """Вместо снятия биржа отклонила тейк прошлого запуска: исполнитель встаёт."""
    previous: list[Route] = []

    def route(request: httpx.Request) -> httpx.Response:
        order = broker.order(LIVE_TAKE)
        if order is not None and order.live:
            broker.reject(LIVE_TAKE, "снятие отклонено на проверке")
        return previous[0](request)

    previous.append(broker.replace_route(CANCEL, route))


def _fill_instead_of_cancel(monkeypatch: pytest.MonkeyPatch, broker: FakeBroker) -> None:
    """Снятие опоздало: тейк прошлого запуска исполнился раньше, чем дошла отмена."""
    previous: list[Route] = []

    def route(request: httpx.Request) -> httpx.Response:
        order = broker.order(LIVE_TAKE)
        if order is not None and order.live:
            broker.execute(LIVE_TAKE, 1, OLD_TAKE_PRICE)
        return previous[0](request)

    previous.append(broker.replace_route(CANCEL, route))


AFTER_THE_CHECK = [
    pytest.param(After("только без незакрытых команд", lone_cancel=True), id="unclosed-command"),
    pytest.param(After("День счёта из базы не прочитан", spoil=_fail_day), id="day-not-read"),
    pytest.param(
        After("Позицию у брокера прочитать не удалось", spoil=_fail_portfolio_after_a_cancel),
        id="position-not-read",
    ),
    pytest.param(After("шорт 1", spoil=_fill_instead_of_cancel), id="old-take-filled"),
    pytest.param(
        After("Биржа отклонила заявку", spoil=_reject_instead_of_cancel), id="old-take-rejected"
    ),
    pytest.param(
        After("выяснить у брокера не удалось", mute_entry=True, spoil=_status_down_for_the_entry),
        id="entry-fate-unknown",
    ),
]


@pytest.mark.parametrize("after", AFTER_THE_CHECK)
def test_a_refusal_after_the_check_says_what_the_check_did_with_the_orders(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch,
    after: After,
) -> None:
    """БЛОК 1, правило 13: разбор журнала снял тейк прошлого запуска, а бой не включился.

    Снятие — команда брокеру. Отказ, который о ней молчит, оставляет человека
    с пропавшей заявкой в приложении брокера и с фразой «команд брокеру
    не было», то есть с неправдой. Стережёт для каждой ветки отказа после
    разбора: строка «Сверка с брокером» называет тейк прошлого запуска, отказ
    назван своей причиной, «команд брокеру не было» в нём нет, бой выключен,
    новых заявок нет.
    """

    async def go() -> tuple[LiveStatus, list[tuple[str, str]], int]:
        await _old_take_at_the_broker(broker)
        created = len([kind for kind, _ in broker.commands if kind == "create"])
        if after.spoil is not None:
            after.spoil(monkeypatch, broker)
        bench, session = await _build(
            tmp_path, broker, before=_plant(broker, after)
        )
        try:
            await _connect(bench)
            fresh = len([kind for kind, _ in broker.commands if kind == "create"]) - created
            return bench.port.live_status(), list(bench.notes), fresh
        finally:
            await _close(bench, session)

    status, notes, created = loop.run_until_complete(go())
    assert any(kind == "cancel" for kind, _ in broker.commands), "разбор ничего не снимал"
    refused = " ".join(reason for event, reason in notes if event == "Бой не включён")
    said = " ".join(reason for event, reason in notes if event == "Сверка с брокером")
    assert after.words in refused, notes
    assert OLD_TAKE_NAME in said, f"что разбор сделал с тейком, не сказано: {notes}"
    assert "команд брокеру не было" not in refused, f"отказ говорит неправду: {refused}"
    assert not status.on and not status.wanted, status
    assert created == 0, broker.commands


# --------------------------------------------------------------- R4: позиция после остановки


@pytest.mark.parametrize("how", ["switched-off-and-on", "halted-and-resumed"])
def test_a_refused_reenable_keeps_the_broker_position_in_sight(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """R4: повторное включение при лонге у брокера отказано — позиция в окне остаётся.

    Отказ — не свежий ноль у брокера: позиция у него та же, лонг 1 с тейком.
    Снятая из снимка боя, она пропала бы из шапки, а закрытие окна и
    «Выключить бой» перестали бы спрашивать про позицию без стопа.
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)

    async def go() -> tuple[LiveStatus, str]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            if how == "switched-off-and-on":
                bench.port.set_live(False)
                await bench.port.wait()
                bench.port.set_live(True)
            else:
                bench.port.halt("Проба остановки", "Проба: робот остановлен извне боя.")
                await bench.port.wait()
                bench.port.resume()
            await bench.port.wait()
            return bench.port.live_status(), bench.said("Бой не включён")
        finally:
            await _close(bench, session)

    status, refused = loop.run_until_complete(go())
    assert "лонг 1" in refused, "повторного включения не было — проверять нечего"
    assert broker.position(SYMBOL) == 1
    assert status.position is not None, "позиция у брокера пропала из окна после отказа"
    assert status.position.signed == 1, status.position


def test_after_a_halt_and_turning_combat_off_the_line_is_not_halted_but_the_position_stays(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4: «Бой остановлен» — пока бой просили; выключили бой — строки нет, позиция видна.

    После «Выключить бой» снимок без просьбы боя: строка «Бой остановлен: …»
    рядом с отжатой кнопкой читалась бы как идущий бой. Остановка робота
    при этом видна в причинах недоступности боя, а позиция у брокера — в шапке.
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)

    async def go() -> LiveStatus:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            bench.port.halt("Проба остановки", "Проба: робот остановлен извне боя.")
            await bench.port.wait()
            assert bench.port.live_status().halted, "остановка не видна в бою — проверять нечего"
            bench.port.set_live(False)
            await bench.port.wait()
            return bench.port.live_status()
        finally:
            await _close(bench, session)

    status = loop.run_until_complete(go())
    assert not status.wanted
    assert status.halted == "", f"бой выключен, а снимок говорит «остановлен»: {status.halted}"
    assert any("Робот остановлен" in reason for reason in status.why_not), status.why_not
    assert status.position is not None and status.position.signed == 1, status.position



def test_turning_combat_off_without_a_position_leaves_no_position_in_sight(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """R4: бой выключен без позиции — у брокера ноль, и в окне позиции нет.

    Позиция после закрытия боевой стороны — свежим чтением у брокера; ноль
    её снимает. Иначе окно показало бы «шорт 0» без цены, а закрытие окна
    спросило бы о позиции без стопа, которой нет.
    """

    async def go() -> tuple[LiveStatus, str]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=20))
            assert bench.port.live_status().on, "бой не включился — проверять нечего"
            bench.port.set_live(False)
            await bench.port.wait()
            return bench.port.live_status(), bench.said("Бой выключен")
        finally:
            await _close(bench, session)

    status, off = loop.run_until_complete(go())
    assert broker.position(SYMBOL) == 0, "к 09:20 позиции у брокера быть не должно"
    assert status.position is None, status.position
    assert "Позиции у робота нет" in off, off


def test_a_portfolio_without_the_account_number_keeps_combat_off_and_says_so(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """Номера договора в портфеле нет — бой не включён: журнал команд не знает, чей это день."""
    broker.account = ""

    async def go() -> tuple[LiveStatus, str]:
        bench, session = await _bench(tmp_path, broker, OWNER)
        try:
            await _connect(bench)
            return bench.port.live_status(), bench.said("Бой не включён")
        finally:
            await _close(bench, session)

    status, refused = loop.run_until_complete(go())
    assert "не назвал номер счёта" in refused, refused
    assert not status.on and not status.wanted, status
    assert broker.commands == [], broker.commands


# --------------------------------------------------------------- R1: поле отказа


@dataclasses.dataclass(frozen=True)
class Change:
    """Смена настроек в бою, которую включение боя не пустило бы, и её поле окна."""

    field: str
    value: object


CHANGES = [
    pytest.param(Change("minute_bar_limit", MinuteBarLimit.FIFTEEN), id="minute-bar-limit"),
    pytest.param(Change("timeframe", "5 минут"), id="timeframe"),
    # Окно объём дробным не шлёт; порт — публичная команда, и шаблон или файл
    # могут принести что угодно. Дробная заявка у брокера остановила бы робота.
    pytest.param(Change("volume", 1.5), id="fractional-volume"),
]


def _apply(bench: Bench, change: Change) -> list[tuple[str, str]]:
    refused: list[tuple[str, str]] = []
    bench.port.settings_refused.connect(
        lambda _values, reason, name: refused.append((reason, name))
    )
    bench.port.apply_settings(OWNER.replace(**{change.field: change.value}))
    return refused


@pytest.mark.parametrize("change", CHANGES)
def test_apply_in_combat_refuses_under_the_field_that_changed(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, change: Change
) -> None:
    """R1: в бою «Применить» отказывает под **сменённым** полем; бой идёт по прежним.

    Скользящий уровень включён на свече 15 минут при пороге минуток 5 — бой
    идёт. Порог 15 или свеча 5 минут нарушают D-141 сменой **другого** поля:
    отказ под полем скользящего уровня открыл бы вкладку, где человеку
    менять нечего. Дробный объём — отказ под объёмом.
    """

    async def go() -> tuple[list[tuple[str, str]], UiSettings, LiveStatus]:
        bench, session = await _bench(tmp_path, broker, OWNER)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=16))
            refused = _apply(bench, change)
            await bench.port.wait()
            return refused, bench.port.values, bench.port.live_status()
        finally:
            await _close(bench, session)

    refused, values, status = loop.run_until_complete(go())
    assert status.on, "бой не включился — проверять нечего"
    assert refused, f"в бою принято {change.field}={change.value!r}"
    reason, name = refused[-1]
    assert name == change.field, f"отказ под чужим полем: {name}"
    assert "В бою так нельзя" in reason and "по прежним настройкам" in reason, reason
    assert getattr(values, change.field) == getattr(OWNER, change.field), "значение принято"


@pytest.mark.parametrize("change", CHANGES)
def test_apply_without_combat_takes_what_combat_refuses(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, change: Change
) -> None:
    """R1, обратная сторона: без боя те же значения принимаются — запрет только у боя."""

    async def go() -> tuple[list[tuple[str, str]], UiSettings]:
        bench, session = await _bench(tmp_path, broker, OWNER)
        try:
            await _connect(bench, live=False)
            refused = _apply(bench, change)
            await bench.port.wait()
            return refused, bench.port.values
        finally:
            await _close(bench, session)

    refused, values = loop.run_until_complete(go())
    assert refused == [], refused
    assert getattr(values, change.field) == change.value


# --------------------------------------------------------------- БЛОК 2: сказано однажды


def test_mode_off_while_a_rebuild_is_held_says_the_unmanaged_position_once(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """БЛОК 2: «Позиция без управления» при отложенной пересборке — одна строка, не каждый проход.

    Пересборка отложена (период средней при лонге), режим «Выключен» дошёл
    до «факта» на ходу. Дальше идут проходы на каждой минутке — и та же
    строка на каждом из них похоронила бы журнал под собой.
    """

    async def go() -> list[str]:
        bench, session = await _bench(tmp_path, broker, NO_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            bench.port.apply_settings(dataclasses.replace(NO_TAKE, average_period=20))
            await bench.port.wait()
            bench.port.set_mode(Mode.OFF)
            await bench.port.wait()
            await _ride(
                bench, LIVE_FROM + timedelta(minutes=70), since=LIVE_FROM + timedelta(minutes=51)
            )
            return bench.events()
        finally:
            await _close(bench, session)

    events = loop.run_until_complete(go())
    assert "Пересборка боя отложена" in events, "пересборка не отложена — проверять нечего"
    assert events.count("Позиция без управления") == 1, events.count("Позиция без управления")


def test_a_second_change_while_the_rebuild_is_held_is_said_and_shown(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """БЛОК 2: пересборка отложена, и человек меняет ещё раз — строка и окно называют новое.

    Период средней 20 при лонге — отложено; затем 25. Строка «отложена»
    правдива, только если называет то, что ждёт сейчас: «15 → 20» после
    второго «Применить» обещала бы пересборку не с тем периодом.
    """

    async def go() -> tuple[str, LiveStatus]:
        bench, session = await _bench(tmp_path, broker, NO_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            for period in (20, 25):
                bench.port.apply_settings(dataclasses.replace(NO_TAKE, average_period=period))
                await bench.port.wait()
            held = [reason for event, reason in bench.notes if event == "Пересборка боя отложена"]
            return held[-1] if held else "", bench.port.live_status()
        finally:
            await _close(bench, session)

    last, status = loop.run_until_complete(go())
    assert last, "пересборка не отложена — проверять нечего"
    assert "25" in last, f"строка «отложена» не назвала новое значение: {last}"
    assert "25" in status.held, f"окно показывает прежнее ожидание: {status.held}"


# --------------------------------------------------------------- замечания: слова кнопок


def test_close_position_in_combat_names_the_position_and_says_turn_combat_off_first(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """«Закрыть позицию» при лонге «факта»: позиция названа, совет — «Выключить бой».

    Закрытия заявкой из программы нет; позиция, закрытая руками при
    включённом бое, остановила бы робота. Отказ «закрывать нечего» здесь —
    неправда о деньгах на счёте.
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)

    async def go() -> str:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            sent = len(broker.commands)
            bench.port.close_position()
            await bench.port.wait()
            assert len(broker.commands) == sent, "«Закрыть позицию» подала команду брокеру"
            return bench.said("Закрытие позиции")
        finally:
            await _close(bench, session)

    said = loop.run_until_complete(go())
    assert "лонг 1" in said, said
    assert "«Выключить бой»" in said, said
    assert "Закрывать нечего" not in said, said


def test_start_without_combat_says_how_combat_is_switched_on(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """«Старт» без боя: отказ называет кнопку «Включить бой», а не «боевого хода ещё нет»."""

    async def go() -> str:
        bench, session = await _bench(tmp_path, broker, OWNER)
        try:
            await _connect(bench, live=False)
            bench.port.start()
            await bench.port.wait()
            return bench.said("Запуск робота")
        finally:
            await _close(bench, session)

    said = loop.run_until_complete(go())
    assert "«Включить бой»" in said, said
    assert "ещё нет" not in said, said


def test_bad_wait_limits_from_the_file_are_said_in_window_words_in_one_line(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """Пределы ожидания из файла, которых окно не примет, — одной строкой и словами окна.

    Человек видит «в файле 0,05, а годно … — поставлено умолчание 1», а не
    `0.05` и `(1, 2, 5, 10, 30)`: запись Python в журнале — это не текст для
    человека, а запятая вместо точки — его привычная запись чисел.
    """
    values = dataclasses.replace(
        OWNER, live_position_settle_sec=0.05, live_reconnect_pauses_sec=(0, 3)
    )

    async def go() -> list[str]:
        bench, session = await _build(tmp_path, broker, values)
        journals: list[str] = []
        bench.port.decisions_replaced.connect(
            lambda rows: journals.extend(getattr(row, "reason", "") for row in rows)
        )
        try:
            await _connect(bench, live=False)
            return [*(reason for _, reason in bench.notes), *journals]
        finally:
            await _close(bench, session)

    reasons = loop.run_until_complete(go())
    settle = f"«{FIELD_CAPTIONS['live_position_settle_sec']}»"
    pauses = f"«{FIELD_CAPTIONS['live_reconnect_pauses_sec']}»"
    rows = [reason for reason in reasons if settle in reason and pauses in reason]
    assert rows, f"о двух негодных пределах не сказано одной строкой: {reasons}"
    row = rows[0]
    assert "в файле 0,05, а годно" in row and "умолчание 1." in row, row
    assert "в файле 0, 3, а годно" in row and "умолчание 1, 2, 5, 10, 30." in row, row
    for python in ("0.05", "1.0", "(", "[", "'"):
        assert python not in row, f"запись Python «{python}» в строке человеку: {row}"


# --------------------------------------------------------------- R2: сверка под замком


class _HeldPortfolio:
    """Транспорт подделки: чтение портфеля один раз ждёт, пока идёт то, что попросил тест.

    `during` — что запустить, когда сверка спросит портфель; транспорт ждёт его
    конца, но не дольше `patience`: держи сверка замок исполнителя, оно
    не начнётся вовсе, и ждать дольше незачем.
    """

    def __init__(self, broker: FakeBroker, patience: float = 0.3) -> None:
        self.broker = broker
        self.patience = patience
        self.during: Callable[[], Coroutine[object, object, None]] | None = None
        self.task: asyncio.Task[None] | None = None

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == PORTFOLIO_PATH and self.during is not None:
            self.task = asyncio.ensure_future(self.during())
            self.during = None
            await asyncio.wait({self.task}, timeout=self.patience)
        return self.broker(request)


def test_a_trailing_sale_while_the_relink_check_reads_the_portfolio_is_not_a_mismatch(
    tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """R2: сторож продаёт по догруженной минутке, **пока** сверка после связи читает портфель.

    Позиция робота и портфель — одной операцией под замком исполнителя:
    продажа сторожа ждёт конца сверки и не вклинивается между подсчётом
    и чтением. Без замка сверка насчитала бы «у робота лонг 1», прочитала бы
    «у брокера позиции нет» и закрыла бы всё — робот встал бы на штатном
    выходе по трейлингу (решение владельца «выйти и работать дальше», 0066).
    """
    held = _HeldPortfolio(broker)
    client = httpx.AsyncClient(
        base_url="https://example.invalid", transport=httpx.MockTransport(held)
    )

    async def scenario() -> None:
        gate, sleep = _gated()
        tokens = _token(tmp_path / "userdata", TokenScope.TRADE)
        async with make_rig(tmp_path, tokens, broker, client=client, sleep=sleep) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(95.0, LevelTouch.FALL))

            def outage() -> None:
                rig.minutes.append(minute(BAR0 + timedelta(minutes=1), low=94.0))

            await _reconnect(rig, gate, outage)
            rig.exchange.plans.append(filled((1, 94.0)))
            held.during = rig.executor.watch
            result = await reconcile(rig.executor, expected=1)
            assert held.task is not None, "сверка не читала портфель — проверять нечего"
            await held.task
            await heard()
            assert result.halted is None, result.halted
            assert rig.executor.halted is None, rig.executor.halted
            assert _market_sells(rig) == [1], "сторож не продал или продал дважды"
            assert rig.broker.position(TICKER) == 0
            fill = only_fill(await rig.executor.fills_at(bar(BAR0)))
            assert (fill.action, fill.order_id, fill.price) == (
                OrderAction.CLOSE, take_name(), 94.0,
            )

    run(scenario())


# --------------------------------------------------------------- сверка после связи ждёт поток


def test_the_relink_check_waits_while_the_status_stream_is_still_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Поток переоткрылся и снова оборвался — сверки нет, пока он не откроется.

    Без потока исход снятия не узнать: сверка, начатая на закрытом потоке,
    не дошла бы до конца и остановила бы робота. Заявки при этом и так
    не подаются (`ExecutionRefused`); сверка — на первом проходе после связи.
    """
    calls: list[int] = []

    async def fake(executor: object, expected: int) -> Reconciliation:
        calls.append(expected)
        return Reconciliation((), expected, expected, None)

    monkeypatch.setattr(live_trading, "reconcile", fake)
    executor = _Executor()
    desk = _desk(executor)
    executor.links, executor.connected = 2, False

    async def go() -> tuple[list[int], list[str | None]]:
        down = await desk.relinked(1)
        while_down = list(calls)
        executor.connected = True
        return while_down, [down, await desk.relinked(1)]

    while_down, said = asyncio.run(go())
    assert while_down == [], "сверка пошла на закрытом потоке"
    assert said == [None, None], said
    assert calls == [1], "после связи сверки не было или было две"


def test_a_relink_check_that_cannot_read_the_portfolio_halts_the_robot_in_words(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0020, правило 13: связь вернулась, а портфель не читается — робот стоит, сказано почему.

    Это остановка не исполнителя, а сверки: исполнитель сам по себе не стоит,
    и путь «остановка исполнителя после свечей» её не подберёт. Проглоченная,
    она дала бы роботу торговать дальше без сверки после обрыва — с позицией,
    которой программа не знает. Стережёт: остановка робота словами сверки,
    ни одной новой заявки к брокеру после обрыва, хотя портфель потом ожил.
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)

    async def go() -> tuple[str, int, int]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        _fast_reconnect(bench, session)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            sent = len(broker.commands)
            portfolio = ("GET", PORTFOLIO_PATH)
            working = broker.replace_route(portfolio, lambda request: _unavailable(request, ""))
            reopened = await _relink(broker)
            await _ride(
                bench, LIVE_FROM + timedelta(minutes=51), since=LIVE_FROM + timedelta(minutes=51)
            )
            broker.replace_route(portfolio, working)
            await _ride(
                bench, LIVE_FROM + timedelta(minutes=61), since=LIVE_FROM + timedelta(minutes=52)
            )
            created = [key for kind, key in broker.commands[sent:] if kind == "create"]
            return bench.port.halted, reopened, len(created)
        finally:
            await _close(bench, session)

    halted, reopened, created = loop.run_until_complete(go())
    assert reopened >= 1, "поток статусов не переоткрылся — проверять нечего"
    assert "Позицию у брокера прочитать не удалось" in halted, f"робот не встал: «{halted}»"
    assert created == 0, "после обрыва без сверки заявка ушла к брокеру"


def test_a_fresh_zero_at_the_broker_clears_the_remembered_position_even_when_combat_is_refused(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4: позиция в окне снимается свежим нулём у брокера — и при отказе включения тоже.

    Бой встал при лонге; владелец закрыл позицию в приложении брокера и жмёт
    «Возобновить». Сборка прочитала у брокера ноль, а бой не включился по
    другой причине (день счёта не прочитан). Окно продолжает показывать
    «лонг 1», закрытие окна и «Выключить бой» спрашивают о позиции, которой
    у брокера нет.
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)

    async def go() -> tuple[LiveStatus, str]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            bench.port.halt("Проба остановки", "Проба: робот остановлен извне боя.")
            await bench.port.wait()
            assert bench.port.live_status().position is not None, "позиции нет — проверять нечего"
            broker.foreign_trade("2", 1, BASE, ticker=SYMBOL)  # закрыл руками у брокера
            _fail_day(monkeypatch, broker)
            bench.port.resume()
            await bench.port.wait()
            return bench.port.live_status(), bench.said("Бой не включён")
        finally:
            await _close(bench, session)

    status, refused = loop.run_until_complete(go())
    assert broker.position(SYMBOL) == 0
    assert "День счёта из базы не прочитан" in refused, "отказ не тот — проверять нечего"
    assert status.position is None, f"у брокера ноль, а окно показывает {status.position}"


def _portfolio_down_after(broker: FakeBroker, answers: int) -> None:
    """Портфель отвечает `answers` раз, дальше — 503 на каждый запрос."""
    previous: list[Route] = []
    answered: list[int] = []

    def route(request: httpx.Request) -> httpx.Response:
        if len(answered) >= answers:
            return _unavailable(request, "")
        answered.append(1)
        return previous[0](request)

    previous.append(broker.replace_route(("GET", PORTFOLIO_PATH), route))


@pytest.mark.parametrize(
    ("answers", "words"),
    [
        pytest.param(0, "Портфель у брокера не прочитан", id="account-not-read"),
        pytest.param(1, "Позицию у брокера прочитать не удалось", id="position-not-read"),
    ],
)
def test_a_refusal_without_a_fresh_zero_keeps_the_remembered_position(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch,
    *, answers: int, words: str,
) -> None:
    """B-081, обратная сторона: позиция снимается **только** свежим нулём, а не отказом чтения.

    Бой встал при лонге 1 у брокера; «Возобновить» — а портфель не читается
    (совсем или после номера счёта). Нуля никто не читал — окно обязано
    держать «лонг 1»: иначе шапка пустая, а закрытие окна и «Выключить бой»
    о позиции без робота не спрашивают.
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)

    async def go() -> tuple[LiveStatus, str]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            bench.port.halt("Проба остановки", "Проба: робот остановлен извне боя.")
            await bench.port.wait()
            assert bench.port.live_status().position is not None, "позиции нет — проверять нечего"
            _portfolio_down_after(broker, answers)
            bench.port.resume()
            await bench.port.wait()
            return bench.port.live_status(), bench.said("Бой не включён")
        finally:
            await _close(bench, session)

    status, refused = loop.run_until_complete(go())
    assert broker.position(SYMBOL) == 1
    assert words in refused, f"отказ не тот: {refused}"
    assert status.position is not None, "у брокера лонг 1, а окно после отказа позиции не видит"
    assert status.position.signed == 1, status.position


# --------------------------------------------------------------- Н1: оба порога прежнего ряда


@pytest.mark.parametrize(
    ("was", "now"),
    [
        pytest.param(MinuteBarLimit.FIFTEEN, MinuteBarLimit.FIVE, id="old-limit-forbids"),
        pytest.param(MinuteBarLimit.FIVE, MinuteBarLimit.FIFTEEN, id="new-limit-forbids"),
    ],
)
def test_apply_in_combat_checks_the_held_series_at_both_minute_limits(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch,
    *, was: MinuteBarLimit, now: MinuteBarLimit,
) -> None:
    """Н1: при лонге «свеча 30 + скользящий уровень + другой порог минуток» — отказ.

    Пересборка на 30 минут отложится позицией, и «факт» пойдёт прежним рядом
    15 минут. Сама новая настройка запрет D-141 проходит (30 крупнее порога),
    но на прежнем ряду она его нарушает — при прежнем пороге (15 ≤ 15) или
    при новом (15 ≤ 15). Стережёт: отказ под полем скользящего уровня, слова
    о прежнем ряде, уровень не включён, бой идёт.
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)
    start = dataclasses.replace(FIXED_TAKE, minute_bar_limit=was)
    refused: list[tuple[str, str]] = []

    async def go() -> tuple[UiSettings, LiveStatus]:
        bench, session = await _bench(tmp_path, broker, start)
        bench.port.settings_refused.connect(
            lambda _values, reason, name: refused.append((reason, name))
        )
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            bench.port.apply_settings(dataclasses.replace(
                start, timeframe="30 минут", minute_bar_limit=now, trailing_enabled=True
            ))
            await bench.port.wait()
            return bench.port.values, bench.port.live_status()
        finally:
            await _close(bench, session)

    values, status = loop.run_until_complete(go())
    assert status.on and status.position is not None, "бой без позиции — пересборка не держится"
    assert refused, "на прежнем ряду запрет D-141 нарушен, а «Применить» приняло"
    reason, name = refused[-1]
    assert name == "trailing_enabled", name
    assert "D-141" in reason and "прежним рядом" in reason, reason
    assert not values.trailing_enabled and values.minute_bar_limit is was


#: Чем держится пересборка в случаях ниже: незакрытая команда, а не позиция.
UNCLOSED = "в журнале команд незакрытых 1: проба"


async def _held_by_a_command(_self: object, _observer: object) -> str:
    return UNCLOSED


def test_a_rebuild_held_by_an_unclosed_command_keeps_the_engine_settings_in_force(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Н1, запасной рубеж `_held_frame`: прежний ряд и новый скользящий уровень — D-141, не пускать.

    Позиции нет, пересборку держит незакрытая команда — «Применить» прежний
    ряд не проверяет и принимает «свеча 30 + скользящий уровень». «Факт» идёт
    прежним рядом 15 минут при пороге 15: скользящий уровень на нём нарушает
    D-141. Стережёт: «факт» остаётся на действующих настройках движка —
    вход 09:45 получает неподвижный тейк у брокера, а не скользящий уровень.
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)

    async def go() -> tuple[UiSettings, LiveStatus, list[tuple[str, float | None]]]:
        bench, session = await _bench(tmp_path, broker, SMALL)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=20))
            monkeypatch.setattr(HistoryPort, "_fact_busy", _held_by_a_command)
            bench.port.apply_settings(
                dataclasses.replace(SMALL, timeframe="30 минут", trailing_enabled=True)
            )
            await bench.port.wait()
            await _ride(
                bench, LIVE_FROM + timedelta(minutes=50), since=LIVE_FROM + timedelta(minutes=21)
            )
            return bench.port.values, bench.port.live_status(), _takes(broker)
        finally:
            await _close(bench, session)

    values, status, takes = loop.run_until_complete(go())
    assert values.trailing_enabled, "«Применить» отказало — рубеж не проверяется"
    assert UNCLOSED in status.held, "пересборка не отложена — рубеж не проверяется"
    assert status.position is not None and status.position.signed == 1, status.position
    assert takes, "у входа нет неподвижного тейка у брокера: «факт» пошёл скользящим на 15 минутах"


@pytest.mark.xfail(
    strict=True,
    reason="B-084: запасной рубеж `_held_frame` держит прежние настройки движка молча",
)
def test_a_rebuild_held_by_an_unclosed_command_says_the_new_take_did_not_reach_the_live(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Н1, запасной рубеж: пересборку держит незакрытая команда, а не позиция.

    Позиции нет — «Применить» прежний ряд не проверяет, и «свеча 30 +
    скользящий уровень» принято. Пересборка отложена командой; на прежнем
    ряду 15 минут при пороге 15 скользящий уровень нарушает D-141, и «факт»
    остаётся на действующих настройках движка — это верно. Но строка
    «Пересборка боя отложена» говорит «тейк уже действует для „факта“»,
    а окно показывает скользящий уровень включённым. Стережёт: человеку
    сказано, что новые настройки движка до боя не дошли и почему (D-141).
    """
    async def go() -> list[tuple[str, str]]:
        bench, session = await _bench(tmp_path, broker, SMALL)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=20))
            assert bench.port.live_status().position is None, "позиция есть — не тот случай"
            said = len(bench.notes)
            monkeypatch.setattr(HistoryPort, "_fact_busy", _held_by_a_command)
            bench.port.apply_settings(
                dataclasses.replace(SMALL, timeframe="30 минут", trailing_enabled=True)
            )
            await bench.port.wait()
            assert UNCLOSED in bench.port.live_status().held, "пересборка не отложена"
            return bench.notes[said:]
        finally:
            await _close(bench, session)

    notes = loop.run_until_complete(go())
    told = [reason for _event, reason in notes if "D-141" in reason]
    assert told, f"что скользящий уровень до боя не дошёл, не сказано: {notes}"
