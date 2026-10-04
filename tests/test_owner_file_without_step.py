"""Файл владельца счёта 04.10.2026 на пути человека: пять находок, пять сторожей.

Файл: алгоритм №2 «Реверс с постоянной позицией», шаг цены 0, ключей
`time_exit_*` и `minute_*` нет вовсе (записан прежней сборкой). Алгоритм
требует выхода по концу окна с предельной ценой, без шага её не посчитать —
порт ставит «по рынку» (`app/port.py::_startup_settings`).

Что стережётся, словами:

1. **Файл не переписывается без человека.** Открытие окна настроек
   (`request_settings`) отдаёт эхо тем же сигналом, что и «Применить»;
   запись шла на каждое эхо и дописывала в файл пять ключей до «ОК»
   и при «Отмене». Мутация записи: убрать сравнение в
   `app/main.py::_keep_settings.remember` — байты файла меняются.
2. **Имена полей человеку — подписями окна.** Мутация молчания: убрать
   строку «взяты по умолчанию» — проверка падает на её отсутствии, а не
   зеленеет на «латиницы нет».
3. **Одна правда при запуске.** Журнал говорит «сейчас идёт „по рынку“»
   ровно одной строкой; строки чтения файла «взято „С предельной ценой“…
   НЕ С ТОЙ» рядом нет. Мутация: снять фильтр в `_say_what_was_read`.
4. **Совет только исполнимый.** У алгоритма №2 совета «выберите „по рынку“»
   нет ни в отказе, ни в строке запуска; у №1 — есть.
5. **Отказ видно.** Порт кладёт его в снимок состояния, главное окно
   показывает плашку, окно настроек открывается на вкладке шага цены
   с фокусом на поле, у серой «ОК» — подсказка почему.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib

from PySide6.QtWidgets import QDialogButtonBox

from app import convert
from app.main import _keep_settings, _say_what_was_read
from app.port import HistoryPort
from app.settings_store import SettingsStore
from market import MarketWorker
from market.journal import redact
from strategies import registry
from tests.test_app_algorithm_demands import ALWAYS
from tests.test_app_port_startup import SYMBOL, _Heard, database  # noqa: F401 — фикстура
from tests.test_ui_settings import make_dialog  # noqa: F401 — фикстура
from ui.models import (
    FIELD_CAPTIONS,
    DecisionLevel,
    ReversalMoment,
    Settings,
    TimeExitKind,
)

#: Ключи, которых в файле владельца не было.
ABSENT = (
    "minute_order", "minute_bar_limit", "time_exit_order",
    "time_exit_limit_steps", "time_exit_wait_bars",
)


def _owner_file(folder: pathlib.Path) -> SettingsStore:
    """Файл той же формы, что у владельца счёта: №2, шаг 0, пяти ключей нет."""
    store = SettingsStore(folder)
    assert not store.save(Settings(
        instrument=SYMBOL, strategy_id=ALWAYS,
        reversal_moment=ReversalMoment.SAME_BAR, price_step=0.0,
    ))
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    for key in ABSENT:
        del payload["settings"][key]
    store.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return store


def _started(loop, database: pathlib.Path, store: SettingsStore, work):  # noqa: F811
    """Собрать порт так, как его собирает `app/main.py`, и выполнить `work`."""
    loaded = store.load()

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(worker, values=loaded.values, days=0, sanitize=redact)
        heard = _Heard(port)
        _say_what_was_read(port, loaded, store, level=DecisionLevel.WARNING)
        _keep_settings(port, store, loaded.values, level=DecisionLevel.WARNING)
        try:
            port.refresh("запуск программы")
            await port.wait()
            await work(port)
        finally:
            await port.aclose()
            await worker.close()
        return heard, port

    return loop.run_until_complete(go())


# ------------------------------------------------------ 1. файл без человека


def test_opening_the_settings_does_not_rewrite_the_owner_file(loop, database, tmp_path) -> None:  # noqa: F811 — фикстура
    """Стережёт: открытие окна настроек файл не трогает; «Применить» — пишет.

    Вторая половина — канарейка: без неё равенство байтов доказывало бы
    только то, что запись не подключена вовсе.
    """
    store = _owner_file(tmp_path / "userdata")
    before = store.path.read_bytes()
    seen: dict[str, bytes] = {}

    async def work(port: HistoryPort) -> None:
        port.request_settings()
        port.request_settings()
        seen["opened"] = store.path.read_bytes()
        port.apply_settings(port.values.replace(
            time_exit_order=TimeExitKind.LIMIT, price_step=25.0,
        ))
        await port.wait()

    _started(loop, database, store, work)
    assert seen["opened"] == before, (
        "открытие окна настроек переписало файл владельца счёта без «ОК»"
    )
    after = json.loads(store.path.read_text(encoding="utf-8"))["settings"]
    assert after["price_step"] == 25.0 and after["time_exit_order"] == "limit", (
        f"«Применить» человека в файл не записано: {after}"
    )


# ------------------------------------------------------ 2. подписи, не имена


def test_missing_settings_are_named_by_their_window_captions(tmp_path) -> None:
    """Стережёт: строка «взяты по умолчанию» есть и называет поля подписями окна."""
    loaded = _owner_file(tmp_path).load()
    said = [note for note in loaded.notes if "взяты по умолчанию" in note]
    assert said, f"о пропавших в файле настройках не сказано ничего: {loaded.notes}"
    for key in ABSENT:
        assert f"«{FIELD_CAPTIONS[key]}»" in said[0], (key, said[0])
        assert key not in said[0], f"имя поля латиницей человеку: {key} в «{said[0]}»"


def test_every_setting_has_the_caption_the_window_shows(make_dialog) -> None:  # noqa: F811 — фикстура
    """Стережёт: подпись есть у каждого поля и совпадает с подписью строки формы."""
    from ui.settings_dialog import SettingsDialog

    names = {item.name for item in dataclasses.fields(Settings)}
    assert set(FIELD_CAPTIONS) == names, names ^ set(FIELD_CAPTIONS)
    dialog = make_dialog()
    for name, caption in FIELD_CAPTIONS.items():
        widget = dialog._widget_of(name)  # noqa: SLF001 — сверка таблицы с окном
        shown = SettingsDialog._caption_of(widget) if widget is not None else ""  # noqa: SLF001
        if shown:
            assert caption == shown, f"{name}: в таблице «{caption}», в окне «{shown}»"


# ------------------------------------------------------ 3–4. одна правда, исполнимый совет


def test_the_start_says_one_truth_with_an_action_that_can_be_done(loop, database, tmp_path) -> None:  # noqa: F811 — фикстура
    """Стережёт: про выход по концу окна при запуске — одна строка, с действием.

    И то, что она утверждает, правда: действует «по рынку».
    """

    async def nothing(_port: HistoryPort) -> None:
        return None

    heard, port = _started(loop, database, _owner_file(tmp_path / "userdata"), nothing)
    assert port.values.time_exit_order is TimeExitKind.MARKET
    rows = [
        row for row in heard.journals[-1]
        if "«по рынку»" in row.reason or "«С предельной ценой»" in row.reason
    ]
    assert len(rows) == 1, "про выход по концу окна — не одна правда:\n" + "\n".join(
        f"{row.event}: {row.reason}" for row in rows
    )
    reason = rows[0].reason
    assert reason.startswith("Сейчас прогон идёт с выходом по концу окна «по рынку»"), reason
    assert "впишите шаг цены в «Настройки» → «Инструмент и данные»" in reason, reason
    assert "«по рынку» на вкладке" not in reason, (
        f"совет, которого алгоритм №2 не позволяет: «{reason}»"
    )


def test_the_refusal_advises_the_market_order_only_where_it_is_allowed() -> None:
    """Стережёт: «выберите „по рынку“» — у №1 есть, у №2 нет."""
    first = Settings(time_exit_order=TimeExitKind.LIMIT, price_step=0.0)
    second = first.replace(strategy_id=ALWAYS, reversal_moment=ReversalMoment.SAME_BAR)
    assert registry.find(first.strategy_id).demands == ()
    assert "«по рынку»" in convert.limit_without_step(first)
    assert "«по рынку»" not in convert.limit_without_step(second)
    assert "Впишите шаг цены" in convert.limit_without_step(second)


def test_the_dialog_says_what_is_in_force_next_to_the_locked_field(make_dialog) -> None:  # noqa: F811 — фикстура
    """Стережёт: под закрытым полем — что действует сейчас, и у серой «ОК» — почему."""
    dialog = make_dialog(Settings(
        strategy_id=ALWAYS, reversal_moment=ReversalMoment.SAME_BAR,
        time_exit_order=TimeExitKind.MARKET, price_step=0.0,
    ))
    dialog.time_exit_order.setCurrentIndex(
        dialog.time_exit_order.findData(TimeExitKind.LIMIT)
    )
    assert "Сейчас прогон идёт с выходом «по рынку»" in dialog.step_note.text()
    ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert not ok.isEnabled()
    assert "шаг цены" in ok.toolTip(), f"серая «ОК» без объяснения: «{ok.toolTip()}»"
    dialog.price_step.setValue(25.0)
    assert ok.isEnabled() and ok.toolTip() == ""


# ------------------------------------------------------ 5. отказ видно


def test_the_refusal_reaches_the_main_window_and_opens_on_the_step(
    loop, database, tmp_path, monkeypatch  # noqa: F811 — фикстура
) -> None:
    """Стережёт всю дорогу: порт → снимок → плашка → окно настроек на поле шага.

    Настоящее всё, кроме `exec()` окна настроек (модальный цикл ждал бы
    человека вечно). После «Применить» с шагом плашка уходит.
    """
    from ui.main_window import MainWindow
    from ui.settings_dialog import SettingsDialog

    seen: dict[str, object] = {}

    def instead_of_exec(dialog: SettingsDialog) -> int:
        seen["tab"] = dialog.tabs.tabText(dialog.tabs.currentIndex())
        seen["focus"] = dialog.focusWidget() is dialog.price_step
        return 0

    monkeypatch.setattr(SettingsDialog, "exec", instead_of_exec)
    # Закрытие окна при открытой позиции спрашивает модальным вопросом
    # (`_confirm_unattended`) — без подмены тест, потерявший выход, не падал,
    # а висел вечно (мутация знака предела 04.10.2026).
    monkeypatch.setattr(MainWindow, "_confirm_unattended", lambda *_args: True)
    store = _owner_file(tmp_path / "userdata")
    shown: dict[str, object] = {}

    async def work(port: HistoryPort) -> None:
        window = MainWindow(port=port, settings=port.values, sanitize=redact)
        window._timer.stop()  # noqa: SLF001 — часы в тесте только мешают
        try:
            port.refresh("снимок для окна")
            await port.wait()
            shown["bar"] = not window.settings_bar.isHidden()
            shown["text"] = window.settings_bar.label.text()
            window.settings_bar.button.click()
            port.apply_settings(port.values.replace(
                time_exit_order=TimeExitKind.LIMIT, price_step=25.0,
            ))
            await port.wait()
            shown["after"] = not window.settings_bar.isHidden()
        finally:
            window.close()
            window.deleteLater()

    heard, _port = _started(loop, database, store, work)
    first = heard.states[0]
    assert first.settings_trouble and first.settings_field == "price_step", first
    assert shown["bar"], "в главном окне нет плашки об отказе"
    assert "впишите шаг цены" in str(shown["text"]), shown["text"]
    assert seen.get("tab") == "Инструмент и данные", seen
    assert seen.get("focus"), "фокус не на поле шага цены"
    assert not shown["after"], "шаг вписан и применён, а плашка осталась"
