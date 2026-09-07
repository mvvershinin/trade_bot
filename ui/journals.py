"""Две вкладки под графиком: «Сделки» и «Решения робота».

Вкладка «Решения» важнее первой. Сделку видно и на графике, а вот **почему** робот
поступил именно так, не видно нигде, кроме этой таблицы. Отсюда правило, которое
здесь соблюдается буквально: **в колонке «Причина» лежит фраза, а не код.**
Текст приходит готовым из `engine/`; окно его не сочиняет, не сокращает
и не подменяет кодом ошибки брокера.

Таблицы сортируются по любой колонке — щелчком по заголовку. Числа сортируются
как числа, а не как строки: `-1 234,50` и `9,90` в текстовой сортировке
встают в неверном порядке, и журнал сделок читается неправильно ровно там,
где важнее всего.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QPushButton,
    QTabWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ui.export import (
    COMMISSION_COLUMN,
    DECISION_HEADERS,
    RESULT_COLUMN,
    result_basis,
    trade_headers,
)
from ui.formatting import (
    EMPTY,
    SUMMARY_FIELDS,
    fmt_datetime,
    fmt_money,
    fmt_percent,
    fmt_price,
    fmt_volume,
    summary_parts,
)
from ui.models import DecisionLevel, DecisionRow, TradeRow, TradesSummary
from ui.theme import Theme, current as current_theme

SORT_ROLE = Qt.ItemDataRole.UserRole + 1

#: Ширина колонки «Причина выхода» в отчёте о прогоне, в точках.
#:
#: Замерена по самой длинной причине движка — «обратный сигнал средней»
#: (`engine.ExitReason.label`), плюс запас на шрифт покрупнее. Не «на глаз»
#: и не «побольше»: причина выхода — единственная колонка этой таблицы
#: с человеческим текстом, и обрезанная она превращается в «обратны…».
REASON_WIDTH = 200


class TradesModel(QAbstractTableModel):
    """Журнал сделок: что реально произошло."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[TradeRow] = []
        self._theme: Theme = current_theme()

    # --- содержимое ---------------------------------------------------------

    def set_rows(self, rows: Sequence[TradeRow]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def append_row(self, row: TradeRow) -> None:
        """Дописать сделку — и переспросить заголовок колонки результата.

        ⚠️ `endInsertRows` заголовки не обновляет, а одна пришедшая сделка
        способна их изменить: строка без комиссии, попавшая в таблицу
        с чистыми результатами, переводит колонку в «вперемешку». Без этой
        строки подпись осталась бы от прежнего набора — то есть соврала бы
        ровно в тот момент, когда стала неправдой.
        """
        position = len(self._rows)
        before = result_basis(self._rows)
        self.beginInsertRows(QModelIndex(), position, position)
        self._rows.append(row)
        self.endInsertRows()
        if result_basis(self._rows) is not before:
            self.headerDataChanged.emit(
                Qt.Orientation.Horizontal, RESULT_COLUMN, RESULT_COLUMN
            )

    def rows(self) -> list[TradeRow]:
        return list(self._rows)

    def set_theme(self, theme: Theme) -> None:
        self._theme = theme
        if self._rows:
            top = self.index(0, 0)
            bottom = self.index(len(self._rows) - 1, self.columnCount() - 1)
            self.dataChanged.emit(top, bottom)

    # --- обязательное для QAbstractTableModel -------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(_TRADE_CELLS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        """Заголовок колонки — **по тем строкам, что лежат в таблице сейчас**.

        ⚠️ Одна колонка здесь меняет подпись, и это не украшение.
        `TradeRow.profit_rub` несёт чистый результат при заданном тарифе
        комиссии и валовый при незаданном (`app/convert.py::trade_row`).
        Тариф стирается штатно — поле комиссии в окне имеет значение
        «не задан», и это способ посмотреть валовую. Безусловная подпись
        «за вычетом комиссии» в этом случае врёт: 05.09.2026 расхождение
        валовой и чистой в прогоне владельца счёта — 4 004 ₽ на 143 сделках.

        Подпись считает `ui.export.trade_headers` — то же место, что подписывает
        колонку в выгруженном файле. Две записи одного названия разошлись бы,
        и экран говорил бы одно, а файл другое.

        Подсказка на заголовке (`ToolTipRole`) существует ровно ради этой
        колонки: подпись коротка, а сказать надо, чего в числе нет.
        """
        if orientation is not Qt.Orientation.Horizontal:
            return section + 1 if role == Qt.ItemDataRole.DisplayRole else None
        if role == Qt.ItemDataRole.DisplayRole:
            return trade_headers(self._rows)[section]
        if role == Qt.ItemDataRole.ToolTipRole:
            return _trade_header_note(self._rows, section)
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        trade = self._rows[index.row()]
        column = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            return _trade_text(trade, column)
        if role == SORT_ROLE:
            return _trade_sort_key(trade, column)
        # ⚠️ Номера колонок берутся из описания (`_TRADE_CELLS`), а не пишутся
        # числами. Числа в этом месте уже стреляли: они остаются допустимыми
        # при вставке колонки в середину и уводят выравнивание и цвет
        # на соседнюю колонку молча.
        if role == Qt.ItemDataRole.TextAlignmentRole and column in _TRADE_NUMERIC:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if (
            role == Qt.ItemDataRole.ForegroundRole
            and column in _TRADE_SIGNED
            and trade.profit_rub is not None
        ):
            return QColor(self._theme.success if trade.profit_rub >= 0 else self._theme.danger)
        if role == Qt.ItemDataRole.ToolTipRole and column == _TRADE_WIDE:
            # Длинный текст режется по ширине колонки — подсказка отдаёт его целиком.
            return trade.exit_reason
        return None


@dataclass(frozen=True, slots=True)
class _Column[RowT]:
    """Одна колонка таблицы целиком: текст, сортировка и вид — **в одной записи**.

    Врозь эти сведения разъезжаются молча. Пара «текст и ключ сортировки»
    уже стояла вместе; номера колонок для выравнивания и раскраски стояли
    отдельными числами в `data` — `(3, 4, 5, 7, 8, 9)`, `(7, 8)`, `== 6`, —
    и с описанием колонок не были связаны ничем. Вставка колонки **в середину**
    уводила бы выравнивание и цвет на соседнюю, не уронив ничего: `IndexError`
    в этом случае не возникает, потому что номера остаются допустимыми.
    Такой шов уже стрелял 03.09.2026 на конце списка, где он хотя бы падал.

    :param text: что показать человеку.
    :param sort: значение для сортировки — **сырое**, а не показанный текст:
        «−640,00 ₽» и «+1 425,00 ₽» в текстовой сортировке встают в неверном
        порядке.
    :param numeric: число. Выравнивается вправо — иначе разряды не сравнить
        глазом по колонке.
    :param signed: величина со знаком, которую красят по знаку: прибыль
        и убыток должны различаться до чтения цифры.
    :param wide: колонка тянется на всю свободную ширину. Такая в таблице
        ровно одна — это длинный человеческий текст, который иначе режется.
    """

    text: Callable[[RowT], str]
    sort: Callable[[RowT], object]
    numeric: bool = False
    signed: bool = False
    wide: bool = False


def _numeric_columns[RowT](columns: Sequence[_Column[RowT]]) -> frozenset[int]:
    return frozenset(index for index, column in enumerate(columns) if column.numeric)


def _signed_columns[RowT](columns: Sequence[_Column[RowT]]) -> frozenset[int]:
    return frozenset(index for index, column in enumerate(columns) if column.signed)


def _wide_column[RowT](columns: Sequence[_Column[RowT]]) -> int:
    """Номер тянущейся колонки. Ноль или две — ошибка описания, и она видна.

    Отказ при сборке модуля намеренный: таблица без тянущейся колонки
    выглядит рабочей, просто с пустым полем справа, и такое не замечают.
    """
    found = [index for index, column in enumerate(columns) if column.wide]
    if len(found) != 1:
        raise ValueError(
            f"тянущаяся колонка должна быть ровно одна, описано {len(found)}: {found}"
        )
    return found[0]


#: Колонки журнала сделок в окне.
#:
#: ⚠️ Число колонок окна берётся **отсюда**, а не из `len(TRADE_HEADERS)`.
#: Заголовков у выгрузки больше: происхождение прогона стоит в файле у каждой
#: строки, а в окне не стоит вовсе — окно показывает журнал одного прогона
#: за раз (решение 0011), а файл уезжает из окна и живёт дальше сам. Пока
#: `columnCount` считал заголовки выгрузки, добавленная одиннадцатая колонка
#: валила обе таблицы `IndexError` на первой же отрисовке: Qt спрашивает
#: `data` про каждую колонку, которую модель объявила.
#:
#: Колонки окна — это **начало** колонок выгрузки, и заголовок обеих берётся
#: из одного места (`ui.export.trade_headers`): две записи одного названия
#: разошлись бы, и экран говорил бы одно, а выгруженный файл другое.
_TRADE_CELLS: tuple[_Column[TradeRow], ...] = (
    _Column(
        text=lambda trade: fmt_datetime(trade.entry_time),
        sort=lambda trade: trade.entry_time.timestamp(),
    ),
    _Column(
        text=lambda trade: fmt_datetime(trade.exit_time),
        sort=lambda trade: trade.exit_time.timestamp() if trade.exit_time else 0.0,
    ),
    _Column(text=lambda trade: trade.side.label, sort=lambda trade: trade.side.label),
    _Column(
        text=lambda trade: fmt_volume(trade.volume),
        sort=lambda trade: trade.volume,
        numeric=True,
    ),
    _Column(
        text=lambda trade: fmt_price(trade.entry_price),
        sort=lambda trade: trade.entry_price,
        numeric=True,
    ),
    _Column(
        text=lambda trade: fmt_price(trade.exit_price),
        sort=lambda trade: trade.exit_price if trade.exit_price is not None else 0.0,
        numeric=True,
    ),
    # Причина выхода — тот самый человеческий текст, ради которого журнал
    # и заведён. Он длинный, режется по ширине, и потому дублируется подсказкой.
    _Column(
        text=lambda trade: trade.exit_reason,
        sort=lambda trade: trade.exit_reason,
        wide=True,
    ),
    _Column(
        text=lambda trade: fmt_money(trade.profit_rub, sign=True),
        sort=lambda trade: trade.profit_rub if trade.profit_rub is not None else 0.0,
        numeric=True,
        signed=True,
    ),
    _Column(
        text=lambda trade: fmt_percent(trade.profit_pct, sign=True),
        sort=lambda trade: trade.profit_pct if trade.profit_pct is not None else 0.0,
        numeric=True,
        signed=True,
    ),
    _Column(
        text=lambda trade: fmt_money(trade.commission_rub),
        sort=lambda trade: trade.commission_rub if trade.commission_rub is not None else 0.0,
        numeric=True,
    ),
)

#: Номера колонок считаются из описания, а не пишутся числами.
_TRADE_NUMERIC = _numeric_columns(_TRADE_CELLS)
_TRADE_SIGNED = _signed_columns(_TRADE_CELLS)
_TRADE_WIDE = _wide_column(_TRADE_CELLS)


#: Подсказки на заголовках журнала сделок. Есть не у каждой колонки: подсказка
#: на очевидном («Сторона») приучает не наводить туда, где сказано важное.
_TRADE_HEADER_NOTES: dict[int, str] = {
    _TRADE_WIDE: "Почему позиция закрылась. Текст приходит от движка как есть.",
    COMMISSION_COLUMN: (
        "Комиссия обеих сторон за эту сделку. Пусто означает «тариф не задан "
        "в настройках», а не «комиссии не было»."
    ),
}


def _trade_header_note(rows: Sequence[TradeRow], column: int) -> str | None:
    """Подсказка на заголовке. У колонки результата она зависит от строк."""
    if column == RESULT_COLUMN:
        return result_basis(rows).note
    return _TRADE_HEADER_NOTES.get(column)


def _trade_text(trade: TradeRow, column: int) -> str:
    return _TRADE_CELLS[column].text(trade)


def _trade_sort_key(trade: TradeRow, column: int) -> object:
    """Значение для сортировки — сырое, а не показанный текст."""
    return _TRADE_CELLS[column].sort(trade)


#: Колонки журнала решений в окне — те же три, что видит глаз: время, событие,
#: причина.
#:
#: Заголовков у выгрузки пять. Важность в окне показана цветом строки
#: и подсказкой, а не колонкой; происхождение прогона в окне не показано вовсе —
#: окно показывает журнал одного прогона за раз (решение 0011). Число колонок
#: берётся отсюда, а не из `len(DECISION_HEADERS)`, по той же причине,
#: что и у сделок: заголовок выгрузки, которому нет ячейки, валит таблицу.
_DECISION_CELLS: tuple[_Column[DecisionRow], ...] = (
    _Column(
        text=lambda row: fmt_datetime(row.time, seconds=True),
        sort=lambda row: row.time.timestamp(),
    ),
    _Column(text=lambda row: row.event, sort=lambda row: row.event),
    # Причина решения — то, ради чего вкладка существует. Тянется на всю ширину.
    _Column(text=lambda row: row.reason, sort=lambda row: row.reason, wide=True),
)

_DECISION_NUMERIC = _numeric_columns(_DECISION_CELLS)
_DECISION_WIDE = _wide_column(_DECISION_CELLS)


class DecisionsModel(QAbstractTableModel):
    """Журнал решений: событие и причина человеческим языком."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[DecisionRow] = []
        self._theme: Theme = current_theme()

    def set_rows(self, rows: Sequence[DecisionRow]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def append_row(self, row: DecisionRow) -> None:
        position = len(self._rows)
        self.beginInsertRows(QModelIndex(), position, position)
        self._rows.append(row)
        self.endInsertRows()

    def rows(self) -> list[DecisionRow]:
        return list(self._rows)

    def set_theme(self, theme: Theme) -> None:
        self._theme = theme
        if self._rows:
            top = self.index(0, 0)
            bottom = self.index(len(self._rows) - 1, self.columnCount() - 1)
            self.dataChanged.emit(top, bottom)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(_DECISION_CELLS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation is Qt.Orientation.Horizontal:
            return DECISION_HEADERS[section]
        return section + 1

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        column = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            return _DECISION_CELLS[column].text(row)
        if role == SORT_ROLE:
            return _DECISION_CELLS[column].sort(row)
        if role == Qt.ItemDataRole.TextAlignmentRole and column in _DECISION_NUMERIC:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ForegroundRole:
            if row.level is DecisionLevel.ERROR:
                return QColor(self._theme.danger)
            if row.level is DecisionLevel.WARNING:
                return QColor(self._theme.warning)
            if row.level is DecisionLevel.TRADE:
                return QColor(self._theme.average)
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{row.level.label}. {row.reason}"
        return None


class _Table(QTableView):
    """Таблица журнала: сортировка по колонкам, выделение строкой, только чтение."""

    def __init__(self, model: QAbstractTableModel, stretch_column: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        proxy = QSortFilterProxyModel(self)
        proxy.setSourceModel(model)
        proxy.setSortRole(SORT_ROLE)
        self.setModel(proxy)
        self.proxy = proxy

        self.setSortingEnabled(True)
        self.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(stretch_column, QHeaderView.ResizeMode.Stretch)
        self.horizontalHeader().setHighlightSections(False)


def trades_view(
    rows: Sequence[TradeRow], parent: QWidget | None = None
) -> QTableView:
    """Готовая таблица сделок — теми же колонками, что и журнал под графиком.

    Нужна отчёту о прогоне (`ui/backtest_report.py`). Собственное описание
    колонок там означало бы две таблицы про одни и те же сделки: подпись,
    поправленная в одной, разошлась бы со второй молча, а числа в них должны
    совпадать до знака — на них человек и смотрит, решая, чем торговать.

    Модель держится самой таблицей (`setParent`), иначе Python унесёт её
    сборщиком мусора сразу после возврата: у Qt-объекта останется живой
    указатель на мёртвую обёртку.

    ⚠️ Колонка причины выхода здесь **не тянется**, в отличие от журнала под
    графиком, и это правка по снимку экрана 06.09.2026. Тянущаяся колонка
    берёт свободную ширину — а в окне отчёта её нет: девять колонок не влезают
    даже в 1180 точек, и Qt ужимает тянущуюся до того, что режется сам
    заголовок («ичина выход»). Здесь ей задана ширина, которой хватает самой
    длинной причине движка («обратный сигнал средней»), а не влезшее уезжает
    под горизонтальную полосу — там оно хотя бы читается целиком.
    """
    model = TradesModel()
    model.set_rows(rows)
    view = _Table(model, stretch_column=_TRADE_WIDE, parent=parent)
    model.setParent(view)
    header = view.horizontalHeader()
    header.setSectionResizeMode(_TRADE_WIDE, QHeaderView.ResizeMode.Interactive)
    view.setColumnWidth(_TRADE_WIDE, REASON_WIDTH)
    return view


def _assumptions_label() -> QLabel:
    """Ярлык оговорки под итогом: перенос строк, шрифт на пункт меньше.

    Несколько строк подряд («это расчёт, а не выписка со счёта» и три факта
    с числами) в одну строку не помещаются ни на каком окне, поэтому перенос
    обязателен. Шрифт меньше основного на пункт, но не «мелкий»: это
    единственное место, где сказано, чего в показанной прибыли нет.
    """
    label = QLabel()
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setContentsMargins(6, 0, 6, 6)
    font = label.font()
    font.setPointSizeF(max(font.pointSizeF() - 1.0, 7.0))
    label.setFont(font)
    return label


class JournalTabs(QWidget):
    """Обе вкладки и кнопка выгрузки.

    Кнопка одна на оба журнала — так и записано в ТЗ §4.6. Куда сохранять,
    спрашивает главное окно: диалог выбора файла — не дело таблицы.
    """

    export_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme: Theme = current_theme()
        self._summary: TradesSummary | None = None
        self.trades_model = TradesModel(self)
        self.decisions_model = DecisionsModel(self)

        # Тянущаяся колонка берётся из описания колонок, а не числом:
        # у сделок это «Причина выхода», у решений — «Причина».
        self.trades_table = _Table(self.trades_model, stretch_column=_TRADE_WIDE)
        self.decisions_table = _Table(self.decisions_model, stretch_column=_DECISION_WIDE)
        self.decisions_table.sortByColumn(0, Qt.SortOrder.AscendingOrder)

        self.summary_label = QLabel()
        self.summary_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.summary_label.setContentsMargins(6, 4, 6, 4)

        # Оговорка про итог: чем он отличается от выписки со счёта. Стоит
        # вплотную под числом намеренно — она приезжает тем же полем того же
        # объекта (`TradesSummary.headline`) и разъехаться с ним не может.
        self.assumptions_label = _assumptions_label()

        trades_tab = QWidget()
        trades_layout = QVBoxLayout(trades_tab)
        trades_layout.setContentsMargins(0, 0, 0, 0)
        trades_layout.setSpacing(0)
        trades_layout.addWidget(self.trades_table, 1)
        trades_layout.addWidget(self.summary_label)
        trades_layout.addWidget(self.assumptions_label)

        decisions_tab = QWidget()
        decisions_layout = QVBoxLayout(decisions_tab)
        decisions_layout.setContentsMargins(0, 0, 0, 0)
        decisions_layout.addWidget(self.decisions_table)

        self.tabs = QTabWidget()
        self.tabs.addTab(trades_tab, "Сделки")
        self.tabs.addTab(decisions_tab, "Решения робота")
        self.tabs.setTabToolTip(0, "Что реально произошло: цены, объём, результат, комиссия.")
        self.tabs.setTabToolTip(1, "Почему робот поступил именно так. Каждая строка — причина.")

        self.export_button = QPushButton("Выгрузить оба журнала…")
        self.export_button.setToolTip(
            "Сохранить обе вкладки в файлы CSV — открываются в Excel двойным щелчком."
        )
        self.export_button.clicked.connect(self.export_requested)
        self.tabs.setCornerWidget(self.export_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tabs)

        self.set_summary(None)

    # ------------------------------------------------------------------ данные

    def set_trades(self, rows: Sequence[TradeRow], summary: TradesSummary | None = None) -> None:
        self.trades_model.set_rows(rows)
        self.set_summary(summary)

    def append_trade(self, row: TradeRow) -> None:
        self.trades_model.append_row(row)

    def set_decisions(self, rows: Sequence[DecisionRow]) -> None:
        self.decisions_model.set_rows(rows)
        self.decisions_table.scrollToBottom()

    def append_decision(self, row: DecisionRow) -> None:
        at_bottom = not self.decisions_table.verticalScrollBar().isVisible() or (
            self.decisions_table.verticalScrollBar().value()
            >= self.decisions_table.verticalScrollBar().maximum() - 2
        )
        self.decisions_model.append_row(row)
        # Прокрутка вниз — только если пользователь и так смотрел на конец списка.
        # Иначе строка, приехавшая в разгар чтения журнала, утаскивала бы экран.
        if at_bottom:
            self.decisions_table.scrollToBottom()

    def summary(self) -> TradesSummary | None:
        """Последний показанный итог — он же уходит в выгрузку."""
        return self._summary

    def set_summary(self, summary: TradesSummary | None) -> None:
        """Итог под таблицей сделок. Числа приходят готовыми, окно их не считает.

        ⚠️ Вместе с числом показывается **оговорка** — чем этот итог отличается
        от выписки со счёта (`backtest.headline`, решение 0030). До 05.09.2026
        она считалась и никуда не выводилась: владелец счёта видел «+9 908 ₽»
        и ни слова о том, что проскальзывание в этой цифре нулевое, а один шаг
        цены забирает её половину.

        Оговорка приезжает полем того же объекта, что и деньги, поэтому
        показать одно без другого нельзя даже по невнимательности.
        """
        self._summary = summary
        if summary is None:
            self.summary_label.setText(
                f"Итог за период: {EMPTY} — движок ещё не отдавал сводку"
            )
            self.summary_label.setStyleSheet(f"color: {self._theme.text_dim};")
            self._show_assumptions("")
            return
        # Подписи, порядок и оформление чисел — из общей таблицы
        # (`ui.formatting.SUMMARY_FIELDS`). Второй потребитель этой же
        # таблицы — итог выделенного участка графика: два разных набора
        # слов про одни и те же деньги были бы новой путаницей.
        parts = summary_parts(summary, SUMMARY_FIELDS)
        colour = self._theme.text
        if summary.net_profit_rub is not None:
            colour = self._theme.success if summary.net_profit_rub >= 0 else self._theme.danger
        self.summary_label.setText("Итог за период — " + " · ".join(parts))
        self.summary_label.setStyleSheet(f"color: {colour}; font-weight: 600;")
        self._show_assumptions(summary.headline)

    def _show_assumptions(self, text: str) -> None:
        """Оговорка под итогом. Пусто — строки нет вовсе, а не пустая полоса.

        Цвет обычный, а не тревожный и не приглушённый, и оба края выбраны
        осознанно. Красным она была бы тревогой на каждом прогоне — а тревога,
        которая горит всегда, перестаёт читаться. Серым мелким её не читают
        вовсе, а это единственное место, где сказано, чего в показанной
        прибыли нет.
        """
        self.assumptions_label.setText(text)
        self.assumptions_label.setStyleSheet(f"color: {self._theme.text};")
        self.assumptions_label.setVisible(bool(text))

    def set_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.trades_model.set_theme(theme)
        self.decisions_model.set_theme(theme)
        # ⚠️ Итоговая строка перекрашивается пересчётом, а не сама: её цвет
        # прописан в таблице стилей в момент показа. Без этой строки после
        # смены темы под таблицей оставался цвет прежней — зелёный светлой
        # темы на тёмном фоне даёт 3,0:1, красный 3,16:1. Это единственная
        # строка с чистой прибылью за период.
        self.set_summary(self._summary)
