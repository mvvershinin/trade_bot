"""Живой ход движка: прогрев, стык, отсутствие заявок у брокера.

Что здесь проверяется, а что нет
--------------------------------
Ни один тест не утверждает, что робот принял **верное** решение: это дело
`engine/` и сверки с прототипом. Здесь проверяется ровно шов Э3:

* прогрев подан и непрерывен;
* на стыке базы и живого нет ни дубля, ни хода времени назад, а пропуск
  и изменение задним числом названы вслух;
* живой ход и прогон по истории на **одном и том же** ряде дают равный
  результат — иначе расхождение с историей означало бы не данные, а две
  разные машины;
* к брокеру не уходит ничего.

⚠️ Живого рынка здесь нет и быть не может: токен в разработке не участвует
(`CLAUDE.md`, правило 8). Приёмка этапа — глазами владельца счёта на живом
потоке. Эти тесты её не заменяют и заменить не могут; они ловят то, что
на живом потоке видно было бы уже деньгами.

⚠️ Сеть в этом файле не нужна ни одному тесту: `LiveObserver` не знает
ни брокера, ни биржи. Сторож на это — `test_the_module_cannot_reach_the_broker`,
и он смотрит на импорты, а не на поведение: модуль, который однажды получит
`broker/` в импортах, начнёт ходить в сеть раньше, чем это заметит любой
поведенческий тест.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import math
import pathlib
from collections.abc import Callable, Coroutine
from datetime import datetime, time, timedelta
from typing import cast

import pytest

from app.observe import RECHECK_BARS, WARMUP_BARS, LiveObserver, LiveSource
from backtest import Costs, replay
from engine import EngineSettings, Mode, TradingWindow
from market import MSK, Candle, Timeframe
from strategies import EmaReverse, EmaReverseSettings, Strategy
from ui.models import DecisionLevel

M5 = Timeframe(5)
DAY = datetime(2026, 6, 19, 7, 0, tzinfo=MSK)  # пятница, до открытия окна


# ------------------------------------------------------------------ обвязка


def bars(count: int = 120, start: datetime = DAY, step: int = 5) -> list[Candle]:
    """Ряд пятиминуток с колебанием вокруг ровной цены.

    Колебание нужно, чтобы средняя пересекалась и решения были: ровный ряд
    даёт ход без единой сделки, и сверять было бы нечего. Ни одно число
    не подбиралось под результат.
    """
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


def settings() -> EngineSettings:
    """Настройки движка с широким окном: иначе решений не будет вовсе.

    Режим назван явно: умолчание `EngineSettings` — «Выключен», и на нём ход
    не даёт ни одной сделки. Тест на таком умолчании был бы зелёным при любой
    поломке — сверять было бы нечего.
    """
    return EngineSettings(
        mode=Mode.REVERSE,
        window=TradingWindow(start=time(0, 0), end=time(23, 59)),
        take_profit_percent=0.5,
        commission_per_side=14.0,
    )


class Said:
    """Строки журнала, которые сказал наблюдатель."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, DecisionLevel]] = []

    def __call__(self, event: str, reason: str, level: DecisionLevel) -> None:
        self.rows.append((event, reason, level))

    def events(self) -> list[str]:
        return [event for event, _, _ in self.rows]

    def about(self, needle: str) -> list[tuple[str, str, DecisionLevel]]:
        return [row for row in self.rows if needle in row[0] or needle in row[1]]


def observer(said: Said | None = None) -> LiveObserver:
    """Наблюдатель на рабочих настройках. Издержки — как в прогоне по истории."""
    return LiveObserver(
        strategy=EmaReverse(EmaReverseSettings()),
        settings=settings(),
        say=said or Said(),
        costs=Costs(commission_per_side=14.0),
    )


#: Дольше этого ни один тест здесь не работает. Предел не про скорость:
#: живой ход синхронизируется признаком «всё разобрано», и поломка этого
#: признака даёт не красный тест, а **зависший** — то есть прогон, который
#: никогда не кончится и ничего не скажет. Проверено мутацией: лента,
#: переставшая сообщать о простое, вешает файл целиком.
DEADLINE = 20.0


@pytest.fixture(scope="module")
def loop():
    """Обычный цикл asyncio, **один на файл**. Qt здесь не участвует вовсе.

    ⚠️ Имя намеренно перекрывает общую фикстуру `loop` из `tests/conftest.py`,
    и это не случайность: та собирает цикл поверх приложения Qt, а здесь Qt
    не нужен вовсе. Перекрытие видно в этом файле и только в нём.

    Наблюдателю окно не нужно, поэтому он и не берёт цикл, связанный с Qt;
    проверки живого хода со стороны порта вынесены в
    `tests/test_app_observe_port.py`, где цикл собран так же, как в
    `app/main.py`.

    ⚠️ Один на файл, а не на тест, и это память о дорогой находке. Пока
    наблюдатель разбирал бары в фоновой задаче, эти проверки роняли процесс
    в SIGSEGV — не сами по себе, а в паре с `tests/test_app_port.py`, с
    плавающим местом падения внутри **чужого** файла. Причину сняли в самом
    наблюдателе (`LiveSource.candles`: лента отдаёт пришедшее и кончается,
    фоновой задачи нет), но привычка не плодить циклы событий на одном
    процессе остаётся — решение 0005 про то же.
    """
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def drive(loop):
    """Прогнать корутину в цикле файла. Предел времени — `DEADLINE`."""

    def run[T](work: Callable[[], Coroutine[object, object, T]]) -> T:
        return cast("T", loop.run_until_complete(asyncio.wait_for(work(), DEADLINE)))

    return run


# ------------------------------------------------------- прогрев и непрерывность


def test_the_warmup_is_delivered_whole_and_in_one_series(drive) -> None:
    """Стережёт: прогрев подан целиком, одним рядом, до первого живого бара."""
    said = Said()
    watch = observer(said)

    async def go():
        await watch.start()
        run = await watch.feed(bars(80))
        await watch.aclose()
        return run

    run = drive(go)
    assert run.bars == 80, (
        f"движок увидел {run.bars} баров из 80 — прогрев подан не целиком"
    )
    assert said.about("Прогрев"), "про прогрев не сказано ни слова"


def test_a_short_warmup_is_named_out_loud(drive) -> None:
    """Стережёт: прогрев короче `market.warmup_bars` — предупреждение, не тишина."""
    said = Said()
    watch = observer(said)

    async def go():
        await watch.start()
        await watch.feed(bars(WARMUP_BARS - 1))
        await watch.aclose()

    drive(go)
    short = [row for row in said.rows if "короче" in row[0]]
    assert short, f"о коротком прогреве не сказано: {said.events()}"
    assert short[0][2] is DecisionLevel.WARNING, "короткий прогрев сказан вполголоса"
    assert str(WARMUP_BARS) in short[0][1], "не названо, сколько баров нужно"


def test_the_warmup_is_said_once_and_not_on_every_bar(drive) -> None:
    """Стережёт: строка о прогреве не повторяется на каждом живом баре."""
    said = Said()
    watch = observer(said)
    series = bars(80)

    async def go():
        await watch.start()
        await watch.feed(series[:60])
        for index in range(60, 80):
            await watch.feed(series[: index + 1])
        await watch.aclose()

    drive(go)
    about = [row for row in said.rows if "рогрев" in row[0] or "рогрев" in row[1]]
    assert len(about) == 1, f"о прогреве сказано {len(about)} раз, ожидалась одна"


# ---------------------------------------------------------------------- стык


def test_the_seam_between_history_and_live_has_no_duplicate(drive) -> None:
    """Стережёт: бар, уже отданный движку, второй раз в ленту не попадает.

    Порт отдаёт **весь** отрезок на каждом проходе, поэтому повтор здесь —
    не редкий случай, а норма: без гашения движок получил бы сотни баров
    с временем назад и упал бы на первом же из них.
    """
    said = Said()
    watch = observer(said)
    series = bars(80)

    async def go():
        await watch.start()
        first = await watch.feed(series)
        again = await watch.feed(series)
        await watch.aclose()
        return first, again

    first, again = drive(go)
    assert again.bars == first.bars == 80, (
        f"повтор ряда добавил бары: было {first.bars}, стало {again.bars}"
    )
    assert again.deals == first.deals, "повтор ряда изменил список сделок"
    assert not said.about("Разрыв"), "повтор ряда объявлен разрывом"


def test_a_repeated_last_bar_after_a_break_gives_no_second_entry(drive) -> None:
    """Стережёт: повтор последнего бара после обрыва не даёт второго входа.

    Так ведёт себя поток брокера после переподключения (`engine/ports.py`).
    Второй вход по одному сигналу — это удвоенная позиция на счёте.
    """
    watch = observer()
    series = bars(80)

    async def go():
        await watch.start()
        before = await watch.feed(series)
        submitted = watch.submitted
        after = await watch.feed([*series, series[-1]])
        await watch.aclose()
        return before, after, submitted

    before, after, submitted = drive(go)
    assert after.bars == before.bars, "повторный бар вошёл в ряд"
    assert watch.submitted == submitted, (
        f"на повторе подано ещё {watch.submitted - submitted} заявок"
    )
    assert after.deals == before.deals, "повтор бара изменил сделки"


def test_a_bar_that_arrived_too_late_is_named_and_not_replayed(drive) -> None:
    """Стережёт: бар, ставший доверенным после ухода вперёд, назван вслух.

    Отдать его движку нельзя — это ход времени назад, порча ряда торгового
    модуля. Промолчать тоже нельзя: свеча не войдёт в среднюю никогда.
    """
    said = Said()
    watch = observer(said)
    series = bars(80)
    without = [*series[:70], *series[71:]]  # бар 70 порт отбросил как неполный

    async def go():
        await watch.start()
        await watch.feed(without)
        run = await watch.feed(series)  # догрузка подтвердила бар 70
        await watch.aclose()
        return run

    run = drive(go)
    assert run.bars == len(without), (
        "поздний бар вошёл в ряд — движку показали свечу с временем назад"
    )
    late = said.about("поздно")
    assert late, f"о позднем баре не сказано: {said.events()}"
    assert late[0][2] is DecisionLevel.WARNING


def test_a_bar_changed_after_the_decision_is_named(drive) -> None:
    """Стережёт: дозаполненный задним числом бар назван вслух.

    Догрузка пропущенных минут вправе изменить бар, по которому робот уже
    решил. Переиграть решение нельзя, промолчать — значит скрыть, что оно
    принято по неполным данным.
    """
    said = Said()
    watch = observer(said)
    series = bars(80)
    filled = list(series)
    filled[-2] = dataclasses.replace(
        series[-2], close=series[-2].close + 25.0, filled_minutes=5
    )
    thin = list(series)
    thin[-2] = dataclasses.replace(series[-2], filled_minutes=3)

    async def go():
        await watch.start()
        await watch.feed(thin)
        await watch.feed(filled)
        await watch.aclose()

    drive(go)
    changed = said.about("задним числом")
    assert changed, f"об изменившемся баре не сказано: {said.events()}"
    assert changed[0][2] is DecisionLevel.WARNING


def test_a_gap_in_the_live_part_is_named_but_the_warmup_is_quiet(drive) -> None:
    """Стережёт: разрыв в живой части — строка журнала, ночь в прогреве — нет.

    Разрыв в истории это ночь и выходные, и говорить о них нечего. Разрыв
    в живой части означает либо отсутствие сделок, либо отсутствие связи,
    и различить их по самому ряду нельзя — поэтому названы обе причины.
    """
    said = Said()
    watch = observer(said)
    day_one = bars(40)
    day_two = bars(40, start=DAY + timedelta(days=1))
    series = [*day_one, *day_two]

    async def go():
        await watch.start()
        await watch.feed(series)          # прогрев с ночью внутри
        assert not said.about("Разрыв"), "ночь в прогреве объявлена разрывом"
        tail = bars(43, start=DAY + timedelta(days=1))
        await watch.feed([*series, *tail[41:]])  # бар 40 второго дня пропущен
        await watch.aclose()

    drive(go)
    gaps = said.about("Разрыв")
    assert gaps, f"о разрыве в живой части не сказано: {said.events()}"
    assert "связи" in gaps[0][1] and "сделок" in gaps[0][1], (
        "строка о разрыве называет только одну из двух причин"
    )


def test_unsettled_bars_never_reach_the_engine(drive) -> None:
    """Стережёт: незакрытый бар в ленту не берётся (решение 0033)."""
    watch = observer()
    series = bars(60)
    growing = dataclasses.replace(
        series[-1].replace(time=series[-1].time + timedelta(minutes=5)),
        unsettled=True,
    )

    async def go():
        await watch.start()
        run = await watch.feed([*series, growing])
        await watch.aclose()
        return run

    run = drive(go)
    assert run.bars == 60, f"незакрытый бар вошёл в ряд: {run.bars} вместо 60"


def test_the_source_remembers_only_the_last_bars_and_stays_bounded() -> None:
    """Стережёт: память отпечатков ограничена `RECHECK_BARS` и не растёт."""
    source = LiveSource()
    source.offer(bars(RECHECK_BARS * 4))
    assert len(source._seen) == RECHECK_BARS, (  # noqa: SLF001 — размер памяти
        f"память отпечатков {len(source._seen)} вместо {RECHECK_BARS}"  # noqa: SLF001
    )


# ------------------------------------------------ живой ход == прогон по истории


def test_the_live_run_equals_the_history_replay_on_the_same_bars(drive) -> None:
    """Стережёт главное: тот же ряд даёт тот же результат обоими путями.

    Это приёмка Э3 без живого рынка. Живой ход и прогон по истории —
    разные машины (движок, созданный один раз и идущий по частям, против
    движка, созданного заново и прошедшего всё сразу), и расхождение между
    ними обязано означать расхождение **данных**, а не устройства. Сравнение
    идёт полем в поле: сделки, заявки, исполнения, средняя, журнал, итог,
    позиция, остановка, число баров.
    """
    series = bars(120)
    engine = settings()
    costs = Costs(commission_per_side=14.0)

    async def go():
        history = await replay(series, EmaReverse(EmaReverseSettings()), engine, costs=costs)
        watch = observer()
        await watch.start()
        # Прогрев половиной ряда, дальше — по бару, как в живом ходе.
        await watch.feed(series[:60])
        for index in range(60, len(series)):
            live = await watch.feed(series[: index + 1])
        await watch.aclose()
        return history, live

    history, live = drive(go)
    assert live.deals == history.deals, (
        f"сделок в живом ходе {len(live.deals)}, в прогоне по истории "
        f"{len(history.deals)} — пути разошлись"
    )
    assert live.plans == history.plans, "заявки разошлись"
    assert live.fills == history.fills, "исполнения разошлись"
    assert live.average == history.average, "средняя разошлась"
    assert live.journal == history.journal, "журнал решений разошёлся"
    assert live.summary == history.summary, "итог разошёлся"
    assert live.position == history.position, "позиция разошлась"
    assert live.halted == history.halted, "остановка разошлась"
    assert live.bars == history.bars, "число баров разошлось"
    assert history.deals, "ряд не дал ни одной сделки — сверять было бы нечего"


def test_the_live_run_matches_even_when_bars_come_one_at_a_time(drive) -> None:
    """Стережёт: результат не зависит от того, какими порциями поданы бары."""
    series = bars(90)
    engine = settings()

    async def go():
        history = await replay(series, EmaReverse(EmaReverseSettings()), engine)
        watch = observer()
        await watch.start()
        for index in range(len(series)):
            live = await watch.feed(series[: index + 1])
        await watch.aclose()
        return history, live

    history, live = drive(go)
    assert live.deals == history.deals, "подача по одному бару изменила сделки"
    assert live.bars == history.bars


# ------------------------------------------------------- ни одной заявки брокеру


def test_no_order_ever_leaves_for_the_broker(drive) -> None:
    """Стережёт: заявки подаются и остаются внутри — исполнителя-тени.

    Счётчик здесь обязателен: без него зелёным был бы и наблюдатель, который
    вообще ничего не делает.
    """
    watch = observer()

    async def go():
        await watch.start()
        run = await watch.feed(bars(120))
        await watch.aclose()
        return run

    run = drive(go)
    assert watch.submitted > 0, "не подано ни одной заявки — проверять нечего"
    assert run.deals, "исполнитель-тень не дал ни одной расчётной сделки"


def test_the_module_cannot_reach_the_broker() -> None:
    """Стережёт: `app/observe.py` не импортирует `broker/` ни одним именем.

    Проверка по разбору исходника, а не по поведению: поведенческий тест
    заметит поход в сеть только на той ветке, которую он выполнил, а импорт
    видно всегда. Ветка подачи боевой заявки не должна существовать вовсе.
    """
    source = pathlib.Path(__file__).resolve().parent.parent / "app" / "observe.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    reached: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            reached += [alias.name for alias in node.names if alias.name.startswith("broker")]
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("broker"):
            reached.append(node.module or "")
    assert not reached, (
        f"живой ход получил доступ к брокеру: {reached}. Наблюдение обязано "
        "быть неспособным подать заявку по устройству, а не по проверке"
    )


# --------------------------------------------------------------- беда и конец


def test_a_failed_run_says_so_and_does_not_hang_the_window(drive) -> None:
    """Стережёт: упавший ход говорит вслух, а `feed` не виснет навсегда.

    Ожидание в `feed` держится на том, что лента ставит признак «разобрано»
    перед каждым ожиданием, а ход — в `finally`. Сломав второе, получаем
    замерший интерфейс, а не красный тест, — поэтому проверка здесь.
    """
    said = Said()
    watch = observer(said)

    class Broken:
        """Торговый модуль, который падает на первой же свече.

        Порт стратегии соблюдён целиком, и падает он **изнутри** решения,
        а не отсутствием метода: модуль без `on_closed_bar` проверял бы
        сборку связки слоёв, а нужна ветка «ход упал по дороге».
        """

        title = "сломанный"

        def reset(self) -> None:
            pass

        def on_closed_bar(self, bar: object) -> object:
            raise RuntimeError("проверка: модуль сломан")

    broken = cast(Strategy, Broken())
    watch._strategy = broken  # noqa: SLF001 — подмена ради ветки отказа
    watch._engine._strategy = broken  # noqa: SLF001 — та же подмена в движке

    async def go():
        await watch.start()
        run = await asyncio.wait_for(watch.feed(bars(20)), timeout=5.0)
        await watch.aclose()
        return run

    run = drive(go)
    assert run.halted, "упавший ход не объявил себя остановленным"
    assert said.about("прекращ"), f"о падении не сказано: {said.events()}"
    assert any(row[2] is DecisionLevel.ERROR for row in said.rows), (
        "падение живого хода сказано не как ошибка"
    )


def test_feeding_a_closed_observer_returns_at_once(drive) -> None:
    """Стережёт: `feed` после закрытия хода возвращается, а не ждёт вечно."""
    watch = observer()

    async def go():
        await watch.start()
        await watch.feed(bars(30))
        await watch.aclose()
        return await asyncio.wait_for(watch.feed(bars(40)), timeout=5.0)

    run = drive(go)
    assert run.bars == 30, "закрытый ход продолжил разбирать бары"


def test_the_run_snapshot_is_taken_after_the_last_bar_is_decided(drive) -> None:
    """Стережёт: `feed` возвращает снимок, в который вошёл последний бар.

    Без ожидания окно рисовало бы бар, по которому решение ещё не принято:
    метка входа появлялась бы на баре позже свечи — ровно то расхождение,
    которое этап и должен ловить.
    """
    series = bars(120)
    watch = observer()

    async def go():
        await watch.start()
        seen = []
        for index in range(1, len(series) + 1):
            run = await watch.feed(series[:index])
            seen.append(run.bars)
        await watch.aclose()
        return seen

    seen = drive(go)
    assert seen == list(range(1, len(series) + 1)), (
        "снимок отстаёт от поданных баров: окно показало бы решение позже свечи"
    )


@pytest.mark.parametrize("count", [0, 1])
def test_an_empty_or_single_offer_does_not_break_the_run(count: int, drive) -> None:
    """Стережёт: пустой и однобаровый отрезок не роняют ход и не виснут."""
    watch = observer()

    async def go():
        await watch.start()
        run = await asyncio.wait_for(watch.feed(bars(count)), timeout=5.0)
        await watch.aclose()
        return run

    run = drive(go)
    assert run.bars == count
