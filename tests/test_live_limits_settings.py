"""Ожидания боевого исполнителя — в настройках и в окне (правило 16, решение 0066).

`app/fact.py::LiveLimits` держит восемь чисел, от которых зависит,
какие сигналы пропускаются, когда робот встаёт, сколько продаётся при
закрытии и по какой цене уходит боевая заявка. Девятое — сколько свечей
ждать выхода с пределом — своего поля не имеет: это `time_exit_wait_bars`
движка, одно число на двоих.

Что стережётся здесь, словами:

* старый файл настроек, где этих полей нет, читается умолчаниями
  `LiveLimits()` — боевой исполнитель получает то же, что без окна;
* `convert.live_limits` переносит **каждое** поле окна в своё поле
  исполнителя, и ни одно поле исполнителя, кроме свечей выхода, не забыто;
* негодное значение (ноль, NaN, «да», выше границы, пустой список пауз)
  отвергается отказом, который называет поле подписью окна;
* изменение каждого поля даёт строку «было → стало» с его подписью, и строка
  говорит, что действует со следующего включения боя (правка 5.2 гейта);
* отступ цены боевой заявки (решение 0067) — проценты: ни отказ, ни строка
  журнала, ни снимок прогона, ни поле окна не называют его секундами
  (правка 5.1 гейта);
* у свечей выхода с пределом поля в окне нет;
* окно не даёт ввести то, что отвергнет проверка: границы полей те же,
  негодный список пауз гасит кнопки и говорится строкой под полем;
* шаблон настроек этих чисел не несёт и при применении их не трогает.
"""

from __future__ import annotations

import dataclasses
import json
import math
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtWidgets import QDialogButtonBox, QDoubleSpinBox, QSpinBox

from app import convert
from app.fact import LiveLimits
from app.settings_store import SETTINGS_FILE_NAME, SettingsStore
from ui.models import FIELD_CAPTIONS, LIVE_LIMITS_BOUNDS, LIVE_PAUSES_MAX, Settings
from ui.settings_dialog import PLACEMENT, SettingsDialog
from ui.templates import Library, Template, with_current_contract

#: Поля окна «Бой» → поле `LiveLimits`. Записано здесь руками,
#: а не взято из `convert._LIVE_FROM_WINDOW`: сверка таблицы с самой собой
#: зеленела бы при любой её ошибке. Вход извне — имена `LiveLimits`.
WINDOW_TO_LIMITS: dict[str, str] = {
    "live_stale_after_sec": "stale_after",
    "live_event_wait_sec": "event_wait",
    "live_fill_wait_sec": "fill_wait",
    "live_cancel_wait_sec": "cancel_wait",
    "live_position_settle_sec": "position_settle",
    "live_reconnect_pauses_sec": "reconnect_pauses",
    "live_fok_pct": "fok_percent",
    "live_band_wait_sec": "band_wait",
    "live_trailing_spread_pct": "trailing_spread_percent",
}

#: Поле исполнителя без своего поля в окне — и откуда оно берётся.
NOT_FROM_WINDOW: dict[str, str] = {"limit_exit_bars": "time_exit_wait_bars"}

#: Отличное от умолчания и годное значение каждого поля окна.
OTHER: dict[str, object] = {
    "live_stale_after_sec": 90,
    "live_event_wait_sec": 5,
    "live_fill_wait_sec": 45,
    "live_cancel_wait_sec": 20,
    "live_position_settle_sec": 2.5,
    "live_reconnect_pauses_sec": (2, 4, 8),
    "live_fok_pct": 3.5,
    "live_band_wait_sec": 12,
    "live_trailing_spread_pct": 0.25,
}

#: Поле → негодные значения. Ноль и NaN — из задания; «да» — `bool` есть
#: подкласс `int`, и без проверки `true` из файла стало бы секундой.
BAD: list[tuple[str, object]] = [
    *((name, value) for name in (
        "live_stale_after_sec", "live_event_wait_sec",
        "live_fill_wait_sec", "live_cancel_wait_sec", "live_position_settle_sec",
        "live_fok_pct", "live_band_wait_sec", "live_trailing_spread_pct",
    ) for value in (0, -5, math.nan, math.inf, True)),
    *((name, LIVE_LIMITS_BOUNDS[name].high * 2) for name in LIVE_LIMITS_BOUNDS
      if name != "live_reconnect_pauses_sec"),
    ("live_reconnect_pauses_sec", ()),
    ("live_reconnect_pauses_sec", (0,)),
    ("live_reconnect_pauses_sec", (1, 2, 601)),
    ("live_reconnect_pauses_sec", (1,) * (LIVE_PAUSES_MAX + 1)),
    ("live_reconnect_pauses_sec", (True,)),
]


def _limits_fields() -> set[str]:
    return {field.name for field in dataclasses.fields(LiveLimits)}


# ---------------------------------------------------------------- старый файл


def test_an_old_file_without_the_live_fields_gives_the_executor_its_defaults(
    tmp_path: Path,
) -> None:
    """Файл сборки до 2в: полей нет — исполнитель получает ровно `LiveLimits()`.

    И это сказано: строка «взяты по умолчанию» называет поля подписями окна.
    Мутации, которые тест ловит: другое умолчание в `ui/models.py` (60 → 30);
    поле, которое `live_limits` не переносит.
    """
    path = tmp_path / SETTINGS_FILE_NAME
    path.write_text(
        json.dumps({"format_version": 1, "settings": {"volume": 2, "average_period": 21}}),
        encoding="utf-8",
    )
    loaded = SettingsStore(tmp_path).load()
    assert loaded.values.volume == 2, "файл не прочитан — проверять нечего"
    assert loaded.troubles == (), f"старый файл прочитан с отказами: {loaded.troubles}"

    got = convert.live_limits(loaded.values)
    want = LiveLimits()
    differ = {
        name: (getattr(got, name), getattr(want, name))
        for name in _limits_fields() - set(NOT_FROM_WINDOW)
        if getattr(got, name) != getattr(want, name)
    }
    assert not differ, f"старый файл дал исполнителю не его умолчания: {differ}"

    said = " ".join(note for note in loaded.notes if "взяты по умолчанию" in note)
    absent = [name for name in WINDOW_TO_LIMITS if f"«{FIELD_CAPTIONS[name]}»" not in said]
    assert not absent, f"о взятых умолчанием полях не сказано: {absent}; {loaded.notes}"


@pytest.mark.parametrize(
    ("field", "filed", "bound"),
    [
        ("live_fill_wait_sec", 0, "от 1 до 600"),
        ("live_stale_after_sec", 100_000, "от 1 до 600"),
        ("live_position_settle_sec", 0.0, "от 0,1 до 30"),
        ("live_reconnect_pauses_sec", [0], "каждое от 1 до 600"),
        ("live_reconnect_pauses_sec", [], "от 1 до 10 чисел"),
        ("live_fok_pct", 50, "число процентов от 0,1 до 20"),
        ("live_band_wait_sec", 0, "число секунд от 1 до 30"),
    ],
    ids=repr,
)
def test_a_bad_wait_limit_in_the_file_is_replaced_by_the_default_out_loud(
    tmp_path: Path, field: str, filed: object, bound: str
) -> None:
    """B-078: предел вне границ из файла — умолчание и строка с подписью поля.

    Тип годный, число негодное: прежде файл пропускал его молча, а потом на нём
    спотыкалось каждое «Применить» — первой карточка биржи, и бой отказывал
    чужой причиной. Остальные поля файла прочитаны.

    Строка называет **свою** границу (`bound` записан здесь руками, а не взят
    у `live_limit_rule`: сверка правила с самим собой зеленела бы при чужой
    границе) и то, что стояло в файле, — после «записано», а не цифрой где-то
    в тексте: «0» есть и в «600».
    """
    path = tmp_path / SETTINGS_FILE_NAME
    path.write_text(
        json.dumps({"format_version": 1, "settings": {"volume": 2, field: filed}}),
        encoding="utf-8",
    )
    loaded = SettingsStore(tmp_path).load()

    assert getattr(loaded.values, field) == getattr(Settings(), field)
    assert loaded.values.volume == 2, "негодный предел отнял у файла соседние поля"
    convert.live_limits(loaded.values)  # прочитанное годно для «Применить»
    said = [line for line in loaded.troubles if f"«{FIELD_CAPTIONS[field]}»" in line]
    assert len(said) == 1, f"о негодном пределе не сказано: {loaded.troubles}"
    assert f"записано {json.dumps(filed)}" in said[0], f"не сказано, что было в файле: {said[0]}"
    assert bound in said[0], f"не та граница поля: {said[0]}"
    assert "Взято прежнее значение" in said[0], said[0]


# ------------------------------------------------------------------ перевод


def test_every_executor_limit_but_the_exit_bars_has_a_window_field() -> None:
    """Полнота: у каждого поля `LiveLimits`, кроме свечей выхода, — поле окна.

    Поле, заведённое исполнителю завтра, роняет тест, пока ему не дадут
    поле в окне или не объяснят, откуда оно берётся.
    """
    covered = set(WINDOW_TO_LIMITS.values()) | set(NOT_FROM_WINDOW)
    assert _limits_fields() == covered, (
        f"поля исполнителя без поля в окне: {sorted(_limits_fields() - covered)}; "
        f"названы, но у исполнителя их нет: {sorted(covered - _limits_fields())}"
    )
    settings = {field.name for field in dataclasses.fields(Settings)}
    assert set(WINDOW_TO_LIMITS) <= settings, sorted(set(WINDOW_TO_LIMITS) - settings)


@pytest.mark.parametrize("window_field", sorted(WINDOW_TO_LIMITS))
def test_live_limits_carries_each_window_field_into_its_own_limit(window_field: str) -> None:
    """Одно поле окна изменено — у исполнителя изменилось оно и только оно.

    Мутации: перепутать два поля в таблице перевода; не переносить поле
    (останется умолчание); переносить паузы не в том порядке.
    """
    base = convert.live_limits(Settings())
    changed = convert.live_limits(Settings().replace(**{window_field: OTHER[window_field]}))
    target = WINDOW_TO_LIMITS[window_field]
    value = OTHER[window_field]
    expected = tuple(float(item) for item in value) if isinstance(value, tuple) else value
    assert getattr(changed, target) == expected, (
        f"«{FIELD_CAPTIONS[window_field]}» = {value!r} дошло до исполнителя "
        f"как {target} = {getattr(changed, target)!r}"
    )
    moved = [
        name for name in _limits_fields() - {target}
        if getattr(changed, name) != getattr(base, name)
    ]
    assert not moved, f"правка «{window_field}» сдвинула чужие пределы: {moved}"


# ------------------------------------------------------------------ отказы


@pytest.mark.parametrize(("field", "value"), BAD, ids=repr)
def test_a_bad_value_is_refused_with_the_window_caption(field: str, value: object) -> None:
    """Негодное число — отказ вслух: подпись поля в тексте, имя поля в `field`.

    `field` открывает в окне вкладку с полем (`SettingsDialog.show_field`).
    Мутации: убрать проверку NaN или «да»; отказ без подписи; отказ без поля.
    """
    with pytest.raises(convert.SettingsRefused) as refused:
        convert.live_limits(Settings().replace(**{field: value}))
    assert refused.value.field == field
    text = str(refused.value)
    assert f"«{FIELD_CAPTIONS[field]}»" in text, text
    assert field not in text, f"имя поля латиницей человеку: {text}"


@pytest.mark.parametrize("field", sorted(LIVE_LIMITS_BOUNDS))
def test_both_bounds_themselves_are_accepted(field: str) -> None:
    """Границы включительно: проверка не строже окна, иначе поле с краю — тупик."""
    bound = LIVE_LIMITS_BOUNDS[field]
    for edge in (bound.low, bound.high):
        value: object = (int(edge),) if field == "live_reconnect_pauses_sec" else edge
        convert.live_limits(Settings().replace(**{field: value}))


# ------------------------------------------------------------------ журнал


@pytest.mark.parametrize("field", sorted(WINDOW_TO_LIMITS))
def test_a_change_of_each_live_field_is_one_journal_line_with_its_caption(field: str) -> None:
    """«Настройки изменены»: строка «подпись: было → стало» на каждое поле.

    Сравнивается тем же `window_changes`, которым порт пишет журнал.
    Мутация: убрать поле из `_WINDOW_TOLD` — изменение пройдёт молча
    (строка «Программа не умеет назвать» общая и подписи не несёт).
    """
    lines = convert.window_changes(Settings(), Settings().replace(**{field: OTHER[field]}))
    caption = FIELD_CAPTIONS[field]
    mine = [line for line in lines if line.startswith(f"{caption}: ") and "→" in line]
    assert len(mine) == 1, f"изменение «{caption}» в журнале: {lines}"


def _line_of(lines: list[str], field: str) -> str:
    """Строка журнала о поле — по подписи окна. Ровно одна, иначе падение."""
    mine = [line for line in lines if line.startswith(f"{FIELD_CAPTIONS[field]}: ")]
    assert len(mine) == 1, f"строк о «{FIELD_CAPTIONS[field]}»: {lines}"
    return mine[0]


def test_the_seconds_in_the_line_read_as_seconds() -> None:
    """Секунды — с единицей и по-русски, список пауз — через запятую."""
    lines = convert.window_changes(
        Settings(),
        Settings(
            live_position_settle_sec=0.5, live_reconnect_pauses_sec=(3, 7),
            live_band_wait_sec=12,
        ),
    )
    assert _line_of(lines, "live_position_settle_sec").startswith(
        f"{FIELD_CAPTIONS['live_position_settle_sec']}: 1 с → 0,5 с"
    ), lines
    assert _line_of(lines, "live_reconnect_pauses_sec").startswith(
        f"{FIELD_CAPTIONS['live_reconnect_pauses_sec']}: 1, 2, 5, 10, 30 с → 3, 7 с"
    ), lines
    assert _line_of(lines, "live_band_wait_sec").startswith(
        f"{FIELD_CAPTIONS['live_band_wait_sec']}: 5 с → 12 с"
    ), lines


@pytest.mark.parametrize("field", sorted(WINDOW_TO_LIMITS))
def test_each_live_change_line_says_it_waits_for_the_next_switch_on(field: str) -> None:
    """Строка о смене поля «Боя» называет все три момента, с которых оно действует.

    Правка 5.2 гейта 04.11.2026 и `/risk` 04.11.2026 п. 2. Исполнитель берёт
    эти числа при сборке «факта» и на ходу не перечитывает
    (`app/fact.py::LiveLimits`); сборка — включение боя, «Возобновить работу»
    и перезапуск хода (`app/port.py`, `_open_fact` → `_fact_limits`). Строка
    «2 % → 3 %» без оговорки читалась бы как действующая; строка «со следующего
    включения боя, идущий бой — со старым» врала бы при перезапуске хода
    в том же применении. Слова записаны здесь руками, а не взяты
    у `ui.models.LIVE_LATER`.
    Мутация молчания: убрать хвост у строк «Боя» или любой из трёх моментов
    из него — тест падает на каждом поле.
    """
    lines = convert.window_changes(Settings(), Settings().replace(**{field: OTHER[field]}))
    line = _line_of(lines, field)
    for moment in (
        "действует не сразу",
        "с включения боя",
        "с «Возобновить работу»",
        "с перезапуска хода робота",
        "по прежнему значению",
    ):
        assert moment in line, (moment, line)
    assert "идущий бой — со старым" not in line, line


def test_the_order_offset_is_a_percent_everywhere_and_never_seconds(dialog) -> None:
    """Правка 5.1 гейта 04.11.2026: отступ цены боевой заявки — проценты.

    Стережёт четыре места, где единица видна человеку: отказ при «Применить»,
    строку журнала «Настройки изменены», снимок прогона, поле окна. Все
    ожидаемые строки записаны руками. Мутация: единица поля в таблице
    `LIVE_LIMITS_BOUNDS` — секунды (как было до 0067) — роняет каждую
    проверку; секунды, вписанные в одно из мест мимо таблицы, — её проверку.
    """
    from app import runs

    caption = FIELD_CAPTIONS["live_fok_pct"]
    with pytest.raises(convert.SettingsRefused) as refused:
        convert.live_limits(Settings(live_fok_pct=50.0))
    said = str(refused.value)
    assert "число процентов от 0,1 до 20" in said and "«50 %»" in said, said
    assert "секунд" not in said, said

    line = _line_of(convert.window_changes(Settings(), Settings(live_fok_pct=3.5)), "live_fok_pct")
    assert line.startswith(f"{caption}: 2 % → 3,5 %"), line

    marks = runs.snapshot_marks(runs.snapshot_of(Settings(live_fok_pct=3.5)))
    assert marks.get(f"{caption}, %") == "3,5", {
        title: value for title, value in marks.items() if caption in title
    }
    assert f"{caption}, с" not in marks

    assert dialog.live_fok_pct.suffix().strip() == "%", dialog.live_fok_pct.suffix()


# ------------------------------------------------------------------- окно


@pytest.fixture
def dialog(qapp) -> Iterator[SettingsDialog]:
    # Отложенное удаление исполняет общая фикстура
    # `conftest.py::deferred_deletions_are_carried_out` после каждого теста.
    shown = SettingsDialog(Settings())
    yield shown
    shown.deleteLater()


def test_the_exit_bars_have_no_field_of_their_own() -> None:
    """Свечи выхода с пределом — одно число: `time_exit_wait_bars`, второго поля нет."""
    settings = {field.name for field in dataclasses.fields(Settings)}
    assert "limit_exit_bars" not in settings
    assert not [name for name in settings if "exit_bars" in name], settings
    placed = {name for name, _tab, _widget in PLACEMENT}
    assert "time_exit_wait_bars" in placed
    assert not [name for name in placed if "limit_exit" in name], placed


def test_the_live_fields_sit_on_their_tab_with_window_captions(dialog) -> None:
    """Шесть полей — на «Бой», подпись строки формы = `FIELD_CAPTIONS`."""
    page = dialog.page_of("Бой")
    assert page is not None, "вкладки «Бой» нет"
    captions = dialog.field_captions()
    for name in WINDOW_TO_LIMITS:
        widget = getattr(dialog, name)
        assert page.isAncestorOf(widget), f"{name} не на вкладке «Бой»"
        assert captions.get(name) == FIELD_CAPTIONS[name], (name, captions.get(name))


def test_the_window_bounds_are_the_check_bounds(dialog) -> None:
    """Окно не даёт ввести то, что откажет проверка, и не строже её.

    Мутация: граница поля шире таблицы — человек введёт 0, «ОК» уйдёт,
    и отказ придёт уже после нажатия.
    """
    for name, bound in LIVE_LIMITS_BOUNDS.items():
        widget = getattr(dialog, name)
        if not isinstance(widget, (QSpinBox, QDoubleSpinBox)):
            continue
        assert (widget.minimum(), widget.maximum()) == pytest.approx((bound.low, bound.high)), name


def test_the_live_fields_survive_the_exchange_through_the_window(dialog) -> None:
    """Показали набор — собрали тот же: поле, забытое в `values()`, вернулось бы к умолчанию."""
    sample = Settings().replace(**OTHER)
    dialog.set_values(sample)
    read = dialog.values()
    lost = {name: getattr(read, name) for name in OTHER if getattr(read, name) != OTHER[name]}
    assert not lost, f"поля не пережили обмен через окно: {lost}"


@pytest.mark.parametrize("text", ["", "1, 0", "1, 2, 601", ", ".join(["1"] * 11)])
def test_bad_pauses_turn_the_buttons_off_and_say_why(dialog, text: str) -> None:
    """Негодный список пауз: «ОК» и «Применить» погашены, причина под полем и на кнопке.

    Стережёт молчание (правило 13): погасшая кнопка без строки — тупик.
    """
    dialog.live_reconnect_pauses_sec.setText(text)
    caption = FIELD_CAPTIONS["live_reconnect_pauses_sec"]
    for standard in (QDialogButtonBox.StandardButton.Ok, QDialogButtonBox.StandardButton.Apply):
        button = dialog.buttons.button(standard)
        assert not button.isEnabled(), f"«{text}»: кнопка {button.text()} доступна"
        assert caption in button.toolTip(), button.toolTip()
    assert not dialog.live_note.isHidden(), "причина под полем спрятана"
    assert caption in dialog.live_note.text(), dialog.live_note.text()


def test_good_pauses_in_any_separators_are_taken(dialog) -> None:
    """Запятая, точка с запятой, пробелы — одно и то же; кнопки доступны."""
    dialog.live_reconnect_pauses_sec.setText("3; 7  15,20")
    assert dialog.values().live_reconnect_pauses_sec == (3, 7, 15, 20)
    assert dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    assert dialog.live_note.isHidden()


# ----------------------------------------------------------------- шаблоны


def test_a_template_neither_carries_nor_overrides_the_live_fields(tmp_path: Path) -> None:
    """Шаблон — про торговое правило, а не про связь с брокером.

    В файл шаблонов эти поля не пишутся, а применённый шаблон оставляет
    числа, выставленные владельцем счёта. Мутация: убрать поле из
    `NOT_IN_TEMPLATE` — оно ляжет в файл, и шаблон, сохранённый до 2в,
    вернёт умолчание поверх выставленного.
    """
    library = Library(tmp_path)
    saved = Settings().replace(**OTHER)
    assert library.write((Template(name="набор", values=saved),)) == ""
    stored = json.loads(library.path.read_text(encoding="utf-8"))
    carried = set(WINDOW_TO_LIMITS) & set(stored["templates"][0]["settings"])
    assert not carried, f"шаблон несёт ожидания исполнителя: {sorted(carried)}"

    # Первым всегда идёт встроенный набор — берётся сохранённый, по имени.
    template = next(one for one in library.read().templates if one.name == "набор")
    assert not set(WINDOW_TO_LIMITS) & set(template.missing), (
        f"шаблон без этих полей назван неполным: {template.missing}"
    )
    current = Settings().replace(**OTHER)
    applied = with_current_contract(template, current)
    moved = [name for name in WINDOW_TO_LIMITS if getattr(applied, name) != OTHER[name]]
    assert not moved, f"шаблон переставил ожидания исполнителя: {moved}"
