"""Окно «Шаблоны настроек»: набор со своей историей, применяемый целиком.

Просьба владельца счёта 05.09.2026, дословно: «рядом с настройками кнопка
"Шаблоны настроек" — открываем, просмотр, **статистика (период и прибыль-убыток,
количество сделок)**, применить. Сохранить текущие со значениями».

Статистика — суть просьбы, а не украшение
------------------------------------------
Человек смотрит на список и должен видеть не «Вариант 3», а **на чём это дало
сколько**. Именно это превращает шаблоны из удобства в инструмент подбора:
сегодня он подбирает настройки вечером, а наутро не может сказать, при каких
именно получены вчерашние сделки.

Три места, где легко соврать, и что здесь сделано против каждого
-----------------------------------------------------------------
**1. Шаблон, ни разу не запускавшийся, статистики не имеет.** В ячейке стоит
прочерк и слова «не запускался», а **не ноль**: ноль читается как «дало ноль
рублей», и это другое утверждение.

**2. Период — свойство прогона, а не шаблона.** Один набор мог гоняться
на трёх отрезках с разным итогом. В строке показан **последний** прогон
и назван таковым, а все прогоны видны отдельным списком ниже. Складывать
разные периоды в одну сумму окно не имеет права: это подгонка на глаз.

**3. Шаблон, применённый частично, — худшее из возможного.** Применение идёт
одним набором и только после подтверждения «было → стало»
(`ui/confirm_changes.py`), где названо и то, чего в шаблоне не было.

Откуда цифры
------------
Из записанных прогонов (`journal_session`), через `ui/backend.py`. Ни одно
число здесь не считается: считать их отдельно значило бы завести вторую правду
о деньгах. Связь «этот прогон сделан этим набором» даёт сравнение снимков
настроек (`app/runs.py::matching_runs`).

Обмен наборами
--------------
Решение 0051. Две кнопки рядом с остальными: «Импортировать…» и
«Экспортировать…». Импорт — **добавление**, а не замена: список наборов
принадлежит владельцу счёта, и импорт, стирающий его, однажды унесёт
единственный набор, который работал. Совпадение имён разрешается вопросом
на каждое имя, а не молча.

Импортированный набор приходит **без прогонов**, и это правда, а не пропажа:
цифры берутся из журнала прогонов, а на **этой** истории чужой набор
не гонялся. Столбцы статистики покажут ему то же, что любому незапускавшемуся
набору, — прочерк и слова «не запускался».

⚠️ У примера из поставки в столбце «Откуда взят» стоит отрезок и инструмент,
на которых он отобран. Набор без этой строки советом не является, и окно
говорит это прямо — и здесь, и в окне импорта.

⚠️ Чтение базы **синхронное**, под курсором ожидания. Это чтение двухсот
записей из местного файла (десятки миллисекунд), а окно открывают раз
в неделю; заводить ради него поток значило бы платить сложностью за то, чего
человек не заметит. Всё, что дольше и сложнее, в этом слое запрещено
(`ui/background.py`).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ui import backend
from ui.backend import RunStats, TemplateRun
from ui.background import busy_cursor
from ui.confirm_changes import confirm_changes
from ui.formatting import EMPTY, fmt_datetime, fmt_money
from ui.models import Settings
from ui.templates import (
    Library,
    Merged,
    NameClash,
    Template,
    export_templates,
    merge_templates,
)
from ui.templates_import import ImportDialog
from ui.theme import current as current_theme

__all__ = ["TemplatesDialog"]

#: Столбцы списка шаблонов: подпись и подсказка к ней.
#:
#: Таблица, а не семь кусков разметки: по ней собирается заголовок, по ней же
#: идёт заполнение строки. Столбец, дописанный мимо неё, оказался бы без
#: подписи или без значения — и молча.
COLUMNS: tuple[tuple[str, str], ...] = (
    ("Шаблон", "Название, которое вы дали набору настроек."),
    ("Сохранён", "Когда набор записан. Время московское."),
    ("Инструмент", "Инструмент и размер свечи из самого набора."),
    ("Прогонов", "Сколько раз этот набор уже прогонялся и записан в журнал."),
    (
        "Последний прогон",
        "Отрезок, на котором гнали в последний раз. Разные отрезки не "
        "складываются: это были бы несопоставимые цифры.",
    ),
    ("Сделок", "Сколько сделок дал последний прогон этого набора."),
    (
        "Прибыль-убыток",
        "Чистый результат последнего прогона — после комиссии. Прочерк "
        "означает «прогонов не было», а не «ноль рублей».",
    ),
    (
        "Откуда взят",
        "На каком отрезке и на каком инструменте набор отобран. Заполнено "
        "у примеров, приехавших с программой; у ваших собственных наборов "
        "пусто — вы знаете, откуда они.",
    ),
)

#: Какой столбец — деньги. Из таблицы по подписи, а не числом: столбец,
#: дописанный в конец, иначе перекрасил бы не тот.
PROFIT_COLUMN: int = [title for title, _ in COLUMNS].index("Прибыль-убыток")

#: Столбцы списка прогонов выбранного набора.
RUN_COLUMNS: tuple[str, ...] = (
    "Когда", "Что это было", "Отрезок", "Сделок", "Прибыль-убыток",
)

#: Какой столбец списка прогонов — деньги. Берётся из таблицы, а не числом:
#: столбец, вставленный в середину, иначе перекрасил бы соседа.
RUN_PROFIT_COLUMN: int = RUN_COLUMNS.index("Прибыль-убыток")

#: Что стоит в ячейках статистики у набора, который ни разу не гоняли.
#: Слова, а не ноль: ноль читается как утверждение о деньгах.
NEVER_RUN = "не запускался"

#: Знаки, запрещённые в именах файлов Windows. Linux терпит их все, кроме `/`,
#: но файл выгрузки едет на другую машину — берётся строгий набор.
FORBIDDEN_IN_FILE_NAMES = '<>:"/\\|?*'


class TemplatesDialog(QDialog):
    """Список шаблонов, статистика каждого, применение целиком."""

    #: Настройки, которые человек выбрал применить. Дальше — дело главного окна.
    applied = Signal(object)  # Settings

    def __init__(
        self,
        library: Library,
        current: Settings,
        parent: QWidget | None = None,
        *,
        examples: Path | None = None,
    ) -> None:
        """:param library: где лежат шаблоны. :param current: что стоит сейчас.

        :param examples: папка примеров. `None` — та, что приехала
            с программой; задаётся отдельно только в проверках.
        """
        super().__init__(parent)
        self.setWindowTitle("Шаблоны настроек")
        self.setModal(True)
        self.resize(1100, 700)

        self._library = library
        self._current = current
        self._examples = examples
        self._templates: tuple[Template, ...] = ()
        self._stats = RunStats()

        self._build_widgets()
        self._lay_out()
        self._set_tab_order()
        self.reload()

    # ---------------------------------------------------------------- сборка

    def _build_widgets(self) -> None:
        """Собрать поля окна — каждую часть своим методом.

        Сборка одной длинной чередой прячет пропущенную строку среди сорока
        похожих; здесь пропажу видно по методу, который её не сделал.
        """
        self._build_notes()
        self._build_table()
        self._build_runs_table()
        self._build_buttons()

    def _build_notes(self) -> None:
        self.hint = QLabel(
            "Шаблон — это набор настроек целиком, сохранённый под именем. "
            "Цифры рядом взяты из записанных прогонов: программа находит те, "
            "что сделаны ровно этим набором. Применение меняет все настройки "
            "разом — и только после того, как вы увидите список изменений."
        )
        self.hint.setWordWrap(True)

        self.trouble = QLabel()
        self.trouble.setWordWrap(True)
        self.trouble.setVisible(False)

        self.where = QLabel(f"Файл: {self._library.path}")
        self.where.setWordWrap(True)
        self.where.setStyleSheet(f"color: {current_theme().text_dim};")

    def _build_table(self) -> None:
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels([title for title, _ in COLUMNS])
        for column, (_, tip) in enumerate(COLUMNS):
            item = self.table.horizontalHeaderItem(column)
            if item is not None:
                item.setToolTip(tip)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.itemSelectionChanged.connect(self._on_selected)
        self.table.itemDoubleClicked.connect(lambda _: self.apply_selected())

    def _build_runs_table(self) -> None:
        self.runs = QTableWidget(0, len(RUN_COLUMNS))
        self.runs.setHorizontalHeaderLabels(list(RUN_COLUMNS))
        self.runs.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.runs.setAlternatingRowColors(True)
        self.runs.verticalHeader().setVisible(False)
        self.runs.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )

        self.snapshot = QPlainTextEdit()
        self.snapshot.setReadOnly(True)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.runs, "Прогоны этого набора")
        self.tabs.addTab(self.snapshot, "Что в наборе")

    def _build_buttons(self) -> None:
        self.apply_button = QPushButton("Применить выбранный")
        self.apply_button.setToolTip(
            "Заменит все настройки робота значениями шаблона. Сначала покажет "
            "список изменений."
        )
        self.apply_button.clicked.connect(self.apply_selected)

        self.save_button = QPushButton("Сохранить текущие как шаблон…")
        self.save_button.setToolTip(
            "Запомнит настройки, которые стоят в программе сейчас, под именем, "
            "которое вы дадите."
        )
        self.save_button.clicked.connect(self.save_current)

        self.remove_button = QPushButton("Удалить")
        self.remove_button.setToolTip("Уберёт шаблон из списка и из файла.")
        self.remove_button.clicked.connect(self.remove_selected)

        self.import_button = QPushButton("Импортировать…")
        self.import_button.setToolTip(
            "Добавит наборы из файла — из примеров, приехавших с программой, "
            "или из файла, выгруженного на другой машине. Ваши шаблоны "
            "останутся на месте: импорт добавляет, а не заменяет."
        )
        self.import_button.clicked.connect(self.import_templates)

        self.export_button = QPushButton("Экспортировать…")
        self.export_button.setToolTip(
            "Сохранит выбранный набор отдельным файлом — перенести на другую "
            "машину, отправить на разбор или отложить перед опытом. Из списка "
            "он никуда не денется."
        )
        self.export_button.clicked.connect(self.export_selected)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.buttons.button(QDialogButtonBox.StandardButton.Close).setText("Закрыть")
        self.buttons.rejected.connect(self.reject)

    def _lay_out(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(self.hint)
        layout.addWidget(self.trouble)
        layout.addWidget(self.table, 3)
        layout.addWidget(self.tabs, 2)
        row = QHBoxLayout()
        row.addWidget(self.apply_button)
        row.addWidget(self.save_button)
        row.addWidget(self.remove_button)
        row.addStretch(1)
        row.addWidget(self.import_button)
        row.addWidget(self.export_button)
        layout.addLayout(row)
        layout.addWidget(self.where)
        layout.addWidget(self.buttons)

    def _set_tab_order(self) -> None:
        """Обход по Tab сверху вниз: список → вкладки → кнопки → «Закрыть»."""
        order: list[QWidget] = [
            self.table,
            self.tabs,
            self.apply_button,
            self.save_button,
            self.remove_button,
            self.import_button,
            self.export_button,
            self.buttons,
        ]
        for previous, following in zip(order, order[1:], strict=False):
            QWidget.setTabOrder(previous, following)

    # ----------------------------------------------------------------- показ

    def reload(self) -> None:
        """Перечитать библиотеку и статистику. Зовётся при открытии и правках."""
        loaded = self._library.read()
        self._templates = loaded.templates
        troubles = list(loaded.troubles)
        with busy_cursor():
            self._stats = self._read_stats([one.values for one in self._templates])
        if self._stats.trouble:
            troubles.append(self._stats.trouble)
        self._say(troubles)
        self._fill_table()

    def _read_stats(self, sets: Sequence[Settings]) -> RunStats:
        """Статистика наборов. Без торговой части — с объяснением, а не молча."""
        door = backend.current()
        if door is None:
            return RunStats(
                tuple(() for _ in sets),
                "Программа собрана без торговой части: прогоны прочитать нечем. "
                "Шаблоны показаны, статистики у них нет.",
            )
        return door.runs(sets)

    def _say(self, troubles: Sequence[str]) -> None:
        """Строка тревоги над списком. Пусто — строки нет вовсе."""
        self.trouble.setVisible(bool(troubles))
        if not troubles:
            self.trouble.setText("")
            return
        self.trouble.setText("\n".join(troubles))
        self.trouble.setStyleSheet(
            f"color: {current_theme().danger}; font-weight: bold;"
        )

    def _fill_table(self) -> None:
        """Заполнить список шаблонов. Одна строка — один набор."""
        self.table.setRowCount(len(self._templates))
        for row, template in enumerate(self._templates):
            runs = self._runs_of(row)
            for column, text in enumerate(_row_of(template, runs)):
                item = QTableWidgetItem(text)
                item.setToolTip(_row_tip(template, runs))
                self.table.setItem(row, column, item)
            self._paint_profit(row, runs)
        if self._templates:
            self.table.selectRow(0)
        self._on_selected()

    def _paint_profit(self, row: int, runs: tuple[TemplateRun, ...]) -> None:
        """Прибыль зелёным, убыток красным. Прочерк — цветом обычного текста.

        Цвет — не украшение: прибыль и убыток человек различает первым
        взглядом, до чтения цифры. Прочерк красить нельзя ни в один из двух
        цветов: он не результат.
        """
        item = self.table.item(row, PROFIT_COLUMN)
        if item is None or not runs or runs[0].profit is None:
            return
        theme = current_theme()
        item.setForeground(
            QColor(theme.success if runs[0].profit >= 0 else theme.danger)
        )

    def _runs_of(self, row: int) -> tuple[TemplateRun, ...]:
        """Прогоны шаблона по его номеру в списке. Нет статистики — пусто."""
        if row < 0 or row >= len(self._stats.runs):
            return ()
        return self._stats.runs[row]

    def _on_selected(self) -> None:
        """Показать прогоны и содержимое выбранного набора."""
        row = self.table.currentRow()
        template = self.selected()
        self.remove_button.setEnabled(template is not None and not template.builtin)
        self.apply_button.setEnabled(template is not None)
        self.export_button.setEnabled(template is not None)
        self._fill_runs(self._runs_of(row))
        self.snapshot.setPlainText(
            _snapshot_text(template) if template is not None else ""
        )

    def _fill_runs(self, runs: tuple[TemplateRun, ...]) -> None:
        """Все прогоны набора, свежие первыми. Суммы по ним не подводятся.

        ⚠️ Итоговой строки здесь нет намеренно. Прогоны сделаны на разных
        отрезках, и сложить их деньги значило бы показать число, которого
        не было ни в одном прогоне.
        """
        self.runs.setRowCount(len(runs))
        theme = current_theme()
        for row, run in enumerate(runs):
            cells = (
                fmt_datetime(run.started_at),
                run.origin,
                run.period or EMPTY,
                str(run.trades) if run.trades is not None else EMPTY,
                fmt_money(run.profit, sign=True) if run.profit is not None else EMPTY,
            )
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(run.note or "Итог прогона не записан.")
                if column == RUN_PROFIT_COLUMN and run.profit is not None:
                    item.setForeground(
                        QColor(theme.success if run.profit >= 0 else theme.danger)
                    )
                self.runs.setItem(row, column, item)

    # ------------------------------------------------------------- поведение

    def templates(self) -> tuple[Template, ...]:
        """Что сейчас показано в списке, включая встроенный набор."""
        return self._templates

    def selected(self) -> Template | None:
        """Выбранный шаблон. `None` — ничего не выбрано."""
        row = self.table.currentRow()
        if row < 0 or row >= len(self._templates):
            return None
        return self._templates[row]

    def apply_selected(self) -> None:
        """Применить выбранный набор целиком — после подтверждения.

        ⚠️ Через то же подтверждение, что и правка настроек руками. Иначе
        шаблон стал бы способом поменять двадцать значений вслепую — ровно
        то, от чего владелец счёта и просил защиты.
        """
        template = self.selected()
        if template is None:
            return
        if confirm_changes(
            self, self._current, template.values, lead=_apply_lead(template)
        ):
            self._current = template.values
            self.applied.emit(template.values)
            self.accept()

    def save_current(self) -> None:
        """Сохранить настройки, которые стоят сейчас, под новым именем.

        Значения показываются до сохранения — «сохранить текущие **со
        значениями**» из просьбы владельца счёта: он видит, что именно
        запоминает, а не полагается на память.
        """
        name, chosen = QInputDialog.getText(
            self,
            "Сохранить текущие настройки",
            "Название набора:\n\n" + _snippet(self._current),
        )
        if not chosen:
            return
        name = name.strip()
        if not name:
            self._complain("У шаблона должно быть название — по нему вы его найдёте.")
            return
        if name == self._library_builtin_name():
            self._complain(
                f"Имя «{name}» занято встроенным набором — умолчаниями "
                "программы. Выберите другое."
            )
            return
        kept = [one for one in self._templates if not one.builtin]
        if any(one.name == name for one in kept):
            if not self._agreed(
                f"Шаблон «{name}» уже есть. Заменить его настройками, "
                "которые стоят сейчас? Прежние значения этого шаблона "
                "восстановить будет нечем."
            ):
                return
            kept = [one for one in kept if one.name != name]
        fresh = Template(
            name=name, values=self._current, saved_at=datetime.now().astimezone()
        )
        self._write((*kept, fresh))

    def remove_selected(self) -> None:
        """Удалить выбранный шаблон. Встроенный не удаляется."""
        template = self.selected()
        if template is None or template.builtin:
            return
        if not self._agreed(
            f"Удалить шаблон «{template.name}»? Восстановить его будет нечем."
        ):
            return
        self._write(tuple(
            one for one in self._templates
            if not one.builtin and one.name != template.name
        ))

    # ----------------------------------------------------- обмен наборами

    def import_templates(self) -> None:
        """Добавить наборы из файла. **Добавить**, а не заменить.

        ⚠️ Ни один набор владельца счёта не исчезает без его прямого ответа:
        сложение делает `ui.templates.merge_templates`, и на каждое совпавшее
        имя оно задаёт вопрос. Список, стёртый импортом, однажды унёс бы
        единственный набор, который работал.
        """
        window = ImportDialog(self._examples, self)
        if window.exec() != QDialog.DialogCode.Accepted:
            return
        incoming = window.chosen()
        if not incoming:
            return
        merged = merge_templates(self._templates, incoming, self.resolve_clash)
        if not merged.added and not merged.replaced:
            self._told(merged)
            return
        trouble = self._library.write(merged.templates)
        if trouble:
            self._complain(trouble)
            return
        self.reload()
        self._told(merged)

    def clash_box(self, template: Template) -> QMessageBox:
        """Вопрос о совпавшем имени — готовым окном, но ещё не показанным.

        Умолчанием стоит «оставить оба»: это единственный из трёх исходов,
        при котором ничего не теряется. «Заменить» уносит прежние значения
        без возврата, «пропустить» — импортируемый набор. Кнопка по умолчанию
        нажимается клавишей Enter, и ставить туда потерю набора нельзя.

        Собрано отдельным методом, чтобы содержимое вопроса можно было
        проверить, не показывая окно на экране.
        """
        box = QMessageBox(self)
        box.setWindowTitle("Импорт наборов настроек")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"Набор «{template.name}» у вас уже есть.")
        box.setInformativeText(
            "«Заменить» запишет поверх вашего — прежние значения восстановить "
            "будет нечем.\n«Оставить оба» добавит импортированный рядом, "
            "под соседним именем.\n«Пропустить» оставит ваш и не возьмёт "
            "импортированный."
        )
        box.addButton("Заменить", QMessageBox.ButtonRole.DestructiveRole)
        both = box.addButton("Оставить оба", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Пропустить", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(both)
        return box

    def resolve_clash(self, template: Template) -> NameClash:
        """Спросить и перевести ответ в решение. Молчаливого ответа здесь нет.

        Ответ читается **по роли кнопки**, а не по её тексту: текст меняют
        при правке фразы, роль — нет. Закрытие окна клавишей Esc Qt приравнивает
        к кнопке с ролью отказа, то есть к «пропустить»: свой набор при этом
        остаётся на месте, а не подменяется чужим.
        """
        box = self.clash_box(template)
        box.exec()
        clicked = box.clickedButton()
        role = (
            box.buttonRole(clicked)
            if clicked is not None
            else QMessageBox.ButtonRole.RejectRole
        )
        if role == QMessageBox.ButtonRole.DestructiveRole:
            return NameClash.REPLACE
        if role == QMessageBox.ButtonRole.RejectRole:
            return NameClash.SKIP
        return NameClash.KEEP_BOTH

    def _told(self, merged: Merged) -> None:
        """Что именно изменил импорт — словами и числами."""
        counts = (
            f"Добавлено наборов: {merged.added}. Заменено: {merged.replaced}. "
            f"Пропущено: {merged.skipped}."
        )
        tail = (
            "\n\nУ добавленных наборов прогонов нет: на вашей истории они "
            "ещё не гонялись. В столбцах статистики у них стоит «не "
            "запускался» и прочерк — это не ноль рублей."
            if merged.added
            else ""
        )
        QMessageBox.information(
            self,
            "Импорт наборов настроек",
            counts + ("\n\n" + "\n".join(merged.notes) if merged.notes else "") + tail,
        )

    def export_selected(self) -> None:
        """Выгрузить выбранный набор отдельным файлом.

        Из списка он при этом никуда не девается: выгрузка только читает.
        """
        template = self.selected()
        if template is None:
            self._complain(
                "Выберите в списке набор, который надо выгрузить, — выгружается "
                "один выбранный."
            )
            return
        start = self._library.path.with_name(_file_name_of(template.name))
        chosen, _ = QFileDialog.getSaveFileName(
            self,
            "Куда выгрузить набор настроек",
            str(start),
            "Наборы настроек (*.json)",
        )
        if not chosen:
            return
        path = Path(chosen)
        if path.suffix.lower() != ".json":
            path = path.with_suffix(".json")
        trouble = export_templates(path, (template,))
        if trouble:
            self._complain(trouble)
            return
        QMessageBox.information(
            self,
            "Шаблоны настроек",
            f"Набор «{template.name}» выгружен в {path}.\n\nЭтот файл можно "
            "перенести на другую машину и добавить там кнопкой "
            "«Импортировать…». Из вашего списка набор никуда не делся.",
        )

    def _write(self, templates: tuple[Template, ...]) -> None:
        """Записать библиотеку и перечитать её. Отказ — словами, а не молча."""
        trouble = self._library.write(templates)
        if trouble:
            self._complain(trouble)
            return
        self.reload()

    def _library_builtin_name(self) -> str:
        """Имя встроенного набора — у самой библиотеки, а не строкой здесь."""
        builtin = [one.name for one in self._templates if one.builtin]
        return builtin[0] if builtin else ""

    def _complain(self, text: str) -> None:
        QMessageBox.warning(self, "Шаблоны настроек", text)

    def _agreed(self, question: str) -> bool:
        answer = QMessageBox.question(
            self,
            "Шаблоны настроек",
            question,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes


def _row_of(template: Template, runs: tuple[TemplateRun, ...]) -> tuple[str, ...]:
    """Ячейки строки списка. Порядок — порядок `COLUMNS`.

    ⚠️ Набор без прогонов получает **слова и прочерки**, а не нули: ноль
    в столбце «Прибыль-убыток» читается как «дало ноль рублей».
    """
    last = runs[0] if runs else None
    return (
        template.name,
        fmt_datetime(template.saved_at) if template.saved_at else EMPTY,
        f"{template.values.instrument}, {template.values.timeframe}",
        str(len(runs)) if runs else NEVER_RUN,
        last.period if last and last.period else EMPTY,
        str(last.trades) if last and last.trades is not None else EMPTY,
        fmt_money(last.profit, sign=True) if last and last.profit is not None else EMPTY,
        template.origin or EMPTY,
    )


def _file_name_of(name: str) -> str:
    """Имя файла для выгрузки набора: имя шаблона плюс `.json`.

    Знаки, запрещённые в именах файлов Windows и Linux, заменяются дефисом —
    иначе диалог сохранения молча откажется писать. Кириллица остаётся:
    это файл владельца счёта на его диске, который он сам называет, а не путь
    внутри программы (правило 6 CLAUDE.md про пути проекта, не про его данные).
    """
    safe = "".join(
        "-" if one in FORBIDDEN_IN_FILE_NAMES or one < " " else one for one in name
    )
    safe = safe.strip(" .")
    return f"{safe or 'набор-настроек'}.json"


def _row_tip(template: Template, runs: tuple[TemplateRun, ...]) -> str:
    """Подсказка строки: чего у набора нет и почему в ячейках прочерки."""
    parts: list[str] = []
    if template.builtin:
        parts.append(
            "Умолчания программы. За ними стоят замеры и сверка с прототипом; "
            "за вашими наборами — то, что вы проверили сами."
        )
    if template.origin:
        parts.append(
            f"Отобран: {template.origin}. Это не совет: набор, отобранный "
            "на прошлом, на другом отрезке ведёт себя иначе."
        )
    if not runs:
        parts.append(
            "Этим набором ещё ни разу не гоняли — цифр по нему нет. Прочерк "
            "здесь означает «прогонов не было», а не «ноль рублей»."
        )
    else:
        parts.append(
            f"Прогонов: {len(runs)}. В строке — последний; остальные видны "
            "во вкладке ниже. Разные отрезки не складываются."
        )
    if template.missing:
        parts.append(
            "В шаблоне нет настроек, появившихся позже: "
            + ", ".join(template.missing)
            + ". Они возьмутся умолчанием программы."
        )
    if template.unknown:
        parts.append(
            "В шаблоне есть настройки, которых эта сборка не знает: "
            + ", ".join(template.unknown)
            + ". Они сохранены в файле, но применены не будут."
        )
    return "\n".join(parts)


def _apply_lead(template: Template) -> str:
    """Первая строка подтверждения при применении шаблона.

    Оговорки про неполный шаблон стоят **до** согласия, а не после: человек
    обязан знать, что берёт, прежде чем нажать «Применить».
    """
    lead = f"Шаблон «{template.name}» заменит настройки робота целиком."
    if template.missing:
        lead += (
            " ⚠️ В шаблоне не было настроек: "
            + ", ".join(template.missing)
            + " — они возьмутся умолчанием программы."
        )
    if template.unknown:
        lead += (
            " ⚠️ В шаблоне есть настройки, которых эта сборка не знает: "
            + ", ".join(template.unknown)
            + " — они не применятся."
        )
    return lead


def _snapshot_text(template: Template) -> str:
    """Что в наборе — тем же текстом, что уходит в журнал прогона."""
    door = backend.current()
    if door is None:
        return (
            "Программа собрана без торговой части: показать настройки набора "
            "словами нечем."
        )
    try:
        return door.snapshot(template.values)
    except Exception as error:  # noqa: BLE001 — фраза человеку важнее типа
        return (
            f"Настройки этого набора показать не удалось: {error}\n\n"
            "Применять его нельзя — сохраните шаблон заново."
        )


def _snippet(values: Settings) -> str:
    """Короткая выжимка текущих настроек для окна сохранения.

    Не весь снимок: в поле ввода имени сорок строк не помещаются. Здесь
    названо то, по чему человек узнаёт свой набор, — инструмент, свеча,
    средняя, тейк, объём. Полный снимок виден во вкладке «Что в наборе»
    сразу после сохранения.
    """
    take = (
        f"{values.take_profit_pct:g}%".replace(".", ",")
        if values.take_profit_enabled
        else "выключен"
    )
    return (
        f"Сейчас: {values.instrument}, {values.timeframe}, "
        f"средняя {values.average_period}, тейк {take}, "
        f"объём {values.volume}, "
        f"окно {values.window_start:%H:%M}–{values.window_end:%H:%M} МСК."
    )
