"""Окно настроек: читается, пишется, применяется на ходу.

Пункт приёмки Э1-16 звучит так: «Изменение объёма, периода средней и тейка
на ходу применилось без перезапуска и попало в журнал». Первую половину
проверяет этот файл: значение из поля доходит до движка. Вторая половина —
строка в журнале — работа `engine/`: окно её не пишет намеренно, иначе в журнале
появлялись бы изменения, которых движок не принял.
"""

from __future__ import annotations

from datetime import date, time
from pathlib import Path

import pytest
from helpers import RecordingPort, settle_qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel

import ui.main_window
import ui.settings_dialog
from market import redact
from ui.models import (
    AfterTakeProfit,
    AlgorithmOption,
    AverageKind,
    CalendarDay,
    MinuteBarLimit,
    MinutePriceOrder,
    ReversalMoment,
    Settings,
    TimeExitKind,
)
from ui.settings_dialog import CATALOGUE_NOT_ARRIVED, SettingsDialog


@pytest.fixture()
def make_dialog(qapp, monkeypatch):
    """Строитель окна настроек: создаёт с нужными настройками и сам убирает.

    ⚠️ Одна фикстура вместо четырёх рукописных пар «создал — `deleteLater`
    в `finally`». Дело не в длине. Рукописная пара **не просит `qapp`**,
    и тест с ней живёт только за счёт соседа, успевшего создать
    `QApplication` раньше. В одиночку такой тест не падает, а роняет процесс:
    `QWidget` без `QApplication` для Qt — `qFatal` и `abort()`, код 134,
    и pytest не успевает сообщить ни о каком тесте. Так вели себя тринадцать
    узлов этого файла, зелёных в компании и смертельных поодиночке.
    Зависимость от приложения теперь одна и видна в подписи теста: попросил
    окно — получил вместе с ним и приложение.

    ⚠️ Уборка — `settle_qt`, а не `deleteLater(); processEvents()`. Вторая
    связка выглядит уборкой и ею не является: окно остаётся живым и видимым,
    разрушается уже в чужом цикле событий, и для теста, который цикл крутит,
    это «закрылось последнее окно» и выход на середине корутины. Сторож —
    `test_the_dialogs_do_not_outlive_their_test`.

    ⚠️ Подтверждение «было → стало» (`ui/confirm_changes.py`) — модальное окно:
    в прогоне оно ждало бы человека вечно, и «Применить» здесь не возвращался
    бы никогда. Само подтверждение проверяется отдельно
    (`tests/test_ui_templates.py`), а тесты ниже — про то, что уходит наружу
    после согласия. Подмена ставится на **каждое** созданное окно, а не только
    на первое: тест, строящий второе окно в цикле, завис бы именно на нём.
    """
    built: list[SettingsDialog] = []

    def build(settings: Settings | None = None) -> SettingsDialog:
        window = SettingsDialog(Settings() if settings is None else settings)
        monkeypatch.setattr(window, "confirm", lambda values: True)
        built.append(window)
        return window

    yield build

    for window in built:
        window.deleteLater()
    # ⚠️ Не `processEvents()`, и разница стоила ревью 06.09.2026. Голая
    # прокрутка очереди отложенное удаление **не исполняет**: событие
    # `DeferredDelete` доставляется при выходе из цикла событий либо
    # по прямой просьбе — просьба стоит внутри `settle_qt`. С прежней
    # редакцией окно оставалось живым и видимым и уезжало в следующий тест
    # (замер: `SettingsDialog`, `isVisible() == True`, в чужом тесте).
    settle_qt(qapp)


@pytest.fixture()
def dialog(make_dialog):
    """Окно настроек с умолчаниями — частый случай строителя выше."""
    return make_dialog()


@pytest.fixture()
def make_window(qapp):
    """Главное окно с подставным портом: часы остановлены, уборка своя.

    Часы останавливаются здесь, а не в каждом тесте, и это не сокращение:
    `QTimer` на секунду, оставшийся жить, тикает в чужом цикле событий уже
    после того, как окно разрушено. Одно место — один способ ошибиться.
    """
    built: list[ui.main_window.MainWindow] = []

    def build(port) -> ui.main_window.MainWindow:
        window = ui.main_window.MainWindow(port=port, sanitize=redact)
        window._timer.stop()
        built.append(window)
        return window

    yield build

    for window in built:
        window.close()
        window.deleteLater()
    settle_qt(qapp)


@pytest.fixture()
def the_dialogs_must_not_outlive_the_test():
    """Сторож уборки `make_dialog`: после разбора фикстуры окон уже нет.

    ⚠️ Стоит в подписи теста **перед** `make_dialog` намеренно: `pytest`
    разбирает фикстуры в обратном порядке, значит эта проверка исполняется
    уже после уборки окон. Поменять местами — и она посмотрит на живое
    окно, то есть не проверит ничего.
    """
    gone: list[str] = []
    yield gone
    assert gone == ["окно настроек удалено"], (
        "показанное окно настроек пережило свой тест. Разрушится оно "
        "в первом же чужом цикле событий — а для теста, который цикл "
        "крутит, это «закрылось последнее окно» и выход на середине корутины"
    )


@pytest.mark.slow
def test_the_dialogs_do_not_outlive_their_test(
    the_dialogs_must_not_outlive_the_test, make_dialog, qapp
) -> None:
    """Окно, показанное тестом, удаляется здесь, а не в чужом цикле событий.

    Проверяется не «в фикстуре написана уборка», а видимое следствие: сигнал
    `destroyed`. Замер 06.09.2026 (ревью): с прежней связкой
    `deleteLater(); processEvents()` окно доезжало до следующего теста живым
    и видимым — `[('SettingsDialog', True)]` в соседнем тесте.

    Окно намеренно показано: невидимый виджет для Qt не окно, и «закрылось
    последнее окно» на нём не срабатывает — проверка была бы вакуумна.

    Мутация: вернуть в конец `make_dialog` голый `qapp.processEvents()`
    вместо `settle_qt(qapp)` — прогон обязан покраснеть на разборе фикстуры.
    """
    window = make_dialog()
    window.show()
    qapp.processEvents()
    window.destroyed.connect(
        lambda *_: the_dialogs_must_not_outlive_the_test.append("окно настроек удалено")
    )
    assert window.isVisible(), (
        "окно не показано — для Qt оно не окно, и проверка вакуумна"
    )
    assert window in qapp.topLevelWidgets(), "окно не самостоятельное"


def test_defaults_match_the_agreed_numbers(dialog) -> None:
    """Умолчания в полях — те, за которыми стоят замеры (DOMAIN.md §4)."""
    values = dialog.values()
    assert values.average_period == 15
    assert values.average_kind is AverageKind.EMA
    assert values.take_profit_pct == pytest.approx(0.5)
    assert values.window_start == time(10, 5)
    assert values.window_end == time(11, 0)
    assert values.volume == 1
    # ПРЕДОХРАНИТЕЛЬ ВЫКЛЮЧЕН НА ЭТАПЕ (D-113)
    # assert values.daily_loss_limit_pct == pytest.approx(2.0)
    # assert values.free_funds_reserve_pct == pytest.approx(30.0)


def test_settings_survive_a_round_trip(dialog) -> None:
    """Что записали в поля, то и прочитали. Без этого «применить» врёт."""
    source = Settings(
        instrument="MXH7",
        timeframe="15 минут",
        average_period=20,
        average_kind=AverageKind.SMA,
        reversal_moment=ReversalMoment.SAME_BAR,
        after_take_profit=AfterTakeProfit.RESTORE_AT_ONCE,
        take_profit_enabled=True,
        take_profit_pct=0.8,
        trailing_enabled=True,
        trailing_start_pct=1.0,
        trailing_offset_pct=0.3,
        trailing_step_pct=0.1,
        window_start=time(9, 45),
        window_end=time(12, 30),
        volume=3,
        # ПРЕДОХРАНИТЕЛЬ ВЫКЛЮЧЕН НА ЭТАПЕ (D-113)
        # volume_cap=7,
        # daily_loss_limit_pct=1.5,
        # free_funds_reserve_pct=40.0,
        commission_per_side_rub=14.0,
    )
    dialog.set_values(source)
    assert dialog.values() == source


def test_a_take_free_configuration_survives_a_round_trip(dialog) -> None:
    """Конфигурация-сторож сверки: тейка нет вовсе.

    Ради неё диапазон и открыт до нуля. Проверяется оба способа сказать
    «тейка нет» — снятая галочка и нулевой процент: движок для обоих
    не подаёт вооружения вовсе.
    """
    source = Settings(take_profit_enabled=False, take_profit_pct=0.0)
    dialog.set_values(source)
    assert dialog.values() == source


def test_every_field_explains_itself_in_plain_russian(dialog) -> None:
    """У каждого поля есть подсказка, и она не на языке программиста.

    Требование ТЗ §4.4: пользователь — не программист, и «EMA period» ему
    ничего не говорит. Поле без подсказки — дефект интерфейса.
    """
    fields = [
        dialog.instrument, dialog.timeframe, dialog.average_period,
        dialog.average_kind, dialog.reversal_moment, dialog.after_take_profit,
        dialog.take_profit_enabled, dialog.take_profit,
        dialog.trailing_enabled, dialog.trailing_start, dialog.trailing_offset,
        dialog.trailing_step,
        dialog.window_start,
        # ⚠️ Потолка объёма нет: ПРЕДОХРАНИТЕЛЬ ВЫКЛЮЧЕН НА ЭТАПЕ (D-113).
        dialog.window_end, dialog.volume,
        dialog.commission,
    ]
    for field in fields:
        hint = field.toolTip()
        assert len(hint) > 40, f"подсказка слишком коротка: {hint!r}"
        assert any("а" <= char <= "я" for char in hint.lower()), (
            f"подсказка не по-русски: {hint!r}"
        )


# ПРЕДОХРАНИТЕЛЬ ВЫКЛЮЧЕН НА ЭТАПЕ (D-113): сторожа потолка в окне.
# def test_volume_cap_is_visible_next_to_volume(dialog) -> None:
#     """Включённый потолок виден рядом с полем объёма и ограничивает ввод."""
#     dialog.set_values(Settings(volume_cap_enabled=True, volume_cap=3))
#     assert dialog.volume.maximum() == 3
#     assert "3" in dialog.cap_note.text()
#
#
# def test_a_cap_that_is_off_does_not_pinch_the_volume_field(dialog) -> None:
#     """Снятая галочка потолка ввод объёма не ограничивает.
#
#     Иначе число, которое ничего не значит, продолжало бы зажимать поле —
#     и владелец счёта решал бы, что потолок работает.
#     """
#     dialog.set_values(Settings(volume_cap_enabled=False, volume_cap=3))
#     assert dialog.volume.maximum() > 3
#     assert not dialog.volume_cap.isEnabled()


#: Обороты, которыми окно обещает предохранитель в НАСТОЯЩЕМ времени.
#: Пока галочка снята, ни один из них появиться не имеет права: ложное
#: обещание предохранителя хуже отсутствия предохранителя — оно снимает
#: настороженность.
PROMISES = (
    "не подаётся",
    "не подаваться",
    "не будет вовсе",
    "закрывает позицию",
    "прекращает торговать",
    "остаётся нетронутой",
    "входит уменьшенным",
)

# ПРЕДОХРАНИТЕЛЬ ВЫКЛЮЧЕН НА ЭТАПЕ (D-113). Таблица «галочка, число и что
# сказано, пока галочка снята» опустела: галочек и чисел в окне больше нет.
# #: Поле-галочка, поле-число и что должно быть сказано, пока галочка снята.
# GUARDS = (
#     ("volume_cap_enabled", "volume_cap", "потолка объёма нет"),
#     ("daily_loss_limit_enabled", "daily_loss_limit", "не следит"),
#     ("free_funds_reserve_enabled", "free_funds_reserve", "не следит"),
# )
#
#
# @pytest.mark.parametrize(("switch", "number", "expected"), GUARDS)
# def test_a_guard_that_is_off_says_so_and_promises_nothing(
#     dialog, switch: str, number: str, expected: str
# ) -> None:
#     """Выключенный предохранитель говорит, что защиты нет, и не обещает работы.
#
#     Обе половины обязательны. Без первой текст становится уклончивым
#     и читается как «всё в порядке»; без второй окно обещает то, чего
#     при снятой галочке не произойдёт.
#     """
#     dialog.set_values(Settings())
#     assert not getattr(dialog, switch).isChecked(), "умолчание предохранителя изменилось"
#     assert not getattr(dialog, number).isEnabled(), (
#         "число выключенного предохранителя доступно для правки — выглядит рабочим"
#     )
#     said = (dialog.cap_note.text() + " " + dialog.guards_note.text()).lower()
#     assert expected in said, f"не сказано, что защиты нет: {said!r}"
#     for promise in PROMISES:
#         assert promise not in said, (
#             f"обещан предохранитель, который выключен: {promise!r}"
#         )


def test_the_window_says_out_loud_that_there_are_no_money_guards(dialog) -> None:
    """Стережёт МОЛЧАНИЕ: вкладка «Деньги» обязана сказать, что защиты нет.

    ПРЕДОХРАНИТЕЛЬ ВЫКЛЮЧЕН НА ЭТАПЕ (D-113). Поля трёх предохранителей
    из окна исчезли, и это самая опасная точка всей правки: вкладка без полей
    и без строки читается как «здесь всё в порядке». Человек, не нашедший
    защиты, обязан прочитать, что её нет **намеренно** и что она вернётся.

    Проверяется **четыре** вещи, и каждая ловит свою мутацию: строка непустая,
    названы все три предохранителя, названо последствие («не остановится»),
    названа недопустимость боя. Стереть любую из них — и окно снова молчит
    наполовину.
    """
    dialog.set_values(Settings())
    said = dialog.guards_note.text().lower()
    assert said.strip(), "окно молчит о том, что предохранителей нет"
    for guard in ("потолок объёма", "дневной лимит убытка", "запас свободных средств"):
        assert guard in said, (
            f"окно не назвало выключенный предохранитель «{guard}»: {said!r}"
        )
    assert "не остановится" in said, (
        "названы имена, но не последствие: перечень без последствия "
        "владельцу счёта ничего не говорит"
    )
    assert "не допущена" in said, "не сказано, что к боевой торговле нельзя"
    assert "bold" in dialog.guards_note.styleSheet(), (
        "строка о том, что защиты нет, набрана обычным шрифтом"
    )


def test_the_window_promises_no_guard_it_cannot_keep(dialog) -> None:
    """Стережёт ложное обещание: выключенная защита не обещает работы.

    Оборот в настоящем времени («не подаётся», «закрывает позицию») снимает
    настороженность там, где защиты нет вовсе, и это хуже её отсутствия.
    Проверка осталась от прежней пары тестов и работает ровно так же:
    по списку оборотов, а не «на глаз».
    """
    dialog.set_values(Settings())
    said = dialog.guards_note.text().lower()
    for promise in PROMISES:
        assert promise not in said, (
            f"обещан предохранитель, которого нет: {promise!r}"
        )


# ПРЕДОХРАНИТЕЛЬ ВЫКЛЮЧЕН НА ЭТАПЕ (D-113): включать нечего.
# @pytest.mark.parametrize(("switch", "number", "expected"), GUARDS)
# def test_a_guard_that_is_on_says_what_happens_when_it_fires(
#     dialog, switch: str, number: str, expected: str
# ) -> None:
#     """Включённый предохранитель называет последствие, а не только факт.
#
#     «Лимит включён» само по себе не говорит, что робот закроет позицию
#     и не откроет новую до конца дня, — а это и есть то, за что владелец
#     счёта отвечает деньгами.
#     """
#     dialog.set_values(Settings().replace(**{switch: True}))
#     assert getattr(dialog, number).isEnabled()
#     hint = getattr(dialog, switch).toolTip().lower()
#     assert any(promise in hint for promise in PROMISES), (
#         f"включённый предохранитель не сказал, что сделает: {hint!r}"
#     )


# ПРЕДОХРАНИТЕЛЬ ВЫКЛЮЧЕН НА ЭТАПЕ (D-113): сторож слепого предохранителя.
# Включать нечего — слепнуть некому.
# def test_the_two_guards_that_need_the_account_size_say_it_is_unknown(dialog) -> None:
#     """Дневной лимит и запас средств считать не от чего — и это сказано заранее.
#
#     Портфель у брокера программа пока не читает. Движок в этом случае
#     пишет «НЕ ПРОВЕРЕНО» в строку входа, но узнавать об этом из журнала
#     задним числом хуже, чем прочитать в настройках при включении.
#     """
#     dialog.set_values(Settings(daily_loss_limit_enabled=True))
#     note = dialog.guards_note.text()
#     # ⚠️ Через `and`, а не `or`. Первая редакция сверяла «НЕ ОТ ЧЕГО»
#     # ИЛИ «не знает размера» — и мутация, выбросившая первое, прошла
#     # незамеченной: осталось второе. Проверка «хоть что-нибудь из двух»
#     # стережёт ровно половину фразы, и неизвестно какую.
#     assert note.startswith("⚠️"), (
#         f"строка про слепой предохранитель не помечена тревогой: {note!r}"
#     )
#     assert "НЕ ОТ ЧЕГО" in note, note
#     assert "не знает размера" in note, note
#     assert "НЕ ПРОВЕРЕНО" in note, "не сказано, что робот напишет об этом в журнал"
#     assert "ЗАПРЕТИТ" in note, (
#         "не сказано про решение 0037: в бою слепой предохранитель вход запретит"
#     )
#     assert "bold" in dialog.guards_note.styleSheet(), (
#         "предупреждение о слепом предохранителе набрано обычным шрифтом"
#     )


# ПРЕДОХРАНИТЕЛЬ ВЫКЛЮЧЕН НА ЭТАПЕ (D-113): сторожа строки под потолком.
# def test_lowering_the_volume_clears_the_warning_by_itself(dialog) -> None:
#     """Красная строка про объём выше потолка гаснет, когда объём уменьшили.
#
#     ⚠️ Пересчёт **не вызывается руками** — в этом весь смысл проверки.
#     Соседний тест ниже дёргает `_sync_volume_cap()` сам, и потому не видел,
#     что поле объёма ни на что не подписано: владелец счёта уменьшал объём
#     до разрешённого, а строка оставалась и продолжала утверждать, что заявка
#     не пойдёт. Красное предупреждение про деньги, которое врёт, хуже
#     отсутствующего: рядом с ним ещё три таких строки.
#     """
#     dialog.set_values(Settings(volume_cap_enabled=True, volume_cap=5, volume=50))
#     assert "не будет" in dialog.cap_note.text().lower(), "не предупредил заранее"
#
#     dialog.volume.setValue(2)
#
#     note = dialog.cap_note.text().lower()
#     assert "не будет" not in note, f"строка соврала после правки объёма: {note!r}"
#     assert "потолок объёма: 5" in note, note
#
#
# def test_the_cap_note_says_the_order_will_not_be_sent(dialog) -> None:
#     """Строка под полем объёма — самое читаемое место, и она не должна врать."""
#     dialog.set_values(Settings(volume_cap_enabled=True, volume_cap=3, volume=1))
#     note = dialog.cap_note.text().lower()
#     assert "не подаётся" in note, note
#
#     dialog.volume.setMaximum(100)
#     dialog.volume.setValue(10)
#     dialog._sync_volume_cap()
#     above = dialog.cap_note.text().lower()
#     assert "выше потолка" in above
#     assert "не будет" in above, (
#         f"сказано, что объём выше потолка, но не сказано, что будет: {above!r}"
#     )
#     assert "поменьше" in above, (
#         "не сказано, что робот не подаст уменьшенную заявку, — а он не подаст"
#     )


def test_take_profit_range_allows_zero(dialog) -> None:
    """Ноль в тейке задаётся через окно, и это не придирка.

    Обе конфигурации-сторожа сверки с прототипом сделаны с **выключенным**
    тейком (PROTOTYPE.md §1): 166 сделок / −5 400 ₽ и 1 211 / −54 550 ₽.
    Прежний диапазон 0,1–5,0 не позволял их воспроизвести через окно —
    дефект был записан прямо в решении 0008.
    """
    assert dialog.take_profit.minimum() == pytest.approx(0.0)
    assert dialog.take_profit.maximum() == pytest.approx(5.0)
    assert dialog.average_period.minimum() == 5
    assert dialog.average_period.maximum() == 300

    dialog.take_profit.setValue(0.0)
    assert dialog.values().take_profit_pct == pytest.approx(0.0), (
        "окно не отдало ноль наружу"
    )


def test_take_profit_can_be_switched_off_by_the_checkbox(dialog) -> None:
    """Выключатель тейка — требование ТЗ §4.4 В, и его в окне не было."""
    assert dialog.values().take_profit_enabled is True
    dialog.take_profit_enabled.setChecked(False)
    assert dialog.values().take_profit_enabled is False
    assert not dialog.take_profit.isEnabled(), "поле цели осталось доступным"
    assert "Цели по прибыли нет" in dialog.take_note.text()


def test_switching_the_take_off_does_not_erase_its_percent(dialog) -> None:
    """Снятая галочка не стирает введённое число — включат обратно, оно на месте."""
    dialog.take_profit.setValue(0.8)
    dialog.take_profit_enabled.setChecked(False)
    dialog.take_profit_enabled.setChecked(True)
    assert dialog.values().take_profit_pct == pytest.approx(0.8)


def test_trailing_take_profit_has_all_three_fields(dialog) -> None:
    """Скользящий тейк перенесён в первый этап решением 0009.

    Движок принимает четыре значения — выключатель, порог, отступ и шаг
    подтяжки; окно обязано их отдавать. Шаг подтяжки не украшение:
    без него каждая свеча с новой лучшей ценой — отдельное обращение
    к брокеру, до двенадцати за час позиции.
    """
    defaults = dialog.values()
    assert defaults.trailing_enabled is False, "скользящий тейк включён по умолчанию"
    assert defaults.trailing_start_pct == pytest.approx(0.5)
    assert defaults.trailing_offset_pct == pytest.approx(0.2)
    assert defaults.trailing_step_pct == pytest.approx(0.05)

    dialog.trailing_enabled.setChecked(True)
    dialog.trailing_start.setValue(1.0)
    dialog.trailing_offset.setValue(0.3)
    dialog.trailing_step.setValue(0.1)
    values = dialog.values()
    assert values.trailing_enabled is True
    assert values.trailing_start_pct == pytest.approx(1.0)
    assert values.trailing_offset_pct == pytest.approx(0.3)
    assert values.trailing_step_pct == pytest.approx(0.1)


def test_trailing_fields_are_locked_while_trailing_is_off(dialog) -> None:
    for field in (dialog.trailing_start, dialog.trailing_offset, dialog.trailing_step):
        assert not field.isEnabled()
    dialog.trailing_enabled.setChecked(True)
    for field in (dialog.trailing_start, dialog.trailing_offset, dialog.trailing_step):
        assert field.isEnabled()


@pytest.mark.parametrize("start, offset", [(0.2, 0.2), (0.1, 1.0), (0.0, 0.2)])
def test_threshold_not_above_offset_never_leaves_the_window(dialog, start, offset) -> None:
    """Сочетание «порог ≤ отступ» окно не выпускает наружу.

    Движок такое отвергает, и правильно: замерено — порог 0,1 при отступе 1,0,
    лонг от 200 000, закрытие 200 400 → уровень 198 396, то есть выход
    в убыток 1 604 ₽ на контракт. Уровень при этом едет вперёд честно, он
    просто начинается в убытке, и приёмочное правило «уровень не едет назад»
    этого не ловит.

    Окно обязано остановить ввод, а не ловить отказ движка: отказ движка
    приходит после «ОК», когда владелец счёта уже считает, что настроил.
    """
    received: list[Settings] = []
    dialog.settings_changed.connect(received.append)
    dialog.trailing_enabled.setChecked(True)
    dialog.trailing_offset.setValue(offset)
    dialog.trailing_start.setValue(start)

    assert dialog.take_error(), "окно считает негодное сочетание годным"
    ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
    apply = dialog.buttons.button(QDialogButtonBox.StandardButton.Apply)
    assert not ok.isEnabled(), "«ОК» доступен при негодных настройках тейка"
    assert not apply.isEnabled(), "«Применить» доступен при негодных настройках тейка"
    assert "⚠️" in dialog.take_note.text()

    apply.click()
    assert not received, "негодные настройки всё-таки ушли движку"


def test_a_fixed_combination_unlocks_the_buttons_again(dialog) -> None:
    """Запрет снимается сам, как только сочетание стало годным."""
    dialog.trailing_enabled.setChecked(True)
    dialog.trailing_offset.setValue(0.6)
    dialog.trailing_start.setValue(0.5)
    ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert not ok.isEnabled()

    dialog.trailing_start.setValue(1.0)
    assert dialog.take_error() == ""
    assert ok.isEnabled()


def test_a_bad_combination_from_outside_is_shown_as_it_is(dialog) -> None:
    """Пришедшее снаружи негодное сочетание окно не правит молча.

    Тот же принцип, что с объёмом выше потолка: подмена на «ОК» — это
    изменение параметра по деньгам, которого владелец счёта не делал
    и которого не будет в журнале решений. Показать как есть, сказать
    вслух, наружу не выпустить.
    """
    dialog.set_values(Settings(
        trailing_enabled=True, trailing_start_pct=0.2, trailing_offset_pct=0.5,
    ))
    assert dialog.values().trailing_start_pct == pytest.approx(0.2), "окно подменило порог"
    assert dialog.values().trailing_offset_pct == pytest.approx(0.5), "окно подменило отступ"
    assert dialog.take_error(), "негодное сочетание объявлено годным"


def test_the_offset_check_only_applies_when_trailing_is_on(dialog) -> None:
    """Невыбранный скользящий уровень своими числами ничего не запрещает.

    ⚠️ Способ не «снимается», а сменяется другим: у группы переключателей
    выбран ровно один, и `setChecked(False)` на выбранном Qt игнорирует.
    Прежняя редакция теста снимала галочку — сегодня это ничего не значит.
    """
    dialog.trailing_enabled.setChecked(True)
    dialog.trailing_offset.setValue(1.0)
    dialog.trailing_start.setValue(0.5)
    assert dialog.take_error()
    dialog.take_fixed.setChecked(True)
    assert dialog.take_error() == ""
    assert dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()


def test_with_the_take_off_no_method_leaves_the_window_and_nothing_is_blocked(
    dialog,
) -> None:
    """Снятая галочка — и наружу уходит «прибыль не фиксируется», без запретов.

    ⚠️ Правило перевернулось 15.09.2026 (решение 0060), и прежнее было верным
    для своего времени: окно отдавало выбор способа как есть, движок получал
    «скользящий включён» при выключенной фиксации и отвергал настройки
    по одной этой паре чисел — поэтому окно и проверяло её независимо
    от галочки. Теперь окно такого сочетания не отдаёт вовсе.

    Стережётся здесь **запрет без предмета**: погашенные кнопки из-за чисел,
    которые никуда не уходят, — состояние, в котором человек не понимает,
    что чинить. Вердикты окна и движка при этом обязаны совпадать, и это
    проверяется тут же, а не предполагается.
    """
    from engine.settings import EngineSettings

    dialog.trailing_enabled.setChecked(True)
    dialog.trailing_offset.setValue(0.5)
    dialog.trailing_start.setValue(0.2)
    dialog.take_profit_enabled.setChecked(False)

    values = dialog.values()
    assert values.trailing_enabled is False, (
        "окно отдало способ, которого не применяет: движок отвергнет настройки "
        "из-за чисел, которых никто не читает"
    )
    assert dialog.take_error() == "", "запрет без предмета: наружу такого не уходит"
    assert dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    EngineSettings(
        take_profit=values.take_profit_enabled,
        take_profit_percent=values.take_profit_pct,
        trailing_take_profit=values.trailing_enabled,
        trailing_start_percent=values.trailing_start_pct,
        trailing_offset_percent=values.trailing_offset_pct,
    )


def test_a_bad_combination_can_always_be_fixed(dialog) -> None:
    """Из состояния «кнопки погашены» есть выход руками, и не один.

    Ловушка была бы такой: числа, которыми чинится запрет, недоступны,
    а запрет держится. Её нет не по осторожности: доступность полей и сам
    запрет считаются от ОДНОГО условия — «скользящий уровень применяется
    сейчас». Пока запрет жив, живы и три выхода из него.
    """
    ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)

    def spoil() -> None:
        dialog.set_values(Settings(
            take_profit_enabled=True, trailing_enabled=True,
            trailing_start_pct=0.2, trailing_offset_pct=0.5,
        ))
        assert dialog.take_error(), "негодное сочетание объявлено годным"
        assert not ok.isEnabled()

    # Выход первый: поправить числа — они доступны, пока запрет жив.
    spoil()
    for field in (dialog.trailing_start, dialog.trailing_offset):
        assert field.isEnabled(), "поле, которым чинится ошибка, недоступно"
    dialog.trailing_start.setValue(1.0)
    assert dialog.take_error() == "" and ok.isEnabled()

    # Выход второй: выбрать другой способ.
    spoil()
    assert dialog.take_fixed.isEnabled(), "второй способ недоступен"
    dialog.take_fixed.setChecked(True)
    assert dialog.take_error() == "" and ok.isEnabled()

    # Выход третий: не фиксировать прибыль вовсе.
    spoil()
    assert dialog.take_profit_enabled.isEnabled(), "выключатель фиксации погашен"
    dialog.take_profit_enabled.setChecked(False)
    assert dialog.take_error() == "" and ok.isEnabled()


def test_trailing_switched_on_without_the_take_is_said_to_do_nothing(dialog) -> None:
    """Выбранный способ, который не применяется, — обман, если об этом молчать.

    Движок включает скользящий уровень только вместе с фиксацией
    (`EngineSettings.plan()`: `trailing = take_profit and trailing_take_profit`).

    ⚠️ Сочетание приходит **снаружи** — из файла прежней сборки и из
    сохранённого набора. Сама программа его больше не записывает
    (`SettingsDialog.values`), но прочитать обязана как есть и сказать словами,
    а не подменить молча.
    """
    dialog.set_values(Settings(take_profit_enabled=False, trailing_enabled=True))
    assert dialog.trailing_enabled.isChecked(), "пришедший выбор не показан"
    assert "тоже не работает" in dialog.take_note.text()
    assert dialog.values().trailing_enabled is False


def test_the_way_to_take_profit_is_a_choice_of_one_not_two_switches(dialog) -> None:
    """Способ фиксации прибыли — выбор ОДНОГО из двух, а не две галочки.

    Слова заказчика 15.09.2026: «это 2 разных способа фиксировать прибыль —
    надо выбирать что мы применяем один или другой», «не И а ИЛИ мы выбираем
    способ» (решение 0060). У позиции один уровень выхода.

    ⚠️ Стережётся **молчание**: прежде окно принимало оба включёнными
    и вид выбирала за человека арифметика движка (`trailing = take_profit
    and trailing_take_profit`) — молча, без строки в журнале и без слова
    в окне. Состояния «выбраны оба» в окне больше нет вовсе, и мутация,
    возвращающая две независимые галочки, обязана ронять эту проверку.
    """
    assert dialog.take_fixed.isChecked() != dialog.trailing_enabled.isChecked(), (
        "выбран не ровно один способ"
    )

    dialog.trailing_enabled.setChecked(True)
    assert not dialog.take_fixed.isChecked(), "выбраны оба способа сразу"
    assert dialog.values().trailing_enabled is True

    dialog.take_fixed.setChecked(True)
    assert not dialog.trailing_enabled.isChecked(), "выбраны оба способа сразу"
    assert dialog.values().trailing_enabled is False

    # Попытка включить оба руками ничего не меняет: у группы выбран один.
    dialog.trailing_enabled.setChecked(True)
    dialog.take_fixed.setChecked(True)
    assert dialog.take_fixed.isChecked() != dialog.trailing_enabled.isChecked()


def test_the_note_names_the_single_level_that_is_watched(dialog) -> None:
    """Пояснение под полями называет, КАКОЙ уровень сторожится сейчас.

    ⚠️ Стережётся молчание, а не оформление. Два способа на экране без слова
    о том, что работает один, читаются как «работают оба» — а с ними рядом
    четыре числа, из которых движок читает одно или три. Молчание здесь
    равно поломке (`CLAUDE.md` №13).
    """
    dialog.take_profit_enabled.setChecked(True)
    dialog.take_fixed.setChecked(True)
    dialog.take_profit.setValue(0.7)
    fixed_note = dialog.take_note.text()
    assert "ОДИН уровень" in fixed_note, "не сказано, что уровень один"
    assert "неподвижная цель" in fixed_note, "не назван сторожимый уровень"
    assert "0,7" in fixed_note, "в пояснении нет числа, по которому уровень стоит"
    assert "Скользящий уровень при этом не применяется" in fixed_note, (
        "про второй способ не сказано, что он не работает"
    )

    dialog.trailing_enabled.setChecked(True)
    dialog.trailing_start.setValue(1.0)
    dialog.trailing_offset.setValue(0.3)
    moving_note = dialog.take_note.text()
    assert moving_note != fixed_note, "пояснение не зависит от выбранного способа"
    assert "ОДИН уровень" in moving_note, "не сказано, что уровень один"
    assert "скользящий" in moving_note, "не назван сторожимый уровень"
    assert "Неподвижная цель при этом не используется" in moving_note, (
        "про второй способ не сказано, что он не работает"
    )
    assert "1%" in moving_note and "0,3" in moving_note, (
        "в пояснении нет чисел, по которым живёт уровень"
    )


def test_the_number_of_the_unused_method_is_not_shown_alive(dialog) -> None:
    """Число способа, который сейчас не работает, недоступно для ввода.

    Живое поле, которого движок не читает, — то же молчание: человек крутит
    число и не узнаёт, что оно ни на что не влияет. Так сегодня живут три
    поля сигнала у второго алгоритма (`D-107`), и повторять это у фиксации
    прибыли нельзя.
    """
    dialog.take_profit_enabled.setChecked(True)
    dialog.take_fixed.setChecked(True)
    assert dialog.take_profit.isEnabled(), "число выбранного способа недоступно"
    for field in (dialog.trailing_start, dialog.trailing_offset, dialog.trailing_step):
        assert not field.isEnabled(), "живо число способа, который не применяется"

    dialog.trailing_enabled.setChecked(True)
    assert not dialog.take_profit.isEnabled(), (
        "цель прибыли осталась живой при выбранном скользящем уровне — "
        "движок её не читает"
    )
    for field in (dialog.trailing_start, dialog.trailing_offset, dialog.trailing_step):
        assert field.isEnabled(), "число выбранного способа недоступно"


#: Тариф из `DOMAIN.md` §5: биржевой сбор из карточки инструмента плюс 1 ₽
#: брокеру. На нём сделаны все замеры проекта. Число живёт здесь, чтобы
#: подстановка «какого-нибудь» тарифа не прошла молча: поменять умолчание,
#: не тронув документ и этот тест, нельзя.
TARIFF_FROM_THE_DOCUMENT = 14.0


def test_commission_tariff_reaches_the_engine(dialog) -> None:
    """Поля тарифа не было вовсе, и правило ТЗ §4.4 В было наполовину ничьё.

    Без тарифа движок пишет на каждой позиции «окупает ли цель комиссию
    обеих сторон — НЕ ПРОВЕРЕНО», и владелец счёта видит предупреждение,
    причина которого в окне настроек ничем не объяснена.

    ⚠️ Здесь стояло `commission_per_side_rub is None` с пояснением «тариф
    подставлен за владельца». Посылка была верной, пока владелец счёта
    не высказался: 31.08.2026 он поручил поставить предварительные цифры
    самому, с учётом того, что боевые придут позже (решение 0010).

    Поэтому сторожится теперь не отсутствие тарифа, а его **происхождение**:
    умолчание обязано совпадать с величиной, записанной в `DOMAIN.md` §5,
    а не быть подобранным числом. Прогон с валовой прибылью вместо чистой
    в этом проекте результатом не является, и умолчание `None` давало
    прочерк в колонке комиссии на каждом прогоне.
    """
    assert dialog.values().commission_per_side_rub == pytest.approx(
        TARIFF_FROM_THE_DOCUMENT
    ), "умолчание тарифа разошлось с DOMAIN.md §5"
    dialog.commission.setValue(21.0)
    assert dialog.values().commission_per_side_rub == pytest.approx(21.0)


def test_the_default_tariff_is_the_one_written_in_the_domain_document(dialog) -> None:
    """Умолчание тарифа сверяется с документом, а не с самим собой.

    Тариф — единственное умолчание в окне, которое прямо превращается
    в рубли отчёта. Подобранное «примерно такое» число дало бы правдоподобный
    итог, который не с чем сверить: замеры проекта сделаны на конкретной
    величине, и разойтись они обязаны громко.
    """
    document = (
        Path(__file__).resolve().parent.parent / ".docs" / "DOMAIN.md"
    ).read_text(encoding="utf-8")
    line = f"{TARIFF_FROM_THE_DOCUMENT:g} ₽ за контракт на сторону"
    assert line in document, (
        f"в DOMAIN.md §5 нет строки «{line}» — либо изменился документ, "
        "либо умолчание тарифа поставлено мимо него"
    )
    assert Settings().commission_per_side_rub == pytest.approx(TARIFF_FROM_THE_DOCUMENT)


def test_zero_commission_means_not_specified_not_free(dialog) -> None:
    """Ноль в поле — «не задан», а не «комиссии нет».

    Подставленный ноль превратил бы проверку «цель окупает комиссию обеих
    сторон» в вечное «окупается» — то есть тихо снял бы требование ТЗ.
    """
    dialog.commission.setValue(0.0)
    assert dialog.values().commission_per_side_rub is None
    note = dialog.commission_note.text()
    assert "не задан" in note
    assert "не выполняется" in note


def test_take_profit_range_upper_bound_matches_the_spec(dialog) -> None:
    assert dialog.take_profit.maximum() == pytest.approx(5.0)


def test_an_unknown_timeframe_is_not_silently_replaced(dialog) -> None:
    """Размер свечи, которого нет в списке, не подменяется на «5 минут».

    Подмена была молчаливой и меняла стратегию целиком: пришло «2 минуты»,
    на «ОК» ушло «5 минут», и в журнале решений этой правки нет — потому
    что владелец счёта её не делал.
    """
    dialog.set_values(Settings(timeframe="2 минуты"))
    assert dialog.values().timeframe == "2 минуты", (
        f"окно подменило размер свечи на {dialog.values().timeframe!r}"
    )


# ПРЕДОХРАНИТЕЛЬ ВЫКЛЮЧЕН НА ЭТАПЕ (D-113)
# def test_a_zero_daily_limit_is_not_silently_raised(dialog) -> None:
#     """Ноль в дневном лимите не подрезается до 0,1 молча."""
#     dialog.set_values(Settings(daily_loss_limit_pct=0.0))
#     assert dialog.values().daily_loss_limit_pct == pytest.approx(0.0)


def test_the_tightening_step_takes_part_in_the_recount(dialog) -> None:
    """Шаг подтяжки пересчитывает пояснение, как четыре соседних поля.

    Сегодня безвредно: в проверке шаг не участвует. Но поле, молчащее
    в ряду говорящих, — это заготовка для тихого расхождения.
    """
    dialog.trailing_enabled.setChecked(True)
    seen: list[str] = []
    dialog.trailing_step.valueChanged.connect(lambda _: seen.append(dialog.take_note.text()))
    dialog.trailing_step.setValue(0.2)
    assert seen, "изменение шага подтяжки ничего не пересчитало"


def test_apply_reaches_the_subscriber(dialog) -> None:
    """Нажали «Применить» — подписчик получил новые значения."""
    received: list[Settings] = []
    dialog.settings_changed.connect(received.append)
    dialog.average_period.setValue(20)
    dialog.take_profit.setValue(0.8)
    dialog.volume.setValue(2)
    dialog.buttons.button(QDialogButtonBox.StandardButton.Apply).click()

    assert len(received) == 1
    assert received[0].average_period == 20
    assert received[0].take_profit_pct == pytest.approx(0.8)
    assert received[0].volume == 2


def test_change_travels_from_window_to_engine(qapp, monkeypatch, make_window) -> None:
    """Полный путь: окно → диалог → порт. Перезапуска нет.

    Диалог подменён на такой, который сразу правит поле и нажимает «Применить»:
    настоящий `exec()` ждёт человека и в прогоне тестов повис бы.
    """
    class Instant(SettingsDialog):
        def confirm(self, values) -> bool:
            # Подтверждение «было → стало» открыло бы модальное окно и повисло.
            return True

        def exec(self) -> int:
            self.average_period.setValue(9)
            self.volume.setValue(4)
            self.buttons.button(QDialogButtonBox.StandardButton.Apply).click()
            return 1

    monkeypatch.setattr(ui.main_window, "SettingsDialog", Instant)
    port = RecordingPort()
    window = make_window(port)
    window.open_settings()

    applied = [payload for name, payload in port.calls if name == "apply_settings"]
    assert applied, "настройки не дошли до движка"
    assert applied[-1].average_period == 9
    assert applied[-1].volume == 4
    assert window.settings().average_period == 9


# ------------------------------------------- выбранный алгоритм и его окно
#
# На вкладке «Сигнал» стоит **строка**, а не абзац. До 08.09.2026 здесь было
# полное описание правила: 391 точка при умолчаниях, 459 при включённом
# фильтре против пилы — половина вкладки, поля средней уходили под прокрутку.
# Описание переехало в отдельное окно (`tests/test_ui_algorithm_dialog.py`),
# здесь проверяется то, что осталось: название, кнопка и обмен значением.


def _option(
    strategy_id: str = "ma_reverse_always",
    title: str = "Реверс по средней",
    *,
    chosen: bool = True,
) -> AlgorithmOption:
    """Строка каталога для проверки. Тексты подставные и это намеренно.

    Настоящие собирает сам алгоритм (`strategies/`), и проверяются они там же.
    Окно обязано показывать пришедшее как есть, каким бы оно ни было.
    """
    return AlgorithmOption(
        id=strategy_id,
        title=title,
        summary=f"{title}: закрытие выше средней — робот хочет быть в лонге.",
        details=f"{title}. Правило абзацами.\n\n• Закрытие выше средней — лонг.",
        chosen=chosen,
    )


def test_the_algorithm_row_lives_on_the_signal_tab(dialog) -> None:
    """Название алгоритма и кнопка выбора — на вкладке «Сигнал», над полями.

    На чужой вкладке они объясняли бы поля, которых на ней нет: человек ищет
    ответ «чьи это настройки» там, где правит числа.
    """
    page = dialog.page_of("Сигнал")
    assert page is not None, "вкладки «Сигнал» в окне нет вовсе"
    assert page.isAncestorOf(dialog.algorithm_name), (
        "название алгоритма лежит не на вкладке «Сигнал»"
    )
    assert page.isAncestorOf(dialog.algorithm_button), (
        "кнопка выбора алгоритма лежит не на вкладке «Сигнал»"
    )


def test_the_signal_tab_keeps_a_line_and_not_the_whole_description(dialog) -> None:
    """Полное описание правила на вкладку не попадает ни одной строкой.

    Ради этого задача и делалась: абзац занимал половину вкладки и вытеснял
    поля средней под прокрутку. Проверяется не высота в точках (она зависит
    от шрифта машины), а сам факт: длинного текста на вкладке нет.

    Мутация, обязанная ронять проверку: вернуть описание ярлыком в
    `_algorithm_group`.
    """
    option = _option()
    long_details = option.details + "\n\n" + "Ещё абзац описания. " * 40
    dialog.set_algorithms((AlgorithmOption(
        id=option.id, title=option.title, summary=option.summary,
        details=long_details, chosen=True,
    ),))
    page = dialog.page_of("Сигнал")
    assert page is not None
    shown = [label.text() for label in page.findChildren(QLabel)]
    assert not any(long_details in text for text in shown), (
        "полное описание правила снова стоит на вкладке «Сигнал»"
    )
    assert any(option.title in text for text in shown), (
        "вкладка не называет выбранный алгоритм вовсе"
    )


def test_the_window_says_out_loud_that_the_catalogue_has_not_arrived(dialog) -> None:
    """Каталог не приехал — окно говорит об этом, а не молчит пустым местом.

    Пустая строка на месте названия читается как «алгоритма нет». Правило 13
    `CLAUDE.md`: молчание — самостоятельный дефект.
    """
    assert dialog.algorithm_note.text() == CATALOGUE_NOT_ARRIVED
    assert dialog.algorithm_name.text() == Settings().strategy_id, (
        "имя выбранного алгоритма не показано вовсе"
    )


def test_the_catalogue_names_the_chosen_algorithm(dialog) -> None:
    """Каталог пришёл — на вкладке стоит название, а не имя латиницей.

    ⚠️ И **только** название: строка беды спрятана, правило словами живёт
    в окне выбора. Абзац на вкладке занимал 391–459 точек и вытеснял поля
    средней под прокрутку — ради этого задача и делалась.
    """
    dialog.set_algorithms((_option(),))
    assert dialog.algorithm_name.text() == "Реверс по средней"
    assert not dialog.algorithm_note.text(), (
        "рядом с исправным названием осталась строка беды"
    )
    assert "лонг" in dialog.algorithm_name.toolTip(), (
        "правило одной фразой не досталось даже всплывающей подсказке"
    )


def test_an_algorithm_missing_from_the_catalogue_is_said_out_loud(dialog) -> None:
    """Выбран алгоритм, которого в сборке нет: сказать, а не подменить молча.

    Файл настроек более новой сборки приносит незнакомое имя. Подстановка
    умолчания означала бы торговлю правилом, которого владелец счёта
    не выбирал, при исправном виде окна.
    """
    dialog.set_values(Settings(strategy_id="atr_channel"))
    dialog.set_algorithms((_option(chosen=False),))
    assert dialog.algorithm_name.text() == "atr_channel"
    assert "нет" in dialog.algorithm_note.text(), (
        "окно не сказало, что такого алгоритма в сборке нет"
    )
    assert dialog.values().strategy_id == "atr_channel", (
        "окно молча подменило выбранный алгоритм"
    )


def test_the_name_is_plain_text_not_markup(dialog) -> None:
    """Разметку окно не угадывает: «<» в названии съел бы всё, что за ним."""
    from PySide6.QtCore import Qt

    assert dialog.algorithm_name.textFormat() == Qt.TextFormat.PlainText
    dialog.set_algorithms((_option(title="Реверс <по средней>"),))
    assert "<" in dialog.algorithm_name.text()


def test_the_chosen_algorithm_reaches_the_settings(dialog, monkeypatch) -> None:
    """Выбрал алгоритм в окне выбора — он уехал в `Settings` по «Применить».

    Проверяется вся дорога окна: кнопка открывает выбор, выбор возвращает имя,
    имя попадает в `values()`. Мутация, обязанная ронять проверку: перестать
    класть `self._strategy_id` в `values()`.
    """
    catalogue = (_option(), _option("atr_channel", "Канал ATR", chosen=False))
    dialog.set_algorithms(catalogue)

    class Picks(ui.settings_dialog.AlgorithmDialog):
        """Окно выбора, которое сразу выбирает второй алгоритм и соглашается."""

        def exec(self) -> int:
            self.set_chosen("atr_channel")
            return int(QDialog.DialogCode.Accepted)

    monkeypatch.setattr(ui.settings_dialog, "AlgorithmDialog", Picks)
    dialog.choose_algorithm()
    assert dialog.algorithm_name.text() == "Канал ATR"
    assert dialog.values().strategy_id == "atr_channel"


def test_a_refused_choice_changes_nothing(dialog, monkeypatch) -> None:
    """Закрыл окно выбора «Отменой» — выбранный алгоритм остался прежним.

    Канарейка предыдущей проверки: окно, кладущее выбор в настройки
    независимо от ответа, зеленело бы на ней и меняло бы торговое правило
    после «Отмены».
    """
    dialog.set_algorithms((_option(), _option("atr_channel", "Канал ATR", chosen=False)))

    class Refuses(ui.settings_dialog.AlgorithmDialog):
        def exec(self) -> int:
            self.set_chosen("atr_channel")
            return int(QDialog.DialogCode.Rejected)

    monkeypatch.setattr(ui.settings_dialog, "AlgorithmDialog", Refuses)
    dialog.choose_algorithm()
    assert dialog.values().strategy_id == "ma_reverse_always"


def test_the_catalogue_reaches_the_open_window_from_the_port(
    qapp, monkeypatch, make_window
) -> None:
    """Пункт приёмки: поменял период, нажал «Применить» — описание обновилось.

    Проверяется весь путь окна: главное окно слушает порт, держит открытое
    окно настроек и передаёт ему новый каталог. Без хранения ссылки
    на открытое окно человек поменял бы период, нажал «Применить» и читал бы
    в «Подробнее» описание **прежнего** правила — то есть окно врало бы ровно
    в том месте, ради которого заведено.

    Мутация, обязанная ронять проверку: убрать `self._settings_dialog`
    из `MainWindow.open_settings` либо перестать звать `set_algorithms`
    в `_on_algorithms`.
    """
    class PortThatAnswers(RecordingPort):
        """Порт, отвечающий на «Применить» новым каталогом — как настоящий.

        `HistoryPort.apply_settings` испускает `algorithms_changed` синхронно,
        внутри самой команды: подмена повторяет эту форму, а не придумывает
        свою.
        """

        def apply_settings(self, settings: Settings) -> None:
            super().apply_settings(settings)
            self.algorithms_changed.emit((AlgorithmOption(
                id="ma_reverse_always",
                title="Реверс по средней",
                summary="Правило одной фразой.",
                details=f"Закрытие выше EMA({settings.average_period}) — лонг.",
                chosen=True,
            ),))

    seen: list[str] = []

    class Instant(SettingsDialog):
        def confirm(self, values) -> bool:
            return True

        def exec(self) -> int:
            seen.append(self._algorithms[0].details if self._algorithms else "")
            self.average_period.setValue(20)
            self.buttons.button(QDialogButtonBox.StandardButton.Apply).click()
            seen.append(self._algorithms[0].details if self._algorithms else "")
            return 1

    monkeypatch.setattr(ui.main_window, "SettingsDialog", Instant)
    port = PortThatAnswers()
    window = make_window(port)
    port.algorithms_changed.emit((AlgorithmOption(
        id="ma_reverse_always",
        title="Реверс по средней",
        summary="Правило одной фразой.",
        details="Закрытие выше EMA(15) — лонг.",
        chosen=True,
    ),))
    window.open_settings()

    assert seen[0] == "Закрытие выше EMA(15) — лонг.", (
        "открытое окно настроек не получило каталога, известного главному окну"
    )
    assert seen[1] == "Закрытие выше EMA(20) — лонг.", (
        "после «Применить» описание осталось с прежним правилом"
    )


def test_the_window_asks_the_port_for_the_catalogue_when_settings_open(
    qapp, monkeypatch, make_window
) -> None:
    """Каталог доезжает до окна настроек **без** «Применить» — потому что спросили.

    Пункт приёмки, пойманный на владельце счёта 09.09.2026: он запустил
    программу, открыл настройки и увидел `ma_reverse_always` вместо названия,
    пустой список выбора и указание выбрать из пустоты. Причина — вызова
    `request_settings` не было ни в одной строке продуктового кода: каталог
    уезжал в окно только из `apply_settings`, то есть после первого
    «Применить».

    ⚠️ Каталог здесь **никто не подаёт руками**: ни `set_algorithms`
    на диалоге, ни `algorithms_changed.emit` до открытия. Он приходит ровно
    так, как приходит у настоящего порта, — ответом на просьбу окна. Прежняя
    проверка подавала его прямо в конструктор диалога и потому зеленела при
    полностью отсутствующей проводке.

    Мутация, обязанная ронять проверку: снять `self.port.request_settings()`
    из `MainWindow.open_settings`.
    """
    class PortWithACatalogue(RecordingPort):
        """Порт, отвечающий на просьбу двумя сигналами — как `HistoryPort`.

        Форма ответа списана с `app/port.py::HistoryPort.request_settings`:
        сперва значения полей, следом каталог. Синхронно, в самом вызове.
        """

        def request_settings(self) -> None:
            self.calls.append(("request_settings", None))
            self.settings_applied.emit(Settings())
            self.algorithms_changed.emit((AlgorithmOption(
                id="ma_reverse_always",
                title="Реверс по скользящей средней",
                summary="Закрытие выше средней — лонг.",
                details="Правило абзацами.",
                chosen=True,
            ),))

    seen: dict[str, object] = {}

    class Instant(SettingsDialog):
        def exec(self) -> int:
            seen["name"] = self.algorithm_name.text()
            seen["note"] = self.algorithm_note.text()
            seen["button"] = self.algorithm_button.isEnabled()
            return 0

    monkeypatch.setattr(ui.main_window, "SettingsDialog", Instant)
    port = PortWithACatalogue()
    window = make_window(port)
    window.open_settings()

    assert ("request_settings", None) in port.calls, (
        "окно открыло настройки, ни о чём не спросив порт: каталог взяться "
        "неоткуда"
    )
    assert seen["name"] == "Реверс по скользящей средней", (
        f"на вкладке стоит {seen.get('name')!r} вместо названия алгоритма"
    )
    assert not seen["note"], (
        f"рядом с исправным названием осталась строка беды: {seen.get('note')!r}"
    )
    assert seen["button"], "кнопка выбора выключена при пришедшем каталоге"


def test_the_choose_button_is_off_while_there_is_nothing_to_choose(dialog) -> None:
    """Каталог не пришёл — кнопка выбора выключена, и сказано почему.

    Владелец счёта 09.09.2026 нажал её при пустом каталоге и получил окно
    с пустым списком и требованием выбрать из него. Кнопка, ведущая в тупик,
    обязана быть выключена **с объяснением**, а не отказывать после нажатия
    (§6 брифа окна, тот же случай, что «Старт» на токене «только чтение»).

    Мутация, обязанная ронять проверку: убрать `setEnabled` из
    `_show_algorithm` либо вернуть кнопке одну подсказку на оба случая.
    """
    assert not dialog.algorithm_button.isEnabled(), (
        "кнопка выбора жмётся при пустом каталоге и ведёт в пустое окно"
    )
    assert "выключена" in dialog.algorithm_button.toolTip(), (
        "выключенная кнопка не говорит, почему она выключена"
    )
    assert "перезапустите программу" in dialog.algorithm_note.text(), (
        "окно сказало, что списка нет, и не сказало, что с этим делать"
    )

    dialog.set_algorithms((_option(),))
    assert dialog.algorithm_button.isEnabled(), (
        "каталог приехал, а кнопка выбора осталась выключенной"
    )
    assert "Подробнее" in dialog.algorithm_button.toolTip(), (
        "на живой кнопке осталась подсказка про выключенную"
    )


def test_the_choose_button_is_hidden_while_the_build_has_one_algorithm(dialog) -> None:
    """Алгоритм в сборке один — кнопки «Выбрать алгоритм…» на вкладке нет.

    Решение 0063: выбирать не из чего, и кнопка, ведущая в окно выбора из
    одной строки, — лишнее нажатие. Название алгоритма остаётся видно.
    Пустой каталог кнопку не прячет (там она выключена с объяснением),
    два алгоритма — показывают.

    Мутация, обязанная ронять проверку: `setVisible(True)` вместо
    `setVisible(len(self._algorithms) != 1)` в `_show_algorithm`.

    ⚠️ `isHidden`, а не `isVisibleTo`: окно не показано, и кнопка на
    невыбранной вкладке «невидима» и без мутации.
    """
    assert not dialog.algorithm_button.isHidden(), (
        "при пустом каталоге кнопка спрятана, а не выключена с объяснением"
    )

    dialog.set_algorithms((_option(),))
    assert dialog.algorithm_button.isHidden(), (
        "алгоритм в сборке один, а кнопка выбора на вкладке осталась"
    )
    assert not dialog.algorithm_name.isHidden(), (
        "вместе с кнопкой спряталось и название алгоритма"
    )

    dialog.set_algorithms((_option(), _option("other", "Другой", chosen=False)))
    assert not dialog.algorithm_button.isHidden(), (
        "алгоритмов два, а кнопки выбора между ними нет"
    )


def test_the_window_does_not_talk_to_the_settings_it_closed(
    qapp, monkeypatch, make_window
) -> None:
    """Закрытому окну настроек новый каталог не рассказывают, а следующему — да.

    Ссылка на закрытый диалог, оставленная у главного окна, — это обращение
    к разрушенному объекту Qt при первом же новом каталоге.

    ⚠️ Проверяется **факт обращения**, а не падение, и это замер, а не вкус:
    исключение из слота Qt наружу из `emit` не выходит — PySide печатает
    трассировку и продолжает (проверено 08.09.2026). Тест «программа
    не упала» был бы поэтому вакуумным.
    """
    told: list[int] = []

    class Instant(SettingsDialog):
        def set_algorithms(self, options) -> None:
            told.append(len(tuple(options)))
            super().set_algorithms(options)

        def exec(self) -> int:
            return 1

    monkeypatch.setattr(ui.main_window, "SettingsDialog", Instant)
    port = RecordingPort()
    window = make_window(port)

    window.open_settings()
    after_first = len(told)
    port.algorithms_changed.emit((AlgorithmOption(
        id="ma_reverse_always", title="Реверс", summary="s", details="d", chosen=True,
    ),))
    assert len(told) == after_first, (
        "главное окно рассказало новый каталог уже закрытому окну настроек"
    )

    window.open_settings()
    assert told[-1] == 1, (
        "каталог, пришедший при закрытом окне, не достался следующему открытию"
    )


def test_tab_order_follows_the_screen(dialog, qapp) -> None:
    """Клавиатура: Tab идёт сверху вниз, а не в порядке создания виджетов.

    Порядок по умолчанию — порядок создания. Поля объявлены одним списком,
    а разложены по разделам, поэтому без явной цепочки Tab прыгает из «объёма»
    в «дневной лимит» через половину окна.
    """
    dialog.show()
    qapp.processEvents()
    # Полный список полей окна в том порядке, в каком они стоят на экране,
    # вкладка за вкладкой. Полный намеренно: пропуск поля здесь тест
    # не роняет — виджет, которого нет в списке, просто не замечается, —
    # и список с дырами перестал бы стеречь то, ради чего заведён.
    expected = [
        # «Инструмент и данные»
        dialog.instrument, dialog.expiry_halt_days, dialog.timeframe,
        dialog.history_depth_days, dialog.depth_days,
        dialog.price_step, dialog.ruble_per_point,
        # «Сигнал»
        dialog.average_period, dialog.average_kind,
        # «Вход и выход»
        dialog.reversal_moment, dialog.after_take_profit,
        dialog.take_profit_enabled,
        dialog.take_fixed, dialog.trailing_enabled,
        dialog.take_profit,
        dialog.trailing_start, dialog.trailing_offset, dialog.trailing_step,
        # «Торговое окно»
        dialog.window_start, dialog.window_end, dialog.close_on_time_end,
        # «Деньги»
        # ⚠️ Потолка объёма нет: ПРЕДОХРАНИТЕЛЬ ВЫКЛЮЧЕН НА ЭТАПЕ (D-113).
        dialog.volume,
        # ПРЕДОХРАНИТЕЛЬ ВЫКЛЮЧЕН НА ЭТАПЕ (D-113)
        # dialog.daily_loss_limit_enabled, dialog.daily_loss_limit,
        # dialog.free_funds_reserve_enabled, dialog.free_funds_reserve,
        dialog.commission, dialog.slippage_steps,
        # «Программа»
        dialog.log_directory,
    ]
    seen: list[object] = []
    current = dialog.instrument
    for _ in range(400):
        current = current.nextInFocusChain()
        if current is None:
            break
        owner = current
        while owner is not None and owner not in expected:
            owner = owner.parentWidget()
        if owner is not None and (not seen or seen[-1] is not owner):
            seen.append(owner)
        if len(seen) >= len(expected) - 1:
            break
    assert seen == expected[1:], "порядок обхода по Tab не совпадает с порядком на экране"


def test_escape_closes_and_enter_confirms(dialog, qapp) -> None:
    """Enter и Esc в диалоге работают — требование к клавиатуре."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QDialogButtonBox as Box

    dialog.show()
    qapp.processEvents()
    ok = dialog.buttons.button(Box.StandardButton.Ok)
    assert ok.isDefault() or ok.autoDefault(), "Enter не подтверждает диалог"

    escape = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    qapp.sendEvent(dialog, escape)
    qapp.processEvents()
    assert not dialog.isVisible(), "Esc не закрывает окно настроек"


def test_all_four_forks_are_settings_not_constants() -> None:
    """Четыре развилки торговой логики — настройки, а не зашитые константы.

    Решение 0004: владелец счёта ответил, что все четыре становятся опциями
    в окне. Умолчание у каждой — поведение действующего прототипа, потому что
    замеры есть только на нём: ставить в бой неизмеренный режим нельзя.

    Тест сторожит и состав значений, и умолчания. Если кто-то решит «упростить»
    и зашить один вариант, движок начнёт делать не то, что показывает окно.
    """
    assert [kind.name for kind in AverageKind] == ["EMA", "SMA"]
    assert [moment.name for moment in ReversalMoment] == ["NEXT_BAR", "SAME_BAR"]
    assert [after.name for after in AfterTakeProfit] == [
        "STOP_FOR_THE_DAY", "WAIT_FOR_SIGNAL", "RESTORE_AT_ONCE"
    ]

    defaults = Settings()
    assert defaults.average_kind is AverageKind.EMA
    # Переворот в одной свече и выход с предельной ценой — требования
    # единственного алгоритма, ставшие умолчаниями (решение 0063).
    assert defaults.strategy_id == "ma_reverse_always"
    assert defaults.reversal_moment is ReversalMoment.SAME_BAR
    assert defaults.time_exit_order is TimeExitKind.LIMIT
    assert defaults.after_take_profit is AfterTakeProfit.STOP_FOR_THE_DAY


# ПРЕДОХРАНИТЕЛЬ ВЫКЛЮЧЕН НА ЭТАПЕ (D-113): сторожа потолка объёма.
# def test_volume_above_the_cap_is_shown_as_is_on_a_fresh_dialog(make_dialog) -> None:
#     """То же, но диалог создаётся с потолком, отличным от умолчания.
#
#     Прежняя версия теста брала готовую фикстуру, у которой потолок уже равнялся
#     тому, что ставил тест. `setValue` при совпадающем значении сигнала не шлёт,
#     пересчёт максимума не срабатывал, и подмена не проявлялась: тест проходил
#     и на сломанном коде. Совпадение чисел в фикстуре маскировало дефект.
#
#     Свежесть окна здесь и есть предмет проверки, поэтому берётся строитель,
#     а не готовое окно: каждое из трёх сочетаний попадает в конструктор,
#     а не в `setValue` поверх уже стоящего значения.
#     """
#     for volume, cap in ((10, 7), (10, 5), (50, 2)):
#         window = make_dialog(
#             Settings(volume=volume, volume_cap=cap, volume_cap_enabled=True)
#         )
#         assert window.values().volume == volume, (
#             f"окно подменило объём {volume} на {window.values().volume} "
#             f"при потолке {cap}"
#         )
#         assert str(cap) in window.cap_note.text()
#         assert "выше потолка" in window.cap_note.text()
#
#
# def test_volume_above_the_cap_is_shown_as_is(dialog) -> None:
#     """Окно показывает настоящий объём, а не подрезанный под потолок.
#
#     Прежде `set_values` ставил min(объём, потолок), и на «ОК» окно отправляло
#     уменьшенное значение как новую настройку — решение по деньгам, принятое
#     окном за владельца счёта и не попадающее в журнал решений.
#     """
#     source = Settings(volume=10, volume_cap=5)
#     dialog.set_values(source)
#     assert dialog.values().volume == 10, "окно молча подменило объём"


def test_the_alerts_follow_the_theme(dialog, qapp) -> None:
    """Строки тревоги перекрашиваются вместе с системной темой.

    `guards_note` берёт цвет в момент создания, а `changeEvent`
    у диалога не было: после переключения на тёмную он оставался красным
    светлой темы — 3,16:1 на тёмном фоне. Сведения при этом не терялись,
    но это строки про отсутствующие предохранители, и приглушать их нечем.
    """
    from PySide6.QtGui import QColor, QPalette

    from ui.theme import DARK, LIGHT

    # Тревожной строка становится по состоянию: включённый предохранитель,
    # который нечем проверить.
    dialog.set_values(Settings(volume=10))
    dialog.show()
    qapp.processEvents()
    assert LIGHT.danger in dialog.guards_note.styleSheet()

    original = QPalette(qapp.palette())
    try:
        dark = QPalette(original)
        dark.setColor(QPalette.ColorRole.Window, QColor("#15181d"))
        qapp.setPalette(dark)
        qapp.processEvents()
        for label, name in (
            (dialog.guards_note, "предохранитель нечем проверить"),
        ):
            assert DARK.danger in label.styleSheet(), (
                f"строка тревоги «{name}» осталась в цветах прежней темы: "
                f"{label.styleSheet()!r}"
            )
    finally:
        qapp.setPalette(original)
        qapp.processEvents()

    assert LIGHT.danger in dialog.guards_note.styleSheet(), "тема не вернулась"


#: Порог контраста, ниже которого текст в окне настроек считается нечитаемым.
#:
#: ⚠️ 3,5, а не 4,5 из WCAG, и разница объяснена, а не подогнана. Меряется
#: **нарисованная** картинка, а подсказки набраны шрифтом на пункт мельче
#: основного: сглаженные глифы такого размера не достигают заданного цвета
#: ни в одном пикселе. Заданный цвет подсказки (`Theme.text_dim`, `#6b7280`
#: в светлой) даёт на подложке группы 4,9:1 — порог WCAG соблюдён; растр
#: того же текста даёт 3,78:1.
#:
#: Число взято как **храповик** от замера 05.09.2026: до правки худшее
#: значение было **1,68:1** (подсказки красились `palette(mid)` — ролью,
#: заведённой в Qt для теней рамок, а не для текста). Опускать порог ниже
#: достигнутого нельзя; поднимать — вместе с правкой шрифта или цвета.
MIN_CONTRAST = 3.5


def _contrast(first, second) -> float:
    """Отношение контраста по WCAG между двумя цветами."""
    def channel(value: float) -> float:
        value /= 255.0
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    def luminance(color) -> float:
        return (
            0.2126 * channel(color.red())
            + 0.7152 * channel(color.green())
            + 0.0722 * channel(color.blue())
        )

    light, dark = sorted((luminance(first), luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def _worst_contrast_in(dialog) -> tuple[float, str]:
    """Худшая пара «текст на своём фоне» по нарисованной картинке окна."""
    from collections import Counter

    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QScrollArea

    body = dialog.findChild(QScrollArea).widget()
    picture = body.grab().toImage()
    worst, guilty = 99.0, ""
    for label in body.findChildren(QLabel):
        if not label.text() or label.height() < 5:
            continue
        corner = label.mapTo(body, label.rect().topLeft())
        seen: Counter = Counter()
        pixels = []
        for y in range(corner.y(), min(corner.y() + label.height(), picture.height())):
            for x in range(corner.x(), min(corner.x() + label.width(), picture.width())):
                color = QColor(picture.pixel(x, y))
                seen[(color.red(), color.green(), color.blue())] += 1
                pixels.append(color)
        if not pixels:
            continue
        # Фон — самый частый цвет полосы; текст — самый далёкий от него
        # пиксель: на сглаженном глифе это его сердцевина.
        background = QColor(*seen.most_common(1)[0][0])
        ink = max(pixels, key=lambda color: _contrast(color, background))
        ratio = _contrast(ink, background)
        if ratio < worst:
            worst, guilty = ratio, f"{label.text()[:60]!r} {ink.name()} на {background.name()}"
    return worst, guilty


def test_every_line_of_the_settings_is_readable_in_both_themes(qapp) -> None:
    """Ни одна строка окна настроек не тонет в фоне — по нарисованной картинке.

    ⚠️ Замер 05.09.2026 нашёл ровно то, чего проверка на цвета в коде найти
    не могла: **все** подсказки окна красились `palette(mid)`, а роль `Mid`
    в Qt предназначена для теней рамок. В светлой палитре это `#b8b8b8`
    на подложке `#ececec` — **1,68:1**, то есть подсказки не читались вовсе.
    В тёмной случайно выходило 5,1:1, поэтому дефект видел только тот,
    у кого система светлая, а разработка идёт на тёмной.

    ⚠️ Тема переключается **на живом окне**, а не задаётся до его сборки.
    Первая редакция строила окно уже в нужной палитре — и не замечала,
    что `changeEvent` перестал перекрашивать подсказки: цвет прописан
    в таблицу стилей в момент создания и сам не меняется. Проверено
    мутацией: снос ветки `HINT_ROLE` оставлял прогон зелёным.

    Меряется картинка, а не таблица стилей: подстановка `palette(...)`
    вычисляется Qt в момент отрисовки, и по коду её значение не видно.
    """
    from PySide6.QtGui import QColor, QPalette

    from ui.settings_dialog import SettingsDialog as Dialog

    original = QPalette(qapp.palette())
    window = Dialog(Settings())
    try:
        window.resize(640, 3800)
        window.show()
        qapp.processEvents()
        qapp.processEvents()
        worst, guilty = _worst_contrast_in(window)
        assert worst >= MIN_CONTRAST, (
            f"строка тонет в фоне светлой темы ({worst:.2f}:1 при пороге "
            f"{MIN_CONTRAST}:1): {guilty}"
        )

        dark = QPalette(original)
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base):
            dark.setColor(role, QColor("#141317"))
        for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text):
            dark.setColor(role, QColor("#e8e6e1"))
        qapp.setPalette(dark)
        qapp.processEvents()
        qapp.processEvents()
        worst, guilty = _worst_contrast_in(window)
        assert worst >= MIN_CONTRAST, (
            f"строка тонет в фоне тёмной темы ({worst:.2f}:1 при пороге "
            f"{MIN_CONTRAST}:1): {guilty}"
        )
    finally:
        qapp.setPalette(original)
        window.deleteLater()
        qapp.processEvents()


def test_the_hints_follow_the_theme(dialog, qapp) -> None:
    """Подсказки под полями перекрашиваются вместе с системной темой.

    ⚠️ Замер контраста этого **не ловит** и не может: приглушённый цвет
    светлой темы (`#6b7280`) на тёмной подложке даёт 3,8:1 — мало для
    порога WCAG, но выше нашего храповика. Проверено мутацией: снос ветки
    `HINT_ROLE` из `changeEvent` оставлял замер зелёным. Поэтому цвет
    сверяется с темой прямо, как у ярлыков тревоги.
    """
    from PySide6.QtGui import QColor, QPalette

    from ui.theme import DARK, LIGHT

    hints = [
        label for label in dialog.findChildren(QLabel)
        if LIGHT.text_dim in label.styleSheet()
    ]
    assert len(hints) > 10, (
        f"подсказок в окне нашлось {len(hints)} — проверка почти вакуумна"
    )

    original = QPalette(qapp.palette())
    try:
        dark = QPalette(original)
        dark.setColor(QPalette.ColorRole.Window, QColor("#15181d"))
        qapp.setPalette(dark)
        qapp.processEvents()
        stale = [
            label.text()[:40] for label in hints
            if DARK.text_dim not in label.styleSheet()
        ]
        assert not stale, (
            f"подсказки остались в цветах прежней темы: {stale[:3]}"
        )
    finally:
        qapp.setPalette(original)
        qapp.processEvents()


def test_no_text_of_the_settings_is_painted_with_the_frame_shadow_role() -> None:
    """`palette(mid)` в цветах текста запрещён — это роль теней рамок.

    Канарейка к замеру выше: тот меряет картинку и стоит секунды, этот
    ловит возврат приёма одной строкой поиска. Оба нужны — замер поймает
    и новый способ покрасить текст незаметно, а поиск не даст вернуть
    именно этот.
    """
    source = (
        Path(__file__).resolve().parent.parent / "ui" / "settings_dialog.py"
    ).read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in source.splitlines()
        if "palette(mid)" in line and not line.lstrip().startswith(("#", '"""', "⚠️"))
    ]
    assert not offenders, (
        "текст красится ролью теней рамок — в светлой палитре это 1,68:1: "
        + "; ".join(offenders)
    )


def test_the_take_error_follows_the_theme(dialog, qapp) -> None:
    """Сообщение о запрете ввода — тоже. Оно красится внутри `_sync_take`."""
    from PySide6.QtGui import QColor, QPalette

    from ui.theme import DARK, LIGHT

    dialog.trailing_enabled.setChecked(True)
    dialog.trailing_offset.setValue(1.0)
    dialog.trailing_start.setValue(0.1)
    assert dialog.take_error(), "проверка вакуумна: сочетание годное"
    assert LIGHT.danger in dialog.take_note.styleSheet()

    original = QPalette(qapp.palette())
    try:
        dark = QPalette(original)
        dark.setColor(QPalette.ColorRole.Window, QColor("#15181d"))
        qapp.setPalette(dark)
        qapp.processEvents()
        assert DARK.danger in dialog.take_note.styleSheet(), (
            f"запрет остался в цветах прежней темы: {dialog.take_note.styleSheet()!r}"
        )
        # ⚠️ Пересчёт не должен подменить текст запрета на успокаивающий.
        assert "⚠️" in dialog.take_note.text()
    finally:
        qapp.setPalette(original)
        qapp.processEvents()


@pytest.mark.parametrize(
    "start, offset",
    [
        (0.5, 0.2), (1.0, 0.2), (0.2, 0.2), (0.1, 1.0), (0.0, 0.2),
        (0.2, 0.0), (0.0, 0.0), (5.0, 4.99), (0.01, 0.01), (0.02, 0.01),
    ],
)
def test_the_window_and_the_engine_agree_on_the_trailing_rule(
    dialog, start, offset
) -> None:
    """Правило скользящего тейка живёт в двух местах — они обязаны совпадать.

    `ui/settings_dialog.take_error()` повторяет границу, которую движок
    держит у себя (`engine/settings.py`), чтобы владелец счёта увидел отказ
    в момент ввода, а не получил его от движка после «ОК». Сегодня тексты
    совпадают дословно, но ничем не связаны: правка в одном месте разойдётся
    с другим молча, и разойдётся она в обе стороны — либо окно выпустит
    настройки, на которых движок упадёт, либо запретит то, что движок
    принимает.

    Импорт `engine` из теста направление зависимостей не нарушает: `ui/`
    разрешено читать `engine/` (ARCHITECTURE.md §2), а здесь и вовсе тест.

    ⚠️ Проверяется совпадение **вердиктов**, а не текстов. Тексты у окна
    и движка разные намеренно: движок пишет для журнала, окно — для человека
    у поля ввода.
    """
    from engine.settings import EngineSettings

    dialog.trailing_enabled.setChecked(True)
    dialog.trailing_offset.setValue(offset)
    dialog.trailing_start.setValue(start)
    window_refuses = bool(dialog.take_error())

    try:
        EngineSettings(
            trailing_take_profit=True,
            trailing_start_percent=start,
            trailing_offset_percent=offset,
        )
        engine_refuses = False
    except (ValueError, TypeError):
        engine_refuses = True

    assert window_refuses == engine_refuses, (
        f"порог {start} и отступ {offset}: окно "
        f"{'запрещает' if window_refuses else 'разрешает'}, движок "
        f"{'запрещает' if engine_refuses else 'разрешает'} — одно из двух "
        "правил разошлось с другим"
    )


def test_the_trailing_rule_check_is_not_blind() -> None:
    """Канарейка: среди перебранных пар есть и запрещённые, и разрешённые.

    Иначе сравнение вердиктов проходило бы на наборе, где обе стороны всегда
    отвечают одинаково, и разойтись им было бы негде.
    """
    from engine.settings import EngineSettings

    verdicts = set()
    for start, offset in [(0.5, 0.2), (0.2, 0.2), (0.2, 0.0), (0.1, 1.0)]:
        try:
            EngineSettings(
                trailing_take_profit=True,
                trailing_start_percent=start,
                trailing_offset_percent=offset,
            )
            verdicts.add(False)
        except (ValueError, TypeError):
            verdicts.add(True)
    assert verdicts == {True, False}, (
        "перебор не содержит обоих исходов — сравнение вердиктов вакуумно"
    )


# ------------------------------------------- поля, заведённые 05.09.2026


def test_the_depth_of_the_view_is_ninety_days_by_default(dialog) -> None:
    """Глубина показа стоит в окне и по умолчанию равна 90 дням (`D-026`)."""
    assert dialog.values().depth_days == 90


def test_the_depth_hint_names_the_warm_up_of_the_average(dialog) -> None:
    """Подсказка глубины называет прогрев средней — тем же числом, что и `market/`.

    Второй расчёт прогрева в окне разошёлся бы с первым молча: одно число
    показывалось бы в настройках, другое требовалось бы при загрузке истории.
    """
    from market import warmup_bars

    dialog.average_period.setValue(15)
    assert str(warmup_bars(15)) in dialog.depth_note.text()
    dialog.average_period.setValue(60)
    assert str(warmup_bars(60)) in dialog.depth_note.text(), (
        "подсказка про прогрев не изменилась вместе с периодом средней"
    )
    assert warmup_bars(60) != warmup_bars(15), "проверка вакуумна: числа совпали"


@pytest.mark.parametrize(
    ("kind", "count", "expected"),
    [
        ("bars", 1, "1 свеча"), ("bars", 2, "2 свечи"), ("bars", 5, "5 свечей"),
        ("bars", 16, "16 свечей"), ("bars", 52, "52 свечи"), ("bars", 21, "21 свеча"),
        ("bars", 111, "111 свечей"),
        ("days", 1, "1 день"), ("days", 22, "22 дня"), ("days", 90, "90 дней"),
        ("days", 13, "13 дней"),
        ("steps", 1, "1 шаг"), ("steps", 2, "2 шага"), ("steps", 5, "5 шагов"),
        # Дробное берёт родительный единственного: «полтора шага», не «шагов».
        ("steps", 1.5, "1,5 шага"), ("steps", 0.5, "0,5 шага"),
    ],
)
def test_numbers_agree_with_their_words(kind: str, count: float, expected: str) -> None:
    """Число согласовано со словом: «52 свечи», «1 день», «1,5 шага».

    Небрежность в этих фразах стоит дороже, чем кажется: им не верят целиком,
    вместе с числом, ради которого фраза написана. Правило одно на все три
    единицы (`_agree`), а не переписано в каждой.
    """
    from ui.settings_dialog import _agree

    words = {
        "bars": ("свеча", "свечи", "свечей"),
        "days": ("день", "дня", "дней"),
        "steps": ("шаг", "шага", "шагов"),
    }[kind]
    assert _agree(count, *words) == expected


def test_closing_at_the_window_end_has_a_field_and_a_price(dialog) -> None:
    """Галочка «закрывать в конце окна» есть, включена и называет цену снятия."""
    assert dialog.values().close_on_time_end is True, "умолчание изменилось"
    assert dialog.window_close_note.text() == ""

    # Галочка действует только при выходе по рынку: «с предельной ценой»
    # (умолчание с 05.10.2026) закрывает всегда.
    dialog.set_values(Settings(
        close_on_time_end=False, time_exit_order=TimeExitKind.MARKET,
    ))
    assert dialog.values().close_on_time_end is False
    note = dialog.window_close_note.text()
    assert "ночь" in note and "выходные" in note, (
        "снятая галочка не сказала, что позиция может пережить ночь и выходные"
    )


def test_slippage_and_the_price_step_survive_a_round_trip(dialog) -> None:
    """Проскальзывание и шаг цены читаются и пишутся окном."""
    dialog.set_values(Settings(price_step=25.0, slippage_steps=1.5))
    values = dialog.values()
    assert values.price_step == pytest.approx(25.0)
    assert values.slippage_steps == pytest.approx(1.5)


def test_an_unconfirmed_cost_of_a_point_shouts_instead_of_standing_quietly(
    dialog,
) -> None:
    """Неподтверждённая стоимость пункта говорит о себе и называет последствие.

    Молчаливая единица хуже отказа: она верна для фьючерса на индекс
    МосБиржи и врёт для пяти контрактов из шести замеренных, а ошибка
    тихая — список сделок тот же, деньги другие.
    """
    dialog.set_values(Settings())
    note = dialog.point_note.text()
    assert "НЕ ПОДТВЕРЖДЕНА" in note, note
    assert "деньги" in note, "не названо последствие, только факт"
    assert "danger" not in note
    assert "font-weight: bold" in dialog.point_note.styleSheet(), (
        "предупреждение о неподтверждённой величине нарисовано как обычная "
        "подсказка — такие не читают"
    )


def test_a_confirmed_cost_of_a_point_says_where_it_came_from(dialog) -> None:
    """Подтверждённая величина называет источник и не красится тревогой."""
    dialog.set_values(Settings(
        ruble_per_point=1.73774, ruble_per_point_source="биржа, RIU6, 04.09.2026"
    ))
    note = dialog.point_note.text()
    assert "биржа, RIU6, 04.09.2026" in note
    assert "НЕ ПОДТВЕРЖДЕНА" not in note
    assert "font-weight: bold" not in dialog.point_note.styleSheet()


def test_editing_the_cost_of_a_point_by_hand_drops_the_source(dialog) -> None:
    """Правка руками снимает отметку «подсказано биржей».

    Иначе окно утверждало бы, что число пришло с биржи, под числом,
    которого биржа не называла, — и владелец счёта доверял бы своей
    опечатке как замеру.
    """
    dialog.set_values(Settings(
        ruble_per_point=1.73774, ruble_per_point_source="биржа, RIU6, 04.09.2026"
    ))
    dialog.ruble_per_point.setValue(2.0)
    assert dialog.values().ruble_per_point_source == ""
    assert "НЕ ПОДТВЕРЖДЕНА" in dialog.point_note.text()


def test_a_cost_of_a_point_at_zero_does_not_leave_the_window(dialog) -> None:
    """Ноль рублей в пункте наружу не выходит: «ОК» и «Применить» гаснут.

    Ноль здесь не «выключено», а «все деньги отчёта нулевые». Молча
    подставить единицу нельзя: это и есть та самая тихая подмена.
    """
    dialog.set_values(Settings(ruble_per_point=0.0))
    assert dialog.costs_error(), "ноль в стоимости пункта принят молча"
    for standard in (
        QDialogButtonBox.StandardButton.Ok,
        QDialogButtonBox.StandardButton.Apply,
    ):
        assert not dialog.buttons.button(standard).isEnabled()
    dialog.ruble_per_point.setValue(1.0)
    assert dialog.costs_error() == ""
    assert dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()


def test_slippage_without_a_price_step_from_the_exchange_is_said_not_blocked(dialog) -> None:
    """Проскальзывание при шаге, которого биржа ещё не сообщила: строка вслух, кнопки горят.

    Шаг программа берёт у биржи сама (решение 05.10.2026) — гасить «ОК»
    на том, что человеку не исправить, значило бы запереть окно настроек
    до ответа биржи. Пока шага нет, проскальзывание не учитывается, и это
    сказано: молчаливая подмена нулём выглядела бы заданной поправкой.
    """
    dialog.set_values(Settings(price_step=0.0, slippage_steps=1.0))
    assert dialog.costs_error() == ""
    assert dialog.costs_note.text().startswith(
        "⚠️ Биржа ещё не сообщила шаг цены — пока проскальзывание"
    ), dialog.costs_note.text()
    for standard in (
        QDialogButtonBox.StandardButton.Ok,
        QDialogButtonBox.StandardButton.Apply,
    ):
        assert dialog.buttons.button(standard).isEnabled()

    dialog.price_step.setValue(25.0)  # так его ставит эхо порта после ответа биржи
    assert dialog.costs_note.text().startswith("В цену каждого исполнения"), (
        dialog.costs_note.text()
    )


def test_the_two_checks_do_not_switch_the_buttons_on_for_each_other(dialog) -> None:
    """Проверок две, и последняя сработавшая не отменяет запрет первой."""
    dialog.set_values(Settings(
        trailing_enabled=True, trailing_start_pct=0.1, trailing_offset_pct=1.0,
        ruble_per_point=0.0,
    ))
    assert dialog.take_error() and dialog.costs_error()
    assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()

    dialog.ruble_per_point.setValue(1.0)      # чинится только вторая
    assert dialog.take_error(), "проверка вакуумна: первая ошибка тоже ушла"
    assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled(), (
        "починка одной проверки включила кнопки при живой второй"
    )


def test_the_log_directory_is_a_field_and_empty_means_the_default(dialog) -> None:
    """Каталог лога задаётся мышкой, пустое значение означает умолчание (`D-021`)."""
    assert dialog.values().log_directory == ""
    assert "userdata" in dialog.log_directory.placeholderText()
    dialog.set_values(Settings(log_directory="/var/log/терминал"))
    assert dialog.values().log_directory == "/var/log/терминал"


def test_every_field_of_the_settings_is_shown_somewhere_in_the_dialog(dialog) -> None:
    """Ни одно поле `Settings` не потеряно окном: показывается и читается.

    Проверка на **обмен**, а не на наличие виджета: поле, у которого есть
    виджет, но которое не попало в `values()`, вернулось бы к умолчанию
    при первом же «Применить» — ровно так жили фильтр против пилы
    и закрытие по концу окна.
    """
    import dataclasses

    dialog.set_values(SAMPLE)
    read = dialog.values()
    lost = [
        field.name
        for field in dataclasses.fields(Settings)
        if getattr(read, field.name) != getattr(SAMPLE, field.name)
    ]
    assert not lost, f"поля не пережили обмен через окно: {lost}"


#: Поле, которому отличающегося от умолчания значения в образце не досталось,
#: и тест, который проверяет его обмен отдельно.
#:
#: ⚠️ Список поимённый и **проверяемый**: названный тест обязан существовать
#: в этом файле, а поле — действительно не иметь второго значения в образце.
#: Тихо вычеркнуть поле из канарейки нельзя — так однажды отключилась проверка
#: сохранения имени алгоритма (`tests/test_app_settings_store.py`, правка
#: 14.09.2026), и мутация «подменять выбранное при каждом старте» не роняла
#: ничего.
#:
#: Почему поле здесь вообще оказалось: `take_profit_enabled` и `trailing_enabled`
#: выражают ОДИН выбор из трёх — не фиксируем, неподвижная цель, скользящий
#: уровень (решение 0060). Умолчание — «неподвижная цель», то есть
#: `(True, False)`; отличаться от него обоими полями значило бы взять
#: `(False, True)`, а такого сочетания у настроек не бывает.
_COVERED_APART = {
    "take_profit_enabled": "test_the_take_switch_survives_the_exchange_on_its_own",
}


def test_the_sample_differs_from_the_defaults_in_every_field() -> None:
    """Канарейка проверки обмена: `SAMPLE` обязан отличаться каждым полем.

    Без неё проверка обмена вакуумна для любого поля, совпавшего
    с умолчанием: окно могло бы вовсе не читать это поле, и `values()`
    вернул бы то же самое умолчание. Ровно так новое поле `filter_enabled`
    прошло проверку обмена, ещё не будучи в неё добавленным.
    """
    import dataclasses

    same = [
        field.name
        for field in dataclasses.fields(Settings)
        if getattr(SAMPLE, field.name) == getattr(Settings(), field.name)
    ]
    assert sorted(same) == sorted(_COVERED_APART), (
        "поля совпали с умолчанием, и проверка обмена их не видит: "
        f"{sorted(set(same) - set(_COVERED_APART))}; а названные исключением "
        f"перестали совпадать: {sorted(set(_COVERED_APART) - set(same))}"
    )
    missing = [name for name in _COVERED_APART.values() if name not in globals()]
    assert not missing, (
        "исключение ссылается на проверку, которой в этом файле нет: " + ", ".join(missing)
    )


def test_the_take_switch_survives_the_exchange_on_its_own(dialog) -> None:
    """Выключенная фиксация прибыли переживает обмен через окно.

    Отдельным тестом, потому что в общем образце этому полю не досталось
    второго значения (`_COVERED_APART`). Проверяется то же самое: поле,
    которое окно не читает, вернулось бы к умолчанию «фиксируем» —
    и робот начал бы ставить уровень, которого человек не просил.
    """
    dialog.set_values(Settings(take_profit_enabled=False))
    assert dialog.values().take_profit_enabled is False
    dialog.set_values(Settings(take_profit_enabled=True))
    assert dialog.values().take_profit_enabled is True


#: Набор, отличающийся от умолчания **каждым** полем: иначе проверка обмена
#: половину полей не видит — они совпали бы и при полностью потерянном окне.
SAMPLE = Settings(
    instrument="MXH7",
    timeframe="15 минут",
    depth_days=45,
    history_depth_days=120,
    expiry_halt_days=5,
    # ⚠️ Имя нарочно **не из реестра**: окно значений не проверяет и проверять
    # не может — реестра оно не видит (ARCHITECTURE.md §2). Проверяется здесь
    # ровно то, что окно обязано делать: пронести пришедшее снаружи имя через
    # обмен без подмены. Годится ли оно, решает `app/convert.py`, и отказ
    # проверяется там же.
    strategy_id="atr_channel",
    average_period=21,
    average_kind=AverageKind.SMA,
    reversal_moment=ReversalMoment.NEXT_BAR,
    after_take_profit=AfterTakeProfit.WAIT_FOR_SIGNAL,
    # ⚠️ Фиксация прибыли включена, хотя умолчание тоже «включена», и это
    # вынужденно: пара «фиксация / способ» выражает ОДИН выбор из трёх,
    # и сочетания «не фиксируем + скользящий уровень» у настроек не бывает
    # (решение 0060). Отличаться от умолчания обоими полями сразу образец
    # не может; второе поле проверяется отдельно — см. `_COVERED_APART`.
    take_profit_enabled=True,
    take_profit_pct=1.25,
    trailing_enabled=True,
    trailing_start_pct=1.5,
    trailing_offset_pct=0.75,
    trailing_step_pct=0.11,
    window_start=time(9, 30),
    window_end=time(11, 30),
    close_on_time_end=False,
    volume=4,
    # ПРЕДОХРАНИТЕЛЬ ВЫКЛЮЧЕН НА ЭТАПЕ (D-113)
    # volume_cap=9,
    # daily_loss_limit_pct=3.5,
    # free_funds_reserve_pct=12.5,
    commission_per_side_rub=None,
    # ПРЕДОХРАНИТЕЛЬ ВЫКЛЮЧЕН НА ЭТАПЕ (D-113)
    # volume_cap_enabled=True,
    # daily_loss_limit_enabled=True,
    # free_funds_reserve_enabled=True,
    price_step=25.0,
    ruble_per_point=1.73774,
    ruble_per_point_source="биржа, RIU6, 04.09.2026 07:00",
    slippage_steps=1.5,
    minute_order=MinutePriceOrder.ADVERSE_FIRST,
    minute_bar_limit=MinuteBarLimit.FIFTEEN,
    # Выход по концу окна (Ф3 задачи З8). Предельная форма — не умолчание
    # и годится второму алгоритму, которого требует (`_ALWAYS_DEMANDS`).
    time_exit_order=TimeExitKind.MARKET,
    time_exit_limit_steps=7,
    time_exit_wait_bars=3,
    log_directory="/tmp/терминал-логи",
    # ⚠️ Поля календаря в этом окне нет: его правят в своём окне, из меню
    # «Настройки». Но пронести его через обмен окно обязано — иначе «Применить»
    # стирало бы отметки, поставленные мышкой, и в журнале это выглядело бы
    # как снятие календаря владельцем счёта.
    calendar=(CalendarDay(day=date(2026, 6, 12), trading=False),),
)


def test_the_note_that_there_are_no_money_guards_is_on_screen(dialog) -> None:
    """Стережёт МОЛЧАНИЕ: строка «предохранителей нет» не только написана, но и видна.

    ПРЕДОХРАНИТЕЛЬ ВЫКЛЮЧЕН НА ЭТАПЕ (D-113). Прочие сторожа читают
    `guards_note.text()` — а текст у спрятанной надписи остаётся прежним.
    Спрятать строку (или группу над ней) значило бы вернуть ровно то, от чего
    она поставлена: вкладка без полей и без слов читается как «здесь всё
    в порядке», и все проверки текста при этом зелёные.

    Вкладка с надписью делается текущей, после чего надпись обязана быть
    видимой в окне: скрытый предок на любом уровне это ломает.
    """
    from PySide6.QtWidgets import QTabWidget

    dialog.set_values(Settings())
    note = dialog.guards_note
    tabs = dialog.findChild(QTabWidget)
    assert tabs is not None, "у окна настроек нет вкладок"
    pages = [i for i in range(tabs.count()) if tabs.widget(i).isAncestorOf(note)]
    assert pages, "строка про предохранители не стоит ни на одной вкладке"
    tabs.setCurrentIndex(pages[0])
    assert note.text().strip(), "строка про предохранители пуста"
    assert note.isVisibleTo(dialog), (
        "строка о том, что предохранителей нет, спрятана — на вкладке её не видно"
    )


# ------------------------------------------- сторож на этот файл

# ⚠️ Проверка «тест, строящий виджет, обязан просить `qapp`» переехала
# в `tests/test_qt_needs_the_application.py` — ревью 06.09.2026. Здесь она
# смотрела только свой файл (`Path(__file__)`), считала постройкой лишь
# `ast.Name` и собирала имена лишь из `ast.ImportFrom`, то есть пропускала
# `ui.main_window.MainWindow(...)` в строке 627 этого самого файла.
# Теперь разбираются все `tests/test_*.py`, и этот в том числе.


def test_the_unfinished_variant_is_shown_but_not_offered(dialog) -> None:
    """B-050: «Сразу восстановить позицию» виден, выключен и объяснён.

    Поведение: вариант, которого робот не умеет, в списке есть (человек
    видит, что он задуман), но выбрать его мышкой нельзя, и подсказка
    говорит почему. Остальные варианты выбираются как прежде.

    ⚠️ Мутации: убрать `setEnabled(False)` в `_choice` — вариант снова
    выбирается и будет отвергнут при «Применить»; убрать подсказку —
    выключенный пункт без объяснения.
    """
    from PySide6.QtCore import Qt as QtNamespace
    from PySide6.QtGui import QStandardItemModel

    box = dialog.after_take_profit
    model = box.model()
    assert isinstance(model, QStandardItemModel)
    offered = {
        box.itemData(index): model.item(index).isEnabled()
        for index in range(box.count())
    }
    assert offered == {
        AfterTakeProfit.STOP_FOR_THE_DAY: True,
        AfterTakeProfit.WAIT_FOR_SIGNAL: True,
        AfterTakeProfit.RESTORE_AT_ONCE: False,
    }, f"выбирается не то, что умеет робот: {offered}"
    index = box.findData(AfterTakeProfit.RESTORE_AT_ONCE)
    why = box.itemData(index, QtNamespace.ItemDataRole.ToolTipRole)
    assert why and "не сделан" in why, f"выключенный вариант не объяснён: {why!r}"


@pytest.mark.parametrize(
    ("chosen", "other"),
    [
        (MinutePriceOrder.NEAR_FIRST, MinutePriceOrder.ADVERSE_FIRST),
        (MinutePriceOrder.ADVERSE_FIRST, MinutePriceOrder.NEAR_FIRST),
    ],
)
def test_the_minute_order_chosen_in_the_window_reaches_the_run(
    dialog, chosen, other
) -> None:
    """Стережёт: порядок цен минутки, выбранный мышкой, доезжает до прогона.

    Путь человека: список в окне → «Применить» (`values`) → перевод
    в `backtest.MinuteOrder` (`convert.minute_order_of`), который подаётся
    в `Minutes`. Окно открыто на **другом** значении, выбор сделан в самом
    списке — зашитое в сборщике или в переводе значение роняет один
    из двух случаев.
    """
    from app import convert
    from backtest import MinuteOrder

    dialog.set_values(Settings(minute_order=other))
    box = dialog.minute_order
    box.setCurrentIndex(box.findData(chosen))
    assert convert.minute_order_of(dialog.values()) is MinuteOrder(chosen.value)


@pytest.mark.parametrize(
    ("chosen", "other"),
    [
        (MinuteBarLimit.ONE, MinuteBarLimit.FIFTEEN),
        (MinuteBarLimit.FIFTEEN, MinuteBarLimit.FIVE),
        (MinuteBarLimit.FIVE, MinuteBarLimit.ONE),
    ],
)
def test_the_minute_bar_limit_chosen_in_the_window_reaches_the_run(
    dialog, chosen, other
) -> None:
    """Стережёт: порог «наибольшая свеча для проверки по минуткам» доезжает до прогона.

    Путь человека: список на вкладке «Деньги» → «Применить» (`values`) →
    минуты для того, кто собирает прогон (`convert.minute_bar_limit_of`,
    `B-058`). Окно открыто на **другом** значении, выбор сделан в самом
    списке: число, зашитое в сборщике окна или в переводе, роняет хотя бы
    один из трёх случаев. Ожидаемое число — минуты из подписи размера
    свечи (`convert.TIMEFRAMES`), а не из самого перечисления.
    """
    from app import convert

    dialog.set_values(Settings(minute_bar_limit=other))
    box = dialog.minute_bar_limit
    box.setCurrentIndex(box.findData(chosen))
    assert box.currentText() == chosen.label
    expected = convert.TIMEFRAMES[box.currentText()].minutes
    assert convert.minute_bar_limit_of(dialog.values()) == expected
