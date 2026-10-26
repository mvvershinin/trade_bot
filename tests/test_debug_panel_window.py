"""Панель «Отладка» в окне: свёртка, забор строк, прокрутка, цвет, копирование.

Строки приходят из настоящего буфера ленты (`app.debug_feed.PanelHandler`),
поданного окну так же, как его подаёт `app/main.py`. Что лента собирает
и чего не пропускает — `tests/test_debug_panel.py`.
"""

from __future__ import annotations

import logging
from datetime import datetime

import pytest
from PySide6.QtCore import QItemSelectionModel, QPoint, Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton

from app.debug_feed import PANEL_LOGGER, PanelHandler
from broker.redaction import MASK, REGISTRY, scrub
from market import MSK, redact
from ui.debug_panel import FOLDED, POLL_MS, UNFOLDED, DebugModel, DebugPanel, line_text
from ui.main_window import MainWindow
from ui.models import DebugBatch, DebugLine, DecisionLevel
from ui.theme import DARK, LIGHT

SECRET = "debug-panel-guarded-marker-7f3a9c1e5b2d"
FOREGROUND = Qt.ItemDataRole.ForegroundRole
#: Метка, которую вырезает подставная чистка окна (`cut`). В реестре секретов
#: её нет: дошла до экрана или буфера вырезанной — значит, прошла чистку окна.
MARK = "метка-под-вырезку-5c1d"


def cut(text: str) -> str:
    """Подставная чистка окна: вырезает `MARK`, и только её."""
    return text.replace(MARK, "[вырезано]")


def emit(feed: PanelHandler, text: str, level: int = logging.INFO) -> None:
    """Строка в буфер ленты — тем же путём, каким её кладёт `app.panel`."""
    feed.handle(logging.makeLogRecord({
        "name": PANEL_LOGGER, "levelno": level, "levelname": logging.getLevelName(level),
        "msg": text, "source": "движок",
    }))


def shown(panel: DebugPanel) -> list[str]:
    return [panel.model.line(row).text for row in range(panel.model.rowCount())]


@pytest.fixture
def feed() -> PanelHandler:
    return PanelHandler()


def _window(qapp, debug) -> MainWindow:
    win = MainWindow(sanitize=scrub, debug=debug)
    win.resize(1280, 860)
    win.show()
    qapp.processEvents()
    return win


@pytest.fixture
def window(qapp, feed):
    win = _window(qapp, feed)
    yield win
    win.close()
    win.deleteLater()
    qapp.processEvents()


def test_the_panel_starts_folded_and_the_button_unfolds_and_folds_it(window, qapp) -> None:
    """Кнопка в строке состояния: свёрнуто «▴», открыто «▾», нажатие переключает."""
    panel = window.debug_panel
    assert window.statusBar().isAncestorOf(panel.button), "кнопки нет в строке состояния"
    assert not panel.isVisible()
    assert (panel.button.text(), panel.button.isChecked()) == (FOLDED, False)
    panel.button.click()
    qapp.processEvents()
    assert panel.isVisible()
    assert (panel.button.text(), panel.button.isChecked()) == (UNFOLDED, True)
    panel.button.click()
    qapp.processEvents()
    assert not panel.isVisible()
    assert (panel.button.text(), panel.button.isChecked()) == (FOLDED, False)


def test_closing_the_dock_with_its_cross_releases_the_button(window, qapp) -> None:
    """Док закрыт крестиком — кнопка отжата и снова «▴». Мутация — отвязать `visibilityChanged`."""
    panel = window.debug_panel
    panel.button.click()
    qapp.processEvents()
    panel.close()
    qapp.processEvents()
    assert not panel.isVisible()
    assert (panel.button.text(), panel.button.isChecked()) == (FOLDED, False)


def test_the_fold_button_in_the_header_folds_the_panel_and_releases_the_button(
    window, qapp
) -> None:
    """«Свернуть» в шапке дока сворачивает панель, кнопка снова «▴».

    Это путь человека изнутри панели: шапка у дока своя (`setTitleBarWidget`),
    и штатного крестика Qt над ней нет. Мутация — отвязать «Свернуть».
    """
    panel = window.debug_panel
    panel.button.click()
    qapp.processEvents()
    assert panel.isVisible()
    [fold] = [
        button for button in panel.titleBarWidget().findChildren(QPushButton)
        if button.text() == "Свернуть"
    ]
    fold.click()
    qapp.processEvents()
    assert not panel.isVisible()
    assert (panel.button.text(), panel.button.isChecked()) == (FOLDED, False)


def test_lines_that_came_while_folded_are_shown_on_unfold(window, feed, qapp) -> None:
    """Пока панель свёрнута, строки копятся в ленте и приходят при открытии все."""
    panel = window.debug_panel
    for number in range(10):
        emit(feed, f"строка {number}")
    QTest.qWait(POLL_MS * 2)
    assert panel.model.rowCount() == 0, "свёрнутая панель забирает строки"
    panel.button.click()
    qapp.processEvents()
    assert shown(panel) == [f"строка {number}" for number in range(10)]


def test_an_open_panel_takes_new_lines_by_itself(window, feed, qapp) -> None:
    """Открытая панель забирает новое по таймеру, без единого вызова снаружи."""
    panel = window.debug_panel
    panel.button.click()
    qapp.processEvents()
    emit(feed, "новая строка")
    QTest.qWait(POLL_MS * 3)
    assert shown(panel) == ["новая строка"]


def test_a_gap_mark_stands_between_old_and_new_lines_on_every_reopening(qapp) -> None:
    """Вытесненное, пока панель была свёрнута, отмечено строкой — на каждом открытии."""
    feed = PanelHandler(capacity=20)
    win = _window(qapp, feed)
    try:
        panel = win.debug_panel
        panel.button.click()
        for number in range(5):
            emit(feed, f"строка {number}")
        panel.poll()
        panel.button.click()
        for number in range(5, 35):
            emit(feed, f"строка {number}")
        panel.button.click()
        qapp.processEvents()
        panel.button.click()
        for number in range(35, 60):
            emit(feed, f"строка {number}")
        panel.button.click()
        qapp.processEvents()
        rows = shown(panel)
        marks = [index for index, text in enumerate(rows) if text.startswith("— вытеснено")]
        assert marks == [5, 26], rows[:8]
        assert rows[4:7] == ["строка 4", rows[5], "строка 15"]
        assert "вытеснено строк: 10." in rows[5]
        assert rows[25:28] == ["строка 34", rows[26], "строка 40"]
        assert "вытеснено строк: 5." in rows[26]
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()


def test_the_panel_never_holds_more_lines_than_its_capacity(qapp) -> None:
    """После лишних строк в модели ровно предел, последние — самые свежие."""
    feed = PanelHandler()
    panel = DebugPanel(feed, sanitize=redact, theme=LIGHT, capacity=50)
    try:
        for number in range(120):
            emit(feed, f"строка {number}")
            if number % 40 == 39:
                panel.poll()
        assert panel.model.rowCount() == 50
        assert shown(panel)[-1] == "строка 119"
    finally:
        panel.deleteLater()
        qapp.processEvents()


def test_a_scrolled_up_view_keeps_its_line_and_a_bottom_view_follows(qapp) -> None:
    """Внизу — лента идёт за новыми строками; прокрутили вверх — верхняя строка стоит.

    Мутации: «всегда `scrollToBottom`» и «без поправки на ушедшие сверху ряды».
    """
    feed = PanelHandler()
    panel = DebugPanel(feed, sanitize=redact, theme=LIGHT, capacity=100)
    panel.resize(900, 300)
    panel.show()
    qapp.processEvents()
    try:
        bar = panel.view.verticalScrollBar()
        for number in range(150):
            emit(feed, f"строка {number}")
        panel.poll()
        qapp.processEvents()
        assert bar.maximum() > 0 and bar.value() == bar.maximum()
        emit(feed, "строка 150")
        panel.poll()
        qapp.processEvents()
        assert bar.value() == bar.maximum(), "лента не пошла за новой строкой"

        bar.setValue(50)
        qapp.processEvents()
        top = panel.model.line(panel.view.indexAt(QPoint(4, 4)).row()).text
        for number in range(151, 181):
            emit(feed, f"строка {number}")
        panel.poll()
        qapp.processEvents()
        assert panel.model.line(panel.view.indexAt(QPoint(4, 4)).row()).text == top
        assert bar.value() < bar.maximum()
    finally:
        panel.close()
        panel.deleteLater()
        qapp.processEvents()


def test_lines_are_painted_by_importance_in_both_themes() -> None:
    """Предупреждение — цветом предупреждения, ошибка — цветом беды, сделка — своим."""
    at = datetime(2026, 10, 12, 10, 0, tzinfo=MSK)
    rows = [
        DebugLine(seq=number + 1, time=at, level=level, source="журнал", text="строка")
        for number, level in enumerate(DecisionLevel)
    ]
    for theme in (LIGHT, DARK):
        model = DebugModel(theme)
        model.add(rows)
        expected = {
            DecisionLevel.INFO: theme.text,
            DecisionLevel.TRADE: theme.average,
            DecisionLevel.WARNING: theme.warning,
            DecisionLevel.ERROR: theme.danger,
        }
        for row, line in enumerate(rows):
            painted = model.data(model.index(row), FOREGROUND)
            assert painted == QColor(expected[line.level]), (theme.dark, line.level)


def test_a_theme_switch_repaints_the_lines_and_the_background(window, feed, qapp) -> None:
    """Смена темы окна перекрашивает строки, уже стоящие в панели, и её фон.

    Мутация — убрать вызов `debug_panel.set_theme` из `MainWindow.apply_theme`.
    """
    panel = window.debug_panel
    panel.button.click()
    emit(feed, "обычная строка")
    emit(feed, "предупреждение", logging.WARNING)
    panel.poll()
    model = panel.model
    assert model.data(model.index(1), FOREGROUND) == QColor(LIGHT.warning)
    assert LIGHT.background in panel.view.styleSheet()
    original = QPalette(qapp.palette())
    try:
        dark = QPalette(original)
        dark.setColor(QPalette.ColorRole.Window, QColor("#15181d"))
        qapp.setPalette(dark)
        qapp.processEvents()
        assert window.theme.dark
        assert model.data(model.index(0), FOREGROUND) == QColor(DARK.text)
        assert model.data(model.index(1), FOREGROUND) == QColor(DARK.warning)
        assert DARK.background in panel.view.styleSheet()
    finally:
        qapp.setPalette(original)
        qapp.processEvents()
    assert not window.theme.dark, "тема не вернулась — проверка протекла"


class _Leaky:
    """Лента, отдавшая строку с токеном: так выглядит пробитый первый рубеж."""

    def __init__(self, *texts: str, lost: int = 0, cause: str = "") -> None:
        at = datetime(2026, 10, 12, 10, 0, tzinfo=MSK)
        self._batch = DebugBatch(
            lines=tuple(
                DebugLine(seq=n + 1, time=at, level=DecisionLevel.INFO, source="журнал", text=t)
                for n, t in enumerate(texts)
            ),
            lost=lost,
            cause=cause,
        )
        self.taken = False

    def since(self, after: int, limit: int) -> DebugBatch:
        if self.taken:
            return DebugBatch(lost=self._batch.lost, cause=self._batch.cause)
        self.taken = True
        return self._batch


def test_copy_takes_the_selection_or_everything_and_cuts_the_token(qapp) -> None:
    """«Скопировать»: выделенное либо всё — и через чистку окна, второй рубеж."""
    win = _window(qapp, _Leaky("первая строка", f"токен {SECRET}", "третья строка"))
    assert REGISTRY.remember(SECRET)
    try:
        panel = win.debug_panel
        panel.button.click()
        qapp.processEvents()
        everything = panel.copy()
        assert QGuiApplication.clipboard().text() == everything
        assert SECRET not in everything and MASK in everything
        assert len(everything.splitlines()) == 3
        panel.view.selectionModel().select(
            panel.model.index(2), QItemSelectionModel.SelectionFlag.Select
        )
        assert panel.copy().endswith("журнал: третья строка")
        assert len(QGuiApplication.clipboard().text().splitlines()) == 1
    finally:
        REGISTRY.forget(SECRET)
        win.close()
        win.deleteLater()
        qapp.processEvents()


def test_lost_lines_are_named_in_the_header_not_swallowed(qapp) -> None:
    """Лента сообщила о несобранных строках — шапка панели называет число цветом предупреждения."""
    win = _window(qapp, _Leaky("строка", lost=3))
    try:
        panel = win.debug_panel
        panel.button.click()
        qapp.processEvents()
        assert "не собрано строк из-за ошибки: 3" in panel.counts.text()
        assert LIGHT.warning in panel.counts.styleSheet()
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()


def test_the_header_names_the_cause_of_the_first_lost_line(window, feed, qapp) -> None:
    """Обработчик ленты не собрал строку — шапка называет число и причину, подсказка тоже.

    Стережёт: причина первой несобранной строки видна человеку в шапке
    панели рядом с числом. Отказ самого обработчика (`handleError`)
    в технический журнал не пишется, и шапка — единственное место, где его
    причина видна. Мутация молчания — шапка с одним числом (или `since`
    без `cause`) — роняет проверку.
    """
    feed.handle(logging.makeLogRecord({
        "name": "app.port", "levelno": logging.WARNING, "levelname": "WARNING",
        "msg": "число %d", "args": ("не число",),
    }))
    panel = window.debug_panel
    panel.button.click()
    qapp.processEvents()
    text = panel.counts.text()
    assert "не собрано строк из-за ошибки: 1" in text, text
    assert "первая причина: TypeError" in text and "app.port" in text, text
    assert "Первая причина: TypeError" in panel.counts.toolTip()


def test_the_cause_in_the_header_and_its_tip_passes_the_window_cleaning(qapp) -> None:
    """Причина потери в шапке и в подсказке — после чистки окна, второй рубеж.

    Стережёт: причину первой несобранной строки шапка и подсказка
    показывают через `sanitize` окна, а не как её прислала лента. Чистка
    подставная (`cut`): так видно, что причина прошла именно её, а не что
    `scrub` нечего было вырезать. Мутация — причина мимо `sanitize` в шапке,
    в подсказке или в обеих — роняет проверку.
    """
    panel = DebugPanel(
        _Leaky("строка", lost=2, cause=f"KeyError: {MARK}"), sanitize=cut, theme=LIGHT
    )
    panel.show()
    qapp.processEvents()
    try:
        panel.poll()
        text, tip = panel.counts.text(), panel.counts.toolTip()
        said = "не собрано строк из-за ошибки: 2, первая причина: KeyError: [вырезано]"
        assert said in text, text
        assert "Первая причина: KeyError: [вырезано]" in tip, tip
        assert MARK not in text and MARK not in tip
    finally:
        panel.close()
        panel.deleteLater()
        qapp.processEvents()


def test_ctrl_c_copies_by_the_same_path_as_the_copy_button(qapp) -> None:
    """Ctrl+C на выделенной строке кладёт в буфер текст, прошедший чистку окна.

    Стережёт: Ctrl+C в списке ленты зовёт ту же процедуру, что «Скопировать»
    (`DebugPanel.copy`), а не штатное копирование Qt, которое кладёт в буфер
    текст ряда мимо чистки окна. Чистка здесь подставная: вырезает метку,
    которой нет в реестре секретов, — так виден именно путь, а не `scrub`.
    Мутация — снять перехват Ctrl+C (`_FeedView.keyPressEvent`) — роняет:
    в буфере оказывается метка.
    """
    feed = PanelHandler()
    panel = DebugPanel(feed, sanitize=cut, theme=LIGHT)
    panel.show()
    qapp.processEvents()
    try:
        emit(feed, "первая строка")
        emit(feed, f"вторая строка {MARK}")
        panel.poll()
        assert MARK in line_text(panel.model.line(1)), "метки нет в модели — проверка пуста"
        panel.view.setCurrentIndex(panel.model.index(1))
        QGuiApplication.clipboard().setText("до нажатия")
        QTest.keyClick(panel.view, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        copied = QGuiApplication.clipboard().text()
        assert copied != "до нажатия", "Ctrl+C ничего не положил в буфер"
        assert MARK not in copied, copied
        assert copied == cut(line_text(panel.model.line(1)))
    finally:
        panel.close()
        panel.deleteLater()
        qapp.processEvents()


def test_ctrl_c_without_a_selection_copies_the_whole_feed_like_the_copy_button(qapp) -> None:
    """Ctrl+C без выделения кладёт в буфер всю ленту, прошедшую чистку окна.

    Стережёт: перехват Ctrl+C работает и без выделения и ведёт в ту же
    `DebugPanel.copy` с её правилом «ничего не выделено — всё». Мутации —
    перехват только при выделении (штатное копирование Qt тогда кладёт
    текущий ряд мимо чистки или ничего) и перехват, копирующий один текущий
    ряд, — роняют проверку.
    """
    feed = PanelHandler()
    panel = DebugPanel(feed, sanitize=cut, theme=LIGHT)
    panel.show()
    qapp.processEvents()
    try:
        emit(feed, f"первая строка {MARK}")
        emit(feed, "вторая строка")
        emit(feed, "третья строка")
        panel.poll()
        assert not panel.view.selectionModel().selectedRows(), "есть выделение — проверка не про то"
        everything = "\n".join(line_text(panel.model.line(row)) for row in range(3))
        assert MARK in everything, "метки нет в модели — проверка пуста"
        QGuiApplication.clipboard().setText("до нажатия")
        QTest.keyClick(panel.view, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        copied = QGuiApplication.clipboard().text()
        assert copied == cut(everything), copied
        assert MARK not in copied
    finally:
        panel.close()
        panel.deleteLater()
        qapp.processEvents()


def test_a_window_without_a_feed_says_so(qapp) -> None:
    """Окно без торговой части: панель есть и говорит, что ленты нет, а не молчит."""
    win = _window(qapp, None)
    try:
        assert "лента не подключена" in win.debug_panel.counts.text()
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()
