"""Поток данных: база живёт в своём потоке, цикл событий при этом не стоит.

Почему это отдельный слой проверок
----------------------------------
`CandleStore` привязан к потоку, который его создал, и это выбор по замеру,
а не недосмотр ([решение 0005](../.docs/decisions/0005-concurrency-model.md)):
`check_same_thread=False` снимает проверку принадлежности и **оставляет** гонку
транзакций из-за голого `BEGIN`. Значит владение обязано обеспечиваться
конструкцией — одним потоком, который создаёт соединение и не отдаёт его
наружу. Проверяется именно это, а не «вроде работает»:

* соединение создано **в** потоке данных, а не в потоке вызывающего;
* цикл событий во время длинной работы **живёт** — счётчиком тиков, числом;
* база **не утекла** из своего потока: обращение мимо фасада падает понятной
  ошибкой, а не работает случайно;
* остановка: база закрыта, поток остановлен, повторный вызов отказывает вслух.

Цикл событий здесь обычный, `asyncio.run`. Qt не поднимается намеренно: фасад
про Qt ничего не знает, а тест, которому нужен `QApplication`, проверял бы
заодно и Qt. Сведение с циклом qasync — задача `app/`.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import TypeVar

import pytest

from market.candles import M5, MINUTE
from market.iss import FUTURES, IssClient, IssStopped
from market.storage import CandleStore, Source
from market.worker import MarketWorker

from market_helpers import FakeTransport, iss_body, minute, minutes_from, msk, no_sleep

_T = TypeVar("_T")


def database_path(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "хранилище" / "candles.sqlite3"


def in_one_loop(coroutine: Callable[[], Awaitable[_T]]) -> _T:
    """Один цикл событий на тест, закрывается сам."""
    return asyncio.run(coroutine())


# -- владение потоком --------------------------------------------------------


def test_the_store_is_created_inside_the_data_thread(tmp_path: pathlib.Path) -> None:
    """Соединение открыто не там, где его попросили открыть.

    Если `CandleStore` создать в потоке вызывающего и лишь пользоваться им
    из рабочего, первое же обращение упадёт — а тесты, где всё в одном потоке,
    этого не увидят.
    """
    async def scenario() -> tuple[str, str]:
        async with MarketWorker(database_path(tmp_path)) as worker:
            inside = await worker.call(lambda _store: threading.current_thread().name)
            return threading.current_thread().name, inside

    outside, inside = in_one_loop(scenario)
    assert inside != outside
    assert inside.startswith("market"), f"поток данных назван {inside!r}"


def test_the_thread_is_the_same_one_every_time(tmp_path: pathlib.Path) -> None:
    """Поток ровно один: иначе соединение окажется в чужом потоке через раз."""
    async def scenario() -> set[str]:
        async with MarketWorker(database_path(tmp_path)) as worker:
            return {
                await worker.call(lambda _s: threading.current_thread().name)
                for _ in range(20)
            }

    assert len(in_one_loop(scenario)) == 1


def test_the_database_does_not_leak_out_of_its_thread(tmp_path: pathlib.Path) -> None:
    """Утащенное наружу соединение падает понятной ошибкой, а не работает.

    Это и есть причина, по которой `check_same_thread=False` не ставится:
    отказ случается на первом же вызове и прямо называет поток.
    """
    async def scenario() -> None:
        async with MarketWorker(database_path(tmp_path)) as worker:
            smuggled_out = await worker.call(lambda store: store)
            with pytest.raises(sqlite3.ProgrammingError, match="thread"):
                smuggled_out.coverage("MXU6")

    in_one_loop(scenario)


def test_a_store_opened_in_one_thread_refuses_another(tmp_path: pathlib.Path) -> None:
    """Сторож против тихого возврата `check_same_thread=False`.

    Флаг снимет эту ошибку и оставит гонку транзакций: понятный отказ
    превратится в редкий `cannot start a transaction within a transaction`.
    """
    refusal: list[BaseException] = []

    def from_a_foreign_thread(store: CandleStore) -> None:
        try:
            store.put_minutes("MXU6", [minute(msk(2026, 8, 26, 10, 0))], Source.ISS)
        except BaseException as error:  # noqa: BLE001 - ловим ровно то, что вернём
            refusal.append(error)

    with CandleStore(database_path(tmp_path)) as store:
        thread = threading.Thread(target=from_a_foreign_thread, args=(store,))
        thread.start()
        thread.join()

    assert refusal and isinstance(refusal[0], sqlite3.ProgrammingError)
    assert "thread" in str(refusal[0])


# -- цикл событий не стоит ----------------------------------------------------


async def ticks_during(work: Callable[[], Awaitable[None]]) -> int:
    """Сколько раз успел сработать таймер, пока шла работа.

    Единственный автоматический способ поймать «окно подвисло»: не «должно
    работать», а число (решение 0005, пункт 25).
    """
    ticks = 0
    stop_ticking = False

    async def ticker() -> None:
        nonlocal ticks
        while not stop_ticking:
            await asyncio.sleep(0.005)
            ticks += 1

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0)  # дать тикеру встать на таймер
    await work()
    counted = ticks
    stop_ticking = True
    await task
    return counted


@pytest.mark.slow
def test_the_event_loop_keeps_running_while_the_store_works(tmp_path: pathlib.Path) -> None:
    """Длинная работа с базой не останавливает цикл событий.

    Три десятых секунды — время догрузки небольшого куска. Прямой вызов
    из корутины на это время останавливает и поток котировок, и окно.
    """
    async def scenario() -> tuple[int, int]:
        async with MarketWorker(database_path(tmp_path)) as worker:
            async def through_the_thread() -> None:
                await worker.call(lambda _store: time.sleep(0.3))

            async def directly() -> None:
                time.sleep(0.3)

            with_the_worker = await ticks_during(through_the_thread)
            with_a_direct_call = await ticks_during(directly)
            return with_the_worker, with_a_direct_call

    with_the_worker, with_a_direct_call = in_one_loop(scenario)
    assert with_a_direct_call == 0, "прямой вызов почему-то не заблокировал цикл — тест ничего не значит"
    assert with_the_worker >= 5, f"цикл событий встал: тиков всего {with_the_worker}"


# -- работа с данными ---------------------------------------------------------


def test_writes_go_through_and_survive_a_restart(tmp_path: pathlib.Path) -> None:
    """Записанное через фасад лежит в той же базе и переживает перезапуск."""
    path = database_path(tmp_path)

    async def write_them() -> None:
        async with MarketWorker(path) as worker:
            await worker.put_minutes("MXU6", minutes_from(msk(2026, 8, 26, 10, 0), 10), Source.ISS)

    async def read_them_back() -> tuple[int, list[str]]:
        async with MarketWorker(path) as worker:
            coverage_now = await worker.coverage("MXU6")
            bars = await worker.bars("MXU6", M5)
            return coverage_now.count, [b.time.strftime("%H:%M") for b in bars]

    in_one_loop(write_them)
    how_many, bar_times = in_one_loop(read_them_back)
    assert how_many == 10
    assert bar_times == ["10:00", "10:05"]


def test_the_known_boundary_reaches_the_store_through_the_facade(
    tmp_path: pathlib.Path,
) -> None:
    """Боевой источник знает границу полноты — и фасад обязан её пропускать.

    Сегодняшний день не отмечен загруженным никогда, и без этого параметра
    бар, закрывающийся на границе торгового окна, ждёт следующей минутки
    со сделкой. На перерывах ждать приходится все пятнадцать минут клиринга.
    """
    async def scenario() -> tuple[bool, bool]:
        async with MarketWorker(database_path(tmp_path)) as worker:
            await worker.put_minutes(
                "MXU6", minutes_from(msk(2026, 8, 26, 10, 50), 8), Source.ISS
            )
            no_known_until = await worker.bars("MXU6", M5)
            with_known_until = await worker.bars(
                "MXU6", M5, known_until=msk(2026, 8, 26, 11, 0)
            )
            return no_known_until[-1].unsettled, with_known_until[-1].unsettled

    without_known_until, with_known_until = in_one_loop(scenario)
    assert without_known_until is True
    assert with_known_until is False


def test_calls_keep_their_order(tmp_path: pathlib.Path) -> None:
    """Поток один, очередь общая: что отправили раньше, то и выполнится раньше.

    На этом стоит `close()`: он закрывает базу после всей отправленной работы,
    а не поперёк неё.
    """
    async def scenario() -> list[int]:
        order_seen: list[int] = []
        async with MarketWorker(database_path(tmp_path)) as worker:
            tasks = [
                asyncio.create_task(worker.call(lambda _s, n=n: order_seen.append(n)))
                for n in range(10)
            ]
            await asyncio.gather(*tasks)
        return order_seen

    assert in_one_loop(scenario) == list(range(10))


def test_sync_runs_in_the_data_thread(tmp_path: pathlib.Path) -> None:
    """Догрузка идёт там же, где база, и отчёт возвращается вызывающему.

    Сеть подставная: проверяется шов, а не доступность биржи.
    """
    day = date(2026, 8, 26)
    page = iss_body(minutes_from(msk(2026, 8, 26, 10, 0), 5))
    pages_left: list[bytes] = [page]

    def serve(_url: str) -> bytes:
        return pages_left.pop(0) if pages_left else iss_body([])

    counted_in_threads: list[str] = []

    async def scenario():
        async with MarketWorker(database_path(tmp_path)) as worker:
            return await worker.sync(
                "MXU6",
                market=FUTURES,
                since=day,
                until=day,
                now=msk(2026, 8, 27, 9, 0),
                client=IssClient(FakeTransport(serve), pause=0, sleep=no_sleep),
                progress=lambda _r: counted_in_threads.append(threading.current_thread().name),
            )

    report = in_one_loop(scenario)
    assert report.fetched == 5 and report.inserted == 5
    assert counted_in_threads, "обратный вызов о ходе работы не звали ни разу"
    assert all(thread_name.startswith("market") for thread_name in counted_in_threads), (
        f"обратный вызов пришёл не из потока данных: {set(counted_in_threads)}"
    )


def test_fetch_minutes_gives_back_what_the_server_gave_and_writes_nothing(
    tmp_path: pathlib.Path,
) -> None:
    """Отдельный вход к бирже: сеть и разбор — да, запись и учёт — нет.

    Что писать из полученного, решает вызывающий: у догрузки пропущенного
    (`app/backfill.py`) и у загрузки истории (`market/sync.py`) правила разные,
    и второй механизм «день полон» над одной таблицей разошёлся бы с первым
    молча.
    """
    day = date(2026, 8, 26)
    served = [iss_body(minutes_from(msk(2026, 8, 26, 10, 0), 5))]

    def serve(_url: str) -> bytes:
        page: bytes = served.pop(0) if served else iss_body([])
        return page

    async def scenario():
        path = database_path(tmp_path)
        client = IssClient(FakeTransport(serve), pause=0, sleep=no_sleep)
        async with MarketWorker(path, iss=client) as worker:
            got = await worker.fetch_minutes(
                "MXU6", market=FUTURES, since=day, until=day
            )
            return got, await worker.minutes("MXU6"), await worker.settled_days("MXU6")

    got, stored, settled = in_one_loop(scenario)
    assert len(got.candles) == 5, f"биржа отдала {len(got.candles)} свечей вместо 5"
    assert stored == [], "запрос к бирже записал свечи сам — писать должен вызывающий"
    assert settled == set(), "запрос к бирже отметил день в учёте — это не его дело"


def test_fetch_minutes_asks_about_a_day_the_accounting_calls_done(
    tmp_path: pathlib.Path,
) -> None:
    """День, отмеченный загруженным, всё равно запрашивается — и это главное.

    Ровно из-за этого `fetch_minutes` существует отдельно от `sync`. Отметка
    в учёте означает «за этот день спрашивали весь день», а не «в базе все
    минуты»: обрыв связи делает дыру внутри уже отмеченного дня штатным
    событием. `sync` такой день отфильтрует до первого запроса и молча
    не сделает ничего — а догрузка обязана его закрыть.
    """
    day = date(2026, 8, 26)
    asked: list[str] = []

    def serve(url: str) -> bytes:
        asked.append(url)
        first = len(asked) == 1
        page: bytes = iss_body(minutes_from(msk(2026, 8, 26, 10, 0), 3) if first else [])
        return page

    async def scenario():
        path = database_path(tmp_path)
        client = IssClient(FakeTransport(serve), pause=0, sleep=no_sleep)
        async with MarketWorker(path, iss=client) as worker:
            await worker.mark_days_requested(
                "MXU6", [day], counts={day: 0}, now=msk(2026, 8, 27, 9, 0)
            )
            through_sync = await worker.sync(
                "MXU6", market=FUTURES, since=day, until=day, now=msk(2026, 8, 27, 9, 0)
            )
            asked.clear()
            through_fetch = await worker.fetch_minutes(
                "MXU6", market=FUTURES, since=day, until=day
            )
            return through_sync, through_fetch

    through_sync, through_fetch = in_one_loop(scenario)
    assert through_sync.ranges == [], (
        "загрузка истории всё-таки полезла за отмеченным днём — проверка вакуумна"
    )
    assert len(through_fetch.candles) == 3, (
        "догрузка не спросила про отмеченный день: дыра внутри него осталась бы "
        f"навсегда, {through_fetch.candles}"
    )


def test_the_facade_client_is_used_when_the_call_does_not_name_one(
    tmp_path: pathlib.Path,
) -> None:
    """Подставная сеть задаётся один раз на фасад, а не в каждом вызове.

    Забытый в одном вызове клиент уходит в настоящий интернет, и прогон
    остаётся зелёным — так уже случилось 05.09.2026 в `tests/test_app_backfill.py`.
    Названный в вызове побеждает фасадный: загрузке истории нужен свой,
    с отчётом о ходе работы.
    """
    day = date(2026, 8, 26)
    seen: list[str] = []

    def facade(_url: str) -> bytes:
        seen.append("фасадный")
        empty: bytes = iss_body([])
        return empty

    def named(_url: str) -> bytes:
        seen.append("названный")
        empty: bytes = iss_body([])
        return empty

    async def scenario():
        path = database_path(tmp_path)
        async with MarketWorker(
            path, iss=IssClient(FakeTransport(facade), pause=0, sleep=no_sleep)
        ) as worker:
            await worker.fetch_minutes("MXU6", market=FUTURES, since=day, until=day)
            await worker.fetch_minutes(
                "MXU6",
                market=FUTURES,
                since=day,
                until=day,
                client=IssClient(FakeTransport(named), pause=0, sleep=no_sleep),
            )

    in_one_loop(scenario)
    assert seen == ["фасадный", "названный"], f"клиент выбран не тот: {seen}"


def test_close_stops_a_running_fetch_too(tmp_path: pathlib.Path) -> None:
    """Закрытие снимает и запрос догрузки, а не только загрузку истории.

    `fetch_minutes` обязан регистрировать своего клиента в том же списке
    идущих загрузок. Не зарегистрировал — и `close()` встаёт в очередь **за**
    обходом страниц: окно уже закрыто, а программа не выходит. Здесь сервер
    отдаёт страницы бесконечно, и единственный выход — остановка.
    """
    served = {"n": 0}
    started = threading.Event()

    def serve(_url: str) -> bytes:
        served["n"] += 1
        started.set()
        page: bytes = iss_body(
            minutes_from(
                msk(2026, 8, 26, 10, 0) + timedelta(minutes=served["n"] * 10), 3
            )
        )
        return page

    async def scenario() -> float:
        client = IssClient(FakeTransport(serve), pause=0, sleep=no_sleep)
        worker = await MarketWorker(database_path(tmp_path), iss=client).open()
        loading = asyncio.ensure_future(
            worker.fetch_minutes(
                "MXU6", market=FUTURES, since=date(2026, 8, 26), until=date(2026, 8, 26)
            )
        )
        while not started.is_set():
            await asyncio.sleep(0.01)

        started_at = time.monotonic()
        await worker.close()
        elapsed = time.monotonic() - started_at
        with pytest.raises(IssStopped):
            await loading
        return elapsed

    elapsed = in_one_loop(scenario)
    assert elapsed < 5, f"закрытие ждало запрос к бирже {elapsed:.1f} с"


# -- жизненный цикл ------------------------------------------------------------


def test_calling_before_open_opens_the_base_itself(tmp_path: pathlib.Path) -> None:
    """Стережёт `B-029`: спросили до `open()` — база открылась, а не отказала.

    Раньше здесь стоял отказ «поток данных не запущен: сначала `await
    worker.open()`». Он выглядел защитой от перепутанного порядка вызовов
    в `app/`, а стоил владельцу счёта неработающей программы: файла базы
    на чистой машине нет, `app/main.py` поэтому поток не открывал, и кнопка
    «Загрузить историю» — единственный способ базу завести — упиралась
    в этот самый отказ.

    ⚠️ Проверяется и **файл**: без него «не отказало» ничего не значит —
    отказать могло бы и пустое соединение в никуда.
    """
    path = database_path(tmp_path)
    assert not path.exists(), "файла быть не должно — иначе проверка вакуумна"

    async def scenario():
        worker = MarketWorker(path)
        try:
            return await worker.coverage("MXU6")
        finally:
            await worker.close()

    coverage = in_one_loop(scenario)
    assert coverage.empty, "в только что заведённой базе свечей быть не может"
    assert path.exists(), "база сама себя не завела: файла на диске нет"


def test_the_load_works_on_a_machine_where_the_base_was_never_created(
    tmp_path: pathlib.Path,
) -> None:
    """Стережёт главный путь `B-029`: запись в базу, которой ещё нет.

    Чтение открывает базу и на сломанном коде — оно шло через `call()`.
    Загрузка идёт **мимо** `call()`, своим входом (`sync`, `load_history`,
    `fetch_minutes` берут соединение сами), и до 06.09.2026 ровно она
    и отказывала: «Загрузка не удалась: поток данных не запущен».
    """
    path = database_path(tmp_path)

    async def scenario() -> int:
        worker = MarketWorker(path)
        try:
            await worker.put_minutes(
                "MXU6", minutes_from(msk(2026, 8, 26, 10, 0), 3), Source.ISS
            )
            return (await worker.coverage("MXU6")).count
        finally:
            await worker.close()

    assert in_one_loop(scenario) == 3, "записать в незаведённую базу не удалось"


def test_opening_twice_is_refused(tmp_path: pathlib.Path) -> None:
    async def scenario() -> None:
        async with MarketWorker(database_path(tmp_path)) as worker:
            with pytest.raises(RuntimeError, match="уже открыт"):
                await worker.open()

    in_one_loop(scenario)


def test_after_close_the_thread_is_stopped_and_the_base_is_closed(
    tmp_path: pathlib.Path,
) -> None:
    """Остановка: поток не жив, база закрыта, повторный вызов отказывает."""
    async def scenario() -> tuple[str, bool]:
        worker = MarketWorker(database_path(tmp_path))
        await worker.open()
        thread_name = await worker.call(lambda _s: threading.current_thread().name)
        await worker.close()
        await worker.close()  # повторная остановка безвредна
        with pytest.raises(RuntimeError, match="остановлен"):
            await worker.coverage("MXU6")
        alive = any(thread.name == thread_name and thread.is_alive() for thread in threading.enumerate())
        return thread_name, alive

    _, alive = in_one_loop(scenario)
    assert not alive, "поток данных остался жить после остановки"


def test_close_without_open_is_safe(tmp_path: pathlib.Path) -> None:
    """`finally: await worker.close()` не должен падать, если открыть не успели."""
    async def scenario() -> None:
        await MarketWorker(database_path(tmp_path)).close()

    in_one_loop(scenario)


def test_a_cancelled_open_does_not_lose_the_connection(tmp_path: pathlib.Path) -> None:
    """Отменённый `open()` не оставляет бесхозного соединения.

    Отменить `await` можно, работу в потоке — нет: `CandleStore` всё равно
    будет создан. Пока ссылка на него присваивалась из корутины, отмена
    оставляла соединение никому не принадлежащим: `close()` его не закрывал,
    а повторный `open()` открывал **второе** соединение к тому же файлу.
    """
    async def scenario() -> tuple[bool, bool]:
        worker = MarketWorker(database_path(tmp_path))
        task = asyncio.ensure_future(worker.open())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Работа в потоке доводится до конца — дождёмся её. Ожидание
        # ограничено: тест обязан падать, а не висеть.
        for _ in range(200):
            if worker.opened:
                break
            await asyncio.sleep(0.01)
        assert worker.opened, "соединение создано, но фасад о нём не знает"

        second_open_refused = False
        try:
            await worker.open()
        except RuntimeError:
            second_open_refused = True

        await worker.close()
        return worker.opened, second_open_refused

    opened_after_close, refused = in_one_loop(scenario)
    assert refused, "второй open() открыл бы второе соединение к тому же файлу"
    assert not opened_after_close, "соединение осталось незакрытым"


def test_a_cancelled_close_still_closes_the_base(tmp_path: pathlib.Path) -> None:
    """Отменённый `close()` всё равно доводит закрытие до конца.

    `shutdown(wait=True)` стоит в `finally`: иначе отмена оставляла бы
    и незакрытую базу, и живой поток.
    """
    async def scenario() -> tuple[str, bool]:
        worker = MarketWorker(database_path(tmp_path))
        await worker.open()
        thread_name = await worker.call(lambda _s: threading.current_thread().name)
        task = asyncio.ensure_future(worker.close())
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # Ждать нечего: `shutdown(wait=True)` уже отработал в `finally`.
        alive = any(thread.name == thread_name and thread.is_alive() for thread in threading.enumerate())
        return thread_name, alive

    _, alive = in_one_loop(scenario)
    assert not alive, "поток данных остался жить после отменённой остановки"


def test_close_asks_a_running_load_to_stop_instead_of_queueing_behind_it(
    tmp_path: pathlib.Path,
) -> None:
    """`close()` не ждёт конца догрузки, а просит её остановиться.

    Очередь одна, и без просьбы закрытие стоит **за** загрузкой: год минуток —
    это минуты ожидания при уже закрытом окне. Здесь сервер отдаёт страницы
    бесконечно, и единственный способ выйти — остановка.
    """
    pages_served = {"n": 0}
    started = threading.Event()

    def serve(_url: str) -> bytes:
        pages_served["n"] += 1
        started.set()
        return iss_body(
            minutes_from(msk(2026, 8, 26, 10, 0), 3, step=1)
            if pages_served["n"] == 1
            else minutes_from(msk(2026, 8, 26, 10, 0) + timedelta(minutes=pages_served["n"] * 10), 3)
        )

    async def scenario() -> int:
        worker = MarketWorker(database_path(tmp_path))
        await worker.open()
        loading = asyncio.ensure_future(
            worker.sync(
                "MXU6", market=FUTURES,
                since=date(2026, 8, 26), until=date(2026, 8, 26),
                client=IssClient(FakeTransport(serve), pause=0, sleep=no_sleep),
                now=msk(2026, 8, 27, 9, 0),
            )
        )
        while not started.is_set():
            await asyncio.sleep(0.01)

        started_at = time.monotonic()
        await worker.close()
        elapsed = time.monotonic() - started_at

        with pytest.raises(IssStopped):
            await loading
        assert elapsed < 5, f"закрытие ждало догрузку {elapsed:.1f} с"
        return pages_served["n"]

    pages_total = in_one_loop(scenario)
    assert pages_total >= 1


def test_an_interrupted_load_still_reaches_the_journal(tmp_path: pathlib.Path) -> None:
    """Остановленная загрузка пишет отчёт: скачанное записано, причина названа."""
    pages_served = {"n": 0}
    client = IssClient(FakeTransport(lambda _u: b""), pause=0, sleep=no_sleep)

    def serve(_url: str) -> bytes:
        pages_served["n"] += 1
        if pages_served["n"] == 2:
            client.stop()
        return iss_body(
            minutes_from(msk(2026, 8, 26, 10, 0) + timedelta(minutes=pages_served["n"] * 10), 3)
        )

    client._transport = FakeTransport(serve)  # noqa: SLF001 — транспорт подменяется
    # после создания клиента: сам клиент нужен обработчику, чтобы его остановить

    async def scenario() -> list[dict[str, object]]:
        async with MarketWorker(database_path(tmp_path)) as worker:
            with pytest.raises(IssStopped):
                await worker.sync(
                    "MXU6", market=FUTURES,
                    since=date(2026, 8, 26), until=date(2026, 8, 26),
                    client=client, now=msk(2026, 8, 27, 9, 0),
                )
            return await worker.load_log("MXU6")

    (entry,) = in_one_loop(scenario)
    assert "оборвалась на куске" in str(entry["summary"])
    assert "IssStopped" in str(entry["summary"])


def test_a_failure_inside_the_thread_comes_back_to_the_caller(
    tmp_path: pathlib.Path,
) -> None:
    """Ошибка не остаётся в потоке данных: её видит тот, кто ждал результата."""
    async def scenario() -> None:
        async with MarketWorker(database_path(tmp_path)) as worker:
            with pytest.raises(ValueError, match="только минутные"):
                await worker.put_minutes(
                    "MXU6",
                    [minute(msk(2026, 8, 26, 10, 0)).replace(timeframe=M5)],
                    Source.ISS,
                )
            # Поток после отказа продолжает работать.
            assert (await worker.coverage("MXU6")).empty

    in_one_loop(scenario)


def test_importing_the_layer_does_not_drag_qt_in() -> None:
    """`import market` не поднимает PySide6 — ни прямо, ни через фасад.

    Ход работы догрузки естественнее всего отдавать сигналом Qt, и место
    такому сигналу — в `app/`, а не здесь. Стоит `Signal` появиться
    в `market/worker.py`, и `import market` потянет PySide6 в процесс-работник
    оптимизатора, где Qt не поднимается вовсе (решение 0005, пункт 18):
    работник перестанет запускаться, а `tests/test_layers.py` этого не увидит —
    PySide6 не слой проекта.

    Проверяется отдельным процессом: в общем прогоне PySide6 давно поднят
    тестами окна, и `sys.modules` ответил бы «да» независимо от `market/`.
    """
    repository_root = pathlib.Path(__file__).resolve().parent.parent
    outcome = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, market; "
            "print(sorted(n for n in sys.modules if n.split('.')[0] in "
            "{'PySide6', 'qasync', 'shiboken6'}))",
        ],
        cwd=repository_root,
        capture_output=True,
        text=True,
        check=True,
    )
    assert outcome.stdout.strip() == "[]", f"import market поднял Qt: {outcome.stdout.strip()}"


FOREIGN = ("PySide6", "qasync", "httpx", "websockets", "broker", "engine", "ui", "app")


def imports_of(source_text: str) -> set[str]:
    """Верхние имена всех импортов модуля — по дереву, а не по подстроке."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source_text)):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def test_the_data_thread_holds_nothing_foreign() -> None:
    """Фасад — стандартная библиотека и свой слой, и только.

    Текстовая проверка `f"import {чужое}" not in источник` пропускала самую
    вероятную форму записи: `from PySide6.QtCore import Signal`. Здесь читается
    дерево, и форма записи значения не имеет.

    Подпроцессная проверка ниже сильнее (она смотрит, что реально поднялось),
    но эта показывает **строку** нарушения и не зависит от того, что уже
    импортировано в прогоне.
    """
    import market.worker

    source_of_the_facade = pathlib.Path(market.worker.__file__).read_text(encoding="utf-8")
    foreign_found = imports_of(source_of_the_facade) & set(FOREIGN)
    assert not foreign_found, f"фасад тянет {sorted(foreign_found)}"


def test_the_import_guard_sees_a_from_import() -> None:
    """Сторож проверен подсадкой: `from PySide6.QtCore import Signal` ловится.

    Прежняя текстовая проверка на этой строке молчала.
    """
    assert "PySide6" in imports_of("from PySide6.QtCore import Signal\n")
    assert "PySide6" in imports_of("import PySide6.QtCore as qt\n")
    assert "httpx" in imports_of("import httpx\n")
    assert imports_of("import asyncio\nfrom datetime import date\n") & set(FOREIGN) == set()


def test_a_minute_written_through_the_worker_keeps_its_timeframe(
    tmp_path: pathlib.Path,
) -> None:
    async def scenario():
        async with MarketWorker(database_path(tmp_path)) as worker:
            await worker.put_minutes("MXU6", [minute(msk(2026, 8, 26, 10, 0))], Source.ISS)
            return await worker.minutes("MXU6")

    (candle,) = in_one_loop(scenario)
    assert candle.timeframe == MINUTE
    assert candle.filled_minutes == 1


# -- журналы через фасад -----------------------------------------------------


def test_the_journal_goes_through_the_data_thread(tmp_path: pathlib.Path) -> None:
    """Журнал пишется и читается тем же одним потоком, что и свечи.

    Отдельного соединения у журнала нет: база одна, поток один. Иначе
    вернулась бы ровно та гонка транзакций, ради которой заведён фасад.
    """
    from market.journal import (
        DecisionRecord,
        RunOrigin,
        SessionRecord,
        TradeRecord,
        TradeSide,
    )

    at = msk(2026, 9, 3, 10, 10)

    async def work() -> tuple[list[str], int, str]:
        async with MarketWorker(database_path(tmp_path)) as worker:
            session, _ = await worker.open_journal_session(
                SessionRecord(RunOrigin.LIVE, symbol="MXU6", timeframe="5 минут"),
                now=at,
            )
            await worker.write_decisions(
                session.id,
                [
                    DecisionRecord(at, "Лонг открыт", "Объём 1 контракт"),
                    DecisionRecord(at, "Тейк выставлен", "Уровень 100,5"),
                ],
            )
            await worker.write_trades(
                session.id,
                [
                    TradeRecord(
                        symbol="MXU6",
                        side=TradeSide.LONG,
                        volume=1.0,
                        entry_time=at,
                        entry_price=100.0,
                        exit_time=at + timedelta(minutes=5),
                        exit_price=101.0,
                        exit_reason="обратный сигнал средней",
                        gross=1.0,
                    )
                ],
            )
            await worker.finish_journal_session(session.id, now=at + timedelta(hours=1))
            rows = await worker.decisions()
            deals = await worker.trades()
            stats = await worker.journal_stats()
            return (
                [row.event for row in rows],
                stats.decisions,
                deals[0].origin.value,
            )

    events, decisions, origin = in_one_loop(work)
    assert events == ["Лонг открыт", "Тейк выставлен"]
    assert decisions == 2
    assert origin == "live"


def test_the_facade_reports_what_the_cleanup_removed(tmp_path: pathlib.Path) -> None:
    """Отчёт о чистке приходит вместе с прогоном, одним ответом.

    Вторым вызовом его читать нельзя: очередь у потока данных общая,
    и между двумя вызовами может встать чужая работа.
    """
    from market.journal import RunOrigin, SessionRecord

    at = msk(2026, 9, 3, 10, 10)

    async def work() -> tuple[int, int]:
        async with MarketWorker(database_path(tmp_path)) as worker:
            for _ in range(4):
                await worker.open_journal_session(
                    SessionRecord(RunOrigin.BACKTEST), now=at, keep_backtest_sessions=2
                )
            _, pruned = await worker.open_journal_session(
                SessionRecord(RunOrigin.BACKTEST), now=at, keep_backtest_sessions=2
            )
            kept = await worker.journal_sessions(origin=RunOrigin.BACKTEST, limit=99)
            return pruned.sessions, len(kept)

    removed, kept = in_one_loop(work)
    assert removed == 1
    assert kept == 2


def test_the_facade_says_which_trades_were_already_written(
    tmp_path: pathlib.Path,
) -> None:
    """Пропуск повтора обязан быть виден **за** границей слоя, а не только внутри.

    Хранилище узнаёт уже записанную сделку и не пишет её второй строкой:
    повтор пачки после таймаута удваивал прибыль за день. Но пачку целиком
    оно больше не отвергает — иначе вместе с дублем пропадала бы соседняя
    новая сделка, а это боевая запись на настоящие деньги.

    Отсюда требование к фасаду. Длина ответа сама по себе лжёт: на три
    поданные сделки в ней всегда три номера, и «записано три» неотличимо
    от «записана одна, две уже лежали». Пропущенные приходят вторым
    элементом ответа — тем же приёмом, что отчёт о чистке
    у `open_journal_session`, и по той же причине: поле базы пришлось бы
    читать вторым вызовом, а очередь у потока данных общая.
    """
    from market.journal import RunOrigin, SessionRecord, TradeRecord, TradeSide

    at = msk(2026, 9, 3, 10, 10)

    def deal(entry: datetime, price: float = 100.0) -> TradeRecord:
        return TradeRecord(
            symbol="MXU6",
            side=TradeSide.LONG,
            volume=1.0,
            entry_time=entry,
            entry_price=price,
            exit_time=entry + timedelta(minutes=5),
            exit_price=price + 1.0,
            exit_reason="обратный сигнал средней",
            gross=1.0,
        )

    async def work() -> tuple[list[int], tuple[int, ...], list[int], tuple[int, ...], int]:
        async with MarketWorker(database_path(tmp_path)) as worker:
            session, _ = await worker.open_journal_session(
                SessionRecord(RunOrigin.LIVE, symbol="MXU6"), now=at
            )
            day = [deal(at), deal(at + timedelta(minutes=5))]
            rows, first_repeats = await worker.write_trades(session.id, day)
            # 10:47, сработал тейк. Вызывающий подаёт день заново — так бывает,
            # когда ответ на первую пачку не дошёл.
            take = deal(at + timedelta(minutes=37), price=101.0)
            again, repeats = await worker.write_trades(session.id, [*day, take])
            stats = await worker.journal_stats()
            return rows, first_repeats, again, repeats, stats.trades

    rows, first_repeats, again, repeats, trades = in_one_loop(work)

    assert first_repeats == (), "новые сделки объявлены повтором"
    assert trades == 3, "новая сделка пропала вместе с пачкой-повтором"
    assert again[:2] == rows, "у повторов сменились номера строк"
    assert repeats == tuple(rows), (
        "фасад не назвал пропущенные сделки: наружу «записано три из трёх», "
        "а записана одна — то же молчание, только этажом выше"
    )


def test_the_facade_passes_the_cleaner_to_the_database(tmp_path: pathlib.Path) -> None:
    """Чистка секретов, отданная фасаду, доезжает до записи в базу."""
    from market.journal import DecisionRecord, RunOrigin, SessionRecord

    at = msk(2026, 9, 3, 10, 10)
    secret = "значение-этого-сеанса"

    async def work() -> str:
        worker = MarketWorker(
            database_path(tmp_path),
            sanitize=lambda text: text.replace(secret, "<вырезано>"),
        )
        async with worker:
            session, _ = await worker.open_journal_session(
                SessionRecord(RunOrigin.LIVE), now=at
            )
            await worker.write_decision(
                session.id, DecisionRecord(at, "Отказ", f"сервер вернул {secret}")
            )
            rows = await worker.decisions()
            return rows[0].reason

    assert secret not in in_one_loop(work)


def test_the_facade_refuses_to_finish_a_run_that_does_not_exist(
    tmp_path: pathlib.Path,
) -> None:
    """Ошибка в номере не превращается в вечно незакрытый прогон.

    Незакрытый прогон читается как «программу закрыли аварийно». Молчаливый
    ноль строк здесь означал бы ложный факт о торговом дне.
    """
    from market.journal import RunOrigin, SessionRecord

    at = msk(2026, 9, 3, 10, 10)

    async def work() -> tuple[bool, bool]:
        async with MarketWorker(database_path(tmp_path)) as worker:
            session, _ = await worker.open_journal_session(
                SessionRecord(RunOrigin.LIVE), now=at
            )
            with pytest.raises(LookupError, match="прогона 999 в журнале нет"):
                await worker.finish_journal_session(999, now=at)
            first = await worker.finish_journal_session(session.id, now=at)
            second = await worker.finish_journal_session(session.id, now=at)
            return first, second

    assert in_one_loop(work) == (True, False)
