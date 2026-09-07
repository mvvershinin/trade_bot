"""Оговорки прогона: что владелец счёта увидит рядом с показанной прибылью.

Проверяется здесь ровно одно свойство, и оно не про текст: **оговорка едет
вместе с числом**. Список считается из самого прогона, пустым не бывает,
рядом с итогом стоят не все записи, а те, без которых число читается неправдой,
и число таких строк ограничено.

⚠️ Тексты сверяются по **именам** записей, а не по фразам целиком. Тест,
повторяющий фразу дословно, ломается на каждой правке формулировки и потому
чинится подгонкой ожидания под код — то есть перестаёт проверять что-либо.
Числа внутри фраз сверяются отдельно и по одному: они приходят из прогона,
и подмена базы или единицы измерения обязана уронить прогон.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from backtest import (
    BESIDE_THE_NUMBER_LIMIT,
    TUNED_FROM,
    TUNED_UNTIL,
    Costs,
    Deal,
    HistoryRun,
    Summary,
    assumptions,
    headline,
    summarise,
)
from engine import ExitReason, Position, Side
from tests.engine_helpers import MSK, STEP

#: Неразрывный пробел, которым `backtest.assumptions` разделяет число и слово.
#: Записан escape-последовательностью намеренно: сам символ в исходнике
#: неотличим от обычного пробела, и тест, сверяющий «4 раза», молча перестал бы
#: находить строку, если бы кто-то поправил один пробел на другой.
NBSP = "\u00a0"

#: День внутри отрезка подбора и день заведомо после него. Второй нужен,
#: чтобы отличить «прогон повторяет подбор» от «прогон идёт по другому
#: отрезку»: на одинаковых датах эта развилка не проверяется вовсе.
INSIDE = datetime(2026, 7, 15, 10, 10, tzinfo=MSK)
OUTSIDE = datetime(2026, 12, 15, 10, 10, tzinfo=MSK)


def _deal(
    *,
    at: datetime = INSIDE,
    entry: float = 100_000.0,
    exit_: float = 100_100.0,
    reason: ExitReason = ExitReason.SIGNAL,
    commission: float | None = 28.0,
    volume: float = 1.0,
    entry_id: str = "open:1",
) -> Deal:
    return Deal(
        side=Side.LONG, volume=volume,
        entry_time=at, entry_price=entry, entry_order_id=entry_id,
        exit_time=at + STEP, exit_price=exit_, exit_order_id="close:1",
        exit_reason=reason, commission=commission,
    )


def _run(
    deals: list[Deal] | None = None,
    *,
    costs: Costs | None = None,
    position: Position | None = None,
) -> HistoryRun:
    """Прогон из готовых сделок. Итог считает `summarise`, а не тест."""
    items = tuple(_deal() if deals is None else deal for deal in (deals or [_deal()]))
    return HistoryRun(
        deals=items,
        summary=summarise(items),
        costs=costs or Costs(commission_per_side=14.0),
        position=position,
    )


def _names(run: HistoryRun) -> list[str]:
    return [item.name for item in run.assumptions]


def _named(run: HistoryRun, name: str):
    found = [item for item in run.assumptions if item.name == name]
    assert found, f"оговорки «{name}» в списке нет: {_names(run)}"
    return found[0]


# ---------------------------------------------------------------------------
# Список существует всегда
# ---------------------------------------------------------------------------

def test_the_list_is_never_empty_even_for_a_run_without_a_single_deal() -> None:
    """Пустой список означал бы «отчёт равен выписке со счёта» — неправда всегда."""
    empty = HistoryRun(summary=Summary())
    assert empty.assumptions, "у пустого прогона не нашлось ни одной оговорки"
    assert empty.headline.strip(), "заголовок пуст, и число осталось без оговорки"


def test_the_caveat_is_a_property_of_the_same_object_that_carries_the_money() -> None:
    """Оговорка берётся из прогона, а не подаётся рядом: разойтись с ним нечему."""
    run = _run()
    assert run.assumptions == assumptions(run)
    assert run.headline == headline(run)
    assert not hasattr(HistoryRun, "__slots__") or "assumptions" not in HistoryRun.__slots__, (
        "оговорка стала хранимым полем — значит её можно не заполнить"
    )


# ---------------------------------------------------------------------------
# Подбор и проверка
# ---------------------------------------------------------------------------

def test_a_run_inside_the_tuned_slice_says_it_repeats_the_tuning() -> None:
    """Прогон по отрезку подбора называет себя повторением подбора, а не проверкой."""
    inside = _run([_deal(at=INSIDE)])
    assert "Настройки подобраны на этом же отрезке" in _names(inside)
    assert "Результата на подборе рядом нет" not in _names(inside)


def test_a_run_outside_the_tuned_slice_says_the_tuning_column_is_missing() -> None:
    """Отрезок вне подбора не выдаётся за проверку: второй колонки всё равно нет."""
    outside = _run([_deal(at=OUTSIDE)])
    assert "Результата на подборе рядом нет" in _names(outside)
    assert "Настройки подобраны на этом же отрезке" not in _names(outside)


def test_a_run_that_only_touches_the_tuned_slice_counts_as_the_tuning_one() -> None:
    """Пересечение хотя бы одним днём — уже повторение подбора, а не проверка.

    Граница проверяется последним днём подбора: сдвиг неравенства на день
    молча переводил бы такой прогон в «независимый».
    """
    edge = datetime.combine(TUNED_UNTIL, INSIDE.timetz())
    assert "Настройки подобраны на этом же отрезке" in _names(_run([_deal(at=edge)]))
    day_after = edge + timedelta(days=1)
    assert "Результата на подборе рядом нет" in _names(_run([_deal(at=day_after)]))


def test_the_tuned_slice_is_the_one_the_documents_name() -> None:
    """Даты подбора — те, что записаны в DOMAIN.md §4, а не соседние."""
    assert (TUNED_FROM, TUNED_UNTIL) == (date(2026, 6, 19), date(2026, 8, 26))


# ---------------------------------------------------------------------------
# Комиссия
# ---------------------------------------------------------------------------

def test_a_run_without_a_tariff_says_the_shown_profit_is_before_commission() -> None:
    """Тариф не задан — показана валовая, и об этом сказано рядом с числом."""
    free = _run([_deal(commission=None)], costs=Costs())
    assert free.summary.net_profit is None, "тест подан с тарифом — развилки нет"
    caveat = _named(free, "Комиссия не учтена")
    assert caveat.beside_number, "оговорка про валовую прибыль ушла из-под числа"


def test_a_run_with_a_tariff_does_not_carry_the_commission_caveat() -> None:
    """Тариф задан — оговорки про валовую нет: иначе она стоит всегда и не читается."""
    charged = _run([_deal(commission=28.0)])
    assert charged.summary.net_profit is not None, "тест подан без тарифа"
    assert _names(charged), "список пуст — отсутствие проверено вхолостую"
    assert "Комиссия не учтена" not in _names(charged)


def test_the_commission_caveat_counts_two_charges_per_closed_deal() -> None:
    """Списаний столько же, сколько исполнений: вход и выход у каждой сделки.

    Число сверено с архивом прототипа: 127 сделок × 2 × 14 ₽ = 3 556 ₽.
    """
    free = _run([_deal(commission=None), _deal(commission=None)], costs=Costs())
    assert "4 раза" in _named(free, "Комиссия не учтена").short.replace(NBSP, ' ')


# ---------------------------------------------------------------------------
# Проскальзывание
# ---------------------------------------------------------------------------

def test_a_run_without_slippage_says_the_price_was_taken_from_the_tape() -> None:
    """Нулевое проскальзывание названо вслух и стоит рядом с числом."""
    ideal = _run()
    assert ideal.costs.slippage == 0.0, "тест подан с проскальзыванием"
    assert _named(ideal, "Проскальзывание не учтено").beside_number


def test_a_run_with_slippage_swaps_the_caveat_instead_of_dropping_it() -> None:
    """Проскальзывание задано — оговорка не исчезает, а называет величину.

    Исчезнувшая оговорка читалась бы как «издержки учтены полностью»,
    а плоская величина от объёма не зависит и на большом объёме занижена.
    """
    slipping = _run(costs=Costs(commission_per_side=14.0, price_step=25.0, slippage_steps=1.0))
    assert "Проскальзывание не учтено" not in _names(slipping)
    named = _named(slipping, "Проскальзывание учтено плоской величиной")
    assert "25" in named.short, "величина проскальзывания в оговорку не попала"


def test_the_slippage_caveat_names_the_profit_left_for_one_fill() -> None:
    """Запас на исполнение — итог, делённый на число исполнений, а не на сделки.

    Деление на сделки вместо исполнений завысило бы запас вдвое — ровно
    в ту сторону, против которой оговорка и написана.
    """
    # Две сделки по +100 пунктов, комиссия 28 ₽ на сделку: чистая 144 ₽
    # на четыре исполнения — 36 ₽ на исполнение.
    run = _run([_deal(entry_id="open:1"), _deal(entry_id="open:2")])
    assert run.summary.net_profit == pytest.approx(144.0)
    short = _named(run, "Проскальзывание не учтено").short.replace(NBSP, ' ')
    assert "4 исполнения" in short, short
    assert "36,0 ₽" in short, short


def test_a_losing_run_does_not_claim_a_reserve_it_does_not_have() -> None:
    """Итог в минусе — «запаса нет вовсе», а не отрицательный запас на исполнение."""
    losing = _run([_deal(exit_=99_000.0)])
    short = _named(losing, "Проскальзывание не учтено").short
    assert "запаса" in short and "-" not in short, short


# ---------------------------------------------------------------------------
# Остальные допущения
# ---------------------------------------------------------------------------

def test_the_take_caveat_appears_only_when_the_run_has_take_exits() -> None:
    """Выходы по уровню названы отдельно: их цена — самая оптимистичная в прогоне."""
    with_take = _run([_deal(reason=ExitReason.TAKE_PROFIT)])
    assert "Тейк исполнен по касанию" in _names(with_take)
    without = _run([_deal(reason=ExitReason.SIGNAL)])
    assert "Тейк исполнен по касанию" not in _names(without)


def test_the_volume_caveat_appears_only_above_one_contract() -> None:
    """На одном контракте про стакан говорить нечего, на пяти — есть."""
    single = _run([_deal(volume=1.0)])
    assert "Объём не двигает цену" not in _names(single)
    many = _run([_deal(volume=5.0)])
    assert "5 контрактов" in _named(many, "Объём не двигает цену").short.replace(NBSP, ' ')


def test_an_open_position_at_the_end_is_named_as_money_the_total_does_not_have() -> None:
    """Открытая позиция названа: её денег в итоге нет ни рубля."""
    left = Position(
        side=Side.LONG, volume=1.0, entry_price=100_000.0, entry_time=INSIDE
    )
    assert "Позиция на конце не закрыта" in _names(_run(position=left))
    assert "Позиция на конце не закрыта" not in _names(_run(position=None))


def test_the_missing_safeguards_and_the_perfect_data_are_named_on_every_run() -> None:
    """Предохранителей и обрывов связи в прогоне нет никогда — и это сказано всегда."""
    for run in (_run(), _run([_deal(at=OUTSIDE)]), HistoryRun(summary=Summary())):
        assert "Предохранители не работали" in _names(run)
        assert "Связь не рвалась, свечи не пропадали" in _names(run)


# ---------------------------------------------------------------------------
# Сколько стоит рядом с числом
# ---------------------------------------------------------------------------

def test_no_run_puts_more_than_the_limit_of_lines_beside_the_number() -> None:
    """Рядом с числом не больше трёх строк — иначе их перестают читать.

    Худшее сочетание: тарифа нет, проскальзывания нет, позиция открыта,
    отрезок совпал с подбором. Проверяется именно оно, а не удобный случай.
    """
    worst = _run(
        [_deal(commission=None, reason=ExitReason.TAKE_PROFIT, volume=5.0)],
        costs=Costs(),
        position=Position(
            side=Side.LONG, volume=5.0, entry_price=100_000.0, entry_time=INSIDE
        ),
    )
    beside = [item for item in worst.assumptions if item.beside_number]
    assert len(beside) <= BESIDE_THE_NUMBER_LIMIT, (
        f"рядом с числом {len(beside)} строк: " + "; ".join(i.name for i in beside)
    )
    assert len(worst.assumptions) > len(beside), (
        "все оговорки встали рядом с числом — значит отбора нет"
    )


def test_the_headline_names_the_rest_instead_of_hiding_them() -> None:
    """Заголовок говорит, сколько оговорок осталось за кадром."""
    run = _run()
    hidden = len(run.assumptions) - sum(
        1 for item in run.assumptions if item.beside_number
    )
    assert hidden > 0, "скрытых оговорок нет — счётчик проверен вхолостую"
    assert str(hidden) in run.headline, run.headline


def test_every_caveat_beside_the_number_carries_its_short_form() -> None:
    """У строки рядом с числом есть короткая форма, у записи в выгрузке — абзац."""
    items = _run().assumptions
    assert items, "список пуст — цикл ниже не выполнится ни разу"
    for item in items:
        assert item.short and item.text, item.name
        assert len(item.short) < len(item.text), (
            f"«{item.name}»: короткая форма не короче абзаца, значит одна из них лишняя"
        )
