"""Окно и действующий контракт: плашка, кнопка перехода, поле настроек, стык.

Что стережётся, словами
-----------------------
1. **Плашка говорит о расхождении**: робот настроен на MXU6, действующий —
   MXZ6. Совпадение и пустая таблица плашку не показывают; истёкший
   «действующий» (беда таблицы) — показывает.
2. **Кнопка перехода заблокирована с объяснением**, пока открыта позиция
   или робот не выключен (решение 0016, бриф окна §6).
3. **Переход идёт подтверждением**: отказ в подтверждении не меняет
   ничего, согласие отдаёт порту настройки с новым кодом и прочими
   значениями прежними.
4. **Поле настроек не меняется само**: «Подставить» ставит код в поле,
   до этого поле остаётся прежним.
5. **Стык на графике нарисован**: в столбце первой свечи нового контракта
   есть цвет метки стыка.
6. **Окно загрузки не обещает «90 дней»** для контракта.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

import ui.main_window
from market.journal import redact
from ui.contract_banner import ContractBar, blocked_reason, notice_text
from ui.formatting import MSK
from ui.history_dialog import HistoryDialog
from ui.main_window import MainWindow
from ui.models import (
    Candle,
    ChartData,
    ContractNotice,
    ContractSeam,
    HistoryFacts,
    Mode,
    Position,
    RobotState,
    Settings,
    Side,
)
from ui.ports import TerminalPort
from ui.settings_dialog import SettingsDialog


class RecordingPort(TerminalPort):
    """Порт, который запоминает команды окна. Свой: `helpers` не виден `mypy`."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, object]] = []

    def apply_settings(self, settings: Settings) -> None:
        self.calls.append(("apply_settings", settings))

MISMATCH = ContractNotice(
    configured="MXU6", current="MXZ6", current_since=date(2026, 9, 17),
    last_trade_day=date(2026, 12, 17),
)


def _position() -> Position:
    return Position(side=next(iter(Side)), volume=1.0, entry_price=100.0)


def test_the_banner_speaks_only_when_there_is_something_to_say() -> None:
    """Стережёт 1: расхождение и беда — текст; совпадение и пустая таблица — нет."""
    assert "MXZ6" in notice_text(MISMATCH) and "MXU6" in notice_text(MISMATCH)
    assert notice_text(ContractNotice(configured="MXZ6", current="MXZ6")) == ""
    assert notice_text(ContractNotice(configured="MXU6", trouble="пусто")) == ""
    urgent = ContractNotice(configured="MXU6", trouble="MXU6 истёк.", urgent=True)
    assert "MXU6 истёк." in notice_text(urgent)


@pytest.mark.parametrize(
    ("state", "blocked"),
    [
        (RobotState(mode=Mode.OFF), False),
        (RobotState(mode=Mode.REVERSE, running=False), False),
        (RobotState(mode=Mode.REVERSE, position=_position()), False),
        (RobotState(mode=Mode.REVERSE, running=True), True),
        (RobotState(mode=Mode.OFF, position=_position(), simulation=False), True),
    ],
    ids=["off", "history-replay", "replay-position", "running", "real-position"],
)
def test_the_switch_is_blocked_with_a_reason_while_trading(qapp, state, blocked) -> None:
    """Стережёт 2: кнопка заблокирована и объясняет почему — до нажатия."""
    bar = ContractBar()
    try:
        bar.show_notice(MISMATCH, state)
        assert bar.button.isEnabled() is not blocked
        assert bool(blocked_reason(state)) is blocked
        if blocked:
            assert bar.button.toolTip() == blocked_reason(state)
    finally:
        bar.deleteLater()


@pytest.fixture
def window(qapp):
    port = RecordingPort()
    made = MainWindow(port=port, settings=Settings(instrument="MXU6", volume=3),
                      sanitize=redact)
    yield made, port
    made.deleteLater()
    qapp.processEvents()


def test_the_window_shows_the_notice_that_the_port_sent(window) -> None:
    """Стережёт проводку: сигнал порта доходит до плашки (правило 13)."""
    made, port = window
    port.contract_checked.emit(MISMATCH)
    assert not made.contract_bar.isHidden()
    assert "MXZ6" in made.contract_bar.label.text()


def test_a_refused_confirmation_switches_nothing(window, monkeypatch) -> None:
    """Стережёт 3: «Не применять» — тикер прежний, порту ничего не ушло."""
    made, port = window
    monkeypatch.setattr(ui.main_window, "confirm_changes", lambda *a, **k: False)
    port.contract_checked.emit(MISMATCH)
    made.contract_bar.button.click()
    assert not [call for call in port.calls if call[0] == "apply_settings"]


def test_a_confirmed_switch_sends_the_new_code_and_keeps_the_rest(window, monkeypatch) -> None:
    """Стережёт 3: согласие — новый код, остальные настройки прежние."""
    made, port = window
    asked: list[tuple[Settings, Settings]] = []

    def confirm(parent, previous, now, *, lead=""):
        asked.append((previous, now))
        return True

    monkeypatch.setattr(ui.main_window, "confirm_changes", confirm)
    port.contract_checked.emit(MISMATCH)
    made.contract_bar.button.click()
    sent = [call[1] for call in port.calls if call[0] == "apply_settings"]
    assert asked, "переход прошёл мимо подтверждения"
    assert [one.instrument for one in sent] == ["MXZ6"]
    assert sent[0].volume == 3


def test_the_switch_does_nothing_while_the_robot_trades(window, monkeypatch) -> None:
    """Стережёт 2 до конца: заблокированную кнопку не обойти вызовом слота."""
    made, port = window
    monkeypatch.setattr(ui.main_window, "confirm_changes", lambda *a, **k: True)
    port.contract_checked.emit(MISMATCH)
    made.contract_bar.update_state(RobotState(mode=Mode.REVERSE, running=True))
    made.contract_bar.switch_requested.emit("MXZ6")
    assert not [call for call in port.calls if call[0] == "apply_settings"]


def test_the_settings_field_changes_only_by_the_button(qapp) -> None:
    """Стережёт 4: поле «Инструмент» само не меняется, «Подставить» — меняет."""
    dialog = SettingsDialog(Settings(instrument="MXU6"))
    try:
        dialog.set_contract(MISMATCH)
        assert dialog.instrument.text() == "MXU6"
        assert "MXZ6" in dialog.contract_note.text()
        assert not dialog.contract_use.isHidden()
        dialog.contract_use.click()
        assert dialog.instrument.text() == "MXZ6"
        assert dialog.contract_use.isHidden()
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_the_seam_is_drawn_at_the_first_candle_of_the_new_contract(qapp) -> None:
    """Стережёт 5: в столбце стыка есть цвет метки стыка, без стыка — нет."""
    from PySide6.QtGui import QColor, QImage

    from ui.chart.painter_surface import PainterChartSurface
    from ui.theme import current as current_theme

    start = datetime(2026, 9, 16, 10, 0, tzinfo=MSK)
    candles = tuple(
        Candle(opens_at=start + timedelta(minutes=5 * i), open=p, high=p + 1,
               low=p - 1, close=p, volume=1.0)
        for i, p in enumerate([100.0] * 20 + [300.0] * 20)
    )
    seam = ContractSeam(time=candles[20].opens_at, symbol="MXZ6", previous="MXU6")
    warning = QColor(current_theme().warning)

    def warning_columns(data: ChartData) -> int:
        surface = PainterChartSurface()
        try:
            surface.resize(900, 400)
            surface.show_chart(data)
            image = QImage(900, 400, QImage.Format.Format_RGB32)
            surface.render(image)
            return sum(
                1 for x in range(image.width())
                if any(image.pixelColor(x, y) == warning for y in range(0, 400, 4))
            )
        finally:
            surface.deleteLater()

    plain = ChartData(instrument="MXZ6", timeframe="5 минут", candles=candles)
    with_seam = ChartData(instrument="MXZ6", timeframe="5 минут", candles=candles,
                          seams=(seam,))
    assert warning_columns(with_seam) > warning_columns(plain), "стык не нарисован"


def test_the_load_dialog_does_not_promise_days_for_a_contract(qapp) -> None:
    """Стережёт 6 и B-069: отрезок с даты по сегодня, каждым ближним контрактом.

    Без обещания «90 дн.». До B-069 окно называло рубеж одного контракта из настроек — и грузило
    только его. Теперь отрезок задаёт дата из окна (умолчание — глубина
    из настроек), а контракты по дням — таблица.
    """
    facts = HistoryFacts(symbol="MXZ6", by_contract=True,
                         contract_from=date(2026, 9, 17), warmup_bars=15)
    dialog = HistoryDialog(facts, Settings(history_depth_days=90), today=date(2026, 9, 27))
    try:
        text = dialog.summary.text()
        assert "с 30.06.2026 по сегодня" in text and "15 баров" in text, text
        assert "каждый ближний контракт" in text and "сверка дней с биржей" in text, text
        assert "дн.)" not in text, text
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_the_load_dialog_names_both_outcomes_while_unchecked(qapp) -> None:
    """Квартальность не подтверждена: окно говорит и «по дням» — как будет у BR."""
    facts = HistoryFacts(symbol="BRZ6", by_contract=True, warmup_bars=15,
                         quarterly_unchecked=True)
    dialog = HistoryDialog(facts, Settings(history_depth_days=90), today=date(2026, 9, 27))
    try:
        text = dialog.summary.text()
        assert "месячные контракты" in text and "90 дн.)" in text, text
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_the_tester_picks_a_period_not_a_ticker(qapp) -> None:
    """Тестер при известных периодах выбирает период, а не тикер (0061).

    Выбор один и назван словами; инструмент в просьбе — тот, что стоит
    в настройках; подпись охвата называет каждый контракт с его отрезком.
    """
    from ui.backtest_dialog import BY_PERIOD, BacktestDialog
    from ui.models import BacktestOptions, InstrumentInfo

    def at(day: int) -> datetime:
        return datetime(2026, 9, day, 10, 0, tzinfo=MSK)

    options = BacktestOptions(
        instruments=(InstrumentInfo("MXU6", at(1), at(17), 10),
                     InstrumentInfo("MXZ6", at(10), at(25), 10)),
        periods=(InstrumentInfo("MXU6", at(1), at(16), 10),
                 InstrumentInfo("MXZ6", at(17), at(25), 10)),
    )
    dialog = BacktestDialog(options, Settings(instrument="MXZ6", depth_days=0))
    try:
        assert dialog.instrument.count() == 1
        assert dialog.instrument.currentText() == BY_PERIOD
        assert not dialog.instrument.isEnabled()
        note = dialog.coverage.text()
        assert "MXU6 01.09.2026" in note and "MXZ6 17.09.2026" in note, note
        request = dialog.request()
        assert request is not None
        assert request.settings.instrument == "MXZ6"
        assert request.since.date() == date(2026, 9, 1)
        assert request.until.date() == date(2026, 9, 25)
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_the_report_shows_the_split_by_contract(qapp) -> None:
    """Отчёт прогона по склейке показывает строки по контрактам; одним — нет."""
    from ui.backtest_report import BacktestReportDialog
    from ui.models import BacktestReport

    lines = ("Итог по склейке: сделок 2", "Смена контракта MXU6 → MXZ6: шорт закрыт")
    shown = BacktestReportDialog(BacktestReport(contracts=lines))
    plain = BacktestReportDialog(BacktestReport())
    try:
        from PySide6.QtWidgets import QGroupBox, QLabel

        def split(dialog) -> QGroupBox | None:
            return next((box for box in dialog.findChildren(QGroupBox)
                         if box.title() == "По контрактам"), None)

        box = split(shown)
        assert box is not None and not box.isHidden()
        texts = [label.text() for label in box.findChildren(QLabel)]
        assert list(lines) == texts
        other = split(plain)
        assert other is None or other.isHidden()
    finally:
        shown.deleteLater()
        plain.deleteLater()
        qapp.processEvents()


def test_the_average_line_breaks_at_the_seam(qapp) -> None:
    """Линия средней не соединяет два контракта: у каждого своя средняя.

    Мутация: линия одной ломаной через стык — между последней точкой MXU6
    и первой MXZ6 появляется почти вертикальный отрезок цвета средней.
    """
    from PySide6.QtGui import QColor, QImage

    from ui.chart.painter_surface import PainterChartSurface
    from ui.models import LinePoint
    from ui.theme import current as current_theme

    start = datetime(2026, 9, 16, 10, 0, tzinfo=MSK)
    prices = [100.0] * 20 + [300.0] * 20
    candles = tuple(
        Candle(opens_at=start + timedelta(minutes=5 * i), open=p, high=p + 1,
               low=p - 1, close=p, volume=1.0)
        for i, p in enumerate(prices)
    )
    average = tuple(LinePoint(c.opens_at, c.close) for c in candles)
    seam = ContractSeam(time=candles[20].opens_at, symbol="MXZ6", previous="MXU6")
    colour = QColor(current_theme().average)

    def middle_pixels(data: ChartData) -> int:
        surface = PainterChartSurface()
        try:
            surface.resize(900, 400)
            surface.show_chart(data)
            image = QImage(900, 400, QImage.Format.Format_RGB32)
            surface.render(image)
            # Средняя по высоте треть — между уровнями 100 и 300.
            return sum(
                1 for x in range(image.width()) for y in range(150, 250)
                if image.pixelColor(x, y) == colour
            )
        finally:
            surface.deleteLater()

    joined = ChartData(instrument="MXZ6", timeframe="5 минут", candles=candles,
                       average=average)
    broken = ChartData(instrument="MXZ6", timeframe="5 минут", candles=candles,
                       average=average, seams=(seam,))
    assert middle_pixels(joined) > 0, "без стыка линии через середину нет — проверка пуста"
    assert middle_pixels(broken) == 0, "линия средней прошла через стык"
