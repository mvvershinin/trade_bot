"""Формы меток сделок — одно место, откуда рисуют и график, и легенда.

Зачем отдельный модуль
----------------------
Легенда объясняет метки словами, а метки рисует отрисовщик. Пока это два
куска кода, они расходятся: правка формы на графике оставляет в легенде
старый значок, и молча — картинка и подпись рядом не сверяются ничем.
Ровно тот паттерн, за который уже заплачено в `EngineSettings`
(`.docs/quality/code-size-2026-09-03.md` §8): перечисление в двух местах.

Здесь форма нарисована один раз. График зовёт `draw_marker` из
`_draw_marker`, легенда — через `ChartSurface.draw_marker_glyph`, и значок
в легенде совпадает с меткой на графике по построению, а не по внимательности.

⚠️ Чего это не решает: веб-отрисовка рисует метки средствами своей библиотеки
(`ui/chart/web_surface.py::_shape`), и её формы задаёт библиотека, а не этот
модуль. Пока вендорного файла нет на диске, веб-график не создаётся вовсе;
когда появится — либо формы сводятся вручную, либо `WebChartSurface`
переопределяет `draw_marker_glyph` под свои. Пункт записан в списке проверок
в заголовке `web_surface.py`.

Торгового здесь нет ничего: модуль знает вид метки и цвет темы, а что этот
вид означает, решено снаружи (`app/convert.py`, `DOMAIN.md` §8).
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF

from ui.models import Layer, MarkerKind
from ui.theme import Theme

#: Половина метки в логических пикселях. Qt масштабирует под HiDPI сам.
MARKER_SIZE = 6.0


@dataclass(frozen=True, slots=True)
class GlyphStyle:
    """Чем рисовать значок: цвета темы, шрифт надписи внутри и размер.

    Три величины ходят вместе всегда, поэтому и передаются вместе. Разложенные
    по отдельным аргументам, они на третьем вызове начинают путаться местами —
    и молча: `QFont` и `Theme` не проверяет никто.
    """

    theme: Theme
    font: QFont
    size: float = MARKER_SIZE


def draw_marker(
    painter: QPainter,
    point: QPointF,
    kind: MarkerKind,
    layer: Layer,
    style: GlyphStyle,
) -> None:
    """Нарисовать метку сделки с центром в `point`.

    Форма и цвет вместе, а не цвет один: на печати отчёта и при нарушении
    цветового зрения оттенок не различается вовсе, и «расчёт» слился бы
    с «фактом». Прогноз — всегда полая окружность, факт — залитая фигура
    своей формы.
    """
    theme, size = style.theme, style.size
    color = QColor(theme.marker_color(kind, layer))
    pen = QPen(color)
    pen.setWidthF(1.6)
    painter.setPen(pen)

    if kind is MarkerKind.UNPLANNED:
        _draw_unplanned(painter, point, style)
        return
    if kind is MarkerKind.MISSED:
        _draw_missed(painter, point, size)
        return

    painter.setBrush(color if layer is Layer.FACT else Qt.BrushStyle.NoBrush)
    if layer is Layer.PLAN:
        # Прогноз — всегда полая окружность, какой бы вид метки ни был.
        painter.drawEllipse(point, size, size)
    elif kind is MarkerKind.REVERSAL:
        # Переворот — своя форма и свой цвет: важно видеть, что позиция
        # не закрылась, а сменила сторону.
        painter.drawPolygon(_diamond(point, size + 1))
    elif kind is MarkerKind.EXIT:
        painter.drawRect(
            QRectF(point.x() - size + 1, point.y() - size + 1, size * 2 - 2, size * 2 - 2)
        )
    elif kind is MarkerKind.ENTRY_LONG:
        painter.drawPolygon(_triangle(point, size, up=True))
    else:
        painter.drawPolygon(_triangle(point, size, up=False))
    painter.setBrush(Qt.BrushStyle.NoBrush)


def _draw_unplanned(painter: QPainter, point: QPointF, style: GlyphStyle) -> None:
    """Сделка, которой алгоритм не предполагал (`DOMAIN.md` §8, последняя строка).

    Красный круг с восклицательным знаком — знак, который читается без легенды.
    Знак стоит **внутри** круга, а не над ним: метка обязана быть одной фигурой,
    иначе в легенде она не помещается, а на графике «!» висит сам по себе
    и читается как чужой.
    """
    theme, font, size = style.theme, style.font, style.size
    pen = QPen(QColor(theme.unplanned))
    pen.setWidthF(2.2)
    painter.setPen(pen)
    painter.setBrush(theme.qcolor(theme.unplanned, 90))
    painter.drawEllipse(point, size + 3, size + 3)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    shout = QFont(font)
    shout.setBold(True)
    shout.setPointSizeF(max(font.pointSizeF() + size - 5.0, 7.0))
    painter.setFont(shout)
    painter.drawText(
        QRectF(point.x() - size - 3, point.y() - size - 3, (size + 3) * 2, (size + 3) * 2),
        int(Qt.AlignmentFlag.AlignCenter),
        "!",
    )
    painter.setFont(font)


def _draw_missed(painter: QPainter, point: QPointF, size: float) -> None:
    """Сигнал пропущен: расчёт был, сделки нет. Полый круг, перечёркнутый крестом."""
    arm = size * 0.58
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(point, size, size)
    painter.drawLine(
        QPointF(point.x() - arm, point.y() - arm), QPointF(point.x() + arm, point.y() + arm)
    )
    painter.drawLine(
        QPointF(point.x() - arm, point.y() + arm), QPointF(point.x() + arm, point.y() - arm)
    )


def _triangle(point: QPointF, size: float, *, up: bool) -> QPolygonF:
    """Треугольник вершиной вверх (лонг) или вниз (шорт)."""
    tip = -size if up else size
    return QPolygonF([
        QPointF(point.x(), point.y() + tip),
        QPointF(point.x() + size, point.y() - tip),
        QPointF(point.x() - size, point.y() - tip),
    ])


def _diamond(point: QPointF, size: float) -> QPolygonF:
    return QPolygonF([
        QPointF(point.x(), point.y() - size),
        QPointF(point.x() + size, point.y()),
        QPointF(point.x(), point.y() + size),
        QPointF(point.x() - size, point.y()),
    ])


def draw_line_sample(
    painter: QPainter, box: QRectF, color: str, *, dashed: bool = False, width: float = 1.8
) -> None:
    """Отрезок линии для легенды: сплошной или пунктирный, поперёк `box`."""
    pen = QPen(QColor(color))
    pen.setWidthF(width)
    if dashed:
        pen.setStyle(Qt.PenStyle.DashLine)
    painter.setPen(pen)
    middle = box.center().y()
    painter.drawLine(QPointF(box.left() + 1, middle), QPointF(box.right() - 1, middle))


def draw_area_sample(
    painter: QPainter, box: QRectF, theme: Theme, color: str, *, filled: bool = True
) -> None:
    """Кусок области для легенды — затенение, выделение, пустое место.

    `filled=False` — только пунктирная рамка. Пропуск данных на графике
    заливки не имеет намеренно: заливкой показано затенение «робот молчал»,
    и залитый значок обещал бы человеку не ту фигуру.
    """
    inner = box.adjusted(1, 2, -1, -2)
    if filled:
        painter.fillRect(inner, theme.qcolor(color, 60))
    pen = QPen(theme.qcolor(color, 190))
    pen.setWidthF(1.0)
    pen.setStyle(Qt.PenStyle.DotLine)
    painter.setPen(pen)
    painter.drawRect(inner)
