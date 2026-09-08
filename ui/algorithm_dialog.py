"""Окно выбора торгового алгоритма и всплывающее описание правила.

Зачем отдельное окно
--------------------
Просьба владельца счёта 08.09.2026: «надо в отдельном окне выбор алгоритма —
там кнопку подробнее с всплывающим описанием». До этого описание правила
стояло абзацем прямо на вкладке «Сигнал», и замер показал, почему так нельзя:
абзац занимает **391 точку** при умолчаниях и **459** при включённом фильтре
против пилы, то есть половину вкладки, а поля средней уходят под прокрутку.
Человек приходит на «Сигнал» править период — и не видит поля.

Отсюда разделение. На вкладке остаётся **строка** — какой алгоритм выбран —
и кнопка сюда. Здесь лежит выбор и полное описание, которое читают один раз
и внимательно.

⚠️ Кнопка «Подробнее» стоит **рядом со списком**, а не под ним и не в окне
настроек. Описание — единственный способ узнать, что алгоритм делает,
не открывая исходник; прочитать его нужно **до** выбора, а не после. Кнопка,
показывающая описание только выбранного, требовала бы сперва выбрать чужое
правило, то есть поменять настройку, чтобы узнать, стоит ли её менять.

Откуда берётся текст
--------------------
Ниоткуда отсюда. Название, правило одной фразой и правило абзацами приходят
готовыми в `ui.models.AlgorithmOption`: собирает их **сам алгоритм** из своей
таблицы утверждений, `app/` перекладывает (`app/convert.py::algorithms`).
Абзац, написанный здесь руками, разошёлся бы с поведением робота при первой
правке алгоритма — и разошёлся бы молча, текст не падает.

Торгового правила в этом файле нет ни одного. Окно показывает список и
возвращает выбранное имя; что оно значит, решает `engine/`.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.models import AlgorithmOption
from ui.theme import current as current_theme

__all__ = [
    "ALONE_NOTE",
    "APPLY_NOTE",
    "EMPTY_NOTE",
    "INTRO",
    "NOTHING_AT_ALL",
    "NOTHING_TO_CHOOSE_FROM",
    "ROWS_SHOWN",
    "AlgorithmDialog",
    "AlgorithmDetails",
    "details_preamble",
]

#: Что такое торговый алгоритм — одной фразой, и сразу его граница.
#:
#: ⚠️ Правило конкретного алгоритма здесь не пересказывается: оно приходит
#: от самого алгоритма и живёт в описании. Здесь только то, что верно для
#: любого из них и потому не может разойтись ни с одним.
INTRO = (
    "Торговый алгоритм — это правило, по которому робот решает, в какую "
    "сторону ему стоять: в лонг, в шорт или никуда. Больше он не решает "
    "ничего. Когда робот работает, каким объёмом, где тейк и какие "
    "предохранители — это настройки, и от выбора алгоритма они не зависят."
)

#: Когда выбор начнёт действовать. Сказано здесь, а не только в настройках:
#: окно закрывается по «ОК», и человек вправе решить, что этого достаточно.
APPLY_NOTE = (
    "Выбор вступит в силу после «Применить» или «ОК» в окне настроек. "
    "Пока вы этого не сделали, робот работает прежним алгоритмом."
)

#: Список из одного пункта. Сказать прямо, что так и задумано.
#:
#: ⚠️ Молчание здесь — дефект по правилу 13 `CLAUDE.md`: список с одной
#: строкой и без объяснения читается как «остальные не загрузились».
ALONE_NOTE = (
    "В этой сборке программы алгоритм пока один. Выбирать не из чего, "
    "и это не поломка: список станет длиннее, когда появятся новые."
)

#: Каталог не приехал вовсе. Пустое место читалось бы как «алгоритмов нет».
#:
#: ⚠️ Здесь сказано **и что делать**, и это не вежливость. Прежняя редакция
#: кончалась на «выбрать сейчас не из чего», а строка под списком при этом
#: требовала «выберите один из списка — иначе робот работать не сможет»:
#: владелец счёта 09.09.2026 получил указание, которое невозможно выполнить,
#: и никакого выхода из окна, кроме «Отмены», о которой не сказано. Указание,
#: которого нельзя исполнить, хуже молчания: молчание заставляет искать,
#: а такое указание заставляет искать **не там**.
EMPTY_NOTE = (
    "Список торговых алгоритмов сюда не пришёл: окно открыто без торговой "
    "части либо программа не ответила. Выбирать не из чего, и изменить "
    "что-либо этим окном сейчас нельзя — закройте его «Отменой»: настройки "
    "останутся прежними. Чтобы программа спросила список заново, закройте "
    "настройки и откройте их снова; если список так и не появился — "
    "перезапустите программу."
)

#: Что стоит под списком, когда выбирать не из чего, а имя в настройках есть.
#:
#: Отдельно от `EMPTY_NOTE`, потому что отвечает на другой вопрос: та строка
#: говорит, что случилось со списком, эта — что сейчас с настройкой. Обещать
#: здесь, что робот сработает, нельзя: есть ли «{name}» в этой сборке,
#: программа без каталога не знает и знать не может.
NOTHING_TO_CHOOSE_FROM = (
    "В настройках стоит алгоритм «{name}». Есть ли он в этой сборке, "
    "программа сейчас сказать не может: список не пришёл. Это окно настройку "
    "не трогало и не тронет."
)

#: То же самое, когда и имени в настройках нет. Пустое место читалось бы
#: как «выбор сделан», а он не сделан и сделан быть не может.
NOTHING_AT_ALL = "Выбирать не из чего: список торговых алгоритмов не пришёл."

#: Сколько строк списка видно без прокрутки.
#:
#: ⚠️ Список **не растягивается** на всё окно, и это не вкус. В реестре
#: сегодня один алгоритм: растянутый список показал бы одну строку и полтораста
#: точек пустоты под ней — окно читалось бы как незагрузившееся. Высота идёт
#: за числом строк и упирается в этот потолок, дальше работает прокрутка.
ROWS_SHOWN = 6


def details_preamble(option: AlgorithmOption) -> str:
    """Чьи числа стоят в описании — фразой над ним.

    ⚠️ Без этой строки описание невыбранного алгоритма читалось бы как
    «правило с вашими настройками», а числа в нём — чужие: полей этого
    алгоритма в окне нет, показываются его собственные умолчания
    (`app/convert.py::_algorithm_details`).
    """
    if option.chosen:
        return "Правило с вашими нынешними настройками."
    return (
        "Правило с умолчаниями самого алгоритма: ваших чисел здесь нет. "
        "Они появятся, когда вы выберете этот алгоритм и нажмёте «Применить»."
    )


def _hint(text: str) -> QLabel:
    """Приглушённая строка с переносом. Цвет — из темы, а не числом.

    Тот же приём и тот же цвет, что у подсказок окна настроек
    (`ui/settings_dialog.py::_hint`): `Theme.text_dim` заведён ровно под это
    и читается в обеих темах, а `palette(mid)` давал 1,7:1 в светлой.
    """
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setStyleSheet(f"color: {current_theme().text_dim};")
    return label


class AlgorithmDialog(QDialog):
    """Выбор торгового алгоритма. Ничего не применяет — возвращает выбор.

    Применяет настройки окно настроек, обычным путём: выбранное имя уходит
    в `Settings.strategy_id` и оттуда в движок по «Применить». Отдельная
    команда порта здесь была бы второй дорогой к тем же настройкам — и первая
    же расхождение между ними никто бы не заметил.
    """

    #: Свойство-пометка на строке про единственный алгоритм: по нему её
    #: находит проверка, а не по тексту.
    ALONE_ROLE = "терминал-алгоритм-один"

    def __init__(
        self,
        options: Sequence[AlgorithmOption] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Торговый алгоритм")
        self.setModal(True)
        # Высота под содержимое, а не «побольше на всякий случай»: список
        # ростом со свои строки, окно ростом со список (см. `ROWS_SHOWN`).
        self.resize(560, 340)

        self._options = tuple(options)

        self.list = QListWidget()
        # ⚠️ Одиночный выбор и никакой правки: список — это переключатель,
        # а не таблица. Множественный выбор здесь означал бы вопрос «а какой
        # из двух работает», на который ответа нет.
        self.list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.list.setWordWrap(True)
        for option in self._options:
            item = QListWidgetItem(option.title)
            item.setData(Qt.ItemDataRole.UserRole, option.id)
            # Правило одной фразой во всплывающей подсказке — для того, кто
            # ведёт мышью по списку и не жмёт «Подробнее».
            item.setToolTip(option.summary)
            self.list.addItem(item)
        self.list.currentRowChanged.connect(self._on_current_changed)
        self._fit_the_list()

        self.details_button = QPushButton("Подробнее…")
        # ⚠️ Не кнопка по умолчанию: Enter в этом окне обязан значить «ОК».
        # Иначе клавиатурный обход упирался бы в описание вместо выбора.
        self.details_button.setAutoDefault(False)
        self.details_button.setDefault(False)
        self.details_button.setToolTip(
            "Показать, как этот алгоритм принимает решения — словами "
            "и с числами. Читать стоит до выбора, а не после."
        )
        self.details_button.clicked.connect(self.show_details)

        #: Правило выделенного алгоритма одной фразой — под списком, без
        #: нажатий. Кнопка «Подробнее» показывает то же самое подробно.
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        # Простой текст, а не разметка: в описании есть «•» и «—», а завтра
        # появится «<». Угадывание разметки съело бы половину строки молча.
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("ОК")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        # ⚠️ Принимать нечего — «ОК» выключен, и сказано почему. Живая кнопка
        # при пустом списке возвращала бы пустое имя, а окно настроек молча
        # его отбрасывало (`SettingsDialog.choose_algorithm`): нажатие, после
        # которого не происходит ничего и не сказано ничего. Правило 13
        # `CLAUDE.md` — это отдельный дефект, а не мелочь.
        if not self._options:
            accept = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
            accept.setEnabled(False)
            accept.setToolTip(
                "Выбирать не из чего: список алгоритмов не пришёл. Закройте "
                "окно «Отменой» — настройки останутся прежними."
            )

        self._build_layout()
        self._set_tab_order()
        self._on_current_changed(self.list.currentRow())

    def _fit_the_list(self) -> None:
        """Высота списка — по числу строк, но не больше потолка `ROWS_SHOWN`.

        ⚠️ Иначе список тянется на всё окно, и при одном алгоритме под
        единственной строкой остаётся полтораста точек пустоты. Пустота
        на месте списка читается как «остальное не загрузилось» — то есть
        как поломка (правило 13 `CLAUDE.md`), хотя всё исправно.

        ⚠️ Нижняя граница — **две** строки, а не одна. Список ростом ровно
        в одну строку выглядит не списком, а подсвеченной полосой: непонятно,
        что это выбор и что в нём можно двигаться стрелками.
        """
        rows = min(max(len(self._options), 2), ROWS_SHOWN)
        one = (
            self.list.sizeHintForRow(0) if self._options
            else self.list.fontMetrics().height() + 6
        )
        self.list.setFixedHeight(rows * one + 2 * self.list.frameWidth() + 6)

    def _build_layout(self) -> None:
        """Список и кнопка «Подробнее» в одной строке — она рядом со списком."""
        body = QVBoxLayout(self)
        body.setContentsMargins(12, 12, 12, 12)
        body.addWidget(_hint(INTRO))

        row = QHBoxLayout()
        row.addWidget(self.list, 1)
        beside = QVBoxLayout()
        beside.addWidget(self.details_button)
        beside.addStretch(1)
        row.addLayout(beside)
        body.addLayout(row)

        body.addWidget(self.summary)
        if not self._options:
            body.addWidget(self._marked_note(EMPTY_NOTE))
        elif len(self._options) == 1:
            body.addWidget(self._marked_note(ALONE_NOTE))
        body.addWidget(_hint(APPLY_NOTE))
        # Свободное место уходит сюда, а не в список: пустота под единственной
        # строкой списка читается как незагрузившийся список.
        body.addStretch(1)
        body.addWidget(self.buttons)

    def _marked_note(self, text: str) -> QLabel:
        """Строка про состав списка, помеченная для проверки."""
        label = _hint(text)
        label.setProperty(self.ALONE_ROLE, True)
        return label

    def _set_tab_order(self) -> None:
        """Список → «Подробнее» → кнопки. Порядок обхода равен порядку чтения."""
        QWidget.setTabOrder(self.list, self.details_button)
        QWidget.setTabOrder(self.details_button, self.buttons)

    # ---------------------------------------------------------------- обмен

    def set_chosen(self, strategy_id: str) -> None:
        """Выделить алгоритм по имени. Незнакомое — выделения нет, и это видно.

        ⚠️ Подстановки первого попавшегося здесь нет намеренно. Окно, молча
        выделившее не тот алгоритм, вернуло бы по «ОК» имя, которого владелец
        счёта не выбирал, — то есть смену торгового правила без его ведома.
        Пустое выделение честно означает «выбранного алгоритма в этом списке
        нет», а строка под списком говорит это словами.
        """
        for row, option in enumerate(self._options):
            if option.id == strategy_id:
                self.list.setCurrentRow(row)
                return
        self.list.setCurrentRow(-1)
        self._show_summary(None, missing=strategy_id)

    def chosen_id(self) -> str:
        """Имя выделенного алгоритма. Пусто — не выделен ни один."""
        option = self.current_option()
        return option.id if option is not None else ""

    def current_option(self) -> AlgorithmOption | None:
        """Выделенный алгоритм целиком. `None` — выделения нет."""
        row = self.list.currentRow()
        if 0 <= row < len(self._options):
            return self._options[row]
        return None

    # ------------------------------------------------------------ поведение

    def _on_current_changed(self, _row: int) -> None:
        """Выделение сменилось: строка под списком и доступность кнопки."""
        option = self.current_option()
        self.details_button.setEnabled(option is not None)
        self._show_summary(option)

    def _show_summary(
        self, option: AlgorithmOption | None, *, missing: str = ""
    ) -> None:
        """Правило выделенного одной фразой. Ничего не выделено — сказать это.

        ⚠️ Случая **два**, а не один, и путать их нельзя — на этом окно
        и сломалось 09.09.2026. «Выберите один из списка» имеет смысл только
        тогда, когда список есть. При пустом списке та же фраза превращалась
        в указание, которое невозможно выполнить, и владелец счёта оставался
        в тупике: программа велела сделать то, чего сделать нельзя.

        * список пришёл, а выбранного в нём нет — это старая настройка
          или более новая сборка, и выход есть: выбрать из списка;
        * список не пришёл вовсе — выхода **здесь** нет, и это говорится
          прямо, вместе с тем, что делать снаружи (`EMPTY_NOTE`).
        """
        if option is not None:
            self.summary.setText(option.summary)
            return
        if not self._options:
            # Выбирать не из чего. Требовать выбора — тупик; здесь говорится
            # только правда о том, что сейчас с настройкой.
            self.summary.setText(
                NOTHING_TO_CHOOSE_FROM.format(name=missing) if missing
                else NOTHING_AT_ALL
            )
            return
        if missing:
            self.summary.setText(
                f"Сейчас в настройках стоит алгоритм «{missing}», а в этой "
                "сборке его нет. Выберите один из списка — иначе робот "
                "работать не сможет."
            )
            return
        self.summary.setText("Алгоритм не выбран.")

    def show_details(self) -> None:
        """Всплывающее описание выделенного алгоритма."""
        option = self.current_option()
        if option is None:
            return
        window = AlgorithmDetails(option, self)
        try:
            window.exec()
        finally:
            # ⚠️ Без этого окно живёт до конца работы программы: родителем
            # ему назначен диалог, а `exec()` его не удаляет. Та же правка,
            # что у окна настроек в `MainWindow.open_settings`.
            window.deleteLater()


class AlgorithmDetails(QDialog):
    """Описание правила целиком: прокрутка, перенос, ничего не обрезано.

    ⚠️ Поле с прокруткой, а не ярлык и не всплывающая подсказка. Длина текста
    **переменная**: 391 точка при умолчаниях, 459 при включённом фильтре
    против пилы, и алгоритм с четырьмя утверждениями даст больше. Подсказка
    Qt прокрутки не имеет вовсе, а ярлык в окне фиксированного размера
    обрезал бы хвост молча — замер 06.09.2026: длинное сообщение в полоске
    хода обрезалось на 2256 точках против 576, и человек читал половину фразы.

    ⚠️ Простой текст, а не разметка. В описании есть «•» и «—», а «<» появится
    в первом же алгоритме, который скажет «закрытие < средней»: угадывание
    разметки съело бы кусок абзаца, не сказав об этом.
    """

    def __init__(self, option: AlgorithmOption, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Подробнее: {option.title}")
        self.setModal(True)
        # Размер под самое длинное известное описание: 29 строк при включённом
        # фильтре против пилы (замер 08.09.2026) помещаются целиком, прокрутка
        # остаётся про запас. Окно растягивается мышью, текст переносится.
        self.resize(640, 660)
        self.setMinimumSize(420, 320)

        self.preamble = _hint(details_preamble(option))

        self.body = QPlainTextEdit(option.details)
        self.body.setReadOnly(True)
        # Перенос по ширине окна: длинные предложения описания иначе уехали бы
        # за правый край, и читать пришлось бы горизонтальной прокруткой.
        self.body.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        # Текст читают и переносят в переписку — выделение мышью и Ctrl+C.
        self.body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.buttons.button(QDialogButtonBox.StandardButton.Close).setText("Закрыть")
        self.buttons.rejected.connect(self.reject)
        self.buttons.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(self.preamble)
        layout.addWidget(self.body, 1)
        layout.addWidget(self.buttons)

    def text(self) -> str:
        """Показанное описание целиком — тем же текстом, что пришло."""
        return self.body.toPlainText()
