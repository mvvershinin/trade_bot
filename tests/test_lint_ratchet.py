"""Храповик против роста находок `ruff` и `mypy` — не разовая уборка.

Замер 07.09.2026 (задача «CI на GitHub», `.docs/packaging/packaging-expert-ci-…`)
завёл эту проверку: делать CI красным с первого дня хуже, чем не делать вовсе —
на красный перестают смотреть через неделю. Находки — разобранный остаток
с известными причинами (см. комментарии у `[tool.ruff]`/`[tool.mypy]`
в `pyproject.toml`), а не мусор.

Приём тот же, что в `tests/test_function_size.py` — **не второй способ**.
Отличие одно: там список поимённый (функция → строки), здесь число одно
на инструмент, потому что у `ruff`/`mypy` нет устойчивого «имени находки»:
одна и та же ошибка типа сдвигается на другую строку от правки в соседнем
месте файла.

## Число — это запись замера, а не бюджет с запасом

Порог обязан **равняться** сегодняшнему факту. Разошлось в любую сторону —
запись протухла, и прогон об этом говорит.

⚠️ Читающему, которому эта строгость мешает и хочется вернуть «не больше чем»:
именно «не больше чем» здесь и стояло до 09.09.2026, и именно оно породило
`D-097`. Ассиметрия выглядела разумно — падать на росте, молчать на снижении, —
а стоила ровно того, ради чего храповик ставили. Два живых примера на день
правки, оба без злого умысла и оба из обычного хода работы:

* `ruff`: порог 575 записан 07.09, находок к 09.09 — 572. Три штуки починили
  попутно, число не тронули. Храповик спал бы на трёх следующих находках;
* `mypy`: порог подняли 190 → 191 08.09 под находку от общего помощника
  `tests/helpers.py`, а к 09.09 она не воспроизводится. Спал бы на одной.

Первая новая находка — самая дешёвая в починке (автор ещё помнит, что писал)
и самая важная в поимке. Сторож, который её пропускает и просыпается на второй,
не сторож. Лечится это не внимательностью — её и не хватило дважды за двое
суток, — а тем, что расхождение вниз тоже роняет прогон.

## Чем за это заплачено, честно

Трение выросло, и вот где именно.

1. **Попутная починка чужой находки роняет прогон**, пока не поправишь число.
   Цена — одна строка, и отказ печатает её готовой к вставке (`KNOWN_… = N`),
   вместе с путём файла. Гадать не приходится ни секунды.
2. **Исполнителя посреди работы это не трогает.** Обе проверки числа помечены
   `@pytest.mark.slow`, а повседневный цикл — `pytest -m "not slow"`
   (`CLAUDE.md`, `.docs/TESTING.md`). Храповик срабатывает на полном прогоне
   и в CI, то есть в момент фиксации куска — там, где число обязано быть правдой,
   а не черновиком.
3. **Лазейка осталась и стала явной.** Осознанный рост по-прежнему узаконивается
   поднятием числа — но ровно до сегодняшнего факта и с объяснением рядом,
   как и было задумано с самого начала. Запрещено не поднимать, а **оставлять
   выше факта**: запас и есть спящий сторож.

Переключателя вроде `RATCHET_ALLOW_SLACK=1` здесь нет намеренно. Тихий
выключатель воспроизвёл бы `D-097` в чистом виде: его ставят на день, а живёт
он месяц, и никто не видит. Правка числа видна в diff и в ревью — это и есть
нужный тормоз.

## Устройство

Инструментов два, и всё, чем они отличаются, — данные: имя, имя константы,
записанное число, команда для человека, способ сосчитать. Поэтому таблица
`_TOOLS` плюс один параметризованный тест, а не две почти одинаковые функции
(правило 9 `CLAUDE.md`). Третий инструмент — одна строка таблицы.

Текст отказа собирает отдельная чистая функция `_threshold_mismatch_message`,
и её проверяют быстрые тесты на выдуманных числах. Так сделано ради правила 13
(«молчание — самостоятельный дефект»): проверка «прогон упал» остаётся зелёной
и когда отказ не назвал ни одного числа, — а такой отказ бесполезен ровно тогда,
когда он нужен.

Отдельно от храповика в CI есть два **справочных** шага (`ruff`/`mypy`
с `continue-on-error`) — они показывают список находок в логе. Годится ли
число, решает этот тест, а не цвет того шага.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import subprocess
import sys
from collections.abc import Callable
from typing import Final, NamedTuple

import pytest

ROOT: Final[pathlib.Path] = pathlib.Path(__file__).resolve().parent.parent

#: Путь, который называет отказ. Не подразумевается «сам догадаешься», а
#: печатается: человек читает отказ в логе CI, где текущего каталога не видно.
#: Что путь ведёт именно сюда, проверяет отдельный тест — иначе переименование
#: файла оставило бы в отказе указание на несуществующее место.
THRESHOLD_FILE: Final[str] = "tests/test_lint_ratchet.py"

#: Порог `ruff`. Замер 09.09.2026 на ветке `fix/lint-ratchet-slack`:
#: `ruff check . --output-format=json`, длина массива — 572; та же цифра
#: в строке `Found 572 errors.` обычного вывода.
#: Было 575 (замер 07.09.2026) — три находки починены попутно между датами.
KNOWN_RUFF_FINDINGS: Final[int] = 572

#: Порог `mypy`. Замер 09.09.2026: строка `Found 190 errors in 58 files`,
#: одинаково при тёплом кэше, холодном и с `--no-incremental`.
#: Было 191 (замер 08.09.2026): число подняли под находку «Cannot find
#: implementation or library stub for module "helpers"» от общего помощника
#: `tests/helpers.py`; на 09.09 она не воспроизводится, запас снят.
KNOWN_MYPY_FINDINGS: Final[int] = 190


def _tool_is_installed(module: str) -> bool:
    """`ruff`/`mypy` стоят отдельной группой `lint` (`pyproject.toml`).

    Кто-то мог поставить окружение без неё (`uv sync` без `--all-groups`).
    Это не повод падать: это повод сказать явно, что порог не проверен,
    а не притвориться, что находок ноль — тихий ноль здесь опаснее падения.
    """
    return importlib.util.find_spec(module) is not None


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _ruff_finding_count() -> int:
    """Число находок `ruff` по всему дереву, как его видит `ruff check .`.

    JSON, а не подсчёт строк текстового вывода: у находки бывает несколько
    строк контекста, а формат `--output-format=json` — документированный
    машиночитаемый контракт самого `ruff`, а не наш разбор его текста.
    """
    result = _run(["ruff", "check", ".", "--output-format=json"])
    if result.returncode not in (0, 1):
        pytest.fail(
            f"ruff завершился не находками, а сбоем (код {result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    try:
        return len(json.loads(result.stdout))
    except json.JSONDecodeError:
        pytest.fail(
            f"не удалось разобрать JSON от ruff:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )


_MYPY_ERROR_COUNT = re.compile(r"Found (\d+) error")


def _mypy_finding_count() -> int:
    """Число находок `mypy` по сводной строке его собственного вывода.

    У этой версии `mypy` нет устойчивого машиночитаемого счётчика (`--output`
    принимает произвольный формат, но не документированный `json` для сводки
    по числу ошибок), поэтому разбирается итоговая строка вида
    `Found N errors in M files` — тот же текст, что видит человек, гоняя
    `mypy` руками, и настолько же стабильный формат.
    """
    result = _run(["mypy"])
    if result.returncode not in (0, 1):
        pytest.fail(
            f"mypy завершился не находками, а сбоем (код {result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    if "Success: no issues found" in result.stdout:
        return 0
    match = _MYPY_ERROR_COUNT.search(result.stdout)
    if match is None:
        pytest.fail(f"не удалось разобрать сводку mypy:\n{result.stdout}")
    return int(match.group(1))


class _Tool(NamedTuple):
    """Инструмент под храповиком: всё, чем они отличаются, — данные.

    `constant` хранится строкой, потому что попадает в текст отказа готовым
    присваиванием; что такая константа в модуле действительно есть и держит
    именно `recorded`, проверяет отдельный тест.
    """

    name: str
    constant: str
    recorded: int
    show_command: str
    count_findings: Callable[[], int]


_TOOLS: Final[tuple[_Tool, ...]] = (
    _Tool(
        name="ruff",
        constant="KNOWN_RUFF_FINDINGS",
        recorded=KNOWN_RUFF_FINDINGS,
        show_command=".venv/bin/ruff check .",
        count_findings=_ruff_finding_count,
    ),
    _Tool(
        name="mypy",
        constant="KNOWN_MYPY_FINDINGS",
        recorded=KNOWN_MYPY_FINDINGS,
        show_command=".venv/bin/mypy",
        count_findings=_mypy_finding_count,
    ),
)


def _findings_word(count: int) -> str:
    """Согласование слова «находка» с числом: отказ читает человек, а не машина.

    «Проспит 1 находок» подрывает доверие к тексту ровно там, где он обязан
    быть понят с первого раза, — в красном прогоне.
    """
    if 11 <= count % 100 <= 14:
        return "находок"
    match count % 10:
        case 1:
            return "находку"
        case 2 | 3 | 4:
            return "находки"
        case _:
            return "находок"


def _threshold_mismatch_message(
    name: str,
    constant: str,
    recorded: int,
    actual: int,
    show_command: str,
) -> str | None:
    """Текст отказа, если запись разошлась с фактом, иначе `None`.

    Вынесено из теста отдельной функцией не ради красоты: отказ, не назвавший
    чисел, роняет прогон ровно так же, как отказ полезный, — и проверка
    «прогон упал» этой разницы не видит (правило 13). Здесь же числа
    проверяются быстрыми тестами на выдуманных величинах, без запуска
    инструментов.

    Оба направления расхождения — дефект, но лечатся по-разному, поэтому
    и текста два: рост требует решения (починить или узаконить), зазор
    требует одной правки числа.
    """
    if actual == recorded:
        return None
    paste = f"{constant} = {actual}"
    if actual > recorded:
        grew_by = actual - recorded
        return (
            f"находок {name} стало больше: записано {recorded}, сейчас {actual} "
            f"(+{grew_by}).\n"
            f"Что делать — одно из двух:\n"
            f"  1) починить новые находки, список даёт `{show_command}`;\n"
            f"  2) если рост осознан — поставить `{paste}` в {THRESHOLD_FILE} "
            f"и рядом написать, чем именно он вызван.\n"
            f"Ставить число больше {actual} нельзя: запас пропустит следующую "
            f"находку молча — это дефект D-097, за который проверку и переделали."
        )
    slack = recorded - actual
    return (
        f"порог {name} разошёлся с фактом вниз: записано {recorded}, сейчас {actual} "
        f"({actual - recorded}).\n"
        f"Находок стало меньше, а число в файле осталось вчерашним — и храповик "
        f"теперь молча пропустит {slack} {_findings_word(slack)}. Это дефект D-097, "
        f"а не мелочь: сторож с запасом просыпается со второй находки.\n"
        f"Что делать: поставить `{paste}` в {THRESHOLD_FILE}. Одна строка, "
        f"и она же — запись сегодняшнего замера; проверить число можно "
        f"командой `{show_command}`."
    )


# --------------------------------------------------------------------------
# Быстрые проверки текста отказа. Вход выдуманный, инструменты не запускаются,
# каждый тест работает в одиночку (правило 14).
# --------------------------------------------------------------------------


def test_a_threshold_equal_to_the_fact_is_not_a_refusal() -> None:
    """Совпало — отказа нет: храповик не мешает, пока запись верна."""
    assert (
        _threshold_mismatch_message("ruff", "KNOWN_RUFF_FINDINGS", 572, 572, "ruff check .") is None
    )


def test_the_refusal_on_growth_names_how_much_it_grew() -> None:
    """Выросло — отказ называет обе величины и разницу, а не просто «не сошлось»."""
    message = _threshold_mismatch_message("mypy", "KNOWN_MYPY_FINDINGS", 190, 193, "mypy")
    assert message is not None
    assert "190" in message, f"в отказе нет записанного числа:\n{message}"
    assert "193" in message, f"в отказе нет сегодняшнего числа:\n{message}"
    assert "+3" in message, f"в отказе не сказано, на сколько выросло:\n{message}"


def test_the_refusal_on_growth_offers_the_line_to_paste() -> None:
    """Рост можно узаконить — и отказ даёт готовую строку, а не совет «поднимите»."""
    message = _threshold_mismatch_message("mypy", "KNOWN_MYPY_FINDINGS", 190, 193, "mypy")
    assert message is not None
    assert "KNOWN_MYPY_FINDINGS = 193" in message, (
        f"отказ не даёт готового присваивания с сегодняшним числом:\n{message}"
    )


def test_the_refusal_on_slack_says_how_many_findings_the_guard_would_sleep_through() -> None:
    """Порог выше факта — отказ называет цену зазора: сколько находок пройдёт молча."""
    message = _threshold_mismatch_message("ruff", "KNOWN_RUFF_FINDINGS", 575, 572, "ruff check .")
    assert message is not None
    assert "575" in message, f"в отказе нет записанного числа:\n{message}"
    assert "572" in message, f"в отказе нет сегодняшнего числа:\n{message}"
    assert "3" in message, f"в отказе не сказано, сколько находок проспит сторож:\n{message}"


def test_the_refusal_on_slack_offers_the_lower_number_to_paste() -> None:
    """Зазор чинится одной строкой, и эта строка написана в отказе целиком."""
    message = _threshold_mismatch_message("ruff", "KNOWN_RUFF_FINDINGS", 575, 572, "ruff check .")
    assert message is not None
    assert "KNOWN_RUFF_FINDINGS = 572" in message, (
        f"отказ не даёт готового присваивания с сегодняшним числом:\n{message}"
    )


@pytest.mark.parametrize(
    ("slack", "expected"),
    [(1, "1 находку"), (2, "2 находки"), (5, "5 находок"), (11, "11 находок"), (21, "21 находку")],
)
def test_the_refusal_on_slack_counts_findings_in_readable_russian(
    slack: int, expected: str
) -> None:
    """Отказ читает человек: «пропустит 1 находок» подрывает доверие к тексту."""
    message = _threshold_mismatch_message(
        "ruff", "KNOWN_RUFF_FINDINGS", 572 + slack, 572, "ruff check ."
    )
    assert message is not None
    assert expected in message, f"число и слово не согласованы:\n{message}"


def test_growth_and_slack_do_not_read_as_the_same_refusal() -> None:
    """Два разных дефекта — два разных текста: иначе рост примут за уборку."""
    grown = _threshold_mismatch_message("ruff", "KNOWN_RUFF_FINDINGS", 572, 575, "ruff check .")
    slack = _threshold_mismatch_message("ruff", "KNOWN_RUFF_FINDINGS", 575, 572, "ruff check .")
    assert grown is not None
    assert slack is not None
    assert grown != slack
    assert "стало больше" in grown, f"рост не назван ростом:\n{grown}"
    assert "вниз" in slack, f"зазор не назван зазором:\n{slack}"


@pytest.mark.parametrize("actual", [569, 575])
def test_every_refusal_names_the_file_where_the_number_lives(actual: int) -> None:
    """Отказ читают в логе CI, где текущего каталога не видно, — путь нужен явно."""
    message = _threshold_mismatch_message(
        "ruff", "KNOWN_RUFF_FINDINGS", 572, actual, "ruff check ."
    )
    assert message is not None
    assert THRESHOLD_FILE in message, f"отказ не говорит, какой файл править:\n{message}"


def test_the_file_named_in_the_refusal_is_this_very_file() -> None:
    """Указание в отказе ведёт туда, где действительно лежат числа.

    Ловит переименование или переезд файла: без этого отказ продолжал бы
    отправлять человека по несуществующему пути, и заметили бы это только
    в момент, когда прогон уже красный.
    """
    assert (ROOT / THRESHOLD_FILE).resolve() == pathlib.Path(__file__).resolve()


@pytest.mark.parametrize("tool", _TOOLS, ids=lambda tool: tool.name)
def test_the_constant_named_in_the_refusal_really_exists(tool: _Tool) -> None:
    """Имя из отказа можно вставить, и оно сработает: такая константа в модуле есть.

    Имя хранится в таблице строкой — иначе его не подставить в текст отказа,
    а строка от переименования не защищена: константу переименуют и не заметят,
    что отказ отправляет править несуществующее.

    ⚠️ Сверять `tool.recorded` с самой константой здесь нечего и **намеренно
    не делается**: таблица берёт число у константы (`recorded=KNOWN_…`),
    то есть сравнение было бы тавтологией — проверкой, которая не может упасть.
    Единственное живое утверждение этого теста — что имя не протухло.
    """
    assert hasattr(sys.modules[__name__], tool.constant), (
        f"отказ {tool.name} предлагает вставить `{tool.constant}`, "
        f"а такой константы в {THRESHOLD_FILE} нет"
    )


def test_the_ratchet_watches_both_tools_and_names_them_apart() -> None:
    """Инструмента два, и каждый со своим числом: таблица не выродилась в один."""
    assert [tool.name for tool in _TOOLS] == ["ruff", "mypy"]
    assert len({tool.constant for tool in _TOOLS}) == len(_TOOLS)


# --------------------------------------------------------------------------
# Сам замер. Запускает инструменты подпроцессом — отсюда `slow`.
# --------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.parametrize("tool", _TOOLS, ids=lambda tool: tool.name)
def test_the_recorded_threshold_still_equals_todays_finding_count(tool: _Tool) -> None:
    """Число в файле описывает сегодняшнее дерево — ни больше, ни меньше.

    Больше — появилась новая находка и её никто не заметил. Меньше — запись
    протухла, и храповик держит вчерашний уровень, пропуская первые находки
    молча (`D-097`). Оба случая роняют прогон, потому что оба означают,
    что записанному числу верить нельзя.
    """
    if not _tool_is_installed(tool.name):
        pytest.skip(
            f"{tool.name} не установлен — окружение собрано без группы lint, порог не проверен"
        )
    problem = _threshold_mismatch_message(
        tool.name, tool.constant, tool.recorded, tool.count_findings(), tool.show_command
    )
    assert problem is None, problem
