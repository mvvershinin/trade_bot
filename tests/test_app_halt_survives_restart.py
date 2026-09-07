"""`D-043` и `D-042`: остановка переживает перезапуск, снимает её человек.

Что здесь стережётся
--------------------
До 07.09.2026 остановка робота жила в памяти порта. Робот, вставший
по дневному лимиту в 11:20, после перезапуска в 11:25 шёл торговать снова,
а кнопки снятия в окне не было вовсе — то есть **перезапуск и был
единственным доступным человеку действием**. Предохранитель отменялся тем
самым движением, которым его пытались обойти (аудит `/risk` 06.09.2026 по
`D-086`).

Файл проверяет четыре вещи, и каждая ломается своей мутацией:

* причины переживают перезапуск **все**, а не первая;
* снятие берёт **одну**, самую раннюю, и говорит про оставшиеся;
* про восстановленную остановку сказано **при запуске**, а не тогда, когда
  человек нажмёт «Старт»;
* окно после перезапуска показывает остановленного робота, а не работающего.

⚠️ Цикл событий здесь нужен настоящий, поверх Qt, — тот же, что собирает
`app/main.py`. Граница «кому нужен Qt, кому нет» проведена так же, как
в `tests/test_app_unknown_outcome_port.py`, и по той же причине: смешение
двух циклов в одном файле уже роняло процесс по SIGSEGV.

⚠️ Изоляция: своя база во временном каталоге, свой порт на каждую проверку,
сети нет. Проверять поодиночке:
`pytest tests/test_app_halt_survives_restart.py::имя`.
"""

from __future__ import annotations

import dataclasses
import math
import os
import pathlib
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, TypeVar, cast

import pytest

os.environ.setdefault("QT_API", "pyside6")  # до первого импорта qasync

from app.halt import HALT_EVENT, halt_reason
from app.main import _restore_halt
from app.port import HistoryPort
from broker.errors import OutcomeUnknown
from market import MSK, Candle, CandleStore, MarketWorker, Source, Timeframe
from market.journal import redact
from ui.models import DecisionLevel
from ui.models import Settings as UiSettings

#: Сценарий одного запуска: получает порт, ждёт сам, отдаёт, что увидел.
_T = TypeVar("_T")
_Scenario = Callable[[HistoryPort], Awaitable[_T]]

DAY = datetime(2026, 6, 19, 7, 0, tzinfo=MSK)  # пятница, до открытия окна

#: Причина «из движка»: её текст движок склеивает сам, заголовка отдельно нет.
LIMIT = "Дневной лимит убытка. Достигнут предел −5 000 ₽ за день."


def lost_answer() -> OutcomeUnknown:
    """Отказ, изображающий потерянный ответ. Без единого признака секрета."""
    return OutcomeUnknown("/some/path: Timeout; исход операции неизвестен")


UNKNOWN = halt_reason(lost_answer())


@pytest.fixture
def live_base(tmp_path: pathlib.Path) -> pathlib.Path:
    """Своя база минуток: хватает и на прогрев, и на живой ход."""
    path = tmp_path / "halt-restart.sqlite3"
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


@dataclasses.dataclass(frozen=True, slots=True)
class _Seen:
    """Что видно после перезапуска: окно, журнал, ход и сама база."""

    halted: str
    causes: tuple[str, ...]
    kinds: tuple[str, ...]
    restored: tuple[bool, ...]
    running: bool
    journal: tuple[tuple[str, str, Any], ...]
    stored: tuple[str, ...]


async def _session(database: pathlib.Path, scenario: _Scenario[_T]) -> _T:
    """Один запуск программы: свой поток данных, свой порт, честное закрытие.

    Закрытие делается тем же порядком, что в `app/main.py`: сначала порт
    (`aclose` дожидается записи остановки в базу), потом поток данных.
    """
    worker = MarketWorker(database, sanitize=redact)
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


async def _halt_twice(port: HistoryPort) -> None:
    """Две беды подряд: сначала предохранитель робота, потом брокер."""
    port._remember_halt(LIMIT)  # noqa: SLF001 — дверь из движка наружу не открыта
    port.halt(HALT_EVENT, UNKNOWN)
    await port.wait()


def _look(port: HistoryPort, database: pathlib.Path) -> _Seen:
    """Снимок всего, что говорит про остановку: окно, журнал, ход, база."""
    state = port._last_state  # noqa: SLF001 — снимок наружу порт не отдаёт
    with CandleStore(database) as store:
        stored = tuple(cause.reason for cause in store.standing_halt())
    return _Seen(
        halted="" if state is None else state.halted,
        causes=() if state is None else tuple(c.reason for c in state.halt_causes),
        kinds=() if state is None else tuple(c.kind for c in state.halt_causes),
        restored=() if state is None else tuple(c.restored for c in state.halt_causes),
        running=port._watch.observer is not None,  # noqa: SLF001 — ход наружу не отдаётся
        journal=tuple(
            (row.event, row.reason, row.level)
            for row in port._notes  # noqa: SLF001 — строки программы уходят сигналом
        ),
        stored=stored,
    )


def _after_restart(loop, database: pathlib.Path, then=None) -> _Seen:
    """Остановить робота, закрыть программу, поднять заново и посмотреть.

    Второй запуск повторяет `app/main.py`: `restore_halt` до первого прогона,
    потом обычный прогон, который и собирает снимок для окна.
    """

    async def second(port: HistoryPort) -> _Seen:
        await _restore_halt(port, database)
        port.refresh("запуск программы")
        await port.wait()
        if then is not None:
            await then(port)
        return _look(port, database)

    async def go() -> _Seen:
        await _session(database, _halt_twice)
        return await _session(database, second)

    seen: _Seen = loop.run_until_complete(go())
    return seen


def test_all_the_reasons_come_back_after_a_restart(loop, live_base) -> None:
    """Обе причины переживают перезапуск, и робот остаётся остановленным.

    ⚠️ Мутация **несохранения**: убрать `self._write_halt(...)` из
    `HistoryPort._mark_halt`. Остановка будет видна в окне ровно до закрытия
    программы, а после перезапуска робот пойдёт работать — то самое `D-043`.

    ⚠️ Мутация **половины**: писать в базу только первую причину. Владелец
    счёта снимет дневной лимит и вместе с ним молча потеряет «исход команды
    брокеру неизвестен» — беду, которая требует похода к брокеру.
    """
    seen = _after_restart(loop, live_base)

    assert seen.halted, "после перезапуска окно показывает работающего робота"
    assert seen.causes == (LIMIT, UNKNOWN), (
        "причины пережили перезапуск не все или не в том порядке: "
        f"{[cause[:40] for cause in seen.causes]}"
    )
    assert seen.stored == (LIMIT, UNKNOWN), (
        f"в базе лежит не то, что показано в окне: {[c[:40] for c in seen.stored]}"
    )
    assert not seen.running, (
        "после перезапуска собран живой ход движка: он рождается с пустым "
        "`halted` и пойдёт разбирать бары, то есть торговать"
    )


def test_a_restored_halt_is_said_out_loud_at_startup(loop, live_base) -> None:
    """Про восстановленную остановку сказано при запуске, а не потом.

    ⚠️ Мутация **молчания**: убрать `self.note(...)` из
    `HistoryPort.restore_halt`. Остановка восстановится, окно покажет красную
    плашку — а в журнале решений, который владелец счёта читает и уносит
    в отчёт, про причину не будет ни строки, и «почему робот стоял со вчера»
    он будет искать сам.

    ⚠️ Проверка «остановка восстановилась» зеленеет и при молчаливом
    восстановлении, то есть стережёт не то. Здесь проверяется именно речь.
    """
    seen = _after_restart(loop, live_base)

    said = [row for row in seen.journal if "остановлен с прошлого запуска" in row[0]]
    assert said, (
        f"при запуске про остановку с прошлого раза не сказано: {seen.journal}"
    )
    event, reason, level = said[0]
    assert "2" in event, f"число причин в заголовке не названо: {event!r}"
    assert LIMIT in reason and UNKNOWN in reason, (
        f"строка запуска называет не все причины: {reason!r}"
    )
    assert "МСК" in reason, f"время остановки названо без пояса: {reason!r}"
    assert "Возобновить работу" in reason, (
        f"человеку не сказано, чем снимать остановку: {reason!r}"
    )
    assert level is DecisionLevel.ERROR, (
        f"остановка с прошлого запуска сказана не тревожным уровнем: {level!r}"
    )


def test_the_window_is_never_told_the_robot_works_over_a_stored_halt(
    loop, live_base
) -> None:
    """Окно после перезапуска показывает остановку, а не работу.

    ⚠️ Мутация **лжи**: вернуть в `HistoryPort._state` только `run.halted`
    (то есть остановку **прогона**), забыв про `self._halt.reason`.
    Показанный прогон по истории сам по себе не остановлен, и окно нарисует
    работающего робота ровно тогда, когда он стоит. На этой мутации уже
    обжигались 06.09.2026.

    ⚠️ Вторая половина той же лжи — список причин. Пустой список при непустой
    остановке гасит кнопку возобновления: человек видит запрет и не видит
    ни одного способа его снять, кроме перезапуска, который больше не снимает.
    """
    seen = _after_restart(loop, live_base)

    assert seen.halted, "снимок состояния говорит, что робот не остановлен"
    assert seen.causes, (
        "снимок не назвал ни одной причины поштучно: кнопка возобновления "
        "спрячется, и снять остановку станет нечем"
    )
    assert all(seen.restored), (
        f"причины не помечены как поднятые в прошлом сеансе: {seen.restored}"
    )
    assert seen.kinds == ("предохранитель робота", "состояние счёта неизвестно"), (
        f"вид остановки не пережил перезапуск: {seen.kinds}"
    )


def test_the_button_lifts_one_reason_and_leaves_the_rest(loop, live_base) -> None:
    """Одно нажатие снимает одну причину — самую раннюю. Робот стоит дальше.

    ⚠️ Мутация «снимать все разом»: заменить `drop_first()` на `causes.clear()`
    (и `lift_halt` на удаление всех строк). Владелец счёта соглашается
    с убытком — и заодно, не читая, снимает требование сходить к брокеру
    и посмотреть позицию, о которой программа не знает.
    """

    async def press_once(port: HistoryPort) -> None:
        port.resume()
        await port.wait()

    seen = _after_restart(loop, live_base, then=press_once)

    assert seen.causes == (UNKNOWN,), (
        "одно нажатие сняло не одну причину: "
        f"{[cause[:40] for cause in seen.causes]}"
    )
    assert seen.stored == (UNKNOWN,), (
        f"в базе после снятия осталось не то: {[c[:40] for c in seen.stored]}"
    )
    assert seen.halted, "робот заработал, хотя вторая причина остановки стоит"
    assert not seen.running, "живой ход собран при стоящей причине остановки"
    left = [row for row in seen.journal if row[0] == "Остановка снята не полностью"]
    assert left, f"про оставшуюся причину не сказано ни строки: {seen.journal}"
    assert UNKNOWN in left[0][1], (
        f"оставшаяся причина не названа целиком: {left[0][1]!r}"
    )


def test_the_last_lifting_lets_the_robot_work_again(loop, live_base) -> None:
    """Канарейка: снятие вообще работает и база пустеет до конца.

    Без неё проверки выше прошли бы и у порта, который не снимает остановку
    никогда, — а такой порт негоден: вернуть робота в работу было бы нечем.
    """

    async def press_twice(port: HistoryPort) -> None:
        port.resume()
        await port.wait()
        port.resume()
        await port.wait()

    seen = _after_restart(loop, live_base, then=press_twice)

    assert seen.causes == (), f"причины остались после двух снятий: {seen.causes}"
    assert seen.stored == (), f"в базе остались причины после снятия: {seen.stored}"
    assert not seen.halted, f"робот остался остановленным: {seen.halted!r}"


def test_a_lifted_reason_does_not_come_back_on_the_next_restart(
    loop, live_base
) -> None:
    """Снятая причина не встаёт заново при следующем запуске.

    Обратная сторона `D-043`: остановка обязана переживать перезапуск,
    но **снятая** остановка переживать его не имеет права. Иначе кнопка
    работает ровно до закрытия программы.
    """

    async def lift_all(port: HistoryPort) -> None:
        port.resume()
        await port.wait()
        port.resume()
        await port.wait()

    async def look_only(port: HistoryPort) -> _Seen:
        await _restore_halt(port, live_base)
        port.refresh("запуск программы")
        await port.wait()
        return _look(port, live_base)

    async def go() -> _Seen:
        await _session(live_base, _halt_twice)

        async def second(port: HistoryPort) -> None:
            await _restore_halt(port, live_base)
            await lift_all(port)

        await _session(live_base, second)
        return await _session(live_base, look_only)

    seen = loop.run_until_complete(go())
    assert seen.stored == (), f"снятые причины вернулись из базы: {seen.stored}"
    assert not seen.halted, f"снятая остановка встала заново: {seen.halted!r}"


def test_the_earliest_reason_is_lifted_first_after_a_restart(
    loop, live_base
) -> None:
    """Снимается та, что встала раньше, — и после перезапуска тоже.

    ⚠️ Мутация: `ORDER BY id DESC` в `CandleStore.standing_halt`. Порядок
    перевернётся, и первым снимется «исход команды брокеру неизвестен» —
    то есть ровно то, что человек обязан прочитать последним, разобравшись
    у брокера.
    """

    async def press_once(port: HistoryPort) -> None:
        port.resume()
        await port.wait()

    seen = _after_restart(loop, live_base, then=press_once)
    lifted = [row for row in seen.journal if row[0] == "Остановка снята не полностью"]
    assert lifted, f"снятия не случилось вовсе: {seen.journal}"
    assert LIMIT in lifted[0][1], (
        f"снята не самая ранняя причина: {lifted[0][1]!r}"
    )


def test_a_refused_write_says_the_halt_will_not_survive(loop, live_base) -> None:
    """База не приняла остановку — об этом сказано, а не промолчано.

    ⚠️ Мутация **молчания**: проглотить исключение в `HistoryPort._save_halt`.
    Робот остановлен, окно это показывает — а после перезапуска запрета
    не будет, и никто об этом не предупредил. Правило 13: отказ, не доехавший
    до человека, стоит дороже поломки.
    """

    async def scenario(port: HistoryPort) -> tuple[str, ...]:
        async def refuse(*args: object, **kwargs: object) -> None:
            raise OSError("database is locked")

        # Вход подставной (правило 14): база отвечает отказом, а не молчанием.
        setattr(port._worker, "raise_halt", refuse)  # noqa: SLF001,B010 — вход порта
        port.halt(HALT_EVENT, UNKNOWN)
        await port.wait()
        return tuple(
            f"{row.event}|{row.reason}|{row.level}"
            for row in port._notes  # noqa: SLF001 — строки программы уходят сигналом
        )

    said = loop.run_until_complete(_session(live_base, scenario))
    trouble = [row for row in said if row.startswith("Остановка робота и база|")]
    assert trouble, f"про отказ базы не сказано ни строки: {said}"
    assert "после перезапуска" in trouble[0], (
        f"последствие отказа не названо: {trouble[0]!r}"
    )
    assert "database is locked" in trouble[0], (
        f"причина отказа базы не названа: {trouble[0]!r}"
    )


def test_a_missing_base_is_not_created_by_the_startup_read(
    loop, tmp_path: pathlib.Path
) -> None:
    """Базы нет — вопроса про остановку нет, и файл не заводится.

    На чистой машине файла базы нет вовсе (`B-029`): `app/main.py` поэтому
    и поток данных не открывает. Вопрос про остановку завёл бы файл сам —
    и программа получила бы две правды о том, есть ли у неё база.
    """
    missing = tmp_path / "no-such.sqlite3"
    asked: list[str] = []

    class _Port:
        async def restore_halt(self) -> None:
            asked.append("спросили")

    # Подставной порт вместо настоящего: проверяется решение «спрашивать или
    # нет», а не то, что порт умеет читать базу (правило 14).
    port = cast(HistoryPort, _Port())
    loop.run_until_complete(_restore_halt(port, missing))

    assert asked == [], "остановку спросили у базы, которой нет"
    assert not missing.exists(), (
        "чтение остановки завело файл базы там, где его не было"
    )
