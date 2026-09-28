"""Ф5: робот работает только живым контрактом из настроек и встаёт перед экспирацией.

Что здесь стережётся — поведения словами
----------------------------------------
1. **Собранный ряд (`@MX`), истёкший и архивный контракт брокеру не отдаются.**
   Поток котировок по ним не включается, при смене инструмента на такой код
   уже работающий поток снимается, — и всё это вслух, строкой в журнале.
   Без потока нет живого хода, без живого хода нет ни одной заявки.
2. **Остановка перед экспирацией** (ТЗ §4.6, решение 0016) — по
   `last_trade_day` строки таблицы `contract` **для кода из настроек**,
   за `expiry_halt_days` дней. Граница включительно: осталось ровно N — стоп,
   N+1 — работа. Остановка снимается только рукой; снятая на прежнем коде —
   встаёт снова, снятая после перехода — нет.
3. **Смена инструмента при запущенном роботе или позиции на счёте —
   отказ порта**, а не только погашенная кнопка плашки: поле «Инструмент»
   в окне настроек ведёт в ту же дверь.
4. Число дней — настройка: проверка пределов и строка в журнале изменений.
5. Срок кода неизвестен — предупреждение, а не тишина (правило 13).

Сеть не участвует: подключение к брокеру — заглушка `attach_stream`, таблица
контрактов пишется в свою базу во временном каталоге, «сейчас» называет
проверка (`clock` порта). Каждую проверку можно гонять поодиночке.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta
from typing import TypeVar

import pytest

os.environ.setdefault("QT_API", "pyside6")  # до первого импорта qasync

from app.port import HistoryPort
from market import (
    MSK,
    Candle,
    CandleStore,
    ContractRow,
    Expiry,
    MarketWorker,
    Source,
    Timeframe,
    expiry_verdict,
)
from market.journal import HaltKind, redact
from ui.models import DecisionLevel, HistoryFacts, Mode, RobotState
from ui.models import Settings as UiSettings

_T = TypeVar("_T")

#: Последний день обращения подставного контракта.
LAST = date(2026, 12, 17)
ROWS = [
    ContractRow("MXU6", last_trade_day=date(2026, 9, 17),
                active_from=date(2026, 6, 18), active_to=date(2026, 9, 16)),
    ContractRow("MXZ6", last_trade_day=LAST, active_from=date(2026, 9, 17)),
]


def at(day: date) -> datetime:
    """Полдень МСК названного дня."""
    return datetime(day.year, day.month, day.day, 12, 0, tzinfo=MSK)


# ------------------------------------------------------------ чистое правило


@pytest.mark.parametrize(
    ("today", "days", "kind"),
    [
        (LAST - timedelta(days=2), 1, Expiry.OK),      # N+1 — работа
        (LAST - timedelta(days=1), 1, Expiry.NEAR),    # ровно N — стоп
        (LAST, 1, Expiry.NEAR),
        (LAST - timedelta(days=1), 0, Expiry.OK),      # ноль — только сам день
        (LAST, 0, Expiry.NEAR),
        (LAST + timedelta(days=1), 0, Expiry.EXPIRED),
    ],
)
def test_the_halt_border_counts_days_to_the_last_trading_day(today, days, kind) -> None:
    """Граница остановки: осталось N дней или меньше — стоп, N+1 — работа."""
    verdict = expiry_verdict(ROWS, "MXZ6", today=today, halt_days=days)
    assert verdict.kind is kind, (
        f"{today}, за {days} дн. до {LAST}: ждали {kind}, получили {verdict.kind}"
    )


def test_the_verdict_reads_the_configured_code_not_the_current_one() -> None:
    """Срок берётся у кода из настроек: MXU6 истёк, хотя MXZ6 живёт."""
    verdict = expiry_verdict(ROWS, "MXU6", today=date(2026, 9, 27), halt_days=1)
    assert verdict.kind is Expiry.EXPIRED and verdict.kind.refused
    assert "17.09.2026" in verdict.text, f"дата не названа: {verdict.text!r}"


def test_an_archived_contract_is_refused_before_it_expires() -> None:
    """Период закрыт, торги ещё идут: робот им всё равно не работает (0061)."""
    verdict = expiry_verdict(ROWS, "MXU6", today=date(2026, 9, 17) - timedelta(days=0),
                             halt_days=0)
    # 17.09 — последний день MXU6 и уже период MXZ6.
    assert verdict.kind is Expiry.ARCHIVED and verdict.kind.refused, verdict


def test_the_last_front_day_of_a_contract_is_not_archived_yet() -> None:
    """16.09 — последний день, когда MXU6 ближний: им ещё работают, отказа нет.

    Граница строгая: архивный — с дня **после** конца периода. Отказ
    на день раньше снял бы поток в последний законный день контракта.
    """
    verdict = expiry_verdict(ROWS, "MXU6", today=date(2026, 9, 16), halt_days=0)
    assert verdict.kind is Expiry.OK, verdict


def test_a_stitched_series_is_refused_without_any_table() -> None:
    """`@MX` отвергается даже тогда, когда таблицу ещё не читали."""
    verdict = expiry_verdict(None, "@MX", today=date(2026, 9, 27), halt_days=1)
    assert verdict.kind is Expiry.SYNTHETIC and verdict.kind.refused
    assert "@MX" in verdict.text


def test_an_unknown_term_is_not_a_refusal_but_is_said() -> None:
    """Кода нет в таблице: не отказ, но фраза есть — остановка не сработает."""
    verdict = expiry_verdict(ROWS, "SiZ6", today=date(2026, 9, 27), halt_days=1)
    assert verdict.kind is Expiry.UNKNOWN and not verdict.kind.refused
    assert "не сработает" in verdict.text


# ------------------------------------------------------------------- порт


class _Link:
    """Подставное подключение к брокеру: что просили у сборки."""

    def __init__(self) -> None:
        self.switched: list[bool] = []
        self.targets: list[str] = []

    def switch(self, on: bool) -> str | None:
        self.switched.append(on)
        return None

    def retarget(self, ticker: str) -> None:
        self.targets.append(ticker)


@pytest.fixture
def base(tmp_path: pathlib.Path) -> pathlib.Path:
    """Своя база с таблицей контрактов. Свечей не нужно."""
    path = tmp_path / "expiry.sqlite3"
    with CandleStore(path) as store:
        store.put_contracts(ROWS, now=at(date(2026, 9, 20)))
    return path


async def _settle(port: HistoryPort) -> None:
    """Дождаться прогона и чтения таблицы контрактов (`HistoryPort.wait` ждёт оба)."""
    await port.wait()


def _run(
    loop,
    database: pathlib.Path,
    today: date,
    scenario: Callable[[HistoryPort, _Link], Awaitable[_T]],
    *,
    read_table: bool = True,
    **values,
) -> _T:
    async def go() -> _T:
        worker = MarketWorker(database, sanitize=redact)
        await worker.open()
        port = HistoryPort(
            worker,
            values=UiSettings(**values),
            days=0,
            sanitize=redact,
            clock=lambda: at(today),
        )
        link = _Link()
        port.attach_stream(link.switch, retarget=link.retarget)
        try:
            if read_table:
                port.check_contract()
                await _settle(port)
            return await scenario(port, link)
        finally:
            await port.aclose()
            await worker.close()

    result: _T = loop.run_until_complete(go())
    return result


def _journal(port: HistoryPort) -> list[tuple[str, str, DecisionLevel]]:
    return [(row.event, row.reason, row.level) for row in port._notes]  # noqa: SLF001 — строки журнала уходят сигналом


def test_the_stream_refuses_a_stitched_series_out_loud(loop, base) -> None:
    """`@MX` в поле «Инструмент»: подключение не просится вовсе, отказ в журнале."""

    async def scenario(port: HistoryPort, link: _Link) -> list:
        port.stream(True)
        await _settle(port)
        return link.switched

    switched = _run(loop, base, date(2026, 9, 27), scenario, instrument="@MX")
    assert switched == [], f"подключение по склейке всё-таки попрошено: {switched}"


def test_the_stream_refusal_reaches_the_journal(loop, base) -> None:
    """Отказ по истёкшему коду — строкой в журнале, с кодом и датой."""

    async def scenario(port: HistoryPort, link: _Link):
        port.stream(True)
        await _settle(port)
        return link.switched, _journal(port)

    switched, journal = _run(loop, base, date(2026, 9, 27), scenario, instrument="MXU6")
    assert switched == [], f"подключение по истёкшему MXU6 попрошено: {switched}"
    said = [row for row in journal if row[0] == "Подключение к брокеру"]
    assert said and "MXU6" in said[0][1] and "17.09.2026" in said[0][1], (
        f"отказ по истёкшему коду не сказан: {journal}"
    )


def test_a_live_robot_halts_before_expiration(loop, base) -> None:
    """За N дней до последнего дня — остановка вида «предохранитель робота»."""

    async def scenario(port: HistoryPort, link: _Link):
        port.stream(True)
        await _settle(port)
        return link.switched, port._halt.reason, port._halt_causes(), _journal(port)  # noqa: SLF001

    switched, reason, causes, journal = _run(
        loop, base, LAST - timedelta(days=1), scenario,
        instrument="MXZ6", expiry_halt_days=1,
    )
    assert switched == [True], "поток по живому MXZ6 не включён"
    assert "Экспирация MXZ6" in reason, f"робот не остановлен перед экспирацией: {reason!r}"
    assert [cause.kind for cause in causes] == [HaltKind.ENGINE.label], causes
    assert any(row[0] == "Остановка перед экспирацией" for row in journal), (
        f"остановка не записана в журнал решений: {journal}"
    )


def test_a_live_robot_keeps_working_one_day_earlier(loop, base) -> None:
    """За N+1 дней — робот не стоит: граница не съезжает на день раньше."""

    async def scenario(port: HistoryPort, link: _Link):
        port.stream(True)
        await _settle(port)
        return port._halt.reason  # noqa: SLF001

    reason = _run(loop, base, LAST - timedelta(days=2), scenario,
                  instrument="MXZ6", expiry_halt_days=1)
    assert not reason, f"робот остановлен за день до срока настройки: {reason!r}"


def test_the_halt_is_lifted_only_after_the_switch(loop, base) -> None:
    """Снятая на прежнем коде остановка встаёт снова; после перехода — нет.

    Робот на MXZ6 встал; человек снимает остановку, не переходя, — робот
    встаёт опять. Переходит на MXH7 (срок далеко) и снимает — работает.
    """
    rows = [*ROWS, ContractRow("MXH7", last_trade_day=date(2027, 3, 18))]

    async def scenario(port: HistoryPort, link: _Link):
        await port._worker.call(lambda store: store.put_contracts(rows))  # noqa: SLF001
        port.check_contract()
        await _settle(port)
        port.stream(True)
        await _settle(port)
        first = port._halt.reason  # noqa: SLF001
        port.resume()
        await _settle(port)
        again = port._halt.reason  # noqa: SLF001
        port.apply_settings(UiSettings(instrument="MXH7", expiry_halt_days=1))
        await _settle(port)
        port.resume()
        await _settle(port)
        return first, again, port._halt.reason, link.targets  # noqa: SLF001

    first, again, after, targets = _run(
        loop, base, LAST, scenario, instrument="MXZ6", expiry_halt_days=1
    )
    assert first, "робот не остановлен в последний день"
    assert again, "остановка снята на прежнем коде и не вернулась"
    assert not after, f"после перехода на MXH7 робот всё ещё стоит: {after!r}"
    assert targets[-1] == "MXH7", f"поток не переведён на новый контракт: {targets}"


def test_switching_to_a_stitched_series_drops_the_live_stream(loop, base) -> None:
    """Живой поток, в поле поставили `@MX`: подписка снята, склейке не отдана."""

    async def scenario(port: HistoryPort, link: _Link):
        port.stream(True)
        await _settle(port)
        port.apply_settings(UiSettings(instrument="@MX"))
        await _settle(port)
        return link.switched, link.targets, _journal(port)

    switched, targets, journal = _run(
        loop, base, date(2026, 9, 27), scenario, instrument="MXZ6"
    )
    assert "@MX" not in targets, f"склейка отдана подписке: {targets}"
    assert switched[-1] is False, f"поток по прежнему коду не снят: {switched}"
    assert any(row[0] == "Поток котировок не переключён" and "@MX" in row[1]
               for row in journal), f"отказ не сказан: {journal}"


def test_a_live_stream_is_dropped_when_its_code_becomes_archived(loop, base) -> None:
    """Поток идёт по MXZ6; таблица уточнилась — MXZ6 стал архивным: поток снят вслух.

    Отказ на входе (`stream(True)`) этого не ловит: код был живым, когда
    поток включали. Архивным его делает уточнение таблицы посреди живого
    хода — и робот обязан перестать смотреть на код вне его периода,
    а не торговать им до перезапуска.
    """
    today = date(2026, 12, 1)
    rolled = [
        ContractRow("MXZ6", last_trade_day=LAST, active_from=date(2026, 9, 17),
                    active_to=date(2026, 11, 30)),
        ContractRow("MXH7", last_trade_day=date(2027, 3, 18),
                    active_from=date(2026, 12, 1)),
    ]

    async def scenario(port: HistoryPort, link: _Link):
        port.stream(True)
        await _settle(port)
        before = list(link.switched)
        await port._worker.call(lambda store: store.put_contracts(rolled))  # noqa: SLF001
        port.check_contract()
        await _settle(port)
        return before, link.switched, _journal(port)

    before, switched, journal = _run(
        loop, base, today, scenario, instrument="MXZ6", expiry_halt_days=0
    )
    assert before == [True], f"поток по живому MXZ6 не включён — проверять нечего: {before}"
    assert switched[-1] is False, f"поток по архивному MXZ6 не снят: {switched}"
    assert any(row[0] == "Поток котировок остановлен" and "MXZ6" in row[1]
               for row in journal), f"снятие потока не сказано: {journal}"


def test_the_instrument_does_not_change_under_a_running_robot(loop, base) -> None:
    """Робот запущен — смена инструмента отвергается портом, настройки прежние."""

    async def scenario(port: HistoryPort, link: _Link):
        port._last_state = RobotState(running=True, mode=Mode.REVERSE)  # noqa: SLF001
        port.apply_settings(UiSettings(instrument="MXH7"))
        await _settle(port)
        return port._values.instrument, link.targets, _journal(port)  # noqa: SLF001

    kept, targets, journal = _run(loop, base, date(2026, 9, 27), scenario, instrument="MXZ6")
    assert kept == "MXZ6", f"инструмент сменён под работающим роботом: {kept}"
    assert "MXH7" not in targets, f"поток переведён: {targets}"
    assert any(row[0] == "Настройки не приняты" and "Робот запущен" in row[1]
               for row in journal), f"отказ не сказан: {journal}"


def test_the_instrument_does_not_change_over_an_account_position(loop, base) -> None:
    """Позиция на счёте (не прогона) — смена инструмента отвергается."""
    from ui.models import Position, Side

    async def scenario(port: HistoryPort, link: _Link):
        port._last_state = RobotState(  # noqa: SLF001
            simulation=False,
            position=Position(side=Side.LONG, volume=1, entry_price=100.0),
        )
        port.apply_settings(UiSettings(instrument="MXH7"))
        await _settle(port)
        return port._values.instrument  # noqa: SLF001

    assert _run(loop, base, date(2026, 9, 27), scenario, instrument="MXZ6") == "MXZ6"


def test_other_settings_still_apply_under_a_running_robot(loop, base) -> None:
    """Блок — на смене инструмента, а не на любой правке."""

    async def scenario(port: HistoryPort, link: _Link):
        port._last_state = RobotState(running=True, mode=Mode.REVERSE)  # noqa: SLF001
        port.apply_settings(UiSettings(instrument="MXZ6", expiry_halt_days=3))
        await _settle(port)
        return port._values.expiry_halt_days  # noqa: SLF001

    assert _run(loop, base, date(2026, 9, 27), scenario, instrument="MXZ6") == 3


def test_the_expiry_days_are_checked_and_logged(loop, base) -> None:
    """Число вне пределов отвергается; смена числа — строкой в журнале изменений."""

    async def scenario(port: HistoryPort, link: _Link):
        port.apply_settings(UiSettings(instrument="MXZ6", expiry_halt_days=99))
        await _settle(port)
        refused = port._values.expiry_halt_days  # noqa: SLF001
        port.apply_settings(UiSettings(instrument="MXZ6", expiry_halt_days=3))
        await _settle(port)
        return refused, _journal(port)

    refused, journal = _run(loop, base, date(2026, 9, 27), scenario, instrument="MXZ6")
    assert refused == 1, f"99 дней принято: {refused}"
    assert any(row[0] == "Настройки не приняты" and "экспирацией" in row[1]
               for row in journal), f"отказ по числу не сказан: {journal}"
    changed = [row for row in journal if row[0] == "Настройки изменены"]
    assert changed and "Остановка перед экспирацией: за 1 дн. → за 3 дн." in changed[-1][1], (
        f"смена числа не записана: {changed}"
    )


def test_an_unknown_term_is_said_when_the_robot_goes_live(loop, base) -> None:
    """Кода нет в таблице — живой ход идёт, но предупреждение сказано."""

    async def scenario(port: HistoryPort, link: _Link):
        port.stream(True)
        await _settle(port)
        return link.switched, _journal(port)

    switched, journal = _run(loop, base, date(2026, 9, 27), scenario, instrument="SiZ6")
    assert switched == [True]
    assert any(row[0] == "Срок контракта не известен" and row[2] is DecisionLevel.WARNING
               for row in journal), f"о неизвестном сроке промолчали: {journal}"


def test_a_stream_asked_before_the_table_waits_for_it(loop, base) -> None:
    """`--stream` при сборке: подписка ждёт таблицу и на истёкший код не открывается.

    Гонка запуска: таблица читается задачей, а связь просят сразу. Прежде
    подписка открывалась на код с неизвестным сроком и снималась потом.
    """

    async def scenario(port: HistoryPort, link: _Link):
        port.stream(True)  # таблица ещё не читалась
        asked_early = list(link.switched)
        await _settle(port)
        return asked_early, link.switched, _journal(port)

    early, switched, journal = _run(loop, base, date(2026, 9, 27), scenario,
                                    read_table=False, instrument="MXU6")
    assert early == [] and switched == [], (
        f"подписка на истёкший MXU6 открыта до чтения таблицы: {early} / {switched}"
    )
    assert any(row[0] == "Подключение к брокеру" and "MXU6" in row[1]
               for row in journal), f"отказ не сказан: {journal}"


def test_a_stream_asked_before_the_table_opens_for_a_live_code(loop, base) -> None:
    """Живой код: подписка открывается после чтения таблицы, без ложного «не известен»."""

    async def scenario(port: HistoryPort, link: _Link):
        port.stream(True)
        await _settle(port)
        return link.switched, _journal(port)

    switched, journal = _run(loop, base, date(2026, 9, 27), scenario,
                             read_table=False, instrument="MXZ6")
    assert switched == [True], f"подписка на живой MXZ6 не открыта: {switched}"
    assert not any(row[0] == "Срок контракта не известен" for row in journal), (
        f"ложное «срок не известен» при известном сроке: {journal}"
    )


def test_the_term_is_checked_again_when_the_day_changes(loop, base, monkeypatch) -> None:
    """Программа живёт через ночь: наутро дня остановки робот встаёт сам.

    И **до** того, как движок увидит первый бар нового дня.
    """
    from app.observe import LiveObserver

    minutes = [
        Candle(
            time=at(LAST - timedelta(days=3)) + timedelta(minutes=index),
            open=100000.0, high=100010.0, low=99990.0, close=100000.0 + index,
            volume=1.0, timeframe=Timeframe(1), filled_minutes=1,
        )
        for index in range(120)
    ]
    with CandleStore(base) as store:
        store.put_minutes("MXZ6", minutes, Source.ISS)
    fed: list[str] = []
    original = LiveObserver.feed

    async def spy(self, candles):
        fed.append(stage[0])
        return await original(self, candles)

    stage = ["before"]
    monkeypatch.setattr(LiveObserver, "feed", spy)

    async def scenario(port: HistoryPort, link: _Link):
        port.stream(True)
        await _settle(port)
        before = port._halt.reason  # noqa: SLF001
        stage[0] = "halt day"
        port._clock = lambda: at(LAST - timedelta(days=1))  # noqa: SLF001 — наступили сутки остановки
        port.refresh("новый бар")
        await _settle(port)
        return before, port._halt.reason  # noqa: SLF001

    before, after = _run(loop, base, LAST - timedelta(days=2), scenario,
                         instrument="MXZ6", expiry_halt_days=1)
    assert not before, f"робот встал раньше срока: {before!r}"
    assert "before" in fed, "живой ход не шёл до смены суток — проверять нечего"
    assert "Экспирация MXZ6" in after, "смена суток не вызвала сверку срока"
    assert "halt day" not in fed, "бар дня остановки прошёл через движок до сверки срока"


def test_an_expired_code_at_midnight_feeds_no_bar(loop, base, monkeypatch) -> None:
    """D-126: наутро после последнего дня код истёк — движок не видит ни одного бара.

    Мутация: убрать выход после `_reread_term` в `_advance` — тот же проход
    подаёт бар нового дня живому ходу на отвергнутом коде.
    """
    from app.observe import LiveObserver

    minutes = [
        Candle(
            time=at(LAST - timedelta(days=2)) + timedelta(minutes=index),
            open=100000.0, high=100010.0, low=99990.0, close=100000.0 + index,
            volume=1.0, timeframe=Timeframe(1), filled_minutes=1,
        )
        for index in range(120)
    ]
    with CandleStore(base) as store:
        store.put_minutes("MXZ6", minutes, Source.ISS)
    fed: list[str] = []
    original = LiveObserver.feed
    stage = ["before"]

    async def spy(self, candles):
        fed.append(stage[0])
        return await original(self, candles)

    monkeypatch.setattr(LiveObserver, "feed", spy)

    async def scenario(port: HistoryPort, link: _Link):
        port.stream(True)
        await _settle(port)
        stage[0] = "expired day"
        port._clock = lambda: at(LAST + timedelta(days=1))  # noqa: SLF001 — наступили сутки после экспирации
        port.refresh("новый бар")
        await _settle(port)
        return port._watch.observer, _journal(port)  # noqa: SLF001 — ход наружу не отдаётся

    observer, journal = _run(loop, base, LAST - timedelta(days=5), scenario,
                             instrument="MXZ6", expiry_halt_days=0)
    assert "before" in fed, "живой ход не шёл до смены суток — проверять нечего"
    assert "expired day" not in fed, "бар после экспирации прошёл через движок"
    assert observer is None, "живой ход на истёкшем коде не закрыт"
    assert any("истёк" in reason for _, reason, _ in journal), (
        f"отказ по сроку не сказан: {journal}"
    )


def test_a_monthly_asset_is_remembered_after_a_restart(loop, base) -> None:
    """D-125: BR назван месячным в прошлом сеансе — новая программа это знает.

    В базе лежит след отказа биржи (месячный код BRX6); порт собран заново,
    загрузки в этом сеансе не было. Мутация: убрать проверку
    `monthly_assets` из `_history_facts` — опись снова обещает загрузку
    по контракту и называет оба исхода.
    """
    with CandleStore(base) as store:
        store.put_contracts([ContractRow("BRX6", last_trade_day=date(2026, 10, 30))],
                            now=at(date(2026, 9, 20)))

    async def scenario(port: HistoryPort, link: _Link):
        seen = _facts(port, "BRZ6")
        await _done(port._history.facts)  # noqa: SLF001 — задача описи наружу не отдаётся
        return seen

    seen = _run(loop, base, date(2026, 9, 27), scenario, instrument="MXZ6")
    assert seen and not seen[-1].by_contract and not seen[-1].quarterly_unchecked, seen


def test_a_halted_robot_may_switch_the_instrument(loop, base) -> None:
    """Робот стоит (остановка), хоть и числится запущенным, — переход разрешён.

    Иначе путь решения 0016 «встал перед экспирацией → сменил код → снял
    остановку» упирался бы в тупик: смена запрещена до снятия, снятие
    на прежнем коде возвращает остановку.
    """

    async def scenario(port: HistoryPort, link: _Link):
        port._last_state = RobotState(  # noqa: SLF001
            running=True, mode=Mode.REVERSE, halted="Экспирация MXZ6"
        )
        port.apply_settings(UiSettings(instrument="MXH7"))
        await _settle(port)
        return port._values.instrument  # noqa: SLF001

    assert _run(loop, base, date(2026, 9, 27), scenario, instrument="MXZ6") == "MXH7"


# ------------------------------------------------ живой ход и рубеж контракта

ROLL = date(2026, 9, 17)  # рубеж MXZ6: 16.09 — день прогрева, качается целиком


def _session_minutes(day: date, base_price: float) -> list[Candle]:
    """Торговый день минуток 10:00–18:00 с волной, дающей сделки."""
    import math

    start = datetime(day.year, day.month, day.day, 10, 0, tzinfo=MSK)
    out = []
    for index in range(480):
        price = base_price * (1 + 0.012 * math.sin(index / 7.0))
        out.append(Candle(
            time=start + timedelta(minutes=index), open=price, high=price * 1.0008,
            low=price * 0.9992, close=price, volume=1.0,
            timeframe=Timeframe(1), filled_minutes=1,
        ))
    return out


@pytest.fixture
def rolled(tmp_path: pathlib.Path) -> pathlib.Path:
    """MXU6 до рубежа, MXZ6 — целый день прогрева 16.09 и три дня после."""
    path = tmp_path / "rolled.sqlite3"
    moment = at(date(2026, 9, 21))
    with CandleStore(path) as store:
        u6 = [date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16)]
        z6 = [date(2026, 9, 16), ROLL, date(2026, 9, 18)]
        for symbol, days, price in (("MXU6", u6, 2800.0), ("MXZ6", z6, 2850.0)):
            for day in days:
                store.put_minutes(symbol, _session_minutes(day, price), Source.ISS)
            store.mark_days_requested(symbol, days, counts={d: 480 for d in days}, now=moment)
        store.put_contracts(ROWS, now=moment)
    return path


def _deals(loop, database: pathlib.Path, *, live: bool) -> list[tuple[datetime, datetime]]:
    async def scenario(port: HistoryPort, link: _Link):
        if live:
            port.stream(True)
        port.refresh("проверка")
        await _settle(port)
        run = port._run  # noqa: SLF001 — показанный прогон наружу уходит сигналом
        assert run is not None
        return [(deal.entry_time, deal.exit_time) for deal in run.deals]

    # Окно на весь день: иначе день прогрева 16.09 (с 10:00) почти целиком
    # уходит в отброс движка, и сделок до рубежа не дала бы и поломка обрезки.
    return _run(loop, database, date(2026, 9, 21), scenario, instrument="MXZ6",
                window_start=time(9, 0), window_end=time(23, 0))


def test_the_live_ride_trades_nothing_on_the_warmup_day(loop, rolled) -> None:
    """С потоком: ни одной сделки MXZ6 до его рубежа — день прогрева в сделки не идёт."""
    deals = _deals(loop, rolled, live=True)
    assert deals, "живой ход не дал ни одной сделки — сравнивать нечего"
    early = [deal for deal in deals if in_msk_date(deal[0]) < ROLL]
    assert not early, f"сделки живого хода до рубежа MXZ6: {early[:3]}"


def test_the_stream_does_not_change_the_contract_trades(loop, rolled) -> None:
    """С потоком и без — сделки периода MXZ6 одни и те же."""
    live = _deals(loop, rolled, live=True)
    shown = [deal for deal in _deals(loop, rolled, live=False) if in_msk_date(deal[0]) >= ROLL]
    assert live == shown, (
        f"включение потока поменяло сделки MXZ6: с потоком {len(live)}, без — {len(shown)}"
    )


def in_msk_date(moment: datetime) -> date:
    return moment.astimezone(MSK).date()


# ------------------------------------------------ опись для диалога загрузки


def _facts(port: HistoryPort, symbol: str) -> list[HistoryFacts]:
    seen: list[HistoryFacts] = []
    port.history_facts_ready.connect(seen.append)
    port.request_history_facts(symbol)
    return seen


async def _done(task: asyncio.Task[None] | None) -> None:
    """Дождаться задачи порта, если она заведена."""
    if task is not None:
        await task


def test_the_load_dialog_tells_the_truth_for_a_monthly_asset(loop, base, monkeypatch) -> None:
    """BR: биржа назвала месячные контракты — опись больше не обещает загрузку по контракту.

    До первой попытки квартальность не проверена, и диалог говорит оба
    исхода (`quarterly_unchecked`); после отказа биржи — только «по дням».
    """
    from market import ChainNotQuarterly
    from ui.models import HistoryLoadRequest

    async def scenario(port: HistoryPort, link: _Link):
        before = _facts(port, "BRZ6")
        await _settle(port)
        await _done(port._history.facts)  # noqa: SLF001 — задача описи наружу не отдаётся
        first = list(before)

        async def monthly(*args, **kwargs):
            raise ChainNotQuarterly("у BR есть месячные контракты: BRX6")

        async def by_days(request):
            return None

        monkeypatch.setattr(port._worker, "refresh_contracts", monthly)  # noqa: SLF001 — биржа заглушкой
        monkeypatch.setattr(port, "_load_history", by_days)  # загрузка по дням заглушкой
        port.load_history(HistoryLoadRequest(symbol="BRZ6"))
        await _done(port._history.load)  # noqa: SLF001
        after = _facts(port, "BRZ6")
        await _done(port._history.facts)  # noqa: SLF001
        return first, after

    before, after = _run(loop, base, date(2026, 9, 27), scenario, instrument="MXZ6")
    assert before and before[-1].by_contract and before[-1].quarterly_unchecked, (
        f"до проверки опись не сказала, что квартальность не подтверждена: {before}"
    )
    assert after and not after[-1].by_contract, (
        f"после отказа биржи опись BR всё ещё обещает загрузку по контракту: {after}"
    )


def test_a_known_quarterly_asset_is_not_called_unchecked(loop, base) -> None:
    """MX в таблице есть — квартальность подтверждена, оговорки про BR нет."""

    async def scenario(port: HistoryPort, link: _Link):
        seen = _facts(port, "MXZ6")
        await _done(port._history.facts)  # noqa: SLF001
        return seen

    seen = _run(loop, base, date(2026, 9, 27), scenario, instrument="MXZ6")
    assert seen and seen[-1].by_contract and not seen[-1].quarterly_unchecked, seen
