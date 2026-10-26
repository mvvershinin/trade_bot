"""Отчёт о прогоне без сделок: причина названа, про тариф — правда.

Снимок владельца счёта 29.09.2026: прогон в режиме «Только закрытие» при
тарифе 14 ₽ за контракт на сторону. Сделок 0 — это верно, режим новых
позиций не открывает. Но отчёт

* не сказал, почему сделок нет: «Сделок: 0» и пустые строки (правило 13);
* написал «Тариф не назван: показана валовая прибыль» — тариф был назван;
* по контрактам написал «комиссия тарифа нет, чистая тарифа нет».

Причина второго и третьего одна: сводка по пустому списку сделок пуста
при любом тарифе (`backtest.history.summarise`), и отчёт читал «тарифа нет»
из неё, а не из издержек прогона.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta

from app import convert
from app.port import HistoryPort
from backtest.assumptions import assumptions
from backtest.execution import Costs
from backtest.history import HistoryRun
from backtest.stitched import StitchedRun, stitched_lines
from market import MSK, HttpxTransport, MarketWorker, redact
from tests.test_ui_contract_load import TODAY, Chain, Heard, _noon, _seed_mxu6
from ui.backtest_report import BacktestReportDialog
from ui.models import BacktestRequest, Mode, Settings

TARIFF = Costs(commission_per_side=14.0)
COMMISSION_NOT_NAMED = "Комиссия не учтена"


def _names(run: HistoryRun) -> list[str]:
    return [one.name for one in assumptions(run)]


def test_a_named_tariff_is_not_called_missing_without_trades() -> None:
    """Тариф 14 ₽ и ноль сделок — оговорки «тариф не назван» нет.

    Мутация, обязанная ронять: вернуть в `backtest/assumptions.py::_facts_of`
    `known = summary.commission is not None`.
    """
    assert COMMISSION_NOT_NAMED not in _names(HistoryRun(costs=TARIFF)), (
        "тариф назван, сделок нет — а отчёт пишет, что тариф не назван"
    )


def test_a_missing_tariff_is_still_said_without_trades() -> None:
    """Обратная сторона: тарифа нет — оговорка остаётся и при нуле сделок.

    Стережёт правку выше от перегиба: «сделок нет — значит, и говорить
    не о чем» спрятало бы незаданный тариф.
    """
    assert COMMISSION_NOT_NAMED in _names(HistoryRun(costs=Costs()))


def test_the_stitched_line_without_trades_does_not_say_no_tariff() -> None:
    """«По контрактам» без сделок: «—», а не «тарифа нет».

    Мутация, обязанная ронять: убрать ветку `if not summary.trades`
    из `backtest/stitched.py::_summary_line`.
    """
    line = stitched_lines(StitchedRun())[0]
    assert "тарифа нет" not in line, f"без сделок написано «тарифа нет»: «{line}»"
    assert "комиссия —" in line and "чистая —" in line, line


def _request(values: Settings) -> BacktestRequest:
    return BacktestRequest(
        settings=values,
        since=datetime(2026, 9, 17, tzinfo=MSK),
        until=datetime(2026, 9, 29, tzinfo=MSK),
    )


#: Прогон, который свечи видел: без них причина другая (`no_trades_reason`).
SEEN = HistoryRun(costs=TARIFF, bars=240)


def test_close_only_names_the_mode_as_the_reason() -> None:
    """«Только закрытие» без сделок: причина — режим, названный по имени.

    Мутация, обязанная ронять: убрать `Mode.CLOSE_ONLY` из `_NO_ENTRY_MODES`.
    """
    report = convert.run_report(
        SEEN, [], _request(Settings()),
        settings_text="", mode=Mode.CLOSE_ONLY,
    )
    assert Mode.CLOSE_ONLY.label in report.no_trades, report.no_trades
    assert "новых позиций не открывает" in report.no_trades


def test_a_trading_mode_names_the_window_as_the_reason() -> None:
    """Режим с входами без сделок: причина — окно, с его границами."""
    values = Settings()
    report = convert.run_report(
        SEEN, [], _request(values),
        settings_text="", mode=Mode.REVERSE,
    )
    assert f"{values.window_start:%H:%M}" in report.no_trades, report.no_trades
    assert "не открывает" not in report.no_trades


def test_off_does_not_claim_it_closes_the_open_position() -> None:
    """«Выключен» без сделок: режим назван, и не сказано, что он закрывает открытую.

    Шаг 1 (`PROTOTYPE.md` §2) в режиме «Выключен» обрывает обработку свечи
    целиком — не закрывает ничего (находка ревью 29.09.2026).
    Мутация, обязанная ронять: отдать `Mode.OFF` текст «Только закрытие».
    """
    reason = convert.no_trades_reason(Mode.OFF, Settings(), SEEN)
    assert Mode.OFF.label in reason, reason
    assert "только закрывает" not in reason, reason
    assert "не закрывает" in reason, reason


def test_no_candles_is_the_reason_before_the_window_and_the_mode() -> None:
    """Прогон без свечей: причина — пустой отрезок, а не окно и не режим.

    Мутация, обязанная ронять: убрать ветку `if not run.bars`.
    """
    for mode in (Mode.REVERSE, Mode.CLOSE_ONLY):
        reason = convert.no_trades_reason(mode, Settings(), HistoryRun(costs=TARIFF))
        assert "нет ни одной свечи" in reason, reason
        assert "окн" not in reason and mode.label not in reason, reason


def test_a_halted_run_names_the_halt_not_the_window() -> None:
    """Прогон, оборванный остановкой: причина — остановка, вина не на окне.

    Мутация, обязанная ронять: убрать ветку `if run.halted`.
    """
    halted = HistoryRun(costs=TARIFF, bars=240, halted="расхождение позиции")
    reason = convert.no_trades_reason(Mode.REVERSE, Settings(), halted)
    assert "остановлен" in reason, reason
    assert "торгового окна" not in reason, reason


def test_the_report_window_shows_the_reason_first(qapp) -> None:
    """Окно отчёта показывает причину первой строкой, а не прячет.

    Мутация, обязанная ронять: убрать `no_trades=` из `convert.run_report`
    либо ярлык `no_trades` из раскладки окна отчёта.
    """
    report = convert.run_report(
        SEEN, [], _request(Settings()),
        settings_text="", mode=Mode.CLOSE_ONLY,
    )
    dialog = BacktestReportDialog(report)
    try:
        layout = dialog.layout()
        assert layout is not None
        item = layout.itemAt(0)
        assert item is not None
        first = item.widget()
        assert first is dialog.no_trades, "причина стоит не первой строкой отчёта"
        assert not dialog.no_trades.isHidden() and dialog.no_trades.text()
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_the_port_reports_the_mode_it_actually_ran_in(loop, tmp_path, monkeypatch) -> None:
    """Порт отдаёт отчёту свой режим и тариф: причина — режим, тариф назван.

    Остальные проверки зовут `convert.run_report` с режимом сами — и зеленеют,
    даже если порт отдаёт отчёту не тот режим, в котором гнал прогон.
    Тогда отчёт «Только закрытие» объяснял бы пустоту торговым окном.
    Мутация, обязанная ронять (проверено 29.09.2026, до этого теста не
    ловилась ничем): `mode=Mode.REVERSE` вместо `mode=self._mode`
    в `HistoryPort._report`.
    """
    def refuse(self: object, url: str, *, timeout: float) -> bytes:
        raise AssertionError(f"тест полез в настоящий интернет: {url}")

    monkeypatch.setattr(HttpxTransport, "get", refuse)
    database = tmp_path / "base.sqlite3"
    _seed_mxu6(database, (9, 8, 7))
    chain = Chain(TODAY)

    async def main():
        worker = MarketWorker(database, iss=chain.client)
        await worker.open()
        port = HistoryPort(worker, values=Settings(instrument="MXU6"), days=0,
                           sanitize=redact, clock=_noon(TODAY), mode=Mode.CLOSE_ONLY)
        heard = Heard(port)
        try:
            await port.wait()
            port.run_backtest(BacktestRequest(
                settings=Settings(instrument="MXU6"),
                since=datetime.combine(TODAY - timedelta(days=9), time(0), MSK),
                until=datetime.combine(TODAY - timedelta(days=7), time(23, 59), MSK),
            ))
            for _ in range(5):
                await port.wait()
                await asyncio.sleep(0.02)
        finally:
            await port.aclose()
            await worker.close()
        return heard

    heard = loop.run_until_complete(main())
    assert heard.reports, f"отчёт прогона не пришёл: {[n.reason for n in heard.notes][-3:]}"
    report = heard.reports[-1]
    assert Mode.CLOSE_ONLY.label in report.no_trades, report.no_trades
    # Тариф из настроек (умолчание окна) доехал до прогона: без сделок
    # отчёт не называет его «не назван». Мутация, которую это ловит:
    # издержки прогона собраны без комиссии (`convert` → `Costs()` без
    # `commission_per_side`).
    assert Settings().commission_per_side_rub is not None, "проверка вакуумна"
    assert COMMISSION_NOT_NAMED not in [one.name for one in report.assumptions], (
        "тариф задан в настройках, а отчёт без сделок пишет, что он не назван"
    )
