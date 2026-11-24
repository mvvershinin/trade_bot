"""Бой 16.11.2026: «Старт», панель во время сборки, причина серой кнопки, линия включения.

Четыре поведения, по сторожу на каждое:

* **B-103** — график показывает цену включения скользящего тейка до порога
  (условная у брокера, `TakeProfit.activation`), после порога — уровень
  программы с оговоркой, что у брокера свой; после выхода линий нет;
* **«Старт» = «Включить бой»** — порт идёт тем же путём `set_live(True)`
  с теми же запретами (`_live_blockers`), отказ `NO_LIVE` больше не звучит;
  кнопка серая с той же причиной в подсказке, что у «Включить бой»,
  и нажатие задаёт тот же вопрос;
* **панель между «Включить бой» и сборкой исполнителя** — «БОЙ: собирается»
  и «заявки пойдут с закрытия свечи» вместо «СИМУЛЯЦИЯ» и «робот стоит»;
* **причина серой «Включить бой» — в технический лог** одной строкой при
  смене набора причин, а не на каждый снимок.

Стенд боя порта — пара движков 2в (`tests/test_app_live_pair.py`): настоящий
порт, исполнитель на `tests/fake_broker.py`, своя база во временном каталоге,
сети нет. Окно — настоящее, порт окна — запоминающая подмена.
"""

from __future__ import annotations

import dataclasses
import logging
import pathlib
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
import websockets

from app.fact import LiveStatus
from app.port import ACTIVATION_LABEL, NO_LIVE, PROGRAM_LEVEL_LABEL, _levels
from engine import MSK, Position, Side, TakeProfit
from market import redact
from tests.fake_broker import FakeBroker
from tests.test_app_live_pair import (
    LIVE_FROM,
    NO_TAKE,
    Bench,
    _bench,
    _close,
    account_read,
)
from ui.main_window import START_HINT, MainWindow
from ui.models import LevelKind, LiveView, RobotState
from ui.ports import TerminalPort
from ui.status_panel import ASSEMBLING_BADGE, ASSEMBLING_MODE, StatusPanel

#: Шорт 16.11.2026 09:05: вход 248 500, порог 0,07 %, шаг цены 25 —
#: включение у брокера 248 325 (записи боя, `userdata/live-watch/2026-11-16-notes.md`).
ENTRY = 248_500.0
ACTIVATION = 248_325.0
PLAN = TakeProfit(
    trailing=True, start_percent=0.07, offset_percent=0.03, step_percent=0.02, price_step=25.0
)
#: Начало причины «счёт не прочитан» — так её пишет порт (`_NO_FUNDS`, `D-044`).
UNREAD = "счёт у брокера ещё не прочитан"
NO_STREAM = "Нет потока котировок: бой идёт поверх живого потока, сначала «Подключиться»."


def _short(take: TakeProfit) -> Position:
    return Position(
        side=Side.SHORT, volume=1.0, entry_price=ENTRY,
        entry_time=datetime(2026, 11, 16, 9, 5, tzinfo=MSK), take=take,
    )


# ---------------------------------------------------------------- B-103


def test_the_chart_shows_where_the_trailing_take_turns_on_then_the_program_level() -> None:
    """Стережёт B-103: до порога — линия включения, после — уровень с оговоркой, после выхода — нет.

    План тейка — арифметикой движка (`initial`, `activation_level`, `advance`),
    принятая условная кладётся так, как её кладёт `pipeline._after_arm`.
    Мутации: не рисовать `activation` — падает первая часть; рисовать её и после
    порога или без оговорки — вторая; оговорка вне боя — третья.
    """
    before = PLAN.initial(ENTRY, -1)
    assert before.level is None, "до порога уровня выхода быть не должно — проверять нечего"
    before = dataclasses.replace(before, activation=before.activation_level(ENTRY, -1))
    assert before.activation == ACTIVATION, "арифметика включения разошлась с боем 16.11"

    lines = _levels(_short(before), live=True)
    assert [(one.kind, one.price) for one in lines] == [
        (LevelKind.ENTRY, ENTRY), (LevelKind.ACTIVATION, ACTIVATION),
    ], lines
    assert lines[1].label == ACTIVATION_LABEL

    after = before.advance(ENTRY, -1, 248_300.0)
    assert after.level is not None and after.activation == ACTIVATION, after
    lines = _levels(_short(after), live=True)
    assert [one.kind for one in lines] == [LevelKind.ENTRY, LevelKind.TRAILING], lines
    assert lines[1].price == after.level
    assert lines[1].label == PROGRAM_LEVEL_LABEL, "после порога не сказано, что у брокера свой"

    # Прогон по истории: брокера нет — и оговорки о нём нет.
    assert _levels(_short(after), live=False)[1].label == LevelKind.TRAILING.label
    assert _levels(None, live=True) == (), "после выхода линии остались"


# ---------------------------------------------------------------- «Старт» в порту


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> FakeBroker:
    fake = FakeBroker()
    monkeypatch.setattr(websockets, "connect", fake.connect)
    return fake


async def _stream_only(bench: Bench) -> None:
    """Поток включён, прогон прошёл — счёт **не** прочитан, бой не просили."""
    bench.exchange.now = LIVE_FROM - timedelta(seconds=58)
    bench.port.stream(True)
    await bench.port.wait()


def test_start_turns_the_live_on_by_the_same_gates(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """Стережёт «Старт» = `set_live(True)`: запрет боя отказывает «Старту», снятый — включает.

    Мутации: прежний отказ `NO_LIVE` — вторая половина (бой не включился)
    и первая (в отказе нет причины запрета); «Старт» мимо `_live_blockers` —
    первая (бой включился без снимка счёта).
    """

    async def go() -> tuple[str, bool, bool]:
        bench, session = await _bench(tmp_path, broker, NO_TAKE)
        try:
            await _stream_only(bench)
            bench.port.start()
            await bench.port.wait()
            refused = " ".join(
                reason for event, reason in bench.notes if event == "Бой не включён"
            )
            wanted = bench.port.live_status().wanted
            account_read(bench)
            bench.port.start()
            await bench.port.wait()
            return refused, wanted, bench.port.live_status().on
        finally:
            await _close(bench, session)

    refused, wanted, on = loop.run_until_complete(go())
    assert UNREAD in refused.lower(), f"«Старт» отказал не причиной запрета боя: «{refused}»"
    assert NO_LIVE not in refused
    assert not wanted, "«Старт» включил бой при запрете"
    assert on, "запрет снят, а «Старт» бой не включил"


def test_the_live_blocker_goes_to_the_log_once_per_change(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, caplog: pytest.LogCaptureFixture
) -> None:
    """Стережёт строку техлога о причине серой «Включить бой»: при смене — да, на опрос — нет.

    Мутации: строки нет — падает «причина не записана»; строка на каждый
    снимок — падает «повторные опросы»; не сказать «доступна» — последняя.
    """

    def said() -> list[str]:
        return [
            record.getMessage() for record in caplog.records
            if record.name == "app.port" and "«Включить бой»" in record.getMessage()
        ]

    async def go() -> tuple[list[str], list[str], list[str]]:
        bench, session = await _bench(tmp_path, broker, NO_TAKE)
        try:
            await _stream_only(bench)
            first = said()
            caplog.clear()
            for _ in range(3):
                bench.port.live_status()
            repeated = said()
            account_read(bench)
            bench.port.live_status()
            return first, repeated, said()
        finally:
            await _close(bench, session)

    with caplog.at_level(logging.INFO, logger="app.port"):
        first, repeated, freed = loop.run_until_complete(go())
    assert any(UNREAD in line.lower() for line in first), f"причина не записана: {first}"
    assert repeated == [], f"повторные опросы с той же причиной пишут строку: {repeated}"
    assert freed == ["«Включить бой» доступна"], freed


# ---------------------------------------------------------------- окно


class StartPort(TerminalPort):
    """Порт окна, запоминающий «Старт» и «Бой»."""

    def __init__(self) -> None:
        super().__init__()
        self.starts = 0
        self.live: list[bool] = []

    def start(self) -> None:
        self.starts += 1

    def set_live(self, on: bool) -> None:
        self.live.append(on)


@pytest.fixture
def window(qapp, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[MainWindow, StartPort, list]]:
    port = StartPort()
    shown = MainWindow(port=port, sanitize=redact)
    asked: list[tuple[str, str, str]] = []

    def ask(title: str, text: str, yes: str) -> bool:
        asked.append((title, text, yes))
        return True

    monkeypatch.setattr(shown, "ask", ask)
    yield shown, port, asked
    shown.apply_state(RobotState())
    shown.close()
    shown.deleteLater()


def _as_view(status: LiveStatus) -> LiveView:
    return status


def test_start_is_grey_with_the_live_reason_and_asks_the_live_question(window) -> None:
    """Стережёт кнопку «Старт» = «Включить бой»: та же доступность, та же причина, тот же вопрос.

    Мутации: «Старт» горит при запрете или без причины в подсказке — первая
    часть; нажатие без вопроса боя или мимо `port.start` — вторая; горит
    при уже включённом бое — третья.
    """
    shown, port, asked = window
    shown.show_live(_as_view(LiveStatus(available=False, why_not=(NO_STREAM,))))
    assert not shown.start_action.isEnabled(), "«Старт» горит при запрете боя"
    assert shown.start_action.toolTip() == shown.live_action.toolTip()
    assert NO_STREAM in shown.start_action.toolTip()

    shown.show_live(_as_view(LiveStatus(available=True)))
    assert shown.start_action.isEnabled()
    assert shown.start_action.toolTip() == START_HINT
    shown.start_action.trigger()
    assert [title for title, _, _ in asked] == ["Включить бой"], asked
    assert port.starts == 1 and port.live == [], (port.starts, port.live)

    shown.show_live(_as_view(LiveStatus(available=True, wanted=True)))
    assert not shown.start_action.isEnabled(), "«Старт» горит при включённом бое"


def test_the_panel_says_the_live_is_assembling_not_simulation(qapp) -> None:
    """Стережёт панель между «Включить бой» и сборкой: «БОЙ: собирается», а не «СИМУЛЯЦИЯ».

    Снимок 16.11.2026 08:58: состояние ещё от прогона (`running` ложно,
    `simulation` стоит), снимок боя — «просили». Мутации: панель по одному
    `RobotState` — падает первая часть; «собирается» и после сборки — вторая.
    """
    panel = StatusPanel()
    try:
        panel.apply_state(RobotState(running=False, simulation=True))
        panel.apply_live(_as_view(LiveStatus(available=True, wanted=True)))
        assert ASSEMBLING_BADGE in panel.regime.text(), panel.regime.text()
        assert "СИМУЛЯЦИЯ" not in panel.regime.text()
        mode = panel.mode.value.text()
        assert ASSEMBLING_MODE in mode and "робот стоит" not in mode, mode

        panel.apply_state(RobotState(running=True, simulation=False))
        assert "БОЕВОЙ РЕЖИМ" in panel.regime.text(), panel.regime.text()
        assert ASSEMBLING_MODE not in panel.mode.value.text()
    finally:
        panel.deleteLater()
