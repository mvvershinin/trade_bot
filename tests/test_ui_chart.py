"""Отрисовка графика: ось, масштаб человека и живая свеча.

Что здесь стережётся, одной фразой: **свеча, метка, точка средней и подпись
на оси считаются в четырёх разных местах и обязаны встать на одну и ту же
отметку** — открытие свечи. Разъезд этих четырёх и есть `B-008`: владелец
счёта сравнил с терминалом брокера и увидел свечу 12:55 на 13:00.

Второе — приближение человека переживает перерисовку (`B-009`): отмотал
историю назад, пришла живая свеча, график не прыгнул к правому краю.

⚠️ До 09.09.2026 половина файла проверяла веб-отрисовку — ту, что никогда
не запускалась: файла библиотеки не было ни в репозитории, ни на диске,
`is_available()` всегда отвечала отказом. Вместе с ней удалены и её проверки
(решение 0056). Ничего из перечисленного выше при этом не осталось
без сторожа: у `PainterChartSurface` на каждое своя проверка ниже,
и они идут **до пикселей**, а не до вызова.
"""

from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest
import synthetic

from ui.models import ChartData, LinePoint

# -------------------------------------------------------------- ось графика

def test_the_painter_surface_lays_candles_out_by_their_axis_mark(qapp) -> None:
    """Свечи стоят на отметках открытия — на всех трёх путях подачи.

    Путей три: полный набор (`show_chart`), добавление закрытой свечи
    (`append_candle`) и уточнение последней (`update_last_candle`), — и перевод
    времени в каждом свой. Проверка идёт по самой оси (`Timeline.stamps`),
    а не по картинке: сдвиг на бар остаётся законной отметкой и по картинке
    отличается на несколько пикселей, а числом — сразу.
    """
    from ui.chart.painter_surface import PainterChartSurface

    data = synthetic.chart_data(12)
    surface = PainterChartSurface()
    try:
        surface.show_chart(data)
        marks = list(surface._line.stamps)  # noqa: SLF001 — ось и есть предмет проверки
        assert marks == [candle.opens_at.timestamp() for candle in data.candles], (
            "свечи встали не на отметки своих открытий — весь график сдвинут"
        )

        extra = data.candles[-1]
        surface.append_candle(extra)
        assert surface._line.stamps[-1] == extra.opens_at.timestamp()  # noqa: SLF001 — то же

        surface.update_last_candle(data.candles[0])
        assert surface._line.stamps[-1] == data.candles[0].opens_at.timestamp()  # noqa: SLF001
    finally:
        surface.deleteLater()
        qapp.processEvents()


# ----------------------------------------------------- ось графика: что поверх

def test_a_moment_of_a_candle_lands_on_that_candles_place_not_the_next_one(qapp) -> None:
    """Метка на времени свечи рисуется над этой свечой, а не над соседней.

    Метки сделок, линии «вход → выход» и полосы затенения ставятся подбором
    места по оси (`_index_of_time`): готового времени у отрисовщика нет,
    есть только список отметок свечей. Сдвиг
    в этом подборе переставляет ВСЁ, что нарисовано поверх свечей, оставив
    сами свечи на месте, — то есть даёт ровно ту картинку, из-за которой
    заведён B-008, и при этом ни одна проверка свечей не краснеет
    (мутация +300 секунд, замер 04.09.2026).

    Проверка по номеру свечи, а не по «попал в диапазон»: сдвиг на бар
    остаётся внутри диапазона и потому диапазоном не ловится.
    """
    from ui.chart.painter_surface import PainterChartSurface

    data = synthetic.chart_data(12)
    surface = PainterChartSurface()
    try:
        surface.show_chart(data)
        for number, candle in enumerate(data.candles):
            place = surface._index_of_time(candle.opens_at)  # noqa: SLF001 — подбор места и есть предмет
            assert place == float(number), (
                f"свеча {candle.opens_at:%H:%M} стоит на месте {number}, "
                f"а всё, что нарисовано поверх неё, — на месте {place}"
            )

        # Момент внутри бара попадает между свечами, а не прыгает на соседнюю:
        # сделка исполняется внутри минуты, и метка факта обязана это показать.
        step = data.candles[4].opens_at - data.candles[3].opens_at
        middle = data.candles[3].opens_at + step / 2
        assert 3.0 < surface._index_of_time(middle) < 4.0  # noqa: SLF001 — то же
    finally:
        surface.deleteLater()
        qapp.processEvents()


def _shot(qapp, data, average):
    """Картинка графика: только свечи и заданная средняя.

    Метки, уровни, затенение и пунктир сделок убраны намеренно — они рисуются
    своими цветами, и разбирать, чей это пиксель, пришлось бы по оттенку.
    Размер окна тот же при любом вызове: раскладка по оси зависит от свечей,
    а они одни и те же, поэтому два снимка сравнимы столбец в столбец.
    """
    from PySide6.QtGui import QImage

    from ui.chart.painter_surface import PainterChartSurface
    from ui.theme import LIGHT

    bare = ChartData(
        instrument=data.instrument,
        timeframe=data.timeframe,
        candles=data.candles,
        average=average,
        average_label=data.average_label,
    )
    surface = PainterChartSurface()
    try:
        surface.set_theme(LIGHT)
        surface.show_chart(bare)
        surface.resize(1000, 420)
        image = QImage(1000, 420, QImage.Format.Format_RGB32)
        surface.render(image)
    finally:
        surface.deleteLater()
        qapp.processEvents()
    return image


def _columns_of(image, *colours: str) -> list[int]:
    """Столбцы картинки, в которых встречается любой из названных цветов.

    Смотрим именно цвет, а не «не фон»: сетка, подписи и пунктир тоже не фон,
    и по ним одну линию от другой не отличить. Допуск в 24 — на сглаживание
    краёв: перо средней шириной 1,8 пикселя рисуется с полутонами.
    """
    from PySide6.QtGui import QColor

    wanted = [QColor(one) for one in colours]
    found = []
    for x in range(image.width()):
        for y in range(image.height()):
            pixel = QColor(image.pixel(x, y))
            if any(
                max(abs(pixel.red() - c.red()),
                    abs(pixel.green() - c.green()),
                    abs(pixel.blue() - c.blue())) <= 24
                for c in wanted
            ):
                found.append(x)
                break
    return found


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


def test_the_average_line_is_drawn_over_the_candles_it_belongs_to(qapp) -> None:
    """Линия средней нарисована над своими свечами, а не над соседними.

    ⚠️ Проверка заведена 09.09.2026 при удалении веб-графика (решение 0056),
    и завела её **дыра, найденная мутацией**. Сдвиг средней на бар
    (`_index_of_time(point.time) + 1.0` в `_draw_average`) проходил полный
    прогон зелёным. Единственная проверка «средняя стоит на своей свече»
    в проекте была написана на **веб-отрисовку** — ту, что не запускалась
    ни разу; на отрисовке, которую видит владелец счёта, средняя
    не проверялась ничем.

    Дыра не создана удалением: она существовала с 04.09.2026, удаление
    её только сделало видимой. Проверки по `_index_of_time` сдвиг именно
    средней не ловят — в `_draw_average` своя строка.

    Читаются **пиксели**, а не индексы: мутация сидела внутри рисования,
    и проверка, спрашивающая место у `_index_of_time`, осталась бы зелёной.

    Свечи меряются на **отдельной картинке, без средней**. Иначе синяя линия,
    проходя по телу свечи, разрезает её столбцы надвое, и проверка падает
    не там, где сломано: замер 09.09.2026 на той же мутации — 13 «свечей»
    вместо 12, при исправном сравнении краёв.
    """
    from ui.theme import LIGHT

    #: На каких свечах ряда задана средняя. Числа записаны здесь, а не взяты
    #: у графика: ожидание, посчитанное проверяемым кодом, верно при любой
    #: его поломке. Всего две точки — чтобы у линии были свои края.
    first_place, last_place = 2, 9

    data = synthetic.chart_data(12)
    candles = data.candles
    line = (
        LinePoint(time=candles[first_place].opens_at, value=candles[first_place].close),
        LinePoint(time=candles[last_place].opens_at, value=candles[last_place].close),
    )

    bodies = _clusters(_columns_of(_shot(qapp, data, ()), LIGHT.bull, LIGHT.bear))
    assert len(bodies) == len(candles), (
        f"свечей нарисовано {len(bodies)}, а в ряду {len(candles)}: "
        "картинка не та, на которой проверяют среднюю"
    )
    drawn = _columns_of(_shot(qapp, data, line), LIGHT.average)
    assert drawn, "линия средней не нарисована вовсе"

    step = _middle(bodies[1]) - _middle(bodies[0])
    edges = (("левый", first_place, min(drawn)), ("правый", last_place, max(drawn)))
    for name, place, edge in edges:
        centre = _middle(bodies[place])
        assert abs(edge - centre) <= step / 2, (
            f"{name} край средней стоит на {edge}, а свеча {place}, на которой "
            f"её значение посчитано, — на {centre}: расхождение "
            f"{abs(edge - centre) / step:.1f} свечи. Линия средней и свечи "
            "размечены по-разному, и человек сравнивает цену не с той свечой"
        )


def test_the_time_axis_is_signed_with_the_openings_of_the_candles(qapp) -> None:
    """Подписи на оси времени — те же открытия, что и у свечей.

    Подпись под свечой и есть то, что владелец счёта сравнивает с терминалом
    брокера: B-008 замечен именно так — «свеча стоит на 12:55 у брокера
    и на 12:56 у нас». Свечи и подписи считаются в двух разных местах
    (`show_chart` и `_draw_axes`), и до 04.09.2026 второе не проверялось
    ничем: подпись, съехавшая на бар, проходила полный прогон зелёной
    (мутация «подписать соседней свечой», замер 04.09.2026).

    Читается нарисованное, а не исходный текст: виджет рисуется в `QPicture`,
    и подписи достаются из записи вызовов. Текст в ней лежит UTF-16BE,
    поэтому поиск по байтам.
    """
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QPainter, QPicture

    from ui.chart.painter_surface import PainterChartSurface

    data = synthetic.chart_data(12)
    surface = PainterChartSurface()
    try:
        surface.resize(900, 500)
        surface.show_chart(data)
        picture = QPicture()
        painter = QPainter()
        painter.begin(picture)
        surface.render(painter, QPoint(0, 0))
        painter.end()
        # Заглушки PySide6 объявляют `QPicture.data()` возвращающим `object`;
        # на деле это `memoryview` с записью вызовов рисования.
        drawn_bytes = cast(memoryview, picture.data()).tobytes()
    finally:
        surface.deleteLater()
        qapp.processEvents()

    signed = {
        f"{hour:02d}:{minute:02d}"
        for hour in range(24)
        for minute in range(60)
        if f"{hour:02d}:{minute:02d}".encode("utf-16-be") in drawn_bytes
    }
    openings = {candle.opens_at.strftime("%H:%M") for candle in data.candles}
    step = data.candles[1].opens_at - data.candles[0].opens_at
    last_close = (data.candles[-1].opens_at + step).strftime("%H:%M")

    assert len(signed) >= 2, "на оси времени не подписано почти ничего"
    assert signed <= openings, (
        f"на оси подписано время, которым не открывается ни одна свеча: "
        f"{sorted(signed - openings)}"
    )
    assert data.candles[0].opens_at.strftime("%H:%M") in signed, (
        "крайняя левая подпись — не открытие первой свечи: вся ось сдвинута"
    )
    assert last_close not in signed, (
        f"на оси подписано закрытие последней свечи ({last_close}) — "
        "подписи размечены закрытиями, как было до B-008"
    )


# ------------------------------------- масштаб человека переживает перерисовку

def _zoomed(surface, *, span: int, follow: bool, right_shift: float = 0.0):
    """Поставить график в положение, в которое его привёл бы человек.

    Колесо и перетаскивание меняют ровно эти три поля (`wheelEvent`,
    `mouseMoveEvent`), и здесь они выставляются напрямую: предмет проверки —
    переживает ли положение перерисовку, а не то, как оно получено мышью.
    Отдельный тест на саму мышь есть в `tests/test_ui_window.py`.
    """
    surface._view.span = span  # noqa: SLF001 — окно обзора и есть предмет проверки
    surface._view.right += right_shift  # noqa: SLF001 — то же
    surface._view.follow = follow  # noqa: SLF001 — то же


def test_the_painter_surface_keeps_the_zoom_of_a_man_who_scrolled_back(qapp) -> None:
    """Отмотал назад и приблизил — перерисовка оставляет его там же.

    Живая свеча приходит раз в минуту, и до 04.09.2026 каждая из них
    заканчивалась полной перерисовкой с пересозданием окна обзора: приближение,
    выставленное руками, жило до ближайшей минуты (`B-009`). Пункт приёмки Э2 —
    «два окна рядом, десять минут наблюдения» — при этом невыполним физически.

    Правый край сверяется **по времени свечи**, а не по её номеру: номера
    съезжают, как только у выборки сдвинулся левый край, и проверка по номеру
    осталась бы зелёной при уехавшем вбок графике.
    """
    from ui.chart.painter_surface import DEFAULT_VISIBLE, PainterChartSurface

    first = synthetic.chart_data(60)
    surface = PainterChartSurface()
    try:
        surface.show_chart(first)
        _zoomed(surface, span=18, follow=False, right_shift=-20.0)
        watched = surface._stamp_at(surface._view.right)  # noqa: SLF001 — то же

        grown = replace(first, candles=first.candles + (
            replace(first.candles[-1], opens_at=first.candles[-1].opens_at + synthetic.STEP),
        ))
        surface.show_chart(grown)

        assert surface._view.span == 18, (  # noqa: SLF001 — то же
            "перерисовка сбросила приближение: наблюдать за появлением свечи "
            "глазами нельзя"
        )
        assert surface._stamp_at(surface._view.right) == watched, (  # noqa: SLF001
            "перерисовка увезла график с того места, где стоял человек"
        )
        assert surface._view.span != DEFAULT_VISIBLE, (  # noqa: SLF001
            "проверка вакуумна: приближение совпало с умолчанием"
        )
    finally:
        surface.deleteLater()
        qapp.processEvents()


def test_the_painter_surface_moves_on_for_a_man_standing_at_the_right_edge(qapp) -> None:
    """Стоял у правого края — новую свечу видит, и с тем же приближением.

    Слежение — режим, а не место: правый край обязан переехать на новую
    последнюю свечу. Сохранить вместо этого прежнюю точку значило бы, что
    новые свечи уходят за экран у того самого человека, который смотрит
    именно на них.
    """
    from ui.chart.painter_surface import PainterChartSurface

    first = synthetic.chart_data(60)
    surface = PainterChartSurface()
    try:
        surface.show_chart(first)
        _zoomed(surface, span=18, follow=True)

        fresh = replace(first.candles[-1], opens_at=first.candles[-1].opens_at + synthetic.STEP)
        surface.show_chart(replace(first, candles=first.candles + (fresh,)))

        assert surface._view.span == 18, "слежение съело приближение"  # noqa: SLF001
        assert surface._stamp_at(surface._view.right) == fresh.opens_at.timestamp(), (  # noqa: SLF001
            "правый край не переехал на новую свечу — она за экраном"
        )
        assert surface.is_following(), "режим слежения потерян перерисовкой"
    finally:
        surface.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("changed", [{"instrument": "SiU6"}, {"timeframe": "1 минута"}])
def test_the_painter_surface_resets_the_view_when_the_chart_itself_changed(
    qapp, changed
) -> None:
    """Сменился инструмент или размер свечи — вид сбрасывается, и это верно.

    Приближение, оставшееся от MXU6 5м, на другом ряду показывает не то же
    место, а случайное. Проверка парная к двум предыдущим: без неё «сохранять
    всегда» прошло бы их обе.
    """
    from ui.chart.painter_surface import DEFAULT_VISIBLE, PainterChartSurface

    first = synthetic.chart_data(60)
    surface = PainterChartSurface()
    try:
        surface.show_chart(first)
        _zoomed(surface, span=18, follow=False, right_shift=-20.0)

        surface.show_chart(replace(first, **changed))

        assert surface._view.span == min(DEFAULT_VISIBLE, 60), (  # noqa: SLF001
            f"вид не сброшен на другом графике ({changed}): человек смотрит "
            "в случайное место чужого ряда"
        )
        assert surface.is_following(), "новый график открылся не у последней свечи"
    finally:
        surface.deleteLater()
        qapp.processEvents()


# ------------- живая свеча доезжает до самого графика, а не до края правки
#
# Три проверки ниже добавлены проходом сторожей 04.09.2026. Правка `B-009`
# перевела живую свечу с полного прогона на добавление — и весь путь этого
# добавления оказался без сторожа: `ChartPanel.append_candle`,
# `ChartPanel.update_last_candle` и `PainterChartSurface.append_candle`
# по отдельности заменялись на `return`, и полный прогон (1902 теста)
# оставался зелёным. График при этом переставал показывать живую свечу вовсе.


def test_the_painter_surface_really_puts_a_live_candle_on_the_chart(qapp) -> None:
    """Прибавленная свеча появляется на графике, а уточнённая — меняется.

    Проверка **новой** свечой, а не последней из набора: с последней «ряд
    вырос» и «ряд не тронут» дают одну и ту же отметку на оси, и заглушка
    вместо прибавки проходит такую проверку зелёной (замер мутацией,
    04.09.2026).
    """
    from ui.chart.painter_surface import PainterChartSurface

    data = synthetic.chart_data(30)
    surface = PainterChartSurface()
    try:
        surface.show_chart(data)
        fresh = replace(
            data.candles[-1],
            opens_at=data.candles[-1].opens_at + synthetic.STEP,
            close=data.candles[-1].close + 500,
        )

        surface.append_candle(fresh)
        assert len(surface._line.candles) == 31, (  # noqa: SLF001 — ряд и есть предмет проверки
            "живая свеча до графика не доехала: ряд не вырос"
        )
        assert surface._line.candles[-1] == fresh, (  # noqa: SLF001 — то же
            "на месте живой свечи оказалась не она"
        )

        grown = replace(fresh, close=fresh.close + 700, high=fresh.high + 700)
        surface.update_last_candle(grown)
        assert len(surface._line.candles) == 31, (  # noqa: SLF001 — то же
            "уточнение последней свечи прибавило вторую"
        )
        assert surface._line.candles[-1].close == grown.close, (  # noqa: SLF001 — то же
            "растущая свеча на графике не растёт: цена осталась прежней"
        )
    finally:
        surface.deleteLater()
        qapp.processEvents()


def test_the_painter_surface_carries_a_watching_man_onto_the_live_candle(qapp) -> None:
    """Стоящий у правого края видит прибавленную свечу, отмотавший — не сдвинут.

    Это вторая половина того же «слежение — режим, а не место», что и при
    полной перерисовке, только на пути прибавки: живая свеча приходит
    именно им (`B-009`). Без переезда правого края новая свеча появляется
    **за** видимой областью — то есть ровно там, где владелец счёта её
    и не увидит.
    """
    from ui.chart.painter_surface import PainterChartSurface

    data = synthetic.chart_data(40)
    fresh = replace(data.candles[-1], opens_at=data.candles[-1].opens_at + synthetic.STEP)

    watcher = PainterChartSurface()
    reader = PainterChartSurface()
    try:
        watcher.show_chart(data)
        _zoomed(watcher, span=15, follow=True)
        watcher.append_candle(fresh)
        place = watcher._index_of_stamp(fresh.opens_at.timestamp())  # noqa: SLF001 — ось
        assert watcher._first_index() <= place <= watcher._view.right, (  # noqa: SLF001 — то же
            f"живая свеча встала за видимой областью: свеча {place}, "
            f"видно {watcher._first_index()}…{watcher._view.right}"  # noqa: SLF001 — то же
        )

        reader.show_chart(data)
        _zoomed(reader, span=15, follow=False, right_shift=-12.0)
        stood = reader._view.right  # noqa: SLF001 — то же
        reader.append_candle(fresh)
        assert reader._view.right == stood, (  # noqa: SLF001 — то же
            "отмотавшего назад утащило вслед за живой свечой"
        )
    finally:
        for surface in (watcher, reader):
            surface.deleteLater()
        qapp.processEvents()


def test_the_chart_panel_hands_a_live_candle_over_to_the_surface(qapp) -> None:
    """Панель графика передаёт живую свечу отрисовщику, а не роняет на пол.

    Панель — единственное звено между окном и отрисовщиком, и до 04.09.2026
    оба её метода живой свечи можно было заменить на `return`, не уронив
    ни одного теста. Владелец счёта увидел бы график, замерший до ближайшего
    прогона, — то есть ровно то, от чего уходили.
    """
    from ui.chart.painter_surface import PainterChartSurface
    from ui.chart_panel import ChartPanel

    data = synthetic.chart_data(20)
    surface = PainterChartSurface()
    panel = ChartPanel(surface=surface)
    try:
        panel.show_chart(data)
        fresh = replace(
            data.candles[-1],
            opens_at=data.candles[-1].opens_at + synthetic.STEP,
            close=data.candles[-1].close + 400,
        )

        panel.append_candle(fresh)
        assert surface._line.candles[-1] == fresh, (  # noqa: SLF001 — ряд и есть предмет проверки
            "панель не передала прибавленную свечу отрисовщику"
        )

        panel.update_last_candle(replace(fresh, close=fresh.close + 900))
        assert surface._line.candles[-1].close == fresh.close + 900, (  # noqa: SLF001 — то же
            "панель не передала уточнение последней свечи отрисовщику"
        )
    finally:
        panel.deleteLater()
        qapp.processEvents()
