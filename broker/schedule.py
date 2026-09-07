"""Расписание торгов у брокера: рабочий ли сегодня день и идут ли торги сейчас.

Зачем это здесь
---------------
Владелец счёта спросил: «ты же учитываешь, что биржа может ночью или вечером
не работать? то есть пинговать напрасно не надо». Проверка показала, что
понятия «биржа закрыта» в программе не было вовсе: ночью она пробовала бы
подключиться раз в минуту до утра и на каждом успешном подключении искать дыру
в данных.

Расписание торгов — **торговое знание**, и придумывать его в слое данных или
в окне значило бы завести вторую правду о торговом времени рядом с первой.
Расходятся такие молча. Поэтому спрашиваем брокера: у него для этого два
готовых метода.

Два вопроса — два метода
------------------------
* `Schedule.status` — «идут ли торги прямо сейчас» по коду класса. Дешёвый
  ответ: `OPEN`/`CLOSE` плюс `nextSessionDate` — когда состояние изменится;
* `Schedule.daily` — «из чего состоит сегодняшний день» по классу и тикеру:
  признак рабочего дня и отрезки — торговый период, аукционы, вечерняя сессия,
  технический перерыв.

Как этим пользоваться
---------------------
::

    from broker.schedule import Schedule

    hours = Schedule(session)
    if await hours.is_open(class_code="SPBFUT") is False:
        ...        # биржа закрыта: подключаться незачем
    today = await hours.daily(class_code="SPBFUT", ticker="MXU6")
    if today is not None and today.is_work_day is False:
        ...        # выходной или праздник: молчим весь день

Решение 1: как часто спрашивать
-------------------------------
Ответ **живёт в памяти**. Ограничитель частоты у сессии общий (5 запросов
в секунду на весь слой), и догрузка истории уже умеет занять очередь надолго;
запрос расписания перед каждым действием отбирал бы место у свечей.

Сроки жизни выбраны так, чтобы **ошибка стоила лишнего запроса, а не
пропущенного открытия торгов**:

* статус — до `nextSessionDate`, но не дольше `STATUS_MAX_AGE` и не короче
  `STATUS_MIN_AGE`. Потолок обязателен: `nextSessionDate` может оказаться
  неверным (в PDF он приходит **без часового пояса**, а трёхчасовая ошибка
  в большую сторону — это пропущенное открытие). Потолок обрезает вред
  до нескольких минут. Пол обязателен по обратной причине: момент в прошлом
  давал бы запрос на каждый вызов;
* расписание на день — `SCHEDULE_MAX_AGE`. Это факт про день, менять его
  внутри дня нечему, а сутки держать нельзя: рабочий день сменится
  на выходной, и программа узнает об этом с опозданием;
* «не знаем» — `UNKNOWN_MAX_AGE`. Короткий срок нужен не ради свежести,
  а чтобы вызывающий в цикле переподключения не сделал по запросу на попытку.

Контраст с деньгами назван нарочно: `BrokerSession` кэша **не имеет** —
`DOMAIN.md` §5 требует видеть свободные средства до заявки, а биржа
пересматривает обеспечение внутри дня. Расписание — не деньги.

Решение 2: если брокер не ответил
---------------------------------
**«Не знаем» — это не «закрыто».** Программа не встаёт из-за незнания
расписания: оба метода возвращают `None`, и вызывающий обязан вести себя так,
как вёл себя до появления расписания. Молчаливое «считаем, что закрыто» хуже
лишнего запроса: оно останавливает торговлю по отказу вспомогательного метода.

Отказ не поднимается наружу, а уходит в **технический** лог — первый по каждому
вопросу предупреждением, повторные отладочной записью. Человеческий текст
отказа сюда не попадает: разговор с владельцем счёта ведёт `app/`, а не слой
брокера (то же правило, что в `session._note_failure`).

⚠️ **Но сам отказ не пропадает: его можно спросить** (`D-062`, 06.09.2026).
До этой правки «спросить не удалось» и «торги идут» приезжали вызывающему
одним и тем же `None`, и различить их было нечем. Цена названа в долге:
ошибись мы адресом — расписание провалилось бы **молча**, а прецедент свежий,
`B-010`, где неверное имя сервиса стоило всего графика. Теперь отказ по каждому
вопросу остаётся в памяти рядом с ответом «не знаем», и `status_trouble`
отдаёт его тому, кто разговаривает с человеком.

Спрашивают, а не сообщают, и это выбор: получатель отказа, назначаемый при
сборке, — это провод, который можно забыть подключить, и тогда молчание
вернётся тем же путём и так же незаметно. Вопрос забыть тоже можно, но он
задаётся там же, где разбирается `None`, — в одном выражении с ним. Человеческий
текст отказа слой по-прежнему **не произносит**: он его только отдаёт.

Решение 3: перерывы внутри дня — да, они в ответе есть
------------------------------------------------------
Дневное расписание приходит **списком отрезков**, и у каждого есть тип. Среди
перечисленных документацией типов — `Технический перерыв QUIK`. Значит
программа принципиально может отличить «свечей нет, потому что перерыв
по расписанию» от «свечей нет, потому что мы их потеряли» — это открытый
вопрос №17 (клиринговый перерыв 14:00–14:05 рисуется пустым местом
с подписью «Данных нет»).

⛔ **Но закрыть вопрос №17 этим модулем сегодня нельзя, и вот почему.**
Времена отрезков приходят строками вида `14:30:00` — **без часового пояса
и без даты**. Документация зовёт их «ISO 8601», но `01-special-info.md`
обещает UTC только для полей типа `dateTime`, а это не они. Приложить такой
отрезок к оси графика — значит угадать зону; ошибка на три часа сдвинула бы
перерыв целиком и ничего при этом не уронила. Поэтому времена отдаются
**строками как есть**, а арифметики со временем суток в этом модуле нет
вовсе. Установить зону может только живой запрос: сравнить `startDate`
торгового периода с известным расписанием биржи.

Что модуль намеренно не делает
------------------------------
Не решает, торговать или нет; не подключает расписание к потоку котировок
и к догрузке истории; не ведёт свой календарь праздников; не переводит времена
отрезков в моменты на оси. Первое — движок, второе — `app/`, третье и четвёртое
не делает никто, пока зона не установлена живым запросом.

Документация
------------
Выписки — `.docs/broker-api/05-daily-schedule.md` и
`.docs/broker-api/06-trading-status.md`. Адреса и имена полей сверены
с официальным PDF `https://cdn.bcs.ru/static/bcs/files/trade-api-docs.pdf`
05.09.2026; расхождения источников перечислены ниже и разобраны там же,
где выбор сделан, — у констант адресов в `broker/session.py`.

Расхождения PDF и нашей выписки (05.09.2026)
--------------------------------------------
* **имя сервиса дневного расписания**: выписка — `trade-api-bff-portfolio`,
  PDF — `trade-api-information-service`. Выбран PDF, разбор у
  `session.DAILY_SCHEDULE_PATH`;
* **признак рабочего дня**: выписка — `isWorkDay`, PDF — `isWorkingDay`.
  Читаются **оба** написания: какое приходит на самом деле, живым запросом
  не проверено;
* **идентификатор сессии**: выписка — `tradingSessionTypeId`, PDF —
  `tradingSessionTypeID`. Разница только в регистре, и `parsing.pick` ищет
  без учёта регистра — выбирать не из чего;
* **`nextSessionDate`**: в примере на сайте `2024-07-29T15:51:28.071Z`,
  в PDF `2021-01-19T09:41:03` — без `Z`. Момент без пояса здесь
  **отбрасывается**, а не толкуется (см. `_aware`);
* **404**: PDF называет `NOT_FOUND` штатным ответом обоих методов, сайт этот
  код у них не упоминает вовсе.

⛔ **Живым запросом не проверено ничего из этого модуля**: токена
в разработке нет. Всё, что здесь написано про поведение брокера, —
утверждение документации, а не наш замер.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Awaitable, Callable, Final, Mapping, TypeVar, cast

from broker.errors import BrokerError, UnexpectedAnswer
from broker.parsing import moment, number, pick, text
from broker.redaction import log
from broker.session import DAILY_SCHEDULE_PATH, TRADING_STATUS_PATH, BrokerSession
from broker.tokens import now_utc

#: Дольше этого ответ «идут ли торги» в памяти не живёт, что бы ни сказал
#: `nextSessionDate`. Пять минут — это цена ошибки в самом дорогом случае:
#: биржа открылась, а программа об этом ещё не знает. Ночью это 12 запросов
#: в час против шестисот попыток подключения, ради которых всё и затевалось.
STATUS_MAX_AGE: Final[timedelta] = timedelta(minutes=5)

#: Короче этого — не спрашиваем. `nextSessionDate` в прошлом (а он там
#: окажется, если брокер пришлёт момент без пояса или просто отстанет)
#: иначе означал бы запрос на каждый вызов.
STATUS_MIN_AGE: Final[timedelta] = timedelta(seconds=15)

#: Срок жизни дневного расписания. Полчаса, а не сутки: рабочий день сменяется
#: выходным на границе суток, и программа обязана это заметить сама.
SCHEDULE_MAX_AGE: Final[timedelta] = timedelta(minutes=30)

#: Срок жизни ответа «не знаем». Нужен не ради свежести, а против запроса
#: на каждую попытку переподключения: попытки идут раз в минуту и чаще.
UNKNOWN_MAX_AGE: Final[timedelta] = timedelta(seconds=30)


#: Ответ на один из двух вопросов. Общий вход в кэш параметризован им,
#: чтобы `status()` возвращал статус, а `daily()` — расписание, а не их
#: объединение, которое вызывающему пришлось бы разбирать `isinstance`.
_Answer = TypeVar("_Answer", "TradingStatus", "DailySchedule")


class SessionKind(Enum):
    """Что за отрезок торгового дня. Значения — машинные, для сравнения.

    `UNKNOWN` — не ошибка и не отказ: брокер вправе прислать тип, которого
    в документации нет. Исходная строка при этом сохраняется целиком
    в `SessionSlot.session_type`, так что ничего не теряется.
    """

    MAIN = "main"
    OPENING_AUCTION = "opening_auction"
    CLOSING_AUCTION = "closing_auction"
    EVENING = "evening"
    BREAK = "break"
    CLOSED = "closed"
    UNKNOWN = "unknown"


#: Тип сессии по строке, которую присылает брокер. Таблица, а не цепочка
#: `if`: шесть значений перечислены документацией поимённо, и добавление
#: седьмого — это строка здесь, а не новая ветка где-то в разборе.
#:
#: Ключи нормализованы (`_normalized`): регистр и лишние пробелы значения
#: не имеют. Совпадение **точное**, а не по куску строки, и это осознанно.
#: Подстрока «перерыв» сработала бы на чужом типе, а цена ошибок разная:
#: неузнанный тип даёт `UNKNOWN`, то есть «не знаем» — вызывающий ведёт себя
#: как раньше; узнанный неверно — это ложное утверждение о торговом времени.
SESSION_KINDS: Final[Mapping[str, SessionKind]] = {
    "торговый период": SessionKind.MAIN,
    "аукцион открытия": SessionKind.OPENING_AUCTION,
    "аукцион закрытия": SessionKind.CLOSING_AUCTION,
    "вечерняя торговая сессия": SessionKind.EVENING,
    "технический перерыв quik": SessionKind.BREAK,
    "не рабочее время/выходной/праздник": SessionKind.CLOSED,
}


@dataclass(frozen=True, slots=True)
class SessionSlot:
    """Один отрезок дневного расписания — как его прислал брокер.

    ⚠️ `starts_at` и `ends_at` — **строки, а не время**. Брокер шлёт `14:30:00`
    без даты и без часового пояса; какая это зона, документация не говорит,
    а живого запроса у нас не было. Перевод в момент на оси — угадывание
    на три часа, поэтому его здесь нет. Шапка модуля, «Решение 3».
    """

    starts_at: str | None
    ends_at: str | None
    session_type: str | None
    kind: SessionKind
    is_open: bool | None


@dataclass(frozen=True, slots=True)
class DailySchedule:
    """Расписание на день по одному инструменту.

    `is_work_day` — три состояния, и `None` среди них равноправно: брокер
    поля не прислал или прислал не тем типом. `None` означает «не знаем»
    и ведёт себя как незнание, а не как выходной.
    """

    class_code: str
    ticker: str
    is_work_day: bool | None
    sessions: tuple[SessionSlot, ...]

    @property
    def breaks(self) -> tuple[SessionSlot, ...]:
        """Отрезки, которые брокер назвал техническим перерывом.

        Это ответ на вопрос «бывает ли сегодня перерыв и как он называется».
        Ответа «когда именно» здесь нет: времена отрезков — строки без зоны.
        """
        return tuple(slot for slot in self.sessions if slot.kind is SessionKind.BREAK)


@dataclass(frozen=True, slots=True)
class TradingStatus:
    """Идут ли торги прямо сейчас и когда состояние изменится.

    `is_open` — три состояния. `None` («брокер прислал незнакомое слово»)
    это **не** «закрыто»: закрыть торги на основании непонятого поля
    значило бы остановить робота из-за опечатки в чужом сервисе.

    `next_change` — момент **с поясом** или `None`. Наивное время
    отбрасывается: истолкованное как местное, оно сдвинуло бы срок
    на три часа молча (`_aware`).
    """

    class_code: str
    session_type: str | None
    session_type_id: int | None
    kind: SessionKind
    is_open: bool | None
    next_change: datetime | None


def parse_status(payload: object, *, class_code: str) -> TradingStatus:
    """Ответ сервиса → статус торгов. Непонятое поле — «не знаем», а не «закрыто».

    Пустой объект отказом **не** считается: все поля документация помечает
    необязательными, а `TradingStatus` со всеми `None` честно означает
    «брокер ответил, но ничего не сказал» — вызывающий обойдётся с этим так
    же, как с отсутствием ответа. Отказом считается только ответ,
    который вообще не объект: это смена формата.
    """
    if not isinstance(payload, Mapping):
        raise UnexpectedAnswer(
            "ответ на запрос статуса торгов — не объект",
            "статус торгов: тело ответа не является объектом JSON",
        )

    session_type = text(pick(payload, ("tradingSessionType",)))
    # Написание `tradingSessionTypeID` из PDF ловится тем же именем:
    # `pick` ищет без учёта регистра. Второе имя в списке было бы мёртвым.
    type_id = number(pick(payload, ("tradingSessionTypeId",)))
    return TradingStatus(
        class_code=class_code,
        session_type=session_type,
        session_type_id=int(type_id) if type_id is not None else None,
        kind=kind_of(session_type),
        is_open=_openness(pick(payload, ("tradingSessionStatus",))),
        next_change=_aware(pick(payload, ("nextSessionDate",))),
    )


def parse_daily(payload: object, *, class_code: str, ticker: str) -> DailySchedule:
    """Ответ сервиса → расписание на день.

    Отсутствующий `dailySchedule` отказом **не** считается, в отличие
    от `bars` у свечей: там список баров — единственное содержимое ответа,
    здесь же рядом живёт `isWorkDay`, и день без отрезков — это законный
    ответ про выходной. Пустое расписание и незнание разводит `is_work_day`.
    """
    if not isinstance(payload, Mapping):
        raise UnexpectedAnswer(
            "ответ на запрос расписания торгов — не объект",
            "расписание торгов: тело ответа не является объектом JSON",
        )

    # Два написания, а не одно: сайт зовёт поле `isWorkDay`, официальный PDF —
    # `isWorkingDay`. `pick` не различает регистр, но эти два имени различаются
    # не регистром, и какое приходит на самом деле, мы не проверяли.
    raw = pick(payload, ("isWorkDay", "isWorkingDay"))
    listed = pick(payload, ("dailySchedule",))
    slots = listed if isinstance(listed, list) else []
    return DailySchedule(
        class_code=class_code,
        ticker=ticker,
        is_work_day=_flag(raw),
        sessions=tuple(_slot(item) for item in slots if isinstance(item, Mapping)),
    )


def kind_of(session_type: str | None) -> SessionKind:
    """Строка типа сессии → тип. Незнакомая строка — `UNKNOWN`, а не догадка."""
    if session_type is None:
        return SessionKind.UNKNOWN
    return SESSION_KINDS.get(_normalized(session_type), SessionKind.UNKNOWN)


def lifetime_of(value: object, now: datetime) -> timedelta:
    """Сколько ответ живёт в памяти. Одно место, где решается частота запросов.

    Таблица политики, а не три `if`, разбросанных по методам: срок жизни —
    это то, чем оплачивается ограничитель частоты, и разъехаться таким
    решениям негде.
    """
    if isinstance(value, TradingStatus):
        return _until_next_change(value, now)
    if isinstance(value, DailySchedule):
        return SCHEDULE_MAX_AGE
    return UNKNOWN_MAX_AGE


class Schedule:
    """Расписание торгов у брокера. Только чтение; ответы живут в памяти.

    Оба метода возвращают `None`, когда спросить не удалось. `None` — это
    «не знаем», и вызывающий обязан вести себя так, как вёл себя без
    расписания вовсе. Причина отказа остаётся в техническом логе: человеческий
    текст говорит `app/`, слой брокера с владельцем счёта не разговаривает.
    """

    def __init__(
        self,
        session: BrokerSession,
        *,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self._session = session
        self._clock = clock
        #: Ключ вопроса → (до какого момента ответ годен, сам ответ).
        #: `None` в качестве ответа — законное значение: так живёт «не знаем».
        self._memory: dict[str, tuple[datetime, Any]] = {}
        #: Вопросы, по которым отказ уже назван в логе. Нужно, чтобы ночь
        #: без связи не превратилась в тысячи одинаковых строк.
        self._reported: set[str] = set()
        #: Ключ вопроса → отказ, из-за которого ответа нет. Живёт ровно
        #: столько, сколько держится «не знаем»: успешный ответ на тот же
        #: вопрос стирает запись. Отдаётся наружу `status_trouble` —
        #: вызывающему, который умеет говорить с владельцем счёта.
        self._trouble: dict[str, BrokerError] = {}
        #: Замок на вопрос, а не на объект: без него десять одновременных
        #: вызовов на старте дали бы десять одинаковых запросов подряд —
        #: тот же приём, что у обмена токена в `BrokerSession.access_token`.
        self._locks: dict[str, asyncio.Lock] = {}

    async def status(self, *, class_code: str) -> TradingStatus | None:
        """Идут ли торги прямо сейчас по этому классу. `None` — «не знаем»."""
        code = _required(class_code, "код класса")

        async def ask() -> TradingStatus:
            payload = await self._session.read(
                TRADING_STATUS_PATH, params={"classCode": code}
            )
            return parse_status(payload, class_code=code)

        return await self._answer(_status_key(code), ask)

    async def daily(self, *, class_code: str, ticker: str) -> DailySchedule | None:
        """Расписание на сегодня по инструменту. `None` — «не знаем».

        Тикер обязателен, хотя брокер, по официальному PDF, при ненайденном
        тикере отдаёт расписание по одному коду класса. Полагаться на это
        не будем: запрос без тикера — это другой ответ, а не тот же самый.
        """
        code = _required(class_code, "код класса")
        symbol = _required(ticker, "тикер")

        async def ask() -> DailySchedule:
            payload = await self._session.read(
                DAILY_SCHEDULE_PATH, params={"classCode": code, "ticker": symbol}
            )
            return parse_daily(payload, class_code=code, ticker=symbol)

        return await self._answer(_daily_key(code, symbol), ask)

    async def is_open(self, *, class_code: str) -> bool | None:
        """Короткий ответ на вопрос владельца счёта: работает биржа или нет.

        Три значения, и `None` среди них равноправно. `if not await
        is_open(...)` — ошибка: `None` так превращается в «закрыто»,
        а это ровно то, чего решение 2 запрещает.
        """
        answer = await self.status(class_code=class_code)
        return answer.is_open if answer is not None else None

    def status_trouble(self, *, class_code: str) -> BrokerError | None:
        """Почему `status` ответил «не знаем». `None` — ответ есть или его не просили.

        Отказ отдаётся **объектом**, а не строкой: у `BrokerError` есть и текст
        владельцу счёта (`human`), и признак «повтор может помочь»
        (`retryable`), и тип — а по типу `WrongAddress` отличается «адреса
        у брокера нет» от «брокер не нашёл данные». Спрашивающий (`app/`)
        по этим трём вещам и выбирает слова; выбирать их здесь значило бы
        разговаривать с человеком из слоя брокера.

        Запись держится, пока держится «не знаем»: она ставится вместе с ним
        и стирается первым же удавшимся ответом на тот же вопрос. Поэтому
        ответ годится и тогда, когда `status` вернул `None` **из памяти**,
        не ходя к брокеру, — а он так и делает, пока не истёк `UNKNOWN_MAX_AGE`.

        ⛔ Парного `daily_trouble` нет намеренно. Отказ дневного расписания
        сегодня ничего не решает: `app/live_feed.py` спрашивает его только
        после того, как статус уже сказал «закрыто», и без ответа фраза
        владельцу счёта просто короче. Пустой метод «для симметрии» — это
        второе место, которое придётся держать в согласии с первым.
        """
        return self._trouble.get(_status_key(class_code))

    def forget(self) -> None:
        """Забыть всё, что помним. Для смены инструмента и для тестов."""
        self._memory.clear()
        self._reported.clear()
        self._trouble.clear()

    async def _answer(
        self, key: str, fetch: Callable[[], Awaitable[_Answer]]
    ) -> _Answer | None:
        """Ответ из памяти или у брокера. Один вход для обоих вопросов.

        Двойная проверка вокруг замка — та же, что у обмена токена: пока
        первый вызов ждал ответа, остальные успели встать в очередь,
        и без второй проверки каждый из них сходил бы к брокеру сам.
        """
        found, value = self._remembered(key)
        if found:
            return cast("_Answer | None", value)
        async with self._lock_for(key):
            found, value = self._remembered(key)
            if found:
                return cast("_Answer | None", value)
            return await self._ask(key, fetch)

    def _lock_for(self, key: str) -> asyncio.Lock:
        """Замок этого вопроса. Создаётся при первом обращении, живёт до конца."""
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    async def _ask(
        self, key: str, fetch: Callable[[], Awaitable[_Answer]]
    ) -> _Answer | None:
        """Спросить брокера. Отказ не выходит наружу, он превращается в «не знаем».

        ⚠️ Ловится `BrokerError`, а не всё подряд. `CancelledError` при выходе
        из программы, `MemoryError` и ошибки нашего же кода обязаны пройти
        насквозь: «не знаем» вместо дефекта программы — это дефект, который
        никто не найдёт.
        """
        try:
            value = await fetch()
        except BrokerError as failure:
            self._report(key, failure)
            self._trouble[key] = failure
            self._remember(key, None)
            return None
        self._reported.discard(key)
        # Отказ снимается **успехом**, а не временем: пока ответа нет,
        # спрашивающий вправе знать, почему его нет, — сколько бы попыток
        # ни прошло. Стирание по сроку вернуло бы «не знаем» без причины.
        self._trouble.pop(key, None)
        self._remember(key, value)
        return value

    def _remembered(self, key: str) -> tuple[bool, Any]:
        """Годный ответ из памяти: (нашли ли, что нашли).

        Пара, а не просто значение: `None` — законный ответ («не знаем»),
        и отличить его от «в памяти пусто» иначе нечем.
        """
        entry = self._memory.get(key)
        if entry is None or entry[0] <= self._clock():
            return False, None
        return True, entry[1]

    def _remember(self, key: str, value: object) -> None:
        now = self._clock()
        self._memory[key] = (now + lifetime_of(value, now), value)

    def _report(self, key: str, failure: BrokerError) -> None:
        """Отказ — в технический лог, и только туда.

        Человеческий текст (`failure.human`) сюда не идёт: разговор
        с владельцем счёта — не дело слоя брокера. Первый отказ по вопросу —
        предупреждением, повторные — отладочной записью: ночь без связи иначе
        оставила бы в логе тысячи одинаковых строк.
        """
        line = "расписание торгов не получено (%s): %s"
        detail = failure.technical or type(failure).__name__
        if key in self._reported:
            log().debug(line, key, detail)
            return
        self._reported.add(key)
        log().warning(line, key, detail)


def _status_key(class_code: str) -> str:
    """Ключ вопроса «идут ли торги». Один на запрос, память и `status_trouble`.

    Отдельная функция, а не строка в двух местах: разъехавшиеся ключи дали бы
    вечное `None` у `status_trouble` — то есть ровно то молчание, против
    которого он и заведён, только уже с виду починенное.
    """
    return f"статус торгов {class_code.strip()}"


def _daily_key(class_code: str, ticker: str) -> str:
    """Ключ вопроса «из чего состоит сегодняшний день»."""
    return f"расписание на день {class_code.strip()}/{ticker.strip()}"


def _slot(item: Mapping[str, Any]) -> SessionSlot:
    """Один отрезок ответа → отрезок расписания."""
    session_type = text(pick(item, ("tradingSessionType",)))
    return SessionSlot(
        starts_at=text(pick(item, ("startDate",))),
        ends_at=text(pick(item, ("endDate",))),
        session_type=session_type,
        kind=kind_of(session_type),
        is_open=_openness(pick(item, ("tradingSessionStatus",))),
    )


def _until_next_change(status: TradingStatus, now: datetime) -> timedelta:
    """Срок жизни статуса: до смены состояния, но в границах.

    `nextSessionDate` — единственное, ради чего этот метод дешевле полного
    расписания: он говорит, до какого момента спрашивать незачем. Верить ему
    без границ нельзя. Сверху режет `STATUS_MAX_AGE`: момент, ошибочно
    отнесённый на три часа вперёд, стоил бы пропущенного открытия торгов.
    Снизу режет `STATUS_MIN_AGE`: момент в прошлом стоил бы запроса
    на каждый вызов.
    """
    if status.next_change is None:
        return STATUS_MAX_AGE
    left = status.next_change - now
    if left < STATUS_MIN_AGE:
        return STATUS_MIN_AGE
    return min(left, STATUS_MAX_AGE)


def _openness(value: object) -> bool | None:
    """`OPEN`/`CLOSE` → да/нет. Всё прочее — `None`, то есть «не знаем».

    `bool` принимается наравне со строкой: официальный PDF подписывает
    значения как «OPEN (true)» и «CLOSE (false)», то есть поле вполне
    может приехать логическим. Незнакомое слово в «закрыто» не превращается.
    """
    if isinstance(value, bool):
        return value
    word = text(value)
    if word is None:
        return None
    return {"open": True, "close": False}.get(word.casefold())


def _flag(value: object) -> bool | None:
    """Логическое поле брокера. Ни числа, ни пустой строки — только да/нет.

    Строки принимаются потому, что PDF подписывает признак рабочего дня как
    «Логический (TRUE/FALSE)» — то есть допускает и такое написание. Всё
    остальное даёт `None`: «брокер не сказал», а не «нерабочий день».
    """
    if isinstance(value, bool):
        return value
    word = text(value)
    if word is None:
        return None
    return {"true": True, "false": False}.get(word.casefold())


def _aware(value: object) -> datetime | None:
    """Момент **с поясом** или `None`. Наивное время отбрасывается.

    Брокер обещает UTC для всех полей `dateTime` (`01-special-info.md`), и
    пример на сайте кончается на `Z`. Пример в PDF — нет. Момент без пояса
    `astimezone` истолковал бы как местный: на московской машине это молчаливый
    сдвиг на три часа. Здесь такой сдвиг стоил бы срока жизни ответа, а срок
    всё равно ограничен сверху, поэтому потеря поля не стоит ничего —
    в отличие от догадки о зоне.
    """
    parsed = moment(value)
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _normalized(value: str) -> str:
    """Строка типа сессии для сравнения с таблицей: без лишних пробелов и регистра."""
    return " ".join(value.split()).casefold()


def _required(value: str, what: str) -> str:
    """Обязательный параметр запроса. Пустой — ошибка вызывающего, а не отказ брокера.

    Проверяется **до** сети: пустой код класса доехал бы до брокера и вернулся
    отказом 400 уже в торговое время, потратив запрос из общей квоты.
    """
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{what} пуст: расписание торгов спрашивать не о чем")
    return cleaned
