"""Шаблоны настроек, подтверждение «было → стало» и колесо мыши.

Что стережёт каждый тест — первой строкой его докстринга.

Три места, где эта работа способна соврать деньгами, и все три проверяются
мутацией — то есть тест краснеет, если убрать защиту, а не только если
сломать вызов:

1. **шаблон применяется целиком или не применяется вовсе** — половина набора,
   выданная за набор, хуже отказа;
2. **шаблон без прогонов даёт прочерк и слова, а не ноль** — ноль читается
   как «дало ноль рублей»;
3. **разные периоды не складываются** — сумма по несопоставимым отрезкам
   и есть подгонка на глаз.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, time, timedelta

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QAbstractSpinBox, QComboBox, QTableWidget

import ui.templates_dialog
from app import convert, runs
from market import CandleStore, RunOrigin, SessionRecord
from ui import backend
from ui.backend import Backend, RunStats, TemplateRun
from ui.models import Settings
from ui.settings_dialog import SettingsDialog
from ui.templates import BUILTIN_NAME, Library, Template
from ui.templates_dialog import NEVER_RUN, TemplatesDialog
from ui.wheel_guard import GUARDED_TYPES, guard_wheel


@pytest.fixture()
def library(tmp_path):
    return Library(tmp_path)


@pytest.fixture()
def real_backend(tmp_path):
    """Настоящая створка: перечень изменений и снимок считает `app/`.

    Прогоны подменены пустыми — про них отдельные тесты; всё остальное
    настоящее, иначе проверка шаблонов проверяла бы заглушку.
    """
    previous = backend.current()
    backend.use(Backend(
        changes=convert.settings_diff,
        runs=lambda sets: RunStats(tuple(() for _ in sets)),
        snapshot=runs.snapshot_of,
        userdata=lambda: tmp_path,
    ))
    yield
    backend.use(previous)


def _accepting(monkeypatch) -> list[tuple[Settings, Settings]]:
    """Подменить подтверждение согласием, запомнив, о чём спрашивали."""
    asked: list[tuple[Settings, Settings]] = []

    def confirm(parent, previous, now, *, lead="") -> bool:
        asked.append((previous, now))
        return True

    monkeypatch.setattr(ui.templates_dialog, "confirm_changes", confirm)
    return asked


def _refusing(monkeypatch) -> None:
    """Подменить подтверждение отказом: человек нажал «Не применять»."""
    monkeypatch.setattr(
        ui.templates_dialog, "confirm_changes",
        lambda parent, previous, now, *, lead="": False,
    )


# --------------------------------------------------------------- библиотека

def test_a_saved_template_returns_every_field_unchanged(library) -> None:
    """Стережёт: набор, сохранённый в файл, возвращается **весь**, поле в поле.

    Мутация, которую тест обязан ловить: укладка, забывшая поле, — тогда
    человек применяет шаблон и получает умолчание вместо того, что сохранял.
    """
    values = Settings(
        instrument="RIU6", timeframe="15 минут", average_period=21,
        take_profit_pct=0.9, volume=4, window_start=time(10, 30),
        commission_per_side_rub=None, threshold_percent=0.4, confirm_bars=3,
    )
    assert library.write((Template(name="Проба", values=values),)) == ""

    got = library.read()
    kept = [one for one in got.templates if not one.builtin]
    assert len(kept) == 1
    assert kept[0].values == values, "шаблон вернулся не тем, каким сохранялся"
    assert kept[0].missing == () and kept[0].unknown == ()


def test_the_field_list_of_a_template_is_the_field_list_of_settings(library) -> None:
    """Стережёт: в файл шаблона попадают **все** поля настроек, без списка руками.

    Поле, заведённое завтра, обязано сохраниться само. Сравнение идёт
    с `dataclasses.fields(Settings)`, а не с перечнем в тесте: перечень
    разошёлся бы с классом ровно так же молча.
    """
    library.write((Template(name="Проба", values=Settings()),))
    payload = json.loads(library.path.read_text(encoding="utf-8"))
    stored = set(payload["templates"][0]["settings"])
    assert stored == {field.name for field in dataclasses.fields(Settings)}


def test_a_template_with_one_bad_value_is_refused_whole(library) -> None:
    """Стережёт: негодное значение отвергает **весь** шаблон, а не одно поле.

    Половина набора, выданная за набор, — худшее из возможного: человек
    считает, что вернул проверенное. Тест мутационный: верни сюда поведение
    файла настроек (потерять поле и читать остальное) — и он покраснеет.
    """
    library.write((Template(name="Кривой", values=Settings(average_period=15)),))
    payload = json.loads(library.path.read_text(encoding="utf-8"))
    payload["templates"][0]["settings"]["average_period"] = "пятнадцать"
    library.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    got = library.read()
    assert [one.name for one in got.templates] == [BUILTIN_NAME], (
        "шаблон с негодным полем всё-таки показан — его можно применить наполовину"
    )
    assert any("Кривой" in trouble for trouble in got.troubles), (
        "шаблон пропал молча: человек не узнает, что набор потерян"
    )
    assert any("наполовину" in trouble for trouble in got.troubles)


def test_a_field_missing_from_a_template_is_named_not_guessed(library) -> None:
    """Стережёт: настройка, появившаяся позже, названа вслух и взята умолчанием.

    Тихая подстановка означала бы применение не того, что человек сохранял.
    """
    library.write((Template(name="Старый", values=Settings(volume=7)),))
    payload = json.loads(library.path.read_text(encoding="utf-8"))
    del payload["templates"][0]["settings"]["slippage_steps"]
    library.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    kept = [one for one in library.read().templates if not one.builtin]
    assert kept[0].missing == ("slippage_steps",)
    assert kept[0].values.slippage_steps == Settings().slippage_steps
    assert kept[0].values.volume == 7, "остальные поля обязаны прочитаться"
    assert not kept[0].complete


def test_an_unknown_key_survives_a_rewrite(library) -> None:
    """Стережёт: настройка от более новой сборки не стирается перезаписью.

    Откатились на сборку постарше, поработали, вернулись — чужие настройки
    на месте. Выбросить незнакомый ключ значит уничтожить чужие данные.
    """
    library.write((Template(name="Будущий", values=Settings()),))
    payload = json.loads(library.path.read_text(encoding="utf-8"))
    payload["templates"][0]["settings"]["hedge_ratio"] = 0.5
    library.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    kept = [one for one in library.read().templates if not one.builtin]
    assert kept[0].unknown == ("hedge_ratio",)
    library.write(tuple(kept))
    again = json.loads(library.path.read_text(encoding="utf-8"))
    assert again["templates"][0]["settings"]["hedge_ratio"] == 0.5


def test_the_builtin_template_is_first_and_never_written(library) -> None:
    """Стережёт: умолчания продукта видны первыми и в файл не попадают.

    За ними стоят замеры и сверка с прототипом; записанные однажды, они
    перестали бы обновляться вместе с программой.
    """
    library.write((Template(name="Мой", values=Settings(volume=2)),))
    names = [one.name for one in library.read().templates]
    assert names[0] == BUILTIN_NAME
    payload = json.loads(library.path.read_text(encoding="utf-8"))
    assert [one["name"] for one in payload["templates"]] == ["Мой"]


def test_a_broken_library_is_set_aside_not_overwritten(library) -> None:
    """Стережёт: нечитаемый файл шаблонов откладывается, а не стирается.

    Это единственный след наборов, которые подбирали руками.
    """
    library.path.write_text("{ это не json", encoding="utf-8")
    got = library.read()
    assert [one.name for one in got.templates] == [BUILTIN_NAME]
    assert got.troubles and "broken-" in got.troubles[0]
    assert list(library.path.parent.glob("*.broken-*.json")), "файл не сохранён"


# ------------------------------------------------------------------- окно

def test_a_template_never_run_shows_words_and_a_dash_not_a_zero(
    qapp, library, real_backend
) -> None:
    """Стережёт: у набора без прогонов в деньгах прочерк, а не ноль.

    Ноль читается как «дало ноль рублей» — это утверждение о деньгах,
    которого никто не делал.
    """
    library.write((Template(name="Непробованный", values=Settings(volume=3)),))
    dialog = TemplatesDialog(library, Settings())
    try:
        row = 1
        cells = [
            dialog.table.item(row, column).text()
            for column in range(dialog.table.columnCount())
        ]
        assert cells[0] == "Непробованный"
        assert cells[3] == NEVER_RUN, "число прогонов подменено нулём"
        assert cells[5] == "—" and cells[6] == "—", (
            "сделки и деньги показаны числом там, где прогонов не было"
        )
        assert "не «ноль рублей»" in dialog.table.item(row, 0).toolTip()
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_runs_of_a_template_are_listed_separately_and_never_summed(
    qapp, library, tmp_path
) -> None:
    """Стережёт: прогоны разных отрезков показаны по одному и не складываются.

    Сумма по несопоставимым периодам — подгонка на глаз. В строке набора
    стоит **последний** прогон, остальные видны списком.
    """
    previous = backend.current()
    made = (
        TemplateRun(
            started_at=datetime(2026, 9, 5, 10, 0), origin="прогон по истории",
            period="01.08.2026 10:05 — 31.08.2026 18:45", trades=40, profit=1000.0,
        ),
        TemplateRun(
            started_at=datetime(2026, 9, 4, 10, 0), origin="прогон по истории",
            period="01.07.2026 10:05 — 31.07.2026 18:45", trades=30, profit=-400.0,
        ),
    )
    backend.use(Backend(
        changes=convert.settings_diff,
        runs=lambda sets: RunStats(tuple(made if number else () for number, _ in enumerate(sets))),
        snapshot=runs.snapshot_of,
        userdata=lambda: tmp_path,
    ))
    library.write((Template(name="Гонянный", values=Settings(volume=2)),))
    dialog = TemplatesDialog(library, Settings())
    try:
        dialog.table.selectRow(1)
        assert dialog.table.item(1, 3).text() == "2"
        assert dialog.table.item(1, 4).text() == made[0].period, (
            "в строке набора стоит не последний прогон"
        )
        assert dialog.table.item(1, 5).text() == "40"
        assert dialog.runs.rowCount() == 2, "прогоны свёрнуты в один"
        shown = {dialog.runs.item(row, 2).text() for row in range(2)}
        assert shown == {one.period for one in made}
        money = {dialog.runs.item(row, 4).text() for row in range(2)}
        assert "+600,00 ₽" not in money and "600,00 ₽" not in money, (
            "деньги разных отрезков сложены в одну сумму"
        )
    finally:
        dialog.deleteLater()
        qapp.processEvents()
        backend.use(previous)


def test_applying_a_template_sends_the_whole_set_at_once(
    qapp, library, real_backend, monkeypatch
) -> None:
    """Стережёт: применение уходит **одним** набором, а не полем за полем.

    Половина применённого набора — это настройки, которых не выбирал никто.
    """
    asked = _accepting(monkeypatch)
    values = Settings(volume=5, average_period=30, take_profit_pct=1.5, depth_days=10)
    library.write((Template(name="Целиком", values=values),))
    got: list[Settings] = []
    dialog = TemplatesDialog(library, Settings())
    dialog.applied.connect(got.append)
    try:
        dialog.table.selectRow(1)
        dialog.apply_selected()
    finally:
        dialog.deleteLater()
        qapp.processEvents()

    assert len(got) == 1, "набор ушёл не одним движением"
    assert got[0] == values, "ушло не то, что лежало в шаблоне"
    assert asked and asked[0][1] == values, "подтверждение спрашивали не про тот набор"


def test_a_refused_confirmation_applies_nothing_at_all(
    qapp, library, real_backend, monkeypatch
) -> None:
    """Стережёт: «Не применять» оставляет настройки прежними целиком.

    Мутация: пропусти ответ подтверждения — и тест покраснеет, потому что
    набор уйдёт наружу.
    """
    _refusing(monkeypatch)
    library.write((Template(name="Отменённый", values=Settings(volume=9)),))
    got: list[Settings] = []
    dialog = TemplatesDialog(library, Settings())
    dialog.applied.connect(got.append)
    try:
        dialog.table.selectRow(1)
        dialog.apply_selected()
    finally:
        dialog.deleteLater()
        qapp.processEvents()

    assert got == [], "отказ от подтверждения всё равно применил настройки"


def test_saving_the_current_settings_keeps_every_field(
    qapp, library, real_backend, monkeypatch
) -> None:
    """Стережёт: «Сохранить текущие» кладёт в шаблон весь набор, а не часть.

    Проверяется через окно, а не мимо него: сохранение — это то, что делает
    кнопка, и путь до файла обязан быть тем же самым.
    """
    from PySide6.QtWidgets import QInputDialog

    monkeypatch.setattr(
        QInputDialog, "getText", staticmethod(lambda *args, **kwargs: ("Вечерний", True))
    )
    current = Settings(volume=6, average_period=25, slippage_steps=2.0, depth_days=45)
    dialog = TemplatesDialog(library, current)
    try:
        dialog.save_current()
        names = [one.name for one in dialog.templates()]
        assert "Вечерний" in names
    finally:
        dialog.deleteLater()
        qapp.processEvents()

    kept = [one for one in library.read().templates if not one.builtin]
    assert kept[0].values == current, "сохранился не весь набор"
    assert kept[0].saved_at is not None, "время сохранения не записано"


def test_the_builtin_template_cannot_be_deleted(qapp, library, real_backend) -> None:
    """Стережёт: умолчания продукта не удаляются мышкой."""
    dialog = TemplatesDialog(library, Settings())
    try:
        dialog.table.selectRow(0)
        assert not dialog.remove_button.isEnabled()
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_without_a_backend_the_window_says_so_instead_of_showing_zeroes(
    qapp, library
) -> None:
    """Стережёт: без торговой части окно говорит вслух, а не показывает пустоту.

    Пустой список статистики читался бы как «прогонов не было».
    """
    previous = backend.current()
    backend.use(None)
    try:
        dialog = TemplatesDialog(library, Settings())
        assert not dialog.trouble.isHidden(), 'строка про отсутствие статистики скрыта'
        assert "без торговой части" in dialog.trouble.text()
        dialog.deleteLater()
        qapp.processEvents()
    finally:
        backend.use(previous)


# --------------------------------------------------- подтверждение изменений

def test_the_money_half_and_the_rest_add_up_to_the_whole_list() -> None:
    """Стережёт: разделение на «деньги» и «прочее» ничего не теряет и не двоит.

    Перечень собирается двумя промежуточными наборами; ошибка в подмене
    полей проявилась бы пропавшей строкой — то есть изменением по деньгам,
    которого человек не увидит.
    """
    before = Settings()
    after = Settings(
        volume=3, volume_cap_enabled=True, commission_per_side_rub=20.0,
        average_period=25, take_profit_pct=1.0, instrument="RIU6",
        daily_loss_limit_enabled=True, free_funds_reserve_pct=40.0,
    )
    diff = convert.settings_diff(before, after)
    whole, trouble = convert.all_changes(before, after)
    assert trouble == ""
    assert sorted(diff.money + diff.other) == sorted(whole), (
        "половины перечня не сходятся с полным перечнем"
    )
    assert not set(diff.money) & set(diff.other), "строка попала в оба списка"


def test_money_changes_are_told_apart_from_the_rest() -> None:
    """Стережёт: объём, потолок, лимит, запас и комиссия идут отдельным списком."""
    diff = convert.settings_diff(Settings(), Settings(volume=3, average_period=25))
    assert any("Объём" in line for line in diff.money)
    assert any("Период средней" in line for line in diff.other)
    assert not any("Объём" in line for line in diff.other)


def test_nothing_changed_means_nothing_to_confirm() -> None:
    """Стережёт: на пустом перечне подтверждения не бывает.

    Диалог на пустом месте приучает жать «Да» не читая, и настоящий
    перестают читать вместе с ним.
    """
    values = Settings(volume=2)
    assert convert.settings_diff(values, values).empty


def test_unreadable_settings_are_a_loud_trouble_not_an_empty_list() -> None:
    """Стережёт: набор, который не разбирается, назван, а не показан пустым.

    Пустой перечень читался бы как «ничего не меняется» ровно там, где
    настройки применить нельзя вовсе.
    """
    from ui.models import AfterTakeProfit

    diff = convert.settings_diff(
        Settings(), Settings(after_take_profit=AfterTakeProfit.RESTORE_AT_ONCE)
    )
    assert diff.trouble, "негодный набор прошёл молча"
    assert not diff.empty, "негодный набор читается как «изменений нет»"


def test_the_settings_dialog_asks_before_letting_values_out(qapp, monkeypatch) -> None:
    """Стережёт: «Применить» в настройках спрашивает подтверждение до отправки.

    Мутация: убери вызов из `_emit` — и тест покраснеет, потому что значения
    уйдут наружу без вопроса.
    """
    dialog = SettingsDialog(Settings())
    asked: list[Settings] = []
    got: list[Settings] = []
    monkeypatch.setattr(dialog, "confirm", lambda values: (asked.append(values), False)[1])
    dialog.settings_changed.connect(got.append)
    try:
        dialog.volume.setValue(4)
        dialog.buttons.button(dialog.buttons.StandardButton.Apply).click()
        assert asked and asked[0].volume == 4, "подтверждения не спросили"
        assert got == [], "отказ не остановил отправку настроек"

        monkeypatch.setattr(dialog, "confirm", lambda values: True)
        dialog.buttons.button(dialog.buttons.StandardButton.Apply).click()
        assert len(got) == 1 and got[0].volume == 4
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_a_second_apply_compares_with_what_was_applied(qapp, monkeypatch) -> None:
    """Стережёт: второе «Применить» сравнивает с применённым, а не с исходным.

    Иначе человек увидел бы во втором подтверждении изменения, которые
    подтвердил минуту назад, и перестал бы читать список.
    """
    dialog = SettingsDialog(Settings())
    asked: list[Settings] = []

    def confirm(values: Settings) -> bool:
        asked.append(dialog.applied())
        return True

    monkeypatch.setattr(dialog, "confirm", confirm)
    try:
        dialog.volume.setValue(4)
        dialog.buttons.button(dialog.buttons.StandardButton.Apply).click()
        dialog.average_period.setValue(30)
        dialog.buttons.button(dialog.buttons.StandardButton.Apply).click()
    finally:
        dialog.deleteLater()
        qapp.processEvents()

    assert asked[0].volume == 1, "первое сравнение шло не с исходным набором"
    assert asked[1].volume == 4, "второе сравнение шло не с уже применённым"


# ------------------------------------------------------------ колесо мыши

def _wheel(widget, *, degrees: int = -120) -> QWheelEvent:
    """Событие колеса мыши над полем — одна ступень вниз."""
    point = QPointF(widget.rect().center())
    return QWheelEvent(
        point, widget.mapToGlobal(point.toPoint()).toPointF(), QPoint(0, 0),
        QPoint(0, degrees), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    )


def test_the_wheel_does_not_change_a_field_without_focus(qapp) -> None:
    """Стережёт: колесо мыши не крутит значение поля, пока на нём нет фокуса.

    Это корень просьбы владельца счёта: прокручивая длинное окно, он проезжал
    курсором над полем и менял цифру по деньгам молча.
    """
    dialog = SettingsDialog(Settings())
    try:
        field = dialog.volume
        field.clearFocus()
        was = field.value()
        qapp.sendEvent(field, _wheel(field))
        assert field.value() == was, "колесо изменило значение поля без фокуса"

        take = dialog.take_profit
        take.clearFocus()
        was_take = take.value()
        qapp.sendEvent(take, _wheel(take))
        assert take.value() == was_take, "колесо изменило цель прибыли без фокуса"
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_the_wheel_still_works_when_the_field_has_focus(qapp) -> None:
    """Стережёт: поле с фокусом колесом крутится — защита не запрещает ввод."""
    dialog = SettingsDialog(Settings())
    dialog.show()
    try:
        field = dialog.volume
        field.setFocus()
        qapp.processEvents()
        if not field.hasFocus():  # без оконного менеджера фокус не всегда даётся
            pytest.skip("окружение не даёт фокус полю — проверять нечего")
        was = field.value()
        # Вверх, а не вниз: объём равен своему минимуму, и вниз крутить некуда.
        qapp.sendEvent(field, _wheel(field, degrees=120))
        assert field.value() != was, "поле с фокусом перестало отвечать на колесо"
    finally:
        dialog.hide()
        dialog.deleteLater()
        qapp.processEvents()


def test_the_arrow_keys_still_change_a_focused_field(qapp) -> None:
    """Стережёт: защита от колеса не трогает клавиатуру.

    Стрелки вверх-вниз — нормальный способ ввода, и ломать его нельзя.
    """
    from PySide6.QtGui import QKeyEvent

    dialog = SettingsDialog(Settings())
    try:
        field = dialog.volume
        was = field.value()
        qapp.sendEvent(field, QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier
        ))
        assert field.value() == was + 1, "стрелка вверх перестала менять значение"
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_every_scrollable_field_of_the_settings_window_is_guarded(qapp) -> None:
    """Стережёт: **все** крутящиеся поля окна настроек под защитой, без пропусков.

    Поле, добавленное завтра мимо защиты, — дыра «во всех полях кроме одного»,
    и находится она деньгами.
    """
    dialog = SettingsDialog(Settings())
    try:
        found: list[object] = []
        for kind in GUARDED_TYPES:
            found.extend(dialog.findChildren(kind))
        assert set(map(id, found)) == set(map(id, dialog.guarded_fields)), (
            "в окне настроек есть крутящееся поле без защиты от колеса"
        )
        assert len(found) > 20, "полей стало подозрительно мало — проверка вакуумна"
        for field in dialog.guarded_fields:
            assert field.focusPolicy() == Qt.FocusPolicy.StrongFocus, (
                "поле берёт фокус колесом — защита снимается тем же движением"
            )
            assert isinstance(field, (QAbstractSpinBox, QComboBox))
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_the_guard_leaves_other_events_alone(qapp) -> None:
    """Стережёт: фильтр пропускает всё, кроме колеса без фокуса."""
    dialog = SettingsDialog(Settings())
    try:
        guarded = guard_wheel(dialog)
        assert guarded, "повторный вызов не должен терять поля"
    finally:
        dialog.deleteLater()
        qapp.processEvents()


# ------------------------------------------------- статистика из журнала

def test_statistics_come_from_recorded_runs_and_match_by_snapshot(tmp_path) -> None:
    """Стережёт: прогон связывается с набором сравнением снимков настроек.

    Числа берутся из записи, а не считаются заново: вторая правда о деньгах
    в этом проекте уже стоила вечера разбора.
    """
    database = tmp_path / "candles.sqlite3"
    values = Settings(volume=2, average_period=21, take_profit_pct=0.7)
    other = Settings(volume=2, average_period=22, take_profit_pct=0.7)
    with CandleStore(database) as store:
        session = store.open_journal_session(SessionRecord(
            origin=RunOrigin.BACKTEST, symbol=values.instrument,
            timeframe=values.timeframe, strategy="EMA-разворот",
            settings=runs.snapshot_of(values),
            note="MXU6, 5 минут: свечей 100, с 01.08.2026 10:05 по 31.08.2026 18:45 МСК.",
        ))
        store.finish_journal_session(session.id, note=(
            "Сделок 12, переворотов 4, свечей 100.\n"
            "  Валовая +1 000,00 ₽, комиссия 200,00 ₽, "
            "чистая +800,00 ₽."
        ))

    stats = runs.matching_runs(database, [values, other])
    assert stats.trouble == ""
    assert len(stats.runs[0]) == 1, "прогон не нашёлся по своему же снимку"
    assert stats.runs[0][0].trades == 12
    assert stats.runs[0][0].profit == pytest.approx(800.0)
    assert stats.runs[0][0].period == "01.08.2026 10:05 — 31.08.2026 18:45"
    assert stats.runs[1] == (), "прогон приписан набору с другим периодом средней"


def test_a_run_on_another_instrument_is_not_this_template(tmp_path) -> None:
    """Стережёт: прогон по другому инструменту шаблону не приписывается.

    Те же торговые настройки на другом контракте — другие деньги.
    """
    database = tmp_path / "candles.sqlite3"
    values = Settings()
    with CandleStore(database) as store:
        session = store.open_journal_session(SessionRecord(
            origin=RunOrigin.BACKTEST, symbol="RIU6", timeframe=values.timeframe,
            strategy="EMA-разворот", settings=runs.snapshot_of(values),
            note="RIU6, 5 минут: свечей 10, с 01.08.2026 10:05 по 02.08.2026 18:45 МСК.",
        ))
        store.finish_journal_session(session.id, note="Сделок 1, переворотов 0, свечей 10.")

    assert runs.matching_runs(database, [values]).runs[0] == ()


def test_a_run_without_a_result_shows_no_numbers_at_all(tmp_path) -> None:
    """Стережёт: прерванный прогон даёт `None`, а не ноль сделок и ноль рублей."""
    database = tmp_path / "candles.sqlite3"
    values = Settings()
    with CandleStore(database) as store:
        store.open_journal_session(SessionRecord(
            origin=RunOrigin.BACKTEST, symbol=values.instrument,
            timeframe=values.timeframe, strategy="EMA-разворот",
            settings=runs.snapshot_of(values), note="прогон прерван",
        ))

    found = runs.matching_runs(database, [values]).runs[0]
    assert len(found) == 1
    assert found[0].trades is None and found[0].profit is None


def test_a_missing_database_is_said_out_loud(tmp_path) -> None:
    """Стережёт: отсутствие базы названо словами, а не показано пустым списком."""
    stats = runs.matching_runs(tmp_path / "нет.sqlite3", [Settings()])
    assert stats.runs == ((),)
    assert "ещё нет" in stats.trouble


def test_the_mode_does_not_break_the_match(tmp_path) -> None:
    """Стережёт: режим робота не участвует в сравнении наборов.

    Режим живёт на панели управления, а не в шаблоне; сравнивай по нему —
    и статистика исчезала бы при каждой смене режима.
    """
    from ui.models import Mode

    database = tmp_path / "candles.sqlite3"
    values = Settings()
    engine = convert.engine_settings(values, Mode.LONG_ONLY)
    module = convert.strategy_settings(values)
    with CandleStore(database) as store:
        session = store.open_journal_session(SessionRecord(
            origin=RunOrigin.BACKTEST, symbol=values.instrument,
            timeframe=values.timeframe, strategy="EMA-разворот",
            settings=runs.settings_text(
                engine, module, algorithm=convert.chosen_algorithm(values)
            ),
            note="MXU6, 5 минут: свечей 10, с 01.08.2026 10:05 по 02.08.2026 18:45 МСК.",
        ))
        store.finish_journal_session(session.id, note="Сделок 3, переворотов 1, свечей 10.")

    assert len(runs.matching_runs(database, [values]).runs[0]) == 1


def test_run_numbers_are_read_back_from_what_the_writer_wrote() -> None:
    """Стережёт: читатель итога прогона согласован с его писателем.

    Полный оборот: `result_note` пишет — разбор читает. Поменяется формат
    записи (пробел, знак минуса, слово) — тест краснеет здесь, а не прочерком
    в окне у владельца счёта.
    """
    from backtest import Costs, HistoryRun, Summary

    summary = Summary(
        trades=143, profitable=57, profitable_share=0.4, gross_profit=-2331.0,
        commission=4004.0, net_profit=-6335.0, max_drawdown=12345.0, reversals=78,
    )
    run = HistoryRun(summary=summary, bars=21811, costs=Costs())
    parsed = runs.run_of(_session_with(runs.result_note(run)))
    assert parsed.trades == 143
    assert parsed.profit == pytest.approx(-6335.0)


def _session_with(finish_note: str):
    """Запись прогона с готовым итогом — для проверки разбора."""
    from market import JournalSession

    return JournalSession(
        id=1, origin=RunOrigin.BACKTEST, started_at=datetime(2026, 9, 5, 10, 0),
        finished_at=datetime(2026, 9, 5, 10, 1) + timedelta(seconds=1),
        symbol="MXU6", timeframe="5 минут", finish_note=finish_note,
    )


# ==========================================================================
# Экспорт, импорт и папка примеров — решение 0051
# ==========================================================================
#
# Четыре места, где эта работа способна отнять у владельца счёта деньги
# или соврать ему про них, и каждое проверено мутацией:
#
# 1. **импорт заменил список вместо добавления** — унесённый набор
#    восстановить нечем;
# 2. **совпадение имён разрешилось молча** — то же самое, только по одному;
# 3. **импортированный набор показал ноль вместо «не запускался»** — ноль
#    это утверждение о деньгах, которого никто не делал;
# 4. **пример потерял своё происхождение** — набор, отобранный на прошлом,
#    без оговорки читается как рекомендация.


def _example_file(path, *, name="Пример 01", origin="MXU6, 5 минут, 01.01.2026 — 01.06.2026",
                  values=None):
    """Файл примера в папке: тот же формат, что у библиотеки."""
    from ui.templates import export_templates

    trouble = export_templates(
        path,
        (Template(name=name, values=values or Settings(volume=3), origin=origin),),
    )
    assert trouble == "", trouble
    return path


def _accept_import(monkeypatch):
    """Окно импорта соглашается, ничего не спрашивая у экрана."""
    from PySide6.QtWidgets import QDialog

    from ui.templates_import import ImportDialog

    monkeypatch.setattr(
        ImportDialog, "exec", lambda self: QDialog.DialogCode.Accepted
    )


def _mute_boxes(monkeypatch):
    """Заглушить сообщения-итоги, чтобы прогон не встал на модальном окне."""
    from PySide6.QtWidgets import QMessageBox

    said: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "information",
        staticmethod(lambda parent, title, text, *a, **k: said.append(text)),
    )
    monkeypatch.setattr(
        QMessageBox, "warning",
        staticmethod(lambda parent, title, text, *a, **k: said.append(text)),
    )
    return said


# ------------------------------------------------------- формат: происхождение

def test_the_origin_of_a_template_survives_a_rewrite(library) -> None:
    """Стережёт: происхождение примера доезжает до файла и обратно.

    Мутация, которую тест обязан ловить: укладчик, забывший `origin`. Тогда
    пример «лучших настроек» приходит без оговорки, на каком отрезке он
    отобран, и читается как рекомендация — а подбор на прошлом дважды
    проиграл бездействию.
    """
    origin = "MXU6, 5 минут, отобран на 01.01.2026 — 01.06.2026"
    library.write((Template(name="Из перебора", values=Settings(), origin=origin),))

    kept = [one for one in library.read().templates if not one.builtin]
    assert len(kept) == 1
    assert kept[0].origin == origin, "происхождение набора потерялось при записи"


def test_an_origin_that_is_not_a_string_becomes_empty_not_invented(library) -> None:
    """Стережёт: негодное происхождение — пусто, а не выдумка.

    Пустое честнее сочинённого: окно скажет «не сказано, на чём отобран»,
    и это верное утверждение.
    """
    library.path.write_text(
        json.dumps({
            "format_version": 1,
            "templates": [{"name": "Кривой", "origin": 42, "settings": {}}],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    kept = [one for one in library.read().templates if not one.builtin]
    assert len(kept) == 1
    assert kept[0].origin == ""


# ------------------------------------------------------------- экспорт файла

def test_an_exported_set_reads_back_as_the_same_set(tmp_path) -> None:
    """Стережёт: выгруженный файл читается обратно — тем же набором.

    Отдельного формата обмена нет намеренно: файл выгрузки — это тот же файл
    библиотеки. Разошлись бы форматы — перенос на другую машину молча
    потерял бы часть значений.
    """
    from ui.templates import export_templates, read_for_import

    values = Settings(instrument="RIU6", average_period=21, take_profit_pct=0.9, volume=4)
    one = Template(name="Перенос", values=values, origin="RIU6, июль 2026")
    path = tmp_path / "перенос.json"
    assert export_templates(path, (one,)) == ""

    got = read_for_import(path)
    assert [t.name for t in got.templates] == ["Перенос"]
    assert got.templates[0].values == values, "значения переехали не те"
    assert got.templates[0].origin == "RIU6, июль 2026"


def test_exporting_the_builtin_set_is_allowed(tmp_path) -> None:
    """Стережёт: умолчания программы тоже выгружаются.

    Запись библиотеки встроенный набор отбрасывает — он не данные человека.
    Выгрузка отбрасывать не должна: сравнить свои умолчания с чужими
    на другой машине — законное дело.
    """
    from ui.templates import builtin_template, export_templates, read_for_import

    path = tmp_path / "builtin.json"
    assert export_templates(path, (builtin_template(),)) == ""
    got = read_for_import(path)
    assert len(got.templates) == 1, "встроенный набор молча не выгрузился"
    assert got.templates[0].values == Settings()


# -------------------------------------------------------------- чужой файл

def test_a_file_offered_for_import_is_never_moved_aside(tmp_path) -> None:
    """Стережёт: негодный **чужой** файл остаётся на месте нетронутым.

    Свой файл откладывается в сторону, чужой — нет: он лежит где угодно
    и принадлежит не нам. Переложить чужой файл значило бы потерять его
    для того, кто его прислал.
    """
    from ui.templates import read_for_import

    path = tmp_path / "чужой.json"
    path.write_text("{ это не json", encoding="utf-8")

    got = read_for_import(path)
    assert got.templates == ()
    assert got.troubles and "не прочитан" in got.troubles[0]
    assert path.read_text(encoding="utf-8") == "{ это не json", "чужой файл изменён"
    assert not list(tmp_path.glob("*.broken-*.json")), "чужой файл переложен в сторону"


def test_an_imported_set_with_one_bad_value_is_refused_whole(tmp_path) -> None:
    """Стережёт: негодное значение в чужом файле отвергает **весь** набор.

    Правило то же, что у своей библиотеки: половина набора, выданная
    за набор, хуже отказа.
    """
    from ui.templates import read_for_import

    path = tmp_path / "кривой.json"
    path.write_text(
        json.dumps({
            "format_version": 1,
            "templates": [{"name": "Кривой", "settings": {"average_period": "пятнадцать"}}],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    got = read_for_import(path)
    assert got.templates == (), "негодный набор пролез в импорт"
    assert any("Кривой" in one and "average_period" in one for one in got.troubles)


def test_an_imported_set_takes_program_defaults_for_fields_it_lacks(tmp_path) -> None:
    """Стережёт: настройка, которой в чужом файле нет, берётся умолчанием.

    Не текущим значением из окна: набор сохранялся, когда этой настройки
    не было, — значит работал он с её умолчанием.
    """
    from ui.templates import read_for_import

    path = tmp_path / "старый.json"
    path.write_text(
        json.dumps({
            "format_version": 1,
            "templates": [{"name": "Старый", "settings": {"volume": 7}}],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    got = read_for_import(path)
    assert got.templates[0].values.volume == 7
    assert got.templates[0].values.average_period == Settings().average_period
    assert "average_period" in got.templates[0].missing, "недостача не названа"


def test_an_unknown_key_in_an_imported_file_is_named_and_not_applied(tmp_path) -> None:
    """Стережёт: ключ из более новой сборки назван и сохранён, но не применён."""
    from ui.templates import read_for_import

    path = tmp_path / "новый.json"
    path.write_text(
        json.dumps({
            "format_version": 1,
            "templates": [{"name": "Будущий", "settings": {"volume": 2, "тайна": 5}}],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    got = read_for_import(path)
    assert got.templates[0].unknown == ("тайна",)
    assert got.templates[0].extras == {"тайна": 5}


# ------------------------------------------------ сложение: добавить, не заменить

def test_import_adds_to_the_list_and_never_replaces_it() -> None:
    """Стережёт: импорт **добавляет**. Главный сторож этой работы.

    Мутация, которую тест обязан ловить: сложение, вернувшее один только
    импортированный список. Тогда единственный набор, который работал,
    исчезает без следа, и восстановить его нечем.
    """
    from ui.templates import merge_templates

    mine = Template(name="Моё", values=Settings(volume=9))
    theirs = Template(name="Чужое", values=Settings(volume=1))
    merged = merge_templates((builtin_of(), mine), (theirs,), _never_asked)

    assert [one.name for one in merged.templates] == ["Моё", "Чужое"], (
        "импорт стёр то, что было в списке"
    )
    assert merged.templates[0].values == mine.values, "мой набор изменился"
    assert (merged.added, merged.replaced, merged.skipped) == (1, 0, 0)


def test_a_name_clash_is_never_resolved_silently() -> None:
    """Стережёт: совпавшее имя разрешается **вопросом**, а не молча.

    Мутация: сложение, само выбравшее «заменить». Тогда набор владельца
    счёта заменяется чужим без единого слова.
    """
    from ui.templates import NameClash, merge_templates

    asked: list[str] = []

    def resolve(one):
        asked.append(one.name)
        return NameClash.SKIP

    mine = Template(name="Общее", values=Settings(volume=9))
    theirs = Template(name="Общее", values=Settings(volume=1))
    merged = merge_templates((builtin_of(), mine), (theirs,), resolve)

    assert asked == ["Общее"], "про совпадение имён не спросили"
    assert merged.templates == (mine,), "мой набор не сохранился"
    assert merged.skipped == 1
    assert any("Общее" in note for note in merged.notes), "о пропуске не сказано"


def test_keeping_both_gives_the_newcomer_a_free_name() -> None:
    """Стережёт: «оставить оба» сохраняет **оба** набора, под разными именами."""
    from ui.templates import NameClash, merge_templates

    mine = Template(name="Общее", values=Settings(volume=9))
    theirs = Template(name="Общее", values=Settings(volume=1))
    merged = merge_templates(
        (builtin_of(), mine), (theirs,), lambda one: NameClash.KEEP_BOTH
    )

    names = [one.name for one in merged.templates]
    assert len(names) == 2 and len(set(names)) == 2, "один из наборов потерялся"
    assert names[0] == "Общее"
    assert merged.templates[1].values == theirs.values
    assert any("Общее" in note for note in merged.notes)


def test_replacing_happens_only_by_a_direct_answer() -> None:
    """Стережёт: замена происходит там и только там, где человек её выбрал."""
    from ui.templates import NameClash, merge_templates

    mine = Template(name="Общее", values=Settings(volume=9))
    theirs = Template(name="Общее", values=Settings(volume=1))
    merged = merge_templates(
        (builtin_of(), mine), (theirs,), lambda one: NameClash.REPLACE
    )

    assert [one.name for one in merged.templates] == ["Общее"]
    assert merged.templates[0].values.volume == 1, "замена не состоялась"
    assert merged.replaced == 1


def test_a_clash_with_the_builtin_name_is_renamed_not_asked_about() -> None:
    """Стережёт: умолчания программы заменить нельзя — и вопрос про них не задаётся.

    Заменить встроенный набор невозможно вовсе: он не хранится в файле.
    Вопрос «заменить ли» был бы обманом — любой ответ дал бы один исход.
    """
    from ui.templates import merge_templates

    theirs = Template(name=BUILTIN_NAME, values=Settings(volume=1))
    merged = merge_templates((builtin_of(),), (theirs,), _never_asked)

    assert len(merged.templates) == 1
    assert merged.templates[0].name != BUILTIN_NAME, "имя встроенного набора занято дважды"
    assert merged.added == 1
    assert any("встроенн" in note for note in merged.notes), "о переименовании не сказано"


def test_a_freshly_made_name_still_fits_the_limit() -> None:
    """Стережёт: имя с номером помещается в предел длины.

    Чтение обрезает имя до предела. Обрезанный номер вернул бы ровно то
    совпадение, от которого имя и уводили.
    """
    from ui.templates import NAME_LIMIT, NameClash, merge_templates

    long_name = "Н" * NAME_LIMIT
    mine = Template(name=long_name, values=Settings(volume=9))
    theirs = Template(name=long_name, values=Settings(volume=1))
    merged = merge_templates(
        (builtin_of(), mine), (theirs,), lambda one: NameClash.KEEP_BOTH
    )

    fresh = merged.templates[1].name
    assert len(fresh) <= NAME_LIMIT, "имя не поместится и будет обрезано при чтении"
    assert fresh != long_name


def _never_asked(one):
    """Резолвер, который звать не должны: совпадения имён в этом тесте нет."""
    raise AssertionError(f"про «{one.name}» спросили там, где совпадения не было")


def builtin_of():
    """Встроенный набор для проверок сложения."""
    from ui.templates import builtin_template

    return builtin_template()


# ----------------------------------------------------------- папка примеров

def test_the_examples_folder_travels_with_the_build() -> None:
    """Стережёт: папка примеров попадает в собранную программу.

    Без ключа сборки кнопка «взять из примеров» показывает у владельца
    счёта пустоту, а отказ тихий: программа работает, примеров просто нет.
    Проверять это глазами на каждой сборке никто не будет.
    """
    from tools import build

    argv = build.command(jobs=1)
    assert f"--include-data-dir={build.EXAMPLES}={build.EXAMPLES}" in argv, (
        "папка примеров не едет в поставку"
    )


def test_every_shipped_example_says_where_it_came_from() -> None:
    """Стережёт: у каждого примера в поставке названо, на чём он отобран.

    Пример без этой строки читается как рекомендация. Рекомендацией он
    не является: подбор на прошлом дважды проиграл бездействию, а валовая
    за год на текущих настройках — +32 ₽ на 1213 сделках.
    """
    from ui.templates import example_files, examples_dir, read_for_import

    folder = examples_dir()
    assert folder.is_dir(), f"папки примеров нет: {folder}"
    for path in example_files(folder):
        loaded = read_for_import(path)
        assert loaded.templates, f"файл примеров {path.name} не прочитался"
        mute = [one.name for one in loaded.templates if not one.origin]
        assert not mute, (
            f"в {path.name} нет происхождения у наборов: {mute}. Пример без "
            "отрезка и инструмента читается как совет"
        )


def test_examples_are_looked_for_next_to_the_program() -> None:
    """Стережёт: папка примеров ищется рядом с программой, а не в текущем каталоге.

    Запуск из другого каталога — обычное дело (ярлык, автозапуск). Поиск
    по относительному пути дал бы «примеров нет» в зависимости от того,
    откуда позвали программу.
    """
    from ui.templates import EXAMPLES_DIR_NAME, examples_dir

    folder = examples_dir()
    assert folder.is_absolute(), "путь к примерам относительный"
    assert folder.name == EXAMPLES_DIR_NAME


# ----------------------------------------------------- окно: импорт и экспорт

def _cell(table: QTableWidget, row: int, column: int) -> str:
    """Текст ячейки. Пустая ячейка — падение с внятными словами, а не `None`."""
    item = table.item(row, column)
    assert item is not None, f"ячейки ({row}, {column}) нет вовсе"
    return item.text()


def _tip(table: QTableWidget, row: int, column: int) -> str:
    """Подсказка ячейки."""
    item = table.item(row, column)
    assert item is not None, f"ячейки ({row}, {column}) нет вовсе"
    return item.toolTip()


def _answer_clash(monkeypatch, answer: str) -> list[str]:
    """Отвечать на вопрос о совпавшем имени нажатием кнопки с этим текстом.

    Возвращает список заданных вопросов: пустой список означает, что окно
    ничего не спросило, — то есть решило молча.
    """
    from PySide6.QtWidgets import QMessageBox

    asked: list[str] = []

    def press(box) -> int:
        asked.append(box.text())
        for button in box.buttons():
            if button.text().replace("&", "") == answer:
                button.click()
                return 0
        raise AssertionError(
            f"кнопки «{answer}» в вопросе нет: {[b.text() for b in box.buttons()]}"
        )

    monkeypatch.setattr(QMessageBox, "exec", press)
    return asked


def test_the_window_imports_by_adding_and_keeps_what_was_there(
    qapp, library, real_backend, monkeypatch, tmp_path
) -> None:
    """Стережёт: импорт в окне **добавляет** набор, не трогая прежние.

    Мутация: запись библиотеки одним импортированным списком. Тогда набор
    владельца счёта исчезает и из окна, и из файла — восстановить нечем.
    """
    library.write((Template(name="Моё", values=Settings(volume=9)),))
    folder = tmp_path / "examples"
    folder.mkdir()
    _example_file(folder / "лидеры.json", name="Из перебора")
    _accept_import(monkeypatch)
    said = _mute_boxes(monkeypatch)

    dialog = TemplatesDialog(library, Settings(), examples=folder)
    try:
        dialog.import_templates()
        names = [one.name for one in dialog.templates()]
    finally:
        dialog.deleteLater()
        qapp.processEvents()

    assert names == [BUILTIN_NAME, "Моё", "Из перебора"], (
        "импорт заменил список вместо того, чтобы добавить"
    )
    kept = [one.name for one in library.read().templates if not one.builtin]
    assert kept == ["Моё", "Из перебора"], "в файле остался не тот список"
    assert said and "Добавлено наборов: 1" in said[-1], "об итоге импорта не сказано"


def test_the_window_asks_before_a_name_is_taken_over(
    qapp, library, real_backend, monkeypatch, tmp_path
) -> None:
    """Стережёт: совпавшее имя в окне разрешается вопросом, а не молча.

    Мутация: импорт, сам выбравший «заменить». Тогда набор владельца счёта
    заменяется чужим, и он узнаёт об этом по чужим цифрам в своей стратегии.
    """
    library.write((Template(name="Общее", values=Settings(volume=9)),))
    folder = tmp_path / "examples"
    folder.mkdir()
    _example_file(folder / "лидеры.json", name="Общее", values=Settings(volume=1))
    _accept_import(monkeypatch)
    _mute_boxes(monkeypatch)
    asked = _answer_clash(monkeypatch, "Оставить оба")

    dialog = TemplatesDialog(library, Settings(), examples=folder)
    try:
        dialog.import_templates()
        kept = [one for one in dialog.templates() if not one.builtin]
    finally:
        dialog.deleteLater()
        qapp.processEvents()

    assert asked, "окно решило судьбу совпавшего имени молча"
    assert "Общее" in asked[0]
    assert len(kept) == 2, "после «оставить оба» в списке не два набора"
    assert kept[0].values.volume == 9, "мой набор изменился без моего согласия"


def test_the_clash_question_offers_three_answers_and_loses_nothing_by_default(
    qapp, library, real_backend
) -> None:
    """Стережёт: у вопроса о совпавшем имени три ответа, и умолчание безопасно.

    «Заменить» уносит прежние значения без возврата. Ставить его кнопкой
    по умолчанию — значит превращать нажатие Enter в потерю набора.
    """
    dialog = TemplatesDialog(library, Settings())
    try:
        box = dialog.clash_box(Template(name="Общее", values=Settings()))
        texts = [button.text().replace("&", "") for button in box.buttons()]
        assert set(texts) == {"Заменить", "Оставить оба", "Пропустить"}
        assert box.defaultButton() is not None
        assert box.defaultButton().text().replace("&", "") == "Оставить оба", (
            "по умолчанию предложено действие, которое теряет набор"
        )
        box.deleteLater()
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_an_imported_set_says_it_was_never_run_instead_of_showing_zero(
    qapp, library, real_backend, monkeypatch, tmp_path
) -> None:
    """Стережёт: импортированный набор показывает «не запускался», а не ноль.

    На **этой** истории он и правда не гонялся. Ноль в столбце денег —
    утверждение «дало ноль рублей», которого никто не делал.
    """
    folder = tmp_path / "examples"
    folder.mkdir()
    _example_file(folder / "лидеры.json", name="Из перебора")
    _accept_import(monkeypatch)
    said = _mute_boxes(monkeypatch)

    dialog = TemplatesDialog(library, Settings(), examples=folder)
    try:
        dialog.import_templates()
        row = [one.name for one in dialog.templates()].index("Из перебора")
        cells = [
            _cell(dialog.table, row, column)
            for column in range(dialog.table.columnCount())
        ]
        tip = _tip(dialog.table, row, 0)
    finally:
        dialog.deleteLater()
        qapp.processEvents()

    assert cells[3] == NEVER_RUN, "число прогонов у импортированного набора подменено"
    assert cells[5] == "—" and cells[6] == "—", "деньги показаны нулём, а не прочерком"
    assert "не «ноль рублей»" in tip
    assert said and "не гонялись" in said[-1], "про отсутствие прогонов не сказано словами"


def test_an_imported_example_keeps_saying_where_it_came_from(
    qapp, library, real_backend, monkeypatch, tmp_path
) -> None:
    """Стережёт: происхождение примера видно в списке после импорта.

    Мутация: столбец «Откуда взят», потерявший значение. Набор, отобранный
    на прошлом, без этой строки читается как рекомендация.
    """
    origin = "MXU6, 5 минут, отобран на 01.01.2026 — 01.06.2026"
    folder = tmp_path / "examples"
    folder.mkdir()
    _example_file(folder / "лидеры.json", name="Из перебора", origin=origin)
    _accept_import(monkeypatch)
    _mute_boxes(monkeypatch)

    dialog = TemplatesDialog(library, Settings(), examples=folder)
    try:
        row = None
        dialog.import_templates()
        row = [one.name for one in dialog.templates()].index("Из перебора")
        columns = [title for title, _ in ui.templates_dialog.COLUMNS]
        shown = _cell(dialog.table, row, columns.index("Откуда взят"))
        tip = _tip(dialog.table, row, 0)
    finally:
        dialog.deleteLater()
        qapp.processEvents()

    assert shown == origin, "происхождение примера пропало из списка"
    assert "не совет" in tip, "оговорка про «это не совет» пропала"


def test_the_import_window_says_out_loud_when_an_example_has_no_origin(
    qapp, tmp_path
) -> None:
    """Стережёт: пример без происхождения назван вслух, а не показан молча.

    Пустая клетка читается как «здесь нечего писать», а написать было что:
    на каком отрезке и на каком инструменте набор отобран.
    """
    from ui.templates_import import NO_ORIGIN, ImportDialog

    folder = tmp_path / "examples"
    folder.mkdir()
    _example_file(folder / "лидеры.json", name="Безродный", origin="")

    window = ImportDialog(folder)
    try:
        assert window.from_examples_now(), "окно открылось не на примерах"
        assert [one.name for one in window.found()] == ["Безродный"]
        assert not window.trouble.isHidden(), "про пример без происхождения промолчали"
        assert "не сказано" in window.trouble.text()
        assert "Безродный" in window.trouble.text()
        columns = [title for title, _ in ui.templates_import.COLUMNS]
        shown = _cell(window.table, 0, columns.index("Откуда взят"))
        assert shown == NO_ORIGIN, "пустая клетка вместо слов"
    finally:
        window.deleteLater()
        qapp.processEvents()


def test_the_import_window_leaves_a_users_own_file_alone_about_origin(
    qapp, tmp_path
) -> None:
    """Стережёт: свой файл без происхождения не попрекают.

    Владелец счёта сохранил свой набор сам и знает, откуда тот взялся.
    Оговорка нужна там, где набор пришёл готовым.
    """
    from ui.templates_import import ImportDialog

    path = _example_file(tmp_path / "моё.json", name="Моё", origin="")
    window = ImportDialog(tmp_path / "нет-такой-папки")
    try:
        window.use_file(path)
        assert [one.name for one in window.found()] == ["Моё"]
        assert window.trouble.isHidden(), (
            "свой файл попрекнули отсутствием происхождения"
        )
    finally:
        window.deleteLater()
        qapp.processEvents()


def test_an_empty_pick_is_refused_with_words(qapp, tmp_path, monkeypatch) -> None:
    """Стережёт: «импортировать», когда ничего не отмечено, объясняет, а не молчит."""
    from PySide6.QtWidgets import QDialog, QDialogButtonBox

    from ui.templates_import import ImportDialog

    folder = tmp_path / "examples"
    folder.mkdir()
    _example_file(folder / "лидеры.json")
    said = _mute_boxes(monkeypatch)

    window = ImportDialog(folder)
    try:
        window.mark(Qt.CheckState.Unchecked)
        window.buttons.button(QDialogButtonBox.StandardButton.Ok).click()
        assert window.result() != QDialog.DialogCode.Accepted, "окно закрылось согласием"
        assert said and "отмечено ни одного" in said[-1]
    finally:
        window.deleteLater()
        qapp.processEvents()


def test_export_writes_the_chosen_set_and_leaves_it_in_the_list(
    qapp, library, real_backend, monkeypatch, tmp_path
) -> None:
    """Стережёт: выгрузка **читает**. Набор остаётся в списке и в файле.

    Мутация: выгрузка, убирающая набор из библиотеки («перенести» вместо
    «скопировать»). Человек отправил бы набор на разбор и потерял его у себя.
    """
    from PySide6.QtWidgets import QFileDialog

    from ui.templates import read_for_import

    values = Settings(volume=4, average_period=21)
    library.write((Template(name="Вечерний", values=values, origin="MXU6, июль"),))
    target = tmp_path / "вынос.json"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        staticmethod(lambda *a, **k: (str(target), "")),
    )
    _mute_boxes(monkeypatch)

    dialog = TemplatesDialog(library, Settings())
    try:
        dialog.table.selectRow(1)
        dialog.export_selected()
    finally:
        dialog.deleteLater()
        qapp.processEvents()

    assert target.is_file(), "файл выгрузки не появился"
    got = read_for_import(target)
    assert [one.name for one in got.templates] == ["Вечерний"]
    assert got.templates[0].values == values
    assert got.templates[0].origin == "MXU6, июль"
    kept = [one.name for one in library.read().templates if not one.builtin]
    assert kept == ["Вечерний"], "выгрузка унесла набор из библиотеки"


def test_export_adds_the_extension_when_the_name_has_none(
    qapp, library, real_backend, monkeypatch, tmp_path
) -> None:
    """Стережёт: имя без расширения получает `.json`.

    Файл без расширения не найдётся в окне открытия, где стоит фильтр
    по наборам настроек, — и человек решит, что выгрузка не сработала.
    """
    from PySide6.QtWidgets import QFileDialog

    library.write((Template(name="Вечерний", values=Settings(volume=4)),))
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        staticmethod(lambda *a, **k: (str(tmp_path / "вынос"), "")),
    )
    _mute_boxes(monkeypatch)

    dialog = TemplatesDialog(library, Settings())
    try:
        dialog.table.selectRow(1)
        dialog.export_selected()
    finally:
        dialog.deleteLater()
        qapp.processEvents()

    assert (tmp_path / "вынос.json").is_file(), "расширение не дописано"


def test_closing_the_clash_question_keeps_my_set_untouched(
    qapp, library, real_backend, monkeypatch
) -> None:
    """Стережёт: закрытое крестиком окно вопроса — это «пропустить», не «заменить».

    Мутация: окно, закрытое без ответа, приравненное к согласию на замену.
    Тогда набор владельца счёта уходит от нажатия Esc, и он об этом
    не узнает.
    """
    from PySide6.QtWidgets import QMessageBox

    from ui.templates import NameClash

    monkeypatch.setattr(QMessageBox, "exec", lambda box: 0)
    dialog = TemplatesDialog(library, Settings())
    try:
        answer = dialog.resolve_clash(Template(name="Общее", values=Settings()))
    finally:
        dialog.deleteLater()
        qapp.processEvents()

    assert answer is NameClash.SKIP, (
        "закрытое без ответа окно решило судьбу набора само"
    )
