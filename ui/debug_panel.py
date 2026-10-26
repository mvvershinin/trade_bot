"""Панель «Отладка»: живая лента того, что делает робот. Открыл — посмотрел — свернул.

Просьба владельца счёта 12.10.2026; миниплан — `.docs/plans/debug-panel.md`.

Окно здесь ничего не решает и ничего не сочиняет: строки приходят готовыми
и очищенными из `app/debug_feed.py` через протокол `ui.ports.DebugSource`.
Панель только забирает новое, красит строку по важности и держит прокрутку.

Как устроено
------------
* **Док у нижнего края**, только закрываемый: отрыва и переноса нет, место
  одно — прямо над строкой состояния, где стоит кнопка «Отладка ▴/▾».
  При запуске свёрнут. Состояние кнопки берётся из `visibilityChanged`:
  крестик дока и кнопка не расходятся.
* **Забор по таймеру и только пока панель открыта.** Строки пишутся десятками
  в секунду при прогреве и из любого потока; сигнал на каждую заваливал бы
  очередь Qt, пока на панель никто не смотрит. Пока панель свёрнута, строки
  копятся в буфере ленты и приходят при открытии, а вытесненные отмечаются
  строкой «вытеснено N» между старым и новым.
* **Прокрутка не борется с человеком.** Внизу — лента идёт за новыми
  строками; прокрутили вверх — видимая строка стоит на месте, даже когда
  старые строки уходят сверху.
* **Цвет — из текущей темы, в `data()`**: смена темы перекрашивает и то,
  что уже на экране.
* «Очистить» очищает окно, а не ленту. «Скопировать» берёт выделенное,
  а без выделения — всё, и прогоняет текст через чистку окна (второй рубеж
  после `app/debug_feed.py`). Ctrl+C в списке идёт тем же путём, а не
  штатным копированием Qt, которое кладёт текст ряда в буфер мимо чистки.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QPersistentModelIndex,
    QSignalBlocker,
    Qt,
    QTimer,
)
from PySide6.QtGui import QColor, QFontDatabase, QGuiApplication, QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QListView,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ui.formatting import fmt_time
from ui.models import DebugLine, DecisionLevel
from ui.ports import DebugSource, Sanitize
from ui.theme import Theme

__all__ = [
    "BATCH",
    "CAPACITY",
    "DebugModel",
    "DebugPanel",
    "FOLDED",
    "POLL_MS",
    "UNFOLDED",
    "line_text",
]

#: Сколько строк держит панель. Столько же, сколько лента
#: (`app/debug_feed.py::BUFFER`): больше показать нечего, меньше — значит
#: терять на экране то, что лента ещё помнит.
CAPACITY = 5000
#: Как часто открытая панель забирает новое, миллисекунды. Четыре раза
#: в секунду — глазу это «сразу», а Qt не замечает нагрузки.
POLL_MS = 250
#: Сколько строк за один забор. Открытие после двух свёрнутых дней — пять
#: заборов по тысяче, а не одна пачка в пять тысяч строк, на которой окно
#: замерло бы.
BATCH = 1000
#: Сколько строк видно при первом открытии. Бар с исполнением, сделкой,
#: словами движка и заявками — до десяти строк: двенадцать вмещают его целиком.
#: Меньше выходит только в окне, где графику и журналам иначе не хватит их
#: наименьшей высоты: их место панель не отбирает.
OPEN_ROWS = 12

TITLE = "Отладка — живая лента, время МСК"
FOLDED = "Отладка ▴"
UNFOLDED = "Отладка ▾"

#: Корень списка — умолчание `rowCount`, один на модуль, а не новый на вызов.
_ROOT = QModelIndex()
_Index = QModelIndex | QPersistentModelIndex

#: Поля вокруг списка: слева, сверху, справа, снизу. Сверху ноль — над
#: списком шапка дока со своими полями.
_BODY_MARGINS = (6, 0, 6, 6)

#: Номер у строки-отметки «вытеснено». Строки ленты нумеруются с единицы.
GAP = 0

#: Пометка важности в начале строки. У обычной строки пометки нет:
#: в ленте их большинство, и слово на каждой было бы шумом.
_TAGS: dict[DecisionLevel, str] = {
    DecisionLevel.INFO: "",
    DecisionLevel.TRADE: "[сделка] ",
    DecisionLevel.WARNING: "[внимание] ",
    DecisionLevel.ERROR: "[ошибка] ",
}


def line_text(line: DebugLine) -> str:
    """Строка ленты так, как она стоит на экране и уходит в буфер обмена."""
    return f"{fmt_time(line.time, seconds=True)}  {line.source}: {_TAGS[line.level]}{line.text}"


def _gap(evicted: int, at: datetime) -> DebugLine:
    """Отметка на месте строк, которые лента вытеснила непрочитанными."""
    return DebugLine(
        seq=GAP,
        time=at,
        level=DecisionLevel.INFO,
        source="лента",
        text=f"— вытеснено строк: {evicted}. Лента держит последние {CAPACITY}, "
             "эти ушли из неё непрочитанными, пока панель была свёрнута —",
    )


class DebugModel(QAbstractListModel):
    """Строки панели. Не больше `capacity`; старые уходят сверху."""

    def __init__(self, theme: Theme, capacity: int = CAPACITY) -> None:
        super().__init__()
        self._rows: list[DebugLine] = []
        self._theme = theme
        self._capacity = capacity

    def rowCount(self, parent: _Index = _ROOT) -> int:  # имя задано Qt
        """Строк на экране. У списка нет вложенных рядов."""
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index: _Index, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        """Текст, подсказка и цвет ряда — цвет из текущей темы."""
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return line_text(row)
        if role == Qt.ItemDataRole.ToolTipRole:
            return row.text
        if role == Qt.ItemDataRole.ForegroundRole:
            return QColor(self.colour(row))
        return None

    def colour(self, row: DebugLine) -> str:
        """Цвет строки в текущей теме: по важности, отметка — приглушённо."""
        if row.seq == GAP:
            return self._theme.text_dim
        return {
            DecisionLevel.INFO: self._theme.text,
            DecisionLevel.TRADE: self._theme.average,
            DecisionLevel.WARNING: self._theme.warning,
            DecisionLevel.ERROR: self._theme.danger,
        }[row.level]

    def line(self, row: int) -> DebugLine:
        """Строка по номеру ряда — для проверок и копирования."""
        return self._rows[row]

    def add(self, lines: Sequence[DebugLine], evicted: int = 0) -> int:
        """Дописать строки снизу; вернуть, сколько старых ушло сверху.

        Отметка «вытеснено» встаёт **между** старыми и новыми строками —
        на каждом заборе, где лента сообщила о вытесненных, а не только
        первой строкой при первом открытии.
        """
        if not lines:
            return 0
        fresh = [_gap(evicted, lines[0].time), *lines] if evicted > 0 else list(lines)
        first = len(self._rows)
        self.beginInsertRows(QModelIndex(), first, first + len(fresh) - 1)
        self._rows.extend(fresh)
        self.endInsertRows()
        extra = len(self._rows) - self._capacity
        if extra <= 0:
            return 0
        self.beginRemoveRows(QModelIndex(), 0, extra - 1)
        del self._rows[:extra]
        self.endRemoveRows()
        return extra

    def clear(self) -> None:
        """Убрать все строки с экрана. Ленту это не трогает."""
        self.beginResetModel()
        self._rows.clear()
        self.endResetModel()

    def set_theme(self, theme: Theme) -> None:
        """Перекрасить строки, уже стоящие на экране."""
        self._theme = theme
        if self._rows:
            self.dataChanged.emit(
                self.index(0), self.index(len(self._rows) - 1),
                [Qt.ItemDataRole.ForegroundRole],
            )

    def text_of(self, rows: Sequence[int]) -> str:
        """Текст рядов по порядку — по строке на ряд."""
        return "\n".join(line_text(self._rows[row]) for row in sorted(rows))


class _FeedView(QListView):
    """Список ленты, у которого Ctrl+C — та же процедура, что «Скопировать».

    Штатное копирование Qt (`QAbstractItemView.keyPressEvent`) кладёт в буфер
    обмена текст текущего ряда мимо чистки окна. Путь в буфер один, и он
    проходит через `DebugPanel.copy`.
    """

    def __init__(self, copy: Callable[[], object]) -> None:
        super().__init__()
        self._copy = copy

    def keyPressEvent(self, event: QKeyEvent) -> None:  # имя задано Qt
        """Ctrl+C (и любая другая клавиша копирования системы) — через `copy`."""
        if event.matches(QKeySequence.StandardKey.Copy):
            self._copy()
            event.accept()
            return
        super().keyPressEvent(event)


class DebugPanel(QDockWidget):
    """Док «Отладка» и его кнопка в строке состояния (`button`)."""

    def __init__(
        self,
        source: DebugSource | None,
        *,
        sanitize: Sanitize,
        theme: Theme,
        capacity: int = CAPACITY,
        parent: QWidget | None = None,
    ) -> None:
        """:param capacity: сколько строк держит панель; меньше — только в проверках."""
        super().__init__(TITLE, parent)
        self.setObjectName("debug_panel")
        self.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable)
        self.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        self._source = source
        self._sanitize = sanitize
        self._theme = theme
        #: Номер последней забранной строки ленты.
        self._seen = 0
        self._lost = 0
        #: Причина первой несобранной строки, как её прислала лента.
        self._cause = ""
        #: Просила ли панель место на `OPEN_ROWS` строк. Один раз: дальше
        #: высоту задаёт человек, перетаскивая край.
        self._sized = False
        self.model = DebugModel(theme, capacity)
        self.view = self._build_view()
        self.counts = QLabel()
        # Причина отказа бывает длинной: она обрезается справа, а не расширяет
        # окно. Целиком — в подсказке.
        self.counts.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setTitleBarWidget(self._build_header())
        self.setWidget(self._build_body())
        self.button = self._build_button()
        self.visibilityChanged.connect(self._mirror)
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self.poll)
        self.set_theme(theme)

    def _build_view(self) -> QListView:
        view = _FeedView(self.copy)
        view.setModel(self.model)
        view.setUniformItemSizes(True)
        view.setWordWrap(False)
        view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # Поштучная прокрутка: значение полосы — номер верхнего ряда, и поправка
        # на ушедшие сверху ряды считается вычитанием, а не пикселями.
        view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerItem)
        fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        size = self.font().pointSizeF()
        if size > 0:
            fixed.setPointSizeF(size)
        view.setFont(fixed)
        view.setMinimumHeight(120)
        return view

    def _build_header(self) -> QWidget:
        """Шапка одной строкой вместо заголовка дока: строка высоты — строка ленты.

        Заголовок, счёт строк, «Скопировать», «Очистить» и «Свернуть» стоят
        в ряд на месте заголовка дока. Две строки шапки съедали у ленты две
        строки, а «время МСК» в них было названо дважды.
        """
        title = QLabel(TITLE)
        title.setStyleSheet("font-weight: bold;")
        copy = QPushButton("Скопировать")
        copy.setToolTip(
            "Скопировать выделенные строки, а если ничего не выделено — все. "
            "Токен из текста вырезается."
        )
        copy.clicked.connect(self.copy)
        wipe = QPushButton("Очистить")
        wipe.setToolTip(
            "Убрать строки с экрана. Лента продолжает идти: новые строки "
            "появятся сами, а записи в журналах и в базе не трогаются."
        )
        wipe.clicked.connect(self.model.clear)
        fold = QPushButton("Свернуть")
        fold.setToolTip(
            "Свернуть панель. Лента продолжает идти; открыть снова — кнопкой "
            "«Отладка» в строке состояния внизу окна."
        )
        fold.clicked.connect(self.close)
        header = QWidget()
        top = QHBoxLayout(header)
        top.setContentsMargins(6, 2, 6, 2)
        top.addWidget(title)
        top.addSpacing(12)
        top.addWidget(self.counts, 1)
        for button in (copy, wipe, fold):
            top.addWidget(button)
        return header

    def _build_body(self) -> QWidget:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(*_BODY_MARGINS)
        layout.addWidget(self.view, 1)
        return body

    def _build_button(self) -> QToolButton:
        button = QToolButton()
        button.setCheckable(True)
        button.setText(FOLDED)
        button.setToolTip(
            "Лента «Отладка»: приём минут, закрытие баров, решения движка, "
            "заявки и исполнения симуляции, ошибки — по времени МСК. "
            "Открыть и свернуть — этой кнопкой."
        )
        button.clicked.connect(self._unfold)
        return button

    # ------------------------------------------------------------- поведение

    def _unfold(self) -> None:
        """Нажата кнопка: показать док, если она нажата, иначе свернуть."""
        self.setVisible(self.button.isChecked())

    def _mirror(self, visible: bool) -> None:
        """Кнопка и таймер — по факту видимости дока, откуда бы она ни пришла."""
        with QSignalBlocker(self.button):
            self.button.setChecked(visible)
        self.button.setText(UNFOLDED if visible else FOLDED)
        if visible:
            self.poll()
            self._timer.start()
            if not self._sized:
                self._sized = True
                self._ask_height()
        else:
            self._timer.stop()

    def _ask_height(self) -> None:
        """Первое открытие: попросить у окна место на `OPEN_ROWS` строк ленты.

        Окно отдаёт его не больше, чем позволяет наименьшая высота графика
        и журналов (`QMainWindow.resizeDocks`). Ряд ещё пустой ленты меряется
        шрифтом списка.
        """
        window = self.parentWidget()
        if not isinstance(window, QMainWindow):
            return
        view = self.view
        row = view.sizeHintForRow(0) if self.model.rowCount() else -1
        if row <= 0:
            row = view.fontMetrics().lineSpacing() + 2
        _, top, _, bottom = _BODY_MARGINS
        chrome = self.titleBarWidget().sizeHint().height() + top + bottom + 2 * view.frameWidth()
        window.resizeDocks([self], [chrome + OPEN_ROWS * row], Qt.Orientation.Vertical)

    def poll(self) -> None:
        """Забрать из ленты новое и дописать снизу, не сбивая прокрутку человека."""
        if self._source is None:
            return
        batch = self._source.since(self._seen, BATCH)
        if (batch.lost, batch.cause) != (self._lost, self._cause):
            self._lost, self._cause = batch.lost, batch.cause
            self._say_counts()
        if not batch.lines:
            return
        bar = self.view.verticalScrollBar()
        follow = bar.value() >= bar.maximum()
        top = bar.value()
        removed = self.model.add(batch.lines, batch.evicted)
        self._seen = batch.lines[-1].seq
        if follow:
            self.view.scrollToBottom()
        else:
            bar.setValue(max(0, top - removed))
        self._say_counts()

    def copy(self) -> str:
        """В буфер обмена — выделенное, а без выделения всё. Через чистку окна."""
        rows = [index.row() for index in self.view.selectionModel().selectedRows()]
        if not rows:
            rows = list(range(self.model.rowCount()))
        text = self._sanitize(self.model.text_of(rows))
        QGuiApplication.clipboard().setText(text)
        return text

    def set_theme(self, theme: Theme) -> None:
        """Фон, текст и цвета строк — по теме окна."""
        self.model.set_theme(theme)
        self.view.setStyleSheet(
            f"QListView {{ background: {theme.background}; color: {theme.text}; }}"
        )
        self._theme = theme
        self._say_counts()

    def _say_counts(self) -> None:
        """Шапка: сколько строк на экране, сколько лента не собрала и почему.

        Причина первой потери стоит рядом с числом, а не «в техническом
        журнале»: отказ самого обработчика ленты в журнал не пишется,
        и обещание журнала для него было бы ложным.
        """
        text = f"строк на экране: {self.model.rowCount()}"
        tip = ""
        if self._source is None:
            text += " · лента не подключена"
        if self._lost:
            cause = self._sanitize(self._cause) or "не названа"
            text += f" · не собрано строк из-за ошибки: {self._lost}, первая причина: {cause}"
            tip = (
                "Сколько строк ленты не удалось собрать из-за ошибки с начала работы. "
                "Робот работает дальше: ошибка ленты не трогает ни котировки, "
                f"ни решения.\n\nПервая причина: {cause}"
            )
        self.counts.setText(text)
        self.counts.setToolTip(tip)
        self.counts.setStyleSheet(f"color: {self._theme.warning};" if self._lost else "")
