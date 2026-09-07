"""Подтверждение «было → стало» перед тем, как настройки уйдут роботу.

Просьба владельца счёта 05.09.2026, из личного опыта: «перед сохранением
настроек выводить "было и стало" с подтверждением. Потому что пока я скроллю
мышкой, я могу случайно поменять значение и не заметить — уже было такое».

Это половина лечения, и вторая половина важнее: колесо мыши вообще не должно
менять значения полей, пока на них нет фокуса (`ui/wheel_guard.py`).
Подтверждение без неё лечит симптом — человек всё равно крутанул бы объём
сделки, только теперь увидел бы это в списке.

Два правила, без которых окно превращается в кнопку «Да»
--------------------------------------------------------
**Показываются только изменённые настройки.** Список всех сорока полей человек
перестанет читать на третий раз, и подтверждение станет украшением.

**Ничего не изменилось — не спрашивать вовсе.** Диалог на пустом месте
раздражает и обесценивает настоящий.

**Изменения по деньгам идут первыми, цветом и отдельным заголовком.** Объём
сделки, потолок объёма, дневной лимит убытка, запас средств и комиссия
читаются не так, как период средней. Что считать деньгами, решает
`app/convert.py::MONEY_FIELDS`, а не это окно.

Откуда берётся перечень
-----------------------
Из `app/convert.py::settings_diff` — то есть из тех же строк, что уходят
в журнал решений (`EngineSettings.changes_from` и соседи). Второе описание
тех же изменений разошлось бы с первым молча; в этом проекте такое уже
ловили. Окно перечня не строит и торговых правил не знает.

⚠️ **Створки может не быть** (окно без торговой части). Тогда подтверждение
всё равно показывается — но честно говорит, что перечислить изменения нечем.
Молча применить в этом случае значило бы обойти ровно ту защиту, ради которой
всё сделано.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ui import backend
from ui.backend import SettingsDiff
from ui.models import Settings
from ui.theme import current as current_theme

__all__ = ["ChangesDialog", "confirm_changes", "diff_of"]


def diff_of(previous: Settings, now: Settings) -> SettingsDiff:
    """Перечень изменений. Без торговой части — с объяснением вместо списка."""
    door = backend.current()
    if door is None:
        return SettingsDiff(
            trouble=(
                "Программа собрана без торговой части, и перечислить изменения "
                "нечем. Проверьте значения в окне настроек глазами."
            )
        )
    return door.changes(previous, now)


def confirm_changes(
    parent: QWidget | None,
    previous: Settings,
    now: Settings,
    *,
    lead: str = "",
) -> bool:
    """Спросить подтверждение. `True` — применять.

    Изменений нет — вопроса нет и ответ сразу `True`: применение набора,
    равного текущему, ничего не меняет и подтверждения не стоит.

    :param lead: первая строка окна. У настроек и у шаблона она разная:
        в одном случае человек правил поля сам, в другом — берёт чужой набор
        целиком и обязан увидеть, что тот ему даёт.
    """
    diff = diff_of(previous, now)
    if diff.empty:
        return True
    dialog = ChangesDialog(diff, parent, lead=lead)
    try:
        return dialog.exec() == QDialog.DialogCode.Accepted
    finally:
        # Та же причина, что у окна настроек: с родителем-окном диалог пережил
        # бы закрытие и остался в памяти вместе со своими надписями.
        dialog.deleteLater()


class ChangesDialog(QDialog):
    """Список «было → стало» с двумя кнопками: применить или вернуться."""

    def __init__(
        self,
        diff: SettingsDiff,
        parent: QWidget | None = None,
        *,
        lead: str = "",
    ) -> None:
        super().__init__(parent)
        self.diff = diff
        self.setWindowTitle("Проверьте изменения")
        self.setModal(True)
        theme = current_theme()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        head = QLabel(lead or _lead(diff))
        head.setWordWrap(True)
        layout.addWidget(head)

        if diff.trouble:
            # Отказ разбора — крупно и цветом тревоги: пустой список ниже
            # означает не «ничего не меняется», а «перечислить не удалось».
            trouble = QLabel(diff.trouble)
            trouble.setWordWrap(True)
            trouble.setStyleSheet(f"color: {theme.danger}; font-weight: bold;")
            layout.addWidget(trouble)

        if diff.money:
            layout.addWidget(_caption("За этим стоят деньги на счёте:", theme.danger))
            self.money = _lines(diff.money, theme.danger, bold=True)
            layout.addWidget(self.money)

        if diff.other:
            layout.addWidget(_caption("Остальные изменения:", theme.text_dim))
            self.other = _lines(diff.other, theme.text)
            layout.addWidget(self.other)

        layout.addStretch(1)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.apply_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.apply_button.setText("Применить")
        self.cancel_button = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.cancel_button.setText("Не применять")
        self.cancel_button.setToolTip(
            "Настройки останутся прежними целиком. Ничего не применяется "
            "наполовину: вернётесь в окно и поправите то, что не собирались "
            "менять."
        )
        # ⚠️ Кнопка по умолчанию — «Не применять». Enter, нажатый не глядя,
        # обязан оставить всё как было: подтверждение заведено ровно затем,
        # что человек мог не заметить изменения.
        self.apply_button.setDefault(False)
        self.apply_button.setAutoDefault(False)
        self.cancel_button.setDefault(True)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.resize(560, min(680, 220 + 26 * diff.count))


def _lead(diff: SettingsDiff) -> str:
    """Первая строка: сколько всего меняется и на что смотреть в первую очередь."""
    if diff.money:
        return (
            f"Изменений: {diff.count}, из них по деньгам: {len(diff.money)}. "
            "Проверьте верхний список — это объём, ограничения и комиссия."
        )
    return f"Изменений: {diff.count}. Проверьте их и подтвердите."


def _caption(text: str, colour: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(f"color: {colour}; font-weight: bold;")
    return label


def _lines(lines: tuple[str, ...], colour: str, *, bold: bool = False) -> QLabel:
    """Строки перечня как есть, без разбора на «было» и «стало».

    Разбирать готовую фразу на части значило бы завести третье знание о том,
    как она устроена: строку пишет тот, кто знает поле, — движок, торговый
    модуль или окно, — и стрелка внутри неё их дело, а не наше.
    """
    label = QLabel("\n".join(lines))
    label.setWordWrap(True)
    # Текст выделяется мышкой: строку про деньги человек копирует в письмо
    # или в записку себе, и перенабирать её руками — лишний повод ошибиться.
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    weight = "bold" if bold else "normal"
    label.setStyleSheet(f"color: {colour}; font-weight: {weight};")
    return label
