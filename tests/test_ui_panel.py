r"""Строка сведений о свече над графиком: числа, знак, цвет и «какая это свеча».

Просьба владельца счёта 04.09.2026 со снимком терминала брокера: при наведении
на свечу тот показывает сверху `ОТКР … МАКС … МИН … ЗАКР … −1 150 (−0,51%)`,
красным на падении и зелёным на росте. Здесь проверяется, что наши числа те же
и что подпись под ними не врёт.

Числа в ожиданиях записаны **буквально**, а не посчитаны теми же функциями,
которые проверяются: сторож, подающий на вход то же, что ждёт на выходе,
остаётся зелёным при замене проверяемого кода на заглушку. Разделитель тысяч
в проекте — неразрывный пробел (`ui/formatting.py`), поэтому в ожиданиях
он записан escape-последовательностью `\u00a0`, а не самим символом:
невидимый символ в ожидании читается как обычный пробел, и правка «поправил
пробел» превращает проверку в молча другую.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from ui.formatting import MSK
from ui.models import Candle, CandleInfo, ChartData
from ui.theme import DARK, LIGHT, contrast

STEP = timedelta(minutes=5)
START = datetime(2026, 6, 19, 10, 5, tzinfo=MSK)

#: Свеча из снимка терминала. Четыре цены **разные**: одинаковые не заметили бы
#: перепутанных местами колонок — а перепутать их можно одной строкой таблицы.
SHOWN = Candle(opens_at=START, open=226_650.0, high=226_900.0, low=225_525.0, close=225_550.0)


def bar(theme=None):
    """Строка сведений сама по себе, без графика под ней."""
    from ui.chart_panel import CandleInfoBar

    return CandleInfoBar(theme=theme)


def rendered(line) -> list[str]:
    """Что написано в строке слева направо — ровно так, как это видит человек.

    Читается разметка виджета, а не его внутренние словари: подпись, отвязанная
    от своего числа, в словаре не видна вовсе — ключ там остаётся верным,
    а рядом на экране стоит чужое слово.
    """
    from PySide6.QtWidgets import QLabel

    layout = line.layout()
    texts = []
    for index in range(layout.count()):
        widget = layout.itemAt(index).widget()
        if isinstance(widget, QLabel):
            texts.append(widget.text())
    return texts


def colour(label) -> str:
    """Цвет надписи из её таблицы стилей: `color: #26a69a;` → `#26a69a`."""
    style: str = label.styleSheet()
    assert style.startswith("color:"), f"у надписи не задан цвет вовсе: {style!r}"
    return style.removeprefix("color:").removesuffix(";").strip()


class FakeSurface:
    """Подставной график: принимает данные и молчит.

    Настоящий отрисовщик здесь не нужен — проверяется панель, а не рисование.
    Заодно тест не зависит от файлов отрисовки, которые правятся отдельно.
    """

    name = "подставной"

    def __init__(self) -> None:
        from PySide6.QtWidgets import QWidget

        self._widget = QWidget()
        self.candles: list[Candle] = []
        #: Кого панель подписала на наведение. Настоящий отрисовщик зовёт
        #: это сам при движении мыши; здесь оно вызывается вручную —
        #: предмет проверки в том, что панель показывает **сказанное ей**.
        self.hover_handler = None

    def widget(self):
        return self._widget

    def show_chart(self, data: ChartData) -> None:
        self.candles = list(data.candles)

    def append_candle(self, candle: Candle) -> None:
        self.candles.append(candle)

    def update_last_candle(self, candle: Candle) -> None:
        self.candles[-1] = candle

    def set_average(self, points, label="") -> None: ...
    def set_markers(self, layer, markers) -> None: ...
    def set_paths(self, layer, paths) -> None: ...
    def set_levels(self, levels) -> None: ...
    def set_shades(self, shades) -> None: ...
    def set_layer_visible(self, layer, visible) -> None: ...
    def set_theme(self, theme) -> None: ...

    def set_hover_handler(self, handler) -> None:
        self.hover_handler = handler

    def set_span_handler(self, handler) -> None:
        #: Кого панель подписала на выделение правой кнопкой. Здесь оно
        #: вызывается вручную: предмет проверки — что панель показывает
        #: **сказанное ей**, а не ищет что-то по координатам сама.
        self.span_handler = handler

    def draw_marker_glyph(self, painter, point, kind, layer, style) -> None:
        from ui.chart.glyphs import draw_marker

        draw_marker(painter, point, kind, layer, style)

    def scroll_to_last(self) -> None: ...
    def is_following(self) -> bool:
        return True


@pytest.fixture()
def panel(qapp):
    """Панель графика с подставным отрисовщиком."""
    from ui.chart_panel import ChartPanel

    made = ChartPanel(surface=FakeSurface())
    yield made
    made.deleteLater()
    qapp.processEvents()


def series(closes: list[float]) -> ChartData:
    """Ряд свечей с заданными закрытиями. Остальные цены сдвинуты от закрытия."""
    candles = tuple(
        Candle(
            opens_at=START + STEP * index,
            open=close - 100, high=close + 200, low=close - 300, close=close,
        )
        for index, close in enumerate(closes)
    )
    return ChartData(instrument="MXU6", timeframe="5 минут", candles=candles)


# ---------------------------------------------------------------- сами числа


def test_the_line_shows_every_price_of_the_candle_it_was_given(qapp) -> None:
    """Каждая цена дошла до своей ячейки и записана как у брокера.

    Четыре разные цены и четыре разных ожидания: одинаковые не заметили бы
    поля, взятого не у той цены. ⚠️ Отвязанную от числа **подпись** этот
    тест не видит — ячейки берутся по ключу поля. Её стережёт проверка
    всей строки целиком, ниже.
    """
    line = bar()
    line.show_candle(CandleInfo(SHOWN, previous_close=226_700.0))
    assert line.values["open"].text() == "226\u00a0650", "цена открытия не та"
    assert line.values["high"].text() == "226\u00a0900", "наибольшая цена не та"
    assert line.values["low"].text() == "225\u00a0525", "наименьшая цена не та"
    assert line.values["close"].text() == "225\u00a0550", "цена закрытия не та"
    line.deleteLater()
    qapp.processEvents()


def test_the_line_reads_left_to_right_like_the_broker_line(qapp) -> None:
    """Вся строка целиком: подписи, числа и порядок — как в терминале брокера.

    Снимок всей разметки, а не отдельных ячеек, и это единственное место,
    которое ловит **отвязанную подпись**: `("МАКС", "low")` в таблице полей —
    правка в один символ, после которой словарь ячеек остаётся правильным,
    все проверки по ключам зелёные, а над графиком написано «МАКС 225 525».

    Замер мутацией 04.09.2026: без этой проверки такая перестановка проходила
    прогон из девятнадцати тестов зелёной.
    """
    line = bar()
    line.show_candle(CandleInfo(SHOWN, previous_close=226_700.0))
    assert rendered(line) == [
        "СВЕЧА", "19.06.2026 10:05 МСК",
        "ОТКР", "226\u00a0650",
        "МАКС", "226\u00a0900",
        "МИН", "225\u00a0525",
        "ЗАКР", "225\u00a0550",
        "ИЗМЕНЕНИЕ", "−1\u00a0150 (−0,51%)",
        "",
    ], "строка над графиком выглядит не так, как у брокера"
    line.deleteLater()
    qapp.processEvents()


def test_the_price_table_covers_every_price_field_of_a_candle() -> None:
    """В строке показаны все цены свечи, а не те, о которых вспомнили.

    Таблица `PRICE_FIELDS` — единственное место, где перечислены колонки.
    Тест сверяет её с самой свечой: появится у свечи пятая цена — строка
    сведений о ней промолчит, и об этом скажут здесь, а не владелец счёта.
    """
    from ui.chart_panel import PRICE_FIELDS

    shown = {field for _, field in PRICE_FIELDS}
    prices = set(Candle.__dataclass_fields__) - {"opens_at", "volume"}
    assert shown == prices, (
        f"строка сведений показывает {sorted(shown)}, а у свечи цены {sorted(prices)}"
    )


def test_the_line_names_the_candle_it_shows_and_its_time_zone(qapp) -> None:
    """Слева стоит время начала свечи и пометка МСК.

    Без времени строка не отвечает на вопрос «а это про какую свечу»,
    а без пометки пояса — врёт на машине владельца счёта, стоящей не в Москве.
    """
    line = bar()
    line.show_candle(CandleInfo(SHOWN, previous_close=226_700.0))
    assert line.moment.text() == "19.06.2026 10:05 МСК", "строка не называет свою свечу"
    line.deleteLater()
    qapp.processEvents()


# ------------------------------------------------------- знак и величина


def test_a_falling_candle_shows_a_negative_change_in_points_and_percent(qapp) -> None:
    """Падение: `−1 150 (−0,51%)` и красный цвет.

    Числа те самые, что на снимке терминала: закрытие 225 550 против
    предыдущего 226 700.
    """
    line = bar(LIGHT)
    line.show_candle(CandleInfo(SHOWN, previous_close=226_700.0))
    assert line.change.text() == "−1\u00a0150 (−0,51%)", "изменение на падении посчитано не так"
    assert colour(line.change) == LIGHT.bear, "падение не окрашено в цвет падающей свечи"
    assert colour(line.values["close"]) == LIGHT.bear, "числа строки не окрашены вместе с ним"
    line.deleteLater()
    qapp.processEvents()


def test_a_rising_candle_shows_a_positive_change_in_points_and_percent(qapp) -> None:
    """Рост: `+1 150 (+0,51%)` и зелёный цвет. Знак «плюс» виден.

    Зеркало предыдущего теста и есть его смысл: перепутанный знак вычитания
    (`предыдущее − текущее` вместо `текущее − предыдущее`) на одном падении
    не виден, на паре — виден сразу.
    """
    line = bar(LIGHT)
    line.show_candle(CandleInfo(SHOWN, previous_close=224_400.0))
    assert line.change.text() == "+1\u00a0150 (+0,51%)", "изменение на росте посчитано не так"
    assert colour(line.change) == LIGHT.bull, "рост не окрашен в цвет растущей свечи"
    line.deleteLater()
    qapp.processEvents()


def test_a_first_candle_has_no_change_at_all_and_it_is_not_zero(qapp) -> None:
    """Первая свеча ряда: прочерк, а не ноль и не «+0,00%».

    Ноль здесь был бы утверждением «цена не изменилась» — утверждением,
    которого у нас нет: сравнивать не с чем. Цвет при этом обычный:
    зелёная или красная строка означала бы направление, которого не знаем.
    """
    line = bar(LIGHT)
    line.show_candle(CandleInfo(SHOWN, previous_close=None))
    assert line.change.text() == "—", f"вместо «неизвестно» показано {line.change.text()!r}"
    assert "0" not in line.change.text(), "отсутствие сравнения показано нулём"
    assert colour(line.change) == LIGHT.text, "у неизвестного изменения появился знак направления"
    assert colour(line.change) not in (LIGHT.bull, LIGHT.bear), "то же, названное прямо"
    assert line.values["close"].text() == "225\u00a0550", "цены первой свечи тоже пропали"
    line.deleteLater()
    qapp.processEvents()


def test_a_repeated_close_is_a_plain_zero_without_a_direction(qapp) -> None:
    """Закрытие повторилось: `0 (0,00%)`, без знака и без цвета.

    Это второй из двух случаев, которые легко слить в один: «не изменилось»
    и «сравнивать не с чем» показываются по-разному.
    """
    line = bar(LIGHT)
    line.show_candle(CandleInfo(SHOWN, previous_close=SHOWN.close))
    assert line.change.text() == "0 (0,00%)", "неизменившаяся цена показана не нулём"
    assert colour(line.change) == LIGHT.text, "у нулевого изменения появился цвет направления"
    line.deleteLater()
    qapp.processEvents()


def test_the_colour_follows_the_change_and_not_the_body_of_the_candle(qapp) -> None:
    """Свеча с `ЗАКР` = `ОТКР` всё равно окрашена — по сравнению с предыдущей.

    Цвет в строке означает ровно одно: знак изменения против закрытия
    предыдущей свечи. Взять его от тела свечи (`ЗАКР` против `ОТКР`) —
    ошибка, которую хочется сделать, потому что именно так красится сама
    свеча на графике; тогда на строке оказались бы два разных «падения»,
    и цвет четырёх чисел спорил бы со знаком пятого.
    """
    flat = Candle(opens_at=START, open=225_550.0, high=225_900.0, low=225_100.0, close=225_550.0)
    line = bar(LIGHT)
    line.show_candle(CandleInfo(flat, previous_close=224_400.0))
    assert colour(line.change) == LIGHT.bull, (
        "свеча без тела осталась бесцветной: цвет взяли от «ЗАКР против ОТКР»"
    )
    assert line.change.text().startswith("+"), "рост против предыдущей свечи потерял знак"
    line.deleteLater()
    qapp.processEvents()


def test_a_zero_previous_close_shows_points_without_a_percent(qapp) -> None:
    """Предыдущее закрытие ноль: пункты показаны, процент опущен, окно живо.

    Нулевой цены у фьючерса не бывает, но деление на неё уронило бы не расчёт,
    а окно — в момент отрисовки, у владельца счёта.
    """
    line = bar(LIGHT)
    line.show_candle(CandleInfo(SHOWN, previous_close=0.0))
    assert line.change.text() == "+225\u00a0550", "процент от нулевой цены всё-таки показан"
    line.deleteLater()
    qapp.processEvents()


def test_the_change_of_a_pair_of_candles_is_read_from_the_earlier_one() -> None:
    """`CandleInfo.from_pair` берёт для сравнения закрытие предыдущей свечи.

    Не открытие, не максимум и не закрытие текущей: перепутанное поле
    даёт правдоподобное, но неверное число — заметить его на глаз нельзя.
    """
    earlier = Candle(opens_at=START, open=1.0, high=9.0, low=0.5, close=226_700.0)
    info = CandleInfo.from_pair(SHOWN, earlier)
    assert info.previous_close == 226_700.0, "сравнение идёт не с закрытием предыдущей свечи"
    assert info.change == pytest.approx(-1150.0), "изменение в пунктах посчитано не так"
    assert info.change_pct == pytest.approx(-0.50727, abs=1e-5), "процент посчитан не так"
    assert CandleInfo.from_pair(SHOWN, None).change is None, "изменение без предыдущей свечи"


# ------------------------------------------------------- панель и курсор


def test_the_panel_shows_the_last_candle_of_the_series_it_was_given(panel) -> None:
    """Пока курсора на графике нет, строка показывает последнюю свечу ряда.

    И говорит об этом словами: числа настоящие, но относятся не к тому месту,
    где стоит мышь. Молчаливая подстановка была бы неправдой о курсоре.
    """
    panel.show_chart(series([100_000.0, 100_500.0, 101_000.0]))
    assert panel.info_bar.values["close"].text() == "101\u00a0000", "показана не последняя свеча"
    assert panel.info_bar.change.text() == "+500 (+0,50%)", "сравнили не с предыдущей свечой"
    assert panel.info_bar.note.text() == "последняя свеча", "строка молчит, что курсора на ней нет"


def test_the_panel_returns_to_the_last_candle_when_the_cursor_leaves(panel) -> None:
    """Курсор ушёл — строка не пустеет, а возвращается к последней свече.

    Пустая строка мигала бы при каждом уходе мыши с графика и отнимала бы
    единственное место, где видна текущая цена.
    """
    panel.show_chart(series([100_000.0, 100_500.0, 101_000.0]))
    hovered = CandleInfo(SHOWN, previous_close=226_700.0)

    panel.show_candle_info(hovered)
    assert panel.info_bar.values["close"].text() == "225\u00a0550", "наведение не дошло до строки"
    assert panel.info_bar.note.text() == "", "свеча под курсором названа последней"

    panel.show_candle_info(None)
    assert panel.info_bar.values["close"].text() == "101\u00a0000", "строка не вернулась к ряду"
    assert panel.info_bar.note.text() == "последняя свеча", "возврат к ряду не назван словами"


def test_the_hovered_candle_is_not_stolen_by_the_live_one(panel) -> None:
    """Пока курсор на свече, растущая последняя свеча строку не перебивает.

    Иначе разглядеть свечу в рабочее время невозможно: живая свеча приходит
    десяток раз в минуту и уводила бы числа из-под курсора.
    """
    panel.show_chart(series([100_000.0, 100_500.0]))
    panel.show_candle_info(CandleInfo(SHOWN, previous_close=226_700.0))

    panel.append_candle(Candle(START + STEP * 5, 100_500.0, 100_900.0, 100_400.0, 100_800.0))
    assert panel.info_bar.values["close"].text() == "225\u00a0550", (
        "новая свеча вытеснила из строки ту, которую разглядывают"
    )

    panel.show_candle_info(None)
    assert panel.info_bar.values["close"].text() == "100\u00a0800", (
        "после ухода курсора строка не показала свежую свечу"
    )


def test_the_live_candle_reaches_the_info_line(panel) -> None:
    """Прибавленная и уточнённая свеча доезжают до строки сведений.

    Путь короткий, но обрывается молча: обе передачи можно заменить на `return`,
    и график будет жить, а числа над ним замрут на свече получасовой давности.
    """
    panel.show_chart(series([100_000.0, 100_500.0]))

    panel.append_candle(Candle(START + STEP * 2, 100_500.0, 100_900.0, 100_400.0, 100_800.0))
    assert panel.info_bar.values["close"].text() == "100\u00a0800", "прибавленная свеча не дошла"
    assert panel.info_bar.change.text() == "+300 (+0,30%)", "прибавленную сравнили не с той"

    panel.update_last_candle(Candle(START + STEP * 2, 100_500.0, 101_100.0, 100_400.0, 101_000.0))
    assert panel.info_bar.values["close"].text() == "101\u00a0000", "уточнение свечи не дошло"
    assert panel.info_bar.values["high"].text() == "101\u00a0100", "новый максимум не дошёл"


def test_the_growing_candle_is_compared_with_the_previous_one_not_with_itself(panel) -> None:
    """Растущая свеча сравнивается с закрытием предыдущей, а не с собой минуту назад.

    Сравнение с предыдущим состоянием той же свечи дало бы дрожащее около нуля
    число вместо «сколько прошли от прошлой свечи» — и владелец счёта увидел бы
    спокойный рынок там, где цена ушла на процент.
    """
    panel.show_chart(series([100_000.0, 100_500.0]))
    panel.update_last_candle(Candle(START + STEP, 100_400.0, 101_600.0, 100_300.0, 101_500.0))
    assert panel.info_bar.change.text() == "+1\u00a0500 (+1,50%)", (
        "растущую свечу сравнили не с закрытием предыдущей"
    )


def test_an_empty_series_leaves_dashes_and_promises_no_candle(panel) -> None:
    """Свечей нет вовсе: прочерки и ни слова про последнюю свечу."""
    panel.show_chart(ChartData(instrument="MXU6", timeframe="5 минут"))
    assert panel.info_bar.values["close"].text() == "—", "на пустом графике показано число"
    assert panel.info_bar.moment.text() == "—", "на пустом графике названо время свечи"
    assert panel.info_bar.note.text() == "", "пустой график назван последней свечой"


def test_a_series_of_one_candle_has_nothing_to_compare_with(panel) -> None:
    """Ряд из одной свечи: цены показаны, изменения нет.

    Граничный случай ряда, а не свечи: `candles[-2]` на ряде длины один —
    это `candles[0]`, то есть сравнение свечи с самой собой и вечный ноль.
    """
    panel.show_chart(series([100_000.0]))
    assert panel.info_bar.values["close"].text() == "100\u00a0000", "цены единственной свечи"
    assert panel.info_bar.change.text() == "—", "единственную свечу сравнили сами с собой"


# ------------------------------------------------------------------ тема


#: Порог контраста для обычного текста по WCAG 2.1 (AA). Не 3,0: три —
#: послабление для крупного текста, от 18pt или от 14pt жирным. Числа строки
#: сведений выведены жирным, но обычного размера, и под послабление
#: не попадают; дата, подписи и примечание не жирные вовсе.
READABLE = 4.5


def test_both_themes_keep_the_rise_and_the_fall_readable() -> None:
    """Зелёный и красный читаются на фоне обеих тем и не совпадают друг с другом.

    Замер 04.09.2026 после правки `B-013`: светлая тема 5,27 и 5,62, тёмная
    5,93 и 5,10. До правки зелёный светлой темы давал 4,45 на белом
    и 3,87 на сером фоне окна — ниже порога на обоих.

    ⚠️ Цвет — не единственный признак направления: знак «+» или «−» стоит
    у числа всегда. Человек, не различающий зелёный и красный, читает знак.
    """
    for theme in (LIGHT, DARK):
        for name, colour_value in (("рост", theme.bull), ("падение", theme.bear)):
            measured = contrast(colour_value, theme.background)
            assert measured >= READABLE, (
                f"{name} в теме «{'тёмная' if theme.dark else 'светлая'}» "
                f"нечитаемо: контраст {measured:.2f} при пороге {READABLE}"
            )
        assert theme.bull != theme.bear, "рост и падение одного цвета"


def _painted(line, qapp, width: int = 900, height: int = 30):
    """Строка сведений, нарисованная поверх фона окна, — как её видит человек.

    Картинка заливается **системным** цветом окна до отрисовки, а не остаётся
    пустой: виджет без своего фона ничего поверх не рисует, и именно так
    и выглядел `B-013` — цвета тёмной темы оказались на светлой подложке
    родителя. Проверка, начинающая с чистого листа, этого не увидела бы.

    ⚠️ Флаги отрисовки заданы явно, и `DrawWindowBackground` среди них нет.
    Этот флаг стоит в умолчании `render()` и означает «нарисуй фон виджета,
    даже если он его сам не заливает»: с ним фон появляется на картинке
    всегда, и проверка не отличает виджет, красящий себя, от виджета,
    который просто отрисован поверх чистого листа. Замер мутацией
    04.09.2026: с умолчанием снятие `setAutoFillBackground(True)` проходило
    зелёным.
    """
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QImage, QPalette, QRegion
    from PySide6.QtWidgets import QWidget

    line.resize(width, height)
    line.show()
    qapp.processEvents()
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(qapp.palette().color(QPalette.ColorRole.Window))
    line.render(image, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
    return image


def _labels(line) -> list:
    """Все надписи строки слева направо — подписи, числа и примечание."""
    from PySide6.QtWidgets import QLabel

    layout = line.layout()
    found = []
    for index in range(layout.count()):
        widget = layout.itemAt(index).widget()
        if isinstance(widget, QLabel):
            found.append(widget)
    return found


def _pixel(image, x: int, y: int) -> str:
    from PySide6.QtGui import QColor

    return QColor(image.pixel(x, y)).name()


def _pixels_coloured(image, wanted: str, tolerance: int = 12) -> int:
    """Сколько точек картинки окрашены этим цветом — с допуском на сглаживание."""
    from PySide6.QtGui import QColor

    want = QColor(wanted)
    found = 0
    for x in range(image.width()):
        for y in range(image.height()):
            pixel = QColor(image.pixel(x, y))
            if max(abs(pixel.red() - want.red()),
                   abs(pixel.green() - want.green()),
                   abs(pixel.blue() - want.blue())) <= tolerance:
                found += 1
    return found


@pytest.mark.parametrize("theme", [LIGHT, DARK], ids=["светлая", "тёмная"])
def test_every_word_of_the_line_is_readable_on_the_painted_picture(qapp, theme) -> None:
    """Контраст меряется по **нарисованной** картинке, а не по числам темы.

    `B-013`: `ChartPanel.set_theme(DARK)` перекрашивал надписи, а подложка
    оставалась системной — светлой. Замер по снимку 04.09.2026: дата и время
    свечи, цвет `#e6e9ef` по фону `#efefef`, контраст 1,06:1. Дата в тёмной
    теме была невидима вовсе, и по числам самой темы (14,63:1) этого
    не видно ни на волос.
    """
    line = bar(theme)
    try:
        line.show_candle(CandleInfo.from_pair(SHOWN, Candle(
            opens_at=START - STEP, open=225_000.0, high=225_100.0,
            low=224_900.0, close=225_000.0,
        )))
        image = _painted(line, qapp)
        background = _pixel(image, 1, 1)

        assert background == theme.background, (
            f"строка нарисована на фоне {background}, а цвета подобраны "
            f"к {theme.background}: подложка осталась чужой"
        )
        # Проверяются **все** надписи строки, а не выбранные: подпись,
        # оставленная без перекраски, отличается от числа только тем, что
        # её никто не назвал в списке.
        labels = _labels(line)
        # Тринадцать: шесть подписей, шесть чисел и примечание под ними.
        assert len(labels) == 13, (
            f"в строке {len(labels)} надписей вместо тринадцати: список "
            "проверяемого перестал совпадать со строкой"
        )
        for label in labels:
            measured = contrast(colour(label), background)
            assert measured >= READABLE, (
                f"надпись {label.text()!r} в теме "
                f"«{'тёмная' if theme.dark else 'светлая'}»: контраст "
                f"{measured:.2f} при пороге {READABLE} "
                f"({colour(label)} на {background})"
            )
    finally:
        line.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("theme", [LIGHT, DARK], ids=["светлая", "тёмная"])
def test_the_date_of_the_candle_is_actually_drawn(qapp, theme) -> None:
    """После слова «СВЕЧА» на картинке есть дата, а не пустое место.

    На снимке `B-013` там пусто: надпись рисовалась цветом подложки. Считаются
    точки цвета `theme.text` — им на этой картинке взяться больше неоткуда:
    подписи выведены `text_dim`, а числа при растущей свече — зелёным.
    """
    line = bar(theme)
    try:
        line.show_candle(CandleInfo.from_pair(SHOWN, Candle(
            opens_at=START - STEP, open=225_000.0, high=225_100.0,
            low=224_900.0, close=225_000.0,
        )))
        image = _painted(line, qapp)

        assert colour(line.values["close"]) == theme.bull, (
            "свеча в этой проверке обязана быть растущей: иначе числа выведены "
            "тем же цветом, что и дата, и считать нечего"
        )
        drawn = _pixels_coloured(image, theme.text)
        assert drawn >= 20, (
            f"на картинке {drawn} точек цвета даты ({theme.text}): "
            "после слова «СВЕЧА» пусто"
        )
    finally:
        line.deleteLater()
        qapp.processEvents()


def test_changing_the_theme_repaints_the_line(panel) -> None:
    """Смена темы перекрашивает строку, а не только график под ней.

    Цвета светлой темы на тёмном фоне не читаются — знак изменения пропадает,
    хотя код его показывает.
    """
    panel.show_chart(series([100_000.0, 100_500.0]))
    panel.set_theme(DARK)
    assert colour(panel.info_bar.change) == DARK.bull, "строка осталась в цветах прежней темы"
    panel.set_theme(LIGHT)
    assert colour(panel.info_bar.change) == LIGHT.bull, "обратно тема не вернулась"
