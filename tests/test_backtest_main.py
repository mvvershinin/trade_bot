"""Запуск перебора: база владельца счёта только читается, число прогонов сказано вперёд.

⚠️ Главная проверка здесь — **снимок**. Перебор идёт минутами, а программа
в это время дописывает свечи; считать разные строки одной таблицы на разных
данных нельзя. Плюс `CandleStore` открывает базу на запись и мигрирует её
при открытии, поэтому настоящей базе его не показывают вовсе.
"""

from __future__ import annotations

import asyncio
import pathlib
import sqlite3
from datetime import date, datetime, time, timedelta

import pytest

import backtest.__main__
from backtest.__main__ import (
    COMMISSION,
    DEPTH,
    PACE,
    _arguments,
    _bars,
    _confirm,
    _cut,
    _days_of,
    _notes,
    _pace,
    _periods,
    _plan,
    _snapshot,
    _table,
    _trim,
    main,
)
from backtest.execution import Costs
from backtest.split import DENSE_ENOUGH, SWEEP_SPAN, Fold, Period
from backtest.sweep import Ground, full_cross, trading_mode
from engine import EngineSettings
from market.candles import MINUTE, Candle
from market.storage import CandleStore, Source
from tests.engine_helpers import MSK
from tests.pinned_clock import PinnedClock

SYMBOL = "MXU6"
#: «Сейчас» для команды во всём файле (`D-112`). `_bars` берёт ряд за `DEPTH`
#: дней от часов, а данные файла лежат в июне 2026: на машинных часах проверки
#: краснели сами с 21.07.2027 (сдвиг +400 дней — девять красных). Сентябрь
#: 2026 — после всех подставных дней и в пределах `DEPTH` от самого раннего.
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=MSK)


@pytest.fixture(autouse=True)
def the_clock_is_named_by_the_file(monkeypatch) -> None:
    """Команда берёт «сейчас» у `NOW`, а не у часов машины (`D-112`).

    Подменено имя `datetime` в одном модуле `backtest.__main__`, и только
    `now()`: рабочий код не менялся, глубина ряда считается тем же путём.
    """
    monkeypatch.setattr(backtest.__main__, "datetime", PinnedClock(NOW))


def _ground_for_test() -> Ground:
    """Условия перебора те же, что у команды: умолчания движка плюс тариф."""
    return Ground(
        engine=trading_mode(EngineSettings(commission_per_side=COMMISSION)),
        costs=Costs(commission_per_side=COMMISSION),
    )


class _Watched:
    """Обёртка над хранилищем, запоминающая, какие файлы ей открывали.

    Нужна ровно одной проверке: `CandleStore` открывает базу **на запись**
    и при открытии выполняет миграции. Показать ему файл владельца счёта —
    это и есть та порча, ради которой заведён снимок. Байтовое сравнение
    её не ловит: на базе свежей схемы миграция ничего не меняет, и тест
    зеленел бы до того дня, когда схема разойдётся.
    """

    seen: list[pathlib.Path] = []

    def __init__(self, path, **rest) -> None:
        _Watched.seen.append(pathlib.Path(path))
        self._real = CandleStore(path, **rest)

    def __enter__(self) -> CandleStore:
        return self._real.__enter__()

    def __exit__(self, *rest) -> None:
        self._real.__exit__(*rest)


def _minutes(day: date, *, count: int, since: time = time(9, 30)) -> list[Candle]:
    """Минутки одного дня подряд: цена ходит пилой, сигналы средней есть."""
    start = datetime.combine(day, since, tzinfo=MSK)
    return [
        Candle(
            time=start + timedelta(minutes=step),
            open=100.0 + step % 7, high=101.0 + step % 7,
            low=99.0 + step % 7, close=100.0 + (step + 1) % 7,
            volume=1.0, timeframe=MINUTE, filled_minutes=1,
        )
        for step in range(count)
    ]


def _database(path: pathlib.Path, *, days: int = 12, minutes: int = 340) -> pathlib.Path:
    """База с `days` буднями подряд, у каждого — минутки от 09:30."""
    file = path / "candles.sqlite3"
    with CandleStore(file) as store:
        day = date(2026, 6, 15)
        made = 0
        while made < days:
            if day.weekday() < 5:
                store.put_minutes(SYMBOL, _minutes(day, count=minutes), Source.ISS)
                made += 1
            day += timedelta(days=1)
    return file


# -- снимок ------------------------------------------------------------------

def test_the_original_database_is_not_touched_by_the_snapshot(tmp_path) -> None:
    """Стережёт главное обещание: база владельца счёта после перебора байт в байт та же."""
    source = _database(tmp_path)
    before = source.read_bytes()
    room = tmp_path / "room"
    room.mkdir()
    _snapshot(source, room)
    assert source.read_bytes() == before


def test_the_snapshot_is_a_different_file_with_the_same_candles(tmp_path) -> None:
    """Стережёт: работаем с копией, и в копии те же свечи, а не пустая база."""
    source = _database(tmp_path)
    room = tmp_path / "room"
    room.mkdir()
    copy = _snapshot(source, room)
    assert copy != source
    assert len(_bars(copy, SYMBOL)) > 0


def test_a_missing_database_is_a_loud_refusal(tmp_path) -> None:
    """Стережёт: пустой перебор не выдаётся за «сделок не нашлось»."""
    with pytest.raises(SystemExit, match="базы со свечами нет"):
        _snapshot(tmp_path / "нет-такой.sqlite3", tmp_path)


def test_a_symbol_without_candles_is_a_loud_refusal(tmp_path) -> None:
    """Стережёт: неизвестный инструмент отвергается, а не даёт таблицу из нулей."""
    copy = _database(tmp_path)
    with pytest.raises(SystemExit, match="нет свечей"):
        _bars(copy, "НЕТ-ТАКОГО")


def test_the_series_reaches_back_depth_days_from_the_clock_not_further(
    tmp_path, monkeypatch
) -> None:
    """Стережёт глубину ряда: день старше `DEPTH` от «сейчас» в перебор не идёт.

    «Сейчас» здесь своё и далеко от настоящего сегодня: на часах машины
    старый день прошёл бы в ряд, и проверка покраснела бы — так видно, что
    подмена часов в этом файле вообще действует, а не просто зелёная.
    """
    now = datetime(2028, 1, 10, 12, 0, tzinfo=MSK)
    monkeypatch.setattr(backtest.__main__, "datetime", PinnedClock(now))
    old, fresh = date(2026, 6, 15), date(2027, 12, 15)
    assert now - datetime.combine(old, time(0), tzinfo=MSK) > DEPTH
    file = tmp_path / "depth.sqlite3"
    with CandleStore(file) as store:
        for day in (old, fresh):
            store.put_minutes(SYMBOL, _minutes(day, count=340), Source.ISS)

    days = {bar.time.astimezone(MSK).date() for bar in _bars(file, SYMBOL)}

    assert days == {fresh}, f"в ряд попали не те дни: {sorted(days)}"


def _old_and_fresh(tmp_path) -> tuple[pathlib.Path, date, date]:
    """База с днём старше глубины по умолчанию от `NOW` и днём внутри неё."""
    old, fresh = date(2025, 6, 16), date(2026, 8, 17)
    assert NOW - datetime.combine(old, time(0), tzinfo=MSK) > DEPTH
    file = tmp_path / "cut.sqlite3"
    with CandleStore(file) as store:
        for day in (old, fresh):
            store.put_minutes(SYMBOL, _minutes(day, count=340), Source.ISS)
    return file, old, fresh


def test_the_days_key_reaches_further_back_than_the_default(tmp_path) -> None:
    """Стережёт `D-056`: ключ `--days` действительно задаёт глубину ряда.

    Мутация, обязанная ронять проверку: в `_bars` брать `DEPTH` вместо
    переданной глубины — или не передавать `args.depth` из `_work`.
    """
    file, old, fresh = _old_and_fresh(tmp_path)
    depth = _arguments(["--days", "500"]).depth
    assert depth == timedelta(days=500)

    days = {bar.time.astimezone(MSK).date() for bar in _bars(file, SYMBOL, depth=depth)}

    assert days == {old, fresh}, f"ключ глубины не подействовал: {sorted(days)}"


@pytest.mark.parametrize("text", ["0", "-5", "год"])
def test_a_depth_that_is_not_a_positive_number_of_days_is_refused(text, capsys) -> None:
    """Стережёт: нулевая или кривая глубина — отказ с именем ключа, а не пустой ряд."""
    with pytest.raises(SystemExit):
        _arguments(["--days", text])
    assert "--days" in capsys.readouterr().err


def test_a_cut_history_is_named_to_the_human(tmp_path, monkeypatch, capsys) -> None:
    """Стережёт `D-056`: история, подрезанная глубиной, названа вслух.

    Строка обязана дойти двумя путями: в поток ошибок (человек видит её до
    согласия на перебор) и в оговорки таблицы (таблицу читают потом, без
    экрана). Мутации, обязанные ронять проверку: `_cut` всегда возвращает
    `None`; `_work` не печатает строку; `_table` не отдаёт её в `_notes`.
    """
    file, old, _fresh = _old_and_fresh(tmp_path)
    seen: dict[str, object] = {}

    async def table(args, bars, *, cut=None) -> str:
        seen["cut"] = cut
        return " ".join(_notes(runs=1, spent=1.0, bars=len(bars), cut=cut))

    monkeypatch.setattr(backtest.__main__, "_table", table)
    assert main(["--db", str(file), "--symbol", SYMBOL, "--out", str(tmp_path / "t.txt")]) == 0

    cut = seen["cut"]
    assert isinstance(cut, str), "подрезка истории прошла молча"
    assert f"{old:%d.%m.%Y}" in cut and "--days" in cut, cut
    assert cut in capsys.readouterr().err, "строка о подрезке не дошла до экрана"
    assert cut in (tmp_path / "t.txt").read_text(encoding="utf-8")


def test_the_days_key_reaches_the_series_through_the_command(tmp_path, monkeypatch) -> None:
    """Стережёт `D-056` на пути команды: `--days` доезжает от ключа до ряда.

    Мутация, обязанная ронять проверку: `_work` зовёт `_bars` и `_cut`
    без `depth=args.depth` — ключ разбирается и молча не действует.
    """
    file, old, _fresh = _old_and_fresh(tmp_path)
    seen: dict[str, object] = {}

    async def table(args, bars, *, cut=None) -> str:
        seen["days"] = {bar.time.astimezone(MSK).date() for bar in bars}
        seen["cut"] = cut
        return "таблица"

    monkeypatch.setattr(backtest.__main__, "_table", table)
    assert main(["--db", str(file), "--symbol", SYMBOL, "--days", "500",
                 "--out", str(tmp_path / "t.txt")]) == 0

    days = seen["days"]
    assert isinstance(days, set) and old in days, "ключ --days до ряда не доехал"
    assert seen["cut"] is None, "при полной истории команда назвала её обрезанной"


def test_the_table_carries_the_cut_among_its_notes(tmp_path, monkeypatch) -> None:
    """Стережёт `D-056` внутри `_table`: строка о подрезке уходит в оговорки отчёта.

    Перебор подменён пустым — проверяется проводка, а не деньги. Мутация,
    обязанная ронять проверку: `_table` зовёт `_notes` без `cut=cut`.
    """
    seen: dict[str, object] = {}

    async def nothing(*_rest, **_named) -> tuple[()]:
        return ()

    def study(**named) -> str:
        seen["notes"] = named["notes"]
        return "отчёт"

    monkeypatch.setattr(backtest.__main__, "sweep", nothing)
    monkeypatch.setattr(backtest.__main__, "study", study)
    monkeypatch.setattr(backtest.__main__, "render", lambda report: report)
    file = _database(tmp_path, days=12)
    bars = _bars(file, SYMBOL)
    args = _arguments(["--symbol", SYMBOL, "--tuning-days", "6", "--checking-days", "2",
                       "--yes"])

    asyncio.run(_table(args, bars, cut="ИСТОРИЯ ОБРЕЗАНА"))

    notes = seen["notes"]
    assert isinstance(notes, tuple) and notes[0] == "ИСТОРИЯ ОБРЕЗАНА", notes


def test_a_whole_history_is_not_called_cut(tmp_path) -> None:
    """Стережёт обратное: без подрезки строки нет — иначе её перестанут читать."""
    file, _old, _fresh = _old_and_fresh(tmp_path)

    assert _cut(file, SYMBOL, depth=timedelta(days=500)) is None
    assert _cut(file, SYMBOL) is not None


# -- какие дни считаются торговыми -------------------------------------------

def test_only_the_hours_of_the_sweep_decide_whether_a_day_counts(tmp_path) -> None:
    """Стережёт отбор дней: считаются бары внутри 09:30–15:00, а не за все сутки."""
    bars = _bars(_database(tmp_path, days=1, minutes=340), SYMBOL)
    since, until = SWEEP_SPAN
    inside = [bar for bar in bars if since <= bar.time.astimezone(MSK).time() < until]
    assert len(inside) >= DENSE_ENOUGH
    assert _days_of(bars) == (date(2026, 6, 15),)


def test_a_short_day_is_not_a_trading_day(tmp_path) -> None:
    """Стережёт: день, не покрывающий часы перебора, в окна не попадает."""
    bars = _bars(_database(tmp_path, days=1, minutes=100), SYMBOL)
    assert _days_of(bars) == ()


def test_the_series_is_cut_to_the_trading_days(tmp_path) -> None:
    """Стережёт: свечи вне торговых дней не попадают в прогон и не тратят время."""
    bars = _bars(_database(tmp_path, days=3), SYMBOL)
    days = _days_of(bars)
    cut = _trim(bars, days[:1])
    assert {bar.time.astimezone(MSK).date() for bar in cut} == {days[0]}


# -- отрезки -----------------------------------------------------------------

def test_the_same_period_is_named_once(tmp_path) -> None:
    """Стережёт: подбор одного деления и подбор первого окна — один отрезок, не два."""
    tuning = Period(date(2026, 6, 1), date(2026, 6, 30))
    checking = Period(date(2026, 7, 1), date(2026, 7, 31))
    later = Period(date(2026, 8, 1), date(2026, 8, 31))
    named = _periods(Fold(0, tuning, checking), [Fold(1, tuning, checking),
                                                 Fold(2, checking, later)])
    assert len(named) == len(set(named))
    assert tuning in named and later in named


# -- число прогонов -----------------------------------------------------------

def test_the_number_of_runs_is_printed_before_the_run(capsys) -> None:
    """Стережёт требование ТЗ §4.10 В: число прогонов названо ДО запуска."""
    _confirm(107, (30.0, 60.0), asked=False)
    assert "107" in capsys.readouterr().err


def test_a_long_sweep_asks_for_a_confirmation(monkeypatch, capsys) -> None:
    """Стережёт: перебор дольше пяти минут не начинается молча."""
    monkeypatch.setattr("builtins.input", lambda _prompt: "нет")
    with pytest.raises(SystemExit, match="отменён"):
        _confirm(9744, (3000.0, 9000.0), asked=False)
    assert "9744" in capsys.readouterr().err


def test_a_short_sweep_does_not_ask(monkeypatch) -> None:
    """Стережёт: вопрос на пустом месте превращается в клавишу, которую жмут не читая."""
    def refuse(_prompt: str) -> str:
        raise AssertionError("короткий перебор не должен ничего спрашивать")

    monkeypatch.setattr("builtins.input", refuse)
    _confirm(107, (30.0, 60.0), asked=False)


def test_the_question_is_asked_by_the_slow_edge_of_the_estimate(monkeypatch, capsys) -> None:
    """Стережёт `D-075`: согласие спрашивается по верхней границе разброса.

    Быстрый край под пятью минутами, медленный — над ними. Спроси команда
    по нижней, человек снова решал бы «подожду» по числу, втрое меньшему
    настоящего. Мутация, обязанная ронять проверку: сравнивать с порогом
    `fastest` вместо `slowest` в `_confirm`.
    """
    monkeypatch.setattr("builtins.input", lambda _prompt: "нет")
    with pytest.raises(SystemExit, match="отменён"):
        _confirm(9744, (120.0, 900.0), asked=False)
    said = capsys.readouterr().err
    assert "от 2 мин 00 с до 15 мин 00 с" in said, said


def test_the_expected_time_grows_with_the_length_of_the_series() -> None:
    """Стережёт оценку времени: она считается от замера, а не берётся постоянной."""
    short = _minutes(date(2026, 6, 15), count=1000)
    fastest, slowest = PACE
    assert _pace(short) == pytest.approx((fastest * 1000, slowest * 1000))
    assert fastest < slowest, "разброс времени перебора вывернут наизнанку"
    assert _pace(short + short)[1] > _pace(short)[1]


# -- сквозной запуск ----------------------------------------------------------

@pytest.mark.slow
def test_the_command_writes_a_table_with_both_columns(tmp_path) -> None:
    """Стережёт весь путь: команда даёт таблицу, и в ней обе колонки и все оговорки."""
    source = _database(tmp_path, days=12)
    out = tmp_path / "таблица.txt"
    code = main([
        "--db", str(source), "--out", str(out), "--symbol", SYMBOL,
        "--tuning-days", "6", "--checking-days", "2", "--yes",
    ])
    assert code == 0
    text = out.read_text(encoding="utf-8")
    from backtest.table import both_columns_filled

    assert both_columns_filled(text)
    assert "ПОДБОР" in text and "ПРОВЕРКА" in text
    assert "весь торговый день" in text
    # 58 окон + 45 средняя×тейк + 2 режима: переворот только в одной свече.
    assert "Прогонов: 105" in text
    assert "СКОЛЬЗЯЩАЯ ПРОВЕРКА ВПЕРЁД" in text

@pytest.mark.slow
def test_the_whole_command_leaves_the_database_byte_for_byte_the_same(tmp_path) -> None:
    """Стережёт обещание «только чтение» на всём пути, а не только в снимке."""
    source = _database(tmp_path, days=12)
    before = source.read_bytes()
    main([
        "--db", str(source), "--out", str(tmp_path / "т.txt"), "--symbol", SYMBOL,
        "--tuning-days", "6", "--checking-days", "2", "--yes",
    ])
    assert source.read_bytes() == before

@pytest.mark.slow
def test_the_store_is_never_opened_on_the_owners_own_file(tmp_path, monkeypatch) -> None:
    """Стережёт главную опасность: `CandleStore` пишет и мигрирует — ему дают копию."""
    source = _database(tmp_path, days=12)
    _Watched.seen.clear()
    monkeypatch.setattr("backtest.__main__.CandleStore", _Watched)
    main([
        "--db", str(source), "--out", str(tmp_path / "т.txt"), "--symbol", SYMBOL,
        "--tuning-days", "6", "--checking-days", "2", "--yes",
    ])
    assert _Watched.seen, "хранилище не открывали вовсе — проверка ничего не проверила"
    assert source.resolve() not in [path.resolve() for path in _Watched.seen]


def test_the_original_is_opened_read_only(tmp_path, monkeypatch) -> None:
    """Стережёт форму вызова: соединение с базой владельца счёта — `mode=ro`.

    ⚠️ Проверяется именно **форма**, а не последствие, и это сказано вслух:
    поведение `mode=ro` и обычного соединения на снимке неотличимо, пока
    никто не пишет. Разница появляется в тот день, когда кто-то напишет,
    и тогда проверять будет поздно.
    """
    source = _database(tmp_path, days=1)
    opened: list[str] = []
    real = sqlite3.connect

    def watch(target, *rest, **named):
        opened.append(str(target))
        return real(target, *rest, **named)

    monkeypatch.setattr("backtest.__main__.sqlite3.connect", watch)
    room = tmp_path / "room"
    room.mkdir()
    _snapshot(source, room)
    to_source = [line for line in opened if str(source) in line]
    assert to_source, "оригинал не открывали — проверка ничего не проверила"
    assert all("mode=ro" in line for line in to_source)


def test_a_day_whose_hours_lie_outside_the_sweep_is_not_a_trading_day(tmp_path) -> None:
    """Стережёт отбор: вечерние часы не делают день торговым, сколько бы их ни было."""
    file = tmp_path / "вечер.sqlite3"
    with CandleStore(file) as store:
        store.put_minutes(
            SYMBOL, _minutes(date(2026, 6, 15), count=300, since=time(16, 0)), Source.ISS
        )
    assert _days_of(_bars(file, SYMBOL)) == ()


@pytest.mark.slow
def test_the_command_drops_the_candles_of_non_trading_days(tmp_path) -> None:
    """Стережёт обрезку ряда: сделки вне отрезков не появляются и не тонут молча."""
    file = tmp_path / "смесь.sqlite3"
    with CandleStore(file) as store:
        day = date(2026, 6, 1)
        for _ in range(4):  # вечерние огрызки: барами богаты, торговыми днями не являются
            while day.weekday() >= 5:
                day += timedelta(days=1)
            store.put_minutes(SYMBOL, _minutes(day, count=200, since=time(16, 0)), Source.ISS)
            day += timedelta(days=1)
        made = 0
        while made < 12:
            if day.weekday() < 5:
                store.put_minutes(SYMBOL, _minutes(day, count=340), Source.ISS)
                made += 1
            day += timedelta(days=1)
    out = tmp_path / "т.txt"
    main([
        "--db", str(file), "--out", str(out), "--symbol", SYMBOL,
        "--tuning-days", "6", "--checking-days", "2", "--yes",
    ])
    assert "потерянных на границах отрезков: нет" in out.read_text(encoding="utf-8")


@pytest.mark.slow
def test_the_full_key_switches_the_plan_to_the_whole_product() -> None:
    """Стережёт ключ `--full`: он даёт полное произведение, а не те же три блока."""
    ground = _ground_for_test()
    plan = _plan(ground, whole=True)
    assert len(plan) == 1
    assert len(plan[0].points) == len(full_cross(ground))
    assert sum(len(block.points) for block in _plan(ground, whole=False)) < len(plan[0].points)


@pytest.mark.slow
def test_the_full_plan_says_what_it_costs_in_its_own_title() -> None:
    """Стережёт честность ключа: блок сам называет, чем полное произведение плохо."""
    question = _plan(_ground_for_test(), whole=True)[0].question
    assert "испытаний" in question


def test_the_notes_do_not_promise_two_months_of_history() -> None:
    """Стережёт `D-059`: оговорка про глубину истории обязана говорить правду.

    До 05.09.2026 подвал таблицы утверждал, что внутридневной истории у биржи
    около двух месяцев и длиннее период не станет. После сшивки ближних
    контрактов (решение 0049) ряд даёт 280 торговых дней, то есть оговорка
    отговаривала от уже сделанного. Проверяется не формулировка, а два факта:
    обещания «двух месяцев» в подвале нет, а способ получить длинный ряд
    назван — иначе через месяц строка снова станет неправдой, и никто
    не заметит. Число «280» из строки убрано 28.09.2026 (`D-056`): при
    глубине по умолчанию ряд уже давал 265, и цифра сама стала неправдой.
    """
    footer = " ".join(_notes(runs=10, spent=1.0, bars=100))
    assert "двух месяцев" not in footer
    assert "--stitch" in footer
    assert "--days" in footer, "не названо, как взять ряд длиннее глубины по умолчанию"


def test_the_notes_say_the_stitched_series_is_not_for_trading() -> None:
    """Стережёт вторую половину `D-059`: длинный ряд не должен звать в бой.

    Цены на стыках контрактов не подгоняются (решение 0049), и ряд собран
    ради замеров. Подвал, который советует сшивку и молчит об этом, хуже
    прежнего: он подталкивает торговать по ценам, которых на бирже не было.
    """
    footer = " ".join(_notes(runs=10, spent=1.0, bars=100))
    assert "торговать по нему нельзя" in footer


def test_the_sweep_says_the_data_variable_does_not_work(
    monkeypatch, tmp_path, capsys
) -> None:
    """Стережёт: `python -m backtest` говорит в stderr о недействующей переменной.

    `D-124`: переменная папки данных вне тестового прогона не действует,
    и перебор, взявший базу не оттуда, где её ждал человек, молчал об этом.
    Строка — в stderr: stdout перебора перенаправляют в файл таблицей.
    Мутация, обязанная ронять проверку: убрать `say_ignored_override`
    из `backtest.__main__.main`.
    """
    from market import paths

    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.setenv(paths.USERDATA_ENV, str(tmp_path / "чужая"))
    monkeypatch.delenv(paths.TEST_RUN_ENV, raising=False)

    async def table(args) -> str:
        return "таблица"

    monkeypatch.setattr(backtest.__main__, "_work", table)
    assert backtest.__main__.main(["--db", str(tmp_path / "candles.sqlite3")]) == 0
    said = capsys.readouterr()
    assert paths.USERDATA_ENV in said.err
    assert paths.USERDATA_ENV not in said.out, "строка попала в таблицу, а не в stderr"
