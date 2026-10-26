"""Наведение на свечу: строка сведений показывает ту свечу, что под курсором.

Просьба владельца счёта 04.09.2026, дословно: «при наведении на свечу я в БКС
вижу инфу о свечке — у нас, если есть эти данные, надо так же выводить
где-нибудь наверху». Строка сведений была сделана раньше, но никто не говорил
ей, над какой свечой мышь: она показывала хвост ряда всегда. Владелец счёта
посмотрел на экран и сказал «свеча есть, но не при наведении, а торчит
последняя; задача не выполнена полностью».

Что здесь стережётся, по одной фразе на проверку:

* строка показывает **ту** свечу, что под курсором, а не соседнюю;
* курсор над **пустым местом** пропуска — строка возвращается к хвосту
  и говорит об этом словами, соседняя свеча вместо пропуска не подставляется;
* изменение считается против предыдущей **свечи**, а не предыдущего места;
* курсор ушёл с виджета — строка возвращается к хвосту;
* курсор за областью построения (поле, оси) — свечи там нет;
* подсказки меток сделок при наведении по-прежнему показываются;
* отслеживание мыши включено — без него наведения не бывает вовсе.

Проверки идут **через настоящую отрисовку и настоящее событие мыши**:
`ChartPanel` с `PainterChartSurface` внутри, событие `QMouseEvent`, отправленное
виджету, и чтение того, что написано в надписях. Именно `PainterChartSurface`
и запускается на машине владельца счёта — с 09.09.2026 он единственный
(решение 0056), — поэтому проверять подстановкой вместо отрисовщика здесь
нельзя: зелёный на подставном отрисовщике ничего не говорит о том,
что человек увидит на экране.

Координаты точек считает сам тест, своей арифметикой по размерам виджета,
а не спрашивает у графика: проверка, берущая место свечи у того же кода,
который его и назначает, останется зелёной при любом одинаковом сдвиге.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from helpers import settle_qt
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QImage, QMouseEvent
from PySide6.QtWidgets import QApplication, QToolTip

from ui.chart.painter_surface import (
    AXIS_HEIGHT,
    AXIS_WIDTH,
    DEFAULT_VISIBLE,
    MIN_VISIBLE,
    PADDING,
    PainterChartSurface,
)
from ui.chart.timeline import Timeline
from ui.chart_panel import LAST_CANDLE_NOTE, ChartPanel
from ui.formatting import MSK
from ui.models import Candle, ChartData, Layer, Marker, MarkerKind

DAY = datetime(2026, 6, 19, tzinfo=MSK)


def _at(hour: int, minute: int) -> datetime:
    """Момент московского дня. Пояс явный: наивных времён здесь нет."""
    return DAY.replace(hour=hour, minute=minute)


def _candle(moment: datetime, close: float) -> Candle:
    """Свеча с четырьмя **разными** ценами: одинаковые скрыли бы путаницу колонок."""
    return Candle(opens_at=moment, open=close - 50, high=close + 100, low=close - 150, close=close)


#: Непрерывный ряд: двенадцать пятиминуток 13:00–13:55, закрытия все разные.
#: Разные закрытия и есть то, чем ловится сдвиг на свечу: у ряда с одинаковыми
#: ценами соседняя свеча неотличима от нужной.
SOLID = tuple(
    _candle(_at(13, 5 * number), 226_000.0 + 100 * number) for number in range(12)
)

#: Ряд из записи `B-005`: три пятиминутки, час без данных, ещё три.
#: На оси это 3 + 12 пустых мест + 3 = 18 мест.
HOLE = (
    _candle(_at(13, 0), 226_000.0),
    _candle(_at(13, 5), 226_100.0),
    _candle(_at(13, 10), 226_200.0),
    _candle(_at(14, 15), 226_800.0),
    _candle(_at(14, 20), 226_900.0),
    _candle(_at(14, 25), 227_000.0),
)

SOLID_PLACES = 12
HOLE_PLACES = 18
#: Место в середине часового пропуска. Пустые места занимают номера 3…14.
INSIDE_THE_HOLE = 8


def _thousands(value: int) -> str:
    """`226000` → `226 000` с неразрывным пробелом.

    Считается здесь, а не вызовом `fmt_level`: ожидание, посчитанное тем же
    кодом, который проверяется, остаётся верным при любой его поломке.
    """
    return f"{value:,}".replace(",", " ")


def _pair(line: Timeline, place: int) -> tuple[Candle, Candle | None]:
    """Свеча этого места и предыдущая. Пустое место здесь — падение проверки."""
    found = line.pair_at(place)
    assert found is not None, f"на месте {place} оси свечи не оказалось"
    return found


def _chart(candles: tuple[Candle, ...]) -> ChartData:
    return ChartData(instrument="MXU6", timeframe="5 минут", candles=candles)


@pytest.fixture()
def panel(qapp):
    """Панель графика с **настоящей** отрисовкой Qt — той, что запускается.

    Убирает за собой по-настоящему: удаление не откладывается на чужой
    цикл событий, см. `settle_qt`.
    """
    made = ChartPanel(surface=PainterChartSurface())
    made.resize(1000, 520)
    made.show()
    qapp.processEvents()
    yield made
    made.deleteLater()
    settle_qt(qapp)


@pytest.fixture()
def the_panel_must_not_outlive_the_test():
    """Сторож уборки: после разбора фикстуры `panel` панели уже нет.

    ⚠️ Стоит в подписи теста **перед** `panel` намеренно: `pytest` разбирает
    фикстуры в обратном порядке, значит эта проверка исполняется уже после
    уборки панели. Поменять местами — и она посмотрит на живую панель,
    то есть не проверит ничего.
    """
    gone: list[str] = []
    yield gone
    assert gone == ["панель удалена"], (
        "показанная панель пережила свой тест. Разрушится она в первом же "
        "чужом цикле событий — а для теста, который цикл крутит, это "
        "«закрылось последнее окно» и выход из цикла на середине корутины"
    )


def test_the_shown_panel_does_not_outlive_its_own_test(
    the_panel_must_not_outlive_the_test, panel, qapp
) -> None:
    """Панель удаляется здесь, а не в чужом тесте.

    Проверяется не «фикстура зовёт уборку», а видимое следствие: панель
    показана, значит она — «последнее окно» для Qt, и место её разрушения
    определяет, кто из соседей упадёт. Замер 06.09.2026: восемь тестов
    из десяти в этом файле оставляли `ChartPanel` живой и видимой, а
    падал от этого `tests/test_ui_history_load.py`.

    Мутация: вернуть в конец фикстуры `panel` голый `qapp.processEvents()`
    вместо `settle_qt(qapp)` — прогон обязан покраснеть на разборе фикстуры.
    """
    panel.destroyed.connect(
        lambda *_: the_panel_must_not_outlive_the_test.append("панель удалена")
    )
    assert panel.isVisible(), (
        "панель не показана — для Qt она не окно, и проверка вакуумна"
    )
    assert panel in qapp.topLevelWidgets(), "панель не самостоятельное окно"


def _surface(panel: ChartPanel) -> PainterChartSurface:
    drawn = panel._surface  # noqa: SLF001 — предмет проверки в том, что рисует именно она
    assert isinstance(drawn, PainterChartSurface)
    return drawn


def _centre(surface: PainterChartSurface, place: int, places: int) -> QPointF:
    """Середина места оси в координатах виджета — арифметика теста.

    Ряд, влезающий в окно обзора целиком, начинается с места 0 и занимает
    всю ширину области построения: `span` равен числу мест, левый край — ноль.
    Границы этого допущения проверяются вызывающей стороной.
    """
    left = float(PADDING)
    width = max(surface.width() - PADDING - AXIS_WIDTH, 1)
    height = max(surface.height() - PADDING - AXIS_HEIGHT, 1)
    step = width / places
    return QPointF(left + (place + 0.5) * step, PADDING + height / 2)


def _fits(places: int) -> None:
    """Ряд влезает в окно обзора целиком — иначе арифметика `_centre` неверна."""
    assert MIN_VISIBLE <= places <= DEFAULT_VISIBLE, (
        f"{places} мест не влезают в окно обзора: точки посчитаны не там"
    )


def _move(surface: PainterChartSurface, point: QPointF) -> None:
    """Движение мыши — настоящим событием через `QWidget.event()`."""
    event = QMouseEvent(
        QEvent.Type.MouseMove,
        point,
        QPointF(point),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(surface, event)


def _leave(surface: PainterChartSurface) -> None:
    QApplication.sendEvent(surface, QEvent(QEvent.Type.Leave))


def _shown(panel: ChartPanel) -> tuple[str, str, str, str]:
    """Что написано в строке сведений: время, закрытие, изменение, примечание."""
    return (
        panel.info_bar.moment.text(),
        panel.info_bar.values["close"].text(),
        panel.info_bar.change.text(),
        panel.info_bar.note.text(),
    )


# ----------------------------------------------------------------- сама ось

def test_an_empty_place_of_the_axis_holds_no_candle_at_all() -> None:
    """Пустое место оси не отдаёт соседнюю свечу — оно не отдаёт ничего.

    Нижнее звено: если ось согласится подменить пропуск соседом, врать будут
    обе отрисовки сразу.
    """
    line = Timeline(HOLE)

    assert len(line) == HOLE_PLACES, f"ось разметила {len(line)} мест вместо {HOLE_PLACES}"
    assert line.pair_at(INSIDE_THE_HOLE) is None, (
        "внутри часового пропуска ось выдала свечу: рядом с подписью "
        "«Данных нет» окажутся её цены"
    )
    assert _pair(line, 2)[0] is HOLE[2], "место 2 отдало не ту свечу"
    assert _pair(line, 2)[1] is HOLE[1], "предыдущей названа не соседняя слева свеча"
    assert _pair(line, 0)[1] is None, "у первой свечи ряда нашлась предыдущая"
    assert line.pair_at(len(line)) is None, "место за краем оси отдало свечу"
    assert line.pair_at(-1) is None, "отрицательное место отдало свечу с конца"


def test_the_candle_after_a_gap_is_compared_with_the_candle_before_it() -> None:
    """Предыдущая — предыдущая **свеча ряда**, а не предыдущее место оси.

    У свечи сразу за часовым пропуском предыдущее место пусто. Сравнивать
    с ним нечего, и «изменение» превратилось бы в прочерк на каждой свече
    после каждого пропуска.
    """
    line = Timeline(HOLE)
    candle, previous = _pair(line, 15)

    assert candle is HOLE[3], "место 15 — это первая свеча после пропуска"
    assert previous is HOLE[2], (
        "предыдущей для свечи после пропуска названа не та, что стояла до него"
    )


# ------------------------------------------------- наведение: до самых надписей

def test_the_line_shows_the_candle_the_cursor_stands_on(panel) -> None:
    """Под курсором свеча — в строке её цены, её время и никакого примечания.

    Проверяются **все** свечи ряда подряд: сдвиг на одну свечу даёт верную
    картинку на любой отдельно взятой точке, если проверять одну.
    """
    _fits(SOLID_PLACES)
    surface = _surface(panel)
    panel.show_chart(_chart(SOLID))

    for number, candle in enumerate(SOLID):
        _move(surface, _centre(surface, number, SOLID_PLACES))
        moment, close, _, note = _shown(panel)
        assert close == _thousands(int(candle.close)), (
            f"под курсором свеча {candle.opens_at:%H:%M} (закрытие "
            f"{candle.close:.0f}), а в строке {close!r}"
        )
        assert moment == f"{candle.opens_at:%d.%m.%Y %H:%M} МСК", (
            f"время в строке ({moment!r}) не от той свечи, что под курсором"
        )
        assert note == "", "свеча под курсором названа последней свечой ряда"


def test_the_cursor_over_an_empty_place_gets_no_candle_at_all(panel) -> None:
    """Курсор в часовом пропуске: строка возвращается к хвосту и говорит это.

    Соседняя свеча здесь — прямая неправда: рядом на графике нарисована
    подпись «Данных нет: 13:15–14:10» (`B-005`). Проверка не вакуумна:
    рядом стоит наведение на соседнюю свечу, и оно показывает именно её.
    """
    _fits(HOLE_PLACES)
    surface = _surface(panel)
    panel.show_chart(_chart(HOLE))
    tail, before_hole = HOLE[-1], HOLE[2]

    # Свеча слева от пропуска — видна, и это она.
    _move(surface, _centre(surface, 2, HOLE_PLACES))
    assert _shown(panel)[1] == _thousands(int(before_hole.close)), (
        "наведение промахнулось мимо свечи перед пропуском: проверка ниже "
        "ничего не докажет"
    )

    _move(surface, _centre(surface, INSIDE_THE_HOLE, HOLE_PLACES))
    moment, close, _, note = _shown(panel)

    assert note == LAST_CANDLE_NOTE, (
        "внутри пропуска строка показывает свечу и молчит о том, что курсор "
        "стоит не на ней"
    )
    assert close == _thousands(int(tail.close)), (
        f"внутри пропуска показано закрытие {close!r} — это не хвост ряда, "
        "а подставленная соседняя свеча"
    )
    assert moment == f"{tail.opens_at:%d.%m.%Y %H:%M} МСК", (
        "внутри пропуска названо время не хвоста ряда"
    )
    assert close != _thousands(int(before_hole.close)), "показана свеча слева от пропуска"
    assert close != _thousands(int(HOLE[3].close)), "показана свеча справа от пропуска"


def test_the_change_after_a_gap_is_counted_from_the_candle_before_it(panel) -> None:
    """Свеча за пропуском сравнивается со свечой до пропуска, а не с пустотой."""
    _fits(HOLE_PLACES)
    surface = _surface(panel)
    panel.show_chart(_chart(HOLE))

    _move(surface, _centre(surface, 15, HOLE_PLACES))
    _, close, change, _ = _shown(panel)

    assert close == _thousands(int(HOLE[3].close)), "под курсором не первая свеча за пропуском"
    # 226 800 − 226 200 = +600, это +0,27% от 226 200.
    assert change == "+600 (+0,27%)", (
        f"изменение против свечи до пропуска посчитано как {change!r}"
    )


def test_the_cursor_leaving_the_widget_returns_the_line_to_the_tail(panel) -> None:
    """Мышь ушла с графика — строка снова про последнюю свечу, и подписана.

    Без этого строка навсегда осталась бы на свече, над которой мышь была
    последний раз, и утверждала бы, что человек разглядывает её.
    """
    _fits(SOLID_PLACES)
    surface = _surface(panel)
    panel.show_chart(_chart(SOLID))

    _move(surface, _centre(surface, 3, SOLID_PLACES))
    assert _shown(panel)[1] == _thousands(int(SOLID[3].close)), "наведение не сработало вовсе"

    _leave(surface)
    _, close, _, note = _shown(panel)
    assert close == _thousands(int(SOLID[-1].close)), "после ухода мыши осталась чужая свеча"
    assert note == LAST_CANDLE_NOTE, "хвост ряда не назван последней свечой"


def test_the_cursor_outside_the_plot_area_stands_over_no_candle(panel) -> None:
    """Поле, ось цены и ось времени — не график: свечи там нет.

    Иначе движение мыши по оси цены переставляло бы строку на крайнюю свечу,
    как будто на неё навели.
    """
    _fits(SOLID_PLACES)
    surface = _surface(panel)
    panel.show_chart(_chart(SOLID))
    _move(surface, _centre(surface, 5, SOLID_PLACES))
    assert _shown(panel)[3] == "", "наведение не сработало: проверка ниже вакуумна"

    inside = _centre(surface, SOLID_PLACES - 1, SOLID_PLACES)
    for name, point in (
        ("ось цены справа", QPointF(surface.width() - AXIS_WIDTH / 2, inside.y())),
        ("ось времени снизу", QPointF(inside.x(), surface.height() - AXIS_HEIGHT / 2)),
        ("поле сверху", QPointF(inside.x(), PADDING / 2)),
    ):
        _move(surface, point)
        assert _shown(panel)[3] == LAST_CANDLE_NOTE, (
            f"курсор над «{name}» засчитан как наведение на свечу"
        )


def test_hovering_is_possible_at_all_without_a_pressed_button(panel) -> None:
    """Отслеживание мыши включено.

    Без него события движения приходят только с нажатой кнопкой: наведение
    в окне не сработает ни разу, а любая проверка, посылающая событие сама,
    останется зелёной.
    """
    assert _surface(panel).hasMouseTracking(), (
        "виджет графика не отслеживает мышь: наведения без нажатой кнопки не будет"
    )


# ------------------------------------------- подсказки меток сделок не сломаны

MARKED_PLACE = 4
OTHER_PLACE = 8


def _markers() -> tuple[Marker, ...]:
    return (
        Marker(
            time=SOLID[MARKED_PLACE].opens_at,
            price=SOLID[MARKED_PLACE].close,
            kind=MarkerKind.ENTRY_LONG,
            layer=Layer.FACT,
            tooltip="Вход в лонг по 226 400",
        ),
        Marker(
            time=SOLID[OTHER_PLACE].opens_at,
            price=SOLID[OTHER_PLACE].close,
            kind=MarkerKind.EXIT,
            layer=Layer.FACT,
            tooltip="Выход по тейку 226 800",
        ),
    )


def _tooltips_down_the_column(surface: PainterChartSurface, x: float) -> set[str]:
    """Какие подсказки показались, пока курсор шёл сверху вниз по столбцу.

    Столбец целиком, а не одна точка: где именно по высоте стоит метка,
    считает отрисовка (`_marker_offset`, `_y_of_price`), и спрашивать это
    у неё же — значит проверять её саму собой.
    """
    QToolTip.showText(QPoint(0, 0), "подсказки не было")
    seen = {QToolTip.text()}
    top, bottom = PADDING, surface.height() - AXIS_HEIGHT
    for y in range(int(top), int(bottom)):
        _move(surface, QPointF(x, float(y)))
        seen.add(QToolTip.text())
    return seen


def test_the_marker_tooltips_still_appear_on_hover(panel, qapp) -> None:
    """Наведение на метку сделки по-прежнему показывает её подсказку.

    Крючок наведения один на оба дела — подсказки меток и строку сведений, —
    и правка ради второго ломает первое молча: подсказка не показывается,
    а прогон остаётся зелёным.
    """
    _fits(SOLID_PLACES)
    surface = _surface(panel)
    panel.show_chart(_chart(SOLID))
    panel.set_markers(Layer.FACT, _markers())
    # Точки меток отрисовка запоминает во время рисования: без картинки
    # попадать курсором не во что.
    surface.render(QImage(surface.width(), surface.height(), QImage.Format.Format_RGB32))

    entry, exit_ = _markers()
    for marker, place in ((entry, MARKED_PLACE), (exit_, OTHER_PLACE)):
        seen = _tooltips_down_the_column(surface, _centre(surface, place, SOLID_PLACES).x())
        assert marker.tooltip in seen, (
            f"подсказка метки «{marker.kind.label}» не показалась ни в одной "
            f"точке её столбца: показывались {sorted(seen)}"
        )

    empty_column = _tooltips_down_the_column(surface, _centre(surface, 0, SOLID_PLACES).x())
    assert empty_column == {"подсказки не было"}, (
        f"над свечой без метки показалась подсказка: {sorted(empty_column)}"
    )
    qapp.processEvents()


def test_the_info_line_and_the_tooltips_do_not_shoulder_each_other_out(panel) -> None:
    """Наведение на метку заодно показывает и свечу под ней.

    Оба дела висят на одном движении мыши, и «работает то или другое» —
    самый вероятный способ сломать это местом.
    """
    _fits(SOLID_PLACES)
    surface = _surface(panel)
    panel.show_chart(_chart(SOLID))
    panel.set_markers(Layer.FACT, _markers())
    surface.render(QImage(surface.width(), surface.height(), QImage.Format.Format_RGB32))

    _tooltips_down_the_column(surface, _centre(surface, MARKED_PLACE, SOLID_PLACES).x())
    _, close, _, note = _shown(panel)

    assert close == _thousands(int(SOLID[MARKED_PLACE].close)), (
        "пока показывалась подсказка метки, строка сведений отстала от курсора"
    )
    assert note == "", "свеча под курсором названа последней свечой ряда"
