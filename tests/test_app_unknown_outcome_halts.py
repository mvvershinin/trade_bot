"""`D-086`: неизвестный исход команды брокеру обязан останавливать робота.

Что здесь стережётся
--------------------
`OutcomeUnknown` — единственный отказ, после которого программа **не может
доказать, что на счёте ничего не изменилось**. Робот, продолживший работу
в таком состоянии, считает себя вне рынка и входит ещё раз: на реверсной
системе это удвоенный объём и удвоенное обеспечение (`B-002`, `D-086`).

Отказ — потомок `BrokerError`, и до 06.09.2026 его ловили шесть широких
`except BrokerError`, каждый из которых превращал его в обычное «брокер
не ответил». Здесь проверяется, что этого больше не происходит, и что
остановка **доезжает до человека словами**, а не остаётся в логе.

⚠️ Две беды, и порознь ни одна проверка их не ловит
---------------------------------------------------
Мутация «остановка не наступает» и мутация «слова не доезжают» — разные
поломки с одинаково зелёным `pytest.raises`. Поэтому проверок на каждое
место по две, и они разведены:

* «остановка наступает» — получатель остановки позван, живой ход закрыт,
  окно показывает робота остановленным;
* «слова доезжают» — в причине стоит указание владельцу счёта **проверить
  заявки и позицию у брокера**, то есть ровно те слова, которыми говорит
  сам отказ (`broker/errors.py`, `OutcomeUnknown`).

Мутации, на которых обе обязаны падать, выписаны у каждого раздела.

⚠️ Изоляция
-----------
Каждая проверка строит своё: подставная сессия, подставной счёт, подставное
расписание, своя база во временном каталоге. Соседей ни одна не читает,
в сеть ни одна не ходит, токена — ни настоящего, ни выдуманного — здесь нет
нигде. Проверять поодиночке: `pytest tests/test_app_unknown_outcome_halts.py::имя`.
"""

from __future__ import annotations

import asyncio
import pathlib
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app import live_feed
from app.backfill import Backfiller, BrokerMinutes, Fetched, LoadReport
from app.halt import HALT_EVENT, halt_on_unknown_outcome, halt_reason
from broker.account import AccountSnapshot
from broker.candles import HistoricalCandle, HistoryIncomplete
from broker.clock import ClockWatch
from broker.errors import BrokerError, NoConnection, OutcomeUnknown, TokenFileError
from broker.session import READ_ENDPOINTS, REPEATABLE
from market import MSK, MarketWorker
from ui.models import Connection, DecisionLevel

#: Слова самого отказа, ради которых всё и затевалось: указание владельцу
#: счёта, что делать руками. Взяты **из текста отказа**, а не сочинены здесь:
#: разойдись они — разошлись бы молча.
CHECK_WITH_THE_BROKER = "Проверьте заявки и позицию у брокера"

#: Отказ, который в проверках изображает потерянный ответ. Технический текст
#: короткий и без единого признака секрета: фикстур с токеном, в том числе
#: выдуманным, в проекте не бывает (ТЗ §4.1).
def lost_answer() -> OutcomeUnknown:
    return OutcomeUnknown("/some/path: Timeout; исход операции неизвестен")


# --------------------------------------------------------------- перевод


def test_the_halt_reason_carries_the_words_of_the_refusal_itself() -> None:
    """Причина остановки — слова самого отказа, а не пересказ.

    ⚠️ Мутация **молчания**: подставить в `halt_reason` любой свой текст
    вместо `unknown.human`. Робот при этом останавливается по-прежнему,
    `pytest.raises` зеленеет, а владелец счёта читает строку, из которой
    не следует, что надо пойти и посмотреть заявки у брокера.
    """
    said = halt_reason(lost_answer())
    assert CHECK_WITH_THE_BROKER in said, (
        "в причине остановки нет главного указания владельцу счёта — "
        f"проверить заявки и позицию у брокера: {said!r}"
    )
    assert "Робот остановлен" in said, (
        f"причина не говорит, что робот встал: {said!r}"
    )


def test_the_translation_hands_the_halt_over_and_does_not_only_log_it() -> None:
    """Остановка отдаётся получателю, а не пишется в лог и забывается.

    ⚠️ Мутация **остановки**: убрать вызов `halt(...)` из
    `halt_on_unknown_outcome`, оставив `log.warning`. Технический лог
    по-прежнему полон, человеку не сказано ничего, робот работает.
    """
    handed: list[tuple[str, str]] = []
    halt_on_unknown_outcome(lost_answer(), lambda event, why: handed.append((event, why)))
    assert len(handed) == 1, f"остановку никому не отдали: {handed}"
    assert handed[0][0] == HALT_EVENT, f"событие названо иначе: {handed[0][0]!r}"
    assert CHECK_WITH_THE_BROKER in handed[0][1], (
        f"слова отказа до получателя не доехали: {handed[0][1]!r}"
    )


# ------------------------------------------------------- поток котировок


class _Heard:
    """Получатели потока — списками. Своё состояние, ничьё чужое."""

    def __init__(self) -> None:
        self.connections: list[Connection] = []
        self.said: list[tuple[str, str, DecisionLevel]] = []
        self.halts: list[tuple[str, str]] = []

    def watchers(self) -> live_feed.Watchers:
        return live_feed.Watchers(
            connection=self.connections.append,
            closed_minute=lambda symbol: None,
            growing_minute=lambda symbol, minute: None,
            say=lambda event, why, level: self.said.append((event, why, level)),
            connected=lambda minute: None,
            halt=lambda event, why: self.halts.append((event, why)),
        )


def _feed(
    heard: _Heard, tmp_path: pathlib.Path, kind: type[live_feed.LiveFeed] | None = None
) -> live_feed.LiveFeed:
    """Поток с подставной сессией: в сеть не ходит, базу не открывает."""
    session = cast("Any", SimpleNamespace(clock=ClockWatch()))
    return (kind or live_feed.LiveFeed)(
        session,
        MarketWorker(tmp_path / "unused.sqlite3"),
        ticker="MXU6",
        class_code="SPBFUT",
        watchers=heard.watchers(),
    )


class _StopTheRide(Exception):
    """Чем прерывается цикл переподключения: сам он не кончается никогда.

    Второй заход подписки роняется этим — `_ride` считает такое поломкой
    программы, снимает поток и возвращается. Отказы, которым повтор помогает
    (`NoConnection`), иначе крутили бы цикл до конца прогона.
    """


def _ride_once(
    heard: _Heard, tmp_path: pathlib.Path, failure: BrokerError, monkeypatch
) -> None:
    """Один заход `_ride`, у которого подписка падает названным отказом.

    Подписка подменена **наследником**, а не присваиванием в поле: предмет
    проверки — сам `_ride`, а вход у него подставной (правило 14 `CLAUDE.md`).
    """
    monkeypatch.setattr(live_feed, "RETRY_FIRST", 0.0)
    monkeypatch.setattr(live_feed, "RETRY_CAP", 0.0)
    attempts = {"count": 0}

    class _Falling(live_feed.LiveFeed):
        """Поток, у которого подписка падает названным отказом."""

        async def _listen(self) -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise failure
            raise _StopTheRide

    feed = _feed(heard, tmp_path, _Falling)

    async def go() -> None:
        await asyncio.wait_for(feed._ride(), timeout=5.0)  # noqa: SLF001 — предмет проверки

    asyncio.run(go())


def test_a_lost_answer_in_the_quote_stream_stops_the_robot(tmp_path, monkeypatch) -> None:
    """Поток получил «исход неизвестен» — робот встал.

    ⚠️ Мутация **остановки**: убрать ветку `except OutcomeUnknown` из
    `LiveFeed._ride`. Отказ тогда ловит соседний `except BrokerError`,
    поток так же снимается (`retryable` у него False), в журнал так же
    уходит строка — и всё это зелено, пока не спросить про остановку.
    """
    heard = _Heard()
    _ride_once(heard, tmp_path, lost_answer(), monkeypatch)
    assert heard.halts, (
        "поток получил отказ с неизвестным исходом и робота не остановил: "
        "он продолжит решать, считая себя вне рынка"
    )


def test_a_lost_answer_in_the_quote_stream_reaches_the_person(tmp_path, monkeypatch) -> None:
    """И говорит человеку теми же словами, что и сам отказ.

    ⚠️ Мутация **молчания**: оставить в `_ride` остановку, но не отдавать
    ей текст отказа (позвать `self._watch.halt(HALT_EVENT, "")`). Робот
    встанет, окно покажет пустую причину, и разбирать будет нечего.
    """
    heard = _Heard()
    _ride_once(heard, tmp_path, lost_answer(), monkeypatch)
    assert heard.halts, "остановки нет — проверять слова не на чем"
    assert CHECK_WITH_THE_BROKER in heard.halts[0][1], (
        f"владельцу счёта не сказано, что проверить у брокера: {heard.halts[0]!r}"
    )


def test_an_ordinary_broken_link_does_not_stop_the_robot(tmp_path, monkeypatch) -> None:
    """Канарейка: обычный обрыв связи робота не останавливает.

    Без неё все проверки выше прошли бы и у потока, который останавливает
    робота на **любом** отказе, — а обрывы у владельца счёта частые, и такой
    робот вставал бы по десять раз на дню с ручным возобновлением.
    """
    heard = _Heard()
    _ride_once(heard, tmp_path, NoConnection("тест"), monkeypatch)
    assert not heard.halts, (
        f"обычный обрыв связи остановил робота: {heard.halts}"
    )


# ---------------------------------------------------------- расписание торгов


class _Hours:
    """Расписание, которое «не знает» и называет причиной названный отказ."""

    def __init__(self, trouble: BrokerError | None) -> None:
        self._trouble = trouble

    async def status(self, *, class_code: str) -> None:
        return None

    async def daily(self, *, class_code: str, ticker: str) -> None:
        return None

    def status_trouble(self, *, class_code: str) -> BrokerError | None:
        return self._trouble


def _doubt_about(trouble: BrokerError | None, tmp_path: pathlib.Path) -> _Heard:
    heard = _Heard()
    feed = _feed(heard, tmp_path)
    feed.follow(_Hours(trouble))
    feed._doubt(None)  # noqa: SLF001 — предмет проверки
    return heard


def test_a_lost_answer_from_the_schedule_is_not_swallowed_as_do_not_know(
    tmp_path,
) -> None:
    """Расписание глотает отказы само — но не этот.

    Слой `broker/` останавливать робота не вправе: он не знает про движок
    и знать не должен. Поэтому перехват стоит у вызывающего, в `_doubt`.

    ⚠️ Мутация **остановки**: убрать `isinstance(failure, OutcomeUnknown)`
    из `LiveFeed._doubt`. Строка «расписание получить не удалось» останется
    на месте (`D-062`), поток так же подключится, и всё будет выглядеть
    рабочим — а команда с неизвестным исходом пропадёт.
    """
    heard = _doubt_about(lost_answer(), tmp_path)
    assert heard.halts, (
        "неизвестный исход утонул в «расписание получить не удалось»"
    )
    assert CHECK_WITH_THE_BROKER in heard.halts[0][1], (
        f"слова отказа до человека не доехали: {heard.halts[0]!r}"
    )


def test_an_unreachable_schedule_alone_does_not_stop_the_robot(tmp_path) -> None:
    """Канарейка: «не знаем расписание» — это не остановка робота.

    Пропущенный торговый день дороже сотни лишних попыток (`B-022`),
    и робот, встающий на каждом отказе расписания, отменяет это решение.
    """
    heard = _doubt_about(NoConnection("тест"), tmp_path)
    assert not heard.halts, f"обычный отказ расписания остановил робота: {heard.halts}"


# ------------------------------------------------------------- опрос счёта


class _Account:
    """Счёт, который на вопрос отвечает названным отказом."""

    def __init__(self, failure: BrokerError) -> None:
        self._failure = failure

    async def snapshot(self) -> AccountSnapshot:
        raise self._failure


def _poll_once(failure: BrokerError) -> tuple[list[tuple[str, str]], list[Any]]:
    halts: list[tuple[str, str]] = []
    said: list[Any] = []
    watch = live_feed.FundsWatch(
        cast("Any", _Account(failure)),  # подставной счёт, предмет проверки — сам опрос
        ticker="MXU6",
        watchers=live_feed.FundsWatchers(
            funds=lambda money: None,
            say=lambda event, why, level: said.append((event, why, level)),
            halt=lambda event, why: halts.append((event, why)),
            # Подставной порт: остановлен ровно тогда, когда `halt` уже
            # звали, — так же, как настоящий (`HistoryPort.halted`).
            halted=lambda: bool(halts),
        ),
        every=timedelta(seconds=1),
    )
    asyncio.run(watch._once())  # noqa: SLF001 — предмет проверки
    return halts, said


def test_a_lost_answer_from_the_account_poll_stops_the_robot() -> None:
    """Опрос счёта получил «исход неизвестен» — робот встал.

    ⚠️ Мутация **остановки**: убрать ветку `except OutcomeUnknown`
    из `FundsWatch._once`. Отказ уйдёт в соседний `except BrokerError`,
    строка «состояние счёта не получено» появится, опрос через минуту
    спросит снова — и всё это молча про то, что на счёте могло измениться.
    """
    halts, _ = _poll_once(lost_answer())
    assert halts, "опрос счёта проглотил неизвестный исход как обычный отказ"


def test_a_lost_answer_from_the_account_poll_reaches_the_person() -> None:
    """⚠️ Мутация **молчания**: остановить робота, не передав слова отказа."""
    halts, _ = _poll_once(lost_answer())
    assert halts, "остановки нет — проверять слова не на чем"
    assert CHECK_WITH_THE_BROKER in halts[0][1], (
        f"владельцу счёта не сказано, что проверить у брокера: {halts[0]!r}"
    )


def test_an_ordinary_account_refusal_does_not_stop_the_robot() -> None:
    """Канарейка: «портфель не ответил» — не повод останавливать робота."""
    halts, said = _poll_once(NoConnection("тест"))
    assert not halts, f"обычный отказ счёта остановил робота: {halts}"
    assert said, "и при этом человеку не сказано вообще ничего"


#: Успокоительная половина жалобы про счёт: она верна ровно до тех пор,
#: пока робот работает. Берётся из продуктовой константы, а не сочиняется
#: здесь: разойдись они — проверка молча перестала бы ловить.
POSITION_IS_MANAGED = "остаётся под управлением робота"
#: Правда для остановленного робота: закрывать позицию некому, кроме
#: владельца счёта.
CLOSE_IT_BY_HAND = "закройте её сами в терминале брокера"


def test_a_halted_robot_is_not_told_that_it_still_manages_the_position() -> None:
    """Остановленному роботу не приписывается управление позицией.

    Тот самый разбор в рублях (`D-086`, находка Н-2 аудита `/risk`
    06.09.2026). Опрос счёта получил «исход команды неизвестен», робот
    встал — и **следующей же строкой** журнала программа сообщала: «Открытая
    позиция при этом остаётся под управлением робота — выход, тейк и конец
    окна работают как обычно». Владелец счёта читает последнюю строку,
    решает, что тейк сработает, и не идёт закрывать позицию руками. Тейк
    не сработает: движка больше нет.

    ⚠️ Мутация **лжи**, а не молчания: заставить `_blind` взять
    успокоительный хвост при остановленном роботе — подменить
    `POSITION_NOT_MANAGED` на `POSITION_STILL_MANAGED` в `app/live_feed.py`
    или перевернуть условие. Строка в журнале при этом останется на месте
    и будет выглядеть работой, поэтому проверка «строка есть» здесь
    не годится вовсе: она зеленеет на неверной строке.

    ⚠️ Вторая мутация, тише первой: переставить в ветке `except
    OutcomeUnknown` вызовы `halt_on_unknown_outcome` и `_blind`. Робот
    на момент жалобы ещё не остановлен, и текст снова становится
    успокоительным.
    """
    halts, said = _poll_once(lost_answer())
    assert halts, "робот не остановлен — проверять слова про позицию не на чем"
    lines = [why for _, why, _ in said]
    assert lines, "про состояние счёта человеку не сказано ничего"
    calming = [line for line in lines if POSITION_IS_MANAGED in line]
    assert not calming, (
        "остановленному роботу приписано управление открытой позицией: "
        f"{calming[0]!r}. Владелец счёта прочтёт это и не пойдёт закрывать "
        "позицию руками, а больше её закрыть некому"
    )
    assert any(CLOSE_IT_BY_HAND in line for line in lines), (
        "человеку не сказано, что позицию придётся закрывать самому: "
        f"{lines!r}"
    )


def test_the_account_poll_is_wired_to_ask_the_port_about_the_halt(
    tmp_path,
) -> None:
    """Проводка: опрос счёта спрашивает про остановку **настоящий** порт.

    Проверка отдельная от двух выше, и без неё они обе вакуумны наполовину.
    Они гоняют `FundsWatch` с подставными получателями и стерегут его
    поведение; кто и чем заполняет поле `halted` в бою — они не видят вовсе.

    ⚠️ Замер 06.09.2026, полный прогон в отдельном дереве: мутация
    `halted=lambda: False` в проводке `LiveLink` не уронила **ни одной**
    из 3565 проверок. То есть строка, от которой зависит, прочтёт ли
    владелец счёта «позиция под управлением» или «закройте её руками»,
    не была закреплена ничем.

    Предмет здесь — `LiveLink._funds_watchers`: ни сессии брокера, ни файла
    токена, ни сети. Порт подставной и ведёт себя как настоящий: сказали
    `halt` — `halted` перестал быть пустым.
    """
    port = _Reporter()
    link = live_feed.LiveLink(
        cast("Any", port),  # подставной порт: протокол проводке хватает
        MarketWorker(tmp_path / "unused.sqlite3"),
        userdata=tmp_path,
        ticker="MXU6",
        class_code="SPBFUT",
    )
    watchers = link._funds_watchers(lambda money: None)  # noqa: SLF001 — предмет проверки
    assert not watchers.halted(), (
        "опрос счёта считает робота остановленным до всякой остановки: "
        "жалоба про счёт станет гнать человека в терминал брокера без повода"
    )
    port.halt(HALT_EVENT, "Дневной лимит убытка исчерпан.")
    assert watchers.halted(), (
        "опрос счёта не видит остановки робота: про открытую позицию он "
        "скажет «остаётся под управлением робота» ровно тогда, когда "
        "управления нет"
    )


def test_a_working_robot_is_still_told_that_it_manages_the_position() -> None:
    """Канарейка к проверке выше: успокоительные слова не выброшены совсем.

    Без неё проверка Н-2 прошла бы и у программы, которая пугает владельца
    счёта «закройте позицию руками» на каждом отказе портфеля. Это ровно
    та же неправда, только в другую сторону: работающий робот из позиции
    выходит сам, и гнать человека в терминал брокера незачем.
    """
    halts, said = _poll_once(NoConnection("тест"))
    assert not halts, f"обычный отказ счёта остановил робота: {halts}"
    lines = [why for _, why, _ in said]
    assert any(POSITION_IS_MANAGED in line for line in lines), (
        f"работающему роботу отказано в управлении позицией: {lines!r}"
    )
    assert not any(CLOSE_IT_BY_HAND in line for line in lines), (
        f"человека гонят закрывать позицию руками у живого робота: {lines!r}"
    )


# --------------------------------------------------------------- догрузка


class _Quotes:
    """Свечи брокера, отвечающие названным отказом."""

    def __init__(self, failure: BaseException) -> None:
        self._failure = failure

    async def candles(self, **kwargs: object) -> tuple[HistoricalCandle, ...]:
        raise self._failure


def _ask_for_minutes(failure: BaseException) -> Fetched:
    source = BrokerMinutes(
        _Quotes(failure),  # подставной источник: протокол `Quotes` он выполняет
        ticker="MXU6",
        class_code="SPBFUT",
    )
    since = datetime(2026, 6, 19, 10, 0, tzinfo=MSK)
    return asyncio.run(source.minutes(since, since + timedelta(minutes=10)))


def test_the_backfill_does_not_turn_a_lost_answer_into_a_refusal() -> None:
    """Догрузка отдаёт неизвестный исход наверх, а не в отчёт о загрузке.

    ⚠️ Мутация **остановки**: убрать `except OutcomeUnknown: raise`
    из `BrokerMinutes.minutes`. Отказ станет `Fetched(refusal=…)`,
    догрузка спросит биржу, пропуск закроется свечами с ISS — и всё будет
    выглядеть удачным заходом.
    """
    with pytest.raises(OutcomeUnknown):
        _ask_for_minutes(lost_answer())


def test_a_lost_answer_wrapped_into_a_partial_load_is_unwrapped() -> None:
    """Завёрнутый в «загружено наполовину» — тоже не отчёт, а остановка.

    Слой брокера заворачивает отказ, случившийся посреди периода, вместе
    с уже полученной частью. Так завёрнутый `OutcomeUnknown` выглядит
    обычной неполной догрузкой.

    ⚠️ Мутация **остановки**: убрать разворачивание `incomplete.cause`
    из `BrokerMinutes.minutes`. Ответом станет `Fetched(partial=…)`, и
    неизвестный исход исчезнет в строке «догружено не всё, повторим позже».
    """
    with pytest.raises(OutcomeUnknown):
        _ask_for_minutes(HistoryIncomplete((), cause=lost_answer()))


class _Reporter:
    """Порт глазами догрузки: четыре вызова, все — списками."""

    def __init__(self) -> None:
        self.said: list[tuple[str, str, DecisionLevel]] = []
        self.halts: list[tuple[str, str]] = []

    def note(
        self, event: str, reason: str, level: DecisionLevel = DecisionLevel.INFO
    ) -> None:
        self.said.append((event, reason, level))

    def history_confirmed(self, symbol: str, until: datetime) -> bool:
        return False

    def refresh(self, why: str = "") -> None:
        return None

    def halt(self, event: str, reason: str) -> None:
        self.halts.append((event, reason))

    @property
    def halted(self) -> str:
        """Причина остановки — как у настоящего порта: что положил `halt`.

        Строка, а не флаг, и это не мелочь: настоящий `HistoryPort.halted`
        отдаёт причину, и проводка приводит её к `bool` сама. Отдай фальшивка
        флаг — проверка проводки зеленела бы на порте, который отдаёт что
        угодно.
        """
        return self.halts[-1][1] if self.halts else ""


def _guard_a_backfill(failure: BaseException, tmp_path: pathlib.Path) -> _Reporter:
    reporter = _Reporter()
    class _Falling(Backfiller):
        """Догрузка, у которой заход падает названным отказом.

        Наследник, а не присваивание в поле: предмет проверки — `_guarded`,
        и он остаётся настоящим (правило 14 `CLAUDE.md`).
        """

        async def run(self, boundary: datetime) -> LoadReport:
            raise failure

    filler = _Falling(
        _Quotes(failure),  # подставной источник
        MarketWorker(tmp_path / "unused.sqlite3"),
        reporter,  # подставной порт: протокол `Reporter` он выполняет целиком
        ticker="MXU6",
        class_code="SPBFUT",
    )
    asyncio.run(filler._guarded(datetime(2026, 6, 19, 10, 0, tzinfo=MSK)))  # noqa: SLF001 — предмет проверки
    return reporter


def test_a_lost_answer_in_the_backfill_stops_the_robot(tmp_path) -> None:
    """⚠️ Мутация **остановки**: убрать `except OutcomeUnknown` из `_guarded`.

    Отказ поймает соседний `except Exception`, в журнал уйдёт «догрузка
    не удалась, повторим при следующем подключении», и робот продолжит.
    """
    reporter = _guard_a_backfill(lost_answer(), tmp_path)
    assert reporter.halts, "догрузка проглотила неизвестный исход как свою неудачу"


def test_a_lost_answer_in_the_backfill_reaches_the_person(tmp_path) -> None:
    """⚠️ Мутация **молчания**: остановить, не передав слова отказа."""
    reporter = _guard_a_backfill(lost_answer(), tmp_path)
    assert reporter.halts, "остановки нет — проверять слова не на чем"
    assert CHECK_WITH_THE_BROKER in reporter.halts[0][1], (
        f"владельцу счёта не сказано, что проверить у брокера: {reporter.halts[0]!r}"
    )


def test_an_ordinary_backfill_failure_does_not_stop_the_robot(tmp_path) -> None:
    """Канарейка: неудавшаяся догрузка — это пропуск в данных, а не остановка."""
    reporter = _guard_a_backfill(RuntimeError("база не открылась"), tmp_path)
    assert not reporter.halts, f"обычная неудача догрузки остановила робота: {reporter.halts}"
    assert reporter.said, "и при этом человеку не сказано вообще ничего"


# ------------------------------------------------ файл токена: сети тут нет


def test_a_token_file_failure_is_not_an_unknown_outcome(tmp_path, monkeypatch) -> None:
    """`LiveLink.switch` ловит отказы сборки, и остановки среди них нет.

    Разница названа словами в самом коде: `_build` в сеть не ходит вовсе —
    он собирает объекты и читает файл токена с диска. Ни один отказ отсюда
    не оставляет у брокера команду с неизвестным исходом, и остановка была
    бы ложной тревогой ценой ручного возобновления.

    Проверка стережёт **отсутствие** ветки: появится здесь запрос к брокеру
    — эта проверка не упадёт, но объяснение в коде станет ложью, и следующий
    читатель обязан её пересмотреть вместе с ним.
    """
    port = _Reporter()
    link = live_feed.LiveLink(
        cast("Any", port),  # подставной порт
        MarketWorker(tmp_path / "unused.sqlite3"),
        userdata=tmp_path,
        ticker="MXU6",
        class_code="SPBFUT",
    )

    def refuse(directory: pathlib.Path) -> object:
        raise TokenFileError("Файл настроек с токеном не найден.", "тест")

    monkeypatch.setattr(live_feed, "TokenStore", refuse)
    said = link.switch(True)
    assert said, "отказ сборки не вернулся в окно ответом на нажатие"
    assert not port.halts, (
        f"отказ файла токена остановил робота: {port.halts}. Сети в этом "
        "месте нет, и останавливать нечего"
    )


# ------------------------------------------------------------- храповик


def test_no_reading_endpoint_of_the_broker_can_raise_an_unknown_outcome() -> None:
    """Ни одно разрешённое сегодня обращение не поднимает «исход неизвестен».

    Зачем этот храповик именно здесь. `_with_retries` поднимает
    `OutcomeUnknown` **всякий раз**, когда обращения нет в `REPEATABLE`,
    — и это верное умолчание: направление ошибки безопасное. Но цена
    расхождения таблиц изменилась. До `D-086` строка, забытая в `REPEATABLE`,
    стоила лишнего отказа на обрыве связи, и только (так и написано
    у самой таблицы). Теперь она стоит **остановки робота с ручным
    возобновлением** — на обычном чтении свечей, где менять нечего.

    Поэтому расхождение перестало быть безобидным и обязано быть замечено
    человеком. Список исключений пуст: сегодня программа не подаёт ни одной
    команды, меняющей состояние счёта. Тому, кто заведёт подачу заявки
    на Э2/Э4: адрес заявки войдёт в допуск и **не войдёт** в `REPEATABLE`,
    и вот тогда его имя встанет сюда — вручную, одной строкой, вместе
    с объяснением. Молча этого не произойдёт.
    """
    #: Обращения, которым «исход неизвестен» положен по существу: они меняют
    #: состояние счёта. Сегодня таких нет — заявок программа не подаёт.
    changes_the_account: set[tuple[str, str]] = set()
    silent = set(READ_ENDPOINTS) - set(REPEATABLE) - changes_the_account
    assert not silent, (
        f"обращение {silent} разрешено к чтению и при этом не повторяется. "
        "Потеря ответа на нём поднимет `OutcomeUnknown`, а он теперь "
        "останавливает робота вручную возобновляемой остановкой — на чтении, "
        "где на счёте не меняется ничего. Либо внесите обращение "
        "в `REPEATABLE`, либо назовите его здесь как меняющее счёт"
    )
