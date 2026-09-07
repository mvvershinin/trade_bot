"""Панель состояния сверху: инструмент, связь, режим, позиция, прибыль за день.

Панель только показывает то, что пришло в `RobotState`. Она не считает прибыль,
не определяет, есть ли позиция, и не решает, боевой сейчас режим или симуляция:
все ответы приходят готовыми.

Два места, где оформление — не косметика:

* **Красная отметка боевого режима.** Спутать симуляцию с боем нельзя (ТЗ §4.4 З).
* **Знак прибыли за день.** Цвет и знак ставятся по числу, которое пришло;
  «−» рисуется типографским минусом, чтобы не читался как дефис.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ui.formatting import EMPTY, fmt_money, fmt_percent, fmt_price, fmt_volume
from ui.models import Connection, RobotState
from ui.notices import Context, guard_note
from ui.theme import Theme, current as current_theme, text_on


class _Cell(QWidget):
    """Подпись и значение в столбик — одна ячейка панели."""

    def __init__(self, caption: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.caption = QLabel(caption)
        self.value = QLabel(EMPTY)
        font = self.value.font()
        font.setBold(True)
        self.value.setFont(font)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(1)
        layout.addWidget(self.caption)
        layout.addWidget(self.value)

    def set(self, text: str, color: str | None = None) -> None:
        self.value.setText(text)
        self.value.setStyleSheet(f"color: {color};" if color else "")


#: Каким цветом показано состояние связи. Таблица, а не развилка внутри
#: `apply_state`: пятое значение `Connection` появилось 05.09.2026, и словарь,
#: живший в теле метода, дал бы `KeyError` в панели — то есть падение окна
#: на добавлении значения перечисления.
#:
#: ⚠️ Закрытая биржа — **приглушённым**, а не тревожным. Суббота это обычное
#: состояние, а не сбой; тревожный цвет учил бы пугаться выходных. Различает
#: эти состояния подпись, а не окраска: «Биржа закрыта» не спутать
#: с «Восстанавливаем связь», из-за которого владелец счёта потерял полчаса
#: (`B-022`).
_CONNECTION_COLORS: dict[Connection, str] = {
    Connection.ONLINE: "success",
    Connection.RECONNECTING: "warning",
    Connection.OFFLINE: "danger",
    Connection.UNKNOWN: "text_dim",
    Connection.MARKET_CLOSED: "text_dim",
}


def _connection_color(theme: Theme, connection: Connection) -> str:
    """Цвет состояния связи в текущей теме.

    Имя роли, а не готовый цвет: цвета живут в теме и меняются вместе с ней,
    а таблица собирается один раз при импорте.
    """
    return str(getattr(theme, _CONNECTION_COLORS[connection]))


class StatusPanel(QFrame):
    """Строка состояния под заголовком окна."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme: Theme = current_theme()
        self.setFrameShape(QFrame.Shape.StyledPanel)

        self.instrument = _Cell("Инструмент")
        self.connection = _Cell("Связь")
        self.mode = _Cell("Режим")
        self.position = _Cell("Позиция")
        self.profit = _Cell("Прибыль за день")
        self.commission = _Cell("Комиссия за день")

        self.regime = QLabel("СИМУЛЯЦИЯ")
        self.regime.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.regime.setToolTip("Заявки на биржу не отправляются.")
        font = self.regime.font()
        font.setBold(True)
        self.regime.setFont(font)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(0)
        for cell in (self.instrument, self.connection, self.mode,
                     self.position, self.profit, self.commission):
            layout.addWidget(cell)
        layout.addStretch(1)
        layout.addWidget(self.regime)

        self.apply_state(RobotState())

    def set_theme(self, theme: Theme) -> None:
        self._theme = theme
        for cell in (self.instrument, self.connection, self.mode,
                     self.position, self.profit, self.commission):
            cell.caption.setStyleSheet(f"color: {theme.text_dim};")

    def apply_state(self, state: RobotState) -> None:
        theme = self._theme
        self.instrument.set(f"{state.instrument} · {state.timeframe}")

        self.connection.set(
            state.connection.label, _connection_color(theme, state.connection)
        )

        # ⚠️ «Остановлен» и «стоит» — разные вещи, и путать их нельзя.
        # «Робот стоит» — обычное состояние: его не запускали. «Остановлен» —
        # движок перестал понимать, что происходит на счёте, и ждёт человека;
        # сам он из этого состояния не выйдет. Подробности — крупной плашкой
        # в главном окне, здесь только отметка, чтобы состояние было видно
        # и после того, как плашку пролистали.
        if state.halted:
            self.mode.set(f"ОСТАНОВЛЕН · {state.mode.label}", theme.danger)
            self.mode.value.setToolTip(state.halted)
        else:
            mode_text = state.mode.label + ("" if state.running else " · робот стоит")
            self.mode.set(mode_text, theme.danger if state.mode.name == "OFF" else None)
            self.mode.value.setToolTip(state.mode.hint)

        if state.position is None:
            self.position.set("Нет позиции", theme.text_dim)
            self.position.value.setToolTip("")
        else:
            side_color = theme.long if state.position.side.name == "LONG" else theme.short
            self.position.set(
                f"{state.position.side.label} {fmt_volume(state.position.volume)} "
                f"по {fmt_price(state.position.entry_price)}",
                side_color,
            )
            # ⚠️ Обстановка передаётся, а не подразумевается. `guard_note`
            # написан так, что часть его утверждений верна только там, где
            # робот перестал управлять позицией; безусловный вызов показывал
            # работающему роботу «и робот её не закроет» — в режиме переворота
            # это прямая неправда, и стоит она позиции, закрытой руками,
            # а следом — открытой в обратную сторону на полный объём.
            self.position.value.setToolTip(
                "Открытая позиция. Изменение объёма в настройках её не трогает — "
                "новый объём действует со следующей сделки.\n\n"
                + guard_note(state.position, Context.of(state))
            )

        if state.day_profit_rub is None:
            self.profit.set(EMPTY, theme.text_dim)
        else:
            color = theme.success if state.day_profit_rub >= 0 else theme.danger
            text = fmt_money(state.day_profit_rub, sign=True)
            if state.day_profit_pct is not None:
                text += f" ({fmt_percent(state.day_profit_pct, sign=True)})"
            if state.day_trades is not None:
                text += f" · сделок {state.day_trades}"
            self.profit.set(text, color)
        self.profit.value.setToolTip(
            "Чистая прибыль: комиссия уже вычтена. Валовая прибыль результатом "
            "не считается."
        )

        self.commission.set(fmt_money(state.day_commission_rub), theme.text_dim)

        if state.simulation:
            self.regime.setText("  СИМУЛЯЦИЯ  ")
            self.regime.setStyleSheet(
                f"color: {theme.background}; background: {theme.text_dim};"
                " border-radius: 3px; padding: 4px 10px;"
            )
            self.regime.setToolTip("Заявки на биржу не отправляются.")
        else:
            # ⚠️ Цвет надписи считается по заливке, а не берётся белым.
            # Белым по `danger` тёмной темы (`#ef5350`) контраст 3,49:1,
            # тёмным — 5,31:1. Отметка «СИМУЛЯЦИЯ» идёт по `text_dim` и даёт
            # 7,06:1, то есть безопасное состояние читалось лучше опасного,
            # а спутать их нельзя (ТЗ §4.4 З).
            self.regime.setText("  БОЕВОЙ РЕЖИМ  ")
            self.regime.setStyleSheet(
                f"color: {text_on(theme.danger)}; background: {theme.danger};"
                " border-radius: 3px; padding: 4px 10px;"
            )
            self.regime.setToolTip("Заявки уходят на биржу. Сделки настоящие.")
