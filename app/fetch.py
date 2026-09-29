"""Ключи `--fetch` и `--inspect`: загрузить историю и посмотреть, что в базе.

Зачем это существует
--------------------
Инструмент в настройках — свободная строка. Сменил её — и график пуст:
своих свечей у нового инструмента в базе нет, а взять их программе было
неоткуда. Механизм загрузки при этом работал, но звался только из тестов.
Здесь — вход из командной строки::

    python3 -m app.main --fetch MXZ6

Тридцать дней, срочный рынок, та же база, что у программы. Больше человеку
знать ничего не надо; остальные ключи — уточнения, и у каждого есть умолчание.

Почему без окна и без цикла событий
-----------------------------------
`--fetch` не поднимает Qt вовсе и возвращается, когда загрузка кончилась.
Три довода, и ни один не про удобство:

* **работает там, где нет экрана.** Загрузка истории — ровно то, что делают
  по ssh или на машине сборки;
* **ход работы видно сразу.** Строка обновляется по мере страниц, а не
  копится в буфере до конца;
* **`MarketWorker` здесь не нужен и вреден.** Он существует, чтобы синхронная
  загрузка не останавливала цикл событий (решение 0005): цикла нет — значит
  нет и того, что надо защищать, а поток появился бы только ради того,
  чтобы его немедленно дождаться.

⚠️ Кнопка в окне сделает **то же самое**, позвав `market.history.load_history`.
Вся логика — там; здесь только разбор ключей, печать и код возврата.

Второй ключ — `--inspect`
-------------------------
::

    python3 -m app.main --inspect MXU6

Опись того, что уже лежит в базе: сколько дней, какие плотные, какие
огрызки, с какого дня рядом можно торговать. Появился по прямому поводу:
на вопрос «почему я вижу данные только с августа» ответить было нечем,
и разбор ушёл в сторону «загрузчик недокачал». Данные оказались целыми —
дальний фьючерс просто почти не торгуется. Считает опись `market.inventory`,
здесь только печать.
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import TextIO

from app.invocation import how_to_fetch
from market import (
    DENSE_SHARE,
    MARKETS,
    MSK,
    CandleStore,
    ContractError,
    FetchResult,
    HistoryOutcome,
    HistoryRequest,
    Inventory,
    IssClient,
    IssError,
    IssUnknownInstrument,
    Leg,
    StitchReport,
    adopt_chain,
    daily_volume,
    default_db_path,
    ensure_userdata_dir,
    is_synthetic,
    legs_of_chain,
    load_history,
    refresh_contracts,
    stitch,
    take_inventory,
    userdata_dir,
)
from market.paths import say_ignored_override

log = logging.getLogger(__name__)

__all__ = [
    "CHAIN_DB_FILE_NAME",
    "STITCH_DEPTH_DAYS",
    "ChainRequest",
    "adopt_into_working_base",
    "build_chain",
    "chain_lines",
    "fetch_history",
    "inventory_lines",
    "main",
    "run_from_arguments",
    "show_inventory",
]

#: Куда кладётся сшитый ряд. **Не рабочая база программы**: собранный ряд —
#: не инструмент биржи, и в `candles.sqlite3` его не пускает сторож
#: (`market.synthetic`). Отдельный файл в той же папке `userdata/`: резервная
#: копия по-прежнему делается копированием папки (решение 0003).
CHAIN_DB_FILE_NAME = "chain.sqlite3"

#: Сколько последних дней минутной истории качается по каждому контракту цепочки.
#:
#: Число выведено из самой цепочки, а не выбрано: контракт бывает ближним
#: около трёх месяцев (замер 05.09.2026: MXZ5 ликвиден 72 дня, MXH6 — 71,
#: MXM6 — 71), и столько же нужно **до** этого, чтобы рубеж с предыдущим
#: контрактом считался по перекрытию, а не по одному дню. Полгода — это
#: то и другое с запасом; вся жизнь контракта (год с лишним) — это вдвое
#: больше запросов ради дней, в которые по нему проходило две сделки.
STITCH_DEPTH_DAYS = 180

#: Ширина стираемой строки хода работы. Меньше — хвост прошлой строки
#: остаётся на экране и складывается в бессмыслицу вроде «12 483 минутки83».
_LINE_WIDTH = 78


class Ticker:
    """Ход загрузки одной строкой, которая переписывает себя.

    ⚠️ `FetchResult` создаётся **на каждый кусок** (`market.sync` режет период
    по 30 дней), и его счётчики обнуляются вместе с ним. Складывать надо
    самим, иначе на годовой загрузке человек двенадцать раз увидит, как счёт
    сбрасывается к нулю, и решит, что загрузка идёт по кругу. Смена куска
    ловится сменой самого объекта, а не числами: числа могут совпасть.
    """

    def __init__(self, out: TextIO, symbol: str, *, live: bool | None = None) -> None:
        self._out = out
        self._symbol = symbol
        self._live = out.isatty() if live is None else live
        self._current: FetchResult | None = None
        self._done_candles = 0
        self._done_pages = 0

    def __call__(self, result: FetchResult) -> None:
        """Показать, сколько уже пришло. Зовётся на каждой странице ISS."""
        if result is not self._current:
            if self._current is not None:
                self._done_candles += self._current.loaded
                self._done_pages += self._current.pages
            self._current = result
        candles = self._done_candles + result.loaded
        pages = self._done_pages + result.pages
        seen = f"{result.last_seen:%d.%m.%Y %H:%M}" if result.last_seen else "—"
        line = f"  {self._symbol}: {candles} свечей, по {seen} МСК, страниц {pages}"
        if self._live:
            self._out.write("\r" + line.ljust(_LINE_WIDTH))
        elif pages % 20 == 0:
            self._out.write(line + "\n")
        self._out.flush()

    def close(self) -> None:
        """Убрать за собой строку хода работы, чтобы итог начался с чистой."""
        if self._live and self._current is not None:
            self._out.write("\r" + " " * _LINE_WIDTH + "\r")
            self._out.flush()


def summary(outcome: HistoryOutcome) -> str:
    """Итог загрузки — таблицей «что» / «сколько», а не строкой в подбор.

    Поля перечислены **одной таблицей**: строка отчёта, которую забыли
    добавить рядом с полем, — это то же самое, что её отсутствие, только
    найти труднее. Пустые строки отсеиваются здесь же, поэтому добавление
    поля не требует помнить про его условие показа в другом месте.
    """
    report = outcome.report
    period = "—"
    if outcome.since is not None and outcome.until is not None:
        days = (outcome.until - outcome.since).days + 1
        period = (
            f"{outcome.since:%d.%m.%Y} … {outcome.until:%d.%m.%Y} "
            f"({days} дн.)"
        )
    rows: list[tuple[str, str]] = [
        ("инструмент", f"{outcome.symbol}, {outcome.request.market.title}"),
        ("глубина биржи", _declared(outcome)),
        ("запрошено", period + (", подрезано под глубину" if outcome.clamped else "")),
    ]
    if report is not None:
        rows += [
            ("пришло с биржи", f"{report.fetched} свечей"),
            (
                "записано",
                f"{report.inserted} новых, {report.updated} обновлено"
                + (f", {report.kept} отвергнуто" if report.kept else ""),
            ),
            ("минут без свечи", f"{report.missing_minutes} (в минуту без сделок "
                                "биржа свечу не отдаёт)"),
            ("дни без ответа", str(len(report.incomplete)) if report.incomplete else ""),
        ]
    rows += [
        (
            f"баров {outcome.request.timeframe.name}",
            f"{outcome.bars} закрытых, для прогрева нужно {outcome.request.warmup}",
        ),
        ("заняло", f"{outcome.seconds:.0f} с"),
    ]
    width = max(len(name) for name, value in rows if value)
    return "\n".join(f"  {name.ljust(width)}  {value}" for name, value in rows if value)


def _declared(outcome: HistoryOutcome) -> str:
    """Что биржа заявила о минутной истории — или почему не заявила."""
    if outcome.border is not None:
        return (
            f"{outcome.border.begin:%d.%m.%Y} … {outcome.border.end:%d.%m.%Y} "
            f"({outcome.border.days} дн.)"
        )
    return f"не удалось спросить: {outcome.border_error}" if outcome.border_error else ""


def fetch_history(
    database: pathlib.Path,
    request: HistoryRequest,
    *,
    out: TextIO,
    client: IssClient | None = None,
) -> int:
    """Загрузить историю в эту базу и рассказать, что вышло. Код возврата процесса.

    Ноль — история загружена и её хватает. Единица — не хватает или не вышло
    вовсе, и тогда напечатано, **что именно** не так: опечатка в коде, чужой
    рынок, период вне истории, пустой период, мало данных для прогрева.

    ⚠️ Отдельный `try` вокруг сети намеренно: обрыв на середине годовой
    загрузки — обычное дело, и скачанное к тому моменту уже записано
    и отмечено (`market.sync`). Трассировка на экране сказала бы человеку,
    что всё пропало, тогда как повторный запуск продолжит с места обрыва.
    """
    iss = client or IssClient()
    try:
        # `D-128`: дальше идёт код биржи, а не набранный — `sber` лёг бы
        # в базу отдельным инструментом, которого окно не найдёт.
        request = replace(request, symbol=iss.secid(request.symbol))
    except IssError as error:
        out.write(f"Загрузка не начата: {error}\n")
        return 1
    ticker = Ticker(out, request.symbol)
    out.write(f"Загрузка истории {request.symbol} с МосБиржи…\n")
    out.flush()
    try:
        with CandleStore(database) as store:
            outcome = load_history(store, iss, request, progress=ticker)
    except IssError as error:
        ticker.close()
        out.write(
            f"Загрузка прервана: {error}\n"
            "Скачанное до обрыва записано и отмечено — повторный запуск "
            "продолжит с этого места, а не с начала.\n"
        )
        return 1
    ticker.close()
    out.write(summary(outcome) + "\n")
    trouble = outcome.problem()
    if trouble is None:
        out.write("Готово. Отчёт о загрузке записан в журнал базы.\n")
        return 0
    out.write("\n" + trouble + "\n")
    return 1


def run_from_arguments(
    args: argparse.Namespace,
    database: pathlib.Path,
    *,
    out: TextIO = sys.stdout,
    client: IssClient | None = None,
) -> int:
    """Собрать запрос из ключей командной строки и выполнить его.

    Правая граница — сегодня, и ключа для неё нет намеренно. История грузится
    «по сейчас»; человек, которому нужен зафиксированный правый край, в этом
    сценарии не существует, а лишний ключ надо объяснять каждому, кто читает
    `--help`.

    Левая граница — три способа, по убыванию явности: `--fetch-since` (дата),
    `--fetch-days N` (N последних дней, считая сегодняшний), `--fetch-days 0`
    («всё, что отдаёт биржа», — то же значение и тот же смысл, что у `--days`
    в показе истории).

    ⚠️ Период средней сюда **не приезжает из `strategies`**. Сборка не имеет
    права брать у торгового слоя ничего сверх названного в
    `tests/test_app_boundaries.py`, и число прогрева живёт в `market.history`
    (`WARMUP_PERIOD`), а связь с модулем стратегии держит тест.
    """
    # `D-124`: переменная папки данных, выставленная вне теста, не действует,
    # и человек, её выставивший, искал бы свечи не там. Технический лог
    # к этой минуте уже поднят (`app/main.py`), строка уходит и в него.
    said = say_ignored_override(database)
    if said:
        log.warning("%s", said)
    today = datetime.now(MSK).date()
    since: date | None = None
    if args.fetch_since is not None:
        since = args.fetch_since.date()
    elif args.fetch_days:
        since = today - timedelta(days=args.fetch_days - 1)
    try:
        ensure_userdata_dir(database.parent)
    except OSError as error:
        out.write(f"{error}\n")
        return 1
    request = HistoryRequest(
        symbol=args.fetch.strip(),
        market=MARKETS[args.fetch_market],
        until=today,
        since=since,
    )
    return fetch_history(database, request, out=out, client=client)


def inventory_lines(inventory: Inventory, database: pathlib.Path) -> list[str]:
    """Опись базы — человеческим языком, сверху итог, снизу помесячная таблица.

    Итог отвечает на вопрос, ради которого опись и написана: **с какого дня
    рядом можно торговать**. До этого дня данные тоже есть, но их единицы
    минут в день, и это не дефект загрузки, а дальний фьючерс.
    """
    if not inventory.days:
        return [
            f"{inventory.symbol}: в базе {database} нет ни одной свечи.",
            how_to_fetch(inventory.symbol),
        ]
    edge = int(inventory.busiest * DENSE_SHARE)
    first_dense = inventory.first_dense
    rows: list[tuple[str, str]] = [
        ("минутных свечей", str(inventory.minutes)),
        (
            "ряд",
            f"{inventory.first:%d.%m.%Y} … {inventory.last:%d.%m.%Y}, "
            f"{len(inventory.days)} дней подряд",
        ),
        (
            "самый плотный день",
            f"{inventory.busiest} свечей, {inventory.busiest_day:%d.%m.%Y} — "
            "это и есть мера полного дня",
        ),
        ("плотных дней", f"{len(inventory.dense)} (от {edge} свечей)"),
        ("редких", f"{len(inventory.sparse)} (сделки были, но их единицы)"),
        ("пустых", f"{len(inventory.empty)} (выходные, праздники, вне торгов)"),
        (
            "торговать можно",
            f"с {first_dense:%d.%m.%Y}" if first_dense else
            "нельзя: плотных дней в ряду нет вовсе",
        ),
        (
            "перезапросим",
            f"{len(inventory.to_request)} дн. при следующей загрузке"
            if inventory.to_request else "",
        ),
    ]
    width = max(len(name) for name, value in rows if value)
    lines = [f"{inventory.symbol} — что лежит в базе {database}", ""]
    lines += [f"  {name.ljust(width)}  {value}" for name, value in rows if value]
    lines += ["", "  месяц     дней  плотных  редких  пустых  медиана минут"]
    lines += [
        f"  {row.month}   {row.days:4d}  {row.dense:7d}  {row.sparse:6d}  "
        f"{row.empty:6d}  {row.median_minutes:13d}"
        for row in inventory.by_month()
    ]
    return lines


def _stored_key(symbols: list[str], symbol: str) -> list[str]:
    """Ключи базы, под которыми может лежать `symbol`.

    Точное совпадение — оно одно. Нет точного — все ключи, равные коду
    без учёта регистра: Si, загруженный до `D-128`, лежит под `SIZ6`,
    а биржа называет его `SiZ6`. Пустой список — в базе такого нет.
    """
    if symbol in symbols:
        return [symbol]
    folded = symbol.casefold()
    return [key for key in symbols if key.casefold() == folded]


def show_inventory(
    database: pathlib.Path,
    symbol: str,
    *,
    out: TextIO,
    client: IssClient | None = None,
) -> int:
    """Напечатать опись базы. Только чтение: ни одной записи не делается.

    База не найдена — это отказ с фразой, а не пустая опись: «минуток нет»
    и «файла нет» человек лечит по-разному.

    Код, набранный человеком, сначала приводится к коду биржи (`D-128`):
    `mxu6` ищет минуты `MXU6`. Сшитый ряд (`@MX`) бирже не известен
    и ищется как набран. Биржа недоступна — код ищется как набран, с фразой,
    что он не сверен: опись только читает базу, сеть ей не обязательна.
    Нет ключа точно — ищется без учёта регистра (Si до `D-128` лежит под
    `SIZ6`): одно совпадение показывается с пометкой, несколько — перечисляются.
    """
    symbol = symbol.strip()
    if not database.exists():
        out.write(
            f"Базы свечей нет: {database}\n"
            f"Она появится сама при первой загрузке. {how_to_fetch(symbol)}\n"
        )
        return 1
    checked = True
    if not is_synthetic(symbol):
        try:
            symbol = (client or IssClient()).secid(symbol)
        except IssUnknownInstrument as error:
            out.write(f"Опись не составлена: {error}\n")
            return 1
        except IssError as error:
            # Опись только читает базу: без биржи она ищет код как набран.
            checked = False
            out.write(
                f"Биржа недоступна ({error}), код {symbol!r} не сверен "
                "с кодом биржи — ищу в базе как набран.\n"
            )
    with CandleStore(database) as store:
        stored = _stored_key(store.symbols(), symbol)
        if len(stored) > 1:
            out.write(
                f"В базе несколько ключей для {symbol!r} без учёта регистра: "
                f"{', '.join(stored)}. Наберите нужный точно.\n"
            )
            return 1
        if stored and stored[0] != symbol:
            out.write(
                f"В базе ключ в другом регистре: {stored[0]}"
                + (f" (код биржи {symbol})" if checked else "")
                + ". Показываю его; база не переименована.\n"
            )
            symbol = stored[0]
        inventory = take_inventory(store, symbol)
    out.write("\n".join(inventory_lines(inventory, database)) + "\n")
    return 0 if inventory.days else 1


# -- сшивка ближних контрактов --------------------------------------------


def chain_lines(report: StitchReport, database: pathlib.Path) -> list[str]:
    """Итог сборки ряда — таблицей отрезков и таблицей стыков.

    Обе таблицы печатаются всегда, и стыки не сворачиваются в «всё хорошо»:
    разрыв цены на стыке — единственное место, где сшитый ряд отличается
    от настоящего инструмента, и человек обязан видеть его числом.
    """
    lines = [f"{report.symbol} — сшитый ряд в базе {database}", ""]
    lines.append("  контракт  с            по           дней   свечей")
    for leg in report.legs:
        lines.append(
            f"  {leg.symbol:<8}  {leg.since:%d.%m.%Y}   {leg.until:%d.%m.%Y}  "
            f"{leg.days:5d}  {report.by_leg.get(leg.symbol, 0):7d}"
        )
    lines += ["", "  стык                 рубеж        разрыв цены  измерен"]
    for seam in report.seams:
        gap = seam.gap
        percent = seam.gap_percent
        said = (
            f"{gap:+9.1f} ({percent:+.2f} %)"
            if gap is not None and percent is not None
            else "        не измерен"
        )
        when = f"{seam.at:%d.%m.%Y %H:%M}" if seam.at else "общей минуты нет"
        lines.append(
            f"  {seam.leaving}→{seam.arriving:<8}      {seam.day:%d.%m.%Y}  "
            f"{said}  {when}"
        )
    span = (
        f"{report.first:%d.%m.%Y} … {report.last:%d.%m.%Y}"
        if report.first and report.last
        else "ряд пуст"
    )
    lines += [
        "",
        f"  ряд        {span}",
        f"  свечей     {report.written} (снято прошлых: {report.forgotten})",
        "",
        "  Ряд не инструмент биржи: заявку по нему подать нельзя, окно его "
        "не покажет.",
        f"  Проверка на истории: python -m backtest --symbol {report.symbol} "
        f"--db {database}",
    ]
    return lines


@dataclass(frozen=True, slots=True)
class ChainRequest:
    """Что человек попросил сшить.

    Отдельным описанием, а не пятью аргументами, — так же, как `HistoryRequest`
    в `market.history`: список контрактов, глубина и «ходить ли на биржу»
    путешествуют вместе и порознь смысла не имеют.
    """

    symbol: str
    #: Контракты в порядке экспирации. Первый в ряд не входит: он датирует
    #: начало второго тем же замером, что и все остальные рубежи.
    legs: Sequence[str]
    depth: int = STITCH_DEPTH_DAYS
    #: Ходить ли на биржу за минутками перед сборкой.
    fetch: bool = True


def build_chain(
    database: pathlib.Path,
    request: ChainRequest,
    *,
    out: TextIO,
    client: IssClient | None = None,
) -> int:
    """Загрузить контракты цепочки и собрать из них ряд. Код возврата процесса.

    Источник и цель — **один файл**, и это не экономия. Рубеж считается
    по объёмам самих контрактов, и без них проверить сборку нечем: сшитый ряд
    без контрактов, из которых он собран, — число без происхождения.
    Отдельный файл от рабочей базы при этом обязателен (`CHAIN_DB_FILE_NAME`).
    """
    symbol, legs = request.symbol, request.legs
    if not is_synthetic(symbol):
        out.write(
            f"«{symbol}» — код инструмента биржи. Сшитый ряд обязан называться "
            "иначе: он не торгуется, и имя должно это говорить. Начните имя "
            "с «@»: --stitch @MX\n"
        )
        return 1
    today = datetime.now(MSK).date()
    market = MARKETS["futures"]
    if request.fetch:
        iss = client or IssClient()
        try:
            legs = [iss.secid(leg) for leg in legs]  # `D-128`: коды биржи
        except IssError as error:
            out.write(f"Ряд не собран: {error}\n")
            return 1
        for leg in legs:
            ticker = Ticker(out, leg)
            out.write(f"Загрузка {leg} с МосБиржи…\n")
            out.flush()
            try:
                with CandleStore(database) as store:
                    load_history(
                        store,
                        iss,
                        HistoryRequest(
                            symbol=leg,
                            market=market,
                            since=today - timedelta(days=request.depth),
                            until=today,
                        ),
                        progress=ticker,
                    )
            except IssError as error:
                ticker.close()
                out.write(
                    f"Загрузка {leg} прервана: {error}\n"
                    "Скачанное записано; повторный запуск продолжит с этого "
                    "места. Ряд не собран.\n"
                )
                return 1
            ticker.close()
    with CandleStore(database) as store:
        volumes = [(leg, daily_volume(store, leg)) for leg in legs]
        empty = [leg for leg, days in volumes if not days]
        if empty:
            out.write(
                f"В базе нет ни одной свечи по: {', '.join(empty)}.\n"
                "Рубеж считается по объёмам самих контрактов — без них "
                "сшивать нечего.\n"
            )
            return 1
        try:
            chain: list[Leg] = legs_of_chain(volumes)
            report = stitch(store, store, symbol, chain)
        except ValueError as trouble:
            out.write(f"Ряд не собран: {trouble}\n")
            return 1
    out.write("\n".join(chain_lines(report, database)) + "\n")
    return 0


def _exchange_code(client: IssClient, leg: str) -> str:
    """Код биржи для контракта `--legs`; незнакомый бирже остаётся как набран.

    Незнакомый код здесь не отказ: `refresh_contracts` называет его строкой
    человеку и решает сам — дальний выпадает, код посреди цепочки
    останавливает перенос (`B-054`). Обрыв связи — отказ, как и был.
    """
    try:
        return client.secid(leg)
    except IssUnknownInstrument:
        return leg.strip()


def adopt_into_working_base(
    source: pathlib.Path,
    target: pathlib.Path,
    legs: Sequence[str],
    *,
    out: TextIO,
    client: IssClient | None = None,
) -> int:
    """Перенести контракты цепочки в рабочую базу и уточнить периоды у биржи.

    Два шага, и второй не обязателен. Перенос не ходит в сеть: минуты
    и отметки дней берутся из `source` только на чтение, периоды считаются
    по перенесённым минутам (`market.contracts.adopt_chain`). Уточнение
    у биржи (`client` не `None`) добавляет последний день обращения и рубеж
    контракта, которого в источнике нет, — без него действующим останется
    последний перенесённый, даже если он истёк.
    """
    try:
        if client is not None:
            # `D-128`: перенос идёт по имени, и `mxu6` в базе цепочки не найдётся.
            legs = [_exchange_code(client, leg) for leg in legs]
        with CandleStore(target) as store:
            report = adopt_chain(source, store, legs)
            for leg in legs:
                out.write(
                    f"{leg}: минут {report.minutes.get(leg, 0)}, "
                    f"записано {report.written.get(leg, 0)}, "
                    f"дней отмечено {report.days.get(leg, 0)}\n"
                )
            rows = report.contracts
            if client is not None:
                refreshed = refresh_contracts(store, client, legs, market=MARKETS["futures"])
                for text in refreshed.said:
                    out.write(f"{text}\n")
                rows = refreshed
    except (ContractError, IssError, sqlite3.Error) as error:
        out.write(f"Перенос не выполнен: {error}\n")
        return 1
    for row in rows:
        period = (
            f"{row.active_from or '?'} … {row.active_to or 'сейчас'}"
            if row.active_from or row.active_to
            else "период не установлен"
        )
        mark = (
            "архивный" if row.archived
            else "срок не проверен у биржи" if row.last_trade_day is None
            else "действующий" if row.current
            else ""
        )
        out.write(f"{row.symbol}: {period} {mark}".rstrip() + "\n")
        if row.current and row.last_trade_day is None:
            # `D-130`: действующий без срока — тот самый код, которым пойдёт
            # робот, и остановка перед экспирацией на нём не сработает.
            how = ", запустив --adopt без --no-fetch" if client is None else ""
            out.write(
                f"  Срок обращения {row.symbol} не проверен у биржи: возможно, "
                f"контракт уже истёк — уточните срок у биржи{how}.\n"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    """`python3 -m app.fetch --stitch @MX --legs MXM5,MXU5,…` — сборка ряда.

    ⚠️ **Своя точка входа, а не ключ у `app/main.py`,** и довод тот же, что
    у `python -m backtest` (решение 0048): сборка ряда не поднимает окно,
    не берёт замок папки данных и не трогает токен. Ключ у `app/main.py`
    связал бы годовую загрузку с интерфейсом.

    Загрузка одного контракта — около четырёх минут; шесть контрактов цепочки
    это двадцать с лишним. Повторный запуск идёт быстро: дни, за которые уже
    спрашивали, в запрос не попадают (`market.sync`).
    """
    parser = argparse.ArgumentParser(
        prog="python3 -m app.fetch",
        description="Сшивка ближних контрактов в один непрерывный ряд.",
    )
    parser.add_argument("--stitch", metavar="NAME", default="@MX",
                        help="имя собранного ряда; начинается с «@», по умолчанию @MX")
    parser.add_argument("--legs", metavar="CODES", required=True,
                        help="контракты через запятую, в порядке экспирации. "
                             "Первый в ряд не входит: он датирует начало второго")
    parser.add_argument("--db", metavar="PATH", default=None,
                        help=f"база цепочки, по умолчанию userdata/{CHAIN_DB_FILE_NAME}")
    parser.add_argument("--stitch-depth", type=int, default=STITCH_DEPTH_DAYS,
                        help="сколько последних дней качать по каждому контракту")
    parser.add_argument("--no-fetch", action="store_true",
                        help="не ходить на биржу: собрать из того, что уже в базе")
    parser.add_argument("--adopt", action="store_true",
                        help="не сшивать, а перенести контракты --legs из базы цепочки "
                             "в рабочую базу и заполнить периоды (решение 0061)")
    parser.add_argument("--target", metavar="PATH", default=None,
                        help="рабочая база для --adopt, по умолчанию userdata/candles.sqlite3")
    args = parser.parse_args(argv)
    database = (
        pathlib.Path(args.db) if args.db else userdata_dir() / CHAIN_DB_FILE_NAME
    )
    say_ignored_override(database)  # `D-124`: молча переменную не игнорируем
    try:
        ensure_userdata_dir(database.parent)
    except OSError as error:
        sys.stdout.write(f"{error}\n")
        return 1
    # Регистр не поднимается (`D-128`): у биржи `SiZ6`, и `SIZ6` лёг бы
    # в базу отдельным инструментом. Код биржи спрашивается у ISS там,
    # где идут в сеть (`build_chain`, `adopt_into_working_base`); с `--no-fetch`
    # контракты ищутся в базе как набраны.
    legs = [leg.strip() for leg in args.legs.split(",") if leg.strip()]
    if args.adopt:
        target = pathlib.Path(args.target) if args.target else default_db_path()
        return adopt_into_working_base(
            database, target, legs, out=sys.stdout,
            client=None if args.no_fetch else IssClient(),
        )
    return build_chain(
        database,
        ChainRequest(
            symbol=args.stitch.strip(),
            legs=legs,
            depth=args.stitch_depth,
            fetch=not args.no_fetch,
        ),
        out=sys.stdout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
