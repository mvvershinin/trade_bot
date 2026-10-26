"""Файл владельца счёта 04.10.2026 на пути человека: пять находок, пять сторожей.

Файл: алгоритм №2 «Реверс с постоянной позицией», шаг цены 0, ключей
`time_exit_*` и `minute_*` нет вовсе (записан прежней сборкой). Алгоритм
требует выхода по концу окна с предельной ценой. С 05.10.2026 шаг цены
программа берёт у биржи (решение владельца счёта): пока биржа его не
сообщила, движок идёт «по рынку» (`app/convert.py::_time_exit_in_force`), выбор в файле
и в окне остаётся предельной ценой, а главное окно держит плашку.

Что стережётся, словами:

1. **Файл не переписывается без человека.** Открытие окна настроек
   (`request_settings`) отдаёт эхо тем же сигналом, что и «Применить»;
   запись шла на каждое эхо и дописывала в файл пять ключей до «ОК»
   и при «Отмене». Карточка биржи (шаг, стоимость пункта) тоже меняла файл
   сама (находка аудитов 05.10.2026). Мутация записи: `_keep_settings`
   снова слушает эхо `settings_applied` либо порт шлёт `settings_to_file`
   при `by_person=False` — байты файла меняются.
2. **Имена полей человеку — подписями окна.** Мутация молчания: убрать
   строку «взяты по умолчанию» — проверка падает на её отсутствии, а не
   зеленеет на «латиницы нет».
3. **Одна правда при запуске.** Движок идёт «по рынку», выбор остаётся
   предельной ценой, и совета вписать шаг нет нигде — вписывать его некуда.
4. **Отказа «предельная цена без шага» нет нигде** (05.10.2026): до шага
   движок идёт «по рынку», и это видно плашкой, а не серой кнопкой.
5. **Ожидание шага видно.** Порт кладёт его в снимок состояния, главное
   окно показывает красную плашку без кнопки, окно настроек — строку под
   полем, «ОК» не гаснет. Карточка пришла — плашки нет.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib

import pytest
from PySide6.QtWidgets import QDialogButtonBox

from app.logs import LogSetup
from app.main import _keep_settings, _say_what_was_read, _wire_settings_and_log
from app.port import HistoryPort
from app.settings_store import SettingsStore
from engine import EngineSettings, TimeExitOrder
from market import MarketWorker
from market.journal import redact
from tests.exchange_card import exchange
from tests.test_app_algorithm_demands import ALWAYS
from tests.test_app_port_startup import SYMBOL, _Heard, database  # noqa: F401 — фикстура
from tests.test_app_settings_store import OWNER_FILE
from tests.test_ui_settings import make_dialog  # noqa: F401 — фикстура
from ui.models import (
    FIELD_CAPTIONS,
    STEP_WAIT,
    DecisionLevel,
    DecisionRow,
    ReversalMoment,
    Settings,
    TimeExitKind,
)
from ui.settings_dialog import STEP_UNKNOWN

#: Чем кончается строка ожидания шага при предельной цене.
MARKET_WAIT = "пока выход по концу окна идёт по рынку"

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
        port.apply_settings(port.values.replace(average_period=21))
        await port.wait()

    _started(loop, database, store, work)
    assert seen["opened"] == before, (
        "открытие окна настроек переписало файл владельца счёта без «ОК»"
    )
    after = json.loads(store.path.read_text(encoding="utf-8"))["settings"]
    assert after["average_period"] == 21 and after["time_exit_order"] == "limit", (
        f"«Применить» человека в файл не записано: {after}"
    )


@pytest.mark.parametrize(("step", "rubles"), [(25.0, 1.0), (25.0, 7.5)], ids=["step", "point"])
def test_the_exchange_card_does_not_rewrite_the_owner_file(
    loop, database, tmp_path, step, rubles  # noqa: F811 — фикстура
) -> None:
    """Стережёт: карточка биржи пришла, окно не открывалось — байты файла те же.

    Два случая: шаг цены (в файле 0, биржа — 25) и стоимость пункта
    (в файле 1, биржа — 7,5). Канарейка — ответ **в памяти**: без неё
    равенство байтов доказывало бы только то, что карточка не пришла.
    """
    store = _owner_file(tmp_path / "userdata")
    before = store.path.read_bytes()
    seen: dict[str, object] = {}

    async def work(port: HistoryPort) -> None:
        port.attach_exchange(exchange(step, rubles))
        port.refresh("карточка биржи")
        await port.wait()
        point = port._point.task  # noqa: SLF001 — задача запроса наружу не отдаётся
        assert point is not None, "порт не спросил биржу — проверка вакуумна"
        await point
        await port.wait()
        seen["values"] = port.values

    _started(loop, database, store, work)
    values = seen["values"]
    assert isinstance(values, Settings), values
    assert (values.price_step, values.ruble_per_point) == (step, rubles), (
        "ответ биржи не дошёл до настроек в памяти — проверка вакуумна"
    )
    assert store.path.read_bytes() == before, (
        "ответ биржи переписал файл владельца счёта без «ОК»: "
        f"{json.loads(store.path.read_text(encoding='utf-8'))['settings']}"
    )


def test_an_old_file_with_the_removed_algorithm_is_said_and_kept_at_start(
    loop, database, tmp_path, monkeypatch  # noqa: F811 — фикстура
) -> None:
    """Стережёт: файл с `ema_reverse` на запуске — строка в журнал, байты те же.

    Проводка настоящая — `app/main.py::_wire_settings_and_log`, а не её
    пересказ в `_started`: запись файла, добавленная при сборке окна,
    иначе ловилась бы только тем, что у подставного хранилища нет `save`.
    Канарейка — «Применить» человека: без неё равенство байтов доказывало
    бы только то, что запись не подключена вовсе.

    Мутации, обязанные ронять проверку: `store.save(loaded.values)` в
    `_wire_settings_and_log`; подмена убранного алгоритма молча
    (`app/settings_store.py::_known_algorithm` без строки).
    """
    monkeypatch.setattr("app.logs.setup_logging", lambda *_a, **_k: LogSetup(
        path=None, directory=None, asked=None, permissions_enforced=False, trouble="",
    ))
    folder = tmp_path / "userdata"
    folder.mkdir()
    store = SettingsStore(folder)
    store.path.write_text(OWNER_FILE, encoding="utf-8")
    before = store.path.read_bytes()
    loaded = store.load()
    seen: dict[str, object] = {}

    async def go() -> None:
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(worker, values=loaded.values, days=0, sanitize=redact)
        heard = _Heard(port)
        try:
            _wire_settings_and_log(
                port, store, loaded, loaded.values, None, level=DecisionLevel.WARNING,
            )
            port.refresh("запуск программы")
            await port.wait()
            seen["opened"] = store.path.read_bytes()
            seen["said"] = " ".join(row.reason for row in heard.journals[-1])
            port.apply_settings(port.values.replace(average_period=21))
            await port.wait()
        finally:
            await port.aclose()
            await worker.close()

    loop.run_until_complete(go())
    assert loaded.values.strategy_id == ALWAYS, loaded.values.strategy_id
    said = seen["said"]
    assert "Алгоритм «Реверс по скользящей средней» убран" in str(said), (
        f"убранный алгоритм подменён молча — в журнале запуска о нём ни слова: {said}"
    )
    assert seen["opened"] == before, "запуск переписал файл владельца счёта без «ОК»"
    after = json.loads(store.path.read_text(encoding="utf-8"))["settings"]
    assert (after["average_period"], after["strategy_id"]) == (21, ALWAYS), (
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


def test_the_start_runs_at_market_and_asks_nothing_impossible(loop, database, tmp_path) -> None:  # noqa: F811 — фикстура
    """Стережёт: без шага от биржи движок идёт «по рынку», выбор не тронут, совета вписать шаг нет.

    ⚠️ Мутация: вернуть подмену формы в `values` при запуске — выбор
    «с предельной ценой» пропадает из окна и файла, проверка падает.
    """

    async def nothing(_port: HistoryPort) -> None:
        return None

    heard, port = _started(loop, database, _owner_file(tmp_path / "userdata"), nothing)
    assert port.values.time_exit_order is TimeExitKind.LIMIT, "выбор человека подменён"
    engine = port._engine_settings  # noqa: SLF001 — что получил движок
    assert engine.time_exit_order is TimeExitOrder.MARKET, engine
    told = " ".join(row.reason for row in heard.journals[-1]).lower()
    assert "впишите шаг" not in told, f"совет сделать невозможное: «{told}»"
    assert heard.states[-1].step_wait == f"{STEP_WAIT} — {MARKET_WAIT}.", heard.states[-1]


def test_the_dialog_says_what_is_in_force_next_to_the_locked_field(make_dialog) -> None:  # noqa: F811 — фикстура
    """Стережёт: под полем шага — что действует сейчас; «ОК» не гаснет, поле не правится."""
    dialog = make_dialog(Settings(
        strategy_id=ALWAYS, reversal_moment=ReversalMoment.SAME_BAR,
        time_exit_order=TimeExitKind.LIMIT, price_step=0.0,
    ))
    assert dialog.step_note.text() == f"⚠️ {STEP_WAIT} — {MARKET_WAIT}.", dialog.step_note.text()
    assert dialog.price_step.isReadOnly(), "шаг цены снова вписывается руками"
    assert dialog.price_step.text() == STEP_UNKNOWN, dialog.price_step.text()
    ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert ok.isEnabled(), f"«ОК» гаснет на том, что человеку не исправить: {ok.toolTip()}"
    dialog.price_step.setValue(25.0)  # так его ставит эхо порта после ответа биржи
    assert dialog.step_note.text() == "" and dialog.step_note.isHidden()


# ------------------------------------------------------ 5. отказ видно


def test_the_step_wait_reaches_the_main_window_and_leaves_with_the_card(
    loop, database, tmp_path, monkeypatch  # noqa: F811 — фикстура
) -> None:
    """Стережёт всю дорогу: порт → снимок → красная плашка → карточка → плашки нет.

    ⚠️ Мутация молчания: `step_wait` возвращает пусто, или окно не зовёт
    `show_wait` — плашки нет, проверка падает на первом же утверждении.
    Канарейка с другой стороны: карточка с шагом пришла — плашка ушла.
    """
    from ui.main_window import MainWindow

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
            shown["bar"] = not window.step_bar.isHidden()
            shown["text"] = window.step_bar.text()
            shown["trouble"] = not window.settings_bar.isHidden()
            port.attach_exchange(exchange(25.0))
            port.refresh("карточка пришла")
            await port.wait()
            shown["after"] = not window.step_bar.isHidden()
            shown["engine"] = port._engine_settings  # noqa: SLF001 — что получил движок
        finally:
            window.close()
            window.deleteLater()

    _started(loop, database, store, work)
    assert shown["bar"], "в главном окне нет плашки «биржа не сообщила шаг»"
    assert shown["text"] == f"{STEP_WAIT} — {MARKET_WAIT}.", shown["text"]
    assert not shown["trouble"], "плашка с кнопкой «Открыть настройки» — чинить там нечего"
    assert not shown["after"], "шаг от биржи пришёл, а плашка осталась"
    engine = shown["engine"]
    assert isinstance(engine, EngineSettings), engine
    assert engine.time_exit_order is TimeExitOrder.LIMIT and engine.price_step == 25.0, engine


# ------------------------------------------- 6. конец окна закрывает всегда


def _window_exits(rows: tuple[DecisionRow, ...]) -> tuple[int, int]:
    """Сколько выходов по концу окна и сколько «позиция остаётся вне окна»."""
    events = [row.event for row in rows]
    return (
        sum(event.endswith("по концу окна") for event in events),
        events.count("Позиция остаётся вне окна"),
    )


def test_an_old_unticked_window_close_still_closes_before_and_after_the_card(
    loop, request, tmp_path
) -> None:
    """Стережёт: алгоритм закрывает позицию по концу окна всегда — и без шага, и с ним.

    Старый файл: галочка «Закрывать в конце окна» снята. Алгоритм требует
    выхода с предельной ценой, а этот выбор сам значит «закрывать всегда».
    Пока биржа не сообщила шаг, движок идёт «по рынку» — и закрытие по концу
    окна обязано остаться: плашка и журнал чтения говорят человеку именно
    это. Пришла карточка — то же закрытие, но с предельной ценой.

    ⚠️ Мутация: `convert.engine_settings` берёт галочку как есть (без
    «или предельная цена») либо порт отдаёт ему уже подменённое «по рынку» —
    без шага в журнале «Позиция остаётся вне окна», проверка падает.
    """
    candles = request.getfixturevalue("database")
    store = SettingsStore(tmp_path / "userdata")
    assert not store.save(Settings(
        instrument=SYMBOL, strategy_id=ALWAYS, reversal_moment=ReversalMoment.SAME_BAR,
        time_exit_order=TimeExitKind.LIMIT, close_on_time_end=False, price_step=0.0,
    ))
    seen: dict[str, tuple[EngineSettings, tuple[DecisionRow, ...]]] = {}

    async def work(port: HistoryPort) -> None:
        heard = _Heard(port)
        port.refresh("без карточки")
        await port.wait()
        seen["before"] = (port._engine_settings, heard.journals[-1])  # noqa: SLF001 — что получил движок
        port.attach_exchange(exchange(25.0))
        port.refresh("карточка пришла")
        await port.wait()
        seen["after"] = (port._engine_settings, heard.journals[-1])  # noqa: SLF001 — что получил движок

    _started(loop, candles, store, work)
    for phase, order in (("before", TimeExitOrder.MARKET), ("after", TimeExitOrder.LIMIT)):
        engine, rows = seen[phase]
        assert engine.time_exit_order is order, (phase, engine)
        closed, kept = _window_exits(rows)
        assert closed > 0, f"{phase}: по концу окна не закрылось ни разу — проверка вакуумна"
        assert kept == 0, f"{phase}: позиция осталась вне окна {kept} раз, а обещано «закрывается»"
