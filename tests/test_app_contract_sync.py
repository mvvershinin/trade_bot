"""Уточнение контрактов при запуске и переход с истёкшего кода (29.09.2026).

Что стережётся, словами
-----------------------
1. **Запуск на истёкшем коде переходит на действующий сам.** База с пустой
   таблицей контрактов, настройки MXU6; биржа говорит: MXU6 истёк, MXZ6
   действующий. После запуска (`first_run`) без единого нажатия: инструмент
   MXZ6, эхо настроек для файла несёт MXZ6, в журнале строка «MXU6 истёк …;
   перешёл на действующий MXZ6», загрузка минут MXZ6 ушла к бирже.
2. **Решение 0016 в силе.** Робот запущен или на счёте позиция — перехода
   нет: инструмент MXU6, в окно ушла плашка с расхождением, минуты
   не запрашивались.
3. **Подключение ждёт уточнения.** «Подключиться» во время уточнения
   не подписывается на истёкший код: поток включается один раз и уже
   на MXZ6. До правки таблица читалась пустой, срок был «неизвестен»,
   и поток вставал на MXU6 (журнал владельца счёта 29.09.2026, 14:38).
4. **Нет связи — работа идёт как раньше.** Одна строка в журнал, прогон
   идёт на прежнем коде, программа не встаёт.

Сеть — заглушкой (`Chain` из `test_ui_contract_load`), настоящий транспорт
обезврежен.
"""

from __future__ import annotations

import asyncio
import pathlib
import threading
import urllib.parse
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta

import pytest

from app import port as port_module
from app.port import HistoryPort
from market import MSK, HttpxTransport, MarketWorker
from market.journal import redact
from tests.test_ui_contract_load import (
    ROLL_BACK,
    TODAY,
    Chain,
    Heard,
    _description_body,
    _noon,
    _seed_mxu6,
)
from ui.models import Mode, Position, RobotState, Settings, Side


@pytest.fixture(autouse=True)
def the_real_transport_is_disarmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Настоящий транспорт биржи обезврежен на весь файл (см. `B-034`)."""

    def refuse(self: object, url: str, *, timeout: float) -> bytes:
        raise AssertionError(f"тест полез в настоящий интернет: {url}")

    monkeypatch.setattr(HttpxTransport, "get", refuse)


class ExpiredChain(Chain):
    """Биржа, у которой MXU6 уже истёк: последний день — день рубежа MXZ6."""

    def answer(self, url: str) -> bytes:
        parsed = urllib.parse.urlparse(url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        code = parsed.path.rsplit("/", 1)[-1].removesuffix(".json")
        if query.get("iss.only") == "description" and code == "MXU6":
            return _description_body(self.today - timedelta(days=ROLL_BACK))
        return super().answer(url)


class DeadChain(Chain):
    """Биржа, до которой не достучаться."""

    def answer(self, url: str) -> bytes:
        raise OSError("нет связи с биржей")


class GatedChain(Chain):
    """Биржа, которая не отвечает, пока её не отпустят: долгое уточнение."""

    def __init__(self, today) -> None:
        super().__init__(today)
        self.gate = threading.Event()

    def answer(self, url: str) -> bytes:
        self.gate.wait(10)
        return super().answer(url)


def _descriptions(chain: Chain) -> int:
    return sum("iss.only=description" in url for url in chain.transport.urls)


class Link:
    """Подставное подключение к брокеру: на какой код его включали."""

    def __init__(self, port: HistoryPort) -> None:
        self.port = port
        self.switched: list[tuple[bool, str]] = []

    def switch(self, on: bool) -> str | None:
        self.switched.append((on, self.port.values.instrument))
        return None

    def retarget(self, symbol: str) -> None:
        return None


def _minutes_asked(chain: Chain, code: str) -> list[str]:
    return [
        url for url in chain.transport.urls
        if f"/securities/{code}/candles" in url
        and dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query)).get("interval") == "1"
    ]


def _start(loop, database: pathlib.Path, chain: Chain, *, state: RobotState | None = None,
           connect: bool = False, instrument: str = "MXU6",
           clock: Callable[[], datetime] | None = None,
           during: Callable[[HistoryPort, Heard], Awaitable[None]] | None = None):
    """Запуск как у `app/main.py`: проводка, `first_run`, «Подключиться» сразу.

    `during` — что делать сразу после запуска, до ожидания конца работ.
    """

    async def main():
        worker = MarketWorker(database, iss=chain.client)
        await worker.open()
        port = HistoryPort(worker, values=Settings(instrument=instrument), days=0,
                           sanitize=redact, clock=clock or _noon(chain.today))
        heard = Heard(port)
        applied: list[Settings] = []
        port.settings_applied.connect(applied.append)
        link = Link(port)
        port.attach_stream(link.switch, retarget=link.retarget)
        if state is not None:
            port._last_state = state  # noqa: SLF001 — состояние робота без прогона и брокера
        try:
            port.first_run("запуск программы")
            if connect:
                port.stream(True)
            if during is not None:
                await during(port, heard)
            for _ in range(5):
                await port.wait()
                await asyncio.sleep(0.02)
        finally:
            await port.aclose()
            await worker.close()
        return port, heard, applied, link

    return loop.run_until_complete(main())


@pytest.fixture
def base(tmp_path: pathlib.Path) -> pathlib.Path:
    """База владельца счёта 29.09: минуты MXU6, таблица контрактов пуста."""
    database = tmp_path / "userdata" / "candles.sqlite3"
    database.parent.mkdir()
    _seed_mxu6(database, [30, 20, 10])
    return database


def test_an_expired_code_switches_to_the_current_one_at_startup(loop, base) -> None:
    """Стережёт 1: MXU6 истёк → MXZ6, в файл, в журнал, минуты MXZ6 запрошены.

    Мутации, которые тест ловит: убрать переход (`_switch_to_current` → "")
    — инструмент останется MXU6; убрать загрузку после перехода — к бирже
    не уйдёт ни одного запроса минут MXZ6; вернуть подмену файла (`for_file`
    с прежним инструментом) — в файл уйдёт MXU6.
    """
    chain = ExpiredChain(TODAY)

    port, heard, applied, _ = _start(loop, base, chain)

    assert port.values.instrument == "MXZ6", "программа осталась на истёкшем MXU6"
    assert applied and port.for_file(applied[-1]).instrument == "MXZ6", (
        f"в файл настроек уходит не MXZ6: {[one.instrument for one in applied]}"
    )
    said = [row for row in heard.notes if row.event == "Переход на действующий контракт"]
    roll = TODAY - timedelta(days=ROLL_BACK)
    assert said, "переход в журнал не сказан"
    assert f"MXU6 истёк {roll:%d.%m.%Y}" in said[-1].reason, said[-1].reason
    assert "перешёл на действующий MXZ6" in said[-1].reason, said[-1].reason
    assert _minutes_asked(chain, "MXZ6"), "загрузка минут MXZ6 не запрошена"
    assert heard.finished and heard.finished[-1].ok, heard.finished
    assert heard.charts and heard.charts[-1].instrument == "MXZ6", (
        "график после запуска не на MXZ6"
    )


@pytest.mark.parametrize(
    "state",
    [
        RobotState(running=True, mode=Mode.REVERSE),
        RobotState(simulation=False, position=Position(side=Side.LONG, volume=1,
                                                         entry_price=100.0)),
    ],
    ids=["robot-running", "account-position"],
)
def test_no_switch_under_a_running_robot_or_a_position(loop, base, state) -> None:
    """Стережёт 2: при запущенном роботе и позиции — только плашка.

    Мутация, которую тест ловит: убрать проверку `switch_blocked_reason`
    в `_switch_to_current` — `apply_settings` откажет вслух «Настройки
    не приняты», а тест требует, чтобы перехода не пробовали вовсе.
    """
    chain = ExpiredChain(TODAY)

    port, heard, _, _ = _start(loop, base, chain, state=state)

    assert port.values.instrument == "MXU6", "инструмент сменён под роботом или позицией"
    assert not [row for row in heard.notes if row.event == "Настройки не приняты"], (
        "переход пробовали и получили отказ — проверка блокировки не стоит до перехода"
    )
    assert heard.contracts and heard.contracts[-1].current == "MXZ6", heard.contracts
    assert heard.contracts[-1].mismatch, "плашка о расхождении в окно не ушла"
    assert not _minutes_asked(chain, "MXZ6"), "история MXZ6 грузилась без перехода"


def test_connecting_during_the_check_does_not_subscribe_to_the_expired_code(
    loop, base
) -> None:
    """Стережёт 3: «Подключиться» во время уточнения — поток один раз и на MXZ6.

    Мутация, которую тест ловит: убрать ворота `syncing` в `stream` —
    поток включится на MXU6 до того, как биржа скажет, что он истёк.
    """
    chain = ExpiredChain(TODAY)

    _, _, _, link = _start(loop, base, chain, connect=True)

    assert link.switched == [(True, "MXZ6")], (
        f"поток включали не один раз на MXZ6: {link.switched}"
    )


def test_without_network_the_program_says_so_and_goes_on(loop, base) -> None:
    """Стережёт 4: нет связи — строка в журнал, прогон на прежнем коде."""
    chain = DeadChain(TODAY)

    port, heard, _, link = _start(loop, base, chain, connect=True)

    said = [row for row in heard.notes if row.event == "Контракты не уточнены у биржи"]
    assert len(said) == 1, f"об отказе биржи сказано {len(said)} раз"
    assert port.values.instrument == "MXU6"
    assert heard.charts and heard.charts[-1].instrument == "MXU6", "прогон не пошёл"
    assert link.switched, "подключение, попрошенное во время уточнения, потеряно"


def test_a_live_code_known_to_the_base_does_not_wait_for_the_exchange(loop, base) -> None:
    """Обычный запуск: таблица в базе ручается за MXZ6 — график не ждёт биржу.

    Мутация, которую тест ловит: убрать раннее отпускание (`_vouched`) —
    прогон ждёт ответа биржи, а биржа здесь не отвечает, пока график
    не нарисован.
    """
    _start(loop, base, ExpiredChain(TODAY))  # первый запуск заполнил таблицу
    chain = GatedChain(TODAY)
    drawn: list[str] = []

    async def during(port: HistoryPort, heard: Heard) -> None:
        try:
            for _ in range(300):
                if heard.charts:
                    drawn.append(heard.charts[-1].instrument)
                    return
                await asyncio.sleep(0.01)
        finally:
            chain.gate.set()

    port, _, _, _ = _start(loop, base, chain, instrument="MXZ6", during=during)

    assert drawn == ["MXZ6"], "график ждал ответа биржи при живом коде в базе"
    assert _descriptions(chain), "уточнение у биржи при запуске не пошло"
    assert port.values.instrument == "MXZ6"


def test_the_check_repeats_when_the_day_changes(loop, base, monkeypatch) -> None:
    """Раз в сутки при работе: сменились сутки — биржу спрашивают снова.

    Мутация, которую тест ловит: не заводить часы (`daily`) — второго
    уточнения нет.
    """
    monkeypatch.setattr(port_module, "_SYNC_LOOK_MS", 10)
    chain = ExpiredChain(TODAY)
    moment = [datetime.combine(TODAY, time(12), MSK)]
    asked: list[int] = []

    async def during(port: HistoryPort, heard: Heard) -> None:
        await port.wait()
        asked.append(_descriptions(chain))
        moment[0] += timedelta(days=1)
        for _ in range(300):
            if _descriptions(chain) > asked[0]:
                break
            await asyncio.sleep(0.01)
        asked.append(_descriptions(chain))

    _start(loop, base, chain, clock=lambda: moment[0], during=during)

    assert asked[0] > 0, "первое уточнение не пошло — проверка вакуумна"
    assert asked[1] > asked[0], "на смене суток биржу не спросили снова"


def test_the_first_chart_waits_for_the_check_and_never_shows_the_expired_code(
    loop, base
) -> None:
    """Стережёт 3, прогонную половину: первый прогон ждёт уточнения таблицы.

    Запуск на истёкшем MXU6: ни один график за весь запуск не нарисован
    на MXU6 — прогон, попрошенный `first_run`, придержан (`holding`)
    и отпущен уже на MXZ6. Без придержки окно сначала показало бы истёкший
    контракт, а через секунду — другой.

    Мутации, которые тест ловит (проверено 29.09.2026): убрать ворота
    `holding` в `refresh`; звать `sync_contracts(..., hold=False)`
    из `first_run`. Обе оставляли зелёным весь остальной файл: там
    проверяется только последний график.
    """
    chain = ExpiredChain(TODAY)

    _, heard, _, _ = _start(loop, base, chain)

    drawn = [chart.instrument for chart in heard.charts]
    assert drawn, "за запуск не нарисовано ни одного графика — проверка вакуумна"
    assert "MXU6" not in drawn, f"до уточнения нарисован истёкший код: {drawn}"


def test_an_exception_during_the_switch_releases_the_run_and_is_said(
    loop, base, monkeypatch
) -> None:
    """Исключение при переходе: флаги сняты, прогон идёт, строка в журнале.

    Мутация, которую тест ловит: вернуть `_release_sync()` из `finally`
    обратно за вызов перехода — исключение оставит `syncing`/`holding`:
    прогон не пойдёт (графика нет), подключение не включится; убрать
    строку журнала — её нет.
    """
    chain = ExpiredChain(TODAY)
    original = HistoryPort.apply_settings

    def broken(self: HistoryPort, settings: Settings, *, by_person: bool = True) -> None:
        if not by_person:
            raise RuntimeError("сбой записи настроек")
        original(self, settings, by_person=by_person)

    monkeypatch.setattr(HistoryPort, "apply_settings", broken)

    port, heard, _, link = _start(loop, base, chain, connect=True)

    assert heard.charts, "прогон после исключения при переходе не пошёл"
    # MXU6 истёк: подключение, попрошенное во время уточнения, обязано
    # дойти до отказа вслух — значит, ворота `syncing` открылись.
    assert [row for row in heard.notes if row.event == "Подключение к брокеру"], (
        "подключение, попрошенное во время уточнения, так и ждёт"
    )
    assert not link.switched, link.switched
    said = [row for row in heard.notes if row.event == "Переход на действующий контракт не удался"]
    assert said and "сбой записи настроек" in said[-1].reason, (
        "исключение при переходе в журнал решений не сказано"
    )
    assert port.values.instrument == "MXU6"


def test_an_archived_but_still_traded_code_is_not_switched_by_itself(
    loop, base, monkeypatch
) -> None:
    """Архивный код (период ближнего кончился, последний день впереди) — только плашка.

    И при запуске (снимка состояния ещё нет), и на суточной проверке.
    Мутация, которую тест ловит: вернуть `Expiry.ARCHIVED` в набор
    вердиктов перехода `_switch_to_current` — инструмент станет MXZ6.
    """
    from market.contracts import Expiry, expiry_verdict
    from market.storage import CandleStore

    monkeypatch.setattr(port_module, "_SYNC_LOOK_MS", 10)
    chain = Chain(TODAY)  # у MXU6 последний день через 80 дней: ещё торгуется
    moment = [datetime.combine(TODAY, time(12), MSK)]
    seen: list[tuple[str, int]] = []
    kinds: list[object] = []

    async def during(port: HistoryPort, heard: Heard) -> None:
        await port.wait()
        with CandleStore(base) as store:
            rows = store.contracts()
        kinds.append(expiry_verdict(rows, "MXU6", today=TODAY,
                                    halt_days=port.values.expiry_halt_days).kind)
        seen.append((port.values.instrument, _descriptions(chain)))
        moment[0] += timedelta(days=1)
        for _ in range(300):
            if _descriptions(chain) > seen[0][1]:
                break
            await asyncio.sleep(0.01)
        await port.wait()
        seen.append((port.values.instrument, _descriptions(chain)))

    port, heard, _, _ = _start(loop, base, chain, clock=lambda: moment[0], during=during)

    assert kinds == [Expiry.ARCHIVED], f"MXU6 в подставной бирже не архивный: {kinds}"
    assert seen[0][0] == "MXU6", "при запуске программа сама ушла с архивного кода"
    assert seen[1][1] > seen[0][1], "суточной проверки не было — вторая половина вакуумна"
    assert port.values.instrument == "MXU6", "на суточной проверке ушла с архивного кода"
    assert not [row for row in heard.notes if row.event == "Переход на действующий контракт"]
    assert heard.contracts and heard.contracts[-1].current == "MXZ6", heard.contracts
    assert heard.contracts[-1].mismatch, "плашка о расхождении в окно не ушла"
    assert not _minutes_asked(chain, "MXZ6"), "история MXZ6 грузилась без перехода"


def test_the_daily_clock_runs_even_if_the_first_code_had_no_chain(
    loop, base, monkeypatch
) -> None:
    """Запуск на коде без цепочки, потом смена на квартальный — часы уточняют его.

    Мутация, которую тест ловит: заводить часы (`daily`) после фильтра
    цепочки, как было, — к бирже не уйдёт ни одного запроса описания.
    """
    monkeypatch.setattr(port_module, "_SYNC_LOOK_MS", 10)
    chain = ExpiredChain(TODAY)
    asked: list[int] = []

    async def during(port: HistoryPort, heard: Heard) -> None:
        await port.wait()
        asked.append(_descriptions(chain))
        port.apply_settings(port.values.replace(instrument="MXU6"))
        for _ in range(300):
            if _descriptions(chain):
                break
            await asyncio.sleep(0.01)
        asked.append(_descriptions(chain))

    _start(loop, base, chain, instrument="MXX6", during=during)

    assert asked[0] == 0, "код без цепочки уточнялся у биржи — проверка вакуумна"
    assert asked[1] > 0, "после смены на квартальный код часы его не уточнили"


def test_a_monthly_asset_is_marked_and_not_retried_every_hour(
    loop, base, monkeypatch
) -> None:
    """Месячный актив при уточнении: пометка, одна правдивая строка, без повторов.

    Мутации, которые тест ловит: убрать ветку `ChainNotQuarterly` в `_sync`
    — строка «повторится через час» и повтор на суточной проверке; убрать
    из неё пометку `monthly` — повтор на суточной проверке.
    """
    from market import ChainNotQuarterly

    monkeypatch.setattr(port_module, "_SYNC_LOOK_MS", 10)
    calls: list[int] = []

    async def monthly(self, *args, **kwargs):
        calls.append(1)
        raise ChainNotQuarterly("у MX есть месячные контракты: MXX6")

    monkeypatch.setattr(MarketWorker, "refresh_contracts", monthly)
    chain = Chain(TODAY)
    moment = [datetime.combine(TODAY, time(12), MSK)]

    async def during(port: HistoryPort, heard: Heard) -> None:
        await port.wait()
        moment[0] += timedelta(days=1)
        for _ in range(30):  # тридцать взглядов часов по 10 мс
            await asyncio.sleep(0.01)
        await port.wait()

    port, heard, _, _ = _start(loop, base, chain, clock=lambda: moment[0], during=during)

    assert len(calls) == 1, f"месячный актив уточняли {len(calls)} раз"
    said = [row for row in heard.notes if row.event == "Контракты не уточняются у биржи"]
    assert len(said) == 1 and "вручную" in said[0].reason, said
    assert not [row for row in heard.notes if row.event == "Контракты не уточнены у биржи"], (
        "месячный актив назван сбоем связи с обещанием повтора"
    )
    assert heard.charts, "прогон после отказа не пошёл"


def test_an_exception_after_the_switch_is_said_as_it_is(loop, base, monkeypatch) -> None:
    """Исключение уже после смены инструмента: строка не врёт, история грузится.

    Мутация, которую тест ловит: писать «остался MXU6» и возвращать пусто
    при любом исключении — строка назовёт код, которого в настройках нет,
    а минуты MXZ6 не запросятся.
    """
    chain = ExpiredChain(TODAY)
    original = HistoryPort.apply_settings

    def late(self: HistoryPort, settings: Settings, *, by_person: bool = True) -> None:
        original(self, settings, by_person=by_person)
        if not by_person:
            raise RuntimeError("сбой после смены")

    monkeypatch.setattr(HistoryPort, "apply_settings", late)

    port, heard, _, _ = _start(loop, base, chain)

    assert port.values.instrument == "MXZ6"
    said = [row for row in heard.notes if "сбой после смены" in row.reason]
    assert said, "исключение после смены в журнал не сказано"
    assert "остался MXU6" not in said[-1].reason, said[-1].reason
    assert "MXZ6" in said[-1].reason, said[-1].reason
    assert _minutes_asked(chain, "MXZ6"), "история MXZ6 после смены не загружается"


class RunningPort(HistoryPort):
    """Порт, у которого робот запущен: снимок состояния без прогона и брокера."""

    def robot_running(self) -> None:
        self._last_state = RobotState(running=True, mode=Mode.REVERSE)


def test_the_instrument_refusal_names_its_field(loop, tmp_path) -> None:
    """Смена инструмента при запущенном роботе: отказ приходит с полем `instrument`.

    Без имени поля окно настроек показывает отказ только наверху, а не
    у поля «Инструмент». Мутация, которую тест ловит: убрать
    `field="instrument"` у отказа в `_check_switch`.
    """

    async def go() -> list[tuple]:
        worker = MarketWorker(tmp_path / "candles.sqlite3")
        port = RunningPort(worker, values=Settings(instrument="MXU6"), days=0,
                           sanitize=redact)
        refused: list[tuple] = []
        port.settings_refused.connect(lambda *payload: refused.append(payload))
        port.robot_running()
        try:
            port.apply_settings(port.values.replace(instrument="MXZ6"))
        finally:
            await port.aclose()
            await worker.close()
        return refused

    refused = loop.run_until_complete(go())

    assert refused, "смена инструмента под роботом не отвергнута — проверка вакуумна"
    kept, reason, field = refused[-1]
    assert "Инструмент не сменён" in reason, reason
    assert field == "instrument", f"отказ пришёл без поля «Инструмент»: {field!r}"
    assert kept.instrument == "MXU6"
