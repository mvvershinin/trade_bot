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
import pathlib
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TextIO

from app.invocation import how_to_fetch
from market import (
    DENSE_SHARE,
    MARKETS,
    MSK,
    CandleStore,
    FetchResult,
    HistoryOutcome,
    HistoryRequest,
    Inventory,
    IssClient,
    IssError,
    Leg,
    StitchReport,
    daily_volume,
    ensure_userdata_dir,
    is_synthetic,
    legs_of_chain,
    load_history,
    stitch,
    take_inventory,
    userdata_dir,
)

__all__ = [
    "CHAIN_DB_FILE_NAME",
    "STITCH_DEPTH_DAYS",
    "ChainRequest",
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
    ticker = Ticker(out, request.symbol)
    out.write(f"Загрузка истории {request.symbol} с МосБиржи…\n")
    out.flush()
    try:
        with CandleStore(database) as store:
            outcome = load_history(
                store, client or IssClient(), request, progress=ticker
            )
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
        symbol=args.fetch.strip().upper(),
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


def show_inventory(database: pathlib.Path, symbol: str, *, out: TextIO) -> int:
    """Напечатать опись базы. Только чтение: ни одной записи не делается.

    База не найдена — это отказ с фразой, а не пустая опись: «минуток нет»
    и «файла нет» человек лечит по-разному.
    """
    if not database.exists():
        out.write(
            f"Базы свечей нет: {database}\n"
            f"Она появится сама при первой загрузке. {how_to_fetch(symbol)}\n"
        )
        return 1
    with CandleStore(database) as store:
        inventory = take_inventory(store, symbol.strip().upper())
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
        for leg in legs:
            ticker = Ticker(out, leg)
            out.write(f"Загрузка {leg} с МосБиржи…\n")
            out.flush()
            try:
                with CandleStore(database) as store:
                    load_history(
                        store,
                        client or IssClient(),
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
    args = parser.parse_args(argv)
    database = (
        pathlib.Path(args.db) if args.db else userdata_dir() / CHAIN_DB_FILE_NAME
    )
    try:
        ensure_userdata_dir(database.parent)
    except OSError as error:
        sys.stdout.write(f"{error}\n")
        return 1
    legs = [leg.strip().upper() for leg in args.legs.split(",") if leg.strip()]
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
