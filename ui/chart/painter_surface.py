"""Отрисовка графика средствами самого Qt: свечи, средняя, метки, уровни, затенение.

Реализация `ChartSurface` — и с 09.09.2026 единственная (решение 0056).
Зависимостей, кроме PySide6, у неё нет, и это её главное свойство: она работает
на старом окружении Linux, в сессии без GPU и на машине без экрана (снимок
для отчёта, тесты) — то есть везде, где вообще открылось окно.

Что здесь не считается
----------------------
Ничего торгового. Свечи, точки средней, метки, уровни и затенённые отрезки
приходят готовыми. Модуль умеет переводить время в координату X, цену — в Y,
и рисовать. Любая арифметика сложнее этого — признак, что в окно уехало правило
из `engine/`.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from PySide6.QtCore import QEvent, QLineF, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QHideEvent,
    QMouseEvent,
    QPainter,
    QPen,
    QPolygonF,
    QWheelEvent,
)
from PySide6.QtWidgets import QToolTip, QWidget

from ui.chart.glyphs import GlyphStyle, draw_marker
from ui.chart.protocol import ChartSurface, ExpandHandler, HoverHandler, SpanHandler
from ui.chart.timeline import Timeline
from ui.formatting import fmt_number, to_msk
from ui.models import (
    Candle,
    CandleInfo,
    ChartData,
    ChartSpan,
    ContractSeam,
    Layer,
    LinePoint,
    Marker,
    MarkerKind,
    PriceLevel,
    Shade,
    TradePath,
)
from ui.theme import Theme, current as current_theme, text_on

# Поля вокруг области построения, в логических пикселях. Qt масштабирует их сам
# под HiDPI — умножать на коэффициент вручную не нужно и вредно.
AXIS_WIDTH = 74      # справа: подписи цены
AXIS_HEIGHT = 24     # снизу: подписи времени
PADDING = 10

MIN_VISIBLE = 10     # ближе не подпускаем: свечи превращаются в столбы
DEFAULT_VISIBLE = 90

# Уже этого пропуск на экране не подписывается: надпись не влезает и легла бы
# на соседние свечи. Сам пропуск при этом виден — там пусто.
GAP_CAPTION_WIDTH = 34

# Протяжка короче этого — щелчок, а не выделение. Рука дрожит на любом нажатии,
# и без порога каждый щелчок правой кнопкой давал бы отрезок случайной ширины.
CLICK_SLACK = 5.0

# Протяжка правой кнопкой за край поля свечей двигает вид вслед за мышью
# (просьба владельца счёта, Н4). Числа не торговые — это скорость прокрутки,
# и правило 16 их не касается. Пока мышь стоит за краем, событий движения нет,
# поэтому вид двигает таймер: раз в EDGE_SCROLL_INTERVAL_MS.
#
# Сдвиг за шаг — **доля видимого размаха**, а не число свечей: прежние «не больше
# трёх мест за шаг» на виде в несколько тысяч мест проезжали экран минутами
# («листается со скоростью черепахи», владелец счёта 09.10.2026). Доля растёт
# линейно с выходом мыши за край и упирается в EDGE_SCROLL_MAX_SHARE начиная
# с EDGE_SCROLL_FULL_PIXELS точек. Что это даёт:
#   * 20 точек за краем на 90 видимых — 1,5 места за шаг, 37 свечей в секунду
#     (столько же, сколько было);
#   * 100 точек за краем — экран за 0,48 с при любом приближении,
#     от 120 точек — за 0,4 с.
EDGE_SCROLL_INTERVAL_MS = 40
EDGE_SCROLL_FULL_PIXELS = 120.0
EDGE_SCROLL_MAX_SHARE = 0.1

# Колесо: во сколько раз меняется видимый размах за щелчок (120 единиц).
# От 90 видимых свечей до всего ряда в 16 тысяч мест — 13 щелчков; при прежних
# 0,85 за щелчок выходило 33. Мелкие шаги тачпада дают дробную степень,
# и меньше одного места за событие размах не меняется никогда — иначе
# на малом приближении округление съедало бы шаг целиком.
WHEEL_ZOOM_FACTOR = 1.5

# Уже этого (в точках на место оси) свечи рисуются по столбцам экрана:
# на столбец одна черта от наименьшей цены до наибольшей. Отдельные свечи там
# всё равно сливаются, а рисовать каждую — 15 тысяч прямоугольников на кадр.
DENSE_STEP = 2.0

# Штрих перекрестья: короткий и частый. Отличается от пунктира прогноза
# (штрих вчетверо длиннее) и от границ пропуска — иначе три разных пунктира
# на одном экране читаются как один.
CROSSHAIR_DASH = (2.0, 3.0)


@dataclass(slots=True)
class _Viewport:
    """Что именно видно: индекс правой свечи и сколько свечей влезает.

    `whole` — человек нажал «Весь период» и с тех пор вид руками не двигал.
    Пока он стоит, новые свечи и новый прогон показываются тоже целиком:
    иначе первая же живая свеча молча отрезала бы начало ряда.
    """

    right: float = 0.0
    span: int = DEFAULT_VISIBLE
    follow: bool = True
    whole: bool = False


class PainterChartSurface(QWidget, ChartSurface):
    """График на `QPainter`. Три действия мыши разведены по кнопкам.

    | Что делает человек | Что происходит |
    |---|---|
    | ведёт мышь, ничего не нажимая | перекрестье идёт за курсором; строка сведений показывает свечу под ним; над меткой всплывает подсказка |
    | зажал **левую** и ведёт | панорама графика — принято владельцем счёта, ломать нельзя |
    | **правая**: щелчок или протяжка | выделение отрезка, по нему разворачивается разбор сделок |
    | **правая**, протяжка за край поля | вид едет следом за мышью, выделение растёт |
    | колесо | масштаб; свеча под курсором остаётся под курсором |

    Разведение именно такое, потому что панорама левой кнопкой уже в руках
    у владельца счёта, а перекрестье он попросил «без кликов, просто
    модернизированный курсор». Третьему поведению осталась правая кнопка,
    и меню по ней отключено (`PreventContextMenu`), чтобы оно не всплывало
    поверх графика.
    """

    name = "встроенная отрисовка Qt"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme: Theme = current_theme()
        self._data = ChartData()
        #: Ось: место под каждую свечу и под каждую пропущенную (`B-005`).
        #: Всё, что раньше считалось по номеру свечи, считается по номеру
        #: **места**: у пропущенного часа теперь есть свои места, и он занимает
        #: на экране столько же, сколько занял бы час свечей.
        self._line = Timeline()
        self._average: list[LinePoint] = []
        #: Отметки точек средней и их места на оси — посчитаны один раз,
        #: а не на каждом кадре. Кадр рисуется на каждое движение мыши
        #: (перекрестье), и перевод 15 тысяч точек в места съедал 20 мс
        #: из каждого. Места зависят от оси и сбрасываются вместе с ней
        #: (`_line_changed`), отметки — только с новой средней.
        self._average_stamps: list[float] = []
        self._average_places: list[float] | None = None
        self._average_label = ""
        self._markers: dict[Layer, list[Marker]] = {Layer.PLAN: [], Layer.FACT: []}
        self._paths: dict[Layer, list[TradePath]] = {Layer.PLAN: [], Layer.FACT: []}
        self._visible: dict[Layer, bool] = {Layer.PLAN: True, Layer.FACT: True}
        self._levels: list[PriceLevel] = []
        self._shades: list[Shade] = []
        self._seams: list[ContractSeam] = []
        self._view = _Viewport()
        self._drag_x: float | None = None
        #: Где стоит мышь. `None` — курсор ушёл с виджета, перекрестья нет.
        #: Держится всегда: перекрестье следует за движением и не требует
        #: ни нажатия, ни включения (просьба владельца счёта 05.09.2026).
        self._cursor: QPointF | None = None
        #: Протяжка правой кнопкой: **место оси**, где начали, и где мышь
        #: сейчас, в пикселях. Начало — номер места, а не пиксель: вид едет
        #: под протяжкой за край (Н4), и начало, запомненное пикселем,
        #: уехало бы вместе с ним. `None` — правая кнопка не нажата.
        self._span: tuple[int, float] | None = None
        #: Двигает вид, пока мышь с зажатой правой кнопкой стоит за краем поля.
        self._edge_timer = QTimer(self)
        self._edge_timer.setInterval(EDGE_SCROLL_INTERVAL_MS)
        self._edge_timer.timeout.connect(self._edge_scroll_tick)
        self._hit: list[tuple[QPointF, Marker]] = []
        #: Кому рассказывать про свечу под курсором. Ставит панель графика.
        self._hover_handler: HoverHandler | None = None
        #: Кому рассказывать про выделенный отрезок. Ставит панель графика.
        self._span_handler: SpanHandler | None = None
        #: Кому сказать про двойной щелчок левой по полю. Ставит панель графика.
        self._expand_handler: ExpandHandler | None = None
        #: Что было рассказано в прошлый раз. Держится, чтобы не переписывать
        #: строку сведений на каждом движении мыши внутри одной свечи:
        #: событий движения приходит порядка сотни в секунду, а перерисовка
        #: шести надписей со сменой таблицы стилей — не бесплатная.
        self._reported: CandleInfo | None = None

        self.setMinimumSize(360, 220)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.CrossCursor)
        # Правая кнопка занята выделением отрезка, и меню поверх графика
        # по ней всплывать не должно.
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)

    # ------------------------------------------------------------------ данные

    def widget(self) -> QWidget:
        return self

    def show_chart(self, data: ChartData) -> None:
        """Полная замена содержимого. Масштаб переживает её, если график тот же.

        ⚠️ Прежде окно обзора здесь **пересоздавалось** всегда, и приближение,
        выставленное руками, жило до ближайшего прогона — то есть до ближайшей
        живой свечи (`B-009`). Наблюдать за появлением свечи глазами было
        нельзя, а именно этим принимается Э2.

        Что считается «тем же графиком»: совпали инструмент и размер свечи.
        Сменился любой из них — данные на оси другие, и сброс к умолчанию
        верен: приближение, оставшееся от MXU6 5м, на минутках другого
        инструмента показывает не то место, а случайное.

        Слежение за последней свечой сохраняется как режим, а не как позиция:
        стоял у правого края — правый край переезжает на новую последнюю свечу
        с тем же приближением; отмотал назад — остаётся там, где стоял, и точка
        привязки берётся **по времени**, а не по номеру свечи, чтобы сдвиг
        левого края ряда (новый день в выборке) не утащил вид вбок.
        """
        same_chart = not self._line.empty and (
            (data.instrument, data.timeframe)
            == (self._data.instrument, self._data.timeframe)
        )
        anchor = self._stamp_at(self._view.right) if same_chart else None
        # Протяжка правой кнопкой переживает замену ряда так же, как вид:
        # начало держится по времени, номера мест после замены другие.
        span_stamp = (
            self._stamp_at(self._span[0]) if same_chart and self._span is not None else None
        )

        self._data = data
        self._line = Timeline(data.candles)
        self._line_changed()
        self._take_average(data.average)
        self._average_label = data.average_label
        self._markers = {
            Layer.PLAN: [m for m in data.markers if m.layer is Layer.PLAN],
            Layer.FACT: [m for m in data.markers if m.layer is Layer.FACT],
        }
        self._paths = {
            Layer.PLAN: [p for p in data.paths if p.layer is Layer.PLAN],
            Layer.FACT: [p for p in data.paths if p.layer is Layer.FACT],
        }
        self._levels = list(data.levels)
        self._shades = list(data.shades)
        self._seams = list(data.seams)
        if same_chart and not self._line.empty:
            self._keep_view(anchor)
        else:
            self._view = _Viewport(
                right=float(max(len(self._line) - 1, 0)),
                span=min(DEFAULT_VISIBLE, max(len(self._line), MIN_VISIBLE)),
                follow=True,
            )
        if self._span is not None:
            if span_stamp is None:
                self._span = None
                self._edge_timer.stop()
            else:
                self._span = (round(self._index_of_stamp(span_stamp)), self._span[1])
        self.update()

    def _keep_view(self, anchor: float | None) -> None:
        """Оставить приближение человека поверх обновившегося ряда.

        `anchor` — отметка времени правого края до замены данных. Приближение
        (`span`) сохраняется всегда, положение — только когда человек отмотал
        историю: в режиме слежения правый край переезжает на новую последнюю
        свечу, иначе новые свечи уходили бы за экран.
        """
        fits = max(len(self._line), MIN_VISIBLE)
        self._view.span = max(MIN_VISIBLE, min(self._view.span, fits))
        if self._view.whole:
            # «Весь период» — режим, как и слежение: новый ряд тоже целиком.
            self._view.span = fits
        if self._view.follow or anchor is None:
            self._view.right = float(max(len(self._line) - 1, 0))
        else:
            self._view.right = self._index_of_stamp(anchor)
        self._clamp_view()

    def append_candle(self, candle: Candle) -> None:
        """Добавить свечу справа — вместе с местами под пропуск перед ней.

        Живая свеча после обрыва связи приходит именно сюда, и пропуск,
        случившийся за время обрыва, обязан появиться на оси тем же приходом:
        иначе картинка ползёт дальше как ни в чём не бывало (`B-005`).
        """
        self._line.append(candle)
        self._line_changed()
        if self._view.whole:
            self._view.span = max(len(self._line), MIN_VISIBLE)
        if self._view.follow:
            self._view.right = float(len(self._line) - 1)
        self.update()

    def update_last_candle(self, candle: Candle) -> None:
        if self._line.empty:
            self.append_candle(candle)
            return
        stamp = self._line.stamps[-1]
        self._line.replace_last(candle)
        if self._line.stamps[-1] != stamp:
            # Растущая свеча приходит много раз за свою жизнь, а ось меняется,
            # только если сменилось её время, — иначе кэш мест сбрасывался бы
            # на каждом тике цены.
            self._line_changed()
        self.update()

    def set_average(self, points: Sequence[LinePoint], label: str = "") -> None:
        self._take_average(points)
        if label:
            self._average_label = label
        self.update()

    def _take_average(self, points: Sequence[LinePoint]) -> None:
        """Запомнить среднюю вместе с отметками её точек — один раз на приход."""
        self._average = list(points)
        self._average_stamps = [to_msk(point.time).timestamp() for point in self._average]
        self._average_places = None

    def _line_changed(self) -> None:
        """Ось сменилась: места точек средней надо пересчитать заново."""
        self._average_places = None

    def set_markers(self, layer: Layer, markers: Sequence[Marker]) -> None:
        self._markers[layer] = list(markers)
        self.update()

    def set_paths(self, layer: Layer, paths: Sequence[TradePath]) -> None:
        self._paths[layer] = list(paths)
        self.update()

    def set_levels(self, levels: Sequence[PriceLevel]) -> None:
        self._levels = list(levels)
        self.update()

    def set_shades(self, shades: Sequence[Shade]) -> None:
        self._shades = list(shades)
        self.update()

    # -------------------------------------------------------------------- вид

    def set_layer_visible(self, layer: Layer, visible: bool) -> None:
        self._visible[layer] = visible
        self.update()

    def set_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.update()

    def set_hover_handler(self, handler: HoverHandler | None) -> None:
        """Кому сообщать про свечу под курсором. Разбор — в `ChartSurface`."""
        self._hover_handler = handler

    def set_span_handler(self, handler: SpanHandler | None) -> None:
        """Кому сообщать про отрезок под правой кнопкой. Разбор — в `ChartSurface`."""
        self._span_handler = handler

    def set_expand_handler(self, handler: ExpandHandler | None) -> None:
        """Кому сообщать про двойной щелчок левой. Разбор — в `ChartSurface`."""
        self._expand_handler = handler

    def show_all(self) -> None:
        """Весь ряд в ширину графика. Слежение за последней свечой остаётся."""
        if self._line.empty:
            return
        count = len(self._line)
        self._view = _Viewport(
            right=float(count - 1), span=max(count, MIN_VISIBLE), follow=True, whole=True
        )
        self._clamp_view()
        self._report_hover(self._cursor)
        self.update()

    def select_all(self) -> None:
        """Выделить весь ряд — тем же путём, что протяжка правой кнопкой."""
        if self._span_handler is None:
            return
        if self._line.empty:
            self._span_handler(None)
            return
        self._report_places(0, len(self._line) - 1)

    def scroll_to_last(self) -> None:
        self._view.right = float(max(len(self._line) - 1, 0))
        self._view.follow = True
        self.update()

    def is_following(self) -> bool:
        return self._view.follow

    def show_moment(self, since: datetime, until: datetime) -> bool:
        """Поставить отрезок посреди экрана; приближение — не ближе прежнего.

        Слежение за последней свечой снимается: иначе первая же новая свеча
        утащила бы экран от сделки, ради которой его сюда привели.
        """
        count = len(self._line)
        if not count:
            return False
        first, last = self._index_of_time(since), self._index_of_time(until)
        if last < 0 or first > count - 1:
            return False
        width = int(last - first) + 1
        self._view.span = max(self._view.span, min(width + 2 * MIN_VISIBLE, count))
        self._view.right = last + (self._view.span - width) / 2
        self._view.follow = False
        self._view.whole = False
        self._clamp_view()
        self.update()
        return True

    # ---------------------------------------------------------------- геометрия

    def _plot_rect(self) -> QRectF:
        return QRectF(
            PADDING,
            PADDING,
            max(self.width() - PADDING - AXIS_WIDTH, 1.0),
            max(self.height() - PADDING - AXIS_HEIGHT, 1.0),
        )

    def _first_index(self) -> float:
        return self._view.right - self._view.span + 1

    def _x_of_index(self, index: float, plot: QRectF) -> float:
        step = plot.width() / max(self._view.span, 1)
        return plot.left() + (index - self._first_index() + 0.5) * step

    def _index_of_time(self, moment: datetime) -> float:
        """Дробный индекс свечи для произвольного момента времени.

        Метки сделок и границы затенения не обязаны совпадать с временем свечи:
        сделка исполняется внутри минуты, а торговое окно открывается в 10:05
        независимо от разметки свечей. Промежуточные моменты попадают между
        свечами, крайние — продолжаются наружу с шагом последней свечи, чтобы
        затенение доходило до края области, а не обрывалось.

        ⚠️ Отметка берётся через `to_msk`, а не вызовом `timestamp()`:
        у наивного времени Python подставляет **системный** пояс машины,
        а ось (`ui/chart/timeline.py`) размечает свои места по московскому.
        Разъехавшись, они кладут метки сделок и границы затенения на чужие
        свечи — ровно на разницу поясов, — и не роняют при этом ничего.
        """
        return self._index_of_stamp(to_msk(moment).timestamp())

    def _index_of_stamp(self, stamp: float) -> float:
        """То же самое, но от отметки эпохи: обратное к `_stamp_at`.

        Считается по **местам** оси, а не по свечам: у пропущенного часа
        свои места, и метка сделки, попавшая в него, встаёт в пустоту,
        а не приклеивается к соседней свече (`B-005`).
        """
        times = self._line.stamps
        if not times:
            return 0.0
        position = bisect.bisect_left(times, stamp)
        if position <= 0:
            step = self._grid_step()
            return (stamp - times[0]) / step if step else 0.0
        if position >= len(times):
            step = self._grid_step()
            return (len(times) - 1) + ((stamp - times[-1]) / step if step else 0.0)
        before, after = times[position - 1], times[position]
        if after == before:
            return float(position)
        return (position - 1) + (stamp - before) / (after - before)

    def _stamp_at(self, index: float) -> float:
        """Отметка времени в дробной позиции оси — обратное к `_index_of_stamp`.

        Нужна ровно для одного: запомнить, куда смотрел человек, до замены
        ряда свечей, и вернуться туда же после неё. По номеру свечи это делать
        нельзя — номера съезжают, как только у выборки сдвинулся левый край.
        """
        times = self._line.stamps
        if not times:
            return 0.0
        last = len(times) - 1
        if index <= 0:
            return times[0] + index * self._grid_step()
        if index >= last:
            return times[last] + (index - last) * self._grid_step()
        low = int(index)
        return times[low] + (index - low) * (times[low + 1] - times[low])

    def _grid_step(self) -> float:
        """Шаг одного места оси в секундах — для продолжения оси наружу.

        Берётся шаг сетки, выведенный из самих данных (`ui.chart.timeline`),
        а не среднее по ряду: среднее растягивал пропуск, ради которого всё
        и затевалось, и затенение «вне окна» уезжало тем дальше, чем больше
        данных потеряно.
        """
        return self._line.step or 300.0  # пятиминутка — только когда шага нет

    def _price_range(self) -> tuple[float, float]:
        first = max(int(self._first_index()), 0)
        last = min(int(self._view.right) + 1, len(self._line))
        window = [c for c in self._line.slots[first:last] if c is not None]
        if not window:
            # Человек отмотал в середину пропуска: свечей на экране нет.
            # Шкала строится по ближайшим свечам за краями пустоты — иначе
            # на оси цены оказались бы 0…1, числа не с этого рынка.
            window = self._line.neighbours(first, last)
        if not window:
            return 0.0, 1.0
        low = min(c.low for c in window)
        high = max(c.high for c in window)
        for level in self._levels:
            # Уровень попадает в диапазон, только если он рядом: тейк уехавшей
            # позиции не должен сплющивать свечи в полоску.
            if low - (high - low) < level.price < high + (high - low):
                low, high = min(low, level.price), max(high, level.price)
        if high - low < 1e-9:
            high = low + 1.0
        pad = (high - low) * 0.08
        return low - pad, high + pad

    def _y_of_price(self, price: float, plot: QRectF, low: float, high: float) -> float:
        return plot.bottom() - (price - low) / (high - low) * plot.height()

    # ------------------------------------------------------------------- мышь

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self._line.empty:
            return
        plot = self._plot_rect()
        steps = event.angleDelta().y() / 120.0
        if steps == 0:
            return
        span = int(round(self._view.span * WHEEL_ZOOM_FACTOR ** -steps))
        if span == self._view.span:
            # Шаг тачпада меньше одного места: двигаемся хотя бы на одно,
            # иначе на малом приближении колесо не делает ничего.
            span += -1 if steps > 0 else 1
        span = max(MIN_VISIBLE, min(span, max(len(self._line), MIN_VISIBLE)))
        self._view.whole = False
        # Свеча под курсором остаётся под курсором — иначе масштабирование
        # «уводит» график и приходится каждый раз искать нужное место заново.
        share = (event.position().x() - plot.left()) / max(plot.width(), 1.0)
        share = min(max(share, 0.0), 1.0)
        anchor = self._first_index() + share * self._view.span
        self._view.span = span
        self._view.right = anchor + (1 - share) * span - 1
        self._clamp_view()
        event.accept()
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            # Выделение отрезка. Щелчок — его вырожденный случай: отбор сделок
            # один на оба действия, и человеку нечего запоминать.
            x = event.position().x()
            self._span = (self._place_at_x(x, self._plot_rect()), x)
            self.update()
        elif event.button() == Qt.MouseButton.LeftButton:
            self._drag_x = event.position().x()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.RightButton and self._span is not None:
            started, ended = self._span
            self._span = None
            self._edge_timer.stop()
            self._report_span(started, ended)
            self.update()
        elif event.button() == Qt.MouseButton.LeftButton:
            self._drag_x = None
            self.setCursor(Qt.CursorShape.CrossCursor)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        """Двойной щелчок левой по полю — развернуть график; правой — как раньше.

        Второе нажатие двойного щелчка Qt присылает этим событием, а не
        `mousePressEvent`. Правой кнопке оно отдаётся обычному нажатию:
        двойной щелчок правой остаётся выделением, как и был. Левой —
        панорама не начинается: щелчок без протяжки ничего не двигал и так.
        """
        if event.button() != Qt.MouseButton.LeftButton:
            self.mousePressEvent(event)
            return
        if self._expand_handler is not None and self._plot_rect().contains(event.position()):
            self._expand_handler()
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        plot = self._plot_rect()
        # Перекрестье идёт за курсором всегда — и при нажатой кнопке тоже:
        # это курсор, а не режим.
        self._cursor = QPointF(event.position())
        if self._span is not None:
            self._span = (self._span[0], event.position().x())
            held = bool(event.buttons() & Qt.MouseButton.RightButton)
            if held and self._edge_overshoot(plot) != 0.0:
                if not self._edge_timer.isActive():
                    self._edge_timer.start()
            else:
                self._edge_timer.stop()
            self.update()
        elif self._drag_x is not None and not self._line.empty:
            step = plot.width() / max(self._view.span, 1)
            shift = (self._drag_x - event.position().x()) / max(step, 0.01)
            self._drag_x = event.position().x()
            if shift:
                self._view.right += shift
                self._view.whole = False
            self._view.follow = self._view.right >= len(self._line) - 1.5
            self._clamp_view()
        else:
            self._show_tooltip(event.position())
        # Строка сведений идёт за курсором и во время перетаскивания: график
        # едет под неподвижной мышью, и свеча под ней меняется.
        self._report_hover(event.position())
        self.update()

    def leaveEvent(self, event: QEvent) -> None:
        """Курсор ушёл с графика — строка сведений возвращается к хвосту ряда.

        Без этого она навсегда осталась бы на свече, над которой мышь была
        последний раз, и подписи «последняя свеча» под числами не появилось
        бы: строка утверждала бы, что человек разглядывает то, на что уже
        не смотрит. Перекрестье уходит вместе с курсором по той же причине:
        оставленное на месте, оно показывает цену, на которую никто не смотрит.
        """
        self._cursor = None
        self._report_hover(None)
        self.update()
        super().leaveEvent(event)

    def hideEvent(self, event: QHideEvent) -> None:
        """Скрытый график сам по себе не едет: отпускания кнопки он не дождётся."""
        self._edge_timer.stop()
        super().hideEvent(event)

    # ------------------------------------------------------------- выделение

    def _edge_overshoot(self, plot: QRectF) -> float:
        """На сколько точек мышь протяжки ушла за край поля: «+» вправо, «−» влево."""
        if self._span is None:
            return 0.0
        x = self._span[1]
        if x > plot.right():
            return x - plot.right()
        if x < plot.left():
            return x - plot.left()
        return 0.0

    def _edge_scroll_tick(self) -> None:
        """Шаг прокрутки вида под протяжкой за край — вызывается таймером.

        Сдвиг — доля видимого размаха (см. `EDGE_SCROLL_MAX_SHARE`): экран
        проезжается за одно и то же время и на 90 свечах, и на всём ряде.

        У края данных вид останавливается: в пустоту за последней свечой
        и перед первой протяжка не везёт. Если человек сам отмотал вид
        за край ряда раньше, назад его тоже не тянет — только не дальше.
        """
        overshoot = self._edge_overshoot(self._plot_rect())
        if overshoot == 0.0 or self._line.empty:
            self._edge_timer.stop()
            return
        share = min(abs(overshoot) / EDGE_SCROLL_FULL_PIXELS, 1.0) * EDGE_SCROLL_MAX_SHARE
        shift = self._view.span * share
        was = self._view.right
        if overshoot > 0:
            limit = max(was, float(len(self._line) - 1))
            self._view.right = min(was + shift, limit)
        else:
            limit = min(was, float(self._view.span - 1))
            self._view.right = max(was - shift, limit)
        if self._view.right == was:
            self._edge_timer.stop()
            return
        self._view.whole = False
        self._view.follow = self._view.right >= len(self._line) - 1.5
        self._report_hover(self._cursor)
        self.update()

    def _report_span(self, started: int, ended: float) -> None:
        """Рассказать панели про выделенный отрезок — по местам оси, не по свечам.

        Границы отрезка — время **мест**: у пропущенного часа места есть, свечей
        нет. Пропуски внутри отрезка приезжают отдельным полем, а не растворяются
        в «сделок не было» (`B-005`).

        Отрезок целиком за пределами ряда — это `None`, а не «пустой отрезок»:
        сказать «сделок здесь нет» про поле, где нет и оси, значит соврать.
        """
        if self._span_handler is None:
            return
        places = self._span_places(started, ended)
        if places is None:
            self._span_handler(None)
            return
        self._report_places(*places)

    def _report_places(self, first: int, last: int) -> None:
        """Отдать панели отрезок мест `first…last` — общий хвост протяжки и «выделить всё».

        Один путь на оба действия: разбор всех сделок обязан быть тем же
        разбором, что протяжка через весь ряд, с теми же пропусками.
        """
        if self._span_handler is None:
            return
        step = self._grid_step()
        self._span_handler(ChartSpan(
            start=self._line.moments[first],
            end=self._line.moments[last] + timedelta(seconds=step),
            holes=tuple(
                (self._line.moments[begin], self._line.moments[end - 1] + timedelta(seconds=step))
                for begin, end in self._line.gaps(first, last + 1)
                if begin <= last and end > first
            ),
        ))

    def _span_places(self, started: int, ended: float) -> tuple[int, int] | None:
        """Номера первого и последнего места оси под протяжкой. `None` — мимо ряда.

        `started` — место, где нажали; `ended` — где мышь сейчас, в точках.
        Мышь за краем поля считается стоящей на краю: выделение — то, что
        видно, а за край его доращивает прокрутка вида, а не пиксели за осью.
        """
        if self._line.empty:
            return None
        plot = self._plot_rect()
        inside = min(max(ended, plot.left()), plot.right() - 1e-6)
        place = self._place_at_x(inside, plot)
        first, last = min(started, place), max(started, place)
        if last < 0 or first > len(self._line) - 1:
            return None
        return max(first, 0), min(last, len(self._line) - 1)

    def _clamp_view(self) -> None:
        """Не даём уехать в пустоту дальше половины экрана в каждую сторону."""
        count = len(self._line)
        lowest = self._view.span / 2
        highest = count - 1 + self._view.span / 2
        self._view.right = min(max(self._view.right, lowest), max(highest, lowest))

    def _place_at_x(self, x: float, plot: QRectF) -> int:
        """Номер места оси под этой точкой экрана — обратное к `_x_of_index`.

        Номер целый: место либо занято свечой, либо пусто, и «между двумя
        свечами» на оси мест не бывает — там пропуск, у которого свои места
        (`ui/chart/timeline.py`).
        """
        step = plot.width() / max(self._view.span, 1)
        if step <= 0:
            return -1
        return int(math.floor((x - plot.left()) / step + self._first_index()))

    def _info_at(self, point: QPointF) -> CandleInfo | None:
        """Сведения о свече под этой точкой. `None` — свечи под ней нет.

        Три случая `None`, и все три означают одно — «сказать нечего»:
        точка вне области построения (поле, ось цены, ось времени), места
        с таким номером на оси нет, место есть, но оно пустое. Последнее —
        пропуск в данных, и подставлять там соседнюю свечу нельзя: рядом
        нарисована подпись «Данных нет: 13:08–14:03» (`B-005`).
        """
        plot = self._plot_rect()
        if not plot.contains(point):
            return None
        pair = self._line.pair_at(self._place_at_x(point.x(), plot))
        if pair is None:
            return None
        candle, previous = pair
        return CandleInfo.from_pair(candle, previous)

    def _report_hover(self, point: QPointF | None) -> None:
        """Рассказать панели про свечу под курсором, если рассказ изменился."""
        info = self._info_at(point) if point is not None else None
        if info == self._reported:
            return
        self._reported = info
        if self._hover_handler is not None:
            self._hover_handler(info)

    def _show_tooltip(self, point: QPointF) -> None:
        """Подсказка при наведении на метку.

        Текст подсказки приходит вместе с меткой. Сравнение прогноза и факта —
        расчётная цена, фактическая, расхождение в пунктах и рублях — считает тот,
        кто знает шаг цены и стоимость пункта. Окно бы посчитало это «примерно»,
        и «примерно» разошлось бы с журналом.
        """
        for position, marker in self._hit:
            if abs(position.x() - point.x()) <= 9 and abs(position.y() - point.y()) <= 9:
                text = marker.tooltip or marker.text or marker.kind.label
                QToolTip.showText(self.mapToGlobal(point.toPoint()), text, self)
                return
        QToolTip.hideText()

    # ---------------------------------------------------------------- рисование

    def paintEvent(self, event) -> None:  # noqa: ANN001 — тип события задан Qt
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        theme = self._theme
        painter.fillRect(self.rect(), QColor(theme.background))

        plot = self._plot_rect()
        if self._line.empty:
            painter.setPen(QPen(QColor(theme.text_dim)))
            painter.drawText(
                self.rect(),
                int(Qt.AlignmentFlag.AlignCenter),
                "Свечей нет. Выберите инструмент и загрузите историю.",
            )
            painter.end()
            return

        low, high = self._price_range()
        self._hit = []

        self._draw_shades(painter, plot)
        self._draw_grid(painter, plot, low, high)
        self._draw_candles(painter, plot, low, high)
        self._draw_gaps(painter, plot)
        self._draw_seams(painter, plot)
        self._draw_average(painter, plot, low, high)
        self._draw_paths(painter, plot, low, high)
        self._draw_levels(painter, plot, low, high)
        self._draw_markers(painter, plot, low, high)
        self._draw_axes(painter, plot, low, high)
        self._draw_caption(painter, plot)
        self._draw_span(painter, plot)
        self._draw_crosshair(painter, plot, low, high)
        painter.end()

    def _draw_shades(self, painter: QPainter, plot: QRectF) -> None:
        font = QFont(self.font())
        font.setPointSizeF(max(font.pointSizeF() - 1.5, 6.0))
        for shade in self._shades:
            left = self._x_of_index(self._index_of_time(shade.start), plot)
            right = self._x_of_index(self._index_of_time(shade.end), plot)
            if right < plot.left() or left > plot.right():
                continue
            box = QRectF(
                max(left, plot.left()), plot.top(),
                min(right, plot.right()) - max(left, plot.left()), plot.height(),
            )
            painter.fillRect(box, self._theme.qcolor(self._theme.shade_color(shade.kind), 40))
            if box.width() > 90:
                painter.setFont(font)
                painter.setPen(QPen(self._theme.qcolor(self._theme.text_dim, 200)))
                painter.drawText(
                    box.adjusted(4, 2, -4, 0),
                    int(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter),
                    shade.note or shade.kind.label,
                )
                painter.setFont(self.font())

    def _draw_grid(self, painter: QPainter, plot: QRectF, low: float, high: float) -> None:
        pen = QPen(QColor(self._theme.grid))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        for price in _nice_ticks(low, high, 6):
            y = self._y_of_price(price, plot, low, high)
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
        for index in self._time_ticks(plot):
            x = self._x_of_index(index, plot)
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))

    def _draw_candles(self, painter: QPainter, plot: QRectF, low: float, high: float) -> None:
        step = plot.width() / max(self._view.span, 1)
        if step < DENSE_STEP:
            self._draw_candle_columns(painter, plot, low, high)
            return
        body = max(step * 0.66, 1.0)
        first = max(int(self._first_index()) - 1, 0)
        last = min(int(self._view.right) + 2, len(self._line))
        for index in range(first, last):
            candle = self._line.slots[index]
            if candle is None:
                continue  # место есть, свечи нет: пропуск рисуется пустым
            x = self._x_of_index(index, plot)
            if x < plot.left() - step or x > plot.right() + step:
                continue
            rising = candle.close >= candle.open
            color = QColor(self._theme.bull if rising else self._theme.bear)
            pen = QPen(color)
            pen.setWidthF(1.0)
            painter.setPen(pen)
            painter.drawLine(
                QPointF(x, self._y_of_price(candle.high, plot, low, high)),
                QPointF(x, self._y_of_price(candle.low, plot, low, high)),
            )
            top = self._y_of_price(max(candle.open, candle.close), plot, low, high)
            bottom = self._y_of_price(min(candle.open, candle.close), plot, low, high)
            rect = QRectF(x - body / 2, top, body, max(bottom - top, 1.0))
            painter.fillRect(rect, color)

    def _draw_candle_columns(
        self, painter: QPainter, plot: QRectF, low: float, high: float
    ) -> None:
        """Свечи гуще двух точек на место — по столбцам экрана, одной чертой на столбец.

        Черта — от наименьшей цены свечей столбца до наибольшей; цвет —
        закрытие последней свечи столбца против открытия первой, то есть
        как у одной большой свечи из них. Пустые места не рисуются: столбец
        из одного пропуска остаётся пустым, как и на обычном приближении.
        Это не расчёт по торговому правилу, а то же сжатие, которое глаз
        делает сам, глядя на сотню свечей в сантиметре экрана.
        """
        rising: list[QLineF] = []
        falling: list[QLineF] = []

        def close_column(column: int, top: float, bottom: float, rises: bool) -> None:
            x = column + 0.5
            stroke = QLineF(
                x, self._y_of_price(top, plot, low, high),
                x, self._y_of_price(bottom, plot, low, high) + 0.5,
            )
            (rising if rises else falling).append(stroke)

        step = plot.width() / max(self._view.span, 1)
        origin = plot.left() + (0.5 - self._first_index()) * step
        first = max(int(self._first_index()), 0)
        last = min(int(self._view.right) + 1, len(self._line))
        column = None
        top = bottom = opened = closed = 0.0
        for index, candle in enumerate(self._line.slots[first:last], start=first):
            if candle is None:
                continue
            here = int(origin + index * step)
            if here != column:
                if column is not None:
                    close_column(column, top, bottom, closed >= opened)
                column, top, bottom = here, candle.high, candle.low
                opened = candle.open
            else:
                top, bottom = max(top, candle.high), min(bottom, candle.low)
            closed = candle.close
        if column is not None:
            close_column(column, top, bottom, closed >= opened)

        for strokes, colour in ((rising, self._theme.bull), (falling, self._theme.bear)):
            pen = QPen(QColor(colour))
            pen.setWidthF(1.0)
            painter.setPen(pen)
            painter.drawLines(strokes)

    def _draw_gaps(self, painter: QPainter, plot: QRectF) -> None:
        """Назвать словами полосы, где свечей нет ни одной.

        Пустота на графике неотличима от края выборки и от спокойного рынка,
        а это разные вещи. Подпись называет пропущенный отрезок временем —
        тем самым, которое владелец счёта сверяет с терминалом брокера, —
        и границы отмечены пунктиром: «график врёт молча» и есть содержание
        `B-005`.

        Заливки нет намеренно: заливкой в этой программе показано затенение
        «робот молчал» (`_draw_shades`), и вторая заливка рядом читалась бы
        как то же самое. Пропуск данных и молчание робота — разные события.
        """
        first = max(int(self._first_index()) - 1, 0)
        last = min(int(self._view.right) + 2, len(self._line))
        runs = self._line.gaps(first, last)
        if not runs:
            return

        font = QFont(self.font())
        font.setPointSizeF(max(font.pointSizeF() - 1.5, 6.0))
        painter.setFont(font)
        metrics = painter.fontMetrics()
        edge = QPen(QColor(self._theme.axis))
        edge.setWidthF(1.0)
        edge.setStyle(Qt.PenStyle.DotLine)

        for start, stop in runs:
            # Границы берутся по краям крайних мест, а не по их серединам:
            # иначе пунктир прошёл бы сквозь соседние свечи.
            left = self._x_of_index(start - 0.5, plot)
            right = self._x_of_index(stop - 0.5, plot)
            if right < plot.left() or left > plot.right():
                continue
            if right - left < 1.0:
                # Пропуск уже точки: два пунктира на одном столбце — это
                # серая черта поверх свечей, а не граница. На весь период
                # таких ежедневно два (клиринг, вечерний перерыв), и сотни
                # черт закрывали график сплошной штриховкой. Пропуск при
                # этом виден в разборе участка.
                continue
            painter.setPen(edge)
            for x in (left, right):
                if plot.left() <= x <= plot.right():
                    painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            box = QRectF(
                max(left, plot.left()), plot.top(),
                min(right, plot.right()) - max(left, plot.left()), plot.height(),
            )
            text = self._gap_caption(box, metrics, start, stop)
            if not text:
                continue
            painter.setPen(QPen(self._theme.qcolor(self._theme.text_dim, 210)))
            painter.drawText(box, int(Qt.AlignmentFlag.AlignCenter), text)
        painter.setFont(self.font())

    def _draw_seams(self, painter: QPainter, plot: QRectF) -> None:
        """Стык контрактов: вертикальный пунктир и тикер нового контракта.

        Решение 0061: на каждом отрезке показан свой действующий контракт.
        Цены на стыке не подгоняются (0049 §2), и скачок между последней
        свечой старого и первой свечой нового — разница контрактов, а не
        движение рынка. Метка стоит ровно затем, чтобы его так и прочли.
        Линия — по левому краю первой свечи нового контракта.
        """
        if not self._seams:
            return
        font = QFont(self.font())
        font.setPointSizeF(max(font.pointSizeF() - 1.0, 6.0))
        font.setBold(True)
        metrics = QFontMetrics(font)
        pen = QPen(QColor(self._theme.warning))
        pen.setWidthF(1.5)
        pen.setStyle(Qt.PenStyle.DashLine)
        for seam in self._seams:
            x = self._x_of_index(self._index_of_time(seam.time) - 0.5, plot)
            if not plot.left() <= x <= plot.right():
                continue
            painter.setPen(pen)
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            text = (
                f"{seam.previous} → {seam.symbol}" if seam.previous else seam.symbol
            )
            width = metrics.horizontalAdvance(text) + 10
            # Подпись у нижнего края: верхний занят подписью затенения
            # («Вне торгового окна…»), и поверх неё тикер читался бы обрывком.
            height = metrics.height() + 4
            box = QRectF(x + 2, plot.bottom() - height - 2, width, height)
            fill = self._theme.warning
            painter.fillRect(box, QColor(fill))
            painter.setFont(font)
            painter.setPen(QPen(QColor(text_on(fill))))
            painter.drawText(box, int(Qt.AlignmentFlag.AlignCenter), text)
        painter.setFont(self.font())

    def _gap_caption(
        self, box: QRectF, metrics: QFontMetrics, start: int, stop: int
    ) -> str:
        """Подпись пропуска: с временем, если влезает, короткая, если нет."""
        if box.width() < GAP_CAPTION_WIDTH:
            return ""
        since = to_msk(self._line.moments[start])
        until = to_msk(self._line.moments[stop - 1])
        full = f"Данных нет: {since:%H:%M}–{until:%H:%M}"
        if metrics.horizontalAdvance(full) + 8 <= box.width():
            return full
        short_form = "Данных нет"
        if metrics.horizontalAdvance(short_form) + 8 <= box.width():
            return short_form
        return ""

    def _draw_average(self, painter: QPainter, plot: QRectF, low: float, high: float) -> None:
        if len(self._average) < 2:
            return
        pen = QPen(QColor(self._theme.average))
        pen.setWidthF(1.8)
        painter.setPen(pen)
        # ⚠️ Линия рвётся на стыке контрактов: у каждого своя средняя
        # (решение 0061), и отрезок между ними рисовал бы переход, которого
        # не считал никто, — скачок стыка выглядел бы сглаженным.
        #
        # Обходятся только точки на экране, найденные по запомненным местам,
        # а гуще точки на место — через одну-две: на весь период их 15 тысяч
        # на тысячу с небольшим столбцов экрана, и линия по каждой не видна
        # глазу, а стоит кадра.
        places = self._average_places_now()
        stamps = self._average_stamps
        seams = sorted(to_msk(seam.time).timestamp() for seam in self._seams)
        begin = bisect.bisect_left(places, self._first_index() - 2)
        end = bisect.bisect_right(places, self._view.right + 2)
        stride = max(int(self._view.span / max(plot.width(), 1.0)), 1)
        picked = list(range(begin, end, stride))
        if picked and picked[-1] != end - 1:
            picked.append(end - 1)
        polygon = QPolygonF()
        passed = bisect.bisect_right(seams, stamps[begin]) if begin < end else 0
        for number in picked:
            while passed < len(seams) and stamps[number] >= seams[passed]:
                passed += 1
                if polygon.count() > 1:
                    painter.drawPolyline(polygon)
                polygon = QPolygonF()
            polygon.append(QPointF(
                self._x_of_index(places[number], plot),
                self._y_of_price(self._average[number].value, plot, low, high),
            ))
        if polygon.count() > 1:
            painter.drawPolyline(polygon)

    def _average_places_now(self) -> list[float]:
        """Места точек средней на оси — из запомненного, пересчёт только по смене оси."""
        if self._average_places is None:
            self._average_places = [self._index_of_stamp(stamp) for stamp in self._average_stamps]
        return self._average_places

    def _draw_paths(self, painter: QPainter, plot: QRectF, low: float, high: float) -> None:
        for layer, paths in self._paths.items():
            if not self._visible.get(layer, True):
                continue
            for path in paths:
                color = QColor(self._theme.plan if layer is Layer.PLAN else self._theme.fact)
                if path.profitable is not None and layer is Layer.FACT:
                    color = QColor(self._theme.success if path.profitable else self._theme.danger)
                pen = QPen(color)
                pen.setWidthF(1.4)
                pen.setStyle(
                    Qt.PenStyle.DashLine if layer is Layer.PLAN else Qt.PenStyle.SolidLine
                )
                painter.setPen(pen)
                painter.drawLine(
                    QPointF(
                        self._x_of_index(self._index_of_time(path.entry_time), plot),
                        self._y_of_price(path.entry_price, plot, low, high),
                    ),
                    QPointF(
                        self._x_of_index(self._index_of_time(path.exit_time), plot),
                        self._y_of_price(path.exit_price, plot, low, high),
                    ),
                )

    def _draw_levels(self, painter: QPainter, plot: QRectF, low: float, high: float) -> None:
        font = QFont(self.font())
        font.setPointSizeF(max(font.pointSizeF() - 1.0, 6.5))
        for level in self._levels:
            if not (low <= level.price <= high):
                continue
            y = self._y_of_price(level.price, plot, low, high)
            color = QColor(self._theme.level_color(level.kind))
            pen = QPen(color)
            pen.setWidthF(1.3)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            chip = QRectF(plot.right() + 1, y - 8, AXIS_WIDTH - 3, 16)
            painter.fillRect(chip, color)
            painter.setFont(font)
            # ⚠️ Цвет надписи считается по заливке, а не берётся белым.
            # В тёмной теме `level_take` (`#66bb6a`) даёт с белым 2,36:1,
            # `level_trailing` (`#4dd0e1`) — 1,84:1. На этой плашке стоит
            # число уровня, по которому владелец счёта находит заявку
            # у брокера; нечитаемое число — это его отсутствие.
            painter.setPen(QPen(QColor(text_on(self._theme.level_color(level.kind)))))
            painter.drawText(chip, int(Qt.AlignmentFlag.AlignCenter), fmt_number(level.price, 0))
            painter.setFont(self.font())
            painter.setPen(QPen(QColor(self._theme.text_dim)))
            # Подпись ниже линии: сверху у левого края стоит подпись инструмента,
            # и уровень, попавший под самый верх графика, перекрывал бы её.
            painter.drawText(
                QRectF(plot.left() + 4, y + 2, 200, 14),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                level.label or level.kind.label,
            )

    def _draw_markers(self, painter: QPainter, plot: QRectF, low: float, high: float) -> None:
        for layer in (Layer.PLAN, Layer.FACT):
            if not self._visible.get(layer, True):
                continue
            for marker in self._markers.get(layer, []):
                index = self._index_of_time(marker.time)
                if index < self._first_index() - 1 or index > self._view.right + 1:
                    continue
                x = self._x_of_index(index, plot)
                y = self._y_of_price(marker.price, plot, low, high)
                y += _marker_offset(marker.kind, layer)
                point = QPointF(x, y)
                self._draw_marker(painter, point, marker, layer)
                self._hit.append((point, marker))

    def _draw_marker(self, painter: QPainter, point: QPointF, marker: Marker, layer: Layer) -> None:
        """Метка сделки. Форму рисует общая функция — та же, что значок легенды.

        Своей копии форм здесь нет намеренно: пока их было две, правка одной
        оставляла вторую старой, и рядом на экране оказывались метка и значок
        легенды, изображающие разное (`ui/chart/glyphs.py`).
        """
        draw_marker(painter, point, marker.kind, layer, GlyphStyle(self._theme, self.font()))

    def _time_ticks(self, plot: QRectF) -> list[int]:
        """Индексы свечей, у которых подписано время. Реже, чем каждая свеча.

        Шаг — от настоящей ширины места, в том числе уже точки: прежний
        потолок «не реже раза в 72 места» на весь период ставил двести с лишним
        линий сетки — по одной на шесть точек экрана.
        """
        step = plot.width() / max(self._view.span, 1)
        every = max(int(72 / step), 1) if step > 0 else 1
        first = max(int(self._first_index()), 0)
        last = min(int(self._view.right) + 1, len(self._line))
        return list(range(-(-first // every) * every, last, every))

    def _draw_axes(self, painter: QPainter, plot: QRectF, low: float, high: float) -> None:
        theme = self._theme
        font = QFont(self.font())
        font.setPointSizeF(max(font.pointSizeF() - 1.0, 6.5))
        painter.setFont(font)

        pen = QPen(QColor(theme.axis))
        painter.setPen(pen)
        painter.drawLine(QPointF(plot.right(), plot.top()), QPointF(plot.right(), plot.bottom()))
        painter.drawLine(QPointF(plot.left(), plot.bottom()), QPointF(plot.right(), plot.bottom()))

        painter.setPen(QPen(QColor(theme.text_dim)))
        for price in _nice_ticks(low, high, 6):
            y = self._y_of_price(price, plot, low, high)
            painter.drawText(
                QRectF(plot.right() + 4, y - 8, AXIS_WIDTH - 8, 16),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                fmt_number(price, 0),
            )

        previous_day = ""
        occupied = plot.left() - 1000.0
        for index in self._time_ticks(plot):
            # Подпись берётся у **места** оси, а не у свечи: у пропущенного
            # часа свечей нет, а подписи 13:10, 13:20, 13:30 обязаны идти —
            # ими и видно, сколько именно времени потеряно (`B-005`).
            moment = to_msk(self._line.moments[index])
            day = moment.strftime("%d.%m")
            text = moment.strftime("%H:%M") if day == previous_day else f"{day} {moment:%H:%M}"
            previous_day = day
            x = self._x_of_index(index, plot)
            # Рамка подписи прижимается к области построения: без этого крайняя
            # левая подпись обрезается и «19.06 09:35» читается как «.06 09:35».
            left = min(max(x - 40, plot.left()), plot.right() - 80)
            width = painter.fontMetrics().horizontalAdvance(text)
            if left + (80 - width) / 2 < occupied + 6:
                continue  # подпись налезла бы на соседнюю — лучше пропустить
            occupied = left + (80 + width) / 2
            painter.drawText(
                QRectF(left, plot.bottom() + 3, 80, AXIS_HEIGHT - 5),
                int(Qt.AlignmentFlag.AlignCenter),
                text,
            )

        # Пояс подписан явно: машина владельца счёта может стоять не в Москве,
        # а всё торговое время в этом проекте московское.
        painter.drawText(
            QRectF(plot.right() + 2, plot.bottom() + 3, AXIS_WIDTH - 4, AXIS_HEIGHT - 5),
            int(Qt.AlignmentFlag.AlignCenter),
            "МСК",
        )
        painter.setFont(self.font())

    def _price_at_y(self, y: float, plot: QRectF, low: float, high: float) -> float:
        """Цена в этой точке экрана — обратное к `_y_of_price`.

        Не расчёт по торговому правилу, а тот же перевод координат, которым
        нарисована шкала цены справа: человек читает это число, ведя пальцем
        от курсора к шкале, и перекрестье избавляет его от ведения пальцем.
        """
        return low + (plot.bottom() - y) / max(plot.height(), 1.0) * (high - low)

    def _chip(self, painter: QPainter, box: QRectF, text: str, font: QFont) -> None:
        """Плашка с числом на шкале. Цвет надписи считается по заливке.

        Белым по светлой заливке число нечитаемо, а именно по нему человек
        сверяет цену с терминалом брокера. То же правило, что у плашек
        уровней (`_draw_levels`).
        """
        fill = self._theme.text_dim
        painter.fillRect(box, QColor(fill))
        painter.setFont(font)
        painter.setPen(QPen(QColor(text_on(fill))))
        painter.drawText(box, int(Qt.AlignmentFlag.AlignCenter), text)

    def _draw_crosshair(self, painter: QPainter, plot: QRectF, low: float, high: float) -> None:
        """Перекрестье за курсором: время внизу, цена справа.

        Просьба владельца счёта 05.09.2026: «от курсора в стороны и вниз-вверх
        идёт линия, чтобы видно было, на какой цене стоит курсор — без кликов».
        Отсюда три свойства:

        * рисуется по одному движению мыши, без нажатий и без включения;
        * линии **тише** свечей и меток: тонкий частый штрих цветом подписей.
          Перекрестье помогает читать, а не спорит за внимание;
        * обе оси подписаны значением под курсором — иначе линию приходится
          вести глазами до шкалы, а просили как раз избавить от этого.

        Честность над пустым местом и за краем ряда:

        * место есть, свечи нет — время названо и рядом сказано «данных нет»;
        * места нет вовсе (поле слева от первой свечи, справа от последней) —
          **подписи времени нет**. Ось там продолжается только вычислением,
          и число, полученное продолжением, — не то время, что было на бирже.
        """
        point = self._cursor
        if point is None or not plot.contains(point):
            return
        theme = self._theme
        pen = QPen(theme.qcolor(theme.text_dim, 185))
        pen.setWidthF(1.0)
        pen.setStyle(Qt.PenStyle.CustomDashLine)
        pen.setDashPattern(list(CROSSHAIR_DASH))
        painter.setPen(pen)

        place = self._place_at_x(point.x(), plot)
        inside = 0 <= place < len(self._line)
        # По горизонтали перекрестье встаёт на середину места оси: так оно
        # показывает ту же свечу, что и строка сведений сверху, а не соседнюю.
        x = self._x_of_index(float(place), plot) if inside else point.x()
        x = min(max(x, plot.left()), plot.right())
        painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
        painter.drawLine(QPointF(plot.left(), point.y()), QPointF(plot.right(), point.y()))

        font = QFont(self.font())
        font.setPointSizeF(max(font.pointSizeF() - 1.0, 6.5))
        self._chip(
            painter,
            QRectF(plot.right() + 1, point.y() - 8, AXIS_WIDTH - 3, 16),
            # Точность — как у подписей шкалы справа: плашка стоит на той же
            # шкале, и «285 962,81» рядом с «286 000» читается как другая цена.
            fmt_number(self._price_at_y(point.y(), plot, low, high), 0),
            font,
        )
        moment = self._crosshair_time(place) if inside else ""
        if not moment:
            painter.setFont(self.font())
            return
        painter.setFont(font)
        width = painter.fontMetrics().horizontalAdvance(moment) + 14
        left = min(max(x - width / 2, plot.left()), max(plot.right() - width, plot.left()))
        self._chip(painter, QRectF(left, plot.bottom() + 3, width, AXIS_HEIGHT - 5), moment, font)
        painter.setFont(self.font())

    def _crosshair_time(self, place: int) -> str:
        """Подпись времени под перекрестьем. Пустое место называет себя пустым."""
        moment = to_msk(self._line.moments[place])
        text = moment.strftime("%d.%m %H:%M")
        if self._line.slots[place] is None:
            text += " · данных нет"
        return text

    def _draw_span(self, painter: QPainter, plot: QRectF) -> None:
        """Полоса, которую человек выделяет правой кнопкой прямо сейчас.

        Видна только во время протяжки: по отпусканию на её месте открывается
        разбор сделок, и оставленная полоса спорила бы с ним за внимание.
        Без полосы протяжка выглядит как «ничего не происходит».
        """
        if self._span is None:
            return
        places = self._span_places(*self._span)
        if places is None:
            return
        first, last = places
        left = max(self._x_of_index(first - 0.5, plot), plot.left())
        right = min(self._x_of_index(last + 0.5, plot), plot.right())
        if right <= left:
            return
        box = QRectF(left, plot.top(), right - left, plot.height())
        painter.fillRect(box, self._theme.qcolor(self._theme.text_dim, 55))
        pen = QPen(self._theme.qcolor(self._theme.text_dim, 200))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.drawLine(QPointF(left, plot.top()), QPointF(left, plot.bottom()))
        painter.drawLine(QPointF(right, plot.top()), QPointF(right, plot.bottom()))

    def _draw_caption(self, painter: QPainter, plot: QRectF) -> None:
        parts = [p for p in (self._data.instrument, self._data.timeframe, self._average_label) if p]
        if not parts:
            return
        text = " · ".join(parts)
        box = QRectF(plot.left() + 4, plot.top() + 2, plot.width() - 8, 18)
        width = painter.fontMetrics().horizontalAdvance(text) + 8
        # Подложка под подписью: без неё линия уровня, попавшая под самый верх,
        # проходит сквозь буквы и обе надписи становятся нечитаемыми.
        painter.fillRect(QRectF(box.left() - 2, box.top(), width, box.height()),
                         self._theme.qcolor(self._theme.background, 220))
        painter.setPen(QPen(QColor(self._theme.text_dim)))
        painter.drawText(box, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)


def _marker_offset(kind: MarkerKind, layer: Layer = Layer.FACT) -> float:
    """Куда сдвинуть метку от цены, чтобы она не легла на свечу.

    Прогноз отодвигается дальше факта: пара «расчёт и исполнение» стоит почти
    в одной точке, и без разноса две метки сливаются в кляксу — как раз тогда,
    когда сравнить их и нужно.
    """
    extra = 15.0 if layer is Layer.PLAN else 0.0
    if kind is MarkerKind.ENTRY_LONG:
        return 16.0 + extra
    if kind is MarkerKind.REVERSAL:
        return 0.0 - extra
    return -16.0 - extra


def _nice_ticks(low: float, high: float, count: int) -> list[float]:
    """Круглые значения цены для сетки: 1, 2, 2,5 или 5 на порядок."""
    if high <= low or count < 2:
        return []
    rough = (high - low) / (count - 1)
    magnitude = 10 ** _floor_log10(rough)
    for multiplier in (1, 2, 2.5, 5, 10):
        step = magnitude * multiplier
        if step >= rough:
            break
    start = step * int(low / step)
    ticks = []
    value = start
    while value <= high + step * 0.001:
        if value >= low:
            ticks.append(round(value, 6))
        value += step
    return ticks


def _floor_log10(value: float) -> int:
    if value <= 0:
        return 0
    return int(math.floor(math.log10(value)))
