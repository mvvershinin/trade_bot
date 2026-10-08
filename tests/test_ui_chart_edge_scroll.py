"""Протяжка правой кнопкой за край графика двигает вид вслед за мышью (Н4).

Просьба владельца счёта: «если зажали правую кнопку мыши, чтобы выделить,
и дошли до края окна — двигать окно вслед за мышью».

Что стережёт каждая проверка, по фразе:

* мышь за **правым** краем с зажатой правой кнопкой — вид едет вправо,
  выделение становится длиннее экрана и начинается **с той же свечи**,
  на которой нажали (начало держится по месту оси, а не по пикселю);
* то же за **левым** краем — зеркально;
* вид двигает **таймер**: пока мышь стоит за краем, событий движения нет,
  и без таймера вид не сдвинулся бы ни на свечу (проверено настоящим
  циклом событий — ловит и неподключённый таймер, и незапущенный);
* дальше мышь — быстрее;
* отпустили кнопку — прокрутка прекратилась;
* у конца и у начала данных вид останавливается, в пустоту не уезжает.

Вход подставной: ряд из 200 пятиминуток, события мыши — настоящие
`QMouseEvent` через `QWidget.event()`, шаги таймера — прямым вызовом его слота
после проверки, что таймер запущен.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt, QTimer
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from ui.chart.painter_surface import (
    AXIS_HEIGHT,
    AXIS_WIDTH,
    EDGE_SCROLL_INTERVAL_MS,
    PADDING,
    PainterChartSurface,
)
from ui.formatting import MSK
from ui.models import Candle, ChartData, ChartSpan

WIDTH, HEIGHT = 1000, 420
STEP = timedelta(minutes=5)
START = datetime(2026, 6, 19, 7, 0, tzinfo=MSK)
COUNT = 200
CANDLES = tuple(
    Candle(
        opens_at=START + n * STEP,
        open=226_000.0 + n,
        high=226_100.0 + n,
        low=225_900.0 + n,
        close=226_050.0 + n,
    )
    for n in range(COUNT)
)
#: Сколько мест видно на экране: ряд длиннее экрана вчетверо с лишним.
VISIBLE = 40
RIGHT = Qt.MouseButton.RightButton

LEFT_EDGE = float(PADDING)
RIGHT_EDGE = float(WIDTH - AXIS_WIDTH)
MIDDLE_Y = (PADDING + HEIGHT - AXIS_HEIGHT) / 2


@pytest.fixture()
def surface(qapp):
    """График, отмотанный в середину ряда: ехать есть куда в обе стороны."""
    made = PainterChartSurface()
    made.resize(WIDTH, HEIGHT)
    made.show_chart(ChartData(instrument="MXU6", timeframe="5 минут", candles=CANDLES))
    view = made._view  # noqa: SLF001 — тест ставит вид, как поставил бы человек колесом
    view.span, view.right, view.follow = VISIBLE, 120.0, False
    yield made
    made.deleteLater()
    qapp.processEvents()


def _send(surface: PainterChartSurface, kind, x: float, button, buttons) -> None:
    point = QPointF(x, MIDDLE_Y)
    QApplication.sendEvent(
        surface,
        QMouseEvent(kind, point, QPointF(point), button, buttons, Qt.KeyboardModifier.NoModifier),
    )


def _press(surface: PainterChartSurface, x: float) -> None:
    _send(surface, QEvent.Type.MouseButtonPress, x, RIGHT, RIGHT)


def _drag(surface: PainterChartSurface, x: float) -> None:
    _send(surface, QEvent.Type.MouseMove, x, Qt.MouseButton.NoButton, RIGHT)


def _release(surface: PainterChartSurface, x: float) -> None:
    _send(surface, QEvent.Type.MouseButtonRelease, x, RIGHT, Qt.MouseButton.NoButton)


def _place_x(place: int, right: float) -> float:
    """Середина места оси на экране — арифметикой теста, не вопросом к графику."""
    step = (RIGHT_EDGE - LEFT_EDGE) / VISIBLE
    first = right - VISIBLE + 1
    return LEFT_EDGE + (place - first + 0.5) * step


def _ticks(surface: PainterChartSurface, count: int) -> None:
    """Шаги таймера прокрутки. Таймер обязан быть запущен — иначе шагов нет."""
    timer = _timer(surface)
    assert timer.isActive(), (
        "мышь с зажатой правой кнопкой стоит за краем графика, а таймер "
        "прокрутки не запущен: вид не поедет, пока мышь не шевельнётся"
    )
    for _ in range(count):
        if not timer.isActive():
            break
        surface._edge_scroll_tick()  # noqa: SLF001 — подставной ход часов вместо цикла событий


def _caught(surface: PainterChartSurface) -> list[ChartSpan | None]:
    heard: list[ChartSpan | None] = []
    surface.set_span_handler(heard.append)
    return heard


def _timer(surface: PainterChartSurface) -> QTimer:
    return surface._edge_timer  # noqa: SLF001 — запущен ли таймер прокрутки, и есть предмет


def _right(surface: PainterChartSurface) -> float:
    return surface._view.right  # noqa: SLF001 — положение вида и есть предмет проверки


def test_past_the_right_edge_the_view_follows_and_the_span_keeps_its_start(surface) -> None:
    heard = _caught(surface)
    pressed = 110
    _press(surface, _place_x(pressed, 120.0))
    _drag(surface, RIGHT_EDGE + 40)
    _ticks(surface, 30)

    assert _right(surface) > 120.0 + VISIBLE, (
        f"мышь тридцать шагов стояла за правым краем, а правый край вида "
        f"на {_right(surface):.1f} вместо дальше {120 + VISIBLE}: вид за мышью не едет"
    )
    _release(surface, RIGHT_EDGE + 40)
    span = heard[0]
    assert span is not None, "выделение с прокруткой не отдало отрезка вовсе"
    assert span.start == CANDLES[pressed].opens_at, (
        f"выделение начинается в {span.start:%H:%M}, а нажали на свече "
        f"{CANDLES[pressed].opens_at:%H:%M}: начало уехало вместе с видом"
    )
    assert span.end - span.start > VISIBLE * STEP, (
        f"выделено {span.end - span.start}, а это не длиннее экрана "
        f"({VISIBLE * STEP}): прокрутка выделение не дорастила"
    )


def test_past_the_left_edge_the_view_follows_back(surface) -> None:
    heard = _caught(surface)
    pressed = 115
    _press(surface, _place_x(pressed, 120.0))
    _drag(surface, LEFT_EDGE - 30)
    _ticks(surface, 25)

    first = _right(surface) - VISIBLE + 1
    assert first < 120.0 - VISIBLE, (
        f"мышь стояла за левым краем, а левый край вида на {first:.1f}: вид назад не едет"
    )
    _release(surface, LEFT_EDGE - 30)
    span = heard[0]
    assert span is not None
    assert span.end == CANDLES[pressed].opens_at + STEP, (
        f"выделение кончается в {span.end:%H:%M}, а нажали на свече "
        f"{CANDLES[pressed].opens_at:%H:%M}: конец уехал вместе с видом"
    )
    assert span.end - span.start > VISIBLE * STEP


def test_further_past_the_edge_scrolls_faster(surface) -> None:
    _press(surface, _place_x(110, 120.0))
    _drag(surface, RIGHT_EDGE + 5)
    _ticks(surface, 5)
    near = _right(surface) - 120.0
    _drag(surface, RIGHT_EDGE + 60)
    was = _right(surface)
    _ticks(surface, 5)
    far = _right(surface) - was
    assert far > near * 2, (
        f"за краем на 60 точек вид проехал {far:.1f} мест, на 5 точек — {near:.1f}: "
        "скорость от расстояния не зависит"
    )


def test_inside_the_plot_nothing_scrolls(surface) -> None:
    _press(surface, _place_x(110, 120.0))
    _drag(surface, RIGHT_EDGE + 40)
    _drag(surface, RIGHT_EDGE - 20)
    assert not _timer(surface).isActive(), (
        "мышь вернулась на поле свечей, а прокрутка продолжается"
    )


def test_the_view_scrolls_by_the_event_loop_itself(surface) -> None:
    """Настоящий цикл событий, без ручных шагов: таймер запущен **и** подключён."""
    _press(surface, _place_x(110, 120.0))
    _drag(surface, RIGHT_EDGE + 40)
    QTest.qWait(EDGE_SCROLL_INTERVAL_MS * 6)
    assert _right(surface) > 121.0, (
        f"мышь стоит за краем {EDGE_SCROLL_INTERVAL_MS * 6} мс без движения, "
        f"а вид на {_right(surface):.1f}: ехать его заставляет только шевеление мыши"
    )


def test_release_stops_the_scroll(surface) -> None:
    _press(surface, _place_x(110, 120.0))
    _drag(surface, RIGHT_EDGE + 40)
    _ticks(surface, 3)
    _release(surface, RIGHT_EDGE + 40)
    stopped = _right(surface)
    QTest.qWait(EDGE_SCROLL_INTERVAL_MS * 4)
    assert not _timer(surface).isActive(), (
        "кнопку отпустили, а таймер прокрутки работает"
    )
    assert _right(surface) == stopped, (
        f"после отпускания вид проехал с {stopped:.1f} до {_right(surface):.1f}"
    )


def test_the_view_stops_at_the_end_of_data(surface) -> None:
    _press(surface, _place_x(110, 120.0))
    _drag(surface, RIGHT_EDGE + 200)
    _ticks(surface, 500)
    assert _right(surface) == float(COUNT - 1), (
        f"правый край вида на {_right(surface):.1f}, последняя свеча — {COUNT - 1}: "
        "протяжка увезла вид в пустоту или не довезла до конца"
    )
    assert not _timer(surface).isActive(), (
        "у конца данных ехать некуда, а таймер прокрутки крутится вхолостую"
    )


def test_the_view_stops_at_the_start_of_data(surface) -> None:
    _press(surface, _place_x(110, 120.0))
    _drag(surface, LEFT_EDGE - 200)
    _ticks(surface, 500)
    first = _right(surface) - VISIBLE + 1
    assert first == 0.0, (
        f"левый край вида на {first:.1f}, первая свеча — 0: "
        "протяжка увезла вид в пустоту или не довезла до начала"
    )
