"""Проверка-образец алгоритма №2: те же данные и настройки — тот же список сделок.

Замена сверки с прототипом (решение владельца счёта 05.10.2026). Стережёт:
любая правка движка, торгового модуля или модели исполнения, которая меняет
хотя бы одну сделку, число вооружений тейка или итог в рублях, роняет тест
с первыми различающимися строками. Оракулом образец не является — он говорит
«изменилось», а не «неправильно». Разбор точек и настроек — `tools/golden_deals.py`.

Образец переписывается только командой
`.venv/bin/python -m tools.golden_deals --rewrite --reason "…"`. Тест его
не пишет никогда. Данные и образец в `reference/` — вне git; нет их — пропуск.
"""

from __future__ import annotations

import pathlib

import pytest

from tools import golden_deals
from tools.golden_deals import CASES, Case

NO_ARCHIVE_HINT = (
    "нет данных или образца в reference/ — он в git не попадает, "
    "в свежем клоне проверки-образца нет"
)


def test_every_settings_field_is_named_explicitly() -> None:
    """Стережёт: ни одна настройка точки не берётся из умолчаний класса.

    Новое поле у `EngineSettings`, `Costs`, `Minutes` или настроек алгоритма
    без явного значения в образце значило бы прогон на умолчании, которое
    поменяют завтра, — и образец разошёлся бы без правки логики.
    """
    assert golden_deals.missing_fields() == [], (
        "в tools/golden_deals.py не названы явно поля: "
        f"{golden_deals.missing_fields()}. Назовите значение и перепишите образец "
        "командой с причиной"
    )


def test_a_rewrite_without_a_reason_is_refused(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Стережёт: без причины образец не переписывается и строки журнала нет.

    Каталог образца подставной, а прогон заменён отказом: сломанный отказ
    обязан упасть здесь, а не переписать настоящий образец с мутанта.
    """

    def no_run(case: Case) -> str:
        raise AssertionError(f"без причины дошло до прогона {case.name}")

    monkeypatch.setattr(golden_deals, "GOLDEN", tmp_path)
    monkeypatch.setattr(golden_deals, "JOURNAL", tmp_path / "JOURNAL.md")
    monkeypatch.setattr(golden_deals, "render", no_run)
    assert golden_deals.main(["--rewrite", "--reason", "  "]) == 2
    assert list(tmp_path.iterdir()) == []


def _non_vacuous(case: Case, text: str) -> None:
    """Каждая настройка точки хоть раз сработала — иначе образец стережёт пустое."""
    assert golden_deals.deal_count(text) > 0, f"{case.name}: ни одной сделки"
    assert golden_deals.header(text, "halted") == "-", f"{case.name}: прогон остановлен"
    rows = text.split("\nentry_time\t", 1)[1].splitlines()[1:]
    reasons = {row.split("\t")[6] for row in rows}
    if case.walks_minutes:
        assert "trailing_take" in reasons, f"{case.name}: скользящий уровень не сработал ни разу"
        assert " bars_without_minutes 0 " in golden_deals.header(text, "bars") + " ", (
            f"{case.name}: часть пятиминуток прошла без минуток"
        )
    else:
        assert "take_profit" in reasons, f"{case.name}: тейк не сработал ни разу"
        assert "submitted=0," not in golden_deals.header(text, "limit_exits"), (
            f"{case.name}: выход с предельной ценой не подавался ни разу"
        )


@pytest.mark.slow
@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_the_run_gives_exactly_the_recorded_deals(case: Case) -> None:
    """Стережёт: список сделок, вооружения и итоги точки равны образцу построчно."""
    if golden_deals.missing_inputs() or not case.path.is_file():
        pytest.skip(NO_ARCHIVE_HINT)
    expected = case.path.read_text(encoding="utf-8")
    stale = golden_deals.stale_data(expected, case)
    assert not stale, (
        f"{case.name}: данные изменились, а не логика — sha256 входных файлов "
        f"не тот, что в образце: {stale}"
    )
    actual = golden_deals.render(case)
    diff = golden_deals.difference(expected, actual)
    assert not diff, (
        f"{case.name}: прогон разошёлся с образцом {case.path}. Первые различия:\n"
        f"{diff}\nЕсли изменение намеренное — перепишите образец командой "
        '`.venv/bin/python -m tools.golden_deals --rewrite --reason "…"`'
    )
    _non_vacuous(case, actual)
