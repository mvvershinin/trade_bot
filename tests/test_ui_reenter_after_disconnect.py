"""Галочка «Заходить заново при обрыве связи» в окне (Р-10 плана 04.11.2026, решение 0067).

Что стережётся, словами:

* галочка стоит на вкладке «Вход и выход» в той же группе, что скользящий
  уровень, и под его числами — а не где-нибудь, где её не найти;
* она доступна, только когда выбран скользящий уровень: у неподвижного тейка
  и без тейка её положение ничего не меняет, и живое поле было бы обманом;
* погашенная, она не теряет выбор владельца счёта: «Применить» уносит
  то положение, что стоит, — иначе смена способа туда и обратно молча
  снимала бы галочку;
* подсказка говорит то, что владелец счёта попросил объяснить: оба положения
  в убытке и случай «не в убытке», режим «Выключен», чем закрывается позиция
  (процент «Отступ предельной цены», П6) и что на прогон по истории галочка
  не влияет (П8).

Обмен значения через окно (показали — собрали то же) стережёт общая
проверка `tests/test_ui_settings.py` через `SAMPLE`.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from PySide6.QtWidgets import QGroupBox

from ui.models import FIELD_CAPTIONS, Settings
from ui.settings_dialog import REENTER_HINT, SettingsDialog


@pytest.fixture
def dialog(qapp) -> Iterator[SettingsDialog]:
    # Отложенное удаление исполняет общая фикстура
    # `conftest.py::deferred_deletions_are_carried_out` после каждого теста.
    shown = SettingsDialog(Settings())
    yield shown
    shown.deleteLater()


def _group_of(widget) -> QGroupBox | None:
    parent = widget.parentWidget()
    while parent is not None and not isinstance(parent, QGroupBox):
        parent = parent.parentWidget()
    return parent


def test_the_switch_sits_next_to_the_trailing_level(dialog) -> None:
    """Вкладка «Вход и выход», группа скользящего уровня, подпись из `FIELD_CAPTIONS`."""
    page = dialog.page_of("Вход и выход")
    assert page is not None
    switch = dialog.reenter_after_disconnect
    assert page.isAncestorOf(switch), "галочка не на вкладке «Вход и выход»"
    group = _group_of(switch)
    assert group is not None and group is _group_of(dialog.trailing_enabled), (
        "галочка не в группе скользящего уровня"
    )
    assert dialog.field_captions().get("reenter_after_disconnect") == (
        FIELD_CAPTIONS["reenter_after_disconnect"]
    )


@pytest.mark.parametrize(
    ("take_on", "trailing", "live"),
    [(True, True, True), (True, False, False), (False, True, False), (False, False, False)],
    ids=["скользящий", "неподвижный", "без тейка, был скользящий", "без тейка"],
)
def test_the_switch_is_live_only_with_the_trailing_level(
    dialog, take_on: bool, trailing: bool, live: bool
) -> None:
    """Доступна при скользящем уровне, погашена иначе — и выбор не теряется."""
    dialog.set_values(Settings().replace(
        take_profit_enabled=take_on, trailing_enabled=trailing,
        trailing_start_pct=0.5, trailing_offset_pct=0.2,
        reenter_after_disconnect=True,
    ))
    assert dialog.reenter_after_disconnect.isEnabled() is live
    assert dialog.values().reenter_after_disconnect is True, (
        "погашенная галочка потеряла выбор владельца счёта на «Применить»"
    )


def test_the_hint_explains_what_the_owner_asked_to_explain(dialog) -> None:
    """Подсказка: оба положения, «не в убытке», «Выключен», П6, П8.

    Слова записаны руками. Мутация: убрать из подсказки любую из пяти вещей.
    """
    hint = dialog.reenter_after_disconnect.toolTip()
    assert hint == REENTER_HINT
    lowered = hint.lower()
    for words in (
        "обрыв",                       # когда срабатывает
        "галочка снята",               # положение «выключена»
        "галочка стоит",               # положение «включена»
        "не в убытке",                 # третья строка таблицы 0067
        "до конца дня не входит",      # что делает «выключена»
        "«выключен»",                  # режим робота
        "«отступ предельной цены»",    # П6: чем считается предел закрытия
        "на прогон по истории галочка не влияет",  # П8
    ):
        assert words in lowered, f"в подсказке нет «{words}»"
