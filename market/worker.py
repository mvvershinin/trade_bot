"""Поток данных: единственный вход в базу и в загрузку с биржи.

Зачем этот модуль существует
----------------------------
Слой `market/` синхронный целиком: `IssClient` спит через `time.sleep`,
запись пачки минуток в SQLite — обычный блокирующий ввод-вывод. Окно, движок
и брокер живут в одном цикле событий ([решение 0005](../.docs/decisions/0005-concurrency-model.md)),
и прямой вызов догрузки из корутины останавливает **всё** на время загрузки.
Замерено: `time.sleep(0.3)` в корутине оставляет от 15 тиков таймера один.

Поэтому синхронность не убирается, а **изолируется**: один выделенный поток,
внутри которого живёт соединение с базой, снаружи — ожидаемые (`await`) методы.

Почему поток, а не `check_same_thread=False`
--------------------------------------------
Флаг отвергнут замером. `CandleStore._transaction` открывает транзакцию голым
`BEGIN`; она принадлежит **соединению**, а не вызову. Два потока на одном
соединении дают

    OperationalError: cannot start a transaction within a transaction

а флаг снимает только проверку принадлежности потоку — гонка остаётся, и
понятный отказ превращается в редкий. Здесь соединение создаётся **внутри**
потока данных и наружу не отдаётся вовсе: нарушить владение можно, только
специально вытащив объект через `call()` и сохранив его на потом.

Открытие — по требованию, а не по приказу
-----------------------------------------
`open()` остаётся: он открывает базу **при старте программы** и тем самым
проверяет её — испорченный файл обязан отказать один раз и вслух, а не
всплывать посреди работы. Но обязанностью вызывающего открытие быть
перестало: любой метод, которому нужна база, откроет её сам.

Причина названа `B-029` и она денежная. Файла базы на чистой машине нет,
`app/main.py` поэтому поток не открывал, а единственный способ базу завести —
кнопка «Загрузить историю» — упирался в отказ «сначала откройте поток».
Замкнутый круг: программа у владельца счёта не работала вовсе, и база
приезжала к нему по `scp` руками.

Что обязан знать вызывающий
---------------------------
* **Обратные вызовы приходят в потоке данных.** `progress` при догрузке
  выполняется там же, где идёт загрузка. Доставка в окно — только через
  `Qt.ConnectionType.QueuedConnection`; трогать виджеты из этого потока нельзя.
* **Отмена `await` не отменяет работу.** Питон не умеет прерывать чужой поток:
  отменённый вызов перестаёт ждать результат, а загрузка или запись доводятся
  до конца. Это правильно для записи — оборванная на середине пачка была бы
  хуже, — и это надо помнить при остановке программы.

  Отсюда же требование к жизненному циклу: **всё, что создаётся в потоке,
  в потоке и закрывается.** Отменённый `open()` иначе оставлял бы созданное
  соединение бесхозным — работа-то доводится до конца, — и следующий `open()`
  открыл бы **второе** соединение к тому же файлу. Поэтому `self._store`
  присваивается изнутри потока, а `close()` читает его там же: очередь одна,
  порядок сохраняется, гонки нет.
* **Порядок сохраняется.** Поток один, очередь общая: вызовы исполняются
  в том порядке, в каком их отправили. Поэтому `close()` закрывает базу
  после всей ранее отправленной работы, а не поперёк неё.
* **Остановка не ждёт конца догрузки.** Порядок очереди означал бы, что
  `close()` во время `sync()` стоит за ней: год минуток — это минуты
  ожидания при закрытом окне. Поэтому `close()` сначала просит все идущие
  загрузки остановиться (`IssClient.stop`, проверка между страницами),
  и только потом встаёт в очередь. Оборванная догрузка пишет отчёт
  в журнал и продолжится при следующем запуске.

Модуль импортирует только стандартную библиотеку и собственный слой —
правило `market/ → (ничего)` из ARCHITECTURE.md §2 не нарушается.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import pathlib
from collections.abc import Callable, Iterable
from datetime import date, datetime
from types import TracebackType
from typing import TypeVar

from market.candles import Candle, Timeframe
from market.gaps import Gap
from market.history import HistoryOutcome, HistoryRequest, load_history
from market.inventory import Inventory, take_inventory
from market.iss import FetchResult, InstrumentSpec, IssClient, Market
from market.journal import (
    BACKTEST_SESSIONS_KEPT,
    DecisionRecord,
    HaltRecord,
    JournalPage,
    JournalSession,
    JournalStats,
    PruneReport,
    RunOrigin,
    SessionRecord,
    StoredDecision,
    StoredHalt,
    StoredTrade,
    TradeRecord,
)
from market.point import MARKETS_PROBED, PointValue, ask_point_value
from market.reports import LoadReport
from market.storage import CandleStore, Coverage, Source, WriteStats
from market.sync import CHUNK_DAYS, GAP_THRESHOLD_MINUTES, sync_minutes
from market.synthetic import refuse_synthetic_for_trading

__all__ = ["MarketWorker", "WorkerClosed"]

_T = TypeVar("_T")

#: Текст для разработчика, а не для владельца счёта. Наружу он не выходит:
#: перевод на человеческий язык делает `app/port.py` — там же, где стоит
#: единственный выход порта в окно.
_CLOSED = (
    "поток данных остановлен, база закрыта. Возобновлять его нельзя: "
    "заведите новый MarketWorker"
)


class WorkerClosed(RuntimeError):
    """Поток данных уже остановлен: программа закрывается.

    Отдельный класс, а не голый `RuntimeError`, ради одной вещи: `app/`
    обязан отличить это состояние от любой другой беды базы и сказать про
    него **человеческую** фразу. Различать по тексту исключения нельзя —
    текст здесь написан разработчику (`_CLOSED`), и он менялся дважды.
    """


class MarketWorker:
    """Однопоточный фасад над `CandleStore` и загрузкой с биржи.

    Владеет исполнителем ровно на один поток. `CandleStore` создаётся внутри
    этого потока при `open()` и закрывается там же при `close()`.

    Использование::

        worker = MarketWorker(path)
        await worker.open()
        try:
            bars = await worker.bars("MXU6", M5)
        finally:
            await worker.close()

    или короче — `async with MarketWorker(path) as worker:`.
    """

    def __init__(
        self,
        path: pathlib.Path | str,
        *,
        thread_name: str = "market",
        sanitize: Callable[[str], str] | None = None,
        iss: IssClient | None = None,
    ) -> None:
        self.path = pathlib.Path(path)
        #: Чистка текста, уходящего в журналы. Передаётся в `CandleStore`
        #: как есть; смысл и требование к вызывающему — там же.
        self._sanitize = sanitize
        #: Чем ходить на биржу, когда вызывающий не назвал клиента сам.
        #: `None` — собрать `IssClient()` с транспортом на httpx.
        #:
        #: Поле фасада, а не параметр каждого вызова: клиент биржи — это
        #: **сеть**, и место, где её подменяют на подставную, должно быть
        #: одно на весь поток данных. Проверка задаёт его здесь и дальше
        #: про биржу не вспоминает; `client=` у отдельного вызова остаётся
        #: и побеждает — им пользуется загрузка истории, которой нужен
        #: свой клиент с отчётом о ходе работы.
        self._iss = iss
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=thread_name
        )
        self._store: CandleStore | None = None
        self._closed = False
        #: Загрузки, идущие прямо сейчас. Трогается только из цикла событий.
        self._loading: set[IssClient] = set()

    # -- жизненный цикл ----------------------------------------------------

    async def open(self) -> "MarketWorker":
        """Открыть базу **внутри** потока данных — заранее и явно.

        Обязательным этот вызов не является (см. шапку модуля): база
        откроется и сама, при первом же обращении. Явное открытие нужно
        ради **проверки**: `app/main.py` зовёт его при запуске, чтобы
        испорченный файл отказал один раз и фразой, а не всплыл потом
        посреди прогона.

        :raises RuntimeError: фасад уже открыт.
        :raises WorkerClosed: фасад уже остановлен.
        """
        if self._closed:
            raise WorkerClosed(_CLOSED)
        if self._store is not None:
            raise RuntimeError(
                f"поток данных уже открыт на {self.path}. Второе открытие "
                "означало бы два соединения с одной базой из одного места"
            )
        await self._submit(self._open_store)
        return self

    def _open_store(self) -> CandleStore:
        """Создать соединение. **Выполняется в потоке данных.**

        Ссылка сохраняется здесь, а не в `open()`, ровно из-за отмены:
        `await` можно отменить, работу в потоке — нет. Соединение всё равно
        будет создано, и если бы `self._store` заполнялся из корутины,
        оно осталось бы бесхозным: `close()` его не закроет, а повторный
        `open()` откроет второе к тому же файлу.
        """
        if self._store is None:
            self._store = CandleStore(self.path, sanitize=self._sanitize)
        return self._store

    async def close(self) -> None:
        """Остановить поток и закрыть базу. Повторный вызов безвреден.

        Закрытие идёт **той же очередью**, что и работа: всё, что отправили
        раньше, успевает доделаться. Поэтому вызов безопасен в `finally`
        главной корутины — ровно там, где его требует решение 0005.

        Идущая догрузка при этом получает просьбу остановиться: без неё
        `close()` во время годовой догрузки ждал бы минуты с уже закрытым
        окном. Ожидание потока — в `finally`: отменить `await` можно,
        а работу в потоке нет, и бросить незакрытое соединение нельзя.
        """
        if self._closed:
            return
        self._closed = True
        for loader in list(self._loading):
            loader.stop()
        try:
            await self._submit(self._close_store)
        finally:
            self._pool.shutdown(wait=True)

    def _close_store(self) -> None:
        """Закрыть соединение. **Выполняется в потоке данных**, после всей работы."""
        store, self._store = self._store, None
        if store is not None:
            store.close()

    @property
    def opened(self) -> bool:
        return self._store is not None

    async def __aenter__(self) -> "MarketWorker":
        return await self.open()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    # -- общий вход --------------------------------------------------------

    async def call(self, work: Callable[[CandleStore], _T]) -> _T:
        """Выполнить работу над базой в потоке данных.

        Запасной вход для всего, чему не нашлось именованного метода.

        ⚠️ `store` действителен **только внутри** `work`. Сохранить ссылку
        и позвать её потом из другого потока — то же самое, что открыть базу
        мимо фасада: `sqlite3` ответит `ProgrammingError` про поток.
        """
        store = await self._ready_store()
        return await self._submit(lambda: work(store))

    async def _ready_store(self) -> CandleStore:
        """База, открытая **по требованию**: не открыта — открыть и работать.

        ⚠️ До 06.09.2026 здесь стоял отказ «поток данных не запущен».
        Он выглядел защитой от перепутанного порядка в `app/`, а обернулся
        `B-029`: на чистой машине файла базы нет, `app/main.py` поэтому
        поток не открывал, и кнопка «Загрузить историю» — единственный
        способ базу завести — отказывала тем самым отказом. Замкнутый круг,
        и цена ему — программа, которая у владельца счёта не работает вовсе.

        Открытие идёт **той же очередью**, что и работа, значит гонки нет:
        поток один. Два вызова, одновременно увидевшие пустое место, дадут
        два задания подряд, и второе застанет соединение уже созданным
        (`_open_store` проверяет это сам).

        Остановленный поток заново не открывается: после `close()` фасад
        мёртв, и это `WorkerClosed`.
        """
        if self._closed:
            raise WorkerClosed(_CLOSED)
        if self._store is None:
            return await self._submit(self._open_store)
        return self._store

    async def _submit(self, work: Callable[[], _T]) -> _T:
        """Отправить работу в поток данных и дождаться результата.

        `run_in_executor` берёт **текущий** цикл событий: у фасада нет своего,
        и переносить его между циклами не нужно.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, work)

    # -- чтение ------------------------------------------------------------

    async def bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        since: datetime | None = None,
        until: datetime | None = None,
        *,
        drop_unsettled: bool = False,
        known_until: datetime | None = None,
    ) -> list[Candle]:
        """Свечи таймфрейма — собираются из минуток на чтении.

        `known_until` — момент, до которого вызывающий **знает**, что минутки
        полны: боевой источник знает это, потому что подписан на поток, тестер —
        потому что сам задал границу отрезка. Разбор — в `CandleStore.bars`.

        :raises ValueError: попросили собранный нами ряд (`@…`). Через этот
            фасад свечи идут в окно и в движок, а по синтетическому коду
            заявку подать нельзя — `market.synthetic`.
        """
        refuse_synthetic_for_trading(symbol)
        return await self.call(
            lambda store: store.bars(
                symbol,
                timeframe,
                since,
                until,
                drop_unsettled=drop_unsettled,
                known_until=known_until,
            )
        )

    async def minutes(
        self,
        symbol: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[Candle]:
        """Минутки инструмента.

        :raises ValueError: попросили собранный нами ряд — см. `bars`.
        """
        refuse_synthetic_for_trading(symbol)
        return await self.call(lambda store: store.minutes(symbol, since, until))

    async def coverage(self, symbol: str) -> Coverage:
        return await self.call(lambda store: store.coverage(symbol))

    async def symbols(self) -> list[str]:
        return await self.call(lambda store: store.symbols())

    async def trading_days(self, symbol: str) -> list[date]:
        return await self.call(lambda store: store.trading_days(symbol))

    async def settled_days(self, symbol: str) -> set[date]:
        """Дни, за которые уже спрашивали и которые уже закончились."""
        return await self.call(lambda store: store.settled_days(symbol))

    async def mark_days_requested(
        self,
        symbol: str,
        days: Iterable[date],
        *,
        counts: dict[date, int] | None = None,
        now: datetime,
    ) -> None:
        """Отметить, что за эти дни у источника уже спрашивали.

        Отдельный вход нужен догрузке от брокера (`app/backfill.py`): свою
        сеть она ведёт сама, а полноту дня объявляет **тем же учётом**,
        каким его объявляет загрузка с биржи. Второй механизм «этот день
        полон» разошёлся бы с первым молча, и `CandleStore.bars` поверил бы
        одному из двух.
        """
        batch = list(days)
        await self.call(
            lambda store: store.mark_days_requested(
                symbol, batch, counts=counts, now=now
            )
        )

    async def gaps(
        self,
        symbol: str,
        since: datetime | None = None,
        until: datetime | None = None,
        *,
        min_minutes: int = 1,
        crossing_date: bool | None = None,
    ) -> list[Gap]:
        """Разрывы в минутном ряду. `crossing_date=False` — только внутри дня."""
        return await self.call(
            lambda store: store.gaps(
                symbol, since, until,
                min_minutes=min_minutes, crossing_date=crossing_date,
            )
        )

    async def minute_times(
        self,
        symbol: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[datetime]:
        """Только **времена** минуток периода, по возрастанию.

        Отдельно от `minutes()` ради поиска дыр: догрузке нужны времена,
        а не цены, и на окне в четверо суток это 5 760 строк одного столбца
        вместо 5 760 объектов `Candle` со всеми полями.
        """
        return await self.call(lambda store: store.minute_times(symbol, since, until))

    async def load_log(
        self, symbol: str | None = None, limit: int = 50
    ) -> list[dict[str, object]]:
        return await self.call(lambda store: store.load_log(symbol, limit))

    # -- запись и догрузка -------------------------------------------------

    async def put_minutes(
        self, symbol: str, candles: Iterable[Candle], source: Source
    ) -> WriteStats:
        """Записать минутки. Сюда же приходит поток брокера (Э1-5)."""
        batch = list(candles)
        return await self.call(lambda store: store.put_minutes(symbol, batch, source))

    async def write_load_report(
        self, report: LoadReport, *, now: datetime | None = None
    ) -> int:
        """Записать отчёт о загрузке в журнал. Отдаёт номер записи.

        Отдельный вход нужен догрузке от брокера (`app/backfill.py`): сеть
        у неё своя (`broker/candles.py`), а отчитывается она тем же журналом
        и теми же полями, что загрузка с биржи. Без записи недокачанный
        отрезок неотличим от полного — ровно то, против чего заведён
        `LoadReport`.
        """
        return await self.call(lambda store: store.write_load_report(report, now=now))

    async def fetch_minutes(
        self,
        symbol: str,
        *,
        market: Market,
        since: date,
        until: date,
        client: IssClient | None = None,
    ) -> FetchResult:
        """Минутки с биржи **без записи в базу и без учёта дней**.

        Второй вход к ISS рядом с `sync`, и он не роскошь: у `sync_minutes`
        своя политика, и для догрузки пропущенного она неверна сразу в двух
        местах.

        * **`days_to_request` пропускает отмеченные дни.** Дыра внутри дня,
          который прошлая загрузка отметила загруженным, для `sync_minutes`
          не существует вовсе: день отфильтруется до первого запроса, и
          догрузка молча ничего не сделает. А отметка означает «спрашивали
          весь день», а не «в базе все минуты» — обрыв связи делает дыру
          внутри уже отмеченного дня штатным событием.
        * **Свою отметку в учёте догрузка ставит по своим правилам**
          (`app/backfill.py::_settle`: день целиком в запросе, хотя бы одна
          свеча, ещё не отмечен). Два механизма «день полон» над одной
          таблицей разошлись бы молча.

        Поэтому здесь — только сеть и разбор: что сервер отдал, то и вернули.
        Что из этого писать, решает вызывающий.

        ⚠️ Загрузка **занимает поток данных целиком**: `IssClient` синхронный
        (решение 0005), и записи живого потока встают в очередь за ней.
        Четверо суток минуток — это дюжина страниц, единицы секунд; глубокая
        история грузится не отсюда, а `sync`. Идущая загрузка регистрируется,
        и `close()` попросит её остановиться, вместо того чтобы ждать конца.

        :raises ValueError: период задом наперёд (проверяет `IssClient`).
        """
        await self._ready_store()
        loader = self._loader(client)
        self._loading.add(loader)
        try:
            return await self._submit(
                lambda: loader.minutes(
                    symbol, market=market, date_from=since, date_to=until
                )
            )
        finally:
            self._loading.discard(loader)

    async def instrument_spec(
        self,
        symbol: str,
        *,
        market: Market,
        client: IssClient | None = None,
    ) -> InstrumentSpec:
        """Карточка инструмента с биржи: шаг цены, стоимость шага, лот, ГО, сбор.

        Нужна ради **стоимости шага**: она превращает пункты в рубли, и
        у каждого контракта она своя (`B-021`). Читает её слой данных,
        а не движок: это свойство инструмента, а не торговое правило.

        Идёт в потоке данных, как и всё остальное здесь, — `IssClient`
        синхронный (решение 0005), и вызов из цикла событий остановил бы
        поток котировок. Запрос один и короткий: карточка — одна строка.

        ⚠️ Ходить за ней **повторно**, а не однажды: у РТС и Брента
        стоимость шага считается через курс доллара и меняется каждый день.
        Когда именно перечитывать, решает вызывающий — здесь только чтение.

        ⚠️ Базы это не касается: карточка **не пишется** в хранилище
        и учёт загрузок не трогает. Записать её значило бы завести второй
        источник правды о величине, которая плавает; вместо этого её
        спрашивают заново.

        :raises RuntimeError: поток данных закрыт.
        :raises IssUnknownInstrument: биржа такого тикера не знает.
        """
        # База здесь не нужна, а закрытый поток — нужен: `_submit` на
        # остановленном пуле даёт `RuntimeError` без единого слова о том,
        # что случилось.
        if self._closed:
            raise WorkerClosed(_CLOSED)
        loader = self._loader(client)
        return await self._submit(lambda: loader.security(symbol, market=market))

    async def point_value(
        self,
        symbol: str,
        *,
        markets: Iterable[Market] = MARKETS_PROBED,
        client: IssClient | None = None,
    ) -> PointValue:
        """Стоимость пункта цены по тикеру — числом либо причиной словами.

        Отличается от `instrument_spec` одним: **рынок не называется**.
        Порт знает тикер из настроек окна и не знает кода класса, поэтому
        рынок подбирается перебором (`market.point`). Для фьючерса это один
        запрос, для акции два.

        Ошибок наружу не отдаёт: биржа, которая не ответила, — такой же
        законный ответ, как число, и вызывающему нужно сказать о нём словами,
        а не поймать исключение (`PointValue.told`). Наружу уходит только
        `RuntimeError` про закрытый поток — это ошибка сборки, а не биржи.

        ⚠️ Базы это не касается: карточка **не пишется** в хранилище.
        Величина плавает — у РТС и Брента она считается через курс доллара
        и пересчитывается на клиринге, — и записанная однажды она стала бы
        вторым источником правды, расходящимся с биржей молча. Вместо
        записи её спрашивают заново; когда именно, решает вызывающий.

        :raises RuntimeError: поток данных закрыт.
        """
        if self._closed:
            raise WorkerClosed(_CLOSED)
        loader = self._loader(client)
        return await self._submit(
            lambda: ask_point_value(loader, symbol, markets=tuple(markets))
        )

    def _loader(self, client: IssClient | None) -> IssClient:
        """Чем идти на биржу: названный вызовом, затем фасадный, затем новый.

        Порядок именно такой. Клиент, названный в вызове, побеждает всегда —
        иначе загрузка истории потеряла бы свой отчёт о ходе работы. Фасадный
        стоит вторым, чтобы подставная сеть задавалась один раз на весь поток
        данных, а не повторялась в каждом вызове (и не забывалась в одном
        из них — забытый ушёл бы в настоящий интернет).
        """
        if client is not None:
            return client
        return self._iss if self._iss is not None else IssClient()

    async def sync(
        self,
        symbol: str,
        *,
        market: Market,
        since: date,
        until: date,
        client: IssClient | None = None,
        source: Source = Source.ISS,
        now: datetime | None = None,
        gap_threshold_minutes: int = GAP_THRESHOLD_MINUTES,
        chunk_days: int = CHUNK_DAYS,
        progress: Callable[[FetchResult], None] | None = None,
        write_report: bool = True,
    ) -> LoadReport:
        """Догрузить пропущенное с биржи. Сеть и запись — в потоке данных.

        ⚠️ `progress` вызывается **в потоке данных**, на каждой странице.
        Доставка в окно — только `QueuedConnection`.

        Загрузка запоминается на время работы: `close()` попросит её
        остановиться, вместо того чтобы ждать её конца в очереди.
        """
        store = await self._ready_store()
        loader = self._loader(client)
        self._loading.add(loader)
        try:
            return await self._submit(
                lambda: sync_minutes(
                    store,
                    loader,
                    symbol,
                    market=market,
                    since=since,
                    until=until,
                    source=source,
                    now=now,
                    gap_threshold_minutes=gap_threshold_minutes,
                    chunk_days=chunk_days,
                    progress=progress,
                    write_report=write_report,
                )
            )
        finally:
            self._loading.discard(loader)

    async def load_history(
        self,
        request: HistoryRequest,
        *,
        client: IssClient | None = None,
        progress: Callable[[FetchResult], None] | None = None,
    ) -> HistoryOutcome:
        """Глубокая загрузка по требованию человека: справка, загрузка, прогрев.

        Пара к `sync`, и разница между ними не в длине. `sync` догружает
        пропущенное и молчит о том, что вышло; `load_history` сначала
        спрашивает биржу, известен ли ей код и с какого дня у неё вообще есть
        минутки, а потом отвечает разбором — опечатка в коде, чужой рынок,
        период вне истории, мало данных для прогрева средней. Ровно этот разбор
        и нужен окну: человек нажал кнопку и обязан узнать, почему свечей нет,
        а не увидеть пустой график.

        ⚠️ Загрузка **занимает поток данных целиком**: `IssClient` синхронный
        (решение 0005), и записи живого потока встают в очередь за ней. Девяносто
        дней минуток — это минуты работы. Идущая загрузка регистрируется,
        поэтому её можно остановить: `stop_loading()` для кнопки «Отменить»
        и `close()` при выходе из программы.

        ⚠️ `progress` вызывается **в потоке данных**, на каждой странице.
        Доставка в окно — только `QueuedConnection`.
        """
        store = await self._ready_store()
        loader = self._loader(client)
        self._loading.add(loader)
        try:
            return await self._submit(
                lambda: load_history(store, loader, request, progress=progress)
            )
        finally:
            self._loading.discard(loader)

    def stop_loading(self) -> None:
        """Попросить идущие загрузки остановиться на ближайшей границе страницы.

        Зовётся **из цикла событий**, пока поток данных занят загрузкой, —
        иначе смысла нет: просьба, поставленная в ту же очередь, дождалась бы
        конца того, что отменяет.

        То же движение, что делает `close()`, но без закрытия базы: кнопка
        «Отменить» останавливает загрузку, а не программу. Скачанное
        и записанное к этому моменту остаётся в базе.
        """
        for loader in list(self._loading):
            loader.stop()

    async def inventory(self, symbol: str) -> Inventory:
        """Опись того, что по инструменту лежит в базе: дни, плотность, дыры.

        Читает **все** минутки инструмента, поэтому живёт здесь, а не в окне:
        на годовом ряде это триста тысяч отметок времени.
        """
        return await self.call(lambda store: take_inventory(store, symbol))

    async def forget_day_marks(self, symbol: str, since: date, until: date) -> int:
        """Снять отметки «за эти дни уже спрашивали». Свечи не трогаются.

        Разбор — в `CandleStore.forget_day_marks`.
        """
        return await self.call(
            lambda store: store.forget_day_marks(symbol, since, until)
        )

    # -- журналы сделок и решений ------------------------------------------

    async def open_journal_session(
        self,
        run: SessionRecord,
        *,
        now: datetime | None = None,
        keep_backtest_sessions: int = BACKTEST_SESSIONS_KEPT,
    ) -> tuple[JournalSession, PruneReport]:
        """Начать прогон. Отдаёт его вместе с отчётом о чистке старых.

        Отчёт возвращается **вместе** с прогоном, а не остаётся полем базы:
        поле пришлось бы читать вторым вызовом, а между двумя вызовами через
        фасад может встать чужая работа — очередь у потока данных общая.
        Молча выброшенные записи журнала — то, о чём вызывающий обязан узнать
        одним ответом.
        """
        def work(store: CandleStore) -> tuple[JournalSession, PruneReport]:
            session = store.open_journal_session(
                run, now=now, keep_backtest_sessions=keep_backtest_sessions
            )
            return session, store.pruned_journal

        return await self.call(work)

    async def finish_journal_session(
        self, session_id: int, *, now: datetime | None = None, note: str | None = None
    ) -> bool:
        """Отметить конец прогона. Отвечает, случилось ли закрытие.

        `False` — прогон уже был закрыт. Неизвестный номер — `LookupError`,
        а не тихий ноль строк: незакрытый прогон читается как «программу
        закрыли аварийно».
        """
        return await self.call(
            lambda store: store.finish_journal_session(session_id, now=now, note=note)
        )

    async def write_decision(self, session_id: int, record: DecisionRecord) -> int:
        """Записать одну строку журнала решений — вход боевого режима."""
        return await self.call(lambda store: store.write_decision(session_id, record))

    async def write_decisions(
        self, session_id: int, records: Iterable[DecisionRecord]
    ) -> list[int]:
        """Записать пачку строк журнала решений одной транзакцией."""
        batch = list(records)
        return await self.call(lambda store: store.write_decisions(session_id, batch))

    async def write_trades(
        self, session_id: int, records: Iterable[TradeRecord]
    ) -> tuple[list[int], tuple[int, ...]]:
        """Записать закрытые сделки одной транзакцией.

        Отдаёт **пару**: номер строки на каждую поданную сделку и, отдельно,
        номера тех из них, что в журнале уже лежали. Сделка, узнанная как
        повтор, второй строкой не пишется — иначе повтор пачки после таймаута
        удваивал бы прибыль за день, — но и пачку целиком не отвергает, иначе
        вместе с дублем пропала бы соседняя новая сделка (`CandleStore.
        write_trades`).

        ⚠️ **Пропущенные названы здесь, а не оставлены полем базы.** Поле
        пришлось бы читать вторым вызовом, а между двумя вызовами через фасад
        встаёт чужая работа: очередь у потока данных общая. Причина та же,
        что у отчёта о чистке в `open_journal_session`.

        Без второй половины ответа длина первой лжёт: на четыре поданные
        сделки в ней всегда четыре номера, и «записано четыре» неотличимо
        от «записано две, две уже лежали». Ровно то молчание, против которого
        заведена проверка перед вставкой.
        """
        batch = list(records)

        def work(store: CandleStore) -> tuple[list[int], tuple[int, ...]]:
            rows = store.write_trades(session_id, batch)
            return rows, store.repeated_trades

        return await self.call(work)

    # -- остановка робота --------------------------------------------------

    async def standing_halt(self) -> tuple[StoredHalt, ...]:
        """Причины остановки, стоящие сейчас, в порядке появления.

        ⚠️ Вызов **заводит базу**, если файла нет, — как и любая работа через
        фасад (`_ready_store`, `B-029`). Спрашивать остановку у несуществующей
        базы незачем: её там быть не может. Проверку «файл на месте» делает
        вызывающий, и делает её до вызова.
        """
        return await self.call(lambda store: store.standing_halt())

    async def raise_halt(
        self, cause: HaltRecord, *, now: datetime | None = None
    ) -> StoredHalt | None:
        """Поднять причину остановки. `None` — такая уже стоит.

        Разбор правила повтора — в `CandleStore.raise_halt`.
        """
        return await self.call(lambda store: store.raise_halt(cause, now=now))

    async def lift_halt(self, reason: str) -> bool:
        """Снять названную причину. `False` — такой не стояло.

        ⚠️ Названную **одну**. Метода «снять всё» у хранилища нет намеренно,
        и заводить его здесь обёрткой нельзя: разбор — в `CandleStore.lift_halt`.
        """
        return await self.call(lambda store: store.lift_halt(reason))

    async def journal_sessions(
        self, *, origin: RunOrigin | None = None, limit: int = 50
    ) -> JournalPage[JournalSession]:
        """Прогоны, свежие первыми. Про обрезку по лимиту выдача говорит сама."""
        return await self.call(
            lambda store: store.journal_sessions(origin=origin, limit=limit)
        )

    async def decisions(
        self,
        *,
        session_id: int | None = None,
        origin: RunOrigin | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 5000,
        all_runs: bool = False,
    ) -> JournalPage[StoredDecision]:
        """Строки журнала решений по порядку записи, старые первыми.

        Без явного прогона отдаётся последний: журнал показывается
        по одному прогону за раз, смешение объявляется `all_runs`.
        Про обрезку по лимиту выдача говорит сама — `JournalPage.truncated`.
        """
        return await self.call(
            lambda store: store.decisions(
                session_id=session_id,
                origin=origin,
                since=since,
                until=until,
                limit=limit,
                all_runs=all_runs,
            )
        )

    async def trades(
        self,
        *,
        session_id: int | None = None,
        origin: RunOrigin | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 5000,
        all_runs: bool = False,
    ) -> JournalPage[StoredTrade]:
        """Закрытые сделки по порядку записи, старые первыми.

        Сужение до прогона и признак обрезки — как у `decisions`.
        """
        return await self.call(
            lambda store: store.trades(
                session_id=session_id,
                origin=origin,
                since=since,
                until=until,
                limit=limit,
                all_runs=all_runs,
            )
        )

    async def journal_stats(self) -> JournalStats:
        """Сколько в базе журнала: прогонов, строк, сделок и байт."""
        return await self.call(lambda store: store.journal_stats())

    async def prune_journal(
        self, *, keep_backtest_sessions: int = BACKTEST_SESSIONS_KEPT
    ) -> PruneReport:
        """Убрать старые прогоны по истории. Боевой журнал не трогается."""
        return await self.call(
            lambda store: store.prune_journal(
                keep_backtest_sessions=keep_backtest_sessions
            )
        )
