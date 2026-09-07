"""Окно «Прогон на истории»: инструмент, отрезок, чем гнать.

Зачем оно есть
--------------
Прогон по истории в программе работал с первого дня — но запускался **только
из консоли**: `python3 -m app.main --symbol MXU6 --days 90 --until 26.08.2026`.
Владелец счёта не программист, консоли для него не существует, и значит для
него не существовало и прогона. Этап Э1 закрывается ровно этим окном: «выбрали
инструмент, период и параметры → нажали → получили отчёт».

Три поля и почему именно они
-----------------------------
**Инструмент** — список того, что есть в базе, а не поле ввода. Код фьючерса
(`MXU6`) человек помнить не обязан, а опечатка в нём даёт пустой график
и вопрос «почему ничего не показывает». Рядом с каждым кодом сказано, за какой
отрезок по нему есть свечи.

⚠️ Собранных нами рядов (`@MX`) в списке нет. Их отсеивает слой сборки
(`app/port.py`), а не окно: цены на стыках такого ряда не подгоняются,
торговать по нему нельзя, и код этот бирже неизвестен (решение 0049).

**Отрезок** — две даты, обе включительно и обе московские. Границы прижаты
к тому, что есть в базе по выбранному инструменту: выбрать даты, за которые
данных нет, нельзя вовсе — это дешевле, чем объяснять потом пустой отчёт.

**Чем гнать** — текущие настройки окна либо сохранённый шаблон. Третьего
не бывает: набор настроек в программе один и тот же и для прогона, и для боя,
и заводить здесь отдельные «параметры прогона» значило бы завести вторую
правду о том, как ведёт себя робот.

Что это окно меняет и почему об этом сказано крупно
----------------------------------------------------
Прогон **применяет** выбранные настройки: после нажатия в окне настроек будет
то, чем гнали, а на графике — сделки этого прогона. Иначе на экране оказались
бы сделки одних настроек рядом с полями других, и различить их было бы нечем.
Об этом сказано в самом окне, до нажатия, а не в докстринге.

Торговых правил здесь нет ни одного. Окно собирает просьбу и отдаёт её порту;
что из неё выйдет, решают `engine/` и `backtest/`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta

from PySide6.QtCore import QDate, QLocale, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ui.formatting import MSK, fmt_date, to_msk
from ui.models import BacktestOptions, BacktestRequest, InstrumentInfo, Settings
from ui.templates import Template
from ui.theme import current as current_theme

__all__ = ["BacktestDialog", "CURRENT_SETTINGS", "day_bounds"]

#: Как называется выбор «взять то, что стоит в окне настроек прямо сейчас».
#: Строка уходит в отчёт и в журнал решений, поэтому она одна на все места.
CURRENT_SETTINGS = "текущие настройки"


def day_bounds(since: date, until: date) -> tuple[datetime, datetime]:
    """Две даты → два момента МСК: начало первого дня и конец последнего.

    Обе границы **включительно**, и это не мелочь. Человек, выбравший
    «по 26 августа», имеет в виду весь день целиком; отрезок, обрезанный
    полуночью, потерял бы последний торговый день молча — а последний день
    в подборе настроек как раз самый интересный.

    Пояс проставляется явно: наивное время уехало бы на три часа на машине,
    стоящей не в Москве, и уехало бы молча.
    """
    return (
        datetime.combine(since, time(0, 0), tzinfo=MSK),
        datetime.combine(until, time(23, 59, 59), tzinfo=MSK),
    )


def _qdate(day: date) -> QDate:
    """Дата Python → дата Qt, по трём числам.

    Тремя числами, а не одним объектом, намеренно: перегрузка `QDate(date)`
    в PySide6 есть, но в описании типов её нет, и проверка типов на неё
    ругается. Три числа — та же дата и ни одного подавления.
    """
    return QDate(day.year, day.month, day.day)


def _pydate(day: QDate) -> date:
    """Дата Qt → дата Python. Пара к `_qdate`, по той же причине."""
    return date(day.year(), day.month(), day.day())


def _covered(info: InstrumentInfo | None) -> tuple[date, date] | None:
    """Первый и последний день, за которые по инструменту есть свечи.

    `None` — данных нет вовсе. Одно место на оба вопроса («можно ли выбирать
    даты» и «какие»), потому что ответ на них обязан быть один: поле,
    включённое без границ, принимает любую дату и молча возвращает пустой
    прогон.
    """
    if info is None or info.first is None or info.last is None:
        return None
    return to_msk(info.first).date(), to_msk(info.last).date()


def _coverage_note(info: InstrumentInfo | None) -> str:
    """Что есть в базе по инструменту — одной строкой человеку."""
    if info is None:
        return "Инструмент не выбран."
    if info.first is None or info.last is None:
        return f"По {info.symbol} в базе нет ни одной свечи."
    # ⚠️ Разряды отбиваются **у числа**, а не заменой запятых во всей строке.
    # Первая редакция делала `f"…{minutes:,}".replace(",", " ")` и съедала
    # заодно запятую после даты: «по 04.09.2026  минуток 82 337».
    minutes = f"{info.minutes:,}".replace(",", "\u00a0")
    return (
        f"В базе по {info.symbol}: с {fmt_date(info.first)} по "
        f"{fmt_date(info.last)}, минутных свечей {minutes}. "
        "Даты вне этого отрезка выбрать нельзя."
    )


class BacktestDialog(QDialog):
    """Просьба о прогоне: инструмент, отрезок, чем гнать.

    Возвращает `request()` — `None`, пока выбор не сложился в просьбу.
    Ничего не запускает и ни к чему не обращается: запуск — дело порта.
    """

    def __init__(
        self,
        options: BacktestOptions,
        settings: Settings,
        templates: Sequence[Template] = (),
        parent: QWidget | None = None,
    ) -> None:
        """:param options: из чего выбирать — инструменты и их покрытие.

        :param settings: то, что стоит в окне настроек сейчас.
        :param templates: сохранённые наборы. Пусто — выбор шаблона недоступен
            и об этом сказано словами, а не пустым списком.
        """
        super().__init__(parent)
        self.setWindowTitle("Прогон на истории")
        self.setModal(True)
        self.resize(660, 520)

        self._options = options
        self._settings = settings
        self._templates = tuple(templates)
        self._theme = current_theme()

        self._build_widgets()
        self._lay_out()
        self._set_tab_order()
        self._pick_instrument(0)
        self._sync()

    # ------------------------------------------------------------- сборка

    def _build_widgets(self) -> None:
        """Поля окна. Раскладка — отдельно: так короче каждое из двух."""
        self.instrument = QComboBox()
        for info in self._options.instruments:
            self.instrument.addItem(info.symbol, info)
        self.instrument.setToolTip(
            "Чем торговать в прогоне. В списке только то, по чему в базе есть "
            "свечи. Кода, которого здесь нет, программа не знает: историю "
            "по нему сначала надо загрузить."
        )
        self.instrument.currentIndexChanged.connect(self._pick_instrument)

        self.coverage = QLabel()
        self.coverage.setWordWrap(True)

        self.since = self._date_field(
            "С какого дня считать. День берётся целиком, с начала торгов."
        )
        self.until = self._date_field(
            "По какой день считать включительно. Последний день берётся "
            "целиком, до конца вечерней сессии."
        )
        self.since.dateChanged.connect(self._sync)
        self.until.dateChanged.connect(self._sync)

        self.use_current = QRadioButton(f"Взять {CURRENT_SETTINGS}")
        self.use_current.setChecked(True)
        self.use_current.setToolTip(
            "Прогнать тем, что стоит в окне «Параметры робота» прямо сейчас."
        )
        self.use_template = QRadioButton("Взять шаблон")
        self.use_template.setToolTip(
            "Прогнать сохранённым набором. Набор применится целиком — "
            "и к прогону, и к окну настроек."
        )
        self.template = QComboBox()
        for item in self._templates:
            self.template.addItem(item.name, item)
        self.template.setEnabled(False)
        self.use_current.toggled.connect(self._sync)
        self.use_template.toggled.connect(self._sync)
        self.template.currentIndexChanged.connect(self._sync)

        self.summary = QLabel()
        self.summary.setWordWrap(True)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.run_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.run_button.setText("Прогнать")
        self.run_button.setDefault(True)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

    def _date_field(self, tip: str) -> QDateEdit:
        """Поле даты: календарь по кнопке, русские месяцы, ввод с клавиатуры.

        Локаль задаётся явно. Qt берёт её у системы, а машина владельца счёта
        вполне может стоять в английской: «June 2026» и неделя с воскресенья —
        чужой календарь, в котором он промахнётся мимо дня.
        """
        field = QDateEdit()
        field.setCalendarPopup(True)
        field.setDisplayFormat("dd.MM.yyyy")
        field.setLocale(QLocale("ru_RU"))
        field.calendarWidget().setLocale(QLocale("ru_RU"))
        field.calendarWidget().setFirstDayOfWeek(Qt.DayOfWeek.Monday)
        field.setToolTip(tip)
        return field

    def _lay_out(self) -> None:
        """Разложить сверху вниз, как читают глазами."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(self._lead())

        what = QGroupBox("Что и за какой отрезок")
        form = QFormLayout(what)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.addRow("Инструмент", self.instrument)
        dates = QHBoxLayout()
        dates.addWidget(QLabel("с"))
        dates.addWidget(self.since)
        dates.addWidget(QLabel("по"))
        dates.addWidget(self.until)
        dates.addWidget(QLabel("включительно, время московское"))
        dates.addStretch(1)
        form.addRow("Отрезок", dates)
        form.addRow("", self.coverage)
        layout.addWidget(what)

        source = QGroupBox("Чем гнать")
        rows = QVBoxLayout(source)
        rows.addWidget(self.use_current)
        picked = QHBoxLayout()
        picked.addWidget(self.use_template)
        picked.addWidget(self.template, 1)
        rows.addLayout(picked)
        layout.addWidget(source)

        layout.addWidget(self.summary)
        layout.addStretch(1)
        layout.addWidget(self._warning())
        layout.addWidget(self.buttons)

    def _lead(self) -> QLabel:
        """Что это окно делает — до того, как в него что-то нажали."""
        label = QLabel(
            "Робот пройдёт по прошлым свечам из базы и покажет, какие сделки "
            "он сделал бы. Заявки при этом никуда не уходят: на бирже не "
            "происходит ничего."
        )
        label.setWordWrap(True)
        return label

    def _warning(self) -> QLabel:
        """Предупреждение о том, что прогон меняет состояние окна.

        Стоит вплотную к кнопке «Прогнать» намеренно: это последнее, что
        человек читает перед нажатием, и единственное последствие, которого
        он не ждёт.
        """
        label = QLabel(
            "⚠️ Прогон применит выбранные настройки: они встанут в окне "
            "«Параметры робота», а на графике окажутся сделки этого прогона. "
            "Живые свечи на выбранный отрезок приходить не будут, пока он "
            "не снят — снимается кнопкой «Вернуться к текущим данным», "
            "она появится на панели сверху."
        )
        label.setWordWrap(True)
        label.setStyleSheet(f"color: {self._theme.warning};")
        return label

    def _set_tab_order(self) -> None:
        """Порядок обхода клавишей Tab — сверху вниз, как читают."""
        self.setTabOrder(self.instrument, self.since)
        self.setTabOrder(self.since, self.until)
        self.setTabOrder(self.until, self.use_current)
        self.setTabOrder(self.use_current, self.use_template)
        self.setTabOrder(self.use_template, self.template)
        self.setTabOrder(self.template, self.buttons)

    # -------------------------------------------------------------- выбор

    def _pick_instrument(self, index: int) -> None:
        """Прижать календари к тому, что есть в базе по выбранному коду.

        Границы ставятся **до** подстановки значений: `QDateEdit` молча
        подтягивает значение внутрь допустимого отрезка, и обратный порядок
        дал бы дату, которой человек не выбирал.
        """
        raw = self.instrument.itemData(index) if index >= 0 else None
        info = raw if isinstance(raw, InstrumentInfo) else None
        self.coverage.setText(_coverage_note(info))
        span = _covered(info)
        for field in (self.since, self.until):
            field.setEnabled(span is not None)
        if span is None:
            self._sync()
            return
        first, last = span
        for field in (self.since, self.until):
            field.setDateRange(_qdate(first), _qdate(last))
        self.since.setDate(_qdate(max(first, self._earliest(last))))
        self.until.setDate(_qdate(last))
        self._sync()

    def _earliest(self, last: date) -> date:
        """С какого дня предложить отрезок. Число — из настроек, не своё.

        Своей константы у окна больше нет, и это правка по замеру: до
        06.09.2026 в программе жили три числа глубины — показ 90 в настройках,
        загрузка 30 в слое данных и 90, зашитое здесь. Владелец счёта ставил
        180, прогон предлагал 90, а скачивалось 30 (`D-068`). Теперь окно
        берёт то же число, которое стоит в поле «Показывать на графике, дней»:
        сколько показано, столько и предлагается прогнать.

        Ноль в настройках означает «вся история», и тогда отрезком
        предлагается весь охват базы. Это осознанно дорого — на годовом ряде
        счёт идёт десятки секунд, — но выбрал это владелец счёта, а не окно
        за него.

        Дней считается **включительно**: «90 дней» — это 90 дней вместе
        с последним, а не 91. Иначе одно и то же число в двух местах окна
        означало бы два разных отрезка.
        """
        days = self._settings.depth_days
        if days <= 0:
            return date.min
        return last - timedelta(days=days - 1)

    def _chosen_template(self) -> Template | None:
        """Выбранный шаблон либо `None`, если гоним текущими настройками."""
        if not self.use_template.isChecked():
            return None
        data = self.template.currentData()
        return data if isinstance(data, Template) else None

    def _sync(self, *_: object) -> None:
        """Свести окно к согласованному виду и сказать, что получится."""
        self.template.setEnabled(
            self.use_template.isChecked() and bool(self._templates)
        )
        self.use_template.setEnabled(bool(self._templates))
        if not self._templates:
            self.use_template.setToolTip(
                "Сохранённых наборов нет. Сохранить текущие можно в окне "
                "«Шаблоны настроек»."
            )
        request = self.request()
        self.run_button.setEnabled(request is not None)
        self.summary.setText(self._summary_text(request))

    def _summary_text(self, request: BacktestRequest | None) -> str:
        """Что именно будет прогнано — одной строкой, до нажатия.

        Размер свечи назван здесь, а не полем: он часть выбранного набора
        настроек, и шаблон способен его сменить. Человек, выбравший шаблон
        с пятнадцатиминутками, обязан увидеть это до нажатия, а не по числу
        свечей в отчёте.
        """
        if request is None:
            if self._options.trouble:
                return self._options.trouble
            if not self._options.instruments:
                return (
                    "В базе нет ни одного инструмента. Историю надо загрузить: "
                    "без свечей прогонять нечего."
                )
            return "Выбор не сложился: проверьте инструмент и даты."
        return (
            f"Будет прогнано: {request.settings.instrument}, свечи "
            f"{request.settings.timeframe}, с {fmt_date(request.since)} "
            f"по {fmt_date(request.until)} МСК, настройки — "
            f"{request.settings_source}."
        )

    # -------------------------------------------------------------- ответ

    def request(self) -> BacktestRequest | None:
        """Просьба о прогоне. `None` — выбор ещё не сложился в просьбу.

        Инструмент подставляется **в настройки**, а не едет рядом с ними:
        второе место, где задан инструмент, разошлось бы с первым молча,
        и прогон пошёл бы не по тому ряду, что показан в настройках.
        """
        info = self.instrument.currentData()
        if not isinstance(info, InstrumentInfo):
            return None
        if info.first is None or info.last is None:
            return None
        template = self._chosen_template()
        if self.use_template.isChecked() and template is None:
            return None
        values = template.values if template is not None else self._settings
        source = template.name if template is not None else CURRENT_SETTINGS
        since, until = day_bounds(_pydate(self.since.date()), _pydate(self.until.date()))
        if until < since:
            return None
        return BacktestRequest(
            settings=values.replace(instrument=info.symbol),
            since=since,
            until=until,
            settings_source=source,
        )
