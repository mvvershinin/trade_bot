"""Включение боя: каждая помеха не пускает бой и называет себя словами (Э4 2в).

План `.docs/plans/E4-live-orders.md` §12 (2в, 2г), решение 0066. Поведение,
которое здесь стережётся, — то, что владелец счёта видит у кнопки «Бой»:

* бой недоступен, пока ему мешает хоть одна помеха из тех, что порт знает
  без брокера: нет подачи заявок в сборке, нет потока котировок, показ
  закреплён на отрезке истории, робот остановлен, скользящий уровень на
  свече не крупнее порога минуток, шаг цены неизвестен. Каждая — своей
  фразой в снимке боя (`LiveStatus.why_not`) и в строке «Бой не включён»
  на нажатие; ни одной команды брокеру, просьба боя снята;
* без помех тот же стенд бой включает — иначе зелёные отказы выше
  ничего бы не доказывали;
* поток статусов заявок не открылся за срок ожидания — бой не пускает;
* команда журнала, оставшаяся незакрытой после сверки, бой не пускает;
* что сверка сделала с командами прошлых запусков, сказано строкой;
* сборка боя упала, прогон боя не открылся в журнале — бой не включён,
  сказано словами, кнопка не висит «включается»;
* негодный предел ожидания отвергается уже на «Применить» — портом;
* шесть пределов ожидания лежат в снимке боевого прогона.

Стенд — тот же, что у пары (`tests/test_app_live_pair.py`): порт, живой ход,
сборка боевой стороны и боевой исполнитель на `tests/fake_broker.py`.
Изоляция: своя база во временном каталоге, свой токен в своём `userdata`,
сети нет. Каждую проверку можно гонять поодиночке.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest
import websockets

os.environ.setdefault("QT_API", "pyside6")  # до первого импорта qasync

from app.fact import FactDesk, FactRequest, LiveStatus
from app.live_trading import LiveDesks
from app.port import HistoryPort
from broker.session import BrokerSession
from broker.throttle import Throttle
from broker.tokens import TokenScope
from market import (
    BACKTEST_SESSIONS_KEPT,
    CandleStore,
    JournalSession,
    MarketWorker,
    PruneReport,
)
from market.commands import BrokerCommand, CommandAction, CommandSide
from market.journal import RunOrigin, SessionRecord, redact
from tests.exchange_card import exchange
from tests.fake_broker import CREATE, FakeBroker
from tests.test_app_live_pair import (
    DAY,
    OWNER,
    STEP,
    SYMBOL,
    Bench,
    Exchange,
    _close,
    _connect,
    _history,
    _token,
)
from tests.test_broker_session import Clock, no_sleep
from ui.models import FIELD_CAPTIONS, BacktestRequest
from ui.models import Settings as UiSettings

#: Ключи команд прошлого запуска — выдуманные, в форме UUID, как у брокера.
LONE_CANCEL = "00000000-0000-4000-8000-0000000000c1"
GONE_TARGET = "00000000-0000-4000-8000-0000000000c0"
LIVE_TAKE = "00000000-0000-4000-8000-0000000000d1"

#: Пределы ожидания — не умолчания: с умолчаниями проверка снимка зеленела бы
#: и при числах, взятых не из окна.
WITH_LIMITS = dataclasses.replace(
    OWNER,
    live_stale_after_sec=97,
    live_event_wait_sec=7,
    live_fill_wait_sec=43,
    live_cancel_wait_sec=19,
    live_position_settle_sec=2.5,
    live_reconnect_pauses_sec=(3, 11),
)

#: Строки снимка прогона под эти значения — подпись снимка и числа в ней.
#: Записаны руками, а не взяты у `ProgramFields` и его подписей: сверка
#: таблицы с самой собой зеленела бы, потеряй таблица поле целиком.
SNAPSHOT: dict[str, tuple[str, ...]] = {
    "Допустимое опоздание заявки, с": ("97",),
    "Ждать первого известия о заявке, с": ("7",),
    "Ждать исполнения рыночной заявки, с": ("43",),
    "Ждать подтверждения снятия заявки, с": ("19",),
    "Пауза перед повторной сверкой позиции, с": ("2,5",),
    "Паузы между попытками восстановить связь, с": ("3, 11",),
}


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> FakeBroker:
    fake = FakeBroker()
    monkeypatch.setattr(websockets, "connect", fake.connect)
    return fake


async def _build(
    tmp_path: pathlib.Path,
    broker: FakeBroker,
    values: UiSettings = OWNER,
    *,
    desks: bool = True,
    step: float | None = STEP,
    before: Callable[[MarketWorker], Awaitable[None]] | None = None,
) -> tuple[Bench, BrokerSession]:
    """Стенд пары с выбором: давать ли порту боевую сторону и шаг цены."""
    path = tmp_path / "candles.sqlite3"
    _history(path)
    worker = MarketWorker(path)
    await worker.open()
    if before is not None:
        await before(worker)
    ex = Exchange(broker)
    session = BrokerSession(
        _token(tmp_path / "userdata", TokenScope.TRADE), client=broker.client(), clock=Clock(),
        sleep=no_sleep, attempts=3, throttle=Throttle(rate=1000.0),
        order_throttle=Throttle(rate=1000.0),
    )
    port = HistoryPort(worker, values=values, days=0, sanitize=redact, clock=lambda: ex.now)
    ex.port = port
    notes: list[tuple[str, str]] = []
    port.decision_appended.connect(
        lambda row: notes.append((getattr(row, "event", ""), getattr(row, "reason", "")))
    )
    if step is not None:
        port.attach_exchange(exchange(step))
    port.attach_stream(lambda on: None)
    real = LiveDesks(lambda: session, worker, class_code="SPBFUT", clock=lambda: ex.now)
    if desks:
        port.attach_live(real)
    return Bench(port, broker, ex, worker, notes, real), session


def _refused(notes: list[tuple[str, str]]) -> str:
    return " ".join(reason for event, reason in notes if event == "Бой не включён")


def _created(broker: FakeBroker) -> list[str]:
    return [key for kind, key in broker.commands if kind == "create"]


# --------------------------------------------------------------- помехи без брокера


async def _pin(port: HistoryPort) -> None:
    """Закрепить показ на отрезке истории — тестером, как это делает окно."""
    port.run_backtest(BacktestRequest(
        settings=port.values, since=DAY - timedelta(days=1), until=DAY.replace(hour=8),
        settings_source="текущие настройки",
    ))
    await port.wait()


async def _halt(port: HistoryPort) -> None:
    port.halt("Проверка", "Робот остановлен проверкой.")
    await port.wait()


@dataclass(frozen=True)
class Gate:
    """Одна помеха: её фраза и как её поставить на стенде."""

    words: str
    values: UiSettings = OWNER
    desks: bool = True
    step: float | None = STEP
    stream: bool = True
    then: Callable[[HistoryPort], Awaitable[None]] | None = None


GATES = [
    pytest.param(
        Gate("Подача заявок в этой сборке программы не подключена", desks=False),
        id="no-order-path",
    ),
    pytest.param(Gate("Нет потока котировок", stream=False), id="no-quote-stream"),
    pytest.param(Gate("Показ закреплён на отрезке истории", then=_pin), id="pinned-span"),
    pytest.param(
        Gate("Робот остановлен, сначала «Возобновить работу»: Робот остановлен проверкой",
             then=_halt),
        id="halted",
    ),
    pytest.param(
        Gate("Скользящий уровень на свече 5 мин не крупнее порога проверки по минуткам (5 мин)",
             values=dataclasses.replace(OWNER, timeframe="5 минут")),
        id="trailing-on-a-small-bar",
    ),
    pytest.param(Gate("Шаг цены инструмента не известен", step=None), id="no-price-step"),
]


@pytest.mark.parametrize("gate", GATES)
def test_each_gate_keeps_live_off_and_names_itself(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, gate: Gate
) -> None:
    """Помеха — бой недоступен, её фраза в снимке и в отказе на нажатие, брокеру ничего.

    Снимок боя говорит «нельзя» своей фразой — это строка у кнопки в окне.
    Нажатие всё равно (кнопку можно нажать до снимка) — отказ той же фразой,
    просьба снята, ни одной заявки, бой не включался.
    """

    async def go() -> tuple[LiveStatus, LiveStatus, list[tuple[str, str]]]:
        bench, session = await _build(
            tmp_path, broker, gate.values, desks=gate.desks, step=gate.step
        )
        try:
            if gate.stream:
                await _connect(bench, live=False)
            else:
                bench.port.refresh("проверка")
                await bench.port.wait()
            if gate.then is not None:
                await gate.then(bench.port)
            before = bench.port.live_status()
            bench.notes.clear()
            bench.port.set_live(True)
            await bench.port.wait()
            return before, bench.port.live_status(), list(bench.notes)
        finally:
            await _close(bench, session)

    before, after, notes = loop.run_until_complete(go())
    assert not before.available, before
    assert any(gate.words in why for why in before.why_not), before.why_not
    assert gate.words in _refused(notes), notes
    assert not after.wanted and not after.on, after
    assert gate.words in after.refused, after.refused
    assert "Бой включён" not in [event for event, _ in notes], notes
    assert _created(broker) == [], broker.commands


def test_without_a_gate_the_same_bench_turns_live_on(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """Контроль к помехам: тот же стенд без помехи — бой доступен и включается.

    Без него отказы выше зеленели бы и на стенде, где бой не включается
    никогда, — по причине, которой проверка не назвала.
    """

    async def go() -> tuple[LiveStatus, LiveStatus, list[str]]:
        bench, session = await _build(tmp_path, broker)
        try:
            await _connect(bench, live=False)
            before = bench.port.live_status()
            bench.port.set_live(True)
            await bench.port.wait()
            return before, bench.port.live_status(), bench.events()
        finally:
            await _close(bench, session)

    before, after, events = loop.run_until_complete(go())
    assert before.available and before.why_not == (), before.why_not
    assert after.on and after.armed, after
    assert "Бой включается" in events and "Бой включён" in events, events


# --------------------------------------------------------------- журнал команд


def test_a_status_stream_that_does_not_open_keeps_live_off_and_says_so(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Поток статусов заявок не открылся за срок ожидания — бой не включён, сказано почему.

    Без потока программа не узнает о сделке: заявку, исполнение которой
    не увидеть, подавать нельзя. Срок — поле окна «Ждать первого известия
    о заявке» (1 с, чтобы проверка не ждала дольше).
    """

    def refuse(*_args: object, **_kwargs: object) -> object:
        raise OSError("поток статусов недоступен на проверке")

    monkeypatch.setattr(websockets, "connect", refuse)
    values = dataclasses.replace(OWNER, live_event_wait_sec=1)

    async def go() -> tuple[LiveStatus, list[tuple[str, str]]]:
        bench, session = await _build(tmp_path, broker, values)
        try:
            await _connect(bench)
            return bench.port.live_status(), list(bench.notes)
        finally:
            await _close(bench, session)

    status, notes = loop.run_until_complete(go())
    assert "Поток статусов заявок не открылся за 1 с" in _refused(notes), notes
    assert not status.wanted and not status.on, status
    assert _created(broker) == []


def test_a_command_left_open_by_the_check_keeps_live_off(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """0066: незакрытая команда журнала после сверки — бой не включается, сказано числом.

    Снятие прошлого запуска, чья заявка в журнале не значится (её строку
    закрыли раньше): сверке разбирать нечего, команда остаётся незакрытой.
    Брокер на месте и отвечает — отказ идёт не от «судьба не выяснена»,
    а от правила «бой — только без незакрытых команд».
    """

    async def plant(worker: MarketWorker) -> None:
        def write(store: CandleStore) -> None:
            run = store.open_journal_session(
                SessionRecord(RunOrigin.LIVE, symbol=SYMBOL), now=DAY.replace(hour=8)
            )
            store.record_broker_command(run.id, BrokerCommand(
                client_order_id=LONE_CANCEL, action=CommandAction.CANCEL,
                order_id="take:long:прошлый", symbol=SYMBOL,
                target_client_order_id=GONE_TARGET, account=broker.account,
            ))

        await worker.call(write)

    async def go() -> tuple[LiveStatus, list[tuple[str, str]]]:
        bench, session = await _build(tmp_path, broker, before=plant)
        try:
            await _connect(bench)
            return bench.port.live_status(), list(bench.notes)
        finally:
            await _close(bench, session)

    status, notes = loop.run_until_complete(go())
    refused = _refused(notes)
    assert "незакрытых: 1" in refused and "только без незакрытых команд" in refused, notes
    assert LONE_CANCEL in refused, refused
    assert not status.on and not status.wanted
    assert _created(broker) == [], broker.commands


def test_what_the_check_did_with_an_old_live_order_is_said(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """0017: живую заявку прошлого запуска сверка снимает — и говорит это строкой.

    Тейк прошлого запуска жив у брокера, позиции нет. Сверка при включении
    его снимает (`4` у брокера) и бой включается; что снято, человек
    узнаёт строкой «Сверка с брокером», а не по пропавшей заявке в
    приложении брокера.
    """

    async def plant(worker: MarketWorker) -> None:
        def write(store: CandleStore) -> None:
            run = store.open_journal_session(
                SessionRecord(RunOrigin.LIVE, symbol=SYMBOL), now=DAY.replace(hour=8)
            )
            store.record_broker_command(run.id, BrokerCommand(
                client_order_id=LIVE_TAKE, action=CommandAction.CREATE,
                order_id="take:long:прошлый", symbol=SYMBOL, side=CommandSide.SELL,
                volume=1, price=251_000.0, account=broker.account,
            ))

        await worker.call(write)

    async def go() -> tuple[LiveStatus, list[tuple[str, str]], str]:
        # Заявка прошлого запуска — тем же путём, каким её подала бы программа:
        # книга подделки ведёт её отчёты сама, и статус по ключу её знает.
        async with broker.client() as client:
            await client.post(CREATE[1], json={
                "clientOrderId": LIVE_TAKE, "side": "2", "orderType": "2",
                "orderQuantity": 1, "ticker": SYMBOL, "classCode": "SPBFUT", "price": 251_000.0,
            })
        order = broker.order(LIVE_TAKE)
        assert order is not None and order.live, "подделка не приняла заявку прошлого запуска"
        bench, session = await _build(tmp_path, broker, before=plant)
        try:
            await _connect(bench)
            return bench.port.live_status(), list(bench.notes), order.status
        finally:
            await _close(bench, session)

    status, notes, at_broker = loop.run_until_complete(go())
    assert at_broker == "4", "живую заявку прошлого запуска сверка не сняла — проверять нечего"
    said = " ".join(reason for event, reason in notes if event == "Сверка с брокером")
    assert "take:long:прошлый" in said and "снята" in said, notes
    assert status.on, status.refused


# --------------------------------------------------------------- беды сборки


@dataclass
class _Broken:
    """Боевая сторона, чья сборка падает ошибкой программы, а не отказом."""

    real: LiveDesks

    def refusal(self) -> str | None:
        return self.real.refusal()

    async def open(self, request: FactRequest) -> FactDesk:
        raise RuntimeError("сборка исполнителя сломалась на проверке")


def test_a_crash_while_building_the_executor_is_said_and_releases_the_button(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """Ошибка сборки исполнителя — «Бой не включён» словами, просьба снята, брокеру ничего.

    Правило 13: упавшая сборка без строки оставила бы кнопку «включается…»
    навсегда, а ход — наблюдением, и человек не узнал бы почему.
    """

    async def go() -> tuple[LiveStatus, list[tuple[str, str]]]:
        bench, session = await _build(tmp_path, broker)
        bench.port.attach_live(_Broken(bench.desks))
        try:
            await _connect(bench)
            return bench.port.live_status(), list(bench.notes)
        finally:
            await _close(bench, session)

    status, notes = loop.run_until_complete(go())
    refused = _refused(notes)
    assert "Боевой исполнитель не собран" in refused, notes
    assert "сломалась на проверке" in refused, refused
    assert not status.wanted and not status.on
    assert "Боевой исполнитель не собран" in status.refused
    assert _created(broker) == []


def test_a_live_run_the_journal_did_not_open_keeps_live_off_and_says_so(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """С5: прогон боя не открылся в журнале прогонов — бой не включён, сказано почему.

    Без прогона журнал команд писать некуда, а без журнала заявка не уходит.
    Журнал отказывает только боевому прогону: ход наблюдения при этом
    свой прогон открывает, и стенд в остальном цел.
    """
    original = MarketWorker.open_journal_session

    async def refuse_live(
        self: MarketWorker,
        run: SessionRecord,
        *,
        now: datetime | None = None,
        keep_backtest_sessions: int = BACKTEST_SESSIONS_KEPT,
    ) -> tuple[JournalSession, PruneReport]:
        if run.origin is RunOrigin.LIVE:
            raise sqlite3.OperationalError("журнал занят проверкой")
        return await original(self, run, now=now, keep_backtest_sessions=keep_backtest_sessions)

    monkeypatch.setattr(MarketWorker, "open_journal_session", refuse_live)

    async def go() -> tuple[LiveStatus, list[tuple[str, str]]]:
        bench, session = await _build(tmp_path, broker)
        try:
            await _connect(bench)
            return bench.port.live_status(), list(bench.notes)
        finally:
            await _close(bench, session)

    status, notes = loop.run_until_complete(go())
    assert "Прогон боя не открылся в журнале прогонов" in _refused(notes), notes
    assert not status.wanted and not status.on, status
    assert _created(broker) == [] and broker.handshakes == [], "исполнитель собран без журнала"


def test_a_bad_wait_limit_is_refused_by_the_port_on_apply(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """«Применить» с негодным пределом: порт отвергает набор, прежние значения в силе.

    Та же проверка, что при сборке исполнителя: граница, принятая окном,
    — та, что дойдёт до брокера. Отказ называет поле подписью окна.
    """
    refused: list[tuple[str, str]] = []

    async def go() -> tuple[UiSettings, list[tuple[str, str]]]:
        bench, session = await _build(tmp_path, broker)
        bench.port.settings_refused.connect(
            lambda _values, reason, field: refused.append((reason, field))
        )
        try:
            bench.port.apply_settings(dataclasses.replace(OWNER, live_cancel_wait_sec=0))
            await bench.port.wait()
            return bench.port.values, list(bench.notes)
        finally:
            await _close(bench, session)

    values, notes = loop.run_until_complete(go())
    caption = FIELD_CAPTIONS["live_cancel_wait_sec"]
    assert values.live_cancel_wait_sec == OWNER.live_cancel_wait_sec
    assert refused and refused[-1][1] == "live_cancel_wait_sec", refused
    said = " ".join(reason for event, reason in notes if event == "Настройки не приняты")
    assert f"«{caption}»" in said, notes


def test_a_bad_wait_limit_from_the_file_is_replaced_and_does_not_block_the_exchange_card(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """Предел из файла вне границ — умолчание вслух, как у чисел движка (`B-050`).

    Файл настроек границ пределов не проверяет (`SettingsStore` пропускает
    `0`), а проверка набора на «Применить» — проверяет. Набор с таким
    пределом, оставленный в силе, роняет каждое следующее применение — и
    первым карточку биржи: шаг цены не встаёт, бой недоступен с чужой
    причиной. Ожидаемое: предел заменён умолчанием, сказано подписью окна,
    шаг цены пришёл, бой доступен.
    """
    values = dataclasses.replace(OWNER, live_fill_wait_sec=0)

    async def go() -> tuple[UiSettings, LiveStatus, list[tuple[str, str]]]:
        bench, session = await _build(tmp_path, broker, values)
        # Строка запуска сказана при сборке порта, до подписки на строки по одной:
        # окну она приходит с журналом первого прохода (`_publish_journal`).
        journals: list[tuple[str, str]] = []
        bench.port.decisions_replaced.connect(lambda rows: journals.extend(
            (getattr(row, "event", ""), getattr(row, "reason", "")) for row in rows
        ))
        try:
            await _connect(bench, live=False)
            return bench.port.values, bench.port.live_status(), [*bench.notes, *journals]
        finally:
            await _close(bench, session)

    taken, status, notes = loop.run_until_complete(go())
    caption = FIELD_CAPTIONS["live_fill_wait_sec"]
    assert taken.price_step == STEP, "карточка биржи не легла: шага цены нет"
    assert taken.live_fill_wait_sec == UiSettings().live_fill_wait_sec
    assert status.available, status.why_not
    assert any(f"«{caption}»" in reason and "умолчани" in reason for _, reason in notes), notes


# --------------------------------------------------------------- снимок прогона


def test_the_live_run_records_all_six_wait_limits(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """Правило 16: шесть пределов ожидания — в снимке боевого прогона, подписью и числом.

    Через месяц по снимку разбирают, с какими пределами робот подавал
    заявки. Строки снимка записаны здесь руками (`SNAPSHOT`), а не взяты
    у `ProgramFields`: сверка таблицы с самой собой зеленела бы, потеряй
    таблица поле целиком.
    """

    async def go() -> None:
        bench, session = await _build(tmp_path, broker, WITH_LIMITS)
        try:
            await _connect(bench)
            assert bench.port.live_status().on, "бой не включился — проверять нечего"
        finally:
            await _close(bench, session)

    loop.run_until_complete(go())
    with CandleStore(tmp_path / "candles.sqlite3") as store:
        live = [one for one in store.journal_sessions(limit=99).rows
                if one.origin is RunOrigin.LIVE]
    assert len(live) == 1, live
    text = live[0].settings
    lines = dict(
        line.strip().split(": ", 1) for line in text.splitlines() if ": " in line
    )
    wrong = {
        title: lines.get(title)
        for title, parts in SNAPSHOT.items()
        if title not in lines or not all(part in lines[title] for part in parts)
    }
    assert not wrong, f"в снимке боевого прогона не так: {wrong}\n{text}"
    # Паузы — как в окне, а не кортежем Питона: «(3, 11)» содержит «3, 11»
    # и прошло бы проверку на вхождение выше.
    pauses = lines.get("Паузы между попытками восстановить связь, с")
    assert pauses == "3, 11", f"паузы в снимке записаны «{pauses}»"
