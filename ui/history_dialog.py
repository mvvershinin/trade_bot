"""Окно «Загрузить историю»: что уже есть, что будет, чем это обернётся.

Зачем оно есть
--------------
Программа не качала историю сама. Замер 06.09.2026 на собранном `.exe`
в виртуалке Windows, где питона нет вовсе:

    > Terminal.exe --inspect MXU6
    Базы свечей нет: …/userdata/candles.sqlite3
    Она появится сама при первой загрузке: python3 -m app.main --fetch MXU6

Программа работала и советовала команду, которой на машине владельца счёта
нет и не будет. Он запускает `.exe` двойным щелчком, консоли у него не
существует — и он видит пустой график и совет, ведущий в никуда. Это окно
закрывает ровно эту дыру: свечи добываются мышкой.

Почему здесь числа, а не «вы уверены?»
--------------------------------------
Прямые слова владельца счёта 06.09.2026: «на попапе с вы уверены предупреди
о последствиях». Слепое «вы уверены?» приучает жать «да» не читая, и первый
же раз, когда вопрос будет важным, его пролистают. Поэтому сверху стоит опись
того, что уже в базе (сколько дней, с какого по какой, сколько из них плотные),
а у каждого из двух вариантов написано, что именно с этими данными случится.

Последствия проверены по коду, и первое из них денежное
-------------------------------------------------------
1. **Свечи изменятся — значит изменятся сделки.** Средняя считается по ряду;
   ряд с другой границей даёт другую среднюю и другие входы. Это касается
   **обоих** вариантов: дыра, закрытая в середине, двигает среднюю так же,
   как новое начало ряда.
2. **Записанные прогоны перестанут воспроизводиться.** Число в журнале
   останется, а повтор того же прогона на изменившихся данных даст другое.
3. **Объём станет грубее** на минутах, пришедших живым потоком: биржевая
   минутка старше рангом (`market.storage.Source`) и заменяет её.
4. **Это надолго.** Биржа отдаёт не быстрее пяти запросов в секунду
   (`broker.throttle`), и девяносто дней минуток идут минутами.
5. **Ничего не удаляется.** Данные за пределами отрезка не трогаются:
   пути удаления настоящих свечей в программе нет вовсе.

⚠️ Почему у безопасного варианта тоже стоит предупреждение. Соблазн написать
его только у «заново» велик — там страшнее, — но среднюю двигает и закрытая
дыра. Сторож на это отдельный: в тексте безопасного варианта обязана стоять
фраза про изменение сделок.

⚠️ Чего этот текст **не** обещает. Написать «существующие свечи не трогаются»
было бы неправдой даже для догрузки: дни, за которые биржу ещё не спрашивали,
запрашиваются целиком, и внутри них минутка от брокера будет заменена
биржевой (`CandleStore.put_minutes`, ранг источника). Не трогаются дни,
которые уже отмечены загруженными, — так и сказано.

Торговых правил здесь нет ни одного. Окно собирает просьбу и отдаёт её порту.
"""

from __future__ import annotations

from datetime import date, timedelta

from PySide6.QtCore import QDate, QEvent, QLocale, QObject, Qt
from PySide6.QtWidgets import (
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ui.models import HistoryFacts, HistoryLoadRequest, Settings
from ui.theme import current as current_theme
from ui.wheel_guard import guard_wheel

__all__ = ["HistoryDialog", "PickOnClick", "SHARED_WARNING", "TRADES_CHANGE", "fill_note"]

#: Главное последствие, общее для обоих вариантов. Одна строка на два места
#: намеренно: два разных текста об одном последствии читались бы как два
#: разных последствия, а последствие здесь одно — новые свечи двигают среднюю.
#:
#: ⚠️ Стоит **у обоих** вариантов, и это правило. Соблазн написать его только
#: у «заново» велик — там страшнее, — но среднюю двигает и дыра, закрытая
#: посреди ряда: ряд с другими свечами даёт другую среднюю и другие входы.
TRADES_CHANGE = (
    "⚠️ Новые свечи сдвинут среднюю, а значит изменят сделки: записанные "
    "прогоны после этого не повторятся числом в число."
)

#: То же и подробнее — под безопасным вариантом, где места хватает.
SHARED_WARNING = (
    f"{TRADES_CHANGE} В журнале останется прежний итог, а новый прогон "
    "на тех же настройках даст другой."
)

#: Сколько запросов в секунду отдаёт биржа. Не расчёт, а справка для человека:
#: она объясняет, почему кнопка «Загрузить» не срабатывает мгновенно.
REQUESTS_PER_SECOND = 5


class PickOnClick(QObject):
    """Щелчок по полю выбирает переключатель, к которому поле привязано.

    Слова владельца счёта 06.09.2026: «неактивное поле не меняется»
    (`B-030`). Поле «Загрузить заново с даты…» стоит справа от своего
    переключателя, читается как часть одной строки — и человек щёлкает
    по полю. Пока щелчок не делал ничего, это выглядело как поломка.

    ⚠️ **Почему фильтр событий, а не сигнал поля.** Выключенное поле сигналов
    не испускает вовсе: `QWidget.setEnabled(False)` отключает и нажатия,
    и фокус. А фильтр событий вызывается **до** проверки на выключенность
    (`QApplicationPrivate::notify_helper`), то есть щелчок по мёртвому полю
    здесь виден. Проверено на живом Qt, а не по документации.

    Событие пропускается дальше (`False`): поле, ставшее включённым,
    обязано получить и сам щелчок — иначе первое нажатие пропадает,
    и человек жмёт дважды.
    """

    def __init__(self, choice: QRadioButton, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._choice = choice

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # имя задано Qt
        """Нажатие мыши на поле — выбрать переключатель и пропустить дальше."""
        if event.type() is QEvent.Type.MouseButtonPress and not self._choice.isChecked():
            self._choice.setChecked(True)
            if isinstance(watched, QWidget):
                # Поле только что ожило — отдать ему и фокус, чтобы щелчок
                # кончился там, куда человек целился, а не в пустоте.
                watched.setFocus(Qt.FocusReason.MouseFocusReason)
        return False


def fill_note(facts: HistoryFacts) -> str:
    """Опись базы человеческими словами. Первое, что читают в этом окне.

    Три разных ответа, и путать их нельзя:

    * базу не прочитали — говорится именно это, а не «истории нет»;
    * инструмента в базе нет вовсе — «ни одной свечи», и это нормальное
      состояние свежепоставленной программы, а не поломка;
    * что-то есть — числа: сколько дней, с какого по какой, сколько плотных.

    «Огрызки» названы отдельно, потому что ровно они породили вопрос «почему
    я вижу данные с 6 августа, контракт с 17 июня»: до августа свечи были,
    но их десяток минут в день.
    """
    if facts.trouble:
        return facts.trouble
    if not facts.known:
        return (
            f"По инструменту {facts.symbol} в базе нет ни одной свечи. "
            "Это обычное состояние только что поставленной программы: "
            "историю нужно загрузить один раз."
        )
    span = ""
    if facts.first is not None and facts.last is not None:
        span = f", {facts.first:%d.%m.%Y} — {facts.last:%d.%m.%Y}"
    parts = [f"В базе по {facts.symbol} уже есть: {facts.trading_days} "
             f"торговых дн.{span}."]
    parts.append(
        f"Из них плотных {facts.dense_days}, огрызков {facts.sparse_days} — "
        "в огрызке свечи есть, но их единицы минут за день, и торговать "
        "по нему нечего."
        if facts.sparse_days else
        f"Все {facts.dense_days} — плотные."
    )
    parts.append(f"Всего минутных свечей: {facts.minutes}.")
    return " ".join(parts)


def _qdate(day: date) -> QDate:
    """Дата Python → дата Qt, по трём числам.

    Тремя числами, а не одним объектом, по той же причине, что и в окне
    прогона: перегрузка `QDate(date)` в PySide6 есть, а в описании типов её
    нет, и проверка типов на неё ругается.
    """
    return QDate(day.year, day.month, day.day)


def _pydate(day: QDate) -> date:
    """Дата Qt → дата Python. Пара к `_qdate`."""
    return date(day.year(), day.month(), day.day())


class HistoryDialog(QDialog):
    """Подтверждение загрузки истории: опись, два варианта, последствия.

    Ничего не запускает и ни к чему не обращается: загрузку ведёт порт.
    Окно собирает `HistoryLoadRequest` и отдаёт его наружу.
    """

    def __init__(
        self,
        facts: HistoryFacts,
        settings: Settings,
        parent: QWidget | None = None,
        *,
        today: date | None = None,
    ) -> None:
        """:param facts: что по инструменту уже лежит в базе.

        :param settings: настройки окна. Отсюда берётся глубина загрузки —
            своей константы у диалога нет намеренно (`D-068`: три числа
            глубины в программе не совпадали между собой ни одно с другим).
        :param today: какой день считать сегодняшним. Задаётся в проверках,
            чтобы прогон не зависел от даты запуска.
        """
        super().__init__(parent)
        self.setWindowTitle("Загрузка истории с биржи")
        self.setModal(True)
        self.resize(640, 560)

        self._facts = facts
        self._settings = settings
        self._today = today or date.today()
        self._theme = current_theme()

        self._build_widgets()
        self._lay_out()
        self._set_tab_order()
        # ⚠️ После сборки полей: колесо мыши не имеет права менять дату, пока
        # на поле нет фокуса. Владелец счёта уже ловил это на себе в настройках.
        self.guarded_fields = guard_wheel(self)
        self._sync()

    # ------------------------------------------------------------- сборка

    def _build_widgets(self) -> None:
        """Поля окна. Раскладка — отдельно: так короче каждое из двух."""
        self.fill = QLabel(fill_note(self._facts))
        self.fill.setWordWrap(True)

        self.add_missing = QRadioButton("Догрузить недостающее")
        self.add_missing.setChecked(True)
        self.add_missing.setToolTip(
            "Обычный путь. Дни, за которые биржу уже спрашивали, "
            "не перезапрашиваются вовсе."
        )
        self.reload = QRadioButton("Загрузить заново с даты…")
        self.reload.setToolTip(
            "Снять отметки «за этот день уже спрашивали» и запросить отрезок "
            "у биржи целиком. Нужно, когда данные в базе вызывают сомнение."
        )
        self.add_missing.toggled.connect(self._sync)
        self.reload.toggled.connect(self._sync)

        self.since = QDateEdit()
        self.since.setCalendarPopup(True)
        self.since.setDisplayFormat("dd.MM.yyyy")
        # Локаль задаётся явно: машина владельца счёта может стоять
        # в английской, и «June 2026» с неделей от воскресенья — чужой
        # календарь, в котором он промахнётся мимо дня.
        self.since.setLocale(QLocale("ru_RU"))
        self.since.calendarWidget().setLocale(QLocale("ru_RU"))
        self.since.calendarWidget().setFirstDayOfWeek(Qt.DayOfWeek.Monday)
        self.since.setDateRange(
            _qdate(self._today - timedelta(days=3650)), _qdate(self._today)
        )
        self.since.setDate(_qdate(self._default_since()))
        self.since.setToolTip(
            "С какого дня перезапросить историю у биржи. День берётся целиком. "
            "Щелчок по полю сам выбирает вариант «Загрузить заново с даты…»."
        )
        self.since.dateChanged.connect(self._sync)
        # ⚠️ `B-030`: щелчок по полю обязан выбирать его переключатель.
        # Ссылка хранится полем окна намеренно: фильтр, созданный и забытый,
        # соберётся сборщиком мусора, и защита исчезнет молча.
        self.pick_on_click = PickOnClick(self.reload, self)
        self.since.installEventFilter(self.pick_on_click)

        self.missing_note = self._note(SHARED_WARNING)
        self.reload_note = self._note("")
        self.summary = QLabel()
        self.summary.setWordWrap(True)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.load_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.load_button.setText("Загрузить")
        self.load_button.setDefault(True)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

    def _note(self, text: str) -> QLabel:
        """Строка последствий: с переносом и в цвете предупреждения темы."""
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(f"color: {self._theme.warning};")
        return label

    def _default_since(self) -> date:
        """Какую дату подставить в поле «заново с…».

        Ровно та же, что у обычной догрузки: глубина из настроек, считая
        сегодняшний день. Другое умолчание означало бы, что переключение
        варианта молча меняет отрезок.
        """
        return self._today - timedelta(days=max(self._depth(), 1) - 1)

    def _depth(self) -> int:
        """Глубина загрузки из настроек. Своей константы у окна нет (`D-068`)."""
        return max(self._settings.history_depth_days, 1)

    def _lay_out(self) -> None:
        """Разложить сверху вниз, как читают глазами."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(self._lead())
        layout.addWidget(self.fill)

        first = QGroupBox("Что сделать")
        rows = QVBoxLayout(first)
        rows.addWidget(self.add_missing)
        rows.addWidget(self.missing_note)
        picked = QHBoxLayout()
        picked.addWidget(self.reload)
        picked.addWidget(self.since)
        picked.addStretch(1)
        rows.addLayout(picked)
        rows.addWidget(self.reload_note)
        layout.addWidget(first)

        layout.addWidget(self.summary)
        layout.addStretch(1)
        layout.addWidget(self._pace())
        layout.addWidget(self.buttons)

    def _lead(self) -> QLabel:
        """Что это окно делает — до того, как в него что-то нажали."""
        label = QLabel(
            "Свечи берутся у Московской биржи и кладутся в базу программы. "
            "Заявки при этом никуда не уходят, на счёте не происходит ничего, "
            "и токен брокера для этого не нужен."
        )
        label.setWordWrap(True)
        return label

    def _pace(self) -> QLabel:
        """Сколько это займёт. Стоит вплотную к кнопке, как последнее слово."""
        label = QLabel(
            f"Займёт несколько минут: биржа отдаёт не быстрее "
            f"{REQUESTS_PER_SECOND} запросов в секунду. Окно при этом "
            "не замирает — ход работы будет виден полоской, и загрузку можно "
            "остановить кнопкой «Отменить»."
        )
        label.setWordWrap(True)
        label.setStyleSheet(f"color: {self._theme.text_dim};")
        return label

    def _set_tab_order(self) -> None:
        """Порядок обхода клавишей Tab — сверху вниз, как читают."""
        self.setTabOrder(self.add_missing, self.reload)
        self.setTabOrder(self.reload, self.since)
        self.setTabOrder(self.since, self.buttons)

    # -------------------------------------------------------------- ответ

    def _sync(self, *_: object) -> None:
        """Свести окно к согласованному виду и сказать, что получится."""
        reloading = self.reload.isChecked()
        self.since.setEnabled(reloading)
        self.reload_note.setText(self._reload_warning())
        self.summary.setText(self._summary_text())

    def _reload_warning(self) -> str:
        """Последствия «загрузить заново». Всё, что у догрузки, и сверх того.

        Фраза про изменение сделок берётся из `TRADES_CHANGE`, а не пишется
        второй раз: одно последствие — один текст. Под безопасным вариантом
        она стоит развёрнуто, здесь — коротко: ниже к ней добавляется то,
        чего у догрузки нет, и повтор целого абзаца читался бы как шум.
        """
        return (
            f"{TRADES_CHANGE}\n"
            "⚠️ И сверх того: минуты, пришедшие живым потоком от брокера, "
            "будут заменены биржевыми — объём у них считается грубее. "
            "Данные вне выбранного отрезка останутся на месте: свечи "
            "не удаляются ни при каком варианте."
        )

    def _summary_text(self) -> str:
        """Что именно будет запрошено — числами, прямо над кнопкой."""
        since, until = self._span()
        days = (until - since).days + 1
        how = (
            "Отметки «уже спрашивали» будут сняты, и весь отрезок "
            "запрошен у биржи заново."
            if self.reload.isChecked() else
            "Дни, за которые биржу уже спрашивали, перезапрошены не будут — "
            "их свечи программа не тронет. Догрузятся дни, которых в базе нет "
            "или которые не докачались; внутри таких дней свеча из потока "
            "брокера будет заменена биржевой."
        )
        return (
            f"Будет запрошено: {self._facts.symbol or 'инструмент не задан'}, "
            f"{since:%d.%m.%Y} — {until:%d.%m.%Y} ({days} дн.). {how}"
        )

    def _span(self) -> tuple[date, date]:
        """Отрезок, который уйдёт в просьбу. Правая граница — сегодня всегда."""
        if self.reload.isChecked():
            return min(_pydate(self.since.date()), self._today), self._today
        return self._today - timedelta(days=self._depth() - 1), self._today

    def request(self) -> HistoryLoadRequest | None:
        """Просьба, собранная из полей. `None` — просить нечего.

        `None` бывает ровно в одном случае: инструмент не назван. Это не
        случайность окна, а пустое поле в настройках, и загрузка по нему
        всё равно была бы отклонена портом.
        """
        if not self._facts.symbol.strip():
            return None
        reloading = self.reload.isChecked()
        return HistoryLoadRequest(
            symbol=self._facts.symbol.strip(),
            days=self._depth(),
            since=_pydate(self.since.date()) if reloading else None,
            replace=reloading,
        )
