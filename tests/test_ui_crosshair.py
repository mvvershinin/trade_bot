"""Перекрестье за курсором и выделение участка правой кнопкой.

Просьба владельца счёта 05.09.2026, дословно: «пересечение двигается вместе
с мышью. есть курсор, и от него в стороны и вниз-вверх идёт линия, чтобы видно
было, на какой цене стоит курсор — без кликов, просто модернизированный
курсор». И там же про разбор сделок: «сейчас при зажатии мышки на поле мы
двигаем экран… предлагаю на правую кнопку мыши повесить показ информации».

Что стережёт каждая проверка, по фразе:

* перекрестье появляется от **одного движения мыши**, без единого нажатия;
* линии **пунктирные** и доходят до обеих границ поля свечей;
* перекрестье встаёт на середину места оси — на ту же свечу, что и строка
  сведений, а не на соседнюю;
* обе шкалы подписаны значением под курсором: время внизу, цена справа;
* над **пустым местом** пропуска подпись времени говорит «данных нет»;
* **за краем ряда** подписи времени нет вовсе: время там получено бы
  продолжением оси, а такого времени на бирже не было;
* курсор ушёл с виджета — перекрестье ушло вместе с ним;
* **левая кнопка по-прежнему двигает график** (принято владельцем счёта);
* правая кнопка вид не двигает;
* протяжка правой кнопкой видна на экране, пока кнопка нажата;
* протяжка правой кнопкой отдаёт **тот** отрезок, который выделили;
* щелчок правой кнопкой — тот же отрезок шириной в одно место;
* отрезок с пропуском внутри называет пропуск отдельно;
* выделение мимо ряда не отдаёт пустой отрезок, а не отдаёт ничего;
* меню по правой кнопке не всплывает.

Проверки идут по **нарисованной картинке**, а не по внутренним полям:
между решением «нарисовать перекрестье» и картинкой стоят `_place_at_x`,
`_x_of_index` и окно обзора, и любое из трёх способно нарисовать не там,
оставив поля верными. Способ — разница двух снимков: один без курсора,
второй с курсором. Что изменилось между ними, то перекрестье и нарисовало.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import cast

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QImage, QMouseEvent, QPainter, QPicture
from PySide6.QtWidgets import QApplication

from ui.chart.painter_surface import (
    AXIS_HEIGHT,
    AXIS_WIDTH,
    PADDING,
    PainterChartSurface,
)
from ui.formatting import MSK
from ui.models import Candle, ChartData, ChartSpan

WIDTH, HEIGHT = 1000, 420
DAY = datetime(2026, 6, 19, tzinfo=MSK)
STEP = timedelta(minutes=5)


def _at(hour: int, minute: int) -> datetime:
    return DAY.replace(hour=hour, minute=minute)


def _candle(moment: datetime, close: float) -> Candle:
    """Свеча с четырьмя разными ценами: одинаковые скрыли бы путаницу колонок."""
    return Candle(opens_at=moment, open=close - 50, high=close + 100, low=close - 150, close=close)


#: Двенадцать пятиминуток подряд, 13:00–13:55. Двенадцать — чтобы шаг места
#: на экране был крупным (около 77 точек) и промах на треть шага был виден.
SOLID = tuple(_candle(_at(13, 5 * n), 226_000.0 + 100 * n) for n in range(12))
SOLID_PLACES = 12

#: Ряд из записи `B-005`: три пятиминутки, час без данных, ещё три.
#: На оси это 3 + 12 пустых мест + 3 = 18 мест.
HOLE = (
    _candle(_at(13, 0), 226_000.0),
    _candle(_at(13, 5), 226_100.0),
    _candle(_at(13, 10), 226_200.0),
    _candle(_at(14, 15), 226_800.0),
    _candle(_at(14, 20), 226_900.0),
    _candle(_at(14, 25), 227_000.0),
)
HOLE_PLACES = 18
#: Место в середине часового пропуска: пустые места занимают номера 3…14.
INSIDE_THE_HOLE = 8


def _chart(candles: tuple[Candle, ...]) -> ChartData:
    return ChartData(instrument="MXU6", timeframe="5 минут", candles=candles)


@pytest.fixture()
def surfaces(qapp):
    """Раздатчик чистых отрисовщиков — по одному на график.

    Повторный `show_chart` того же инструмента намеренно сохраняет приближение
    человека (`B-009`), и два ряда, поданных в один виджет, сравнивались бы
    в разных окнах обзора.
    """
    made: list[PainterChartSurface] = []

    def one(candles: tuple[Candle, ...]) -> PainterChartSurface:
        surface = PainterChartSurface()
        surface.resize(WIDTH, HEIGHT)
        surface.show_chart(_chart(candles))
        made.append(surface)
        return surface

    yield one
    for widget in made:
        widget.deleteLater()
    qapp.processEvents()


# ------------------------------------------------------------------ геометрия


def _plot() -> tuple[float, float, float, float]:
    """Границы области построения — арифметика теста, а не вопрос к графику.

    Проверка, спрашивающая координаты у того же кода, который их назначает,
    остаётся зелёной при любом одинаковом сдвиге.
    """
    left, top = float(PADDING), float(PADDING)
    right = float(WIDTH - AXIS_WIDTH)
    bottom = float(HEIGHT - AXIS_HEIGHT)
    return left, top, right, bottom


def _centre(place: int, places: int) -> QPointF:
    """Середина места оси в координатах виджета.

    Верно, пока ряд влезает в окно обзора целиком: тогда левый край — место 0,
    а `span` равен числу мест.
    """
    left, top, right, bottom = _plot()
    step = (right - left) / places
    return QPointF(left + (place + 0.5) * step, (top + bottom) / 2)


# --------------------------------------------------------------------- мышь


def _send(surface: PainterChartSurface, kind, point: QPointF, button, buttons) -> None:
    """Настоящее событие мыши — через `QWidget.event()`, а не вызовом метода."""
    QApplication.sendEvent(
        surface,
        QMouseEvent(kind, point, QPointF(point), button, buttons, Qt.KeyboardModifier.NoModifier),
    )


def _move(surface: PainterChartSurface, point: QPointF, held=Qt.MouseButton.NoButton) -> None:
    _send(surface, QEvent.Type.MouseMove, point, Qt.MouseButton.NoButton, held)


def _press(surface: PainterChartSurface, point: QPointF, button) -> None:
    _send(surface, QEvent.Type.MouseButtonPress, point, button, button)


def _release(surface: PainterChartSurface, point: QPointF, button) -> None:
    _send(surface, QEvent.Type.MouseButtonRelease, point, button, Qt.MouseButton.NoButton)


def _leave(surface: PainterChartSurface) -> None:
    QApplication.sendEvent(surface, QEvent(QEvent.Type.Leave))


# ------------------------------------------------------------------ картинка


def _painted(surface: PainterChartSurface) -> QImage:
    image = QImage(WIDTH, HEIGHT, QImage.Format.Format_RGB32)
    surface.render(image)
    return image


def _drawn_text(surface: PainterChartSurface) -> bytes:
    """Байты записи рисования: текст в ней лежит UTF-16BE, поиск по байтам."""
    picture = QPicture()
    painter = QPainter()
    painter.begin(picture)
    surface.render(painter, QPoint(0, 0))
    painter.end()
    return cast(memoryview, picture.data()).tobytes()


def _changed(before: QImage, after: QImage) -> set[tuple[int, int]]:
    """Точки, которые изменились между двумя снимками.

    Разница снимков, а не поиск цвета: перекрестье рисуется полупрозрачным,
    сглаживание размывает его по двум соседним столбцам, и искать точный цвет
    значило бы искать не то, что нарисовано. Что изменилось после того, как
    мышь встала на график, то перекрестье и добавило.
    """
    return {
        (x, y)
        for x in range(before.width())
        for y in range(before.height())
        if before.pixel(x, y) != after.pixel(x, y)
    }


def _column_counts(points: set[tuple[int, int]]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for x, _ in points:
        counts[x] = counts.get(x, 0) + 1
    return counts


def _busiest_column(points: set[tuple[int, int]]) -> tuple[int, int]:
    """Столбец, в котором изменилось больше всего точек, — вертикаль перекрестья."""
    counts = _column_counts(points)
    assert counts, "на картинке не изменилось ни одной точки"
    x = max(counts, key=lambda key: counts[key])
    return x, counts[x]


def _in_band(points: set[tuple[int, int]], *, x0=0.0, x1=1e9, y0=0.0, y1=1e9) -> int:
    return sum(1 for x, y in points if x0 <= x <= x1 and y0 <= y <= y1)


def _crosshair(surface: PainterChartSurface, point: QPointF) -> set[tuple[int, int]]:
    """Что добавило на картинку одно движение мыши в эту точку."""
    before = _painted(surface)
    _move(surface, point)
    return _changed(before, _painted(surface))


# ------------------------------------------------------------- перекрестье


def test_the_crosshair_appears_from_a_bare_mouse_move(surfaces) -> None:
    """Перекрестье рисуется от движения мыши, без единого нажатия.

    Слова владельца счёта: «без кликов, просто модернизированный курсор».
    Проверка ловит любую попытку повесить его на кнопку или на режим:
    событие движения здесь идёт с `NoButton`.
    """
    surface = surfaces(SOLID)
    points = _crosshair(surface, _centre(5, SOLID_PLACES))

    assert len(points) > 200, (
        f"движение мыши изменило на картинке {len(points)} точек: "
        "перекрестья нет, курсор остался обычным"
    )


def test_the_crosshair_is_dotted_and_reaches_both_edges_of_the_plot(surfaces) -> None:
    """Линия пунктирная и идёт от верхней границы поля свечей до нижней.

    Сплошная линия — не то, о чём просили, и спорит за внимание со свечами;
    линия, обрывающаяся на полпути, не даёт свести свечу со шкалой, ради чего
    всё и затевалось. Пунктир виден по тому, что закрашена **часть** столбца.
    """
    surface = surfaces(SOLID)
    _, top, _, bottom = _plot()
    points = _crosshair(surface, _centre(5, SOLID_PLACES))
    column, _ = _busiest_column(points)
    # Только точки внутри поля свечей: ниже него лежит плашка времени, и она
    # одна дотянула бы «линию» до нижней границы, будь линия вдвое короче.
    rows = sorted(y for x, y in points if x == column and top <= y <= bottom)
    filled = len(rows)
    height = bottom - top

    assert filled < height * 0.8, (
        f"в столбце {column} закрашено {filled} точек из {int(height)}: "
        "линия сплошная, а просили пунктир"
    )
    assert filled > height * 0.25, (
        f"в столбце {column} закрашено всего {filled} точек из {int(height)}: "
        "это не линия, а несколько случайных точек"
    )
    assert rows[0] <= top + 6, (
        f"вертикаль начинается на {rows[0]}, а верх поля свечей — {int(top)}: "
        "линия не доходит до границы"
    )
    assert rows[-1] >= bottom - 6, (
        f"вертикаль кончается на {rows[-1]}, а низ поля свечей — {int(bottom)}: "
        "линия не доходит до границы"
    )


def test_the_crosshair_stands_on_the_candle_under_it_not_beside_it(surfaces) -> None:
    """Вертикаль встаёт на середину места оси, а не туда, где именно мышь.

    Иначе перекрестье показывает одну свечу, а строка сведений сверху —
    другую, и человек сверяет время не с той свечой. Мышь здесь намеренно
    смещена от середины места на треть шага: без привязки вертикаль уехала бы
    вместе с ней.
    """
    surface = surfaces(SOLID)
    left, _, right, _ = _plot()
    step = (right - left) / SOLID_PLACES
    middle = _centre(5, SOLID_PLACES)
    aside = QPointF(middle.x() + step / 3, middle.y())
    points = _crosshair(surface, aside)
    column, _ = _busiest_column(points)

    assert abs(column - middle.x()) <= 2, (
        f"вертикаль встала на {column}, середина места — {middle.x():.0f}, "
        f"мышь стояла на {aside.x():.0f}: перекрестье идёт за мышью, "
        "а не за свечой"
    )


def test_both_axes_are_signed_with_the_value_under_the_cursor(surfaces) -> None:
    """Под перекрестьем подписаны время внизу и цена справа.

    Слова владельца счёта: «чтобы видно было, на какой цене стоит курсор».
    Считаются точки, изменившиеся **в полосах шкал**, — плашка с числом
    закрашивает их сотнями, а без плашки там не меняется ничего.
    """
    surface = surfaces(SOLID)
    _, _, right, bottom = _plot()
    points = _crosshair(surface, _centre(5, SOLID_PLACES))

    price_chip = _in_band(points, x0=right + 1)
    time_chip = _in_band(points, y0=bottom + 1)
    assert price_chip > 400, (
        f"на шкале цены изменилось {price_chip} точек: цены под курсором нет"
    )
    assert time_chip > 400, (
        f"на шкале времени изменилось {time_chip} точек: времени под курсором нет"
    )


def test_the_crosshair_over_an_empty_place_says_there_is_no_data(surfaces) -> None:
    """Над пустым местом пропуска подпись времени говорит, что данных нет.

    Место на оси есть и у пропущенного бара (`B-005`), и время у него честное.
    Но подпись «19.06 13:45» под перекрестьем читается как «здесь свеча
    в 13:45», а свечи здесь нет ни одной.
    """
    surface = surfaces(HOLE)
    _move(surface, _centre(INSIDE_THE_HOLE, HOLE_PLACES))
    drawn = _drawn_text(surface)

    assert "данных нет".encode("utf-16-be") in drawn, (
        "перекрестье над пустым местом подписало время так же, как над свечой: "
        "человек прочтёт это как «здесь была свеча»"
    )


def test_the_crosshair_past_the_series_shows_no_time_at_all(surfaces) -> None:
    """За краем ряда времени под перекрестьем нет вовсе.

    Ось за последней свечой продолжается вычислением, шагом сетки. Время,
    полученное таким продолжением, — не то время, что было на бирже: там
    может быть закрытие торгов, выходной, что угодно. Цена при этом
    подписывается: шкала цены непрерывна и врать не может.
    """
    surface = surfaces(SOLID)
    left, _, right, bottom = _plot()
    # Человек отмотал вправо: справа от последней свечи появилось поле.
    # Поле трогается напрямую — предмет проверки в том, что рисуется
    # за краем ряда, а не в том, как туда попали.
    surface._view.right = SOLID_PLACES - 1 + surface._view.span / 2  # noqa: SLF001
    surface._view.follow = False  # noqa: SLF001

    points = _crosshair(surface, QPointF(right - 20, (PADDING + bottom) / 2))
    time_chip = _in_band(points, y0=bottom + 1)
    price_chip = _in_band(points, x0=right + 1)

    assert time_chip == 0, (
        f"под пустым полем за последней свечой подписано время "
        f"({time_chip} изменившихся точек на шкале): такого времени "
        "на бирже не было, оно получено продолжением оси"
    )
    assert price_chip > 400, (
        "цена под курсором пропала вместе со временем, а шкала цены "
        "непрерывна и подписывается всегда"
    )


def test_the_crosshair_leaves_together_with_the_cursor(surfaces) -> None:
    """Курсор ушёл с графика — перекрестье ушло с ним.

    Оставленное на месте, оно показывает цену, на которую никто не смотрит,
    и время свечи, которую никто не разглядывает.
    """
    surface = surfaces(SOLID)
    clean = _painted(surface)
    _move(surface, _centre(5, SOLID_PLACES))
    assert _changed(clean, _painted(surface)), "перекрестье не появилось вовсе"

    _leave(surface)
    assert not _changed(clean, _painted(surface)), (
        "после ухода курсора на картинке осталось перекрестье"
    )


# --------------------------------------------------- кнопки мыши разведены


def test_the_left_button_still_drags_the_chart(surfaces) -> None:
    """Панорама левой кнопкой работает — это принятое владельцем счёта поведение.

    Сторож на месте потому, что к той же мыши добавились ещё два поведения,
    и любое из них способно съесть события панорамы, ничего не уронив.
    """
    surface = surfaces(SOLID)
    start = _centre(6, SOLID_PLACES)
    was = surface._view.right  # noqa: SLF001

    _press(surface, start, Qt.MouseButton.LeftButton)
    _move(surface, QPointF(start.x() - 120, start.y()), held=Qt.MouseButton.LeftButton)
    _release(surface, QPointF(start.x() - 120, start.y()), Qt.MouseButton.LeftButton)

    assert surface._view.right > was + 0.5, (  # noqa: SLF001
        f"после перетаскивания влево правый край остался на {surface._view.right:.2f} "  # noqa: SLF001
        f"вместо сдвига от {was:.2f}: панорама сломана"
    )


def test_the_right_button_does_not_drag_the_chart(surfaces) -> None:
    """Правая кнопка выделяет участок и вид не двигает.

    Иначе выделение уезжало бы вместе с графиком, и человек отпускал бы кнопку
    совсем не над тем куском, который отмечал.
    """
    surface = surfaces(SOLID)
    start = _centre(6, SOLID_PLACES)
    was = surface._view.right  # noqa: SLF001

    _press(surface, start, Qt.MouseButton.RightButton)
    _move(surface, QPointF(start.x() - 200, start.y()), held=Qt.MouseButton.RightButton)

    assert surface._view.right == was, (  # noqa: SLF001
        "протяжка правой кнопкой сдвинула график: выделение и панорама "
        "оказались на одной кнопке"
    )


def test_the_stretch_being_selected_is_visible_while_the_button_is_held(surfaces) -> None:
    """Пока правая кнопка нажата, выделяемая полоса видна на картинке.

    Без неё протяжка выглядит как «ничего не происходит», и человек не знает,
    что он вообще выделяет.
    """
    surface = surfaces(SOLID)
    left = _centre(3, SOLID_PLACES)
    right = _centre(7, SOLID_PLACES)
    # Снимок для сравнения — с курсором **в той же** конечной точке и без
    # выделения: иначе разница снимков покажет переехавшее перекрестье,
    # а не полосу.
    _move(surface, right)
    plain = _painted(surface)

    _move(surface, left)
    _press(surface, left, Qt.MouseButton.RightButton)
    _move(surface, right, held=Qt.MouseButton.RightButton)
    during = _in_band(_changed(plain, _painted(surface)), x0=left.x() + 4, x1=right.x() - 4)

    assert during > 2000, (
        f"между началом и концом протяжки изменилось {during} точек: "
        "полосы выделения на экране нет"
    )

    _release(surface, right, Qt.MouseButton.RightButton)
    after = _in_band(_changed(plain, _painted(surface)), x0=left.x() + 4, x1=right.x() - 4)
    assert after < 200, (
        f"после отпускания кнопки внутри участка осталось {after} изменённых "
        "точек: полоса выделения не убралась и спорит за внимание с разбором, "
        "который на её месте открылся"
    )


# ------------------------------------------------------------ отбор участка


def _caught(surface: PainterChartSurface) -> list[ChartSpan | None]:
    """Что отрисовщик рассказал про выделение."""
    heard: list[ChartSpan | None] = []
    surface.set_span_handler(heard.append)
    return heard


def test_a_right_drag_reports_the_stretch_it_covered_and_no_other(surfaces) -> None:
    """Отрезок — тот, который выделили: от первой отмеченной свечи до последней.

    Конец отрезка — **конец** последнего бара, а не его начало: сделка,
    случившаяся внутри последней выделенной пятиминутки, обязана в него попасть.
    """
    surface = surfaces(SOLID)
    heard = _caught(surface)

    _press(surface, _centre(3, SOLID_PLACES), Qt.MouseButton.RightButton)
    _move(surface, _centre(7, SOLID_PLACES), held=Qt.MouseButton.RightButton)
    _release(surface, _centre(7, SOLID_PLACES), Qt.MouseButton.RightButton)

    assert len(heard) == 1, f"про выделение сказано {len(heard)} раз вместо одного"
    span = heard[0]
    assert span is not None, "выделение по свечам не отдало отрезка вовсе"
    assert span.start == SOLID[3].opens_at, (
        f"отрезок начинается в {span.start:%H:%M}, а выделили с {SOLID[3].opens_at:%H:%M}"
    )
    assert span.end == SOLID[7].opens_at + STEP, (
        f"отрезок кончается в {span.end:%H:%M}, а последний выделенный бар "
        f"закрывается в {(SOLID[7].opens_at + STEP):%H:%M}: сделка внутри него "
        "в разбор не попадёт"
    )


def test_a_right_click_reports_the_single_place_it_hit(surfaces) -> None:
    """Щелчок правой кнопкой — тот же отрезок, шириной в одно место оси.

    Отдельного случая для щелчка нет намеренно: отбор сделок один на оба
    действия, и человеку нечего запоминать.
    """
    surface = surfaces(SOLID)
    heard = _caught(surface)
    point = _centre(5, SOLID_PLACES)

    _press(surface, point, Qt.MouseButton.RightButton)
    _release(surface, point, Qt.MouseButton.RightButton)

    assert len(heard) == 1 and heard[0] is not None
    span = heard[0]
    assert (span.start, span.end) == (SOLID[5].opens_at, SOLID[5].opens_at + STEP), (
        f"щелчок по свече 13:25 отдал отрезок {span.start:%H:%M}–{span.end:%H:%M}"
    )


def test_a_stretch_with_a_hole_inside_names_the_hole(surfaces) -> None:
    """Пропуск внутри выделения приезжает отдельным полем, а не молчанием.

    «Сделок не было» и «свечей за это время нет» — разные утверждения,
    и второе, сказанное вместо первого, есть содержание `B-005`.
    """
    surface = surfaces(HOLE)
    heard = _caught(surface)

    _press(surface, _centre(1, HOLE_PLACES), Qt.MouseButton.RightButton)
    _move(surface, _centre(16, HOLE_PLACES), held=Qt.MouseButton.RightButton)
    _release(surface, _centre(16, HOLE_PLACES), Qt.MouseButton.RightButton)

    span = heard[0]
    assert span is not None and span.holes, (
        "в выделении был час без данных, а отрезок про него промолчал"
    )
    since, until = span.holes[0]
    assert (since.hour, since.minute) == (13, 15), (
        f"пропуск назван с {since:%H:%M}, а данных нет с 13:15"
    )
    assert (until.hour, until.minute) == (14, 15), (
        f"пропуск назван по {until:%H:%M}, а следующая свеча — в 14:15"
    )


def test_a_selection_beside_the_series_reports_nothing_at_all(surfaces) -> None:
    """Выделение мимо ряда не отдаёт пустой отрезок, а не отдаёт ничего.

    Пустой отрезок открыл бы разбор со словами «сделок здесь нет» про поле,
    где нет и самой оси. Это разные ответы.
    """
    surface = surfaces(SOLID)
    heard = _caught(surface)
    _, _, right, bottom = _plot()
    surface._view.right = SOLID_PLACES - 1 + surface._view.span / 2  # noqa: SLF001
    surface._view.follow = False  # noqa: SLF001
    middle = (PADDING + bottom) / 2

    _press(surface, QPointF(right - 40, middle), Qt.MouseButton.RightButton)
    _move(surface, QPointF(right - 5, middle), held=Qt.MouseButton.RightButton)
    _release(surface, QPointF(right - 5, middle), Qt.MouseButton.RightButton)

    assert heard == [None], (
        f"выделение за последней свечой отдало {heard[0]!r} вместо «ничего»"
    )


def test_no_context_menu_pops_up_over_the_chart(surfaces) -> None:
    """Правая кнопка занята выделением, и меню поверх графика не всплывает.

    Всплывшее меню перехватило бы отпускание кнопки, и разбор не открылся бы
    вовсе — а виноватым выглядел бы разбор.
    """
    surface = surfaces(SOLID)
    assert surface.contextMenuPolicy() == Qt.ContextMenuPolicy.PreventContextMenu, (
        "виджету графика оставлено меню по правой кнопке: оно перехватит "
        "выделение участка"
    )
