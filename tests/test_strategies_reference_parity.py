"""Сверка торгового модуля с эталонным набором — свеча за свечой.

Что здесь сверяется и что **не** сверяется
-------------------------------------------
Модуль №1 отдаёт намерение, и только его здесь и проверяют: значение средней
и знак «закрытие относительно средней» на каждой свече эталонного отрезка.
Исполнение по открытию следующей свечи, торговое окно, тейк-профит, переворот
и список сделок — это `engine/`, его ещё нет. Главный пункт приёмки ТЗ §9
(«те же сделки, что у прототипа») этим тестом **не закрывается**.

Оракул — независимый пересчёт средней в `Decimal` прямо здесь, по формуле
из `PROTOTYPE.md` §3, а не вызов рабочего кода. Смысл в том, чтобы источник
ошибки не был общим: рабочий модуль считает в двоичной плавающей точке
и рекуррентно, оракул — в десятичной и от затравки. Совпадение знака
на 10 878 свечах — это утверждение о поведении, а не тавтология.

⚠️ Круговая проверка тут всё равно возможна: оракул написан по тому же
разбору `PROTOTYPE.md`, что и модуль. Если разбор прочитан неверно обеими
сторонами — сверка сойдётся против неверного эталона (`PROTOTYPE.md` §9).
Неоспоримый оракул один: сохранённый журнал самого прототипа, 127 сделок,
и добраться до него можно только через движок.

Данные — из локального архива `reference/`, он вне git. В свежем клоне тестам
этого файла заняться нечем, и они пропускаются: это ограничение, а не поломка.
"""

from __future__ import annotations

import pathlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from strategies import Bar, EmaReverse, EmaReverseSettings, Intent

REPO = pathlib.Path(__file__).resolve().parent.parent
DATASET = REPO / "reference/stand/install/data/Set_MoexFuturesMxu6/MXU6/Min5/MXU6.txt"

MSK = timezone(timedelta(hours=3), "MSK")
STEP = timedelta(minutes=5)

#: Отрезок, на котором получены все известные замеры прототипа (DOMAIN.md §4).
FROM = datetime(2026, 6, 19, tzinfo=MSK)
TO = datetime(2026, 8, 26, 23, 59, tzinfo=MSK)
PERIOD = 15

NO_ARCHIVE_HINT = (
    f"нет эталонного набора {DATASET.relative_to(REPO)} — архив reference/ "
    "в git не попадает (гигиена репозитория), в свежем клоне сверки нет"
)


@pytest.fixture(scope="module")
def series() -> list[tuple[datetime, Decimal]]:
    """Начало свечи и цена закрытия. Отрезок режется по времени НАЧАЛА свечи.

    Формат строки набора: `ГГГГММДД,ЧЧММСС,open,high,low,close,volume,0`.
    У свечи хранится начало; время закрытия = начало + таймфрейм.
    """
    if not DATASET.is_file():
        pytest.skip(NO_ARCHIVE_HINT)

    out: list[tuple[datetime, Decimal]] = []
    for line in DATASET.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split(",")
        if len(parts) < 6:
            continue
        moment = datetime.strptime(parts[0] + parts[1], "%Y%m%d%H%M%S").replace(tzinfo=MSK)
        if FROM <= moment <= TO:
            out.append((moment, Decimal(parts[5])))
    out.sort()
    assert out, NO_ARCHIVE_HINT
    return out


@pytest.fixture(scope="module")
def oracle(series) -> list[Decimal | None]:
    """Средняя в `Decimal` по формуле PROTOTYPE.md §3. Затравка — простая средняя.

    Намеренно написана здесь заново и другим числовым типом: оракул, который
    зовёт проверяемый код, доказывает только то, что код равен себе.
    """
    closes = [close for _, close in series]
    alpha = Decimal(2) / Decimal(PERIOD + 1)
    out: list[Decimal | None] = [None] * len(closes)
    value: Decimal | None = None
    for index, close in enumerate(closes):
        if value is None:
            if index < PERIOD - 1:
                continue
            value = sum(closes[:PERIOD]) / PERIOD
        else:
            value = value + alpha * (close - value)
        out[index] = value
    return out


@pytest.fixture(scope="module")
def decisions(series):
    """Решения модуля на том же отрезке, свеча за свечой."""
    strategy = EmaReverse(EmaReverseSettings(period=PERIOD))
    return [
        strategy.on_closed_bar(Bar(
            closes_at=moment + STEP,
            open=float(close), high=float(close), low=float(close),
            close=float(close), volume=0.0,
        ))
        for moment, close in series
    ]


def test_the_slice_is_the_one_the_document_describes(series) -> None:
    """Отрезок не подменили: те же 10 878 свечей и те же границы.

    Без этой проверки «совпало на всех свечах» можно получить на трёх.
    Числа — из `PROTOTYPE.md` §3 и §9.
    """
    assert len(series) == 10_878
    assert series[0][0] == datetime(2026, 6, 19, 8, 55, tzinfo=MSK)
    weekend = sum(1 for moment, _ in series if moment.weekday() >= 5)
    assert weekend == 1_520, (
        "выходные свечи участвуют в расчёте средней, даже когда торговля "
        "в выходные выключена (PROTOTYPE.md §3)"
    )


def test_the_first_signal_lands_where_the_prototype_journal_has_it(series, decisions) -> None:
    """Первый сигнал — на закрытии 16-й свечи, 19.06.2026 в 10:15.

    Сцена из `PROTOTYPE.md` §3: первая свеча отрезка начинается в 08:55,
    период 15, сигнал возможен на закрытии 16-й свечи, а первый вход в журнале
    прототипа записан на 10:15 — открытие 17-й свечи. Сдвиг прогрева на одну
    свечу проявился бы именно здесь.
    """
    first = next(index for index, decision in enumerate(decisions) if decision.warmed_up)
    assert first == 15, f"первый сигнал на свече №{first + 1}, ожидалась №16"
    assert series[first][0] + STEP == datetime(2026, 6, 19, 10, 15, tzinfo=MSK)
    assert decisions[first].intent is not Intent.NONE


def test_intents_match_an_independent_decimal_computation(series, oracle, decisions) -> None:
    """Намерение на каждой из 10 878 свечей совпадает с независимым расчётом."""
    mismatch = []
    for index, ((moment, close), decision) in enumerate(zip(series, decisions)):
        average = oracle[index]
        if average is None or index + 1 <= PERIOD:
            expected = Intent.NONE          # прогрев: свечей <= периода
        elif close > average:
            expected = Intent.LONG
        elif close < average:
            expected = Intent.SHORT
        else:
            expected = Intent.NONE          # закрытие ровно на средней
        if decision.intent is not expected:
            mismatch.append(
                f"  свеча №{index + 1} ({moment:%d.%m.%Y %H:%M}): ожидалось "
                f"{expected.label}, получено {decision.intent.label}; "
                f"закрытие {close}, средняя {average}, наша {decision.average}"
            )

    assert not mismatch, (
        f"расхождений {len(mismatch)} из {len(series)}:\n" + "\n".join(mismatch[:10])
    )


def test_the_close_never_lands_exactly_on_the_average(series, oracle, decisions) -> None:
    """Ветку «закрытие ровно на средней» этим набором не проверить в принципе.

    0 свечей из 10 878, ближайший подход 0,017 пункта (`PROTOTYPE.md` §9).
    Тест держит это утверждение измеренным: если однажды на отрезке появится
    такая свеча, поведение по умолчанию станет проверяемым данными — и об этом
    надо будет узнать от теста, а не догадаться.
    """
    equal = [
        index for index, decision in enumerate(decisions)
        if decision.warmed_up and decision.average == decision.close
    ]
    assert not equal, f"свечей с равенством: {len(equal)} — ветку стало чем проверять"

    approach = min(
        abs(close - oracle[index])
        for index, (_, close) in enumerate(series)
        if oracle[index] is not None and index + 1 > PERIOD
    )
    assert Decimal("0.01") < approach < Decimal("0.02"), (
        f"ближайший подход {approach}, в PROTOTYPE.md §9 записано 0,017"
    )


def test_binary_arithmetic_stays_far_from_the_decision_boundary(series, oracle, decisions) -> None:
    """Мы считаем в double, прототип — в десятичном типе. Запас измерен.

    Это не одна и та же арифметика, и делать вид, что одна, нельзя: сравнение
    строгое, и достаточно расхождения в последнем знаке, чтобы знак «закрытие
    минус средняя» перевернулся. Здесь измеряется запас: насколько наибольшее
    расхождение значений средней меньше, чем ближайший подход закрытия к ней.

    Замеренные значения на этом отрезке:

    * расхождение double против точного расчёта — **6,366789e-11**, это
      **2,19 ULP**, свеча №10118 (закрытие 21.08.2026 14:25);
    * ближайший подход закрытия к средней — **0,016708** (09.08 10:50).

    Запас восемь порядков. На другом инструменте — с мелким шагом цены
    и длинным боковиком — он может оказаться меньше, и тогда выбор типа
    придётся пересматривать. Пока это измерение, а не вера.

    ⚠️ Расхождение меряется через `Decimal(float)` — точное представление
    двоичного числа. Вычитание в самом double дало бы результат, легший
    на сетку той арифметики, погрешность которой меряется (получалось
    5,8e-11), а `Decimal(repr(float))` добавляет до полуULP сверху
    (получалось 6,93e-11). Правильная цифра — только у первого способа.

    ⚠️ Этот тест меряет запас на одном отрезке одного инструмента и
    **не ловит** класс расхождения на ровной цене: там запас обнуляется
    ступенькой, а не постепенно. Он проверяется на синтетике —
    `test_a_flat_price_never_produces_a_signal`.
    """
    drift = max(
        abs(Decimal(decision.average) - oracle[index])
        for index, decision in enumerate(decisions)
        if decision.average is not None and oracle[index] is not None
    )
    assert drift < Decimal("1e-9"), (
        f"расхождение двоичной и десятичной средней {drift:.6e} выросло: "
        "замеренное значение было 6,366789e-11"
    )
    approach = min(
        abs(close - oracle[index])
        for index, (_, close) in enumerate(series)
        if oracle[index] is not None and index + 1 > PERIOD
    )
    assert drift < approach / 1000, (
        f"расхождение двоичной и десятичной средней {drift} подобралось "
        f"к ближайшему подходу закрытия {approach}: знак сравнения может "
        "перевернуться, и список сделок разойдётся с прототипом"
    )
