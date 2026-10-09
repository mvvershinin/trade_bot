"""«Весь период», разбор всех сделок, быстрая прокрутка и разворот графика.

Просьба владельца счёта 09.10.2026: «по-прежнему неудобно — нельзя увеличить
график и выделить ВЕСЬ период, листается со скоростью черепахи».

Что стережёт каждая проверка, по фразе:

* нажатие **кнопки** «Весь период» панели кладёт на поле графика и первую,
  и последнюю свечу ряда длиной больше десяти тысяч мест — по пикселям
  картинки, а не по вопросу к графику, где у него вид;
* «Весь период» — режим: пришедшая живая свеча и новый прогон того же
  графика показываются тоже целиком, а колесо после него приближает;
* кнопка «Разбор всех сделок» открывает разбор, где сделок столько же,
  сколько передано панели, включая сделки на первой и на последней свече
  ряда, и пропуск данных назван; отрезок приходит тем же обработчиком
  и равен отрезку протяжки правой кнопкой через весь ряд;
* до прогона разбор говорит «сделок в окно ещё не передавали»; на пустом
  графике кнопки погашены, и ни пустой ряд, ни ряд из одной свечи окно
  не роняют;
* протяжка за край: сдвиг за шаг пропорционален видимому размаху; далёкий
  выход за край проезжает экран не дольше полсекунды; чуть за краем
  на 90 местах — прежние 25–40 свечей в секунду;
* колесо: от 90 мест до 16 тысяч — не больше 15 щелчков; мелкий шаг
  тачпада на малом размахе не застревает;
* «Развернуть график» и двойной щелчок левой в настоящем разделителе
  схлопывают соседа и возвращают прежние размеры; двойной щелчок правой —
  по-прежнему выделение; вне разделителя кнопки нет;
* пропусков данных или затенённых отрезков в участке больше трёх — одна
  строка со счётом вместо списка.

Вход подставной: ряды пятиминуток, собранные здесь же, без базы и без сети.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QSplitter, QWidget

from ui.chart.painter_surface import (
    AXIS_HEIGHT,
    AXIS_WIDTH,
    PADDING,
    PainterChartSurface,
)
from ui.chart_panel import ChartPanel, digest_lines
from ui.formatting import MSK
from ui.models import Candle, ChartData, ChartSpan, Shade, ShadeKind, Side, TradeRow
from ui.theme import LIGHT

START = datetime(2026, 6, 19, 7, 0, tzinfo=MSK)
STEP = timedelta(minutes=5)
RIGHT = Qt.MouseButton.RightButton
LEFT = Qt.MouseButton.LeftButton

#: Длина «длинного» ряда — больше десяти тысяч мест, как просил владелец
#: счёта: на настоящей базе их 15 тысяч.
LONG = 12_000
#: Отметины крайних свечей: первая выше всех, последняя ниже всех. Вертикаль
#: такой длины в крайнем столбце поля бывает, только если свеча на экране.
MARK = 5_000.0


def _row(count: int, *, start: datetime = START) -> tuple[Candle, ...]:
    """Ряд пятиминуток подряд; цена гуляет в пределах сотни пунктов."""
    return tuple(
        Candle(
            opens_at=start + n * STEP,
            open=226_000.0 + n % 50,
            high=226_040.0 + n % 50,
            low=225_960.0 + n % 50,
            close=226_010.0 + n % 50,
        )
        for n in range(count)
    )


def _marked(candles: tuple[Candle, ...]) -> tuple[Candle, ...]:
    """Тот же ряд, у которого крайние свечи видны издалека."""
    first = replace(candles[0], high=candles[0].high + MARK)
    last = replace(candles[-1], low=candles[-1].low - MARK)
    return (first, *candles[1:-1], last)


def settle_qt(qapp) -> None:
    """Догасить очередь Qt вместе с отложенными удалениями — как `helpers.settle_qt`.

    Своя копия в три строки, а не импорт: `helpers` mypy не находит
    (`import-not-found`), и импорт добавил бы замечание в общий счёт.
    """
    qapp.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


def _data(candles: tuple[Candle, ...]) -> ChartData:
    return ChartData(instrument="MXZ6", timeframe="5 минут", candles=candles)


@pytest.fixture()
def panel(qapp):
    """Панель с настоящим отрисовщиком, показанная, светлая тема."""
    made = ChartPanel(surface=PainterChartSurface())
    made.set_theme(LIGHT)
    made.legend_button.setChecked(False)  # легенда отнимала бы высоту у поля
    made.resize(1000, 560)
    made.show()
    qapp.processEvents()
    yield made
    made.digest.hide()
    made.deleteLater()
    settle_qt(qapp)


def _surface(panel: ChartPanel) -> PainterChartSurface:
    surface = panel._surface  # noqa: SLF001 — картинку снимаем с самого графика
    assert isinstance(surface, PainterChartSurface)
    return surface


def _candle_colours() -> list[tuple[int, int, int]]:
    """Цвета свечей на картинке: чистые и наполовину под линией оси цены.

    Ось цены — линия по правому краю поля со сглаживанием, и последний
    столбец свечей лежит под её половиной: там цвет свечи смешан с цветом
    оси ровно пополам. Без этого правый край ряда не нашёлся бы никогда.
    """
    axis = QColor(LIGHT.axis)
    found = []
    for name in (LIGHT.bull, LIGHT.bear):
        pure = QColor(name)
        found.append((pure.red(), pure.green(), pure.blue()))
        found.append((
            (pure.red() + axis.red()) // 2,
            (pure.green() + axis.green()) // 2,
            (pure.blue() + axis.blue()) // 2,
        ))
    return found


def _longest_run(image: QImage, x: int, top: int, bottom: int) -> int:
    """Самая длинная вертикаль цвета свечей в столбце `x` картинки, в точках."""
    colours = _candle_colours()
    best = run = 0
    for y in range(top, bottom):
        pixel = QColor(image.pixel(x, y))
        here = (pixel.red(), pixel.green(), pixel.blue())
        candle = any(max(abs(a - b) for a, b in zip(here, c, strict=True)) <= 2 for c in colours)
        run = run + 1 if candle else 0
        best = max(best, run)
    return best


def _both_ends_on_the_field(surface: PainterChartSurface) -> tuple[int, int, int]:
    """Длины отметин в трёх крайних столбцах поля слева и справа и высота поля.

    Границы поля — арифметикой теста из полей отрисовки, а не вопросом
    к графику: ответ проверяемого кода был бы верен при любой его поломке.
    """
    image = surface.grab().toImage()
    left, right = PADDING, surface.width() - AXIS_WIDTH
    top, bottom = PADDING, surface.height() - AXIS_HEIGHT
    first = max(_longest_run(image, x, top, bottom) for x in range(left, left + 3))
    last = max(_longest_run(image, x, top, bottom) for x in range(right - 3, right))
    return first, last, bottom - top


def _assert_whole(surface: PainterChartSurface, when: str) -> None:
    first, last, height = _both_ends_on_the_field(surface)
    assert first > height * 0.3, (
        f"{when}: у левого края поля нет первой свечи ряда (отметина {first} точек "
        f"при высоте поля {height}) — начало периода за экраном"
    )
    assert last > height * 0.3, (
        f"{when}: у правого края поля нет последней свечи ряда (отметина {last} "
        f"точек при высоте поля {height}) — конец периода за экраном"
    )


# =========================================================== п.1 «Весь период»


def test_the_whole_period_button_puts_both_ends_of_a_long_row_on_the_field(panel, qapp) -> None:
    panel.show_chart(_data(_marked(_row(LONG))))
    qapp.processEvents()
    first, _, height = _both_ends_on_the_field(_surface(panel))
    assert first < height * 0.3, "проверка вакуумна: начало ряда видно и без кнопки"

    QTest.mouseClick(panel.all_button, LEFT)
    qapp.processEvents()

    _assert_whole(_surface(panel), "после кнопки «Весь период»")


def test_the_whole_period_survives_a_live_candle_and_a_new_run(panel, qapp) -> None:
    candles = _row(LONG)
    panel.show_chart(_data(_marked(candles)))
    QTest.mouseClick(panel.all_button, LEFT)
    surface = _surface(panel)

    fresh = replace(candles[-1], opens_at=candles[-1].opens_at + STEP)
    fresh = replace(fresh, low=fresh.low - MARK)
    panel.append_candle(fresh)
    qapp.processEvents()
    _assert_whole(surface, "после живой свечи")

    longer = _marked(_row(LONG + 300))
    panel.show_chart(_data(longer))
    qapp.processEvents()
    _assert_whole(surface, "после нового прогона того же графика")

    before = surface._view.span  # noqa: SLF001 — размах и есть предмет проверки колеса
    _wheel(surface, 120)
    assert surface._view.span < before, "после «Весь период» колесо не приближает"  # noqa: SLF001


# ===================================================== п.2 разбор всех сделок


def _trade(entry: Candle, exit_: Candle | None, reason: str) -> TradeRow:
    return TradeRow(
        entry_time=entry.opens_at,
        exit_time=exit_.opens_at if exit_ is not None else None,
        side=Side.LONG,
        volume=1,
        entry_price=entry.close,
        exit_price=exit_.close if exit_ is not None else None,
        exit_reason=reason,
        profit_rub=100.0,
        profit_pct=0.1,
        commission_rub=28.0,
    )


def _holey_row() -> tuple[Candle, ...]:
    """Шестьсот мест с часовым пропуском посреди дня (15:20–16:20)."""
    whole = _row(600)
    return whole[:100] + whole[112:]


def _edge_trades(candles: tuple[Candle, ...]) -> list[TradeRow]:
    """Сделки на первой свече ряда, на последней (открыта) и между ними.

    Первая сделка и входит, и выходит на первой свече: её задевает только
    первое место ряда. Выход на третьей свече (как было до 09.10.2026)
    оставлял сделку в разборе и при отрезке, потерявшем первое место, —
    отбор берёт сделки, задевшие участок хоть краем.
    """
    return [
        _trade(candles[0], candles[0], "выход-на-первой-свече"),
        _trade(candles[200], candles[230], "выход-в-середине"),
        _trade(candles[400], candles[420], "выход-ближе-к-концу"),
        _trade(candles[-1], None, "открыта-на-последней-свече"),
    ]


def test_the_digest_of_all_trades_holds_every_trade_including_both_ends(panel, qapp) -> None:
    candles = _holey_row()
    trades = _edge_trades(candles)
    panel.show_chart(_data(candles))
    panel.set_trades(trades)

    QTest.mouseClick(panel.digest_button, LEFT)
    qapp.processEvents()

    assert panel.digest.isVisible(), "кнопка «Разбор всех сделок» разбор не открыла"
    shown = panel.digest.body.text()
    assert f"Сделок в участке: {len(trades)}" in shown, (
        f"передано сделок {len(trades)}, а разбор насчитал другое: {shown[:400]!r}"
    )
    for row in trades:
        assert row.exit_reason in shown, (
            f"в разборе всех сделок нет сделки «{row.exit_reason}» "
            f"({row.entry_time:%d.%m %H:%M}): край ряда отрезан"
        )
    assert "пропуск данных" in shown, "пропуск данных внутри ряда в разборе не назван"


def test_the_digest_of_all_is_the_same_span_as_a_right_drag_through_the_row(
    panel, qapp
) -> None:
    """Один путь на оба действия: тот же обработчик, то же значение отрезка."""
    candles = _holey_row()
    panel.show_chart(_data(candles))
    panel.set_trades(_edge_trades(candles))
    surface = _surface(panel)
    heard: list[ChartSpan | None] = []

    def listen(span: ChartSpan | None) -> None:
        heard.append(span)
        panel.show_span(span)

    surface.set_span_handler(listen)

    QTest.mouseClick(panel.digest_button, LEFT)
    QTest.mouseClick(panel.all_button, LEFT)
    middle = (PADDING + surface.height() - AXIS_HEIGHT) / 2
    far_right = surface.width() - AXIS_WIDTH - 0.5
    _mouse(surface, QEvent.Type.MouseButtonPress, (PADDING + 0.5, middle), RIGHT, RIGHT)
    _mouse(surface, QEvent.Type.MouseMove, (far_right, middle), Qt.MouseButton.NoButton, RIGHT)
    _mouse(surface, QEvent.Type.MouseButtonRelease, (far_right, middle),
           RIGHT, Qt.MouseButton.NoButton)

    assert len(heard) == 2 and heard[0] is not None, f"отрезков пришло: {heard!r}"
    assert heard[0] == heard[1], (
        f"разбор всех сделок разбирает {heard[0]!r}, а протяжка через весь ряд — "
        f"{heard[1]!r}: у кнопки свой путь, и разбор разойдётся с протяжкой"
    )
    assert heard[0].holes, "пропуск данных не доехал до отрезка"


def test_the_digest_of_all_before_any_run_says_no_trades_were_handed_over(panel, qapp) -> None:
    panel.show_chart(_data(_row(50)))
    QTest.mouseClick(panel.digest_button, LEFT)
    qapp.processEvents()
    assert panel.digest.isVisible(), "до прогона кнопка разбора промолчала"
    assert "сделок в окно ещё не передавали" in panel.digest.body.text()


def test_an_empty_chart_dims_the_buttons_and_one_candle_breaks_nothing(panel, qapp) -> None:
    assert not panel.all_button.isEnabled() and not panel.digest_button.isEnabled(), (
        "свечей нет, а кнопки «Весь период» и «Разбор всех сделок» нажимаются впустую"
    )
    surface = _surface(panel)
    surface.show_all()
    surface.select_all()  # пустой ряд: обработчику `None`, окно не открывается
    assert not panel.digest.isVisible()

    panel.show_chart(_data(_row(1)))
    assert panel.all_button.isEnabled() and panel.digest_button.isEnabled()
    QTest.mouseClick(panel.all_button, LEFT)
    QTest.mouseClick(panel.digest_button, LEFT)
    qapp.processEvents()
    assert panel.digest.isVisible(), "ряд из одной свечи: разбор всех сделок не открылся"


# =================================================== п.3 прокрутка и колесо


def _mouse(surface, kind, at: tuple[float, float], button, buttons) -> None:
    point = QPointF(*at)
    QApplication.sendEvent(
        surface,
        QMouseEvent(kind, point, QPointF(point), button, buttons, Qt.KeyboardModifier.NoModifier),
    )


def _wheel(surface: PainterChartSurface, delta: int) -> None:
    centre = QPointF(surface.width() / 2, surface.height() / 2)
    QApplication.sendEvent(surface, QWheelEvent(
        centre, surface.mapToGlobal(centre.toPoint()).toPointF(),
        QPoint(0, 0), QPoint(0, delta), Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False,
    ))


@pytest.fixture()
def wide_row(qapp):
    """График на 20 тысяч мест, 1000×420 точек, без панели вокруг."""
    made = PainterChartSurface()
    made.resize(1000, 420)
    made.show_chart(_data(_row(20_000)))
    yield made
    made.deleteLater()
    settle_qt(qapp)


def _edge_ticks(
    surface: PainterChartSurface,
    span: int,
    overshoot: float,
    ticks: int,
    *,
    leftward: bool = False,
) -> tuple[float, int]:
    """Поставить вид, протянуть правой за край и сделать шаги таймера.

    Возвращает, на сколько мест вид уехал в сторону мыши, и шаг часов
    прокрутки в мс — снятый с **запущенного** таймера: интервал, с которым
    его запустило событие мыши, и есть настоящая скорость. Снятый до
    запуска, он пропускал запуск с другим интервалом (мутация 09.10.2026:
    `start(40 × 3)` — экран втрое медленнее, сторож «полсекунды» зелёный).
    Таймер обязан быть запущен событием мыши — иначе шаги вручную
    проверяли бы не то.

    Влево вид стартует от 19 тысяч, вправо от 10 тысяч: на ряде в 20 тысяч
    мест у обоих хватает хода на экран в 9000 мест до края данных.
    """
    start = 19_000.0 if leftward else 10_000.0
    view = surface._view  # noqa: SLF001 — вид ставим, как поставил бы человек колесом
    view.span, view.right, view.follow = span, start, False
    middle = (PADDING + surface.height() - AXIS_HEIGHT) / 2
    left_edge, right_edge = PADDING, surface.width() - AXIS_WIDTH
    if leftward:
        pressed, held = left_edge + 100, left_edge - overshoot
    else:
        pressed, held = right_edge - 100, right_edge + overshoot
    _mouse(surface, QEvent.Type.MouseButtonPress, (pressed, middle), RIGHT, RIGHT)
    _mouse(surface, QEvent.Type.MouseMove, (held, middle), Qt.MouseButton.NoButton, RIGHT)
    timer = surface._edge_timer  # noqa: SLF001 — запущен ли таймер, и есть предмет
    assert timer.isActive(), "мышь за краем, а таймер прокрутки не запущен"
    interval = timer.interval()
    for _ in range(ticks):
        surface._edge_scroll_tick()  # noqa: SLF001 — подставной ход часов
    moved = start - view.right if leftward else view.right - start
    _mouse(surface, QEvent.Type.MouseButtonRelease, (held, middle),
           RIGHT, Qt.MouseButton.NoButton)
    return moved, interval


def test_the_edge_scroll_step_grows_with_the_visible_span(wide_row) -> None:
    near, _ = _edge_ticks(wide_row, 90, 40.0, 1)
    far, _ = _edge_ticks(wide_row, 9_000, 40.0, 1)
    assert near > 0, "протяжка за край вид не сдвинула"
    assert 90 <= far / near <= 110, (
        f"при одном и том же выходе за край сдвиг за шаг {near:.2f} места на 90 "
        f"видимых и {far:.2f} на 9000: отношение {far / near:.1f} вместо ~100. "
        "Скорость не следует за размахом, и весь период листается черепахой"
    )


@pytest.mark.parametrize("leftward", [False, True], ids=["right_edge", "left_edge"])
def test_far_past_the_edge_the_screen_passes_in_half_a_second(wide_row, leftward: bool) -> None:
    """Обе стороны: левый край считается своей веткой, и черепаха там была бы не видна."""
    for span in (90, 9_000):
        ticks = 0
        moved = 0.0
        interval = 0
        while moved < span and ticks < 100:
            ticks += 1
            moved, interval = _edge_ticks(wide_row, span, 150.0, ticks, leftward=leftward)
        assert moved >= span and ticks * interval <= 500, (
            f"мышь на 150 точек за краем, видно {span} мест: экран проезжается "
            f"за {ticks * interval} мс (уехало {moved:.0f} мест), а не за полсекунды"
        )


def test_just_past_the_edge_ninety_candles_scroll_as_before(wide_row) -> None:
    moved, interval = _edge_ticks(wide_row, 90, 20.0, 1)
    per_second = moved * 1000 / interval
    assert 25 <= per_second <= 40, (
        f"чуть за краем (20 точек) на 90 свечах вид едет {per_second:.0f} свечей "
        "в секунду вместо прежних 25–40: точная протяжка у края сломана"
    )


def test_the_wheel_reaches_sixteen_thousand_places_within_fifteen_clicks(qapp) -> None:
    surface = PainterChartSurface()
    surface.resize(1000, 420)
    surface.show_chart(_data(_row(16_000)))
    try:
        assert surface._view.span == 90  # noqa: SLF001 — начало счёта
        clicks = 0
        while surface._view.span < 16_000 and clicks < 40:  # noqa: SLF001
            _wheel(surface, -120)
            clicks += 1
        assert clicks <= 15, (
            f"от 90 мест до всего ряда в 16 тысяч — {clicks} щелчков колеса"
        )
    finally:
        surface.deleteLater()
        settle_qt(qapp)


@pytest.mark.parametrize("delta", [15, 5], ids=["eighth_of_a_click", "smallest_step"])
def test_a_small_touchpad_step_does_not_stick_on_a_small_span(wide_row, delta: int) -> None:
    view = wide_row._view  # noqa: SLF001 — размах и есть предмет проверки
    view.span = 12
    _wheel(wide_row, delta)
    assert view.span < 12, (
        f"шаг тачпада {delta}/120 не приблизил 12 мест: округление съело шаг"
    )
    narrowed = view.span
    _wheel(wide_row, -delta)
    assert view.span > narrowed, (
        f"шаг тачпада {delta}/120 не отдалил {narrowed} мест: округление съело шаг"
    )


# ===================================================== п.4 развернуть график


@pytest.fixture()
def split(qapp):
    """Панель графика в настоящем разделителе над соседом — как в главном окне."""
    splitter = QSplitter(Qt.Orientation.Vertical)
    made = ChartPanel(surface=PainterChartSurface())
    splitter.addWidget(made)
    splitter.addWidget(QWidget())
    splitter.resize(1000, 900)
    splitter.show()
    qapp.processEvents()
    splitter.setSizes([520, 360])
    qapp.processEvents()
    made.show_chart(_data(_row(300)))
    yield splitter, made
    made.digest.hide()
    splitter.deleteLater()
    settle_qt(qapp)


def test_the_wide_button_folds_the_neighbour_and_brings_it_back(split, qapp) -> None:
    splitter, made = split
    before = splitter.sizes()
    assert not made.wide_button.isHidden(), "в разделителе кнопки разворота нет"

    QTest.mouseClick(made.wide_button, LEFT)
    qapp.processEvents()
    assert splitter.sizes() == [sum(before), 0], (
        f"после «Развернуть график» размеры {splitter.sizes()}, были {before}: "
        "журналы не схлопнулись"
    )
    QTest.mouseClick(made.wide_button, LEFT)
    qapp.processEvents()
    assert splitter.sizes() == before, (
        f"повторное нажатие вернуло {splitter.sizes()} вместо {before}"
    )


def _double_click(surface: PainterChartSurface, button, x: float, y: float) -> None:
    """Двойной щелчок так, как его присылает Qt от настоящей мыши.

    Нажатие, отпускание, событие двойного щелчка, отпускание. `QTest.mouseDClick`
    здесь не годится: в этой версии он шлёт одно событие двойного щелчка
    без нажатий вокруг, а у настоящей мыши их два.
    """
    none = Qt.MouseButton.NoButton
    _mouse(surface, QEvent.Type.MouseButtonPress, (x, y), button, button)
    _mouse(surface, QEvent.Type.MouseButtonRelease, (x, y), button, none)
    _mouse(surface, QEvent.Type.MouseButtonDblClick, (x, y), button, button)
    _mouse(surface, QEvent.Type.MouseButtonRelease, (x, y), button, none)


def test_a_double_click_does_the_same_and_the_right_one_still_selects(split, qapp) -> None:
    splitter, made = split
    before = splitter.sizes()
    surface = _surface(made)
    x, y = surface.width() / 3, surface.height() / 2

    _double_click(surface, LEFT, x, y)
    qapp.processEvents()
    assert splitter.sizes()[1] == 0, "двойной щелчок левой график не развернул"
    _double_click(surface, LEFT, x, y)
    qapp.processEvents()
    assert splitter.sizes() == before, "второй двойной щелчок не вернул журналы"

    # Разбор — всплывающее окно, и второе нажатие двойного щелчка закрыло бы
    # его, как закрывает любой щелчок мимо. Выделение слушаем напрямую.
    heard: list[ChartSpan | None] = []
    surface.set_span_handler(heard.append)
    _double_click(surface, RIGHT, x, y)
    qapp.processEvents()
    assert splitter.sizes() == before, "двойной щелчок правой развернул график"
    assert len(heard) == 2 and None not in heard, (
        f"двойной щелчок правой — два выделения, как и было; пришло {heard!r}"
    )


def test_outside_a_splitter_there_is_no_wide_button(panel) -> None:
    assert panel.wide_button.isHidden(), (
        "панель не в разделителе, а кнопка «Развернуть график» видна: "
        "нажатие ничего бы не сделало"
    )


# ============================================== свёртка длинных разделов разбора


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 6, day, hour, minute, tzinfo=MSK)


def _holes(count: int) -> tuple[tuple[datetime, datetime], ...]:
    """Пропуски по пять минут, по одному в день начиная с 01.06."""
    day = timedelta(days=1)
    return tuple((_at(1, 14) + n * day, _at(1, 14, 5) + n * day) for n in range(count))


@pytest.mark.parametrize(
    ("count", "total"), [(5, "25 мин"), (85, "7 ч 5 мин")], ids=["five", "eighty_five"]
)
def test_many_holes_fold_into_one_line_with_a_count(count: int, total: str) -> None:
    holes = _holes(count)
    span = ChartSpan(start=_at(1, 9), end=holes[-1][1] + timedelta(hours=1), holes=holes)
    lines = digest_lines(span, ())
    folded = [line for line in lines if "пропуск" in line.lower() and "данных" in line]
    assert len(folded) == 1, f"пропусков {count}, строк про них {len(folded)}: {folded!r}"
    assert f"Пропусков данных внутри участка: {count}, всего {total}." in folded[0]


def test_three_holes_stay_listed_one_by_one() -> None:
    span = ChartSpan(start=_at(1, 9), end=_at(4, 23), holes=_holes(3))
    lines = [line for line in digest_lines(span, ()) if "В участок попал пропуск" in line]
    assert len(lines) == 3, f"три пропуска свернулись или потерялись: {lines!r}"


def test_many_shaded_stretches_fold_into_one_line_with_a_count() -> None:
    shades = tuple(
        Shade(start=_at(19 + n, 12), end=_at(19 + n, 13), kind=ShadeKind.OUTSIDE_WINDOW)
        for n in range(6)
    )
    span = ChartSpan(start=_at(19, 9), end=_at(25, 23))
    lines = [line for line in digest_lines(span, (), shades=shades) if "Затен" in line]
    assert lines == [
        "Затенённых отрезков внутри участка: 6, всего 6 ч 0 мин — там робот "
        "сделок не совершает"
    ], f"шесть затенённых отрезков дали: {lines!r}"
