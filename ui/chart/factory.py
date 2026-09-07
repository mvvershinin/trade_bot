"""Выбор отрисовщика графика. **Единственное место, где известны их имена.**

Смена библиотеки отрисовки — правка этого файла и добавление одного модуля рядом.
Ни главное окно, ни настройки, ни журналы о выборе не знают: они работают
с `ChartSurface` (ARCHITECTURE.md §4, ТЗ §6 — запасной вариант отрисовки).

Отказ обязан быть **видимым**. Если основной вариант не поднялся, программа берёт
запасной и говорит об этом строкой в журнале и подписью в окне: молча упавший
на запасной путь график выглядит как «работает», а потом выясняется, что заказчик
три недели смотрел не то, что мы проверяли.
"""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

from ui.chart.painter_surface import PainterChartSurface
from ui.chart.protocol import ChartSurface
from ui.chart.web_surface import WebChartSurface

# Порядок предпочтения: сначала основной вариант ТЗ, потом запасной.
ORDER: tuple[type[ChartSurface], ...] = (WebChartSurface, PainterChartSurface)


def available_surfaces() -> list[tuple[str, bool, str]]:
    """Что доступно на этой машине: имя, признак, причина отказа."""
    result = []
    for surface in ORDER:
        ok, reason = surface.is_available()
        result.append((surface.name, ok, reason))
    return result


def create_surface(parent: QWidget | None = None) -> tuple[ChartSurface, str]:
    """Первый доступный отрисовщик и объяснение выбора человеческим языком.

    Объяснение не выбрасывается: главное окно кладёт его в подпись и передаёт
    в журнал решений при старте.
    """
    refusals: list[str] = []
    for surface in ORDER:
        ok, reason = surface.is_available()
        if not ok:
            refusals.append(f"{surface.name}: {reason}")
            continue
        try:
            instance = surface(parent)  # type: ignore[call-arg]
        except Exception as error:  # noqa: BLE001 — падение отрисовщика не должно
            # уносить с собой окно: без графика программа торгует, без окна — нет.
            refusals.append(f"{surface.name}: не запустился ({error})")
            continue
        note = f"График: {surface.name}"
        if refusals:
            note += ". Не подошло — " + "; ".join(refusals)
        return instance, note

    # Сюда попасть нельзя: отрисовка на Qt не требует ничего, кроме PySide6,
    # который и так нужен окну. Если попали — значит сломан сам PySide6.
    raise RuntimeError("Ни один отрисовщик графика не доступен: " + "; ".join(refusals))
