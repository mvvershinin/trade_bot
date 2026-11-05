"""Окно настроек по ширине: ни одна вкладка не шире окна.

Что стережётся, словами: на каждой вкладке содержимое помещается в окно
настроек того размера, который оно просит при открытии, — снизу нет
горизонтальной прокрутки, и пояснения групп не обрезаны справа.

Дефект прохода 4.1 Э4 (05.11.2026): галочка «Заходить заново при обрыве
связи» одной строкой «Держать позицию в убытке, если выход по уровню
не исполнился» была шире всего остального на вкладке «Вход и выход»
(482 точки при кегле 10,8) и раздвинула вкладку до 781 точки при окне
в 678 — прокрутка снизу, пояснения всех групп срезаны справа.

Ширину задают элементы, которые не переносятся: подписи строк формы,
галочки, переключатели, списки. Переносимые пояснения ширину не держат —
они подстраиваются. Поэтому проверка смотрит на то, что видит человек:
есть ли у области вкладки горизонтальная прокрутка.

Две пары «кегль — окно», обе с пути человека:

* кегль машины прогона 9 пт ×1,2 и окно, какое получилось на экране
  в 800 точек (на нём шёл проход 4.1): 720 точек, область вкладки 678;
* кегль владельца счёта 10 пт ×1,2 и окно `DEFAULT_SIZE` целиком (его экран
  шире окна): 820 точек, область вкладки 778.

⚠️ Кегль 12 пт в окне 720 точек не проверяется: такой пары у владельца
счёта нет, а вкладка «Деньги» в ней шире области на 56 точек (замер
05.11.2026) — подпись «Наибольшая свеча для проверки по минуткам».
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from PySide6.QtWidgets import QScrollArea

from tests.helpers import settle_qt
from ui.models import Settings
from ui.settings_dialog import DEFAULT_SIZE, SettingsDialog
from ui.theme import scaled_font

#: Кегль системы на машине прохода 4.1 и у владельца счёта. Окно поверх
#: него берёт ×1,2 (`ui.theme.FONT_SCALE`) — как в `app/main.py`.
_RUN_MACHINE_POINTS = 9.0
_OWNER_POINTS = 10.0


@pytest.fixture
def system_font(qapp) -> Iterator[Callable[[float], None]]:
    """Поставить приложению кегль системы ×1,2 и вернуть прежний шрифт после.

    Шрифт общий на процесс: без возврата сосед по воркеру получил бы чужой
    кегль и чужие переносы строк.
    """
    before = qapp.font()

    def apply(points: float) -> None:
        system = qapp.font()
        system.setPointSizeF(points)
        qapp.setFont(scaled_font(system))

    try:
        yield apply
    finally:
        qapp.setFont(before)


def _settle(qapp) -> None:
    """Раскладка доходит до области прокрутки цепочкой отложенных запросов."""
    for _ in range(10):
        qapp.processEvents()
    settle_qt(qapp)


@pytest.mark.parametrize(
    ("points", "width"),
    [(_RUN_MACHINE_POINTS, None), (_OWNER_POINTS, DEFAULT_SIZE[0])],
    ids=["кегль машины, окно по экрану 800", "кегль владельца, окно целиком"],
)
def test_no_settings_tab_is_wider_than_the_window(
    qapp, system_font, points: float, width: int | None
) -> None:
    """Каждая вкладка без горизонтальной прокрутки в окне размера по умолчанию.

    Мутация: вернуть галочке «Заходить заново при обрыве связи» подпись
    одной строкой — падают обе пары, вкладка «Вход и выход».
    """
    system_font(points)
    dialog = SettingsDialog(Settings())
    try:
        if width is not None:
            dialog.resize(width, DEFAULT_SIZE[1])
        dialog.show()
        _settle(qapp)
        assert dialog.width() <= DEFAULT_SIZE[0], (
            f"окно настроек открылось шире {DEFAULT_SIZE[0]} точек — "
            "проверка шла бы не при том размере, который видит человек"
        )
        wide: list[str] = []
        for index in range(dialog.tabs.count()):
            dialog.tabs.setCurrentIndex(index)
            _settle(qapp)
            page = dialog.tabs.widget(index)
            assert isinstance(page, QScrollArea), dialog.tabs.tabText(index)
            body = page.widget()
            assert body is not None, dialog.tabs.tabText(index)
            content = body.minimumSizeHint().width()
            if page.horizontalScrollBar().maximum() > 0 or content > page.viewport().width():
                wide.append(
                    f"«{dialog.tabs.tabText(index)}»: содержимое {content} точек, "
                    f"видно {page.viewport().width()}"
                )
        assert not wide, (
            f"в окне {dialog.width()} точек при кегле {qapp.font().pointSizeF():g} пт "
            "вкладка шире окна — снизу прокрутка, пояснения обрезаны справа: "
            + "; ".join(wide)
        )
    finally:
        dialog.deleteLater()
        settle_qt(qapp)
