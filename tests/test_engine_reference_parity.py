"""Сверка движка с архивом прототипа: 127 сделок построчно и два сторожа.

Что здесь закрывается
---------------------
**Главный пункт приёмки ТЗ §9.** Полный журнал самого прототипа — 127 сделок
конфигурации 09:30–11:30 с тейком 0,5% — воспроизводится **построчно и через
настоящий движок**: время входа, сторона, цена входа, время выхода, цена
выхода. 42 выхода из 127 идут по уровню тейка, три сделки открылись
и закрылись внутри одной свечи.

Это единственный оракул проекта с включённым тейком, и до Э1-8 он через
`engine/` не проходил ни разу: при Э1-6 те же строки сходились через отдельную
обвязку аудитора сверки (ROADMAP §9.5б).

⚠️ **После Э1-9 сверка идёт двумя путями, и оба обязательны.** Правила
исполнения — «по открытию следующей свечи» и «уровень сторожится внутри бара» —
переехали из тестовой оснастки в производственный слой `backtest/execution.py`,
и приёмка Э1-8 закрывала пару «движок плюс оснастка», а не движок с настоящим
портом (ROADMAP §9.6). Поэтому:

* `play()` — движок с оснасткой тестов, как было. Оснастка теперь наследует
  производственную модель и своих правил не имеет ни одного;
* `replayed()` — `backtest.replay` целиком, то есть ровно то, что зовёт
  программа. Этим путём сверяются ещё и **деньги**: валовая 6 479 ₽, комиссия
  3 556 ₽, чистая 2 923 ₽, профит-фактор и число прибыльных сделок.

**Две точки архива с выключенным тейком** остаются сторожами разбора свечи —
прогрева, торгового окна, снимка позиций, переворота через свечу, закрытия
по концу окна, исполнения по открытию следующей свечи:

============================  =======  ==========
Конфигурация                  Сделок   Прибыль ₽
============================  =======  ==========
09:30–11:30, будни, тейк 0        166      −5 400
без окна, будни, тейк 0         1 211     −54 550
============================  =======  ==========

⚠️ Закрыть ими Э1-8 нельзя: про тейк они не говорят ничего. Разошлись —
значит сломан разбор свечи, а не уровень.

Сверяются не только эти две цифры: число прибыльных сделок, худшая сделка
и число позиций, оставшихся открытыми, тоже берутся из архива.

Чего здесь нет и не будет
-------------------------
⚠️ **Скользящего тейка.** В прототипе его нет (PROTOTYPE.md §5), сверять
не с чем и не будет чем; `/parity` прогоняется с выключенным скользящим
(решение 0009). Его единственная проверка — приёмочное правило «едет только
в сторону прибыли и ни разу не откатился назад», и живёт она
в `tests/test_engine_take_profit.py`.

⚠️ **Режима «в одной свече».** Сделки в нём заведомо другие, и это не дефект:
у прототипа такого режима нет по построению.

Оракул и его пределы
--------------------
Числа архива записаны самим прототипом — это неоспоримая часть. Построчный
список сделок для этих двух точек не сохранился (`positions` в тех файлах
пуст), поэтому времена и цены каждой сделки сверяются с **производной
моделью** прототипа из локального архива. Модель и движок писались по одному
источнику: совпадение с ней доказывает согласованность, а не правильность
(PROTOTYPE.md §9). Совпадение с числами архива — доказывает.

Данные и модель лежат в `reference/`, он вне git. В свежем клоне этим тестам
заняться нечем, и они пропускаются: это ограничение, а не поломка.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, time, timedelta

import pytest

from backtest import Costs, Tariff, replay
from engine import AccountFunds, Engine, EngineSettings, Mode, TradingWindow
from strategies import EmaReverse, EmaReverseSettings
from engine import Fill, MarketCandle, OrderAction, OrderRequest
from tests.engine_helpers import MSK, FakeCandle, FakeTimeframe, NextOpenExecutor

REPO = pathlib.Path(__file__).resolve().parent.parent
DATASET = REPO / "reference/stand/install/data/Set_MoexFuturesMxu6/MXU6/Min5/MXU6.txt"
RESULTS = REPO / "reference/stand/results"
MODEL = REPO / "reference/parity-model"

FROM = datetime(2026, 6, 19, tzinfo=MSK)
TO = datetime(2026, 8, 26, 23, 59, tzinfo=MSK)
PERIOD = 15
ROBOT = "BreakEmaMoexFutures"
M5 = FakeTimeframe(5)

NO_ARCHIVE_HINT = (
    "нет локального архива reference/ — он в git не попадает (гигиена "
    "репозитория), в свежем клоне сверки нет"
)

#: Две точки архива, в которых тейк выключен. Параметры прогонов в файлах
#: не сохранены: восстановлены из меток в именах и обратным расчётом —
#: разбор в `reference/parity-model/verify.py`.
POINTS = {
    # Две точки с ВЫКЛЮЧЕННЫМ тейком. Про тейк они не говорят ничего,
    # но остаются сторожами разбора свечи.
    "09:30–11:30, будни, тейк 0": (
        "screen_MXU6_Min5_2026-06-19_2026-08-26_c0.01.json",
        TradingWindow(time(9, 30), time(11, 30)), False, 0.0,
    ),
    "без окна, будни, тейк 0": (
        "screen_MXU6_Min5_2026-06-19_2026-08-26_c0f14_Trad0000_Trad0000.json",
        TradingWindow(time(0, 0), time(0, 0)), False, 0.0,
    ),
    # Три точки с ВКЛЮЧЁННЫМ тейком, включая боевую конфигурацию ТЗ.
    # Списка сделок для них не сохранилось — сверяются пары чисел из архива.
    "10:05–11:00 + выходные (боевая), тейк 0,5%": (
        "screen_MXU6_Min5_2026-06-19_2026-08-26_c0f14_Trad1005_Trad1100_Tradtrue.json",
        TradingWindow(time(10, 5), time(11, 0)), True, 0.5,
    ),
    "10:05–11:00, будни, тейк 0,5%": (
        "screen_MXU6_Min5_2026-06-19_2026-08-26_c0f14_Trad1005_Trad1100.json",
        TradingWindow(time(10, 5), time(11, 0)), False, 0.5,
    ),
    "09:30–11:30 + выходные, тейк 0,5%": (
        "screen_MXU6_Min5_2026-06-19_2026-08-26_c0f14_Tradtrue.json",
        TradingWindow(time(9, 30), time(11, 30)), True, 0.5,
    ),
}


@pytest.fixture(scope="module")
def candles() -> list[FakeCandle]:
    """Свечи отрезка. Формат строки: `ГГГГММДД,ЧЧММСС,o,h,l,c,v,0`.

    В файле хранится **начало** свечи; время закрытия считает движок.
    Отрезок режется по времени начала — так же, как его получал прототип.

    ⚠️ Ряд **не сортируется**. Сортировка здесь чинила бы данные под тест:
    файл, поехавший назад по времени, — это порча, на которой производственный
    путь обязан падать (торговый модуль отвергает ход времени назад
    исключением). Отсортировав его в подставке, мы бы проверяли ряд, которого
    движок в бою не увидит, и заодно спрятали бы порчу от самой сверки.
    """
    if not DATASET.is_file():
        pytest.skip(NO_ARCHIVE_HINT)
    rows: list[FakeCandle] = []
    for line in DATASET.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split(",")
        if len(parts) < 6:
            continue
        moment = datetime.strptime(parts[0] + parts[1], "%Y%m%d%H%M%S").replace(
            tzinfo=MSK
        )
        if FROM <= moment <= TO:
            rows.append(FakeCandle(
                time=moment,
                open=float(parts[2]), high=float(parts[3]),
                low=float(parts[4]), close=float(parts[5]),
                volume=float(parts[6]) if len(parts) > 6 else 0.0,
                timeframe=M5, filled_minutes=5,
            ))
    assert rows, NO_ARCHIVE_HINT
    return rows


def archive(file: str) -> dict:
    path = RESULTS / file
    if not path.is_file():
        pytest.skip(NO_ARCHIVE_HINT)
    for row in json.loads(path.read_text(encoding="utf-8"))["rating"]:
        if row["robot"] == ROBOT:
            return row
    raise AssertionError(f"в {file} нет записи прототипа")


def play(
    candles,
    window: TradingWindow,
    weekend: bool,
    take: float = 0.0,
    stop_after: bool = True,
    *,
    module: EmaReverseSettings | None = None,
):
    """Прогон движка через оба порта. Возвращает сделки и остаток позиции.

    ⚠️ Прогон обязан **дойти до конца**. Остановка робота в середине оборвёт
    его тихо, и сверка скажет «1 205 сделок вместо 1 211» — причина окажется
    за один шаг от симптома, а искать её будут в торговой логике.

    `module` — настройки торгового модуля. Нужен ровно одному месту: проверке,
    что сверка **краснеет**, если умолчание фильтра против пилы сдвинуть.
    Умолчание здесь то же, что у самой сверки, — период 15 и оба фильтра
    выключены.
    """
    engine = Engine(
        EmaReverse(EmaReverseSettings(period=PERIOD) if module is None else module),
        EngineSettings(
            mode=Mode.REVERSE, window=window, close_on_time_end=True,
            trade_in_weekend=weekend, volume=1.0,
            stop_after_take_profit=stop_after,
            take_profit=take > 0, take_profit_percent=take,
        ),
    )
    executor = CountingGuards()

    async def scenario() -> None:
        for item in candles:
            await engine.on_market_candle(item, executor)

    asyncio.run(scenario())
    assert not engine.halted, (
        "робот остановился посреди сверки, и дальше сравнивать нечего: "
        + engine.halted
    )
    assert not executor.orphans, (
        f"сторожимых уровней пережило свою позицию: {len(executor.orphans)}"
    )
    if take > 0:
        assert executor.cancels > 0 and executor.arms > 0, (
            "за весь прогон не случилось ни одного снятия или вооружения — "
            "инвариант живых заявок проверен вхолостую"
        )
        # ⚠️ Максимум живых сторожей проверяется здесь, а не только ассертом
        # внутри подделки: замеренное число, которое никто не сравнивает,
        # со временем перестаёт что-либо значить.
        assert executor.most == 1, (
            f"максимум живых сторожей за прогон {executor.most}, а должен быть "
            "ровно один: ноль означает, что тейк не вооружался вовсе, больше "
            "одного — что заявки копятся у брокера"
        )
    else:
        assert executor.arms == 0 and executor.most == 0, (
            "тейк выключен, а вооружения всё равно подавались"
        )
    return executor.deals, engine.position


class CountingGuards(NextOpenExecutor):
    """Поддельный брокер, считающий **живые заявки**, а не вызовы.

    ⚠️ Требование к приёмке боевого адаптера, проверенное на потоке заявок
    настоящего прогона: на одну позицию в любой момент **не больше одного
    сторожимого уровня**, и ни один не переживает свою позицию.

    Замерено: движок подаёт сотни вооружений на сотню позиций. Адаптер,
    понявший вооружение как «поставить стоп-заявку», оставит у брокера
    за час позиции дюжину стопов: выход снимет один, рыночная заявка закроет
    позицию, а **одиннадцать оставшихся станут заявками на открытие**,
    каждая на полный объём в обратную сторону.

    Проверка живёт здесь, а не в синтетическом сценарии, ровно потому, что
    в синтетике снятий бывает ноль — и ветка снятия не выполняется ни разу.
    """

    def __init__(self, **rest: object) -> None:
        super().__init__(**rest)
        self.live: dict[str, float] = {}
        self.arms = 0
        self.cancels = 0
        self.most = 0

    async def submit(self, order: OrderRequest) -> None:
        if order.action is OrderAction.ARM_TAKE_PROFIT:
            self.live[order.order_id] = order.price or 0.0
            self.arms += 1
        elif order.action is OrderAction.CANCEL_TAKE_PROFIT:
            if self.live.pop(order.target_id or "", None) is not None:
                self.cancels += 1
        self.most = max(self.most, len(self.live))
        assert len(self.live) <= 1, (
            f"у брокера {len(self.live)} живых сторожей одновременно: {self.live}"
        )
        await super().submit(order)

    async def fills_at(self, candle: MarketCandle) -> Sequence[Fill]:
        fills = await super().fills_at(candle)
        for fill in fills:
            self.live.pop(fill.order_id, None)
            if fill.action is OrderAction.CLOSE and self.live:
                raise AssertionError(
                    f"позиция закрыта, а сторож остался живым: {self.live}"
                )
        return fills


def replayed(
    candles,
    window: TradingWindow,
    weekend: bool,
    take: float,
    *,
    costs: Costs | None = None,
):
    """Тот же прогон, но через **производственный** слой `backtest/` целиком.

    `play()` выше собирает движок с оснасткой тестов; здесь работает `replay` —
    ровно то, что зовёт программа, когда владелец счёта нажимает «прогнать
    по истории». Разница не косметическая: `replay` строит и деньги — комиссию,
    чистую прибыль, показатели за период, — а сделок в нём столько же и тех же.

    ⚠️ **Тариф ставится в оба места сразу.** Комиссия живёт в двух: движок
    по своей цифре судит, окупает ли цель тейка комиссию обеих сторон
    (ТЗ §4.4 В), отчёт по своей считает чистую прибыль. Здесь стояла одна —
    только у отчёта, — и прогон выдавал «комиссия 3 556 ₽, чистая 2 923 ₽»
    вместе с журналом, который на каждой позиции писал «окупает ли цель
    комиссию — НЕ ПРОВЕРЕНО, тариф движку не задан». Сегодня расхождение
    отвергает сам `replay`, и обходить его подстановкой нельзя.

    На список сделок цифра движка не влияет: `commission_per_side` читает
    только строка журнала про уровень (`engine/pipeline.py`, `_take_event`).
    Что это правда, а не обещание, проверяет
    `test_the_costs_change_the_money_but_not_a_single_deal` — там сделки
    сравниваются построчно между прогоном с тарифом и без.
    """
    settings = EngineSettings(
        mode=Mode.REVERSE, window=window, close_on_time_end=True,
        trade_in_weekend=weekend, volume=1.0, stop_after_take_profit=True,
        take_profit=take > 0, take_profit_percent=take,
        commission_per_side=None if costs is None else costs.per_side,
    )
    outcome = asyncio.run(replay(
        candles, EmaReverse(EmaReverseSettings(period=PERIOD)), settings, costs=costs
    ))
    assert not outcome.halted, (
        "робот остановился посреди сверки, и дальше сравнивать нечего: "
        + outcome.halted
    )
    return outcome


def profit(deals) -> float:
    """Зафиксированная прибыль в пунктах = рублях для этого инструмента."""
    return sum(
        (deal.exit_price - deal.entry_price)
        * (1 if deal.side.value == "long" else -1)
        * deal.volume
        for deal in deals
    )


def test_the_slice_is_the_one_the_document_describes(candles) -> None:
    """Отрезок не подменили: те же 10 878 свечей и та же первая.

    Без этой проверки «сошлось на всех свечах» можно получить на трёх.
    Тот же датасет с более ранней границы даёт другой список сделок целиком:
    к 19.06 средняя приходит уже прогретой (PROTOTYPE.md §3).
    """
    assert len(candles) == 10_878
    assert candles[0].time == datetime(2026, 6, 19, 8, 55, tzinfo=MSK)
    # Строго больше нуля: две свечи с одним временем — это не «порядок
    # соблюдён», а повтор, который сдвигает среднюю и прогрев.
    backwards = [
        f"{first.time} → {second.time}"
        for first, second in zip(candles, candles[1:])
        if second.time - first.time <= timedelta(0)
    ]
    assert not backwards, (
        "ряд эталонного датасета не возрастает строго:\n  "
        + "\n  ".join(backwards[:5])
    )


@pytest.mark.slow
@pytest.mark.parametrize("case", sorted(POINTS))
def test_the_engine_reproduces_the_archive_numbers(candles, case: str) -> None:
    """Число сделок, прибыль, прибыльных сделок, худшая сделка — как у прототипа.

    Комиссия здесь не сравнивается намеренно: на список сделок она не влияет,
    прототип её не читает вообще, и в этих двух прогонах тариф стоял разный
    (PROTOTYPE.md §6). Сделки совпали, а деньги нет — расходится комиссия;
    сделки разошлись — комиссия ни при чём.
    """
    file, window, weekend, take = POINTS[case]
    expected = archive(file)
    deals, left = play(candles, window, weekend, take)

    results = [
        (deal.exit_price - deal.entry_price) * (1 if deal.side.value == "long" else -1)
        for deal in deals
    ]
    assert len(deals) == expected["deals"], case
    assert profit(deals) == pytest.approx(expected["realized"]), case
    assert min(results) == pytest.approx(expected["worst_deal"]), case
    assert (1 if left is not None else 0) == expected["open_left"], case

    # ⚠️ «Прибыльных сделок» в архиве считает сама платформа и включает туда
    # позицию, оставшуюся открытой на конце отрезка, если её бумажная прибыль
    # положительна. У движка закрытых сделок на одну меньше — это разница
    # в определении, а не расхождение. Та же оговорка про открытую позицию
    # уже записана в PROTOTYPE.md §6 применительно к комиссии.
    open_winner = 1 if (left is not None and expected["paper"] > 0) else 0
    assert sum(1 for value in results if value > 0) + open_winner == expected["win"], case


#: Конфигурации, для которых числа прототипа в архиве не сохранены вовсе.
#: Сверяются только с производной моделью — и потому доказывают лишь
#: согласованность. Ветка выходных сессий у прототипа не проверялась построчно
#: ни разу: в единственном полном журнале выходных дней нет (PROTOTYPE.md §9).
MODEL_ONLY = {
    # Выключенное «стоп после тейка» прототипом не прогонялось ни разу:
    # восстановлено по исходнику и данными не подкреплено (PROTOTYPE.md §9).
    # Ветка важная — она третий из вариантов ТЗ §4.4 В, — и хоть какая-то
    # проверка у неё должна быть.
    "09:30–11:30, будни, тейк 0,5%, без стопа после тейка": (
        None, TradingWindow(time(9, 30), time(11, 30)), False, 0.5, False,
    ),
}


@pytest.mark.slow
@pytest.mark.parametrize("case", sorted(POINTS) + sorted(MODEL_ONLY))
def test_the_engine_matches_the_derived_model_deal_by_deal(candles, case: str) -> None:
    """Каждая сделка: время входа, сторона, цена входа, время выхода, цена выхода.

    ⚠️ Оракул здесь **производный** — модель прототипа из локального архива,
    а не сам прототип. Она и движок писались по одному источнику: совпадение
    доказывает согласованность, а не правильность (PROTOTYPE.md §9).
    Неоспоримая часть сверки — числа архива в тесте выше.

    Конфигурация с выходными сессиями сверяется только так: числа прототипа
    для неё при выключенном тейке в архиве не сохранены.
    """
    if not (MODEL / "prototype_model.py").is_file():
        pytest.skip(NO_ARCHIVE_HINT)
    if str(MODEL) not in sys.path:
        sys.path.insert(0, str(MODEL))
    import prototype_model  # модуль вне репозитория, только здесь

    from decimal import Decimal

    point = (POINTS | MODEL_ONLY)[case]
    window, weekend, take = point[1], point[2], point[3]
    stop_after = point[4] if len(point) > 4 else True
    rows = [
        (item.time.replace(tzinfo=None), Decimal(str(int(item.open))),
         Decimal(str(int(item.high))), Decimal(str(int(item.low))),
         Decimal(str(int(item.close))))
        for item in candles
    ]
    expected = prototype_model.run(
        rows,
        moving_period=PERIOD,
        take_profit_percent=Decimal(str(take)),
        trade_time_start=(window.start.hour, window.start.minute),
        trade_time_end=(window.end.hour, window.end.minute),
        close_on_time_end=True,
        stop_after_take_profit=stop_after,
        trade_in_weekend=weekend,
        regime="On",
        volume=1,
    )
    deals, _ = play(candles, window, weekend, take, stop_after)
    assert len(expected) > 50, (
        f"{case}: модель дала {len(expected)} сделок — сверять нечего, "
        "проверка была бы вакуумной"
    )

    mismatch: list[str] = []
    for index in range(max(len(deals), len(expected))):
        mine = None
        if index < len(deals):
            deal = deals[index]
            mine = (
                deal.entry_time.replace(tzinfo=None),
                "Long" if deal.side.value == "long" else "Short",
                float(deal.entry_price),
                deal.exit_time.replace(tzinfo=None),
                float(deal.exit_price),
            )
        theirs = None
        if index < len(expected):
            row = expected[index]
            theirs = (
                row["open_time"], row["direction"], float(row["entry_price"]),
                row["close_time"], float(row["close_price"]),
            )
        if mine != theirs:
            mismatch.append(f"  строка {index}\n    движок  {mine}\n    модель  {theirs}")

    assert not mismatch, (
        f"{case}: расхождений {len(mismatch)} из {len(expected)}, построчно:\n"
        + "\n".join(mismatch[:10])
    )


@pytest.mark.slow
def test_the_engine_reproduces_the_prototypes_own_journal_of_127_deals(
    candles,
) -> None:
    """**Главный пункт приёмки Э1-8.** 127 сделок прототипа, построчно.

    Единственный оракул проекта с **включённым тейком** и единственный,
    записанный самим прототипом посделочно: окно 09:30–11:30, будни,
    период 15, тейк 0,5%, «стоп после тейка» включён, «закрывать в конце окна»
    включено, объём 1 контракт, заявки по рынку, проскальзывание 0.

    Из 127 выходов **42 идут по уровню тейка**, и три сделки открылись
    и закрылись внутри одной свечи. Ни то, ни другое не проверяется
    конфигурациями с выключенным тейком: они про тейк не говорят ничего.

    ⚠️ Прогон идёт через **настоящий движок и настоящий порт**. При Э1-6 те же
    127 строк сходились через отдельную обвязку аудитора сверки, а не через
    `engine/` (ROADMAP §9.5б), и довод «уровень считает движок, потому что его
    сверяет `/parity`» оставался неподкреплённым.

    Сравниваются время входа, сторона, цена входа, время выхода, цена выхода —
    те же пять величин, что в протоколе сверки (PROTOTYPE.md §7). Причина
    выхода не сравнивается: в журнале прототипа такого поля нет вовсе (§9).
    """
    if not (RESULTS / "mxu6_p2.json").is_file():
        pytest.skip(NO_ARCHIVE_HINT)
    reference = archive("mxu6_p2.json")
    assert reference["deals"] == 127
    expected = list(reversed(reference["positions"]))
    assert len(expected) == 127

    deals, left = play(candles, TradingWindow(time(9, 30), time(11, 30)), False, 0.5)

    mismatch: list[str] = []
    for index in range(max(len(deals), len(expected))):
        mine = None
        if index < len(deals):
            deal = deals[index]
            mine = (
                deal.entry_time.replace(tzinfo=None),
                "Long" if deal.side.value == "long" else "Short",
                float(deal.entry_price),
                deal.exit_time.replace(tzinfo=None),
                float(deal.exit_price),
            )
        theirs = None
        if index < len(expected):
            row = expected[index]
            theirs = (
                datetime.fromisoformat(row["open_time"][:19]),
                row["direction"],
                float(row["entry_price"]),
                datetime.fromisoformat(row["close_time"][:19]),
                float(row["close_price"]),
            )
        if mine != theirs:
            mismatch.append(
                f"  строка {index}\n    движок  {mine}\n    прототип {theirs}"
            )

    assert not mismatch, (
        f"расхождений {len(mismatch)} из 127, построчно:\n" + "\n".join(mismatch[:10])
    )
    assert left is None, "у прототипа на конце отрезка позиций не осталось"
    assert profit(deals) == pytest.approx(reference["realized"])


# ---------------------------------------------------------------------------
# Фильтр против пилы: умолчание обязано быть «выключено» (05.09.2026)
# ---------------------------------------------------------------------------

WINDOW_OF_THE_127 = TradingWindow(time(9, 30), time(11, 30))


def five(deals) -> list[tuple]:
    """Пять величин протокола сверки на каждую сделку (PROTOTYPE.md §7)."""
    return [
        (
            deal.entry_time.replace(tzinfo=None),
            "Long" if deal.side.value == "long" else "Short",
            float(deal.entry_price),
            deal.exit_time.replace(tzinfo=None),
            float(deal.exit_price),
        )
        for deal in deals
    ]


@pytest.mark.slow
def test_the_anti_chop_filter_switched_off_leaves_all_127_deals_untouched(
    candles,
) -> None:
    """Явно выключенный фильтр — те же 127 сделок, что и без него вовсе.

    Мутационная проверка нуля на **настоящих** данных: сравниваются пять
    величин протокола сверки построчно, а не итог. Совпадение итога при
    разных сделках — это совпадение двух ошибок.
    """
    plain, _ = play(candles, WINDOW_OF_THE_127, False, 0.5)
    off, _ = play(
        candles, WINDOW_OF_THE_127, False, 0.5,
        module=EmaReverseSettings(period=PERIOD, threshold_percent=0.0, confirm_bars=1),
    )
    assert len(plain) == 127
    assert five(off) == five(plain)


@pytest.mark.slow
@pytest.mark.parametrize(
    "changed, what",
    [
        (
            EmaReverseSettings(period=PERIOD, threshold_percent=0.02),
            "порог пересечения 0,02%",
        ),
        (
            EmaReverseSettings(period=PERIOD, confirm_bars=2),
            "подтверждение в две свечи",
        ),
    ],
)
def test_the_parity_goes_red_if_the_filter_default_is_ever_moved(
    candles, changed: EmaReverseSettings, what: str
) -> None:
    """Сдвинутое умолчание фильтра ломает сверку — значит сверка его и стережёт.

    Без этой проверки «фильтр выключен умолчанием» держалось бы на честном
    слове: сверка сошлась бы и в том случае, если бы фильтр вовсе не работал.
    """
    plain, _ = play(candles, WINDOW_OF_THE_127, False, 0.5)
    filtered, _ = play(candles, WINDOW_OF_THE_127, False, 0.5, module=changed)
    assert five(filtered) != five(plain), (
        f"{what} не изменил ни одной сделки из 127 — фильтр не работает "
        "либо сверка его не видит"
    )


#: Снимок счёта для мутаций денежных предохранителей. Числа подобраны так,
#: чтобы предохранитель заведомо сработал: счёт мал против размаха цен
#: эталонного отрезка, а свободных средств хватает ровно на один контракт
#: без запаса и ни на один — с запасом 99%.
#:
#: ⚠️ Время снимка — заглушка: `_guarded` подставляет его **на каждой свече**
#: (см. там же), иначе снимок протухает на второй же минуте отрезка и мутация
#: краснела бы от срока годности, а не от предохранителя, ради которого
#: поставлена (решение 0040).
_TIGHT_FUNDS = AccountFunds(
    at=datetime(2026, 6, 19, 9, 0, tzinfo=MSK),
    equity=100_000.0,
    free=100_000.0,
    margin_per_contract=100_000.0,
)


def _guarded(candles, guards: dict, funds: AccountFunds | None = None):
    """Тот же прогон, но с включённым предохранителем. **Без `assert not halted`.**

    Остановка здесь — ожидаемый исход, а не поломка: дневной лимит убытка
    обязан останавливать робота, и `play()` на этом бы упал. Возвращаются
    сделки, которые успели случиться.

    ⚠️ Снимок счёта кладётся **перед каждой свечой**, с её собственным
    временем. Один снимок на весь отрезок в два месяца устарел бы сразу
    (`engine.FUNDS_MAX_AGE` — две минуты), и предохранители перестали бы
    считать вовсе: мутация краснела бы, но проверяла бы срок годности,
    а не то, что написано в её имени. Ровно так этот прогон и работал
    до 05.09.2026.
    """
    engine = Engine(
        EmaReverse(EmaReverseSettings(period=PERIOD)),
        EngineSettings(
            mode=Mode.REVERSE, window=WINDOW_OF_THE_127, close_on_time_end=True,
            trade_in_weekend=False, volume=1.0, stop_after_take_profit=True,
            take_profit=True, take_profit_percent=0.5,
        ).replace(**guards),
    )
    executor = NextOpenExecutor()

    async def scenario() -> None:
        for item in candles:
            if funds is not None:
                # ⚠️ Время **закрытия** свечи, а не начала. `FakeCandle.time` —
                # начало (так же, как в слое данных), а срок годности снимка
                # движок считает от закрытия. Разница ровно в один таймфрейм,
                # то есть пять минут против двух минут годности: снимок,
                # положенный по времени начала, протух бы на каждой свече,
                # и мутация «запас средств 99%» краснела бы от срока годности.
                # Проверено обратной мутацией: с выключенной проверкой
                # обеспечения она обязана позеленеть.
                engine.on_funds(
                    replace(
                        funds,
                        at=item.time + timedelta(minutes=item.timeframe.minutes),
                    )
                )
            await engine.on_market_candle(item, executor)

    asyncio.run(scenario())
    return executor.deals


@pytest.mark.slow
@pytest.mark.parametrize(
    "guards, funds, what",
    [
        ({"volume_cap": 0.5}, None, "потолок объёма ниже объёма сделки"),
        (
            {"daily_loss_limit_percent": 0.01},
            _TIGHT_FUNDS,
            "дневной лимит убытка 0,01% от счёта",
        ),
        (
            {"free_funds_reserve_percent": 99.0},
            _TIGHT_FUNDS,
            "запас свободных средств 99%",
        ),
    ],
    ids=["потолок", "лимит убытка", "запас средств"],
)
def test_the_parity_goes_red_if_a_money_guard_default_is_ever_moved(
    candles, guards: dict, funds: AccountFunds | None, what: str
) -> None:
    """Сдвинутое умолчание предохранителя ломает сверку — значит она его стережёт.

    Без этой проверки «предохранители выключены умолчанием» держалось бы
    на честном слове: сверка сошлась бы и в том случае, если бы проверки
    вовсе не работали. Здесь каждая включается заведомо срабатывающим
    значением, и 127 сделок обязаны развалиться.

    ⚠️ Сравниваются **сделки построчно**, а не их число: одинаковое
    количество при разных сделках — это совпадение двух ошибок.
    """
    plain, _ = play(candles, WINDOW_OF_THE_127, False, 0.5)
    assert len(plain) == 127
    guarded = _guarded(candles, guards, funds)
    assert five(guarded) != five(plain), (
        f"{what} не изменил ни одной сделки из 127 — предохранитель "
        "не работает либо сверка его не видит"
    )


@pytest.mark.slow
def test_the_money_guards_at_their_defaults_change_nothing(candles) -> None:
    """Умолчания предохранителей — те же 127 сделок, что и без них вовсе.

    Мутационная проверка нуля на **настоящих** данных, парная к предыдущей:
    выключенное умолчание обязано быть неотличимо от отсутствия проверки.
    Снимок счёта при этом положен внутрь — то есть данные у движка есть,
    а предохранители всё равно молчат, потому что их никто не включал.
    """
    plain, _ = play(candles, WINDOW_OF_THE_127, False, 0.5)
    quiet = _guarded(candles, {}, _TIGHT_FUNDS)
    assert len(plain) == 127
    assert five(quiet) == five(plain)


@pytest.mark.slow
def test_the_journal_of_127_has_the_two_shapes_only_the_take_profit_creates(
    candles,
) -> None:
    """42 выхода по уровню и три сделки внутри одной свечи — они и есть тейк.

    Обе цифры взяты из разбора эталонного журнала (PROTOTYPE.md §5). Первая
    ловит потерю тейка целиком, вторая — запрет срабатывания на «своей» свече,
    который даёт ровно 3 расхождения из 127 и никак иначе не виден.
    """
    deals, _ = play(candles, TradingWindow(time(9, 30), time(11, 30)), False, 0.5)
    assert sum(1 for deal in deals if deal.exit_reason == "take") == 42
    assert sum(1 for deal in deals if deal.entry_time == deal.exit_time) == 3


@pytest.mark.slow
def test_the_configurations_without_a_take_still_guard_the_candle_walk(
    candles,
) -> None:
    """Сторожа разбора свечи: сошлись — сломан не тейк.

    166 сделок и 1 211 сделок про тейк не говорят ничего, и закрыть ими Э1-8
    нельзя. Но роль у них есть, и она своя: разошлись — искать надо в разборе
    свечи, а не в уровне.
    """
    short, _ = play(candles, TradingWindow(time(9, 30), time(11, 30)), False)
    assert len(short) == 166  # цифра из архива, не магия
    assert profit(short) == pytest.approx(-5_400)

    wide, left = play(candles, TradingWindow(time(0, 0), time(0, 0)), False)
    assert len(wide) == 1_211
    assert profit(wide) == pytest.approx(-54_550)
    assert left is not None, "у прототипа на этой точке осталась открытая позиция"


# ---------------------------------------------------------------------------
# Тот же оракул, но через производственный слой прогона (Э1-9)
# ---------------------------------------------------------------------------

#: Числа эталонного прогона `mxu6_p2.json`: 127 сделок, окно 09:30–11:30,
#: будни, период 15, тейк 0,5%. Комиссия посчитана прототипом **сбоку**:
#: 127 сделок × 2 стороны × 14 ₽ = 3 556 ₽ (PROTOTYPE.md §6).
REFERENCE_GROSS = 6_479.0
REFERENCE_COMMISSION = 3_556.0
REFERENCE_NET = 2_923.0
#: Профит-фактор и число прибыльных сделок архив считает по **валовой**
#: прибыли: тариф в самих прогонах стоял нулевой.
REFERENCE_GROSS_PROFIT_FACTOR = 1.155745
REFERENCE_GROSS_PROFITABLE = 49


@pytest.mark.slow
def test_the_history_layer_reproduces_the_127_deals_line_by_line(candles) -> None:
    """Сверка идёт через `backtest.replay`, а не только через оснастку тестов.

    ⚠️ **Это и есть то, ради чего Э1-9 существует.** До переезда правила
    исполнения — «по открытию следующей свечи» и «уровень сторожится внутри
    бара» — жили в `tests/engine_helpers.py`, и приёмка Э1-8 закрывала пару
    «движок плюс тестовая оснастка», а не движок с производственным портом
    (ROADMAP §9.6). Здесь зовётся ровно тот код, который работает, когда
    владелец счёта нажимает «прогнать по истории».

    Сравниваются пять величин протокола сверки (PROTOTYPE.md §7): время входа,
    сторона, цена входа, время выхода, цена выхода.
    """
    if not (RESULTS / "mxu6_p2.json").is_file():
        pytest.skip(NO_ARCHIVE_HINT)
    reference = archive("mxu6_p2.json")
    expected = list(reversed(reference["positions"]))
    assert len(expected) == 127

    outcome = replayed(candles, TradingWindow(time(9, 30), time(11, 30)), False, 0.5)

    mismatch: list[str] = []
    for index in range(max(len(outcome.deals), len(expected))):
        mine = None
        if index < len(outcome.deals):
            deal = outcome.deals[index]
            mine = (
                deal.entry_time.replace(tzinfo=None),
                "Long" if deal.side.value == "long" else "Short",
                float(deal.entry_price),
                deal.exit_time.replace(tzinfo=None),
                float(deal.exit_price),
            )
        theirs = None
        if index < len(expected):
            row = expected[index]
            theirs = (
                datetime.fromisoformat(row["open_time"][:19]),
                row["direction"],
                float(row["entry_price"]),
                datetime.fromisoformat(row["close_time"][:19]),
                float(row["close_price"]),
            )
        if mine != theirs:
            mismatch.append(
                f"  строка {index}\n    прогон   {mine}\n    прототип {theirs}"
            )

    assert not mismatch, (
        f"расхождений {len(mismatch)} из 127, построчно:\n" + "\n".join(mismatch[:10])
    )
    assert outcome.position is None, "у прототипа на конце отрезка позиций не осталось"
    assert sum(
        1 for deal in outcome.deals if deal.exit_reason.value == "take_profit"
    ) == 42
    assert sum(1 for deal in outcome.deals if deal.entry_time == deal.exit_time) == 3


@pytest.mark.slow
def test_the_report_shows_the_commission_as_its_own_line(candles) -> None:
    """Валовая, комиссия, чистая — три числа, и все три совпадают с архивом.

    Валовая прибыль в этом проекте результатом **не** является (DOMAIN.md §5):
    из 6 479 ₽ комиссия забирает 3 556 ₽, то есть больше половины. Реверсная
    система на пятиминутках делает 47 переворотов на 127 сделок, и каждый
    переворот — две стороны комиссии.

    Тариф задан разбивкой ТЗ §4.4 Е: биржевой сбор из карточки инструмента
    плюс 1 ₽ брокеру за контракт. Опорная цифра замеров — 14 ₽ на сторону.
    """
    if not (RESULTS / "mxu6_p2.json").is_file():
        pytest.skip(NO_ARCHIVE_HINT)
    reference = archive("mxu6_p2.json")
    outcome = replayed(
        candles, TradingWindow(time(9, 30), time(11, 30)), False, 0.5,
        costs=Costs(tariff=Tariff(exchange_fee=13.0)),
    )
    summary = outcome.summary

    assert summary.trades == 127
    assert summary.gross_profit == pytest.approx(REFERENCE_GROSS)
    assert summary.gross_profit == pytest.approx(reference["realized"])
    assert summary.commission == pytest.approx(REFERENCE_COMMISSION)
    assert summary.commission == pytest.approx(reference["commission"])
    assert summary.net_profit == pytest.approx(REFERENCE_NET)
    assert summary.net_profit == pytest.approx(reference["net"])
    assert summary.net_profit == pytest.approx(
        summary.gross_profit - summary.commission
    ), "чистая посчитана не как валовая минус комиссия"
    assert summary.reversals == 47


@pytest.mark.slow
def test_the_profit_factor_matches_the_archive_on_the_archives_own_basis(
    candles,
) -> None:
    """Профит-фактор архива — **валовой**, и совпадает только с валовым.

    ⚠️ Тариф в эталонных прогонах стоял нулевой: комиссию прототип считал
    сбоку и в журнал не переносил (PROTOTYPE.md §6). Значит и профит-фактор
    1,1557, и «49 прибыльных» посчитаны по валовой прибыли.

    Наш итог при заданном тарифе считает всё по **чистой** — так требует
    докстринг `Summary`, и смешивать базы внутри одного отчёта нельзя. Отсюда
    два числа рядом, а не одно: на чистой базе профит-фактор 1,0668, а
    прибыльных 48, потому что одна сделка с валовой прибылью меньше 28 ₽
    комиссию не окупила. **Это разница определений, а не расхождение**, и
    видно её только так — показав обе цифры.
    """
    if not (RESULTS / "mxu6_p2.json").is_file():
        pytest.skip(NO_ARCHIVE_HINT)
    reference = archive("mxu6_p2.json")
    window = TradingWindow(time(9, 30), time(11, 30))

    gross = replayed(candles, window, False, 0.5).summary
    assert gross.commission is None, "тариф не задан — база обязана быть валовой"
    assert gross.profit_factor == pytest.approx(REFERENCE_GROSS_PROFIT_FACTOR, abs=1e-6)
    assert gross.profit_factor == pytest.approx(reference["pf"], abs=1e-6)
    assert gross.profitable == REFERENCE_GROSS_PROFITABLE == reference["win"]
    assert gross.trades - gross.profitable == 78

    net = replayed(
        candles, window, False, 0.5, costs=Costs(commission_per_side=14.0)
    ).summary
    assert net.profit_factor is not None
    assert net.profit_factor == pytest.approx(1.066755, abs=1e-6)
    assert net.profitable == 48
    assert net.profit_factor < gross.profit_factor, (
        "комиссия не ухудшила профит-фактор — значит она в него не вошла"
    )


@pytest.mark.slow
def test_the_costs_change_the_money_but_not_a_single_deal(candles) -> None:
    """Комиссия — единственная величина, которую можно менять, не меняя сделок.

    Прототип её не читает вообще (PROTOTYPE.md §6). Отсюда правило разбора
    расхождений: сделки совпали, а деньги нет — расходится комиссия; сделки
    разошлись — комиссия ни при чём, искать надо в другом месте.
    """
    window = TradingWindow(time(9, 30), time(11, 30))
    free = replayed(candles, window, False, 0.5)
    charged = replayed(
        candles, window, False, 0.5, costs=Costs(commission_per_side=14.0)
    )
    assert [
        (deal.entry_time, deal.entry_price, deal.exit_time, deal.exit_price)
        for deal in free.deals
    ] == [
        (deal.entry_time, deal.entry_price, deal.exit_time, deal.exit_price)
        for deal in charged.deals
    ]
    assert free.summary.gross_profit == pytest.approx(charged.summary.gross_profit)


@pytest.mark.slow
def test_slippage_is_zero_in_the_parity_run_and_says_so(candles) -> None:
    """Сверка гонится на нулевом проскальзывании, и это записано в результате.

    ⚠️ Проскальзывание, в отличие от комиссии, меняет **список сделок**: оно
    двигает цену входа, от цены входа считается уровень тейка, и дальше
    расходится всё. Прогон с ненулевым проскальзыванием сверять с прототипом
    нельзя — он считал без него.
    """
    outcome = replayed(candles, TradingWindow(time(9, 30), time(11, 30)), False, 0.5)
    assert outcome.costs.slippage == 0.0
    assert outcome.costs.slippage_steps == 0.0
