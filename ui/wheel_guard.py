"""Колесо мыши не меняет значения полей, пока на них нет фокуса.

Зачем
-----
Просьба владельца счёта 05.09.2026, из личного опыта: «пока я скроллю мышкой,
я могу случайно поменять значение и не заметить — уже было такое».

Это классическая ловушка Qt, и она не косметическая. `QSpinBox`,
`QDoubleSpinBox`, `QTimeEdit` и `QComboBox` ловят колесо мыши **без фокуса**:
курсор проехал над полем во время прокрутки длинного окна — значение
изменилось. Молча. В окне настроек под курсором лежат объём сделки, потолок
объёма, дневной лимит убытка, цель прибыли и порог фильтра: проехал колесом
над «Целью прибыли» — поменял 0,50 на 0,55 и не заметил.

Как чинится
-----------
Двумя движениями, и оба нужны.

**Первое.** Полю ставится `StrongFocus` вместо умолчания `WheelFocus`: иначе
колесо само отдаёт полю фокус, и защита снимается тем самым движением, от
которого защищает.

**Второе.** Событие колеса без фокуса не доходит до поля, а **передаётся
области прокрутки**. Просто съесть его нельзя: тогда окно перестанет
прокручиваться, когда курсор оказался над полем, — а полей в настройках
больше двадцати, и половина окна стала бы «мёртвой» для колеса.

⚠️ **Ввод с клавиатуры не трогается.** Стрелки вверх-вниз на поле с фокусом
меняют значение, как и раньше: это нормальный способ ввода, и ломать его
нельзя. Фильтр смотрит **только** на события колеса.

Почему фильтр событий, а не правка каждого поля
-----------------------------------------------
Полей больше двадцати, и следующее добавят без этой защиты — так и появляются
дыры «во всех полях кроме одного». Фильтр ставится обходом
(`QWidget.findChildren`) на все поля разом, и новое поле накрывается тем же
вызовом. Что не осталось ни одного незащищённого поля, стережёт тест.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QWidget,
)

__all__ = ["GUARDED_TYPES", "WheelGuard", "guard_wheel"]

#: Поля, которые крутятся колесом и потому обязаны быть под защитой.
#:
#: `QAbstractSpinBox` покрывает и `QSpinBox`, и `QDoubleSpinBox`, и `QTimeEdit`
#: с `QDateTimeEdit` — все они наследники, и перечислять их поимённо значило бы
#: забыть один. `QComboBox` крутится колесом сам по себе.
GUARDED_TYPES: tuple[type[QWidget], ...] = (QAbstractSpinBox, QComboBox)


class WheelGuard(QObject):
    """Фильтр событий: колесо без фокуса уходит прокрутке, а не полю."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # имя задано Qt
        """Колесо на поле без фокуса — не его дело.

        Возвращает `True` (событие обработано) только для колеса без фокуса.
        Всё остальное — клавиши, щелчки, колесо на поле с фокусом — проходит
        насквозь.
        """
        if event.type() is not QEvent.Type.Wheel:
            return False
        widget = watched if isinstance(watched, QWidget) else None
        if widget is None or widget.hasFocus():
            return False
        area = _scroll_area(widget)
        if area is not None:
            # Прокрутка окна обязана продолжаться: курсор над полем не должен
            # превращать половину окна в мёртвую зону для колеса.
            QApplication.sendEvent(area.viewport(), event)
        return True


def _scroll_area(widget: QWidget) -> QAbstractScrollArea | None:
    """Ближайшая область прокрутки над полем. `None` — прокручивать нечего."""
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QAbstractScrollArea):
            return parent
        parent = parent.parentWidget()
    return None


def guard_wheel(root: QWidget) -> tuple[QWidget, ...]:
    """Накрыть защитой все крутящиеся поля внутри окна. Возвращает накрытые.

    Зовётся **после** сборки полей: `findChildren` видит то, что уже создано.
    Поле, добавленное позже, останется незащищённым — и это ловит тест,
    а не память разработчика.
    """
    guard = root.findChild(WheelGuard) or WheelGuard(root)
    guarded: list[QWidget] = []
    for kind in GUARDED_TYPES:
        for field in root.findChildren(kind):
            # ⚠️ `WheelFocus` (умолчание спинбоксов) отдаёт полю фокус самим
            # колесом — то есть первым же щелчком колеса защита снималась бы.
            field.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            field.installEventFilter(guard)
            guarded.append(field)
    return tuple(guarded)
