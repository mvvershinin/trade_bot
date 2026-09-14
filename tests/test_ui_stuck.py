"""Долгая работа не отвечает: что об этом узнаёт человек. `B-025`.

Поймано на владельце счёта 06.09.2026. Он запустил прогон по истории, потом
сменил настройки — **не произошло ничего, без единого сообщения на экране**.
Программа при этом жила: ошибка повторного входа (`B-026`) оставила задачу
`HistoryPort._refresh` навсегда незавершённой, и дальше сработало то, что
написано в коде: перед каждой командой порт смотрит, не занят ли он, видит
незавершённую задачу и отказывает. Отказ до окна не доезжал.

Проверяется здесь **две** вещи, и вторая важнее первой.

1. Отказ доезжает до окна **словами**, и слова называют, чем порт занят
   и сколько уже. «Занято» без имени операции человеку не отвечает ни на что:
   он и так видит, что ничего не происходит.
2. Застревание **обнаруживается само**. Без этого первая половина делает
   хуже: окно скажет «занята» и будет показывать это вечно, а человек
   по-прежнему не будет знать, что делать.

⚠️ Проверка «команда отвергнута» здесь не годится и намеренно не пишется:
она зеленеет и при молчаливом отказе, то есть проверяет не то, ради чего
поставлена. Ломать надо так, чтобы **сообщение перестало доезжать до окна**;
каждый сторож ниже называет свою мутацию.

Все проверки идут на **настоящем** окне и **настоящем** порту: между ними
и лежит то, что ломалось. Подставная здесь только застрявшая задача — она
и есть вход, который в бою даёт беда.
"""

from __future__ import annotations

import ast
import asyncio
import os
import pathlib
import time
from collections.abc import Callable, Coroutine, Iterator
from typing import TypeVar

import pytest

os.environ.setdefault("QT_API", "pyside6")  # до первого импорта qasync

from market import MarketWorker
from market.journal import redact
from ui.models import DecisionLevel, DecisionRow, Settings

from app.port import _WORKS, HistoryPort

#: Срок молчания для проверок. Настоящий — тридцать секунд (`STUCK_AFTER`),
#: и ждать его в тесте нельзя: тест, ждущий полминуты, не запускают.
#: Частоту взгляда на часы порт выводит отсюда сам (`_Stuck.watching`).
STUCK_AFTER = 0.02

#: Предел терпения проверки: столько она ждёт обещанного сообщения, прежде
#: чем объявить, что его не будет. Не порог из продукта — тот в полсекунды
#: короче; здесь запас на нагрузку параллельного прогона.
PATIENCE = 3.0

#: Срок молчания для проверки, которая, наоборот, держит **исправную** работу
#: и следит, чтобы её не объявили застрявшей. Подлиннее умолчания: работа
#: отчитывается о ходе каждые `ALIVE_EVERY`, и промежуток обязан быть заметно
#: короче срока, иначе покраснеет от одной заминки машины.
PATIENT_AFTER = 0.2

#: Как часто исправная работа отчитывается о ходе в той же проверке.
ALIVE_EVERY = 0.02


class Bench:
    """Порт, окно и всё, что они друг другу сказали.

    Собирается из настоящих `HistoryPort` и `MainWindow`, потому что ломалось
    ровно между ними. База свечей не открывается ни разу: ни одна проверка
    здесь до чтения свечей не доходит — порт отказывает **до** работы.
    """

    def __init__(self, path: pathlib.Path, *, stuck_after: float) -> None:
        from ui.main_window import MainWindow

        self.worker = MarketWorker(path / "нет-такой-базы.sqlite3")
        self.port = HistoryPort(
            self.worker, values=Settings(), sanitize=redact, stuck_after=stuck_after
        )
        self.window = MainWindow(port=self.port, sanitize=redact)
        # Часы окна тикают раз в секунду и к делу не относятся; в прогоне
        # без экрана они только мешают разбирать очередь.
        self.window._timer.stop()  # noqa: SLF001 — так же делают соседние тесты
        self.said: list[str] = []
        self.window.statusBar().messageChanged.connect(self.said.append)

    def rows(self) -> list[DecisionRow]:
        """Строки журнала решений — из таблицы окна, а не из порта.

        Именно из таблицы: между портом и ею лежит доставка, и проверять
        надо то, что человек увидит глазами.
        """
        return self.window.journals.decisions_model.rows()

    def close(self) -> None:
        self.window.close()
        self.window.deleteLater()


@pytest.fixture
def benches(loop, qapp, tmp_path: pathlib.Path) -> Iterator[Callable[..., Bench]]:
    """Строитель окон с портом. Срок молчания задаёт тот, кто строит.

    Строитель, а не готовое окно: одной проверке нужен срок покороче
    (застревание надо дождаться), другой — подлиннее (исправную работу
    надо успеть подержать). Число в обеих названо на месте, рядом с тем,
    что оно значит.

    Уборка обязательна и своя: незакрытое окно переживает свой тест
    и разрушается в чужом, а незакрытый поток данных держит поток
    до конца прогона.
    """
    made: list[Bench] = []

    def build(*, stuck_after: float = STUCK_AFTER) -> Bench:
        one = Bench(tmp_path, stuck_after=stuck_after)
        made.append(one)
        return one

    try:
        yield build
    finally:
        for one in made:
            loop.run_until_complete(one.port.aclose())
            loop.run_until_complete(one.worker.close())
            one.close()
        qapp.processEvents()


async def _wedge(port: HistoryPort) -> asyncio.Task[None]:
    """Оставить задачу прогона висеть — ровно то, что сделала `B-026`.

    Заводится тем же путём, каким её заводит сам порт (`_begin`), иначе
    проверялась бы не та дорога: часы работы ставит именно он.
    """

    async def never() -> None:
        await asyncio.Event().wait()

    task = port._begin("run", never())  # noqa: SLF001 — так её заводит сам порт
    port._task = task  # noqa: SLF001 — то же поле, что у настоящего прогона
    await asyncio.sleep(0)  # дать задаче начаться
    return task


#: Что вернула корутина проверки. Обычно — текст, увиденный в окне.
_Seen = TypeVar("_Seen")


def _drive(
    loop: asyncio.AbstractEventLoop, work: Callable[[], Coroutine[object, object, _Seen]]
) -> _Seen:
    """Прокрутить корутину на цикле qasync — как это делает программа.

    Возвращает то, что вернула корутина. Нужно это ровно затем, чтобы
    проверка смотрела на состояние окна **изнутри** прогона, пока оно ещё
    то самое, а не пересматривала окно после — разбор в `_shown`.
    """
    return loop.run_until_complete(work())


async def _until(shown: Callable[[], bool], *, what: str) -> None:
    """Ждать, пока окно скажет (или перестанет говорить), но не дольше срока.

    Ожидание **по событию**, а не «поспать и понадеяться». Фиксированная
    пауза уже сделала этот файл мигающим: в одиночку зелёный, в компании
    полутора сотен тестов — красный, потому что таймер Qt под нагрузкой
    просыпается не за обещанные миллисекунды. Мигающий сторож хуже
    отсутствующего — его снимают, а не чинят.

    Срок здесь не порог из продукта, а предел терпения проверки: сторож
    обязан сработать за двадцатую долю этого времени, и если не сработал —
    он не сработает вовсе.
    """
    end = time.monotonic() + PATIENCE
    while time.monotonic() < end:
        if shown():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"не дождались: {what}")


async def _shown(bench: Bench) -> str:
    """Дождаться плашки о застревании и вернуть текст, **который в ней был**.

    Вернуть, а не оставить проверке дочитать самой, — в этом вся разница,
    и вот чем ошибались обе проверки ниже. Они делали так: дождаться плашки
    внутри корутины, снять задачу-затычку, выйти из цикла — и только потом
    посмотреть на текст. К этому моменту текста могло уже не быть, причём
    **по делу**: затычка снята, работа кончилась, сторож порта проснулся
    и честно убрал сообщение. Убирать его — его обязанность, и она отдельно
    стережётся (`test_the_notice_goes_away_when_the_work_finishes`).
    Проверка читала пустую плашку и падала словами «не сказано, чем работу
    остановить: ''» — то есть обвиняла окно в молчании ровно там, где окно
    сказало всё и вовремя.

    Замер 09.09.2026, полный прогон `-n auto`, шесть одинаковых тел в общей
    очереди: 27,33 мс — плашка показана, 27,35 мс — затычка снята, 28,08 мс —
    в окно пришла пустая строка, 28,10 мс — проверка прочла плашку. Попасть
    надо в зазор **0,75 мс**: в одиночку сторож не попал в него ни разу
    за десять заходов, под нагрузкой попал в одном теле из шести.

    ⚠️ Ждать здесь **появления** плашки, а не нужных слов в ней. Ожидание
    того же, что проверяет `assert`, сделало бы проверку тавтологией:
    выброшенный из `_stuck_words` совет дал бы не красное «совета нет»,
    а невнятное «не дождались», и это при том, что сообщение в окне есть.
    """
    await _until(
        lambda: bench.window.stuck_banner.isVisibleTo(bench.window),
        what="плашка о застревании не появилась",
    )
    return bench.window.stuck_banner.text()


# =========================================================================
# 1. Отказ доезжает до окна словами
# =========================================================================

def test_a_command_refused_by_a_wedged_task_reaches_the_window_by_name(
    benches, loop
) -> None:
    """Смена настроек при застрявшем прогоне: окно говорит, чем занято.

    Это буквальный случай владельца счёта: прогон висит, он меняет настройки,
    и раньше не происходило **ничего**.

    Мутация молчания: убрать `self._send(self.failed, reason)` из
    `HistoryPort._refuse_busy` — журнал по-прежнему полон, команда по-прежнему
    отвергнута, а тест обязан покраснеть.
    """
    bench = benches()

    async def work() -> None:
        task = await _wedge(bench.port)
        bench.port.apply_settings(Settings(average_period=20))
        await asyncio.sleep(0)
        task.cancel()

    _drive(loop, work)

    assert bench.said, "окно не сказало ни слова — ровно то, за что заведена B-025"
    named = [text for text in bench.said if "прогон робота по истории" in text]
    assert named, (
        "окно не назвало, чем программа занята; сказано было вот что: "
        f"{bench.said}"
    )
    assert any("идёт уже" in text for text in named), (
        f"окно не сказало, сколько работа уже идёт: {named}"
    )


def test_the_refusal_is_written_to_the_journal_once_and_not_every_bar(
    benches, loop
) -> None:
    """В журнал — одна строка на занятость, в окно — каждый отказ.

    Причина у обоих правил одна и та же, только с разных сторон: человек
    нажал сейчас и ответа ждёт сейчас, а `refresh` зовётся на каждом закрытом
    баре живого хода, и повтор одной строки похоронил бы её под собой же.

    Мутация: снять проверку `key not in self._stuck.refused` в `_refuse_busy`
    — журнал заполнится одинаковыми строками, тест покраснеет.
    """
    bench = benches()
    answered: list[str] = []

    async def work() -> None:
        task = await _wedge(bench.port)
        for period in (20, 21, 22):
            # Строка состояния гасится перед каждым нажатием: так проверяется
            # ответ **на это** нажатие, а не оставшийся от предыдущего.
            # Ровно то же делает Qt, когда истекают пятнадцать секунд показа.
            bench.window.statusBar().clearMessage()
            bench.port.apply_settings(Settings(average_period=period))
            await asyncio.sleep(0)
            answered.append(bench.window.statusBar().currentMessage())
        task.cancel()

    _drive(loop, work)

    busy = [row for row in bench.rows() if "Программа занята" in row.reason]
    assert len(busy) == 1, (
        f"строк о занятости в журнале {len(busy)}, а должна быть одна: "
        f"{[row.event for row in busy]}"
    )
    silent = [
        number for number, text in enumerate(answered, 1)
        if "прогон робота по истории" not in text
    ]
    assert not silent, (
        f"на нажатия {silent} окно не ответило ничем: {answered}"
    )


# =========================================================================
# 2. Застревание обнаруживается само
# =========================================================================

def test_a_task_that_never_finishes_says_so_by_itself(benches, loop) -> None:
    """Никто ничего не нажимал, а окно уже сказало: работа не отвечает.

    Главный сторож задачи. Без этой половины первая делает хуже: окно
    показывало бы «занята» вечно.

    Мутация молчания: убрать `self._send(self.stuck_changed, said)` из
    `HistoryPort._look_at_the_clock` — застревание по-прежнему обнаружено
    и записано в журнал, но до окна не доехало. Тест обязан покраснеть.

    Вторая мутация, на само обнаружение: не заводить таймер в `_begin`
    (или не звать `_look_at_the_clock` из `_stuck_tick`) — тест обязан
    покраснеть тоже.
    """
    bench = benches()

    async def work() -> str:
        task = await _wedge(bench.port)
        # Никаких команд: человек просто ждёт, а окно обязано заговорить само.
        # Текст снимается здесь же, пока работа ещё висит: почему — в `_shown`.
        text = await _shown(bench)
        task.cancel()
        return text

    text = _drive(loop, work)
    assert "прогон робота по истории" in text, f"не названа сама работа: {text!r}"
    assert "идёт уже" in text, f"не сказано, сколько она идёт: {text!r}"


def test_the_notice_says_what_the_person_can_do(benches, loop) -> None:
    """«Программа не отвечает» без продолжения — то же молчание, только вежливое.

    Владельцу счёта нужно знать, ждать ему, отменять или перезапускать.

    Мутация: выбросить из `_stuck_words` хвост про перезапуск — тест
    покраснеет.
    """
    bench = benches()

    async def work() -> str:
        task = await _wedge(bench.port)
        # Текст снимается до снятия затычки, а не после: почему — в `_shown`.
        text = await _shown(bench)
        task.cancel()
        return text

    text = _drive(loop, work)
    assert "«Отменить»" in text, f"не сказано, чем работу остановить: {text!r}"
    assert "закройте программу" in text.lower(), (
        f"не сказано, что делать, если отмена не помогла: {text!r}"
    )
    assert "не потеряются" in text, (
        f"не сказано, что перезапуск не стоит данных: {text!r}"
    )


def test_the_journal_keeps_a_warning_about_the_stuck_work(benches, loop) -> None:
    """Плашка гаснет, а разбирать случившееся потом придётся по журналу.

    Мутация: понизить уровень строки до `INFO` — тест покраснеет.
    """
    bench = benches()

    def written() -> bool:
        return any(row.event == "Операция не отвечает" for row in bench.rows())

    async def work() -> None:
        task = await _wedge(bench.port)
        await _until(written, what="в журнале нет строки о застревании")
        task.cancel()

    _drive(loop, work)

    stuck = [row for row in bench.rows() if row.event == "Операция не отвечает"]
    assert stuck[0].level is DecisionLevel.WARNING, (
        f"строка о застревании записана уровнем {stuck[0].level}"
    )


def test_the_notice_is_said_once_and_not_on_every_tick(benches, loop) -> None:
    """Сторож смотрит на часы раз в тик, а говорит однажды.

    Иначе он завалил бы журнал одной и той же строкой и похоронил её
    под собой же.

    Мутация: снять `self._stuck.told` из `_look_at_the_clock` — строк станет
    столько, сколько было тиков.
    """
    bench = benches()

    async def work() -> None:
        task = await _wedge(bench.port)
        await _until(
            lambda: bench.window.stuck_banner.isVisibleTo(bench.window),
            what="плашка о застревании не появилась",
        )
        # Ещё десяток тиков сторожа поверх первого: столько же строк
        # в журнале и должно **не** появиться.
        await asyncio.sleep(STUCK_AFTER * 10)
        task.cancel()

    _drive(loop, work)

    stuck = [row for row in bench.rows() if row.event == "Операция не отвечает"]
    assert len(stuck) == 1, f"строк о застревании {len(stuck)}, а должна быть одна"


def test_the_notice_goes_away_when_the_work_finishes(benches, loop) -> None:
    """Сообщение о застревании снимается само, когда работа кончилась.

    Плашка, которую нечем убрать, врёт ровно так же, как молчание: человек
    видит «не отвечает» на работающей программе.

    Мутация: не отправлять пустую строку в `_look_at_the_clock` (например,
    слать текст только когда `stuck` не пуст) — плашка останется висеть,
    тест покраснеет.
    """
    bench = benches()

    async def work() -> None:
        task = await _wedge(bench.port)
        await _until(
            lambda: bench.window.stuck_banner.isVisibleTo(bench.window),
            what="плашка не появилась — проверять снятие нечего",
        )
        task.cancel()
        await _until(
            lambda: not bench.window.stuck_banner.isVisibleTo(bench.window),
            what="плашка осталась висеть на работающей программе",
        )

    _drive(loop, work)


def test_the_notice_reaches_the_progress_dialog_that_covers_the_window(
    benches, loop
) -> None:
    """Во время прогона окно закрыто модальной полоской хода.

    Плашка за ней человеку не видна вовсе, поэтому сообщение идёт и в подпись
    полоски. Это не украшение: полоска — единственное, что владелец счёта
    видит во время прогона на истории, и застревает как раз прогон.

    Мутация: убрать из `MainWindow.show_stuck` проход по полоскам — тест
    покраснеет.
    """
    bench = benches()

    async def work() -> None:
        bench.window._start_progress()  # noqa: SLF001 — то же делает кнопка прогона
        task = await _wedge(bench.port)
        await _until(
            lambda: "прогон робота по истории" in bench.window._progress.labelText(),  # noqa: SLF001
            what="полоска хода не сказала о застревании",
        )
        task.cancel()

    _drive(loop, work)


def test_the_progress_bar_wraps_the_notice_instead_of_cutting_it(
    benches, loop
) -> None:
    """Сообщение в полоске хода переносится по словам, а не уезжает за экран.

    Замер 06.09.2026 на том же тексте: с переносом полоска просит 576 точек
    ширины, без переноса — **2256**, то есть шире любого экрана ноутбука.
    Снимок без переноса показал фразу, обрезанную на середине, — сообщение,
    которого нет.

    Мутация: убрать `wrapping.setWordWrap(True)` из `MainWindow._bar` —
    тест покраснеет.
    """
    from PySide6.QtWidgets import QLabel

    bench = benches()

    async def work() -> None:
        bench.window._start_progress()  # noqa: SLF001 — то же делает кнопка прогона
        task = await _wedge(bench.port)
        await _until(
            lambda: "прогон робота по истории" in bench.window._progress.labelText(),  # noqa: SLF001
            what="полоска хода не сказала о застревании",
        )
        task.cancel()

    _drive(loop, work)

    dialog = bench.window._progress  # noqa: SLF001 — читаем то, что видит человек
    label = dialog.findChild(QLabel)
    assert label is not None and label.wordWrap(), (
        "подпись полоски не переносится по словам"
    )
    assert dialog.sizeHint().width() <= 900, (
        "полоска с сообщением о застревании просит "
        f"{dialog.sizeHint().width()} точек ширины — текст уедет за край экрана"
    )


def test_a_healthy_wait_is_not_called_stuck(benches, loop) -> None:
    """Работа, подающая признаки жизни, застрявшей не считается.

    Ложная тревога хуже молчания: человек перестаёт читать сообщения. Прогон
    на годе минуток идёт 7 секунд (замер 06.09.2026) и всё это время шлёт
    долю сделанного — объявить его застрявшим нельзя.

    Срок здесь длиннее умолчания файла, и это не поблажка: работа обязана
    прожить **дольше** срока, иначе проверять нечего, а отчитываться —
    заметно чаще него, иначе покраснеет от одной заминки машины.

    Мутация: убрать `self._alive("run")` из `HistoryPort._progress` — работа,
    которая исправно отчитывается, будет объявлена застрявшей, тест
    покраснеет.
    """
    bench = benches(stuck_after=PATIENT_AFTER)
    rounds = round(PATIENT_AFTER * 1.5 / ALIVE_EVERY)

    async def work() -> None:
        task = await _wedge(bench.port)
        for _ in range(rounds):
            await asyncio.sleep(ALIVE_EVERY)
            bench.port._progress(1, 10)  # noqa: SLF001 — так отчитывается прогон
        task.cancel()

    _drive(loop, work)

    assert not bench.window.stuck_banner.isVisibleTo(bench.window), (
        "исправная работа, отчитывающаяся о ходе, объявлена застрявшей: "
        f"{bench.window.stuck_banner.text()!r}"
    )
    assert not [row for row in bench.rows() if row.event == "Операция не отвечает"]


# =========================================================================
# 3. Таблица работ полна
# =========================================================================

def test_every_task_of_the_port_goes_through_the_clock() -> None:
    """Задача, заведённая мимо `_begin`, — работа без часов и без имени.

    Проверка по исходнику, а не по поведению: забыть здесь можно ровно одну
    строку, и ни один тест поведения об этом не скажет. Отказ по такой работе
    придёт без имени, а о застревании её не скажет никто — это и есть `B-025`
    в новом месте.
    """
    source = pathlib.Path(__file__).resolve().parent.parent / "app" / "port.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    inside_begin = {
        node
        for holder in ast.walk(tree)
        if isinstance(holder, ast.FunctionDef) and holder.name == "_begin"
        for node in ast.walk(holder)
    }
    culprits = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and ast.unparse(node.func).endswith("ensure_future")
        and node not in inside_begin
    ]
    assert not culprits, (
        "задача порта заводится мимо `_begin`, то есть без часов и без имени:\n  "
        + "\n  ".join(culprits)
    )


def test_the_table_of_works_covers_exactly_the_tasks_of_the_port(
    tmp_path: pathlib.Path,
) -> None:
    """Имена работ и задачи порта — одно и то же множество.

    Таблица без задачи даёт имя, за которым ничего нет; задача без таблицы —
    работу, о которой окну сказать нечем.
    """
    worker = MarketWorker(tmp_path / "нет-такой-базы.sqlite3")
    try:
        port = HistoryPort(worker, sanitize=redact)
        tasks = set(port._tasks())  # noqa: SLF001 — предмет проверки
    finally:
        worker._pool.shutdown(wait=False)  # noqa: SLF001 — база не открывалась
    named = {work.key for work in _WORKS}
    assert tasks == named, (
        f"таблица работ и задачи порта разошлись: только в таблице "
        f"{named - tasks}, только в порту {tasks - named}"
    )


def test_every_work_is_named_in_russian_and_has_a_deadline() -> None:
    """У каждой работы есть человеческое имя и срок молчания.

    Имя без срока — работа, о застревании которой не скажут; срок без имени
    — сообщение «не отвечает: run», которое человеку не отвечает ни на что.
    """
    for work in _WORKS:
        assert work.title and not work.title.isascii(), (
            f"работа {work.key} названа не по-русски: {work.title!r}"
        )
        assert work.limit > 0, f"у работы {work.key} нет срока молчания"
        assert work.key.isascii(), f"ключ работы не латиницей: {work.key!r}"
