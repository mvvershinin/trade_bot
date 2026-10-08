"""Прокрутка, которая на просторном экране не видна, а на тесном не режет.

Заведена 05.10.2026 вместе со шрифтом ×1,2 (`ui.theme.FONT_SCALE`). Текст
с переносом строк (легенда графика, итог и оговорка под журналом сделок)
требовал высоту целиком **как наименьшую**: на экране 1280×800 главное окно
вылезало за нижний край, у графика пропадала ось времени, а таблица сделок
оставалась без единой видимой строки. В прокрутке такой текст занимает всё,
что ему нужно, когда место есть, и сжимается до нескольких строк с полосой
прокрутки, когда места нет. Ничего не режется: лишнее уходит под полосу.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import QFrame, QScrollArea, QWidget


class FitScrollArea(QScrollArea):
    """Желаемая высота — содержимое целиком; наименьшая — `min_lines` строк.

    Желаемая считается по нынешней ширине (`heightForWidth`): текст с
    переносом при другой ширине занимает другое число строк, и раскладке
    об этом говорится при каждой смене ширины.
    """

    def __init__(self, content: QWidget, *, min_lines: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidget(content)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._min_lines = min_lines
        self._width = -1

    def _wanted_height(self, width: int) -> int:
        content = self.widget()
        if content is None:
            return 0
        wanted = content.heightForWidth(width)
        return wanted if wanted > 0 else content.sizeHint().height()

    def sizeHint(self) -> QSize:
        """Содержимое целиком при нынешней ширине."""
        hint = super().sizeHint()
        width = self.viewport().width() if self.isVisible() else hint.width()
        return QSize(hint.width(), self._wanted_height(width) + 2 * self.frameWidth())

    def minimumSizeHint(self) -> QSize:
        """`min_lines` строк текста, но не больше самого содержимого."""
        lines = self.fontMetrics().lineSpacing() * self._min_lines
        return QSize(super().minimumSizeHint().width(), min(lines, self.sizeHint().height()))

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Ширина сменилась — сменилась и желаемая высота: сказать раскладке."""
        super().resizeEvent(event)
        if event.size().width() != self._width:
            self._width = event.size().width()
            self.updateGeometry()
