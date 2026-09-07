"""БКС: токены, котировки, заявки, портфель, поток реального времени.

Слой знает только то, что программа говорит брокеру и слышит от него.
Про стратегии, торговое окно, тейк-профит и лимиты убытка он не знает ничего
и ни один другой слой проекта не импортирует (`ARCHITECTURE.md` §2).

**Что сделано (задача Э1-4):** хранение токена в `userdata/` с правами `0600`,
обмен токена личного кабинета на рабочий и его автообновление, обратный отсчёт
срока с предупреждением за 10 дней, состояние счёта — свободные средства,
гарантийное обеспечение и фактические позиции, — справочник инструментов
и внятное поведение при каждом виде отказа.

Плюс расписание торгов (`broker/schedule.py`, 05.09.2026): рабочий ли сегодня
день, идут ли торги прямо сейчас и из каких отрезков состоит день. Оба запроса
читающие, ответ живёт в памяти, а «брокер не ответил» означает «не знаем»,
а не «биржа закрыта».

**Чего здесь нет:** подачи, изменения и снятия заявок. Это этап 2. Метод
`BrokerSession.read` отказывается обращаться к торговым адресам вовсе.
Потока котировок реального времени тоже нет — это задача Э1-5.

Плюс два ответа на вопросы, из-за которых торговля вставала на ровном месте
(05.09.2026): **ГО под контракт до первого входа** — у брокера такого поля нет
ни в одном методе, поэтому берётся биржевое `INITIALMARGIN` с надбавкой
и помечается оценкой (`broker/margin.py`, решение 0043); **расхождение часов
машины с часами брокера** — измеряется по заголовку `Date` обычного ответа,
без единого лишнего запроса, и говорится вслух, а время не подкручивается
(`broker/clock.py`, решение 0044).

**Токен.** Значение живёт в `Secret` и наружу выходит одним вызовом `reveal()`
в трёх местах слоя: заголовок запроса, тело запроса авторизации, запись
в свой файл. В лог, журнал, отчёт, экспорт, текст исключения и трассировку
оно не попадает — ни целиком, ни частично, ни первыми символами.
"""

from broker.account import (
    SNAPSHOT_INTERVAL,
    SNAPSHOT_MAX_AGE,
    Account,
    AccountSnapshot,
    Money,
    Position,
    Side,
)
from broker.clock import (
    SKEW_BLOCKING,
    SKEW_NOTICE,
    ClockWatch,
    Reading,
    Verdict,
)
from broker.connection import Backoff, ConnectionState, Outage
from broker.errors import (
    AuthFailed,
    BadRequest,
    BrokerError,
    IncompleteAccountData,
    NoConnection,
    NotFound,
    OutcomeUnknown,
    ReadOnlyToken,
    ServerFailure,
    Throttled,
    Timeout,
    TokenExpired,
    TokenFileError,
    UnexpectedAnswer,
    WrongAddress,
)
from broker.instruments import Instrument, Instruments
from broker.margin import (
    BROKER_MARKUP,
    MarginBook,
    MarginPerContract,
    MarginSource,
)
from broker.redaction import (
    FOREIGN_LOGGERS,
    LOGGER_NAME,
    MASK,
    RedactingFilter,
    RedactingFormatter,
    scrub,
)
from broker.redaction import install as install_redaction
from broker.schedule import (
    SCHEDULE_MAX_AGE,
    STATUS_MAX_AGE,
    STATUS_MIN_AGE,
    UNKNOWN_MAX_AGE,
    DailySchedule,
    Schedule,
    SessionKind,
    SessionSlot,
    TradingStatus,
)
from broker.secret import Secret
from broker.session import BrokerSession
from broker.throttle import Throttle
from broker.token_store import StoredToken, TokenStore
from broker.tokens import (
    ACCESS_LIFETIME,
    REFRESH_LIFETIME,
    WARN_BEFORE,
    AccessToken,
    NoticeLevel,
    RefreshToken,
    TokenNotice,
    TokenScope,
)

# Чистка ставится при импорте слоя, а не при создании подключения. Иначе
# гарантия «токен не попадает в лог» зависела бы от того, дошло ли дело
# до `BrokerSession`: `TokenStore` пишет в журнал раньше и сам по себе.
# Вызов идемпотентен.
#
# Ставится **только поддерево `broker`** — то, что слою принадлежит. Ветки
# `httpx` и `httpcore` общие для процесса: через `httpx` ходит и загрузка
# истории с биржи в `market/`. Слой брокера не вправе менять записи чужого
# слоя на импорте, и настройка журналирования по `ARCHITECTURE.md` §2 —
# обязанность `app/`. Что именно `app/` должен сделать на Э1-18 и чем это
# опасно до тех пор — перечислено в докстринге `redaction.install`.
install_redaction()

__all__ = [
    "ACCESS_LIFETIME",
    "BROKER_MARKUP",
    "SKEW_BLOCKING",
    "SKEW_NOTICE",
    "SNAPSHOT_INTERVAL",
    "SNAPSHOT_MAX_AGE",
    "SCHEDULE_MAX_AGE",
    "STATUS_MAX_AGE",
    "STATUS_MIN_AGE",
    "UNKNOWN_MAX_AGE",
    "DailySchedule",
    "Schedule",
    "SessionKind",
    "SessionSlot",
    "TradingStatus",
    "FOREIGN_LOGGERS",
    "LOGGER_NAME",
    "MASK",
    "REFRESH_LIFETIME",
    "WARN_BEFORE",
    "Account",
    "AccountSnapshot",
    "AccessToken",
    "AuthFailed",
    "Backoff",
    "BadRequest",
    "BrokerError",
    "BrokerSession",
    "ClockWatch",
    "ConnectionState",
    "MarginBook",
    "MarginPerContract",
    "MarginSource",
    "Reading",
    "Verdict",
    "IncompleteAccountData",
    "Instrument",
    "Instruments",
    "WrongAddress",
    "Money",
    "NoConnection",
    "NotFound",
    "NoticeLevel",
    "Outage",
    "OutcomeUnknown",
    "Position",
    "ReadOnlyToken",
    "RedactingFilter",
    "RedactingFormatter",
    "RefreshToken",
    "Secret",
    "ServerFailure",
    "Side",
    "StoredToken",
    "Throttle",
    "Throttled",
    "Timeout",
    "TokenExpired",
    "TokenFileError",
    "TokenNotice",
    "TokenScope",
    "TokenStore",
    "UnexpectedAnswer",
    "install_redaction",
    "scrub",
]
