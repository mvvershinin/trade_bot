"""Сборка работает с **любым** алгоритмом реестра, а не с первым.

Зачем этот файл существует
--------------------------
Реестр торговых алгоритмов содержит одну запись, и это надолго: владелец
счёта 08.09.2026 — «текущий алгоритм пока оставляем как по умолчанию».
Реестр на одну запись — удобнейшее место спрятать дефект «работает только
для первого»: любая привязка к алгоритму №1 остаётся зелёной, пока он
единственный, и вылезает в первый же день сравнения двух алгоритмов —
окно показывает второй, а считает первый, **молча**.

Поэтому здесь заведён **подставной** второй алгоритм. Настоящий второй
не пишется по прямому решению владельца счёта; подставной проверяет ровно
то, что от механизма требуется, и стоит три десятка строк.

Подставной сделан **непохожим нарочно**
---------------------------------------
Совпадения имён — главный способ пройти проверку случайно. Поэтому у него:

* другое имя класса настроек и **другие имена полей** (`bars`, `smoothing`
  вместо `period`, `kind`) — сборщик, знающий поля алгоритма №1, упадёт;
* своё перечисление сглаживания с теми же **именами элементов** и другими
  значениями — мост между окном и алгоритмом обязан идти по имени элемента,
  а не по значению и не по классу;
* своё название, свои подписи полей и своё правило словами — снимок прогона
  и журнал обязаны взять их у него, а не у алгоритма по умолчанию.

⚠️ Изоляция (правило 14 `CLAUDE.md`). Каждая проверка ставит свою таблицу
реестра через `monkeypatch` и не полагается на соседей; файл проверен
запуском **по одному тесту**, а не только целиком.
"""

from __future__ import annotations

import dataclasses
import enum

import pytest

from app import convert
from app import runs as runs_log
from strategies import (
    Bar,
    Claim,
    Decision,
    Description,
    Intent,
    SettingsField,
    StrategyEntry,
    registry,
)
from ui.models import AverageKind, Settings

# ---------------------------------------------------------------------------
# Подставной второй алгоритм: свои поля, своё перечисление, своё правило
# ---------------------------------------------------------------------------


class Smoothing(enum.Enum):
    """Сглаживание подставного алгоритма.

    ⚠️ Имена элементов те же, что у окна (`ui.models.AverageKind`), а
    **значения другие**: мост между окном и алгоритмом обязан идти по имени
    элемента. Мост по значению здесь бы прошёл случайно и сломался бы
    на настоящем втором алгоритме.
    """

    EMA = "сглаженное"
    SMA = "простое"


@dataclasses.dataclass(frozen=True, slots=True)
class SecondSettings:
    """Настройки подставного алгоритма. Имена полей **не** как у первого."""

    bars: int = 7
    smoothing: Smoothing = Smoothing.EMA

    @property
    def label(self) -> str:
        """Подпись линии: своя, чтобы её нельзя было спутать с `EMA(15)`."""
        return f"ВТОРОЙ[{self.bars}]"

    def changes_from(self, previous: SecondSettings) -> list[str]:
        """Строки журнала — свои, чтобы отличались от строк первого."""
        if previous.bars == self.bars:
            return []
        return [f"Свечей у второго: {previous.bars} → {self.bars}"]


class SecondAlgorithm:
    """Подставной модуль: намерений не даёт, порт соблюдает."""

    title = "Подставной второй алгоритм"

    def __init__(self, settings: SecondSettings) -> None:
        self.settings = settings
        self.seen = 0

    def reset(self) -> None:
        """Забыть накопленное."""
        self.seen = 0

    def on_closed_bar(self, bar: Bar) -> Decision:
        """Прогрев до `bars` свечей, дальше молчание.

        ⚠️ Намерений не даёт **никогда**, и это не лень подставного, а его
        рабочая часть: сделок у него не бывает, а у алгоритма №1 на тех же
        свечах бывают. «Сделок 0» в записи прогона — единственное
        свидетельство о том, каким алгоритмом **считали**; название в записи
        говорит только о том, что написали.
        """
        self.seen += 1
        if self.seen <= self.settings.bars:
            return Decision(
                intent=Intent.NONE,
                reason="прогрев второго",
                close=bar.close,
                skip_bar=True,
            )
        return Decision(
            intent=Intent.NONE,
            reason="второй молчит",
            close=bar.close,
            average=bar.close,
            warmed_up=True,
            bars=self.seen,
        )


def describe_second(settings: SecondSettings) -> Description:
    """Правило подставного словами. Ни одного слова из правила первого."""
    return Description(
        title=SecondAlgorithm.title,
        lead=f"Второй смотрит выше и ниже своей линии по {settings.bars} свечам.",
        claims=(
            Claim(
                relation="закрытие выше линии второго",
                detail=f"закрытие выше {settings.label}",
                intent=Intent.LONG,
                probe=lambda average: average + 10.0,
            ),
            Claim(
                relation="закрытие ниже линии второго",
                detail=f"закрытие ниже {settings.label}",
                intent=Intent.SHORT,
                probe=lambda average: average - 10.0,
            ),
        ),
    )


def second_entry(
    *,
    fields: tuple[SettingsField, ...] | None = None,
) -> StrategyEntry:
    """Запись подставного алгоритма. Собирается на каждый вызов.

    ⚠️ Своя на каждый вызов, а не общая на модуль: общая таблица, которую
    можно поправить из любого места, однажды будет поправлена из теста
    и достанется соседям (`D-078`).
    """
    return StrategyEntry(
        id="second",
        title=SecondAlgorithm.title,
        settings_type=SecondSettings,
        factory=SecondAlgorithm,
        fields=fields
        or (
            SettingsField("bars", "average_period", "Свечей у второго"),
            SettingsField("smoothing", "average_kind", "Сглаживание второго"),
        ),
        describe=describe_second,
    )


@pytest.fixture
def two_algorithms(monkeypatch: pytest.MonkeyPatch) -> StrategyEntry:
    """Реестр из двух записей: настоящая первая и подставная вторая.

    Первая остаётся на месте намеренно: проверяется **выбор**, а реестр
    из одной подставной записи проверял бы только то, что программа
    работает с единственным алгоритмом, каким бы он ни был.
    """
    second = second_entry()
    monkeypatch.setattr(
        registry, "_ENTRIES", (registry.default_entry(), second), raising=True
    )
    return second


def chose_the_second() -> Settings:
    """Набор окна с выбранным вторым алгоритмом."""
    return Settings(strategy_id="second", average_period=9)


# ---------------------------------------------------------------------------
# Сборка настроек: собирается ВЫБРАННЫЙ алгоритм, а не первый
# ---------------------------------------------------------------------------


def test_the_settings_of_the_chosen_algorithm_are_built_not_the_first(
    two_algorithms: StrategyEntry,
) -> None:
    """Выбран второй — собраны настройки второго, его полями и его именами.

    Это главная проверка всей развязки. Сборщик, знающий поля алгоритма №1,
    здесь либо упадёт (полей `period`/`kind` у второго нет), либо соберёт
    чужой класс — и оба случая видны.

    Мутация, обязанная ронять проверку: вернуть в `convert.strategy_settings`
    сборку по своей таблице полей вместо таблицы записи реестра.
    """
    made = convert.strategy_settings(chose_the_second())
    assert isinstance(made, SecondSettings), (
        f"собраны настройки не выбранного алгоритма, а {type(made).__name__}"
    )
    assert made.bars == 9, "значение поля окна не доехало до второго алгоритма"


def test_the_window_enum_reaches_the_algorithm_by_the_name_of_its_member(
    two_algorithms: StrategyEntry,
) -> None:
    """Перечисление окна переводится в перечисление алгоритма по имени.

    У окна и у алгоритмов свои комплекты перечислений — `ui/` торговые слои
    не импортирует. Мост между ними один, и он обязан работать для любого
    алгоритма: у подставного значения элементов **другие**, а имена те же.

    Мутация, обязанная ронять проверку: переводить по значению элемента.
    """
    made = convert.strategy_settings(
        chose_the_second().replace(average_kind=AverageKind.SMA)
    )
    assert isinstance(made, SecondSettings)
    assert made.smoothing is Smoothing.SMA, (
        "выбранное в окне сглаживание не доехало до алгоритма"
    )


def test_an_algorithm_asking_for_a_field_the_window_has_not_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Поле, которого у окна нет, — отказ вслух с именем поля.

    Молча подставить умолчание значило бы торговать настройкой, которой
    владелец счёта не выбирал, при исправном виде окна.
    """
    broken = second_entry(
        fields=(
            SettingsField("bars", "momentum_period", "Свечей у второго"),
            SettingsField("smoothing", "average_kind", "Сглаживание второго"),
        )
    )
    monkeypatch.setattr(registry, "_ENTRIES", (registry.default_entry(), broken))
    with pytest.raises(convert.SettingsRefused) as refusal:
        convert.strategy_settings(chose_the_second())
    said = str(refusal.value)
    assert "momentum_period" in said, "отказ не называет, какого поля не хватило"
    assert SecondAlgorithm.title in said, "отказ не называет алгоритм"


def test_a_table_naming_a_setting_the_algorithm_has_not_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Опечатка в имени поля алгоритма — отказ вслух, а не потеря настройки."""
    broken = second_entry(
        fields=(
            SettingsField("barz", "average_period", "Свечей у второго"),
            SettingsField("smoothing", "average_kind", "Сглаживание второго"),
        )
    )
    monkeypatch.setattr(registry, "_ENTRIES", (registry.default_entry(), broken))
    with pytest.raises(convert.SettingsRefused) as refusal:
        convert.strategy_settings(chose_the_second())
    said = str(refusal.value)
    assert "barz" in said or "bars" in said, "отказ не называет разошедшееся поле"


# ---------------------------------------------------------------------------
# Правило словами: рассказывается правило ВЫБРАННОГО алгоритма (`D-098`)
# ---------------------------------------------------------------------------


def test_the_rule_in_words_is_the_rule_of_the_chosen_algorithm(
    two_algorithms: StrategyEntry,
) -> None:
    """Описание берётся у выбранной записи, а не у алгоритма по умолчанию.

    `D-098`: `rule_of` спрашивал `registry.default_entry()`, то есть при
    выбранном втором алгоритме окно, журнал и снимок прогона рассказывали бы
    правило первого — молча и убедительно.

    Мутация, обязанная ронять проверку: вернуть `default_entry()` в `rule_of`.
    """
    said = convert.strategy_rule(chose_the_second())
    assert SecondAlgorithm.title not in said or "ВТОРОЙ[9]" in said
    assert "ВТОРОЙ[9]" in said, "правило рассказано не про выбранный алгоритм"
    assert "EMA(15)" not in said, "в правиле числа и слова чужого алгоритма"


def test_the_headline_for_the_journal_is_the_chosen_algorithms(
    two_algorithms: StrategyEntry,
) -> None:
    """Строка правила для журнала решений — тоже выбранного алгоритма."""
    values = chose_the_second()
    said = convert.rule_headline_of(
        convert.chosen_algorithm(values), convert.strategy_settings(values)
    )
    assert said.startswith(SecondAlgorithm.title), (
        "строка журнала называет чужой алгоритм"
    )
    assert "ВТОРОЙ[9]" in said


# ---------------------------------------------------------------------------
# Снимок настроек прогона: название, подписи полей и правило — выбранного
# ---------------------------------------------------------------------------


def test_the_snapshot_takes_titles_and_rule_from_the_chosen_algorithm(
    two_algorithms: StrategyEntry,
) -> None:
    """Снимок прогона целиком описывает **выбранный** алгоритм.

    Три вещи в снимке зависят от алгоритма — название, подписи его полей
    и правило словами. Пока подписи лежали списком в `app/runs.py`, а правило
    бралось у алгоритма по умолчанию, снимок при выбранном втором назвал бы
    его по имени и описал бы первым.

    Мутация, обязанная ронять проверку: вернуть в `app/runs.py` свой список
    подписей полей.
    """
    text = runs_log.snapshot_of(chose_the_second())
    assert f"Торговый алгоритм: {SecondAlgorithm.title}" in text
    assert "Свечей у второго: 9" in text, "подписи полей взяты не у алгоритма"
    assert "Сглаживание второго" in text
    assert "Период средней" not in text, "в снимке подписи чужого алгоритма"
    assert "ВТОРОЙ[9]" in text, "правило в снимке не про выбранный алгоритм"


def test_the_line_on_the_chart_is_labelled_by_the_chosen_algorithm(
    two_algorithms: StrategyEntry,
) -> None:
    """Подпись линии на графике собирает сам алгоритм.

    Мутация, обязанная ронять проверку: собрать подпись в `app/convert.py`
    из полей окна и короткого имени средней.
    """
    said = convert.average_label(chose_the_second())
    assert said.startswith("ВТОРОЙ[9]"), f"подпись линии чужая: {said!r}"


# ---------------------------------------------------------------------------
# Смена алгоритма: журнал говорит, во что превратилось правило
# ---------------------------------------------------------------------------


def test_switching_the_algorithm_does_not_compare_settings_across_classes(
    two_algorithms: StrategyEntry,
) -> None:
    """При смене алгоритма настройки поле в поле не сравниваются.

    `changes_from` принимает настройки **того же** алгоритма. Сравнение
    через границу алгоритмов либо упало бы (у классов разные поля), либо —
    что хуже — выдало бы «Период средней: 15 → 15» про два разных правила.
    """
    said = convert.rule_changes(Settings(), chose_the_second())
    assert said, "смена алгоритма не оставила в журнале ни строки"
    assert any("Правило теперь читается так" in line for line in said)
    assert any("ВТОРОЙ[9]" in line for line in said), (
        "журнал не назвал правило, в которое всё превратилось"
    )
    assert not any("Период средней" in line for line in said), (
        "настройки сравнены через границу алгоритмов"
    )


def test_a_change_inside_one_algorithm_is_told_by_that_algorithm(
    two_algorithms: StrategyEntry,
) -> None:
    """Строку «было → стало» пишет сам алгоритм, своими словами."""
    said = convert.rule_changes(
        chose_the_second(), chose_the_second().replace(average_period=11)
    )
    assert any("Свечей у второго: 9 → 11" in line for line in said), (
        f"строку изменения написал не алгоритм: {said}"
    )


# ---------------------------------------------------------------------------
# Живой ход и прогон собирают ВЫБРАННЫЙ модуль
# ---------------------------------------------------------------------------


def test_the_registry_builds_the_chosen_module_not_the_first(
    two_algorithms: StrategyEntry,
) -> None:
    """Модуль собирается записью реестра — тем, который выбран.

    Дорога та же, что в живом ходе и в прогоне по истории
    (`app/port.py::_watcher`, `_replay`): `entry.build(module)`.
    """
    values = chose_the_second()
    made = convert.chosen_algorithm(values).build(
        convert.strategy_settings(values)
    )
    assert isinstance(made, SecondAlgorithm), (
        f"собран не выбранный алгоритм, а {type(made).__name__}"
    )
    assert made.title == SecondAlgorithm.title


def test_the_settings_of_one_algorithm_are_refused_by_another(
    two_algorithms: StrategyEntry,
) -> None:
    """Настройки одного алгоритма, поданные другому, — отказ вслух."""
    first = registry.default_entry()
    with pytest.raises(TypeError) as refusal:
        first.build(SecondSettings())
    assert "SecondSettings" in str(refusal.value)


# ---------------------------------------------------------------------------
# Перебор лидеров: чужому алгоритму — честный отказ (ловушка 14)
# ---------------------------------------------------------------------------


def test_the_sweep_refuses_an_algorithm_it_was_not_written_for(
    two_algorithms: StrategyEntry,
) -> None:
    """Сетка перебора написана под один алгоритм и отказывает остальным.

    Без отказа перебор честно перебрал бы поля алгоритма №1 при выбранном
    втором и показал бы результат как «ваши лидеры»: числа настоящие,
    к выбранному правилу отношения не имеют.
    """
    from backtest.sweep import ForeignStrategy, refuse_foreign_strategy

    with pytest.raises(ForeignStrategy) as refusal:
        refuse_foreign_strategy("second", SecondSettings())
    said = str(refusal.value)
    assert "second" in said, "отказ не называет, какой алгоритм выбран"
    assert registry.default_entry().title in said, (
        "отказ не называет, под какой алгоритм написана сетка"
    )
    assert "не запущен" in said, "отказ не говорит, что перебора не будет"


def test_the_sweep_ground_refuses_foreign_settings_at_construction() -> None:
    """Отказ стоит в модели данных, а не в проводке.

    Через поле `Ground.strategy` настройки расходятся по всему перебору,
    включая рабочие процессы; перехватывать их в каждой двери означало бы
    забыть одну.
    """
    from backtest.sweep import ForeignStrategy, Ground
    from engine import EngineSettings

    with pytest.raises(ForeignStrategy):
        Ground(
            engine=EngineSettings(),
            # Подставные настройки и есть предмет проверки.
            strategy=SecondSettings(),  # type: ignore[arg-type]
            strategy_id="second",
        )


def test_the_sweep_accepts_the_algorithm_it_was_written_for() -> None:
    """Канарейка: отказ не срабатывает на своём алгоритме.

    Без неё проверка выше зеленела бы и на отказе, который отвергает всё
    подряд, — то есть перебор не работал бы вовсе.
    """
    from backtest.sweep import GRID_STRATEGY_ID, refuse_foreign_strategy
    from strategies import EmaReverseSettings

    given = EmaReverseSettings()
    assert refuse_foreign_strategy(GRID_STRATEGY_ID, given) is given


# ---------------------------------------------------------------------------
# Незнакомое имя алгоритма — отказ, а не подстановка умолчания
# ---------------------------------------------------------------------------


def test_an_unknown_algorithm_name_is_refused_loudly() -> None:
    """Имя из более новой сборки — отказ вслух, прежние настройки в силе.

    Подставить умолчание значило бы торговать правилом, которого владелец
    счёта не выбирал, при исправном виде окна.
    """
    with pytest.raises(convert.SettingsRefused) as refusal:
        convert.strategy_settings(Settings().replace(strategy_id="atr_channel"))
    said = str(refusal.value)
    assert "atr_channel" in said, "отказ не называет, чего не нашлось"
    assert registry.DEFAULT_ID in said, "отказ не называет, что есть в сборке"


def test_the_catalogue_shows_every_algorithm_of_the_registry(
    two_algorithms: StrategyEntry,
) -> None:
    """Окно выбора показывает все записи реестра, и выбранная помечена."""
    catalogue = convert.algorithms(chose_the_second())
    assert [one.id for one in catalogue] == [registry.DEFAULT_ID, "second"]
    chosen = [one for one in catalogue if one.chosen]
    assert [one.id for one in chosen] == ["second"]
    assert "ВТОРОЙ[9]" in chosen[0].details, (
        "у выбранного алгоритма в каталоге не ваши числа"
    )


def test_the_numbers_of_an_unchosen_algorithm_are_called_its_own(
    two_algorithms: StrategyEntry,
) -> None:
    """У невыбранного алгоритма показаны **его** умолчания, и это сказано.

    Полей чужого алгоритма окно не показывает, брать числа неоткуда; молча
    показать умолчания значило бы дать прочесть «вот что будет у меня».
    """
    catalogue = convert.algorithms(Settings())
    second = next(one for one in catalogue if one.id == "second")
    assert not second.chosen
    assert "ВТОРОЙ[7]" in second.details, "у невыбранного не его умолчания"
    assert "умолчаниями" in second.summary, "молчаливые чужие числа в списке"


# ---------------------------------------------------------------------------
# Сетка перебора идёт за своим объявлением, а не за умолчанием реестра
# ---------------------------------------------------------------------------


def test_the_sweep_follows_its_own_declaration_not_the_registry_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сменилось умолчание реестра — сетка осталась при своём алгоритме.

    ⚠️ Сегодня `GRID_STRATEGY_ID` и `registry.DEFAULT_ID` совпадают, поэтому
    подмена одного другим ничего не ломает **сегодня** — и мутация проходит
    молча. Она станет дефектом ровно в день, когда умолчанием сделают второй
    алгоритм: перебор молча начнёт собирать его, перебирая поля первого.

    Мутация, обязанная ронять проверку: `registry.default_entry()` вместо
    `registry.find(GRID_STRATEGY_ID)` в `backtest/sweep.py::grid_strategy`.
    """
    from backtest.sweep import GRID_STRATEGY_ID, grid_strategy
    from strategies import EmaReverseSettings

    first = registry.default_entry()
    monkeypatch.setattr(registry, "_ENTRIES", (second_entry(), first))
    monkeypatch.setattr(registry, "DEFAULT_ID", "second")
    assert registry.default_entry().id == "second", "подмена умолчания не сработала"

    made = grid_strategy()
    assert made.id == GRID_STRATEGY_ID, (
        "сетка пошла за умолчанием реестра, а не за своим объявлением"
    )
    assert made.settings_type is EmaReverseSettings


# ---------------------------------------------------------------------------
# Порт: живой ход и прогон по истории считают ВЫБРАННЫМ алгоритмом
# ---------------------------------------------------------------------------


def test_the_port_records_the_run_under_the_chosen_algorithm(
    loop, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Прогон через порт записывается **выбранным** алгоритмом целиком.

    Самая дальняя точка развязки: сюда доезжают и название для колонки
    журнала прогонов, и подписи полей снимка, и правило словами. Пока порт
    собирал алгоритм по имени класса, эта запись была бы про первый при любом
    выборе — а именно по ней через месяц разбирают, чем гнали прогон.

    Мутация, обязанная ронять проверку: `registry.default_entry().build(...)`
    вместо `algorithm.build(...)` в `app/port.py::_replay`.
    """
    import math
    from datetime import datetime, timedelta

    from app.port import HistoryPort
    from market import MSK, Candle, MarketWorker, Source, Timeframe
    from market.storage import CandleStore

    monkeypatch.setattr(registry, "_ENTRIES", (registry.default_entry(), second_entry()))
    day = datetime(2026, 6, 19, 7, 0, tzinfo=MSK)
    minutes = [
        Candle(
            time=day + timedelta(minutes=index),
            open=(price := 100000.0 + 300.0 * math.sin(index / 9.0)),
            high=price + 40.0, low=price - 40.0, close=price,
            volume=1.0, timeframe=Timeframe(1), filled_minutes=1,
        )
        for index in range(300)
    ]
    database = tmp_path / "candles.sqlite3"
    with CandleStore(database) as store:
        store.put_minutes("MXU6", minutes, Source.ISS)

    async def go() -> None:
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(
            worker, values=chose_the_second(), days=0, sanitize=lambda text: text
        )
        try:
            port.refresh("тест")
            await port.wait()
        finally:
            await port.aclose()
            await worker.close()

    loop.run_until_complete(go())
    with CandleStore(database) as store:
        sessions = list(store.journal_sessions(limit=999).rows)
    assert sessions, "прогон не записан в журнал прогонов вовсе"
    session = sessions[0]
    assert session.strategy == SecondAlgorithm.title, (
        f"прогон записан чужим алгоритмом: {session.strategy!r}"
    )
    assert "Свечей у второго: 9" in session.settings, (
        "снимок настроек прогона собран подписями чужого алгоритма"
    )
    assert "ВТОРОЙ[9]" in session.settings, (
        "правило в снимке прогона — не выбранного алгоритма"
    )
    # ⚠️ Самая важная строка проверки. Всё выше — **надписи**: они могли бы
    # называть второй алгоритм, пока движок считает первым. Подставной
    # намерений не даёт вовсе, а алгоритм №1 на этом ряду сделки делает,
    # поэтому «Сделок 0» — единственное здесь свидетельство о том, **чем
    # считали**, а не о том, что написали.
    assert "Сделок 0" in (session.finish_note or ""), (
        "прогон посчитан не выбранным алгоритмом: подставной сделок "
        f"не делает, а в итоге записано «{session.finish_note}»"
    )


def test_the_live_run_is_computed_by_the_chosen_algorithm_too(
    loop, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Живой ход считает выбранным алгоритмом, а не первым в реестре.

    Вторая дорога, по которой алгоритм доезжает до движка
    (`app/port.py::_watcher`), и она отдельная от прогона по истории.
    Проверять её надписями бессмысленно по той же причине: надпись могла бы
    называть второй алгоритм при первом внутри. Подставной сделок не делает,
    алгоритм №1 на этом ряду делает — отсюда и «Сделок 0».

    Мутация, обязанная ронять проверку: собрать в `_watcher` алгоритм
    по умолчанию вместо выбранного.
    """
    import dataclasses as dc
    import math
    from datetime import datetime, timedelta

    from app.port import HistoryPort
    from market import MSK, Candle, CandleStore, MarketWorker, Source, Timeframe
    from market.journal import redact

    monkeypatch.setattr(registry, "_ENTRIES", (registry.default_entry(), second_entry()))
    day = datetime(2026, 6, 19, 7, 0, tzinfo=MSK)
    minutes = [
        dc.replace(
            Candle(
                time=day + timedelta(minutes=index),
                open=(price := 100000.0 + 300.0 * math.sin(index / 9.0)),
                high=price + 40.0, low=price - 40.0, close=price,
                volume=1.0, timeframe=Timeframe(1), filled_minutes=1,
            ),
        )
        for index in range(600)
    ]
    database = tmp_path / "live.sqlite3"
    with CandleStore(database) as store:
        store.put_minutes("MXU6", minutes, Source.ISS)

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(
            worker, values=chose_the_second(), days=0, sanitize=redact
        )
        port.attach_stream(lambda on: None)
        try:
            port.stream(True)
            await port.wait()
            for number in range(4):
                port.refresh(f"закрылся бар {number}")
                await port.wait()
            await port.aclose()
            return (await worker.journal_sessions(limit=100)).rows
        finally:
            await port.aclose()
            await worker.close()

    rows = loop.run_until_complete(go())
    live = [row for row in rows if row.origin.value == "paper"]
    assert live, "живой ход не записан вовсе"
    assert live[0].strategy == SecondAlgorithm.title, (
        f"живой ход записан чужим алгоритмом: {live[0].strategy!r}"
    )
    assert "Сделок 0" in (live[0].finish_note or ""), (
        "живой ход посчитан не выбранным алгоритмом: подставной сделок "
        f"не делает, а в итоге записано «{live[0].finish_note}»"
    )
