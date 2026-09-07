"""График за своим интерфейсом: смена темы не стирает содержимое.

Файл заведён под один дефект, найденный ревью 31.08.2026 и **сегодня
не стреляющий**. `WebChartSurface.set_theme()` звал `_reload()`: синхронное
чтение ~250 КБ библиотеки с диска и `setHtml` заново. После перезагрузки
страница пуста — свечи, метки и уровни ей больше никто не подаёт: `_pending`
очищается на первой загрузке, а повторной подачи нет ни в `set_theme`,
ни в `_on_loaded`. График восстанавливался только следующим полным
`chart_replaced`.

Не стреляет это потому, что `is_available()` отказывает, пока рядом нет
вендорного файла библиотеки, и веб-график вообще не создаётся. Включит дефект
не правка `ui/chart/`, а **выкладка библиотеки** — и там про него не вспомнят.
Поэтому проверка написана заранее и работает без QtWebEngine: она обходит
конструктор, который тянет и виджет, и файл с диска.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from typing import cast

import pytest
import synthetic

from ui.chart.timeline import Timeline
from ui.chart.web_surface import WebChartSurface
from ui.formatting import MSK
from ui.models import (
    Candle,
    ChartData,
    Layer,
    LevelKind,
    LinePoint,
    Marker,
    MarkerKind,
    PriceLevel,
)
from ui.theme import DARK, LIGHT


class Probe(WebChartSurface):
    """Веб-график без веб-движка: записывает вызовы вместо их отправки.

    Конструктор родителя намеренно не вызывается — он поднимает
    `QWebEngineView` и читает библиотеку с диска, а ни того, ни другого
    в прогоне нет. Проверяются настоящие `set_theme`, `set_levels`
    и `_push_markers`, а не их пересказ.
    """

    def __init__(self) -> None:  # родитель намеренно не зовётся
        self.scripts: list[str] = []
        self.reloads = 0
        self._theme = LIGHT
        self._ready = True
        self._pending = []
        self._visible = {Layer.PLAN: True, Layer.FACT: True}
        self._markers = {Layer.PLAN: [], Layer.FACT: []}
        self._levels = []
        self._following = True
        self._line = Timeline()
        self._chart_id = None

    def _run(self, script: str) -> None:
        self.scripts.append(script)

    def _reload(self) -> None:
        self.reloads += 1


@pytest.fixture()
def probe(qapp) -> Probe:
    return Probe()


def test_changing_the_theme_does_not_reload_the_page(probe) -> None:
    """Смена темы не перезагружает страницу — и потому ничего не стирает."""
    probe.set_theme(DARK)
    assert probe.reloads == 0, (
        "смена темы перезагрузила страницу: содержимое графика стёрто, "
        "а повторной подачи свечей нет"
    )
    assert any("terminal.setTheme(" in script for script in probe.scripts), (
        "цвета не переданы в страницу вовсе"
    )


def test_the_new_colours_actually_reach_the_page(probe) -> None:
    """В странице оказываются цвета новой темы, а не старой."""
    probe.set_theme(DARK)
    payload = next(
        json.loads(script[len("terminal.setTheme("):-1])
        for script in probe.scripts
        if script.startswith("terminal.setTheme(")
    )
    assert payload["background"] == DARK.background
    assert payload["bull"] == DARK.bull
    assert payload["bear"] == DARK.bear
    assert payload["average"] == DARK.average
    assert payload["grid"] == DARK.grid
    assert payload["text"] == DARK.text


def test_markers_and_levels_are_repainted_by_hand(probe) -> None:
    """Цвет меток и уровней считается в Python — страница их сама не перекрасит.

    `Theme.marker_color` и `Theme.level_color` живут в `ui/theme.py`, и после
    смены темы страница о них ничего не знает. Если их не подать заново,
    метки останутся в цветах прежней темы — на тёмном фоне это ровно та
    неразличимость лонга и шорта, ради которой тема вообще заведена.
    """
    data = synthetic.chart_data(count=40)
    probe.show_chart(data)
    probe.set_levels([PriceLevel(285_000.0, LevelKind.TAKE, "Тейк")])
    probe.scripts.clear()

    probe.set_theme(DARK)
    joined = "\n".join(probe.scripts)
    assert "terminal.setMarkers(" in joined, "метки не переданы заново"
    assert "terminal.addLevel(" in joined, "уровни не переданы заново"
    assert DARK.level_take in joined, "уровень остался в цвете прежней темы"
    assert DARK.long in joined or DARK.short in joined or DARK.plan in joined, (
        "метки остались в цветах прежней темы"
    )


def test_the_levels_are_remembered_between_pushes(probe) -> None:
    """Уровни держатся в объекте: иначе перекрашивать было бы нечего."""
    levels = [
        PriceLevel(285_000.0, LevelKind.TAKE, "Тейк"),
        PriceLevel(284_000.0, LevelKind.TRAILING, "Скользящий тейк"),
    ]
    probe.set_levels(levels)
    assert len(probe._levels) == 2

    probe.scripts.clear()
    probe._push_levels()
    joined = "\n".join(probe.scripts)
    assert joined.count("terminal.addLevel(") == 2
    assert "terminal.clearLevels()" in joined, (
        "уровни добавляются поверх прежних — на графике их станет вдвое больше"
    )


def test_the_page_knows_how_to_change_colours_without_reloading() -> None:
    """В самой странице есть, чем менять цвета: иначе вызов уходил бы в пустоту.

    Проверка по тексту страницы, а не по её работе: страница не исполняется
    ни в одном прогоне, пока рядом нет вендорного файла. Это не заменяет
    ручной прогон при выкладке библиотеки — он записан в заголовке
    `ui/chart/web_surface.py`.
    """
    from ui.chart.web_surface import PAGE

    assert "setTheme:" in PAGE, "у страницы нет способа сменить цвета на ходу"
    for call in ("chart.applyOptions", "candles.applyOptions", "average.applyOptions"):
        assert call in PAGE, f"смена цветов не доходит до {call}"


def test_the_probe_is_not_lying_about_the_real_class(probe) -> None:
    """Канарейка: подставка зовёт настоящий код, а не свой собственный.

    Если `set_theme` переедет в другое место или сменит имя, подставка
    продолжит «проходить» проверку, ничего не проверяя.
    """
    assert type(probe).set_theme is WebChartSurface.set_theme
    assert type(probe).set_levels is WebChartSurface.set_levels
    assert type(probe)._push_markers is WebChartSurface._push_markers
    assert WebChartSurface.is_available()[0] is False, (
        "вендорный файл появился — веб-график пора прогнать вручную, "
        "как записано в заголовке ui/chart/web_surface.py"
    )


# ----------------------------------------------------- ось запасного графика

def test_the_fallback_surface_lays_candles_out_by_their_axis_mark(qapp) -> None:
    """Запасной отрисовщик размечает ось теми же отметками, что и веб.

    Отрисовщиков два (`ui/chart/factory.py`), и правка одного молча оставляет
    другой со старым соглашением: на машине владельца счёта тогда починится
    не то, что он видит. Здесь проверяется именно запасной — по нему
    до 04.09.2026 не было ни одного теста, и переименование поля прошло бы
    мимо него до первого падения на отказе веб-графика.
    """
    from ui.chart.painter_surface import PainterChartSurface

    data = synthetic.chart_data(12)
    surface = PainterChartSurface()
    try:
        surface.show_chart(data)
        marks = list(surface._line.stamps)  # noqa: SLF001 — ось и есть предмет проверки
        assert marks == [candle.opens_at.timestamp() for candle in data.candles], (
            "запасной отрисовщик ставит свечи не по отметкам оси"
        )

        extra = data.candles[-1]
        surface.append_candle(extra)
        assert surface._line.stamps[-1] == extra.opens_at.timestamp()  # noqa: SLF001 — то же

        surface.update_last_candle(data.candles[0])
        assert surface._line.stamps[-1] == data.candles[0].opens_at.timestamp()  # noqa: SLF001
    finally:
        surface.deleteLater()
        qapp.processEvents()


# ------------------------------------------------ ось основного графика (веб)

WINDOW_DAY = datetime(2026, 6, 19, tzinfo=MSK)


def _at(hour: int, minute: int) -> datetime:
    """Момент московского дня фикстуры. Пояс явный, наивных времён здесь нет."""
    return WINDOW_DAY.replace(hour=hour, minute=minute)


def _stamp(hour: int, minute: int) -> int:
    """Отметка оси в секундах эпохи — считается мимо кода графика.

    Ожидаемая сторона строится из часа и минуты, а не из свечи, которую
    отдали графику: иначе проверка пересказала бы реализацию и осталась бы
    зелёной при любом одинаковом сдвиге всех рядов сразу.
    """
    return int(_at(hour, minute).timestamp())


def _times_sent(probe: Probe, call: str) -> list[int]:
    """Отметки оси, ушедшие в страницу последним вызовом `call`.

    Набор свечей и ряд средней приходят списком, одиночная свеча — словарём;
    и то и другое приводится к списку отметок, потому что предмет проверки
    один — где ряд встал на оси.

    Разбирается **первый** довод вызова, а не всё до закрывающей скобки:
    у `setCandles` их с 04.09.2026 два — данные и признак «тот же график».
    """
    script = next(s for s in reversed(probe.scripts) if s.startswith(call))
    payload, _ = json.JSONDecoder().raw_decode(script[len(call):])
    items = payload if isinstance(payload, list) else [payload]
    return [int(item["time"]) for item in items]


def _chart_around_thirteen() -> ChartData:
    """Четыре пятиминутки 12:45–13:05 и по одному ряду каждого вида.

    Числа взяты из записи B-008: владелец счёта сравнил с терминалом брокера
    крупную свечу 12:55–13:00 и увидел её у нас на 13:00.
    """
    candles = tuple(
        Candle(opens_at=_at(12, minute), open=100.0, high=101.0,
               low=99.0, close=100.5, volume=7.0)
        for minute in (45, 50, 55)
    ) + (Candle(opens_at=_at(13, 0), open=100.5, high=102.0,
                low=100.0, close=101.5, volume=8.0),)
    return ChartData(
        instrument="MXU6",
        timeframe="5 минут",
        candles=candles,
        average=(LinePoint(time=_at(12, 55), value=100.25),),
        average_label="EMA(15)",
        markers=(
            Marker(_at(12, 55), 100.5, MarkerKind.ENTRY_LONG, Layer.PLAN, "Расчёт"),
            Marker(_at(13, 0), 100.6, MarkerKind.ENTRY_LONG, Layer.FACT, "Лонг"),
        ),
    )


def test_the_web_surface_sends_every_series_on_the_axis_of_the_candles(probe) -> None:
    """В страницу уходят отметки **открытия**, и все ряды на одной оси.

    Это последний перевод времени перед библиотекой рисования: `_candle`,
    `set_average` и `_push_markers` переводят `datetime` в секунды эпохи,
    и дальше отвечает уже не Python. До 04.09.2026 по этому месту не было
    ни одной проверки — прибавка одного бара во всех трёх местах проходила
    полный прогон зелёной (мутация +300 секунд, замер 04.09.2026), хотя
    веб-график и есть то, что владелец счёта видит на своей машине.

    ⚠️ Проверка абсолютная, а не «время попало в набор отметок». Закрытие
    бара численно равно открытию следующего, поэтому сдвинутый на бар ряд
    тоже попадает на законную отметку оси — просто на чужую, и членством
    в наборе это не ловится.
    """
    probe.show_chart(_chart_around_thirteen())

    assert _times_sent(probe, "terminal.setCandles(") == [
        _stamp(12, 45), _stamp(12, 50), _stamp(12, 55), _stamp(13, 0)
    ], "свечи ушли в страницу не на отметках открытия — весь график сдвинут"

    assert _times_sent(probe, "terminal.setAverage(") == [_stamp(12, 55)], (
        "точка средней ушла в страницу не на своей свече"
    )

    markers = _times_sent(probe, "terminal.setMarkers(")
    assert markers == [_stamp(12, 55), _stamp(13, 0)], (
        "метки ушли в страницу не на своих свечах"
    )
    assert markers[1] - markers[0] == 300, (
        "расчёт и факт встали на одну свечу — наложение слоёв перестало "
        "отвечать на вопрос «факт правее по времени или нет»"
    )


def test_a_candle_added_one_by_one_keeps_the_same_axis_mark(probe) -> None:
    """Свеча, доехавшая по одной, стоит там же, где стояла бы в полном наборе.

    Путей в страницу три — полный набор, добавление и обновление последней, —
    и перевод времени в каждом свой. Живая свеча ходит вторым и третьим
    (`B-009`), то есть ровно теми, что не проверялись.
    """
    fresh = Candle(opens_at=_at(13, 0), open=100.5, high=102.0,
                   low=100.0, close=101.5, volume=8.0)

    probe.append_candle(fresh)
    assert _times_sent(probe, "terminal.updateCandle(") == [_stamp(13, 0)], (
        "добавленная свеча встала не на отметку своего открытия"
    )

    probe.update_last_candle(replace(fresh, close=101.0))
    assert _times_sent(probe, "terminal.updateCandle(") == [_stamp(13, 0)], (
        "обновление последней свечи переставило её на соседнюю отметку"
    )


# ------------------------------------------ ось запасного графика: что поверх

def test_a_moment_of_a_candle_lands_on_that_candles_place_not_the_next_one(qapp) -> None:
    """Метка на времени свечи рисуется над этой свечой, а не над соседней.

    Метки сделок, линии «вход → выход» и полосы затенения ставятся на запасном
    графике подбором места по оси (`_index_of_time`): готового времени
    у библиотеки здесь нет, есть только список отметок свечей. Сдвиг
    в этом подборе переставляет ВСЁ, что нарисовано поверх свечей, оставив
    сами свечи на месте, — то есть даёт ровно ту картинку, из-за которой
    заведён B-008, и при этом ни одна проверка свечей не краснеет
    (мутация +300 секунд, замер 04.09.2026).

    Проверка по номеру свечи, а не по «попал в диапазон»: сдвиг на бар
    остаётся внутри диапазона и потому диапазоном не ловится.
    """
    from ui.chart.painter_surface import PainterChartSurface

    data = synthetic.chart_data(12)
    surface = PainterChartSurface()
    try:
        surface.show_chart(data)
        for number, candle in enumerate(data.candles):
            place = surface._index_of_time(candle.opens_at)  # noqa: SLF001 — подбор места и есть предмет
            assert place == float(number), (
                f"свеча {candle.opens_at:%H:%M} стоит на месте {number}, "
                f"а всё, что нарисовано поверх неё, — на месте {place}"
            )

        # Момент внутри бара попадает между свечами, а не прыгает на соседнюю:
        # сделка исполняется внутри минуты, и метка факта обязана это показать.
        step = data.candles[4].opens_at - data.candles[3].opens_at
        middle = data.candles[3].opens_at + step / 2
        assert 3.0 < surface._index_of_time(middle) < 4.0  # noqa: SLF001 — то же
    finally:
        surface.deleteLater()
        qapp.processEvents()


def test_the_time_axis_is_signed_with_the_openings_of_the_candles(qapp) -> None:
    """Подписи на оси времени — те же открытия, что и у свечей.

    Подпись под свечой и есть то, что владелец счёта сравнивает с терминалом
    брокера: B-008 замечен именно так — «свеча стоит на 12:55 у брокера
    и на 12:56 у нас». Свечи и подписи считаются в двух разных местах
    (`show_chart` и `_draw_axes`), и до 04.09.2026 второе не проверялось
    ничем: подпись, съехавшая на бар, проходила полный прогон зелёной
    (мутация «подписать соседней свечой», замер 04.09.2026).

    Читается нарисованное, а не исходный текст: виджет рисуется в `QPicture`,
    и подписи достаются из записи вызовов. Текст в ней лежит UTF-16BE,
    поэтому поиск по байтам.
    """
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QPainter, QPicture

    from ui.chart.painter_surface import PainterChartSurface

    data = synthetic.chart_data(12)
    surface = PainterChartSurface()
    try:
        surface.resize(900, 500)
        surface.show_chart(data)
        picture = QPicture()
        painter = QPainter()
        painter.begin(picture)
        surface.render(painter, QPoint(0, 0))
        painter.end()
        # Заглушки PySide6 объявляют `QPicture.data()` возвращающим `object`;
        # на деле это `memoryview` с записью вызовов рисования.
        drawn_bytes = cast(memoryview, picture.data()).tobytes()
    finally:
        surface.deleteLater()
        qapp.processEvents()

    signed = {
        f"{hour:02d}:{minute:02d}"
        for hour in range(24)
        for minute in range(60)
        if f"{hour:02d}:{minute:02d}".encode("utf-16-be") in drawn_bytes
    }
    openings = {candle.opens_at.strftime("%H:%M") for candle in data.candles}
    step = data.candles[1].opens_at - data.candles[0].opens_at
    last_close = (data.candles[-1].opens_at + step).strftime("%H:%M")

    assert len(signed) >= 2, "на оси времени не подписано почти ничего"
    assert signed <= openings, (
        f"на оси подписано время, которым не открывается ни одна свеча: "
        f"{sorted(signed - openings)}"
    )
    assert data.candles[0].opens_at.strftime("%H:%M") in signed, (
        "крайняя левая подпись — не открытие первой свечи: вся ось сдвинута"
    )
    assert last_close not in signed, (
        f"на оси подписано закрытие последней свечи ({last_close}) — "
        "подписи размечены закрытиями, как было до B-008"
    )


# ------------------------------------- масштаб человека переживает перерисовку

def _zoomed(surface, *, span: int, follow: bool, right_shift: float = 0.0):
    """Поставить график в положение, в которое его привёл бы человек.

    Колесо и перетаскивание меняют ровно эти три поля (`wheelEvent`,
    `mouseMoveEvent`), и здесь они выставляются напрямую: предмет проверки —
    переживает ли положение перерисовку, а не то, как оно получено мышью.
    Отдельный тест на саму мышь есть в `tests/test_ui_window.py`.
    """
    surface._view.span = span  # noqa: SLF001 — окно обзора и есть предмет проверки
    surface._view.right += right_shift  # noqa: SLF001 — то же
    surface._view.follow = follow  # noqa: SLF001 — то же


def test_the_fallback_surface_keeps_the_zoom_of_a_man_who_scrolled_back(qapp) -> None:
    """Отмотал назад и приблизил — перерисовка оставляет его там же.

    Живая свеча приходит раз в минуту, и до 04.09.2026 каждая из них
    заканчивалась полной перерисовкой с пересозданием окна обзора: приближение,
    выставленное руками, жило до ближайшей минуты (`B-009`). Пункт приёмки Э2 —
    «два окна рядом, десять минут наблюдения» — при этом невыполним физически.

    Правый край сверяется **по времени свечи**, а не по её номеру: номера
    съезжают, как только у выборки сдвинулся левый край, и проверка по номеру
    осталась бы зелёной при уехавшем вбок графике.
    """
    from ui.chart.painter_surface import DEFAULT_VISIBLE, PainterChartSurface

    first = synthetic.chart_data(60)
    surface = PainterChartSurface()
    try:
        surface.show_chart(first)
        _zoomed(surface, span=18, follow=False, right_shift=-20.0)
        watched = surface._stamp_at(surface._view.right)  # noqa: SLF001 — то же

        grown = replace(first, candles=first.candles + (
            replace(first.candles[-1], opens_at=first.candles[-1].opens_at + synthetic.STEP),
        ))
        surface.show_chart(grown)

        assert surface._view.span == 18, (  # noqa: SLF001 — то же
            "перерисовка сбросила приближение: наблюдать за появлением свечи "
            "глазами нельзя"
        )
        assert surface._stamp_at(surface._view.right) == watched, (  # noqa: SLF001
            "перерисовка увезла график с того места, где стоял человек"
        )
        assert surface._view.span != DEFAULT_VISIBLE, (  # noqa: SLF001
            "проверка вакуумна: приближение совпало с умолчанием"
        )
    finally:
        surface.deleteLater()
        qapp.processEvents()


def test_the_fallback_surface_moves_on_for_a_man_standing_at_the_right_edge(qapp) -> None:
    """Стоял у правого края — новую свечу видит, и с тем же приближением.

    Слежение — режим, а не место: правый край обязан переехать на новую
    последнюю свечу. Сохранить вместо этого прежнюю точку значило бы, что
    новые свечи уходят за экран у того самого человека, который смотрит
    именно на них.
    """
    from ui.chart.painter_surface import PainterChartSurface

    first = synthetic.chart_data(60)
    surface = PainterChartSurface()
    try:
        surface.show_chart(first)
        _zoomed(surface, span=18, follow=True)

        fresh = replace(first.candles[-1], opens_at=first.candles[-1].opens_at + synthetic.STEP)
        surface.show_chart(replace(first, candles=first.candles + (fresh,)))

        assert surface._view.span == 18, "слежение съело приближение"  # noqa: SLF001
        assert surface._stamp_at(surface._view.right) == fresh.opens_at.timestamp(), (  # noqa: SLF001
            "правый край не переехал на новую свечу — она за экраном"
        )
        assert surface.is_following(), "режим слежения потерян перерисовкой"
    finally:
        surface.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("changed", [{"instrument": "SiU6"}, {"timeframe": "1 минута"}])
def test_the_fallback_surface_resets_the_view_when_the_chart_itself_changed(
    qapp, changed
) -> None:
    """Сменился инструмент или размер свечи — вид сбрасывается, и это верно.

    Приближение, оставшееся от MXU6 5м, на другом ряду показывает не то же
    место, а случайное. Проверка парная к двум предыдущим: без неё «сохранять
    всегда» прошло бы их обе.
    """
    from ui.chart.painter_surface import DEFAULT_VISIBLE, PainterChartSurface

    first = synthetic.chart_data(60)
    surface = PainterChartSurface()
    try:
        surface.show_chart(first)
        _zoomed(surface, span=18, follow=False, right_shift=-20.0)

        surface.show_chart(replace(first, **changed))

        assert surface._view.span == min(DEFAULT_VISIBLE, 60), (  # noqa: SLF001
            f"вид не сброшен на другом графике ({changed}): человек смотрит "
            "в случайное место чужого ряда"
        )
        assert surface.is_following(), "новый график открылся не у последней свечи"
    finally:
        surface.deleteLater()
        qapp.processEvents()


def test_the_web_surface_tells_the_page_whether_the_chart_is_the_same_one(probe) -> None:
    """Веб-поверхность говорит странице, сбрасывать ли вид, — и говорит правду.

    Отрисовщиков два, и по умолчанию работает **веб** (`ui/chart/factory.py`,
    `ORDER`). Правка одного запасного оставила бы владельца счёта ровно с тем
    же сбросом масштаба, что и был: у `B-008` это уже случилось.
    """
    first = synthetic.chart_data(40)

    probe.show_chart(first)
    assert _keep_flag(probe) is False, (
        "первый набор свечей пришёл с просьбой сохранить вид: сохранять нечего"
    )

    probe.show_chart(first)
    assert _keep_flag(probe) is True, (
        "тот же график перерисован со сбросом вида — приближение человека "
        "не переживёт ни одной живой свечи"
    )

    probe.show_chart(replace(first, timeframe="1 минута"))
    assert _keep_flag(probe) is False, "смена размера свечи не сбросила вид"

    probe.show_chart(replace(first, timeframe="1 минута", instrument="SiU6"))
    assert _keep_flag(probe) is False, "смена инструмента не сбросила вид"


def _keep_flag(probe: Probe) -> bool:
    """Второй довод последнего `setCandles`: сохранять ли вид страницы."""
    call = "terminal.setCandles("
    script = next(s for s in reversed(probe.scripts) if s.startswith(call))
    decoder = json.JSONDecoder()
    _, end = decoder.raw_decode(script[len(call):])
    rest = script[len(call) + end:].lstrip(", ").rstrip(")")
    flag = json.loads(rest)
    assert isinstance(flag, bool), f"признак «тот же график» не булев: {rest!r}"
    return flag


def test_the_page_knows_how_to_keep_the_view_across_a_full_redraw() -> None:
    """В самой странице есть, чем сохранить вид: иначе признак уходит в пустоту.

    Проверка по тексту страницы, а не по её работе: страница не исполняется
    ни в одном прогоне, пока рядом нет вендорного файла библиотеки. Это
    **не заменяет** ручной прогон при её выкладке — он записан в заголовке
    `ui/chart/web_surface.py`, и сохранение вида в этот список входит.
    """
    from ui.chart.web_surface import PAGE

    assert "getVisibleLogicalRange" in PAGE, "страница не снимает вид перед заменой"
    assert "setVisibleLogicalRange" in PAGE, "страница не возвращает вид после замены"


# ------------- живая свеча доезжает до самого графика, а не до края правки
#
# Три проверки ниже добавлены проходом сторожей 04.09.2026. Правка `B-009`
# перевела живую свечу с полного прогона на добавление — и весь путь этого
# добавления оказался без сторожа: `ChartPanel.append_candle`,
# `ChartPanel.update_last_candle` и `PainterChartSurface.append_candle`
# по отдельности заменялись на `return`, и полный прогон (1902 теста)
# оставался зелёным. График при этом переставал показывать живую свечу вовсе.


def test_the_fallback_surface_really_puts_a_live_candle_on_the_chart(qapp) -> None:
    """Прибавленная свеча появляется на графике, а уточнённая — меняется.

    Проверка **новой** свечой, а не последней из набора: с последней «ряд
    вырос» и «ряд не тронут» дают одну и ту же отметку на оси, и заглушка
    вместо прибавки проходит такую проверку зелёной (замер мутацией,
    04.09.2026).
    """
    from ui.chart.painter_surface import PainterChartSurface

    data = synthetic.chart_data(30)
    surface = PainterChartSurface()
    try:
        surface.show_chart(data)
        fresh = replace(
            data.candles[-1],
            opens_at=data.candles[-1].opens_at + synthetic.STEP,
            close=data.candles[-1].close + 500,
        )

        surface.append_candle(fresh)
        assert len(surface._line.candles) == 31, (  # noqa: SLF001 — ряд и есть предмет проверки
            "живая свеча до графика не доехала: ряд не вырос"
        )
        assert surface._line.candles[-1] == fresh, (  # noqa: SLF001 — то же
            "на месте живой свечи оказалась не она"
        )

        grown = replace(fresh, close=fresh.close + 700, high=fresh.high + 700)
        surface.update_last_candle(grown)
        assert len(surface._line.candles) == 31, (  # noqa: SLF001 — то же
            "уточнение последней свечи прибавило вторую"
        )
        assert surface._line.candles[-1].close == grown.close, (  # noqa: SLF001 — то же
            "растущая свеча на графике не растёт: цена осталась прежней"
        )
    finally:
        surface.deleteLater()
        qapp.processEvents()


def test_the_fallback_surface_carries_a_watching_man_onto_the_live_candle(qapp) -> None:
    """Стоящий у правого края видит прибавленную свечу, отмотавший — не сдвинут.

    Это вторая половина того же «слежение — режим, а не место», что и при
    полной перерисовке, только на пути прибавки: живая свеча приходит
    именно им (`B-009`). Без переезда правого края новая свеча появляется
    **за** видимой областью — то есть ровно там, где владелец счёта её
    и не увидит.
    """
    from ui.chart.painter_surface import PainterChartSurface

    data = synthetic.chart_data(40)
    fresh = replace(data.candles[-1], opens_at=data.candles[-1].opens_at + synthetic.STEP)

    watcher = PainterChartSurface()
    reader = PainterChartSurface()
    try:
        watcher.show_chart(data)
        _zoomed(watcher, span=15, follow=True)
        watcher.append_candle(fresh)
        place = watcher._index_of_stamp(fresh.opens_at.timestamp())  # noqa: SLF001 — ось
        assert watcher._first_index() <= place <= watcher._view.right, (  # noqa: SLF001 — то же
            f"живая свеча встала за видимой областью: свеча {place}, "
            f"видно {watcher._first_index()}…{watcher._view.right}"  # noqa: SLF001 — то же
        )

        reader.show_chart(data)
        _zoomed(reader, span=15, follow=False, right_shift=-12.0)
        stood = reader._view.right  # noqa: SLF001 — то же
        reader.append_candle(fresh)
        assert reader._view.right == stood, (  # noqa: SLF001 — то же
            "отмотавшего назад утащило вслед за живой свечой"
        )
    finally:
        for surface in (watcher, reader):
            surface.deleteLater()
        qapp.processEvents()


def test_the_chart_panel_hands_a_live_candle_over_to_the_surface(qapp) -> None:
    """Панель графика передаёт живую свечу отрисовщику, а не роняет на пол.

    Панель — единственное звено между окном и отрисовщиком, и до 04.09.2026
    оба её метода живой свечи можно было заменить на `return`, не уронив
    ни одного теста. Владелец счёта увидел бы график, замерший до ближайшего
    прогона, — то есть ровно то, от чего уходили.
    """
    from ui.chart.painter_surface import PainterChartSurface
    from ui.chart_panel import ChartPanel

    data = synthetic.chart_data(20)
    surface = PainterChartSurface()
    panel = ChartPanel(surface=surface)
    try:
        panel.show_chart(data)
        fresh = replace(
            data.candles[-1],
            opens_at=data.candles[-1].opens_at + synthetic.STEP,
            close=data.candles[-1].close + 400,
        )

        panel.append_candle(fresh)
        assert surface._line.candles[-1] == fresh, (  # noqa: SLF001 — ряд и есть предмет проверки
            "панель не передала прибавленную свечу отрисовщику"
        )

        panel.update_last_candle(replace(fresh, close=fresh.close + 900))
        assert surface._line.candles[-1].close == fresh.close + 900, (  # noqa: SLF001 — то же
            "панель не передала уточнение последней свечи отрисовщику"
        )
    finally:
        panel.deleteLater()
        qapp.processEvents()
