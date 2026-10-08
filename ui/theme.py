"""Цвета окна и графика — одинаково читаемые в светлой и тёмной теме.

Qt 6.5+ подхватывает системную тему сам, но цвета свечей, меток лонга и шорта
и линий уровней он не знает. Здесь это не косметика: если шорт от лонга
не отличается на тёмном фоне, владелец счёта не видит, в какую сторону стоит
позиция. Поэтому каждый цвет задан для обеих тем и проверяется глазом на обеих.

Модуль не знает, чем нарисован график. Он отдаёт числа; рисуют ими и виджеты,
и отрисовщик графика (`ui/chart/`).
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QWidget

from ui.models import Layer, LevelKind, MarkerKind, ShadeKind


@dataclass(frozen=True, slots=True)
class Theme:
    """Набор цветов. Все — строками `#rrggbb`, чтобы годиться и Qt, и вебу."""

    dark: bool
    background: str
    grid: str
    text: str
    text_dim: str
    badge_quiet: str     # заливка отметки «СИМУЛЯЦИЯ» — см. `QUIET_BADGE`
    axis: str
    bull: str            # свеча вверх
    bear: str            # свеча вниз
    average: str         # линия скользящей средней
    long: str            # метка входа в лонг
    short: str           # метка входа в шорт
    exit: str            # метка выхода
    reversal: str        # точка переворота — отдельный цвет, это не обычный выход
    plan: str            # слой «прогноз»
    fact: str            # слой «факт»
    unplanned: str       # сделка вне расчёта — DOMAIN.md §8
    level_entry: str
    level_take: str
    level_trailing: str
    shade: str
    danger: str
    warning: str
    success: str

    def marker_color(self, kind: MarkerKind, layer: Layer) -> str:
        """Цвет метки. Слой «прогноз» приглушён, «факт» — основной.

        Различие слоёв держится не только на цвете: форма у них тоже разная
        (`ui/chart/painter_surface.py`). Цвет один — недостаточно для человека,
        который плохо различает оттенки, и недостаточно для чёрно-белой печати
        отчёта.
        """
        if kind is MarkerKind.UNPLANNED:
            return self.unplanned
        if kind is MarkerKind.MISSED:
            return self.plan
        if layer is Layer.PLAN:
            return self.plan
        return {
            MarkerKind.ENTRY_LONG: self.long,
            MarkerKind.ENTRY_SHORT: self.short,
            MarkerKind.EXIT: self.exit,
            MarkerKind.REVERSAL: self.reversal,
        }.get(kind, self.fact)

    def level_color(self, kind: LevelKind) -> str:
        return {
            LevelKind.ENTRY: self.level_entry,
            LevelKind.TAKE: self.level_take,
            LevelKind.TRAILING: self.level_trailing,
        }[kind]

    def shade_color(self, kind: ShadeKind) -> str:
        return self.shade if kind is ShadeKind.OUTSIDE_WINDOW else self.warning

    def qcolor(self, value: str, alpha: int = 255) -> QColor:
        color = QColor(value)
        color.setAlpha(alpha)
        return color


#: Зелёный светлой темы: свеча вверх, метка лонга, прибыль. Один цвет
#: на три поля намеренно — число и свеча, которую оно описывает, обязаны
#: быть одного цвета.
#:
#: ⚠️ Значение подобрано по контрасту, а не по вкусу. Прежний `#12876f`
#: давал на белом 4,45:1 при пороге 4,5:1 для обычного текста, а на сером
#: фоне окна (`#efefef`, системная светлая тема) — 3,87:1, и знак роста
#: в строке сведений о свече читался хуже, чем знак падения. Замер
#: 04.09.2026: `#0f7a64` даёт 5,27:1 на белом и 4,58:1 на `#efefef`,
#: то есть проходит порог на обоих фонах, на которых он оказывается.
#: Порог стережёт `tests/test_ui_panel.py`.
LIGHT_GREEN = "#0f7a64"

#: Во сколько раз шрифт окна крупнее системного. Просьба владельца счёта
#: 05.10.2026 (`ROADMAP.md`, Н2): «шрифт увеличить в 1.2 раза — очень плохо
#: видно». Не торговое число, поэтому константа, а не поле настроек.
#:
#: Применяется **один раз**, ко шрифту приложения (`app/main.py::
#: _dress_application`): все виджеты наследуют его сами. Здесь же через него
#: пересчитаны немногие кегли, заданные в окне абсолютным числом, — иначе
#: они остались бы прежними и на фоне выросшего текста стали бы мельче.
FONT_SCALE = 1.2


def scaled(size: float) -> float:
    """Абсолютный кегль окна, пересчитанный на `FONT_SCALE`."""
    return size * FONT_SCALE


def scaled_font(font: QFont) -> QFont:
    """Копия шрифта, крупнее в `FONT_SCALE` раз. Исходный не меняется.

    ⚠️ Системный шрифт бывает задан в точках экрана, а не в пунктах: тогда
    `pointSizeF()` возвращает −1, и умножение дало бы отрицательный кегль.
    Такой шрифт растёт по `pixelSize()`.
    """
    bigger = QFont(font)
    if font.pointSizeF() > 0:
        bigger.setPointSizeF(font.pointSizeF() * FONT_SCALE)
    else:
        bigger.setPixelSize(round(font.pixelSize() * FONT_SCALE))
    return bigger


#: Сколько оставить от края экрана окну, которое просит размер больше
#: экрана: рамка, заголовок и панель задач. Не точный расчёт — запас.
SCREEN_MARGIN = 80


def resize_within_screen(window: QWidget, width: int, height: int) -> None:
    """Задать окну размер, но не больше доступной части его экрана.

    ⚠️ Зачем: с крупным шрифтом окна просят больше места, а у владельца
    счёта бывает экран 1280×800 с панелью задач. Окно, вылезшее за экран,
    прячет нижние кнопки «ОК» и «Применить» — и не скажет об этом.
    """
    screen = window.screen() or QApplication.primaryScreen()
    if screen is not None:
        room = screen.availableGeometry()
        width = min(width, room.width() - SCREEN_MARGIN)
        height = min(height, room.height() - SCREEN_MARGIN)
    window.resize(width, height)


#: Приглушённый текст — пояснения под полями, подписи, описания легенды.
#:
#: ⚠️ Подобран по жалобе владельца счёта 05.10.2026: «очень плохо видно».
#: Прежние `#9aa4b2` (тёмная) и `#6b7280` (светлая) давали 7,1:1 и 4,8:1
#: (на сером фоне окна `#efefef` — 4,2:1, ниже порога 4,5). Новые:
#: тёмная `#c5ccd6` — 11,0:1 на фоне графика и 10,7:1 на панели; светлая
#: `#4b5563` — 7,6:1 на белом и 6,6:1 на `#efefef`. Текст остаётся
#: приглушённым: живой текст тёмной темы — 14:1. Порог стережёт
#: `tests/test_ui_theme_font.py`.
DARK_TEXT_DIM = "#c5ccd6"
LIGHT_TEXT_DIM = "#4b5563"

#: Заливка отметки «СИМУЛЯЦИЯ» — прежние значения `text_dim`, отдельным
#: полем намеренно. Отметка безопасного режима не должна читаться лучше
#: отметки боевого (`tests/test_ui_window.py`, ТЗ §4.4 З): посветлевший
#: `text_dim` дал бы ей 11:1 против 5,3:1 у «БОЕВОЙ РЕЖИМ».
QUIET_BADGE_LIGHT = "#6b7280"
QUIET_BADGE_DARK = "#9aa4b2"

LIGHT = Theme(
    dark=False,
    background="#ffffff",
    grid="#e6e8ec",
    text="#1b1f24",
    text_dim=LIGHT_TEXT_DIM,
    badge_quiet=QUIET_BADGE_LIGHT,
    axis="#c2c8d0",
    bull=LIGHT_GREEN,
    bear="#c62828",
    average="#1565c0",
    long=LIGHT_GREEN,
    short="#c62828",
    exit="#455a64",
    reversal="#8e24aa",
    plan="#5e5ce6",
    fact="#ef6c00",
    unplanned="#d50000",
    level_entry="#455a64",
    level_take="#2e7d32",
    level_trailing="#00838f",
    shade="#9aa4b2",
    danger="#c62828",
    warning="#b26a00",
    success=LIGHT_GREEN,
)

DARK = Theme(
    dark=True,
    background="#15181d",
    grid="#242a33",
    text="#e6e9ef",
    text_dim=DARK_TEXT_DIM,
    badge_quiet=QUIET_BADGE_DARK,
    axis="#39424f",
    bull="#26a69a",
    bear="#ef5350",
    average="#64b5f6",
    long="#26a69a",
    short="#ef5350",
    exit="#b0bec5",
    reversal="#ce93d8",
    plan="#9d8cff",
    fact="#ffb74d",
    unplanned="#ff5252",
    level_entry="#b0bec5",
    level_take="#66bb6a",
    level_trailing="#4dd0e1",
    shade="#5c6673",
    danger="#ef5350",
    warning="#ffb74d",
    success="#26a69a",
)


def _relative_luminance(color: QColor) -> float:
    """Относительная яркость по WCAG 2.1 — с гамма-коррекцией, а не «на глаз».

    Упрощённая формула без линеаризации (та, что в `is_dark`) для выбора темы
    годится: там вопрос «светлее или темнее половины». Для контраста надписи
    не годится — она ошибается как раз на насыщенных оранжевых и красных,
    то есть ровно на цветах предупреждений.
    """
    def channel(value: float) -> float:
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    return (
        0.2126 * channel(color.redF())
        + 0.7152 * channel(color.greenF())
        + 0.0722 * channel(color.blueF())
    )


def contrast(first: str, second: str) -> float:
    """Отношение контраста двух цветов по WCAG. 1,0 — неразличимы, 21,0 — предел."""
    light, dark = sorted(
        (_relative_luminance(QColor(first)), _relative_luminance(QColor(second))),
        reverse=True,
    )
    return (light + 0.05) / (dark + 0.05)


#: Тёмный цвет надписи на светлой плашке. Не чистый чёрный: на насыщенном
#: оранжевом он выглядит дырой, а выигрыш по контрасту — доли единицы.
INK = "#101418"


def text_on(background: str) -> str:
    """Цвет надписи, читаемой на этой заливке: белый или почти чёрный.

    ⚠️ Не косметика, а видимость предупреждения. Плашки рисовались белым
    по цвету заливки безусловно, и в тёмной теме `warning` — это `#ffb74d`,
    светлый оранжевый: белым по нему контраст около 1,7:1. «Токен брокера
    истекает через 6 дн.» становилось нечитаемым — то есть предупреждения
    не было вовсе, хотя код его показывал.

    Выбор не по порогу яркости, а сравнением двух контрастов: порог
    промахивается на цветах у середины шкалы, а сравнение — нет.

    ⚠️ Негодная строка цвета даёт **тёмную** надпись, а не белую. Qt на такой
    строке возвращает `QColor` с нулями, то есть «чёрный», и сравнение
    контрастов честно выбрало бы белый. Но заливка при этом не применится
    вовсе: Qt молча выбрасывает негодное правило таблицы стилей, и фон
    останется системным — в светлой теме светлым. Белая надпись на нём
    исчезает совсем, тёмная читается и там, и на чёрном.
    """
    color = QColor(background)
    if not color.isValid():
        return INK
    return max(("#ffffff", INK), key=lambda ink: contrast(ink, background))


def is_dark(palette: QPalette | None = None) -> bool:
    """Тёмная ли сейчас тема. Считается по яркости фона окна, а не по названию.

    Названия тем в Linux не стандартизованы («Adwaita-dark», «Breeze Dark»,
    «Yaru-dark», собственные сборки), и разбор строки промахивается. Яркость фона
    даёт ответ при любой теме, включая заданную пользователем вручную.
    """
    if palette is None:
        app = QApplication.instance()
        if app is None:
            return False
        palette = app.palette()
    color = palette.color(QPalette.ColorRole.Window)
    # Коэффициенты — стандартная светлота по Rec. 709.
    luma = 0.2126 * color.redF() + 0.7152 * color.greenF() + 0.0722 * color.blueF()
    return luma < 0.5


def current(palette: QPalette | None = None) -> Theme:
    """Тема под текущую системную. Вызывается при старте и при смене темы на ходу."""
    return DARK if is_dark(palette) else LIGHT
