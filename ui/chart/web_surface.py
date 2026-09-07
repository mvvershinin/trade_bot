"""Отрисовка графика встроенным веб-графиком TradingView Lightweight Charts.

Основной вариант по ТЗ §6 и ARCHITECTURE.md §4. Работает так: в окно вставляется
`QWebEngineView`, в него загружается страница с библиотекой, данные передаются
вызовами JavaScript. Наружу торчит тот же `ChartSurface`, что и у отрисовки на Qt, —
окно не знает, какая из реализаций сейчас работает.

⚠️ Состояние на 30.08.2026: **не проверено ни одним запуском**
--------------------------------------------------------------
Библиотека `lightweight-charts.standalone.production.js` в репозиторий не положена,
скачивать её в ходе разработки запрещено. Пока файла нет, `is_available()` отвечает
отказом, и фабрика молча берёт отрисовку на Qt — но код ниже целиком написан
вслепую и обязан быть прогнан вручную, когда файл появится. Считать его рабочим
до этого нельзя.

Что нужно сделать, чтобы включить:

1. Положить `lightweight-charts.standalone.production.js` (лицензия Apache-2.0)
   в `ui/chart/vendor/`.
2. Добавить каталог в состав поставки: `[tool.setuptools.package-data]` в
   `pyproject.toml` и `--include-data-dir` для Nuitka — иначе из исходников
   график будет, а в собранной программе пропадёт.
3. Проверить на Wayland и на X11: QtWebEngine — отдельный процесс, и падение
   плагина платформы выглядит как пустой белый прямоугольник вместо графика.
4. Прогнать глазами **сохранение вида**: приблизить график руками, дождаться
   живой свечи и убедиться, что масштаб и место не уехали (`B-009`). В Python
   это проверено (`tests/test_ui_chart.py`), но `getVisibleLogicalRange`
   и `setVisibleLogicalRange` на странице не исполнялись ни разу — их поведение
   на границе ряда проверяется только руками.
5. Прогнать глазами **пропуск в данных** (`B-005`): открыть день, в котором
   час не догружен, и убедиться, что соседние свечи не встали встык. Пустое
   место здесь делается «whitespace data» — элементом с одним полем `time`;
   в Python это проверено (`tests/test_ui_chart_gaps.py`), но `candles.update`
   с таким элементом на странице не исполнялся ни разу.
6. Прогнать глазами **перекрестье**: оно здесь штатное, библиотечное, и ниже
   ему заданы наши цвета, тонкий штрих и подписи на обеих шкалах. У запасной
   отрисовки то же самое сделано вручную (`painter_surface._draw_crosshair`),
   и выглядеть они обязаны одинаково — сравнить рядом.
7. **Выделения участка правой кнопкой здесь нет.** Разбор сделок у запасной
   отрисовки открывается по правой кнопке; здесь мышь принадлежит странице,
   и вернуть выделение в Python нечем — тот же `QWebChannel`, что и у
   наведения (`D-014`). До того дня разбор сделок на веб-графике
   не работает; сказать об этом владельцу счёта, а не молчать.
8. **Значки легенды рисует Qt** (`ui/chart/glyphs.py`), а метки на странице —
   библиотека своими формами (`_shape`). Свести их глазами: значок легенды
   и метка графика обязаны быть одной фигурой, иначе легенда объясняет
   не то, что нарисовано.

Почему не CDN. Программа обязана работать без интернета: связь с брокером может
быть, а выход в сеть — закрыт корпоративной политикой. График, который тянет
скрипт из сети, в такой день просто не нарисуется.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from importlib import util as import_util
from pathlib import Path

from PySide6.QtWidgets import QVBoxLayout, QWidget

from ui.chart.protocol import ChartSurface, HoverHandler, SpanHandler
from ui.chart.timeline import Timeline
from ui.formatting import PRICE_FIELDS, to_msk
from ui.models import (
    Candle,
    ChartData,
    Layer,
    LinePoint,
    Marker,
    MarkerKind,
    PriceLevel,
    Shade,
    TradePath,
)
from ui.theme import Theme, current as current_theme

VENDOR = Path(__file__).resolve().parent / "vendor"
LIBRARY = VENDOR / "lightweight-charts.standalone.production.js"

# Страница держится строкой, а не отдельным файлом: файл пришлось бы отдельно
# протаскивать в сборку, и его пропажа выглядела бы как «график не открывается»
# только у заказчика.
PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<style>
  html, body {{ margin: 0; padding: 0; height: 100%; background: {background}; }}
  #chart {{ position: absolute; inset: 0; }}
  /* Строка сведений о свече лежит поверх графика и мышь не перехватывает:
     иначе верхняя полоса графика перестала бы отзываться на перетаскивание. */
  #legend {{
    position: absolute; left: 0; right: 0; top: 0; z-index: 3;
    padding: 3px 8px; font: 12px sans-serif; white-space: nowrap;
    overflow: hidden; pointer-events: none;
    background: {background}; color: {text};
  }}
</style>
<script>{library}</script>
</head><body><div id="chart"></div><div id="legend"></div>
<script>
const chart = LightweightCharts.createChart(document.getElementById('chart'), {{
  layout: {{ background: {{ color: '{background}' }}, textColor: '{text}' }},
  grid: {{ vertLines: {{ color: '{grid}' }}, horzLines: {{ color: '{grid}' }} }},
  timeScale: {{ timeVisible: true, secondsVisible: false }},
  localization: {{ locale: 'ru-RU' }},
  // Перекрестье за курсором — просьба владельца счёта 05.09.2026: «без кликов,
  // просто модернизированный курсор». Здесь оно штатное, библиотечное; наше
  // дело — тонкий штрих цветом подписей (тише свечей и меток) и подписи
  // значений на обеих шкалах: время внизу, цена справа.
  crosshair: {{
    mode: LightweightCharts.CrosshairMode.Normal,
    vertLine: {{ color: '{text_dim}', width: 1, style: 2, labelVisible: true }},
    horzLine: {{ color: '{text_dim}', width: 1, style: 2, labelVisible: true }},
  }},
}});
const candles = chart.addCandlestickSeries({{
  upColor: '{bull}', downColor: '{bear}',
  wickUpColor: '{bull}', wickDownColor: '{bear}', borderVisible: false,
}});
const average = chart.addLineSeries({{ color: '{average}', lineWidth: 2 }});
window._bars = 0;

// ------------------------------------------------------------------------
// Сведения о свече под курсором — штатным перекрестьем библиотеки.
//
// Просьба владельца счёта 04.09.2026: в терминале брокера наведение на свечу
// показывает сверху её цены, и у нас должно быть так же. У запасной отрисовки
// это делает `CandleInfoBar` над графиком; здесь строку рисует сама страница,
// потому что только она знает, где на экране какая свеча.
//
// ⚠️ Ни одна строка ниже не исполнялась ни разу: файла библиотеки на диске
// нет, `is_available()` отказывает, и веб-график не создаётся вовсе.
// Прогнать глазами вместе с остальными пунктами списка в шапке модуля.
//
// ⚠️ Наружу, в Python, наведение отсюда НЕ уходит: для этого нужен
// `QWebChannel`, и писать его вслепую опаснее, чем не писать. Следствие:
// при работающем веб-графике сведений будет две строки — эта и `CandleInfoBar`
// панели, которая покажет последнюю свечу ряда. Долг `D-014`.
window._series = [];
window._hover = false;
window._colors = {{ text: '{text}', dim: '{text_dim}', bull: '{bull}', bear: '{bear}' }};
const legend = document.getElementById('legend');
// Подписи и порядок полей приходят из Python одной таблицей
// (`ui.formatting.PRICE_FIELDS`) — той же самой, по которой собрана строка
// сведений запасной отрисовки. Второй список здесь означал бы два места,
// которые надо помнить, и расхождение, невидимое до выкладки библиотеки.
const FIELDS = {fields};

const pad2 = (n) => (n < 10 ? '0' + n : '' + n);
// МСК — постоянный сдвиг +03:00, как и в Python (`ui/formatting.py`):
// база часовых поясов для этого не нужна, промахнуться ею нельзя.
const msk = (t) => {{
  const d = new Date((t + 10800) * 1000);
  return pad2(d.getUTCDate()) + '.' + pad2(d.getUTCMonth() + 1) + '.' + d.getUTCFullYear()
    + ' ' + pad2(d.getUTCHours()) + ':' + pad2(d.getUTCMinutes());
}};
const num = (v) => v.toLocaleString('ru-RU', {{
  minimumFractionDigits: Number.isInteger(v) ? 0 : 2,
  maximumFractionDigits: Number.isInteger(v) ? 0 : 2,
}});
const signed = (v) => (v < 0 ? '\u2212' : (v > 0 ? '+' : '')) + num(Math.abs(v));
const isBar = (one) => !!one && one.close !== undefined;
const previousBar = (index) => {{
  // Предыдущая СВЕЧА, а не предыдущее место: у свечи сразу за пропуском
  // предыдущей считается та, что стояла до пропуска.
  //
  // ⚠️ Это же правило написано отдельно в Python (`ui/models.py::CandleInfo`,
  // `timeline.py::pair_at`): подписи полей у двух строк общие, числа — нет,
  // и одна свеча сможет показать два разных «изменения». Долг `D-015`.
  for (let i = index - 1; i >= 0; i--) {{
    if (isBar(window._series[i])) {{ return window._series[i]; }}
  }}
  return null;
}};
const cell = (caption, body, color) => '<span style="color:' + window._colors.dim + '">'
  + caption + '</span> <b style="color:' + color + '">' + body + '</b>&nbsp;&nbsp;';
const showBar = (index, hovered) => {{
  const bar = window._series[index];
  if (!isBar(bar)) {{ legend.innerHTML = ''; return; }}
  const previous = previousBar(index);
  // Изменение против закрытия предыдущей свечи. Прочерк, когда сравнивать
  // не с чем: ноль здесь был бы утверждением «цена не изменилась».
  const change = previous ? bar.close - previous.close : null;
  const color = (change === null || change === 0) ? window._colors.text
    : (change > 0 ? window._colors.bull : window._colors.bear);
  let text = cell('СВЕЧА', msk(bar.time) + ' МСК', window._colors.text);
  FIELDS.forEach((pair) => {{ text += cell(pair[0], num(bar[pair[1]]), color); }});
  let body = '\u2014';
  if (change !== null) {{
    body = signed(change);
    if (previous.close !== 0) {{
      body += ' (' + signed(change / previous.close * 100) + '%)';
    }}
  }}
  text += cell('ИЗМЕНЕНИЕ', body, color);
  if (!hovered) {{
    text += '<span style="color:' + window._colors.dim + '">последняя свеча</span>';
  }}
  legend.innerHTML = text;
}};
const showTail = () => {{
  for (let i = window._series.length - 1; i >= 0; i--) {{
    if (isBar(window._series[i])) {{ showBar(i, false); return; }}
  }}
  legend.innerHTML = '';
}};
const remember = (one) => {{
  const last = window._series[window._series.length - 1];
  if (last && last.time === one.time) {{ window._series[window._series.length - 1] = one; }}
  else {{ window._series.push(one); }}
}};
chart.subscribeCrosshairMove((param) => {{
  const index = param ? param.logical : null;
  // Курсор ушёл с графика либо встал на пустое место пропуска: свечи здесь
  // нет, и соседняя вместо неё не подставляется — рядом стоит подпись
  // «данных нет» (B-005). Строка возвращается к последней свече и говорит
  // об этом словами, как и `CandleInfoBar` запасной отрисовки.
  if (!param || !param.point || index === undefined || index === null
      || !isBar(window._series[index])) {{
    window._hover = false;
    showTail();
    return;
  }}
  window._hover = true;
  showBar(index, true);
}});

window.terminal = {{
  // `keep` — тот же график, только с новыми данными: приближение и место,
  // выставленные человеком, обязаны его пережить. `setData` сбрасывает шкалу
  // времени к содержимому, поэтому диапазон снимается до замены и ставится
  // обратно после (B-009). Стоял у правого края — диапазон сдвигается
  // на число прибавившихся свечей, иначе новые уходили бы за экран.
  setCandles: (data, keep) => {{
    const scale = chart.timeScale();
    const before = keep ? scale.getVisibleLogicalRange() : null;
    const was = window._bars;
    candles.setData(data);
    window._bars = data.length;
    window._series = data.slice();
    if (!window._hover) {{ showTail(); }}
    if (!before) {{ return; }}
    const shift = before.to >= was - 1 ? data.length - was : 0;
    scale.setVisibleLogicalRange({{ from: before.from + shift, to: before.to + shift }});
  }},
  updateCandle: (one) => {{
    candles.update(one);
    remember(one);
    // Пока курсор стоит на свече, живая свеча строку не перебивает:
    // человек разглядывает то, на что навёл.
    if (!window._hover) {{ showTail(); }}
  }},
  // Пустые места пропуска. У библиотеки это «whitespace data» — элемент
  // с одним полем `time` и без цен; тип ряда свечей объявлен как
  // `WhitespaceData | CandlestickData`, поэтому `update` принимает и его.
  // Другого способа показать разрыв нет: шкала времени здесь порядковая,
  // и без пустых мест соседние свечи встают встык (B-005).
  fillGap: (items) => items.forEach(one => {{ candles.update(one); remember(one); }}),
  setAverage: (data) => average.setData(data),
  setMarkers: (data) => candles.setMarkers(data),
  clearLevels: () => {{ (window._levels || []).forEach(l => candles.removePriceLine(l)); window._levels = []; }},
  addLevel: (options) => {{ window._levels = window._levels || []; window._levels.push(candles.createPriceLine(options)); }},
  scrollToLast: () => chart.timeScale().scrollToRealTime(),
  setTheme: (t) => {{
    document.body.style.background = t.background;
    // Строка сведений красится вместе с графиком: её цвета подобраны
    // к фону темы, а не к системному (B-013).
    window._colors = {{ text: t.text, dim: t.text_dim, bull: t.bull, bear: t.bear }};
    legend.style.background = t.background;
    legend.style.color = t.text;
    if (!window._hover) {{ showTail(); }}
    chart.applyOptions({{
      layout: {{ background: {{ color: t.background }}, textColor: t.text }},
      grid: {{ vertLines: {{ color: t.grid }}, horzLines: {{ color: t.grid }} }},
      crosshair: {{
        vertLine: {{ color: t.text_dim }},
        horzLine: {{ color: t.text_dim }},
      }},
    }});
    candles.applyOptions({{
      upColor: t.bull, downColor: t.bear,
      wickUpColor: t.bull, wickDownColor: t.bear,
    }});
    average.applyOptions({{ color: t.average }});
  }},
}};
</script></body></html>
"""


class WebChartSurface(QWidget, ChartSurface):
    """График в `QWebEngineView`. Данные уходят в страницу вызовами JavaScript."""

    name = "веб-график TradingView Lightweight Charts"

    @classmethod
    def is_available(cls) -> tuple[bool, str]:
        if import_util.find_spec("PySide6.QtWebEngineWidgets") is None:
            return False, "в этой сборке нет QtWebEngine — веб-график недоступен"
        if not LIBRARY.is_file():
            return False, (
                "рядом с программой нет файла библиотеки веб-графика "
                f"({LIBRARY.name}) — используется встроенная отрисовка"
            )
        return True, ""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from PySide6.QtWebEngineWidgets import QWebEngineView  # локально: модуль тяжёлый

        self._theme: Theme = current_theme()
        self._ready = False
        self._pending: list[str] = []
        self._visible: dict[Layer, bool] = {Layer.PLAN: True, Layer.FACT: True}
        self._markers: dict[Layer, list[Marker]] = {Layer.PLAN: [], Layer.FACT: []}
        # Уровни держатся здесь, потому что их цвет считается в Python по теме:
        # после смены темы страница о них ничего не знает и перекрасить их
        # некому. Метки — по той же причине, они уже лежат в `_markers`.
        self._levels: list[PriceLevel] = []
        self._following = True
        #: Кому рассказывать про свечу под курсором. Хранится, но не зовётся —
        #: разбор в `set_hover_handler`.
        self._hover_handler: HoverHandler | None = None
        self._span_handler: SpanHandler | None = None
        #: Ось: место под каждую свечу и под каждую пропущенную (`B-005`).
        #: Держится в Python, потому что решать, где на оси пропуск, обязан
        #: тот, кто видит времена свечей, — библиотека рисования их только
        #: подписывает.
        self._line = Timeline()
        #: Инструмент и размер свечи последнего показанного графика. `None` —
        #: не показано ничего: первый набор свечей сбрасывает вид всегда.
        self._chart_id: tuple[str, str] | None = None

        self._view = QWebEngineView(self)
        self._view.loadFinished.connect(self._on_loaded)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._view)
        self._reload()

    # ------------------------------------------------------------------ служебное

    def _reload(self) -> None:
        self._ready = False
        # Страница после перезагрузки пуста: сохранять на ней нечего, и первый
        # набор свечей обязан выставить вид заново.
        self._chart_id = None
        theme = self._theme
        self._view.setHtml(PAGE.format(
            library=LIBRARY.read_text(encoding="utf-8"),
            background=theme.background,
            text=theme.text,
            text_dim=theme.text_dim,
            grid=theme.grid,
            bull=theme.bull,
            bear=theme.bear,
            average=theme.average,
            fields=json.dumps(PRICE_FIELDS, ensure_ascii=False),
        ))

    def _on_loaded(self, ok: bool) -> None:
        self._ready = bool(ok)
        if not ok:
            return
        for script in self._pending:
            self._view.page().runJavaScript(script)
        self._pending.clear()

    def _run(self, script: str) -> None:
        """Отправить вызов в страницу.

        До готовности страницы вызовы копятся: первые свечи приходят раньше,
        чем QtWebEngine успевает поднять свой процесс, и потерять их — значит
        получить пустой график на старте.
        """
        if self._ready:
            self._view.page().runJavaScript(script)
        else:
            self._pending.append(script)

    # ------------------------------------------------------------------ данные

    def widget(self) -> QWidget:
        return self

    def show_chart(self, data: ChartData) -> None:
        """Полная замена содержимого. Масштаб переживает её, если график тот же.

        ⚠️ `candles.setData()` сбрасывает шкалу времени к содержимому, а через
        `show_chart` проходит **каждый** прогон: до 04.09.2026 приближение,
        выставленное руками, жило до ближайшей живой свечи (`B-009`).
        Признак «тот же график» считается здесь, в Python, а страница получает
        его вторым доводом `setCandles` — решать, сбрасывать ли вид, обязан
        тот, кто знает, что за данные пришли.

        «Тот же» — совпали инструмент и размер свечи. Сменился любой — на оси
        другие данные, и сброс к умолчанию верен.
        """
        same_chart = self._chart_id == (data.instrument, data.timeframe)
        self._chart_id = (data.instrument, data.timeframe)
        self._line = Timeline(data.candles)
        self._run(
            f"terminal.setCandles({json.dumps(_axis(self._line))}, "
            f"{json.dumps(same_chart)})"
        )
        self.set_average(data.average, data.average_label)
        self._markers = {
            Layer.PLAN: [m for m in data.markers if m.layer is Layer.PLAN],
            Layer.FACT: [m for m in data.markers if m.layer is Layer.FACT],
        }
        self._push_markers()
        self.set_levels(data.levels)
        self.set_shades(data.shades)

    def append_candle(self, candle: Candle) -> None:
        """Добавить свечу справа, а перед ней — места пропуска, если он был.

        Пропуск обязан появиться и на этом пути, а не только при полной
        перерисовке: живая свеча после обрыва связи приходит именно сюда,
        и без пустых мест библиотека поставит её вплотную к последней
        доехавшей — то есть склеит час, которого нет (`B-005`).
        """
        empty = self._line.append(candle)
        if empty:
            self._run(f"terminal.fillGap({json.dumps([{'time': int(s)} for s in empty])})")
        self._run(f"terminal.updateCandle({json.dumps(_candle(candle))})")

    def update_last_candle(self, candle: Candle) -> None:
        self._line.replace_last(candle)
        self._run(f"terminal.updateCandle({json.dumps(_candle(candle))})")

    def set_average(self, points: Sequence[LinePoint], label: str = "") -> None:
        payload = [{"time": int(to_msk(p.time).timestamp()), "value": p.value} for p in points]
        self._run(f"terminal.setAverage({json.dumps(payload)})")

    def set_markers(self, layer: Layer, markers: Sequence[Marker]) -> None:
        self._markers[layer] = list(markers)
        self._push_markers()

    def _push_markers(self) -> None:
        payload = []
        for layer in (Layer.PLAN, Layer.FACT):
            if not self._visible.get(layer, True):
                continue
            for marker in self._markers.get(layer, []):
                payload.append({
                    "time": int(to_msk(marker.time).timestamp()),
                    "position": "belowBar" if marker.kind is MarkerKind.ENTRY_LONG else "aboveBar",
                    "color": self._theme.marker_color(marker.kind, layer),
                    "shape": _shape(marker.kind, layer),
                    "text": marker.text or marker.kind.label,
                })
        payload.sort(key=lambda item: item["time"])
        self._run(f"terminal.setMarkers({json.dumps(payload)})")

    def set_paths(self, layer: Layer, paths: Sequence[TradePath]) -> None:
        # Линия «вход → выход» в Lightweight Charts делается отдельной серией
        # на каждую сделку. Реализуется вместе с включением веб-графика.
        return

    def set_levels(self, levels: Sequence[PriceLevel]) -> None:
        self._levels = list(levels)
        self._push_levels()

    def _push_levels(self) -> None:
        self._run("terminal.clearLevels()")
        for level in self._levels:
            self._run("terminal.addLevel(" + json.dumps({
                "price": level.price,
                "color": self._theme.level_color(level.kind),
                "lineStyle": 2,
                "axisLabelVisible": True,
                "title": level.label or level.kind.label,
            }) + ")")

    def set_shades(self, shades: Sequence[Shade]) -> None:
        # Затенение — гистограмма-подложка поверх шкалы времени.
        # Реализуется вместе с включением веб-графика.
        return

    # ------------------------------------------------------------------ вид

    def set_layer_visible(self, layer: Layer, visible: bool) -> None:
        self._visible[layer] = visible
        self._push_markers()

    def set_theme(self, theme: Theme) -> None:
        """Сменить цвета, **не** перезагружая страницу.

        ⚠️ Прежде здесь стоял `_reload()`, и это стирало содержимое графика.
        Перезагрузка читает с диска ~250 КБ библиотеки и заново вызывает
        `setHtml`; после неё страница пуста, а повторной подачи свечей, меток
        и уровней нет — `_pending` очищен на первой загрузке. График
        восстанавливался только следующим полным `chart_replaced`.

        Стреляло это не всегда: `is_available()` отказывает, пока рядом нет
        вендорного файла, и до его выкладки веб-график вообще не создаётся.
        Включила бы дефект не правка `ui/chart/`, а выкладка библиотеки.

        Вдобавок при автоматическом переключении системной темы окну приходят
        два события палитры подряд — было бы две синхронных перезагрузки
        в потоке интерфейса. Второе схлопывает `MainWindow.changeEvent`,
        первое больше не перезагружает ничего.
        """
        self._theme = theme
        self._run("terminal.setTheme(" + json.dumps({
            "background": theme.background,
            "text": theme.text,
            "text_dim": theme.text_dim,
            "grid": theme.grid,
            "bull": theme.bull,
            "bear": theme.bear,
            "average": theme.average,
        }) + ")")
        # Цвет меток и уровней считается в Python (`Theme.marker_color`,
        # `Theme.level_color`), поэтому страница их сама не перекрасит.
        self._push_markers()
        self._push_levels()

    def set_hover_handler(self, handler: HoverHandler | None) -> None:
        """Принять обработчик наведения и **не звать его**. Так и задумано.

        Сведения о свече под курсором эта отрисовка показывает сама, строкой
        поверх графика: перекрестье — штатная возможность библиотеки, и всё,
        что нужно для надписи, у страницы уже есть. Вернуть их в Python
        нечем: это направление требует `QWebChannel`, а писать его вслепую
        под библиотеку, которой нет на диске, — способ получить неработающий
        график вместо работающего.

        Обработчик всё равно запоминается: он понадобится в тот день, когда
        канал появится, и `getattr`-проверок «а умеет ли эта поверхность»
        в панели быть не должно.

        ⚠️ Следствие, которое вылезет в день выкладки библиотеки: строк
        сведений окажется две — эта, внутри страницы, и `CandleInfoBar`
        панели, застрявшая на последней свече ряда. Долг `D-014`
        в `.docs/BACKLOG.md`, там же названы оба способа починки.
        """
        self._hover_handler = handler

    def set_span_handler(self, handler: SpanHandler | None) -> None:
        """Принять обработчик выделения и **не звать его**. Здесь его нечем звать.

        Выделение участка правой кнопкой у запасной отрисовки открывает разбор
        сделок. Здесь мышь принадлежит странице, и вернуть её события в Python
        нечем — нужен `QWebChannel`, тот же самый, что и для наведения.
        Писать его вслепую, под библиотеку, которой нет на диске, — способ
        получить неработающий график вместо работающего.

        Обработчик запоминается: он понадобится в день, когда канал появится,
        а `getattr`-проверок «а умеет ли эта поверхность» в панели быть
        не должно.

        ⚠️ Следствие: на веб-графике разбор сделок не открывается вовсе.
        Сказать об этом владельцу счёта в день выкладки библиотеки, а не
        оставить его щёлкать правой кнопкой по молчащему графику.
        """
        self._span_handler = handler

    def scroll_to_last(self) -> None:
        self._following = True
        self._run("terminal.scrollToLast()")

    def is_following(self) -> bool:
        return self._following


def _axis(line: Timeline) -> list[dict[str, float | int]]:
    """Ряд для библиотеки: свечи и пустые места пропусков вперемешку.

    Пустое место — элемент с одним полем `time`. Так устроена «whitespace
    data» библиотеки: шкала времени у неё порядковая, свечи ложатся подряд
    независимо от своих отметок, и разрыв во времени показывается только
    тем, что место на оси занято, а цен на нём нет.
    """
    return [
        _candle(candle) if candle is not None else {"time": int(stamp)}
        for candle, stamp in zip(line.slots, line.stamps, strict=True)
    ]


def _candle(candle: Candle) -> dict[str, float | int]:
    """Свеча для библиотеки. Отметка — та же, что у оси (`Timeline`).

    ⚠️ `to_msk`, а не `timestamp()` напрямую: у наивного времени Python берёт
    системный пояс машины, а ось — московский. Свечи и пустые места пропуска
    уехали бы друг относительно друга на разницу поясов, и на немосковской
    машине метка сделки встала бы на чужую свечу молча.
    """
    return {
        "time": int(to_msk(candle.opens_at).timestamp()),
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
    }


def _shape(kind: MarkerKind, layer: Layer) -> str:
    """Форма метки. Слои различаются формой, а не только цветом."""
    if layer is Layer.PLAN:
        return "circle"
    if kind is MarkerKind.ENTRY_LONG:
        return "arrowUp"
    if kind is MarkerKind.ENTRY_SHORT:
        return "arrowDown"
    return "square"
