"""Главное окно: панель состояния, график, журналы, настройки.

Окно ничего не решает. Оно показывает то, что пришло от `TerminalPort`,
и отправляет туда команды. Ответ на вопрос «войдёт ли робот сейчас» находится
в `engine/`; в этом файле его нет и быть не должно.

Опасные места, ради которых здесь есть предупреждения
-----------------------------------------------------
Четыре состояния способны стоить денег, и каждое перехвачено:

* **Режим «Выключен» при открытой позиции.** Выключение намеренно не закрывает
  позицию — иначе выключение робота само по себе приводило бы к сделке. Но
  позиция остаётся без присмотра, и сказать это надо крупно и до, а не после.
* **Закрытие окна при открытой позиции.** Тихий выход недопустим по той же причине.
* **Остановленный робот.** Это не «робот вне рынка», а «робот перестал понимать,
  что происходит на счёте, и ждёт человека». Видно должно быть крупно.
* **Старт на токене «только чтение».** Кнопка гасится с объяснением, а не даёт
  отказ после нажатия.

⚠️ Сами тексты первых трёх предупреждений живут не здесь, а в `ui/notices.py`,
и это не оформительская прихоть. Пока они были написаны в трёх местах этого
файла, все три одинаково разошлись с движком: обещали, что после выключения
робота «ни тейк, ни переворот к ней больше не применяются», — а тейк это заявка
у брокера, которую выключение не снимает (решение 0008). Утверждение про
судьбу позиции без робота делается ровно один раз; новое место зовёт функцию,
а не пишет свой текст.
"""

from __future__ import annotations

import errno
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QSignalBlocker, QTimer, Qt
from PySide6.QtGui import QAction, QCloseEvent, QResizeEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QSplitter,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ui import backend
from ui.background import busy_cursor
from ui.backtest_dialog import BacktestDialog
from ui.backtest_report import BacktestReportDialog
from ui.chart_panel import ChartPanel
from ui.export import decisions_table, trades_table, write_csv
from ui.formatting import MSK, fmt_time
from ui.history_dialog import HistoryDialog
from ui.journals import JournalTabs
from ui.models import (
    BacktestOptions,
    BacktestReport,
    Candle,
    ChartData,
    Connection,
    DecisionRow,
    HistoryFacts,
    HistoryLoadOutcome,
    Layer,
    Marker,
    Mode,
    PriceLevel,
    RobotState,
    Settings,
    Shade,
    TradeRow,
    TradesSummary,
)
from ui.notices import (
    Context,
    halt_note,
    position_line,
    resume_question,
    unmanaged_note,
)
from ui.ports import DetachedPort, Sanitize, TerminalPort
from ui.calendar_dialog import CalendarDialog
from ui.settings_dialog import SettingsDialog
from ui.status_panel import StatusPanel
from ui.templates import Library, Template
from ui.templates_dialog import TemplatesDialog
from ui.theme import current as current_theme, text_on
from ui.version import version

TITLE = "Терминал"

#: Подсказка крестика на плашке закреплённого отрезка. Вынесена сюда,
#: а не написана на месте, по прозаической причине: сборка окна упирается
#: в предел длины функции (`tests/test_function_size.py`).
UNPIN_HINT = (
    "Снять закреплённый отрезок и вернуться к текущим данным. "
    "То же самое делает кнопка «Вернуться к текущим данным»."
)
TOKEN_WARNING_DAYS = 10


@dataclass(frozen=True, slots=True)
class StreamLook:
    """Как выглядит кнопка связи с брокером: нажата ли, подпись, подсказка."""

    checked: bool
    label: str
    tooltip: str


#: Смысл, который подсказка кнопки связи обязана сохранить при любом состоянии:
#: подписка на котировки — не запуск робота. Одной строкой, а не в каждой
#: подсказке по-своему: разошлись бы молча.
_STREAM_NOTE = (
    "Подписка на котировки — не запуск робота: он НЕ торгует и решений "
    "не принимает, свечи только приходят в базу и на график. Для подписки "
    "достаточно токена с правом только на чтение."
)

#: Вид кнопки связи при каждом состоянии связи из снимка `RobotState`.
#:
#: Таблица, а не ветвление: состояний ровно четыре, и новое значение
#: `Connection` обязано получить здесь строку — иначе `KeyError` вслух,
#: а не кнопка, застывшая в прежнем виде.
#:
#: Кнопка **зеркалит снимок**, а не помнит, что нажимали. Единственный
#: источник правды о связи — поле `connection` снимка (`app/port.py`,
#: `HistoryPort.connection`); второго пути к панели не заводится, потому что
#: два ответа на один вопрос расходятся во времени. Кнопка нажата ровно там,
#: где поток жив: связь есть или её добывают. Переходы — `app/live_feed.py`:
#:
#: * `ONLINE` — пришёл первый снимок свечи; нажатие снимет поток.
#: * `RECONNECTING` — поток жив, связи нет: первая попытка или пауза перед
#:   повтором после обрыва (`LiveFeed._ride`). Это не то же самое, что
#:   `OFFLINE`: там просьбы больше нет и никто не пробует. Кнопка нажата,
#:   нажатие прекращает попытки.
#: * `OFFLINE` — поток снят. Три пути: «Отключиться» из окна
#:   (`request_stop`); отказ, которому повтор не поможет, — просроченный
#:   токен, инструмент не найден, чужой формат (`BrokerError.retryable`);
#:   отказ собрать сессию — нет файла токена, каталог назван не `userdata`
#:   (`HistoryPort.stream`). Во всех трёх нажатие — единственный способ
#:   попробовать снова, и подсказка отсылает за причиной в журнал решений.
#: * `UNKNOWN` — связи не просили ни разу.
#: * `MARKET_CLOSED` — связь в порядке, торгов нет: биржа закрыта, и
#:   программа ждёт открытия вместо повторов (`B-022`). Кнопка нажата,
#:   как при `RECONNECTING`: поток жив. Отличать эти два состояния
#:   обязательно — владелец счёта уже потерял полчаса, приняв закрытую
#:   биржу за поломку.
#:
#: ⚠️ Поле различает «снято» и «пробуем» только потому, что в паузе между
#: попытками поток держит `RECONNECTING`, а `OFFLINE` ставит лишь при снятии.
#: Первая редакция `LiveFeed` ставила `OFFLINE` и в паузе, и кнопке
#: приходилось выбирать из двух неверных. Поменяется это — таблица соврёт
#: молча: разжатая кнопка при живом потоке.
STREAM_BUTTON: dict[Connection, StreamLook] = {
    Connection.UNKNOWN: StreamLook(
        False,
        "Подключиться",
        f"Подписаться на живые котировки брокера. {_STREAM_NOTE}",
    ),
    Connection.OFFLINE: StreamLook(
        False,
        "Подключиться",
        "Связи с брокером нет: поток снят — по команде из окна или из-за отказа, "
        "которому повтор не поможет; причина записана в журнале решений. "
        f"Подписаться на живые котировки снова. {_STREAM_NOTE}",
    ),
    Connection.RECONNECTING: StreamLook(
        True,
        "Отключиться",
        "Связь запрошена, но её сейчас нет: программа подключается к брокеру "
        f"или восстанавливает оборванную связь. Нажатие прекратит попытки. {_STREAM_NOTE}",
    ),
    Connection.ONLINE: StreamLook(
        True,
        "Отключиться",
        "Связь с брокером есть. Отписаться от живых котировок: свечи перестанут "
        f"приходить в базу и на график. {_STREAM_NOTE}",
    ),
    Connection.MARKET_CLOSED: StreamLook(
        True,
        "Отключиться",
        "Биржа закрыта — торгов сейчас нет. Это не сбой и не потеря связи: "
        "программа знает расписание, ждёт открытия и не тратит попытки "
        "впустую. Когда начнутся торги — сказано строкой в журнале решений; "
        "время там берётся из расписания брокера, а если брокер его не назвал, "
        f"так и написано. Нажатие снимет поток совсем. {_STREAM_NOTE}",
    ),
}

#: Метка времени в имени файла выгрузки. Точность — минута.
#:
#: ⚠️ Отсюда следует, что **две выгрузки в одну минуту пишут в одни имена**:
#: вторая перезаписывает первую молча. Сегодня это выбор в пользу коротких
#: имён, а не недосмотр: содержимое обеих одинаково, если между нажатиями
#: не пришла новая строка журнала. Из-за этого же откат неудачной выгрузки
#: удаляет только те файлы, которых в каталоге до неё не было (`_write_all`).

STAMP_FORMAT = "%Y-%m-%d_%H-%M"

#: Одно задание выгрузки: куда писать, шапка, строки.
ExportJob = tuple[Path, Sequence[str], Sequence[Sequence[str]]]

#: Почему не удалось записать файл — человеческим языком, по коду системы.
#:
#: Таблица, а не цепочка `if`: случаев ровно столько, сколько строк, и новый
#: случай — это дописанная строка, а не вспомненное место среди ветвлений.
#:
#: Ключ — **имя** из `errno`, а не число. Часть кодов объявлена не везде
#: (`EDQUOT` есть в Linux и нет в MSVC), обращение по имени через `getattr`
#: это переживает; числа пришлось бы выписывать руками, а они на разных
#: системах разные.
#:
#: Ни кода, ни номера в ответе нет: `Errno 13` и `WinError 32` — техническая
#: подробность, а человеку нужна причина (ТЗ §4.6).
WRITE_REASONS: tuple[tuple[str, str], ...] = (
    ("EACCES", "нет прав на запись в этот каталог"),
    ("EPERM", "нет прав на запись в этот каталог"),
    ("EROFS", "диск доступен только для чтения"),
    ("ENOSPC", "на диске не осталось места"),
    ("EDQUOT", "исчерпана дисковая квота"),
    ("ENOENT", "каталог не найден — возможно, съёмный диск отключили"),
    ("ENOTDIR", "в пути к файлу оказался не каталог, а файл"),
    # `write_csv` начинает с `mkdir(exist_ok=True)`, а он на существующем
    # **файле** отдаёт `EEXIST`, а не `ENOTDIR`. Проверено вживую 03.09.2026:
    # без этой строки владелец счёта, выбравший файл вместо папки, получал
    # «операционная система отказала в записи: File exists» — по-английски
    # и ни о чём.
    ("EEXIST", "выбран файл, а не каталог"),
    ("ENAMETOOLONG", "слишком длинное имя файла"),
    ("EBUSY", "файл занят другой программой"),
    ("EISDIR", "этим именем в каталоге уже назван не файл, а папка"),
    ("ETXTBSY", "файл занят другой программой"),
)

#: Windows: `ERROR_SHARING_VIOLATION` — файл держит открытым другая программа.
#: Приходит в `PermissionError.winerror`; в `errno` он превращается в тот же
#: `EACCES`, что и «нет прав на каталог», поэтому различить их можно только тут.
#:
#: ⚠️ **Проверено не на Windows и не в Excel** — машины с ними у меня нет.
#: Номер взят из `winerror.h` (документация Microsoft). На Linux этого случая
#: не бывает вовсе: обязательных блокировок здесь нет, файл, открытый
#: в LibreOffice, перезаписывается молча (проверено 03.09.2026) — на диске
#: окажется новое содержимое, а в открытом окне LibreOffice останется то,
#: которое он прочитал при открытии.
SHARING_VIOLATION = 32


def write_failure(error: BaseException) -> str:
    """Почему не удалось записать файл — фразой, а не кодом и не трассировкой.

    Порядок разбора: сначала Windows (`winerror` точнее — он отличает занятый
    файл от отсутствия прав, а `errno` сводит оба случая в `EACCES`), потом
    таблица кодов, потом текст самой системы как последнее средство.

    Текст системы в последнем случае оставлен намеренно. Без него владелец
    счёта и служба поддержки не узнают о редком отказе ничего, а «неизвестная
    ошибка» — это молчание, только с формулировкой.
    """
    if getattr(error, "winerror", None) == SHARING_VIOLATION:
        return "файл занят другой программой — возможно, он открыт в Excel"
    if isinstance(error, OSError):
        for name, reason in WRITE_REASONS:
            if error.errno is not None and error.errno == getattr(errno, name, None):
                return reason
        if error.strerror:
            return f"операционная система отказала в записи: {error.strerror}"
    return "программа не смогла собрать файлы отчёта"


class _Banner(QLabel):
    """Крупная строка предупреждения над графиком.

    Крупная — намеренно. То, что попадает сюда, строкой в журнале сообщать
    бесполезно: журнал читают после происшествия, а это надо увидеть до.
    """

    #: Кегль плашки в пунктах. Не «покрупнее системного», а именно 14 —
    #: это граница, с которой WCAG считает полужирный текст крупным и требует
    #: контраста 3:1, а не 4,5:1. Прежние «системный + 2» давали 11–12 пунктов,
    #: то есть обоснование порога в проверке контраста не выдерживалось:
    #: формально требовалось 4,5:1. Число здесь и порог в тесте связаны.
    LARGE_POINTS = 14.0

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        on_close: Callable[[], None] | None = None,
        close_hint: str = "",
    ) -> None:
        """:param on_close: что делает крестик. `None` — крестика нет вовсе.

        ⚠️ Крестик — **необязательная** часть плашки, и это правило,
        а не удобство. Плашку «робот остановлен дневным лимитом» закрывать
        нечем: снимает её только явное возобновление (решение 0045, `D-043`),
        и крестик на ней означал бы, что сообщение о вставшем роботе можно
        смахнуть. Крестик ставится там, где есть **действие**, которое он
        выполняет, — и выполняет он именно его, а не `hide()`. Плашка,
        спрятанная при закреплённом отрезке, была бы ложью: живые свечи
        по-прежнему не идут, а сказать об этом стало бы нечем.
        """
        super().__init__(parent)
        self.setWordWrap(True)
        self.setVisible(False)
        # Справа место под крестик, если он есть: иначе текст уезжает под него.
        self.setContentsMargins(12, 8, 36 if on_close is not None else 12, 8)
        self.close_button: QToolButton | None = None
        if on_close is not None:
            self.close_button = self._add_close(on_close, close_hint)
        # ⚠️ Простой текст, а не разметка. Сюда попадает строка, пришедшая
        # снаружи (`state.halted` — причина остановки из движка, а до него
        # из брокера). Символ `<` в такой строке съел бы половину
        # предупреждения молча: QLabel по умолчанию угадывает разметку.
        self.setTextFormat(Qt.TextFormat.PlainText)
        font = self.font()
        font.setPointSizeF(max(font.pointSizeF() + 2.0, self.LARGE_POINTS))
        font.setBold(True)
        self.setFont(font)

    #: Отступ крестика от правого верхнего угла плашки, в точках.
    CLOSE_INSET = 6

    def _add_close(self, on_close: Callable[[], None], hint: str) -> QToolButton:
        """Крестик в правом верхнем углу плашки.

        ⚠️ Кнопка ставится **поверх** надписи и двигается в `resizeEvent`,
        а не кладётся в раскладку. Раскладка внутри `QLabel` отбирает
        у надписи её собственный размер: плашка начинает мерить себя
        по кнопке, и текст в две строки обрезается на середине второй.
        Поймано снимком, а не рассуждением: 68 точек высоты превратились
        в 42, и половина предупреждения исчезла.
        """
        button = QToolButton(self)
        button.setText("✕")
        button.setToolTip(hint)
        button.setAutoRaise(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        button.clicked.connect(on_close)
        button.adjustSize()
        return button

    def resizeEvent(self, event: QResizeEvent) -> None:  # имя метода задано Qt
        """Держать крестик в правом верхнем углу при любой ширине окна."""
        super().resizeEvent(event)
        if self.close_button is not None:
            width = self.close_button.width()
            self.close_button.move(
                self.width() - width - self.CLOSE_INSET, self.CLOSE_INSET
            )

    def show_text(self, text: str, background: str) -> None:
        # ⚠️ Цвет надписи считается по заливке, а не берётся белым всегда.
        # Белым по `warning` тёмной темы (`#ffb74d`) контраст 1,7:1 — плашка
        # «Токен брокера истекает» была нечитаема, то есть предупреждения
        # не было, хотя код его показывал.
        self.setText(text)
        if self.close_button is not None:
            # ⚠️ Цвет крестика задаётся **явно**, тем же расчётом, что и текст
            # плашки. Наследовать его от заливки нельзя: `QToolButton` берёт
            # цвет из палитры кнопки, и на оранжевой плашке крестик выходил
            # светло-серым — контраст 1,6:1, то есть кнопки на ней не видно.
            self.close_button.setStyleSheet(
                f"QToolButton {{ color: {text_on(background)}; "
                "background: transparent; border: none; font-size: 16px; "
                "font-weight: bold; }"
            )
            self.close_button.adjustSize()
        self.setStyleSheet(
            f"background: {background}; color: {text_on(background)}; border-radius: 4px;"
        )
        self.setVisible(True)

    def hide_text(self) -> None:
        self.clear()
        self.setVisible(False)


class MainWindow(QMainWindow):
    """Окно программы."""

    #: Полоска идущей загрузки истории. `None` — загрузку не запускали.
    #: Своя, а не общая с полоской прогона: почему — в `_start_loading`.
    #:
    #: Объявлена у класса, а не в `__init__`, ровно затем, чтобы не удлинять
    #: сборку окна: она и без того упирается в предел длины функции
    #: (`tests/test_function_size.py`). Значение неизменяемое, поэтому общий
    #: на класс умолчанник безопасен — у изменяемого так делать нельзя.
    _loading: QProgressDialog | None = None

    def __init__(
        self,
        port: TerminalPort | None = None,
        settings: Settings | None = None,
        parent: QWidget | None = None,
        *,
        sanitize: Sanitize,
    ) -> None:
        """:param sanitize: чистка секретов для выгрузки журналов.

        Обязательна и умолчания не имеет — окно, собранное без чистки,
        не собирается вовсе. Это **второй** рубеж: первый стоит на границе
        `app/` → `ui/` (`app.port.HistoryPort._send`), и текст, дошедший
        до окна, уже чистый. Второй нужен потому, что выгрузка — единственный
        выход, который уезжает с машины файлом; разбор — в шапке `ui/export.py`.

        Своей чистки слой окна не пишет и не импортирует: полная чистка —
        `broker.redaction.scrub`, а `broker/` окну по ARCHITECTURE.md §2
        не положен. Функцию даёт `app/` — тем же вызовом, каким он даёт её
        слою данных и порту.
        """
        super().__init__(parent)
        self.port = port or DetachedPort(self)
        self.theme = current_theme()
        self._settings = settings or Settings()
        self._sanitize = sanitize
        self._state = RobotState()
        #: Окно собрано целиком. Сторож для `changeEvent`: событие палитры
        #: приходит и в середине сборки, а `apply_theme()` трогает график
        #: и журналы — их в этот момент ещё нет.
        self._ready = False

        self.setWindowTitle(f"{TITLE} {version()}")
        self.resize(1360, 900)

        self.status_panel = StatusPanel()
        # Порядок плашек — порядок срочности. Остановка сверху: пока робот
        # остановлен, всё остальное вторично.
        self.halt_banner = _Banner()
        self.stuck_banner = _Banner()
        self.token_banner = _Banner()
        self.mode_banner = _Banner()
        #: Плашка «показан прогон на истории». Живые свечи на закреплённый
        #: отрезок не приходят вовсе, и без этой строки застывший график
        #: неотличим от потерянной связи (`app/port.py::_Span`).
        self.history_banner = _Banner(on_close=self.show_recent_data,
                                      close_hint=UNPIN_HINT)
        #: Полоска идущего прогона. `None` — прогон не запрашивали.
        self._progress: QProgressDialog | None = None
        self.chart = ChartPanel()
        self.journals = JournalTabs()

        self._build_toolbar()
        self._build_menu()

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.chart)
        splitter.addWidget(self.journals)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([540, 320])

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        layout.addWidget(self.status_panel)
        layout.addWidget(self.halt_banner)
        layout.addWidget(self.stuck_banner)
        layout.addWidget(self.token_banner)
        layout.addWidget(self.mode_banner)
        layout.addWidget(self.history_banner)
        layout.addWidget(splitter, 1)
        self.setCentralWidget(central)

        self._clock = QLabel()
        self.statusBar().addPermanentWidget(self._clock)
        self.statusBar().showMessage(self.chart.renderer_note)

        self.journals.export_requested.connect(self.export_journals)

        self._connect_port()
        self.apply_theme()
        self.apply_state(self._state)

        # Часы в углу — с явной пометкой МСК. Компьютер владельца счёта может
        # стоять в другом поясе, а всё торговое время в этом проекте московское.
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._tick()
        # Последняя строка сборки — и именно её проверяет `changeEvent`.
        self._ready = True

    # --------------------------------------------------------------- сборка

    def _build_toolbar(self) -> None:
        """Панель управления: что жмут каждый день.

        Собирается тремя кусками — команды роботу, команды показу, раскладка.
        Врозь потому, что доводы у них разные и читаются они по отдельности:
        «почему связь — отдельная кнопка» не имеет отношения к тому, «почему
        календарь живёт в меню».
        """
        self._build_robot_actions()
        self._build_view_actions()

        bar = QToolBar("Управление")
        bar.setMovable(False)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        bar.addAction(self.start_action)
        bar.addAction(self.stop_action)
        # Возобновление стоит рядом со «Стопом», а не в меню: пока робот
        # остановлен, это единственное действие, которое вообще что-то меняет.
        bar.addAction(self.resume_action)
        bar.addSeparator()
        bar.addAction(self.stream_action)
        bar.addSeparator()
        bar.addWidget(QLabel("  Режим:  "))
        bar.addWidget(self.mode_box)
        bar.addSeparator()
        bar.addAction(self.settings_action)
        bar.addAction(self.recent_action)
        self.addToolBar(bar)
        self.toolbar = bar

    def _build_robot_actions(self) -> None:
        """Старт, стоп и связь с брокером."""
        self.start_action = QAction("Старт", self)
        self.start_action.setToolTip("Запустить робота.")
        self.start_action.triggered.connect(self.port.start)

        self.stop_action = QAction("Стоп", self)
        self.stop_action.setToolTip(
            "Остановить робота. Открытую позицию он не закроет и управлять ею "
            "перестанет — при открытой позиции окно спросит подтверждение."
        )
        # ⚠️ Не `port.stop` напрямую. Последствие «Стопа» при открытой позиции
        # ровно то же, что у режима «Выключен»: переворот и закрытие по концу
        # окна к ней больше не применяются. Предупреждать в одном случае
        # и молчать в другом — значит объявить одно из двух безопасным.
        self.stop_action.triggered.connect(self.request_stop)

        #: Возобновление после остановки. Кнопки нет **вовсе**, пока робот
        #: не остановлен: она появляется вместе с красной плашкой и стоит
        #: рядом с ней — тот же приём, что у «Вернуться к текущим данным».
        #:
        #: ⚠️ **Не крестик на плашке.** Крестик означает «сообщение можно
        #: смахнуть», а здесь смахнуть нечего: остановка снимается действием
        #: с последствиями, и у действия есть вопрос (`_Banner.__init__`
        #: говорит про это прямо, `D-043`).
        #:
        #: ⚠️ **Многоточие в подписи — обещание вопроса**, а не украшение:
        #: нажатие открывает окно подтверждения, а не снимает остановку молча.
        self.resume_action = QAction("Возобновить работу…", self)
        self.resume_action.setVisible(False)
        self.resume_action.setToolTip(
            "Снять одну причину остановки — самую раннюю. Если причин "
            "несколько, робот останется остановленным: остальные снимаются "
            "по одной, каждая своим нажатием."
        )
        self.resume_action.triggered.connect(self.request_resume)

        #: Связь с брокером — отдельная кнопка, а не часть «Старта».
        #: Подписка на котировки и торговля — разные вещи: смотреть на живой
        #: рынок можно и на токене для чтения, а торговать по нему нельзя.
        #:
        #: Подпись, подсказка и нажатость — из `STREAM_BUTTON` по снимку
        #: состояния (`_mirror_stream`); здесь только вид до первого снимка.
        look = STREAM_BUTTON[Connection.UNKNOWN]
        self.stream_action = QAction(look.label, self)
        self.stream_action.setCheckable(True)
        self.stream_action.setToolTip(look.tooltip)
        self.stream_action.toggled.connect(self.port.stream)

    def _build_view_actions(self) -> None:
        """Настройки, календарь, возврат к текущим данным и выбор режима.

        Календарь собирается здесь, а показывается в меню: где чему стоять,
        решено ниже, а собраны все действия в одном месте, чтобы не искать
        половину по файлу.
        """
        self.settings_action = QAction("Настройки…", self)
        self.settings_action.setToolTip("Параметры робота. Применяются без перезапуска.")
        self.settings_action.setShortcut("Ctrl+,")
        self.settings_action.triggered.connect(self.open_settings)

        #: Календарь живёт в меню, а не на панели, и это решение владельца
        #: счёта от 05.09.2026. Довод записан здесь, чтобы следующий не начал
        #: вешать кнопки: **панель — для того, что жмут каждый день** (старт,
        #: стоп, связь, параметры). Календарь открывают раз в неделю; на панели
        #: он отвлекает, а в меню его ищут и находят. Место оставлено на вырост:
        #: следующая настроечная штука ляжет четвёртым пунктом меню, а не
        #: четвёртой кнопкой панели.
        self.calendar_action = QAction("Календарь нерабочих дней…", self)
        self.calendar_action.setToolTip(
            "Дни, в которые робот не работает: ваши отметки и дни, названные "
            "биржей."
        )
        self.calendar_action.triggered.connect(self.open_calendar)

        #: Пара к «Прогону на истории»: снять закреплённый отрезок.
        #:
        #: На панели, а не в меню, и это не противоречит доводу выше. Кнопки
        #: нет **вовсе**, пока отрезок не закреплён: она появляется вместе
        #: с плашкой и стоит рядом с ней, то есть выход находится там же, где
        #: сказано, из чего выходить. В меню «Настройки» ей места нет
        #: по другой причине: там лежат пункты, открывающие окна, и все они
        #: с многоточием — а это действие без окна (`tests/test_ui_window.py`).
        self.recent_action = QAction("Вернуться к текущим данным", self)
        self.recent_action.setVisible(False)
        self.recent_action.setToolTip(
            "Снять отрезок прогона на истории и снова показывать последние дни "
            "вместе с живыми свечами."
        )
        self.recent_action.triggered.connect(self.show_recent_data)

        self.mode_box = QComboBox()
        for mode in Mode:
            self.mode_box.addItem(mode.label, mode)
        self.mode_box.setToolTip("Режим работы робота.")
        self.mode_box.currentIndexChanged.connect(self._on_mode_selected)

    def _build_menu(self) -> None:
        """Строка меню: «Программа», «Настройки», «Справка».

        Настройки вынесены **отдельным меню**, а не пунктом «Программы»
        (решение владельца счёта 05.09.2026): настроечных окон стало больше
        одного, и складывать их вперемешку с выходом и выгрузкой значит
        прятать. Пункт настроек из «Программы» при этом убран: одно и то же
        в двух местах меню — способ потом поправить одно и забыть другое.

        Кнопка «Настройки» на панели осталась: параметры робота открывают
        чаще всего, и путь до них короче на один щелчок.
        """
        # ⚠️ Меню держатся полями окна, а не только строкой меню. Qt-объект
        # живёт у строки меню, а обёртка Python — нет: без ссылки она умирает,
        # и обращение к меню из кода даёт «Internal C++ object already deleted»
        # на заново занятом адресе. Поймано тестом меню 05.09.2026.
        self.program_menu = self.menuBar().addMenu("Программа")
        program = self.program_menu
        #: Загрузка истории — **первым** пунктом первого меню, и это не вкус.
        #: Свежепоставленная программа показывает пустой график, и первое,
        #: что человеку нужно сделать, — добыть свечи. До 06.09.2026 сделать
        #: это можно было только командой в консоли, которой на его машине
        #: нет (`B-024`).
        self.history_action = QAction("Загрузить историю…", self)
        self.history_action.setToolTip(
            "Скачать свечи с Московской биржи и положить их в базу программы. "
            "Заявки при этом никуда не уходят, токен брокера не нужен. "
            "Перед загрузкой окно покажет, что уже есть в базе и что "
            "изменится."
        )
        self.history_action.triggered.connect(self.open_history)
        program.addAction(self.history_action)
        program.addSeparator()
        export_action = QAction("Выгрузить журналы…", self)
        export_action.triggered.connect(self.export_journals)
        program.addAction(export_action)
        program.addSeparator()
        quit_action = QAction("Выход", self)
        quit_action.triggered.connect(self.close)
        program.addAction(quit_action)

        self.settings_menu = self.menuBar().addMenu("Настройки")
        settings_menu = self.settings_menu
        # Многоточие у всех трёх пунктов — обещание, что откроется окно, а не
        # случится действие. Пункт без многоточия читается как «сделать сразу».
        self.settings_action.setText("Параметры робота…")
        settings_menu.addAction(self.settings_action)
        settings_menu.addAction(self.calendar_action)
        #: Шаблоны — третьим пунктом меню, а не кнопкой на панели, и довод тот
        #: же, что у календаря: **панель для того, что жмут каждый день**
        #: (старт, стоп, связь, параметры). Шаблоны открывают раз в неделю —
        #: на панели они отвлекали бы, а в меню их ищут и находят.
        self.templates_action = QAction("Шаблоны настроек…", self)
        self.templates_action.setToolTip(
            "Наборы настроек, сохранённые под именем: что каждый дал на "
            "прогонах и применение целиком одним движением."
        )
        self.templates_action.triggered.connect(self.open_templates)
        settings_menu.addAction(self.templates_action)

        #: Прогон на истории — четвёртым пунктом того же меню, а не кнопкой
        #: на панели. Довод тот же, что у календаря и шаблонов: **панель для
        #: того, что жмут каждый день** (старт, стоп, связь, параметры).
        #: Прогон — вечернее занятие, как подбор шаблона.
        #:
        #: И вторая причина, своя: прогон **меняет настройки окна целиком**,
        #: как и шаблоны. Место среди настроек говорит об этом раньше, чем
        #: предупреждение внутри диалога.
        settings_menu.addSeparator()
        self.backtest_action = QAction("Прогон на истории…", self)
        self.backtest_action.setToolTip(
            "Прогнать робота по прошлым свечам из базы и получить отчёт: "
            "список сделок, итог по деньгам, метки входов и выходов "
            "на графике. Заявки при этом никуда не уходят."
        )
        self.backtest_action.triggered.connect(self.open_backtest)
        settings_menu.addAction(self.backtest_action)


        self.help_menu = self.menuBar().addMenu("Справка")
        help_menu = self.help_menu
        about_action = QAction("О программе", self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)

    def _connect_port(self) -> None:
        port = self.port
        port.state_changed.connect(self.apply_state)
        port.chart_replaced.connect(self.show_chart)
        port.candle_appended.connect(self.append_candle)
        port.candle_updated.connect(self.update_last_candle)
        port.average_changed.connect(lambda points: self.chart.set_average(points))
        port.markers_changed.connect(self.chart.set_markers)
        port.paths_changed.connect(self.chart.set_paths)
        port.levels_changed.connect(self.chart.set_levels)
        port.shades_changed.connect(self.chart.set_shades)
        port.trades_replaced.connect(self.set_trades)
        port.trade_appended.connect(self.append_trade)
        port.decisions_replaced.connect(self.journals.set_decisions)
        port.decision_appended.connect(self.journals.append_decision)
        port.settings_applied.connect(self._on_settings_echo)
        port.failed.connect(self.show_error)
        port.stuck_changed.connect(self.show_stuck)
        port.busy_changed.connect(self._on_busy)
        port.progress_changed.connect(self._on_progress)
        port.backtest_options_ready.connect(self.show_backtest_dialog)
        port.backtest_finished.connect(self.show_backtest_report)
        port.history_facts_ready.connect(self.show_history_dialog)
        port.history_progress.connect(self._on_history_progress)
        port.history_finished.connect(self.show_history_result)

    # ---------------------------------------------------------------- показ

    def changeEvent(self, event) -> None:  # имя метода задано Qt
        """Смена системной темы на ходу.

        Без этого `apply_theme()` вызывался ровно один раз, при старте, хотя
        его докстринг обещал «и при её смене». Qt 6.5+ подхватывает системную
        тему сам, но наши цвета — свечи, лонг и шорт, заливки плашек — он
        не знает: после переключения на тёмную оставались светлые.
        """
        # ⚠️ Сторож — флаг, выставляемый последней строкой `__init__`, а не
        # `getattr` на первой созданной панели. Прежняя проверка смотрела
        # на `status_panel`, которая создаётся первой, тогда как `apply_theme()`
        # трогает `chart` и `journals`, появляющиеся позже: в этом промежутке
        # защиты не было вовсе, хотя комментарий её обещал.
        #
        # ⚠️ Два события палитры схлопываются в одну перерисовку. При
        # автоматическом переключении системной темы приходят и
        # `ApplicationPaletteChange`, и `PaletteChange`; перерисовка тяжёлая
        # (веб-график перечитывает страницу), а вторая ничего не меняет.
        if event.type() in (
            QEvent.Type.ApplicationPaletteChange,
            QEvent.Type.PaletteChange,
        ) and getattr(self, "_ready", False) and current_theme() is not self.theme:
            self.apply_theme()
        super().changeEvent(event)

    def apply_theme(self) -> None:
        """Перечитать системную тему. Вызывается при старте и при её смене."""
        self.theme = current_theme()
        self.status_panel.set_theme(self.theme)
        self.chart.set_theme(self.theme)
        self.journals.set_theme(self.theme)
        # ⚠️ Плашки перерисовываются пересчётом состояния. Их цвет и цвет
        # надписи на них взяты из темы в момент показа: без этого уже
        # показанное предупреждение остаётся в цветах прежней темы, а надпись
        # на нём может стать нечитаемой — ровно то, ради чего заведён `text_on`.
        self.status_panel.apply_state(self._state)
        self._update_banners(self._state)

    def apply_state(self, state: RobotState) -> None:
        """Показать состояние робота. Ничего не вычисляет — только отображает."""
        self._state = state
        self.status_panel.apply_state(state)

        index = self.mode_box.findData(state.mode)
        if index >= 0 and index != self.mode_box.currentIndex():
            # Смена режима движком не должна выглядеть как команда пользователя,
            # иначе окно отправит её обратно и получится петля.
            self.mode_box.blockSignals(True)
            self.mode_box.setCurrentIndex(index)
            self.mode_box.blockSignals(False)

        self.start_action.setEnabled(not state.running and not state.token_read_only)
        self.stop_action.setEnabled(state.running)
        self._mirror_stream(state.connection)
        if state.token_read_only:
            self.start_action.setToolTip(
                "Запуск недоступен: токен выдан только на чтение. Заявки по такому "
                "токену брокер не примет. Нужен токен с правом торговли."
            )
        elif state.halted:
            # Кнопка намеренно НЕ гасится: что делает «Старт» с остановленным
            # роботом, решает не окно. Но обещать, что нажатие поможет, оно
            # тоже не вправе — отсюда прямая речь вместо умолчания.
            self.start_action.setToolTip(
                "Робот остановлен, и сам он из этого состояния не выйдет. "
                "Причина — в красной плашке над графиком: разберитесь с ней "
                "и проверьте счёт у брокера, прежде чем запускать снова."
            )
        else:
            self.start_action.setToolTip("Запустить робота.")

        self._update_banners(state)
        title = f"{TITLE} {version()} — {state.instrument}"
        if not state.simulation:
            title += " — БОЕВОЙ РЕЖИМ"
        self.setWindowTitle(title)

    def _mirror_stream(self, connection: Connection) -> None:
        """Привести кнопку связи к состоянию связи из снимка.

        Кнопка не помнит, что нажимали, — она показывает то, что пришло.
        Иначе после отказа сокета (нет сети, брокер отказал, инструмент истёк)
        она оставалась бы нажатой: панель говорила бы «подключено», а связи нет.
        Какое состояние считается «нажато» и почему — у `STREAM_BUTTON`.

        Отказ подключения приходит тем же снимком: `HistoryPort.stream` ставит
        `OFFLINE` и шлёт состояние заново (`restate`), и кнопка, нажатая при
        негодном токене, разжимается сама. Снимка нет только там, где порту
        нечего переслать: до первого прогона и в порту без сборки связи
        (`DetachedPort`, `_stream is None`) — тогда кнопка стоит там, куда
        её нажали, до первого снимка, ровно как список режимов. Окно снимок
        зеркалит, а не угадывает.
        """
        look = STREAM_BUTTON[connection]
        # ⚠️ Без блокировки `setChecked()` поднимает `toggled`, а он подключён
        # к `port.stream()`: снимок «связи нет» превращался бы в команду
        # «отключиться» — снятую подписку и строку журнала «Отключено
        # по команде из окна», которой никто не давал, — а снимок «связь
        # есть» — в повторное включение потока. Снимок показывается,
        # а не исполняется. Кнопка в панели при этом обновляется: Qt
        # доставляет виджетам `ActionChanged` событием, а не сигналом.
        with QSignalBlocker(self.stream_action):
            self.stream_action.setChecked(look.checked)
        self.stream_action.setText(look.label)
        self.stream_action.setToolTip(look.tooltip)

    def _update_banners(self, state: RobotState) -> None:
        # Остановка робота. Строкой в журнале её показывать нельзя: журнал
        # читают после происшествия, а остановленный робот с открытой позицией
        # это происшествие, которое ещё идёт. Причина приходит готовой строкой
        # из движка — окно её не сочиняет и не сокращает.
        halt = halt_note(state)
        if halt:
            self.halt_banner.show_text(halt, self.theme.danger)
        else:
            self.halt_banner.hide_text()
        # ⚠️ Кнопка возобновления привязана к **списку причин**, а не к тексту
        # остановки. Остановку показывает и прогон по истории, а снимать
        # у него нечего: `resume` работает по причинам порта. Кнопка,
        # видимая там, где нажатие ничего не делает, — обещание, которого
        # программа не выполнит.
        self.resume_action.setVisible(bool(state.halt_causes))

        if state.token_days_left is not None and state.token_days_left <= TOKEN_WARNING_DAYS:
            days = state.token_days_left
            if days <= 0:
                self.token_banner.show_text(
                    "Токен брокера истёк. Робот не сможет подавать заявки, "
                    "пока вы не получите новый токен в кабинете брокера.",
                    self.theme.danger,
                )
            else:
                self.token_banner.show_text(
                    f"Токен брокера истекает через {days} дн. Получите новый в кабинете "
                    "брокера заранее: после истечения робот перестанет подавать заявки.",
                    self.theme.warning,
                )
        else:
            self.token_banner.hide_text()

        # Плашка режима молчит, пока показана плашка остановки: вторая говорит
        # про ту же позицию всё то же самое и вдобавок называет причину.
        # Два одинаковых красных абзаца подряд читаются хуже одного —
        # тревога, повторённая дважды, перестаёт быть тревогой.
        if state.mode is Mode.OFF and state.position is not None and not state.halted:
            self.mode_banner.show_text(
                f"Робот выключен, но позиция открыта: {position_line(state.position)}. "
                "Она остаётся без присмотра. "
                f"{unmanaged_note(state.position, Context.of(state))} "
                "Закройте её вручную или включите режим «Только закрытие».",
                self.theme.danger,
            )
        else:
            self.mode_banner.hide_text()

        # Закреплённый отрезок истории. Плашка не тревожная, а поясняющая:
        # ничего не сломалось, просто показано прошлое. Но сказать это надо
        # крупно — на живом потоке застывший график читается как обрыв связи.
        if state.history_span:
            self.history_banner.show_text(state.history_span, self.theme.warning)
        else:
            self.history_banner.hide_text()
        self.recent_action.setVisible(bool(state.history_span))

    def show_chart(self, data: ChartData) -> None:
        self.chart.show_chart(data)

    def append_candle(self, candle: Candle) -> None:
        self.chart.append_candle(candle)

    def update_last_candle(self, candle: Candle) -> None:
        self.chart.update_last_candle(candle)

    def set_markers(self, layer: Layer, markers: Sequence[Marker]) -> None:
        self.chart.set_markers(layer, markers)

    def set_levels(self, levels: Sequence[PriceLevel]) -> None:
        self.chart.set_levels(levels)

    def set_shades(self, shades: Sequence[Shade]) -> None:
        self.chart.set_shades(shades)

    def set_trades(self, trades: Sequence[TradeRow], summary: TradesSummary | None = None) -> None:
        """Сделки прогона — в журнал и в график.

        Одни и те же строки в двух местах экрана: журнал показывает их таблицей,
        график — по выделенному правой кнопкой участку. Второго источника
        и второго набора чисел здесь не заводится.
        """
        self.journals.set_trades(trades, summary)
        self.chart.set_trades(trades)

    def append_trade(self, trade: TradeRow) -> None:
        """Робот закрыл позицию, пока окно открыто: строка идёт туда же, куда все."""
        self.journals.append_trade(trade)
        self.chart.append_trade(trade)

    def set_decisions(self, rows: Sequence[DecisionRow]) -> None:
        self.journals.set_decisions(rows)

    def show_error(self, text: str) -> None:
        """Сообщение об отказе. Уже человеческим языком: код сюда не доходит."""
        self.statusBar().showMessage(text, 15000)

    def show_stuck(self, text: str) -> None:
        """Долгая работа не отвечает: сказать об этом крупно и не убирать.

        Заведено по `B-025`, пойманному на владельце счёта 06.09.2026: порт
        был занят навсегда застрявшей задачей, отказывал каждой команде —
        по делу, — и окно выглядело мёртвым. Текст приходит готовым
        (`app/port.py::_stuck_words`): окно его не сочиняет и не сокращает.

        **Три места, а не одно, и каждое по своей причине.**

        * Плашка над графиком — потому что это состояние, которое длится.
          Строка состояния гаснет через пятнадцать секунд, а работа
          не отвечает по-прежнему.
        * Обе полоски хода — потому что во время прогона и во время загрузки
          на экране стоит **модальное** окошко полоски, и плашка за ним
          человеку не видна вовсе. Своей подписи полоска при этом не теряет:
          застрявшая работа доли не шлёт, и перетирать текст нечему.
        * Журнал решений — строкой уровня «предупреждение», и её пишет порт.
          Она остаётся после того, как всё кончилось, и по ней разбирают
          случившееся.

        ⚠️ Крестика у плашки нет намеренно (`_Banner`): снять её значило бы
        спрятать то, что продолжается. Гаснет она сама — пустой строкой,
        когда работа отозвалась или кончилась.

        ⚠️ Модального окна отсюда не открывается. Сигнал приходит **изнутри
        шага корутины** порта, и `exec()` поднял бы вложенный цикл событий,
        рвущий соседнюю задачу (`B-026`, `_open_modal`). Плашка и подпись
        полоски вложенного цикла не поднимают.
        """
        if not text:
            self.stuck_banner.hide_text()
            return
        self.stuck_banner.show_text(text, self.theme.danger)
        for dialog in (self._progress, self._loading):
            if dialog is not None:
                dialog.setLabelText(text)

    def _on_busy(self, busy: bool, what: str) -> None:
        """Длинная операция началась или кончилась.

        Конец закрывает полоску прогона, и это единственное место, где она
        закрывается по своей воле. Отчёт для этого не годится: он приходит
        не всегда — свечей на отрезок могло не найтись, настройки могли быть
        отвергнуты, — и полоска осталась бы на экране навсегда.
        """
        self.statusBar().showMessage(what if busy else "", 0 if busy else 1)
        if busy:
            if self._progress is not None:
                self._progress.setLabelText(what)
            return
        self._close_progress()

    def _on_progress(self, percent: int, what: str) -> None:
        self.statusBar().showMessage(f"{what}: {percent}%")
        if self._progress is not None:
            self._progress.setValue(percent)

    def _tick(self) -> None:
        self._clock.setText(f"{fmt_time(datetime.now(MSK), seconds=True)} МСК · {version()}")

    # -------------------------------------------------------------- команды

    def _on_mode_selected(self, index: int) -> None:
        mode = self.mode_box.itemData(index)
        if mode is None:
            return
        if mode is Mode.OFF and self._state.position is not None:
            agreed = self._confirm_unattended(
                "Позиция останется без присмотра",
                "Режим «Выключен» её не закрывает — это сделано намеренно, чтобы "
                "выключение робота само по себе не приводило к сделке. Но робот "
                "перестанет ею управлять.",
                "Выключить робота?",
            )
            if not agreed:
                self.apply_state(self._state)  # вернуть список к прежнему режиму
                return
        self.port.set_mode(mode)

    def request_stop(self) -> None:
        """«Стоп» при открытой позиции — с тем же вопросом, что и «Выключен».

        Прежде кнопка отправляла команду молча, а про последствие сообщала
        подсказка, которую читают, наведя мышь. Последствие при этом
        одинаковое: остановленный робот позицию не переворачивает и по концу
        окна не закрывает. Разная громкость у одинаковых последствий читается
        как «здесь опасно, а здесь нет».
        """
        if self._state.position is not None and not self._confirm_unattended(
            "Позиция останется без присмотра",
            "Остановка робота позицию не закрывает — это сделано намеренно, чтобы "
            "остановка сама по себе не приводила к сделке. Но робот перестанет "
            "ею управлять.",
            "Остановить робота?",
        ):
            return
        self.port.stop()

    def request_resume(self) -> None:
        """«Возобновить работу…» — с вопросом, называющим снимаемую причину.

        ⚠️ Молча кнопка не срабатывает **никогда**. Снимается одна причина
        из, может быть, нескольких, и после снятия робот вправе остаться
        остановленным: человек обязан прочитать, с чем именно он соглашается
        и что останется (`D-086`, аудит `/risk` 06.09.2026).

        ⚠️ Текст вопроса сочиняет не окно, а `ui/notices.py` — там же, где
        живёт текст красной плашки. Две копии одного утверждения про
        остановку разошлись бы, и разошлись бы молча.

        Пустой вопрос означает, что снимать нечего: причин поштучно порт
        не дал. Отправить команду вслепую нельзя — она снимет **что-то**,
        а что именно, окно человеку не назвало.
        """
        question = resume_question(self._state)
        if not question:
            self.show_error(
                "Возобновление: снимать нечего — программа не назвала ни одной "
                "причины остановки. Посмотрите журнал решений."
            )
            return
        answer = QMessageBox.warning(
            self,
            "Снять одну причину остановки",
            question,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.port.resume()

    def _confirm_unattended(self, title: str, lead: str, question: str) -> bool:
        """Вопрос перед тем, как позиция останется без управления.

        Один на три места: «Выключен», «Стоп» и закрытие программы. Середина
        текста — `unmanaged_note`, то есть то самое утверждение, которое
        в этом слое делается ровно один раз (`ui/notices.py`).
        """
        position = self._state.position
        if position is None:
            return True
        answer = QMessageBox.warning(
            self,
            title,
            f"Сейчас открыта позиция: {position_line(position)}.\n\n"
            f"{lead}\n\n{unmanaged_note(position, Context.of(self._state))}\n\n"
            f"{question}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def open_settings(self) -> None:
        dialog = SettingsDialog(self._settings, self)
        dialog.settings_changed.connect(self._on_settings_changed)
        try:
            dialog.exec()
        finally:
            # ⚠️ Без этого диалог живёт до конца работы программы: родителем
            # ему назначено окно, а окно не закрывается. Каждое открытие
            # настроек оставляло за собой ещё одну копию — вместе с полями,
            # подписками на тему и обработчиками.
            dialog.deleteLater()

    def open_calendar(self) -> None:
        """Календарь нерабочих дней. Отметки уезжают движку тем же путём.

        Своего пути до движка у календаря нет намеренно: отметки — часть
        настроек, и уходят они через `apply_settings`, как и всё остальное.
        Второй путь означал бы второе место, где настройки меняются, и первую
        же рассинхронизацию окна с движком.

        ⚠️ Дни, названные биржей, окно сегодня получить неоткуда: расписание
        брокера отвечает только про сегодняшний день, и проводка живого ответа
        не сделана (`F-002`). Календарь показывает их видом, который проверен
        сторожами, но список пока пуст.
        """
        dialog = CalendarDialog(self._settings.calendar, {}, self)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                marks = dialog.marks()
                if marks != self._settings.calendar:
                    self._on_settings_changed(self._settings.replace(calendar=marks))
        finally:
            # Та же причина, что у окна настроек: с родителем-окном диалог
            # пережил бы закрытие и остался в памяти вместе с полями.
            dialog.deleteLater()

    def open_templates(self) -> None:
        """Шаблоны настроек: просмотр, статистика, применение целиком.

        Библиотека лежит в папке данных программы, рядом с настройками и базой
        (`ui/templates.py`). Где эта папка, окно не решает: путь приходит
        от слоя сборки (`ui/backend.py`) — окно, запущенное без торговой части,
        говорит об этом вслух, а не открывает пустой список, читаемый как
        «шаблонов нет».

        ⚠️ Применение уходит движку тем же путём, что и правка настроек
        руками, — через `_on_settings_changed`. Второй путь означал бы второе
        место, где настройки меняются, и первое же расхождение окна с движком.
        Подтверждение «было → стало» показывает само окно шаблонов: человек
        обязан увидеть, что даёт ему набор, до того как согласится.
        """
        door = backend.current()
        if door is None:
            QMessageBox.warning(
                self,
                "Шаблоны настроек",
                "Программа собрана без торговой части: где хранить шаблоны "
                "и откуда брать статистику прогонов — неизвестно. Запустите "
                "программу обычным способом.",
            )
            return
        dialog = TemplatesDialog(Library(door.userdata()), self._settings, self)
        dialog.applied.connect(self._on_settings_changed)
        try:
            dialog.exec()
        finally:
            # Та же причина, что у окна настроек: с родителем-окном диалог
            # пережил бы закрытие и остался в памяти вместе с таблицами.
            dialog.deleteLater()

    # ------------------------------------------------- прогон на истории

    def open_backtest(self) -> None:
        """Попросить у движка список инструментов. Диалог откроется на ответ.

        Двухшажно, а не одним вызовом, и это не усложнение: список
        инструментов и границы их истории лежат в базе, а чтение базы идёт
        в своём потоке. Спросить синхронно значило бы читать диск в слоте
        Qt — то есть замереть на открытии меню, и тем дольше, чем больше
        накоплено истории.
        """
        self.statusBar().showMessage("Читаю, что есть в базе…", 3000)
        self.port.request_backtest_options()

    def _open_modal(self, work: Callable[[], None]) -> None:
        """Открыть модальное окно **следующим оборотом** цикла, а не сейчас.

        Это не вежливость, а обход пойманной живьём поломки (`B-026`,
        06.09.2026): владелец счёта запустил прогон по истории, и окно встало
        насмерть. В журнале:

            RuntimeError: Cannot enter into task <Task HistoryPort._refresh()>
            while another task <Task HistoryPort._backtest_options()>
            is being executed.

        Механизм, подтверждённый с трёх сторон:

        1. порт испускает сигнал **прямым** `emit` и живёт в главном потоке,
           значит Qt доставляет его синхронно — обработчик исполняется
           **внутри шага корутины**;
        2. сигнал испущен изнутри корутины (`HistoryPort._backtest_options`,
           `_load_history`, `_history_facts`);
        3. обработчик зовёт `exec()` модального окна, а `exec()` — это
           **вложенный цикл событий Qt**, и под `qasync` цикл Qt и есть цикл
           asyncio. Во вложенном цикле шагает соседняя задача, asyncio видит,
           что первая ещё помечена текущей, и отказывает. Задача умирает,
           диалог данных не получает, окно стоит.

        `singleShot(0, …)` разрывает третье звено: шаг корутины успевает
        закончиться, и вложенный цикл поднимается уже вне задачи.

        ⚠️ Чинится **на стороне окна**, а не в порту. Прямой `signal.emit`
        мимо `HistoryPort._send` запрещён и стережётся `tests/test_app_port.py`:
        `_send` — первый рубеж чистки секретов, и обходить его ради удобства
        доставки нельзя.

        ⚠️ Правило общее: **любое модальное окно, открываемое по сигналу
        порта, открывается отсюда.** Немодальные (`show`, полоска хода)
        вложенного цикла не поднимают и в отсрочке не нуждаются.
        """
        QTimer.singleShot(0, work)

    def show_backtest_dialog(self, options: BacktestOptions) -> None:
        """Открыть окно прогона на том, что нашлось в базе.

        Открытие отложено на следующий оборот цикла (`_open_modal`) — почему,
        сказано там же. Коротко: этот метод вызывается сигналом порта изнутри
        работающей корутины, а модальное окно поднимает вложенный цикл
        событий (`B-026`).
        """
        self._open_modal(lambda: self._run_backtest_dialog(options))

    def _run_backtest_dialog(self, options: BacktestOptions) -> None:
        """Само окно прогона. Зовётся отложенно, вне шага корутины.

        Шаблоны читаются здесь же и синхронно: это десяток записей из файла
        рядом с базой (десятки миллисекунд), и заводить ради них поток значило
        бы платить сложностью за то, чего человек не заметит. Без торговой
        части шаблонов нет вовсе — окно говорит это списком в одну строку,
        а не пустым выбором.
        """
        door = backend.current()
        templates: tuple[Template, ...] = ()
        if door is not None:
            with busy_cursor():
                templates = Library(door.userdata()).read().templates
        dialog = BacktestDialog(options, self._settings, templates, self)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            request = dialog.request()
        finally:
            # Та же причина, что у окна настроек: с родителем-окном диалог
            # пережил бы закрытие и остался в памяти вместе с полями.
            dialog.deleteLater()
        if request is None:
            return
        self._settings = request.settings
        self._start_progress()
        self.port.run_backtest(request)

    #: Наименьшая ширина полоски хода, в точках. Не «на глаз»: подпись
    #: переносится по словам, и на узкой полоске сообщение о застрявшей работе
    #: вытянулось бы в десяток строк, а на широкой — вылезло бы за край экрана
    #: ноутбука. 520 точек — примерно та же ширина, что у диалогов программы.
    BAR_WIDTH = 520

    def _bar(
        self, text: str, title: str, on_cancel: Callable[[], None]
    ) -> QProgressDialog:
        """Полоска хода с кнопкой «Отменить». Одна сборка на прогон и загрузку.

        Обе полоски настраивались семёркой одинаковых вызовов каждая, и обе
        обязаны одинаково показывать сообщение о застревании. Разойдись они —
        разошлось бы и оно, молча и ровно в том месте, ради которого заведено.

        ⚠️ Подпись **переносится по словам**, и это не оформление. Полоска —
        единственное, что видно во время прогона: окно за ней закрыто
        модальностью. Сюда приходит сообщение о застрявшей работе
        (`show_stuck`), а оно длиной в несколько строк; штатная подпись
        `QProgressDialog` не переносится вовсе. Снимок 06.09.2026 показал
        фразу, обрезанную посередине, — то есть сообщение, которого нет.
        """
        dialog = QProgressDialog(text, "Отменить", 0, 100, self)
        wrapping = QLabel(dialog)
        wrapping.setWordWrap(True)
        dialog.setLabel(wrapping)  # полоска забирает надпись себе
        dialog.setLabelText(text)
        dialog.setWindowTitle(title)
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setMinimumWidth(self.BAR_WIDTH)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setValue(0)
        dialog.canceled.connect(on_cancel)
        return dialog

    def _start_progress(self) -> None:
        """Полоска прогона с кнопкой отмены.

        Окно **модальное к программе**, но цикл событий оно не держит: Qt
        продолжает крутиться, прогон идёт, полоска обновляется. Модальность
        нужна ровно затем, чтобы во время счёта нельзя было поменять настройки
        и получить отчёт не о том, что просили.

        Полоску закрывает конец длинной операции (`_on_busy`), а не отчёт:
        отчёта может не быть вовсе — свечей на выбранный отрезок не нашлось,
        настройки отвергнуты. Полоска, закрываемая только отчётом, в этом
        случае осталась бы на экране навсегда.
        """
        self._close_progress()
        # Ноль в `_bar`: полоска показывается сразу, а не через штатные
        # 4 секунды Qt. Прогон на годе идёт десятки секунд, и первые четыре
        # из них окно выглядело бы просто замершим.
        dialog = self._bar(
            "Прогон робота по истории…", "Прогон на истории", self.port.cancel_backtest
        )
        self._progress = dialog
        dialog.show()

    def _close_progress(self) -> None:
        """Убрать полоску, если она была. Повторный вызов безвреден."""
        dialog, self._progress = self._progress, None
        if dialog is None:
            return
        # ⚠️ Отключение сигнала обязательно и стоит **до** закрытия: `close()`
        # у отменяемого диалога Qt испускает `canceled`, и без этой строки
        # закрытие полоски по концу прогона отправляло бы движку отмену
        # уже законченного прогона.
        with suppress(RuntimeError, TypeError):
            dialog.canceled.disconnect()
        dialog.close()
        dialog.deleteLater()

    def show_backtest_report(self, report: BacktestReport) -> None:
        """Окно отчёта. Немодальное: график с метками этого прогона рядом.

        Ссылка на окно держится полем — без неё Python унесёт обёртку
        сборщиком мусора сразу после возврата, и отчёт закроется сам собой
        через долю секунды.
        """
        self._close_progress()
        self.report_dialog = BacktestReportDialog(report, self)
        self.report_dialog.finished.connect(self.report_dialog.deleteLater)
        self.report_dialog.show()
        self.report_dialog.raise_()

    # ------------------------------------------------ загрузка истории

    def show_recent_data(self) -> None:
        """Снять закреплённый отрезок прогона и вернуться к текущим данным.

        Один метод на два входа — кнопку панели и крестик на плашке —
        намеренно. Крестик обязан делать **то же самое**, а не прятать
        плашку: спрятанная плашка при закреплённом отрезке — это ложь,
        живые свечи по-прежнему не приходят.
        """
        self.port.show_recent()

    def open_history(self) -> None:
        """Попросить опись базы. Диалог откроется на ответ, а не сразу.

        Двухшажно по той же причине, что и прогон: опись считается по всем
        минуткам инструмента, а это чтение базы в своём потоке. Спросить
        синхронно значило бы замереть на открытии меню, и тем дольше, чем
        больше накоплено истории.
        """
        self.statusBar().showMessage("Смотрю, что уже есть в базе…", 3000)
        self.port.request_history_facts(self._settings.instrument)

    def show_history_dialog(self, facts: HistoryFacts) -> None:
        """Окно загрузки на том, что нашлось в базе.

        Как и у прогона, открытие отложено на следующий оборот цикла:
        сигнал `history_facts_ready` испускается изнутри корутины порта,
        а модальное окно поднимает вложенный цикл событий (`B-026`).
        """
        self._open_modal(lambda: self._run_history_dialog(facts))

    def _run_history_dialog(self, facts: HistoryFacts) -> None:
        """Само окно загрузки. Зовётся отложенно, вне шага корутины.

        Глубину диалог берёт из настроек, а не из своей константы: до
        06.09.2026 в программе жили три числа глубины, и ни одно не совпадало
        с другим (`D-068`).
        """
        dialog = HistoryDialog(facts, self._settings, self)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            request = dialog.request()
        finally:
            # Та же причина, что у окна настроек: с родителем-окном диалог
            # пережил бы закрытие и остался в памяти вместе с полями.
            dialog.deleteLater()
        if request is None:
            self.show_error(
                "Инструмент не задан: впишите код фьючерса в настройках — "
                "например, MXZ6. Загружать нечего."
            )
            return
        self._start_loading(request.symbol)
        self.port.load_history(request)

    def _start_loading(self, symbol: str) -> None:
        """Полоска загрузки с кнопкой отмены.

        Окно **модальное к программе**, но цикл событий оно не держит: Qt
        продолжает крутиться, загрузка идёт, полоска обновляется. Модальность
        нужна затем, чтобы во время загрузки нельзя было сменить инструмент
        и получить свечи не того, что показано на графике.

        Кнопка меню гасится на то же время: вторая загрузка всё равно была бы
        отклонена портом, и отказ после нажатия хуже погашенной кнопки.

        ⚠️ Полоска **своя**, а не общая с прогоном на истории. Ту закрывает
        общий сигнал «работа кончилась» (`busy_changed`), который испускает
        и обычная перерисовка графика, а во время долгой загрузки перерисовка
        случается от каждой живой свечи. Общая полоска закрылась бы сама,
        и владелец счёта увидел бы, что окно отменило то, что он запустил
        минуту назад.
        """
        self._close_loading()
        self.history_action.setEnabled(False)
        # Ноль в `_bar`: полоска показывается сразу, а не через штатные
        # 4 секунды Qt. Первый запрос к бирже идёт секунды, и всё это время
        # окно выглядело бы просто замершим.
        dialog = self._bar(
            f"Загружаю историю {symbol} с биржи…",
            "Загрузка истории",
            self.port.cancel_history_load,
        )
        self._loading = dialog
        dialog.show()

    def _on_history_progress(self, percent: int, what: str) -> None:
        """Ход загрузки: и в полоску, и в строку состояния."""
        self.statusBar().showMessage(f"Загрузка истории: {what}")
        if self._loading is not None:
            self._loading.setValue(percent)
            self._loading.setLabelText(what)

    def show_history_result(self, outcome: HistoryLoadOutcome) -> None:
        """Чем кончилась загрузка. Окно с ответом — всегда, даже на отказе.

        Полоска закрывается **сразу**, а окно с ответом открывается следующим
        оборотом цикла: полоска модального цикла не поднимает, а окно ответа
        поднимает, и сигнал приходит изнутри корутины порта (`B-026`).
        """
        self._close_loading()
        self._open_modal(lambda: self._say_history_result(outcome))

    def _say_history_result(self, outcome: HistoryLoadOutcome) -> None:
        """Само окно с ответом. Зовётся отложенно, вне шага корутины.

        Молчание после долгого ожидания читается как «ничего не произошло»,
        и человек нажимает кнопку второй раз. Поэтому итог показывается
        и когда всё вышло, и когда не вышло, и когда загрузку отменили.
        """
        body = "\n\n".join(
            part for part in (outcome.detail, outcome.trouble) if part
        )
        box = QMessageBox(self)
        # ⚠️ Время жизни задаётся явно, и `deleteLater()` после `exec()`
        # здесь **запрещён**: Qt удаляет окно сообщения при закрытии сам,
        # и обращение к нему после `exec()` даёт «Internal C++ object already
        # deleted» — поймано на прогоне пути целиком 06.09.2026, до правки
        # падало на каждой загрузке.
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.setWindowTitle("Загрузка истории")
        box.setIcon(
            QMessageBox.Icon.Information if outcome.ok
            else QMessageBox.Icon.Warning
        )
        box.setText(outcome.headline or "Загрузка истории закончена")
        box.setInformativeText(body)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.button(QMessageBox.StandardButton.Ok).setText("Понятно")
        box.exec()

    def _close_loading(self) -> None:
        """Убрать полоску загрузки, если она была. Повторный вызов безвреден."""
        self.history_action.setEnabled(True)
        dialog, self._loading = self._loading, None
        if dialog is None:
            return
        # ⚠️ Отключение сигнала обязательно и стоит **до** закрытия: `close()`
        # у отменяемого диалога Qt испускает `canceled`, и без этой строки
        # закрытие полоски по концу загрузки отправляло бы порту отмену
        # уже законченной загрузки.
        with suppress(RuntimeError, TypeError):
            dialog.canceled.disconnect()
        dialog.close()
        dialog.deleteLater()

    def _on_settings_changed(self, settings: Settings) -> None:
        """Отдать настройки движку.

        Строку «объём изменён с 1 на 2» в журнал решений пишет движок: он знает,
        что принял, а что отклонил. Если бы её писало окно, в журнале появилось
        бы изменение, которого не было.
        """
        self._settings = settings
        self.port.apply_settings(settings)

    def _on_settings_echo(self, settings: Settings) -> None:
        self._settings = settings

    def settings(self) -> Settings:
        return self._settings

    def export_journals(self) -> None:
        """Выгрузить обе вкладки одной кнопкой (ТЗ §4.6)."""
        folder = QFileDialog.getExistingDirectory(self, "Куда выгрузить журналы")
        if not folder:
            return
        self.export_journals_to(Path(folder))

    def export_journals_to(self, folder: Path) -> list[Path]:
        """Записать оба журнала в папку. Отделено от диалога ради проверяемости.

        **Отказ виден в окне.** Раньше `PermissionError` уходил из слота Qt
        в консоль, которой у собранной программы нет: строка состояния
        не менялась, и нажавший кнопку видел ровно то же, что до нажатия, —
        то есть считал, что отчёт выгружен. Находка `/risk` 03.09.2026.

        **Либо оба файла, либо ни одного нового.** Отчёт за день — это две
        половины, сделки и решения; одна половина на диске выглядит как целый
        отчёт. По числу сделок в нём владелец счёта выбирает объём
        (решение 0011).

        ⚠️ Строки берутся из моделей окна, то есть **минуя базу**: их привозят
        сигналы порта. Значит чистка, стоящая на записи в базу, здесь
        не срабатывает, и её приходится звать отдельно — `self._sanitize`
        уходит в `write_csv` и проходит по каждой ячейке.
        """
        stamp = datetime.now(MSK).strftime(STAMP_FORMAT)
        headers, rows = trades_table(
            self.journals.trades_model.rows(), self.journals.summary()
        )
        decision_headers, decision_rows = decisions_table(self.journals.decisions_model.rows())
        jobs: tuple[ExportJob, ...] = (
            (folder / f"trades_{stamp}.csv", headers, rows),
            (folder / f"decisions_{stamp}.csv", decision_headers, decision_rows),
        )
        try:
            with busy_cursor():
                written = self._write_all(jobs)
        except Exception as error:  # noqa: BLE001 — молчаливый отказ хуже широкой ловли
            # Широко намеренно: сюда ведёт кнопка, а не библиотечный вызов.
            # Всё, что не поймано здесь, PySide6 печатает в консоль и глотает,
            # и это ровно тот отказ, ради которого метод переписан.
            self._export_refused(folder, jobs, write_failure(error))
            return []
        self.statusBar().showMessage(
            "Выгружено: " + ", ".join(path.name for path in written), 15000
        )
        return written

    def _write_all(self, jobs: Sequence[ExportJob]) -> list[Path]:
        """Единица работы с компенсацией: либо все файлы, либо ни одного нового.

        Компенсация трогает только то, чего в каталоге до нас не было. Имя
        файла считается по минуте, поэтому вторая выгрузка в ту же минуту
        попадает в те же имена; удалять в ответ на свою неудачу файл, который
        владелец счёта уже забрал, нельзя.

        Само удаление молчит намеренно: если не удалось и оно, файл останется,
        и о нём скажет сообщение. `_export_refused` перечисляет то, что
        действительно лежит на диске, а не то, что мы собирались удалить.
        """
        created: list[Path] = []
        written: list[Path] = []
        try:
            for target, headers, rows in jobs:
                if not target.exists():
                    created.append(target)
                written.append(write_csv(target, headers, rows, self._sanitize))
        except Exception:
            for path in created:
                with suppress(OSError):
                    path.unlink(missing_ok=True)
            raise
        return written

    def _export_refused(self, folder: Path, jobs: Sequence[ExportJob], reason: str) -> None:
        """Сказать, что выгрузки не было, — окном, а не строкой состояния.

        Строка состояния для этого слаба: её не замечают, а незамеченный отказ
        стоит владельцу счёта отчёта за день. Поэтому модальное окно, которое
        закрывают рукой, и та же фраза следом в строке состояния — как след,
        переживающий закрытие окна.

        Через `self._sanitize` проходит **весь** текст. В нём выбранный
        человеком путь и причина от операционной системы — две строки, которых
        этот слой не составлял и за содержимое которых не отвечает.
        """
        stayed = sorted(path.name for path, *_ in jobs if path.exists())
        note = (
            "Ни одного файла не создано."
            if not stayed
            else "В каталоге остались файлы с теми же именами: "
            + ", ".join(stayed)
            + ". Целым отчётом их считать нельзя: они остались от прежней выгрузки "
            "или переписаны той, что сейчас не удалась."
        )
        QMessageBox.critical(
            self,
            "Журналы не выгружены",
            self._sanitize(
                f"Не удалось записать журналы в каталог:\n{folder}\n\n"
                f"Причина: {reason}.\n\n{note}\n\n"
                "Выберите другой каталог или устраните причину и повторите выгрузку."
            ),
        )
        self.show_error(self._sanitize(f"Журналы не выгружены: {reason}"))

    def show_about(self) -> None:
        QMessageBox.about(
            self,
            "О программе",
            f"<b>{TITLE}</b><br>Версия {version()}<br><br>"
            f"{self.chart.renderer_note}<br><br>"
            "Всё торговое время — московское.",
        )

    # ------------------------------------------------------------- закрытие

    def closeEvent(self, event: QCloseEvent) -> None:
        """Перехват закрытия окна при открытой позиции.

        Тихий выход недопустим: закрытая программа не делает переворот
        и не закрывает позицию по концу окна, а позиция на счёте остаётся.

        ⚠️ Чего закрытие программы **не** отменяет — вооружённого тейка:
        это заявка у брокера, она переживает и выключение робота, и выход
        из программы (решение 0008). Прежний текст обещал обратное.
        """
        if self._state.position is not None and not self._confirm_unattended(
            "Открыта позиция",
            "Закрытие программы позицию не закрывает — это сделано намеренно, "
            "чтобы выход из программы сам по себе не приводил к сделке. "
            "Но робот перестанет ею управлять.",
            "Всё равно закрыть программу?",
        ):
            event.ignore()
            return
        event.accept()
