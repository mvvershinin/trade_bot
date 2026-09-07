"""Окно «Импорт наборов настроек»: что именно взять и откуда.

Решение 0051. Импорт нужен сам по себе — перенести набор на другую машину,
отправить на разбор, сохранить перед экспериментом, — и он же служит дорогой
для примеров, приехавших с программой.

Три вещи, которые это окно обязано сказать до нажатия «Импортировать»
---------------------------------------------------------------------
**1. Импорт добавляет, а не заменяет.** Человек, у которого в списке лежит
единственный работающий набор, обязан прочитать это раньше, чем нажмёт.
Сама защита стоит не здесь, а в `ui.templates.merge_templates`; здесь стоит
обещание, и оно совпадает с поведением.

**2. Импортированный набор приходит без прогонов.** Цифр по нему не будет,
пока его не прогонят на **этой** истории. Пустая клетка без этих слов читается
как ноль, а «ноль рублей» — другое утверждение, чем «не запускался».

**3. Пример без происхождения — это дефект поставки, а не мелочь.** Набор,
про который не сказано, на каком отрезке и на каком инструменте он отобран,
читается как рекомендация. Рекомендацией он не является: подбор на прошлом
дважды проиграл бездействию. Такой пример показывается красным и с оговоркой,
а не молча.

⚠️ Чтение файлов **синхронное**. Это несколько десятков килобайт с местного
диска в окне, которое открывают раз в месяц; заводить ради него поток значило
бы платить сложностью за то, чего человек не заметит. Всё, что дольше,
в этом слое запрещено (`ui/background.py`).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ui.templates import Template, example_files, examples_dir, read_for_import
from ui.theme import current as current_theme

__all__ = ["NO_ORIGIN", "ImportDialog"]

#: Столбцы списка: подпись и подсказка.
COLUMNS: tuple[tuple[str, str], ...] = (
    ("Брать", "Отметьте наборы, которые хотите добавить к своим."),
    ("Набор", "Название, под которым набор появится в вашем списке."),
    (
        "Откуда взят",
        "На каком отрезке и на каком инструменте этот набор отобран. "
        "Набор без этой строки нельзя считать советом: на другом отрезке "
        "он ведёт себя иначе.",
    ),
    ("Инструмент, свеча", "Инструмент и размер свечи из самого набора."),
    (
        "Оговорки",
        "Чего в наборе не хватает и что в нём эта сборка не понимает.",
    ),
)

#: Что стоит в столбце происхождения, когда его нет. Слова, а не пустая клетка:
#: пустая читается как «здесь нечего писать», а написать было что.
NO_ORIGIN = "не сказано, на чём отобран"

#: Столбец с галочкой. Берётся из таблицы, а не числом.
PICK_COLUMN: int = 0
ORIGIN_COLUMN: int = 2


class ImportDialog(QDialog):
    """Выбор источника и наборов для импорта. Ничего не записывает.

    Окно только читает и отдаёт выбранное (`chosen`). Складывать выбранное
    со списком владельца счёта — дело того, кто это окно открыл: там же
    задаётся вопрос про совпавшие имена.
    """

    def __init__(self, examples: Path | None = None, parent: QWidget | None = None) -> None:
        """:param examples: папка примеров. `None` — та, что приехала с программой."""
        super().__init__(parent)
        self.setWindowTitle("Импорт наборов настроек")
        self.setModal(True)
        self.resize(900, 560)

        self._examples_dir = examples if examples is not None else examples_dir()
        self._files: tuple[Path, ...] = example_files(self._examples_dir)
        self._chosen_file: Path | None = None
        self._found: tuple[Template, ...] = ()

        self._build_widgets()
        self._lay_out()
        self._set_tab_order()
        self._start_source()

    # ---------------------------------------------------------------- сборка

    def _build_widgets(self) -> None:
        self._build_notes()
        self._build_source()
        self._build_table()
        self._build_buttons()

    def _build_notes(self) -> None:
        self.hint = QLabel(
            "Импорт ДОБАВЛЯЕТ наборы к вашему списку. Ваши шаблоны остаются "
            "на месте: ни один из них импорт не стирает. Если имя совпадёт "
            "с вашим — программа спросит, что делать."
        )
        self.hint.setWordWrap(True)

        self.note = QLabel(
            "Импортированный набор приходит без прогонов: в списке у него "
            "будет написано «не запускался», а в деньгах — прочерк. Это "
            "правда — на вашей истории он ещё не гонялся. Прочерк означает "
            "«прогонов не было», а не «ноль рублей»."
        )
        self.note.setWordWrap(True)
        self.note.setStyleSheet("font-weight: bold;")

        self.trouble = QLabel()
        self.trouble.setWordWrap(True)
        self.trouble.setVisible(False)

    def _build_source(self) -> None:
        self.from_examples = QRadioButton("Из примеров, поставляемых с программой")
        self.from_examples.setToolTip(
            "Готовые наборы, приехавшие вместе с программой. Ваш файл шаблонов "
            "они не трогают, а при обновлении программы обновляются сами."
        )
        self.from_examples.setEnabled(bool(self._files))
        if not self._files:
            self.from_examples.setToolTip(
                f"Примеров нет: папка {self._examples_dir} пуста или удалена. "
                "Программа без неё работает — импортировать можно из своего файла."
            )
        self.from_examples.toggled.connect(self._on_source)

        self.from_file = QRadioButton("Из своего файла на диске")
        self.from_file.setToolTip(
            "Файл, выгруженный кнопкой «Экспортировать…» — здесь или на другой "
            "машине."
        )

        self.example_box = QComboBox()
        self.example_box.setToolTip("Какой файл примеров смотреть.")
        for one in self._files:
            self.example_box.addItem(one.name, one)
        self.example_box.currentIndexChanged.connect(lambda _: self.refresh())

        self.pick_button = QPushButton("Выбрать файл…")
        self.pick_button.setToolTip("Открыть файл наборов настроек с диска.")
        self.pick_button.clicked.connect(self.pick_file)

        self.where = QLabel()
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
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )

    def _build_buttons(self) -> None:
        self.mark_all = QPushButton("Отметить все")
        self.mark_all.clicked.connect(lambda: self.mark(Qt.CheckState.Checked))
        self.mark_none = QPushButton("Снять все")
        self.mark_none.clicked.connect(lambda: self.mark(Qt.CheckState.Unchecked))

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Импортировать отмеченные"
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)

    def _lay_out(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(self.hint)
        layout.addWidget(self.from_examples)
        picked = QHBoxLayout()
        picked.addWidget(self.example_box, 1)
        layout.addLayout(picked)
        layout.addWidget(self.from_file)
        chooser = QHBoxLayout()
        chooser.addWidget(self.pick_button)
        chooser.addStretch(1)
        layout.addLayout(chooser)
        layout.addWidget(self.where)
        layout.addWidget(self.trouble)
        layout.addWidget(self.table, 1)
        marks = QHBoxLayout()
        marks.addWidget(self.mark_all)
        marks.addWidget(self.mark_none)
        marks.addStretch(1)
        layout.addLayout(marks)
        layout.addWidget(self.note)
        layout.addWidget(self.buttons)

    def _set_tab_order(self) -> None:
        """Обход по Tab сверху вниз, как читается окно."""
        order: list[QWidget] = [
            self.from_examples,
            self.example_box,
            self.from_file,
            self.pick_button,
            self.table,
            self.mark_all,
            self.mark_none,
            self.buttons,
        ]
        for previous, following in zip(order, order[1:], strict=False):
            QWidget.setTabOrder(previous, following)

    def _start_source(self) -> None:
        """С чего окно открывается: с примеров, если они есть."""
        if self._files:
            self.from_examples.setChecked(True)
        else:
            self.from_file.setChecked(True)
        self._on_source()

    # -------------------------------------------------------------- источник

    def _on_source(self) -> None:
        """Переключение источника: показать нужные поля и перечитать."""
        examples = self.from_examples.isChecked()
        self.example_box.setEnabled(examples)
        self.pick_button.setEnabled(not examples)
        self.refresh()

    def from_examples_now(self) -> bool:
        """Смотрим ли мы сейчас на примеры из поставки."""
        return bool(self.from_examples.isChecked())

    def source_path(self) -> Path | None:
        """Файл, который сейчас читается. `None` — не выбран."""
        if self.from_examples_now():
            chosen = self.example_box.currentData()
            return chosen if isinstance(chosen, Path) else None
        return self._chosen_file

    def pick_file(self) -> None:
        """Спросить файл на диске и перечитать список."""
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "Файл с наборами настроек",
            str(self._chosen_file or Path.home()),
            "Наборы настроек (*.json);;Все файлы (*)",
        )
        if not chosen:
            return
        self.use_file(Path(chosen))

    def use_file(self, path: Path) -> None:
        """Смотреть на этот файл с диска. Источник переключается сам."""
        self._chosen_file = path
        self.from_file.setChecked(True)
        self.refresh()

    # ------------------------------------------------------------------ показ

    def refresh(self) -> None:
        """Перечитать выбранный файл и показать, что в нём есть."""
        path = self.source_path()
        if path is None:
            self._found = ()
            self.where.setText("Файл не выбран.")
            self._say(())
            self._fill()
            return
        loaded = read_for_import(path)
        self._found = loaded.templates
        self.where.setText(f"Файл: {path}")
        self._say((*loaded.troubles, *self._origin_troubles(loaded.templates)))
        self._fill()

    def _origin_troubles(self, found: tuple[Template, ...]) -> tuple[str, ...]:
        """Примеры без происхождения — вслух. Свой файл этим не попрекают.

        Владелец счёта сохранил свой набор сам и знает, откуда тот взялся.
        Пример же приехал с программой, и без строки «на чём отобран» он
        читается как рекомендация — решение 0051, следствие 5.
        """
        if not self.from_examples_now():
            return ()
        mute = [one.name for one in found if not one.origin]
        if not mute:
            return ()
        return (
            "⚠️ У примеров не сказано, на каком отрезке и на каком инструменте "
            "они отобраны: " + ", ".join(f"«{name}»" for name in mute) + ". "
            "Считать их советом нельзя: набор, отобранный на прошлом, "
            "на другом отрезке ведёт себя иначе.",
        )

    def _say(self, troubles: tuple[str, ...]) -> None:
        """Строка тревоги. Пусто — строки нет вовсе."""
        self.trouble.setVisible(bool(troubles))
        self.trouble.setText("\n".join(troubles))
        if troubles:
            self.trouble.setStyleSheet(
                f"color: {current_theme().danger}; font-weight: bold;"
            )

    def _fill(self) -> None:
        """Заполнить список найденного. Всё отмечено — отменяется галочкой."""
        self.table.setRowCount(len(self._found))
        for row, one in enumerate(self._found):
            self._fill_row(row, one)
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setEnabled(bool(self._found))
        for button in (self.mark_all, self.mark_none):
            button.setEnabled(bool(self._found))

    def _fill_row(self, row: int, one: Template) -> None:
        """Одна строка списка: галочка, имя, происхождение, оговорки."""
        pick = QTableWidgetItem("")
        pick.setFlags(pick.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        pick.setCheckState(Qt.CheckState.Checked)
        pick.setToolTip("Снимите галочку, чтобы не брать этот набор.")
        self.table.setItem(row, PICK_COLUMN, pick)
        cells = (
            one.name,
            one.origin or NO_ORIGIN,
            f"{one.values.instrument}, {one.values.timeframe}",
            _reservations(one),
        )
        for shift, text in enumerate(cells, start=1):
            item = QTableWidgetItem(text)
            item.setToolTip(_row_tip(one))
            self.table.setItem(row, shift, item)
        if not one.origin:
            cell = self.table.item(row, ORIGIN_COLUMN)
            if cell is not None:
                cell.setForeground(QColor(current_theme().danger))

    def mark(self, state: Qt.CheckState) -> None:
        """Поставить или снять галочки у всех строк разом."""
        for row in range(self.table.rowCount()):
            item = self.table.item(row, PICK_COLUMN)
            if item is not None:
                item.setCheckState(state)

    # -------------------------------------------------------------- поведение

    def found(self) -> tuple[Template, ...]:
        """Всё, что прочиталось из выбранного файла, — отмечено или нет."""
        return self._found

    def chosen(self) -> tuple[Template, ...]:
        """Отмеченные наборы в порядке файла."""
        picked: list[Template] = []
        for row, one in enumerate(self._found):
            item = self.table.item(row, PICK_COLUMN)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                picked.append(one)
        return tuple(picked)

    def _on_accept(self) -> None:
        """Согласие: пустой выбор — не тихий выход, а объяснение."""
        if not self.chosen():
            QMessageBox.warning(
                self,
                "Импорт наборов настроек",
                "Не отмечено ни одного набора — импортировать нечего. "
                "Поставьте галочки в первом столбце или нажмите «Отмена».",
            )
            return
        self.accept()


def _reservations(one: Template) -> str:
    """Короткая строка оговорок для столбца. Пусто — оговорок нет."""
    parts: list[str] = []
    if one.missing:
        parts.append(f"нет настроек: {len(one.missing)}")
    if one.unknown:
        parts.append(f"незнакомых: {len(one.unknown)}")
    return "; ".join(parts)


def _row_tip(one: Template) -> str:
    """Подсказка строки: происхождение целиком и что с набором не так."""
    parts: list[str] = []
    parts.append(
        f"Отобран: {one.origin}"
        if one.origin
        else (
            "Про этот набор не сказано, на каком отрезке и на каком инструменте "
            "он отобран. Советом его считать нельзя."
        )
    )
    if one.missing:
        parts.append(
            "В наборе нет настроек, появившихся позже: "
            + ", ".join(one.missing)
            + ". Они возьмутся умолчанием программы."
        )
    if one.unknown:
        parts.append(
            "В наборе есть настройки, которых эта сборка не знает: "
            + ", ".join(one.unknown)
            + ". Они сохранятся, но применены не будут."
        )
    parts.append(
        "Прогонов у импортированного набора нет: на вашей истории он ещё "
        "не гонялся."
    )
    return "\n".join(parts)
