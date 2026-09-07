"""Легенда графика, верхняя полоса и разбор выделенного участка.

Просьба владельца счёта 05.09.2026 со снимком экрана, дословно: «это сейчас
максимально запутано и непонятно мне, или легенду расширь что ли». И через
несколько часов, про метки: «точки, где сделки, с графика не убирай — оставь,
но сделай понятней. а при выделении более подробная информация».

Что стережёт каждая проверка, по фразе:

* **каждый** вид метки объяснён словом — проверка на полноту таблицы,
  а не на текст: пять случаев `DOMAIN.md` §8 и есть виды меток;
* цвет, названный в легенде, существует в теме — иначе значок пустой;
* значок легенды — **та же фигура**, что метка на графике, и рисует её
  отрисовщик, а не вторая копия форм;
* строки слоя «Прогноз» появляются и исчезают вместе со слоем;
* легенда сворачивается, и кнопка говорит, куда;
* **верхняя полоса читается в обеих темах** (`D-016`) — контраст меряется
  по нарисованной картинке;
* разбор участка берёт сделки **того** куска, что выделили;
* разбор различает «сделок не передавали», «здесь сделок нет» и «данных нет»;
* у каждой сделки названы причина выхода, цены и результат;
* сравнение расчёта с фактом показывается готовым, а не считается заново;
* слой, снятый галочкой, в разбор не попадает;
* правый щелчок по графику открывает разбор той сделки, что под ним;
* **итог участка стоит выше списка сделок** и складывает те сделки, что
  в участке, — просьба владельца счёта 05.09.2026: «тут не хватает итоговой
  цифры — профит или проёб». Что стережёт каждая из проверок итога, написано
  списком у их раздела.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import datetime, timedelta

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QKeyEvent, QMouseEvent, QPalette, QRegion
from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QWidget

from ui.chart.glyphs import GlyphStyle
from ui.chart.painter_surface import AXIS_HEIGHT, AXIS_WIDTH, PADDING, PainterChartSurface
from ui.chart_panel import (
    SPAN_FIELDS,
    ChartPanel,
    SpanFacts,
    TradeDigest,
    _fit_on_screen,
    digest_lines,
    span_totals,
    swatch,
    trades_in_span,
)
from ui.formatting import MSK, SUMMARY_FIELDS
from ui.legend import LEGEND, AreaSample, LineSample, MarkerSample
from ui.legend import visible as legend_items
from ui.models import (
    Candle,
    ChartData,
    ChartSpan,
    Layer,
    Marker,
    MarkerKind,
    Shade,
    ShadeKind,
    Side,
    TradeRow,
)
from ui.theme import DARK, LIGHT, Theme, contrast
from ui.theme import current as current_theme

#: Порог читаемости обычного текста по WCAG 2.1. Тот же, что в `test_ui_panel`.
READABLE = 4.5

DAY = datetime(2026, 6, 19, tzinfo=MSK)
STEP = timedelta(minutes=5)
WIDTH, HEIGHT = 1000, 420


def _at(hour: int, minute: int) -> datetime:
    return DAY.replace(hour=hour, minute=minute)


def _candle(moment: datetime, close: float) -> Candle:
    return Candle(opens_at=moment, open=close - 50, high=close + 100, low=close - 150, close=close)


SOLID = tuple(_candle(_at(13, 5 * n), 226_000.0 + 100 * n) for n in range(12))
SOLID_PLACES = 12


def _trade(
    number: int,
    place: int,
    span: int = 2,
    *,
    exit_reason: str = "Тейк-профит +0,5%",
    profit_rub: float | None = 1425.0,
    closed: bool = True,
) -> TradeRow:
    """Сделка на местах `place`…`place + span` ряда `SOLID`.

    `closed=False` — позиция ещё открыта: ни времени выхода, ни цены выхода.
    """
    return TradeRow(
        entry_time=SOLID[place].opens_at,
        exit_time=SOLID[place + span].opens_at if closed else None,
        side=Side.LONG,
        volume=1,
        entry_price=SOLID[place].close,
        exit_price=SOLID[place + span].close if closed else None,
        exit_reason=exit_reason,
        profit_rub=profit_rub,
        profit_pct=0.5,
        commission_rub=28.0,
        trade_id=f"order-{number}",
    )


#: Три сделки в разных местах ряда: ранняя, средняя и поздняя. Разные
#: причины выхода — по ним и видно, чью строку показали.
EARLY = _trade(1, 0, exit_reason="Обратный сигнал средней — переворот")
MIDDLE = _trade(2, 4, exit_reason="Тейк-профит +0,5%")
LATE = _trade(3, 8, exit_reason="Конец торгового окна — закрытие в деньги")
TRADES = (EARLY, MIDDLE, LATE)


#: Три сделки с **разными** деньгами, в разных местах ряда.
#:
#: У `TRADES` результат у всех один и тот же, и итог по одной сделке отличается
#: там от итога по трём только множителем: подмена «сложить все вместо
#: выделенных» на таких числах видна плохо. Здесь она видна сразу.
#:
#: Числа — чистые, как их отдаёт прогон (`TradeRow.profit_rub` — результат
#: **после** комиссии, `app/convert.py`). Валовыми это +1 200, −640 и +400.
MONEY = (
    _trade(11, 0, profit_rub=1172.0),
    _trade(12, 4, profit_rub=-668.0),
    _trade(13, 8, profit_rub=372.0),
)


def _span(first: int, last: int) -> ChartSpan:
    """Отрезок по местам ряда — так же, как его строит отрисовщик."""
    return ChartSpan(start=SOLID[first].opens_at, end=SOLID[last].opens_at + STEP)


# ---------------------------------------------------------------- легенда


def test_every_marker_kind_is_explained_in_the_legend() -> None:
    """У каждого вида метки есть своя строка легенды.

    Проверка на полноту таблицы, а не на текст: пять случаев «прогноз против
    факта» (`DOMAIN.md` §8) выражены видами меток, и вид, забытый в таблице,
    не объяснён нигде — заметить это на экране некому.
    """
    explained = {
        item.sample.kind for item in LEGEND if isinstance(item.sample, MarkerSample)
    }
    forgotten = sorted(kind.name for kind in MarkerKind if kind not in explained)

    assert not forgotten, (
        "виды меток, которые могут появиться на графике и не объяснены "
        f"в легенде: {forgotten}"
    )


def test_every_colour_named_in_the_legend_is_a_real_colour_of_the_theme() -> None:
    """Цвет строки легенды берётся у темы по имени — имя обязано существовать.

    Опечатка в имени поля дала бы значок цвета `#000000` в обеих темах,
    и молча: `getattr` упал бы только в момент отрисовки, у владельца счёта.
    """
    wrong = [
        item.sample.colour
        for item in LEGEND
        if isinstance(item.sample, LineSample | AreaSample)
        and not hasattr(LIGHT, item.sample.colour)
    ]
    assert not wrong, f"в легенде названы цвета, которых в теме нет: {wrong}"


def _glyph(kind: MarkerKind, layer: Layer, theme: Theme, host: QWidget) -> QImage:
    """Значок легенды картинкой — тот самый, что попадает на экран."""
    item = next(
        one
        for one in LEGEND
        if isinstance(one.sample, MarkerSample)
        and (one.sample.kind, one.sample.layer) == (kind, layer)
    )
    surface = PainterChartSurface()
    try:
        return swatch(item, theme, surface, host).toImage()
    finally:
        surface.deleteLater()


def _weight(image: QImage, theme: Theme, half: str) -> int:
    """Сколько точек значка **не** цвета фона в верхней или нижней половине."""
    background = QColor(theme.background)
    rows = (
        range(image.height() // 2)
        if half == "top"
        else range(image.height() // 2, image.height())
    )
    found = 0
    for y in rows:
        for x in range(image.width()):
            pixel = QColor(image.pixel(x, y))
            if max(
                abs(pixel.red() - background.red()),
                abs(pixel.green() - background.green()),
                abs(pixel.blue() - background.blue()),
            ) > 24:
                found += 1
    return found


def test_the_legend_glyph_is_the_shape_it_explains(qapp) -> None:
    """Значок «вход в лонг» — треугольник вершиной вверх, «в шорт» — вниз.

    Проверка по нарисованному значку, а не по имени вида метки: словарь
    остался бы верным и при перепутанных фигурах, а человек различает их
    только глазами. Треугольник вершиной вверх тяжелее снизу, вершиной
    вниз — сверху.
    """
    host = QWidget()
    try:
        long_glyph = _glyph(MarkerKind.ENTRY_LONG, Layer.FACT, LIGHT, host)
        short_glyph = _glyph(MarkerKind.ENTRY_SHORT, Layer.FACT, LIGHT, host)

        long_top = _weight(long_glyph, LIGHT, "top")
        long_bottom = _weight(long_glyph, LIGHT, "bottom")
        short_top = _weight(short_glyph, LIGHT, "top")
        short_bottom = _weight(short_glyph, LIGHT, "bottom")

        assert long_bottom > long_top * 1.4, (
            f"значок лонга: сверху {long_top} точек, снизу {long_bottom} — "
            "это не треугольник вершиной вверх"
        )
        assert short_top > short_bottom * 1.4, (
            f"значок шорта: сверху {short_top} точек, снизу {short_bottom} — "
            "это не треугольник вершиной вниз"
        )
    finally:
        host.deleteLater()
        qapp.processEvents()


def test_the_forecast_glyph_is_hollow_and_the_fact_glyph_is_filled(qapp) -> None:
    """Прогноз — полая фигура, факт — залитая: слои различимы и без цвета.

    На печати отчёта и при нарушении цветового зрения оттенок не различается
    вовсе, и «расчёт» слился бы с «фактом».
    """
    host = QWidget()
    try:
        plan = _glyph(MarkerKind.ENTRY_LONG, Layer.PLAN, LIGHT, host)
        fact = _glyph(MarkerKind.ENTRY_LONG, Layer.FACT, LIGHT, host)
        middle = QColor(plan.pixel(plan.width() // 2, plan.height() // 2))
        background = QColor(LIGHT.background)

        assert middle.name() == background.name(), (
            f"середина значка прогноза закрашена ({middle.name()}): "
            "фигура не полая, от факта её не отличить"
        )
        filled = QColor(fact.pixel(fact.width() // 2, fact.height() // 2))
        assert filled.name() != background.name(), (
            "середина значка факта пустая: фигура не залита"
        )
    finally:
        host.deleteLater()
        qapp.processEvents()


class WatchfulSurface(PainterChartSurface):
    """Отрисовщик, который считает, сколько раз у него просили значок метки."""

    def __init__(self) -> None:
        super().__init__()
        self.asked: list[MarkerKind] = []

    def draw_marker_glyph(self, painter, point, kind, layer, style: GlyphStyle) -> None:
        self.asked.append(kind)
        super().draw_marker_glyph(painter, point, kind, layer, style)


def test_the_legend_asks_the_surface_for_its_glyphs(qapp) -> None:
    """Значок легенды рисует тот, кто рисует график, а не вторая копия форм.

    Пока форм было две — одна на графике, другая в легенде, — правка одной
    оставляла вторую старой, и рядом на экране оказывались метка и её
    объяснение, изображающие разное.
    """
    surface = WatchfulSurface()
    panel = ChartPanel(surface=surface)
    try:
        assert surface.asked, (
            "легенда собрана, а у отрисовщика ни разу не спросили форму метки: "
            "значки нарисованы своей копией форм"
        )
        marker_rows = [
            item for item in legend_items(plan=True) if isinstance(item.sample, MarkerSample)
        ]
        assert len(surface.asked) >= len(marker_rows), (
            f"формы спрошены {len(surface.asked)} раз при {len(marker_rows)} "
            "строках с метками"
        )
    finally:
        panel.deleteLater()
        qapp.processEvents()


def test_the_forecast_rows_come_and_go_with_the_layer(qapp) -> None:
    """Снял галочку «Прогноз» — объяснения прогноза из легенды ушли.

    Легенда объясняет то, что **может появиться** на экране. Со снятой галочкой
    полая метка появиться не может, и место она занимать не должна.
    """
    panel = ChartPanel(surface=PainterChartSurface())
    try:
        with_plan = len(panel.legend.rows)
        panel.plan_box.setChecked(False)
        without_plan = len(panel.legend.rows)

        assert without_plan < with_plan, (
            f"со снятой галочкой в легенде осталось {without_plan} строк из "
            f"{with_plan}: она объясняет то, чего на экране нет"
        )
        assert without_plan == len(legend_items(plan=False)), (
            "число строк легенды разошлось с таблицей"
        )
        panel.plan_box.setChecked(True)
        assert len(panel.legend.rows) == with_plan, (
            "галочку вернули, а объяснения прогноза не вернулись"
        )
    finally:
        panel.deleteLater()
        qapp.processEvents()


def test_the_legend_folds_away_and_the_button_says_where(qapp) -> None:
    """Легенда сворачивается кнопкой и возвращает место графику.

    Раскрыта она по умолчанию намеренно: владелец счёта сейчас график
    прочитать не может, а легенда за кнопкой — это легенда, которую он
    не найдёт.
    """
    panel = ChartPanel(surface=PainterChartSurface())
    panel.resize(1000, 600)
    panel.show()
    qapp.processEvents()
    try:
        assert panel.legend.isVisible(), "легенда спрятана по умолчанию"
        was = panel.legend_button.text()
        panel.legend_button.setChecked(False)
        qapp.processEvents()

        assert not panel.legend.isVisible(), "кнопка нажата, а легенда осталась"
        assert panel.legend_button.text() != was, (
            f"кнопка после сворачивания говорит то же самое: {was!r}"
        )
    finally:
        panel.deleteLater()
        qapp.processEvents()


# ------------------------------------------------- верхняя полоса, `D-016`


def _painted(widget: QWidget, qapp) -> QImage:
    """Снимок виджета на **системном** фоне — том, что под ним на экране.

    Заливка системным цветом до отрисовки и есть суть `D-016`: виджет, который
    не красит свой фон, оставляет на экране именно её.
    """
    widget.resize(max(widget.sizeHint().width(), 900), max(widget.sizeHint().height(), 30))
    qapp.processEvents()
    image = QImage(widget.width(), widget.height(), QImage.Format.Format_RGB32)
    image.fill(qapp.palette().color(QPalette.ColorRole.Window))
    widget.render(image, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
    return image


def _captions(strip: QWidget) -> list[QLabel | QCheckBox]:
    """Все надписи полосы: подписи и галочки."""
    layout = strip.layout()
    assert layout is not None, "у полосы нет разметки вовсе"
    found: list[QLabel | QCheckBox] = []
    for index in range(layout.count()):
        cell = layout.itemAt(index)
        widget = cell.widget() if cell is not None else None
        if isinstance(widget, QLabel | QCheckBox):
            found.append(widget)
    return found


def _colour(widget: QLabel | QCheckBox) -> str:
    style: str = widget.styleSheet()
    assert style.startswith("color:"), f"у надписи не задан цвет вовсе: {style!r}"
    return style.removeprefix("color:").removesuffix(";").strip()


@pytest.mark.parametrize("theme", [LIGHT, DARK], ids=["светлая", "тёмная"])
def test_the_top_strip_is_readable_in_both_themes(qapp, theme) -> None:
    """`D-016`: полоса с галочками красит свой фон и её надписи читаются.

    На снимке `.docs/ui/hover-dark.png` полоса осталась светлым рубцом поперёк
    тёмного окна: виджет без собственного фона показывается на системном.
    Контраст меряется по **нарисованной** картинке — по числам самой темы
    этого не видно ни на волос.
    """
    panel = ChartPanel(surface=PainterChartSurface())
    try:
        panel.set_theme(theme)
        image = _painted(panel.strip, qapp)
        background = QColor(image.pixel(1, 1)).name()

        assert background == theme.background, (
            f"полоса нарисована на фоне {background}, а цвета подобраны "
            f"к {theme.background}: подложка осталась системной"
        )
        captions = _captions(panel.strip)
        assert len(captions) == 3, (
            f"в полосе {len(captions)} надписей вместо трёх: список "
            "проверяемого перестал совпадать с полосой"
        )
        for caption in captions:
            measured = contrast(_colour(caption), background)
            assert measured >= READABLE, (
                f"надпись {caption.text()!r} в теме "
                f"«{'тёмная' if theme.dark else 'светлая'}»: контраст "
                f"{measured:.2f} при пороге {READABLE} "
                f"({_colour(caption)} на {background})"
            )
    finally:
        panel.deleteLater()
        qapp.processEvents()


def test_the_top_strip_paints_its_background_from_the_very_start(qapp) -> None:
    """Полоса красит фон сразу при сборке, а не только при смене темы.

    Отдельная проверка потому, что `set_theme` красит полосу тоже, и сторож,
    который сначала переключает тему, зелен даже с полосой, забытой в сборке.
    Смена темы на ходу — редкий случай; обычный — открыть программу и увидеть.
    """
    system = qapp.palette().color(QPalette.ColorRole.Window).name()
    theme = current_theme()
    if system == theme.background:
        pytest.skip(
            "системный фон совпал с фоном темы: отличить крашеную полосу "
            "от некрашеной на этой машине нечем"
        )
    panel = ChartPanel(surface=PainterChartSurface())
    try:
        background = QColor(_painted(panel.strip, qapp).pixel(1, 1)).name()
        assert background == theme.background, (
            f"свежесобранная полоса нарисована на фоне {background} вместо "
            f"{theme.background}: свой фон она красит только при смене темы"
        )
    finally:
        panel.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("theme", [LIGHT, DARK], ids=["светлая", "тёмная"])
def test_the_legend_is_drawn_on_the_background_of_the_theme(qapp, theme) -> None:
    """Легенда — продолжение графика, и фон у неё тот же (`D-016`).

    Значки легенды рисуются на фоне темы; окажись под ними системный светлый
    фон, полупрозрачные заливки выглядели бы иначе, чем на графике.
    """
    panel = ChartPanel(surface=PainterChartSurface())
    try:
        panel.set_theme(theme)
        image = _painted(panel.legend, qapp)
        background = QColor(image.pixel(1, 1)).name()

        assert background == theme.background, (
            f"легенда нарисована на фоне {background} вместо {theme.background}"
        )
    finally:
        panel.deleteLater()
        qapp.processEvents()


# ------------------------------------------------------------ разбор участка


def test_the_digest_takes_the_trades_of_the_stretch_and_not_the_neighbours() -> None:
    """В разбор попадают сделки выделенного куска, а не соседнего.

    Отбор по времени: человек выделяет кусок графика, а не строки таблицы.
    Причины выхода у трёх сделок разные — по ним и видно, чью строку показали.
    """
    picked = trades_in_span(_span(4, 6), TRADES)

    assert [row.trade_id for row in picked] == ["order-2"], (
        f"в участок 13:20–13:35 попали сделки {[r.trade_id for r in picked]}, "
        "а там только вторая"
    )


def test_a_trade_that_started_before_the_stretch_still_belongs_to_it() -> None:
    """Сделка, начавшаяся раньше и закрывшаяся внутри куска, — тоже его сделка.

    Это ровно та сделка, про которую человек и спрашивает, когда выделяет
    место закрытия позиции.
    """
    picked = trades_in_span(_span(5, 7), TRADES)

    assert [row.trade_id for row in picked] == ["order-2"], (
        "сделка, вошедшая до участка и вышедшая внутри него, из разбора выпала"
    )


def test_the_digest_says_out_loud_that_no_trades_were_handed_over_at_all() -> None:
    """Сделок не передавали вовсе — так и сказано, а не «сделок нет».

    Разрешение владельца счёта 05.09.2026: «если данные не готовы — заглушки
    и пометку об этом». Молчаливый пропуск читался бы как «робот ничего
    не делал», хотя на деле прогона ещё не было.
    """
    text = "\n".join(digest_lines(_span(0, 5), ()))

    assert "не передавали" in text, (
        f"при пустом списке сделок разбор сказал: {text!r}"
    )


def test_the_digest_tells_an_empty_stretch_from_a_hole_in_the_data() -> None:
    """«Здесь сделок нет» и «данных за это время нет» — разные строки.

    Второе, сказанное вместо первого, и есть содержание `B-005`: график
    сообщает о своих данных не то, что в них есть.
    """
    hole = ChartSpan(
        start=_at(13, 45),
        end=_at(14, 15),
        holes=((_at(13, 45), _at(14, 15)),),
    )
    text = "\n".join(digest_lines(hole, TRADES))

    assert "пропуск данных" in text, "разбор промолчал о том, что данных нет"
    assert "13:45" in text and "14:15" in text, (
        f"пропуск назван без времени: {text!r}"
    )


def test_the_digest_names_the_exit_reason_price_and_result_of_every_trade() -> None:
    """У каждой сделки названы причина выхода, обе цены и результат.

    Просьба владельца счёта: «как, что, на какой цене и почему сделка
    сработала». Числа взяты из `TradeRow` как есть — ни одно здесь
    не выводится.
    """
    text = "\n".join(digest_lines(_span(0, 11), TRADES))

    for row in TRADES:
        assert row.exit_reason in text, f"причина выхода {row.exit_reason!r} не названа"
    assert "1\u00a0425,00\u00a0₽" in text, f"результата сделки в разборе нет: {text!r}"
    assert "комиссия 28,00\u00a0₽" in text, "комиссия в разборе не названа"
    assert "держали 10 мин" in text, "сколько держали позицию — не сказано"


def test_a_trade_without_a_result_says_so_instead_of_showing_zero() -> None:
    """Результата нет — прочерк и объяснение, а не ноль.

    Ноль здесь означал бы «сделка вышла в ноль», и это утверждение, которого
    у нас нет.
    """
    text = "\n".join(digest_lines(_span(0, 5), (_trade(9, 0, profit_rub=None),)))

    assert "не передал" in text, f"пустой результат показан как есть: {text!r}"
    assert "0,00\u00a0₽" not in text, "вместо отсутствующего результата показан ноль"


def test_an_open_position_is_named_open_and_not_closed() -> None:
    """Позиция ещё открыта — так и написано, выхода у неё нет."""
    text = "\n".join(digest_lines(_span(0, 5), (_trade(9, 0, closed=False),)))

    assert "ещё открыта" in text, f"незакрытая сделка показана закрытой: {text!r}"


def test_the_digest_shows_the_ready_made_comparison_of_plan_and_fact() -> None:
    """Сравнение расчёта с фактом показывается готовым, а не считается заново.

    Расхождение в пунктах и рублях считает тот, кто знает шаг цены и стоимость
    пункта (`app/convert.py`); окно посчитало бы «примерно», и «примерно»
    разошлось бы с журналом.
    """
    note = "Факт: вход в лонг по 285 400\nРасчёт: 285 380\nРасхождение: 20 п. (20 ₽)"
    marker = Marker(
        time=SOLID[4].opens_at,
        price=SOLID[4].close,
        kind=MarkerKind.ENTRY_LONG,
        layer=Layer.FACT,
        tooltip=note,
    )
    text = "\n".join(digest_lines(_span(3, 6), TRADES, (marker,)))

    assert "Расхождение: 20 п. (20 ₽)" in text, (
        f"готовое сравнение расчёта с фактом в разбор не попало: {text!r}"
    )
    assert MarkerKind.ENTRY_LONG.label in text, "вид метки в разборе не назван"


def test_the_digest_says_why_the_robot_was_quiet_on_a_shaded_stretch() -> None:
    """На затенённом куске сказано словами, почему там ничего не происходило.

    Причина уже нарисована затенением; здесь она названа — это первый вопрос,
    который возникает у пустого участка.
    """
    shade = Shade(
        start=_at(13, 0), end=_at(13, 30), kind=ShadeKind.OUTSIDE_WINDOW, note="Вне окна"
    )
    text = "\n".join(digest_lines(_span(1, 3), (), (), (shade,)))

    assert "сделок не совершает" in text and "Вне окна" in text, (
        f"на затенённом участке разбор не сказал, почему там тихо: {text!r}"
    )


# --------------------------------------------------------- итог участка
#
# Что стережёт каждая проверка ниже, по фразе:
#
# * итог стоит выше списка сделок — ради него разбор и открывают;
# * складываются сделки выделенного куска, а не все переданные;
# * знак денег не теряется: минус остаётся минусом, плюс — плюсом;
# * пустой участок объясняется словами, а не нулём рублей;
# * слова и порядок итога совпадают со строкой под журналом сделок;
# * сложение сходится с тем, что посчитал прогон;
# * чистая прибыль в окне выделена цветом по знаку;
# * пропуск данных внутри участка назван оговоркой к итогу;
# * без тарифа комиссии чистой прибыли нет, и сказано почему.


def test_the_total_of_the_stretch_stands_above_the_list_of_trades() -> None:
    """Итог — до списка сделок, а не под ним.

    Владелец счёта открывает разбор ради ответа «сколько», а не ради чтения
    восьми абзацев (05.09.2026). Итог, стоящий после списка, отвечает
    на вопрос последним.
    """
    lines = digest_lines(_span(0, 11), MONEY)
    total = next(i for i, line in enumerate(lines) if line.startswith("Итог участка"))
    first_trade = next(i for i, line in enumerate(lines) if line.startswith("1. "))

    assert total < first_trade, (
        f"итог стоит строкой {total}, первая сделка — {first_trade}: "
        "человек дочитывает до итога через весь список"
    )


def test_the_total_adds_up_the_trades_of_the_stretch_and_not_all_of_them() -> None:
    """Складываются сделки выделенного куска, а не весь переданный журнал.

    Ошибка здесь не падает и не видна: на экране просто стоит итог за день
    там, где человек выделил полчаса.
    """
    text = "\n".join(digest_lines(_span(0, 2), MONEY))

    assert "Чистая прибыль: +1\u00a0172,00\u00a0₽" in text, (
        f"итог одной выделенной сделки посчитан не по ней: {text!r}"
    )
    assert "876,00" not in text, (
        "в итог участка попали сделки за его пределами: показана сумма всех трёх"
    )


def test_the_total_keeps_the_sign_of_the_money() -> None:
    """Минус остаётся минусом, плюс — плюсом.

    Итог по модулю показывает убыточный участок прибыльным, и это ровно то
    число, ради которого окно открывают.
    """
    losing = "\n".join(digest_lines(_span(4, 6), MONEY))
    winning = "\n".join(digest_lines(_span(0, 2), MONEY))

    assert "Чистая прибыль: −668,00\u00a0₽" in losing, (
        f"убыточный участок показан без минуса: {losing!r}"
    )
    assert "+668" not in losing, "убыток показан как прибыль"
    assert "Чистая прибыль: +1\u00a0172,00\u00a0₽" in winning, (
        f"у прибыльного участка пропал знак плюса: {winning!r}"
    )


def test_an_empty_stretch_is_explained_in_words_and_not_by_a_zero() -> None:
    """В участке сделок нет — так и сказано, а не «0,00 ₽».

    Ноль рублей — утверждение «участок вышел в ноль». Такого утверждения
    у нас здесь нет, есть «складывать нечего».
    """
    # Место 3 — 13:15…13:20: первая сделка вышла в 13:10, вторая входит в 13:20.
    text = "\n".join(digest_lines(_span(3, 3), MONEY))

    assert "Итог участка: в этом участке сделок нет" in text, (
        f"пустой участок остался без объяснения: {text!r}"
    )
    assert "0,00\u00a0₽" not in text, "пустой участок показан нулём рублей"


def test_a_stretch_whose_results_were_never_handed_over_says_so() -> None:
    """Сделки есть, результата по ним нет — сказано словами, а не нулём.

    Третий вид пустоты, отличный от двух других: прогон был, сделки пришли,
    а денег в них не передали.
    """
    text = "\n".join(digest_lines(_span(0, 5), (_trade(9, 0, profit_rub=None),)))

    assert "результата по ним прогон не передал" in text, (
        f"сделка без результата попала в итог молча: {text!r}"
    )
    assert "0,00\u00a0₽" not in text, "отсутствующий результат показан нулём"


def _journal_line(qapp, summary) -> str:
    """Итоговая строка под журналом сделок — та самая, что видит человек."""
    from ui.journals import JournalTabs

    tabs = JournalTabs()
    try:
        tabs.set_summary(summary)
        return tabs.summary_label.text()
    finally:
        tabs.deleteLater()
        qapp.processEvents()


def test_the_total_of_the_stretch_repeats_the_words_of_the_journal_summary(qapp) -> None:
    """Слова, порядок и оформление чисел — те же, что под журналом сделок.

    Сверяются **получившиеся строки**, а не один и тот же текст, написанный
    в двух местах проверки. Итог участка обязан быть началом итога за период
    буква в букву: подпись, поправленная в одном из двух мест, разводит два
    набора слов про одни и те же деньги, и это новая путаница вместо снятой.

    Числа для обеих строк берутся из одного прогона: `backtest.summarise`
    считает итог за период, те же сделки складываются как участок.
    """
    from app.convert import trade_row, trades_summary
    from backtest import HistoryRun, summarise

    deals = _deals()
    rows = [trade_row(deal) for deal in deals]
    span = ChartSpan(start=deals[0].entry_time, end=deals[-1].exit_time + STEP)

    stretch = next(
        line
        for line in digest_lines(span, rows)
        if line.startswith("Итог участка")
    )
    counted = summarise(deals)
    period = _journal_line(
        qapp, trades_summary(HistoryRun(deals=tuple(deals), summary=counted))
    )

    body = stretch.removeprefix("Итог участка — ")
    assert period.startswith("Итог за период — " + body), (
        "итог участка и итог за период говорят разными словами про одни "
        f"и те же деньги:\n  участок: {body!r}\n  период:  {period!r}"
    )
    assert len(SPAN_FIELDS) == 5, (
        f"в итоге участка {len(SPAN_FIELDS)} чисел вместо пяти — проверка "
        "сверяет уже не то, что просил владелец счёта"
    )
    assert [field.key for field in SPAN_FIELDS] == [
        field.key for field in SUMMARY_FIELDS[: len(SPAN_FIELDS)]
    ], "порядок чисел в итоге участка разошёлся с порядком под журналом"


def _deals():
    """Три сделки прогона: два плюса и минус, тариф комиссии задан."""
    from backtest import Deal
    from engine import ExitReason
    from engine import Side as EngineSide

    def one(number: int, place: int, side, entry: float, exit_: float) -> Deal:
        return Deal(
            side=side,
            volume=1,
            entry_time=SOLID[place].opens_at,
            entry_price=entry,
            entry_order_id=f"in-{number}",
            exit_time=SOLID[place + 2].opens_at,
            exit_price=exit_,
            exit_order_id=f"out-{number}",
            exit_reason=ExitReason.SIGNAL,
            commission=28.0,
            ruble_per_point=1.0,
        )

    return [
        one(1, 0, EngineSide.LONG, 226_000, 227_200),
        one(2, 4, EngineSide.SHORT, 227_200, 227_840),
        one(3, 8, EngineSide.LONG, 227_840, 228_240),
    ]


def test_the_total_of_the_stretch_agrees_with_what_the_run_counted() -> None:
    """Сложение в окне сходится с `backtest.summarise` на тех же сделках.

    Возражение, из-за которого итога участка сначала не было: вторая сумма
    на экране, которую никто не сверял с движком. Здесь она сверена — все
    четыре числа, включая валовую, которая получается обратным ходом
    равенства «чистая = валовая − комиссия».
    """
    from app.convert import trade_row
    from backtest import summarise

    deals = _deals()
    counted = summarise(deals)
    totals = span_totals([trade_row(deal) for deal in deals])

    assert totals.trades == counted.trades
    assert totals.net_profit_rub == counted.net_profit, (
        f"чистая в окне {totals.net_profit_rub}, у прогона {counted.net_profit}"
    )
    assert totals.gross_profit_rub == counted.gross_profit, (
        f"валовая в окне {totals.gross_profit_rub}, у прогона {counted.gross_profit}"
    )
    assert totals.commission_rub == counted.commission, (
        f"комиссия в окне {totals.commission_rub}, у прогона {counted.commission}"
    )
    assert totals.profitable_share == counted.profitable_share, (
        f"доля прибыльных в окне {totals.profitable_share}, "
        f"у прогона {counted.profitable_share}"
    )


def test_a_mixed_column_gives_no_share_of_profitable_trades() -> None:
    """Доля прибыльных не считается по смеси чистых и валовых результатов.

    Сделка с валовым плюсом 10 ₽ попадала в «прибыльные», хотя комиссия обеих
    сторон на контракт — 28 ₽, то есть на счёте это убыток. Слово «прибыльная»
    в двух половинах столбика означает разное, и доля по такой смеси —
    число ни о чём.
    """
    from app.convert import trade_row

    deals = _deals()
    rows = [trade_row(deal) for deal in deals]
    mixed = [rows[0], dataclasses.replace(rows[1], commission_rub=None)]

    totals = span_totals(mixed)
    assert totals.counted == 2, "проверка вакуумна: строки не дошли до расчёта"
    assert totals.net_profit_rub is None
    assert totals.gross_profit_rub is None
    assert totals.profitable_share is None, (
        "доля прибыльных посчитана по столбику, в котором чистые лежат "
        "вперемешку с валовыми"
    )


def test_a_uniform_column_still_gives_the_share() -> None:
    """Доля остаётся там, где база одна: смесь запрещена, однородность — нет."""
    from app.convert import trade_row

    rows = [trade_row(deal) for deal in _deals()]
    assert span_totals(rows).profitable_share is not None
    gross_only = [dataclasses.replace(row, commission_rub=None) for row in rows]
    assert span_totals(gross_only).profitable_share is not None


@pytest.mark.parametrize(
    ("first", "last", "expected"),
    [(0, 2, "success"), (4, 6, "danger")],
    ids=["плюс", "минус"],
)
def test_the_net_result_is_set_apart_in_the_popup(qapp, first, last, expected) -> None:
    """Чистая прибыль выделена цветом по знаку — не теряется среди пяти чисел.

    Цвет здесь второй носитель смысла: знак остаётся буквой внутри числа,
    иначе минус исчезает на чёрно-белой печати и для того, кто плохо
    различает красное и зелёное.
    """
    digest = TradeDigest(theme=DARK)
    try:
        digest.show_span(SpanFacts(_span(first, last), MONEY), QPoint(100, 100))
        markup = digest.body.text()
        colour = getattr(DARK, expected)

        assert f'<b style="color:{colour}">Чистая прибыль:' in markup, (
            f"чистая прибыль не выделена цветом {expected}: {markup!r}"
        )
        assert "Валовая" in markup and 'color:' not in markup.split("Валовая")[0][-40:], (
            "цветом выделено не то число"
        )
    finally:
        digest.hide()
        digest.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("theme", [LIGHT, DARK], ids=["светлая", "тёмная"])
def test_the_net_result_of_the_stretch_is_readable_in_both_themes(qapp, theme) -> None:
    """Выделенная чистая прибыль читается в обеих темах (`D-016`, `B-013`).

    Фон берётся **с нарисованной картинки**, а не из темы: окно разбора без
    собственного фона показывалось бы на системном, и цвета темы оказались бы
    не на том фоне, для которого подобраны. Цвет числа берётся из разметки,
    которая ушла в надпись, — то есть тот же, что увидит человек.
    """
    digest = TradeDigest(theme=theme)
    try:
        digest.show_span(SpanFacts(_span(4, 6), MONEY), QPoint(100, 100))
        qapp.processEvents()
        paper = QColor(digest.body.grab().toImage().pixel(1, 1)).name()
        found = re.search(
            r'<b style="color:(#[0-9a-fA-F]{6})">Чистая прибыль:', digest.body.text()
        )
        assert found is not None, "чистая прибыль в окне разбора ничем не выделена"

        assert contrast(found.group(1), paper) >= READABLE, (
            f"чистая прибыль участка: {found.group(1)} на {paper} даёт "
            f"{contrast(found.group(1), paper):.2f}:1 при пороге {READABLE}:1"
        )
    finally:
        digest.hide()
        digest.deleteLater()
        qapp.processEvents()


def test_the_total_admits_it_was_counted_over_a_gap_in_the_data() -> None:
    """Внутри участка пропуск данных — итог считается по тому, что есть.

    Молчание здесь превращает «часть свечей потеряна» в «столько робот
    и заработал», и это `B-005` в денежном выражении.
    """
    span = ChartSpan(
        start=SOLID[0].opens_at,
        end=SOLID[11].opens_at + STEP,
        holes=((_at(13, 45), _at(14, 15)),),
    )
    text = "\n".join(digest_lines(span, MONEY))

    assert "по тем сделкам, что есть" in text, (
        f"итог посчитан поверх пропуска данных молча: {text!r}"
    )


def test_without_a_tariff_the_stretch_has_no_net_profit_and_says_why() -> None:
    """Тариф комиссии не задан — чистой прибыли нет, и названа причина.

    Ноль комиссии объявил бы участок окупившим издержки, а реверсная система
    на пятиминутках делает много переворотов (`DOMAIN.md` §5).
    """
    rows = tuple(
        TradeRow(
            entry_time=row.entry_time, exit_time=row.exit_time, side=row.side,
            volume=row.volume, entry_price=row.entry_price, exit_price=row.exit_price,
            exit_reason=row.exit_reason, profit_rub=row.profit_rub,
            profit_pct=row.profit_pct, commission_rub=None, trade_id=row.trade_id,
        )
        for row in MONEY
    )
    text = "\n".join(digest_lines(_span(0, 11), rows))

    assert "Чистая прибыль: —" in text, (
        f"без тарифа комиссии показана чистая прибыль: {text!r}"
    )
    assert "тариф комиссии не задан" in text, (
        "чистой прибыли нет, и почему — не сказано"
    )


# ------------------------------------------------------- через живую панель


def _right_click(surface: PainterChartSurface, point: QPointF) -> None:
    for kind, button, buttons in (
        (QEvent.Type.MouseButtonPress, Qt.MouseButton.RightButton, Qt.MouseButton.RightButton),
        (QEvent.Type.MouseButtonRelease, Qt.MouseButton.RightButton, Qt.MouseButton.NoButton),
    ):
        QApplication.sendEvent(
            surface,
            QMouseEvent(
                kind, point, QPointF(point), button, buttons, Qt.KeyboardModifier.NoModifier
            ),
        )


def test_a_right_click_opens_the_digest_of_the_trade_under_it(qapp) -> None:
    """Правый щелчок по графику открывает разбор — с той сделкой, что под ним.

    Проверка идёт через настоящую отрисовку и настоящее событие мыши: между
    щелчком и текстом стоят `_place_at_x`, отрезок времени и отбор сделок,
    и любое из трёх способно показать соседнюю сделку, ничего не уронив.
    """
    surface = PainterChartSurface()
    panel = ChartPanel(surface=surface)
    panel.resize(1000, 640)
    panel.show()
    qapp.processEvents()
    surface.resize(WIDTH, HEIGHT)
    surface.show_chart(ChartData(instrument="MXU6", timeframe="5 минут", candles=SOLID))
    panel.set_trades(TRADES)
    try:
        step = (WIDTH - AXIS_WIDTH - PADDING) / SOLID_PLACES
        middle = QPointF(
            PADDING + (5 + 0.5) * step, (PADDING + HEIGHT - AXIS_HEIGHT) / 2
        )
        _right_click(surface, middle)
        qapp.processEvents()

        assert panel.digest.isVisible(), "правый щелчок разбор не открыл"
        shown = panel.digest.body.text()
        assert MIDDLE.exit_reason in shown, (
            f"в разборе нет сделки, на которую щёлкнули: {shown!r}"
        )
        assert EARLY.exit_reason not in shown and LATE.exit_reason not in shown, (
            "в разбор попали сделки соседних участков"
        )
    finally:
        panel.digest.hide()
        panel.deleteLater()
        qapp.processEvents()


def test_the_digest_closes_by_escape(qapp) -> None:
    """Окно разбора закрывается клавишей Esc — без кнопок и без поиска крестика.

    Всплывающее окно Qt закрывается ещё и щелчком мимо себя; проверить это
    без настоящего экрана нечем, а Esc проверяется событием.
    """
    digest = TradeDigest()
    try:
        digest.show_span(SpanFacts(_span(0, 3)), QPoint(100, 100))
        assert digest.isVisible(), "разбор не открылся вовсе"

        QApplication.sendEvent(
            digest,
            QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier),
        )
        qapp.processEvents()
        assert not digest.isVisible(), (
            "по Esc окно разбора не закрылось: закрыть его человеку нечем"
        )
    finally:
        digest.deleteLater()
        qapp.processEvents()


def test_the_digest_stays_on_the_screen_even_at_its_far_corner(qapp) -> None:
    """Разбор сделки у правого края графика не уезжает за границу экрана.

    Вместе с ним уехала бы половина чисел, ради которых его открыли.
    """
    area = QGuiApplication.primaryScreen().availableGeometry()
    corner = QPoint(area.right() - 2, area.bottom() - 2)
    placed = _fit_on_screen(corner, 560, 420)

    assert placed.x() + 560 <= area.right() + 1, (
        f"окно шириной 560 поставлено в x={placed.x()}: правый край экрана "
        f"{area.right()}"
    )
    assert placed.y() + 420 <= area.bottom() + 1, (
        f"окно высотой 420 поставлено в y={placed.y()}: нижний край экрана "
        f"{area.bottom()}"
    )


def test_the_layer_that_is_switched_off_stays_out_of_the_digest(qapp) -> None:
    """Слой, снятый галочкой, в разбор не попадает.

    Разбор объясняет нарисованное. Метка слоя, которого на экране нет,
    в нём была бы ответом про то, чего человек не видит.
    """
    surface = PainterChartSurface()
    panel = ChartPanel(surface=surface)
    try:
        plan = Marker(
            time=SOLID[5].opens_at,
            price=SOLID[5].close,
            kind=MarkerKind.MISSED,
            layer=Layer.PLAN,
            tooltip="Сигнал был, сделки нет: не было связи",
        )
        panel.set_markers(Layer.PLAN, [plan])
        panel.plan_box.setChecked(False)
        panel.show_span(_span(4, 6))
        without = panel.digest.body.text()

        panel.plan_box.setChecked(True)
        panel.show_span(_span(4, 6))
        with_plan = panel.digest.body.text()

        assert MarkerKind.MISSED.label not in without, (
            "метка снятого слоя попала в разбор"
        )
        assert MarkerKind.MISSED.label in with_plan, (
            "метка включённого слоя в разбор не попала"
        )
    finally:
        panel.digest.hide()
        panel.deleteLater()
        qapp.processEvents()
