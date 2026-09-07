"""Таблица перебора для владельца счёта: по-русски, в рублях, двумя колонками.

Правило вёрстки здесь ровно одно, и оно из ТЗ §4.10 Г: **колонка «проверка»
стоит рядом с колонкой «подбор» в каждой строке**. Ключа, скрывающего вторую
колонку, в этом модуле нет и не будет — не потому, что его забыли добавить,
а потому, что таблица с одной колонкой не отчёт, а иллюстрация.

Модуль ничего не считает. Все числа приходят готовыми из `backtest.report`;
здесь только выравнивание, знаки и русские слова. Проверять текст отчёта
тестом на смысл — плохая мысль: смысл проверяется на `Study`, а тут проверяется
то, что в строке действительно две колонки, а не одна.
"""

from __future__ import annotations

from backtest.report import BlockReport, Choice, Forward, Row, Study
from backtest.sweep import Money

__all__ = ["MINUS", "THOUSANDS", "both_columns_filled", "money", "plural", "render"]

#: Типографский минус. Тот же знак, что в окне программы: в моноширинном
#: шрифте он занимает столько же места, сколько плюс, и колонка не пляшет.
MINUS = "−"

#: Разделитель разрядов — **неразрывный** пробел (U+00A0), тот же, что
#: в `ui/formatting.py`. Обычный пробел разорвал бы «−22 102 ₽» переносом
#: при копировании таблицы в письмо, и число стало бы читаться как два.
#: Знак назван постоянной, потому что на вид он неотличим от обычного
#: пробела, и «поправить» его молча — вопрос одной замены.
THOUSANDS = "\u00a0"

_WIDTH = 102

#: Десятки, у которых окончание всегда «много»: одиннадцать … четырнадцать.
#: Это не диапазон «с 11 по 14 включительно вообще», а именно исключение
#: русского счёта, и потому оно названо, а не вписано числами в условие.
_TEENS = (11, 14)

#: С какой последней цифры окончание становится «много»: 5, 6, 7, 8, 9.
_MANY_FROM = 5


def money(value: float | None, *, sign: bool = True) -> str:
    """Рубли по-русски: разряды пробелом, минус типографский.

    `None` — «нет», а не ноль. Ноль в отчёте читается как результат,
    а «сделок не было» результатом не является (DOMAIN.md §5).
    """
    if value is None:
        return "—"
    text = f"{value:+,.0f}" if sign else f"{value:,.0f}"
    return text.replace(",", THOUSANDS).replace("-", MINUS)


def plural(count: int, one: str, few: str, many: str) -> str:
    """Русское согласование числительного: «1 сделка», «2 сделки», «5 сделок».

    Отдельной функцией, а не тремя `if` по месту: согласование нужно в шести
    местах отчёта, и написанное шесть раз оно разойдётся на седьмом.
    """
    tail, tens = abs(count) % 10, abs(count) % 100
    if _TEENS[0] <= tens <= _TEENS[1] or tail == 0 or tail >= _MANY_FROM:
        return many
    return one if tail == 1 else few


def _cell(item: Money) -> str:
    """Пара «сделок · рублей» одной ячейкой."""
    if item.trades == 0:
        return f"{'—':>6}  {'сделок нет':>9}"
    return f"{item.trades:>6}  {money(item.rubles):>9}"


def _row(row: Row) -> str:
    """Одна строка таблицы: настройка, подбор, проверка, соседи."""
    around = (
        "—"
        if row.verdict.neighbours == 0
        else f"{money(row.verdict.worst)}…{money(row.verdict.best)}"
    )
    return (
        f"  {row.label:<30.30} │ {_cell(row.tuning)} │ {_cell(row.checking)} │ "
        f"{around:>17} {row.verdict.word}"
    )


def _head() -> tuple[str, str, str]:
    """Шапка таблицы: два яруса заголовков и линейка."""
    return (
        f"  {'настройка':<30} │ {'ПОДБОР':^17} │ {'ПРОВЕРКА':^17} │ "
        f"{'соседи на проверке':>17}",
        f"  {'':<30} │ {'сделок':>6}  {'₽':>9} │ {'сделок':>6}  {'₽':>9} │ "
        f"{'худший…лучший':>17}",
        "  " + "─" * _WIDTH,
    )


def _choice(choice: Choice) -> list[str]:
    """Что выбрал бы подбор — и чем это обернулось. Отдельным абзацем."""
    lines = [
        "",
        f"  Лучшее НА ПОДБОРЕ: {choice.label}",
        f"    подбор   {money(choice.tuning.rubles)} ₽ ({_deals(choice.tuning.trades)})",
        f"    ПРОВЕРКА {money(choice.checking.rubles)} ₽ "
        f"({_deals(choice.checking.trades)}) — место {choice.place} из {choice.total}",
    ]
    if choice.verdict.stable is None:
        lines.append("    соседей по сетке нет: устойчивость не проверяется")
    else:
        lines.append(
            f"    соседи на проверке: {money(choice.verdict.worst)}…"
            f"{money(choice.verdict.best)} ₽ — {choice.verdict.word}"
        )
    return lines


def _block(report: BlockReport) -> list[str]:
    """Блок перебора целиком: вопрос, таблица, выбор подбора, поправка."""
    lines = ["", f"{report.title.upper()}", f"  Вопрос: {report.question}", ""]
    lines.extend(_head())
    lines.extend(_row(row) for row in report.rows)
    lines.extend(_choice(report.choice))
    if report.deflated is None:
        lines.append("    поправка Шарпа не считается: сделок слишком мало")
    else:
        lines.append(
            f"    поправка Шарпа на число испытаний: {report.deflated:.2f}".replace(".", ",")
            + " (1,00 — лучший точно не случаен, 0,50 — не отличается от случайного)"
        )
    return lines


def _deals(count: int) -> str:
    """«66 сделок» с согласованным окончанием."""
    return f"{count} {plural(count, 'сделка', 'сделки', 'сделок')}"


def _forward(item: Forward) -> list[str]:
    """Скользящая проверка вперёд: по окну на строку плюс итог."""
    lines = [
        "",
        "СКОЛЬЗЯЩАЯ ПРОВЕРКА ВПЕРЁД",
        "  Подбираем на прошлом, гоняем следующие дни, ничего не переподбирая.",
        "",
        f"  {'окно':<5} {'подбор':<23} {'проверка':<23} {'что выбрал подбор':<27} {'₽':>9}",
        "  " + "─" * _WIDTH,
    ]
    lines.extend(
        f"  {step.fold.number:<5} {str(step.fold.tuning):<23} "
        f"{str(step.fold.checking):<23} {step.label:<27.27} "
        f"{money(step.money.rubles):>9}  место {step.place} из {step.total}"
        for step in item.steps
    )
    lines.extend((
        "  " + "─" * _WIDTH,
        f"  ИТОГО переподстройка: {money(item.rubles)} ₽",
        f"  Те же дни на умолчаниях: {money(item.baseline)} ₽",
    ))
    if item.overfit is not None:
        below = round(item.overfit * len(item.steps))
        lines.append(
            f"  Лучший на подборе оказался ниже середины на проверке "
            f"в {below} окнах из {len(item.steps)}"
        )
    return lines


def _preamble(study: Study) -> list[str]:
    """Шапка отчёта: на чём считали, чем считали и сколько прогонов было."""
    return [
        "ПЕРЕБОР НАСТРОЕК: ПОДБОР И ПРОВЕРКА",
        "",
        f"  Инструмент: {study.symbol}, пятиминутные свечи",
        f"  История: {study.days} "
        f"{plural(study.days, 'торговый день', 'торговых дня', 'торговых дней')}, "
        f"{study.since:%d.%m.%Y}–{study.until:%d.%m.%Y}",
        f"  Комиссия: {_tariff(study.costs.per_side)}",
        f"  Проскальзывание в шагах цены: {study.costs.slippage_steps:g}",
        f"  Прогонов: {study.runs}",
        "",
        "  ГРАНИЦА МЕЖДУ ПОДБОРОМ И ПРОВЕРКОЙ ОДНА НА ВЕСЬ ОТЧЁТ:",
        f"    подбор   {study.split.tuning}",
        f"    проверка {study.split.checking}",
        "",
        f"  Стандартная ошибка итога на умолчаниях: ±{money(study.error_of_total, sign=False)} ₽.",
        "  Прибыль меньше этой величины от нуля не отличается.",
    ]


def _tariff(value: float | None) -> str:
    """Тариф словами. `None` — тарифа нет, а не ноль."""
    if value is None:
        return "не задана — чистой прибыли в отчёте не будет"
    return f"{value:g} ₽ за контракт на сторону"


def _tail(study: Study) -> list[str]:
    """Оговорки: то, без чего числа выше читаются неправдой."""
    lines = ["", "ЧТО НАДО ЗНАТЬ ПРО ЭТИ ЧИСЛА", ""]
    lines.append(
        "  • Сделок, потерянных на границах отрезков: нет — колонки считают всё."
        if not study.straddling and not study.outside
        else f"  • Сделки, не попавшие ни в подбор, ни в проверку: до "
             f"{study.straddling} разорвано границей и до {study.outside} вне "
             "отрезков (в одном прогоне). Сумма колонок на них меньше итога."
    )
    lines.extend(f"  • {note}" for note in study.notes)
    return lines


def render(study: Study) -> str:
    """Весь отчёт текстом. Ни одного числа здесь не считается."""
    lines: list[str] = list(_preamble(study))
    for block in study.blocks:
        lines.extend(_block(block))
    lines.extend(_forward(study.forward))
    lines.extend(_tail(study))
    return "\n".join(lines) + "\n"


def both_columns_filled(text: str) -> bool:
    """В каждой строке таблицы заполнены **обе** колонки: подбор и проверка.

    Вынесено функцией, а не оставлено тесту, ровно по одной причине: правило
    «спрятать вторую колонку невозможно» — требование ТЗ §4.10 Г, и проверять
    его надо тем же кодом, который сдаётся, а не переписанным в тесте
    регулярным выражением, которое разойдётся с вёрсткой при первой правке.

    Строкой таблицы считается всякая строка с разделителями колонок. У неё
    обязано быть ровно четыре поля, и во втором и третьем — либо деньги,
    либо честное «сделок нет». Пустое поле означало бы спрятанную колонку.
    """
    for line in text.splitlines():
        if "│" not in line:
            continue
        parts = [part.strip() for part in line.split("│")]
        if len(parts) != _COLUMNS:
            return False
        if not all(parts[at] for at in (1, 2)):
            return False
    return True


#: Столько полей в строке таблицы: настройка, подбор, проверка, соседи.
#: Число названо, а не вписано в проверку: колонка, выброшенная из вёрстки,
#: обязана уронить проверку, а не совпасть с новым числом по совпадению.
_COLUMNS = 4
