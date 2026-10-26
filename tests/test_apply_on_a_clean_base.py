"""«Применить» на чистой базе: правка на любой вкладке принимается как есть.

Слова владельца счёта 05.10.2026: «настройки привести к „реверс с постоянной
позицией“!!! проверить, что их можно применять после изменения!!!»
(решение 0063).

Стережёт: окно, открытое на умолчаниях программы, после правки одного поля
на вкладке отдаёт «Применить» доступной, порт настройки **принимает**, а
журнал называет только сделанную правку. Если умолчания не годятся
единственному алгоритму, порт выставляет их сам и пишет строку «Настройки
выставлены под алгоритм» либо в журнале появляется изменение, которого
человек не делал, — проверка падает.

Мутация, обязанная ронять проверку: вернуть прежние умолчания
(`Settings.strategy_id = "ema_reverse"`, `reversal_moment = NEXT_BAR`,
`time_exit_order = MARKET`).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from PySide6.QtCore import QTime
from PySide6.QtWidgets import QDialogButtonBox

from app import convert
from app.port import HistoryPort
from market.journal import redact
from market.worker import MarketWorker
from ui.models import FIELD_CAPTIONS, DecisionRow, Settings
from ui.settings_dialog import SettingsDialog


class Agreeing(SettingsDialog):
    """Окно настроек без модального «было → стало»: в прогоне оно повисло бы."""

    def confirm(self, values: Settings) -> bool:
        return True


def _later(dialog: SettingsDialog) -> None:
    end = dialog.window_end.time()
    dialog.window_end.setTime(QTime(end.hour(), end.minute()).addSecs(-15 * 60))


#: Вкладка → правка одного поля на ней и слово, которым журнал её называет.
EDITS: dict[str, tuple[Callable[[SettingsDialog], None], str]] = {
    "Инструмент и данные": (
        lambda dialog: dialog.depth_days.setValue(dialog.depth_days.value() + 7),
        "Глубина показа",
    ),
    "Сигнал": (
        lambda dialog: dialog.average_period.setValue(
            dialog.average_period.value() + 2
        ),
        "Период средней",
    ),
    "Вход и выход": (
        lambda dialog: dialog.take_profit.setValue(dialog.take_profit.value() + 0.1),
        "Тейк",
    ),
    "Торговое окно": (_later, "окн"),
    "Деньги": (
        lambda dialog: dialog.volume.setValue(dialog.volume.value() + 1),
        "Объём",
    ),
    "Программа": (
        lambda dialog: dialog.log_directory.setText("/tmp/логи-проверки"),
        "Каталог",
    ),
}

#: Строки, которых в журнале после правки одного поля быть не должно:
#: это изменения, которых человек не делал. Подписи берутся из той же
#: таблицы, что пишет журнал: набранное руками «Выход по концу окна» не
#: совпадало с «Заявка на выход по концу окна» в журнале, и умолчание
#: `time_exit_order = MARKET` проходило эту проверку на каждой вкладке.
FOREIGN = tuple(
    FIELD_CAPTIONS[name] for name in ("strategy_id", "reversal_moment", "time_exit_order")
)


def test_every_tab_has_an_edit(qapp) -> None:
    """Канарейка полноты: правка задана для **каждой** вкладки окна."""
    dialog = Agreeing(Settings())
    tabs = [dialog.tabs.tabText(index) for index in range(dialog.tabs.count())]
    dialog.deleteLater()
    assert sorted(tabs) == sorted(EDITS), tabs


@pytest.mark.parametrize("tab", sorted(EDITS))
def test_an_edit_on_the_tab_is_applied_as_it_is(loop, tmp_path, tab: str) -> None:
    """Правка на вкладке → «Применить» доступна → порт принял, журнал назвал её."""
    edit, word = EDITS[tab]
    applied: list[Settings] = []
    failures: list[str] = []
    journal: list[DecisionRow] = []

    async def go() -> None:
        worker = MarketWorker(tmp_path / "candles.sqlite3")
        port = HistoryPort(worker, values=Settings(), days=0, sanitize=redact)
        port.settings_applied.connect(applied.append)
        port.failed.connect(failures.append)
        port.decision_appended.connect(journal.append)
        dialog = Agreeing(Settings())
        dialog.set_algorithms(convert.algorithms(Settings()))
        dialog.settings_changed.connect(port.apply_settings)
        edit(dialog)
        button = dialog.buttons.button(QDialogButtonBox.StandardButton.Apply)
        assert button.isEnabled(), f"«Применить» погашена после правки на «{tab}»"
        button.click()
        dialog.deleteLater()
        await port.aclose()
        await worker.close()

    loop.run_until_complete(go())
    assert applied and not failures, f"настройки не приняты: {failures}"
    events = [row.event for row in journal]
    assert "Настройки выставлены под алгоритм" not in events, (
        "умолчания программы не годятся единственному алгоритму — порт "
        f"выставил их сам: {[row.reason for row in journal]}"
    )
    said = " ".join(row.reason for row in journal if row.event == "Настройки изменены")
    assert word in said, f"правка на «{tab}» не названа в журнале: «{said}»"
    stray = [one for one in FOREIGN if one in said]
    assert not stray, f"в журнале изменения, которых не делали: {stray} — «{said}»"


def test_the_clean_defaults_already_satisfy_the_algorithm() -> None:
    """Умолчания программы — то, чего требует единственный алгоритм."""
    values, said = convert.settled(Settings())
    assert said == (), said
    assert values == Settings()
