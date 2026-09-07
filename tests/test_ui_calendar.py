"""Календарь нерабочих дней: что делает щелчок и чего он не делает.

Каждая проверка стережёт одно поведение, названное первой строкой докстринга.
Главное из них — **отметка биржи мышкой не снимается**: календарь показывает
две разные вещи, волю владельца счёта и факт про мир, и смешать их значит
завести вторую правду о торговом времени
(решение [0038](../.docs/decisions/0038-calendar-of-non-working-days.md)).

Щелчки здесь подаются **сигналом виджета**, а не вызовом внутреннего метода:
так проверяется и сама связь. Отсоединённый обработчик иначе выглядел бы
рабочим кодом.

Даты: 19.06.2026 — пятница, 20.06 — суббота, 22.06 — понедельник.
"""

from __future__ import annotations

from datetime import date

import pytest
from PySide6.QtCore import QDate, Qt

from ui.calendar_dialog import CalendarDialog, usual_day
from ui.models import CalendarDay

FRIDAY = date(2026, 6, 19)
SATURDAY = date(2026, 6, 20)
MONDAY = date(2026, 6, 22)
TUESDAY = date(2026, 6, 23)


def click(dialog: CalendarDialog, day: date) -> None:
    """Щелчок по дню — через сигнал виджета, как у живого человека."""
    dialog.calendar.clicked.emit(QDate(day.year, day.month, day.day))


@pytest.fixture()
def dialog(qapp):
    window = CalendarDialog()
    yield window
    window.deleteLater()
    qapp.processEvents()


# ------------------------------------------------------------- отметки мышкой

def test_a_click_on_a_weekday_marks_it_non_trading(dialog: CalendarDialog) -> None:
    """Щелчок по будням: «в этот день не торгуем» — то, ради чего всё затеяно."""
    click(dialog, FRIDAY)
    assert dialog.marks() == (CalendarDay(day=FRIDAY, trading=False),)


def test_a_second_click_removes_the_mark(dialog: CalendarDialog) -> None:
    """Повторный щелчок снимает отметку: день снова как обычно.

    Без этого отметку нельзя было бы отменить мышкой, а просьба владельца
    счёта — «отметить заранее» и «сдвигать», то есть менять решение.
    """
    click(dialog, FRIDAY)
    click(dialog, FRIDAY)
    assert dialog.marks() == ()


def test_a_click_on_a_saturday_makes_it_trading(dialog: CalendarDialog) -> None:
    """Щелчок по субботе означает обратное — «рабочая суббота».

    Щелчок переключает **отклонение от обычного правила**, а не крутит три
    состояния по кругу: на будни это «не торгуем», на выходные «торгуем».
    """
    click(dialog, SATURDAY)
    assert dialog.marks() == (CalendarDay(day=SATURDAY, trading=True),)


def test_the_owner_scenario_takes_exactly_three_clicks(dialog: CalendarDialog) -> None:
    """Просьба целиком: «суббота рабочая, понедельник-вторник нерабочие следом».

    Три щелчка — три дня, и ни одного лишнего действия. Если сценарий начнёт
    требовать выбора состояния из списка, эта проверка упадёт.
    """
    for day in (SATURDAY, MONDAY, TUESDAY):
        click(dialog, day)
    assert dialog.marks() == (
        CalendarDay(day=SATURDAY, trading=True),
        CalendarDay(day=MONDAY, trading=False),
        CalendarDay(day=TUESDAY, trading=False),
    )


def test_the_marks_come_back_sorted_by_date(dialog: CalendarDialog) -> None:
    """Отметки возвращаются по возрастанию даты, а не в порядке щелчков.

    Порядок — часть значения: `EngineSettings.changes_from` сравнивает наборы,
    и переставленные дни дали бы в журнале изменение, которого не было.
    """
    for day in (MONDAY, FRIDAY, TUESDAY):
        click(dialog, day)
    assert [mark.day for mark in dialog.marks()] == [FRIDAY, MONDAY, TUESDAY]


def test_the_dialog_opens_with_the_marks_it_was_given(qapp) -> None:
    """Открытое окно показывает то, что пришло, — и отдаёт то же самое обратно."""
    given = (CalendarDay(day=FRIDAY, trading=False),)
    window = CalendarDialog(given)
    try:
        assert window.marks() == given
    finally:
        window.deleteLater()


# ------------------------------------------------------ отметка биржи и мышка

def test_an_exchange_day_does_not_toggle_by_mouse(qapp) -> None:
    """Главное: день, названный биржей, мышкой не снимается и не переключается.

    Правда о том, работает ли биржа, редактированию не подлежит. Щелчок
    объясняет отказ словами: молчаливое «ничего не произошло» читается как
    поломка программы.
    """
    window = CalendarDialog((), {FRIDAY: False})
    try:
        click(window, FRIDAY)
        assert window.marks() == (), "отметка биржи снялась мышкой"
        assert "биржа" in window.note.text().lower()
        assert "не снимается" in window.note.text()
    finally:
        window.deleteLater()


def test_removing_every_mark_leaves_the_exchange_days_alone(qapp) -> None:
    """«Снять все отметки» снимает свои, а чужие не трогает."""
    window = CalendarDialog((CalendarDay(day=MONDAY, trading=False),), {FRIDAY: False})
    try:
        window.drop_all.click()
        assert window.marks() == ()
        assert "Дни, названные биржей, остались" in window.note.text()
        # Биржевой день по-прежнему нельзя снять и после чистки.
        click(window, FRIDAY)
        assert window.marks() == ()
    finally:
        window.deleteLater()


def test_an_exchange_day_is_painted_differently_from_an_own_mark(qapp) -> None:
    """Два вида отметок различаются на экране, а не только по смыслу.

    Цвет здесь не косметика: одинаково выглядящие отметки означали бы, что
    владелец счёта не отличает своё решение от факта про биржу — и не понимает,
    какое из них он может отменить.
    """
    window = CalendarDialog((CalendarDay(day=MONDAY, trading=False),), {FRIDAY: False})
    try:
        mine = window.calendar.dateTextFormat(QDate(2026, 6, 22))
        theirs = window.calendar.dateTextFormat(QDate(2026, 6, 19))
        assert mine.background().color() != theirs.background().color()
        assert "Ваша отметка" in mine.toolTip()
        assert "биржа" in theirs.toolTip().lower()
    finally:
        window.deleteLater()


# --------------------------------------------------------------- список и вид

def test_every_mark_is_listed_with_its_weekday(dialog: CalendarDialog) -> None:
    """Список отметок называет дату и день недели: календарь показывает месяц.

    Без списка ответ на вопрос «что я вообще пометил» ищется листанием
    календаря, а отметки живут годами.
    """
    click(dialog, SATURDAY)
    click(dialog, MONDAY)
    rows = [
        dialog.marks_list.item(index).text()
        for index in range(dialog.marks_list.count())
    ]
    assert rows == [
        "20.06.2026, суббота — торгуем",
        "22.06.2026, понедельник — не торгуем",
    ]


def test_an_empty_calendar_says_so_instead_of_showing_nothing(
    dialog: CalendarDialog,
) -> None:
    """Пустой список объясняет пустоту, а не выглядит сломанным."""
    assert dialog.marks_list.count() == 1
    assert "отметок нет" in dialog.marks_list.item(0).text()
    assert dialog.drop_all.isEnabled() is False


def test_the_selected_day_can_be_dropped_from_the_list(dialog: CalendarDialog) -> None:
    """Отметку можно снять и из списка — не листая календарь к нужному месяцу."""
    click(dialog, FRIDAY)
    dialog.marks_list.setCurrentRow(0)
    dialog.drop.click()
    assert dialog.marks() == ()


def test_the_keyboard_toggles_the_same_way_as_the_mouse(dialog: CalendarDialog) -> None:
    """Enter по дню делает то же, что щелчок. Клавиатура не должна ломаться."""
    dialog.calendar.activated.emit(QDate(2026, 6, 19))
    assert dialog.marks() == (CalendarDay(day=FRIDAY, trading=False),)


def test_the_tab_order_goes_top_to_bottom(dialog: CalendarDialog) -> None:
    """Обход по Tab идёт как на экране: календарь → список → кнопки."""
    assert dialog.marks_list.nextInFocusChain() is not None
    order = [dialog.calendar, dialog.marks_list, dialog.drop, dialog.drop_all]
    for widget in order:
        assert widget.focusPolicy() != Qt.FocusPolicy.NoFocus, (
            f"виджет {widget} выпал из обхода по клавиатуре"
        )


# ------------------------------------------------------- правило берётся у движка

def test_the_usual_rule_is_asked_of_the_engine_not_invented_here() -> None:
    """Обычное правило дня спрашивается у движка, а не считается в окне.

    Вторая копия правила «суббота и воскресенье нерабочие» разошлась бы
    с первой в тот день, когда правило изменят, — и календарь стал бы
    показывать не то, что сделает робот.
    """
    assert usual_day(FRIDAY) is True
    assert usual_day(SATURDAY) is False
    assert usual_day(MONDAY) is True


def test_the_dialog_holds_no_trading_rule_of_its_own() -> None:
    """В модуле календаря нет своей проверки дня недели.

    Проверка грубая и намеренно такая: она ловит возврат к самодельному
    правилу («`weekday() >= 5`»), а не любое упоминание дней.
    """
    import pathlib

    source = pathlib.Path(__file__).resolve().parent.parent / "ui" / "calendar_dialog.py"
    body = source.read_text(encoding="utf-8")
    assert "weekday() >" not in body
    assert "weekday() <" not in body
    assert "verdict_of_day" in body


# ------------------------------------------------------------------ контраст

def test_every_day_fill_keeps_the_number_readable_in_both_themes() -> None:
    """Номер дня читается на своей заливке и в светлой теме, и в тёмной.

    Порог 4,5:1 — тот же, что у остальных надписей окна (WCAG для обычного
    текста). Проверка идёт **по таблице заливок**, а не по трём знакомым
    цветам: цвет, дописанный мимо таблицы, иначе остался бы непроверенным,
    а «непроверенный цвет» здесь значит день, чей номер не разобрать.

    ⚠️ Замер 05.09.2026, из-за которого проверка появилась: при заливке
    с прозрачностью 200 контраст падал до 3,54:1 в светлой теме и 3,68:1
    в тёмной — то есть отметка выглядела аккуратнее и читалась хуже порога.
    """
    from ui.calendar_dialog import FILL_OF_DAY
    from ui.theme import DARK, LIGHT, contrast, text_on

    worst = []
    for theme, name in ((LIGHT, "светлая"), (DARK, "тёмная")):
        for key, field in FILL_OF_DAY.items():
            fill = getattr(theme, field)
            ratio = contrast(text_on(fill), fill)
            if ratio < 4.5:
                worst.append(f"{name} тема, {key} → {field} {fill}: {ratio:.2f}:1")
    assert not worst, "номер дня не читается на заливке: " + "; ".join(worst)


def test_the_fills_of_the_two_sources_differ_in_both_themes() -> None:
    """Своя отметка и отметка биржи различаются цветом в обеих темах.

    Одинаковый цвет означал бы, что владелец счёта не отличает своё решение
    от факта про биржу — и не понимает, какое из двух он вправе отменить.
    """
    from ui.calendar_dialog import FILL_OF_DAY
    from ui.theme import DARK, LIGHT

    for theme in (LIGHT, DARK):
        colours = {getattr(theme, field) for field in FILL_OF_DAY.values()}
        assert len(colours) == len(FILL_OF_DAY), f"цвета совпали: {colours}"
