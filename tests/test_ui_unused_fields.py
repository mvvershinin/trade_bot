"""Поля, которые выбранный алгоритм не читает: окно их гасит, журнал молчит.

Что здесь стережётся (`D-107`, пункт Г0 плана
`.docs/plans/E5-module-2-implementation.md`)
------------------------------------------------------------------------
«Реверс с постоянной позицией» читает только период и тип средней. Фильтр
против пилы (галочка, порог, подтверждение) и «Цена закрылась ровно
на средней» до него не доходят — а в окне стояли активными, и журнал писал
«Фильтр против пилы: выключен → включён». Человек менял число, робот его
игнорировал, узнать можно было только из исходника.

⚠️ **Истина берётся из поведения, а не из списка.** Какое поле алгоритм
читает, тест узнаёт, шевеля поле и глядя, поменялись ли настройки,
которые уходят торговому модулю (`convert.strategy_settings`). Список
«нечитаемых» в тесте не написан: сторож, сверяющий список со списком,
зеленел бы вместе с ошибкой в обоих.

⚠️ **Контрольная половина обязательна.** Тот же сценарий на первом
алгоритме обязан дать активные поля и строки в журнале — иначе «всегда
гасить» и «всегда молчать» проходили бы тест.
"""

from __future__ import annotations

import enum

import pytest
from helpers import settle_qt

from app import convert
from strategies import registry
from ui.models import ReversalMoment, Settings
from ui.settings_dialog import PLACEMENT, SettingsDialog

#: Поля вкладки «Сигнал», принадлежащие алгоритму. Выбор алгоритма
#: (`strategy_id`) не входит: это не настройка алгоритма, а он сам.
SIGNAL_FIELDS = tuple(
    name for name, tab, _ in PLACEMENT if tab == "Сигнал" and name != "strategy_id"
)

#: Сценарий из ревью: переворот в одной свече, фильтр включён,
#: порог 0,3 %, подтверждение 3.
BASE = Settings(
    reversal_moment=ReversalMoment.SAME_BAR,
    filter_enabled=True,
    threshold_percent=0.3,
    confirm_bars=3,
)


def _perturbed(values: Settings, name: str) -> Settings:
    """Тот же набор, у которого поле `name` сдвинуто на другое допустимое значение."""
    current = getattr(values, name)
    if isinstance(current, bool):
        new: object = not current
    elif isinstance(current, enum.Enum):
        new = next(one for one in type(current) if one != current)
    elif isinstance(current, int):
        new = current + 2
    else:
        new = current + 0.2
    return values.replace(**{name: new})


def _read_by(values: Settings) -> set[str]:
    """Поля, от которых настройки торгового модуля **действительно** меняются."""
    before = convert.strategy_settings(values)
    return {
        name
        for name in SIGNAL_FIELDS
        if convert.strategy_settings(_perturbed(values, name)) != before
    }


@pytest.fixture()
def make_dialog(qapp, monkeypatch):
    """Окно настроек с настоящим каталогом алгоритмов; убирается за собой."""
    built: list[SettingsDialog] = []

    def build(values: Settings) -> SettingsDialog:
        window = SettingsDialog(values)
        monkeypatch.setattr(window, "confirm", lambda _values: True)
        window.set_algorithms(convert.algorithms(values))
        window.set_values(values)
        built.append(window)
        return window

    yield build

    for window in built:
        window.deleteLater()
    settle_qt(qapp)


@pytest.mark.parametrize("strategy_id", registry.known_ids())
def test_a_field_is_active_exactly_when_the_algorithm_reads_it(
    make_dialog, strategy_id
) -> None:
    """Поле вкладки «Сигнал» активно тогда и только тогда, когда алгоритм его читает."""
    values = BASE.replace(strategy_id=strategy_id)
    read = _read_by(values)
    window = make_dialog(values)
    widgets = {name: getattr(window, attr) for name, _, attr in PLACEMENT}
    wrong = {
        name: widgets[name].isEnabled()
        for name in SIGNAL_FIELDS
        if widgets[name].isEnabled() != (name in read)
    }
    assert not wrong, (
        f"у алгоритма «{strategy_id}» поля окна не совпадают с тем, что он читает. "
        f"Читает: {sorted(read)}; расхождение (поле → активно ли в окне): {wrong}"
    )


def test_the_second_algorithm_greys_the_filter_and_says_why(make_dialog) -> None:
    """При №2 строка под алгоритмом видна и называет погашенные поля по-человечески."""
    values = BASE.replace(strategy_id="ma_reverse_always")
    window = make_dialog(values)
    assert not window.unused_note.isHidden(), (
        "поля погашены, а объяснения, почему, в окне нет"
    )
    text = window.unused_note.text()
    for caption in ("Фильтр против пилы", "Цена закрылась ровно на средней"):
        assert caption in text, f"в объяснении нет поля «{caption}»: {text!r}"
    assert window.filter_note.text() == "", (
        "фильтр при №2 не действует, а строка про включённый фильтр горит: "
        f"{window.filter_note.text()!r}"
    )


def test_the_first_algorithm_shows_no_unused_note(make_dialog) -> None:
    """Контроль: №1 читает всё, строки про погашенные поля нет."""
    window = make_dialog(BASE.replace(strategy_id="ema_reverse"))
    assert window.unused_note.isHidden()


#: Правки только тех полей, которые №2 не читает. Первая — сценарий ревью:
#: включить фильтр с порогом 0,3 % и подтверждением 3 и поменять «ровно
#: на средней». Вторая — правка чисел при снятой галочке.
EDITS = (
    (
        Settings(reversal_moment=ReversalMoment.SAME_BAR),
        lambda was: _perturbed(
            was.replace(filter_enabled=True, threshold_percent=0.3, confirm_bars=3),
            "on_price_equals_average",
        ),
    ),
    (
        Settings(reversal_moment=ReversalMoment.SAME_BAR),
        lambda was: was.replace(threshold_percent=0.3, confirm_bars=3),
    ),
)


@pytest.mark.parametrize("edit", range(len(EDITS)))
def test_the_journal_is_silent_about_fields_the_algorithm_does_not_read(edit) -> None:
    """№2: правка нечитаемых полей не даёт строк; №1: та же правка — строки есть."""
    base, change = EDITS[edit]
    for strategy_id, expect_lines in (("ma_reverse_always", False), ("ema_reverse", True)):
        was = base.replace(strategy_id=strategy_id)
        now = change(was)
        lines, error = convert.all_changes(was, now)
        assert error == "", error
        journal = convert.window_changes(was, now) + convert.rule_changes(was, now)
        if expect_lines:
            assert lines and journal, (
                f"«{strategy_id}» читает эти поля, а их правка в журнал не попала"
            )
        else:
            assert lines == [] and journal == [], (
                f"«{strategy_id}» этих полей не читает, а журнал о них пишет: "
                f"{lines or journal}"
            )
