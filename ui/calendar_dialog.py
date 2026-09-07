"""Календарь нерабочих дней: дни, в которые робот не работает.

Просьба владельца счёта 05.09.2026, дословно: «типа решил, что в этот день
выборы — лучше не трогать, и заранее отметить, чтобы не было движений и заявок
у бота. Вот там можно сразу отметить выходные и там же сдвигать их, если
производственный календарь меняется: суббота рабочая, понедельник-вторник
нерабочие следом».

Две разные вещи на одном календаре
----------------------------------
**Отметка владельца счёта** — его воля. Причина никого не касается, ставится
и снимается мышкой в любой момент.

**День, названный биржей** — факт о мире: праздник, перенос рабочего дня.
Мышкой он не снимается вовсе, и это не строгость интерфейса: правда о том,
работает ли биржа, редактированию не подлежит. Клик по такому дню объясняет,
а не переключает.

Смешать их — значит завести вторую правду о торговом времени. Поэтому у них
разный вид, разная подпись в легенде и разное поведение по клику
(решение [0038](../.docs/decisions/0038-calendar-of-non-working-days.md)).

⚠️ **Здесь нет ни одного торгового правила.** Что значит отметка и кто кого
перебивает, решает `engine/window.py`; окно спрашивает у него готовый ответ
(`verdict_of_day`) и рисует. Вторая реализация правила разошлась бы с первой
молча — и владелец счёта увидел бы в календаре не то, что сделает робот.

Один клик — одно понятное действие
----------------------------------
Клик по дню переключает **отклонение от обычного правила**, а не крутит три
состояния по кругу. На будний день это «не торгуем», на субботу и воскресенье
— «торгуем». Повторный клик снимает отметку. Три состояния по кругу владелец
счёта не помнит, а отклонение он держит в голове само: «этот день не как
всегда».

Сценарий из просьбы — ровно три клика: суббота (стала рабочей), понедельник
и вторник (стали нерабочими).

⚠️ Хранится при этом **готовый ответ** («торгуем» / «не торгуем»), а не
отклонение: см. `ui.models.CalendarDay`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date

from PySide6.QtCore import QDate, QLocale, Qt
from PySide6.QtGui import QColor, QTextCharFormat
from PySide6.QtWidgets import (
    QCalendarWidget,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from engine import verdict_of_day
from ui.models import WEEKDAYS, CalendarDay
from ui.theme import current as current_theme
from ui.theme import text_on

__all__ = ["CalendarDialog", "usual_day"]

#: Чем залит день: чья это отметка и что она означает. Ключ — пара
#: «моя ли отметка, торговый ли день», значение — поле темы.
#:
#: Таблицей, а не четырьмя `if`: по ней идёт и раскраска, и проверка контраста
#: (`tests/test_ui_calendar.py`). Цвет, дописанный мимо таблицы, оказался бы
#: непроверенным — а «непроверенный цвет» здесь значит «день, чей номер
#: не читается на своей заливке в одной из двух тем».
#:
#: ⚠️ Заливка **сплошная**, без прозрачности. Замер 05.09.2026: при alpha 200
#: контраст номера дня падал до 3,54:1 (светлая тема, «торгуем») и 3,68:1
#: (тёмная, «не торгуем») при пороге 4,5:1; на сплошной заливке — от 5,27:1
#: до 7,33:1. Полупрозрачность здесь выглядела аккуратнее и стоила читаемости.
FILL_OF_DAY: dict[tuple[bool, bool], str] = {
    (True, False): "danger",     # моя отметка: не торгуем
    (True, True): "success",     # моя отметка: торгуем
    (False, False): "shade",     # биржа: не работает
    (False, True): "text_dim",   # биржа: рабочий день
}

#: ⚠️ Окно не знает про настройку «торговать в выходные»: поля у неё нет
#: (`app/convert.py::_ENGINE_FROM_BASE`), движок берёт её у прежних настроек,
#: и из окна она недостижима. Поэтому обычное правило здесь спрашивается
#: у движка с выключенной торговлей в выходные — то есть в точности так, как
#: сегодня работает программа. Появится поле — сюда приедет его значение,
#: а правило останется одно и то же.
_TRADE_IN_WEEKEND = False


def usual_day(day: date) -> bool:
    """Торговал бы робот в этот день **без единой отметки**.

    Спрашивается у движка, а не считается здесь: «суббота и воскресенье
    нерабочие» — торговое правило, и вторая его копия в окне разошлась бы
    с первой в день, когда правило изменят.
    """
    return verdict_of_day(day, trade_in_weekend=_TRADE_IN_WEEKEND).trading


class CalendarDialog(QDialog):
    """Месяц с отметками. Возвращает отметки владельца счёта, и только их."""

    def __init__(
        self,
        marks: Iterable[CalendarDay] = (),
        exchange: Mapping[date, bool] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Календарь нерабочих дней")
        self.setModal(True)
        self.resize(560, 620)

        #: Отметки владельца счёта: дата → торгуем ли. Словарь, а не список:
        #: клик спрашивает про один день, и линейный поиск по списку здесь
        #: означал бы разные ответы при случайно задвоенной дате.
        self._marks: dict[date, bool] = {mark.day: mark.trading for mark in marks}
        #: Что сказала биржа. Только чтение: мышкой не снимается.
        self._exchange: dict[date, bool] = dict(exchange or {})

        self._build_widgets()
        self._lay_out()
        self._set_tab_order()
        self._repaint()
        self._say("")

    def _build_widgets(self) -> None:
        """Собрать поля окна. Раскладка — отдельно: так короче каждое из двух."""
        self.calendar = QCalendarWidget()
        # ⚠️ Язык и первый день недели задаются явно. Qt берёт их из системной
        # локали, а машина владельца счёта вполне может стоять в английской:
        # «June 2026» и неделя с воскресенья — это чужой календарь, в котором
        # он промахнётся мимо дня. Интерфейс программы русский целиком, и
        # календарь — не исключение.
        self.calendar.setLocale(QLocale("ru_RU"))
        self.calendar.setFirstDayOfWeek(Qt.DayOfWeek.Monday)
        self.calendar.setGridVisible(True)
        self.calendar.setVerticalHeaderFormat(
            QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader
        )
        self.calendar.clicked.connect(self._toggle)
        # Enter и двойной щелчок — тот же переключатель. Владелец счёта
        # пользуется мышкой, но клавиатура ломаться не должна.
        self.calendar.activated.connect(self._toggle)

        self.note = QLabel()
        self.note.setWordWrap(True)

        self.marks_list = QListWidget()
        self.marks_list.setAlternatingRowColors(True)
        self.marks_list.itemSelectionChanged.connect(self._sync_buttons)
        self.marks_list.itemActivated.connect(self._drop_selected)

        self.drop = QPushButton("Снять отметку с выбранного дня")
        self.drop.clicked.connect(self._drop_selected)
        self.drop_all = QPushButton("Снять все отметки")
        self.drop_all.clicked.connect(self._drop_all)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("ОК")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

    def _lay_out(self) -> None:
        """Разложить собранное сверху вниз, как читают глазами."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(self._hint())
        layout.addWidget(self.calendar, 1)
        layout.addWidget(self._legend())
        layout.addWidget(self.note)
        layout.addWidget(QLabel("Отмеченные дни:"))
        layout.addWidget(self.marks_list, 1)
        row = QHBoxLayout()
        row.addWidget(self.drop)
        row.addWidget(self.drop_all)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addWidget(self.buttons)

    # ------------------------------------------------------------------ обмен

    def marks(self) -> tuple[CalendarDay, ...]:
        """Отметки владельца счёта, по возрастанию даты. Дней биржи здесь нет."""
        return tuple(
            CalendarDay(day=day, trading=trading)
            for day, trading in sorted(self._marks.items())
        )

    def set_exchange(self, days: Mapping[date, bool]) -> None:
        """Дни, названные биржей. Приходят снаружи и мышкой не меняются.

        ⚠️ Сегодня их не приносит никто: расписание брокера отвечает только
        про сегодняшний день, и проводка живого ответа в окно не сделана
        (`F-002`). Метод существует, потому что вид у этих дней отдельный
        и проверен сторожами, — а не потому, что он уже работает.
        """
        self._exchange = dict(days)
        self._repaint()

    # ------------------------------------------------------------- поведение

    def _toggle(self, when: QDate) -> None:
        """Щелчок по дню: поставить отметку, снять её или объяснить отказ."""
        # ⚠️ Дата собирается из трёх чисел, а не берётся у `QDate.toPython()`.
        # Тот в разных сборках Qt отдаёт то `date`, то `datetime`, а `datetime`
        # в наборе отметок не совпадёт ни с одной датой: отметка выглядела бы
        # поставленной и молча не работала (`engine.DayMarks`).
        day = date(when.year(), when.month(), when.day())
        named = f"{day:%d.%m.%Y}, {WEEKDAYS[day.weekday()]}"
        exchange = self._exchange.get(day)
        if exchange is not None:
            self._say(
                f"{named}: так сказала биржа, и мышкой это не снимается. "
                + (
                    "Биржа в этот день не работает — заявок не будет, даже если "
                    "пометить день торговым."
                    if not exchange
                    else "Биржа в этот день работает."
                ),
                alarming=not exchange,
            )
            return
        if day in self._marks:
            del self._marks[day]
            self._say(f"{named}: отметка снята, день как обычно.")
        else:
            self._marks[day] = not usual_day(day)
            self._say(
                f"{named}: "
                + (
                    "теперь торговый день."
                    if self._marks[day]
                    else "робот в этот день не торгует — ни входов, ни заявок."
                )
            )
        self._repaint()

    def _drop_selected(self, *_: object) -> None:
        """Снять отметку с выбранной строки списка."""
        # `selectedItems()`, а не `currentItem()`: второй по стабам Qt никогда
        # не `None`, хотя на пустом списке он именно `None`, — и проверка
        # на пустоту выглядела бы недостижимой веткой.
        chosen = self.marks_list.selectedItems()
        if not chosen:
            return
        day = chosen[0].data(Qt.ItemDataRole.UserRole)
        # Строка-заглушка «отметок нет» даты не несёт, и снимать по ней нечего.
        if isinstance(day, date) and day in self._marks:
            del self._marks[day]
            self._say(f"{day:%d.%m.%Y}: отметка снята, день как обычно.")
            self._repaint()

    def _drop_all(self, *_: object) -> None:
        """Снять все отметки разом. Дни биржи не трогает — они не наши."""
        if not self._marks:
            return
        count = len(self._marks)
        self._marks.clear()
        self._say(f"Снято отметок: {count}. Дни, названные биржей, остались.")
        self._repaint()

    # ---------------------------------------------------------------- отрисовка

    def _repaint(self) -> None:
        """Заново раскрасить дни и перебрать список отметок.

        Прежняя раскраска снимается целиком: `QCalendarWidget` держит форматы
        дат до тех пор, пока их не отменят, и снятая отметка иначе осталась бы
        на экране цветной.
        """
        self.calendar.setDateTextFormat(QDate(), QTextCharFormat())
        theme = current_theme()
        # Порядок важен: своя отметка рисуется поверх биржевой. Так их и видно
        # — но щелчок по такому дню всё равно объяснит, что решает биржа.
        for mine, days in ((False, self._exchange), (True, self._marks)):
            for day, trading in days.items():
                self._paint(
                    day,
                    getattr(theme, FILL_OF_DAY[(mine, trading)]),
                    self._tip(mine=mine, trading=trading),
                )
        self._fill_list()
        self._sync_buttons()

    @staticmethod
    def _tip(*, mine: bool, trading: bool) -> str:
        """Подпись при наведении: чья отметка, что значит и снимается ли."""
        if mine:
            what = "торгуем" if trading else "не торгуем"
            return f"Ваша отметка: {what}. Щелчок по дню снимает её."
        what = "день рабочий" if trading else "биржа не работает"
        return f"Так сказала биржа: {what}. Мышкой не снимается."

    def _paint(self, day: date, fill: str, tip: str) -> None:
        """Залить один день. Цвет надписи считается по заливке, а не белым."""
        look = QTextCharFormat()
        look.setBackground(QColor(fill))
        look.setForeground(QColor(text_on(fill)))
        look.setToolTip(tip)
        self.calendar.setDateTextFormat(QDate(day.year, day.month, day.day), look)

    def _fill_list(self) -> None:
        """Список отметок: то же самое словами и с днём недели.

        Список нужен не для красоты: календарь показывает один месяц, а отметки
        живут годами. Без списка «что я вообще пометил» проверяется листанием.
        """
        self.marks_list.clear()
        for mark in self.marks():
            item = QListWidgetItem(mark.label)
            item.setData(Qt.ItemDataRole.UserRole, mark.day)
            self.marks_list.addItem(item)
        if not self._marks:
            empty = QListWidgetItem("отметок нет — робот работает по обычному правилу")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.marks_list.addItem(empty)

    def _say(self, text: str, *, alarming: bool = False) -> None:
        """Строка под календарём: что сделал последний щелчок."""
        theme = current_theme()
        self.note.setText(text or " ")
        colour = theme.danger if alarming else theme.text_dim
        weight = "bold" if alarming else "normal"
        self.note.setStyleSheet(f"color: {colour}; font-weight: {weight};")

    def _sync_buttons(self) -> None:
        """Кнопки снятия отметок гаснут, когда снимать нечего."""
        chosen = self.marks_list.selectedItems()
        self.drop.setEnabled(
            bool(chosen) and isinstance(chosen[0].data(Qt.ItemDataRole.UserRole), date)
        )
        self.drop_all.setEnabled(bool(self._marks))

    # -------------------------------------------------------------- обвязка

    def _hint(self) -> QLabel:
        label = QLabel(
            "Щёлкните по дню, чтобы отметить его не таким, как обычно: будний "
            "день станет нерабочим, суббота или воскресенье — рабочим. "
            "Повторный щелчок снимает отметку. В нерабочий день робот "
            "не входит в рынок и не подаёт заявок, а открытая позиция "
            "закрывается накануне, по концу торгового окна. Отметки действуют "
            "и при проверке на истории. Всё время — московское."
        )
        label.setWordWrap(True)
        return label

    def _legend(self) -> QFrame:
        """Что означает каждый цвет. Без легенды цвет — загадка, а не сведение."""
        frame = QFrame()
        row = QHBoxLayout(frame)
        row.setContentsMargins(0, 0, 0, 0)
        theme = current_theme()
        for colour, caption in (
            (theme.danger, "не торгуем (ваша отметка)"),
            (theme.success, "торгуем (ваша отметка)"),
            (theme.shade, "биржа не работает — не снимается"),
        ):
            chip = QLabel(" ")
            chip.setFixedWidth(18)
            chip.setStyleSheet(f"background: {colour}; border: 1px solid {theme.axis};")
            row.addWidget(chip)
            row.addWidget(QLabel(caption))
            row.addSpacing(12)
        row.addStretch(1)
        return frame

    def _set_tab_order(self) -> None:
        """Обход по Tab сверху вниз: календарь → список → кнопки → ОК."""
        order: list[QWidget] = [
            self.calendar, self.marks_list, self.drop, self.drop_all, self.buttons,
        ]
        for previous, following in zip(order, order[1:], strict=False):
            QWidget.setTabOrder(previous, following)
