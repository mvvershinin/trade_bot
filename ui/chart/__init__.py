"""График: интерфейс отрисовки и её сменные реализации.

Остальная программа знает только `ChartSurface` и `create_surface`. Чем именно
нарисованы свечи — веб-графиком или средствами Qt — за пределами этого пакета
не известно и знать не нужно (ARCHITECTURE.md §4).
"""

from ui.chart.factory import available_surfaces, create_surface
from ui.chart.protocol import ChartSurface

__all__ = ["ChartSurface", "create_surface", "available_surfaces"]
