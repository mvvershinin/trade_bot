"""Окно боя: кнопка, строка рядом, позиция «факта», вопросы, подписи слоёв.

Э4 2в, второй заход окна (план `.docs/plans/E4-live-orders.md` §12). Что
стережётся, словами:

* бой недоступен — кнопка погашена, причина видна **строкой** рядом, а не
  только в подсказке;
* нажатие без согласия в вопросе `set_live` не зовёт;
* кнопка следует снимку порта, а не нажатию: нажатая с согласием, она
  остаётся отжатой до снимка, а отказ разжимает её;
* выключение боя при позиции «факта» спрашивает, без позиции — нет;
* закрытие окна при позиции «факта» спрашивает, без позиции — нет;
* «Стоп» в бою при позиции «факта» — вопрос выключения боя (в бою «Стоп»
  выключает бой, решение 0066); «Выключен» — вопрос про застывший уровень,
  который программа сторожит, пока открыто окно и есть связь; «Отмена»
  не меняет ничего; без позиции «факта» — прежний вопрос по `RobotState`;
* шапка показывает позицию «факта»: сторону, объём, цену входа, уровень;
* после остановки боя строка говорит «Бой остановлен» с причиной, позиция
  у брокера видна, «Выключить бой» и закрытие окна спрашивают по ней (R4);
* в бою прогон на отрезке истории недоступен с подсказкой, вне боя — доступен (R5);
* слова порта о тейке (`take_note`) стоят вместо «уровня нет» в шапке
  и вместо «тейка не знает» в вопросах: «неизвестно» не становится «нет» (Н7);
* в бою загрузка истории погашена и не запускается, вне боя — доступна (Н8);
* оговорка порта о позиции (`position_note`) видна под позицией в шапке,
  целиком — в подсказке, и стоит в вопросах; без уровня тейка вопросы
  живой тейк не обещают;
* подпись слоя «Факт», легенда и вкладки журнала — по данным
  (`fact_origin`, `origin` строки), а не по режиму окна (`D-172`);
* подсказки шести полей «Бой» говорят, когда значение действует.

Снимок — настоящий `app.fact.LiveStatus`: окно читает его через протокол
`ui.models.LiveView`, и функция `_as_view` ниже — проверка `mypy`, что класс
порта протоколу отвечает.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import pytest
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QDialog, QMessageBox, QProgressDialog, QWidget

from app.fact import FactPosition, LiveStatus
from engine import Side as EngineSide
from market import redact
from ui.journals import REAL_MONEY_MARK, JournalTabs
from ui.legend import FACT_MEANING
from ui.live_control import (
    LIVE_OFF_LABEL,
    LIVE_OFF_TAIL,
    LIVE_ON_LABEL,
    LIVE_UNGUARDED,
    LIVE_UNMANAGED,
    POSITION_UNREAD,
    TAKE_MAY_STAY,
    TAKE_NONE,
)
from ui.main_window import (
    BACKTEST_HINT,
    BACKTEST_IN_COMBAT,
    HISTORY_HINT,
    HISTORY_IN_COMBAT,
    MainWindow,
)
from ui.models import (
    FIELD_CAPTIONS,
    DecisionLevel,
    DecisionRow,
    HistoryFacts,
    HistoryLoadOutcome,
    HistoryLoadRequest,
    LiveView,
    MarkerKind,
    Mode,
    Position,
    RobotState,
    RunOrigin,
    Settings,
    Side,
)
from ui.ports import TerminalPort
from ui.settings_dialog import LIVE_WHEN, SettingsDialog

#: Две причины недоступности — дословно в том виде, в каком их пишет порт.
NO_STREAM = "Нет потока котировок: бой идёт поверх живого потока, сначала «Подключиться»."
NO_STEP = "Шаг цены неизвестен: биржа его ещё не сообщила."

LONG = FactPosition(side=EngineSide.LONG, volume=1.0, entry_price=2850.0, take_level=2864.0)
SHORT = FactPosition(side=EngineSide.SHORT, volume=2.0, entry_price=2900.0)


def _as_view(status: LiveStatus) -> LiveView:
    """Проверка для `mypy`: снимок порта отвечает протоколу окна."""
    return status


class LivePort(TerminalPort):
    """Порт, который запоминает команды боя."""

    def __init__(self) -> None:
        super().__init__()
        self.live: list[bool] = []
        self.modes: list[Mode] = []
        self.stops = 0
        self.backtests = 0
        self.histories: list[str] = []
        self.loads: list[HistoryLoadRequest] = []

    def set_live(self, on: bool) -> None:
        self.live.append(on)

    def set_mode(self, mode: Mode) -> None:
        self.modes.append(mode)

    def stop(self) -> None:
        self.stops += 1

    def request_backtest_options(self) -> None:
        self.backtests += 1

    def request_history_facts(self, symbol: str) -> None:
        self.histories.append(symbol)

    def load_history(self, request: HistoryLoadRequest) -> None:
        self.loads.append(request)


class AgreedHistory(QDialog):
    """Окно загрузки, которое человек сразу подтвердил: модальное ждало бы его."""

    def __init__(self, _facts: object, _settings: object, parent: QWidget) -> None:
        super().__init__(parent)

    def exec(self) -> int:
        return QDialog.DialogCode.Accepted.value

    def request(self) -> HistoryLoadRequest:
        return HistoryLoadRequest(symbol="MXZ6")


class Asked:
    """Подмена вопроса окна: что спросили и что ответить."""

    def __init__(self) -> None:
        self.answer = False
        self.questions: list[tuple[str, str, str]] = []

    def __call__(self, title: str, text: str, yes: str) -> bool:
        self.questions.append((title, text, yes))
        return self.answer


@pytest.fixture
def setup(qapp, monkeypatch) -> Iterator[tuple[MainWindow, LivePort, Asked]]:
    port = LivePort()
    window = MainWindow(port=port, sanitize=redact)
    asked = Asked()
    # Модальное окно вопроса ждало бы человека вечно — подменяется вопрос.
    monkeypatch.setattr(window, "ask", asked)
    yield window, port, asked
    # Позиция снимается до закрытия: иначе закрытие спросило бы человека.
    window.apply_state(RobotState())
    asked.answer = True
    window.close()
    window.deleteLater()


def _show(window: MainWindow, status: LiveStatus) -> None:
    window.show_live(_as_view(status))


def _combat(
    position: FactPosition | None = None,
    *,
    held: str = "",
    origin: RunOrigin = RunOrigin.LIVE,
) -> LiveStatus:
    """Снимок включённого и прогретого боя."""
    return LiveStatus(
        available=False, wanted=True, on=True, armed=True,
        held=held, position=position, fact_origin=origin,
    )


# ------------------------------------------------------------- доступность


def test_unavailable_combat_turns_the_button_off_and_says_why_in_a_line(setup) -> None:
    """Погашенная кнопка без строки — тупик: причина обязана быть видна сразу."""
    window, _port, _asked = setup
    _show(window, LiveStatus(available=False, why_not=(NO_STREAM, NO_STEP)))

    assert not window.live_action.isEnabled()
    assert not window.live_note.isHidden(), "строки рядом с кнопкой нет"
    assert window.live_note.text().startswith("Бой недоступен, причин 2: Нет потока"), (
        window.live_note.text()
    )
    for reason in (NO_STREAM, NO_STEP):
        assert reason in window.live_action.toolTip(), window.live_action.toolTip()


def test_before_the_first_snapshot_the_button_is_off_and_says_so(qapp) -> None:
    """Порт без боя (`DetachedPort`) снимка не шлёт — кнопка не обещает ничего."""
    window = MainWindow(sanitize=redact)
    try:
        assert not window.live_action.isEnabled()
        assert window.live_note.text(), "до снимка строка пуста — молчание"
    finally:
        window.close()
        window.deleteLater()


# ------------------------------------------------------------- включение


def test_pressing_without_agreement_does_not_call_set_live(setup) -> None:
    """Вопрос перед боем обязателен, и «Отмена» — это отмена."""
    window, port, asked = setup
    _show(window, LiveStatus(available=True))
    asked.answer = False

    window.live_action.trigger()

    assert len(asked.questions) == 1, "включение боя без вопроса"
    title, text, yes = asked.questions[0]
    assert text.startswith("Бой: заявки уйдут брокеру на реальный счёт"), text
    assert yes == "Включить бой"
    assert port.live == [], "бой включён без согласия"
    assert not window.live_action.isChecked(), "кнопка нажата без согласия"


def test_the_button_follows_the_snapshot_and_not_the_press(setup) -> None:
    """Согласие — команда порту; нажатой кнопку делает снимок, отказ разжимает."""
    window, port, asked = setup
    _show(window, LiveStatus(available=True))
    asked.answer = True

    window.live_action.trigger()
    assert port.live == [True]
    assert not window.live_action.isChecked(), "кнопка нажата раньше ответа порта"

    _show(window, LiveStatus(available=True, wanted=True))
    assert window.live_action.isChecked()
    assert window.live_action.text() == LIVE_OFF_LABEL

    refusal = "У брокера позиция 1, а у робота нет: новый бой — только при нулевой."
    _show(window, LiveStatus(available=True, refused=refusal))
    assert not window.live_action.isChecked(), "отказ не разжал кнопку"
    assert window.live_action.text() == LIVE_ON_LABEL
    assert window.live_note.text().startswith("Бой не включён"), window.live_note.text()
    assert refusal in window.live_note.toolTip()


def test_held_rebuild_is_said_in_words(setup) -> None:
    """Отложенная пересборка — словами рядом с кнопкой, а не молча."""
    window, _port, _asked = setup
    held = "у «факта» открыта позиция."
    _show(window, _combat(LONG, held=held))
    assert "пересборка отложена" in window.live_note.text()
    assert held in window.live_note.toolTip()


# ------------------------------------------------------------- выключение


def test_turning_combat_off_with_a_fact_position_asks_first(setup) -> None:
    """Выключение при позиции «факта» — вопрос; «Отмена» оставляет бой."""
    window, port, asked = setup
    _show(window, _combat(LONG))
    assert window.live_action.isChecked() and window.live_action.isEnabled()

    asked.answer = False
    window.live_action.trigger()
    assert port.live == [], "бой выключен без согласия при открытой позиции"
    assert LIVE_OFF_TAIL in asked.questions[0][1]

    asked.answer = True
    window.live_action.trigger()
    assert port.live == [False]


def test_turning_combat_off_without_a_position_does_not_ask(setup) -> None:
    window, port, asked = setup
    _show(window, _combat())
    window.live_action.trigger()
    assert asked.questions == []
    assert port.live == [False]


# ------------------------------------------------------------- закрытие окна


def test_closing_with_a_fact_position_asks_and_cancel_keeps_the_window(setup) -> None:
    """С8: закрытие окна при позиции «факта» — вопрос про стоп, «Отмена» — окно живо.

    Вопрос привязан к позиции «факта» из снимка боя, а не к `RobotState`:
    `RobotState.position` здесь пуст.
    """
    window, _port, asked = setup
    _show(window, _combat(SHORT))
    asked.answer = False

    event = QCloseEvent()
    window.closeEvent(event)

    assert len(asked.questions) == 1, "окно закрывается при позиции «факта» без вопроса"
    title, text, yes = asked.questions[0]
    assert LIVE_UNGUARDED in text and "трейлинг" in text, text
    assert "Шорт 2" in text, text
    assert yes == "Закрыть окно"
    assert not event.isAccepted(), "«Отмена» закрыла окно"

    asked.answer = True
    event = QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted()


def test_closing_without_a_fact_position_does_not_ask(setup) -> None:
    window, _port, asked = setup
    _show(window, _combat())
    event = QCloseEvent()
    window.closeEvent(event)
    assert asked.questions == []
    assert event.isAccepted()


# ------------------------------------------------------------- «Стоп» и «Выключен»

#: Робот работает в перевороте. Позиции в `RobotState` нет нарочно: вопрос
#: в бою обязан идти по позиции «факта» из снимка боя, а не по нему.
WORKING = RobotState(mode=Mode.REVERSE, running=True)


def _choose(window: MainWindow, mode: Mode) -> None:
    window.mode_box.setCurrentIndex(window.mode_box.findData(mode))


def test_mode_off_in_combat_asks_about_the_broker_position_and_cancel_keeps_the_mode(
    setup,
) -> None:
    window, port, asked = setup
    window.apply_state(WORKING)
    _show(window, _combat(LONG))

    _choose(window, Mode.OFF)

    assert len(asked.questions) == 1, "«Выключен» в бою при позиции без вопроса"
    _title, text, yes = asked.questions[0]
    assert LIVE_UNMANAGED in text and "Лонг 1" in text, text
    assert "продолжает сторожить" in text and "аварийного стопа" in text.lower(), text
    assert yes == "Выключить робота"
    assert port.modes == [], "«Отмена» переключила режим"
    assert window.mode_box.currentData() is Mode.REVERSE, "список не вернулся к режиму"

    asked.answer = True
    _choose(window, Mode.OFF)
    assert port.modes == [Mode.OFF]


def test_stop_in_combat_asks_as_turning_combat_off_and_cancel_does_nothing(setup) -> None:
    """В бою «Стоп» выключает бой — вопрос и кнопка говорят именно это."""
    window, port, asked = setup
    window.apply_state(WORKING)
    _show(window, _combat(SHORT))

    window.stop_action.trigger()

    assert len(asked.questions) == 1, "«Стоп» в бою при позиции без вопроса"
    _title, text, yes = asked.questions[0]
    assert text.count("«Стоп» в бою выключает бой") == 1, text
    assert LIVE_OFF_TAIL in text and "Шорт 2" in text, text
    assert LIVE_UNMANAGED not in text, "«Стоп» описан как режим «Выключен»"
    assert yes == "Выключить бой"
    assert port.stops == 0, "«Отмена» остановила робота"

    asked.answer = True
    window.stop_action.trigger()
    assert port.stops == 1


def test_without_a_fact_position_stop_and_off_ask_the_old_question(setup, monkeypatch) -> None:
    """Бой выключен — прежний вопрос по `RobotState.position`, новый не задаётся."""
    window, port, asked = setup
    warned: list[str] = []

    def refuse(_parent, _title, text, *_args, **_kwargs):
        warned.append(text)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(refuse))
    _show(window, LiveStatus(available=True))
    window.apply_state(RobotState(
        mode=Mode.REVERSE, running=True,
        position=Position(side=Side.LONG, volume=1.0, entry_price=2850.0),
    ))

    _choose(window, Mode.OFF)
    window.stop_action.trigger()

    assert asked.questions == [], "без боя задан вопрос про позицию у брокера"
    assert len(warned) == 2, f"прежний вопрос задан {len(warned)} раз вместо двух"
    assert all(
        LIVE_UNMANAGED not in text and LIVE_OFF_TAIL not in text for text in warned
    )
    assert port.modes == [] and port.stops == 0


# ------------------------------------------------------------- шапка


def test_the_header_shows_the_fact_position_only_in_combat(setup) -> None:
    window, _port, _asked = setup
    cell = window.status_panel.fact

    _show(window, LiveStatus(available=True))
    assert cell.isHidden(), "позиция у брокера показана без боя"

    _show(window, _combat(LONG))
    assert not cell.isHidden()
    text = cell.value.text()
    for part in ("Лонг", "1", "2\N{NO-BREAK SPACE}850", "уровень", "2\N{NO-BREAK SPACE}864"):
        assert part in text, f"{part!r} нет в «{text}»"

    _show(window, _combat())
    assert cell.value.text() == "Нет позиции"


# ------------------------------------------------------------- прогноз и факт


def test_the_fact_layer_is_named_by_the_data_and_not_by_the_mode(setup) -> None:
    """Подпись «Факт» и легенда — из `fact_origin` снимка, а не из режима окна.

    Снимок с `fact_origin=PAPER` при включённом «факте» нарочно противоречит
    «бою»: подпись обязана сказать то, что в данных, — иначе она
    выводится из режима, а не берётся из данных.
    """
    window, _port, _asked = setup
    chart = window.chart

    _show(window, _combat())
    assert RunOrigin.LIVE.label in chart.fact_box.text()
    texts = [text.text() for _icon, text in chart.legend.rows]
    assert any(FACT_MEANING[RunOrigin.LIVE] in text for text in texts), texts
    assert not any(MarkerKind.WAITING.label in text for text in texts), (
        "в бою легенда обещает «исполнится по открытию следующей свечи» (D-172)"
    )
    assert any("Прогноз" in text for text in texts), "в легенде нет «Прогноза»"

    _show(window, _combat(origin=RunOrigin.PAPER))
    assert RunOrigin.PAPER.label in chart.fact_box.text()
    assert RunOrigin.LIVE.label not in chart.fact_box.text()

    _show(window, LiveStatus(available=True))
    assert chart.fact_box.text() == "Факт", "без боя подпись утверждает происхождение"


def _row(origin: RunOrigin | None) -> DecisionRow:
    return DecisionRow(
        time=datetime(2026, 11, 2, 10, 5), level=DecisionLevel.INFO,
        event="Вход", reason="сигнал", origin=origin,
    )


def test_journal_tabs_mark_real_money_rows_by_their_origin(qapp) -> None:
    """Вкладка с боевыми строками помечена — по `origin` строки, не по режиму."""
    tabs = JournalTabs()
    try:
        tabs.set_decisions([_row(RunOrigin.PAPER)])
        assert tabs.tabs.tabText(1) == "Решения робота"
        tabs.append_decision(_row(RunOrigin.LIVE))
        assert tabs.tabs.tabText(1) == "Решения робота" + REAL_MONEY_MARK
        assert tabs.tabs.tabText(0) == "Сделки", "пометка ушла на чужую вкладку"
        tabs.set_decisions([_row(None)])
        assert tabs.tabs.tabText(1) == "Решения робота"
    finally:
        tabs.deleteLater()


# ------------------------------------------------------------- подсказки


def test_every_live_field_hint_says_when_the_value_takes_effect(qapp) -> None:
    """Пределы действуют со следующей сборки исполнителя — это сказано у каждого поля."""
    dialog = SettingsDialog(Settings())
    try:
        page = dialog.page_of("Бой")
        assert page is not None
        for name in (
            "live_stale_after_sec", "live_event_wait_sec", "live_fill_wait_sec",
            "live_cancel_wait_sec", "live_position_settle_sec", "live_reconnect_pauses_sec",
        ):
            widget = getattr(dialog, name)
            assert LIVE_WHEN in widget.toolTip(), FIELD_CAPTIONS[name]
        assert "включения боя" in LIVE_WHEN and "«Возобновить" in LIVE_WHEN
    finally:
        dialog.deleteLater()


# ------------------------------------------------------------- после остановки (R4)

HALT = "Исход заявки у брокера неизвестен: проверьте позицию в приложении брокера."


def _halted(position: FactPosition | None = LONG) -> LiveStatus:
    """Бой остановлен: просили включить, «факт» не собран, позиция у брокера осталась."""
    return LiveStatus(available=False, wanted=True, halted=HALT, position=position)


def test_after_a_halt_the_line_says_halted_and_the_position_stays_in_sight(setup) -> None:
    """Остановка — не «Бой включается…»; позиция у брокера не прячется."""
    window, _port, _asked = setup
    _show(window, _halted())

    assert window.live_note.text().startswith("Бой остановлен"), window.live_note.text()
    assert HALT in window.live_note.toolTip()
    cell = window.status_panel.fact
    assert not cell.isHidden(), "позиция у брокера скрыта после остановки боя"
    assert "Лонг 1" in cell.value.text(), cell.value.text()


def test_after_a_halt_turning_combat_off_and_closing_ask_by_the_position(setup) -> None:
    window, port, asked = setup
    _show(window, _halted())

    window.live_action.trigger()
    assert len(asked.questions) == 1, "«Выключить бой» при позиции у брокера без вопроса"
    assert "Лонг 1" in asked.questions[0][1] and LIVE_OFF_TAIL in asked.questions[0][1]
    assert port.live == [], "«Отмена» выключила бой"

    event = QCloseEvent()
    window.closeEvent(event)
    assert len(asked.questions) == 2, "закрытие при позиции у брокера без вопроса"
    assert LIVE_UNGUARDED in asked.questions[1][1]
    assert not event.isAccepted()


# ------------------------------------------------------------- прогон на отрезке (R5)


@pytest.mark.parametrize("status", [_combat(), LiveStatus(available=False, wanted=True)],
                         ids=["running", "starting"])
def test_the_history_run_is_off_in_combat_with_a_hint(setup, status: LiveStatus) -> None:
    """Владелец: прогон на отрезке «недоступен в бою»; бой при этом не трогается."""
    window, port, _asked = setup
    _show(window, status)

    assert not window.backtest_action.isEnabled()
    assert window.backtest_action.toolTip() == BACKTEST_IN_COMBAT
    window.open_backtest()
    assert port.backtests == 0, "прогон на отрезке запрошен в бою"
    assert port.live == [], "попытка прогона тронула бой"


def test_the_history_run_is_back_when_combat_is_off(setup) -> None:
    window, port, _asked = setup
    _show(window, _combat())
    _show(window, LiveStatus(available=True))

    assert window.backtest_action.isEnabled()
    assert window.backtest_action.toolTip() == BACKTEST_HINT
    window.open_backtest()
    assert port.backtests == 1


# ------------------------------------------------------------- оговорка о позиции

NOTE = (
    "на момент остановки 14:05 МСК — у брокера могло измениться; по брокеру, "
    "а не по роботу: робот считал лонг 2"
)
#: Что тейк обещан безусловно — слова прежнего текста, которых быть не должно.
PROMISED = "Неподвижный тейк — лимитная заявка у брокера — остаётся"


def test_a_stale_position_carries_its_note_under_it_in_the_header(setup) -> None:
    window, _port, _asked = setup
    _show(window, LiveStatus(
        available=False, wanted=True, halted=HALT, position=LONG, position_note=NOTE,
    ))
    note = window.status_panel.fact_note
    assert not note.isHidden(), "оговорки о позиции в шапке нет"
    assert note.text().startswith("на момент остановки"), note.text()
    assert note.toolTip() == NOTE
    assert NOTE in window.status_panel.fact.value.toolTip()


def test_a_fresh_position_has_no_note(setup) -> None:
    window, _port, _asked = setup
    _show(window, _combat(LONG))
    assert window.status_panel.fact_note.isHidden()
    assert "Лонг 1" in window.status_panel.fact.value.text()


def test_the_questions_carry_the_note(setup) -> None:
    window, _port, asked = setup
    window.apply_state(WORKING)  # «Стоп» доступен только работающему роботу
    _show(window, LiveStatus(
        available=False, wanted=True, halted=HALT, position=LONG, position_note=NOTE,
    ))
    window.live_action.trigger()
    window.stop_action.trigger()
    window.closeEvent(QCloseEvent())
    assert len(asked.questions) == 3, asked.questions
    for title, text, _yes in asked.questions:
        assert NOTE in text, f"«{title}»: оговорки о позиции в вопросе нет"


@pytest.mark.parametrize(
    ("position", "take"),
    [(SHORT, TAKE_NONE), (LONG, TAKE_MAY_STAY.format(level="2\N{NO-BREAK SPACE}864,00"))],
    ids=["no-level", "with-level"],
)
def test_the_questions_promise_a_take_only_when_there_is_a_level(
    setup, position: FactPosition, take: str
) -> None:
    """Без уровня — «рассчитывать на тейк нельзя», а не «тейк остаётся»."""
    window, _port, asked = setup
    window.apply_state(WORKING)
    _show(window, _combat(position))
    window.live_action.trigger()
    window.stop_action.trigger()
    _choose(window, Mode.OFF)
    window.closeEvent(QCloseEvent())
    assert len(asked.questions) == 4, asked.questions
    for title, text, _yes in asked.questions:
        assert take in text, f"«{title}»: о тейке сказано не то:\n{text}"
        assert PROMISED not in text, f"«{title}»: тейк обещан безусловно"


# ------------------------------------------------------------- позиция не прочитана

UNREAD = (
    "у брокера не прочитана на момент остановки 14:05 МСК — проверьте позицию "
    "в приложении брокера"
)


def _unread() -> LiveStatus:
    return LiveStatus(available=False, wanted=True, halted=HALT, position_note=UNREAD)


def test_an_unread_position_shows_the_cell_with_its_note(setup) -> None:
    """Позиции нет, а оговорка есть — ячейка видна: «не прочитана» и оговорка."""
    window, _port, _asked = setup
    _show(window, _unread())
    cell = window.status_panel.fact
    assert not cell.isHidden(), "непрочитанная позиция у брокера спрятана"
    assert cell.value.text() == "не прочитана"
    # Подсказка ячейки — оговорка целиком: строка под ячейкой обрезана по ширине.
    assert UNREAD in cell.value.toolTip(), cell.value.toolTip()
    note = window.status_panel.fact_note
    assert not note.isHidden() and note.toolTip() == UNREAD


def test_no_position_and_no_note_after_a_halt_hide_the_cell(setup) -> None:
    window, _port, _asked = setup
    _show(window, LiveStatus(available=False, wanted=True, halted=HALT))
    assert window.status_panel.fact.isHidden()
    assert window.status_panel.fact_note.isHidden()


def test_an_unread_position_is_asked_about_before_off_stop_and_close(setup) -> None:
    """Позиции может не быть, но проверить её надо — вопрос это говорит."""
    window, port, asked = setup
    window.apply_state(WORKING)
    _show(window, _unread())
    window.live_action.trigger()
    window.stop_action.trigger()
    window.closeEvent(QCloseEvent())
    assert len(asked.questions) == 3, asked.questions
    for title, text, _yes in asked.questions:
        assert POSITION_UNREAD in text and UNREAD in text, f"«{title}»:\n{text}"
        assert TAKE_NONE in text, f"«{title}»: о тейке сказано не то"
    assert port.live == [] and port.stops == 0, "«Отмена» что-то выключила"


# ------------------------------------------------------------- слова о тейке (Н7)

TAKE_NOTE = "по журналу жива лимитная по 251 375 — возможно, тейк"


def _left(take_note: str) -> LiveStatus:
    """Бой выключен, у брокера осталась позиция без известного уровня."""
    return LiveStatus(
        available=True, position=SHORT, take_note=take_note,
        position_note="портфель у брокера не прочитан на момент выключения",
    )


def test_the_header_shows_the_take_note_instead_of_no_level(setup) -> None:
    """Тейк жив по журналу — шапка не пишет «уровня нет»."""
    window, _port, _asked = setup
    _show(window, _left(TAKE_NOTE))
    value = window.status_panel.fact.value
    assert "уровня нет" not in value.text(), value.text()
    assert "по журналу жива" in value.text(), value.text()
    assert TAKE_NOTE in value.toolTip(), value.toolTip()


def test_the_questions_use_the_take_note_and_never_say_unknown_is_none(setup) -> None:
    window, _port, asked = setup
    window.apply_state(WORKING)
    _show(window, _left(TAKE_NOTE))
    window.stop_action.trigger()
    window.closeEvent(QCloseEvent())
    assert len(asked.questions) == 2, asked.questions
    for title, text, _yes in asked.questions:
        assert TAKE_NOTE in text, f"«{title}»: слов порта о тейке нет"
        assert TAKE_NONE not in text, f"«{title}»: «тейка не знает» при живом тейке"
        assert "уровня нет" not in text, f"«{title}»: «уровня нет» при живом тейке"


def test_without_a_take_note_the_live_position_speaks_by_its_level(setup) -> None:
    """Живая позиция «факта» без слов порта — прежний вид: «уровень 2 864,00»."""
    window, _port, _asked = setup
    _show(window, _combat(LONG))
    assert "уровень 2\N{NO-BREAK SPACE}864" in window.status_panel.fact.value.text()


# ------------------------------------------------------------- загрузка истории (Н8)


@pytest.mark.parametrize("status", [_combat(), LiveStatus(available=False, wanted=True)],
                         ids=["running", "starting"])
def test_history_loading_is_off_in_combat_with_a_hint(setup, status: LiveStatus) -> None:
    window, port, _asked = setup
    _show(window, status)
    assert not window.history_action.isEnabled()
    assert window.history_action.toolTip() == HISTORY_IN_COMBAT
    window.open_history()
    assert port.histories == [], "загрузка истории запрошена в бою"
    assert port.live == [], "попытка загрузки тронула бой"


def test_history_loading_is_back_when_combat_is_off(setup) -> None:
    window, port, _asked = setup
    _show(window, _combat())
    _show(window, LiveStatus(available=True))
    assert window.history_action.isEnabled()
    assert window.history_action.toolTip() == HISTORY_HINT
    window.open_history()
    assert len(port.histories) == 1


def test_the_end_of_a_load_does_not_light_history_loading_in_combat(
    setup, qapp, monkeypatch
) -> None:
    """Н8: загрузка кончилась (здесь — отказом порта «выключите бой»), а бой идёт.

    Полоска загрузки поставлена до снимка боя: окно ещё не знало, что бой
    включается. Конец загрузки гасит полоску — и пункт обязан остаться
    погашенным с подсказкой боя, а не вернуться в строй посреди боя.
    """
    window, port, _asked = setup
    monkeypatch.setattr(QMessageBox, "exec", lambda _box: 0)
    monkeypatch.setattr("ui.main_window.HistoryDialog", AgreedHistory)
    _show(window, LiveStatus(available=True))
    # Путь человека: пункт меню → опись базы от порта → окно загрузки → «Загрузить».
    window.history_action.trigger()
    window.show_history_dialog(HistoryFacts(symbol="MXZ6"))
    qapp.processEvents()  # окно загрузки открывается следующим оборотом цикла
    assert port.loads, "загрузка не началась — проверять нечего"
    assert _bars_on_screen(window), "полоски загрузки нет — проверять нечего"

    _show(window, _combat())
    window.show_history_result(HistoryLoadOutcome(
        symbol="MXZ6", headline="Загрузка не начата",
        trouble="Загрузка истории в бою недоступна: выключите бой.",
    ))
    qapp.processEvents()
    assert not _bars_on_screen(window), "полоска загрузки осталась на экране"
    assert not window.history_action.isEnabled(), "конец загрузки зажёг пункт посреди боя"
    assert window.history_action.toolTip() == HISTORY_IN_COMBAT


def _bars_on_screen(window: MainWindow) -> list[QProgressDialog]:
    """Видимые полоски хода окна — то, что видит человек."""
    return [bar for bar in window.findChildren(QProgressDialog) if bar.isVisible()]


def test_history_loading_pressed_in_combat_says_why_in_the_status_line(setup) -> None:
    """Н8: пункт нажат в бою — отказ словами, а не тишина.

    Погашенный пункт — не единственная дорога к действию: оно зовётся
    и в обход меню (сочетание клавиш, снимок боя, пришедший после нажатия).
    Стережёт: опись базы не запрошена, строка состояния говорит «выключите бой».
    """
    window, port, _asked = setup
    _show(window, _combat())
    window.open_history()
    assert port.histories == [], "опись базы запрошена в бою"
    assert window.statusBar().currentMessage() == HISTORY_IN_COMBAT


class AgreedWhileCombatStarts(AgreedHistory):
    """Окно загрузки подтверждено, а пока оно было открыто, бой включился."""

    def exec(self) -> int:
        window = self.parent()
        assert isinstance(window, MainWindow)
        window.show_live(_combat())
        return super().exec()


def test_combat_turned_on_while_the_load_dialog_was_open_starts_no_load(
    setup, qapp, monkeypatch
) -> None:
    """Н8: бой включился, пока окно загрузки было открыто, — «Загрузить» загрузку не начинает.

    Пункт меню проверен при нажатии, но окно загрузки модальное и висит,
    сколько человек думает: снимок боя приходит за это время. Стережёт:
    загрузка порту не ушла, полоски хода нет, отказ словами в строке состояния.
    """
    window, port, _asked = setup
    monkeypatch.setattr("ui.main_window.HistoryDialog", AgreedWhileCombatStarts)
    _show(window, LiveStatus(available=True))
    window.history_action.trigger()
    window.show_history_dialog(HistoryFacts(symbol="MXZ6"))
    qapp.processEvents()  # окно загрузки открывается следующим оборотом цикла
    assert port.histories, "опись базы не запрошена — проверять нечего"
    assert port.loads == [], "загрузка ушла порту, хотя бой включился"
    assert not _bars_on_screen(window), "полоска загрузки на экране, хотя загрузки нет"
    assert window.statusBar().currentMessage() == HISTORY_IN_COMBAT


# ------------------------------------------------------------- цена не названа (З6)


def test_an_unnamed_entry_price_is_said_in_words_and_never_as_nan(setup) -> None:
    """Брокер цену входа не назвал (`NaN`) — «цена не названа», а не «по nan»."""
    window, _port, asked = setup
    window.apply_state(WORKING)
    nameless = FactPosition(side=EngineSide.LONG, volume=1.0, entry_price=float("nan"))
    _show(window, LiveStatus(available=False, wanted=True, halted=HALT, position=nameless))
    header = window.status_panel.fact.value
    window.stop_action.trigger()
    window.closeEvent(QCloseEvent())
    shown = [header.text(), header.toolTip(), *(text for _t, text, _y in asked.questions)]
    assert len(asked.questions) == 2, asked.questions
    for text in shown:
        assert "nan" not in text.lower(), text
    assert "цена не названа" in header.text(), header.text()
    for _title, text, _yes in asked.questions:
        assert "цена не названа" in text, text
