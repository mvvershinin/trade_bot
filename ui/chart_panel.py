"""Панель графика: сам график, строка сведений о свече, легенда и разбор сделок.

Панель не знает, чем нарисован график: она держит `ChartSurface`, полученный
от фабрики. Имя библиотеки отрисовки здесь не встречается — и не должно
(ARCHITECTURE.md §4).

Торговых правил здесь нет ни одного. Панель показывает то, что ей дали,
и передаёт наружу то, что нажали. В разборе сделок она **ничего не считает**:
цены, причина выхода, результат и комиссия приходят готовыми в `TradeRow`,
её дело — отобрать строки по времени и написать их словами.

Три решения, принятые здесь и названные вслух
---------------------------------------------
1. **Легенда раскрыта по умолчанию.** Владелец счёта сейчас не может прочитать
   собственный график («это максимально запутано и непонятно мне», 05.09.2026);
   легенда, спрятанная за кнопкой, — это легенда, которую он не найдёт.
   Она сворачивается кнопкой и место графику возвращает, но первым делом
   человек видит объяснение, а не догадывается.
2. **Оба слоя меток включены, ни одна метка с графика не убрана.** Было
   предложено гасить «Прогноз» по умолчанию — шесть видов меток сразу и есть
   причина жалобы, — и владелец счёта это отклонил прямо: «точки, где сделки,
   с графика не убирай, оставь, но сделай понятней» (05.09.2026). Значит
   понятность делается легендой, формой и разбором по правой кнопке,
   а не сокращением показанного. Галочка «Прогноз» остаётся: спрятать слой
   человек может сам, и подсказка ему об этом говорит.
3. **Итог по выделенным сделкам показывается — и первым делом.** Решение
   обратное принятому здесь же днём раньше, и отменил его владелец счёта,
   получив список из восьми сделок: «тут не хватает итоговой цифры — профит
   или проёб» (05.09.2026). Складывать столбик глазами он не должен.

   Возражение, из-за которого итога сначала не было, никуда не делось: вторая
   сумма на экране, которую никто не сверял с журналом и с движком, — это две
   правды про один день. Снято оно не словами, а двумя сторожами. Подписи,
   порядок и оформление чисел взяты из общей таблицы с итогом под журналом
   сделок (`ui.formatting.SUMMARY_FIELDS`), а не написаны вторыми словами;
   сами числа — сложение готовых `TradeRow.profit_rub` и `commission_rub`,
   и совпадение этого сложения с `backtest.summarise` на одних и тех же
   сделках проверяется отдельно (`tests/test_ui_legend.py`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import QPoint, QRectF, Qt
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ui.chart import create_surface
from ui.chart.glyphs import GlyphStyle, draw_area_sample, draw_line_sample
from ui.chart.protocol import ChartSurface
from ui.formatting import (
    EMPTY,
    PRICE_FIELDS,
    SUMMARY_FIELDS,
    fmt_datetime,
    fmt_level,
    fmt_money,
    fmt_percent,
    fmt_time,
    fmt_volume,
    summary_field,
    summary_parts,
    to_msk,
)
from ui.legend import (
    LegendItem,
    LineSample,
    MarkerSample,
)
from ui.legend import (
    visible as legend_items,
)
from ui.models import (
    Candle,
    CandleInfo,
    ChartData,
    ChartSpan,
    Layer,
    LinePoint,
    Marker,
    PriceLevel,
    Shade,
    TradePath,
    TradeRow,
)
from ui.theme import Theme
from ui.theme import current as current_theme

BAR_HINT = (
    "Сведения о свече: цена открытия, наибольшая и наименьшая цены за свечу,\n"
    "цена закрытия и изменение против предыдущей свечи.\n"
    "Время слева — время начала свечи по Москве."
)

CHANGE_HINT = (
    "Насколько закрытие этой свечи отличается от закрытия предыдущей:\n"
    "в пунктах цены и в процентах.\n"
    "Это не прибыль: прибыль зависит от объёма, стороны сделки и комиссии\n"
    "и показана в журнале сделок."
)

NO_PREVIOUS_HINT = (
    "Предыдущей свечи нет — сравнивать не с чем.\n"
    "Прочерк здесь означает «неизвестно», а не «цена не изменилась»."
)

#: Подпись под числами, когда курсор не над графиком.
LAST_CANDLE_NOTE = "последняя свеча"

#: Значок легенды в логических пикселях. Высоты хватает самой крупной метке —
#: кругу «сделка вне расчёта» радиусом `MARKER_SIZE + 3`, то есть 18 точкам
#: с запасом на обводку. Больше делать нельзя: легенда стоит над графиком
#: и каждая её точка по высоте отнята у свечей.
SWATCH_WIDTH = 26
SWATCH_HEIGHT = 20

#: Сколько строк легенды в ряду. Три — чтобы объяснение помещалось в одну-две
#: строки на окне обычной ширины и легенда не съедала высоту графика.
LEGEND_COLUMNS = 3

#: Потолок размеров окна разбора. Дальше содержимое прокручивается: сделок
#: в выделенном отрезке может оказаться много, а окно, вылезшее за экран,
#: не закрыть.
DIGEST_WIDTH = 560
DIGEST_HEIGHT = 420


def tail_info(candles: Sequence[Candle]) -> CandleInfo | None:
    """Последняя свеча ряда и закрытие предыдущей. Пустой ряд — `None`.

    Ряд из одной свечи даёт `previous_close is None`, то есть «сравнивать
    не с чем», а не нулевое изменение.
    """
    if not candles:
        return None
    return CandleInfo.from_pair(candles[-1], candles[-2] if len(candles) > 1 else None)


def change_text(info: CandleInfo | None) -> str:
    """«−1 150 (−0,51%)» — изменение против закрытия предыдущей свечи.

    Прочерк, когда сравнивать не с чем: ноль здесь был бы утверждением
    «цена не изменилась», которого у нас нет. Процент опускается вовсе,
    если его не посчитать (нулевое предыдущее закрытие) — показать пункты
    и умолчать о процентах честнее, чем подставить ноль.

    Знак ставится у любого ненулевого изменения, включая то, которое
    округлилось до «0,00%»: «+0,00%» говорит «выросло на копейки»,
    а «0,00%» — «не изменилось», и это разные вещи.
    """
    if info is None or info.change is None:
        return EMPTY
    body = fmt_level(abs(info.change))
    if info.change < 0:
        body = f"−{body}"
    elif info.change > 0:
        body = f"+{body}"
    percent = info.change_pct
    if percent is None:
        return body
    return f"{body} ({fmt_percent(percent, sign=percent != 0)})"


class CandleInfoBar(QWidget):
    """Строка сведений о свече над графиком — такая же, как в терминале брокера.

    `СВЕЧА 19.06.2026 10:05 МСК · ОТКР 226 650 · МАКС 226 650 · МИН 225 525 ·
    ЗАКР 225 550 · ИЗМЕНЕНИЕ −1 150 (−0,51%)`

    Строка ничего не берёт сама и ничего не решает: какая свеча под курсором —
    знает отрисовщик, он же приносит её сюда готовой.

    **Когда курсор не над свечой** строка не пустеет, а показывает последнюю
    свечу ряда и подписывает её словами «последняя свеча». Выбор из трёх:
    пустая строка мигала бы при каждом уходе мыши с графика и отнимала бы
    единственное место, где видна текущая цена; молча оставленные прежние
    числа — неправда о том, где стоит курсор; поэтому числа остаются, но
    названы своим именем. Время свечи стоит в строке слева всегда, так что
    вопрос «к какой свече это относится» без ответа не остаётся ни разу.

    **Цвет один на всю строку и означает ровно одно** — знак изменения против
    закрытия предыдущей свечи. Не направление тела свечи (`ЗАКР` против
    `ОТКР`): тогда на одной строке оказались бы два разных «падения», и цвет
    четырёх чисел спорил бы со знаком пятого. ⚠️ У брокера правило может
    оказаться другим; сверить глазами на первой же демонстрации.

    ⚠️ **Строка красит свой фон сама, цветом темы** (`B-013`). Виджет без
    собственного фона показывается на системном — в светлой системе это
    `#efefef`, — и цвета темы оказываются не на том фоне, для которого
    подобраны. Замер по снимку 04.09.2026: в тёмной теме дата и время свечи
    выводились цветом `#e6e9ef` по `#efefef`, контраст 1,06:1 при пороге
    4,5:1, то есть их не было видно вовсе; в светлой на том же сером фоне
    зелёный давал 3,87:1. Фон темы делает строку продолжением графика,
    у которого фон тот же, и возвращает цветам тот фон, на котором они
    измерены. Контраст на **нарисованной картинке** меряет
    `tests/test_ui_panel.py`.
    """

    def __init__(self, parent: QWidget | None = None, theme: Theme | None = None) -> None:
        super().__init__(parent)
        self._theme: Theme = theme if theme is not None else current_theme()
        self._info: CandleInfo | None = None
        self._hovered = False
        #: Ячейки строки. Публичные — как ячейки `StatusPanel`, и по той же
        #: причине: написанное в них и есть всё видимое состояние виджета.
        #: Ключ — имя поля свечи, то же, что во второй колонке `PRICE_FIELDS`.
        self.values: dict[str, QLabel] = {}
        self._captions: list[QLabel] = []

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(4)

        self.moment = self._add_cell(layout, "СВЕЧА")
        for caption, field in PRICE_FIELDS:
            self.values[field] = self._add_cell(layout, caption)
        self.change = self._add_cell(layout, "ИЗМЕНЕНИЕ")
        self.note = QLabel()
        self._captions.append(self.note)
        layout.addWidget(self.note)
        layout.addStretch(1)

        self.setToolTip(BAR_HINT)
        paint_background(self, self._theme)
        self._refresh()

    def _add_cell(self, layout: QHBoxLayout, caption: str) -> QLabel:
        """Подпись и значение рядом — одна ячейка строки. Возвращает значение."""
        title = QLabel(caption)
        self._captions.append(title)
        value = QLabel(EMPTY)
        font = value.font()
        font.setBold(True)
        value.setFont(font)
        layout.addWidget(title)
        layout.addWidget(value)
        layout.addSpacing(10)
        return value

    # ------------------------------------------------------------------ данные

    def show_candle(self, info: CandleInfo, *, hovered: bool = True) -> None:
        """Показать сведения об этой свече.

        `hovered=False` — свеча показана не потому, что на неё навели, а как
        последняя в ряду; строка подписывает это словами.
        """
        self._info = info
        self._hovered = hovered
        self._refresh()

    def clear(self) -> None:
        """Свечей нет вовсе — график ещё пуст. Прочерки вместо чисел."""
        self._info = None
        self._hovered = False
        self._refresh()

    def set_theme(self, theme: Theme) -> None:
        """Перекрасить строку под новую тему — фон, подписи и числа."""
        self._theme = theme
        paint_background(self, theme)
        self._refresh()

    # ------------------------------------------------------------------ отрисовка

    def _color(self) -> str:
        """Цвет чисел: рост — зелёный, падение — красный, иначе обычный текст.

        Оттенки берутся у свечей (`ui/theme.py`), а не подбираются здесь:
        число и свеча, которую оно описывает, обязаны быть одного цвета.
        Контраст к фону обеих тем меряет `tests/test_ui_panel.py` — на тёмном
        фоне «зелёный» и «красный» из светлой темы нечитаемы, и это не
        косметика, а невидимый знак изменения.
        """
        change = self._info.change if self._info is not None else None
        if change is None or change == 0:
            return self._theme.text
        return self._theme.bull if change > 0 else self._theme.bear

    def _refresh(self) -> None:
        """Переписать всю строку целиком — числа, цвет, подсказки и примечание.

        Целиком, а не по изменившемуся: строка коротка, а частичное обновление
        оставляет на экране числа прошлой свечи рядом с цветом новой.
        """
        info = self._info
        color = self._color()
        for _, field in PRICE_FIELDS:
            label = self.values[field]
            label.setText(fmt_level(getattr(info.candle, field) if info is not None else None))
            label.setStyleSheet(f"color: {color};")
        self.moment.setText(
            f"{fmt_datetime(info.candle.opens_at)} МСК" if info is not None else EMPTY
        )
        self.moment.setStyleSheet(f"color: {self._theme.text};")
        self.change.setText(change_text(info))
        self.change.setStyleSheet(f"color: {color};")
        self.change.setToolTip(
            NO_PREVIOUS_HINT if info is not None and info.change is None else CHANGE_HINT
        )
        self.note.setText("" if info is None or self._hovered else LAST_CANDLE_NOTE)
        for caption in self._captions:
            caption.setStyleSheet(f"color: {self._theme.text_dim};")


def paint_background(widget: QWidget, theme: Theme) -> None:
    """Залить виджет фоном темы — тем же, что у графика.

    Палитрой, а не таблицей стилей: правило `background-color` на обычном
    `QWidget` Qt не рисует вовсе, пока виджету не выставлен признак
    `WA_StyledBackground`, и молчаливо не рисует — фон остаётся системным.
    Ровно так выглядели `B-013` (строка сведений) и `D-016` (полоса
    с галочками и легендой): светлый рубец поперёк тёмного окна.

    Цвет надписей (`WindowText`) ставится заодно, хотя каждая надпись красится
    ещё и своей таблицей стилей: надпись, добавленная сюда завтра без стиля,
    иначе взяла бы системный тёмный цвет и пропала бы на тёмном фоне.
    """
    palette = widget.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor(theme.background))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(theme.text))
    widget.setPalette(palette)
    widget.setAutoFillBackground(True)


def swatch(item: LegendItem, theme: Theme, surface: ChartSurface, widget: QWidget) -> QPixmap:
    """Значок строки легенды — картинкой, нарисованной тем же кодом, что график.

    Метку рисует **отрисовщик** (`ChartSurface.draw_marker_glyph`), а не эта
    функция: пока форм было две — одна на графике, другая в легенде, — правка
    одной оставляла вторую старой, и рядом на экране оказывались метка и её
    объяснение, изображающие разное.

    Фон значка — фон темы, тот же, что у графика: полупрозрачные заливки
    (круг «вне расчёта», затенение) на прозрачном фоне выглядят иначе, чем
    на графике, а значок обязан выглядеть ровно так же.
    """
    ratio = widget.devicePixelRatioF()
    pixmap = QPixmap(int(SWATCH_WIDTH * ratio), int(SWATCH_HEIGHT * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(QColor(theme.background))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    box = QRectF(0, 0, SWATCH_WIDTH, SWATCH_HEIGHT)
    if isinstance(item.sample, MarkerSample):
        surface.draw_marker_glyph(
            painter,
            box.center(),
            item.sample.kind,
            item.sample.layer,
            GlyphStyle(theme, widget.font()),
        )
    elif isinstance(item.sample, LineSample):
        draw_line_sample(
            painter, box, getattr(theme, item.sample.colour), dashed=item.sample.dashed
        )
    else:
        draw_area_sample(
            painter, box, theme, getattr(theme, item.sample.colour), filled=item.sample.filled
        )
    painter.end()
    return pixmap


class LegendPanel(QWidget):
    """Легенда: значок, название и объяснение на каждое обозначение графика.

    Содержимое — таблица `ui/legend.py`, а не разметка руками. Панель обходит
    её и раскладывает по трём колонкам; строка, забытая в таблице, не появится
    ни здесь, ни где-либо ещё, и это ловит проверка полноты.

    Строки слоя «Прогноз» показываются, только когда слой включён: легенда
    объясняет то, что **может появиться** на экране, а не всё, что бывает
    на свете.
    """

    def __init__(
        self, surface: ChartSurface, theme: Theme, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._surface = surface
        self._theme = theme
        self._plan = False
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(10, 2, 10, 4)
        self._grid.setHorizontalSpacing(14)
        self._grid.setVerticalSpacing(1)
        for column in range(LEGEND_COLUMNS):
            self._grid.setColumnStretch(column * 2 + 1, 1)
        #: Написанное на панели, слева направо и сверху вниз. Публичное
        #: по той же причине, что ячейки строки сведений: это всё её
        #: видимое состояние, и проверять его больше негде.
        self.rows: list[tuple[QLabel, QLabel]] = []
        paint_background(self, theme)
        self.rebuild()

    def set_plan_visible(self, visible: bool) -> None:
        """Слой «Прогноз» включили или выключили — легенда идёт следом."""
        if visible == self._plan:
            return
        self._plan = visible
        self.rebuild()

    def set_theme(self, theme: Theme) -> None:
        self._theme = theme
        paint_background(self, theme)
        self.rebuild()

    def rebuild(self) -> None:
        """Собрать легенду заново — целиком.

        Целиком, а не по изменившемуся: строк полтора десятка, а частичное
        обновление оставляет значок прошлой темы рядом с текстом новой.
        """
        while self._grid.count():
            taken = self._grid.takeAt(0)
            widget = taken.widget() if taken is not None else None
            if widget is not None:
                widget.deleteLater()
        self.rows = []
        for number, item in enumerate(legend_items(plan=self._plan)):
            row, column = divmod(number, LEGEND_COLUMNS)
            icon = QLabel()
            icon.setPixmap(swatch(item, self._theme, self._surface, self))
            icon.setToolTip(item.hint)
            text = QLabel(
                f'<b style="color:{self._theme.text}">{item.title}</b>'
                f'<span style="color:{self._theme.text_dim}"> — {item.meaning}</span>'
            )
            text.setTextFormat(Qt.TextFormat.RichText)
            text.setWordWrap(True)
            text.setToolTip(item.hint)
            self._grid.addWidget(icon, row, column * 2)
            self._grid.addWidget(text, row, column * 2 + 1)
            self.rows.append((icon, text))


def trades_in_span(span: ChartSpan, trades: Sequence[TradeRow]) -> list[TradeRow]:
    """Сделки, задевшие выделенный отрезок времени.

    Отбор по времени, а не по номеру строки в журнале: человек выделяет кусок
    графика, а не строки таблицы. Сделка попадает в отрезок, если хоть какой-то
    её частью в него заходит — вход внутри, выход внутри или отрезок целиком
    внутри сделки. Сделка, начавшаяся до выделенного куска и закрывшаяся
    в нём, — это ровно та сделка, про которую человек и спрашивает.

    Всё время приводится к МСК обеими сторонами: `ChartSpan` приходит с оси
    (там уже МСК), а `TradeRow` — из прогона, где время может оказаться
    наивным. Сравнение наивного с зонным роняет окно исключением, а сравнение
    приведённого — нет.
    """
    since, until = to_msk(span.start), to_msk(span.end)
    picked = []
    for row in trades:
        entry = to_msk(row.entry_time)
        finish = to_msk(row.exit_time) if row.exit_time is not None else entry
        if entry < until and max(finish, entry) >= since:
            picked.append(row)
    return picked


@dataclass(frozen=True, slots=True)
class SpanFacts:
    """Всё, что известно про выделенный участок, — одним свёртком.

    Сюда попадает то, что панель уже получила от прогона и держит для графика:
    сделки, метки и затенение. Ничего нового не считается — участок только
    отбирает по времени то, что и так нарисовано на экране.
    """

    span: ChartSpan
    trades: tuple[TradeRow, ...] = ()
    markers: tuple[Marker, ...] = ()
    shades: tuple[Shade, ...] = ()


#: Какие числа итога показывает разбор участка.
#:
#: Подмножество общей таблицы (`SUMMARY_FIELDS`) и в её порядке: отбор здесь,
#: подписи и порядок — там. Профит-фактор, максимальная просадка и число
#: переворотов сюда не взяты намеренно: их считает прогон по всему периоду,
#: и на куске в восемь сделок под теми же подписями стояли бы другие числа.
SPAN_SUMMARY_KEYS = (
    "trades",
    "profitable_share",
    "gross_profit_rub",
    "commission_rub",
    "net_profit_rub",
)

SPAN_FIELDS = tuple(field for field in SUMMARY_FIELDS if field.key in SPAN_SUMMARY_KEYS)


@dataclass(frozen=True, slots=True)
class SpanTotals:
    """Итог выделенного участка: сложение чисел, которые уже посчитаны.

    Имена полей совпадают с полями `TradesSummary` не случайно. Обе итоговые
    строки — под журналом сделок и здесь — собираются по одной таблице
    подписей обращением к полю по имени, поэтому совпадение слов и порядка
    держится на коде, а не на памяти того, кто правит одно из двух мест.

    `counted` подписью на экран не выходит: это служебное «по скольким
    сделкам результат вообще известен». Когда оно меньше `trades`, разбор
    говорит об этом отдельной строкой — итог по половине сделок, показанный
    как итог по всем, есть ровно то враньё, ради которого окно и открывали.
    """

    trades: int
    profitable_share: float | None
    gross_profit_rub: float | None
    commission_rub: float | None
    net_profit_rub: float | None
    counted: int


def span_totals(rows: Sequence[TradeRow]) -> SpanTotals:
    """Сложить готовые числа сделок участка. Ничего не пересчитывать.

    Складываются два столбика, которые уже посчитал прогон: `profit_rub`
    и `commission_rub`. Ни цены, ни объёма, ни стороны сделки здесь нет —
    прибыль считает `engine/`, и второй её версии в окне не заводится.

    Валовая получается обратным ходом того самого равенства, которым прогон
    получил чистую: «чистая = валовая − комиссия» (`DOMAIN.md` §5). Это
    перестановка слагаемых в уже посчитанном, а не второй расчёт прибыли;
    совпадение с `backtest.summarise` на одних и тех же сделках стережёт
    отдельная проверка.

    Три случая с комиссией разведены, потому что означают разное:

    * тариф известен у всех — `profit_rub` у всех чистый, известны все три
      числа;
    * тариф не известен ни у одной — `profit_rub` у всех валовый, известна
      валовая, а чистой нет. `None`, а не ноль: ноль объявил бы участок
      окупившим комиссию;
    * тариф известен у части — в одном столбике вперемешку чистые
      и валовые, складывать нечего. Пусты все три числа, и разбор говорит
      об этом словами.

    ⚠️ **Доля прибыльных в смешанном случае тоже пуста**, и это не мелочь.
    Она считалась по тому же смешанному столбику: сделка с валовыми +10 ₽
    попадала в «прибыльные», хотя комиссия обеих сторон на контракт — 28 ₽,
    то есть на счёте это убыток. Слово «прибыльная» в двух половинах столбика
    означает разное, и доля по такой смеси — число ни о чём.

    В однородных случаях доля остаётся: при заданном тарифе она считается
    по чистым, при незаданном — по валовым, то есть по той же базе, что
    и показанные рядом деньги.
    """
    results: list[float] = []
    fees: list[float | None] = []
    for row in rows:
        if row.profit_rub is None:
            continue
        results.append(row.profit_rub)
        fees.append(row.commission_rub)
    if not results:
        return SpanTotals(len(rows), None, None, None, None, 0)

    share = sum(1 for value in results if value > 0) / len(results)
    total = sum(results)
    known = [fee for fee in fees if fee is not None]
    if len(known) == len(fees):
        commission = sum(known)
        return SpanTotals(len(rows), share, total + commission, commission, total, len(results))
    if not known:
        return SpanTotals(len(rows), share, total, None, None, len(results))
    # Смешанный случай: ни денег, ни доли — считать не из чего. Разбор
    # говорит об этом словами (`_summary_notes`).
    return SpanTotals(len(rows), None, None, None, None, len(results))


def span_summary(facts: SpanFacts) -> SpanTotals | None:
    """Итог участка или `None`, если складывать нечего: сделок здесь нет."""
    picked = trades_in_span(facts.span, facts.trades)
    return span_totals(picked) if picked else None


def _summary_notes(facts: SpanFacts, totals: SpanTotals) -> list[str]:
    """Оговорки к итогу: чего в нём не хватает и почему.

    Каждая строка появляется, только когда её случай наступил. Пустая
    оговорка на каждый разбор приучает не читать оговорки вовсе.
    """
    notes = []
    if totals.counted < totals.trades:
        notes.append(
            f"   Итог посчитан по {totals.counted} сделкам из {totals.trades}: "
            "по остальным прогон результата не передал."
        )
    if totals.net_profit_rub is None and totals.gross_profit_rub is not None:
        notes.append(
            "   Чистой прибыли нет: тариф комиссии не задан, и во что обошёлся "
            "участок — неизвестно. Валовая прибыль результатом не считается."
        )
    if totals.gross_profit_rub is None and totals.counted:
        notes.append(
            "   Деньги не сложены и доля прибыльных не посчитана: тариф комиссии "
            "задан не у всех сделок участка, и чистые результаты лежат в одном "
            "столбике с валовыми. «Прибыльная» в этих двух половинах означает "
            "разное — сделка с валовым плюсом бывает убыточной после комиссии."
        )
    if facts.span.holes:
        notes.append(
            "   Итог — по тем сделкам, что есть: внутри участка пропуск данных "
            "(о нём ниже отдельной строкой)."
        )
    return notes


def _summary_lines(facts: SpanFacts) -> list[str]:
    """Итог участка одной строкой — до списка сделок, а не под ним.

    Разбор открывают ради ответа «сколько», а не ради чтения восьми абзацев:
    «тут не хватает итоговой цифры — профит или проёб» (владелец счёта,
    05.09.2026). Список сделок остаётся ниже как расшифровка.

    Слова и порядок — те же, что в итоговой строке под журналом сделок,
    и берутся из одной таблицы. Два разных набора слов про одни и те же
    деньги были бы новой путаницей вместо снятой.

    Пустота называется словами, а не нулём: «0,00 ₽» — это утверждение
    «участок вышел в ноль», и в трёх случаях ниже такого утверждения у нас
    нет. Случаи разные и названы по-разному, потому что означают разное:

    * сделок в окно не передавали вовсе — прогона ещё не было;
    * сделки есть, но не в этом участке;
    * сделки есть, а результат по ним прогон не передал.

    Два краевых случая итог не отменяют. **Пропуск данных** внутри участка:
    итог считается по тому, что есть, и оговорка об этом стоит тут же.
    **Часть участка вне торгового окна**: особым случаем это не является —
    сделок там нет по построению, и затенение уже говорит об этом своей
    строкой.
    """
    if not facts.trades:
        return ["Итог участка: сделок в окно ещё не передавали — складывать нечего."]
    totals = span_summary(facts)
    if totals is None:
        return ["Итог участка: в этом участке сделок нет — складывать нечего."]
    if not totals.counted:
        return [
            f"Итог участка: сделок здесь {totals.trades}, но результата по ним "
            "прогон не передал — складывать нечего."
        ]
    line = "Итог участка — " + " · ".join(summary_parts(totals, SPAN_FIELDS))
    return [line, *_summary_notes(facts, totals)]


def _head_lines(facts: SpanFacts) -> list[str]:
    """Заголовок: какой отрезок времени разбираем.

    Дата у конца отрезка появляется, только если он в другом дне: в обычном
    случае она повторяла бы начало и мешала бы читать, а на участке через
    ночь без неё непонятно, о каком дне речь.
    """
    since, until = to_msk(facts.span.start), to_msk(facts.span.end)
    tail = fmt_time(until) if since.date() == until.date() else fmt_datetime(until)
    return [f"Разбор участка {fmt_datetime(since)} — {tail} МСК"]


def _hole_lines(facts: SpanFacts) -> list[str]:
    """Пропуски данных внутри участка — отдельной строкой и вслух.

    «Данных нет» и «сделок не было» — разные утверждения, и второе, сказанное
    вместо первого, есть ровно то, за что заведён `B-005`.
    """
    return [
        f"⚠ В участок попал пропуск данных: {fmt_time(since)}–{fmt_time(until)} МСК. "
        "Свечей за это время нет — это не «сделок не было»."
        for since, until in facts.span.holes
    ]


def _quiet_lines(facts: SpanFacts) -> list[str]:
    """Затенённое время внутри участка: там робот молчал, и сказано почему.

    Ответ на первый вопрос, который возникает у пустого участка: «почему
    здесь ничего не происходило». Причина уже нарисована затенением, здесь
    она названа словами.
    """
    since, until = to_msk(facts.span.start), to_msk(facts.span.end)
    return [
        f"Затенено: {shade.note or shade.kind.label} "
        f"({fmt_time(shade.start)}–{fmt_time(shade.end)} МСК) — там робот "
        "сделок не совершает"
        for shade in facts.shades
        if to_msk(shade.start) < until and to_msk(shade.end) > since
    ]


def _trade_section(facts: SpanFacts) -> list[str]:
    """Сделки участка — со всеми числами, которые пришли из прогона.

    Три случая пустоты названы по-разному, и это не педантизм: «сделок
    не передавали вовсе», «сделки есть, но не здесь» и «здесь пропуск данных»
    означают разное, а молчание вместо любого из них читается как «сделок
    не было».
    """
    if not facts.trades:
        return [
            "Сделок в окно ещё не передавали: разбор появится после прогона "
            "на истории или после первой сделки робота."
        ]
    picked = trades_in_span(facts.span, facts.trades)
    if not picked:
        return ["В этом участке сделок нет."]
    lines = [f"Сделок в участке: {len(picked)}"]
    for number, row in enumerate(picked, start=1):
        lines.extend(trade_lines(number, row))
    return lines


def _marker_section(facts: SpanFacts) -> list[str]:
    """Метки участка вместе с их готовой подсказкой.

    Подсказка метки уже содержит сравнение расчёта с исполнением — расчётную
    цену, фактическую и расхождение в пунктах и рублях (`app/convert.py`).
    Считает это тот, кто знает шаг цены и стоимость пункта; здесь оно только
    показывается. Ради этих строк разбор и открывают: они отвечают на вопрос
    «почему сделка вышла такой», а не «какая она была».
    """
    since, until = to_msk(facts.span.start), to_msk(facts.span.end)
    picked = [m for m in facts.markers if since <= to_msk(m.time) < until]
    if not picked:
        return []
    lines = ["Метки на этом участке:"]
    for marker in picked:
        head = (
            f"   {marker.kind.label} · {fmt_time(marker.time)} МСК · "
            f"{fmt_level(marker.price)}"
        )
        lines.append(f"{head} · {marker.layer.label.lower()}")
        lines.extend(f"      {part}" for part in (marker.tooltip or "").splitlines() if part)
    return lines


#: Разделы разбора по порядку. Список, а не пять вызовов подряд: порядок
#: разделов — это данные, и добавление раздела не требует помнить о втором
#: месте, где перечислены все остальные.
DIGEST_SECTIONS = (
    _head_lines,
    _summary_lines,
    _hole_lines,
    _quiet_lines,
    _trade_section,
    _marker_section,
)


def digest_lines(
    span: ChartSpan,
    trades: Sequence[TradeRow],
    markers: Sequence[Marker] = (),
    shades: Sequence[Shade] = (),
) -> list[str]:
    """Разбор выделенного отрезка — готовыми строками для человека.

    Ничего не считает. Причина выхода, цены, результат, комиссия и сравнение
    расчёта с фактом приходят готовыми из прогона; здесь они отбираются
    по времени и раскладываются словами.
    """
    facts = SpanFacts(span, tuple(trades), tuple(markers), tuple(shades))
    lines: list[str] = []
    for section in DIGEST_SECTIONS:
        lines.extend(section(facts))
    return lines


def trade_lines(number: int, row: TradeRow) -> list[str]:
    """Одна сделка словами: сторона, объём, вход, выход, причина, результат.

    Прочерк там, где числа нет, и рядом сказано, почему его нет. Молчаливый
    пропуск строки читался бы как «этого не было», хотя на деле «этого
    не передали» — разные вещи, и вторая означает недоделку, а не сделку.
    """
    lines = [
        f"{number}. {row.side.label} · {fmt_volume(row.volume)} — "
        f"вход {fmt_datetime(row.entry_time)} по {fmt_level(row.entry_price)}",
    ]
    if row.exit_time is None:
        lines.append("   Выход: позиция ещё открыта")
    else:
        lines.append(
            f"   Выход: {fmt_datetime(row.exit_time)} по {fmt_level(row.exit_price)}"
            f" · держали {fmt_span(row.entry_time, row.exit_time)}"
        )
    lines.append(f"   Почему вышли: {row.exit_reason or 'причина не передана'}")
    lines.append(_result_line(row))
    if row.origin is not None:
        # Происхождение прогона называет тот, кто его затеял (`app/convert.py`).
        # Не назвал — строки нет: «происхождение не названо» в каждой сделке
        # заняло бы место и не сказало бы ничего.
        lines.append(f"   Откуда: {row.origin.label}")
    if row.trade_id:
        lines.append(f"   Заявка у брокера: {row.trade_id}")
    return lines


def _result_line(row: TradeRow) -> str:
    """Строка результата сделки. Числа взяты как есть, ни одно не выводится."""
    if row.profit_rub is None:
        return "   Результат: — (прогон его не передал)"
    percent = (
        f" ({fmt_percent(row.profit_pct, sign=True)})" if row.profit_pct is not None else ""
    )
    commission = (
        f" · комиссия {fmt_money(row.commission_rub)}"
        if row.commission_rub is not None
        else " · комиссия не передана: тариф не задан"
    )
    return f"   Результат: {fmt_money(row.profit_rub, sign=True)}{percent}{commission}"


def fmt_span(since: datetime, until: datetime) -> str:
    """Сколько времени прошло между двумя отметками: «1 ч 15 мин».

    ⚠️ Вычитание здесь — то же самое действие, которое человек делает глазами
    по двум часам на экране, и разобрано оно теми же признаками, что вычитание
    закрытий в строке сведений (`ui/models.py::CandleInfo`): ни объёма, ни
    стороны, ни комиссии, ни одного торгового правила. Прибыль, процент
    и комиссию считает `engine/` и приносит готовыми.
    """
    minutes = int((to_msk(until) - to_msk(since)).total_seconds() // 60)
    if minutes < 0:
        return EMPTY
    hours, rest = divmod(minutes, 60)
    return f"{hours} ч {rest} мин" if hours else f"{rest} мин"


class TradeDigest(QWidget):
    """Всплывающее окно с разбором выделенного участка.

    Всплывающее (`Qt.Popup`), а не панель под графиком: разбор нужен в ответ
    на действие мышью и тут же закрывается. Панель отняла бы высоту у графика
    навсегда ради того, что смотрят несколько секунд.

    Закрывается очевидно и без кнопок: щелчок мимо окна и клавиша Esc —
    и то и другое даёт Qt всплывающему окну само. Окно прижимается к экрану,
    чтобы разбор сделки у правого края графика не уехал за его границу.
    """

    def __init__(self, parent: QWidget | None = None, theme: Theme | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Popup)
        self._theme = theme if theme is not None else current_theme()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.StyledPanel)
        self.body = QLabel()
        self.body.setTextFormat(Qt.TextFormat.RichText)
        self.body.setWordWrap(True)
        self.body.setMargin(10)
        self.body.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._scroll.setWidget(self.body)
        layout.addWidget(self._scroll)
        self.set_theme(self._theme)

    def set_theme(self, theme: Theme) -> None:
        self._theme = theme
        paint_background(self, theme)
        paint_background(self.body, theme)
        self.body.setStyleSheet(f"color: {theme.text};")

    def show_span(self, facts: SpanFacts, at: QPoint) -> None:
        """Показать разбор отрезка рядом с точкой `at` (координаты экрана)."""
        lines = digest_lines(facts.span, facts.trades, facts.markers, facts.shades)
        head = lines[0]
        rest = "<br>".join(_escape(line).replace("   ", "&nbsp;&nbsp;&nbsp;") for line in lines[1:])
        self.body.setText(_stress_net(f"<b>{_escape(head)}</b><br>{rest}", facts, self._theme))
        self.body.adjustSize()
        width = min(max(self.body.sizeHint().width() + 30, 320), DIGEST_WIDTH)
        height = min(self.body.heightForWidth(width - 30) + 26, DIGEST_HEIGHT)
        self.resize(width, max(height, 90))
        self.move(_fit_on_screen(at, self.size().width(), self.size().height()))
        self.show()


def _stress_net(markup: str, facts: SpanFacts, theme: Theme) -> str:
    """Выделить чистую прибыль участка среди пяти одинаковых чисел.

    Ради неё разбор и открывают, а в ряду «Сделок · Прибыльных · Валовая ·
    Комиссия · Чистая прибыль» она ничем не отличается от соседей.

    Кусок, который выделяется, строится тем же вызовом `SummaryField.part`,
    что и сама строка. Разойтись со строкой он поэтому не может: не совпало —
    ничего не выделено, а не выделено чужое место.

    Цвет — по знаку, как в итоге под журналом сделок и в панели состояния:
    `success` на плюсе, `danger` на минусе. Знак при этом остаётся буквой
    внутри числа (`fmt_result`): цвет здесь второй носитель смысла, а не
    единственный, иначе минус исчезает на чёрно-белой печати и для того,
    кто плохо различает красное и зелёное.
    """
    totals = span_summary(facts)
    if totals is None or totals.net_profit_rub is None:
        return markup
    fragment = _escape(summary_field("net_profit_rub").part(totals.net_profit_rub))
    if fragment not in markup:
        return markup
    colour = theme.success if totals.net_profit_rub >= 0 else theme.danger
    return markup.replace(fragment, f'<b style="color:{colour}">{fragment}</b>', 1)


def _escape(text: str) -> str:
    """Текст сделки в разметку. Экранируется, потому что приходит из прогона."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fit_on_screen(at: QPoint, width: int, height: int) -> QPoint:
    """Сдвинуть окно так, чтобы оно целиком осталось на экране.

    Разбор сделки у правого края графика иначе уезжает за границу экрана —
    вместе с половиной чисел, ради которых его открыли.
    """
    # `screenAt` возвращает `None`, когда точка вне всех экранов — например,
    # окно перетащили за край. Тогда берётся главный экран: он есть всегда,
    # пока есть само приложение Qt.
    screen = QGuiApplication.screenAt(at) or QGuiApplication.primaryScreen()
    area = screen.availableGeometry()
    if area.isEmpty():
        return at
    x = min(max(at.x() + 12, area.left()), max(area.right() - width, area.left()))
    y = min(max(at.y() + 12, area.top()), max(area.bottom() - height, area.top()))
    return QPoint(int(x), int(y))


class ChartPanel(QWidget):
    """График со строкой сведений, легендой, слоями меток и разбором сделок."""

    def __init__(self, parent: QWidget | None = None, surface: ChartSurface | None = None) -> None:
        super().__init__(parent)
        self._theme: Theme = current_theme()
        if surface is None:
            self._surface, self.renderer_note = create_surface(self)
        else:
            self._surface, self.renderer_note = surface, f"График: {surface.name}"

        #: Сделки прогона — те же строки, что в журнале под графиком.
        #: Панель их не считает и не меняет: отбирает по времени для разбора.
        self._trades: list[TradeRow] = []
        #: Метки и затенение — то же самое, что ушло в отрисовку. Держатся
        #: здесь, чтобы разбор участка мог показать сравнение расчёта с фактом
        #: и назвать словами, почему на участке тихо. Своей второй правды
        #: панель не заводит: что нарисовано, то и разбирается.
        self._markers: dict[Layer, list[Marker]] = {Layer.PLAN: [], Layer.FACT: []}
        self._shades: list[Shade] = []
        #: Последняя свеча ряда и закрытие предыдущей. Не расчёт и не выбор:
        #: панель запоминает то, что через неё же и прошло, чтобы строке
        #: сведений было что показывать, пока курсор не на графике.
        self._tail: CandleInfo | None = None
        #: Курсор стоит на свече. Пока стоит — обновления ряда строку не трогают,
        #: иначе живая свеча перебивала бы то, что человек разглядывает.
        self._hovered = False

        self.info_bar = CandleInfoBar(theme=self._theme)
        self.legend = LegendPanel(self._surface, self._theme)
        self.digest = TradeDigest(self, self._theme)
        self._build_controls()
        self._listen_to_surface()
        self._build_layout()
        self._paint_strip()

    def _build_controls(self) -> None:
        """Галочки слоёв и кнопки верхней полосы — вместе с их подсказками.

        Подсказка есть у каждого управляющего элемента и написана обычными
        словами: программой пользуется владелец счёта, а не разработчик.
        """
        self.plan_box = QCheckBox("Прогноз")
        # Включена: с графика ничего не убираем (докстринг модуля, пункт 2).
        self.plan_box.setChecked(True)
        self.plan_box.setToolTip(
            "Где робот должен был войти и выйти по расчёту.\n"
            "Полые метки и пунктир поверх обычных: видно, насколько исполнение\n"
            "разошлось с расчётом.\n"
            "Снимите галочку, если хотите видеть только то, что произошло."
        )
        self.fact_box = QCheckBox("Факт")
        self.fact_box.setChecked(True)
        self.fact_box.setToolTip(
            "Где сделка произошла на самом деле: цена исполнения у брокера\n"
            "и фактическое время."
        )
        self.plan_box.toggled.connect(self._toggle_plan)
        self.fact_box.toggled.connect(lambda on: self._surface.set_layer_visible(Layer.FACT, on))

        self.legend_button = QPushButton("Что на графике ▴")
        self.legend_button.setCheckable(True)
        self.legend_button.setChecked(True)
        self.legend_button.setToolTip(
            "Показать или спрятать объяснение всех значков графика.\n"
            "Спрятанная легенда возвращает место графику."
        )
        self.legend_button.toggled.connect(self._toggle_legend)

        self.last_button = QPushButton("К последней свече")
        self.last_button.setToolTip(
            "Вернуть график к правому краю и дальше следовать за новыми свечами."
        )
        self.last_button.clicked.connect(self._surface.scroll_to_last)
        self._layers_caption = QLabel("Слои меток:")

    def _listen_to_surface(self) -> None:
        """Подписаться на то, что отрисовщик знает, а панель — нет.

        Где на экране какая свеча и какие места оси попали под выделение,
        знает только он. Панель по координатам ничего не ищет —
        см. `ChartSurface.set_hover_handler` и `set_span_handler`.
        """
        self._surface.set_hover_handler(self.show_candle_info)
        self._surface.set_span_handler(self.show_span)
        # Состояние галочек раздаётся сразу, а не только при переключении:
        # у отрисовщика все слои видимы по умолчанию, у легенды — наоборот,
        # и обе стороны обязаны узнать, как оно на самом деле. Иначе легенда
        # молча объясняет не то, что нарисовано, — а именно на это
        # и жаловался владелец счёта.
        self._toggle_plan(self.plan_box.isChecked())
        self._surface.set_layer_visible(Layer.FACT, self.fact_box.isChecked())

    def _build_layout(self) -> None:
        """Сверху вниз: полоса управления, легенда, строка сведений, график."""
        #: Верхняя полоса. Публичная, как и остальные части панели: нарисованное
        #: в ней — всё её видимое состояние, и мерить контраст больше негде
        #: (`D-016`).
        self.strip = QWidget()
        paint_background(self.strip, self._theme)
        top = QHBoxLayout(self.strip)
        top.setContentsMargins(8, 4, 8, 4)
        top.addWidget(self._layers_caption)
        top.addWidget(self.plan_box)
        top.addWidget(self.fact_box)
        top.addStretch(1)
        top.addWidget(self.legend_button)
        top.addWidget(self.last_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.strip)
        layout.addWidget(self.legend)
        layout.addWidget(self.info_bar)
        layout.addWidget(self._surface.widget(), 1)

    # ------------------------------------------------------------------ данные

    def show_chart(self, data: ChartData) -> None:
        self._surface.show_chart(data)
        for layer in (Layer.PLAN, Layer.FACT):
            self._markers[layer] = [m for m in data.markers if m.layer is layer]
        self._shades = list(data.shades)
        self._set_tail(tail_info(data.candles))

    def append_candle(self, candle: Candle) -> None:
        self._surface.append_candle(candle)
        self._set_tail(CandleInfo.from_pair(candle, self._tail.candle if self._tail else None))

    def update_last_candle(self, candle: Candle) -> None:
        self._surface.update_last_candle(candle)
        # Свеча та же самая, она ещё растёт: сравнивать её по-прежнему
        # с закрытием предыдущей, а не с самой собой минуту назад.
        self._set_tail(CandleInfo(candle, self._tail.previous_close if self._tail else None))

    def set_average(self, points: Sequence[LinePoint], label: str = "") -> None:
        self._surface.set_average(points, label)

    def set_markers(self, layer: Layer, markers: Sequence[Marker]) -> None:
        self._surface.set_markers(layer, markers)
        self._markers[layer] = list(markers)

    def set_paths(self, layer: Layer, paths: Sequence[TradePath]) -> None:
        self._surface.set_paths(layer, paths)

    def set_levels(self, levels: Sequence[PriceLevel]) -> None:
        self._surface.set_levels(levels)

    def set_shades(self, shades: Sequence[Shade]) -> None:
        self._surface.set_shades(shades)
        self._shades = list(shades)

    def set_trades(self, trades: Sequence[TradeRow]) -> None:
        """Сделки прогона — для разбора выделенного участка.

        Те же строки, что в журнале сделок под графиком. Панель их не считает
        и не переупорядочивает: одна правда на два места на экране.
        """
        self._trades = list(trades)

    def append_trade(self, trade: TradeRow) -> None:
        """Ещё одна сделка — робот закрыл позицию, пока окно открыто."""
        self._trades.append(trade)

    def scroll_to_last(self) -> None:
        self._surface.scroll_to_last()

    def set_theme(self, theme: Theme) -> None:
        self._theme = theme
        self._surface.set_theme(theme)
        self.info_bar.set_theme(theme)
        self.legend.set_theme(theme)
        self.digest.set_theme(theme)
        paint_background(self.strip, theme)
        self._paint_strip()

    # ------------------------------------------------------------------ вид

    def _paint_strip(self) -> None:
        """Перекрасить надписи верхней полосы под тему (`D-016`).

        Полоса — обычный виджет на системной подложке: свой фон она не рисует,
        пока ей не задана палитра, а надписи берут системный цвет. На тёмном
        окне это давало светлый рубец поперёк — видно на снимке
        `.docs/ui/hover-dark.png`. Контраст на нарисованной картинке меряет
        `tests/test_ui_legend.py`.
        """
        for label in (self._layers_caption,):
            label.setStyleSheet(f"color: {self._theme.text_dim};")
        for box in (self.plan_box, self.fact_box):
            box.setStyleSheet(f"color: {self._theme.text};")

    def _toggle_plan(self, on: bool) -> None:
        """Слой «Прогноз» включили или выключили: график и легенда идут вместе."""
        self._surface.set_layer_visible(Layer.PLAN, on)
        self.legend.set_plan_visible(on)

    def _toggle_legend(self, on: bool) -> None:
        """Свернуть или развернуть легенду. Стрелка на кнопке показывает, куда."""
        self.legend.setVisible(on)
        self.legend_button.setText("Что на графике ▴" if on else "Что на графике ▾")

    # ------------------------------------------------------------------ сведения о свече

    def show_candle_info(self, info: CandleInfo | None) -> None:
        """Показать сведения о свече под курсором. **Это точка привязки наведения.**

        `None` — курсор ушёл с графика или встал мимо свечи: строка возвращается
        к последней свече ряда и говорит об этом словами (см. `CandleInfoBar`).

        Метод публичный именно для этого: отрисовщик, поймавший наведение,
        собирает `CandleInfo` из свечи под курсором и предыдущей и отдаёт сюда.
        Ни выбора свечи, ни поиска по координатам панель не делает.
        """
        self._hovered = info is not None
        if info is None:
            self._show_tail()
        else:
            self.info_bar.show_candle(info, hovered=True)

    def show_span(self, span: ChartSpan | None) -> None:
        """Показать разбор выделенного участка. **Это точка привязки выделения.**

        `None` — выделение пришлось на поле за пределами ряда: разбирать нечего,
        и окно не открывается. Пустое окно в ответ на промах читалось бы как
        «сделок не было», хотя на том месте нет и оси.
        """
        if span is None:
            self.digest.hide()
            return
        self.digest.show_span(
            SpanFacts(span, tuple(self._trades), self._shown_markers(), tuple(self._shades)),
            QCursor.pos(),
        )

    def _shown_markers(self) -> tuple[Marker, ...]:
        """Метки видимых слоёв — те же, что человек видит на графике.

        Слой, снятый галочкой, в разбор не попадает: разбор объясняет
        нарисованное, а не всё, что известно программе.
        """
        shown: list[Marker] = []
        for layer, box in ((Layer.PLAN, self.plan_box), (Layer.FACT, self.fact_box)):
            if box.isChecked():
                shown.extend(self._markers[layer])
        return tuple(shown)

    def _set_tail(self, info: CandleInfo | None) -> None:
        self._tail = info
        self._show_tail()

    def _show_tail(self) -> None:
        """Показать последнюю свечу ряда, если курсор не занят разглядыванием."""
        if self._hovered:
            return
        if self._tail is None:
            self.info_bar.clear()
        else:
            self.info_bar.show_candle(self._tail, hovered=False)
