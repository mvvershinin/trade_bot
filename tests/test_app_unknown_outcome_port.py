"""`D-086` со стороны порта: остановка снаружи движка и её снятие.

Отдельный файл, а не раздел в `tests/test_app_unknown_outcome_halts.py`,
и причина не в оформлении. Здесь нужен цикл событий поверх Qt — тот же,
что собирает `app/main.py`; соседнему файлу Qt не нужен вовсе, он гоняет
`asyncio.run`. Смешение двух циклов в одном файле уже роняло процесс
по SIGSEGV, причём **в чужом** файле и с плавающим местом падения
(разбор — в шапке `tests/test_app_observe_port.py`). Граница «кому нужен
Qt, кому нет» проведена там верно и повторяется здесь.

Что проверяется: `HistoryPort.halt` останавливает робота **тем же**
механизмом, что и движок (`_Halt`), говорит причину в журнал решений
уровнем ERROR, закрывает живой ход — и не снимается ни настройкой,
ни новым баром, а только рукой владельца счёта.

⛔ Движок про `OutcomeUnknown` здесь не узнаёт ничем: внутрь уходит
**строка** человеческим языком, а тип отказа остаётся в слое приложения
(`ARCHITECTURE.md` §1).

⚠️ Изоляция: своя база во временном каталоге, свой порт на каждую проверку,
сети нет. Проверять поодиночке:
`pytest tests/test_app_unknown_outcome_port.py::имя`.
"""

from __future__ import annotations

import dataclasses
import math
import os
import pathlib
from datetime import datetime, timedelta
from typing import Any

import pytest

os.environ.setdefault("QT_API", "pyside6")  # до первого импорта qasync

from app.halt import HALT_EVENT, halt_reason
from app.port import HistoryPort
from broker.errors import OutcomeUnknown
from market import MSK, Candle, CandleStore, MarketWorker, Source, Timeframe
from market.journal import redact
from ui.models import Settings as UiSettings

#: Слова самого отказа: указание владельцу счёта, что делать руками.
CHECK_WITH_THE_BROKER = "Проверьте заявки и позицию у брокера"


def lost_answer() -> OutcomeUnknown:
    """Отказ, изображающий потерянный ответ. Без единого признака секрета."""
    return OutcomeUnknown("/some/path: Timeout; исход операции неизвестен")


DAY = datetime(2026, 6, 19, 7, 0, tzinfo=MSK)  # пятница, до открытия окна


@pytest.fixture
def live_base(tmp_path: pathlib.Path) -> pathlib.Path:
    """Своя база минуток: хватает и на прогрев, и на живой ход."""
    path = tmp_path / "halt-live.sqlite3"
    minutes = [
        Candle(
            time=DAY + timedelta(minutes=index),
            open=100000.0 + 300.0 * math.sin(index / 9.0),
            high=100040.0 + 300.0 * math.sin(index / 9.0),
            low=99960.0 + 300.0 * math.sin(index / 9.0),
            close=100000.0 + 300.0 * math.sin(index / 9.0),
            volume=1.0,
            timeframe=Timeframe(1),
            filled_minutes=1,
        )
        for index in range(600)
    ]
    with CandleStore(path) as store:
        store.put_minutes("MXU6", minutes, Source.ISS)
    return path


def _with_live_port(loop, database: pathlib.Path, scenario):
    """Порт с включённым наблюдением; сценарий получает его и ждёт сам."""

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


@dataclasses.dataclass(frozen=True, slots=True)
class _Seen:
    """Что видно после остановки: окно, журнал и сам живой ход."""

    halted: str
    running: bool
    journal: tuple[tuple[str, str, Any], ...]


async def _halt_and_look(port: HistoryPort) -> _Seen:
    port.halt(HALT_EVENT, halt_reason(lost_answer()))
    await port.wait()
    port.refresh("закрылся бар после остановки")
    await port.wait()
    state = port._last_state  # noqa: SLF001 — снимок наружу порт не отдаёт
    return _Seen(
        halted="" if state is None else state.halted,
        running=port._watch.observer is not None,  # noqa: SLF001 — ход наружу порт не отдаёт
        journal=tuple(
            (row.event, row.reason, row.level)
            for row in port._notes  # noqa: SLF001 — строки программы наружу отдаются сигналом
        ),
    )


def test_the_port_halt_stops_the_live_run_and_shows_it(loop, live_base) -> None:
    """Остановка снаружи движка закрывает живой ход и видна в окне.

    ⚠️ Мутация **остановки**: убрать из `HistoryPort.halt` строку
    `self._watch.key = None`. Причина останется в `_Halt`, надпись
    в окне появится — а ход движка продолжит разбирать бары. Надпись
    без хода это ложь окну, ход без надписи — ложь владельцу счёта;
    остановка обязана быть и тем и другим разом.
    """
    seen = _with_live_port(loop, live_base, _halt_and_look)
    assert seen.halted, "окно показывает работающего робота после остановки"
    assert not seen.running, (
        "живой ход остался собран: движок пойдёт по барам и продолжит решать "
        "после того, как исход команды брокеру стал неизвестен"
    )


def test_the_port_halt_says_the_words_to_the_journal(loop, live_base) -> None:
    """И говорит человеку — строкой уровня ERROR, а не молча.

    ⚠️ Мутация **молчания**: убрать `self.note(...)` из `HistoryPort.halt`.
    Робот встанет, ход закроется, окно покажет причину в панели — а в журнале
    решений, который владелец счёта читает и уносит в отчёт, не будет ничего.
    Именно так выглядят три случая правила 13, стоившие суток разбора.
    """
    seen = _with_live_port(loop, live_base, _halt_and_look)
    rows = [row for row in seen.journal if row[0] == HALT_EVENT]
    assert rows, (
        f"в журнале решений про остановку не сказано ничего: {seen.journal}"
    )
    assert CHECK_WITH_THE_BROKER in rows[0][1], (
        f"строка журнала не говорит, что проверить у брокера: {rows[0]!r}"
    )
    assert str(rows[0][2]).endswith("ERROR"), (
        f"остановка сказана не тревожным уровнем: {rows[0][2]!r}"
    )


def test_the_port_halt_is_not_lifted_by_a_trading_setting(loop, live_base) -> None:
    """Остановка не снимается сама: ни настройкой, ни новым баром.

    Тот же сценарий, что у дневного лимита убытка (`D-033`): робот встал,
    владелец счёта поменял период средней — посмотреть, что было бы, —
    и робот пошёл торговать дальше. Здесь причина ещё хуже: программа
    не знает, что на счёте.
    """

    async def scenario(port: HistoryPort) -> tuple[str, bool]:
        port.halt(HALT_EVENT, halt_reason(lost_answer()))
        await port.wait()
        port.apply_settings(UiSettings().replace(average_period=20))
        await port.wait()
        state = port._last_state  # noqa: SLF001 — снимок наружу порт не отдаёт
        return (
            "" if state is None else state.halted,
            port._watch.observer is not None,  # noqa: SLF001 — ход наружу порт не отдаёт
        )

    halted, running = _with_live_port(loop, live_base, scenario)
    assert halted, "смена торговой настройки сняла остановку с окна"
    assert not running, (
        "смена торговой настройки собрала движку новый ход: он рождается "
        "с пустым `halted` и продолжит торговать"
    )


def test_only_a_hand_lifts_the_halt_that_came_from_the_broker(loop, live_base) -> None:
    """Канарейка к трём проверкам выше: снятие рукой вообще работает.

    Без неё они прошли бы и у порта, который остановку не снимает никогда,
    — а такой порт негоден: вернуть робота в работу можно было бы только
    перезапуском программы.
    """

    async def scenario(port: HistoryPort) -> tuple[str, bool]:
        port.halt(HALT_EVENT, halt_reason(lost_answer()))
        await port.wait()
        port.resume()
        await port.wait()
        port.refresh("закрылся бар после снятия")
        await port.wait()
        state = port._last_state  # noqa: SLF001 — снимок наружу порт не отдаёт
        return (
            "" if state is None else state.halted,
            port._watch.observer is not None,  # noqa: SLF001 — ход наружу порт не отдаёт
        )

    halted, running = _with_live_port(loop, live_base, scenario)
    assert halted == "", f"остановка не снялась рукой владельца счёта: {halted}"
    assert running, "остановку сняли, а живой ход заново не собрался"




# ------------------------------------- вторая причина на остановленном роботе


#: Первая беда: она приходит из движка, воспроизводится прогревом
#: и снимается решением человека «принимаю убыток». К позиции у брокера
#: отношения не имеет никакого — и в этом вся суть проверок ниже.
DAILY_LIMIT = "Дневной лимит убытка исчерпан: −4 200 ₽ при пределе 4 000 ₽."


async def _halt_by_the_engine(port: HistoryPort, reason: str) -> None:
    """Остановить робота так, как это делает движок, — полем живого хода.

    ⚠️ Причина ставится в поле наблюдателя, и это не лень. Законного пути
    довести движок до дневного лимита убытка в проверке сегодня нет:
    боевого исполнителя в программе нет вовсе. `_failure` — ровно то, что
    порт читает как «робот остановлен» (`LiveObserver.run.halted`), поэтому
    подмена идёт тем же путём, что и настоящая беда (тот же приём, что
    в `tests/test_app_observe_port.py`).
    """
    observer = port._watch.observer  # noqa: SLF001 — ход наружу порт не отдаёт
    assert observer is not None, "живой ход не собрался — останавливать нечего"
    observer._failure = reason  # noqa: SLF001 — законного пути остановки сегодня нет
    port.refresh("закрылся бар с дневным лимитом убытка")
    await port.wait()


def test_an_already_halted_robot_still_hears_about_an_unknown_outcome(
    loop, live_base
) -> None:
    """Вторая причина не глотается первой: про неизвестный исход сказано.

    Разбор в рублях (`D-086`, находка Н-1 аудита `/risk` 06.09.2026).
    11:20 робот встал по дневному лимиту убытка. 11:20:30 опрос счёта
    получает «исход команды брокеру неизвестен» — и до правки это не попадало
    **никуда**: ни строки в журнале решений, ни надписи в окне. Оставался
    один `log.warning` в техническом логе, которого владелец счёта не читает.

    Дневной лимит убытка и неизвестная позиция — разные беды, требующие
    разных действий: первая — согласиться с убытком, вторая — сходить
    к брокеру и посмотреть заявки и позицию.

    ⚠️ Мутация: вернуть в `HistoryPort.halt` первой строкой
    `if self._halt.causes: return` (прежнее «первая причина главнее»).
    Проверка обязана упасть **на том, что вторая причина исчезла**, —
    поэтому здесь ищется строка именно про неё, а не «журнал непустой»
    и не «функция что-то вернула».
    """

    async def scenario(port: HistoryPort) -> tuple[str, str, tuple[tuple[str, str], ...]]:
        await _halt_by_the_engine(port, DAILY_LIMIT)
        port.halt(HALT_EVENT, halt_reason(lost_answer()))
        await port.wait()
        state = port._last_state  # noqa: SLF001 — снимок наружу порт не отдаёт
        return (
            "" if state is None else state.halted,
            port.halted,
            tuple(
                (row.event, row.reason)
                for row in port._notes  # noqa: SLF001 — строки программы наружу отдаются сигналом
            ),
        )

    shown, asked, journal = _with_live_port(loop, live_base, scenario)
    rows = [row for row in journal if row[0] == HALT_EVENT]
    assert rows, (
        "про неизвестный исход команды на уже остановленном роботе "
        f"не сказано ни строки: {journal}"
    )
    assert CHECK_WITH_THE_BROKER in rows[0][1], (
        f"строка journal не говорит, что проверить у брокера: {rows[0]!r}"
    )
    assert CHECK_WITH_THE_BROKER in shown, (
        "окно показывает только первую причину остановки: неизвестная позиция "
        f"у брокера из него пропала — {shown!r}"
    )
    assert DAILY_LIMIT in shown, (
        f"вторая причина вытеснила первую вместо того, чтобы дописаться: {shown!r}"
    )
    assert CHECK_WITH_THE_BROKER in asked, (
        "порт на вопрос «остановлен?» отвечает без неизвестного исхода: "
        f"{asked!r}. Этот ответ читает жалоба про счёт (`FundsWatch._blind`)"
    )


def test_lifting_the_first_reason_leaves_the_unknown_outcome_standing(
    loop, live_base
) -> None:
    """Снятие дневного лимита не уносит с собой неизвестную позицию.

    Продолжение того же разбора. Владелец счёта читает «дневной лимит убытка
    исчерпан», соглашается с убытком и снимает остановку — а до правки
    `resume` делал `self._halt = _Halt()` и стирал **заодно** то, о чём
    человеку не говорили вовсе.

    ⚠️ Мутация: снять в `HistoryPort.resume` разбор по одной причине —
    вернуть `self._halt = _Halt()`. Проверка обязана упасть на том, что
    робот пошёл работать с неизвестной позицией у брокера.
    """

    async def scenario(
        port: HistoryPort,
    ) -> tuple[str, bool, tuple[tuple[str, str], ...]]:
        await _halt_by_the_engine(port, DAILY_LIMIT)
        port.halt(HALT_EVENT, halt_reason(lost_answer()))
        await port.wait()
        before = len(port._notes)  # noqa: SLF001 — строки программы наружу отдаются сигналом
        port.resume()
        await port.wait()
        port.refresh("закрылся бар после снятия")
        await port.wait()
        state = port._last_state  # noqa: SLF001 — снимок наружу порт не отдаёт
        return (
            "" if state is None else state.halted,
            port._watch.observer is not None,  # noqa: SLF001 — ход наружу порт не отдаёт
            tuple(
                (row.event, row.reason)
                for row in port._notes[before:]  # noqa: SLF001 — строки программы наружу отдаются сигналом
            ),
        )

    shown, running, after = _with_live_port(loop, live_base, scenario)
    assert CHECK_WITH_THE_BROKER in shown, (
        "снятие дневного лимита унесло с собой неизвестный исход команды: "
        f"окно показывает {shown!r}"
    )
    assert not running, (
        "живой ход собран заново: робот пошёл торговать, не зная своей "
        "позиции у брокера"
    )
    assert DAILY_LIMIT not in shown, (
        f"снятая рукой причина осталась висеть в окне: {shown!r}"
    )
    named = [row for row in after if CHECK_WITH_THE_BROKER in row[1]]
    assert named, (
        "в момент снятия человеку не сказано, какая причина осталась: "
        f"{after}"
    )


def test_two_reasons_need_two_liftings_and_then_the_robot_works(
    loop, live_base
) -> None:
    """Канарейка к двум проверкам выше: снять обе причины вообще возможно.

    Без неё обе прошли бы и у порта, который остановку не снимает никогда,
    — а такой порт негоден: вернуть робота в работу можно было бы только
    перезапуском программы, то есть тем самым способом, который стирает
    предохранитель без следа (`D-042`, `D-043`).
    """

    async def scenario(port: HistoryPort) -> tuple[str, bool]:
        await _halt_by_the_engine(port, DAILY_LIMIT)
        port.halt(HALT_EVENT, halt_reason(lost_answer()))
        await port.wait()
        port.resume()
        await port.wait()
        port.resume()
        await port.wait()
        port.refresh("закрылся бар после снятия обеих причин")
        await port.wait()
        state = port._last_state  # noqa: SLF001 — снимок наружу порт не отдаёт
        return (
            "" if state is None else state.halted,
            port._watch.observer is not None,  # noqa: SLF001 — ход наружу порт не отдаёт
        )

    shown, running = _with_live_port(loop, live_base, scenario)
    assert shown == "", f"обе причины сняты, а окно показывает остановку: {shown!r}"
    assert running, "обе причины сняты, а живой ход заново не собрался"


def test_the_same_reason_twice_is_said_to_the_person_once(loop, live_base) -> None:
    """Один и тот же неизвестный исход не заливает журнал каждые полминуты.

    Опрос счёта после остановки заход не прекращает (`FundsWatch._ride`)
    и зовёт `halt` тем же текстом каждые тридцать секунд. Сто двадцать
    одинаковых строк в час хоронят под собой всё остальное — то самое
    молчание, только наоборот.

    ⚠️ Проверка держится на том, что текст причины — константа
    (`app/halt.py`): дедупликация точная, по строке.
    """

    async def scenario(port: HistoryPort) -> tuple[tuple[str, str], ...]:
        port.halt(HALT_EVENT, halt_reason(lost_answer()))
        await port.wait()
        port.halt(HALT_EVENT, halt_reason(lost_answer()))
        await port.wait()
        return tuple(
            (row.event, row.reason)
            for row in port._notes  # noqa: SLF001 — строки программы наружу отдаются сигналом
        )

    journal = _with_live_port(loop, live_base, scenario)
    rows = [row for row in journal if row[0] == HALT_EVENT]
    assert len(rows) == 1, (
        f"одна и та же причина сказана {len(rows)} раз(а): {rows}"
    )
