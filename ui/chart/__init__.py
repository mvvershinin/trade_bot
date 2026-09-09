"""График: интерфейс отрисовки и её сменные реализации.

Остальная программа знает только `ChartSurface` и `create_surface`. Чем именно
нарисованы свечи — за пределами этого пакета не известно и знать не нужно
(ARCHITECTURE.md §4). Сегодня реализация одна, на средствах самого Qt;
встроенный веб-график удалён 09.09.2026 (решение 0056).
"""

from ui.chart.factory import available_surfaces, create_surface
from ui.chart.protocol import ChartSurface

__all__ = ["ChartSurface", "create_surface", "available_surfaces"]
