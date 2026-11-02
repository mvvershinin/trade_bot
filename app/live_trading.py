"""Боевая сторона пары движков: исполнитель «факта», собранный после сверки.

Э4, шаг 2, под-шаг 2в (`.docs/plans/E4-live-orders.md` §7, §12; решение 0066).
Порт и живой ход брокера не знают (`app/fact.py`, шапка): исполнитель приходит
к ним параметром, и собирается он **здесь** — только по явной команде
«Включить бой» (`HistoryPort.set_live`), а не при запуске и не при связи.

Что делает сборка, по порядку
-----------------------------
1. **Права** — токен перечитывается из файла (`D-175`): на токене для чтения
   бой недоступен, причина словами (`ReadOnlyToken.human`).
2. **Счёт** — номер договора из портфеля: под ним пишется каждая команда
   журнала и считается день (в `userdata/` бывает два токена на два счёта).
3. **Позиция у брокера** — свежим запросом, **до** разбора журнала; не ноль —
   бой не включается, и ни одной команды брокеру: живая заявка прошлого
   запуска при позиции — это её тейк, и снять его значило бы оставить
   позицию голой (`/risk` 2в, БЛОК 1). Позицию, открытую не этим включением,
   «факт» вести не может: его движок рождается без позиции. Закрывать её
   программа здесь **не** берётся: включение боя само к сделке не ведёт.
   Совпавший ноль становится основой позиции по сделкам (К2 `/risk`).
4. **Журнал** — поток статусов открыт; незакрытые команды прошлых
   запусков разобраны по ключу у брокера (0017: живые сняты, исполненные
   и снятые закрыты в журнале), каждое снятие — фразой. Судьба хоть одной
   не выяснена, команда осталась незакрытой или позиция после разбора не
   ноль — **бой не включается** (0066).
5. **День** — из таблицы исполнений счёта (Р7): перезапуск в день с тейком
   не снимает «стоп после тейка». Не прочитался — бой не включается.

Сверка после обрыва потока статусов — `LiveDesk.relinked`: тем же
`app/reconcile.py`, позиция робота против брокера; расхождение — «закрыть
всё и остановиться» (0020). Зовёт её порт внутри прохода, до свечей, —
то есть до первой заявки после связи.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from app.fact import FactRefused, FactRequest, day_record, position_words
from app.live_executor import (
    EventsOpener,
    Instrument,
    LiveExecutor,
    StoreBook,
    Wiring,
)
from app.reconcile import reconcile
from broker.account import parse_positions
from broker.errors import BrokerError
from broker.session import PORTFOLIO_PATH, BrokerSession
from broker.tokens import now_utc
from engine import DayRecord, in_moscow, is_take_order
from market import Candle as MarketCandle
from market import MarketWorker
from market.commands import StoredCommand

__all__ = ["LiveDesk", "LiveDesks", "Seat"]

#: Хвост каждого отказа: что стало с боем.
_NOT_ON = "Бой не включён: команд брокеру не было, позиция у брокера не тронута."


def _tail(notes: tuple[str, ...]) -> str:
    """Хвост отказа: «команд не было» — только если их правда не было."""
    if not notes:
        return _NOT_ON
    return (
        "Бой не включён. Позиция у брокера не тронута; что сверка сделала с заявками "
        "прошлых запусков — строками «Сверка с брокером»."
    )


def _command_words(row: StoredCommand) -> str:
    """Незакрытая команда журнала словами: что, ключ, в каком состоянии."""
    command = row.command
    return (
        f"{command.order_id} ({command.action.value}, ключ {command.client_order_id}, "
        f"{row.status.value})"
    )


@dataclass(frozen=True, slots=True)
class Seat:
    """Чей счёт и что на нём: сессия брокера, инструмент, номер договора."""

    session: BrokerSession
    ticker: str
    account: str


class LiveDesk:
    """Собранная боевая сторона (`app.fact.FactDesk`). Живёт вместе с ходом пары."""

    def __init__(
        self,
        executor: LiveExecutor,
        *,
        book: StoreBook,
        seat: Seat,
        day: DayRecord | None,
        notes: tuple[str, ...],
    ) -> None:
        self._executor = executor
        self._book = book
        self._seat = seat
        self._account = seat.account
        self._day = day
        self._notes = notes
        #: Открытий потока, уже сверенных. Рост — связь вернулась (`relinked`).
        self._links = executor.links

    @property
    def executor(self) -> LiveExecutor:
        """Исполнитель «факта»: порту он виден как `engine.OrderExecutor`."""
        return self._executor

    @property
    def day(self) -> DayRecord | None:
        """День счёта до запуска (Р7), прочитанный при сборке."""
        return self._day

    @property
    def notes(self) -> tuple[str, ...]:
        """Что сверка при сборке сделала с командами прошлых запусков."""
        return self._notes

    @property
    def halted(self) -> str | None:
        """Почему исполнитель стоит — его словами; `None` — не стоит."""
        return self._executor.halted

    @property
    def undelivered(self) -> tuple[str, ...]:
        """Имена заявок, чьи сделки у брокера есть, а движку ещё не отданы."""
        return self._executor.undelivered

    async def watch(self) -> None:
        """Записана минутка: сторожа трейлинга (`LiveExecutor.watch`)."""
        await self._executor.watch()

    async def relinked(self, expected: int) -> str | None:
        """Поток статусов переоткрылся с прошлой сверки — сверить до заявок.

        Пока поток закрыт, сверять нечем: заявки и так не подаются
        (`ExecutionRefused`), сверка будет на первом проходе после связи.
        """
        links = self._executor.links
        if links <= self._links or not self._executor.connected:
            return None
        result = await reconcile(self._executor, expected)
        if result.halted is None:
            # Сверено — только теперь. Сверка, не дошедшая до конца, повторится
            # на следующем проходе (замечание `/risk` 2в).
            self._links = links
        return result.halted

    async def unclosed(self) -> tuple[str, ...]:
        """Незакрытые команды журнала этого счёта и неотданные сделки — словами (С4)."""
        rows = await self._book.open_commands()
        open_ = tuple(
            _command_words(row) for row in rows if row.command.account == self._account
        )
        given = tuple(
            f"{name} (у брокера, движку ещё не отдана)" for name in self._executor.undelivered
        )
        return open_ + given

    async def aclose(self) -> None:
        """Закрыть поток статусов и дописать журнал. Позиция у брокера остаётся."""
        await self._executor.close()

    async def holding(self) -> tuple[int, float | None]:
        """Позиция у брокера свежим чтением портфеля: со знаком и средняя цена.

        Через сессию, а не исполнитель: зовётся и после `aclose`, когда
        боевая сторона уже закрыта, — окну нужна позиция, которая осталась
        (`/risk` 2в N2). Отказ — `BrokerError`.
        """
        positions, _ = parse_positions(await self._seat.session.read(PORTFOLIO_PATH))
        mine = [one for one in positions if one.ticker == self._seat.ticker]
        signed = sum(one.quantity for one in mine)
        prices = [one.average_price for one in mine if one.average_price is not None]
        return int(signed), (prices[0] if prices else None)

    async def resting_levels(self) -> tuple[float, ...]:
        """Цены лимитных заявок этого счёта, незакрытых в журнале: что живо у брокера.

        Неподвижный тейк — лимитная у брокера; снятый (сверка, «закрыть всё»)
        или исполненный в журнале закрыт, и уровня у позиции больше нет (N3).
        """
        rows = await self._book.open_commands()
        return tuple(
            row.command.price for row in rows
            if row.command.account == self._account and row.command.price is not None
        )


@dataclass(frozen=True, slots=True)
class LiveDesks:
    """Чем порт собирает боевую сторону (`app.fact.FactDesks`). Ставит `app/main.py`.

    :param session: сессия брокера живого потока — **та же**, а не своя:
        вторая сессия меняла бы токен отдельно, и обмен одной сделал бы
        негодным токен другой. `None` — поток ещё не включали.
    :param events: как открыть поток статусов; `None` — поток брокера.
    :param clock: «сейчас» исполнителя — UTC; день считается по МСК.
    """

    session: Callable[[], BrokerSession | None]
    worker: MarketWorker
    class_code: str
    events: EventsOpener | None = None
    clock: Callable[[], datetime] = now_utc
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    def refusal(self) -> str | None:
        """Бой недоступен без вопроса брокеру: нет связи или токен для чтения."""
        session = self.session()
        if session is None:
            return (
                "Подключения к брокеру нет: бой идёт поверх живого потока котировок, "
                "сначала нажмите «Подключиться»."
            )
        try:
            session.require_trade_rights(reread=True)
        except BrokerError as error:
            return error.human
        return None

    async def open(self, request: FactRequest) -> LiveDesk:
        """Собрать исполнитель «факта» после сверки. Отказ — `FactRefused` словами."""
        refusal = self.refusal()
        if refusal is not None:
            raise FactRefused(f"{refusal} {_NOT_ON}")
        session = self.session()
        assert session is not None, "сессию только что проверил `refusal`"
        account = await self._account(session)
        book = StoreBook(self.worker, request.session_id)
        worker, ticker = self.worker, request.ticker

        async def minutes(since: datetime) -> Sequence[MarketCandle]:
            return await worker.minutes(ticker, since=since)

        wiring = Wiring(
            session, book, minutes, events=self.events, clock=self.clock,
            sleep=self.sleep, on_news=request.on_news, notice=request.notice,
        )
        instrument = Instrument(ticker, self.class_code, request.bar_minutes, account)
        executor = LiveExecutor(wiring, instrument, request.limits)
        await executor.start()
        try:
            notes = await self._prepare(executor, book, account, request)
            day = await self._day(account, request, notes)
        except BaseException:
            await executor.close()
            raise
        seat = Seat(session, request.ticker, account)
        return LiveDesk(executor, book=book, seat=seat, day=day, notes=notes)

    async def _account(self, session: BrokerSession) -> str:
        """Номер договора из портфеля. Не назван — бой не включается."""
        try:
            _, account = parse_positions(await session.read(PORTFOLIO_PATH))
        except BrokerError as error:
            raise FactRefused(f"Портфель у брокера не прочитан: {error.human} {_NOT_ON}") from None
        if not account:
            raise FactRefused(
                "Брокер не назвал номер счёта в портфеле: без него журнал команд не знает, "
                f"чей это день. {_NOT_ON}"
            )
        return account

    async def _prepare(
        self, executor: LiveExecutor, book: StoreBook, account: str, request: FactRequest
    ) -> tuple[str, ...]:
        """Поток открыт, позиция у брокера — ноль, журнал разобран. Иначе отказ.

        ⚠️ Позиция читается **первой**, до разбора журнала (`/risk` 2в, БЛОК 1).
        Разбор снимает живые заявки прошлых запусков (0017) — а при позиции
        у брокера живая заявка прошлого запуска это её тейк. Снять его и тут же
        отказать «позиция не ноль» значило бы оставить позицию без тейка
        и без робота. Поэтому позиция не ноль — отказ до любой команды
        брокеру, и живые заявки не трогаются.
        """
        if not await executor.wait_connected(request.limits.event_wait):
            raise FactRefused(
                f"Поток статусов заявок не открылся за {request.limits.event_wait:g} с: "
                f"без него программа не узнает о сделке. {_NOT_ON}"
            )
        before = await self._position(executor, (), None)
        if before != 0:
            raise FactRefused(self._not_flat(before, request, ()), position=before)
        notes = await executor.settle_open_commands()
        tail = _tail(notes)
        if executor.unsettled:
            raise FactRefused(
                "Судьбу команды из журнала прошлого запуска выяснить у брокера не удалось — "
                f"состояние счёта неизвестно. {tail}",
                notes=notes, position=await self._fresh(executor, notes, before),
            )
        left = [row for row in await book.open_commands() if row.command.account == account]
        if left:
            raise FactRefused(
                f"В журнале команд незакрытых: {len(left)} — "
                f"{'; '.join(_command_words(row) for row in left)}. Бой включается только "
                f"без незакрытых команд. {tail}",
                notes=notes, position=await self._fresh(executor, notes, before),
            )
        if executor.halted is not None:
            raise FactRefused(
                f"{executor.halted} {tail}",
                notes=notes, position=await self._fresh(executor, notes, before),
            )
        actual = await self._position(executor, notes, None if notes else before)
        if actual != 0:
            raise FactRefused(self._not_flat(actual, request, notes), notes=notes, position=actual)
        return notes

    @staticmethod
    async def _fresh(executor: LiveExecutor, notes: tuple[str, ...], before: int) -> int | None:
        """Позиция для отказа после разбора журнала — свежая (B-081, `/risk` 2в З5).

        Разбор ничего не делал — первое чтение ещё свежее. Делал (снял, нашёл
        исполненной) — читается заново; не прочиталась — `None`, а не ноль.
        """
        if not notes:
            return before
        try:
            return await executor.broker_position()
        except BrokerError:
            return None

    @staticmethod
    async def _position(
        executor: LiveExecutor, notes: tuple[str, ...], known: int | None
    ) -> int:
        """Позиция у брокера свежим запросом; совпавший ноль — основа книги позиции (К2).

        Не прочиталась — отказ несёт `known`: последнее свежее чтение, если оно
        есть. У первого чтения его нет (`None`); у второго — ноль первого, только
        если разбор журнала ничего не делал: иначе (снята или нашлась исполненной
        заявка прошлого запуска) он уже не свеж (B-081).
        """
        try:
            return await executor.check_position(0)
        except BrokerError as error:
            raise FactRefused(
                f"Позицию у брокера прочитать не удалось: {error.human} {_tail(notes)}",
                notes=notes, position=known,
            ) from None

    @staticmethod
    def _not_flat(actual: int, request: FactRequest, notes: tuple[str, ...]) -> str:
        return (
            f"У брокера {position_words(actual)} по {request.ticker}. Бой включается только "
            "при нулевой позиции у брокера: позицию, открытую не этим включением, робот "
            "вести не может, а её заявки у брокера (тейк) программа не трогает. Закройте "
            f"позицию в приложении брокера и включите бой снова. {_tail(notes)}"
        )

    async def _day(
        self, account: str, request: FactRequest, notes: tuple[str, ...]
    ) -> DayRecord:
        """День счёта по исполнениям (Р7) — то, что движок держал до перезапуска."""
        today = in_moscow(self.clock()).date()
        try:
            trading = await self.worker.executions_day(
                account, today,
                ruble_per_point={request.ticker: request.ruble_per_point},
                is_take=is_take_order,
            )
        except (sqlite3.Error, ValueError, LookupError, RuntimeError, OSError) as error:
            raise FactRefused(
                f"День счёта из базы не прочитан ({type(error).__name__}): без него "
                "«стоп после тейка» после перезапуска не сработал бы. " + _tail(notes),
                # Позиция у брокера только что прочитана нулём (`_prepare`): окно
                # не должно держать запомненную позицию прошлого боя (B-081).
                notes=notes, position=0,
            ) from None
        return day_record(trading)
