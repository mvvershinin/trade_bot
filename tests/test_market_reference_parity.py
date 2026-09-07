"""Сверка сборки пятиминуток с эталонным набором — построчно.

QUALITY.md §5: «Сборка таймфрейма из минуток — уже есть эталон, совпадение
построчно». Эталон — набор `MXU6 / Min5` из локального архива `reference/`:
20 021 пятиминутная свеча за 06.09.2025 … 26.08.2026, собранная проверенным
загрузчиком архива. Это единственная имеющаяся проверка правильности сборки,
и терять её нельзя.

Как это устроено, чтобы тест не ходил в сеть
--------------------------------------------
Минутки лежат в базе `userdata/`, которую наполняет отдельный, **явно
включаемый** шаг. По умолчанию тест только читает базу, а если её нет —
пропускается с указанием команды.

Готовится база так (займёт несколько минут, ~77 тысяч минуток):

    TERMINAL_ISS_NETWORK=1 .venv/bin/pytest tests/test_market_reference_parity.py

В свежем клоне теста не будет чем заняться: архив `reference/` в git
не попадает (гигиена репозитория), и тест пропустится — это ожидаемо,
а не поломка.
"""

from __future__ import annotations

import os
import pathlib
import shutil
from datetime import datetime, timedelta

import pytest

from market.aggregate import bar_start
from market.candles import M5, MSK
from market.iss import FUTURES, IssClient
from market.paths import default_db_path
from market.storage import CandleStore, Source
from market.sync import sync_minutes

REPO = pathlib.Path(__file__).resolve().parent.parent
REFERENCE = REPO / "reference/stand/install/data/Set_MoexFuturesMxu6/MXU6/Min5/MXU6.txt"

#: Минутки для сверки лежат в **продуктовой** базе, а не в своей.
#: Решение 0003 — одна база на всё; отдельный файл рядом означал бы, что
#: сверка идёт не по тем данным, которые читает программа.
#:
#: ⚠️ Читаются они с **копии** (см. фикстуру `store`): открытие базы поднимает
#: её схему, а миграция односторонняя. Обычный прогон тестов не имеет права
#: необратимо править файл владельца счёта. Данные при этом те же — копия
#: побайтовая, — так что довод «сверять по тому, что читает программа» цел.
CACHE = default_db_path()
SYMBOL = "MXU6"

#: Загрузка из сети — только по явному разрешению. Тесты по умолчанию
#: не зависят ни от связи, ни от того, что сегодня отвечает биржа.
NETWORK_ALLOWED = os.environ.get("TERMINAL_ISS_NETWORK") == "1"

NO_MINUTES_HINT = (
    "нет минутных данных для сверки. Наполнить базу: "
    "TERMINAL_ISS_NETWORK=1 .venv/bin/pytest tests/test_market_reference_parity.py"
)


def as_in_set(value: float) -> str:
    """Число в том виде, в каком его пишет набор данных архива.

    Цены срочного рынка целые, и в файле они записаны без дробной части.
    Формат принадлежит стенду, а не продукту, поэтому живёт в тесте.
    """
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def set_line(candle) -> str:
    """`ГГГГММДД,ЧЧММСС,open,high,low,close,volume,0`."""
    return (
        f"{candle.time:%Y%m%d},{candle.time:%H%M%S},"
        f"{as_in_set(candle.open)},{as_in_set(candle.high)},"
        f"{as_in_set(candle.low)},{as_in_set(candle.close)},"
        f"{int(candle.volume)},0"
    )


def moment(line: str) -> datetime:
    day, moment = line.split(",")[:2]
    return datetime.strptime(day + moment, "%Y%m%d%H%M%S").replace(tzinfo=MSK)


@pytest.fixture(scope="module")
def reference() -> list[str]:
    if not REFERENCE.is_file():
        pytest.skip(f"нет эталонного набора {REFERENCE} — архив reference/ вне git")
    lines = REFERENCE.read_text(encoding="utf-8").split("\n")
    return [s for s in lines if s.strip()]


@pytest.fixture(scope="module")
def store(reference: list[str], tmp_path_factory: pytest.TempPathFactory):
    """Минутки для сверки — на **копии** рабочей базы, а не на ней самой.

    ⚠️ Копия обязательна, и вот почему. Открытие базы поднимает её схему
    до текущей, а миграция односторонняя: база схемы 3 более старой сборкой
    программы не откроется вовсе. Пока схема была одна, чтение рабочей базы
    из теста было холостым; со второй миграцией обычный `pytest` стал
    необратимо править файл владельца счёта — и в этом файле лежит боевой
    журнал, который удалять запрещено специально.

    Смысл, ради которого сверка ходит именно в рабочую базу (решение 0003 —
    одна база на всё, сверка идёт по тому, что читает программа), копией
    не нарушается: минутки в ней те же, побайтово.

    Наполнение из сети — единственный случай, когда пишем в рабочую базу:
    в том и смысл ключа `TERMINAL_ISS_NETWORK=1`, чтобы пополнить общий кэш.
    Это осознанное разрешение, а не побочный эффект обычного прогона.
    """
    since, until = moment(reference[0]).date(), moment(reference[-1]).date()
    if NETWORK_ALLOWED:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        with CandleStore(CACHE) as filling:
            report = sync_minutes(
                filling, IssClient(pause=0.1), SYMBOL, market=FUTURES,
                since=since - timedelta(days=1), until=until, source=Source.ISS,
            )
            print("\n" + report.summary())
    if not CACHE.is_file():
        pytest.skip(NO_MINUTES_HINT)
    copy = tmp_path_factory.mktemp("сверка") / CACHE.name
    shutil.copy2(CACHE, copy)
    with CandleStore(copy) as opened:
        if opened.coverage(SYMBOL).count == 0:
            pytest.skip(NO_MINUTES_HINT)
        yield opened


def test_five_minute_bars_match_reference_line_by_line(store: CandleStore, reference: list[str]) -> None:
    """Пятиминутки, собранные из минуток, совпадают с эталоном построчно.

    Сверяется всё: дата, время начала свечи, open, high, low, close и объём.
    Совпадение числа строк совпадением не считается — сравниваются строки.
    """
    first_one, last_one = moment(reference[0]), moment(reference[-1])
    ours = store.bars(SYMBOL, M5, first_one, last_one + M5.delta)
    stored_coverage = store.coverage(SYMBOL)
    # Сравнивается бар первой минутки, а не сама минутка: первая сделка дня
    # случилась в 10:18, и это законно попадает в бар 10:15.
    if stored_coverage.first is None or bar_start(stored_coverage.first, M5) > first_one:
        pytest.skip(f"минутки начинаются с {stored_coverage.first}, reference — с {first_one}: {NO_MINUTES_HINT}")

    our_lines = [set_line(candle) for candle in ours]
    mismatches = [
        (number, expected, produced)
        for number, (expected, produced) in enumerate(zip(reference, our_lines), start=1)
        if expected != produced
    ]

    report = "\n".join(
        f"  line {number}:\n    reference: {a}\n    наше:   {b}"
        for number, a, b in mismatches[:20]
    )
    assert not mismatches, f"расхождений {len(mismatches)} из {len(reference)}:\n{report}"
    assert len(our_lines) == len(reference), (
        f"свечей собрано {len(our_lines)}, в эталоне {len(reference)}"
    )


def test_the_reference_itself_is_made_of_partial_bars(
    store: CandleStore, reference: list[str]
) -> None:
    """Эталон, совпавший построчно, **состоит** из неполных баров почти наполовину.

    Это не наблюдение о рынке, а факт о пункте приёмки. Настройка движка
    «пропускать неполные свечи» (`engine.PartialCandles.SKIP`) выбрасывает
    ровно эти бары — значит, при ней ряд перестаёт совпадать с эталоном
    **по построению**, а не «получается короче». Умолчание `ACCEPT` не выбор
    удобства: любое другое ломает главный пункт приёмки ТЗ §9.

    Замер 31.08.2026 на MXU6: 8 706 неполных пятиминуток из 20 021, 43,5 %;
    пустых минутных слотов 26 527 из 100 105.

    ⚠️ Оговорка, которую нельзя терять: `filled_minutes` **не проверен ни одним
    внешним оракулом**. Эталонный набор сверяет `дата,время,O,H,L,C,volume` —
    колонки полноты в нём нет, и число 8 706 держится на внутренней арифметике
    (сумма `filled_minutes` = число минуток в базе). Здесь проверяется именно
    эта арифметика и порядок величины, а не «правильность» полноты.
    """
    first_one, last_one = moment(reference[0]), moment(reference[-1])
    bars = store.bars(SYMBOL, M5, first_one, last_one + M5.delta)
    if len(bars) != len(reference):
        pytest.skip(f"собрано {len(bars)} баров, в эталоне {len(reference)}: {NO_MINUTES_HINT}")

    partial_bars = sum(1 for bar in bars if bar.is_partial)
    share = partial_bars / len(bars)
    assert 0.35 < share < 0.55, f"неполных {partial_bars} из {len(bars)} — {share:.1%}"

    # Единственная имеющаяся проверка самого `filled_minutes`: сумма по барам
    # обязана сойтись с числом минуток, из которых они собраны.
    minutes_stored = len(store.minutes(SYMBOL, first_one, last_one + M5.delta))
    assert sum(bar.filled_minutes for bar in bars) == minutes_stored


def test_reference_dataset_shape_is_the_one_we_expect(reference: list[str]) -> None:
    """Эталон не подменили: те же 20 021 свеча и те же границы.

    Без этой проверки «совпало построчно» можно получить на трёх строках.
    """
    assert len(reference) == 20_021
    assert moment(reference[0]) == datetime(2025, 9, 6, 10, 15, tzinfo=MSK)
    assert moment(reference[-1]) == datetime(2026, 8, 26, 8, 45, tzinfo=MSK)


def test_every_reference_bar_starts_on_the_five_minute_grid(reference: list[str]) -> None:
    """Все свечи эталона стоят на сетке кратно пяти минутам от начала часа."""
    off_grid = [s for s in reference if moment(s).minute % 5 or moment(s).second]
    assert not off_grid, f"вне сетки {len(off_grid)} свечей, например {off_grid[:3]}"


def test_bars_are_not_stored_only_minutes_are(store: CandleStore) -> None:
    """В базе лежат только минутки: пересобрать таймфрейм из пересобранного нельзя."""
    tables = {
        table_name for (table_name,) in store._db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert "minute_candle" in tables
    assert not {t for t in tables if "bar" in t or "min5" in t.lower()}
