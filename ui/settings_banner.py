"""Плашка над графиком: настройки из файла приняты не так, как записаны.

Заведена по находке 04.10.2026 на пути владельца счёта. Файл настроек
просил выход с предельной ценой при незаданном шаге цены, порт подменил
выход на «по рынку» и сказал это строкой журнала — а строка обрезалась
колонкой на «Впишите ша…», в главном окне не было ни плашки, ни строки
состояния, и окно настроек открывалось на другой вкладке. Отказ был,
но до человека не доезжал (правило 13).

Текст приходит готовым от порта (`RobotState.settings_trouble`): окно
его не сочиняет. Кнопка открывает настройки сразу на поле, которое
надо поправить (`RobotState.settings_field`).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from ui.theme import current as current_theme
from ui.theme import text_on

#: Кегль плашки — тот же, что у остальных плашек над графиком (`_Banner`).
LARGE_POINTS = 14.0


class SettingsTroubleBar(QWidget):
    """Плашка «настройки приняты не так» и кнопка «Открыть настройки…»."""

    #: Человек нажал кнопку: открыть окно настроек на нужном поле.
    open_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.label = QLabel()
        self.label.setWordWrap(True)
        # Простой текст: строка пришла снаружи, `<` в ней съел бы полфразы.
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setContentsMargins(12, 8, 12, 8)
        font = self.label.font()
        font.setPointSizeF(max(font.pointSizeF() + 2.0, LARGE_POINTS))
        font.setBold(True)
        self.label.setFont(font)
        self.button = QPushButton("Открыть настройки…")
        self.button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.button.setToolTip(
            "Откроет окно настроек сразу на поле, которое нужно поправить."
        )
        self.button.clicked.connect(self.open_requested.emit)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.label, 1)
        row.addWidget(self.button)
        self.setVisible(False)

    def show_trouble(self, text: str) -> None:
        """Показать текст порта; пустой — спрятать плашку."""
        if not text:
            self.label.clear()
            self.setVisible(False)
            return
        fill = current_theme().danger
        self.label.setText(f"Настройки приняты не так, как записаны. {text}")
        self.label.setStyleSheet(
            f"background: {fill}; color: {text_on(fill)}; border-radius: 4px;"
        )
        self.setVisible(True)
