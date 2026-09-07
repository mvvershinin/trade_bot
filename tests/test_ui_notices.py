"""Тексты предупреждений про деньги: то, за неверность чего платит владелец счёта.

Файл заведён под один конкретный дефект, найденный гейтом `/risk` 30.08.2026
и записанный в `ROADMAP.md` §9.7 красным. Окно в трёх местах обещало, что после
выключения робота «ни тейк, ни переворот к ней больше не применяются». Половина
обещания неверна: тейк-профит — заявка **у брокера** (решение 0008, пункт 2),
движок её при выключении не снимает, и уровень остаётся жить на стороне брокера.

Сценарий, который это стоило: владелец счёта выключает робота, читает окно,
верит и ставит **свой** стоп в приложении брокера. У брокера два. Срабатывает
наш — позиция закрыта; его стоп остаётся и при следующем касании открывает
позицию в обратную сторону на полный объём.

Поэтому тесты здесь проверяют не «функция вернула строку», а содержание:
названный уровень, указание на брокера и **отсутствие** прежнего обещания.
"""

from __future__ import annotations

from dataclasses import replace

from datetime import datetime

from ui.formatting import MSK
from market import redact
from ui.models import (
    DecisionLevel,
    DecisionRow,
    Mode,
    Position,
    RobotState,
    Side,
    TakeGuard,
    TradeRow,
    TradesSummary,
)
from ui.notices import Context, guard_note, halt_note, position_line, unmanaged_note

#: Разделитель тысяч в этой программе — **неразрывный** пробел (`ui/formatting.py`):
#: «211 050» не должно переноситься на две строки посреди числа. Обычный пробел
#: в тесте дал бы вечно красный результат при верном коде.
NBSP = "\u00a0"

#: Обороты, которыми окно раньше отрицало живой стоп у брокера. Ни один
#: из них не должен вернуться ни в один текст про открытую позицию.
DENIALS = (
    "ни тейк",
    "ни тейк-профит",
    "тейк-профит и переворот работать не будут",
    "тейк-профит, ни переворот больше не сработают",
)

ARMED = Position(
    side=Side.LONG,
    volume=1,
    entry_price=210_000.0,
    take=TakeGuard.ARMED,
    take_level=211_050.0,
)


def test_armed_level_is_named_with_its_number() -> None:
    """Уровень называется числом, а не «тейк выставлен».

    Число здесь работа́ет: с ним владелец счёта находит заявку в списке
    у брокера, без него — ищет наугад.
    """
    text = guard_note(ARMED)
    assert f"211{NBSP}050" in text, f"уровень не назван: {text!r}"
    assert "брокер" in text.lower(), f"не сказано, чья это заявка: {text!r}"
    assert "может сработать" in text, f"не сказано, что уровень живой: {text!r}"


def test_armed_level_says_where_to_remove_it() -> None:
    """Сказать «стоп у брокера» и не сказать, как его снять, — полдела."""
    assert "приложении брокера" in guard_note(ARMED)


def test_trailing_level_is_called_by_its_name() -> None:
    text = guard_note(replace(ARMED, take_trailing=True))
    assert "скользящий тейк" in text
    assert f"211{NBSP}050" in text


def test_level_without_a_number_still_warns() -> None:
    """Движок сказал «уровень вооружён», но какой — не сообщил.

    Молчать в этом случае нельзя: предупреждение без числа хуже, чем
    с числом, но несравнимо лучше, чем ничего.
    """
    text = guard_note(replace(ARMED, take_level=None))
    assert "остаётся у брокера" in text
    assert "может сработать" in text


def test_unknown_is_the_default_and_never_claims_there_is_no_stop() -> None:
    """Умолчание — «неизвестно», и оно не утверждает, что стопа нет.

    Самый вероятный способ вернуть дефект: `app/` не заполнит поле. Если бы
    умолчанием было «уровня нет», забытое поле превратилось бы ровно в ту
    неправду, из-за которой всё это написано.
    """
    fresh = Position(side=Side.LONG, volume=1, entry_price=210_000.0)
    assert fresh.take is TakeGuard.UNKNOWN, "умолчание перестало быть безопасным"

    text = guard_note(fresh)
    assert "не может" in text or "не знает" in text, f"окно что-то утверждает: {text!r}"
    assert "не считайте, что её нет" in text
    for denial in DENIALS:
        assert denial not in text.lower()


def test_absent_level_is_said_plainly() -> None:
    """Когда движок сказал «уровня нет» — так и написано, без запугивания."""
    text = guard_note(replace(ARMED, take=TakeGuard.NONE, take_level=None))
    assert "нет" in text
    assert "робот её не закроет" in text
    assert "⚠️" not in text, "спокойное состояние помечено тревогой"


def test_no_text_promises_the_position_will_survive() -> None:
    """«Робот её не закроет» — можно. «Сама она не закроется» — нельзя.

    Второе никто дать не может: биржа пересматривает обеспечение внутри дня,
    и при нехватке свободных средств позицию закрывает брокер — по своей цене
    и в свой момент (DOMAIN.md §5). Владелец счёта, прочитавший это в пятницу
    вечером, уходит на выходные и за обеспечением не следит.
    """
    for position in (
        replace(ARMED, take=TakeGuard.NONE, take_level=None),
        replace(ARMED, take=TakeGuard.UNKNOWN, take_level=None),
        ARMED,
        replace(ARMED, closing=True),
    ):
        for text in (guard_note(position), unmanaged_note(position)):
            assert "сама она не закроется" not in text, f"обещание выживания: {text!r}"
            assert "останется на счёте" not in text, f"обещание выживания: {text!r}"


def test_unknown_trailing_flag_does_not_name_the_level() -> None:
    """Незаполненный признак скользящего не превращается в «неподвижный тейк».

    Умолчание `None` — «движок не сказал». Назвать при нём уровень тейком
    значило бы утверждать, что он стоит там, где его поставили, тогда как
    скользящий мог уехать далеко от цены входа.
    """
    fresh = Position(side=Side.LONG, volume=1, entry_price=210_000.0,
                     take=TakeGuard.ARMED, take_level=211_050.0)
    assert fresh.take_trailing is None, "умолчание перестало быть безопасным"
    text = guard_note(fresh)
    assert "уровень выхода" in text
    assert "тейк " not in text.lower(), f"неизвестный уровень назван тейком: {text!r}"
    assert f"211{NBSP}050" in text


def test_halt_with_a_position_says_what_to_do() -> None:
    """Плашка остановки закрывает собой плашку режима — совет не должен пропасть.

    Совет при этом другой: остановленному роботу переключение режима
    не поможет, помогут только руки у брокера.
    """
    text = halt_note(RobotState(
        halted="Исполнитель не принял заявку", position=ARMED, simulation=False))
    assert "вручную" in text
    assert "переключение режима" in text


def test_pending_exit_order_is_not_merged_with_the_open_state() -> None:
    """Позиция «закрывается» — отдельное состояние, и у него своя строка.

    Заявка на выход уже у брокера, сделки по ней ещё не было. Тейк движок
    к этому моменту снял (решение 0008, пункт 3), то есть уровня нет,
    но позиция может закрыться сама — и два эти утверждения в одном тексте
    не должны противоречить друг другу.
    """
    text = guard_note(replace(ARMED, closing=True, take=TakeGuard.NONE, take_level=None))
    assert "заявка на выход" in text
    assert "может исполниться сама" in text
    assert "сама она не закроется" not in text, (
        "текст одновременно говорит, что позиция может закрыться сама "
        f"и что не закроется: {text!r}"
    )


def test_unmanaged_note_never_promises_that_the_take_stops_working() -> None:
    """Главный запрет: обещание «тейк больше не сработает» не возвращается."""
    for position in (
        ARMED,
        replace(ARMED, take=TakeGuard.UNKNOWN, take_level=None),
        replace(ARMED, take=TakeGuard.NONE, take_level=None),
        replace(ARMED, closing=True),
    ):
        text = unmanaged_note(position).lower()
        for denial in DENIALS:
            assert denial not in text, f"вернулось прежнее обещание {denial!r}: {text!r}"


def test_unmanaged_note_says_what_really_stops() -> None:
    """Что действительно перестаёт работать — переворот и конец окна."""
    text = unmanaged_note(ARMED)
    assert "Переворот" in text
    assert "концу торгового окна" in text
    assert "список заявок" in text, "зовут проверить позицию, но не заявки"


def test_position_line_reads_like_russian() -> None:
    assert position_line(ARMED).startswith("Лонг 1 по ")


def test_halt_note_is_empty_when_the_robot_works() -> None:
    assert halt_note(RobotState()) == ""


def test_halt_note_repeats_the_engine_reason_verbatim() -> None:
    """Причина приходит готовой строкой и не пересказывается окном.

    Окно не знает, почему движок встал, и сочинять причину не вправе:
    в одном из случаев остановки её нет больше нигде — отказал сам
    приёмник журнала.
    """
    reason = "Журнал решений не принял строку: OSError: диск переполнен"
    text = halt_note(RobotState(halted=reason))
    assert reason in text
    assert "остановлен" in text.lower()


def test_a_halt_without_a_reason_is_still_shown() -> None:
    """Флаг есть, причина потерялась — остановку всё равно видно.

    Так будет выглядеть `app/`, заполнивший флаг и не передавший строку.
    Неназванная причина плохо; невидимая остановка — хуже.
    """
    text = halt_note(RobotState(halted="   "))
    assert text, "остановка без причины исчезла с экрана"
    assert "причина не названа" in text


def test_halt_note_with_a_position_points_at_the_orders_too() -> None:
    """Остановка с открытой позицией зовёт проверить и заявки, а не только позицию."""
    text = halt_note(RobotState(
        halted="Сделка не сопоставлена с заявкой", position=ARMED, simulation=False))
    assert f"211{NBSP}050" in text
    assert "список заявок" in text
    assert "Лонг 1" in text


# ==========================================================================
# Сторож на запрещённые обороты — по всему слою окна, а не по одному модулю
# ==========================================================================
#
# Почему сторож переписан 31.08.2026. Обещание про судьбу позиции убрали
# из одного текста, и оно **переехало в соседний**: фраза «и робот её
# не закроет» встала в подсказку панели, где ложна, а «а позиция останется
# на счёте» осталась в `main_window.py`. Прежняя проверка смотрела ровно
# в `guard_note` и `unmanaged_note` — и не видела ни того, ни другого,
# хотя запрещённая подстрока была в файле дословно.
#
# Отсюда правило: проверяется **весь `ui/`**. Два прохода, потому что
# ни один из них по отдельности не полон:
#
# * разбор исходников ловит фразу в любом новом месте, включая модуль,
#   которого ещё нет, — но не видит строк, склеенных из кусков;
# * обход собранного окна ловит склейку и всё, что дошло до экрана, —
#   но только для тех состояний, которые тест перебрал.

import ast
import pathlib
import re

import pytest
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMessageBox, QWidget

UI = pathlib.Path(__file__).resolve().parent.parent / "ui"

#: Обороты, которых в текстах окна быть не должно, и причина запрета.
#: Список именно оборотов, а не смыслов: «логика» не имеет синтаксиса.
#: Полнота держится не на нём, а на том, что он применяется ко всему слою.
PROMISES = (
    (
        re.compile(
            r"(останется|остаётся|останутся|остаются)\s+"
            r"(на\s+счёте|в\s+рынке|у\s+вас|как\s+есть|открыт\w*|нетронут\w*)"
        ),
        "обещание, что позиция доживёт до утра. Биржа пересматривает "
        "обеспечение внутри дня, и при нехватке свободных средств позицию "
        "закрывает брокер — по своей цене и в свой момент (DOMAIN.md §5)",
    ),
    (
        re.compile(r"сама\s+(она\s+)?не\s+закроется|не\s+закроется\s+сама"),
        "то же обещание другими словами",
    ),
    (
        re.compile(r"никто\s+(её|ее)\s+не\s+закроет|ничего\s+с\s+ней\s+не\s+случится"),
        "то же обещание третьими словами",
    ),
    (
        re.compile(r"ни\s+тейк"),
        "прежнее обещание «ни тейк, ни переворот к ней больше не применяются». "
        "Тейк — заявка у брокера (решение 0008), выключение её не снимает",
    ),
    (
        re.compile(
            r"тейк[^.!?]{0,60}"
            r"(работать\s+не\s+буд|больше\s+не\s+сработа|перестанет\s+работать)"
        ),
        "прежнее обещание, что выставленный уровень перестанет действовать",
    ),
)

#: Обороты, которые НЕ запрещены, и почему. Список нужен не коду, а тому,
#: кто в следующий раз захочет расширить `PROMISES` по слову «останется»:
#:
#: * «позиция останется без присмотра» — утверждение про присмотр, а не про
#:   выживание. Оно верное и несущее: ради него заведены три подтверждения.
#: * «позиция при этом осталась открыта» — прошедшее время, отчёт о том,
#:   что пришло в `RobotState`, а не обещание на будущее.
#: * «выставленный тейк остаётся у брокера» — то самое утверждение, ради
#:   которого всё это написано.
#: * «робот её не закроет» — можно, но только там, где робот не управляет;
#:   за обстановкой следит отдельный тест ниже, а не список оборотов.


def promises(text: str) -> list[str]:
    """Какие запрещённые обороты нашлись в тексте. Пустой список — чисто."""
    lowered = " ".join(text.lower().split())
    return [why for rule, why in PROMISES if rule.search(lowered)]


def _literals(path: pathlib.Path) -> list[tuple[int, str]]:
    """Строковые литералы файла, кроме документирующих строк.

    Документирующие строки исключены намеренно: там запрещённый оборот
    почти наверняка процитирован, чтобы объяснить, почему он запрещён —
    ровно как в заголовке `ui/notices.py`. Запрет на цитату превратил бы
    сторожа в помеху объяснению.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docs: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)) or not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            docs.add(id(first.value))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docs
    ]


def test_no_source_in_ui_promises_the_position_will_survive() -> None:
    """Первый проход: запрещённый оборот не встречается ни в одном файле `ui/`.

    Именно этой проверки не было. Прежний сторож смотрел в две функции
    `ui/notices.py`, и фраза, переехавшая в `ui/main_window.py`, прошла мимо
    него, сохранив запрещённую подстроку дословно. Новый файл в `ui/`
    попадает под проверку сам, без правки теста.
    """
    sources = sorted(p for p in UI.rglob("*.py") if "__pycache__" not in p.parts)
    assert len(sources) > 5, "в ui/ почти нет файлов — проверка вакуумна"

    guilty: list[str] = []
    seen = 0
    for path in sources:
        for line, text in _literals(path):
            seen += 1
            for why in promises(text):
                guilty.append(f"{path.relative_to(UI.parent)}:{line} — {why}\n    {text!r}")
    assert seen > 200, f"разобрано подозрительно мало литералов: {seen}"
    assert not guilty, "запрещённый оборот в текстах окна:\n  " + "\n  ".join(guilty)


def test_the_forbidden_phrase_guard_is_not_blind() -> None:
    """Канарейка: сторож ловит фразу и там, где её сегодня нет.

    Без неё опечатка в регулярном выражении сделала бы проверку вечно
    зелёной — третий тавтологичный сторож за сессию был найден именно так.
    """
    for sample in (
        "Робот перестанет ею управлять, а позиция останется на счёте.",
        "Позиция останется открытой до вашего вмешательства.",
        "Сама она не закроется.",
        "Она не закроется сама.",
        "Ни тейк, ни переворот к ней больше не применяются.",
        "Выставленный тейк после выключения работать не будет.",
        "Тейк больше не сработает.",
        "Позиция\nостанется\nна счёте",  # перенос строки сторожа не обманывает
    ):
        assert promises(sample), f"сторож пропустил запрещённый оборот: {sample!r}"

    for sample in (
        "Позиция останется без присмотра.",
        "Позиция при этом осталась открыта: Лонг 1 по 210 000.",
        "Выставленный тейк 211 050 остаётся у брокера и может сработать.",
        "Выставленного уровня выхода нет — сторожить у брокера нечего, "
        "и робот её не закроет.",
        "Скользящий тейк при снятой галочке тоже не работает.",
        "Открытый шорт закроется на ближайшем сигнале.",
    ):
        assert not promises(sample), f"сторож запретил верный текст: {sample!r}"


def _visible_texts(root) -> list[str]:
    """Всё, что человек может прочитать в этом окне.

    Собирается по дереву объектов Qt, а не по списку полей: перечень полей
    устарел бы на первом новом ярлыке — а именно так дефект и переезжает.
    """
    texts: list[str] = []
    for child in root.findChildren(QWidget) + [root]:
        for getter in ("text", "toolTip", "windowTitle", "placeholderText",
                       "title", "statusTip", "whatsThis"):
            method = getattr(child, getter, None)
            if not callable(method):
                continue
            try:
                value = method()
            except TypeError:
                continue
            if isinstance(value, str) and value.strip():
                texts.append(value)
    for action in root.findChildren(QAction):
        texts.extend(v for v in (action.text(), action.toolTip()) if v.strip())
    return texts


#: Наполнение журналов для обхода. Числа взяты произвольно: проверяется
#: не их верность, а то, что строки таблиц тоже попадают под сторожа.
TRADES = (
    TradeRow(
        entry_time=datetime(2026, 6, 19, 10, 10, tzinfo=MSK),
        exit_time=datetime(2026, 6, 19, 10, 25, tzinfo=MSK),
        side=Side.LONG, volume=1, entry_price=210_000.0, exit_price=211_050.0,
        exit_reason="Тейк-профит +0,5%", profit_rub=1022.0, profit_pct=0.5,
        commission_rub=28.0, trade_id="1",
    ),
)
SUMMARY = TradesSummary(
    trades=1, profitable_share=1.0, net_profit_rub=1022.0,
    gross_profit_rub=1050.0, commission_rub=28.0, max_drawdown_rub=0.0, reversals=0,
)
DECISIONS = (
    DecisionRow(
        time=datetime(2026, 6, 19, 10, 10, tzinfo=MSK),
        event="Сигнал",
        reason="Закрытие выше средней EMA(15) → покупка 1 контракта",
        level=DecisionLevel.TRADE,
    ),
)

STATES = [
    pytest.param(mode, running, halted, simulation, take, closing,
                 id=f"{mode.name}-{'run' if running else 'idle'}"
                    f"-{'halt' if halted else 'ok'}"
                    f"-{'sim' if simulation else 'live'}-{take.name}"
                    f"-{'closing' if closing else 'open'}")
    for mode in Mode
    for running in (True, False)
    for halted in ("", "Исполнитель не принял заявку")
    for simulation in (True, False)
    for take in TakeGuard
    for closing in (True, False)
]


@pytest.mark.parametrize(
    "mode, running, halted, simulation, take, closing", STATES
)
def test_no_window_text_promises_the_position_will_survive(
    qapp, monkeypatch, mode, running, halted, simulation, take, closing
) -> None:
    """Второй проход: то же по собранному окну, во всех состояниях подряд.

    Разбор исходников не видит строк, склеенных из кусков, — а все тексты
    про позицию именно такие. Здесь перебираются режим, работа, остановка,
    симуляция, состояние уровня и поданная заявка на выход, и собирается всё,
    что окно способно показать: плашки, подсказки, заголовки, названия
    действий и оба текста подтверждений вместе с их заголовками.
    """
    from ui.main_window import MainWindow
    from ui.settings_dialog import SettingsDialog

    shown: list[str] = []

    def refuse(parent, title, text, *args, **kwargs):
        shown.append(title)
        shown.append(text)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(refuse))

    window = MainWindow(sanitize=redact)
    window._timer.stop()
    try:
        position = Position(
            side=Side.SHORT, volume=1, entry_price=284_812.53,
            closing=closing, take=take,
            take_level=284_400.0 if take is TakeGuard.ARMED else None,
        )
        state = RobotState(
            instrument="MXU6", mode=mode, running=running, halted=halted,
            simulation=simulation, position=position, token_days_left=6,
        )
        window.apply_state(state)
        # Журналы наполняются, иначе итоговая строка и таблицы остаются
        # пустыми и из-под проверки выпадают: дефект переезжает и туда.
        window.set_trades(TRADES, SUMMARY)
        window.set_decisions(DECISIONS)

        texts = _visible_texts(window)
        for model in (window.journals.trades_model, window.journals.decisions_model):
            texts.extend(
                str(model.data(model.index(row, column)) or "")
                for row in range(model.rowCount())
                for column in range(model.columnCount())
            )
        # Оба подтверждения: «Стоп» и закрытие программы. Переключение режима
        # спрашивает тем же `_confirm_unattended`, поэтому третий вызов
        # ничего нового не добавил бы.
        window.request_stop()
        window.close()
        texts.extend(shown)

        dialog = SettingsDialog(parent=window)
        try:
            texts.extend(_visible_texts(dialog))
        finally:
            dialog.deleteLater()

        assert texts, "с экрана не собрано ни одной строки — проверка вакуумна"
        guilty = [
            f"{why}\n    {text!r}" for text in texts for why in promises(text)
        ]
        assert not guilty, "запрещённый оборот на экране:\n  " + "\n  ".join(guilty)
    finally:
        window.apply_state(RobotState())
        window.close()
        window.deleteLater()
        qapp.processEvents()


# ==========================================================================
# Обстановка: одно и то же утверждение верно не везде
# ==========================================================================

def test_a_working_robot_is_never_told_it_will_not_close_the_position() -> None:
    """BLOCK 31.08.2026: «и робот её не закроет» на работающем роботе — ложь.

    Сценарий, из-за которого правка сделана. Рабочая конфигурация
    с выключенным тейком (ею сделаны обе конфигурации-сторожа сверки,
    PROTOTYPE.md §1): движок отдаёт `TakeGuard.NONE`, режим «Лонг и шорт
    (переворот)», робот работает. Панель звала `guard_note` безусловно
    и показывала «сторожить у брокера нечего, и робот её не закроет».

    В этой конфигурации переворот по средней — единственное, что позицию
    закрывает, и он закроет её на ближайшем обратном сигнале. Поверив окну,
    владелец счёта закрывает позицию руками у брокера; сверки позиции
    с брокером в движке нет (этап 2), движок считает позицию открытой
    и на следующем обратном сигнале подаёт рыночный выход — заявка
    открывает позицию в обратную сторону на полный объём, без сигнала
    и без строки в журнале.
    """
    bare = replace(ARMED, take=TakeGuard.NONE, take_level=None)
    text = guard_note(bare, Context(managed=True))
    assert "робот её не закроет" not in text, f"работающему роботу солгали: {text!r}"
    # Безусловная половина утверждения при этом на месте.
    assert "Выставленного уровня выхода у этой позиции нет" in text


def test_a_working_robot_is_not_invited_to_cancel_the_take_by_hand() -> None:
    """Вторая половина той же находки: «снять её можно в приложении брокера».

    Снятой руками заявки движок не заметит: срабатывание тейка приходит ему
    только сделкой (решение 0008). Он будет считать сторожа живым, пока
    позиция идёт без уровня.
    """
    text = guard_note(ARMED, Context(managed=True))
    assert "приложении брокера" not in text, f"позвали снимать заявку: {text!r}"
    # А то, что уровень существует и живёт у брокера, сказано по-прежнему.
    assert "остаётся у брокера" in text
    assert f"211{NBSP}050" in text


def test_the_same_text_without_the_robot_keeps_both_halves() -> None:
    """Обратная сторона: там, где робот не управляет, обе фразы обязаны быть."""
    bare = replace(ARMED, take=TakeGuard.NONE, take_level=None)
    assert "робот её не закроет" in guard_note(bare, Context(managed=False))
    assert "приложении брокера" in guard_note(ARMED, Context(managed=False))


def test_a_forgotten_context_is_the_loud_one() -> None:
    """Умолчание `Context()` — боевой режим без управления, а не наоборот.

    Забытый аргумент обязан давать лишний совет проверить счёт, а не молчание:
    первое стоит человеку минуты, второе — денег. Умолчание намеренно
    не совпадает с умолчанием `RobotState.simulation`, и это проверяется,
    чтобы правка «привести к общему виду» не прошла молча.
    """
    assert Context() == Context(managed=False, simulation=False)
    assert RobotState().simulation is True, "умолчание состояния изменилось"
    bare = replace(ARMED, take=TakeGuard.NONE, take_level=None)
    assert "робот её не закроет" in guard_note(bare)
    assert "приложении брокера" in guard_note(ARMED)


def test_context_of_reads_the_three_flags() -> None:
    """«Робот управляет» записано ровно один раз и читается по трём полям."""
    working = RobotState(mode=Mode.REVERSE, running=True, halted="", simulation=False)
    assert Context.of(working) == Context(managed=True, simulation=False)

    for broken, why in (
        (replace(working, running=False), "робота не запускали"),
        (replace(working, mode=Mode.OFF), "режим «Выключен»"),
        (replace(working, halted="Исполнитель не принял заявку"), "робот остановлен"),
    ):
        assert not Context.of(broken).managed, f"робот считается управляющим: {why}"

    assert Context.of(replace(working, simulation=True)).simulation


def test_unmanaged_note_ignores_a_managed_context() -> None:
    """`unmanaged_note` — текст про «робота больше нет», и обстановку он задаёт сам.

    Зовут его из подтверждений, где робот ещё работает: `Context.of(state)`
    в этот момент говорит «управляет». Разбираться в этом на каждом вызове —
    способ получить четвёртую копию утверждения.
    """
    bare = replace(ARMED, take=TakeGuard.NONE, take_level=None)
    text = unmanaged_note(bare, Context(managed=True, simulation=False))
    assert "робот её не закроет" in text
    assert "Проверьте позицию и список заявок" in text


def test_simulation_never_sends_anyone_to_the_broker() -> None:
    """В симуляции у брокера нет ни позиции, ни заявки — и звать туда нельзя.

    Разработка и первые недели идут на симуляции (это умолчание `RobotState`).
    Два похода к брокеру впустую обесценивают красную плашку к тому дню,
    когда она станет правдой: тревога, повторённая дважды, перестаёт быть
    тревогой.
    """
    simulated = Context(simulation=True)
    for position in (
        ARMED,
        replace(ARMED, take=TakeGuard.NONE, take_level=None),
        replace(ARMED, take=TakeGuard.UNKNOWN, take_level=None),
        replace(ARMED, closing=True),
    ):
        for text in (guard_note(position, simulated), unmanaged_note(position, simulated)):
            assert "приложении брокера" not in text, f"позвали к брокеру зря: {text!r}"
            assert "Проверьте позицию и список заявок" not in text, (
                f"позвали к брокеру зря: {text!r}"
            )
            # Утверждать, что у брокера что-то живёт, тоже нельзя: там пусто.
            # Это тот же поход впустую, только менее прямым текстом.
            for claim in ("остаётся у брокера", "живёт у брокера"):
                assert claim not in text, (
                    f"в симуляции обещан живой сторож у брокера: {text!r}"
                )
        # ⚠️ Оговорка про симуляцию делается **один раз**, в конце текста,
        # и потому проверяется на `unmanaged_note`, а не на каждом
        # предложении. Три «идёт симуляция» подряд в одном абзаце — шум,
        # а абзац читают ради денег.
        assert "симуляц" in unmanaged_note(position, simulated).lower()
        assert unmanaged_note(position, simulated).lower().count("симуляц") == 1, (
            "оговорка про симуляцию повторяется — так и обесценивается "
            f"весь абзац: {unmanaged_note(position, simulated)!r}"
        )


def test_simulation_still_names_the_level_and_its_number() -> None:
    """Не звать к брокеру — не то же самое, что молчать про уровень."""
    text = guard_note(ARMED, Context(simulation=True))
    assert f"211{NBSP}050" in text
    assert "только внутри программы" in text, (
        f"не сказано, где живёт уровень: {text!r}"
    )


def test_simulation_does_not_hide_the_unknown_take() -> None:
    """Умолчание «неизвестно» остаётся громким и в симуляции."""
    text = guard_note(replace(ARMED, take=TakeGuard.UNKNOWN, take_level=None),
                      Context(simulation=True))
    assert "не считайте, что её нет" in text


def test_halt_in_simulation_does_not_send_anyone_to_the_broker() -> None:
    """Плашка остановки — тоже. Она самая крупная и читается первой."""
    for position in (None, ARMED):
        text = halt_note(RobotState(
            halted="Журнал решений не принял строку", position=position, simulation=True))
        assert "приложении брокера" not in text, f"позвали к брокеру зря: {text!r}"
        assert "симуляц" in text.lower(), f"не сказано, что это симуляция: {text!r}"


def test_an_unexpected_trailing_flag_does_not_break_the_panel() -> None:
    """Признак скользящего вне трёх известных значений не роняет показ состояния.

    Прежде здесь стояла индексация словаря, и любое другое значение бросало
    `KeyError` внутри слота показа состояния: окно переставало показывать
    позицию из-за одного неверного поля. Соседнее поле `take` устроено
    безопаснее, и это несоответствие и было дефектом.
    """
    strange = replace(ARMED, take_trailing="да")  # type: ignore[arg-type]
    text = guard_note(strange)
    assert "уровень выхода" in text, f"неизвестный признак назван тейком: {text!r}"
    assert f"211{NBSP}050" in text
