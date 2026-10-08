"""Окно «Отчёты»: финансовый результат за период и разбор каждой сделки.

Просьба владельца счёта 05.10.2026, дословно: «окно "отчеты" чтобы можно было
увидеть финансовый результат с выбором периода. с объяснениями по сделкам
по типу как есть сейчас если пкм нажимаем на графике — надо для анализа
поведения бота». Миниплан — `.docs/plans/reports-window-step1.md`.

Шаг 1: отчёт по **прогону, показанному на экране**, — те же строки и тот же
итог, что в журнале «Сделки» под графиком (`ShownRun`). Сохранённых сделок
симуляции за прошлые дни здесь нет — это шаг 2, и окно говорит об этом само,
а не оставляет догадываться.

⚠️ **Здесь не считается ни одной прибыли.** Итог периода — `span_totals`,
то самое сложение готовых столбиков, которым подводит итог «Разбор сделок»
по правой кнопке на графике и которое сверено с `backtest.summarise`.
Объяснение сделки — `trade_lines` оттуда же. Окно только отбирает сделки
по дате и раскладывает готовые числа по строкам.

Правило периода
---------------
Сделка относится к периоду по **дате выхода по МСК**, границы включительно.
Результат сделки появляется в момент выхода; сделка, открытая 30-го
и закрытая 1-го, — деньги 1-го. Позиция без выхода — по дате входа.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ui.chart_panel import SpanTotals, span_totals, trade_lines
from ui.formatting import (
    EMPTY,
    MSK,
    fmt_date,
    fmt_datetime,
    fmt_money,
    fmt_number,
    fmt_price,
    fmt_volume,
    to_msk,
)
from ui.models import RunOrigin, TradeRow, TradesSummary
from ui.theme import current as current_theme
from ui.theme import resize_within_screen, scaled

__all__ = [
    "PERIODS",
    "PeriodTotals",
    "ReportsDialog",
    "ShownRun",
    "explanation_lines",
    "period_totals",
    "run_title",
    "trade_day",
    "trades_in_period",
]


@dataclass(frozen=True, slots=True)
class ShownRun:
    """Прогон, который сейчас на экране: сделки журнала, его итог и свечи графика.

    Собирает главное окно из того, что ему уже прислал порт, — второго
    источника сделок у отчёта нет. `first` и `last` — первая и последняя
    свеча графика: на них прогон и считался.
    """

    trades: tuple[TradeRow, ...] = ()
    summary: TradesSummary | None = None
    instrument: str = ""
    first: datetime | None = None
    last: datetime | None = None


#: Как назвать прогон в заголовке — по происхождению его строк.
_RUN_NAMES = {
    RunOrigin.BACKTEST: "по прогону на истории",
    RunOrigin.PAPER: "по симуляции на боевом потоке",
    RunOrigin.LIVE: "по боевому режиму",
}


def run_title(run: ShownRun) -> str:
    """Заголовок окна: что за прогон, инструмент, даты свечей."""
    # Происхождение одно — оно и названо. Смесь (история плюс сделки живого
    # хода) или пусто — нейтрально: «на истории» про смесь было бы неправдой.
    origins = {row.origin for row in run.trades}
    only = origins.pop() if len(origins) == 1 else None
    name = _RUN_NAMES[only] if only is not None else "по прогону на экране"
    dates = f"{fmt_date(run.first)} — {fmt_date(run.last)} МСК"
    return f"Отчёт {name}, {run.instrument or EMPTY}, {dates}"


def msk_today() -> date:
    """Сегодня по Москве — машина владельца счёта стоит в другом поясе."""
    return datetime.now(MSK).date()


def _month(today: date) -> tuple[date, date]:
    return today.replace(day=1), today


def _week(today: date) -> tuple[date, date]:
    return today - timedelta(days=today.weekday()), today


#: Кнопки периода: подпись и границы от «сегодня». `None` — весь прогон.
#: Таблица, а не четыре обработчика: добавить «квартал» — одна строка.
PERIODS: tuple[tuple[str, Callable[[date], tuple[date, date] | None]], ...] = (
    ("Сегодня", lambda today: (today, today)),
    ("Неделя", _week),
    ("Месяц", _month),
    ("Всё", lambda _today: None),
)

_PERIOD_TIPS = {
    "Сегодня": "Сделки, закрытые сегодня (МСК).",
    "Неделя": "С понедельника этой недели по сегодня (МСК).",
    "Месяц": "С 1-го числа текущего месяца по сегодня (МСК).",
    "Всё": "Все сделки прогона, без границ.",
}


def trade_day(row: TradeRow) -> date:
    """День сделки для отбора по периоду: дата выхода по МСК, без выхода — входа."""
    return to_msk(row.exit_time if row.exit_time is not None else row.entry_time).date()


def trades_in_period(
    rows: Sequence[TradeRow], since: date | None, until: date | None
) -> list[TradeRow]:
    """Сделки периода в их порядке. `None` у границы — граница не задана."""
    return [
        row
        for row in rows
        if (since is None or trade_day(row) >= since)
        and (until is None or trade_day(row) <= until)
    ]


@dataclass(frozen=True, slots=True)
class PeriodTotals:
    """Итог периода: сложение `span_totals` плюс счёт прибыльных и убыточных.

    Прибыльные и убыточные считаются по тому же столбику, что и деньги рядом.
    Где доля прибыльных пуста (тариф задан не у всех сделок), пусты и они:
    «прибыльная» по смеси чистых и валовых — слово ни о чём (`span_totals`).
    """

    sums: SpanTotals
    winners: int | None
    losers: int | None


def period_totals(rows: Sequence[TradeRow]) -> PeriodTotals:
    """Итог периода: `span_totals` и счёт прибыльных и убыточных по тем же строкам."""
    sums = span_totals(rows)
    if sums.profitable_share is None:
        return PeriodTotals(sums, None, None)
    results = [row.profit_rub for row in rows if row.profit_rub is not None]
    return PeriodTotals(
        sums,
        sum(1 for value in results if value > 0),
        sum(1 for value in results if value < 0),
    )


def explanation_lines(number: int, row: TradeRow) -> list[str]:
    """Объяснение сделки — тем же текстом, что в «Разборе сделок» по ПКМ.

    Вызов той же функции, а не копия: правка слов в разборе графика
    приезжает сюда сама. Совпадение стережёт `tests/test_ui_reports.py`.
    """
    return trade_lines(number, row)


def _fmt_points(value: float | None) -> str:
    """Пункты со знаком: `+120`, `−35,5`. Знак в числе, а не только в цвете."""
    if value is None:
        return EMPTY
    body = fmt_number(abs(value), 0 if float(value).is_integer() else 2)
    if value < 0:
        return f"−{body}"
    return f"+{body}" if value > 0 else body


#: Колонки таблицы позиций: заголовок, текст ячейки, число ли (вправо).
_COLUMNS: tuple[tuple[str, Callable[[TradeRow], str], bool], ...] = (
    ("Открытие, МСК", lambda row: fmt_datetime(row.entry_time), False),
    ("Сторона", lambda row: row.side.label, False),
    ("Цена входа", lambda row: fmt_price(row.entry_price), True),
    (
        "Закрытие, МСК",
        lambda row: fmt_datetime(row.exit_time) if row.exit_time else "ещё открыта",
        False,
    ),
    ("Цена выхода", lambda row: fmt_price(row.exit_price), True),
    ("Причина выхода", lambda row: row.exit_reason or "не передана", False),
    ("Объём", lambda row: fmt_volume(row.volume), True),
    ("Пункты", lambda row: _fmt_points(row.profit_points), True),
    ("Результат, ₽", lambda row: fmt_money(row.profit_rub, sign=True), True),
    ("Комиссия, ₽", lambda row: fmt_money(row.commission_rub), True),
)

_HEADER_TIPS = {
    "Пункты": "Сколько пунктов цена прошла в пользу позиции, без комиссии.",
    "Результат, ₽": (
        "Чистый результат сделки, если тариф комиссии задан в настройках; "
        "без тарифа — до комиссии, и колонка «Комиссия» тогда пуста."
    ),
    "Комиссия, ₽": "Комиссия обеих сторон сделки. Пусто — тариф не задан.",
}

#: Что сказать, когда таблицу показывать не из чего (правило 13).
NO_RUN = (
    "Прогона на экране ещё нет — отчитываться не о чем. Отчёт строится по тому "
    "прогону, что показан на графике и в журнале «Сделки»: дождитесь, пока он "
    "посчитается, или запустите «Прогон на истории…» в меню «Настройки» — "
    "окно заполнится само."
)
NO_TRADES = (
    "В прогоне на экране сделок нет. Почему робот не входил — сказано "
    "в журнале «Решения робота» под графиком."
)


class ReportsDialog(QDialog):
    """Отчёт за период по прогону на экране. Немодальное.

    `today` подставляется тестом: «текущий месяц» иначе зависел бы от дня
    прогона тестов. `jump` переводит график к сделке и говорит, удалось ли;
    `None` — двойной щелчок ничего не обещает.
    """

    def __init__(
        self,
        run: ShownRun | None,
        parent: QWidget | None = None,
        *,
        today: Callable[[], date] = msk_today,
        jump: Callable[[TradeRow], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self._theme = current_theme()
        self._today = today
        self._jump = jump
        self._run: ShownRun | None = None
        self._shown: list[TradeRow] = []
        #: Какая кнопка периода нажата последней; `None` — даты правили руками.
        #: Новый прогон перечитывает период по ней: «Всё» старого прогона
        #: не должно молча обрезать новый.
        self._picked: str | None = "Месяц"
        self._all = False
        self.setWindowTitle("Отчёты")
        resize_within_screen(self, 1240, 760)
        self._build()
        self.set_run(run)

    # ------------------------------------------------------------------ сборка

    def _build(self) -> None:
        """Сверху вниз: заголовок, период, итог, таблица, разбор строки."""
        self.title_label = QLabel()
        self.title_label.setWordWrap(True)
        self.title_label.setStyleSheet("font-weight: 600;")
        self.scope_label = QLabel(
            "Здесь сделки прогона, показанного сейчас на графике, — те же, что "
            "в журнале «Сделки». Сохранённые сделки симуляции за прошлые дни "
            "появятся в этом окне следующим шагом."
        )
        self.scope_label.setWordWrap(True)

        self.period_buttons: dict[str, QPushButton] = {}
        period_row = self._period_row()

        self.gross_label = QLabel()
        self.commission_label = QLabel()
        self.net_label = QLabel()
        self.counts_label = QLabel()
        self.notes_label = QLabel()
        self.notes_label.setWordWrap(True)
        self.empty_label = QLabel()
        self.empty_label.setWordWrap(True)
        self.empty_label.setStyleSheet(f"font-size: {scaled(12):.1f}pt; padding: 12px;")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        self._build_table()
        lower = self._lower()
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.addWidget(self.table)
        self.splitter.addWidget(lower)
        self.splitter.setSizes([440, 200])

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close = buttons.button(QDialogButtonBox.StandardButton.Close)
        if close is not None:
            close.setText("Закрыть")
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.title_label)
        layout.addWidget(self.scope_label)
        layout.addLayout(period_row)
        for label in (self.gross_label, self.commission_label, self.net_label):
            layout.addWidget(label)
        layout.addWidget(self.counts_label)
        layout.addWidget(self.notes_label)
        # Растяжение у ярлыка «пусто»: без таблицы свободное место берёт он,
        # а не промежутки между строками сверху.
        layout.addWidget(self.empty_label, 1)
        layout.addWidget(self.splitter, 1)
        layout.addWidget(buttons)

    def _period_row(self) -> QHBoxLayout:
        """Кнопки периода и две даты с календарём."""
        period_row = QHBoxLayout()
        period_row.addWidget(QLabel("Период:"))
        for caption, _ in PERIODS:
            button = QPushButton(caption)
            button.setToolTip(_PERIOD_TIPS.get(caption, ""))
            button.clicked.connect(lambda _=False, name=caption: self.pick_period(name))
            # Нажатая кнопка остаётся нажатой: видно, какой период на экране.
            # Не «по умолчанию»: Enter в окне не должен менять период.
            button.setCheckable(True)
            button.setAutoDefault(False)
            period_row.addWidget(button)
            self.period_buttons[caption] = button
        self.since_edit = self._date_edit("С какого дня, включительно (МСК).")
        self.until_edit = self._date_edit("По какой день, включительно (МСК).")
        period_row.addSpacing(12)
        period_row.addWidget(QLabel("с"))
        period_row.addWidget(self.since_edit)
        period_row.addWidget(QLabel("по"))
        period_row.addWidget(self.until_edit)
        rule = QLabel("сделка относится к дню выхода, МСК")
        rule.setToolTip(
            "Результат сделки появляется в момент выхода: сделка, открытая "
            "вечером 30-го и закрытая 1-го, попадает в 1-е."
        )
        period_row.addWidget(rule)
        period_row.addStretch(1)
        return period_row

    def _build_table(self) -> None:
        """Таблица позиций: колонки — из `_COLUMNS`."""
        self.table = QTableWidget(0, len(_COLUMNS))
        for index, (name, _, _) in enumerate(_COLUMNS):
            item = QTableWidgetItem(name)
            item.setToolTip(_HEADER_TIPS.get(name, ""))
            self.table.setHorizontalHeaderItem(index, item)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.setToolTip("Двойной щелчок по строке — перейти к сделке на графике.")
        self.table.itemSelectionChanged.connect(self._explain_selected)
        self.table.cellDoubleClicked.connect(self._jump_to)

    def _lower(self) -> QWidget:
        """Разбор выбранной сделки и строка про переход к графику."""
        self.explanation = QPlainTextEdit()
        self.explanation.setReadOnly(True)
        self.explanation.setPlaceholderText(
            "Выберите строку — здесь появится разбор сделки, тот же, что по правой "
            "кнопке на графике."
        )
        self.jump_label = QLabel()
        self.jump_label.setWordWrap(True)

        lower = QWidget()
        lower_layout = QVBoxLayout(lower)
        lower_layout.setContentsMargins(0, 0, 0, 0)
        lower_layout.addWidget(QLabel("Разбор выбранной сделки:"))
        lower_layout.addWidget(self.explanation, 1)
        lower_layout.addWidget(self.jump_label)
        return lower

    def _date_edit(self, tip: str) -> QDateEdit:
        edit = QDateEdit()
        edit.setCalendarPopup(True)
        edit.setDisplayFormat("dd.MM.yyyy")
        edit.setToolTip(tip + " Щелчок по стрелке открывает календарь.")
        edit.dateChanged.connect(self._dates_edited)
        return edit

    # ------------------------------------------------------------------ данные

    def set_run(self, run: ShownRun | None) -> None:
        """Новый прогон — окно перестраивается, выбранный период остаётся."""
        self._run = run
        self.title_label.setText("Отчёт по прогону" if run is None else run_title(run))
        if self._picked is not None:
            self.pick_period(self._picked)
        else:
            self._refresh()

    def pick_period(self, name: str) -> None:
        """Нажата кнопка периода: даты в полях встают по таблице `PERIODS`."""
        self._picked = name
        self._mark_period()
        bounds = dict(PERIODS)[name](self._today())
        self._all = bounds is None
        if bounds is None:
            # Даты в полях — только для глаза: отбор «Всё» идёт без границ.
            bounds = self._run_bounds() or (self._today(), self._today())
        since, until = bounds
        for edit, day in ((self.since_edit, since), (self.until_edit, until)):
            edit.blockSignals(True)
            edit.setDate(QDate(day.year, day.month, day.day))
            edit.blockSignals(False)
        self._refresh()

    def _mark_period(self) -> None:
        """Нажата ровно кнопка выбранного периода; даты руками — ни одна."""
        for caption, button in self.period_buttons.items():
            # Галочка и жирный шрифт, а не таблица стилей: частичная таблица
            # стилей сбрасывает у кнопки системный вид, и она съёживается.
            picked = caption == self._picked
            button.setChecked(picked)
            button.setText(f"✓ {caption}" if picked else caption)
            font = button.font()
            font.setBold(picked)
            button.setFont(font)

    def _run_bounds(self) -> tuple[date, date] | None:
        """«Всё» — от первой до последней сделки прогона; сделок нет — `None`."""
        if self._run is None or not self._run.trades:
            return None
        days = [trade_day(row) for row in self._run.trades]
        return min(days), max(days)

    def _dates_edited(self, *_: object) -> None:
        self._picked = None
        self._mark_period()
        self._all = False
        self._refresh()

    def period(self) -> tuple[date | None, date | None]:
        """Выбранный период. «Всё» — без границ: ни одна сделка не теряется."""
        if self._all:
            return None, None
        return self.since_edit.date().toPython(), self.until_edit.date().toPython()  # type: ignore[return-value]  # QDate.toPython отдаёт date, заглушки PySide6 пишут object

    def shown_trades(self) -> list[TradeRow]:
        """Сделки выбранного периода — те, что стоят в таблице, в её порядке."""
        return list(self._shown)

    # ------------------------------------------------------------------ показ

    def _refresh(self) -> None:
        since, until = self.period()
        rows = list(self._run.trades) if self._run is not None else []
        self._shown = trades_in_period(rows, since, until)
        empty = self._empty_text(rows, since, until)
        self.empty_label.setText(empty)
        self.empty_label.setVisible(bool(empty))
        self.splitter.setVisible(not empty)
        self._fill_table()
        self._show_totals()
        self.explanation.clear()
        self.jump_label.clear()

    def _empty_text(
        self, rows: Sequence[TradeRow], since: date | None, until: date | None
    ) -> str:
        """Почему таблицы нет — словами. Пусто — таблица есть."""
        if self._run is None:
            return NO_RUN
        if not rows:
            return NO_TRADES
        if self._shown:
            return ""
        if since is not None and until is not None and since > until:
            return "Начало периода позже конца — поменяйте даты местами."
        first, last = min(map(trade_day, rows)), max(map(trade_day, rows))
        return (
            f"С {since:%d.%m.%Y} по {until:%d.%m.%Y} сделок нет. Сделки прогона — "
            f"с {first:%d.%m.%Y} по {last:%d.%m.%Y}, всего {len(rows)}: "
            "нажмите «Всё» или выберите даты в календаре."
        )

    def _fill_table(self) -> None:
        self.table.setRowCount(len(self._shown))
        for line, row in enumerate(self._shown):
            for column, (_, text, numeric) in enumerate(_COLUMNS):
                item = QTableWidgetItem(text(row))
                if numeric:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                self.table.setItem(line, column, item)

    def _show_totals(self) -> None:
        if not self._shown:
            for label in self._total_labels():
                label.setVisible(False)
            return
        totals = period_totals(self._shown)
        sums = totals.sums
        gross = fmt_money(sums.gross_profit_rub, sign=True)
        self.gross_label.setText(f"Прибыль до комиссии: {gross}")
        self.commission_label.setText(f"Комиссия: {fmt_money(sums.commission_rub)}")
        self.net_label.setText(f"Чистый результат: {fmt_money(sums.net_profit_rub, sign=True)}")
        colour = self._theme.text
        if sums.net_profit_rub is not None:
            colour = self._theme.success if sums.net_profit_rub >= 0 else self._theme.danger
        self.net_label.setStyleSheet(
            f"color: {colour}; font-weight: 600; font-size: {scaled(12):.1f}pt;"
        )
        self.counts_label.setText(
            f"Сделок: {sums.trades} · прибыльных: {_count(totals.winners)} · "
            f"убыточных: {_count(totals.losers)}"
        )
        notes = _notes(sums)
        self.notes_label.setText("\n".join(notes))
        for label in self._total_labels():
            label.setVisible(label is not self.notes_label or bool(notes))

    def _total_labels(self) -> tuple[QLabel, ...]:
        return (
            self.gross_label,
            self.commission_label,
            self.net_label,
            self.counts_label,
            self.notes_label,
        )

    def _selected_line(self) -> int | None:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else None

    def _explain_selected(self) -> None:
        line = self._selected_line()
        self.jump_label.clear()
        if line is None or line >= len(self._shown):
            self.explanation.clear()
            return
        self.explanation.setPlainText(
            "\n".join(explanation_lines(line + 1, self._shown[line]))
        )

    def _jump_to(self, line: int, _column: int = 0) -> None:
        if self._jump is None or not 0 <= line < len(self._shown):
            return
        if self._jump(self._shown[line]):
            self.jump_label.setText(f"График переведён к сделке {line + 1}.")
        else:
            self.jump_label.setText(
                f"Сделки {line + 1} на графике нет: он показывает другие свечи. "
                "Прогон на истории заново выведет её на график."
            )


def _count(value: int | None) -> str:
    return EMPTY if value is None else str(value)


def _notes(sums: SpanTotals) -> list[str]:
    """Оговорки к итогу — только наступившие. Пустое — не ноль, а «неизвестно»."""
    notes = []
    if sums.counted < sums.trades:
        notes.append(
            f"Итог посчитан по {sums.counted} сделкам из {sums.trades}: "
            "по остальным прогон результата не передал."
        )
    if sums.net_profit_rub is None and sums.gross_profit_rub is not None:
        notes.append(
            "Чистого результата нет: тариф комиссии не задан. "
            "Прибыль до комиссии результатом не считается."
        )
    if sums.gross_profit_rub is None and sums.counted:
        notes.append(
            "Деньги не сложены: тариф комиссии задан не у всех сделок периода, "
            "и чистые результаты лежат вперемешку с результатами до комиссии."
        )
    return notes
