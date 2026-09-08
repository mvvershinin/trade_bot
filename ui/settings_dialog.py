"""Окно настроек. Одно на бой и на проверку истории (ТЗ §4.4).

Пользователь — владелец счёта, не программист. Отсюда два правила, которые здесь
соблюдаются буквально:

1. **Ничего не редактируется в файлах.** Всё, что влияет на работу робота,
   задаётся мышкой и полями ввода.
2. **У каждого поля — подсказка простым языком.** Не «EMA period», а «на скольких
   свечах считается линия; больше значение — линия спокойнее». Подсказка видна
   прямо под полем, а не только во всплывающем окошке: во всплывающее никто
   не наводит, пока не сломалось.

Логики здесь нет. Диалог собирает значения в `Settings` и отдаёт их наружу.
Что с ними делать, с какого момента они действуют и какую строку записать
в журнал решений — решает `engine/` (SPEC §4.4 А: «действует со следующей сделки,
пишется в журнал с прежним и новым значением»).

Про умолчания
-------------
Числа в полях — отправные точки, а не рекомендации. Тейк 0,5% и окно 10:05–11:00
подобраны на том же отрезке истории, на котором проверялись (DOMAIN.md §4).
Это сказано владельцу счёта прямо в окне, а не спрятано в документации: иначе
он примет подобранные цифры за проверенные.

Три правила, из-за которых поля устроены не самым коротким способом
-------------------------------------------------------------------
1. **Значение, пришедшее снаружи, не подменяется молча.** Ни объём выше потолка,
   ни негодное сочетание порога и отступа. Подмена на «ОК» — это изменение
   параметра по деньгам, которого владелец счёта не делал и которого не будет
   в журнале решений. Поэтому границы полей широкие, а негодное показывается
   как есть и не выпускается наружу запретом кнопок.
2. **Ноль в тейке — рабочая конфигурация, а не ошибка ввода.** Обе
   конфигурации-сторожа сверки с прототипом сделаны с выключенным тейком
   (PROTOTYPE.md §1); прежний диапазон 0,1–5,0 не позволял их воспроизвести.
3. **Ноль в комиссии — «тариф не задан», а не «комиссии нет».** Разница видна
   в журнале движка: без тарифа он пишет, что правило «цель окупает комиссию
   обеих сторон» НЕ ПРОВЕРЕНО, а с нулём объявил бы окупающейся любую цель.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from PySide6.QtCore import QEvent, QSignalBlocker, QTime, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from market import warmup_bars
from ui.algorithm_dialog import AlgorithmDialog
from ui.confirm_changes import confirm_changes
from ui.models import (
    AfterTakeProfit,
    AlgorithmOption,
    AverageKind,
    OnPriceEqualsAverage,
    ReversalMoment,
    Settings,
)
from ui.theme import current as current_theme
from ui.wheel_guard import guard_wheel

TIMEFRAMES = ("1 минута", "5 минут", "15 минут", "30 минут", "1 час", "4 часа", "День")

#: Что написано вместо названия алгоритма, пока каталог не пришёл.
#:
#: ⚠️ Молчание здесь запрещено правилом 13 `CLAUDE.md`: пустая строка на месте
#: названия читается как «алгоритма нет». Каталог приходит от торговой части
#: (`app/convert.py::algorithms`), и окно, открытое без неё — снимок экрана,
#: разбор журнала, — обязано сказать об этом, а не показать пустое место.
CATALOGUE_NOT_ARRIVED = (
    "Список алгоритмов сюда не пришёл: окно открыто без торговой части "
    "либо программа не успела ответить. Выбранное имя показано как есть."
)

#: Раскладка окна по вкладкам: заголовок и строители групп на нём.
#:
#: Порядок вкладок — порядок работы с ними: сначала «чем торгуем и на чём»,
#: потом «как читается сигнал», потом «что делаем с позицией», время, деньги
#: и в конце настройки самой программы.
_TAB_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Инструмент и данные", ("_instrument_group", "_point_group")),
    # ⚠️ Выбранный алгоритм назван **первым** на вкладке, до полей: поля
    # средней настраивают его, и читать их, не зная, чей они, бессмысленно.
    # Это **строка и кнопка**, а не абзац: описание правила занимает 391–459
    # точек и вытесняло бы поля под прокрутку — замер 08.09.2026. Полное
    # описание живёт в отдельном окне (`ui/algorithm_dialog.py`).
    ("Сигнал", ("_algorithm_group", "_signal_group", "_filter_group")),
    ("Вход и выход", ("_entry_group", "_take_group")),
    ("Торговое окно", ("_time_group",)),
    ("Деньги", ("_volume_group", "_risk_group", "_commission_group")),
    ("Программа", ("_program_group",)),
)

#: Где живёт каждое поле `Settings`: имя поля → вкладка → поле окна.
#:
#: Таблица, а не память. Полей тридцать пять, вкладок шесть, и вопрос «а куда
#: делось поле, которое я добавил вчера» иначе задать некому: поле без места
#: на экране не падает и не светится — оно просто не показывается, а на «ОК»
#: возвращается к умолчанию. Ровно так уже жили фильтр против пилы и закрытие
#: по концу окна.
#:
#: Третий столбец — имя поля **окна**, а не поля настроек: у половины они
#: не совпадают (`take_profit_pct` показывается полем `take_profit`). Пустая
#: строка означает, что своего виджета у настройки нет вовсе, и таких две:
#:
#: * `calendar` правится в отдельном окне из меню «Настройки», а здесь только
#:   переносится через обмен. На вкладке про него стоит строка-указатель —
#:   она и названа третьим столбцом, иначе «поле есть, а найти негде»;
#: * `ruble_per_point_source` — не поле ввода, а происхождение соседнего
#:   числа; показывается строкой `point_note` под ним.
#:
#: На полноту таблицы стоит сторож: каждое поле `Settings` обязано быть здесь
#: ровно один раз, и виджет обязан лежать на той вкладке, которую таблица
#: называет. Расхождение таблицы с окном молчит, поэтому проверяется машиной.
PLACEMENT: tuple[tuple[str, str, str], ...] = (
    ("instrument", "Инструмент и данные", "instrument"),
    ("timeframe", "Инструмент и данные", "timeframe"),
    ("history_depth_days", "Инструмент и данные", "history_depth_days"),
    ("depth_days", "Инструмент и данные", "depth_days"),
    ("price_step", "Инструмент и данные", "price_step"),
    ("ruble_per_point", "Инструмент и данные", "ruble_per_point"),
    ("ruble_per_point_source", "Инструмент и данные", "point_note"),
    ("strategy_id", "Сигнал", "algorithm_name"),
    ("average_period", "Сигнал", "average_period"),
    ("average_kind", "Сигнал", "average_kind"),
    ("on_price_equals_average", "Сигнал", "on_price_equals_average"),
    ("filter_enabled", "Сигнал", "filter_enabled"),
    ("threshold_percent", "Сигнал", "threshold_percent"),
    ("confirm_bars", "Сигнал", "confirm_bars"),
    ("reversal_moment", "Вход и выход", "reversal_moment"),
    ("after_take_profit", "Вход и выход", "after_take_profit"),
    ("take_profit_enabled", "Вход и выход", "take_profit_enabled"),
    ("take_profit_pct", "Вход и выход", "take_profit"),
    ("trailing_enabled", "Вход и выход", "trailing_enabled"),
    ("trailing_start_pct", "Вход и выход", "trailing_start"),
    ("trailing_offset_pct", "Вход и выход", "trailing_offset"),
    ("trailing_step_pct", "Вход и выход", "trailing_step"),
    ("window_start", "Торговое окно", "window_start"),
    ("window_end", "Торговое окно", "window_end"),
    ("close_on_time_end", "Торговое окно", "close_on_time_end"),
    ("calendar", "Торговое окно", "calendar_note"),
    ("volume", "Деньги", "volume"),
    ("volume_cap_enabled", "Деньги", "volume_cap_enabled"),
    ("volume_cap", "Деньги", "volume_cap"),
    ("daily_loss_limit_enabled", "Деньги", "daily_loss_limit_enabled"),
    ("daily_loss_limit_pct", "Деньги", "daily_loss_limit"),
    ("free_funds_reserve_enabled", "Деньги", "free_funds_reserve_enabled"),
    ("free_funds_reserve_pct", "Деньги", "free_funds_reserve"),
    ("commission_per_side_rub", "Деньги", "commission"),
    ("slippage_steps", "Деньги", "slippage_steps"),
    ("log_directory", "Программа", "log_directory"),
)


def _choice(enumeration) -> QComboBox:
    """Список значений перечисления с человеческими подписями.

    Подпись берётся из самого перечисления (`label`), а не пишется здесь:
    иначе окно и движок разъедутся в названиях, и владелец счёта увидит
    в журнале решений не то, что выбирал в окне.
    """
    box = QComboBox()
    for item in enumeration:
        box.addItem(item.label, item)
    return box


def _num(value: float) -> str:
    """Число по-русски, без хвостовых нулей: `0,5`, `0,05`, `14`."""
    return f"{value:g}".replace(".", ",")


def _pct(value: float) -> str:
    """Процент по-русски: `0,5%`."""
    return f"{_num(value)}%"


def _whole(low: int, high: int, suffix: str = "") -> QSpinBox:
    """Поле целого числа: диапазон и подпись единиц измерения.

    Строитель, а не пять вызовов на каждое поле. Пятёрка вызовов, повторённая
    на десятке полей, — это десяток мест, где можно забыть суффикс и оставить
    владельца счёта гадать, чего именно «пять».
    """
    box = QSpinBox()
    box.setRange(low, high)
    if suffix:
        box.setSuffix(suffix)
    return box


def _decimal(
    low: float, high: float, step: float, suffix: str, decimals: int = 2
) -> QDoubleSpinBox:
    """Поле дробного числа: диапазон, шаг стрелок, знаки после запятой, подпись.

    ⚠️ `decimals` доводом, а не константой: поле с двумя знаками молча
    округлит копеечный шаг цены акции до нуля, и настройка будет выглядеть
    заданной. Умолчание — два знака, потому что так у процентов и рублей.
    """
    box = QDoubleSpinBox()
    box.setRange(low, high)
    box.setSingleStep(step)
    box.setDecimals(decimals)
    box.setSuffix(suffix)
    return box


#: Свойство-пометка на ярлыках тревоги. По нему они находятся при смене темы:
#: цвет прописан в таблице стилей в момент создания и сам не меняется.
ALERT_ROLE = "терминал-тревога"


def _alert(text: str = "") -> QLabel:
    """Строка тревоги под полем: цвет опасности из темы, полужирный.

    Цвет из темы, а не числом: красный светлой темы на тёмном фоне читается
    плохо (3,16:1), а тёмной на светлом — 3,49:1. Это не украшение,
    а предупреждение об отсутствующем предохранителе.

    Ярлык помечается, чтобы `SettingsDialog.changeEvent` нашёл его при смене
    системной темы на ходу и перекрасил. Без пометки пришлось бы держать
    список вручную, и первый же новый ярлык в него не попал бы.
    """
    label = QLabel(text)
    label.setWordWrap(True)
    label.setProperty(ALERT_ROLE, True)
    _paint_alert(label)
    return label


def _paint_alert(label: QLabel) -> None:
    """Перекрасить ярлык тревоги под текущую тему."""
    label.setStyleSheet(f"color: {current_theme().danger}; font-weight: bold;")


#: Свойство-пометка на приглушённых строках: подсказках под полями и спокойных
#: строках состояния. По нему они находятся при смене темы — как и ярлыки
#: тревоги, цвет прописан в момент создания и сам не меняется.
HINT_ROLE = "терминал-подсказка"


def _paint_hint(label: QLabel) -> None:
    """Приглушённый цвет подсказки — **из темы**, а не `palette(mid)`.

    ⚠️ `palette(mid)` тут стояло и было ошибкой, замеренной 05.09.2026.
    Роль `Mid` в Qt предназначена для теней рамок, а не для текста: в светлой
    палитре это `#b8b8b8`, и на подложке группы `#ececec` подсказка давала
    контраст **1,7:1** при пороге 4,5:1 — то есть не читалась вовсе. В тёмной
    теме случайно выходило 5,1:1, поэтому глазами дефект видели только те,
    у кого система светлая.

    `Theme.text_dim` — цвет, заведённый ровно для этого: `#6b7280` в светлой
    (4,9:1) и `#9aa4b2` в тёмной (8,2:1).
    """
    label.setProperty(HINT_ROLE, True)
    label.setStyleSheet(f"color: {current_theme().text_dim};")


def _paint_note(label: QLabel, *, alarming: bool) -> None:
    """Строка состояния: тревожная или обычная.

    Одна функция на все такие строки. Красное, которое горит всегда,
    перестают читать через день — поэтому строка про **выключенный**
    предохранитель приглушённая, хотя и говорит, что защиты нет, а красной
    становится только то, что требует внимания сейчас.
    """
    if alarming:
        label.setProperty(HINT_ROLE, False)
        _paint_alert(label)
        return
    _paint_hint(label)


#: Остатки, на которых кончается «одна свеча» и «две свечи». Числами, а не
#: выводом на месте: правило русского счёта не выводится из значения.
_ONE_REMAINDERS = (1,)
_FEW_REMAINDERS = (2, 3, 4)
_TEEN_REMAINDERS = range(11, 15)


def _agree(count: float, one: str, few: str, many: str) -> str:
    """Число и слово в согласованной форме: «1 свеча», «52 свечи», «16 свечей».

    «52 свечей» и «1 дней» в окне читаются как небрежность, а небрежности
    не верят целиком — вместе с числом, ради которого фраза написана.

    ⚠️ Дробное число берёт **родительный единственного**: «1,5 шага»,
    «0,5 шага». Правило русского счёта не выводится из значения, его надо
    знать, и знать его должно одно место, а не каждая фраза.
    """
    if not float(count).is_integer():
        return f"{_num(count)} {few}"
    whole = int(count)
    tail_hundred = abs(whole) % 100
    tail_ten = abs(whole) % 10
    if tail_hundred in _TEEN_REMAINDERS:
        word = many
    elif tail_ten in _ONE_REMAINDERS:
        word = one
    elif tail_ten in _FEW_REMAINDERS:
        word = few
    else:
        word = many
    return f"{whole} {word}"


def _bars(count: int) -> str:
    """Свечи: «1 свеча», «52 свечи», «16 свечей»."""
    return _agree(count, "свеча", "свечи", "свечей")


def _days(count: int) -> str:
    """Дни: «1 день», «22 дня», «90 дней»."""
    return _agree(count, "день", "дня", "дней")


def _steps(count: float) -> str:
    """Шаги цены: «1 шаг», «2 шага», «1,5 шага», «5 шагов»."""
    return _agree(count, "шаг", "шага", "шагов")


def _hint(text: str) -> QLabel:
    """Подсказка под полем: серым, некрупно, с переносом строк."""
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    font = label.font()
    font.setPointSizeF(max(font.pointSizeF() - 1.0, 7.0))
    label.setFont(font)
    _paint_hint(label)
    return label


class SettingsDialog(QDialog):
    """Настройки робота. Применяются на ходу, без перезапуска программы."""

    #: Отдаётся при «Применить» и при «ОК». Дальше — дело главного окна и движка.
    settings_changed = Signal(object)  # Settings

    def __init__(self, settings: Settings | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Настройки робота")
        self.setModal(True)
        self.resize(560, 720)

        self._build_fields()
        self.tabs = self._build_tabs()

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Apply
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("ОК")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        self.buttons.button(QDialogButtonBox.StandardButton.Apply).setText("Применить")
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)
        self.buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._emit)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 0)
        # ⚠️ Оговорка про подобранные умолчания стоит НАД вкладками, а не
        # внутри одной из них: она относится ко всем числам окна сразу,
        # а спрятанная на вкладку читалась бы как оговорка к этой вкладке.
        layout.addWidget(self._notice())
        layout.addWidget(self.tabs, 1)
        layout.addWidget(self.buttons)

        self._set_tab_order()
        #: Настройки, которые робот считает действующими: с ними сравнивается
        #: содержимое полей при «Применить» и «ОК». Обновляется после каждой
        #: удачной отправки — иначе второе «Применить» показало бы изменения,
        #: сделанные до первого.
        self._applied = settings or Settings()
        self.set_values(self._applied)
        # ⚠️ После сборки полей и до первого показа: колесо мыши не имеет права
        # менять значения, пока на поле нет фокуса (`ui/wheel_guard.py`).
        # Владелец счёта уже ловил это на себе — прокрутка окна меняла цифру
        # по деньгам молча.
        self.guarded_fields = guard_wheel(self)
        # Последняя строка сборки: до неё `changeEvent` не имеет права трогать
        # поля — событие палитры приходит и в середине конструктора.
        self._ready = True

    def changeEvent(self, event) -> None:  # имя метода задано Qt
        """Смена системной темы на ходу.

        Три текста здесь берут цвет из темы в момент создания: `cap_note`,
        `guards_note` и сообщение об ошибке в `take_note`. Без этого метода
        после переключения темы они оставались в цветах прежней — красный
        светлой темы на тёмном фоне даёт 3,16:1, тёмной на светлом — 3,49:1.
        Сведения при этом не теряются (полужирный, текст читается), но это
        строки про отсутствующие предохранители и про запрет ввода, и
        приглушать их нечем.
        """
        if event.type() in (
            QEvent.Type.ApplicationPaletteChange,
            QEvent.Type.PaletteChange,
        ) and getattr(self, "_ready", False):
            for label in self.findChildren(QLabel):
                if label.property(ALERT_ROLE):
                    _paint_alert(label)
                elif label.property(HINT_ROLE):
                    _paint_hint(label)
            # `take_note` красится не через `_alert`, а внутри `_sync_take`:
            # цвет там зависит от того, есть ли ошибка. Пересчёт вернёт
            # и текст, и цвет разом.
            self._sync_take()
            # То же самое и по той же причине: `point_note`, `cap_note`
            # и `guards_note` красятся не через `_alert`, а по состоянию —
            # включён предохранитель или нет, подтверждена величина или нет.
            self._sync_costs()
            self._sync_volume_cap()
            self._sync_guards()
        super().changeEvent(event)

    # ------------------------------------------------------------------ поля

    def _build_fields(self) -> None:
        """Собрать поля. Шесть строителей по группам окна, а не одна простыня.

        ⚠️ Диапазоны и особые подписи задаются через `_whole` и `_decimal`,
        а не пятёркой `set*` подряд на каждое поле. Причина не в длине:
        пятёрка вызовов, повторённая на десятке полей, — это десяток мест,
        где можно забыть `setDecimals` и получить поле, молча округляющее
        ввод владельца счёта до целых.

        Комментарии у диапазонов оставлены на местах: каждый объясняет,
        почему нижняя граница именно такая, и все они куплены поломками.
        """
        self._build_instrument_fields()
        self._build_algorithm_fields()
        self._build_take_fields()
        self._build_cost_fields()
        self._build_time_fields()
        self._build_risk_fields()
        self._build_algorithm_row()

    def _build_instrument_fields(self) -> None:
        """Инструмент, размер свечи и две глубины — загрузки и показа."""
        self.instrument = QLineEdit()
        self.instrument.setPlaceholderText("Например, MXU6")

        self.timeframe = QComboBox()
        self.timeframe.addItems(TIMEFRAMES)

        # ⚠️ Две глубины, и они разные. Ниже — сколько ПОКАЗЫВАТЬ из того,
        # что уже есть; здесь — сколько СКАЧАТЬ с биржи. Поля стоят рядом
        # и подписаны глаголами, а не одним словом «глубина»: до 06.09.2026
        # в программе жили три таких числа, и ни одно не совпадало с другим
        # (`D-068`). Цена путаницы названа владельцем счёта: он поставил бы
        # 400 в поле показа и ждал бы года данных, которых никто не скачал.
        #
        # Нуля здесь нет намеренно. «Вся история» — осмысленная просьба
        # к базе, которая уже есть, и бессмысленная к бирже: сколько именно
        # она отдаст, до запроса неизвестно, а подтверждение загрузки обязано
        # называть отрезок числами ДО нажатия.
        self.history_depth_days = _whole(1, 3650, " дн.")
        self.history_depth_days.valueChanged.connect(self._sync_depth)

        # Глубина показа — настройка программы, а не торговли. Ноль означает
        # «вся история»; десять лет сверху взяты как заведомо больший предел,
        # чем есть данных у любого фьючерса.
        self.depth_days = _whole(0, 3650)
        self.depth_days.setSpecialValueText("0 — вся история")
        self.depth_days.valueChanged.connect(self._sync_depth)

    def _build_algorithm_fields(self) -> None:
        """Средняя, четыре развилки поведения и фильтр против пилы."""
        self.average_period = _whole(5, 300)
        self.average_period.valueChanged.connect(self._sync_depth)

        # Четыре развилки торговой логики — обычные списки. Решение 0004:
        # владелец счёта выбрал сделать их опциями, а не зашивать одну ветку.
        self.average_kind = _choice(AverageKind)
        self.reversal_moment = _choice(ReversalMoment)
        self.after_take_profit = _choice(AfterTakeProfit)
        self.on_price_equals_average = _choice(OnPriceEqualsAverage)

        # Фильтр против пилы. Выключен умолчанием — и выключен всеми тремя
        # элементами сразу: галочка снята, порог ноль, подтверждение одна
        # свеча. Это в точности поведение прототипа, и на нём стоит сверка
        # 127 сделок из 127.
        #
        # ⚠️ Галочка отдельно от чисел не для красоты. «0 % и 1 свеча» —
        # верное выражение выключенности, но с экрана оно не читается:
        # состояние приходится выводить из двух чисел в разных строках.
        # Числа при снятой галочке сохраняются, чтобы включение вернуло
        # подобранное, а не умолчание; до движка они при этом не доходят
        # (`app/convert.py::_FILTER_OFF`).
        self.filter_enabled = QCheckBox("Включить фильтр против пилы")
        self.filter_enabled.toggled.connect(self._sync_filter)

        self.threshold_percent = _decimal(0.0, 5.0, 0.05, " %")
        self.threshold_percent.setSpecialValueText("0,00 % — полосы нет")
        self.threshold_percent.valueChanged.connect(self._sync_filter)

        self.confirm_bars = _whole(1, 20)
        self.confirm_bars.setSpecialValueText("1 — подтверждения нет")
        self.confirm_bars.valueChanged.connect(self._sync_filter)

    def _build_take_fields(self) -> None:
        """Фиксация прибыли: обычная цель и скользящий уровень."""
        # Выключатель тейка — ТЗ §4.4 В.
        self.take_profit_enabled = QCheckBox("Фиксировать прибыль по цели")
        self.take_profit_enabled.toggled.connect(self._sync_take)

        # ⚠️ Нижняя граница — ноль, а не 0,1. Ноль здесь не вырожденный
        # случай, а рабочая конфигурация: обе конфигурации-сторожа сверки
        # с прототипом сделаны с выключенным тейком (PROTOTYPE.md §1),
        # и через окно их надо уметь воспроизвести. Прежний диапазон
        # 0,1–5,0 этого не позволял.
        self.take_profit = _decimal(0.0, 5.0, 0.1, " %")
        self.take_profit.setSpecialValueText("0,00 % — тейка нет")
        self.take_profit.valueChanged.connect(self._sync_take)

        # Скользящий тейк. Перенесён в первый этап решением 0009 по требованию
        # владельца счёта; движок его принимает, окно обязано отдавать.
        self.trailing_enabled = QCheckBox("Включить скользящий тейк")
        self.trailing_enabled.toggled.connect(self._sync_take)

        # ⚠️ Нижняя граница обоих полей — ноль, хотя ноль отступа негоден.
        # Границей поля это правило не выражается: Qt подрезал бы пришедшее
        # снаружи значение молча, и окно отправило бы на «ОК» число, которого
        # владелец счёта не вводил. Негодное сочетание ловится проверкой
        # `take_error` и не выпускается наружу запретом кнопок.
        self.trailing_start = _decimal(0.0, 20.0, 0.05, " %")
        self.trailing_start.valueChanged.connect(self._sync_take)

        self.trailing_offset = _decimal(0.0, 20.0, 0.05, " %")
        self.trailing_offset.valueChanged.connect(self._sync_take)

        self.trailing_step = _decimal(0.0, 5.0, 0.01, " %")
        self.trailing_step.setSpecialValueText("0,00 % — без ограничения")
        self.trailing_step.valueChanged.connect(self._sync_take)

        self.take_note = QLabel()
        self.take_note.setWordWrap(True)

    def _build_cost_fields(self) -> None:
        """Издержки: тариф комиссии, шаг цены и проскальзывание."""
        # Тариф комиссии. Ноль означает «не задан», а не «комиссии нет»:
        # подставленный за владельца счёта ноль превратил бы проверку
        # «цель окупает комиссию обеих сторон» в вечное «окупается».
        self.commission = _decimal(0.0, 10_000.0, 1.0, " ₽ за контракт на сторону")
        self.commission.setSpecialValueText("не задан")
        self.commission.valueChanged.connect(self._sync_commission)

        self.commission_note = QLabel()
        self.commission_note.setWordWrap(True)

        # Шаг цены инструмента и проскальзывание в шагах (ТЗ §4.4 Ж).
        # Четыре знака после запятой: у фьючерса на индекс шаг целый, у акций
        # он копеечный, и округление до сотых сделало бы поле негодным для них.
        self.price_step = _decimal(0.0, 1_000_000.0, 1.0, " ₽", decimals=4)
        self.price_step.setSpecialValueText("не задан")
        self.price_step.valueChanged.connect(self._sync_costs)

        # ⚠️ Нижняя граница — ноль, и ноль здесь рабочая конфигурация: ровно
        # так считал прототип, и только на нуле сверка с ним сходится
        # по построению. Половина шага достижима намеренно — на ней сделан
        # замер 05.09.2026 по окну 09:30–11:30.
        self.slippage_steps = _decimal(0.0, 20.0, 0.5, "")
        self.slippage_steps.setSpecialValueText("0 — не учитывать")
        self.slippage_steps.valueChanged.connect(self._sync_costs)

        # Рублей в пункте цены. Пять знаков после запятой не с потолка:
        # у фьючерса на РТС величина 1,73774, и два знака округлили бы её
        # до 1,74 — расхождение в 0,1 % на каждой сделке.
        #
        # ⚠️ Нижняя граница — ноль, хотя ноль негоден. Границей поля это
        # правило не выражается: Qt подрезал бы пришедшее снаружи значение
        # молча. Негодное ловит `costs_error` и не выпускает наружу запретом
        # кнопок — так же, как проскальзывание без шага цены.
        self.ruble_per_point = _decimal(
            0.0, 1_000_000.0, 1.0, " ₽ за пункт", decimals=5
        )
        self.ruble_per_point.setSpecialValueText("не задано")
        # Отдельный слот, а не `_sync_costs`: правка руками обязана **стереть**
        # происхождение числа. Programmatic `setValue` в `set_values` идёт под
        # `QSignalBlocker`, поэтому подстановка с биржи своё происхождение
        # не теряет, а правка человеком — теряет. Без этого окно продолжало бы
        # утверждать «подсказано биржей» под числом, которого биржа не называла.
        self.ruble_per_point.valueChanged.connect(self._forget_point_source)

        #: Откуда взялось число в поле выше. Пусто — не подтверждено биржей.
        self._point_source = ""
        # Не `_alert`: строка тревожная только тогда, когда величина
        # не подтверждена. Подтверждённая — обычная подсказка, и красить её
        # красным значит приучать не читать красное.
        self.point_note = QLabel()
        self.point_note.setWordWrap(True)
        self.costs_note = _alert()

    def _build_time_fields(self) -> None:
        """Торговое окно и календарь нерабочих дней.

        Отдельно от полей объёма и предохранителей, а не одним куском:
        это разные вкладки окна, и общий строитель означал бы, что правка
        одной вкладки читается вперемешку с правкой другой.
        """
        self.window_start = QTimeEdit()
        self.window_start.setDisplayFormat("HH:mm")
        self.window_end = QTimeEdit()
        self.window_end.setDisplayFormat("HH:mm")

        # ⚠️ Умолчание «включено» — то же, что у движка, и на нём стоит сверка
        # с прототипом. Снятая галочка убирает верхнюю границу времени жизни
        # позиции; про это говорит `_sync_window_close`, а не молчит.
        self.close_on_time_end = QCheckBox("Закрывать позицию в конце окна")
        self.close_on_time_end.toggled.connect(self._sync_window_close)
        self.window_close_note = _alert()

        # Календарь нерабочих дней своего поля здесь не имеет: его правят
        # в отдельном окне из меню «Настройки». Но указатель на вкладке
        # обязателен — настройка, которую негде найти, для владельца счёта
        # не существует, а отметки в ней меняют список сделок и на истории.
        self.calendar_note = _hint(
            "Свои нерабочие дни — отпуск, выборы, дурное предчувствие — "
            "отмечаются в отдельном окне: меню «Настройки» → «Календарь "
            "нерабочих дней…». Отметки действуют и на истории, поэтому "
            "проверка на истории отвечает на тот же вопрос, что и бой. "
            "Дни, объявленные нерабочими самой биржей, сюда не попадают "
            "никогда: это факт про мир, а не настройка."
        )

    def _build_risk_fields(self) -> None:
        """Объём сделки, три предохранителя по деньгам и каталог лога."""
        self.volume = _whole(1, 1_000_000, " контрактов")
        # ⚠️ Объём подписан на тот же пересчёт, что и потолок, и это не
        # симметрия ради симметрии: строка под потолком **читает объём**
        # («⚠️ Объём 50 выше потолка 5: заявка подаваться НЕ БУДЕТ»). Без этой
        # связи она пересчитывалась только при правке потолка — владелец счёта
        # уменьшал объём до разрешённого, а красная строка оставалась и
        # продолжала утверждать, что заявка не пойдёт. Красное предупреждение
        # про деньги, которое врёт, хуже его отсутствия: рядом ещё три таких,
        # и перестают верить всем сразу.
        #
        # Правило общее: **поле, чья подсказка зависит от другого поля,
        # пересчитывается при изменении обоих.**
        self.volume.valueChanged.connect(self._sync_volume_cap)

        # ⚠️ У каждого из трёх предохранителей — **своя галочка**, и все три
        # сняты умолчанием. Число без галочки ничего не ограничивает: 5
        # контрактов и 2 % стоят в полях как предложение, а включённые сами
        # собой они означали бы остановку торговли, которой владелец счёта
        # не просил. Цифры предохранителей называет он (`CLAUDE.md`).
        self.volume_cap_enabled = QCheckBox("Ограничить объём заявки")
        self.volume_cap_enabled.toggled.connect(self._sync_volume_cap)
        self.volume_cap = _whole(1, 1_000_000, " контрактов")
        self.volume_cap.valueChanged.connect(self._sync_volume_cap)

        self.daily_loss_limit_enabled = QCheckBox(
            "Останавливать робота при убытке за день"
        )
        self.daily_loss_limit_enabled.toggled.connect(self._sync_guards)
        # Нижняя граница — ноль, а не 0,1: пришедший снаружи ноль Qt подрезал бы
        # молча, и окно отдало бы на «ОК» лимит, которого владелец счёта
        # не ставил. Ноль означает «лимита нет» и так и подписан.
        self.daily_loss_limit = _decimal(0.0, 100.0, 0.1, " % от счёта")
        self.daily_loss_limit.setSpecialValueText("0,00 % — лимита нет")
        self.daily_loss_limit.valueChanged.connect(self._sync_guards)

        self.free_funds_reserve_enabled = QCheckBox(
            "Беречь часть свободных средств"
        )
        self.free_funds_reserve_enabled.toggled.connect(self._sync_guards)
        self.free_funds_reserve = _decimal(
            0.0, 90.0, 1.0, " % свободных средств", decimals=1
        )
        self.free_funds_reserve.valueChanged.connect(self._sync_guards)

        # Каталог технического лога. Пустое поле означает умолчание программы,
        # и так и написано в подсказке поля: пустота, про которую не сказано,
        # читается как «логов нет вовсе».
        self.log_directory = QLineEdit()
        self.log_directory.setPlaceholderText(
            "по умолчанию — папка userdata/logs рядом с программой"
        )

        self.depth_note = _hint("")
        self.filter_note = _alert()

        # Обе строки говорят о состоянии предохранителя: включён он или нет
        # и что будет, когда сработает. Тревожное оформление — не всегда:
        # красным горит только то, что требует внимания сейчас. Красное,
        # которое горит всегда, перестают читать через день.
        self.cap_note = QLabel()
        self.cap_note.setWordWrap(True)
        self.guards_note = QLabel()
        self.guards_note.setWordWrap(True)

    def _build_algorithm_row(self) -> None:
        """Строка «какой алгоритм выбран» и кнопка в окно выбора.

        ⚠️ Строка, а не абзац. До 08.09.2026 здесь стояло полное описание
        правила: 391 точка при умолчаниях, 459 при включённом фильтре против
        пилы — половина вкладки, поля средней уходили под прокрутку. Описание
        переехало в отдельное окно, где его читают целиком и с прокруткой.

        ⚠️ Название приходит **готовым** из каталога (`AlgorithmOption.title`),
        а не пишется здесь: окно торговых слоёв не импортирует
        (ARCHITECTURE.md §2), и второй список названий разошёлся бы с первым
        молча — в окне одно, в журнале другое.
        """
        #: Имя выбранного алгоритма латиницей. Держится отдельно от каталога:
        #: `values()` обязан вернуть его даже тогда, когда каталог не приехал,
        #: иначе «Применить» молча сменил бы алгоритм на умолчание.
        self._strategy_id = Settings().strategy_id
        #: Каталог, пришедший от торговой части. Пустой — окно об этом скажет.
        self._algorithms: tuple[AlgorithmOption, ...] = ()

        self.algorithm_name = QLabel()
        self.algorithm_name.setWordWrap(True)
        # Простой текст, а не разметка: название приходит снаружи, а угадывание
        # разметки съело бы «<» и всё, что за ним, не сказав об этом.
        self.algorithm_name.setTextFormat(Qt.TextFormat.PlainText)
        self.algorithm_name.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        font = self.algorithm_name.font()
        font.setBold(True)
        self.algorithm_name.setFont(font)

        self.algorithm_button = QPushButton("Выбрать алгоритм…")
        # ⚠️ Не кнопка по умолчанию: Enter в окне настроек означает «ОК».
        self.algorithm_button.setAutoDefault(False)
        self.algorithm_button.setDefault(False)
        self.algorithm_button.setToolTip(
            "Открыть список торговых алгоритмов. Там же кнопка «Подробнее» — "
            "она показывает словами, как алгоритм принимает решения."
        )
        self.algorithm_button.clicked.connect(self.choose_algorithm)

        #: Строка про **беду**: каталог не приехал либо выбранного алгоритма
        #: в сборке нет. В обычном случае пустая и спрятанная — вкладка обязана
        #: остаться строкой, а не абзацем, ради чего задача и делалась.
        self.algorithm_note = _hint("")
        self._show_algorithm()

    def _notice(self) -> QLabel:
        label = QLabel(
            "Значения в полях — отправная точка, а не рекомендация. Тейк 0,5% "
            "и окно 10:05–11:00 подобраны на том же отрезке истории, на котором "
            "потом проверялись. Прежде чем ставить их в бой, прогоните проверку "
            "на другом периоде."
        )
        label.setWordWrap(True)
        label.setProperty(HINT_ROLE, True)
        label.setStyleSheet(
            f"color: {current_theme().text_dim}; font-style: italic;"
        )
        return label

    def _group(self, title: str, rows: list[tuple[str, QWidget, str]]) -> QGroupBox:
        box = QGroupBox(title)
        form = QFormLayout(box)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for caption, widget, hint in rows:
            form.addRow(caption, widget)
            if hint:
                widget.setToolTip(hint)
                # Подсказка занимает обе колонки: в узкой колонке поля она
                # разъезжается на шесть строк, и окно перестаёт помещаться.
                form.addRow(_hint(hint))
        return box

    def _instrument_group(self) -> QGroupBox:
        box = self._group("Инструмент и свечи", [
            ("Инструмент", self.instrument,
             "Чем торгуем. Один инструмент за раз — так задумано."),
            ("Размер свечи", self.timeframe,
             "На каких свечах работает робот. Решение он принимает только "
             "на закрытии свечи: на пятиминутке — раз в пять минут."),
            ("Скачивать историю за, дней", self.history_depth_days,
             "Сколько последних дней качать с биржи, когда вы нажимаете "
             "«Загрузить историю…» в меню «Программа». Это про добычу данных, "
             "а не про показ: ниже стоит второе поле — сколько из добытого "
             "показывать. Девяносто дней — примерно вся жизнь одного "
             "фьючерсного контракта в ближней позиции, дальше по нему идут "
             "огрызки: свечи есть, но их единицы минут в день. Больше дней — "
             "дольше загрузка: биржа отдаёт не быстрее пяти запросов в секунду."),
            ("Показывать на графике, дней", self.depth_days,
             "Сколько последних дней истории показывать на графике и прогонять. "
             "Это настройка показа, а не торговли: список сделок от неё не "
             "меняется — меняется, сколько их видно. Больше дней — дольше "
             "считается прогон при каждом изменении настроек. Ноль означает "
             "всю историю, что есть в базе. Показать больше, чем скачано, "
             "нельзя: покажется то, что есть."),
        ])
        layout = box.layout()
        if isinstance(layout, QFormLayout):
            # ⚠️ Строка про прогрев встаёт **под своим полем**, а не в конце
            # группы: внизу она читалась как продолжение подсказки про соседа.
            # Номер строки берётся у самой раскладки, а не пишется числом, —
            # вставка поля выше сдвинула бы жёсткий номер молча.
            row = cast("tuple[int, object]", layout.getWidgetPosition(self.depth_days))
            layout.insertRow(row[0] + 2, self.depth_note)
        return box

    def _point_group(self) -> QGroupBox:
        """Свойства самого инструмента: шаг цены и сколько рублей в пункте.

        Стоят рядом с инструментом, а не среди денег, намеренно: это не выбор
        владельца счёта, а свойства контракта, которые программа спрашивает
        у биржи и подставляет сама. Менять их приходится ровно тогда, когда
        сменился инструмент, — то есть вместе с полем «Инструмент», а не
        вместе с комиссией.
        """
        box = self._group("Свойства инструмента", [
            ("Шаг цены инструмента", self.price_step,
             "Наименьшее движение цены этого инструмента — из карточки "
             "инструмента на сайте биржи. Нужен для поправки на "
             "проскальзывание на вкладке «Деньги»: она задаётся в шагах, "
             "потому что «полшага» на фьючерсе и на акции — разные деньги."),
            ("Рублей в пункте цены", self.ruble_per_point,
             "На сколько рублей меняется ваш результат, когда цена проходит "
             "один пункт. Программа спрашивает это число у биржи и подставляет "
             "сама; вы вправе его перебить. Величина у каждого контракта своя "
             "и у некоторых плавает изо дня в день: замер 05.09.2026 — "
             "фьючерс на индекс МосБиржи 1 ₽, на РТС 1,74 ₽, на Брент 869 ₽. "
             "⚠️ Ошибка здесь тихая: сделки в отчёте останутся те же, а деньги "
             "будут другими — по Бренту в 869 раз."),
        ])
        layout = box.layout()
        if isinstance(layout, QFormLayout):
            layout.addRow(self.point_note)
        return box

    def _algorithm_group(self) -> QGroupBox:
        """Какой алгоритм выбран — строкой, и кнопка, открывающая выбор.

        Окно показывает «Период средней» и «Порог пересечения» и само по себе
        не говорит, **что** программа с ними делает: узнать это можно было
        только прочитав исходник. Владелец счёта не программист, а решения
        по этим полям — решения о деньгах. Ответ живёт за кнопкой ниже,
        в окне «Торговый алгоритм» → «Подробнее».
        """
        box = QGroupBox("Торговый алгоритм")
        body = QVBoxLayout(box)
        row = QHBoxLayout()
        row.addWidget(self.algorithm_name, 1)
        row.addWidget(self.algorithm_button)
        body.addLayout(row)
        body.addWidget(self.algorithm_note)
        body.addWidget(_hint(
            "Алгоритм решает только направление: лонг, шорт или ничего. "
            "Поля ниже — его настройки; что он с ними делает, показывает "
            "кнопка «Подробнее» в окне выбора."
        ))
        return box

    def _signal_group(self) -> QGroupBox:
        box = self._group("Средняя и сигнал", [
            ("Период средней", self.average_period,
             "На скольких свечах считается линия средней. Больше значение — "
             "линия спокойнее и сигналов меньше; меньше — сигналов больше "
             "и больше комиссии. В прототипе стоит 15."),
            ("Тип средней", self.average_kind,
             "Экспоненциальная сильнее реагирует на последние свечи, простая "
             "считает все свечи одинаково. Все известные результаты получены "
             "на экспоненциальной. Простая удваивает время проверки на истории."),
            ("Цена закрылась ровно на средней", self.on_price_equals_average,
             "Редкий случай: закрытие свечи совпало со средней до копейки. Ваш "
             "нынешний робот в этот момент не делает ничего — и остаётся вне рынка. "
             "На проверенном отрезке истории случай не встретился ни разу."),
        ])
        return box

    def _filter_group(self) -> QGroupBox:
        box = self._group("Фильтр против пилы", [
            ("Фильтр против пилы", self.filter_enabled,
             "Против того случая, когда цена ходит вплотную к средней: робот "
             "переворачивается на каждой свече и платит комиссию за каждый "
             "переворот. Замер 05.09.2026 на ваших данных, 79 дней, окно "
             "10:05–11:00: выключено — 111 сделок, чистая +2 151 ₽, просадка "
             "8 718 ₽; порог 0,04 % — 93 сделки, +8 327 ₽, просадка 7 950 ₽; "
             "подтверждение 3 свечами — 71 сделка, +10 340 ₽, просадка 6 362 ₽. "
             "На дне 03.09, где вы поймали четыре переворота подряд, "
             "подтверждение оставило одну сделку и −103 ₽ вместо шести "
             "и −1 268 ₽. ⚠️ И вторая половина того же замера: ни один "
             "из вариантов статистически от нуля не отличается — стандартная "
             "ошибка итога 6–8,5 тыс. ₽ при самом итоге 2–10 тыс. ₽. Это "
             "означает «79 дней мало», а не «фильтр работает»."),
            ("Порог пересечения", self.threshold_percent,
             "Полоса вокруг средней, внутри которой цена сигналом не считается: "
             "чтобы перевернуться, цена обязана уйти за среднюю на эту величину. "
             "Ноль означает «полосы нет» — так работает ваш нынешний робот, "
             "и сверка с ним сделана на нуле. ⚠️ Лучшая на подборе цифра "
             "провалилась на проверке: порог 0,20 % дал +7 048 ₽ на первой "
             "половине отрезка и −3 497 ₽ на второй. Обе половины пережил "
             "только порог 0,04 %."),
            ("Подтверждение сигнала, свечей подряд", self.confirm_bars,
             "Сколько свечей подряд цена должна закрываться по одну сторону "
             "средней, прежде чем робот войдёт. Одна свеча означает «без "
             "подтверждения», как сейчас. Больше — входов меньше и они позже. "
             "⚠️ Соседние значения скачут: 3 свечи дали +10 340 ₽, 4 свечи "
             "−889 ₽, 5 свечей +7 656 ₽. Соседи так вести себя не должны — это "
             "мера шума на 79 днях, а не свойство рынка. Обе половины отрезка "
             "пережили только 3 и 6."),
        ])
        layout = box.layout()
        if isinstance(layout, QFormLayout):
            layout.addRow(self.filter_note)
        return box

    def _entry_group(self) -> QGroupBox:
        """Две развилки про то, когда робот меняет сторону и когда молчит.

        Лежат рядом с фиксацией прибыли, а не рядом со средней, потому что
        обе отвечают на один вопрос — что делать с **позицией**, а не как
        прочитать сигнал. Вторая из них прямо продолжает тейк: «после
        сработавшего тейка» без тейка не значит ничего.
        """
        box = self._group("Переворот и вход", [
            ("Момент переворота", self.reversal_moment,
             "Когда робот разворачивается по обратному сигналу. «Через свечу» — "
             "так делает ваш нынешний робот, и все замеры сделаны на этом. "
             "«В той же свече» быстрее, но цифр по нему пока нет: они появятся "
             "после проверки на истории."),
            ("После сработавшего тейка", self.after_take_profit,
             "Что делать остаток дня, когда прибыль уже зафиксирована. «Не входить» — "
             "как сейчас. «Сразу восстановить» — это ваше «всегда должна быть позиция» "
             "в чистом виде, но и самый дорогой вариант по комиссии."),
        ])
        return box

    def _take_group(self) -> QGroupBox:
        box = self._group("Фиксация прибыли", [
            ("Тейк-профит", self.take_profit_enabled,
             "Снятая галочка означает, что цели по прибыли нет вовсе: позицию "
             "держит только переворот по средней. Так сделаны две проверочные "
             "конфигурации, на которых сверяется расчёт свечи. ⚠️ Без цели "
             "прибыли стратегия убыточна на любом торговом окне — на замеренном "
             "отрезке от −3 038 до −108 366 ₽. Выключать осознанно."),
            ("Цель прибыли", self.take_profit,
             "Насколько цена должна уйти в плюс от цены входа, чтобы позиция "
             "закрылась. Ноль означает то же, что снятая галочка выше: цели нет. "
             "Уровень выставляется заявкой у брокера и живёт всё время позиции — "
             "он сработает, даже если программа закрыта."),
            ("Скользящий тейк", self.trailing_enabled,
             "Обычная цель стоит на месте: дошли — закрылись. Скользящая едет "
             "за ценой вслед и забирает больше, если движение продолжилось. "
             "⚠️ Замеров по ней у проекта нет ни одного: она не воспроизводит "
             "поведение вашего нынешнего робота, а добавляет новое."),
            ("Порог включения", self.trailing_start,
             "Насколько прибыль должна вырасти, прежде чем скользящий уровень "
             "вообще появится. Пока порог не пройден, позицию держит переворот "
             "по средней, а уровня выхода нет."),
            ("Отступ", self.trailing_offset,
             "Насколько уровень выхода отстаёт от лучшей достигнутой цены. "
             "Цена откатилась на эту величину — позиция закрывается. Назад "
             "уровень не едет никогда."),
            ("Шаг подтяжки", self.trailing_step,
             "Насколько цена должна пройти, чтобы уровень сдвинулся. Не "
             "украшение: без шага каждая свеча с новой лучшей ценой — это "
             "отдельное обращение к брокеру, до двенадцати за час позиции."),
        ])
        layout = box.layout()
        if isinstance(layout, QFormLayout):
            layout.addRow(self.take_note)
        return box

    def _commission_group(self) -> QGroupBox:
        box = self._group("Издержки", [
            ("Комиссия", self.commission,
             "Сколько вы платите за один контракт при покупке и столько же "
             "при продаже — из вашего тарифа у брокера, вместе с биржевым "
             "сбором. Пока не задана, программа не может проверить, окупает ли "
             "цель прибыли издержки обеих сторон, и пишет об этом "
             "предупреждение на каждой позиции."),
            ("Проскальзывание, шагов цены", self.slippage_steps,
             "Насколько цена сделки на счёте хуже расчётной. Прогон по истории "
             "считает, что заявка исполнилась ровно по расчётной цене; на счёте "
             "она берётся из стакана и всегда чуть хуже. Считается в шагах цены, "
             "а сам шаг задаётся на вкладке «Инструмент и данные». ⚠️ Это "
             "не мелочь: замер 05.09.2026 на фьючерсе на индекс — ноль шагов "
             "дают +9 908 ₽, один шаг +4 394 ₽, два шага −153 ₽. Умолчание "
             "ноль: так считает ваш нынешний робот, и только на нуле сходится "
             "сверка с ним."),
        ])
        layout = box.layout()
        if isinstance(layout, QFormLayout):
            layout.addRow(self.commission_note)
            layout.addRow(self.costs_note)
        return box

    def _time_group(self) -> QGroupBox:
        box = self._group("Время работы (московское)", [
            ("Начало окна", self.window_start,
             "С какого времени робот может открывать позиции. Время московское, "
             "даже если компьютер стоит в другом поясе."),
            ("Конец окна", self.window_end,
             "До какого времени. Граница считается по времени закрытия свечи "
             "и в окно не входит: свеча, закрывшаяся ровно в конце окна, "
             "сигналом уже не считается."),
            ("В конце окна", self.close_on_time_end,
             "Галочка стоит — позиция закрывается по времени, что бы ни было "
             "на графике. Галочка снята — держим до сигнала средней, тейка или "
             "переворота, и позиция может остаться открытой на ночь и на "
             "выходные. ⚠️ Все замеры проекта и сверка с вашим нынешним "
             "роботом сделаны с поставленной галочкой."),
        ])
        layout = box.layout()
        if isinstance(layout, QFormLayout):
            layout.addRow(self.window_close_note)
            layout.addRow(self.calendar_note)
        return box

    def _program_group(self) -> QGroupBox:
        """Настройки самой программы, а не торговли.

        Стоят отдельной вкладкой намеренно: смешивать их с параметрами,
        от которых зависят деньги, — значит приучать пролистывать глазами
        всё окно одинаково.
        """
        return self._group("Программа", [
            ("Куда писать журнал работы", self.log_directory,
             "Папка для технического журнала — того, в который программа пишет "
             "отказы связи, ошибки брокера и подробности для разбора. Пустое "
             "поле означает папку по умолчанию рядом с программой. Торговый "
             "журнал и журнал решений здесь ни при чём: они лежат в базе "
             "и выгружаются кнопкой под таблицами."),
        ])

    def _volume_group(self) -> QGroupBox:
        box = self._group("Объём сделки", [
            ("Объём сделки", self.volume,
             "Сколько контрактов покупаем или продаём за раз. Новое значение "
             "действует со следующей сделки — уже открытую позицию оно не трогает: "
             "робот не будет ни доливать, ни отрезать."),
            ("Потолок объёма", self.volume_cap_enabled,
             "Галочка снята — потолка нет, робот подаёт заявку любого объёма, "
             "какой стоит выше. Галочка стоит — заявка на вход объёмом больше "
             "потолка НЕ ПОДАЁТСЯ ВОВСЕ: робот пропускает сигнал и пишет "
             "причину в журнал решений. Он не подаёт «поменьше» — это была бы "
             "догадка за вас. Заведено против опечатки в один знак: она меняет "
             "последствия в десять раз. Выход из позиции потолок не проверяет "
             "никогда — иначе позиция крупнее потолка осталась бы в рынке "
             "навсегда."),
            ("Потолок объёма, контрактов", self.volume_cap,
             "Сколько контрактов в одной заявке на вход — предел, а не цель. "
             "Объём, равный потолку, проходит: потолок — это «не больше чем». "
             "Число 5 в поле ничем не подкреплено, это предложение; цифру "
             "называете вы."),
        ])
        layout = box.layout()
        if isinstance(layout, QFormLayout):
            layout.addRow(self.cap_note)
        return box

    def _risk_group(self) -> QGroupBox:
        box = self._group("Ограничение убытка", [
            ("Дневной лимит убытка", self.daily_loss_limit_enabled,
             "Галочка снята — робот за дневным убытком не следит и сам "
             "не остановится. Галочка стоит — дойдя до лимита, он закрывает "
             "позицию и прекращает торговать до конца дня; включить его снова "
             "можно только руками, само не возобновится. В убыток считается "
             "и бумажная переоценка открытой позиции, а не только "
             "зафиксированное: иначе лимит молчал бы ровно тогда, когда "
             "убыток самый большой. Решение принимается на закрытии свечи — "
             "убыток, сходивший за лимит и вернувшийся внутри пятиминутки, "
             "робота не остановит."),
            ("Дневной лимит убытка, % от счёта", self.daily_loss_limit,
             "Сколько процентов счёта вы готовы потерять за день. Считается "
             "от размера счёта на первый снимок этой даты, без пересчёта "
             "внутри дня: иначе просадка сама уменьшала бы порог и остановка "
             "наступала бы всё раньше. Цифру называете вы — это ответ "
             "на вопрос «сколько я готов потерять», а не расчётная величина."),
            ("Минимальный запас средств", self.free_funds_reserve_enabled,
             "Галочка снята — робот занимает под обеспечение все свободные "
             "деньги. Галочка стоит — эту долю денег он под обеспечение "
             "не берёт, и если на полный объём с запасом не хватает, входит "
             "УМЕНЬШЕННЫМ объёмом, а не пропускает сигнал; не хватает даже "
             "на один контракт — вход пропускается. Запас нужен на случай, "
             "когда биржа поднимает требования к обеспечению прямо внутри дня: "
             "без него позицию закроет брокер — по своей цене и в свой момент."),
            ("Минимальный запас средств, %", self.free_funds_reserve,
             "Какую долю свободных денег не трогать. Вычитается до расчёта "
             "числа контрактов, а не после: запас, занятый под последний "
             "рубль, запасом не является."),
        ])
        layout = box.layout()
        if isinstance(layout, QFormLayout):
            layout.insertRow(0, self.guards_note)
        return box

    # -------------------------------------------------------------- вкладки

    def _build_tabs(self) -> QTabWidget:
        """Шесть вкладок вместо свитка на тридцать четыре поля.

        Просьба владельца счёта 06.09.2026 дословно: «настройки разрослись
        надо как то делить визуально листать портянку неудобно,
        неинформативно».

        Раскладка задана **данными** (`PLACEMENT`), а не порядком вызовов
        здесь, и это не оформление. Таблица отвечает на вопрос, который иначе
        задать некому: попало ли каждое поле `Settings` ровно на одну вкладку.
        Полей тридцать пять; добавление тридцать шестого обязано ронять
        прогон, а не тихо оставлять поле без места на экране.
        """
        tabs = QTabWidget()
        for title, builders in _TAB_GROUPS:
            page = QWidget()
            body = QVBoxLayout(page)
            body.setContentsMargins(12, 12, 12, 12)
            for name in builders:
                body.addWidget(getattr(self, name)())
            body.addStretch(1)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            tabs.addTab(scroll, title)
        return tabs

    def set_algorithms(self, options: Sequence[AlgorithmOption]) -> None:
        """Показать каталог алгоритмов. Пустой — сказать, что он не приехал.

        Каталог приходит от торговой части и обновляется на каждом применении
        настроек: описание выбранного алгоритма считается по **применённым**
        числам, а не по тому, что стоит в полях сию секунду.

        ⚠️ Выбор в `Settings` этот вызов **не меняет**. Каталог — это то, из
        чего выбирают; выбранное имя держится отдельно и меняется только
        мышкой человека либо приходом настроек (`set_values`).
        """
        self._algorithms = tuple(options)
        self._show_algorithm()

    def choose_algorithm(self) -> None:
        """Открыть окно выбора алгоритма и запомнить выбранное.

        Ничего не применяет: имя ложится в поля окна и уходит движку обычным
        путём, по «Применить» или «ОК», вместе с остальными настройками.
        Отдельная дорога к движку означала бы смену торгового правила
        в обход подтверждения «было → стало».
        """
        dialog = AlgorithmDialog(self._algorithms, self)
        dialog.set_chosen(self._strategy_id)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            chosen = dialog.chosen_id()
        finally:
            # Без этого окно живёт до конца работы программы: родителем ему
            # назначено окно настроек, а `exec()` его не удаляет.
            dialog.deleteLater()
        if not chosen:
            return
        self._strategy_id = chosen
        self._show_algorithm()

    def _show_algorithm(self) -> None:
        """Название выбранного алгоритма и оговорка, если что-то не так.

        Три случая, и каждый говорится вслух.

        * Всё в порядке — **только название**, строка беды спрятана. Правило
          словами живёт в окне выбора: абзац на вкладке занимал 391–459 точек
          и вытеснял поля средней под прокрутку (замер 08.09.2026), ради чего
          задача и делалась.
        * Каталог не приехал — показано имя латиницей и сказано почему.
          Окно, открытое без торговой части, обязано назвать себя таким.
        * Каталог есть, а выбранного в нём нет — это сборка постарше, чем
          файл настроек, и молчать нельзя: робот таким алгоритмом работать
          не сможет.
        """
        found = next(
            (item for item in self._algorithms if item.id == self._strategy_id),
            None,
        )
        if found is not None:
            self.algorithm_name.setText(found.title)
            # Правило одной фразой — во всплывающей подсказке: тому, кто ведёт
            # мышью, оно достаётся без нажатий, а вкладка остаётся строкой.
            self.algorithm_name.setToolTip(found.summary)
            self._say_about_the_algorithm("", alarming=False)
            return
        self.algorithm_name.setText(self._strategy_id or "не выбран")
        self.algorithm_name.setToolTip("")
        if not self._algorithms:
            self._say_about_the_algorithm(CATALOGUE_NOT_ARRIVED, alarming=False)
            return
        self._say_about_the_algorithm(
            f"⚠️ Алгоритма «{self._strategy_id}» в этой сборке нет — робот "
            "работать им не сможет. Скорее всего настройки или шаблон сделаны "
            "более новой сборкой программы. Выберите алгоритм из списка "
            "кнопкой справа либо обновите программу целиком.",
            alarming=True,
        )

    def _say_about_the_algorithm(self, text: str, *, alarming: bool) -> None:
        """Строка беды под названием. Пустая — спрятать, а не оставить пробел.

        ⚠️ Спрятать, а не очистить: пустой ярлык с переносом строк держит
        высоту строки и раздвигает группу на пустом месте — ровно то, против
        чего эта вкладка и перестраивалась.
        """
        self._paint_note(self.algorithm_note, alarming=alarming)
        self.algorithm_note.setText(text)
        self.algorithm_note.setVisible(bool(text))

    def page_of(self, tab: str) -> QWidget | None:
        """Страница вкладки по её заголовку. `None` — такой вкладки нет.

        Нужна проверке: без неё «поле лежит на той вкладке, которую называет
        таблица» проверить нечем, и таблица разошлась бы с окном молча.
        """
        for index in range(self.tabs.count()):
            if self.tabs.tabText(index) == tab:
                return self.tabs.widget(index)
        return None

    def _set_tab_order(self) -> None:
        """Обход по Tab идёт сверху вниз и вкладка за вкладкой, как на экране.

        По умолчанию Qt строит цепочку в порядке создания виджетов, а он у нас
        не совпадает с порядком на экране: поля объявлены одним списком, а
        разложены по вкладкам и разделам. Владелец счёта, заполняющий настройки
        с клавиатуры, прыгал бы из «объёма» в «дневной лимит».

        Цепочка одна на всё окно, а не своя на каждую вкладку, и это верно:
        Qt внутри вкладки ходит по своему куску общей цепочки, а порядок
        кусков совпадает с порядком вкладок.
        """
        order = [
            # «Инструмент и данные»
            self.instrument, self.timeframe,
            self.history_depth_days, self.depth_days,
            self.price_step, self.ruble_per_point,
            # «Сигнал»
            self.algorithm_button,
            self.average_period, self.average_kind,
            self.on_price_equals_average,
            self.filter_enabled, self.threshold_percent, self.confirm_bars,
            # «Вход и выход»
            self.reversal_moment, self.after_take_profit,
            self.take_profit_enabled, self.take_profit,
            self.trailing_enabled, self.trailing_start, self.trailing_offset,
            self.trailing_step,
            # «Торговое окно»
            self.window_start, self.window_end, self.close_on_time_end,
            # «Деньги»
            self.volume, self.volume_cap_enabled, self.volume_cap,
            self.daily_loss_limit_enabled, self.daily_loss_limit,
            self.free_funds_reserve_enabled, self.free_funds_reserve,
            self.commission, self.slippage_steps,
            # «Программа»
            self.log_directory,
        ]
        for previous, following in zip(order, order[1:]):
            QWidget.setTabOrder(previous, following)
        QWidget.setTabOrder(order[-1], self.buttons)

    # ------------------------------------------------------------------ обмен

    def set_values(self, settings: Settings) -> None:
        """Показать настройки в полях."""
        self.instrument.setText(settings.instrument)
        self._show_timeframe(settings.timeframe)
        self.depth_days.setValue(settings.depth_days)
        self.history_depth_days.setValue(max(settings.history_depth_days, 1))
        self._take_algorithm(settings.strategy_id)
        self.average_period.setValue(settings.average_period)
        self.threshold_percent.setValue(settings.threshold_percent)
        self.confirm_bars.setValue(settings.confirm_bars)
        for field, value in (
            (self.average_kind, settings.average_kind),
            (self.reversal_moment, settings.reversal_moment),
            (self.after_take_profit, settings.after_take_profit),
            (self.on_price_equals_average, settings.on_price_equals_average),
        ):
            field.setCurrentIndex(max(field.findData(value), 0))
        # Выключатели — одной таблицей, как и списки выше. Не ради длины:
        # выключатель, дописанный в поля и забытый здесь, возвращался бы
        # к умолчанию при каждом «Применить» и молча — ровно так жили
        # «закрывать в конце окна» и фильтр против пилы. Строк стало меньше,
        # а мест, где можно забыть, — одно вместо четырёх.
        for switch, state in (
            (self.filter_enabled, settings.filter_enabled),
            (self.take_profit_enabled, settings.take_profit_enabled),
            (self.trailing_enabled, settings.trailing_enabled),
            (self.close_on_time_end, settings.close_on_time_end),
            (self.volume_cap_enabled, settings.volume_cap_enabled),
            (self.daily_loss_limit_enabled, settings.daily_loss_limit_enabled),
            (self.free_funds_reserve_enabled, settings.free_funds_reserve_enabled),
        ):
            switch.setChecked(state)
        self.take_profit.setValue(settings.take_profit_pct)
        # ⚠️ Порог и отступ ставятся без хитростей и БЕЗ взаимных границ.
        # Соблазн выразить правило «порог больше отступа» границами полей
        # велик, и он же превращает окно в молчаливого правщика: пришедшее
        # снаружи сочетание Qt подрезал бы сам, а на «ОК» ушло бы значение,
        # которого владелец счёта не вводил. Правило проверяется и говорится
        # вслух в `_sync_take`, а негодное сочетание не выпускается наружу
        # запретом кнопок.
        self.trailing_start.setValue(settings.trailing_start_pct)
        self.trailing_offset.setValue(settings.trailing_offset_pct)
        self.trailing_step.setValue(settings.trailing_step_pct)
        self.commission.setValue(
            0.0 if settings.commission_per_side_rub is None
            else settings.commission_per_side_rub
        )
        self.price_step.setValue(settings.price_step)
        blocker_point = QSignalBlocker(self.ruble_per_point)
        self.ruble_per_point.setValue(settings.ruble_per_point)
        del blocker_point
        self._point_source = settings.ruble_per_point_source
        # ⚠️ Календарь нерабочих дней в этом окне не показывается: его открывают
        # из меню «Настройки» (`ui/calendar_dialog.py`). Но пронести его через
        # обмен обязательно — `values()` собирает `Settings` целиком, и поле,
        # здесь забытое, обнулялось бы при каждом «Применить». Ровно так жили
        # «закрывать в конце окна» и фильтр против пилы.
        self._calendar = settings.calendar
        self.slippage_steps.setValue(settings.slippage_steps)
        self.log_directory.setText(settings.log_directory)
        self.window_start.setTime(QTime(settings.window_start.hour, settings.window_start.minute))
        self.window_end.setTime(QTime(settings.window_end.hour, settings.window_end.minute))
        self._show_volume_and_guards(settings)
        self._sync_take()
        self._sync_commission()
        self._sync_costs()
        self._sync_depth()
        self._sync_filter()
        self._sync_window_close()

    def _show_timeframe(self, timeframe: str) -> None:
        """Размер свечи в списке. Незнакомый ДОБАВЛЯЕТСЯ, а не подменяется.

        Прежняя подмена на «5 минут» была молчаливой и меняла стратегию
        целиком: настройка приходила одна, на «ОК» уходила другая, и в журнале
        решений этой правки не было. Правило файла №1 — значение, пришедшее
        снаружи, не подменяется молча — исключений для списков не знает.

        Отдельным шагом от `set_values`, потому что это единственное поле
        обмена с ветвлением, и объяснение к нему длиннее самого поля.
        """
        index = self.timeframe.findText(timeframe)
        if index < 0 and timeframe:
            self.timeframe.insertItem(0, timeframe)
            index = 0
        self.timeframe.setCurrentIndex(max(index, 0))

    def _take_algorithm(self, strategy_id: str) -> None:
        """Имя выбранного алгоритма — как пришло, без сверки с каталогом.

        ⚠️ Незнакомое имя показывается как есть, а `_show_algorithm` говорит
        вслух, что такого алгоритма в этой сборке нет. Подстановка умолчания
        была бы сменой торгового правила без ведома владельца счёта: на экране
        всё выглядело бы исправно, а робот работал бы не тем, что записано
        в настройках.
        """
        self._strategy_id = strategy_id
        self._show_algorithm()

    def _show_volume_and_guards(self, settings: Settings) -> None:
        """Объём, потолок и два процента предохранителей — отдельным шагом.

        Отдельно от `set_values` не ради длины, а потому что здесь
        единственное место окна, где показ значения требует **порядка**
        и перекрытых сигналов: остальные поля ставятся в любом порядке
        и ничего друг о друге не знают.

        Объём показывается настоящий, а не подрезанный потолком. Окно,
        молча уменьшившее объём, отправит на «ОК» изменение параметра
        по деньгам, которого владелец счёта не делал и которого не будет
        в журнале решений. Что делать с объёмом выше потолка, решает движок.

        Сигналы на время загрузки перекрыты намеренно. `volume_cap.setValue()`
        синхронно дёргает `_sync_volume_cap`, а тот считает максимум по ещё
        не обновлённому значению объёма — и сужает его до потолка ровно перед
        тем, как мы поставим настоящее число. Прежняя правка «сначала поднять
        максимум» этого не спасала: пересчёт по сигналу шёл после неё.
        """
        blocker_volume = QSignalBlocker(self.volume)
        blocker_cap = QSignalBlocker(self.volume_cap)
        self.volume.setMaximum(max(settings.volume, settings.volume_cap, 1))
        self.volume_cap.setValue(settings.volume_cap)
        self.volume.setValue(settings.volume)
        del blocker_volume, blocker_cap
        self.daily_loss_limit.setValue(settings.daily_loss_limit_pct)
        self.free_funds_reserve.setValue(settings.free_funds_reserve_pct)
        self._sync_volume_cap()
        self._sync_guards()

    def values(self) -> Settings:
        """Собрать настройки из полей."""
        start = self.window_start.time()
        end = self.window_end.time()
        return Settings(
            instrument=self.instrument.text().strip(),
            timeframe=self.timeframe.currentText(),
            depth_days=self.depth_days.value(),
            history_depth_days=self.history_depth_days.value(),
            # ⚠️ Имя алгоритма отдаётся как есть, даже если каталог не приехал
            # и названия мы не знаем. Подстановка умолчания на этом месте была
            # бы сменой торгового правила, которой владелец счёта не делал
            # и которой не будет в журнале решений.
            strategy_id=self._strategy_id,
            average_period=self.average_period.value(),
            # ⚠️ Числа отдаются такими, какие стоят в полях, независимо
            # от галочки. Выключенный фильтр — это не «нули в окне»,
            # а подмена на границе с торговым модулем
            # (`app/convert.py::_FILTER_OFF`). Обнулить их здесь значило бы
            # потерять подобранное при первом же снятии галочки.
            filter_enabled=self.filter_enabled.isChecked(),
            threshold_percent=self.threshold_percent.value(),
            confirm_bars=self.confirm_bars.value(),
            average_kind=self.average_kind.currentData(),
            reversal_moment=self.reversal_moment.currentData(),
            after_take_profit=self.after_take_profit.currentData(),
            on_price_equals_average=self.on_price_equals_average.currentData(),
            take_profit_enabled=self.take_profit_enabled.isChecked(),
            take_profit_pct=self.take_profit.value(),
            trailing_enabled=self.trailing_enabled.isChecked(),
            trailing_start_pct=self.trailing_start.value(),
            trailing_offset_pct=self.trailing_offset.value(),
            trailing_step_pct=self.trailing_step.value(),
            window_start=start.toPython(),
            window_end=end.toPython(),
            close_on_time_end=self.close_on_time_end.isChecked(),
            price_step=self.price_step.value(),
            ruble_per_point=self.ruble_per_point.value(),
            # ⚠️ Происхождение числа переживает обмен, но **не переживает
            # правку руками**: `_sync_costs` стирает его, как только владелец
            # счёта тронул поле. Иначе окно продолжало бы утверждать
            # «подсказано биржей» под числом, которого биржа не называла.
            ruble_per_point_source=self._point_source,
            # Календарь окно не правит, а переносит: правится он в своём окне.
            calendar=self._calendar,
            slippage_steps=self.slippage_steps.value(),
            log_directory=self.log_directory.text().strip(),
            volume=self.volume.value(),
            # ⚠️ Числа отдаются как стоят, независимо от галочек. «Выключено»
            # выражается на границе с движком (`app/convert.py::_GUARDS`),
            # а не обнулением полей: обнулив, окно потеряло бы цифры, которые
            # владелец счёта подбирал, при первом же снятии галочки.
            volume_cap_enabled=self.volume_cap_enabled.isChecked(),
            volume_cap=self.volume_cap.value(),
            daily_loss_limit_enabled=self.daily_loss_limit_enabled.isChecked(),
            daily_loss_limit_pct=self.daily_loss_limit.value(),
            free_funds_reserve_enabled=self.free_funds_reserve_enabled.isChecked(),
            free_funds_reserve_pct=self.free_funds_reserve.value(),
            # Ноль в поле — это «тариф не задан», а не «комиссии нет».
            # Разница в том, что делает движок: при `None` он пишет, что
            # правило «цель окупает комиссию обеих сторон» НЕ ПРОВЕРЕНО,
            # а при нуле объявил бы окупающейся любую цель.
            commission_per_side_rub=(
                None if self.commission.value() <= 0 else self.commission.value()
            ),
        )

    def _sync_volume_cap(self, *_: object) -> None:
        """Состояние потолка объёма: включён или нет и что из этого следует.

        ⚠️ Тексты здесь **в настоящем времени**, и это стало правдой
        05.09.2026: проверка живёт в `engine/guards.py::entry_size`, а число
        доезжает до неё через галочку (`app/convert.py::_GUARDS`). До того
        здесь стояло будущее время и прямая отметка «предохранитель ещё
        не включён» — обещание в настоящем времени тогда снимало бы
        настороженность там, где защиты нет.

        ⚠️ Обратное правило остаётся в силе: **снятая галочка обязана
        говорить, что защиты нет**. Пустая строка на этом месте читалась бы
        как «всё в порядке».
        """
        on = self.volume_cap_enabled.isChecked()
        cap = self.volume_cap.value()
        self.volume_cap.setEnabled(on)
        if not on:
            # Потолок снят — ввод объёма он не ограничивает. Верхняя граница
            # поля возвращается к своей собственной, а не остаётся зажатой
            # числом, которое ничего не значит.
            self.volume.setMaximum(1_000_000)
            self._paint_note(self.cap_note, alarming=False)
            self.cap_note.setText(
                "Потолка объёма нет: заявка уйдёт тем объёмом, который стоит "
                "выше, каким бы он ни был. Опечатка в один знак меняет "
                "последствия в десять раз, и остановить её будет некому."
            )
            return
        # Потолок ограничивает ввод, но НЕ подрезает уже пришедшее значение:
        # если движок отдал объём выше потолка, окно обязано показать настоящее
        # число и сказать вслух, что оно выше. Подмена на «ОК» превратилась бы
        # в изменение параметра по деньгам, которого владелец счёта не делал
        # и которого нет в журнале решений.
        self.volume.setMaximum(max(cap, self.volume.value(), 1))
        if self.volume.value() > cap:
            self._paint_note(self.cap_note, alarming=True)
            self.cap_note.setText(
                f"⚠️ Объём {self.volume.value()} выше потолка {cap}: заявка "
                "на вход подаваться НЕ БУДЕТ вовсе, робот пропустит сигнал "
                "и напишет причину в журнал. Уменьшите объём или поднимите "
                "потолок — сам он «поменьше» не подаст."
            )
            return
        self._paint_note(self.cap_note, alarming=False)
        self.cap_note.setText(
            f"Потолок объёма: {cap}. Заявка на вход объёмом больше него "
            "не подаётся; объём ровно по потолку проходит. Выход из позиции "
            "через потолок не проверяется никогда."
        )

    def _sync_guards(self, *_: object) -> None:
        """Строка про два предохранителя, считающих от размера счёта.

        ⚠️ **Оба они сегодня слепы, и об этом сказано заранее.** Дневной
        лимит считается от размера счёта на утро, запас средств — от свободных
        денег и ГО контракта; ни того, ни другого программе никто не сообщает:
        портфель у брокера ещё не читается. Включённый предохранитель при
        этом не молчит — движок пишет «НЕ ПРОВЕРЕНО» в строку входа
        (`engine/pipeline.py::_blind_note`), — но узнавать об этом из журнала
        задним числом хуже, чем прочитать в настройках заранее.
        """
        limit_on = self.daily_loss_limit_enabled.isChecked()
        funds_on = self.free_funds_reserve_enabled.isChecked()
        self.daily_loss_limit.setEnabled(limit_on)
        self.free_funds_reserve.setEnabled(funds_on)
        if not limit_on and not funds_on:
            self._paint_note(self.guards_note, alarming=False)
            self.guards_note.setText(
                "Оба предохранителя выключены: робот не следит ни за убытком "
                "за день, ни за свободными средствами. Он остановится только "
                "по концу торгового окна или когда вы переключите режим."
            )
            return
        which = []
        if limit_on:
            which.append(f"дневной лимит {_pct(self.daily_loss_limit.value())}")
        if funds_on:
            which.append(f"запас {_pct(self.free_funds_reserve.value())}")
        self._paint_note(self.guards_note, alarming=True)
        self.guards_note.setText(
            "⚠️ Включено: " + ", ".join(which) + ". Но считать это НЕ ОТ ЧЕГО: "
            "программа не знает размера вашего счёта — портфель у брокера она "
            "пока не читает. Пока так, проверка не выполняется, и робот пишет "
            "об этом «НЕ ПРОВЕРЕНО» в журнал решений при каждом входе. "
            "Заработает сама, без правки настроек, как только счёт станет "
            "известен. Вашим решением от 05.09.2026 в боевом режиме такой "
            "предохранитель вход ЗАПРЕТИТ — в движке этого пока нет; прогона "
            "по истории решение не касается, там счёта нет по определению."
        )

    # ------------------------------------------------- согласование полей

    def take_error(self) -> str:
        """Почему такое сочетание настроек тейка отдавать движку нельзя.

        Пустая строка — можно. Проверка здесь **не** торговое правило: она
        не решает, где будет уровень и когда закроется позиция. Она повторяет
        границу, которую движок уже держит у себя (`engine/settings.py`),
        чтобы владелец счёта увидел отказ в момент ввода, а не получил его
        от движка после «ОК».

        ⚠️ Порог включения обязан быть строго больше отступа. Иначе первый же
        расчёт ставит уровень **по убыточную сторону** от цены входа: замерено
        на пороге 0,1 и отступе 1,0 — лонг от 200 000, закрытие 200 400 даёт
        уровень 198 396, то есть выход в убыток 1 604 ₽ на контракт. Уровень
        при этом едет вперёд честно, он просто начинается в убытке, и
        приёмочное правило «уровень не едет назад» такого не ловит.

        ⚠️ Условие — ровно то же, что у движка: проверка идёт при включённом
        скользящем тейке **независимо** от выключателя фиксации прибыли.
        Сузить его до «и то и другое включено» было соблазнительно и неверно:
        движок отвергает настройки по одному только `trailing_take_profit`,
        и сочетание «фиксация выключена, скользящий включён, порог ≤ отступа»
        прошло бы через окно и упало бы уже за ним.
        """
        if not self.trailing_enabled.isChecked():
            return ""
        start = self.trailing_start.value()
        offset = self.trailing_offset.value()
        if offset <= 0:
            return (
                "Отступ скользящего тейка должен быть больше нуля: на нулевом "
                "отступе уровень выхода стоит ровно на цене."
            )
        if start <= offset:
            return (
                f"Порог включения ({_pct(start)}) должен быть больше отступа "
                f"({_pct(offset)}). Иначе уровень выхода встанет ниже цены "
                "входа для лонга и выше — для шорта, то есть «фиксация "
                "прибыли» окажется выходом в убыток."
            )
        return ""

    def costs_error(self) -> str:
        """Почему такие издержки прогону отдавать нельзя. Пусто — можно.

        Проверка **не** торговое правило: она повторяет условие, которое
        уже держит у себя модель исполнения (`backtest.Costs.__post_init__`),
        чтобы владелец счёта увидел отказ в момент ввода, а не после «ОК».
        Источник правды один, условие совпадает дословно.

        ⚠️ Проскальзывание без шага цены — не ноль, а отказ. Иначе настройка
        выглядит заданной и не делает ничего: человек поставил поправку
        и продолжил смотреть на прибыль без неё.
        """
        if self.ruble_per_point.value() <= 0:
            return (
                "Стоимость пункта не задана. Без неё прогон не может "
                "перевести движение цены в рубли: все деньги отчёта вышли бы "
                "нулевыми. Для фьючерса на индекс МосБиржи это 1 ₽ за пункт."
            )
        if self.slippage_steps.value() > 0 and self.price_step.value() <= 0:
            return (
                f"Проскальзывание {_steps(self.slippage_steps.value())} "
                "задано, а шаг цены инструмента не назван. Так поправка "
                "не сработает вовсе, а отчёт выглядел бы посчитанным с ней. "
                "Шаг цены есть в карточке инструмента на сайте биржи."
            )
        return ""

    def _sync_buttons(self) -> None:
        """«ОК» и «Применить» гаснут, пока хоть одна проверка не пройдена.

        Проверок теперь две — тейк и издержки, — и решение о кнопках
        принимается в одном месте. Врозь они гасили бы кнопки по очереди:
        последняя сработавшая включала бы их обратно, отменяя чужой запрет.

        `getattr`, а не прямое обращение: поля создаются раньше кнопок,
        и сигнал поля, пришедший в момент сборки окна, не должен ронять
        диалог на несуществующем `self.buttons`.
        """
        buttons = getattr(self, "buttons", None)
        if buttons is None:
            return
        allowed = not (self.take_error() or self.costs_error())
        for standard in (
            QDialogButtonBox.StandardButton.Ok,
            QDialogButtonBox.StandardButton.Apply,
        ):
            button = buttons.button(standard)
            if button is not None:
                button.setEnabled(allowed)

    @staticmethod
    def _paint_note(label: QLabel, *, alarming: bool) -> None:
        """Строка состояния под полем: тревожная или обычная. См. `_paint_note`."""
        _paint_note(label, alarming=alarming)

    def _forget_point_source(self, *_: object) -> None:
        """Число тронули руками — происхождение «с биржи» больше не действует."""
        self._point_source = ""
        self._sync_costs()

    def _sync_point(self) -> None:
        """Строка про стоимость пункта: подтверждена биржей или нет.

        ⚠️ **Молчаливая единица хуже отказа.** Умолчание 1 ₽ за пункт верно
        для фьючерса на индекс МосБиржи и неверно для пяти других контрактов
        из шести замеренных; ошибка при этом тихая — список сделок тот же,
        а деньги другие. Поэтому неподтверждённая величина говорит о себе
        тревожно и называет последствие, а не просто стоит в поле.
        """
        if self._point_source:
            self._paint_note(self.point_note, alarming=False)
            self.point_note.setText(
                f"Стоимость пункта подсказана: {self._point_source}. Правка "
                "руками эту отметку снимает — окно не станет утверждать, "
                "что число пришло с биржи, если его тронули."
            )
            return
        self._paint_note(self.point_note, alarming=True)
        self.point_note.setText(
            "⚠️ Стоимость пункта НЕ ПОДТВЕРЖДЕНА биржей: "
            f"{_num(self.ruble_per_point.value())} ₽ за пункт — это ваш ввод "
            "или умолчание программы, а не величина из карточки инструмента. "
            "Для фьючерса на индекс МосБиржи умолчание 1 ₽ верно; для другого "
            "контракта деньги в отчёте будут неверны, и незаметно — сделки "
            "останутся теми же. Величина есть в карточке инструмента "
            "на сайте биржи, поля «шаг цены» и «стоимость шага»."
        )

    def _sync_costs(self, *_: object) -> None:
        """Строка про поправку на проскальзывание — и запрет негодного сочетания."""
        error = self.costs_error()
        self._sync_buttons()
        self._sync_point()
        if error:
            self.costs_note.setText("⚠️ " + error)
            return
        steps = self.slippage_steps.value()
        if steps <= 0:
            self.costs_note.setText(
                "Поправки нет: отчёт считает, что каждая заявка исполнилась "
                "ровно по расчётной цене. На счёте так не бывает, и разница "
                "вычитается из каждого исполнения — прибыль в отчёте выше "
                "настоящей, а не ниже."
            )
            return
        self.costs_note.setText(
            f"В цену каждого исполнения заложено {_steps(steps)} против "
            f"позиции — это {_num(steps * self.price_step.value())} ₽ "
            "на контракт, одинаково на любом объёме. Список сделок от этого "
            "не меняется, меняются только деньги отчёта."
        )

    def _sync_depth(self, *_: object) -> None:
        """Две глубины разом: прогрев средней и согласие показа с загрузкой.

        Число баров прогрева считает `market.warmup_bars` — то же место,
        которым пользуется загрузка истории. Второй расчёт того же в окне
        разошёлся бы с первым молча, и владелец счёта увидел бы одно число
        в настройках и другое при загрузке.

        ⚠️ Строка про «показываю больше, чем скачано» — не педантизм.
        Владелец счёта 05.09.2026 полдня разбирался, почему видит данные
        только с 6 августа при контракте с 17 июня, и причина была ровно
        такой: показ просил больше, чем лежало в базе. Молчание на этом месте
        читается как «данных нет», а данных просто не скачали.

        ⚠️ Пересчитывается при изменении **обоих** полей и периода средней:
        строка читает все три, и подписаны на неё все три. Правило общее —
        поле, чья подсказка зависит от другого поля, пересчитывается при
        изменении каждого из них.
        """
        need = warmup_bars(self.average_period.value())
        days = self.depth_days.value()
        loaded = self.history_depth_days.value()
        span = (
            "Показывается вся история, что есть в базе."
            if days == 0 else f"Показываются последние {_days(days)}."
        )
        short = (
            f" ⚠️ Скачивается только {_days(loaded)} — глубже показывать "
            "нечего, пока не увеличена загрузка выше."
            if 0 < loaded < days else ""
        )
        self.depth_note.setText(
            f"{span}{short} Первые {_bars(need)} показанного отрезка уходят "
            "на прогрев средней — решений на них нет вовсе, и это не пропуск "
            "сигналов. Глубина больше, чем есть в базе, — не ошибка: "
            "покажется то, что есть в базе."
        )

    def _sync_filter(self, *_: object) -> None:
        """Поля фильтра пилы и строка под ними. Молчит, пока фильтр выключен.

        ⚠️ Молчит именно **выключенный**: тревога, которая горит всегда,
        перестаёт читаться, а этой строкой сказано то, что владелец счёта
        обязан знать до включения, — фильтр глушит и выход тоже.

        Числа при снятой галочке гасятся, но не обнуляются: подобранное
        сохраняется до следующего включения. До торгового модуля выключённые
        числа не доходят — подмену делает `app.convert.strategy_settings`
        таблицей `_FILTER_OFF`, и делает её одинаково для окна и для любого
        другого вызывающего.
        """
        on = self.filter_enabled.isChecked()
        for field in (self.threshold_percent, self.confirm_bars):
            field.setEnabled(on)
        threshold = self.threshold_percent.value()
        bars = self.confirm_bars.value()
        if not on:
            self.filter_note.setText("")
            return
        if threshold <= 0 and bars <= 1:
            # Галочка стоит, а обе цифры выключены — фильтр не делает ничего.
            # Это не ошибка ввода и не повод гасить кнопки: сочетание рабочее
            # и в точности равно выключенному фильтру. Но выглядит оно как
            # включённая защита, поэтому названо вслух.
            self.filter_note.setText(
                "⚠️ Фильтр включён, но обе его цифры стоят в «выключено»: "
                "полоса 0 % и подтверждение одной свечой. Робот ведёт себя "
                "ровно так же, как с снятой галочкой. Задайте порог, "
                "подтверждение или оба."
            )
            return
        parts = []
        if threshold > 0:
            parts.append(f"полоса ±{_pct(threshold)} вокруг средней")
        if bars > 1:
            parts.append(f"подтверждение {_bars(bars)}")
        self.filter_note.setText(
            "⚠️ Фильтр против пилы включён (" + ", ".join(parts) + "). Он "
            "убирает часть переворотов и вместе с ними часть комиссии — "
            "но глушит и выход тоже: слабый обратный сигнал перестаёт быть "
            "поводом закрыться, и позиция живёт дольше обычного. При снятой "
            "галочке «Закрывать позицию в конце окна» верхней границы времени "
            "жизни позиции не остаётся вовсе. Сверка с вашим нынешним роботом "
            "гоняется с выключенным фильтром: включённый её не воспроизводит "
            "по построению — у вашего робота фильтра нет."
        )

    def _sync_window_close(self, *_: object) -> None:
        """Строка про выход по концу окна. Тревожная только когда галочка снята."""
        if self.close_on_time_end.isChecked():
            self.window_close_note.setText("")
            return
        self.window_close_note.setText(
            "⚠️ Закрытие по времени выключено: у позиции больше нет верхней "
            "границы времени жизни. Она может пережить конец окна, ночь "
            "и выходные — до обратного сигнала средней, тейка или переворота. "
            "Сверка с вашим нынешним роботом гоняется с включённым закрытием; "
            "выключенный режим замерами не подкреплён ничем."
        )

    def _sync_take(self, *_: object) -> None:
        """Поля скользящего тейка, пояснение под ними и запрет негодного ввода.

        Негодное сочетание из окна **не выходит**: «ОК» и «Применить» гаснут.
        Подрезать значения молча нельзя по той же причине, что и объём выше
        потолка, — это изменение параметра по деньгам, которого владелец счёта
        не делал и которого не будет в журнале решений.
        """
        take_on = self.take_profit_enabled.isChecked()
        trailing_on = self.trailing_enabled.isChecked()

        self.take_profit.setEnabled(take_on)
        # ⚠️ Выключатель скользящего тейка и его поля НЕ гасятся вслед
        # за выключателем фиксации прибыли. Иначе получается ловушка: негодное
        # сочетание порога и отступа осталось в полях, кнопки погашены, а поля,
        # которыми это чинится, недоступны. Выйти из такого состояния нечем.
        for field in (self.trailing_start, self.trailing_offset, self.trailing_step):
            field.setEnabled(trailing_on)

        error = self.take_error()
        self._sync_buttons()

        if error:
            # Цвет берётся из темы, а не задаётся числом: на тёмном фоне
            # тревожный красный из светлой темы читается плохо, а «просто
            # выделить» здесь мало — это запрет, а не подсказка.
            self.take_note.setText("⚠️ " + error)
            self.take_note.setStyleSheet(
                f"color: {current_theme().danger}; font-weight: bold;"
            )
            return
        self.take_note.setStyleSheet("")
        if not take_on:
            # ⚠️ Снятая галочка «Фиксировать прибыль» отменяет и скользящий
            # тейк: движок включает его только вместе с обычным. Умолчать
            # об этом — значит показать владельцу счёта включённый переключатель,
            # который ничего не делает.
            tail = (
                " Скользящий тейк при снятой галочке тоже не работает."
                if trailing_on else ""
            )
            self.take_note.setText(
                "Цели по прибыли нет: позицию держит только переворот по средней. "
                "На замеренном отрезке истории это от −3 038 до −108 366 ₽ "
                "за период — тейк держит весь результат." + tail
            )
        elif trailing_on:
            self.take_note.setText(
                f"Уровень появится, когда прибыль дойдёт до {_pct(self.trailing_start.value())}, "
                f"и дальше поедет за ценой с отступом {_pct(self.trailing_offset.value())}. "
                "Назад он не поедет никогда. Обычная цель прибыли при этом "
                "не используется: уровень целиком задаётся порогом и отступом. "
                "Замеров по скользящему тейку у проекта нет — сравнивать его "
                "будет не с чем."
            )
        elif self.take_profit.value() <= 0:
            self.take_note.setText(
                "Цель прибыли равна нулю — это то же самое, что снятая галочка: "
                "цели нет, позицию держит только переворот по средней. "
                "На замеренном отрезке истории это от −3 038 до −108 366 ₽ за период."
            )
        else:
            self.take_note.setText(
                f"Уровень {_pct(self.take_profit.value())} от цены входа "
                "выставляется заявкой у брокера и не двигается. Он живёт всё "
                "время позиции и срабатывает без участия программы."
            )

    def _sync_commission(self, *_: object) -> None:
        """Тариф рядом с полем — и прямая речь о том, чего без него не будет.

        Требование ТЗ §4.4 В состоит из двух половин: «проверять, окупает ли
        цель комиссию обеих сторон» и «не ставить цель ниже издержек». Обе
        невыполнимы, пока тариф не назван, и молчать об этом нельзя: движок
        в таком случае пишет «НЕ ПРОВЕРЕНО» на каждой позиции, а владелец
        счёта видит предупреждение, причина которого спрятана в окне настроек.
        """
        value = self.commission.value()
        if value <= 0:
            self.commission_note.setText(
                "Тариф не задан: проверка «цель прибыли окупает комиссию обеих "
                "сторон» не выполняется, и в журнале решений на каждой позиции "
                "будет предупреждение. В замерах проекта использовалось 14 ₽ "
                "за контракт на сторону — но это не ваш тариф, посмотрите его "
                "у брокера."
            )
            return
        # ⚠️ Здесь намеренно нет арифметики. Издержки сделки «вошли и вышли»
        # считает движок и пишет отдельной строкой в журнал; вторая, оконная
        # версия того же расчёта разошлась бы с первой при первой же правке.
        self.commission_note.setText(
            f"Тариф {_num(value)} ₽ за контракт берётся и на входе, и на выходе — "
            "за сделку целиком вы платите его дважды. Реверсная система делает "
            "много переворотов, поэтому это не мелочь: сколько именно вышло, "
            "движок считает сам и пишет отдельной строкой."
        )

    def _emit(self) -> bool:
        """Отдать настройки наружу, спросив подтверждение. `False` — отказался.

        Подтверждение показывается **до** отправки и только при изменениях
        (`ui/confirm_changes.py`). Ничего не изменилось — вопроса нет: строку
        «Значения совпали с прежними» по-прежнему пишет движок, и окно эту
        дорогу не перекрывает.

        ⚠️ Отказ обязан оставить настройки прежними **целиком**. Поэтому
        отправка идёт одним сигналом после ответа, а не по полю: применение
        наполовину — это набор, которого человек не выбирал.
        """
        values = self.values()
        if not self.confirm(values):
            return False
        self._applied = values
        self.settings_changed.emit(values)
        return True

    def applied(self) -> Settings:
        """Набор, который робот считает действующим: с ним идёт сравнение.

        Не то же самое, что `values()`: там содержимое полей прямо сейчас,
        здесь — то, что уже применено. Разница между ними и есть перечень,
        который показывает подтверждение.
        """
        return self._applied

    def confirm(self, values: Settings) -> bool:
        """Показать «было → стало» и дождаться ответа. `True` — применять.

        Отдельным методом, а не строкой внутри `_emit`, по двум причинам.
        Место, где спрашивают человека, остаётся ровно одно и видно
        с первого взгляда. И прогон тестов подменяет его: модальное окно
        в прогоне ждало бы человека вечно, а проверки самого подтверждения
        стоят отдельно (`tests/test_ui_templates.py`).
        """
        return confirm_changes(self, self._applied, values)

    def _on_accept(self) -> None:
        """«ОК»: применить и закрыть. Отказ от подтверждения окно не закрывает.

        Закрыть окно после «не применять» значило бы потерять правки, среди
        которых человек как раз и ищет ту, что сделал случайно.
        """
        if self._emit():
            self.accept()
