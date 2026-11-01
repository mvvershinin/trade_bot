"""Сверка с брокером при старте и после связи: решения 0017 и 0020 (0066).

Что делает, по порядку:

1. **Незакрытые команды журнала** — статусом по ключу у брокера (0017):
   живая заявка снимается, исполненная и снятая закрываются в журнале,
   неизвестная брокеру — «не подана». Строка `recorded` здесь не значит
   «не ушла» (`D-177`): судьбу заявки решает ответ брокера, а не журнал.
2. **Позиция у брокера против позиции робота** — свежим запросом. Полное
   совпадение — сверка прошла. Любое другое — «закрыть всё и остановиться»
   (0020): снять заявки, закрыть позицию по рынку **один раз**, робот стоит
   до решения владельца счёта. Остановка ставится до закрывающей заявки.

Чего не делает: не решает, что движок делает после сверки, — это 2в (пара
движков, «Возобновить» — только через сверку, С3). Сделки «закрыть всё»
ложатся в таблицу исполнений тем же путём, что все исполнения, — потоком
статусов исполнителя (`app/live_executor.py`).

⚠️ ARCHITECTURE.md §3 называл сверку обязанностью `engine/`; решение 0066
перенесло её сюда («Делает `app/`»). Правка §3 — за 2в вместе с подключением.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.live_executor import LiveExecutor, position_words
from broker.errors import BrokerError

__all__ = ["Reconciliation", "reconcile"]


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """Итог сверки.

    :param notes: что сделано с незакрытыми командами журнала — словами.
    :param broker: позиция у брокера со знаком; `None` — прочитать не удалось.
    :param expected: позиция робота со знаком.
    :param halted: почему торговать нельзя — словами; `None` — сверка прошла.
    """

    notes: tuple[str, ...]
    broker: int | None
    expected: int
    halted: str | None


async def reconcile(executor: LiveExecutor, expected: int) -> Reconciliation:
    """Сверить журнал и позицию с брокером; расхождение — закрыть всё и стоять.

    * судьба хоть одной команды журнала не выяснена — сверка **не прошла**:
      позиция не сравнивается и не закрывается, состояние неизвестно (С1);
    * позиция совпала — она же становится основой позиции по сделкам,
      по которой «закрыть всё» потом проверяет отставание портфеля (К2);
    * `halted` в итоге — `executor.halted`, если исполнитель стоит: сверка
      брокера с роботом могла сойтись, а торговать этим исполнителем всё
      равно нельзя («Возобновить» — пересборка, С3).

    :param expected: позиция робота со знаком (`live_executor.signed_position`).
    """
    notes = await executor.settle_open_commands()
    if executor.unsettled:
        return Reconciliation(notes, None, expected, executor.halted)
    try:
        actual = await executor.check_position(expected)
    except BrokerError as error:
        return Reconciliation(
            notes, None, expected,
            f"Позицию у брокера прочитать не удалось: {error.human} Сверка не прошла — "
            "робот не торгует.",
        )
    if actual == expected:
        return Reconciliation(notes, actual, expected, executor.halted)
    why = (
        f"Сверка с брокером: у брокера {position_words(actual)}, "
        f"у робота {position_words(expected)}."
    )
    return Reconciliation(notes, actual, expected, await executor.close_everything(why))
