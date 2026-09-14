"""Настройки окна доезжают до прогона и до файла — а не останавливаются на полпути.

Три поля, заведённые 05.09.2026, объединены здесь одним свойством: каждое
выглядит работающим, пока не проверишь конец пути.

* **проскальзывание** меняет деньги прогона. Замер того же дня: ноль шагов
  дают +9 908 ₽, один шаг +4 394 ₽, два шага −153 ₽. Поле, не доехавшее
  до модели исполнения, оставляет владельца счёта смотреть на прибыль,
  которая систематически лучше реальной;
* **глубина показа** доезжает до выборки свечей, а не остаётся числом в окне;
* **закрытие по концу окна** доезжает до движка, а не берётся у прежних
  настроек — то есть его выключение действительно выключает выход по времени.

И четвёртое: **применённые настройки попадают в файл**, иначе подобранный
ночью набор живёт до закрытия окна.
"""

from __future__ import annotations

import math
import pathlib
from datetime import datetime, timedelta

import pytest

from app import convert
from app.main import _keep_settings, _stored_settings
from app.port import HistoryPort
from app.settings_store import SettingsStore
from market import MSK, Candle, CandleStore, MarketWorker, Source, Timeframe, redact
from ui.models import DecisionRow, Mode, Settings

DAY = datetime(2026, 6, 19, 7, 0, tzinfo=MSK)


def _minutes(count: int, start: datetime) -> list[Candle]:
    """Минутки с колебанием вокруг ровной цены: без движения сделок нет вовсе."""
    return [
        Candle(
            time=start + timedelta(minutes=index),
            open=100_000.0 + 300.0 * math.sin(index / 9.0),
            high=100_000.0 + 300.0 * math.sin(index / 9.0) + 50.0,
            low=100_000.0 + 300.0 * math.sin(index / 9.0) - 50.0,
            close=100_000.0 + 300.0 * math.sin((index + 1) / 9.0),
            volume=10.0,
            timeframe=Timeframe(1),
            filled_minutes=1,
        )
        for index in range(count)
    ]


@pytest.fixture()
def database(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "candles.sqlite3"
    with CandleStore(path) as store:
        store.put_minutes("MXU6", _minutes(300, DAY - timedelta(days=2)), Source.ISS)
        store.put_minutes("MXU6", _minutes(300, DAY), Source.ISS)
    return path


def _replay(loop, database: pathlib.Path, values: Settings, **kwargs):
    """Прогнать историю с этими настройками и вернуть последний итог и сделки."""
    seen: list[tuple] = []

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(worker, values=values, sanitize=redact, **kwargs)
        port.trades_replaced.connect(lambda rows, summary: seen.append((rows, summary)))
        try:
            port.refresh("тест")
            await port.wait()
        finally:
            await port.aclose()
            await worker.close()

    loop.run_until_complete(go())
    assert seen, "прогон не отдал в окно ни одного итога — проверка вакуумна"
    return seen[-1]


# --------------------------------------------------------- проскальзывание

def test_slippage_reaches_the_run_and_changes_the_money(loop, database) -> None:
    """Поправка на проскальзывание доезжает до прогона и меняет деньги.

    Список сделок при этом обязан остаться прежним: издержки в решения движка
    не входят (PROTOTYPE.md §6). Разница только в рублях — и она есть.
    """
    clean = Settings(depth_days=0)
    rough = clean.replace(price_step=25.0, slippage_steps=2.0)

    clean_rows, clean_total = _replay(loop, database, clean, days=0)
    rough_rows, rough_total = _replay(loop, database, rough, days=0)

    assert clean_total is not None and rough_total is not None
    assert clean_total.trades > 0, "сделок нет — сравнивать нечего"
    assert rough_total.trades == clean_total.trades, (
        "проскальзывание изменило список сделок: издержки попали в решения движка"
    )
    assert rough_total.net_profit_rub != clean_total.net_profit_rub, (
        "проскальзывание не доехало до прогона: деньги те же, что без него"
    )
    assert rough_total.net_profit_rub < clean_total.net_profit_rub, (
        "проскальзывание сдвинуло цену в пользу позиции — знак перепутан"
    )
    assert len(rough_rows) == len(clean_rows)


def test_slippage_without_a_price_step_is_refused_aloud() -> None:
    """Проскальзывание без шага цены — отказ с фразой, а не тихий ноль.

    Тихий ноль означал бы настройку, которая выглядит заданной и не делает
    ничего: владелец счёта поставил поправку и продолжил смотреть на прибыль
    без неё.
    """
    with pytest.raises(convert.SettingsRefused, match="шаг цены"):
        convert.run_costs(Settings(slippage_steps=1.0, price_step=0.0))


def test_the_default_costs_repeat_the_prototype() -> None:
    """Умолчание — ноль шагов: только на нём сходится сверка с прототипом."""
    costs = convert.run_costs(Settings())
    assert costs.slippage_steps == 0.0
    assert costs.slippage == 0.0
    assert costs.commission_per_side == Settings().commission_per_side_rub


def test_the_summary_says_which_slippage_it_was_counted_with(loop, database) -> None:
    """Оговорка под итогом называет поправку — и меняется вместе с ней."""
    clean = _replay(loop, database, Settings(depth_days=0), days=0)[1]
    rough = _replay(
        loop, database,
        Settings(depth_days=0, price_step=25.0, slippage_steps=2.0),
        days=0,
    )[1]
    assert "из стакана" in clean.headline, (
        "итог без поправки показан без единого слова о том, что её нет"
    )
    assert clean.headline != rough.headline, (
        "оговорка не заметила поправки: она разошлась с числом, рядом "
        "с которым стоит"
    )


# ---------------------------------------------------------- глубина показа

def test_the_default_depth_is_ninety_days() -> None:
    """Умолчание глубины показа — 90 дней, а не 30 (`D-026`).

    Тридцать дней от 4 сентября — это ровно 6 августа, и владелец счёта решил,
    что истории нет, хотя контракт торгуется с 17 июня.
    """
    assert Settings().depth_days == 90


def test_depth_is_not_an_engine_setting() -> None:
    """Глубина показа в настройки движка не попадает.

    Там каждое поле стережётся сверкой с прототипом, и глубина списка сделок
    не меняет — она меняет, сколько их видно.
    """
    produced = convert.engine_settings(Settings(depth_days=7), Mode.REVERSE)
    assert not hasattr(produced, "depth_days")
    shallow = convert.engine_settings(Settings(depth_days=1), Mode.REVERSE)
    assert produced == shallow, "глубина показа просочилась в настройки движка"


def test_depth_narrows_what_is_shown_and_run(loop, database) -> None:
    """Глубина показа режет выборку свечей, а не остаётся числом в окне.

    В базе два дня, разнесённых на трое суток. Глубина в один день обязана
    оставить только последний.
    """
    whole = _replay(loop, database, Settings(depth_days=0), days=0)[1]
    last_day = _replay(loop, database, Settings(depth_days=1), days=1)[1]
    assert whole.trades > last_day.trades > 0, (
        "глубина не сузила прогон: показывается столько же сделок, сколько "
        "и на всей истории"
    )


def test_the_key_beats_the_setting_and_the_setting_beats_the_default(
    tmp_path: pathlib.Path,
) -> None:
    """`--days` перебивает настройку, а без ключа берётся настройка."""
    import argparse

    store = SettingsStore(tmp_path)
    store.save(Settings(depth_days=45))

    named = argparse.Namespace(symbol=None, days=7)
    _, _, kept, depth = _stored_settings(tmp_path, named)
    assert depth == 7, "ключ командной строки не перебил настройку"
    assert kept.depth_days == 45, (
        "ключ переписал настройку: она названа для одного запуска, а не навсегда"
    )

    silent = argparse.Namespace(symbol=None, days=None)
    _, _, values, depth = _stored_settings(tmp_path, silent)
    assert depth == 45, "без ключа глубина взята не из настроек"
    assert values.depth_days == 45


def test_the_warmup_is_not_cut_by_the_depth() -> None:
    """Глубина показа прогрева средней не укорачивает и не заменяет.

    Прогрев считается от **периода средней**, и число одно на программу:
    `market.warmup_bars`. Если бы окно считало его само, два числа разошлись бы
    молча — одно в настройках, другое при загрузке истории.
    """
    from market import warmup_bars

    need = warmup_bars(Settings().average_period)
    assert need > Settings().average_period, (
        "прогрев не может быть короче периода средней"
    )
    assert warmup_bars(30) > need, "прогрев не растёт вместе с периодом средней"
    # Глубина в этот расчёт не входит вовсе — брать её здесь неоткуда.
    assert "depth" not in warmup_bars.__code__.co_varnames


# ------------------------------------------------- закрытие по концу окна

def test_closing_at_the_window_end_comes_from_the_window() -> None:
    """Галочка «закрывать в конце окна» доезжает до движка из окна.

    Прежде значение бралось у предыдущих настроек: окно про него не знало,
    и любое «Применить» возвращало умолчание — ровно тот же дефект, что
    с фильтром против пилы (`D-029`).
    """
    previous = convert.engine_settings(Settings(), Mode.REVERSE)
    assert previous.close_on_time_end is True, "умолчание изменилось без замера"

    off = convert.engine_settings(
        Settings(close_on_time_end=False), Mode.REVERSE, previous
    )
    assert off.close_on_time_end is False, (
        "снятая галочка не доехала до движка: выход по времени остался включён"
    )
    back = convert.engine_settings(Settings(), Mode.REVERSE, off)
    assert back.close_on_time_end is True, (
        "поставленная галочка не доехала: значение тащится из прежних настроек"
    )


def test_the_saw_filter_is_not_reset_by_applying_settings() -> None:
    """Включённый фильтр против пилы переживает применение настроек (`D-029`).

    ⚠️ `filter_enabled=True` здесь обязателен и не является поблажкой тесту:
    05.09.2026 у фильтра появился выключатель, и при снятой галочке
    до модуля доходят его умолчания. Без галочки этот тест проверял бы
    ровно обратное тому, ради чего написан.
    """
    from strategies import EmaReverseSettings

    values = Settings(filter_enabled=True, threshold_percent=0.4, confirm_bars=3)
    module = convert.strategy_settings(values)
    assert isinstance(module, EmaReverseSettings)
    assert module.threshold_percent == pytest.approx(0.4), (
        "порог фильтра сброшен в «выключено» при переводе настроек"
    )
    assert module.confirm_bars == 3, "подтверждение сброшено в «выключено»"


def test_a_new_field_of_the_strategy_cannot_be_forgotten() -> None:
    """Поле торгового алгоритма, не названное в таблице, — отказ вслух.

    Проверка на механизм, а не на сегодняшний список полей: пустой список
    расхождений и есть утверждение «таблица полна».

    ⚠️ Таблица переехала из `app/convert.py` в запись реестра
    (`StrategyEntry.fields`) — сборка больше не знает, какие поля бывают
    у алгоритма. Проверка от этого стала сильнее, а не слабее: она идёт
    **по всем** записям реестра, то есть второй алгоритм получает её даром.
    """
    from strategies import registry

    for entry in registry.entries():
        assert entry.settings_gap() == (), (
            f"у алгоритма «{entry.title}» есть настройки, которых таблица "
            f"полей не называет: {entry.settings_gap()}. Они молча получили "
            "бы умолчания вместо выбранного в окне"
        )
        assert entry.stray_fields() == (), (
            f"таблица полей алгоритма «{entry.title}» называет настройки, "
            f"которых у него нет: {entry.stray_fields()}"
        )


def test_a_new_field_of_the_engine_cannot_be_forgotten() -> None:
    """Поле движка обязано попасть ровно в одну из двух таблиц перевода.

    ⚠️ Таблиц было три: третья перечисляла поля, которые в окне **есть**,
    а до движка намеренно не доходят, — три предохранителя по деньгам.
    05.09.2026 они проведены (`D-030`) и переехали в «из окна»: доходят,
    но только вместе со своей галочкой, снятой умолчанием.
    """
    import dataclasses

    from engine import EngineSettings

    known = {field.name for field in dataclasses.fields(EngineSettings)}
    named = (
        set(convert._ENGINE_FROM_WINDOW)  # noqa: SLF001 — проверяются сами таблицы
        | convert._ENGINE_FROM_BASE  # noqa: SLF001
    )
    assert known == named, (
        "таблицы перевода настроек движка разошлись с самим движком: поле, "
        "не названное ни в одной, молча вернётся к умолчанию при «Применить»"
    )


# ------------------------------------------------------- запись настроек

def test_applied_settings_reach_the_file(loop, database, tmp_path) -> None:
    """Принятые настройки попадают в файл — иначе набор живёт до закрытия окна."""
    store = SettingsStore(tmp_path / "userdata")

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(worker, values=Settings(), days=0, sanitize=redact)
        _keep_settings(port, store, level=convert.DecisionLevel.WARNING)
        try:
            port.apply_settings(Settings(instrument="MXU6", volume=3, depth_days=12))
            await port.wait()
        finally:
            await port.aclose()
            await worker.close()

    loop.run_until_complete(go())
    read = SettingsStore(tmp_path / "userdata").load()
    assert read.values.volume == 3, "изменённый объём не пережил бы перезапуск"
    assert read.values.depth_days == 12


def test_a_depth_change_reaches_the_port_without_a_restart(loop, database, tmp_path) -> None:
    """Изменённая глубина действует сразу, а не со следующего запуска."""
    store = SettingsStore(tmp_path / "userdata")
    seen: list[tuple] = []

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(worker, values=Settings(depth_days=0), days=0, sanitize=redact)
        port.trades_replaced.connect(lambda rows, total: seen.append((rows, total)))
        _keep_settings(port, store, level=convert.DecisionLevel.WARNING)
        try:
            port.refresh("тест")
            await port.wait()
            port.apply_settings(Settings(depth_days=1))
            await port.wait()
        finally:
            await port.aclose()
            await worker.close()

    loop.run_until_complete(go())
    assert len(seen) >= 2
    assert seen[-1][1].trades < seen[0][1].trades, (
        "новая глубина не дошла до прогона: показано столько же сделок"
    )


# ------------------------------------------------- каталог технического лога


def test_the_log_directory_from_the_settings_is_used(loop, database, tmp_path) -> None:
    """Папка лога, названная в окне, доезжает до самого лога (`D-021`).

    Сборка зовёт `setup_logging` дважды: без настроек — до всех веток, чтобы
    консоль молчала уже при `--fetch`, — и ещё раз с папкой из настроек.
    Вторая половина проверяется здесь: без неё поле в окне ничего не делало бы.
    """
    import logging

    from app.logs import LOG_FILE_NAME
    from app.main import _wire_settings_and_log

    chosen = tmp_path / "мой-каталог-логов"
    store = SettingsStore(tmp_path / "userdata")
    values = Settings(log_directory=str(chosen))

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(worker, values=values, days=0, sanitize=redact)
        try:
            _wire_settings_and_log(
                port, store, store.load(), values,
                level=convert.DecisionLevel.WARNING,
            )
            logging.getLogger("terminal").warning("проверочная строка")
        finally:
            await port.aclose()
            await worker.close()

    loop.run_until_complete(go())
    written = chosen / LOG_FILE_NAME
    assert written.exists(), f"лог не поехал в папку из настроек: в {chosen} пусто"
    assert "проверочная строка" in written.read_text(encoding="utf-8")


def test_the_journal_says_where_the_log_goes(loop, database, tmp_path) -> None:
    """В журнал решений пишется, куда именно уехал технический лог.

    Молчание здесь стоит дорого в один день — в тот, когда лог понадобится.
    """
    from app.main import _wire_settings_and_log

    chosen = tmp_path / "логи"
    store = SettingsStore(tmp_path / "userdata")
    values = Settings(log_directory=str(chosen))
    said: list[DecisionRow] = []

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(worker, values=values, days=0, sanitize=redact)
        port.decision_appended.connect(said.append)
        try:
            _wire_settings_and_log(
                port, store, store.load(), values,
                level=convert.DecisionLevel.WARNING,
            )
        finally:
            await port.aclose()
            await worker.close()

    loop.run_until_complete(go())

    about_log = [row for row in said if row.event == "Технический журнал"]
    assert about_log, "куда пишется лог, в журнале не сказано ни строкой"
    assert str(chosen) in about_log[-1].reason


def test_no_setting_changes_without_a_line_in_the_journal() -> None:
    """О каждом поле `Settings` кто-то обязан рассказать в журнале (ТЗ §4.4 А).

    Три рассказчика: движок, торговый модуль и само окно. Поле, не названное
    ни одним, меняется молча — владелец счёта поменял параметр по деньгам,
    а в журнале «Значения совпали с прежними».

    Проверка на **механизм**: список полей берётся у класса, а не пишется
    здесь от руки, поэтому поле, заведённое завтра, попадает сюда само.
    """
    import dataclasses

    known = {field.name for field in dataclasses.fields(Settings)}
    told = (
        set(convert._WINDOW_TOLD)  # noqa: SLF001 — проверяются сами таблицы
        | convert._TOLD_BY_ENGINE  # noqa: SLF001
        | convert._TOLD_BY_STRATEGY  # noqa: SLF001
    )
    assert known == told, (
        "поля настроек, об изменении которых в журнал не попадёт ни строки: "
        f"{sorted(known - told)}"
    )


def test_the_new_window_fields_have_a_journal_line() -> None:
    """Глубина, шаг цены, проскальзывание и каталог лога называются словами."""
    before = Settings()
    after = before.replace(
        depth_days=30, price_step=25.0, slippage_steps=1.0,
        log_directory="/tmp/логи",
    )
    lines = convert.window_changes(before, after)
    joined = " | ".join(lines)
    for word in ("Глубина показа", "Шаг цены", "Проскальзывание", "Каталог"):
        assert word in joined, f"про «{word}» в журнале не сказано: {joined}"
    assert joined.count("→") == 4, f"строк меньше, чем изменений: {joined}"
    assert "вся история" not in joined, "ноль дней подписан не тем словом"


def test_an_unchanged_pair_gives_no_lines() -> None:
    """Ничего не меняли — и строк нет: журнал не засоряется пустыми записями."""
    values = Settings()
    assert convert.window_changes(values, values) == []
    assert convert.guard_changes(values, values) == []


def test_the_guards_are_reported_apart_from_the_rest() -> None:
    """Три поля предохранителей идут отдельной строкой уровня «предупреждение».

    Тихое «принято» читалось бы как «ограничение поставлено», а за ними
    сегодня не стоит ничего.
    """
    before = Settings()
    after = before.replace(volume_cap=7, daily_loss_limit_pct=5.0)
    assert convert.window_changes(before, after) == [], (
        "предохранители попали в обычную строку и потеряли предупреждение"
    )
    guards = convert.guard_changes(before, after)
    assert len(guards) == 2 and all("→" in line for line in guards)
