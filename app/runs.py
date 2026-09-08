"""Прогон по истории записывается: с чем гнали, на чём и что вышло.

Зачем это существует
--------------------
Владелец счёта показал итог: 143 сделки, валовая −2 331 ₽, комиссия 4 004 ₽,
чистая −6 335 ₽, профит-фактор 0,82, переворотов 78 — и спросил, почему убыток.
Ответить было нечем. Перебор 270 сочетаний настроек его цифры не воспроизвёл:
по числу сделок совпадение ловилось, по деньгам нет, значит в его настройке
было что-то, чего не угадали. Посмотреть было негде: таблица `journal_session`
в базе существовала и была пуста.

Число, живущее отдельно от условий, при которых получено, — это ровно то,
против чего написано правило проекта про подгонку (`CLAUDE.md` §5). Здесь
условия и число складываются в одну строку базы.

Что записывается
----------------
* `settings` — **все** поля настроек движка и торгового модуля, снимком;
* `note` — на чём гнали: инструмент, размер свечи, границы отрезка, сколько
  свечей, каким отбором эти границы получились (`--days`, `--until`);
* `finish_note` — что вышло: сделки, деньги, профит-фактор, просадка **и
  издержки, на которых деньги посчитаны** (`HistoryRun.costs`);
* `app_version` — версия программы, `symbol`, `timeframe`, `strategy`,
  `origin` — своими колонками.

Мерило полноты — **повторимость**: записанного должно хватать, чтобы
повторить прогон и получить те же числа. Отсюда в записи оказались две вещи,
которых нет в списке «настройки движка»: разрешённые границы отрезка (ключ
`--days` считается от последней свечи в базе, то есть завтра означает другой
период) и издержки прогона (проскальзывание живёт только в `backtest.Costs`,
и настройками движка его не восстановить).

Как устроена полнота полей
--------------------------
Поля **не перечисляются руками**. Снимок собирается обходом
`dataclasses.fields()`, поэтому настройка, заведённая завтра, попадает
в запись сама — даже если про неё здесь не вспомнили. Таблицы
`_ENGINE_TITLES`, `_STRATEGY_TITLES` и `_COSTS_TITLES` дают полям человеческие
подписи и на полноту записи не влияют: поля без подписи пишутся своим именем.
Полнота подписей стережётся тестом (`tests/test_app_runs.py`), полнота
самой записи — устройством обхода, и её ломает только замена обхода
на список.

Это тот же дефект, что описан правилом 9 `CLAUDE.md`: перечисление полей
руками в двух местах молчит. Забыл дописать — запись врёт, а прогон при этом
идёт и числа показывает.

Чего здесь нет
--------------
Строк журнала решений и журнала сделок. `HistoryPort` их в базу не пишет:
прогон гоняется заново на каждое изменение настройки, и полтора десятка
таких проходов за вечер положили бы в базу десятки тысяч строк. На этом
стоит `RUNS_KEPT` — см. его обоснование, там же названо, что придётся
пересмотреть, когда строки начнут писаться.
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import pathlib
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, TextIO

from app import convert
from app.invocation import how_to_fetch
from backtest import HistoryRun
from engine import EngineSettings
from market import Candle as MarketCandle
from market import (
    CandleStore,
    JournalSession,
    MarketWorker,
    RunOrigin,
    SessionRecord,
    default_db_path,
    userdata_dir,
)
from strategies import EmaReverseSettings
from ui import backend
from ui.backend import RunStats, TemplateRun
from ui.formatting import fmt_datetime, fmt_money, fmt_share
from ui.models import Mode, Settings

__all__ = [
    "RUNS_KEPT",
    "RUNS_SHOWN",
    "RunConditions",
    "RunLog",
    "matching_runs",
    "result_note",
    "run_lines",
    "run_of",
    "settings_text",
    "show_runs",
    "snapshot_marks",
    "snapshot_of",
]

#: Сколько прогонов по истории хранится в базе. **Своё число, а не
#: `market.BACKTEST_SESSIONS_KEPT`**, и разница обоснована размером строки.
#:
#: Двадцать в `market/` посчитаны для прогона **вместе с его строками**:
#: проход по всей истории MXU6 даёт 1558 строк решений и 266 сделок, около
#: 0,5 МБ на прогон. `HistoryPort` этих строк не пишет вовсе (см. шапку
#: модуля), и прогон стоит одну строку. Замер 05.09.2026 **на базе владельца
#: счёта** (82 339 минуток, прогон по 21 811 пятиминуткам, настройки
#: по умолчанию): снимок настроек 805 знаков, условия 138, итог 313 — вместе
#: 2 074 байта в UTF-8 (кириллица по два байта). Двести таких строк — 0,4 МБ,
#: то есть в двадцать пять раз меньше, чем двадцать полных прогонов.
#:
#: Почему двести, а не двадцать: прогон гоняется на **каждое** изменение
#: настройки. Двадцати не хватает на вечер подбора — первые пробы
#: вычистились бы к его концу, то есть ровно та запись, ради которой всё это
#: заводится, и пропала бы.
#:
#: ⚠️ **Живой ход движка сюда не считается, и это правка дефекта** (Э3,
#: 05.09.2026). Здесь стояло «и на закрытие каждого бара живого потока»: так
#: и было, и цена посчитана ревью — `--stream` на минутках давал 840
#: одинаковых строк за день, то есть двести ручных подборов вычищались
#: за три с половиной часа. Теперь у живого хода **одна** запись на весь ход
#: с происхождением `PAPER` (`app/port.py::_watcher`), а `market/storage.py`
#: чистит только прогоны по истории и симуляцию на боевом потоке не трогает
#: вовсе. Две сущности — два правила чистки, и в базе они уже разные.
#:
#: ⚠️ **Число держится на том, что порт не пишет решений и сделок.** Начнёт
#: писать — двести прогонов станут сотней мегабайт, и число обязано вернуться
#: к порядку `BACKTEST_SESSIONS_KEPT`. Опора проверяется тестом
#: `test_the_port_writes_no_decisions_and_no_trades_and_that_holds_the_limit`.
RUNS_KEPT: Final[int] = 200

#: Сколько прогонов показывает `--runs` без числа. Двадцать — вечер подбора:
#: больше на экран консоли всё равно не помещается, а ключ принимает число.
RUNS_SHOWN: Final[int] = 20

#: Отступ вложенных строк снимка. Тот же в записи и в выдаче `--runs`:
#: человек сверяет одно с другим глазами.
_STEP = "  "


# --------------------------------------------------------------- отрисовка

def _number(value: float) -> str:
    """Число настройки — так же, как его пишет журнал изменений настроек.

    `engine.settings` печатает `f"{value:g}"` с запятой в дробной части, и
    расхождение здесь читалось бы как разные значения: строка «Тейк-профит,
    %: 0,5 → 1» и снимок «take_profit_percent: 1.0» рядом в одном журнале.
    """
    return f"{value:g}".replace(".", ",")


#: Простые типы значений настройки → как их печатать. Поиск по **точному**
#: типу (`type(value)`), а не по `isinstance`, и это не мелочь: `bool`
#: наследует `int`, а `Mode` и `RunOrigin` наследуют `str`. Цепочка
#: `isinstance` держалась бы на порядке проверок — поставь строку выше
#: подписи, и в снимок ушло бы `long_only` вместо «Только лонг». Точный тип
#: этой ловушки не имеет вовсе: наследник в таблицу не попадает.
_SCALARS: Mapping[type, Callable[[Any], str]] = {
    bool: lambda value: "да" if value else "нет",
    int: str,
    float: _number,
    str: str,
}


def _text(value: object) -> str:
    """Значение любого поля настроек человеческим текстом.

    Подпись (`label`) идёт первой: все перечисления проекта её имеют, и
    человеческое «Только лонг» лучше значения `long_only`, под которым
    оно лежит в базе.

    Незнакомый тип печатается через `repr`, а не отвергается: снимок обязан
    получиться при любом поле, которое заведут завтра. Нечитаемое значение —
    повод дописать подпись, а не потерять запись.
    """
    label = getattr(value, "label", None)
    if isinstance(label, str):
        return label
    if value is None:
        return "не задано"
    render = _SCALARS.get(type(value))
    if render is not None:
        return render(value)
    if isinstance(value, enum.Enum):
        return str(value.value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return ", ".join(
            f"{field.name} {_text(getattr(value, field.name))}"
            for field in dataclasses.fields(value)
        )
    return repr(value)


def _block(title: str, values: object, titles: Mapping[str, str]) -> list[str]:
    """Заголовок и строка на каждое поле — **обходом, а не списком**.

    Полнота держится здесь. `dataclasses.fields` перечисляет то, что есть
    в классе сейчас, поэтому новое поле попадает в снимок само; `titles`
    только даёт подпись, и поле без подписи пишется своим именем — запись
    от этого не теряется.
    """
    if not dataclasses.is_dataclass(values) or isinstance(values, type):
        raise TypeError(f"снимок настроек снимается с объекта-датакласса, дан {values!r}")
    lines = [title]
    for field in dataclasses.fields(values):
        name = titles.get(field.name, field.name)
        lines.append(f"{_STEP}{name}: {_text(getattr(values, field.name))}")
    return lines


#: Подписи полей `engine.EngineSettings`. Единицы измерения стоят **в
#: подписи**, а не рядом со значением: значение печатается общим правилом,
#: одинаковым для всех полей, и «₽» после числа пришлось бы приписывать
#: развилкой по имени поля.
_ENGINE_TITLES: Mapping[str, str] = {
    "mode": "Режим",
    "window": "Торговое окно",
    "close_on_time_end": "Закрывать в конце окна",
    "trade_in_weekend": "Торговля в выходные",
    "volume": "Объём, контрактов",
    "reversal": "Момент переворота",
    "take_profit": "Тейк-профит",
    "take_profit_percent": "Тейк-профит, %",
    "trailing_take_profit": "Скользящий тейк-профит",
    "trailing_start_percent": "Скользящий тейк, порог включения, %",
    "trailing_offset_percent": "Скользящий тейк, отступ, %",
    "trailing_step_percent": "Скользящий тейк, шаг подтяжки, %",
    "stop_after_take_profit": "Стоп на день после тейка",
    "partial_candles": "Неполные свечи",
    "commission_per_side": "Комиссия за контракт на сторону, ₽",
    "ruble_per_point": "Рублей в пункте цены",
    "close_wait_bars": "Ожидание исполнения выхода, свечей",
    # Календарь обязан попасть в снимок прогона: два прогона с одинаковыми
    # на вид настройками дадут разные сделки, если в одном из них день
    # помечен нерабочим.
    "calendar": "Календарь нерабочих дней",
    "exchange_days": "Дни, названные биржей",
    # Три предохранителя по деньгам. В снимок прогона они обязаны попасть:
    # без них два прогона с одинаковыми на вид настройками могут дать разные
    # сделки — потолок объёма и лимит убытка меняют список.
    "volume_cap": "Потолок объёма, контрактов",
    "daily_loss_limit_percent": "Дневной лимит убытка, % от счёта на утро",
    "free_funds_reserve_percent": "Минимальный запас свободных средств, %",
}

#: Подписи полей `strategies.EmaReverseSettings`.
_STRATEGY_TITLES: Mapping[str, str] = {
    "period": "Период средней",
    "kind": "Тип средней",
    "on_equal": "Закрытие ровно на средней",
    "threshold_percent": "Порог пересечения, %",
    "confirm_bars": "Подтверждение сигнала, свечей",
}

#: Подписи полей `backtest.Costs`. Издержки записываются рядом с деньгами,
#: а не рядом с настройками: `HistoryRun.costs` — то, на чём деньги
#: посчитаны, и два прогона с разными издержками дают **один и тот же
#: список сделок** при разных рублях.
_COSTS_TITLES: Mapping[str, str] = {
    "tariff": "Тариф разбивкой",
    "commission_per_side": "Комиссия за контракт на сторону, ₽",
    "price_step": "Шаг цены",
    "slippage_steps": "Проскальзывание, шагов цены",
}


def settings_text(
    engine: EngineSettings, strategy: EmaReverseSettings, *, strategy_title: str
) -> str:
    """Снимок настроек прогона: движок целиком, модуль целиком и его правило.

    Текст, а не набор полей: колонка `settings` заведена под человеческий
    снимок и разбирается глазами, а не программой (`market.SessionRecord`).
    Набор полей поменяется, прочитанная фраза останется верной.

    ⚠️ **Правило словами обязано быть здесь, а не только в окне.** Снимок —
    единственное место, по которому через месяц разбирают, чем гнали прогон;
    список значений полей на этот вопрос отвечает наполовину — «период 15,
    порог 0» не говорит, что робот с ними делал. Числа и правило, собранное
    из тех же чисел, расходятся невозможным образом: описание строится
    из той же таблицы утверждений, что и решения модуля.

    ⚠️ Название алгоритма приходит **доводом**, а правило собирается по его
    настройкам. Разъехаться они не могут только потому, что название берётся
    у реестра по выбранному имени (`convert.strategy_title`), а не пишется
    константой на месте вызова: константа осталась бы прежней после смены
    алгоритма, и снимок назвал бы не то, чем гнали прогон.

    ⚠️ Абзацы правила пишутся **без отступа** намеренно: `snapshot_marks`
    считает подписью поля всё, что начинается с отступа и содержит «: ».
    Отступ здесь превратил бы предложение описания в поле снимка, и сверка
    набора с прогоном (`_made_with`) стала бы искать это «поле» в чужих
    записях.
    """
    lines = _block("Настройки движка", engine, _ENGINE_TITLES)
    lines += _block(
        f"Торговый алгоритм: {strategy_title}", strategy, _STRATEGY_TITLES
    )
    lines += ["", "Правило робота словами:"]
    lines += convert.rule_of(strategy).splitlines()
    return "\n".join(lines)


def period_note(
    symbol: str,
    timeframe: str,
    candles: Sequence[MarketCandle],
    *,
    days: int,
    until: datetime | None,
) -> str:
    """На чём гнали: границы отрезка и то, каким отбором они получились.

    Границы разрешённые, а не ключи: `--days 30` считается от последней
    свечи **в базе**, и завтра тот же ключ означает другой период. Ключи
    записаны рядом — они объясняют, откуда границы взялись, но повторить
    прогон позволяют именно границы.

    Отрезок называется по **закрытию** первой и последней свечи — так же,
    как строка «Прогон по истории» в журнале решений и как ось графика.
    Разойдись эти подписи на один таймфрейм, сверять журнал с графиком
    стало бы нельзя.
    """
    first, last = candles[0].close_time, candles[-1].close_time
    chosen = "вся история в базе" if days <= 0 else f"последние дни, штук {days}"
    edge = "последняя свеча в базе" if until is None else f"{fmt_datetime(until)} МСК"
    return (
        f"{symbol}, {timeframe}: свечей {len(candles)}, "
        f"с {fmt_datetime(first)} по {fmt_datetime(last)} МСК.\n"
        f"{_STEP}Отбор: {chosen}; правый край: {edge}."
    )


def result_note(run: HistoryRun) -> str:
    """Что вышло — и на каких издержках это посчитано.

    Издержки в той же строке намеренно. «Прибыль 2 923 ₽» без «комиссия
    3 556 ₽ при тарифе 14 ₽ за контракт на сторону» читается как результат
    стратегии, а является результатом стратегии на конкретном тарифе
    (`backtest.HistoryRun`). Различить два таких прогона по списку сделок
    нельзя: издержки в решения движка не входят вовсе.
    """
    it = run.summary
    factor = (
        "не определён"
        if it.profit_factor is None
        else _number(round(it.profit_factor, 2))
    )
    lines = [
        f"Сделок {it.trades}, переворотов {it.reversals}, свечей {run.bars}.",
        f"{_STEP}Валовая {fmt_money(it.gross_profit, sign=True)}, "
        f"комиссия {fmt_money(it.commission)}, "
        f"чистая {fmt_money(it.net_profit, sign=True)}.",
        f"{_STEP}Профит-фактор {factor}, прибыльных {it.profitable} из {it.trades} "
        f"({fmt_share(it.profitable_share)}), просадка {fmt_money(it.max_drawdown)}.",
    ]
    if run.halted:
        lines.append(f"{_STEP}Робот остановлен на прогоне: {run.halted}")
    lines += _block("Издержки прогона", run.costs, _COSTS_TITLES)
    return "\n".join(lines)


# ------------------------------------------------------------------ запись

@dataclass(frozen=True, slots=True)
class RunConditions:
    """Всё, чем задан один прогон, кроме самих свечей.

    Значение, а не девять доводов функции: набор растёт (`origin` станет
    настоящим на боевом режиме, за ним придут предохранители), и подпись
    из девяти позиций читалась бы на месте вызова как набор строк подряд.

    ⚠️ Свечей здесь нет намеренно. Условия известны до чтения базы, ряд —
    после; сложив их в одно значение, пришлось бы либо тянуть весь ряд
    в снимок настроек, либо собирать условия дважды.
    """

    origin: RunOrigin
    symbol: str
    timeframe: str
    engine: EngineSettings
    strategy: EmaReverseSettings
    strategy_title: str
    app_version: str
    #: Ключ `--days`: сколько последних календарных дней взято. 0 — вся история.
    days: int = 0
    #: Ключ `--until`: правый край. `None` — последняя свеча в базе.
    until: datetime | None = None

    def record(self, candles: Sequence[MarketCandle]) -> SessionRecord:
        """Объявление прогона — то, что уходит в базу при его открытии.

        Итога здесь нет: он известен только после прогона и ложится
        в `finish_note` при закрытии (`RunLog`). Условия — до, результат —
        после; прогон, оборванный посередине, остаётся в базе со своими
        условиями и без времени конца, то есть читается как прерванный,
        а не как пустой.
        """
        return SessionRecord(
            origin=self.origin,
            symbol=self.symbol,
            timeframe=self.timeframe,
            strategy=self.strategy_title,
            settings=settings_text(
                self.engine, self.strategy, strategy_title=self.strategy_title
            ),
            app_version=self.app_version,
            note=period_note(
                self.symbol, self.timeframe, candles,
                days=self.days, until=self.until,
            ),
        )


@dataclass(slots=True)
class RunEntry:
    """Открытый прогон: его номер в базе и итог, который в него запишут.

    `id is None` — открыть не удалось (база занята, диска нет). Прогон при
    этом идёт: график владельцу счёта важнее записи о графике, а о том,
    что записи не будет, ему уже сказано строкой журнала.
    """

    id: int | None = None
    note: str = ""


class RunLog:
    """Строка `journal_session` вокруг прогона: открыть до, закрыть после.

    Единица работы с компенсацией (`CLAUDE.md`, правило 9): у прогона два
    исхода, и оба обязаны оставить в базе правду. Прогон удался — строка
    закрывается итогом; отказал — тем же закрытием, но с причиной отказа;
    снят при выходе из программы — не закрывается вовсе, и `finished_at`
    остаётся пустым, что и означает «прервано».

    ⚠️ **Запись не имеет права уронить прогон.** Отказ базы здесь — это
    отсутствие записи, а не отсутствие графика; поэтому оба обращения
    к хранилищу обёрнуты, а о неудаче говорится строкой журнала решений,
    не молчанием. Обратное — прогон, падающий из-за журнала, — стоило бы
    владельцу счёта картинки, ради которой он программу и открыл.
    """

    def __init__(
        self,
        worker: MarketWorker,
        *,
        warn: Callable[[str, str], None],
        note: Callable[[str, str], None],
        keep: int = RUNS_KEPT,
    ) -> None:
        """Кому писать и сколько хранить.

        :param warn: строка-предупреждение в журнал решений: событие, причина.
        :param note: обычная строка журнала решений — теми же двумя доводами.
        :param keep: сколько прогонов по истории хранить. Обоснование
            умолчания — `RUNS_KEPT`.
        """
        self._worker = worker
        self._warn = warn
        self._note = note
        self._keep = keep

    @asynccontextmanager
    async def around(self, record: SessionRecord) -> AsyncIterator[RunEntry]:
        """Обернуть прогон записью. Тело обязано положить итог в `entry.note`."""
        entry = RunEntry(id=await self._open(record))
        try:
            yield entry
        except asyncio.CancelledError:
            # ⚠️ Снятый прогон не закрывается вовсе, и это намеренно.
            # Снимают его при выходе из программы (`HistoryPort.aclose`),
            # когда хранилище закрывается следом: ещё одно обращение к нему
            # либо повиснет в очереди, либо отвергнется. Прогон без времени
            # конца — честная запись о том, что программу закрыли на середине,
            # и ровно так его читает `--runs`.
            raise
        except Exception as error:
            await self._close(entry.id, f"Прогон не удался: {error}")
            raise
        await self._close(entry.id, entry.note)

    async def _open(self, record: SessionRecord) -> int | None:
        try:
            session, pruned = await self._worker.open_journal_session(
                record, keep_backtest_sessions=self._keep
            )
        except Exception as error:  # noqa: BLE001 — прогон важнее записи о нём
            self._warn(
                "Прогон не записан",
                f"Не удалось открыть прогон в журнале: {error}. Сам прогон идёт, "
                "но восстановить потом, с какими настройками он сделан, будет "
                "нечем.",
            )
            return None
        if not pruned.empty:
            # Молча выброшенных записей не бывает, даже воспроизводимых:
            # без этой строки прогон, которого владелец счёта не нашёл
            # в `--runs`, выглядел бы как никогда не сделанный.
            self._note("Старые прогоны убраны", pruned.summary())
        return session.id

    async def _close(self, session_id: int | None, note: str) -> None:
        if session_id is None:
            return
        try:
            await self._worker.finish_journal_session(session_id, note=note)
        except Exception as error:  # noqa: BLE001 — прогон важнее записи о нём
            self._warn(
                "Прогон записан не до конца",
                f"Не удалось закрыть прогон {session_id} в журнале: {error}. "
                "В базе он останется без итога и без времени конца, то есть "
                "будет читаться как прерванный.",
            )


# ------------------------------------------- прогоны, сделанные этим набором

#: Подписи полей, которых набор настроек из окна **не задаёт**.
#:
#: Режим живёт на панели управления, а не в окне настроек; торговля в выходные,
#: неполные свечи, ожидание исполнения и дни, названные биржей, берутся
#: у прежних настроек движка (`app/convert.py::_ENGINE_FROM_BASE`). Сравнивать
#: по ним — значит не найти ни одного прогона там, где он есть.
#:
#: ⚠️ Берётся из той же таблицы подписей, что и сам снимок: переименуй поле
#: в `_ENGINE_TITLES`, и исключение переименуется вместе с ним. Список строк
#: руками разошёлся бы молча — сравнение стало бы строже, чем задумано,
#: и статистика шаблона просто исчезла бы.
_UNCONTROLLED_TITLES: frozenset[str] = frozenset(
    _ENGINE_TITLES[name]
    for name in (
        "mode",
        "trade_in_weekend",
        "partial_candles",
        "close_wait_bars",
        "exchange_days",
    )
)

#: Режим, на котором собирается снимок набора для сравнения. Любой: его подпись
#: в сравнении не участвует (`_UNCONTROLLED_TITLES`).
_SNAPSHOT_MODE: Final[Mode] = Mode.REVERSE

#: «Сделок 143, переворотов 78, свечей 21811.» — первая строка `result_note`.
_TRADES_FOUND = re.compile(r"Сделок (\d+)")

#: «… чистая −6 335,00 ₽.» — вторая строка `result_note`.
#:
#: ⚠️ Два знака здесь **не те, на которые похожи**, и оба ставит `fmt_money`:
#: минус типографский (U+2212), а разряды и рубль отбиты неразрывным пробелом
#: (U+00A0). Обычный дефис и обычный пробел в этой строке не встречаются
#: никогда — на этом первая редакция выражения и не нашла ни одной суммы,
#: показав прочерк там, где деньги были. Отсюда `\s`, а не пробел.
_NET_FOUND = re.compile(r"чистая ([−+]?[\d\s]+,\d+)\s₽")

#: «с 19.06.2026 10:00 по 26.08.2026 23:45 МСК» — строка `period_note`.
_PERIOD_FOUND = re.compile(
    r"с (\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}) по (\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}) МСК"
)


def snapshot_of(values: Settings) -> str:
    """Снимок набора настроек окна — тем же текстом, что уходит в журнал.

    Ровно та же функция (`settings_text`), что записывает прогон. Отдельный
    вид снимка для окна означал бы два описания одного набора, и человек
    сверял бы глазами два разных текста про одно и то же.
    """
    engine = convert.engine_settings(values, _SNAPSHOT_MODE)
    module = convert.strategy_settings(values)
    return settings_text(
        engine, module, strategy_title=convert.strategy_title(values)
    )


def snapshot_marks(text: str) -> dict[str, str]:
    """Снимок настроек словами → «подпись поля» → «значение».

    Читатель того, что пишет `_block`: подписи полей идут с отступом,
    заголовки блоков — без него. Разбор текста здесь не «вторая правда»:
    текст — единственная форма, в которой снимок хранится
    (`market.SessionRecord.settings`), а связь читателя с писателем стережёт
    тест на полный оборот.
    """
    marks: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith(_STEP):
            continue
        title, sign, value = line.strip().partition(": ")
        if sign:
            marks[title] = value
    return marks


def _made_with(session: JournalSession, marks: Mapping[str, str], symbol: str,
               timeframe: str) -> bool:
    """Сделан ли прогон этим набором настроек.

    Требование одностороннее: **каждая** подпись набора обязана совпасть
    с записанной. Обратное не требуется — прогон более новой сборки несёт
    поля, которых у набора нет, и это не повод его не узнать.

    Прогон, в снимке которого подписи нет вовсе (запись более старой сборки),
    не совпадает: «не знаем» здесь обязано читаться как «не он». Приписать
    шаблону чужие деньги хуже, чем показать прочерк.
    """
    if session.symbol != symbol or session.timeframe != timeframe:
        return False
    stored = snapshot_marks(session.settings)
    return all(stored.get(title) == value for title, value in marks.items())


def _money_of(text: str) -> float | None:
    """Число рублей из записи. Не разобралось — `None`, а не ноль."""
    # `split()` убирает любые пробелы, включая неразрывный: `fmt_number`
    # отбивает разряды именно им.
    body = "".join(text.split()).replace("−", "-").replace("+", "").replace(",", ".")
    try:
        return float(body)
    except ValueError:
        return None


def run_of(session: JournalSession) -> TemplateRun:
    """Прогон журнала → строка статистики шаблона.

    ⚠️ Числа читаются из записи, а не считаются заново. Не нашлись — `None`:
    прогон прерван, не удался или закрыт без итога. Ноль здесь стоял бы
    утверждением «дало ноль рублей», а это другое.
    """
    trades = _TRADES_FOUND.search(session.finish_note)
    net = _NET_FOUND.search(session.finish_note)
    period = _PERIOD_FOUND.search(session.note)
    return TemplateRun(
        started_at=session.started_at,
        origin=session.origin.label,
        symbol=session.symbol,
        timeframe=session.timeframe,
        period=f"{period.group(1)} — {period.group(2)}" if period else "",
        trades=int(trades.group(1)) if trades else None,
        profit=_money_of(net.group(1)) if net else None,
        note=session.finish_note,
    )


def matching_runs(
    database: pathlib.Path,
    sets: Sequence[Settings],
    *,
    limit: int = RUNS_KEPT,
) -> RunStats:
    """Прогоны, сделанные каждым из поданных наборов настроек. Только чтение.

    Связь «этот прогон сделан этим набором» берётся сравнением снимков:
    прогон уже пишет снимок настроек в журнал (решение 0032), и цифры
    поэтому **не считаются заново**. Считать их отдельно значило бы завести
    вторую правду о деньгах — ровно то, из-за чего 05.09.2026 не удалось
    восстановить настройки убыточного прогона.

    ⚠️ Разные отрезки **не складываются**. Один набор мог гоняться на трёх
    периодах с разным итогом; здесь возвращается список прогонов, а сложить
    их в одну сумму окно не имеет права — это была бы подгонка на глаз.
    """
    empty = tuple(() for _ in sets)  # type: tuple[tuple[TemplateRun, ...], ...]
    if not database.exists():
        return RunStats(empty, (
            f"Базы {database} ещё нет, поэтому про прогоны сказать нечего. "
            "Она появится при первой загрузке истории."
        ))
    try:
        with CandleStore(database) as store:
            page = store.journal_sessions(limit=limit)
    except Exception as error:  # noqa: BLE001 — фраза человеку важнее типа
        return RunStats(empty, (
            f"Прогоны прочитать не удалось: {error}. Настройки шаблонов "
            "показаны, статистики у них нет."
        ))
    troubles: list[str] = []
    found: list[tuple[TemplateRun, ...]] = []
    for values in sets:
        try:
            marks = {
                title: value
                for title, value in snapshot_marks(snapshot_of(values)).items()
                if title not in _UNCONTROLLED_TITLES
            }
            symbol = convert.instrument_of(values.instrument)
        except (convert.SettingsRefused, ValueError, TypeError) as error:
            troubles.append(f"Набор пропущен: {error}")
            found.append(())
            continue
        found.append(tuple(
            run_of(session)
            for session in page.rows
            if _made_with(session, marks, symbol, values.timeframe)
        ))
    return RunStats(tuple(found), " ".join(troubles))


# ---------------------------------------------- створка двери окна наружу

def _library() -> Path:
    """Где программа держит свои файлы. Библиотека шаблонов ложится туда же."""
    return userdata_dir()


#: Окно спрашивает у слоя сборки то, что ему считать не положено: перечень
#: изменений, статистику набора и снимок настроек словами (`ui/backend.py`).
#: Створка вставляется здесь, при импорте: `app/port.py` импортирует этот
#: модуль всегда, а `ui/` импортировать `app/` не имеет права
#: (ARCHITECTURE.md §2).
#:
#: ⚠️ Ключ `--db` сюда пока не доходит: база берётся умолчанием
#: (`market.default_db_path`). Владелец счёта запускает программу без ключа,
#: но при `--db FILE` статистика шаблонов читалась бы из другой базы —
#: записано в `BACKLOG.md`.
backend.use(backend.Backend(
    changes=convert.settings_diff,
    runs=lambda sets: matching_runs(default_db_path(), sets),
    snapshot=snapshot_of,
    userdata=_library,
))


# ------------------------------------------------------------------ чтение

def _indented(text: str) -> list[str]:
    """Многострочное поле записи под своей подписью. Пустое поле строк не даёт.

    Отступ вдвое: подпись («На чём:», «С чем:») уже стоит на одном шаге,
    и содержимое обязано быть глубже неё — иначе список читается как один
    сплошной уровень, и не видно, где кончается одно поле и начинается другое.
    """
    return [f"{_STEP}{_STEP}{line}" for line in text.splitlines() if line.strip()]


def session_lines(session: JournalSession) -> list[str]:
    """Один прогон для консоли. Порядок строк — порядок вопросов человека.

    Сперва «когда и что это было», потом «на чём», потом «с чем», и только
    в конце «что вышло»: итог без условий — то самое число без условий,
    ради которого запись и заведена.
    """
    ended = (
        fmt_datetime(session.finished_at) + " МСК"
        if session.finished_at is not None
        else "не закрыт — программу закрыли на середине прогона"
    )
    lines = [
        f"Прогон {session.id} — {session.origin.label}",
        f"{_STEP}Начат:   {fmt_datetime(session.started_at)} МСК",
        f"{_STEP}Окончен: {ended}",
        f"{_STEP}Инструмент: {session.symbol or '—'}, {session.timeframe or '—'}",
        f"{_STEP}Торговый алгоритм: {session.strategy or '—'}",
        f"{_STEP}Версия программы: {session.app_version or '—'}",
    ]
    if session.note:
        lines += [f"{_STEP}На чём:"] + _indented(session.note)
    if session.settings:
        lines += [f"{_STEP}С чем:"] + _indented(session.settings)
    lines += [f"{_STEP}Итог:"] + (
        _indented(session.finish_note) if session.finish_note
        else [f"{_STEP}{_STEP}итога нет"]
    )
    return lines


def run_lines(sessions: Sequence[JournalSession], database: pathlib.Path) -> list[str]:
    """Список прогонов для консоли. Пустой список — это ответ, а не пустота."""
    if not sessions:
        return [
            f"В базе {database} нет ни одного записанного прогона.",
            "Записи появляются сами при каждом прогоне робота по истории — "
            "то есть при запуске окна и при каждом изменении настроек.",
        ]
    lines = [f"Прогоны в базе {database}, свежие первыми:", ""]
    for session in sessions:
        lines += session_lines(session)
        lines.append("")
    return lines


def show_runs(
    database: pathlib.Path,
    *,
    limit: int = RUNS_SHOWN,
    origin: RunOrigin | None = None,
    out: TextIO,
) -> int:
    """Напечатать последние прогоны. Только чтение: ни одной записи.

    База не найдена — отказ с фразой, а не пустой список: «прогонов нет»
    и «файла нет» человек лечит по-разному.
    """
    if not database.exists():
        out.write(
            f"Базы свечей нет: {database}\n"
            "Прогоны записываются в неё же; она появится при первой загрузке "
            f"истории. {how_to_fetch('MXU6')}\n"
        )
        return 1
    with CandleStore(database) as store:
        page = store.journal_sessions(origin=origin, limit=limit)
    lines = run_lines(page.rows, database)
    if page.truncated:
        lines.append(
            f"Показаны последние {limit}; в базе есть и более старые — "
            f"спросите больше: --runs {limit * 2}"
        )
    out.write("\n".join(lines).rstrip() + "\n")
    return 0 if page.rows else 1
