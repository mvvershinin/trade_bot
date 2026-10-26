"""Шрифт окна ×1,2 и посветлевший приглушённый текст (`ROADMAP.md`, Н1 и Н2).

Владелец счёта 05.10.2026: «шрифт увеличить в 1.2 раза и сделать посветлее —
очень плохо видно». Стерегутся три вещи:

* шрифт приложения действительно крупнее системного в `FONT_SCALE` раз —
  и на дороге настоящего запуска (`app.main.main`), а не только в функции;
* `Theme.text_dim` читается не хуже порогов, которые прежние цвета
  не проходили (мутация «вернуть старый цвет» роняет проверку);
* текст выключенных полей тёмной палитры светлее прежнего, но тусклее
  живого.
"""

from __future__ import annotations

import pytest

from tests.test_app_main import _database, _launch


def test_the_scaled_font_is_one_point_two_times_larger(qapp) -> None:
    """Кегль в пунктах и кегль в точках экрана растут одинаково.

    Мутация: `FONT_SCALE = 1.0` или возврат исходного шрифта — падает.
    """
    from PySide6.QtGui import QFont

    from ui.theme import scaled_font

    in_points = QFont()
    in_points.setPointSizeF(10.0)
    assert scaled_font(in_points).pointSizeF() == pytest.approx(12.0), (
        "шрифт в пунктах не вырос в 1,2 раза"
    )
    assert in_points.pointSizeF() == pytest.approx(10.0), "исходный шрифт изменён"

    in_pixels = QFont()
    in_pixels.setPixelSize(20)
    assert scaled_font(in_pixels).pixelSize() == 24, (
        "шрифт, заданный в точках экрана, не вырос — или вырос в минус"
    )


def test_dressing_the_application_enlarges_its_font(qapp) -> None:
    """`_dress_application` — та самая строка `main`, что ставит шрифт.

    Мутация: убрать `application.setFont(...)` из `_dress_application` —
    падает. Шрифт и палитра общего на прогон приложения возвращаются
    в `finally`: сосед по процессу не должен получить ни крупный шрифт,
    ни подменённую палитру.
    """
    from app.main import _arguments, _dress_application

    before, palette = qapp.font(), qapp.palette()
    try:
        _dress_application(qapp, _arguments(["--light"]))
        assert qapp.font().pointSizeF() == pytest.approx(before.pointSizeF() * 1.2), (
            f"шрифт приложения {qapp.font().pointSizeF()} пт при системном "
            f"{before.pointSizeF()} пт — не ×1,2"
        )
    finally:
        qapp.setFont(before)
        qapp.setPalette(palette)


#: Ребёнок — та же программа тем же `main()`. Подменён только снимок: вместо
#: картинки он печатает кегль готового окна и системный кегль платформы.
#: Системный берётся у `QFontDatabase`, а не у приложения: шрифт приложения
#: уже подменён, а шрифт платформы — нет.
FONT_DRIVER = '''
import sys

import app.main


async def report(application, window, port, shot):
    from PySide6.QtGui import QFontDatabase

    system = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    print(f"FONT {window.font().pointSizeF()} {system.pointSizeF()}")
    return 0


app.main._snapshot = report
raise SystemExit(app.main.main(sys.argv[1:]))
'''


@pytest.mark.slow
def test_the_running_program_shows_the_enlarged_font(tmp_path) -> None:
    """На дороге настоящего запуска окно получает шрифт ×1,2.

    Проверка функции выше не ловит «написано, но не вызывается»: строку
    `_dress_application(...)` можно убрать из `main`, и функция останется
    зелёной. Мутация: убрать вызов из `main` — падает здесь.
    """
    outcome = _launch(
        ["-c", FONT_DRIVER],
        ["--db", str(_database(tmp_path / "candles.sqlite3")),
         "--shot", str(tmp_path / "unused.png")],
    )
    assert outcome.returncode == 0, outcome.stderr
    line = next(
        (row for row in outcome.stdout.splitlines() if row.startswith("FONT ")), None
    )
    assert line is not None, outcome.stdout + outcome.stderr
    shown, system = (float(value) for value in line.split()[1:])
    assert shown == pytest.approx(system * 1.2), (
        f"окно показано кеглем {shown} пт при системном {system} пт — не ×1,2"
    )


#: Пороги контраста `text_dim` к фону — **выше** того, что давали прежние
#: цвета (тёмная `#9aa4b2` — 7,1:1 на фоне графика и 6,9:1 на панели;
#: светлая `#6b7280` — 4,8:1 на белом и 4,2:1 на сером фоне окна). Порог
#: ниже прежнего значения возврат к старому цвету не поймал бы.
DIM_FLOORS = {
    "dark": (("#15181d", 10.0), ("#141317", 10.0), ("#1b1a20", 10.0)),
    "light": (("#ffffff", 7.0), ("#efefef", 6.0)),
}


@pytest.mark.parametrize("name", sorted(DIM_FLOORS))
def test_dim_text_reads_well_on_every_background(name: str) -> None:
    from ui.theme import DARK, LIGHT, contrast

    theme = DARK if name == "dark" else LIGHT
    for background, floor in DIM_FLOORS[name]:
        measured = contrast(theme.text_dim, background)
        assert measured >= floor, (
            f"приглушённый текст {theme.text_dim} на {background}: "
            f"{measured:.1f}:1 при пороге {floor}:1 — снова «плохо видно»"
        )
        assert measured < contrast(theme.text, background), (
            f"приглушённый текст {theme.text_dim} не тусклее основного — "
            "подсказка перестала отличаться от подписи"
        )


def test_switched_off_text_is_lighter_than_before_but_dimmer_than_live(qapp) -> None:
    """Выключенное поле тёмной палитры читается, но остаётся выключенным.

    Прежний `#8a8590` давал 4,8:1 на кнопке. Порог 6,5:1 выше прежнего:
    мутация «вернуть `#8a8590`» падает. Верхняя граница — половина контраста
    живого текста: ярче неё выключенное поле не отличить от живого.
    """
    from PySide6.QtGui import QPalette

    from app.main import _dark_palette
    from ui.theme import contrast

    palette = _dark_palette()
    paper = palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Button).name()
    for role in (
        QPalette.ColorRole.ButtonText,
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
    ):
        off = contrast(palette.color(QPalette.ColorGroup.Disabled, role).name(), paper)
        live = contrast(palette.color(QPalette.ColorGroup.Active, role).name(), paper)
        assert off >= 6.5, f"выключенный {role.name}: {off:.1f}:1 — плохо видно"
        assert off <= live * 0.6, (
            f"выключенный {role.name}: {off:.1f}:1 при живом {live:.1f}:1 — "
            "выключенное не отличить от живого"
        )


def test_switched_off_text_reads_in_the_light_palette_too(qapp) -> None:
    """Светлая (системная) палитра: выключенный текст читается и остаётся тусклым.

    Системный `#bebebe` на `#efefef` — 1,6:1: «С предельной ценой» в окне
    настроек почти не читалось (снимок 05.10.2026). Мутация «убрать цикл
    из `_light_palette`» или «вернуть системный цвет» — падает.
    """
    from PySide6.QtGui import QColor, QPalette

    from app.main import _light_palette
    from ui.theme import contrast

    system = QPalette()
    for role in (
        QPalette.ColorRole.ButtonText,
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
    ):
        system.setColor(QPalette.ColorGroup.Active, role, QColor("#000000"))
        system.setColor(QPalette.ColorGroup.Disabled, role, QColor("#bebebe"))
    system.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Button, QColor("#efefef"))

    palette = _light_palette(system)
    paper = palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Button).name()
    for role in (
        QPalette.ColorRole.ButtonText,
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
    ):
        off = contrast(palette.color(QPalette.ColorGroup.Disabled, role).name(), paper)
        live = contrast(palette.color(QPalette.ColorGroup.Active, role).name(), paper)
        assert off >= 4.5, f"выключенный {role.name}: {off:.1f}:1 — плохо видно"
        assert off <= live * 0.6, f"выключенный {role.name} не отличить от живого"


def test_long_headers_break_in_two_and_short_ones_stay_whole() -> None:
    """Шапка журнала: длинная подпись — две строки, без потери знаков.

    Без разбивки десять колонок журнала сделок с шрифтом ×1,2 не влезали
    в 1280 точек, и колонка комиссии уезжала за край (снимок 05.10.2026).
    """
    from ui.journals import two_lines

    wrapped = two_lines("Результат за вычетом комиссии, ₽")
    assert "\n" in wrapped, "длинная подпись осталась одной строкой"
    assert wrapped.replace("\n", " ") == "Результат за вычетом комиссии, ₽"
    assert two_lines("Комиссия, ₽") == "Комиссия, ₽", "«₽» оторван на вторую строку"
    assert two_lines("Объём") == "Объём"


def test_the_trades_table_keeps_three_rows_however_tight(qapp) -> None:
    """Наименьшая высота таблицы сделок — шапка и три строки.

    На 1280×800 с шрифтом ×1,2 таблица сжималась до нуля видимых строк:
    журнал был, сделок в нём не было (снимок 05.10.2026). Мутация «убрать
    `minimumSizeHint` у таблицы» — падает.
    """
    from ui.journals import JournalTabs

    tabs = JournalTabs()
    table = tabs.trades_table
    rows = table.verticalHeader().defaultSectionSize() * 3
    assert table.minimumSizeHint().height() >= rows + table.horizontalHeader().sizeHint().height()


def test_wrapped_text_in_a_fit_scroll_shrinks_to_a_few_lines(qapp) -> None:
    """Текст в прокрутке: желаемая высота — весь, наименьшая — несколько строк.

    Легенда графика и итог под журналом требовали высоту целиком как
    наименьшую, и окно вылезало за нижний край экрана 1280×800.
    """
    from PySide6.QtWidgets import QLabel

    from ui.fit_scroll import FitScrollArea

    label = QLabel(" ".join(["слово"] * 400))
    label.setWordWrap(True)
    area = FitScrollArea(label, min_lines=2)
    area.resize(400, 100)
    line = area.fontMetrics().lineSpacing()
    assert area.minimumSizeHint().height() <= 2 * line, "наименьшая — больше двух строк"
    assert area.sizeHint().height() > 5 * line, "желаемая высота не держит текст целиком"


def test_hints_under_fields_use_the_full_font(qapp) -> None:
    """Пояснение под полем — основным кеглем, а не «на пункт мельче».

    До 05.10.2026 подсказки окна настроек и оговорка под журналом сделок
    шли на пункт мельче основного; владелец счёта: «очень плохо видно».
    Мутация «вернуть −1 пт» в любом из двух мест — падает.
    """
    from PySide6.QtWidgets import QLabel

    from ui.journals import _assumptions_label
    from ui.settings_dialog import _hint

    full = QLabel().font().pointSizeF()
    # ⚠️ После `ensurePolished`: кегль из таблицы стилей (`font-size` рядом
    # с цветом в `_paint_hint`) доходит до `font()` только при полировке,
    # и проверка без неё зеленела на подсказке в 7 пт.
    for made in (_hint("пояснение"), _assumptions_label()):
        made.ensurePolished()
        assert made.font().pointSizeF() == pytest.approx(full), made.styleSheet()
