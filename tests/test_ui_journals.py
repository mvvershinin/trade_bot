"""Журналы: сортировка, итог, выгрузка в файл.

Пункт приёмки Э1-17: «Журналы пережили перезапуск и выгрузились в файл».
Здесь проверяется вторая половина — выгрузка. Первая (хранение) относится
к базе и к `market/`: окно журналы не хранит и хранить не должно, иначе
запись зависела бы от того, открыто ли окно.
"""

from __future__ import annotations

import contextlib
import csv
import dataclasses
import errno
import os
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

import synthetic
from market import redact
from market.journal import SECRET_MASK
from ui import main_window
from ui.formatting import MSK
from ui.export import (
    DECISION_HEADERS,
    RESULT_COLUMN,
    TRADE_HEADERS,
    trade_headers,
    UNNAMED_RUN,
    decisions_table,
    trades_table,
    write_csv,
)
from ui.journals import SORT_ROLE, JournalTabs
from ui.models import RunOrigin, Side, TradeRow, TradesSummary


@pytest.fixture()
def tabs(qapp):
    widget = JournalTabs()
    widget.set_trades(synthetic.trades(), synthetic.summary())
    widget.set_decisions(synthetic.decisions())
    yield widget
    widget.deleteLater()
    qapp.processEvents()


def test_two_tabs_with_expected_names(tabs) -> None:
    assert [tabs.tabs.tabText(i) for i in range(tabs.tabs.count())] == [
        "Сделки", "Решения робота",
    ]


def test_numbers_sort_as_numbers(tabs) -> None:
    """Сортировка по результату идёт по числу, а не по строке.

    Текстовая сортировка ставит «−640,00 ₽» после «+1 425,00 ₽», и журнал
    сделок в самом важном месте читается неправильно.
    """
    view = tabs.trades_table
    view.sortByColumn(7, Qt.SortOrder.AscendingOrder)
    proxy = view.proxy
    values = [
        proxy.data(proxy.index(row, 7), SORT_ROLE) for row in range(proxy.rowCount())
    ]
    assert values == sorted(values)
    assert values[0] < 0 < values[-1]


def test_summary_shows_commission_separately(tabs) -> None:
    """Комиссия — отдельная строка итога. Валовая прибыль результатом не считается."""
    text = tabs.summary_label.text()
    assert "Комиссия" in text
    assert "Валовая" in text
    assert "Чистая прибыль" in text
    assert "Макс. просадка" in text


def test_summary_shows_the_profit_factor(tabs) -> None:
    """Профит-фактор — обязательный показатель ТЗ §4.10 Б, а не украшение."""
    assert "Профит-фактор" in tabs.summary_label.text()


def test_summary_says_when_it_has_no_numbers(qapp) -> None:
    """Пустой итог — это «сводки нет», а не нули. Нули читаются как результат."""
    widget = JournalTabs()
    try:
        assert "не отдавал сводку" in widget.summary_label.text()
    finally:
        widget.deleteLater()
        qapp.processEvents()


def test_export_opens_in_excel(tmp_path: Path) -> None:
    """Выгрузка: BOM, разделитель `;`, запятая в числах.

    Каждое из трёх условий проверено на практике: без BOM Excel читает UTF-8
    как cp1251, с запятой-разделителем кладёт всё в одну колонку, с точкой
    в числах считает их текстом.
    """
    headers, rows = trades_table(synthetic.trades(), synthetic.summary())
    target = write_csv(tmp_path / "trades.csv", headers, rows, redact)

    raw = target.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "нет BOM — Excel покажет кракозябры"

    text = raw.decode("utf-8-sig")
    assert ";" in text.splitlines()[0]
    parsed = list(csv.reader(text.splitlines(), delimiter=";"))
    assert parsed[0][0] == "Вход (МСК)"
    assert parsed[0][-1] == "Происхождение"
    assert parsed[0][-2] == "Комиссия, ₽"
    assert parsed[1][7].replace("-", "").replace(",", "").isdigit()
    assert "," in parsed[1][7], "число выгружено с точкой — Excel сочтёт его текстом"
    assert any(row and row[0] == "Комиссия, ₽" for row in parsed), "в итоге нет комиссии"


def test_export_carries_the_profit_factor() -> None:
    """Показатель обязан быть и в выгрузке: отчёт заказчику собирается из неё."""
    _, rows = trades_table(synthetic.trades(), synthetic.summary())
    assert any(row and row[0] == "Профит-фактор" for row in rows)


def test_export_of_decisions_keeps_the_reason(tmp_path: Path) -> None:
    headers, rows = decisions_table(synthetic.decisions())
    target = write_csv(tmp_path / "decisions.csv", headers, rows, redact)
    text = target.read_text(encoding="utf-8-sig")
    assert "Причина" in text.splitlines()[0]
    assert "недостаточно свободных средств" in text
    assert "ERR_" not in text, "в выгрузку попал код ошибки вместо фразы"


def test_export_disarms_excel_formulas(tmp_path: Path) -> None:
    """Ячейка, начинающаяся с `=`, в Excel выполняется как формула.

    Причина выхода и текст отказа брокера приходят снаружи; безобидная выгрузка
    не должна превращаться в исполнение чужого кода на машине владельца счёта.
    """
    target = write_csv(
        tmp_path / "dangerous.csv", ("Причина",), [["=1+1"], ["@SUM(A1)"], ["-640,00"]], redact
    )
    rows = list(csv.reader(target.read_text(encoding="utf-8-sig").splitlines(), delimiter=";"))
    assert rows[1][0] == "'=1+1"
    assert rows[2][0] == "'@SUM(A1)"
    assert rows[3][0] == "-640,00", "числу приписали апостроф — Excel сочтёт его текстом"


def _exported(qapp, folder: Path, trades, decisions) -> list[Path]:
    """Выгрузить журналы **через окно** и вернуть написанные файлы.

    Сборка окна — одна на все проверки выгрузки (`_window` ниже): путь
    владельца счёта это кнопка «Выгрузить оба журнала», и проверять его надо
    целиком. Две копии сборки разошлись бы, и одна из проверок перестала бы
    проверять то, что думает.
    """
    with _window(qapp, trades, decisions) as window:
        return window.export_journals_to(folder)


def test_window_exports_both_journals_by_one_button(qapp, tmp_path: Path) -> None:
    written = _exported(qapp, tmp_path, synthetic.trades(), synthetic.decisions())

    assert len(written) == 2
    assert all(path.exists() and path.stat().st_size > 0 for path in written)
    # Имена файлов латиницей — правило 6 `CLAUDE.md`. Выгрузку владелец счёта
    # пересылает и открывает на чужой машине, а кириллица в имени ломает путь
    # в консоли, в архиве и на чужой Windows. Русский остаётся ВНУТРИ файла:
    # заголовки колонок и причины решений — на русском, имя — латиницей.
    assert {path.name.split("_")[0] for path in written} == {"trades", "decisions"}


# -- происхождение прогона в выгрузке (Э1-10б) --------------------------------
#
# Гарантия «по строке видно, боевая ли она» живёт в `market/` и теряется
# по дороге в файл. Цена потери названа в решении 0011: владелец счёта
# выгружает отчёт за день, видит сорок сделок вместо четырёх настоящих
# и от этого числа выбирает объём.


@pytest.mark.parametrize("origin", list(RunOrigin), ids=[one.value for one in RunOrigin])
def test_the_export_of_trades_names_the_run_of_every_row(tmp_path: Path, origin) -> None:
    """Происхождение стоит у **каждой** строки, а не в шапке файла.

    Выгрузка уезжает из окна и живёт дальше сама: её открывают через полгода
    и складывают с другой. Пометка в шапке этого не переживает, пометка
    в строке — переживает.
    """
    rows = [dataclasses.replace(one, origin=origin) for one in synthetic.trades()]
    headers, table = trades_table(rows)
    target = write_csv(tmp_path / "trades.csv", headers, table, redact)

    parsed = list(csv.reader(target.read_text(encoding="utf-8-sig").splitlines(), delimiter=";"))
    place = parsed[0].index("Происхождение")
    assert [line[place] for line in parsed[1:]] == [origin.label] * len(rows)


@pytest.mark.parametrize("origin", list(RunOrigin), ids=[one.value for one in RunOrigin])
def test_the_export_of_decisions_names_the_run_of_every_row(origin) -> None:
    rows = [dataclasses.replace(one, origin=origin) for one in synthetic.decisions()]
    headers, table = decisions_table(rows)
    place = headers.index("Происхождение")
    assert {line[place] for line in table} == {origin.label}


def test_a_row_without_a_run_says_so_instead_of_leaving_it_blank() -> None:
    """Пустая ячейка читалась бы как «обычная сделка», то есть как боевая.

    Молчание здесь — то же самое, что тихая потеря свечи: неполный отчёт
    выглядит как полный.
    """
    trades = synthetic.trades()
    assert all(one.origin is None for one in trades), (
        "оснастка стала называть прогон — проверять «не назван» стало нечем"
    )
    headers, table = trades_table(trades)
    place = headers.index("Происхождение")
    assert {line[place] for line in table[: len(trades)]} == {UNNAMED_RUN}
    assert UNNAMED_RUN != "", "признак «прогон не назван» выродился в пустую ячейку"


def test_the_run_is_the_only_thing_the_origin_column_adds() -> None:
    """Вместе с происхождением в файл не поехало ничего лишнего.

    Токен и данные подключения в строках журнала не лежат, и колонка
    происхождения их туда не заводит: в ячейке ровно одно из четырёх
    заранее известных значений.
    """
    allowed = {one.label for one in RunOrigin} | {UNNAMED_RUN}
    for build, rows in (
        (trades_table, synthetic.trades()),
        (decisions_table, synthetic.decisions()),
    ):
        headers, table = build(rows)
        place = headers.index("Происхождение")
        cells = {line[place] for line in table[: len(rows)]}
        assert cells <= allowed, f"в колонке происхождения чужое значение: {cells - allowed}"


# -- колонки окна и колонки выгрузки ------------------------------------------


def test_every_column_the_model_declares_has_a_cell(tabs) -> None:
    """Qt спрашивает `data` про каждую объявленную колонку — на каждой роли.

    Проверка написана после отказа: `columnCount` считал заголовки **выгрузки**,
    у выгрузки появилась одиннадцатая колонка, и обе таблицы валились
    `IndexError` на первой же отрисовке. Падало не в journals.py, а внутри
    Qt-обёртки `rowCount`, и разобрать это по трассировке было нельзя.
    """
    roles = (
        Qt.ItemDataRole.DisplayRole,
        SORT_ROLE,
        Qt.ItemDataRole.TextAlignmentRole,
        Qt.ItemDataRole.ForegroundRole,
        Qt.ItemDataRole.ToolTipRole,
    )
    for model in (tabs.trades_model, tabs.decisions_model):
        assert model.rowCount() > 0, "модель пуста — проверка вакуумна"
        assert model.columnCount() > 0
        for row in range(model.rowCount()):
            for column in range(model.columnCount()):
                for role in roles:
                    model.data(model.index(row, column), role)


def test_window_columns_are_the_beginning_of_the_export_columns(tabs) -> None:
    """Одно название колонки — одна запись. Две разошлись бы молча.

    Выгрузка длиннее окна, и это намеренно: важность в окне показана цветом,
    происхождение прогона — не показано вовсе, окно показывает журнал одного
    прогона за раз (решение 0011). Но общие колонки обязаны называться
    одинаково, иначе одно и то же поле в окне и в файле зовётся по-разному.

    ⚠️ Заголовки сделок берутся **под те же строки**, что в модели: подпись
    колонки результата зависит от них (чистый результат или валовый).
    Сравнение с неизменной константой прошло бы мимо ровно того случая,
    ради которого подпись и сделана переменной.
    """
    for model, headers in (
        (tabs.trades_model, trade_headers(tabs.trades_model.rows())),
        (tabs.decisions_model, DECISION_HEADERS),
    ):
        shown = [
            model.headerData(column, Qt.Orientation.Horizontal)
            for column in range(model.columnCount())
        ]
        assert shown == list(headers[: model.columnCount()])
        assert model.columnCount() <= len(headers), (
            "модель объявила колонок больше, чем есть заголовков: "
            "шапка таблицы упрётся в IndexError"
        )
    assert TRADE_HEADERS[-1] == "Происхождение"
    assert DECISION_HEADERS[-1] == "Происхождение"


# -- чистка секретов в выгрузке ----------------------------------------------
#
# Строки журнала приезжают в окно сигналами порта, **минуя базу**, — значит
# минуя `CandleStore(..., sanitize=...)`, единственное место, где чистка
# стояла. Выгрузка уезжает файлом и пересылается в переписку; токен внутри
# неё — это доступ к счёту. Разбор — в шапке `ui/export.py`.

#: Приметная подделка формы JWT. Тот же приём и то же значение, что
#: в `test_app_journal_records` и `test_market_journal`: одно и то же
#: в трёх местах читается как одно и то же.
FAKE_JWT = "eyJGQUtF-NOT-A-REAL.eyJGQUtF-NOT-A-REAL.FAKE-NOT-A-REAL-TOKEN"

#: Формы, в которых токен приходит от сервера и попадает в текст отказа
#: исполнителя, а оттуда — в причину решения (`app/port.py` кладёт `{error}`
#: в строку журнала целиком).
LEAKY_TEXTS = (
    f'Отказ брокера: {{"access_token": "{FAKE_JWT}"}}',
    f"Authorization: Bearer {FAKE_JWT}",
    f"refresh_token={FAKE_JWT}&grant_type=x",
    f"в ответе пришло {FAKE_JWT}",
)


@pytest.mark.parametrize("leaky", LEAKY_TEXTS)
def test_the_export_cleans_every_text_cell(tmp_path: Path, leaky: str) -> None:
    """Чистка идёт по всем ячейкам и заголовкам, а не по выбранным колонкам.

    Список «а эти колонки чистые» здесь не заводится намеренно: в `market/`
    такой список уже оказывался неверным сразу в семи местах. Поэтому токен
    подставляется **в каждую** ячейку разом, включая шапку.
    """
    table = [[leaky, leaky], [leaky, leaky]]
    target = write_csv(tmp_path / "everything.csv", (leaky, leaky), table, redact)
    raw = target.read_text(encoding="utf-8-sig")
    assert "eyJGQUtF" not in raw, f"токен уехал в выгрузку: {raw}"
    assert SECRET_MASK in raw, "чистка не сработала вовсе — маски в файле нет"


@pytest.mark.parametrize("leaky", LEAKY_TEXTS)
def test_the_window_cleans_the_journals_it_writes(qapp, tmp_path: Path, leaky: str) -> None:
    """Тот же путь, по которому идёт владелец счёта: кнопка «Выгрузить оба».

    Проверка через окно, а не через `write_csv`: дыра была именно на этом
    шве — окно брало строки из своих моделей и отдавало их в файл, не позвав
    ничего. Чистка самой функции этого бы не поймала.
    """
    trades = [dataclasses.replace(one, exit_reason=leaky) for one in synthetic.trades()]
    decisions = [
        dataclasses.replace(one, event=leaky, reason=leaky) for one in synthetic.decisions()
    ]
    written = _exported(qapp, tmp_path, trades, decisions)

    for path in written:
        raw = path.read_text(encoding="utf-8-sig")
        assert "eyJGQUtF" not in raw, f"токен уехал в {path.name}: {raw}"
    assert any(SECRET_MASK in path.read_text(encoding="utf-8-sig") for path in written), (
        "маски нет ни в одном файле — чистка не сработала, а проверка вакуумна"
    )


def test_writing_a_file_without_a_cleaner_is_impossible() -> None:
    """У `sanitize` нет умолчания, и появиться оно не должно.

    Умолчание «ничего не делать» вернуло бы прежнее устройство: обещание
    вместо механизма, только записанное кодом. Забытый параметр обязан
    падать на месте вызова, а не тихо выгружать сырой текст.
    """
    with pytest.raises(TypeError):
        write_csv("must-not-be-created.csv", ("Причина",), [["строка"]])  # type: ignore[call-arg]  # ровно это и проверяется
    assert not Path("must-not-be-created.csv").exists(), "файл всё-таки создан"


# -- номера колонок связаны с описанием колонок -------------------------------
#
# Номера в `data` были голыми числами: `(3, 4, 5, 7, 8, 9)`, `(7, 8)`, `== 6`.
# На конце списка такой шов падает громко (03.09.2026, 247 упавших тестов),
# а при вставке колонки **в середину** — не падает вовсе: номера остаются
# допустимыми, и выравнивание с раскраской молча уезжают на соседнюю колонку.

#: Что должно быть выровнено вправо — **по названию**, а не по номеру.
#: Разряды в колонке сравнивают глазом, и левое выравнивание это ломает.
#: ⚠️ Колонка результата названа здесь **нейтральной** подписью — той, что
#: лежит в константе `TRADE_HEADERS`. Точную («за вычетом комиссии» или «до
#: вычета») даёт `trade_headers` по самим строкам, и она тут ни при чём:
#: выравнивание и цвет привязаны к номеру колонки, а не к её словам.
RIGHT_ALIGNED = {
    "Объём", "Цена входа", "Цена выхода", "Результат сделки, ₽", "Движение цены, %", "Комиссия, ₽",
}

#: Что красится по знаку: прибыль и убыток должны различаться до чтения цифры.
COLOURED_BY_SIGN = {"Результат сделки, ₽", "Движение цены, %"}


def test_alignment_and_colour_belong_to_named_columns_not_to_numbers() -> None:
    """Числа в `data` заменены описанием — и описание совпадает с названиями.

    Вставка колонки в середину сдвигает названия вместе с номерами, и эта
    проверка падает. Голые номера в `data` не падали.
    """
    from ui.journals import (
        _DECISION_WIDE,
        _TRADE_CELLS,
        _TRADE_NUMERIC,
        _TRADE_SIGNED,
        _TRADE_WIDE,
    )

    assert {TRADE_HEADERS[index] for index in _TRADE_NUMERIC} == RIGHT_ALIGNED
    assert {TRADE_HEADERS[index] for index in _TRADE_SIGNED} == COLOURED_BY_SIGN
    assert TRADE_HEADERS[_TRADE_WIDE] == "Причина выхода"
    assert DECISION_HEADERS[_DECISION_WIDE] == "Причина"
    # Колонок в окне десять: четыре текстовых (вход, выход, сторона, причина)
    # и шесть числовых. Появилась одиннадцатая — список выше надо пересмотреть,
    # а не молча оставить.
    assert len(_TRADE_CELLS) == 10, (
        f"колонок стало {len(_TRADE_CELLS)} — списки RIGHT_ALIGNED "
        "и COLOURED_BY_SIGN не пересмотрены"
    )


def test_the_model_really_uses_that_description(tabs) -> None:
    """Описание не декорация: выравнивание и цвет спрашиваются у него.

    Без этой проверки поля `numeric` и `signed` могли бы остаться мёртвыми,
    а в `data` вернуться числа — и предыдущий тест остался бы зелёным.
    """
    from ui.journals import _TRADE_NUMERIC, _TRADE_SIGNED

    model = tabs.trades_model
    assert model.rowCount() > 0, "модель пуста — проверка вакуумна"
    for column in range(model.columnCount()):
        index = model.index(0, column)
        aligned = model.data(index, Qt.ItemDataRole.TextAlignmentRole) is not None
        assert aligned is (column in _TRADE_NUMERIC), TRADE_HEADERS[column]
        coloured = model.data(index, Qt.ItemDataRole.ForegroundRole) is not None
        assert coloured is (column in _TRADE_SIGNED), TRADE_HEADERS[column]


def test_a_table_without_exactly_one_wide_column_is_refused() -> None:
    """Ноль или две тянущихся колонки — отказ при сборке, а не пустое поле справа.

    Таблица без тянущейся колонки выглядит рабочей: просто справа остаётся
    пустота. Такое не замечают.
    """
    from ui.journals import _Column, _wide_column

    plain = _Column[str](text=str, sort=str)
    wide = _Column[str](text=str, sort=str, wide=True)
    assert _wide_column((plain, wide)) == 1
    for broken in ((plain, plain), (wide, wide)):
        with pytest.raises(ValueError, match="ровно одна"):
            _wide_column(broken)


# -- отказ выгрузки: он обязан быть виден -------------------------------------
#
# Находка `/risk` 03.09.2026, помеченная красным. Каталог с правами `0500`
# давал `PermissionError`, ни одного файла не создавалось, а строка состояния
# **не менялась**: в ней с запуска висит подпись отрисовщика. Исключение
# из слота PySide6 глотает и печатает трассировку в консоль, которой
# у собранной программы нет. Владелец счёта видел ровно то же, что до нажатия,
# и уходил с уверенностью, что отчёт за день у него есть. По числу сделок
# в этом отчёте он выбирает объём (решение 0011).


class _Refusal:
    """Перехваченное модальное окно: было ли оно и что в нём написано."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, _parent, title: str, text: str, *args, **kwargs) -> object:
        self.calls.append((title, text))
        return QMessageBox.StandardButton.Ok

    @property
    def text(self) -> str:
        assert self.calls, "модального окна не было — отказ прошёл молча"
        return self.calls[-1][1]


@contextlib.contextmanager
def _window(qapp, trades=None, decisions=None) -> Iterator[main_window.MainWindow]:
    """Собранное окно с журналами — одно место сборки на все проверки выгрузки.

    Общее намеренно: путь владельца счёта — кнопка «Выгрузить оба журнала»,
    и проверять его надо целиком, вместе со сборкой окна. Две копии сборки
    разошлись бы, и одна из проверок перестала бы проверять то, что думает.

    Тип возврата назван не для красоты: без него `window` внутри `with`
    становится `Any`, и `mypy` теряет проверку всего, что через окно идёт.
    """
    from helpers import RecordingPort

    window = main_window.MainWindow(port=RecordingPort(), sanitize=redact)
    window._timer.stop()
    try:
        window.set_trades(synthetic.trades() if trades is None else trades, synthetic.summary())
        window.set_decisions(synthetic.decisions() if decisions is None else decisions)
        yield window
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def _breaking_writer(fail_on: int, error: BaseException):
    """Подставка `write_csv`: пишет настоящие файлы и падает на `fail_on`-м.

    Настоящие, а не пустышки: проверяется именно откат — то, что записанная
    половина отчёта с диска убрана. Подставка без файла оставила бы каталог
    пустым сама по себе, и проверка была бы вакуумной.
    """
    calls: list[Path] = []

    def write(target, headers, rows, sanitize):
        calls.append(Path(target))
        if len(calls) == fail_on:
            raise error
        Path(target).write_text("строка отчёта\n", encoding="utf-8-sig")
        return Path(target)

    write.calls = calls  # type: ignore[attr-defined]  # список вызовов нужен проверке
    return write


def _no_space() -> OSError:
    return OSError(errno.ENOSPC, "No space left on device")


def test_a_refused_export_is_told_in_a_window(qapp, tmp_path: Path, monkeypatch) -> None:
    """Настоящий отказ файловой системы: каталог только на чтение.

    Воспроизведение находки один в один. Проверяется не текст сообщения,
    а сам факт: после нажатия в окне что-то изменилось.
    """
    if getattr(os, "geteuid", lambda: 1)() == 0:
        pytest.skip("под root права 0500 не мешают записи — отказ не воспроизводится")
    folder = tmp_path / "readonly"
    folder.mkdir()
    folder.chmod(0o500)
    refusal = _Refusal()
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(refusal))
    try:
        with _window(qapp) as window:
            before = window.statusBar().currentMessage()
            written = window.export_journals_to(folder)
            after = window.statusBar().currentMessage()
    finally:
        folder.chmod(0o700)

    assert written == [], "выгрузка отчиталась файлами, которых нет"
    assert refusal.calls, "отказ прошёл молча: модального окна не было"
    assert refusal.calls[-1][0] == "Журналы не выгружены"
    assert "нет прав на запись" in refusal.text, refusal.text
    assert after != before, "строка состояния не изменилась — на экране всё как до нажатия"
    assert "не выгружены" in after
    assert list(folder.iterdir()) == [], "в каталоге что-то осталось"


def test_the_refusal_speaks_words_not_a_traceback(qapp, tmp_path: Path, monkeypatch) -> None:
    """Человеку — причина, коду ошибки место в техническом логе (ТЗ §4.6).

    `PermissionError`, `[Errno 13]` и `Traceback` — это не сообщение
    владельцу счёта, а содержимое консоли, которой у собранной программы нет.
    """
    refusal = _Refusal()
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(refusal))
    monkeypatch.setattr(
        main_window, "write_csv", _breaking_writer(1, PermissionError(errno.EACCES, "Denied"))
    )
    with _window(qapp) as window:
        window.export_journals_to(tmp_path)

    text = refusal.text
    for technical in ("Traceback", "Errno", "PermissionError", "OSError", "File \""):
        assert technical not in text, f"в окно уехала техническая подробность «{technical}»: {text}"
    assert "нет прав на запись" in text


def test_a_failed_export_leaves_no_half_report(qapp, tmp_path: Path, monkeypatch) -> None:
    """Первый файл записался, второй упал — на диске не должно остаться ничего.

    Половина отчёта выглядит как целый: журнал сделок открывается, читается
    и считается за отчёт за день. Число сделок в нём — то самое, по которому
    владелец счёта выбирает объём.
    """
    refusal = _Refusal()
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(refusal))
    writer = _breaking_writer(2, _no_space())
    monkeypatch.setattr(main_window, "write_csv", writer)
    with _window(qapp) as window:
        written = window.export_journals_to(tmp_path)

    assert written == []
    assert len(writer.calls) == 2, "второй файл даже не начинали писать — проверка вакуумна"
    assert sorted(p.name for p in tmp_path.iterdir()) == [], (
        "на диске осталась половина отчёта: " + ", ".join(p.name for p in tmp_path.iterdir())
    )
    assert "на диске не осталось места" in refusal.text
    assert "Ни одного файла не создано" in refusal.text


def test_a_failed_export_does_not_delete_what_was_there_before(
    qapp, tmp_path: Path, monkeypatch
) -> None:
    """Откат трогает только свои файлы, а не прежнюю выгрузку.

    Имя считается по минуте, поэтому вторая выгрузка в ту же минуту попадает
    в те же имена. Удалить в ответ на свою неудачу файл, который владелец
    счёта уже забрал, — та же потеря отчёта, только с другой стороны.

    Обе минуты создаются заранее: минута может смениться посреди проверки,
    и тогда имена были бы другими, а проверка — вакуумной.
    """
    now = datetime.now(MSK)
    for shift in (0, 1):
        stamp = (now + timedelta(minutes=shift)).strftime(main_window.STAMP_FORMAT)
        for kind in ("trades", "decisions"):
            (tmp_path / f"{kind}_{stamp}.csv").write_text("прежняя выгрузка", encoding="utf-8")

    refusal = _Refusal()
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(refusal))
    writer = _breaking_writer(2, _no_space())
    monkeypatch.setattr(main_window, "write_csv", writer)
    with _window(qapp) as window:
        assert window.export_journals_to(tmp_path) == []

    survivor = writer.calls[1]
    assert survivor.exists(), f"откат снёс чужой файл {survivor.name}"
    assert survivor.read_text(encoding="utf-8") == "прежняя выгрузка"
    assert writer.calls[0].exists(), f"откат снёс чужой файл {writer.calls[0].name}"
    assert survivor.name in refusal.text, "в сообщении не названо, что осталось в каталоге"
    assert "Целым отчётом их считать нельзя" in refusal.text
    # Утверждение обязано быть верным в обоих случаях: файл мог и остаться
    # от прежней выгрузки, и быть переписан той, что сейчас не удалась.
    # «Они лежали там до выгрузки» было ложью для первого из двух файлов.
    assert "переписаны той, что сейчас не удалась" in refusal.text


def test_an_unexpected_failure_is_reported_too(qapp, tmp_path: Path, monkeypatch) -> None:
    """Не только отказ файловой системы. Молчание хуже широкого перехвата.

    Сюда ведёт кнопка, а не библиотечный вызов: всё, что не поймано в слоте,
    PySide6 печатает в консоль и глотает.
    """
    refusal = _Refusal()
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(refusal))
    monkeypatch.setattr(main_window, "write_csv", _breaking_writer(1, RuntimeError("сбой")))
    with _window(qapp) as window:
        written = window.export_journals_to(tmp_path)

    assert written == []
    assert refusal.calls, "неожиданный отказ прошёл молча"
    assert "сбой" not in refusal.text, "в окно уехал текст исключения вместо человеческой фразы"


@pytest.mark.parametrize("leaky", LEAKY_TEXTS)
def test_the_words_of_the_system_are_cleaned_before_the_window(
    qapp, tmp_path: Path, monkeypatch, leaky: str
) -> None:
    """Причина от операционной системы — текст, которого этот слой не составлял.

    На редком коде она уходит в окно как есть (`write_failure`, последнее
    средство), а окно отказа — такой же выход наружу, как файл: скриншот
    пересылают. Взят `EIO` — код, которого нет в таблице, поэтому в сообщение
    попадает именно текст системы.
    """
    refusal = _Refusal()
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(refusal))
    monkeypatch.setattr(main_window, "write_csv", _breaking_writer(1, OSError(errno.EIO, leaky)))
    with _window(qapp) as window:
        window.export_journals_to(tmp_path)
        status = window.statusBar().currentMessage()

    assert "eyJGQUtF" not in refusal.text, f"токен уехал в окно отказа: {refusal.text}"
    assert "eyJGQUtF" not in status, f"токен уехал в строку состояния: {status}"
    assert SECRET_MASK in refusal.text, "маски нет — чистка не сработала, проверка вакуумна"


def test_a_secret_in_the_chosen_path_is_cleaned_too(qapp, tmp_path: Path, monkeypatch) -> None:
    """Второй кусок чужого текста в сообщении — путь, выбранный человеком.

    Каталог называет владелец счёта, и в имени может оказаться что угодно.
    Проверка держит `self._sanitize` на месте: без него сообщение об отказе
    стало бы новым выходом наружу мимо обоих рубежей.
    """
    refusal = _Refusal()
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(refusal))
    monkeypatch.setattr(main_window, "write_csv", _breaking_writer(1, _no_space()))
    folder = tmp_path / FAKE_JWT
    folder.mkdir()
    with _window(qapp) as window:
        window.export_journals_to(folder)

    assert "eyJGQUtF" not in refusal.text, f"токен уехал в окно отказа: {refusal.text}"
    assert SECRET_MASK in refusal.text, "маски нет — чистка не сработала, проверка вакуумна"


# -- причина отказа: таблица кодов, а не цепочка ветвлений --------------------


@pytest.mark.parametrize(("name", "reason"), main_window.WRITE_REASONS)
def test_every_known_code_has_words(name: str, reason: str) -> None:
    """Каждая строка таблицы работает: код на входе — фраза на выходе."""
    code = getattr(errno, name, None)
    if code is None:
        pytest.skip(f"кода {name} на этой системе нет")
    assert main_window.write_failure(OSError(code, "raw text")) == reason
    assert "raw text" not in reason


def test_no_reason_shows_a_code_to_the_owner() -> None:
    """`ERR_...`, `Errno` и цифры кодов — в технический лог, не в окно."""
    for _, reason in main_window.WRITE_REASONS:
        assert "Errno" not in reason
        assert "ERR_" not in reason
        assert not any(char.isdigit() for char in reason), reason


def test_an_unknown_code_still_says_something() -> None:
    """Редкий отказ не превращается в «неизвестную ошибку», то есть в молчание.

    Текст системы оставлен последним средством: без него ни владелец счёта,
    ни поддержка не узнают о случившемся ничего.
    """
    text = main_window.write_failure(OSError(errno.EIO, "Input/output error"))
    assert "Input/output error" in text


class _SharingViolation(PermissionError):
    """Подделка windows-отказа «файл занят другой программой».

    ⚠️ Это **подделка, а не проверка на Windows**. Машины с Windows и Excel
    у меня нет. На POSIX четвёртый аргумент `OSError` становится `filename2`,
    а не `winerror`, поэтому настоящий отказ здесь не собрать — собран объект
    с тем же полем. Номер 32 (`ERROR_SHARING_VIOLATION`) взят из `winerror.h`.
    Что Excel держит открытый CSV именно так — не проверено.
    """

    winerror = 32


def test_a_file_held_by_another_program_names_excel() -> None:
    """Занятый файл и запрет на каталог — разные беды с одним `errno`.

    Windows отдаёт оба как `EACCES`, и различает их только `winerror`.
    «Нет прав на запись в этот каталог» на занятом файле отправило бы
    владельца счёта чинить права там, где надо закрыть Excel.
    """
    text = main_window.write_failure(_SharingViolation(errno.EACCES, "Permission denied"))
    assert "Excel" in text
    assert "нет прав" not in text


# ------------------- подпись колонки результата и оговорка под итогом
#
# Заголовок «Результат за вычетом комиссии, ₽» стоял безусловно, а в колонке
# лежит `net` при заданном тарифе и `gross` при незаданном. Тариф стирается
# штатно — поле комиссии в окне имеет значение «не задан». В прогоне владельца
# счёта 05.09.2026 расхождение валовой и чистой — 4 004 ₽ на 143 сделках.


def _trade(commission: float | None) -> TradeRow:
    """Одна сделка с заданной комиссией. `None` — тариф не задан."""
    moment = datetime(2026, 6, 19, 10, 5, tzinfo=MSK)
    return TradeRow(
        entry_time=moment,
        exit_time=moment + timedelta(minutes=15),
        side=Side.LONG,
        volume=1.0,
        entry_price=100_000.0,
        exit_price=100_500.0,
        exit_reason="тейк-профит",
        profit_rub=500.0,
        profit_pct=0.5,
        commission_rub=commission,
    )


def test_the_result_column_says_whether_commission_is_deducted(tabs) -> None:
    """Подпись колонки результата отвечает тому, что в ней лежит.

    Три случая означают разное, и одна подпись на все три обязана врать
    в двух из них.
    """
    net_only = [_trade(28.0), _trade(28.0)]
    gross_only = [_trade(None), _trade(None)]
    mixed = [_trade(28.0), _trade(None)]

    tabs.set_trades(net_only)
    assert "за вычетом комиссии" in _result_header(tabs)

    tabs.set_trades(gross_only)
    assert "ДО вычета" in _result_header(tabs), (
        "тариф не задан, в колонке валовая — а подпись обещает чистую"
    )

    tabs.set_trades(mixed)
    assert "часть строк без комиссии" in _result_header(tabs)


def _result_header(tabs) -> str:
    """Подпись колонки результата так, как её видит шапка таблицы."""
    shown = tabs.trades_model.headerData(RESULT_COLUMN, Qt.Orientation.Horizontal)
    assert isinstance(shown, str), f"шапка отдала не текст: {shown!r}"
    return shown


def test_the_result_column_has_a_tooltip_that_explains_the_gap(tabs) -> None:
    """У заголовка есть подсказка, и она меняется вместе с содержимым колонки."""
    tabs.set_trades([_trade(None)])
    gross = tabs.trades_model.headerData(
        RESULT_COLUMN, Qt.Orientation.Horizontal, Qt.ItemDataRole.ToolTipRole
    )
    assert gross and "НЕ вычтена" in gross

    tabs.set_trades([_trade(28.0)])
    net = tabs.trades_model.headerData(
        RESULT_COLUMN, Qt.Orientation.Horizontal, Qt.ItemDataRole.ToolTipRole
    )
    assert net and net != gross, "подсказка не заметила смены содержимого колонки"


def test_a_row_arriving_later_repaints_the_header(tabs) -> None:
    """Сделка без комиссии, дописанная в таблицу чистых, **перерисовывает** шапку.

    ⚠️ Проверяется сигнал, а не только значение. `headerData` пересчитает
    подпись при любом вызове — но таблица зовёт его лишь тогда, когда модель
    сказала `headerDataChanged`. Без сигнала на экране осталась бы прежняя
    подпись: она соврала бы ровно в тот момент, когда стала неправдой,
    и прогон при этом был бы зелёным.
    """
    repainted: list[tuple] = []
    tabs.trades_model.headerDataChanged.connect(
        lambda orientation, first, last: repainted.append((orientation, first, last))
    )
    tabs.set_trades([_trade(28.0)])
    assert "за вычетом комиссии" in _result_header(tabs)

    tabs.append_trade(_trade(28.0))
    assert not repainted, (
        "шапка перерисовывается на каждой строке — сигнал не смотрит на смысл"
    )

    tabs.append_trade(_trade(None))
    assert "часть строк без комиссии" in _result_header(tabs)
    assert repainted, "модель не сказала таблице перерисовать шапку"
    assert repainted[-1][1] <= RESULT_COLUMN <= repainted[-1][2], (
        f"перерисована не та колонка: {repainted[-1]}"
    )


def test_the_file_and_the_window_call_the_column_the_same(tabs) -> None:
    """Выгрузка и окно берут подпись из одного места."""
    rows = [_trade(None), _trade(None)]
    tabs.set_trades(rows)
    headers, _ = trades_table(rows)
    assert headers[RESULT_COLUMN] == _result_header(tabs)


CAVEAT = "⚠️ Это расчёт, а не выписка со счёта.\n• Проверочная строка."


def test_the_summary_is_shown_with_the_caveat_beside_it(tabs) -> None:
    """Итог показывается вместе с оговоркой, чем он отличается от выписки.

    До 05.09.2026 оговорка считалась (`backtest.headline`) и никуда
    не выводилась: владелец счёта видел «+9 908 ₽» и ни слова о том, что
    проскальзывание в этой цифре нулевое.
    """
    tabs.set_summary(dataclasses.replace(synthetic.summary(), headline=CAVEAT))
    assert "выписка со счёта" in tabs.assumptions_label.text(), (
        "оговорка посчитана и никуда не выведена"
    )
    assert "Проверочная строка" in tabs.assumptions_label.text()


def test_without_a_summary_there_is_no_empty_caveat_strip(tabs) -> None:
    """Нет итога — нет и полосы под ним: пустая строка тревоги хуже отсутствующей."""
    tabs.set_summary(None)
    assert tabs.assumptions_label.text() == ""


def test_the_caveat_reaches_the_exported_file(tabs) -> None:
    """Оговорка уезжает и в файл: он живёт дальше сам и складывается с другими."""
    with_note = dataclasses.replace(synthetic.summary(), headline=CAVEAT)
    _, rows = trades_table([_trade(28.0)], with_note)
    flat = [cell for row in rows for cell in row]
    assert "Оговорка к итогу" in flat
    assert any("выписка со счёта" in cell for cell in flat)
