"""`D-042`: кнопка «Возобновить работу…» — что она снимает и что говорит.

Что здесь стережётся
--------------------
Кнопки не было вовсе: `HistoryPort.resume()` звал только тест, а владелец
счёта снимал остановку перезапуском программы. Перезапуск остановку больше
не снимает (`D-043`), поэтому без кнопки робота стало бы нечем вернуть
в работу совсем.

⚠️ Снимается **одна** причина, самая ранняя. Кнопка, снимающая всё разом,
ломает работу 06.09.2026: причины про разные беды, и согласиться с убытком
заодно, не читая, сняв требование сходить к брокеру, нельзя (`D-086`).

Данные синтетические, движка нет, сети нет: порт запоминает команды.
Проверять поодиночке: `pytest tests/test_ui_resume_button.py::имя`.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from helpers import RecordingPort
from PySide6.QtWidgets import QMessageBox

from market import MSK, redact
from ui.main_window import MainWindow
from ui.models import HaltCause, RobotState
from ui.notices import resume_question

LIMIT = "Дневной лимит убытка. Достигнут предел −5 000 ₽ за день."
UNKNOWN = (
    "Заявка отправлена, исход неизвестен. Проверьте заявки и позицию "
    "у брокера. Робот остановлен и новых решений не принимает."
)
YESTERDAY = datetime(2026, 9, 6, 11, 20, tzinfo=MSK)


def halted_state(*causes: HaltCause) -> RobotState:
    """Снимок остановленного робота с поданными причинами.

    Строка `halted` собирается тем же правилом, что в порту (`_Halt.reason`):
    одна причина — она сама, несколько — перечисление. Плашка и кнопка читают
    разные поля, и подменять одно другим здесь нельзя.
    """
    if len(causes) == 1:
        glued = causes[0].reason
    else:
        listed = " ".join(
            f"({number}) {cause.reason}" for number, cause in enumerate(causes, 1)
        )
        glued = f"Причин остановки {len(causes)}. {listed}"
    return RobotState(halted=glued, halt_causes=causes)


@pytest.fixture()
def window(qapp):
    port = RecordingPort()
    win = MainWindow(port=port, sanitize=redact)
    yield win
    win._timer.stop()  # noqa: SLF001 — часы окна наружу не выведены
    win.apply_state(RobotState())
    win.close()
    win.deleteLater()
    qapp.processEvents()


def test_the_button_is_hidden_while_the_robot_is_not_halted(window) -> None:
    """Кнопки нет, пока снимать нечего: она появляется вместе с остановкой."""
    window.apply_state(RobotState())
    assert not window.resume_action.isVisible(), (
        "кнопка возобновления видна у неостановленного робота"
    )


def test_the_button_appears_with_the_halt(window) -> None:
    """Робот остановлен — кнопка на панели есть."""
    window.apply_state(halted_state(HaltCause(reason=LIMIT, kind="предохранитель робота")))
    assert window.resume_action.isVisible(), (
        "робот остановлен, а снять остановку в окне нечем"
    )


def test_the_button_stays_hidden_when_there_is_nothing_to_lift(window) -> None:
    """Остановку показал прогон по истории — снимать нечего, кнопки нет.

    ⚠️ Мутация: привязать видимость к `state.halted` вместо `halt_causes`.
    Кнопка появилась бы там, где нажатие не делает ничего: `resume` работает
    по причинам порта, а у прогона по истории их нет. Обещание, которого
    программа не выполнит, хуже отсутствующей кнопки.
    """
    window.apply_state(RobotState(halted="Робот остановлен на прогоне: лимит."))
    assert not window.resume_action.isVisible(), (
        "кнопка возобновления предложена там, где снимать нечего"
    )


def test_the_question_names_the_one_being_lifted_and_what_stays() -> None:
    """Вопрос называет снимаемую причину и оставшиеся — поимённо.

    ⚠️ Мутация «снимать все разом»: текст, обещающий снять остановку целиком,
    и есть та кнопка, против которой написан `D-086`. Здесь проверяется, что
    человеку сказано «одна из двух» и названа вторая.
    """
    text = resume_question(
        halted_state(
            HaltCause(reason=LIMIT, kind="предохранитель робота", at=YESTERDAY),
            HaltCause(reason=UNKNOWN, kind="состояние счёта неизвестно",
                      broker_check=True),
        )
    )
    assert "причин 2" in text, f"число причин не названо: {text!r}"
    assert "самая ранняя" in text, f"не сказано, что снимается одна: {text!r}"
    assert LIMIT in text, f"снимаемая причина не названа: {text!r}"
    assert UNKNOWN in text, f"оставшаяся причина не названа: {text!r}"
    assert "останется остановленным" in text, (
        f"не сказано, что робот останется стоять: {text!r}"
    )
    assert "06.09.2026 11:20 МСК" in text, (
        f"время остановки не названо или названо без пояса: {text!r}"
    )


def test_the_question_sends_to_the_broker_only_when_it_is_needed() -> None:
    """Про поход к брокеру сказано тогда, когда он нужен, и не сказано иначе.

    Совет, выданный не по делу, обесценивает его к тому дню, когда он станет
    правдой (шапка `ui/notices.py`). Признак приходит готовым полем — решать,
    надо ли идти к брокеру, окно не имеет права.
    """
    account = resume_question(
        halted_state(HaltCause(reason=UNKNOWN, kind="состояние счёта неизвестно",
                               broker_check=True))
    )
    engine = resume_question(
        halted_state(HaltCause(reason=LIMIT, kind="предохранитель робота"))
    )
    assert "приложении брокера" in account, (
        f"про поход к брокеру не сказано там, где он нужен: {account!r}"
    )
    assert "приложении брокера" not in engine, (
        f"к брокеру послали без надобности: {engine!r}"
    )
    assert "последняя причина" in engine, (
        f"не сказано, что после снятия робот заработает: {engine!r}"
    )


def test_the_question_is_empty_when_there_are_no_causes() -> None:
    """Причин поштучно нет — вопроса нет: спрашивать не о чем."""
    assert resume_question(RobotState(halted="что-то")) == ""


def test_a_refusal_sends_nothing_to_the_engine(window, monkeypatch) -> None:
    """«Нет» в окне подтверждения — команда не уходит вовсе."""
    monkeypatch.setattr(
        QMessageBox, "warning",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.No),
    )
    window.apply_state(halted_state(HaltCause(reason=LIMIT)))
    window.request_resume()
    assert ("resume", None) not in window.port.calls, (
        f"отказ в окне подтверждения снял остановку: {window.port.calls}"
    )


def test_one_press_sends_exactly_one_resume(window, monkeypatch) -> None:
    """«Да» отправляет ровно одну команду снятия — не две и не ни одной.

    ⚠️ Мутация: позвать `port.resume()` в цикле по числу причин. Кнопка стала
    бы снимать всё разом руками окна, при исправном порту, — и проверка
    в порту этого не поймала бы вовсе.
    """
    monkeypatch.setattr(
        QMessageBox, "warning",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    window.apply_state(
        halted_state(
            HaltCause(reason=LIMIT),
            HaltCause(reason=UNKNOWN, broker_check=True),
        )
    )
    window.request_resume()
    presses = [call for call in window.port.calls if call[0] == "resume"]
    assert len(presses) == 1, (
        f"одно нажатие отправило снятий: {len(presses)} ({window.port.calls})"
    )


def test_a_press_without_causes_says_so_instead_of_guessing(
    window, monkeypatch
) -> None:
    """Причин поштучно нет — окно говорит об этом, а не шлёт команду вслепую.

    Команда вслепую сняла бы **что-то**, а что именно, окно человеку
    не назвало. Молчаливое нажатие здесь было бы хуже отказа.
    """
    asked: list[object] = []

    def remember(*args: object, **kwargs: object) -> QMessageBox.StandardButton:
        asked.append(args)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(remember))
    window.apply_state(RobotState(halted="Робот остановлен на прогоне: лимит."))
    window.request_resume()
    assert asked == [], "окно спросило подтверждение там, где снимать нечего"
    assert not [call for call in window.port.calls if call[0] == "resume"], (
        f"команда снятия ушла вслепую: {window.port.calls}"
    )
