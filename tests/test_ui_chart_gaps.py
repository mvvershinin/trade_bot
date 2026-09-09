"""Пропуск во времени виден как пропуск (`B-005`).

Что здесь стережётся, одной фразой: **час без данных занимает на оси столько
же места, сколько занял бы час свечей, и ни одна свеча не встаёт вплотную
к свече, отстоящей от неё на час.**

Замечено владельцем счёта 04.09.2026: на оси подряд стояли 13:07, 13:08,
14:04, 14:05 — 56 минут пропало, а свечи нарисованы встык. Дыра в данных
существует (`B-006`), но график её прятал: человек видит непрерывный ряд
и знать про пропуск не может. Решение, принятое глазами по такой картинке,
ошибочно, и обнаружить это нечем.

Проверки идут **до пикселей**, а не до сигнала. Замер 04.09.2026 по двум
предыдущим задачам: заглушка `ChartPanel.append_candle → return` проходила
полный прогон из 1902 тестов зелёной, потому что путь «свеча → окно → панель →
отрисовщик → картинка» не стерёгся ни на одном звене.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import cast

import pytest

from ui.chart.timeline import Timeline
from ui.formatting import MSK
from ui.models import Candle, ChartData, Layer, Marker, MarkerKind

DAY = datetime(2026, 6, 19, tzinfo=MSK)


def _at(hour: int, minute: int, *, day: int = 19) -> datetime:
    """Момент московского дня. Пояс явный: наивных времён здесь нет."""
    return DAY.replace(day=day, hour=hour, minute=minute)


def _candle(moment: datetime, price: float = 100.0) -> Candle:
    return Candle(
        opens_at=moment, open=price, high=price + 1.0,
        low=price - 1.0, close=price + 0.5, volume=5.0,
    )


def _series(*moments: datetime) -> tuple[Candle, ...]:
    return tuple(_candle(moment, 100.0 + number) for number, moment in enumerate(moments))


#: Ряд из записи B-005: три пятиминутки, час без данных, ещё три пятиминутки.
#: Пятиминутки, а не минутки, — чтобы весь ряд влезал в окно обзора по
#: умолчанию (90 мест) и проверка картинки не зависела от прокрутки.
HOLE = _series(
    _at(13, 0), _at(13, 5), _at(13, 10),
    _at(14, 15), _at(14, 20), _at(14, 25),
)
#: Тот же ряд без пропуска — контрольный. Без него проверка «свечи разошлись»
#: зелёная при любой расстановке: сравнивать не с чем.
SOLID = _series(
    _at(13, 0), _at(13, 5), _at(13, 10),
    _at(13, 15), _at(13, 20), _at(13, 25),
)


def _chart(candles: tuple[Candle, ...], **extra) -> ChartData:
    return ChartData(instrument="MXU6", timeframe="5 минут", candles=candles, **extra)


# --------------------------------------------------------------- сама ось

def test_an_hour_without_data_takes_an_hours_worth_of_room_on_the_axis() -> None:
    """Час пропуска занимает столько мест, сколько занял бы час свечей."""
    line = Timeline(HOLE)

    assert line.step == 300.0, f"шаг сетки выведен неверно: {line.step}"
    assert len(line) == 3 + 12 + 3, (
        f"час между 13:10 и 14:15 занял {len(line) - 6} мест вместо 12: "
        "свечи по краям пропуска стоят ближе, чем были на самом деле"
    )
    assert len(line.candles) == 6, "свечи потерялись при разметке оси"
    assert line.gaps(0, len(line)) == [(3, 15)], (
        f"полоса пустых мест размечена не там: {line.gaps(0, len(line))}"
    )


def test_a_solid_series_gets_no_empty_room_at_all() -> None:
    """Непрерывный ряд пустых мест не получает: проверка выше не вакуумна."""
    line = Timeline(SOLID)
    assert len(line) == len(SOLID), "в непрерывный ряд вставлены пустые места"
    assert line.gaps(0, len(line)) == [], "пустота найдена там, где данные есть"


def test_the_night_between_two_trading_days_is_not_drawn_at_all() -> None:
    """Ночь и выходные схлопываются: иначе день — полоска по краю пустоты.

    Между вечерним закрытием и утренним открытием девять часов, между
    пятницей и понедельником — двое с лишним суток. Нарисованные честно,
    они не оставляют на экране места самим свечам.
    """
    night = Timeline(_series(_at(23, 40), _at(23, 45), _at(9, 30, day=22), _at(9, 35, day=22)))
    assert len(night) == 4, (
        f"ночь между 19 и 22 июня развернулась в {len(night)} мест — "
        "график превратился в две полоски по краям пустоты"
    )
    assert night.gaps(0, len(night)) == [], "межсессионный разрыв нарисован пустотой"


def test_the_axis_is_read_the_same_way_from_any_time_zone() -> None:
    """Тот же ряд, поданный в UTC, даёт ту же ось.

    Машина владельца счёта может стоять не в Москве. Отметка эпохи и признак
    «тот же торговый день» обязаны считаться от одной и той же величины:
    `datetime.timestamp()` у наивного времени берёт пояс машины, а `to_msk` —
    московский, и граница дня уехала бы на разницу между ними.
    """
    in_utc = tuple(replace(c, opens_at=c.opens_at.astimezone(timezone.utc)) for c in HOLE)
    moscow, utc = Timeline(HOLE), Timeline(in_utc)

    assert utc.stamps == moscow.stamps, "ось разъехалась от смены пояса подачи"
    assert utc.gaps(0, len(utc)) == moscow.gaps(0, len(moscow)), (
        "пропуск найден не в том месте, если время пришло в UTC"
    )

    # Ночь остаётся ночью и в UTC: 23:40 МСК — это 20:40 UTC того же дня,
    # а 09:30 МСК следующего — 06:30 UTC. Сравнение по дате UTC дало бы
    # тот же ответ здесь, поэтому проверка берёт вечер, у которого дата
    # в UTC уже другая: 03:10 МСК = 00:10 UTC.
    across = _series(_at(23, 40), _at(23, 45), _at(3, 10, day=20), _at(3, 15, day=20))
    in_utc_across = tuple(replace(c, opens_at=c.opens_at.astimezone(timezone.utc)) for c in across)
    assert len(Timeline(in_utc_across)) == len(Timeline(across)), (
        "граница торгового дня посчитана по календарю UTC, а не по московскому"
    )


def test_a_broken_step_cannot_blow_the_axis_up() -> None:
    """Испорченный шаг сетки не разворачивает час в тысячи пустых мест.

    Правило «в пределах суток» ограничивает пропуск и само: минут в сутках
    1440, и на минутках больше мест не выйдет. Потолок стоит на случай, когда
    шаг выведен неверно — например, отметки времени приехали посекундные.
    Час при секундном шаге даёт 3599 мест: отрисовка на них не подвиснет,
    но экран превратится в пустоту, а пустота без единой свечи — не то, чем
    надо отвечать на испорченные данные.

    Проверка не вакуумна: `missing` здесь **больше** потолка. Замер мутацией
    04.09.2026: с прежней проверкой (час на пятиминутках) снятие потолка
    проходило зелёным — 130 мест меньше 1500, и потолок не участвовал.
    """
    from ui.chart.timeline import MAX_GAP_SLOTS

    second = timedelta(seconds=1)
    start = _at(13, 0)
    broken = tuple(_candle(start + second * n) for n in range(3)) + (_candle(_at(14, 0)),)
    line = Timeline(broken)

    assert line.step == 1.0, f"шаг выведен как {line.step}: подстроен не тот случай"
    assert (_at(14, 0) - start).total_seconds() > MAX_GAP_SLOTS, (
        "пропуск в этой выборке меньше потолка — снятие потолка не заметят"
    )
    assert len(line) == len(broken), (
        f"пропуск при испорченном шаге развернулся в {len(line)} мест: "
        "экран заполнен пустотой, свечей на нём нет"
    )


# ----------------------------------------------------- отрисовка: до пикселей

def _painted(surface, width: int = 1000, height: int = 420):
    """Снимок нарисованного графика."""
    from PySide6.QtGui import QImage

    surface.resize(width, height)
    image = QImage(width, height, QImage.Format.Format_RGB32)
    surface.render(image)
    return image


def _candle_columns(image, theme) -> list[int]:
    """Столбцы картинки, в которых есть цвет свечи.

    Смотрим именно цвет тела свечи, а не «не фон»: сетка, подписи и пунктир
    границы пропуска тоже не фон, и по ним пустое место не отличить
    от нарисованного.
    """
    from PySide6.QtGui import QColor

    wanted = [QColor(theme.bull), QColor(theme.bear)]
    columns = []
    for x in range(image.width()):
        for y in range(image.height()):
            pixel = QColor(image.pixel(x, y))
            if any(
                max(abs(pixel.red() - c.red()),
                    abs(pixel.green() - c.green()),
                    abs(pixel.blue() - c.blue())) <= 18
                for c in wanted
            ):
                columns.append(x)
                break
    return columns


def _clusters(columns: list[int]) -> list[tuple[int, int]]:
    """Столбцы, идущие подряд, — это одна свеча."""
    runs: list[tuple[int, int]] = []
    for x in columns:
        if runs and x - runs[-1][1] <= 2:
            runs[-1] = (runs[-1][0], x)
        else:
            runs.append((x, x))
    return runs


def _middle(run: tuple[int, int]) -> float:
    return (run[0] + run[1]) / 2



@pytest.fixture()
def surfaces(qapp):
    """Раздатчик чистых отрисовщиков.

    Каждый график берёт свой: повторный `show_chart` того же инструмента
    намеренно сохраняет приближение человека (`B-009`), и два ряда, поданных
    в один виджет, сравнивались бы в разных окнах обзора.
    """
    from ui.chart.painter_surface import PainterChartSurface

    made = []

    def one():
        made.append(PainterChartSurface())
        return made[-1]

    yield one
    for widget in made:
        widget.deleteLater()
    qapp.processEvents()


@pytest.fixture()
def surface(surfaces):
    return surfaces()


@pytest.mark.slow
def test_the_hole_is_a_hole_on_the_picture_itself(surfaces) -> None:
    """На нарисованной картинке между свечами по краям часа — пустота.

    Проверка по пикселям, а не по внутреннему ряду: между разметкой оси
    и картинкой стоят `_x_of_index`, `_draw_candles` и окно обзора, и любое
    из трёх способно вернуть склейку, оставив разметку верной.
    """
    from ui.theme import current as current_theme

    theme = current_theme()

    control = surfaces()
    control.show_chart(_chart(SOLID))
    solid = _clusters(_candle_columns(_painted(control), theme))
    assert len(solid) == len(SOLID), (
        f"на контрольной картинке видно {len(solid)} свечей вместо {len(SOLID)}"
    )

    holed_surface = surfaces()
    holed_surface.show_chart(_chart(HOLE))
    holed = _clusters(_candle_columns(_painted(holed_surface), theme))
    assert len(holed) == len(HOLE), (
        f"на картинке с пропуском видно {len(holed)} свечей вместо {len(HOLE)}"
    )

    # Отношение, а не расстояние в точках: ширина одного места зависит
    # от того, сколько их влезло, и у двух картинок она разная. Сравнивать
    # надо разрыв с шагом **той же** картинки.
    solid_ratio = (_middle(solid[3]) - _middle(solid[2])) / (_middle(solid[1]) - _middle(solid[0]))
    holed_ratio = (_middle(holed[3]) - _middle(holed[2])) / (_middle(holed[1]) - _middle(holed[0]))

    assert solid_ratio < 1.5, (
        f"на контрольной картинке соседние свечи и так разъехались "
        f"(в {solid_ratio:.1f} раза): проверка ниже ничего не докажет"
    )
    assert holed_ratio > 10.0, (
        f"свечи 13:10 и 14:15 стоят в {holed_ratio:.1f} шага друг от друга, "
        "а между ними час — двенадцать пятиминуток: пропуск на картинке склеен"
    )


def _drawn_text(surface, data: ChartData, width: int = 1000, height: int = 420) -> bytes:
    """Байты записи рисования: текст в ней лежит UTF-16BE, поиск по байтам."""
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QPainter, QPicture

    surface.resize(width, height)
    surface.show_chart(data)
    picture = QPicture()
    painter = QPainter()
    painter.begin(picture)
    surface.render(painter, QPoint(0, 0))
    painter.end()
    return cast(memoryview, picture.data()).tobytes()


def test_the_hole_says_out_loud_that_there_is_no_data(surface) -> None:
    """Пропуск подписан словами и временем, а не оставлен молчаливой пустотой.

    Пустое место неотличимо от края выборки и от спокойного рынка. Подпись
    называет отрезок тем самым временем, которое владелец счёта сверяет
    с терминалом брокера.
    """
    drawn = _drawn_text(surface, _chart(HOLE))

    assert "Данных нет".encode("utf-16-be") in drawn, (
        "пропуск нарисован пустотой без единого слова: отличить его "
        "от края выборки нечем"
    )


def test_the_caption_names_the_whole_gap_even_when_half_of_it_is_off_screen(surface) -> None:
    """Подпись пропуска называет его настоящее начало, а не край экрана.

    Замечено глазами на снимке 04.09.2026: человек отмотал вправо, левый край
    пропуска ушёл за экран, и подпись сказала «Данных нет: 13:13–14:03» —
    хотя данных нет с 13:08. Это новая неправда вместо старой: `B-005` про то
    и заведён, что график сообщает о своих данных не то, что в них есть.
    """
    surface.resize(1000, 420)
    surface.show_chart(_chart(HOLE))
    surface._view.span = 10  # noqa: SLF001 — окно обзора и есть предмет проверки
    surface._view.right = 12.0  # noqa: SLF001 — левый край пропуска за экраном

    runs = surface._line.gaps(  # noqa: SLF001 — то же
        int(surface._first_index()), int(surface._view.right) + 1  # noqa: SLF001
    )
    assert runs == [(3, 15)], (
        f"полоса пропуска обрезана по краю экрана: {runs}. Подпись назовёт "
        "началом пропуска то место, до которого человек домотал"
    )


def test_the_axis_keeps_counting_the_time_across_the_hole(surface) -> None:
    """Подписи оси идут сквозь пропуск и называют пропавшие минуты.

    Ровно этим на графике видно, **сколько** времени потеряно: 13:10 и 14:15
    подряд читаются как соседние свечи, 13:10 · 13:30 · 13:45 · 14:00 · 14:15 —
    как час пустоты.

    Проверяются времена **внутри** пропуска, а не его края. Края называет
    подпись «Данных нет: 13:15–14:10», и по ним подмена «подписывать оси
    свечами вместо мест» осталась бы незамеченной: замер мутацией 04.09.2026
    показал такую подмену зелёной на всех прочих проверках файла.
    """
    drawn = _drawn_text(surface, _chart(HOLE))
    inside = ("13:30", "13:45", "14:00")
    lost = [text for text in inside if text.encode("utf-16-be") not in drawn]

    assert not lost, (
        f"на оси нет подписей {lost} — часа, которого нет в данных, "
        "на шкале времени тоже нет, и пропуск снова невидим"
    )


def test_a_live_candle_after_a_break_opens_the_hole_too(surface) -> None:
    """Свеча, доехавшая по одной, тоже разводит ось на пропуск.

    Живая свеча после обрыва связи приходит `append_candle`, мимо полной
    перерисовки. Это отдельный путь, и до 04.09.2026 он ставил свечу
    вплотную к последней доехавшей.
    """
    surface.show_chart(_chart(SOLID[:3]))
    before = len(surface._line)  # noqa: SLF001 — ось и есть предмет проверки

    surface.append_candle(_candle(_at(14, 15)))

    assert len(surface._line) - before == 13, (  # noqa: SLF001 — то же
        f"живая свеча прибавила {len(surface._line) - before} мест вместо 13: "  # noqa: SLF001
        "час обрыва на оси не появился"
    )
    assert surface._line.candles[-1].opens_at == _at(14, 15), (  # noqa: SLF001 — то же
        "сама свеча до ряда не доехала"
    )


def test_a_marker_inside_the_hole_stays_inside_the_hole(surface) -> None:
    """Метка сделки, попавшая в пропуск, стоит в пропуске.

    Метки, линии сделок и затенение ставятся подбором места по оси
    (`_index_of_time`). Пока ось шла по номерам свечей, сделка, совершённая
    в 13:40, приклеивалась к краю пропуска — то есть показывалась на час
    раньше или позже, чем была.
    """
    surface.show_chart(_chart(HOLE, markers=(
        Marker(_at(13, 40), 100.0, MarkerKind.ENTRY_LONG, Layer.FACT, "Лонг"),
    )))
    place = surface._index_of_time(_at(13, 40))  # noqa: SLF001 — подбор места и есть предмет

    # 13:40 — девятое место оси: 13:00, 13:05, 13:10 со свечами и шесть
    # пустых, 13:15…13:40. Проверка точная, а не «попал в промежуток»:
    # склейка тоже попадает в промежуток, просто в чужой.
    assert place == 8.0, (
        f"сделка 13:40 встала на место {place} вместо восьмого (считая с нуля): "
        "метка приклеилась к краю пропуска и показана не тогда, когда была"
    )


def test_the_price_axis_survives_a_man_who_scrolled_into_the_hole(surface) -> None:
    """Отмотал в середину пропуска — на оси цены остаются цены этого рынка.

    Свечей на экране нет ни одной, и шкале цены не от чего оттолкнуться.
    Без ближайших свечей за краями пустоты ось показала бы 0…1.
    """
    surface.show_chart(_chart(HOLE))
    surface._view.span = 4  # noqa: SLF001 — окно обзора и есть предмет проверки
    surface._view.right = 10.0  # noqa: SLF001 — то же, середина пропуска
    low, high = surface._price_range()  # noqa: SLF001 — то же

    assert low > 50.0, (
        f"внутри пропуска ось цены показывает {low}…{high} — числа не с этого рынка"
    )


# ------------------------------- наивное время и пояс машины владельца счёта

#: Места, на которых стоят шесть свечей ряда `HOLE`: три до часового пропуска
#: и три после двенадцати пустых мест. Записаны числами, а не взяты у оси:
#: ожидание, посчитанное проверяемым кодом, верно при любой его поломке.
HOLE_FILLED = (0, 1, 2, 15, 16, 17)


@pytest.fixture()
def machine_outside_moscow():
    """Машина владельца счёта стоит не в Москве. Пояс процесса меняется всерьёз.

    Подменить это иначе нечем: `datetime.timestamp()` у наивного времени
    берёт именно **системный** пояс процесса, а не что-то, что можно
    передать доводом. Владивосток выбран за разницу с Москвой в семь часов —
    ошибка на такую величину видна на любом ряде.
    """
    import os
    import time as clock

    was = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Vladivostok"
    clock.tzset()
    try:
        yield
    finally:
        if was is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = was
        clock.tzset()


def _naive(candles: tuple[Candle, ...]) -> tuple[Candle, ...]:
    """Тот же ряд, но время без пояса — как оно приходит из источника без него."""
    return tuple(replace(one, opens_at=one.opens_at.replace(tzinfo=None)) for one in candles)


def test_a_naive_time_lands_on_its_own_candle_on_a_machine_outside_moscow(
    surface, machine_outside_moscow
) -> None:
    """Метка на времени свечи стоит над этой свечой и на немосковской машине.

    Ось размечает места через `to_msk` (время без пояса считается московским),
    а всё, что рисуется поверх свечей, подбирает себе место через
    `_index_of_time`. Пока второе звало `datetime.timestamp()` напрямую,
    у наивного времени оно брало **системный** пояс машины: на машине
    во Владивостоке метки сделок, линии «вход → выход» и границы затенения
    съезжали на семь часов, то есть на 84 пятиминутки, — и не роняли ничего.

    Проверка парная к `test_the_axis_is_read_the_same_way_from_any_time_zone`:
    та сравнивает МСК с UTC, но обе стороны там со своим поясом, и системный
    пояс машины в неё не входит вовсе.
    """
    naive = _naive(HOLE)
    surface.show_chart(_chart(naive))

    assert len(surface._line) == 18, "ось разметила не тот ряд"  # noqa: SLF001
    for candle, place in zip(naive, HOLE_FILLED, strict=True):
        found = surface._index_of_time(candle.opens_at)  # noqa: SLF001 — подбор места и есть предмет
        assert found == float(place), (
            f"свеча {candle.opens_at:%H:%M} стоит на месте {place}, а метка "
            f"на её времени — на месте {found}: на машине не в Москве всё, "
            "что нарисовано поверх свечей, уехало на разницу поясов"
        )
