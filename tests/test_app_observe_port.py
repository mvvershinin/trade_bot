"""Живой ход движка со стороны порта: включение, пометка строк, запись прогона.

Отдельный файл, а не раздел в `tests/test_app_observe.py`, и причина
не в оформлении: здесь нужен цикл событий поверх Qt — тот же, что собирает
`app/main.py`, — а наблюдателю самому Qt не нужен вовсе.

⚠️ Разделение появилось при разборе SIGSEGV: пока наблюдатель разбирал бары
в фоновой задаче, обе части в одном файле роняли процесс — падало при этом
не здесь, а **в чужом** `tests/test_app_port.py`, с плавающим местом падения.
Настоящую причину сняли в самом наблюдателе (`app/observe.py`), но границу
«кому нужен Qt, кому нет» она провела верно, и она остаётся.

Что здесь проверяется: наблюдение включается вместе с потоком котировок;
строки помечены «симуляция на боевом потоке», а не «прогон по истории»;
запись в журнале прогонов **одна на весь ход**, а не по строке на бар.
"""

from __future__ import annotations

import dataclasses
import math
import os
import pathlib
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("QT_API", "pyside6")  # до первого импорта qasync

from app.port import HistoryPort
from market import MSK, Candle, CandleStore, MarketWorker, Source, Timeframe
from market.journal import RunOrigin as StoredOrigin
from market.journal import redact
from ui.models import ChartData
from ui.models import RunOrigin as WindowOrigin
from ui.models import Settings as UiSettings

DAY = datetime(2026, 6, 19, 7, 0, tzinfo=MSK)  # пятница, до открытия окна


def bars(count: int = 120, start: datetime = DAY, step: int = 5) -> list[Candle]:
    """Ряд свечей с колебанием вокруг ровной цены — как в соседнем файле."""
    out: list[Candle] = []
    for index in range(count):
        price = 100000.0 + 300.0 * math.sin(index / 9.0)
        out.append(Candle(
            time=start + timedelta(minutes=index * step),
            open=price,
            high=price + 40.0,
            low=price - 40.0,
            close=price,
            volume=1.0,
            timeframe=Timeframe(step),
            filled_minutes=step,
        ))
    return out



@pytest.fixture
def live_database(tmp_path: pathlib.Path) -> pathlib.Path:
    """База с минутками на два часа: хватает и на прогрев, и на живые бары."""
    path = tmp_path / "live.sqlite3"
    minutes = [
        dataclasses.replace(bar, timeframe=Timeframe(1), filled_minutes=1)
        for bar in bars(600, step=1)
    ]
    with CandleStore(path) as store:
        store.put_minutes("MXU6", minutes, Source.ISS)
    return path


@dataclasses.dataclass(frozen=True, slots=True)
class _Watched:
    """Снимок порта, взятый до завершения: после него наблюдения уже нет."""

    observing: bool
    observer: bool
    journal: tuple


def watched_port(loop, database: pathlib.Path, passes: int = 4):
    """Порт с включённым наблюдением и `passes` проходами, как на закрытии бара."""

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(worker, values=UiSettings(), days=0, sanitize=redact)
        port.attach_stream(lambda on: None)  # подключение «удалось»
        try:
            port.stream(True)
            await port.wait()
            for number in range(passes):
                port.refresh(f"закрылся бар {number}")
                await port.wait()
            # Снимок состояния берётся ДО завершения: `aclose` закрывает ход
            # и обнуляет поле, и проверка «ход собран» после него была бы
            # проверкой завершения, а не включения.
            seen = _Watched(
                port._watch.observing,  # noqa: SLF001
                port._watch.observer is not None,  # noqa: SLF001
                port._journal,  # noqa: SLF001
            )
            # ⚠️ Порт закрывается **до** чтения журнала прогонов, а не в общем
            # `finally` после него. Две причины, и обе по делу. Итог хода
            # дописывается при закрытии — читать записи раньше значит читать
            # незаконченные. И порядок «закрыли порт → читаем → закрыли базу»
            # тот же, что в `app/main.py`: живой ход пишет в хранилище, значит
            # гаснет раньше него.
            await port.aclose()
            sessions = await worker.journal_sessions(limit=100)
            return seen, sessions.rows
        finally:
            await port.aclose()
            await worker.close()

    return loop.run_until_complete(go())


def test_observation_starts_together_with_the_quote_stream(loop, live_database) -> None:
    """Стережёт: нажали «Подключиться» — движок пошёл по живому ряду.

    Отдельной кнопки у наблюдения нет: без потока движку неоткуда брать новые
    бары. Сломав связку, получаем поток без решений — то есть Э2 вместо Э3,
    и заметить это можно было бы только глазами.
    """
    seen, _ = watched_port(loop, live_database, passes=1)
    assert seen.observing, "поток включён, а наблюдения нет"
    assert seen.observer, "живой ход не собран"


def test_the_live_run_writes_one_record_and_not_one_per_bar(loop, live_database) -> None:
    """Стережёт находку ревью 05.09.2026: запись прогона одна на весь ход.

    Прежде запись открывалась вокруг каждого прогона, а прогон гнался
    на закрытии каждого бара: `--stream` на минутках давал 840 одинаковых
    строк за день при хранении в 200 — ручной подбор владельца счёта
    вычищался за три с половиной часа.
    """
    _, sessions = watched_port(loop, live_database, passes=6)
    assert len(sessions) == 1, (
        f"на семь проходов записано {len(sessions)} прогонов — запись идёт "
        "на каждый бар, и она вычистит ручной подбор владельца счёта"
    )
    assert sessions[0].origin is StoredOrigin.PAPER, (
        f"живой ход записан как «{sessions[0].origin}»: чистка журнала "
        "удаляет прогоны по истории и не трогает симуляцию, и по этому "
        "признаку они и различаются"
    )


def test_the_live_record_is_closed_with_its_result(loop, live_database) -> None:
    """Стережёт: ход, закрытый по выключению потока, дописывает итог.

    Итог пишется на закрытии хода, а не по дороге: колонка называется «чем
    прогон кончился», и промежуточное число в ней было бы неправдой ровно
    там, где человек ищет, что случилось.
    """

    async def go():
        worker = MarketWorker(live_database)
        await worker.open()
        port = HistoryPort(worker, values=UiSettings(), days=0, sanitize=redact)
        port.attach_stream(lambda on: None)
        try:
            port.stream(True)
            await port.wait()
            port.refresh("закрылся бар")
            await port.wait()
            port.stream(False)  # выключили поток — ход закрывается
            await port.wait()
            sessions = await worker.journal_sessions(limit=10)
            return sessions.rows
        finally:
            await port.aclose()
            await worker.close()

    rows = loop.run_until_complete(go())
    live = [row for row in rows if row.origin is StoredOrigin.PAPER]
    assert live, "живой ход не записан вовсе"
    assert live[0].finished_at is not None, "ход закрыт без времени конца"
    assert live[0].finish_note, "ход закрыт без итога"


def test_live_rows_are_marked_as_simulation_not_as_a_history_run(loop, live_database) -> None:
    """Стережёт: строки живого хода помечены «симуляция на боевом потоке».

    Выгрузка журнала уезжает из окна файлом и живёт дальше сама (решение 0011).
    День наблюдения на живом потоке и вечер подбора на истории обязаны
    отличаться в самой строке: по числу сделок из второго объём для первого
    не выбирают.
    """
    seen, _ = watched_port(loop, live_database, passes=2)
    rows = seen.journal
    live = [row for row in rows if row.origin is WindowOrigin.PAPER]
    assert live, f"ни одной строки живого хода: {[row.origin for row in rows][:5]}"
    assert all(row.origin is not WindowOrigin.LIVE for row in rows), (
        "строка помечена боевым режимом — заявок программа не подаёт"
    )


def test_the_history_run_is_still_marked_as_a_history_run(loop, live_database) -> None:
    """Стережёт: без потока всё осталось как было — прогон по истории.

    Обратная сторона предыдущей проверки: пометив живым ходом всё подряд,
    мы получили бы отчёт, в котором прогон по прошлому выдан за наблюдение.
    """

    async def go():
        worker = MarketWorker(live_database)
        await worker.open()
        port = HistoryPort(worker, values=UiSettings(), days=0, sanitize=redact)
        try:
            port.refresh("тест")
            await port.wait()
            sessions = await worker.journal_sessions(limit=10)
            return port._journal, sessions.rows  # noqa: SLF001
        finally:
            await port.aclose()
            await worker.close()

    rows, sessions = loop.run_until_complete(go())
    assert rows and all(row.origin is WindowOrigin.BACKTEST for row in rows), (
        "прогон по истории помечен не как прогон по истории"
    )
    assert sessions and all(row.origin is StoredOrigin.BACKTEST for row in sessions)


def test_the_owner_sees_the_live_decisions_on_the_chart(loop, live_database) -> None:
    """Стережёт то, ради чего этап и делается: решения видны на экране.

    Не «движок посчитал», а **владелец счёта увидел**: свечи, линия средней,
    метки входов и выходов, строки решений. Без этой проверки живой ход мог бы
    работать вхолостую — считать и никуда не отдавать, — и заметить это можно
    было бы только глазами на живом потоке, то есть слишком поздно.
    """
    charts: list[ChartData] = []
    decisions: list[tuple] = []

    async def go():
        worker = MarketWorker(live_database)
        await worker.open()
        port = HistoryPort(worker, values=UiSettings(), days=0, sanitize=redact)
        port.chart_replaced.connect(charts.append)
        port.decisions_replaced.connect(decisions.append)
        port.attach_stream(lambda on: None)
        try:
            port.stream(True)
            await port.wait()
            port.refresh("закрылся бар")
            await port.wait()
        finally:
            await port.aclose()
            await worker.close()

    loop.run_until_complete(go())
    assert charts, "график не отправлен в окно ни разу"
    chart = charts[-1]
    assert chart.candles, "свечей на графике нет"
    assert chart.average, "линии средней нет — окно не показывает, по чему робот решает"
    assert chart.markers, "меток входов и выходов нет — решения не видно"
    assert decisions and decisions[-1], "журнал решений пуст"
    live = [row for row in decisions[-1] if row.origin is WindowOrigin.PAPER]
    assert live, "в журнале нет ни одной строки живого хода"


def test_the_history_run_reports_itself_and_the_live_run_does_not(
    loop, live_database
) -> None:
    """Стережёт: строка «столько-то свечей, столько-то сделок» — только у истории.

    Прогон по истории делается по команде человека, и такая строка — ответ
    на неё. Живой ход проходит на **каждом** закрытом баре: та же строка стала
    бы дюжиной записей в час об одном и том же, и настоящие решения движка
    утонули бы под ней. Проверяются обе стороны сразу — иначе «убрать строку
    везде» прошло бы молча.
    """

    async def go(observing: bool):
        worker = MarketWorker(live_database)
        await worker.open()
        port = HistoryPort(worker, values=UiSettings(), days=0, sanitize=redact)
        port.attach_stream(lambda on: None)
        try:
            if observing:
                port.stream(True)
            port.refresh("тест")
            await port.wait()
            port.refresh("ещё раз")
            await port.wait()
            return [row.event for row in port._journal]  # noqa: SLF001
        finally:
            await port.aclose()
            await worker.close()

    history = loop.run_until_complete(go(observing=False))
    live = loop.run_until_complete(go(observing=True))
    assert history.count("Прогон по истории") >= 1, (
        f"прогон по истории о себе не сказал: {history}"
    )
    assert "Прогон по истории" not in live, (
        f"живой ход отчитывается строкой прогона по истории на каждом баре: {live}"
    )
    assert "Наблюдение включено" in live, (
        f"о включении живого хода не сказано вовсе: {live}"
    )


# --------------------------- глубина показа и остановка робота (`D-033`)

def live_port(loop, database: pathlib.Path, scenario):
    """Порт с включённым наблюдением; `scenario` получает его и порт ждёт сам."""

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(worker, values=UiSettings(), days=0, sanitize=redact)
        port.attach_stream(lambda on: None)  # подключение «удалось»
        try:
            port.stream(True)
            await port.wait()
            return await scenario(port)
        finally:
            await port.aclose()
            await worker.close()

    return loop.run_until_complete(go())


def test_changing_the_depth_does_not_rebuild_the_live_run(loop, live_database) -> None:
    """Стережёт `D-033`: глубина показа — настройка окна, а не хода движка.

    До 05.09.2026 глубина входила в то, чем задан живой ход, и её смена
    собирала движок заново. Движку глубина показа не принадлежит вовсе:
    лента берёт из отрезка только то, чего ещё не видела.
    """

    async def scenario(port: HistoryPort):
        before = port._watch.observer  # noqa: SLF001 — ход наружу порт не отдаёт
        port.set_depth(1)
        port.refresh("глубина показа изменена")
        await port.wait()
        return before, port._watch.observer  # noqa: SLF001 — то же

    before, after = live_port(loop, live_database, scenario)
    assert before is not None, "живой ход не собрался — проверять нечего"
    assert after is before, (
        "смена глубины показа пересобрала живой ход движка: новый движок "
        "рождается с чистого листа, и всё, что он помнил, теряется"
    )


def test_changing_the_depth_does_not_lift_a_halt(loop, live_database) -> None:
    """Стережёт `D-033` по последствию: остановленный робот остаётся остановленным.

    Сценарий из аудита: робот встал по дневному лимиту убытка, владелец счёта
    крутит глубину показа, чтобы разглядеть, что случилось, — и робот торгует
    дальше. Решение 0014 обещает «возобновление только вручную», а тут
    остановка снималась настройкой показа.

    ⚠️ Остановка ставится **напрямую в поле хода**, и это не лень. Сегодня
    остановить движок законным путём в проверке нечем: боевого исполнителя
    в программе нет, а исполнитель-тень заявки не отвергает никогда. Поле
    `_failure` — ровно то, что порт читает как «робот остановлен»
    (`LiveObserver.run.halted`), поэтому подмена проверяет тот же путь.
    """
    halt = "Робот остановлен. Дневной лимит убытка достигнут."

    async def scenario(port: HistoryPort):
        observer = port._watch.observer  # noqa: SLF001 — ход наружу порт не отдаёт
        assert observer is not None
        observer._failure = halt  # noqa: SLF001 — законного пути остановки сегодня нет
        port.set_depth(1)
        port.refresh("глубина показа изменена")
        await port.wait()
        run = port._run  # noqa: SLF001 — снимок прогона наружу порт не отдаёт
        return None if run is None else run.halted

    assert live_port(loop, live_database, scenario) == halt, (
        "остановка робота снята сменой глубины показа — настройкой, которая "
        "к торговле отношения не имеет"
    )


def test_changing_a_trading_setting_still_rebuilds_the_live_run(
    loop, live_database
) -> None:
    """Канарейка к двум тестам выше: пересборка не выключена целиком.

    Без неё оба прошли бы и при полностью снятой пересборке — а она нужна:
    средняя, посчитанная наполовину по прежнему периоду, не соответствует
    ни старым настройкам, ни новым, а выглядит как настоящая.
    """

    async def scenario(port: HistoryPort):
        before = port._watch.observer  # noqa: SLF001 — то же
        port.apply_settings(UiSettings().replace(average_period=20))
        await port.wait()
        return before, port._watch.observer  # noqa: SLF001 — то же

    before, after = live_port(loop, live_database, scenario)
    assert before is not None and after is not None
    assert after is not before, (
        "период средней сменился, а движок остался прежним — он идёт "
        "по средней, посчитанной наполовину по старому периоду"
    )


# ------------- остановка переживает смену торговой настройки (`D-033`)

#: Причина остановки, которую **не воспроизвести** прогревом. Дневной лимит
#: убытка на тех же барах сработал бы у нового движка снова и потому ничего
#: не доказывает: он вернулся бы и при полностью сломанной правке. Отказ
#: исполнителя лежит в прошлом, которого в барах нет, — если он пережил
#: пересборку, значит остановку помнит порт, а не движок.
HALT = (
    "Исполнитель не ответил на запрос сделок: TimeoutError: . Робот остановлен: "
    "решать по устаревшему состоянию — значит подать вторую заявку по тому же поводу."
)


async def _halt_the_live_run(port: HistoryPort) -> None:
    """Остановить живой ход так, как это делает беда в разборе.

    ⚠️ Остановка ставится в поле хода, и это не лень. Законного пути
    остановить движок в проверке сегодня нет: боевого исполнителя в программе
    нет вовсе, а исполнитель-тень заявки не отвергает никогда. Поле `_failure`
    — ровно то, что порт читает как «робот остановлен» (`LiveObserver.run.halted`),
    поэтому подмена идёт тем же путём, что и настоящая беда.
    """
    observer = port._watch.observer  # noqa: SLF001 — ход наружу порт не отдаёт
    assert observer is not None, "живой ход не собрался — останавливать нечего"
    observer._failure = HALT  # noqa: SLF001 — законного пути остановки сегодня нет
    # Проход, на котором порт заберёт остановку себе: она снимается с хода
    # до того, как ход будет закрыт.
    port.refresh("закрылся бар")
    await port.wait()


def test_changing_a_trading_setting_does_not_lift_a_halt(loop, live_database) -> None:
    """Остановленный робот остаётся остановленным после смены периода средней.

    Тот самый сценарий в бою: робот встал, владелец счёта меняет период
    средней — посмотреть, что было бы, — и робот торгует дальше на настоящем
    счёте. Решение 0014 обещает «возобновление только вручную».

    Причина взята **невоспроизводимая** (`HALT`): дневной лимит на тех же
    барах вернулся бы и без всякой правки, то есть ничего бы не доказал.

    ⚠️ Проверок две, и порознь каждая слаба. Мутация показала это прямо:
    снеси в `_advance` запрет собирать ход — и надпись «остановлен» останется
    на месте, потому что причину помнит порт, а движок при этом уже работает.
    Надпись без хода — ложь окну, ход без надписи — ложь владельцу счёта;
    остановка это и то и другое разом.
    """

    async def scenario(port: HistoryPort):
        await _halt_the_live_run(port)
        port.apply_settings(UiSettings().replace(average_period=20))
        await port.wait()
        state = port._last_state  # noqa: SLF001 — снимок наружу порт не отдаёт
        return (
            None if state is None else state.halted,
            port._watch.observer,  # noqa: SLF001 — ход наружу порт не отдаёт
        )

    said, observer = live_port(loop, live_database, scenario)
    assert said == HALT, (
        "окно перестало показывать остановку после смены торговой настройки: "
        "владелец счёта увидит работающего робота вместо остановленного"
    )
    assert observer is None, (
        "смена торговой настройки собрала движку новый ход: он пойдёт по барам "
        "и на реальном счёте продолжит торговать после остановки"
    )


def test_a_halted_robot_gets_no_live_run_at_all(loop, live_database) -> None:
    """Остановленному роботу живой ход не собирается заново — вовсе.

    Проверка устройства, а не текста: пока живого хода нет, подавать заявки
    неоткуда. Строка «остановлен» в снимке без этого держалась бы на том,
    что её не забыли переписать.
    """

    async def scenario(port: HistoryPort):
        await _halt_the_live_run(port)
        port.apply_settings(UiSettings().replace(average_period=20))
        await port.wait()
        port.refresh("закрылся ещё бар")
        await port.wait()
        return port._watch.observer  # noqa: SLF001 — ход наружу порт не отдаёт

    assert live_port(loop, live_database, scenario) is None, (
        "у остановленного робота собрался новый живой ход — он пойдёт "
        "по барам и начнёт принимать решения"
    )


def test_reconnecting_does_not_lift_a_halt(loop, live_database) -> None:
    """Кнопка связи остановку не снимает.

    Обрыв связи и переподключение — обычное дело в торговый день. Снимай
    остановку связь, «возобновление только вручную» держалось бы на том,
    что владелец счёта не нажмёт кнопку дважды.
    """

    async def scenario(port: HistoryPort):
        await _halt_the_live_run(port)
        port.stream(False)
        await port.wait()
        port.stream(True)
        await port.wait()
        state = port._last_state  # noqa: SLF001 — снимок наружу порт не отдаёт
        return None if state is None else state.halted

    assert live_port(loop, live_database, scenario) == HALT, (
        "переподключение сняло остановку робота"
    )


def test_the_reason_shown_is_the_first_one_not_the_latest(loop, live_database) -> None:
    """Показывается то, с чего всё началось, а не последняя беда.

    Движок останавливается один раз, но беда способна прийти второй — журнал
    перестал принимать строки уже после отказа исполнителя. Показав вторую,
    окно отправит владельца счёта лечить следствие.
    """
    later = "Журнал решений не принял строку: OSError: . Робот остановлен."

    async def scenario(port: HistoryPort):
        await _halt_the_live_run(port)
        # Вторая беда приходит тем же путём, что первая: ход отдаёт другую
        # причину на следующем проходе. Написать её прямо в поле порта
        # было бы проверкой присваивания — снимок состояния при этом
        # не пересобирается, и тест прошёл бы при любом порядке.
        observer = port._watch.observer  # noqa: SLF001 — ход наружу порт не отдаёт
        assert observer is not None
        observer._failure = later  # noqa: SLF001 — законного пути остановки сегодня нет
        port.refresh("закрылся ещё бар")
        await port.wait()
        state = port._last_state  # noqa: SLF001 — снимок наружу порт не отдаёт
        return None if state is None else state.halted

    assert live_port(loop, live_database, scenario) == HALT, (
        "вторая беда затёрла первую причину остановки: владельца счёта "
        "отправят лечить следствие вместо того, с чего всё началось"
    )


def test_the_journal_says_why_the_live_run_was_not_rebuilt(loop, live_database) -> None:
    """Молчания не бывает: сказано, что ход не пересобран и почему.

    Владелец счёта поменял настройку и ждёт увидеть живой ход по ней. Он
    видит прогон по истории и вправе знать, что это не одно и то же.
    """

    async def scenario(port: HistoryPort):
        await _halt_the_live_run(port)
        port.apply_settings(UiSettings().replace(average_period=20))
        await port.wait()
        return tuple(row.event for row in port._journal)  # noqa: SLF001 — журнал наружу отдаётся сигналом

    events = live_port(loop, live_database, scenario)
    assert "Живой ход не пересобран" in events, (
        f"о том, что ход не пересобран, не сказано ничего: {events}"
    )


def test_the_history_run_shown_while_halted_is_not_called_live(
    loop, live_database
) -> None:
    """Пометка строк честная: показан прогон по истории — так и помечен.

    Иначе окно говорит «симуляция на боевом потоке» ровно тогда, когда робот
    остановлен и никакой работы не ведёт.
    """

    async def scenario(port: HistoryPort):
        await _halt_the_live_run(port)
        port.apply_settings(UiSettings().replace(average_period=20))
        await port.wait()
        return port._origin  # noqa: SLF001 — пометка наружу порт не отдаёт

    assert live_port(loop, live_database, scenario) is WindowOrigin.BACKTEST, (
        "прогон по истории у остановленного робота помечен как работа "
        "робота на боевом потоке"
    )


def test_only_a_person_lifts_the_halt(loop, live_database) -> None:
    """Канарейка к пяти проверкам выше: снятие вообще работает.

    Без неё все они прошли бы и у порта, который остановку не снимает
    никогда, — а такой порт негоден: единственным способом вернуть робота
    в работу остался бы перезапуск программы.
    """

    async def scenario(port: HistoryPort):
        await _halt_the_live_run(port)
        port.resume()
        await port.wait()
        port.refresh("закрылся бар после снятия")
        await port.wait()
        state = port._last_state  # noqa: SLF001 — снимок наружу порт не отдаёт
        return (
            "" if state is None else state.halted,
            port._watch.observer is not None,  # noqa: SLF001 — ход наружу порт не отдаёт
        )

    halted, running = live_port(loop, live_database, scenario)
    assert halted == "", f"остановка не снялась рукой владельца счёта: {halted}"
    assert running, "остановку сняли, а живой ход заново не собрался"


def test_lifting_a_halt_that_is_not_there_is_refused(loop, live_database) -> None:
    """Снимать нечего — сказано вслух, а не сделано молча.

    Кнопка «возобновить» у неостановленного робота не должна выглядеть
    сработавшей: иначе владелец счёта решит, что остановка была и снята.
    """

    async def scenario(port: HistoryPort):
        port.resume()
        await port.wait()
        # Читаются строки самой программы: отказ прогона не запускает,
        # а `_journal` пересобирается только проходом.
        return tuple(
            (row.event, row.reason) for row in port._notes  # noqa: SLF001 — строки программы наружу отдаются сигналом
        )

    rows = live_port(loop, live_database, scenario)
    said = [reason for event, reason in rows if event == "Возобновление"]
    assert said and "снимать нечего" in said[0], (
        f"о том, что снимать нечего, не сказано: {rows}"
    )
