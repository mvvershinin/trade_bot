"""Выгрузка журналов в файл одной кнопкой (ТЗ §4.6).

Формат — CSV, который открывается в Excel двойным щелчком и без мастера импорта.
Это накладывает три условия, каждое из которых проверено на практике чаще, чем
хотелось бы:

* **Разделитель `;`.** В русской локали Excel считает запятую разделителем
  дробной части, и файл с запятыми ложится в одну колонку.
* **Запятая в числах.** С точкой Excel в той же локали кладёт число как текст,
  и сумма по колонке не считается.
* **BOM в начале файла.** Без него Excel читает UTF-8 как cp1251, и весь журнал
  выглядит как «Ð¡Ð´ÐµÐ»ÐºÐ°».

Чистка секретов — **второй** рубеж, не первый
--------------------------------------------
Первый стоит на границе `app/` → `ui/`: `app.port` чистит каждое текстовое поле
каждого объекта, который отправляет в окно (`HistoryPort._send`). Текст,
дошедший сюда, уже чистый.

Здесь чистка всё равно зовётся, и это не перестраховка ради спокойствия.
Выгрузка — единственный выход, который **уезжает с машины**: файл открывают
через полгода, складывают с другим и пересылают в переписку. Рубеж на границе
живёт в другом файле и снимается другой правкой; связка «два рубежа, каждый
достаточен сам по себе» — то же устройство, что у `broker/` и `market/`
(два набора образцов, разные слои). Чистка идемпотентна по построению, поэтому
второй проход ничего не портит.

Умолчания у параметра `sanitize` нет намеренно. Умолчание «ничего не делать»
было бы обещанием, только записанным кодом; собрать файл, не назвав чистку,
нельзя вовсе.

Саму чистку слой окна не пишет и не импортирует: полная чистка —
`broker.redaction.scrub`, у неё есть реестр живых значений токена,
а `broker/` окну по ARCHITECTURE.md §2 не положен. Функция приходит параметром
с границы `app/`; тип объявлен в `ui/ports.py`.

⚠️ Здесь стояло обещание «токена и любых данных подключения здесь нет и быть
не может: выгружаются строки журналов, которые их не содержат». Оно было
неверным (строки приезжают в окно сигналами порта, мимо базы и мимо чистки)
и вредным: это ровно тот список «а вот эти колонки чистые», против которого
написан докстринг `market/journal.py`. Обещание в комментарии механизмом
не является.
"""

from __future__ import annotations

import csv
import enum
from collections.abc import Iterable, Sequence
from pathlib import Path

from ui.formatting import csv_number, fmt_datetime
from ui.models import DecisionRow, RunOrigin, TradeRow, TradesSummary
from ui.ports import Sanitize

#: Заголовки журнала сделок. Колонка результата подписана **нейтрально**,
#: и это не лень: в ней лежит `net`, если тариф комиссии задан, и `gross`,
#: если не задан (`app/convert.py::trade_row`). Тариф стирается штатно —
#: поле комиссии в окне имеет особое значение «не задан», и это способ
#: посмотреть валовую. Поэтому безусловное «за вычетом комиссии» здесь врало
#: бы: 05.09.2026 в прогоне владельца счёта расхождение валовой и чистой
#: составило 4 004 ₽ на 143 сделках.
#:
#: Точную подпись даёт `trade_headers` — по самим строкам. Константа остаётся
#: как форма таблицы (число колонок, порядок, ширина) и как заголовок,
#: который **не может соврать**, если строк ещё нет.
TRADE_HEADERS = (
    "Вход (МСК)", "Выход (МСК)", "Сторона", "Объём",
    "Цена входа", "Цена выхода", "Причина выхода",
    "Результат сделки, ₽", "Движение цены, %", "Комиссия, ₽", "Происхождение",
)

#: Номер колонки результата. Считается из заголовков, а не пишется числом:
#: вставка колонки в середину увела бы подпись на соседнюю молча.
RESULT_COLUMN = TRADE_HEADERS.index("Результат сделки, ₽")

#: Номер колонки комиссии — по той же причине именем, а не числом.
COMMISSION_COLUMN = TRADE_HEADERS.index("Комиссия, ₽")


class ResultBasis(enum.Enum):
    """Из чего сложена колонка результата — и, значит, как её подписать.

    Различие не косметическое. `TradeRow.profit_rub` несёт **чистый**
    результат, когда тариф комиссии задан, и **валовый**, когда не задан
    (`app/convert.py::trade_row`, `DOMAIN.md` §5). Одна подпись на оба случая
    обязана быть неправдой в одном из них.

    `MIXED` сегодня недостижим — окно показывает журнал одного прогона за раз,
    и тариф у прогона один. Он станет достижимым, как только журнал начнут
    склеивать из базы и текущего прогона (`D-027`), и тогда складывать
    столбик будет нельзя вовсе.
    """

    NET = "net"
    GROSS = "gross"
    MIXED = "mixed"
    EMPTY = "empty"

    @property
    def heading(self) -> str:
        """Подпись колонки результата."""
        return _RESULT_HEADINGS[self]

    @property
    def note(self) -> str:
        """Подсказка на заголовке: что именно в колонке и чего в ней нет."""
        return _RESULT_NOTES[self]


_RESULT_HEADINGS = {
    ResultBasis.NET: "Результат за вычетом комиссии, ₽",
    ResultBasis.GROSS: "Результат ДО вычета комиссии, ₽",
    ResultBasis.MIXED: "Результат, ₽ — часть строк без комиссии",
    ResultBasis.EMPTY: "Результат сделки, ₽",
}

_RESULT_NOTES = {
    ResultBasis.NET: (
        "Чистый результат сделки: комиссия обеих сторон уже вычтена. "
        "Сама комиссия — в колонке справа."
    ),
    ResultBasis.GROSS: (
        "⚠️ Комиссия НЕ вычтена: тариф не задан в настройках, и во что обошлась "
        "сделка — программе неизвестно. Валовая прибыль результатом не "
        "считается: реверсная система делает много переворотов, и комиссия "
        "съедает заметную часть. Впишите тариф в настройках — колонка станет "
        "чистым результатом."
    ),
    ResultBasis.MIXED: (
        "⚠️ В колонке вперемешку чистые и валовые результаты: тариф комиссии "
        "известен не у всех сделок. Складывать такой столбик нельзя — итог "
        "по нему был бы ни тем ни другим."
    ),
    ResultBasis.EMPTY: "Результат сделки. Сделок пока нет — считать нечего.",
}


def result_basis(trades: Sequence[TradeRow]) -> ResultBasis:
    """Что лежит в колонке результата у этого набора строк.

    Признак — заполненность колонки комиссии, и он тот же, каким пользуется
    итог участка (`ui/chart_panel.py::span_totals`): `None` в комиссии
    означает «тариф не задан», а не «комиссия нулевая».
    """
    known = [trade.commission_rub is not None for trade in trades]
    if not known:
        return ResultBasis.EMPTY
    if all(known):
        return ResultBasis.NET
    if not any(known):
        return ResultBasis.GROSS
    return ResultBasis.MIXED


def trade_headers(trades: Sequence[TradeRow]) -> tuple[str, ...]:
    """Заголовки журнала сделок под конкретные строки.

    Одно место на окно и на выгрузку — намеренно: две записи одного названия
    разошлись бы молча, и файл говорил бы одно, а экран другое.
    """
    basis = result_basis(trades)
    headers = list(TRADE_HEADERS)
    headers[RESULT_COLUMN] = basis.heading
    return tuple(headers)

DECISION_HEADERS = ("Время (МСК)", "Событие", "Причина", "Важность", "Происхождение")

#: Что стоит в колонке происхождения, когда строка пришла без него.
#:
#: Пустая ячейка здесь читалась бы как «обычная сделка», то есть как боевая, —
#: а это ровно та ошибка, ради которой колонка и заведена: владелец счёта
#: выгружает отчёт за день, видит сорок сделок вместо четырёх настоящих
#: и от этого числа выбирает объём (задача Э1-10б, решение 0011).
UNNAMED_RUN = "прогон не назван"


def origin_cell(origin: RunOrigin | None) -> str:
    """Происхождение прогона одной ячейкой — словами, а не кодом.

    Выгрузка уезжает из окна файлом и живёт дальше сама: её открывают через
    полгода, пересылают и складывают с другой. Поэтому происхождение стоит
    у **каждой строки**, а не в заголовке файла, — склейка двух выгрузок его
    не теряет.
    """
    return UNNAMED_RUN if origin is None else origin.label


def write_csv(
    path: str | Path,
    headers: Sequence[str],
    rows: Iterable[Sequence[str]],
    sanitize: Sanitize,
) -> Path:
    """Записать таблицу. Возвращает путь — его показывают пользователю.

    :param sanitize: чистка секретов. Обязательна, умолчания не имеет —
        см. шапку модуля. Через неё проходит **каждая** ячейка и каждый
        заголовок; списка «а эти колонки чистые» здесь нет намеренно,
        такой список уже оказывался неверным сразу в семи местах
        (докстринг `market/journal.py`).
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writerow([_cell(head, sanitize) for head in headers])
        for row in rows:
            writer.writerow([_cell(cell, sanitize) for cell in row])
    return target


def _cell(value: str, sanitize: Sanitize) -> str:
    """Ячейка на выходе: сначала чистка, потом обезвреживание формулы.

    Порядок именно такой. `_safe` смотрит на первый символ того текста,
    который действительно уедет в файл; чистка, отработавшая после проверки,
    правила бы уже проверенную строку, и проверка относилась бы не к ней.
    """
    return _safe(sanitize(str(value)))


def _safe(cell: str) -> str:
    """Обезвредить ячейку, которую Excel принял бы за формулу.

    Причина выхода и текст ошибки брокера приходят снаружи. Строка, начинающаяся
    с `=` или `@`, в Excel выполняется как формула — это известный способ
    исполнить чужой код через безобидную выгрузку. Числа не трогаем: `-1 234,50`
    формулой не является.
    """
    text = str(cell)
    if text[:1] in {"=", "@"}:
        return "'" + text
    if text[:1] in {"+", "-"} and not text[1:2].isdigit():
        return "'" + text
    return text


def trades_table(
    trades: Sequence[TradeRow], summary: TradesSummary | None = None
) -> tuple[tuple[str, ...], list[list[str]]]:
    """Журнал сделок в виде таблицы строк.

    Итог, если он есть, дописывается снизу отдельными строками — так же, как
    он показан под таблицей в окне. Комиссия остаётся своей колонкой и своей
    строкой итога: валовая прибыль в этом проекте не результат (DOMAIN.md §5).
    """
    rows: list[list[str]] = []
    for trade in trades:
        rows.append([
            fmt_datetime(trade.entry_time),
            fmt_datetime(trade.exit_time),
            trade.side.label,
            csv_number(trade.volume, 0),
            csv_number(trade.entry_price),
            csv_number(trade.exit_price),
            trade.exit_reason,
            csv_number(trade.profit_rub),
            csv_number(trade.profit_pct),
            csv_number(trade.commission_rub),
            origin_cell(trade.origin),
        ])
    if summary is not None:
        rows.append([])
        rows.append(["Итог"])
        rows.append(["Сделок", csv_number(summary.trades, 0)])
        if summary.profitable_share is not None:
            rows.append(["Доля прибыльных, %", csv_number(summary.profitable_share * 100, 1)])
        rows.append(["Валовая прибыль, ₽", csv_number(summary.gross_profit_rub)])
        rows.append(["Комиссия, ₽", csv_number(summary.commission_rub)])
        rows.append(["Чистая прибыль, ₽", csv_number(summary.net_profit_rub)])
        rows.append(["Профит-фактор", csv_number(summary.profit_factor)])
        rows.append(["Максимальная просадка, ₽", csv_number(summary.max_drawdown_rub)])
        if summary.reversals is not None:
            rows.append(["Переворотов", csv_number(summary.reversals, 0)])
        rows.extend(_assumption_rows(summary))
    return trade_headers(trades), rows


def _assumption_rows(summary: TradesSummary) -> list[list[str]]:
    """Оговорка про итог — строками под итогом, а не только на экране.

    Файл уезжает с машины и живёт дальше сам: его открывают через полгода
    и складывают с другим. Число без оговорки в таком файле читается как
    выписка со счёта — ровно то, чем оно не является (решение 0030).

    ⚠️ Здесь только то, что стоит рядом с числом на экране. Полный перечень
    допущений прогона (`backtest.assumptions`) в файл пока не попадает —
    у окна его нет вовсе, до него доезжает лишь `headline`.
    """
    if not summary.headline:
        return []
    return [[], ["Оговорка к итогу"], *([line] for line in summary.headline.splitlines())]


def decisions_table(decisions: Sequence[DecisionRow]) -> tuple[tuple[str, ...], list[list[str]]]:
    """Журнал решений в виде таблицы строк."""
    rows = [
        [
            fmt_datetime(row.time, seconds=True),
            row.event,
            row.reason,
            row.level.label,
            origin_cell(row.origin),
        ]
        for row in decisions
    ]
    return DECISION_HEADERS, rows
