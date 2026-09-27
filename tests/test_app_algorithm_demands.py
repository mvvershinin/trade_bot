"""Требования алгоритма к общим настройкам программы: отказ и подмена.

Что здесь стережётся
--------------------
Правило принятия решений и настройки движка живут в разных слоях, и есть
сочетания, при которых название алгоритма становится ложью. «Реверс
с постоянной позицией» — тот случай: момент переворота принадлежит движку,
и при перевороте через свечу робот покидает рынок на каждом перевороте
(в эталонном журнале прототипа — **79 переворотов из 79** с разрывом ровно
в одну пятиминутку).

Решение владельца счёта 14.09.2026 — **отказ**: пара не принимается, прежние
настройки остаются в силе, причина называется словами.

Два ответа на один вопрос, и разница намеренная
-----------------------------------------------
* **«Применить»** — отказ. Прежние настройки есть, и человеку возвращают то,
  что работало.
* **чтение файла настроек** — подмена вслух. Прежних настроек в этот момент
  не существует: программа только открывается, и отказ означал бы «не
  запускаюсь из-за поля в файле».

⚠️ **Проверяется пара, а не то поле, которое трогали.** Оба порядка: «выбрали
алгоритм при чужом перевороте» и «поменяли переворот при выбранном алгоритме».
Сторож, повешенный на смену алгоритма, вторую правку пропустил бы молча.

⚠️ **Правило 13 `CLAUDE.md`: ловится мутация молчания, а не текста.** Отказ
здесь проверяется не по фразе, а по проверяемому следствию — настройки
**не применились**: сигнала «настройки приняты» не было.
"""

from __future__ import annotations

import dataclasses
import pathlib
from asyncio import AbstractEventLoop
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta

import pytest

from app import convert
from app.port import HistoryPort
from app.settings_store import SettingsStore
from market import (
    MSK,
    Candle,
    CandleStore,
    MarketWorker,
    Source,
    Timeframe,
    redact,
)
from strategies import registry
from strategies.registry import SettingsDemand, StrategyEntry
from ui.models import (
    AverageKind,
    BacktestRequest,
    DecisionRow,
    ReversalMoment,
    Settings,
)

#: Инструмент синтетической истории — тот, что стоит в окне по умолчанию.
#: Порт читает базу по инструменту настроек, а умолчание сменяется вместе
#: с текущим контрактом (MXU6 → MXZ6 17.09.2026): символ, записанный
#: буквой, отвязал бы свечи в базе от умолчания и опустошил график.
SYMBOL = Settings().instrument

#: Алгоритм, который требует переворота в одной свече, и его требование.
ALWAYS = "ma_reverse_always"

#: Настройки, при которых пара сходится, и при которых нет.
GOOD = Settings(strategy_id=ALWAYS, reversal_moment=ReversalMoment.SAME_BAR)
BAD = Settings(strategy_id=ALWAYS, reversal_moment=ReversalMoment.NEXT_BAR)


class Recorded:
    """Что порт сказал наружу: принятые настройки, отказы и журнал решений.

    Три списка, и они разного веса. `applied` — **проверяемое следствие**:
    настройки доехали до окна и до файла. `failures` — текст, который человек
    прочитает. Сторож на одном тексте зеленел бы на программе, которая
    ругается и всё равно применяет.

    ⚠️ `journal` появился 14.09.2026 не для полноты. `HistoryPort._refuse`
    шлёт в `failed` **только причину**, а заголовок события («Прогон
    на истории не начат» против «Настройки не приняты») живёт единственно
    в журнале решений. Два разных отказа снаружи неразличимы без него —
    и проверка запроса прогона на этом зеленела ровно на той мутации,
    ради которой была написана.
    """

    def __init__(self, port: HistoryPort) -> None:
        self.applied: list[Settings] = []
        self.failures: list[str] = []
        self.journal: list[DecisionRow] = []
        port.settings_applied.connect(self.applied.append)
        port.failed.connect(self.failures.append)
        port.decision_appended.connect(self.journal.append)

    def events(self) -> list[str]:
        """Заголовки строк журнала: чем один отказ отличается от другого."""
        return [row.event for row in self.journal]


def drive(
    loop: AbstractEventLoop,
    tmp_path: pathlib.Path,
    start: Settings,
    work: Callable[[HistoryPort], None],
) -> Recorded:
    """Собрать порт на пустой базе, выполнить `work(port)`, вернуть записи.

    База пустая намеренно: проверяется разбор настроек, а не прогон.
    Порт закрывается до того, как поставленная им задача что-то посчитает.
    """

    async def go() -> Recorded:
        worker = MarketWorker(tmp_path / "candles.sqlite3")
        port = HistoryPort(worker, values=start, days=0, sanitize=redact)
        recorded = Recorded(port)
        work(port)
        await port.aclose()
        await worker.close()
        return recorded

    done: Recorded = loop.run_until_complete(go())
    return done


# --------------------------------------------------------------------------
# Б1. Отказ на границе применения — обе стороны пары
# --------------------------------------------------------------------------


def test_choosing_the_algorithm_at_the_wrong_reversal_is_not_accepted(
    loop, tmp_path
) -> None:
    """Выбор алгоритма при чужом моменте переворота не принимается.

    Стережёт: **молчаливое принятие** несовместимой пары при смене алгоритма.
    Мутация, обязанная ронять проверку: принять настройки и тихо переставить
    момент переворота — сторож на тексте отказа этого не заметил бы, а сторож
    на «настройки не применились» падает.
    """
    recorded = drive(loop, tmp_path, Settings(), lambda port: port.apply_settings(BAD))
    assert not recorded.applied, (
        "несовместимая пара принята: настройки уехали в окно и в файл, "
        f"а робот стоял бы вне рынка на каждом перевороте. Принято: "
        f"{recorded.applied}"
    )
    assert recorded.failures, "отказ прошёл молча — человеку не сказано ничего"
    said = " ".join(recorded.failures)
    assert registry.find(ALWAYS).title in said, (
        f"отказ не называет алгоритм, из-за которого он случился: «{said}»"
    )
    assert "В той же свече" in said, (
        f"отказ не говорит, что поставить вместо нынешнего: «{said}»"
    )


def test_changing_the_reversal_under_the_chosen_algorithm_is_not_accepted(
    loop, tmp_path
) -> None:
    """Обратный порядок: алгоритм уже выбран, меняют момент переворота.

    Стережёт: **заднюю дверь**. Сторож, повешенный на смену алгоритма,
    эту правку пропустил бы молча — человек сперва выбирает алгоритм при
    «в одной свече» (принято), а потом отдельным «Применить» возвращает
    «через свечу», и название в окне снова становится ложью.

    Мутация, обязанная ронять проверку: проверять требования только тогда,
    когда изменилось поле `strategy_id`.
    """
    recorded = drive(loop, tmp_path, GOOD, lambda port: port.apply_settings(BAD))
    assert not recorded.applied, (
        f"момент переворота переставлен под выбранным алгоритмом: {recorded.applied}"
    )
    assert recorded.failures, "отказ прошёл молча"


def test_the_compatible_pair_is_accepted(loop, tmp_path) -> None:
    """Канарейка отказа: сходящаяся пара принимается.

    Без неё оба сторожа выше зеленели бы на программе, которая не принимает
    новый алгоритм **никогда**, — то есть на алгоритме, выбрать который
    нельзя вовсе.
    """
    recorded = drive(loop, tmp_path, Settings(), lambda port: port.apply_settings(GOOD))
    assert recorded.applied == [GOOD], (
        f"сходящаяся пара не принята: {recorded.failures}"
    )
    assert not recorded.failures


def test_the_first_algorithm_is_not_restricted(loop, tmp_path) -> None:
    """Алгоритм без требований принимается при любом моменте переворота.

    Стережёт: требование, приклеенное ко **всем** алгоритмам. Умолчание
    программы — переворот через свечу, и именно на нём держится сверка
    с прототипом 127 из 127; отказ, сработавший здесь, отнял бы у проекта
    его единственный машинный пункт приёмки.
    """
    values = Settings(average_period=20, reversal_moment=ReversalMoment.NEXT_BAR)
    assert registry.find(values.strategy_id).demands == ()
    recorded = drive(
        loop, tmp_path, Settings(), lambda port: port.apply_settings(values)
    )
    assert recorded.applied == [values], f"отказано без требования: {recorded.failures}"


def test_a_backtest_with_an_incompatible_pair_does_not_start(loop, tmp_path) -> None:
    """Второй путь применения: запрос прогона по истории.

    Стережёт: проверку, поставленную **только** в «Применить». Этот путь
    закрепляет отрезок и ставит прогон в очередь до того, как настройки
    станут текущими, — без отказа прямо здесь остался бы запрошенный прогон
    при непринятых настройках.

    ⚠️ **Ловится заголовком события, а не тем, что настройки не применились,
    и это замер, а не осторожность.** `run_backtest` кончается вызовом
    `apply_settings`, и та откажет сама: `applied` пуст, а `failures` полон
    **и без** проверки в `run_backtest`. Прежняя редакция стояла ровно
    на этих двух утверждениях и мутацию «убрать вызов требований
    из `run_backtest`» пережила зелёной (14.09.2026). Различает их журнал:
    запрошенный прогон оставляет в нём «Прогон на истории запрошен»,
    а за собой — закреплённый отрезок и занятую очередь.

    Мутация, обязанная ронять проверку: убрать вызов требований из
    `HistoryPort.run_backtest`.
    """
    since, until = _span()
    request = BacktestRequest(settings=BAD, since=since, until=until)
    recorded = drive(
        loop, tmp_path, Settings(), lambda port: port.run_backtest(request)
    )
    assert not recorded.applied, "прогон принял несовместимые настройки"
    assert recorded.failures, "прогон не начат молча"
    events = recorded.events()
    assert "Прогон на истории запрошен" not in events, (
        "прогон запрошен при непринятых настройках: отрезок закреплён, "
        f"очередь занята тем, чему отказано. Журнал: {events}"
    )
    assert "Прогон на истории не начат" in events, (
        f"человеку не сказано, что не начат именно прогон: {events}"
    )


def _span() -> tuple[datetime, datetime]:
    """Отрезок прогона: сутки. Содержание неважно, важен разбор настроек."""
    return datetime(2026, 6, 19, tzinfo=MSK), datetime(2026, 6, 20, tzinfo=MSK)


# --------------------------------------------------------------------------
# Б1. Требование — данные, а не рукописный `if`
# --------------------------------------------------------------------------


def test_the_requirement_is_declared_by_the_registry_record() -> None:
    """Требование объявлено записью реестра, а не спрятано в проверке.

    Стережёт: возврат к рукописному `if` по имени алгоритма. Третий такой
    отказ был бы ветвлением, которое обязано быть таблицей (правило 9).
    """
    demands = registry.find(ALWAYS).demands
    assert len(demands) == 1, f"требований у «{ALWAYS}» стало {len(demands)}"
    only = demands[0]
    assert only.outer == "reversal_moment", (
        f"требование названо про другую настройку: {only.outer}"
    )
    assert only.value == "SAME_BAR", (
        f"требуется другое значение момента переворота: {only.value}"
    )
    # ⚠️ Причина проверяется по существу, а не «непустая»: она **одна на два
    # ответа** — её читает и отказ при «Применить», и подмена при чтении файла.
    # Сверять её саму с собой значило бы не проверять ничего.
    assert "в одной свече" in only.reason, (
        f"причина не называет, чего алгоритм требует: «{only.reason}»"
    )
    assert registry.find(ALWAYS).title not in only.reason, (
        "причина называет алгоритм по имени: тогда у неё два места — здесь "
        "и там, где её показывают, — и они разойдутся"
    )


def test_the_field_of_the_record_has_no_default() -> None:
    """Требования пишутся руками у каждого алгоритма, в том числе пустые.

    Стережёт: умолчание `()` у поля записи. С умолчанием алгоритм, заведённый
    завтра и забывший про свои требования, **молча** получил бы поведение
    первого — то есть работал бы при настройках, при которых работать
    не должен, и никто бы об этом не узнал.
    """
    field = next(
        one for one in dataclasses.fields(StrategyEntry) if one.name == "demands"
    )
    assert field.default is dataclasses.MISSING, (
        "у поля требований появилось умолчание: новый алгоритм теперь может "
        "промолчать о них вместо того, чтобы назвать их пустыми"
    )
    assert field.default_factory is dataclasses.MISSING


def test_the_refusal_follows_the_declaration_and_not_the_name_of_the_algorithm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Отказ идёт по объявленному требованию, а не по имени `ma_reverse_always`.

    Стережёт: проверку, приколоченную к личности алгоритма. Требование
    объявляется **на местной** записи реестра — про другое поле и другое
    значение, — и отказ обязан случиться на нём тоже.

    ⚠️ Таблица реестра подменяется местной (`D-078`): настоящая не трогается.
    """
    demanding = dataclasses.replace(
        registry.default_entry(),
        demands=(
            SettingsDemand(
                outer="average_kind",
                value="SMA",
                reason="требует простой средней — так придумано для проверки",
            ),
        ),
    )
    monkeypatch.setattr(registry, "_ENTRIES", (demanding,))
    convert._algorithm_trouble.cache_clear()  # noqa: SLF001 — запись подменена
    with pytest.raises(convert.SettingsRefused, match="простой средней"):
        convert.check_demands(Settings(average_kind=AverageKind.EMA))
    convert.check_demands(Settings(average_kind=AverageKind.SMA))


@pytest.mark.parametrize(
    ("outer", "value", "expected"),
    [
        ("no_such_field", "SAME_BAR", "нет такой настройки"),
        ("average_period", "SAME_BAR", "не выбор из списка"),
        ("reversal_moment", "NO_SUCH_VALUE", "нет такого значения"),
    ],
)
def test_a_typo_in_the_declaration_is_refused_out_loud(
    monkeypatch: pytest.MonkeyPatch, outer: str, value: str, expected: str
) -> None:
    """Опечатка в объявлении — отказ вслух, а не молча выключенное требование.

    Стережёт: **самую тихую поломку этой работы**. Требование объявлено
    строками, и строка, не попавшая ни в одно поле настроек и ни в один
    элемент перечисления, просто никогда не совпадёт: отказ не сработает
    ни разу, прогон останется зелёным, а робот будет торговать при настройке,
    при которой его название — неправда.

    Мутация, обязанная ронять проверку: убрать `_demand_gap` из таблицы
    проверок алгоритма.
    """
    broken = dataclasses.replace(
        registry.default_entry(),
        demands=(SettingsDemand(outer=outer, value=value, reason="проверка"),),
    )
    monkeypatch.setattr(registry, "_ENTRIES", (broken,))
    convert._algorithm_trouble.cache_clear()  # noqa: SLF001 — запись подменена
    with pytest.raises(convert.SettingsRefused, match=expected):
        convert.check_demands(Settings())


@pytest.fixture(autouse=True)
def _forget_cached_troubles() -> Iterator[None]:
    """Забыть разбор записей реестра до и после каждой проверки.

    `convert._algorithm_trouble` кэширован по записи — он зовётся на каждой
    свече живого хода. Проверки выше подменяют таблицу реестра местными
    записями, и кэш, переживший подмену, достался бы соседям: зелёный тест,
    падающий при запуске в одиночку, либо наоборот (правило 14 `CLAUDE.md`).
    """
    convert._algorithm_trouble.cache_clear()  # noqa: SLF001 — кэш проверки
    yield
    convert._algorithm_trouble.cache_clear()  # noqa: SLF001 — кэш проверки


# --------------------------------------------------------------------------
# Б2. Чтение файла настроек — подмена, но вслух
# --------------------------------------------------------------------------


def test_an_incompatible_pair_in_the_file_is_corrected_out_loud(
    tmp_path: pathlib.Path,
) -> None:
    """Файл с несовместимой парой: значение поправлено, и об этом сказано.

    Стережёт **две** вещи сразу, и обе — правило 13 `CLAUDE.md`:
    молчаливую подмену (строка обязана уйти в `troubles`, то есть дойти
    до журнала предупреждением) и отказ на старте (программа обязана
    открыться: прежних настроек в этот момент не существует).

    Мутация, обязанная ронять проверку: вернуть значение как есть; либо
    поправить его молча, не добавив строки.
    """
    store = SettingsStore(tmp_path)
    assert store.save(BAD) == ""
    read = SettingsStore(tmp_path).load()
    assert read.values.reversal_moment is ReversalMoment.SAME_BAR, (
        "несовместимый момент переворота оставлен: робот покидал бы рынок "
        "на каждом перевороте при названии, обещающем обратное"
    )
    assert read.values.strategy_id == ALWAYS, "подменён не тот из двух"
    said = " ".join(read.troubles)
    assert "Через свечу" in said, (
        f"подмена прошла молча либо не назвала, что стояло: {read.troubles}"
    )
    # ⚠️ Предупреждение обязано **объяснять**, а не только сообщать. Мутация
    # «убрать из фразы название алгоритма и объявленную причину» 14.09.2026
    # прошла весь прогон зелёной: человек читал «в файле стояло X — взято Y»
    # и не узнавал ни кто это сделал, ни почему.
    demand = registry.find(ALWAYS).demands[0]
    assert "в одной свече" in demand.reason, (
        f"объявленная причина не называет, чего алгоритм требует: "
        f"«{demand.reason}» — утверждения ниже без этого пусты"
    )
    assert demand.reason in said, (
        "предупреждение не объясняет, почему значение подменено. Причина "
        f"объявлена один раз и обязана читаться здесь: {read.troubles}"
    )
    assert registry.find(ALWAYS).title in said, (
        f"предупреждение не называет алгоритм, из-за которого подмена: "
        f"{read.troubles}"
    )
    assert not read.notes, "оговорка ушла не в тот список: её могут не показать"


def test_a_typo_in_the_declaration_does_not_stay_silent_at_start_up(
    loop, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Поломанное объявление доезжает до человека при запуске программы.

    ⚠️ Это **дыра, которую закрывает не чтение файла**. Чтение настроек
    несогласованное объявление обходит стороной и молчит: лечить его там
    нечем, оно одинаково при любом файле. Громко о нём говорит первый же
    пересчёт — `app/main.py` зовёт `refresh("запуск программы")` сразу после
    чтения, и разбор доходит до `convert.strategy_settings` →
    `chosen_algorithm` → `_demand_gap`.

    Проверка держит **эту цепочку**, а не её кусок: без неё «скажется где-то
    дальше» означало бы «не скажется нигде».

    Мутация, обязанная ронять проверку: убрать `_demand_gap` из таблицы
    проверок алгоритма.
    """
    monkeypatch.setattr(
        registry,
        "_ENTRIES",
        (
            dataclasses.replace(
                registry.default_entry(),
                demands=(SettingsDemand("reversal_moment", "TYPO", "проверка"),),
            ),
        ),
    )
    said: list[str] = []
    notes: list[str] = []

    async def go() -> None:
        worker = MarketWorker(_database(tmp_path))
        await worker.open()
        port = HistoryPort(worker, values=Settings(), days=0, sanitize=redact)
        port.failed.connect(said.append)
        port.decision_appended.connect(lambda row: notes.append(row.reason))
        port.refresh("запуск программы")
        await port.wait()
        await port.aclose()
        await worker.close()

    loop.run_until_complete(go())
    assert any("TYPO" in one for one in said), (
        f"несогласованное требование не доехало до окна: {said}"
    )
    assert any("TYPO" in one for one in notes), (
        f"несогласованное требование не попало в журнал решений: {notes}"
    )


def _database(tmp_path: pathlib.Path) -> pathlib.Path:
    """База с минутками: без свечей пересчёт останавливается раньше разбора.

    ⚠️ Не украшение фикстуры. На пустой базе порт отвечает «базы свечей нет»
    и до сборки настроек алгоритма не доходит — проверка выше зеленела бы,
    не проверив ничего.
    """
    start = datetime(2026, 6, 19, 7, 0, tzinfo=MSK)
    path = tmp_path / "candles.sqlite3"
    with CandleStore(path) as store:
        store.put_minutes(SYMBOL, [
            Candle(
                time=start + timedelta(minutes=number),
                open=100.0, high=101.0, low=99.0, close=100.0 + number % 7,
                volume=1.0, timeframe=Timeframe(1), filled_minutes=1,
            )
            for number in range(400)
        ], Source.ISS)
    return path


def test_a_compatible_pair_in_the_file_is_left_alone(tmp_path: pathlib.Path) -> None:
    """Канарейка подмены: сходящаяся пара не трогается и не ворчит.

    Без неё проверка зеленела бы на подмене, которая срабатывает **всегда**, —
    то есть на программе, которая никогда не читает выбор человека.
    """
    store = SettingsStore(tmp_path)
    assert store.save(GOOD) == ""
    read = SettingsStore(tmp_path).load()
    assert read.values.reversal_moment is ReversalMoment.SAME_BAR
    assert read.troubles == ()


def test_the_file_of_an_algorithm_without_demands_is_left_alone(
    tmp_path: pathlib.Path,
) -> None:
    """Алгоритм без требований: момент переворота из файла берётся как есть.

    Стережёт: подмену, приклеенную к полю, а не к требованию. Умолчание
    программы — переворот через свечу, и переписать его у первого алгоритма
    значило бы менять правило, на котором стоит сверка с прототипом.
    """
    values = Settings(reversal_moment=ReversalMoment.NEXT_BAR)
    assert registry.find(values.strategy_id).demands == ()
    store = SettingsStore(tmp_path)
    assert store.save(values) == ""
    read = SettingsStore(tmp_path).load()
    assert read.values.reversal_moment is ReversalMoment.NEXT_BAR
    assert read.troubles == ()
