"""Журнал команд брокеру и таблица исполнений: что программа велела брокеру.

Зачем
-----
Реальная заявка уходит брокеру только после того, как её строка
**зафиксирована** в базе (план Э4 §2.2 п. 4, «не закрыто» С5). Ключ заявки
у брокера — `clientOrderId`, UUID, который создаёт пишущий путь `broker/`;
имя заявки у движка — текст из её содержания, и у тейка оно одно на все
перестановки, а у брокера каждая перестановка — новая заявка с новым UUID
(план §2.9). Связь «имя движка ↔ UUID» живёт только здесь: после перезапуска
её больше негде взять, а без неё живая заявка у брокера — чужая.

**Дубль заявки начинается с потерянной записи.** Запись, которая вернулась
раньше фиксации, переживает отправку и не переживает перезапуск: заявка
у брокера живёт, журнал о ней не знает, сверка при старте подаёт её снова.
Поэтому журнал — граница безопасности денег, а не отчёт.

Три правила хранения
--------------------
* **Возврат только после фиксации, сбой — исключение.** Отправку разрешает
  возврат из записи, и ничто другое; проглоченный сбой разрешил бы её
  без строки.
* **Повтор ключа — отказ, а не замена.** Заменённая строка — это заявка
  у брокера, о которой журнал забыл; молча пропущенная — то же самое.
* **Строки переживают прогон.** Прогон по истории чистится, таблица прогонов
  пересобирается миграцией, а живая заявка у брокера остаётся живой
  независимо от того, что стало с прогоном, который её подал.

Исполнения идемпотентны в обратную сторону: поток статусов вправе прислать
одно и то же исполнение дважды, и второй раз — не вторая сделка
(план §2.10, ключ — `executionId`).

Типы здесь, а не в `broker/`: слой данных не импортирует ни одного слоя
проекта (`ARCHITECTURE.md` §2, `tests/test_layers.py`). Что значат коды
брокера (`orderStatus`, `side` `"1"`/`"2"`), решает `broker/`; сюда приходит
уже переведённое на наш язык.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

__all__ = [
    "BrokerCommand",
    "BrokerExecution",
    "CommandAction",
    "CommandSide",
    "CommandStatus",
    "StoredCommand",
]


class CommandAction(str, Enum):
    """Что команда велит брокеру: создать заявку или снять её.

    Изменения заявки нет намеренно: план §2.8 предлагает свою пару
    «снять → дождаться → создать», у каждой половины свой ключ.
    """

    CREATE = "create"
    CANCEL = "cancel"


class CommandSide(str, Enum):
    """Сторона **сделки**, как она ушла брокеру: купить или продать.

    Не сторона позиции. У движка `OrderRequest.side` — сторона позиции
    (`engine/contracts.py`), и закрытие лонга там «лонг», а у брокера —
    продажа. В журнал команд ложится то, что увидел брокер: по этой строке
    сверяют заявку у него, а не намерение движка.
    """

    BUY = "buy"
    SELL = "sell"


class CommandStatus(str, Enum):
    """Чем кончилась команда — или что о ней известно сейчас.

    =========  ==================================================  =========
    статус     смысл                                               закрыта
    =========  ==================================================  =========
    recorded   записана до отправки; ушла ли — не известно         нет
    accepted   брокер принял: заявка жива, отмена в работе          нет
    unknown    исход неизвестен: таймаут, обрыв, 5xx, 429           нет
    refused    брокер отказал до приёма — заявки (отмены) нет       да
    rejected   принята и отклонена биржей (`orderStatus 8`)         да
    filled     исполнена полностью; у отмены — исходная успела      да
    cancelled  снята; у отмены — исходная снята, отмена удалась     да
    =========  ==================================================  =========

    ⚠️ **`recorded` — не «не отправлена».** Программа может упасть между
    записью и ответом на отправку, и тогда заявка у брокера есть, а строка
    так и стоит в `recorded`. Поэтому статус открытый: такую команду сверяют
    с брокером, а не забывают.

    В базе на статусе нет `CHECK` — так же, как на источнике свечи
    (`market.storage.Source`): словарь статусов вырастет на шаге 2, а `CHECK`
    в SQLite правится только пересборкой таблицы с деньгами внутри. Незнакомое
    значение считается **открытым** (`CandleStore.open_broker_commands` берёт
    всё, что не закрыто) и роняет чтение — то есть ведёт к сверке и отказу,
    а не к тихому «всё закрыто».
    """

    RECORDED = "recorded"
    ACCEPTED = "accepted"
    UNKNOWN = "unknown"
    REFUSED = "refused"
    REJECTED = "rejected"
    FILLED = "filled"
    CANCELLED = "cancelled"

    @property
    def closed(self) -> bool:
        """Закрыта ли команда: у брокера по ней больше ничего не случится.

        Отсюда выводятся и выбор незакрытых, и сторож в базе, запрещающий
        переписывать закрытую, — одно правило в одном месте.
        """
        return self in (
            CommandStatus.REFUSED,
            CommandStatus.REJECTED,
            CommandStatus.FILLED,
            CommandStatus.CANCELLED,
        )


def _canonical_uuid(value: str, what: str) -> str:
    """Отказ, если текст — не UUID в каноническом написании.

    Ключ **не чистится, а проверяется**. Чистка, изменившая ключ, развела бы
    строку журнала и заявку у брокера: в POST ушёл бы один текст, на диск —
    другой, и сверка не нашла бы своей заявки. Канонический UUID — 36 знаков
    из цифр, `a`–`f` и дефисов — токена нести не может, поэтому проверка
    формата закрывает ту же дыру, что и чистка.

    Каноническое написание обязательно: `UUID("ABC…")` и `UUID("abc…")` —
    один ключ, а для базы и для брокера — две разные строки.
    """
    try:
        parsed = uuid.UUID(value)
    except (TypeError, ValueError, AttributeError):
        raise ValueError(f"{what} {value!r} — не UUID") from None
    if str(parsed) != value:
        raise ValueError(
            f"{what} {value!r} записан не в каноническом виде {str(parsed)!r}: "
            "в журнале и в теле заявки ключ обязан быть одной и той же строкой"
        )
    return value


def _whole_positive(value: object) -> bool:
    """Целое число контрактов не меньше одного. `True` числом не считается."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _finite_or_none(value: float | None) -> bool:
    """Число без `NaN` и бесконечности либо пусто.

    ⚠️ `NaN` в SQLite ложится как `NULL`: лимитная цена `NaN` прочиталась бы
    пустой, то есть заявкой **по рынку**. Отказ здесь — единственное место,
    где это видно.
    """
    return value is None or (
        isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
    )


def _create_problem(command: BrokerCommand) -> str | None:
    """Чего не хватает созданию: стороны и объёма; отменяемой заявки быть не должно."""
    if command.side is None:
        return "у создания заявки нет стороны"
    if not _whole_positive(command.volume):
        return f"объём {command.volume!r} — не целое число контрактов от одного"
    if command.target_client_order_id is not None:
        return "у создания заявки не бывает отменяемой заявки"
    return None


def _cancel_problem(command: BrokerCommand) -> str | None:
    """Отмене нужен ключ исходной; стороны, объёма и цены у неё нет."""
    if command.target_client_order_id is None:
        return "у отмены нет ключа отменяемой заявки"
    _canonical_uuid(command.target_client_order_id, "ключ отменяемой заявки")
    if command.target_client_order_id == command.client_order_id:
        return "отмена не может отменять саму себя: у неё свой ключ (план §2.7)"
    if (command.side, command.volume, command.price) != (None, None, None):
        return (
            "у отмены нет ни стороны, ни объёма, ни цены — это поля отменяемой "
            "заявки, и второй их записи здесь не место"
        )
    return None


#: Форма команды по действию — таблицей, а не веткой на каждое (правило 9).
#: Те же правила держит `CHECK` таблицы `broker_command` для записи снаружи.
_SHAPE_PROBLEM: dict[CommandAction, Callable[[BrokerCommand], str | None]] = {
    CommandAction.CREATE: _create_problem,
    CommandAction.CANCEL: _cancel_problem,
}


@dataclass(frozen=True, slots=True)
class BrokerCommand:
    """Команда брокеру, как её записывают **до** отправки.

    :param client_order_id: ключ этой команды у брокера (`clientOrderId`),
        UUID в каноническом написании. У отмены он **свой**, не ключ
        отменяемой заявки (`broker-api/12`).
    :param action: создание или отмена.
    :param order_id: имя заявки у движка (`OrderRequest.order_id`). У отмены —
        имя той заявки, которую снимают.
    :param symbol: тикер, по которому подана команда. Строка обязана
        описывать себя сама: она переживает свой прогон, и взять тикер
        из прогона после его удаления будет негде.
    :param side: сторона сделки — только у создания.
    :param volume: объём в целых контрактах — только у создания.
    :param price: цена лимитной заявки. Пусто — заявка по рынку; у отмены
        пусто всегда.
    :param target_client_order_id: ключ отменяемой заявки — только у отмены.
    """

    client_order_id: str
    action: CommandAction
    order_id: str
    symbol: str
    side: CommandSide | None = None
    volume: int | None = None
    price: float | None = None
    target_client_order_id: str | None = None

    def __post_init__(self) -> None:
        _canonical_uuid(self.client_order_id, "ключ команды")
        action = CommandAction(self.action)
        if self.side is not None:
            CommandSide(self.side)
        if not self.order_id.strip():
            raise ValueError("у команды нет имени заявки движка: связать её будет не с чем")
        if not self.symbol.strip():
            raise ValueError("у команды нет тикера")
        if not _finite_or_none(self.price):
            raise ValueError(f"цена {self.price!r} — не конечное число")
        problem = _SHAPE_PROBLEM[action](self)
        if problem is not None:
            raise ValueError(f"команда {self.client_order_id}: {problem}")


@dataclass(frozen=True, slots=True)
class StoredCommand:
    """Команда, прочитанная из журнала, со статусом и номером прогона.

    :param session_id: номер прогона, подавшего команду. Прогона с этим
        номером может уже не быть — строка его переживает, а номер прогона
        не выдаётся повторно никогда (`AUTOINCREMENT`, счётчик переносится
        и пересборкой таблицы прогонов).
    :param at: когда команда записана.
    :param status_at: когда статус стал нынешним.
    :param reason: причина нынешнего статуса — отказ брокера, текст
        исключения. Прошла ту же чистку, что весь текст журнала.
    """

    id: int
    session_id: int
    command: BrokerCommand
    at: datetime
    status: CommandStatus
    status_at: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class BrokerExecution:
    """Исполнение заявки у брокера: одна сделка биржи.

    :param execution_id: `executionId` — уникальный ключ исполнения. Повтор
        события с тем же ключом второй строки не даёт.
    :param client_order_id: ключ заявки, к которой исполнение относится.
        UUID-формат **не** требуется: исполнение по заявке, поданной руками
        из приложения брокера, — тоже деньги на счёте, и потерять его строку
        хуже, чем записать чужой ключ (план §2.9).
    :param volume: сколько контрактов исполнено этим событием.
    :param price: цена, которую дал брокер (план §2.10: отдельной цены
        исполнения в событии нет, есть средняя по заявке).
    :param at: момент исполнения по брокеру.
    :param commission: комиссия, если брокер её назвал. Пусто — не известна,
        а не ноль (`engine/contracts.py`, `Fill.commission`).
    """

    execution_id: str
    client_order_id: str
    volume: int
    price: float
    at: datetime
    commission: float | None = None

    def __post_init__(self) -> None:
        if not self.execution_id.strip():
            raise ValueError("исполнение без executionId: узнать его повтор будет нечем")
        if not self.client_order_id.strip():
            raise ValueError(f"исполнение {self.execution_id}: нет ключа заявки")
        if not _whole_positive(self.volume):
            raise ValueError(
                f"исполнение {self.execution_id}: объём {self.volume!r} — "
                "не целое число контрактов от одного"
            )
        if self.price is None or not _finite_or_none(self.price):
            raise ValueError(f"исполнение {self.execution_id}: цена {self.price!r}")
        if not _finite_or_none(self.commission):
            raise ValueError(
                f"исполнение {self.execution_id}: комиссия {self.commission!r}"
            )
        if self.at.tzinfo is None:
            raise ValueError(f"исполнение {self.execution_id}: время без часового пояса")
