"""Отказы «Применить» и прогона доезжают до окна, а окно не врёт после них.

Дефект 29.09.2026, воспроизведён на копии программы: в окне настроек
выбрали «Реверс с постоянной позицией» при перевороте «через свечу»
и нажали «Применить». Порт отказал, а окно

* запомнило отвергнутый алгоритм как принятый и отдало его следующему
  прогону на истории;
* показало отказ на 15 секунд в строке состояния **за** окном настроек,
  обрезанным на 1440 точках;
* после отказа прогона оставило полоску «Прогон робота по истории… 0 %»
  навсегда — прогона нет, закрыть её нечему.

Правило 13 `CLAUDE.md`: отказ, не доехавший до человека, равен поломке.
Поэтому каждое утверждение здесь проверяет **следствие** (окно откатилось,
полоска закрыта, поле закрыто), а не только текст.

Что стережёт каждая проверка и какая мутация обязана её ронять — в её
докстринге.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QDialogButtonBox, QMessageBox, QProgressDialog

import ui.main_window
from app import convert
from market import redact
from tests.helpers import RecordingPort, settle_qt
from tests.test_app_algorithm_demands import BAD, GOOD, _span, drive
from ui.algorithm_dialog import AlgorithmDialog
from ui.models import BacktestRequest, ReversalMoment, Settings
from ui.settings_dialog import SettingsDialog

#: Алгоритм с требованием к перевороту и его название — от реестра через
#: каталог, а не буквами: сторож не должен зеленеть на переименовании.
ALWAYS = BAD.strategy_id


def _catalogue(values: Settings) -> tuple:
    """Каталог алгоритмов так, как его отдаёт торговая часть."""
    return convert.algorithms(values)


def _note(dialog: SettingsDialog, field: str, kind: str):
    """Строка у поля; её отсутствие — само по себе провал."""
    found = dialog.note_at(field, kind)
    assert found is not None, f"строки «{kind}» у поля {field} нет вовсе"
    return found


def _apply(dialog: SettingsDialog) -> None:
    dialog.buttons.button(QDialogButtonBox.StandardButton.Apply).click()


def _title() -> str:
    return str(next(one.title for one in _catalogue(Settings()) if one.id == ALWAYS))


class Agreeing(SettingsDialog):
    """Окно настроек без модального «было → стало»: в прогоне оно повисло бы."""

    def confirm(self, values: Settings) -> bool:
        return True


@pytest.fixture
def dialog(qapp) -> Iterator[SettingsDialog]:
    shown = Agreeing(Settings())
    shown.set_algorithms(_catalogue(Settings()))
    yield shown
    shown.deleteLater()
    qapp.processEvents()


@pytest.fixture
def window(qapp) -> Iterator[ui.main_window.MainWindow]:
    port = RecordingPort()
    built = ui.main_window.MainWindow(port=port, sanitize=redact)
    yield built
    built.close()
    built.deleteLater()
    settle_qt(qapp)


# --------------------------------------------------------------------------
# 2. Сочетание не собирается: выбор алгоритма ставит и закрывает поле
# --------------------------------------------------------------------------


def test_choosing_the_algorithm_sets_and_locks_the_reversal(qapp) -> None:
    """Алгоритм ставит «В той же свече», закрывает поле и говорит почему.

    Окно открыто на настройках с переворотом через свечу (старый файл):
    каталог пришёл — поле выставлено и закрыто.
    Стережёт: окно, в котором несовместимую пару можно собрать мышкой.
    Мутация, обязанная ронять: убрать вызов `_sync_demands` из
    `SettingsDialog._show_algorithm`.
    """
    dialog = Agreeing(Settings(reversal_moment=ReversalMoment.NEXT_BAR))
    assert dialog.reversal_moment.currentData() is ReversalMoment.NEXT_BAR
    dialog.set_algorithms(_catalogue(Settings()))
    assert dialog.reversal_moment.currentData() is ReversalMoment.SAME_BAR, (
        "алгоритм выбран, а момент переворота остался «через свечу» — "
        "порт такое сочетание отвергнет"
    )
    assert not dialog.reversal_moment.isEnabled(), (
        "поле открыто: переворот можно вернуть «через свечу» при выбранном алгоритме"
    )
    note = _note(dialog, "reversal_moment", "demand")
    assert not note.isHidden() and _title() in note.text(), (
        f"поле закрыто молча — не сказано, какой алгоритм и почему: «{note.text()}»"
    )
    assert dialog.values().reversal_moment is ReversalMoment.SAME_BAR


def test_settings_arriving_with_the_algorithm_keep_the_field_locked(dialog) -> None:
    """Пришедшие настройки с алгоритмом №2 — поле закрыто и после `set_values`.

    Стережёт порядок в `set_values`: списки ставят значения из настроек
    **после** сверки требований — и поле открылось бы снова.
    Мутация, обязанная ронять: вернуть `_take_algorithm` в начало `set_values`.
    """
    dialog.set_values(GOOD)
    assert not dialog.reversal_moment.isEnabled()
    assert dialog.reversal_moment.currentData() is ReversalMoment.SAME_BAR


# --------------------------------------------------------------------------
# 3. Список алгоритмов пишет требование заранее
# --------------------------------------------------------------------------


def test_the_list_names_the_requirement_before_choosing(qapp, dialog) -> None:
    """В списке выбора у алгоритма №2 написано требование к перевороту.

    Мутация, обязанная ронять: убрать `demand_lines` из текста строки списка.
    """
    shown = AlgorithmDialog(_catalogue(Settings()), dialog, captions=dialog.field_captions())
    try:
        rows = {
            shown.list.item(index).text().split("\n")[0]: shown.list.item(index).text()
            for index in range(shown.list.count())
        }
        row = rows[_title()]
        assert "Момент переворота" in row and "В той же свече" in row, (
            f"требование алгоритма не видно в списке до выбора: «{row}»"
        )
        others = [text for title, text in rows.items() if title != _title()]
        assert all("Требует" not in text for text in others), (
            f"требование приписано алгоритму, у которого его нет: {others}"
        )
    finally:
        shown.deleteLater()


def test_the_list_is_tall_enough_for_two_line_rows(qapp, dialog) -> None:
    """Строка с требованием в две строки текста не обрезается высотой списка.

    Снимок 29.09.2026: высота считалась «первая строка × число строк»,
    и вторая, двустрочная, уходила под прокрутку — видно было одну из двух.
    Мутация, обязанная ронять: вернуть в `_fit_the_list` расчёт по первой строке.
    """
    shown = AlgorithmDialog(_catalogue(Settings()), dialog, captions=dialog.field_captions())
    try:
        rows = sum(shown.list.sizeHintForRow(index) for index in range(shown.list.count()))
        assert shown.list.maximumHeight() >= rows, (
            f"список ростом {shown.list.maximumHeight()} при строках {rows}: часть списка скрыта"
        )
    finally:
        shown.deleteLater()


# --------------------------------------------------------------------------
# 1. Отказ «Применить»: откат и отказ у поля, окно не закрывается
# --------------------------------------------------------------------------


def _refuse_every_apply(dialog: SettingsDialog, kept: Settings) -> list[Settings]:
    """Порт, отвечающий отказом, — внутри `emit`, как настоящий (`ui/ports.py`)."""
    sent: list[Settings] = []

    def refuse(values: Settings) -> None:
        sent.append(values)
        dialog.take_refusal(kept, "Порт отказал: нужно другое значение.", "reversal_moment")

    dialog.settings_changed.connect(refuse)
    return sent


def test_a_refused_apply_keeps_the_edits_and_says_so_at_the_field(dialog) -> None:
    """Отказ: «принятое» — то, что в силе; правки — в полях; отказ крупно и у поля.

    Стережёт два дефекта 29.09.2026. Окно помнило отвергнутый набор как
    принятый. А после исправления того — откатывало поля целиком, стирая
    правки человека (проверка «поля откатились» перевёрнута по заданию
    ревью 29.09.2026, а не ослаблена).
    Мутации, обязанные ронять: убрать `self._applied = kept`; вернуть
    `self.set_values(kept)` в `take_refusal`; убрать показ `refusal_note`.
    """
    kept = Settings()
    sent = _refuse_every_apply(dialog, kept)
    dialog.average_period.setValue(kept.average_period + 5)
    edited = dialog.values()
    _apply(dialog)
    assert sent, "настройки не ушли вовсе"
    assert dialog.applied() == kept, "окно считает принятым отвергнутый набор"
    assert dialog.values() == edited, "отказ стёр правки человека в полях"
    assert not dialog.refusal_note.isHidden(), "отказ не показан в окне настроек"
    assert "Момент переворота" in dialog.refusal_note.text(), (
        f"отказ не называет поле, которое надо поменять: «{dialog.refusal_note.text()}»"
    )
    at_field = _note(dialog, "reversal_moment", "refusal")
    assert not at_field.isHidden() and "Порт отказал" in at_field.text()
    assert dialog.tabs.tabText(dialog.tabs.currentIndex()) == "Вход и выход"


class Remembering(Agreeing):
    """Согласное окно, запоминающее, с чем сравнивало каждое подтверждение."""

    def __init__(self, settings: Settings) -> None:
        self.compared: list[Settings] = []
        super().__init__(settings)

    def confirm(self, values: Settings) -> bool:
        self.compared.append(self.applied())
        return True


def test_a_refusal_on_one_field_keeps_the_others_and_the_retry_passes(qapp) -> None:
    """Отказ по одному полю не стирает правки в других; исправил — ушло всё.

    Сценарий ревью 29.09.2026: период и окно поменяны вместе с полем,
    на которое пришёл отказ. После отказа период цел; исправленное поле
    и «Применить» — порт получает и период, и исправление, а сравнение
    шло от того, что в силе.
    Мутации, обязанные ронять: вернуть `self.set_values(kept)`
    в `take_refusal`; убрать `self._applied = kept`.
    """
    kept = Settings().replace(average_period=30)
    dialog = Remembering(Settings())
    try:
        dialog.set_algorithms(_catalogue(Settings()))
        answers = iter([True, False])
        sent: list[Settings] = []

        def port(values: Settings) -> None:
            sent.append(values)
            if next(answers):
                dialog.take_refusal(kept, "Порт отказал: нужно другое значение.",
                                    "reversal_moment")

        dialog.settings_changed.connect(port)
        dialog.average_period.setValue(21)
        dialog.depth_days.setValue(dialog.depth_days.value() + 1)
        _apply(dialog)
        assert dialog.average_period.value() == 21, "отказ по другому полю стёр период"
        assert dialog.depth_days.value() == Settings().depth_days + 1
        index = dialog.reversal_moment.currentIndex()
        dialog.reversal_moment.setCurrentIndex(1 if index == 0 else 0)
        _apply(dialog)
        assert len(sent) == 2, sent
        assert sent[1].average_period == 21 and sent[1].reversal_moment != sent[0].reversal_moment
        assert dialog.compared[1] == kept, "второе «Применить» сравнивало не с тем, что в силе"
        assert dialog.applied() == sent[1] and dialog.refusal_note.isHidden()
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_a_refused_ok_does_not_close_the_dialog(qapp, dialog) -> None:
    """«ОК» с отказом окно не закрывает: иначе отказ закрылся бы вместе с ним.

    Мутация, обязанная ронять: `_emit` возвращает `True` безусловно.
    """
    _refuse_every_apply(dialog, Settings())
    closed: list[int] = []
    dialog.finished.connect(closed.append)
    dialog.average_period.setValue(Settings().average_period + 5)
    dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).click()
    qapp.processEvents()
    assert not closed, "окно настроек закрылось, унеся с собой отказ"


def test_the_next_apply_clears_the_refusal(dialog) -> None:
    """Принятое следующее «Применить» снимает прежний отказ."""
    sent = _refuse_every_apply(dialog, Settings())
    dialog.average_period.setValue(Settings().average_period + 5)
    _apply(dialog)
    dialog.settings_changed.disconnect()
    dialog.average_period.setValue(Settings().average_period + 6)
    _apply(dialog)
    assert sent
    assert dialog.refusal_note.isHidden()


def test_a_refusal_without_a_field_says_the_reason_on_top(dialog) -> None:
    """Отказ без названного поля — причина целиком наверху, а не «смотрите под полем».

    Мутация, обязанная ронять: верхняя строка без причины при пустом поле.
    """
    dialog.settings_changed.connect(
        lambda _values: dialog.take_refusal(Settings(), "Порт отказал целиком.", "")
    )
    dialog.average_period.setValue(Settings().average_period + 5)
    _apply(dialog)
    assert "Порт отказал целиком" in dialog.refusal_note.text()


def test_a_foreign_refusal_does_not_wipe_the_fields(dialog) -> None:
    """Отказ не на отправку из этого окна не стирает неотправленные правки.

    Мутация, обязанная ронять: убрать проверку `_emitting` в `take_refusal`.
    """
    dialog.average_period.setValue(Settings().average_period + 5)
    dialog.take_refusal(Settings(), "чужой отказ", "")
    assert dialog.average_period.value() == Settings().average_period + 5
    assert dialog.refusal_note.isHidden()


def test_the_main_window_rolls_back_on_refusal(window) -> None:
    """Главное окно после отказа держит то, что в силе у порта.

    Мутация, обязанная ронять: снять подписку на `settings_refused`
    в `MainWindow` либо `self._settings = kept` в обработчике.
    """
    kept = window.settings()
    window.port.settings_applied.emit(BAD)
    assert window.settings() == BAD
    window.port.settings_refused.emit(kept, "отказ", "reversal_moment")
    assert window.settings() == kept, (
        "окно запомнило отвергнутые настройки и отдаст их следующему прогону"
    )


class RefusingPort(RecordingPort):
    """Порт, отказывающий любому «Применить» — внутри вызова, как `HistoryPort`."""

    def apply_settings(self, settings: Settings) -> None:
        super().apply_settings(settings)
        self.settings_refused.emit(Settings(), "Порт отказал: нужно другое значение.",
                                   "reversal_moment")


def test_the_refusal_travels_from_the_port_into_the_open_settings_dialog(
    qapp, monkeypatch
) -> None:
    """Сквозной путь: «Применить» в окне, открытом из главного, → отказ у поля.

    Остальные проверки кормят `take_refusal` окну настроек сами, в обход
    главного окна, и зеленеют, даже если главное окно отказ окну настроек
    не передаёт — человек тогда видел бы откатившиеся поля и ни слова
    почему (правило 13).
    Мутация, обязанная ронять (проверено 29.09.2026, до этого теста не
    ловилась ничем): убрать `self._settings_dialog.take_refusal(...)`
    в `MainWindow._on_settings_refused`.
    """
    seen: dict[str, object] = {}

    class Clicking(Agreeing):
        def exec(self) -> int:
            self.average_period.setValue(Settings().average_period + 5)
            _apply(self)
            seen["top"] = not self.refusal_note.isHidden()
            seen["values"] = self.values()
            note = self.note_at("reversal_moment", "refusal")
            seen["field"] = note is not None and not note.isHidden()
            return 0

    monkeypatch.setattr(ui.main_window, "SettingsDialog", Clicking)
    port = RefusingPort()
    window = ui.main_window.MainWindow(port=port, sanitize=redact)
    try:
        window.open_settings()
        assert [name for name, _ in port.calls].count("apply_settings") == 1, port.calls
        assert seen["top"], "отказ порта не показан в окне настроек"
        assert seen["field"], "у поля, которое надо поменять, отказа нет"
        assert seen["values"] == Settings().replace(
            average_period=Settings().average_period + 5
        ), "отказ стёр правку человека в окне настроек"
    finally:
        window.close()
        window.deleteLater()
        settle_qt(qapp)


# --------------------------------------------------------------------------
# 4. Отказ прогона: полоска закрыта, отказ в окне, сказано про прежний прогон
# --------------------------------------------------------------------------


class Instant(QObject):
    """Окно прогона, сразу отвечающее «Запустить» с несовместимой парой."""

    def __init__(self, *_args: object) -> None:
        super().__init__()
        since, until = _span()
        self._request = BacktestRequest(settings=BAD, since=since, until=until)

    def exec(self) -> int:
        return int(ui.main_window.QDialog.DialogCode.Accepted)

    def request(self) -> BacktestRequest:
        return self._request


def _ask_for_a_run(qapp, window, monkeypatch) -> None:
    """«Прогон на истории» → «Запустить», как это делает человек."""
    from ui.models import BacktestOptions

    monkeypatch.setattr(ui.main_window, "BacktestDialog", Instant)
    window.show_backtest_dialog(BacktestOptions())
    qapp.processEvents()  # окно прогона открывается следующим оборотом (`B-026`)


def _bars(window) -> list[QProgressDialog]:
    return [one for one in window.findChildren(QProgressDialog) if one.isVisible()]


def test_a_refused_backtest_closes_the_bar_and_says_so(qapp, window, monkeypatch) -> None:
    """Полоска «0 %» не висит, отказ — окном, а не строкой состояния.

    Мутации, обязанные ронять: снять подписку на `backtest_refused`;
    убрать `_close_progress()` из `show_backtest_refusal`; убрать показ окна.
    """
    _ask_for_a_run(qapp, window, monkeypatch)
    assert _bars(window), "полоска прогона не открылась — проверять нечего"
    window.port.backtest_refused.emit("Настройки не годятся: причина.")
    qapp.processEvents()
    assert not _bars(window), "полоска прогона осталась после отказа"
    boxes = [one for one in window.findChildren(QMessageBox) if one.isVisible()]
    assert boxes, "отказ прогона не показан в окне"
    text = boxes[0].text()
    assert "причина" in text
    assert "прежний прогон" in text, f"не сказано, что на экране — прежний прогон: «{text}»"


def test_the_backtest_dialog_does_not_remember_unaccepted_settings(
    qapp, window, monkeypatch
) -> None:
    """Настройки прогона становятся действующими только эхом порта.

    Мутация, обязанная ронять: вернуть `self._settings = request.settings`
    в `_run_backtest_dialog`.
    """
    before = window.settings()
    _ask_for_a_run(qapp, window, monkeypatch)
    assert window.settings() == before, "окно запомнило настройки, которых порт не принимал"


# --------------------------------------------------------------------------
# Порт: отказы уходят своими сигналами
# --------------------------------------------------------------------------


def _with_refusals(port) -> tuple[list, list]:
    settings: list = []
    backtests: list[str] = []
    port.settings_refused.connect(lambda *payload: settings.append(payload))
    port.backtest_refused.connect(backtests.append)
    return settings, backtests


def test_the_port_names_the_kept_settings(loop, tmp_path) -> None:
    """Отказ «Применить» уходит своим сигналом с настройками в силе.

    Негодное значение — срок остановки перед экспирацией вне пределов.
    Мутация, обязанная ронять: `_refuse` вместо `_refuse_settings` в
    `HistoryPort.apply_settings`.
    """
    caught: dict[str, list] = {}

    def work(port) -> None:
        caught["settings"], caught["backtests"] = _with_refusals(port)
        port.apply_settings(Settings(expiry_halt_days=10_000))

    drive(loop, tmp_path, Settings(), work)
    assert caught["settings"], "отказ «Применить» не дошёл до окна своим сигналом"
    kept, reason, _field = caught["settings"][0]
    assert kept == Settings() and "экспирацией" in reason


def test_the_port_refuses_a_backtest_out_loud(loop, tmp_path) -> None:
    """Прогон с незнакомым алгоритмом: `backtest_refused`, полоску есть чем закрыть.

    Мутация, обязанная ронять: `_refuse` вместо `_refuse_backtest`
    в предпроверке `HistoryPort.run_backtest`.
    """
    since, until = _span()
    caught: dict[str, list] = {}

    def work(port) -> None:
        caught["settings"], caught["backtests"] = _with_refusals(port)
        port.run_backtest(BacktestRequest(
            settings=Settings(strategy_id="no_such_algorithm"),
            since=since, until=until,
        ))

    drive(loop, tmp_path, Settings(), work)
    assert len(caught["backtests"]) == 1, caught["backtests"]
    assert "no_such_algorithm" in caught["backtests"][0]


def test_a_backtest_refused_after_the_precheck_is_said_too(loop, tmp_path) -> None:
    """Предпроверка прошла, `apply_settings` отказал — прогона нет, отказ есть.

    Второй путь к вечной полоске: предпроверка не смотрит срок остановки
    перед экспирацией, и `apply_settings` внутри `run_backtest` отказывает
    уже при закреплённой просьбе. Задача не ставится, «работа кончилась»
    не придёт. Мутация, обязанная ронять: вернуть в `_forget_report`
    `failed` вместо `backtest_refused`.
    """
    since, until = _span()
    odd = Settings(expiry_halt_days=99)
    caught: dict[str, list] = {}

    def work(port) -> None:
        caught["settings"], caught["backtests"] = _with_refusals(port)
        port.run_backtest(BacktestRequest(settings=odd, since=since, until=until))

    drive(loop, tmp_path, Settings(), work)
    assert caught["backtests"], "прогон не состоялся, а окну не сказано — полоска висит"
    assert "экспирац" in caught["backtests"][-1], (
        f"причина не названа: «{caught['backtests'][-1]}»"
    )


# --------------------------------------------------------------------------
# 3. Программа сменила настройки, пока окно открыто: окно узнаёт
# --------------------------------------------------------------------------


class EchoingPort(RecordingPort):
    """Порт, отвечающий на «Применить» эхом внутри вызова, как `HistoryPort`."""

    def apply_settings(self, settings: Settings) -> None:
        super().apply_settings(settings)
        self.settings_applied.emit(settings)


def test_the_programs_switch_reaches_the_open_settings_dialog(qapp, monkeypatch) -> None:
    """Суточный переход MXU6 → MXZ6 при открытом окне: поле, «принятое» и строка.

    Находка ревью 29.09.2026: эхо перехода доходило только до главного окна,
    в полях оставался истёкший код, и «Применить» молча возвращал на него.
    Правка периода, сделанная до перехода, не стирается.
    Мутации, обязанные ронять: убрать передачу эха в
    `MainWindow._on_settings_echo`; заменить слияние в `take_echo` на
    `set_values(settings)`; не обновлять `_applied` в `take_echo`.
    """
    seen: dict[str, object] = {}
    old = Settings().replace(instrument="MXU6")
    new = old.replace(instrument="MXZ6")

    class Watching(Agreeing):
        def exec(self) -> int:
            self.average_period.setValue(old.average_period + 5)
            port.settings_applied.emit(new)  # переход программы
            seen["field"] = self.instrument.text()
            seen["applied"] = self.applied()
            seen["period"] = self.average_period.value()
            seen["note"] = "" if self.echo_note.isHidden() else self.echo_note.text()
            _apply(self)
            seen["note_after"] = not self.echo_note.isHidden()
            return 0

    monkeypatch.setattr(ui.main_window, "SettingsDialog", Watching)
    port = EchoingPort()
    window = ui.main_window.MainWindow(port=port, sanitize=redact)
    try:
        port.settings_applied.emit(old)
        window.open_settings()
        assert seen["field"] == "MXZ6", "поле инструмента осталось на истёкшем коде"
        assert seen["applied"] == new, "окно считает принятым прежний код"
        assert seen["period"] == old.average_period + 5, "переход стёр правку человека"
        assert "MXU6" in str(seen["note"]) and "MXZ6" in str(seen["note"]), (
            f"переход не сказан в окне: «{seen['note']}»"
        )
        applied = [value for name, value in port.calls if name == "apply_settings"]
        assert applied and applied[-1] == new.replace(
            average_period=old.average_period + 5
        ), applied
        assert not seen["note_after"], "строка о переходе не снята после «Применить»"
    finally:
        window.close()
        window.deleteLater()
        settle_qt(qapp)


def test_the_own_apply_echo_says_nothing_about_a_switch(qapp) -> None:
    """Эхо собственного «Применить» — не переход программы: строки нет.

    Мутация, обязанная ронять: убрать `self._emitting` из условия
    в `take_echo`.
    """
    dialog = Agreeing(Settings().replace(instrument="MXU6"))
    try:
        # Порт вправе вернуть принятое в своём написании: эхо отличается
        # от отправленного, и переходом программы это всё равно не становится.
        dialog.settings_changed.connect(
            lambda values: dialog.take_echo(values.replace(instrument="MXZ6"))
        )
        dialog.instrument.setText("mxz6")
        _apply(dialog)
        assert dialog.echo_note.isHidden(), dialog.echo_note.text()
        assert dialog.applied().instrument == "MXZ6"
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_a_switch_leaves_the_instrument_the_person_typed(qapp) -> None:
    """Человек уже ввёл свой код — переход программы его не затирает и говорит об этом.

    Мутация, обязанная ронять: заменить слияние в `take_echo` на
    `set_values(settings)`.
    """
    dialog = Agreeing(Settings().replace(instrument="MXU6"))
    try:
        dialog.instrument.setText("SiZ6")
        dialog.take_echo(dialog.applied().replace(instrument="MXZ6"))
        assert dialog.instrument.text() == "SiZ6"
        assert dialog.applied().instrument == "MXZ6"
        assert "SiZ6" in dialog.echo_note.text(), dialog.echo_note.text()
    finally:
        dialog.deleteLater()
        qapp.processEvents()
