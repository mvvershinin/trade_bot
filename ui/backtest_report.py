"""Окно отчёта о прогоне на истории: деньги, отрезок и чего в этих деньгах нет.

Зачем отдельное окно, если итог уже есть под журналом сделок
-------------------------------------------------------------
Под журналом стоит **строка** — одна на всю ширину, с числами через точку.
Она нужна там, где на неё смотрят мельком, между делом. Отчёт же читают ровно
один раз и внимательно: человек только что нажал «Прогнать» и ждёт ответа
на вопрос «сколько». Ответ этот способен обмануть его деньгами, и потому
рядом с ним обязано стоять то, чему в строке места нет:

* **на каком отрезке считано** и сколько в нём **торговых** дней. Отрезок
  19.06–26.08.2026 — это 69 календарных дней и 49 торговых; годовой ряд —
  365 против 280. Прибыль «за год» и прибыль «за 280 торговых дней» человек
  делит на разное;
* **чего в показанных деньгах нет** — не тремя фразами, как в строке, а всеми
  допущениями целиком, абзацами. Главное из них: проскальзывание по умолчанию
  ноль, и замер 05.09.2026 говорит, во что это обходится — 0 шагов дают
  +9 908 ₽, один шаг +4 394 ₽, два шага −153 ₽. Половина итога и весь итог.

Ни одно число здесь не считается. Всё приходит готовым в `ui.models.BacktestReport`
из `app/convert.py`, а допущения — из `backtest/assumptions.py`, где считаются
из самого прогона и разъехаться с его деньгами не могут.

Почему деньги стоят отдельными строками
----------------------------------------
Валовая, комиссия, чистая — три строки, а не «прибыль». Валовая прибыль в этом
проекте результатом не является (`DOMAIN.md` §5): реверсная система
на пятиминутках делает много переворотов, и комиссия — первый кандидат съесть
весь плюс. Подписи и порядок берутся из общей таблицы
(`ui.formatting.SUMMARY_FIELDS`), той же, по которой собирается строка под
журналом: два разных набора слов про одни и те же деньги были бы новой
путаницей.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QPlainTextEdit,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ui.formatting import EMPTY, SUMMARY_FIELDS, fmt_date, fmt_datetime, fmt_number
from ui.journals import trades_view
from ui.models import BacktestReport
from ui.theme import current as current_theme

__all__ = ["BacktestReportDialog", "period_lines"]


def period_lines(report: BacktestReport) -> list[str]:
    """На чём считано — строками, как их читает человек.

    ⚠️ Границы «просили» и «нашлось» показываются **обе** и всегда, даже когда
    совпадают. Совпадение — это тоже сведение: оно означает, что в базе есть
    весь запрошенный отрезок. Показывать одну строку вместо двух значит
    молчаливо утверждать, что посчитано ровно на том, что просили, — а в базе
    запросто нет первых дней отрезка либо нет последних.
    """
    asked = (
        f"{fmt_date(report.asked_since)} — {fmt_date(report.asked_until)}"
        if report.asked_since is not None and report.asked_until is not None
        else EMPTY
    )
    found = (
        f"{fmt_datetime(report.first_bar)} — {fmt_datetime(report.last_bar)}"
        if report.first_bar is not None and report.last_bar is not None
        else "свечей на этот отрезок в базе не нашлось"
    )
    return [
        f"Просили: {asked} МСК",
        f"Посчитано: {found} МСК",
        f"Свечей в прогоне: {fmt_number(report.bars, 0)}. "
        f"Торговых дней: {report.trading_days} "
        "(дней, в которые были торги, — не календарных)",
    ]


class BacktestReportDialog(QDialog):
    """Отчёт о прогоне. Показывает и молчит: ни одной команды отсюда не уходит.

    Немодальное окно намеренно: владелец счёта сверяет отчёт с графиком,
    на котором стоят метки этого же прогона, и заслонять график модальным
    окном значит заставить его закрыть отчёт, чтобы посмотреть на сделки.
    """

    def __init__(self, report: BacktestReport, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._report = report
        self._theme = current_theme()
        self.setWindowTitle("Отчёт о прогоне на истории")
        # Ширина выбрана по таблице сделок, а не на глаз: у неё девять
        # колонок, и тянущаяся из них — «Причина выхода», то есть человеческий
        # текст. На 940 точках свободной ширины не оставалось вовсе, и причина
        # выхода резалась до «конец …» — единственная колонка, ради которой
        # в эту таблицу и смотрят после денег.
        self.resize(1180, 780)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(self._head())
        layout.addWidget(self._money())
        layout.addWidget(self._headline())
        layout.addWidget(self._tabs(), 1)
        layout.addWidget(self._close_button())

    # -------------------------------------------------------------- части

    def _head(self) -> QWidget:
        """Чем и на чём гнали: инструмент, свеча, набор настроек, отрезок."""
        box = QGroupBox("Прогон")
        rows = QVBoxLayout(box)
        made = self._report.settings_source or "не названы"
        title = QLabel(
            f"{self._report.instrument or EMPTY} · свечи "
            f"{self._report.timeframe or EMPTY} · настройки: {made}"
        )
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        rows.addWidget(title)
        for line in period_lines(self._report):
            label = QLabel(line)
            label.setWordWrap(True)
            rows.addWidget(label)
        if self._report.halted:
            stopped = QLabel(f"⚠️ Робот остановлен на прогоне: {self._report.halted}")
            stopped.setWordWrap(True)
            stopped.setStyleSheet(f"color: {self._theme.danger}; font-weight: 600;")
            rows.addWidget(stopped)
        return box

    def _money(self) -> QWidget:
        """Деньги — по строке на число, а не всё в одну строку.

        Подписи и порядок из `SUMMARY_FIELDS`: сначала сколько сделок, потом
        деньги от валовой к чистой, комиссия между ними отдельной строкой.
        Валовая без комиссии в этом проекте не результат.
        """
        box = QGroupBox("Итог по деньгам")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        summary = self._report.summary
        if summary is None:
            form.addRow(QLabel("Итога нет: движок не отдал сводку по этому прогону."))
            return box
        for field in SUMMARY_FIELDS:
            value = getattr(summary, field.key)
            if value is None and field.omit_when_empty:
                continue
            cell = QLabel(field.render(value))
            cell.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            if field.key == "net_profit_rub" and value is not None:
                colour = self._theme.success if value >= 0 else self._theme.danger
                cell.setStyleSheet(f"color: {colour}; font-weight: 600;")
            form.addRow(f"{field.label}:", cell)
        return box

    def _headline(self) -> QWidget:
        """Оговорка рядом с числом — та же, что под журналом сделок.

        Берётся полем того же объекта, который несёт деньги
        (`TradesSummary.headline`), поэтому показать одно без другого нельзя
        даже по невнимательности.
        """
        summary = self._report.summary
        label = QLabel(summary.headline if summary is not None else "")
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setStyleSheet(f"color: {self._theme.warning}; font-weight: 600;")
        label.setVisible(bool(label.text()))
        return label

    def _tabs(self) -> QTabWidget:
        """Три вкладки: допущения, сделки, снимок настроек.

        Допущения стоят **первой** вкладкой, а не последней. Человек, открывший
        отчёт, читает деньги и первую вкладку; список сделок он смотрит,
        когда уже поверил числу.
        """
        tabs = QTabWidget()
        tabs.addTab(self._assumptions(), "Допущения прогона")
        tabs.addTab(trades_view(self._report.trades), "Сделки")
        tabs.addTab(self._settings_snapshot(), "Настройки прогона")
        tabs.setTabToolTip(
            0, "Чем показанный итог отличается от выписки со счёта. Пустым "
               "не бывает: отчёт не равен счёту ни при каких настройках."
        )
        tabs.setTabToolTip(1, "Что сделал бы робот: цены, объём, результат, комиссия.")
        tabs.setTabToolTip(
            2, "Все настройки прогона снимком — тем же, что уходит в запись прогона."
        )
        return tabs

    def _assumptions(self) -> QWidget:
        """Все допущения абзацами. Пустым этот список не бывает по построению."""
        page = QWidget()
        rows = QVBoxLayout(page)
        rows.setContentsMargins(10, 10, 10, 10)
        if not self._report.assumptions:
            # Пустой список означал бы «отчёт равен выписке со счёта».
            # Такого не бывает, и молчать об этом нельзя даже при пустом списке.
            rows.addWidget(QLabel(
                "Список допущений не пришёл. Это неисправность: отчёт не равен "
                "выписке со счёта ни при каких настройках, и оговорка обязана "
                "быть здесь."
            ))
        for item in self._report.assumptions:
            name = QLabel(item.name)
            font = name.font()
            font.setBold(True)
            name.setFont(font)
            if item.beside_number:
                name.setStyleSheet(f"color: {self._theme.warning};")
            rows.addWidget(name)
            body = QLabel(item.text)
            body.setWordWrap(True)
            body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            body.setContentsMargins(0, 0, 0, 10)
            rows.addWidget(body)
        rows.addStretch(1)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(page)
        return area

    def _settings_snapshot(self) -> QWidget:
        """Снимок настроек словами — тот же текст, что уходит в запись прогона.

        Текст, а не таблица полей: колонка `settings` в базе заведена под
        человеческий снимок и разбирается глазами. Человек видит здесь ровно
        то, что потом найдёт в `--runs`.
        """
        view = QPlainTextEdit(self._report.settings_text or "Снимок настроек не передан.")
        view.setReadOnly(True)
        return view

    def _close_button(self) -> QDialogButtonBox:
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("Закрыть")
        buttons.rejected.connect(self.reject)
        return buttons
