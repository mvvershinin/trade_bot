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

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
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
from ui.theme import Theme, resize_within_screen, scaled
from ui.theme import current as current_theme

__all__ = [
    "NO_PERIOD_TRADES",
    "PERIODS",
    "CardContent",
    "CommissionShare",
    "PeriodCard",
    "PeriodTotals",
    "ReportsDialog",
    "ShownRun",
    "card_content",
    "card_title",
    "commission_share",
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
    #: «Считается на: …» прогона по склейке, готовой строкой (B-069).
    basis: str = ""


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


# ---------------------------------------------------------------- карточка итога
#
# Просьба владельца счёта 09.10.2026: «если выбрали период, показываем стату
# за период… Убери оценочные суждения — мне нужны цифры и оценка комиссии».
# Карточка — только числа `period_totals` и оговорки к ним; ни одного слова
# о том, хорош результат или плох.

#: Период без сделок — одной строкой, без нулей (правило 13).
NO_PERIOD_TRADES = "Сделок в периоде нет"


def _half_up(value: float) -> int:
    """Округление «как на бумаге»: 12,5 → 13. Встроенное `round` дало бы 12."""
    return math.floor(value + 0.5)


def _days_text(count: int) -> str:
    """«1 день», «24 дня», «11 дней» — склонение по двум последним цифрам."""
    if count % 100 in range(11, 15) or count % 10 not in range(1, 5):
        return f"{count} дней"
    return f"{count} день" if count % 10 == 1 else f"{count} дня"


def _dates_text(since: date, until: date) -> str:
    """«16.09–09.10.2026»; разные годы — оба года; один день — одна дата."""
    if since == until:
        return f"{until:%d.%m.%Y}"
    head = f"{since:%d.%m}" if since.year == until.year else f"{since:%d.%m.%Y}"
    return f"{head}–{until:%d.%m.%Y}"


def card_title(instrument: str, since: date, until: date) -> str:
    """Заголовок карточки: «MXZ6 · 24 дня, 16.09–09.10.2026».

    Дни календарные, границы включительно: «Сегодня» — «1 день».
    """
    days = (until - since).days + 1
    return f"{instrument or EMPTY} · {_days_text(days)}, {_dates_text(since, until)}"


@dataclass(frozen=True, slots=True)
class CommissionShare:
    """Прибыль до комиссии, разделённая на «осталось» и «ушло на комиссию».

    Проценты — готовым текстом без знака «%»: «76», «0,4», «менее 0,1».
    В сумме всегда 100: округляется одна доля, вторая — дополнение до ста.
    Отрезки полосы — точные доли, без округления.
    """

    kept_percent: str
    fee_percent: str
    kept_part: float
    fee_part: float


#: Знаков после запятой у процентов, по очереди: целые, затем десятые.
#: Следующая точность берётся, только когда ненулевая доля на предыдущей
#: округлилась в ноль — «0 % осталось» при чистом +11 ₽ читается как ложь.
_SHARE_DIGITS = (0, 1)


def _share_texts(fee: float, net: float, gross: float) -> tuple[str, str]:
    """Проценты «осталось» и «ушло» текстом: (осталось, ушло).

    Точный ноль остаётся целым «0». Ненулевая доля нулём не пишется: сначала
    десятые, а если и они ноль — «менее 0,1», вторая доля тогда «более 99,9».
    """
    for digits in _SHARE_DIGITS:
        scale = 10**digits
        fee_units = _half_up(fee * 100 * scale / gross)
        kept_units = 100 * scale - fee_units
        if not (fee > 0 and fee_units == 0) and not (net > 0 and kept_units == 0):
            return fmt_number(kept_units / scale, digits), fmt_number(fee_units / scale, digits)
    digits = _SHARE_DIGITS[-1]
    step = 10.0**-digits
    tiny = f"менее\N{NO-BREAK SPACE}{fmt_number(step, digits)}"
    rest = f"более\N{NO-BREAK SPACE}{fmt_number(100 - step, digits)}"
    # Сюда доходит только доля меньше половины последнего знака: меньшая из двух.
    return (rest, tiny) if fee < net else (tiny, rest)


def commission_share(sums: SpanTotals) -> CommissionShare | None:
    """Доли комиссии и остатка от прибыли **до** комиссии.

    `None`, когда долей нет: прибыль до комиссии не больше нуля или комиссия
    её больше — «ушло 140 %, осталось −40 %» не доли. Тогда карточка пишет
    оба числа строкой (`CardContent.fee_words`).
    """
    gross, fee, net = sums.gross_profit_rub, sums.commission_rub, sums.net_profit_rub
    if gross is None or fee is None or net is None or gross <= 0 or fee > gross:
        return None
    kept_text, fee_text = _share_texts(fee, net, gross)
    return CommissionShare(kept_text, fee_text, net / gross, fee / gross)


@dataclass(frozen=True, slots=True)
class CardContent:
    """Что стоит в карточке итога — готовыми строками. Пустая строка — строки нет.

    Имена полей совпадают с ключами ячеек `PeriodCard`: карточка раскладывает
    их по таблице `_CELLS`, а не перечислением руками.
    """

    title: str
    #: Период без сделок: эта строка вместо всех чисел.
    empty: str = ""
    #: Чистый результат числом; нет его — `net_words` говорят почему.
    net: str = ""
    net_words: str = ""
    #: Знак чистого: −1, 0 или 1 — цвет крупной строки.
    net_sign: int = 0
    trades: str = ""
    counts: str = ""
    commission: str = ""
    gross: str = ""
    average: str = ""
    share: CommissionShare | None = None
    #: Оценка комиссии числами там, где долей нет.
    fee_words: str = ""
    notes: tuple[str, ...] = ()


def _money_or_blank(value: float | None, *, sign: bool = False) -> str:
    return "" if value is None else fmt_money(value, sign=sign)


def _counts_text(totals: PeriodTotals) -> str:
    """«прибыльных 16 · убыточных 6 · 73 % прибыльных»; смесь тарифов — пусто."""
    share = totals.sums.profitable_share
    if totals.winners is None or totals.losers is None or share is None:
        return ""
    return (
        f"прибыльных {totals.winners} · убыточных {totals.losers} · "
        f"{_half_up(share * 100)}\N{NO-BREAK SPACE}% прибыльных"
    )


def _no_net_text(sums: SpanTotals) -> str:
    """Почему чистого результата нет — словами, а не нулём.

    Три случая по убыванию общности: результата нет ни у одной сделки;
    деньги не сложены из-за смеси тарифов; тариф не задан ни у одной.
    """
    if not sums.counted:
        return "Результата нет: прогон не передал его ни по одной сделке периода."
    if sums.gross_profit_rub is None:
        return (
            "Деньги не сложены: тариф комиссии задан не у всех сделок периода, "
            "и чистые результаты лежат вперемешку с результатами до комиссии."
        )
    return (
        "Чистого результата нет: тариф комиссии не задан. "
        "Прибыль до комиссии результатом не считается."
    )


def _fee_words(sums: SpanTotals) -> str:
    """Комиссия и прибыль до комиссии числами — там, где долей нет."""
    gross, fee = sums.gross_profit_rub, sums.commission_rub
    if gross is None or fee is None or commission_share(sums) is not None:
        return ""
    return f"Комиссия {fmt_money(fee)} при прибыли до комиссии {fmt_money(gross, sign=True)}"


def card_content(title: str, totals: PeriodTotals | None) -> CardContent:
    """Итог периода → строки карточки. Ни одного нового расчёта прибыли.

    Все деньги — из `period_totals`, то есть те же столбцы, что в таблице.
    Средний чистый — чистый на число сделок с известным результатом.
    """
    if totals is None or not totals.sums.trades:
        return CardContent(title, empty=NO_PERIOD_TRADES)
    sums = totals.sums
    net = sums.net_profit_rub
    return CardContent(
        title,
        net=_money_or_blank(net, sign=True),
        net_words="" if net is not None else _no_net_text(sums),
        net_sign=0 if net is None else (net > 0) - (net < 0),
        trades=str(sums.trades),
        counts=_counts_text(totals),
        commission=_money_or_blank(sums.commission_rub),
        gross=_money_or_blank(sums.gross_profit_rub, sign=True),
        average=(
            _money_or_blank(net / sums.counted, sign=True)
            if net is not None and sums.counted
            else ""
        ),
        share=commission_share(sums),
        fee_words=_fee_words(sums),
        notes=tuple(_notes(sums)),
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

#: Ячейки карточки: поле `CardContent`, подпись, строка и столбец сетки,
#: подсказка. Таблица, а не пять одинаковых кусков сборки: ячейку добавляет
#: одна строка здесь и одно поле в `CardContent`.
_CELLS: tuple[tuple[str, str, int, int, str], ...] = (
    ("net", "Чистый результат", 0, 0, "Сумма столбца «Результат, ₽» за период."),
    (
        "trades",
        "Сделок",
        0,
        1,
        "Сделок в периоде. Сделка с нулевым результатом — ни прибыльная, "
        "ни убыточная; доля прибыльных — от сделок с известным результатом.",
    ),
    ("commission", "Комиссия", 0, 2, "Сумма столбца «Комиссия, ₽» за период."),
    (
        "gross",
        "Прибыль до комиссии",
        1,
        0,
        "Чистый результат плюс комиссия за период.",
    ),
    (
        "average",
        "Чистый результат на сделку, в среднем",
        1,
        1,
        "Чистый результат периода, делённый на число сделок с известным результатом.",
    ),
)


class PeriodCard(QFrame):
    """Карточка итога периода над таблицей: числа и оговорки к ним, без оценок.

    Содержимое приходит готовым (`card_content`) — карточка только
    раскладывает строки по ячейкам и красит. Цвета — из темы окна.
    """

    def __init__(self, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theme
        self.setObjectName("periodCard")
        self.setStyleSheet(
            f"QFrame#periodCard {{ border: 1px solid {theme.axis}; border-radius: 8px; }}"
        )
        self.title = QLabel()
        self.title.setStyleSheet("font-weight: 600;")
        self.empty = QLabel()
        self.numbers = QWidget()
        grid = QGridLayout(self.numbers)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(24)
        self.cells: dict[str, QWidget] = {}
        self.values: dict[str, QLabel] = {}
        boxes: dict[str, QVBoxLayout] = {}
        for name, caption, row, column, tip in _CELLS:
            boxes[name] = self._cell(name, caption, tip)
            grid.addWidget(self.cells[name], row, column, Qt.AlignmentFlag.AlignTop)
        for column in range(3):
            grid.setColumnStretch(column, 1)
        self.counts = self._dim(QLabel())
        boxes["trades"].addWidget(self.counts)
        self.share_box = self._share_box()
        self.fee_words = QLabel()
        self.fee_words.setWordWrap(True)
        self.notes = self._dim(QLabel())
        self.notes.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        for widget in (
            self.title,
            self.empty,
            self.numbers,
            self.share_box,
            self.fee_words,
            self.notes,
        ):
            layout.addWidget(widget)

    def _dim(self, label: QLabel) -> QLabel:
        label.setStyleSheet(f"color: {self._theme.text_dim};")
        return label

    def _cell(self, name: str, caption: str, tip: str) -> QVBoxLayout:
        """Ячейка: подпись приглушённо, под ней число. Отдаёт раскладку ячейки."""
        cell = QWidget()
        cell.setToolTip(tip)
        box = QVBoxLayout(cell)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(2)
        box.addWidget(self._dim(QLabel(caption)))
        value = QLabel()
        value.setWordWrap(True)
        value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # Число жирнее и крупнее подписи: приглушённый цвет подписи в тёмной
        # теме близок к цвету текста, и различие держит начертание.
        font = value.font()
        font.setBold(True)
        if font.pointSizeF() > 0:
            font.setPointSizeF(font.pointSizeF() * 1.15)
        value.setFont(font)
        box.addWidget(value)
        self.cells[name] = cell
        self.values[name] = value
        return box

    def _share_box(self) -> QWidget:
        """Полоса «осталось | ушло на комиссию» и проценты под её концами."""
        box = QWidget()
        box.setToolTip(
            "Вся полоса — прибыль до комиссии за период. Первая часть — "
            "сколько осталось после комиссии, вторая — сколько ушло на комиссию."
        )
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(3)
        self.kept_part = QFrame()
        self.fee_part = QFrame()
        self.bar = QHBoxLayout()
        self.bar.setSpacing(0)
        for part in (self.kept_part, self.fee_part):
            part.setFixedHeight(10)
            part.setMinimumWidth(0)
            self.bar.addWidget(part)
        self.kept_label = QLabel()
        self.fee_label = QLabel()
        self.fee_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        under = QHBoxLayout()
        under.addWidget(self.kept_label)
        under.addStretch(1)
        under.addWidget(self.fee_label)
        layout.addLayout(self.bar)
        layout.addLayout(under)
        return box

    def show_content(self, content: CardContent) -> None:
        """Разложить готовые строки. Пустое поле — ячейка или строка скрыта."""
        self.title.setText(content.title)
        self.empty.setText(content.empty)
        self.empty.setVisible(bool(content.empty))
        self.numbers.setVisible(not content.empty)
        for name, _caption, _row, _column, _tip in _CELLS:
            text = getattr(content, name)
            self.values[name].setText(text)
            self.cells[name].setVisible(bool(text) and not content.empty)
        self._show_net(content)
        self.counts.setText(content.counts)
        self.counts.setVisible(bool(content.counts))
        self._show_share(content.share if not content.empty else None)
        self.fee_words.setText(content.fee_words)
        self.fee_words.setVisible(bool(content.fee_words) and not content.empty)
        self.notes.setText("\n".join(content.notes))
        self.notes.setVisible(bool(content.notes) and not content.empty)

    def _show_net(self, content: CardContent) -> None:
        """Чистый — крупно и цветом знака; нет его — словами обычным шрифтом."""
        label = self.values["net"]
        if not content.net:
            label.setText(content.net_words)
            label.setStyleSheet(f"color: {self._theme.text}; font-weight: 400;")
            self.cells["net"].setVisible(bool(content.net_words) and not content.empty)
            return
        colour = {1: self._theme.success, -1: self._theme.danger}.get(
            content.net_sign, self._theme.text
        )
        label.setStyleSheet(
            f"color: {colour}; font-weight: 600; font-size: {scaled(16):.1f}pt;"
        )

    def _show_share(self, share: CommissionShare | None) -> None:
        """Полоса из двух отрезков пропорционально долям; долей нет — скрыта."""
        self.share_box.setVisible(share is not None)
        if share is None:
            return
        kept = round(share.kept_part * 1000)
        fee = round(share.fee_part * 1000)
        self.bar.setStretch(0, kept)
        self.bar.setStretch(1, fee)
        self.kept_part.setVisible(kept > 0)
        self.fee_part.setVisible(fee > 0)
        for part, colour, left, right in (
            (self.kept_part, self._theme.success, True, fee == 0),
            (self.fee_part, self._theme.warning, kept == 0, True),
        ):
            part.setStyleSheet(_bar_style(colour, left=left, right=right))
        self.kept_label.setText(f"{share.kept_percent}\N{NO-BREAK SPACE}% осталось")
        self.fee_label.setText(f"{share.fee_percent}\N{NO-BREAK SPACE}% ушло на комиссию")


def _bar_style(colour: str, *, left: bool, right: bool) -> str:
    """Отрезок полосы: скруглены только внешние концы."""
    radius = {True: "5px", False: "0px"}
    return (
        f"background: {colour}; border: none;"
        f" border-top-left-radius: {radius[left]}; border-bottom-left-radius: {radius[left]};"
        f" border-top-right-radius: {radius[right]};"
        f" border-bottom-right-radius: {radius[right]};"
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

        self.card = PeriodCard(self._theme)
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
        layout.addWidget(self.card)
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
        # Вторая строка заголовка — «Считается на: …» склейки (B-069).
        self.title_label.setText(
            "Отчёт по прогону" if run is None
            else "\n".join(line for line in (run_title(run), run.basis) if line)
        )
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
        self._show_card(rows)
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
        # «Сделок в периоде нет» уже сказала карточка — здесь только где они есть.
        return (
            f"Сделки прогона — с {first:%d.%m.%Y} по {last:%d.%m.%Y}, всего {len(rows)}: "
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

    def _show_card(self, rows: Sequence[TradeRow]) -> None:
        """Карточка итога: границы — из полей дат, числа — `period_totals` строк таблицы.

        Прогона или сделок в нём нет — карточки нет, за окно говорит
        `empty_label`. Начало позже конца — тоже нет: «−3 дня» не период.
        """
        since, until = _day_of(self.since_edit), _day_of(self.until_edit)
        if self._run is None or not rows or since > until:
            self.card.setVisible(False)
            return
        title = card_title(self._run.instrument, since, until)
        totals = period_totals(self._shown) if self._shown else None
        self.card.show_content(card_content(title, totals))
        self.card.setVisible(True)

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


def _day_of(edit: QDateEdit) -> date:
    """День из поля даты — без `toPython`, у которого заглушки пишут `object`."""
    day = edit.date()
    return date(day.year(), day.month(), day.day())


def _notes(sums: SpanTotals) -> list[str]:
    """Оговорки к итогу — только наступившие. Пустое — не ноль, а «неизвестно».

    «Тариф не задан» и «деньги не сложены» стоят на месте чистого результата
    (`_no_net_text`), а не здесь: одна фраза дважды в карточке не стоит.
    """
    if 0 < sums.counted < sums.trades:
        return [
            f"Итог посчитан по {sums.counted} сделкам из {sums.trades}: "
            "по остальным прогон результата не передал."
        ]
    return []
