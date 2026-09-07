"""Подставки для тестов движка: свеча источника, модуль, порты.

⚠️ **Ни одного правила торговли здесь нет.** Правила исполнения — исполнение
по открытию следующей свечи, сторож уровня внутри бара, проверка «заявка
не исполняется раньше, чем подана» — переехали на Э1-9 в производственный код,
в `backtest/execution.py`, и тесты движка зовут именно его: `NextOpenExecutor`
наследует `backtest.ExecutionModel` и добавляет только запись сделок в удобной
сценариям форме.

Так требует решение 0008: два куска кода, сторожащих один уровень, — та же
болезнь этажом ниже. Пока копий было две, сверка с прототипом сходилась
на той, которую зовёт, а вторая могла разойтись с ней молча.

Импорт `backtest` в оснастке движка — не нарушение слоёв, а его соблюдение:
`backtest` зависит от `engine`, не наоборот, и подставляется он движку через
порт, как и боевой адаптер. Движок про этот модуль по-прежнему не знает.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from backtest import ExecutionModel, Guard, OpenLeg
from engine import (
    ExecutionRefused,
    ExitReason,
    Fill,
    OrderAction,
    OrderRequest,
    Position,
    Refusal,
    Side,
    TakeProfit,
)
from strategies import Bar, Decision, Intent

MSK = timezone(timedelta(hours=3), "MSK")
STEP = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class FakeTimeframe:
    """Размер свечи. Движку от таймфрейма нужны только минуты."""

    minutes: int = 5


@dataclass(frozen=True, slots=True)
class FakeCandle:
    """Свеча источника: `time` — **начало**, как в слое данных.

    Форма повторяет `market.Candle` намеренно, но класс свой: тесты движка
    не должны зависеть от слоя, который движку импортировать нельзя.
    """

    time: datetime
    open: float = 100.0
    high: float = 100.0
    low: float = 100.0
    close: float = 100.0
    volume: float = 0.0
    timeframe: FakeTimeframe = FakeTimeframe()
    filled_minutes: int = 5
    unsettled: bool = False


def candle(
    hour: int,
    minute: int,
    *,
    close: float = 100.0,
    day: int = 19,
    month: int = 6,
    year: int = 2026,
    minutes: int = 5,
    **rest: object,
) -> FakeCandle:
    """Свеча, начинающаяся в указанное время МСК. По умолчанию 19.06.2026, пятница."""
    return FakeCandle(
        time=datetime(year, month, day, hour, minute, tzinfo=MSK),
        open=close, high=close, low=close, close=close,
        timeframe=FakeTimeframe(minutes),
        **rest,  # type: ignore[arg-type]
    )


def bar(hour: int, minute: int, *, close: float = 100.0, day: int = 19) -> Bar:
    """Закрытая свеча торгового модуля. Время — **закрытия**."""
    return Bar(
        closes_at=datetime(2026, 6, day, hour, minute, tzinfo=MSK),
        open=close, high=close, low=close, close=close,
    )


def decision(
    intent: Intent = Intent.LONG,
    *,
    reason: str = "",
    skip_bar: bool = False,
    warmed_up: bool = True,
    close: float = 100.0,
    average: float | None = 99.0,
) -> Decision:
    """Готовое намерение модуля — движок не знает, как оно получено."""
    if not reason:
        reason = {
            Intent.LONG: "Закрытие 100 выше EMA(15) 99",
            Intent.SHORT: "Закрытие 100 ниже EMA(15) 101",
            Intent.NONE: "Закрытие 100 ровно на EMA(15)",
        }[intent]
    return Decision(
        intent=intent, reason=reason, close=close, average=average,
        bars=99, warmed_up=warmed_up, skip_bar=skip_bar,
    )


def position(
    side: Side = Side.LONG,
    *,
    price: float = 100.0,
    volume: float = 1.0,
    take: float | None = None,
    **rest: object,
) -> Position:
    """Открытая позиция. `take` — уже вооружённый уровень, если он нужен тесту.

    Без него у позиции **нет плана тейка вовсе** (процент 0), и шаг 5 ничего
    не вооружает: тесты, которые про тейк не спрашивают, не должны получать
    лишнюю заявку. План замораживается на входе в позицию, поэтому настройки
    движка на него не влияют — так требует ТЗ §4.4 А.
    """
    plan = TakeProfit(percent=0.5 if take is not None else 0.0, level=take)
    return Position(
        side=side, volume=volume, entry_price=price,
        entry_time=datetime(2026, 6, 19, 10, 15, tzinfo=MSK),
        take=plan,
        **rest,  # type: ignore[arg-type]
    )


class ScriptedStrategy:
    """Торговый модуль, отдающий заранее записанные намерения.

    Движок не знает, какая стратегия внутри (ARCHITECTURE.md §1) — значит
    подставить сюда можно что угодно, лишь бы форма совпадала.
    """

    title = "Подставной модуль"

    def __init__(self, script: Sequence[Decision]) -> None:
        self.script = list(script)
        self.seen: list[Bar] = []
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1

    def on_closed_bar(self, closed: Bar) -> Decision:
        self.seen.append(closed)
        if not self.script:
            raise AssertionError("модулю подали больше свечей, чем записано в сценарии")
        return self.script.pop(0)


class ListSource:
    """Источник свечей: заранее известный список. Порт не знает, откуда он взят."""

    def __init__(self, candles: Sequence[FakeCandle]) -> None:
        self._candles = list(candles)

    async def candles(self) -> AsyncIterator[FakeCandle]:
        for item in self._candles:
            yield item


@dataclass
class RecordingExecutor:
    """Исполнитель, который ничего не исполняет и всё записывает.

    `scripted` — очередь ответов на `fills_at`. ⚠️ Ответы отдаются **по одному
    вызову**, а вызовов на одну свечу теперь несколько: обмен идёт по кругу,
    пока порт не ответит пустотой. Сценарий на одну свечу — это список
    из одного непустого ответа: следующий вызов вернёт пустоту и круг
    закончится.

    `refuse` — имена заявок и **причина**, по которой исполнитель их
    отклоняет определённым отказом. Обе ветки обязаны быть выразимы моделью,
    иначе они существуют только в бою и первый раз выполняются на реальных
    деньгах.
    """

    submitted: list[OrderRequest] = field(default_factory=list)
    scripted: list[Sequence[Fill]] = field(default_factory=list)
    seen: list[FakeCandle] = field(default_factory=list)
    refuse: dict[str, Refusal] = field(default_factory=dict)

    async def submit(self, order: OrderRequest) -> None:
        reason = _refused(order, self.refuse)
        if reason is not None:
            raise ExecutionRefused(
                f"заявка {order.order_id} отклонена: так велел сценарий теста",
                reason,
            )
        self.submitted.append(order)

    async def fills_at(self, market_candle: FakeCandle) -> Sequence[Fill]:
        self.seen.append(market_candle)
        return self.scripted.pop(0) if self.scripted else ()


def _refused(order: OrderRequest, names: dict[str, Refusal]) -> Refusal | None:
    """Причина отказа для этой заявки — или `None`, если отказа нет.

    Заявка названа своим именем или именем той, которую она снимает:
    собственное имя снятия выводится из момента подачи, и записать его
    в сценарий теста заранее нельзя. Отказ в снятии — главный случай, ради
    которого этот механизм и заведён (решение 0008, пункт 3).

    ⚠️ Причина — **значение**, а не текст: «нет такой заявки» и «уровень уже
    задет» требуют от движка противоположных действий, и модель обязана уметь
    выдать обе по отдельности. Сверкой эти ветки не проходятся никогда —
    отказов в снятии в эталонном прогоне ноль из 85.
    """
    if order.order_id in names:
        return names[order.order_id]
    return names.get(order.target_id or "")


@dataclass
class Deal:
    """Закрытая сделка, как её видит модель исполнения в тестах движка.

    ⚠️ Причина выхода здесь **строка**, а не `ExitReason`, и это не небрежность:
    тесты движка сравнивают её с `"take"`, а не с именем перечисления. Отдельный
    тип сделки у тестов движка есть ровно затем, чтобы деньги, комиссия и
    разбивки отчёта в них не участвовали: разбор свечи от них не зависит,
    и таскать их через каждый сценарий значило бы проверять отчёт вместо движка.
    """

    side: Side
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    volume: float
    exit_reason: str


class NextOpenExecutor(ExecutionModel):
    """Модель исполнения прототипа для тестов движка.

    ⚠️ **Своих правил здесь нет ни одного.** Все они — исполнение по открытию
    следующей свечи, сторож уровня внутри бара, проверка «заявка не исполняется
    раньше, чем подана», порядок кругов — живут в `backtest/execution.py`,
    в производственном коде, и тесты движка зовут именно его. Так требует
    решение 0008: два куска кода, сторожащих один уровень, — та же болезнь
    этажом ниже, и сверка продолжала бы сходиться на той копии, которую зовёт.

    Здесь добавлена только запись сделок в форме, удобной сценариям движка.

    `refuse` — имена заявок и **причина**, по которой исполнитель их отклоняет;
    `hollow_cancel` — снятие, отвечающее по подтверждению приёма, а не по факту
    снятия. Оба параметра — часть общей модели: ветка обработки отказа,
    недостижимая ни одним тестом, существует только в бою.
    """

    def __init__(self, **rest: object) -> None:
        self._leg: OpenLeg | None = None
        super().__init__(**rest)  # type: ignore[arg-type]  # подпись задана ExecutionModel, здесь только проброс
        self.deals: list[Deal] = []

    @property
    def _open(self) -> OpenLeg | None:
        """Открытая нога сделки — та же, что у общей модели."""
        return self._leg

    @_open.setter
    def _open(self, value: OpenLeg | tuple[Side, float, datetime, float] | None) -> None:
        """Принять открытую ногу — в том числе кортежем.

        ⚠️ Сценарии тестов движка ставят позицию **готовым кортежем**
        `(сторона, цена, время, объём)`: сделки открытия в них не было, а модель
        обязана думать, что позиция есть, — иначе сработавший уровень окажется
        сиротой и проверяться будет не то. Общая модель хранит ногу структурой
        (`backtest.OpenLeg`), потому что отчёту нужно ещё и имя заявки входа.

        Перевод одного в другое живёт здесь, а не в производственном коде:
        поддержка формы, нужной только тестам, в `backtest/` была бы кодом,
        существующим ради теста.

        ⚠️ **Сторожей это присваивание не трогает, и трогать не должно.**
        Через него проходит не только сценарий: ровно так пишет открытую ногу
        сама производственная модель, когда исполняет заявку на вход
        (`backtest/execution.py`, `_market_fills`). Связывание, повешенное
        сюда, перехватило бы и её — и на этой оснастке уровень, вооружённый
        над пустым местом, усыновил бы первую же ногу от сделки открытия.
        Получился бы зелёный тест на выдуманном тейке в том самом классе,
        которым проверяют движок и от которого наследует счётчик сторожей
        в сверке. Так и было написано в первой редакции правки 03.09.2026;
        найдено ревью контракта в тот же день.

        Кому нужна связка — зовёт `place` и называет её вслух.
        """
        self._leg = OpenLeg(*value) if isinstance(value, tuple) else value

    def place(
        self, side: Side, price: float, at: datetime, volume: float = 1.0
    ) -> OpenLeg:
        """Поставить готовую позицию **и отдать ей сторожей без ноги**.

        Сценарии тестов движка ставят позицию готовой, а вооружить уровень
        могут и до этого — порядок двух шагов сценарий выбирает свободно.
        Производственная модель такому уровню ноги не даёт: вооружение
        над пустым местом остаётся сиротой навсегда (`backtest.Guard`),
        и поблажек там больше нет ни одной.

        Значит связку обязан назвать тот, кому она нужна, — и называет он её
        здесь, вызовом с именем, а не побочным действием присваивания.
        Разница не косметическая: присваивание делает и производственный код,
        а `place` зовёт только сценарий.

        Отдаются сторожа **без ноги**. У кого нога есть, тот принадлежит ей,
        и подменять её постановкой новой позиции нельзя — на этом стоит
        распознавание сироты при перевороте.
        """
        leg = OpenLeg(side=side, price=price, at=at, volume=volume)
        self._open = leg
        for name, guard in list(self.armed.items()):
            if guard.leg is None:
                self.armed[name] = Guard(order=guard.order, leg=leg)
        return leg

    def _record(
        self,
        leg: OpenLeg,
        price: float,
        at: datetime,
        order: OrderRequest,
        order_id: str,
    ) -> None:
        """Оформить закрытую сделку. Причина — из заявки, а не из цены."""
        if order.action is OrderAction.ARM_TAKE_PROFIT:
            why = "take"
        else:
            why = (order.exit_reason or ExitReason.SIGNAL).value
        self.deals.append(Deal(
            side=leg.side, entry_time=leg.at, entry_price=leg.price,
            exit_time=at, exit_price=price, volume=leg.volume, exit_reason=why,
        ))
