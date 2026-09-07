"""Состояние счёта: свободные средства, гарантийное обеспечение, позиции.

Два сервиса БКС, оба на чтение:

* **«Лимиты»** — деньги и гарантийное обеспечение. Документация БКС прямо
  советует его для «получения информации о вашем портфеле»;
* **«Портфель»** — позиции: сторона, количество, цена открытия, текущая цена,
  шаг цены, лот, дата экспирации.

**Ноль по умолчанию здесь запрещён.** `DOMAIN.md` §5 требует, чтобы программа
видела свободные средства **до** подачи заявки и не узнавала о нехватке
обеспечения из отказа брокера. Подставленный ноль в поле «свободные средства»
читается как «денег нет» — это ложная тревога; подставленный ноль в поле
«гарантийное обеспечение» читается как «обеспечение не требуется» — а это уже
разрешение войти в позицию, которую нечем держать. Поэтому недостающее число
остаётся `None`, а снимок помечается неполным.

⚠️ **Размер счёта и ГО под контракт — производные, а не поля ответа.**
Дневной лимит убытка считается от размера счёта, а запас средств — от ГО
одного контракта (решение 0035). По HTTP брокер не отдаёт напрямую ни того,
ни другого:

* размер счёта складывается из `currentValueRub` всех записей портфеля —
  деньги там отдельной записью типа `moneyLimit`. Прямое поле
  `portfolioCurrentValue.currentValueRub` есть **только** в сервисе
  «Маржинальные показатели», а он живёт только на вебсокете;
* ГО под контракт выводится из открытой позиции: `lockedForFutures` делённое
  на количество. Пока позиции нет — числа нет, и это честное «не знаем».

Оба вывода **не проверены живым подключением** и помечены в коде.

⚠️ **О точности имён полей.** Ответ сервиса «Лимиты» описан в документации
таблицей, и часть имён в ней набрана с опечатками: рядом стоят `cbpl.init`,
`cbplUsed`, `cbp1Planned` (цифра «1» вместо буквы «l»), `cbplusedForOrders`
и `cbplusedForPositions`. Какое написание приходит на самом деле, по таблице
установить нельзя. Поэтому поиск поля идёт по нескольким написаниям
и без учёта регистра, а если не нашлось ни одного — поле остаётся пустым
и попадает в список недостающих. Угадывать здесь нельзя: цена ошибки —
позиция, открытая без обеспечения. Проверить можно только на живом
подключении, и до этой проверки числа считаются неподтверждёнными.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Mapping

from broker.errors import IncompleteAccountData, UnexpectedAnswer
from broker.parsing import first_mapping, moment, number, pick, text
from broker.session import LIMITS_PATH, PORTFOLIO_PATH, BrokerSession
from broker.tokens import now_utc

#: Как часто спрашивать состояние счёта, пока робот работает.
#:
#: Один снимок — **два** запроса, «Лимиты» и «Портфель». При тридцати
#: секундах это 0,067 запроса в секунду против предела 10 RPS на каждый
#: из сервисов (`.docs/broker-api/29-restrictions.md`) и против нашего
#: осторожного ведра в 5 запросов в секунду (`broker/throttle.py`).
#: Запас — два порядка.
#:
#: Почему не «раз в свечу»: снимок нужен **до** заявки, а очередь у ведра
#: общая с догрузкой истории и способна задержать запрос на десятки секунд.
#: Спрашивать заранее и ровным темпом дешевле, чем ждать ответа в момент
#: решения — тогда пришлось бы либо пропустить сигнал, либо подать заявку
#: вслепую.
#:
#: Почему не чаще: чаще нечего узнавать. Деньги меняются сделками и клирингом,
#: а не каждую секунду, а общая квота нужна потоку котировок.
SNAPSHOT_INTERVAL: Final[timedelta] = timedelta(seconds=30)

#: Старше этого снимок не годится как основание для боевой заявки.
#:
#: `DOMAIN.md` §5: «кэшированное значение получасовой давности здесь
#: не годится: биржа пересматривает ГО внутри дня». Две минуты — это четыре
#: пропущенных опроса подряд; дальше числа считаются неизвестными,
#: а не просто старыми. Разница важна: «неизвестно» останавливает проверку
#: средств вслух, «старое» позволяет ей молча пройти по вчерашним деньгам.
SNAPSHOT_MAX_AGE: Final[timedelta] = timedelta(minutes=2)


class Side(enum.Enum):
    """Сторона позиции."""

    LONG = "лонг"
    SHORT = "шорт"
    FLAT = "нет позиции"

    @classmethod
    def of(cls, quantity: float) -> "Side":
        if quantity > 0:
            return cls.LONG
        if quantity < 0:
            return cls.SHORT
        return cls.FLAT


@dataclass(frozen=True, slots=True)
class Position:
    """Одна позиция портфеля — так, как её видит брокер.

    `quantity` со знаком: положительное — лонг, отрицательное — шорт.
    Это **фактическая** позиция, шаг 2 порядка восстановления после обрыва
    (`DOMAIN.md` §7). Сравнение с расчётной — работа движка, не эта.
    """

    ticker: str
    class_code: str | None
    side: Side
    quantity: float
    average_price: float | None
    current_price: float | None
    lot: float | None
    price_step: float | None
    currency: str | None
    expires_on: datetime | None
    locked_for_futures: float | None
    display_name: str | None = None

    @property
    def open(self) -> bool:
        return self.side is not Side.FLAT


@dataclass(frozen=True, slots=True)
class Money:
    """Деньги и обеспечение. `None` означает «брокер не прислал», а не ноль.

    ⚠️ **Какое именно число считать «свободными средствами» — не решено.**
    Документация БКС даёт два источника, и они в разных сервисах:

    * сервис «Лимиты» (HTTP, есть здесь) — контейнер `moneyLimit`: сколько
      денег на счёте (`quantity.value`) и сколько занято в активных заявках
      (`locked`). Разность — то, что здесь названо свободными деньгами;
    * сервис «Маржинальные показатели» (**только WebSocket**, здесь его нет) —
      `fortsStability.freeCash`, «свободные деньги на ФОРТС». Для срочного
      рынка это более прямой ответ.

    Совпадают ли эти числа на счёте с фьючерсной позицией, по документации
    сказать нельзя, и на живом подключении не проверялось. До такой проверки
    поле `free_rub` **не годится как единственное основание** для проверки
    средств перед боевой заявкой (`DOMAIN.md` §5). Оба исходных числа хранятся
    рядом, чтобы расхождение было видно, а не спрятано за одним итогом.
    """

    #: Свободные деньги: `money_total` минус `money_locked`, если оба известны.
    free_rub: float | None
    #: Деньги на счёте по контейнеру `moneyLimit` сервиса «Лимиты».
    money_total: float | None
    #: Деньги, занятые в активных заявках.
    money_locked: float | None
    #: Гарантийное обеспечение под открытыми позициями (`cbplUsedForPositions`).
    collateral_positions: float | None
    #: Гарантийное обеспечение под выставленными заявками (`cbplUsedForOrders`).
    collateral_orders: float | None
    #: Текущий лимит открытых позиций срочного рынка (`cbpl.init`).
    futures_limit: float | None
    #: Вариационная маржа на текущий день.
    variation_margin: float | None
    missing: tuple[str, ...] = ()
    #: Почему числа не прочитались — **техническим** языком, для лога и разбора.
    #:
    #: Заведено 05.09.2026 ревью: `missing` называет **имена полей**, и когда
    #: причина была в другом (рублёвых блоков пришло два — MOEX и FORTS),
    #: разбор уходил искать опечатку в написании поля. Диагностика, уводящая
    #: не туда, дороже отсутствующей: она тратит торговое время на ложный след.
    problems: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def collateral_total(self) -> float | None:
        parts = (self.collateral_positions, self.collateral_orders)
        if any(part is None for part in parts):
            return None
        return sum(parts)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Что брокер ответил про счёт в конкретный момент.

    Снимок нигде не кэшируется. `taken_at` есть, чтобы вызывающий мог
    отказаться работать по устаревшим данным, а не чтобы их переиспользовать.
    """

    taken_at: datetime
    account: str | None
    money: Money
    positions: tuple[Position, ...] = field(default_factory=tuple)
    #: Размер счёта в рублях. `None` — «брокер не дал», а не ноль: от этого
    #: числа считается дневной лимит убытка, и ноль означал бы лимит ноль,
    #: то есть остановку робота на первом же убытке.
    equity_rub: float | None = None

    def position(self, ticker: str) -> Position | None:
        wanted = ticker.strip().upper()
        for item in self.positions:
            if item.ticker.strip().upper() == wanted:
                return item
        return None

    def open_positions(self) -> tuple[Position, ...]:
        return tuple(item for item in self.positions if item.open)

    def fresh(self, now: datetime, max_age: timedelta = SNAPSHOT_MAX_AGE) -> bool:
        """Годится ли снимок как основание для решения по деньгам.

        Умолчание названо здесь, чтобы вызывающему не пришлось выдумывать
        свой срок годности: `SNAPSHOT_MAX_AGE` выбран по `DOMAIN.md` §5.
        """
        return now - self.taken_at <= max_age

    def margin_per_contract(self, ticker: str) -> float | None:
        """ГО под один контракт по открытой позиции. `None` — «не знаем».

        ⚠️ **Это вывод, а не поле ответа.** Брокер отдаёт `lockedForFutures` —
        «занято под ГО по фьючерсам» по позиции целиком; делённое на число
        контрактов, оно даёт ГО одного. Отсюда два ограничения, и оба
        обязаны быть видны наверху:

        * **пока позиции нет, числа нет.** Ровно перед первым входом за день
          ГО неизвестно, и движок скажет «НЕ ПРОВЕРЕНО» вместо того, чтобы
          считать по выдуманному числу (решение 0035);
        * **число живёт до ближайшего пересмотра биржей.** ГО меняется внутри
          дня, поэтому годится оно ровно столько же, сколько сам снимок.

        Прямой источник — `futuresParameters.futuresPortfolio[].futuresCollateral`
        сервиса «Маржинальные показатели», но он только на вебсокете
        (`.docs/broker-api/27-ws-marginal-indicators.md`).

        Ноль и отрицательное отбрасываются: делением на них получилось бы
        «свободных средств хватает на любой объём».
        """
        position = self.position(ticker)
        if position is None or not position.open:
            return None
        locked = position.locked_for_futures
        if locked is None:
            return None
        per_contract = locked / abs(position.quantity)
        return per_contract if per_contract > 0 else None

    def funds_missing(self) -> tuple[str, ...]:
        """Чего не хватает, чтобы сообщить движку деньги счёта. Пусто — всё есть.

        Один вопрос вместо трёх проверок у вызывающего: `engine.AccountFunds`
        требует размер счёта и свободные средства **числами**, и построить его
        с пропуском нельзя. Пустой ответ означает «стройте», непустой —
        «не стройте и скажите вслух, чего не хватает» (решение 0035:
        предохранитель без данных пишет «⚠️ НЕ ПРОВЕРЕНО», а не молчит).

        ГО под контракт сюда не входит намеренно: движок принимает его
        пустым и сам говорит об этом строкой.

        ⚠️ **Спрашивается ровно то, что движку нужно**, а не полнота ответа
        «Лимитов» целиком. Прежде здесь стоял весь `money.missing`, а в него
        входят обе величины гарантийного обеспечения — числа, которых движок
        не считает ни в одном предохранителе. Имена их полей выписаны
        из таблицы документации с опечатками и живьём не проверены
        (`.docs/broker-api/03-limits.md`), то есть промах вероятен; ценой
        промаха был бы потерянный торговый день из-за поля, которое никто
        не читает. Полноту ответа спрашивает `require_money()` — это другой
        вопрос и другой ответ.
        """
        gaps: list[str] = []
        if self.money.free_rub is None:
            gaps.append("свободные средства")
        if self.equity_rub is None:
            gaps.append("размер счёта")
        return tuple(gaps)

    def require_money(self) -> Money:
        """Отдать деньги или отказать. Вызывается перед проверкой средств.

        Технический текст называет **причину**, а не только список пустых
        полей: причин у пустоты две, и лечатся они по-разному. «Не нашли
        поле» — промах по написанию, «рублёвых блоков два» — неоднозначность
        выбора по валюте и бирже.
        """
        if not self.money.complete:
            why = "; ".join(self.money.problems)
            raise IncompleteAccountData(
                self.money.missing,
                technical="сервис «Лимиты»: не найдены поля "
                + ", ".join(self.money.missing)
                + (f". {why}" if why else ""),
            )
        return self.money

    def human_summary(self) -> str:
        """Строка для панели состояния и журнала."""
        free = self.money.free_rub
        money_text = (
            # Разряды пробелом, копейки запятой: строку читает владелец счёта,
            # а не программист. `f"{x:,.2f}"` даёт «248,500.00» — американский
            # формат, в котором запятая означает не то, что здесь ожидают.
            "свободно " + f"{free:,.2f}".replace(",", "\u00a0").replace(".", ",") + " ₽"
            if free is not None
            else "свободные средства брокер не прислал"
        )
        opened = self.open_positions()
        if not opened:
            position_text = "открытых позиций нет"
        else:
            position_text = "; ".join(
                f"{item.ticker}: {item.side.value}, {abs(item.quantity):g} шт."
                + (f", средняя {item.average_price:g}" if item.average_price else "")
                for item in opened
            )
        account = f"счёт {self.account}, " if self.account else ""
        return f"{account}{money_text}. {position_text}."


# --- разбор ответов ---

#: Написания одного и того же поля, встречающиеся в таблицах документации.
#: Часть из них — следствие набора: `cbplusedForOrders` рядом с `cbplUsed`
#: и `cbp1Planned` (цифра вместо буквы). Какое написание приходит на самом
#: деле, по таблице не установить, поэтому перебираются все правдоподобные.
_COLLATERAL_POSITIONS: Final[tuple[str, ...]] = (
    "cbplUsedForPositions",
    "cbplusedForPositions",
    "cbpl_used_for_positions",
)
_COLLATERAL_ORDERS: Final[tuple[str, ...]] = (
    "cbplUsedForOrders",
    "cbplusedForOrders",
    "cbpl_used_for_orders",
)
_VARIATION_MARGIN: Final[tuple[str, ...]] = ("varMargin", "accruedInt", "var_margin")
_FREE_CASH: Final[tuple[str, ...]] = ("freeCash", "free_cash")

#: Имена контейнеров ответа сервиса «Лимиты».
#:
#: ⚠️ **Единственное и множественное число — расхождение источников, а не
#: наша перестраховка.** Страница сайта (`.docs/broker-api/03-limits.md`)
#: называет контейнеры `moneyLimits` и `futuresLimits`, официальный PDF —
#: `moneyLimit` и `futuresLimit`. Живьём не проверено ни одно написание.
#: Промах по имени здесь не ломает ничего громко: все числа остаются
#: пустыми, снимок помечается неполным, и робот не идёт в бой — но
#: выясняется это в торговое время, поэтому читаются оба.
_MONEY_BLOCK: Final[tuple[str, ...]] = ("moneyLimit", "moneyLimits", "money_limit")
_FUTURES_BLOCK: Final[tuple[str, ...]] = (
    "futuresLimit",
    "futuresLimits",
    "futures_limit",
)

#: Коды рубля, которые встречаются в ответах биржи и брокера: ISO 4217 даёт
#: `RUB`, торговые системы до сих пор шлют советские `SUR` и `RUR`, а иногда
#: цифровой код. Список нужен не для красоты: контейнеры «Лимитов» приходят
#: по валютам, и «взять первый» означает шанс прочитать доллары как рубли.
_RUBLE_CODES: Final[frozenset[str]] = frozenset({"RUB", "RUR", "SUR", "643"})
_CURRENCY: Final[tuple[str, ...]] = ("currencyCode", "currency", "currency_code")

#: Код биржи внутри блока «Лимитов». Рублёвых блоков бывает **несколько**:
#: документация (`.docs/broker-api/03-limits.md`) даёт каждому элементу
#: и `currencyCode`, и `exchange`, то есть рубли на фондовом рынке и рубли
#: на срочном — два законных блока, а не экзотика.
_EXCHANGE: Final[tuple[str, ...]] = ("exchange", "exchangeCode", "exchange_code")

#: Стоимость записи портфеля в рублях. Из неё складывается размер счёта.
_VALUE_RUB: Final[tuple[str, ...]] = ("currentValueRub", "current_value_rub")

#: «Заблокировано в заявках». Перебираем написания по той же причине,
#: что и у остальных полей: в таблице документации встречаются опечатки,
#: и одно-единственное написание — это ставка на то, что повезло.
_LOCKED: Final[tuple[str, ...]] = ("locked", "lockedSum", "locked_sum", "blocked")

#: Количество в позиции. Отдельная константа, а не строка на месте: именно
#: это поле решает, есть позиция или нет, и промах по имени читается
#: как «позиции нет» — то есть как разрешение войти поверх открытой.
_QUANTITY: Final[tuple[str, ...]] = ("quantity", "balance", "qty")

#: Записи портфеля, которые позицией **не являются**: деньги и лимиты.
#:
#: ⚠️ Найдено ревью 05.09.2026. Разбор не смотрел на `type` вовсе, и денежная
#: запись `{"type": "moneyLimit", "ticker": "RUB", "quantity": 208750}`
#: превращалась в `Position(ticker="RUB", side=LONG, quantity=208750)`.
#: На **плоском** счёте `open_positions()` переставал быть пустым, и сверка
#: позиции после обрыва (решение 0020 — «выровнять и встать») видела
#: призрачную позицию и пыталась закрыть то, чего нет.
#:
#: У денежной записи `quantity` — это **сумма денег**, а не число контрактов.
#: Совпадение имени поля и есть причина, по которой промах здесь не виден
#: глазом: разбор отрабатывает без единой жалобы.
_NOT_A_POSITION: Final[frozenset[str]] = frozenset(
    {"moneylimit", "futureslimit", "otclimit"}
)

#: Записи портфеля, которые позицией **являются**: бумаги и фьючерсы.
#: Оба списка выписаны из перечисления `type` документации
#: (`.docs/broker-api/04-portfolio.md`): `moneyLimit`, `depoLimit`,
#: `futuresLimit`, `futuresHolding`, `otcLimit`. Нашего фьючерса касается
#: `futuresHolding`.
_IS_A_POSITION: Final[frozenset[str]] = frozenset({"depolimit", "futuresholding"})


def _kind(value: str | None) -> str:
    """Тип записи портфеля без регистра и разделителей.

    `futuresHolding`, `FUTURES_HOLDING` и `futures_holding` — одно и то же
    слово в трёх соглашениях об именовании, и различать их значило бы
    отказывать в работе из-за стиля. Различать надо **слова**, а не написания:
    как именно сервер пишет это поле, живьём не проверено ни разу.
    """
    if value is None:
        return ""
    return value.strip().lower().replace("_", "").replace("-", "").replace(" ", "")


def _currency_of(block: Mapping[str, Any]) -> str | None:
    code = text(pick(block, _CURRENCY))
    return code.upper() if code else None


def _exchange_of(block: Mapping[str, Any]) -> str | None:
    code = text(pick(block, _EXCHANGE))
    return code.strip().upper() if code else None


def _ruble_block(
    container: object, what: str, exchange: str | None = None
) -> tuple[Mapping[str, Any] | None, str | None]:
    """Рублёвый блок контейнера «Лимитов» и, если не вышло, — почему.

    Контейнер документация описывает массивом: деньги и фьючерсные лимиты
    приходят **по валютам и биржам**. Прежде здесь стоял `first_mapping`,
    то есть «взять первый объект», и на счёте с валютным остатком первым
    оказался бы любой: доллары прочитались бы как рубли и попали прямиком
    в проверку свободных средств перед заявкой.

    Отбор в два шага, и второй заведён ревью 05.09.2026:

    1. **по валюте** — `RUB`/`RUR`/`SUR`/`643`;
    2. **по бирже** — если рублёвых блоков осталось несколько. У каждого
       элемента есть `exchange`, и рубли фондового рынка рядом с рублями
       срочного — **ожидаемая форма ответа**, а не экзотика. Прежде правило
       «рублёвый блок ровно один» объявляло такой ответ неоднозначным,
       все три числа оставались пустыми, а `require_money()` бросал отказ,
       называя имена полей, — и разбор уходил искать опечатку в написании
       вместо настоящей причины.

    Блок один и валюту не назвал — берём его. Во всех остальных случаях
    возвращаем `None` **вместе с текстом причины**: пустое число видно
    наверху и останавливает бой, сложенные наугад числа — нет, но и молчание
    про причину стоит времени в торговый день.
    """
    if isinstance(container, Mapping):
        items = [container]
    elif isinstance(container, list):
        items = [item for item in container if isinstance(item, Mapping)]
    else:
        return None, f"{what}: контейнер не объект и не массив"
    if not items:
        return None, f"{what}: контейнер пуст"
    rubles = [item for item in items if _currency_of(item) in _RUBLE_CODES]
    if len(rubles) == 1:
        return rubles[0], None
    if len(rubles) > 1:
        return _by_exchange(rubles, what, exchange)
    if len(items) == 1 and _currency_of(items[0]) is None:
        return items[0], None
    seen = ", ".join(sorted({_currency_of(item) or "без валюты" for item in items}))
    return None, f"{what}: рублёвого блока нет, пришли валюты {seen}"


def _by_exchange(
    rubles: list[Mapping[str, Any]], what: str, exchange: str | None
) -> tuple[Mapping[str, Any] | None, str | None]:
    """Из нескольких рублёвых блоков выбрать блок нужной биржи.

    Биржа не угадывается: её называет вызывающий (`Account(exchange=...)`),
    и пока не назвал — ответ честно неоднозначен. Подставить сюда
    «первый рублёвый» значило бы сложить свободные деньги фондового рынка
    в проверку обеспечения срочного, где их нет.
    """
    where = ", ".join(sorted(_exchange_of(item) or "без биржи" for item in rubles))
    if exchange is None:
        return None, (
            f"{what}: рублёвых блоков {len(rubles)} (биржи: {where}), "
            "а какая из них наша — программе не сказано"
        )
    wanted = exchange.strip().upper()
    picked = [item for item in rubles if _exchange_of(item) == wanted]
    if len(picked) == 1:
        return picked[0], None
    return None, (
        f"{what}: рублёвых блоков {len(rubles)} (биржи: {where}), "
        f"биржа {wanted} нашлась {len(picked)} раз"
    )


def _futures_limit(futures: Mapping[str, Any] | None) -> float | None:
    """Текущий лимит открытых позиций срочного рынка.

    Имя поля источники пишут по-разному: сайт — `cbpLimit`, PDF — `cbpl.init`
    (точка означает либо вложенность, либо опечатку набора). Читаются оба.

    ⚠️ Отсюда убрано `cbplUsed` (05.09.2026). Оно стояло последним запасным
    написанием, а означает **другое число** — «позиции после последнего
    клиринга». Не найдя лимита, разбор молча отдавал бы под его именем
    занятое, то есть в панели стояло бы правдоподобное, но чужое число.
    """
    nested = first_mapping(pick(futures, ("cbpl",)))
    if nested is not None:
        found = number(pick(nested, ("init",)))
        if found is not None:
            return found
    return number(pick(futures, ("cbpLimit", "cbplInit", "cbpl.init")))


def parse_money(
    limits: Any, marginal: Any = None, *, exchange: str | None = None
) -> Money:
    """Разобрать ответ сервиса «Лимиты»; при наличии — маржинальные показатели.

    `marginal` — ответ сервиса «Маржинальные показатели». На этапе 1 его нет:
    документация даёт этот сервис **только по WebSocket**, а поток — задача
    Э1-5. Параметр оставлен, чтобы источник свободных денег появился без
    переделки разбора, и чтобы в тестах было видно, что при его наличии
    берётся именно он.

    `exchange` — код биржи, чьи рубли наши. Нужен, когда рублёвых блоков
    несколько (фондовый рынок и срочный — обычный случай, см. `_ruble_block`).
    Не назван — неоднозначный ответ остаётся неоднозначным, и причина
    попадает в `problems`, а не в догадку.
    """
    root = limits if isinstance(limits, Mapping) else {}
    futures, futures_problem = _ruble_block(
        pick(root, _FUTURES_BLOCK), "фьючерсные лимиты", exchange
    )
    money_block, money_problem = _ruble_block(
        pick(root, _MONEY_BLOCK), "денежные лимиты", exchange
    )

    total = number(pick(money_block, ("value", "sum")))
    if total is None:
        quantity = first_mapping(pick(money_block, ("quantity",)))
        total = number(pick(quantity, ("value",)))
    locked = number(pick(money_block, _LOCKED))

    forts = first_mapping(pick(marginal if isinstance(marginal, Mapping) else {}, ("fortsStability",)))
    free = number(pick(forts, _FREE_CASH))
    if free is None and total is not None:
        # Отсутствующее «заблокировано в заявках» НЕ считается нулём. Правило
        # модуля («ноль по умолчанию запрещён») действует на все поля, а не
        # на два из трёх: молча подставленный ноль завышает свободные деньги
        # ровно на сумму в активных заявках, и расчёт объёма от процента
        # свободных средств завышается во столько же раз. Имена полей здесь
        # угаданы по таблице документации, в которой найдены опечатки, —
        # значит промах по имени вероятен, и он обязан быть виден.
        free = total - locked if locked is not None else None

    collateral_positions = number(pick(futures, _COLLATERAL_POSITIONS))
    collateral_orders = number(pick(futures, _COLLATERAL_ORDERS))

    missing: list[str] = []
    if free is None:
        missing.append("свободные средства")
    if collateral_positions is None:
        missing.append("гарантийное обеспечение под позициями")
    if collateral_orders is None:
        missing.append("гарантийное обеспечение под заявками")

    return Money(
        free_rub=free,
        money_total=total,
        money_locked=locked,
        collateral_positions=collateral_positions,
        collateral_orders=collateral_orders,
        futures_limit=_futures_limit(futures),
        variation_margin=number(pick(futures, _VARIATION_MARGIN)),
        missing=tuple(missing),
        problems=tuple(
            note for note in (money_problem, futures_problem) if note is not None
        ),
    )


def _shape(value: object) -> str:
    """Чем оказался ответ: тип и имена полей. **Без значений.**

    Значения из ответа портфеля в технический лог не идут: там номера счетов
    и состав позиций. Имена полей — то, что нужно для разбора, и они безопасны.
    """
    if isinstance(value, Mapping):
        names = sorted(str(key) for key in value)[:10]
        return f"объект с полями: {', '.join(names) or 'полей нет'}"
    # Ветки для массива здесь нет намеренно: массив — законный вид ответа,
    # до отказа он не доходит.
    return f"значение типа {type(value).__name__}"


def positions_list(portfolio: object) -> list[Any]:
    """Массив позиций из ответа «Портфеля» — в любом из двух видов.

    ⚠️ **Вид ответа у источников расходится, и живой сервер уже побеждал.**
    Страница сайта (`.docs/broker-api/04-portfolio.md`) обещает **голый
    массив**, официальный PDF — объект с контейнером `positions`. У соседнего
    метода, справочника инструментов, ровно это расхождение кончилось багом
    `B-014`: разбор ждал контейнер, сервер прислал массив, и программа молча
    решила, что такого тикера нет.

    Здесь цена той же ошибки выше на порядок. Пустой список позиций читается
    как «позиции нет» — то есть как разрешение войти. Это шаг 2 сверки после
    обрыва связи (`DOMAIN.md` §7): робот спрашивает у брокера **фактическую**
    позицию, и «нет» означает вход поверх уже открытой — двойной объём
    и прямая дорога к принудительному закрытию по марже.

    Поэтому принимаются оба вида, а третий — **отказ**, а не пустота.
    Ложная тревога стоит несделанной сделки, молчание — денег.
    """
    if isinstance(portfolio, list):
        return portfolio
    if isinstance(portfolio, Mapping):
        raw = pick(portfolio, ("positions",))
        if isinstance(raw, list):
            return raw
    # Текст короткий и без запятых-объяснений: `UnexpectedAnswer` вставляет
    # его в готовую фразу «Брокер ответил не так, как ожидает программа: …
    # Работа остановлена, чтобы не принимать решений по непонятным данным».
    # Почему именно остановлена, а не «позиций нет», — сказано выше, в этом
    # докстринге, и живёт в техническом тексте.
    raise UnexpectedAnswer(
        "портфель пришёл в неизвестном виде, и позиции прочитать нельзя",
        f"портфель: ожидался массив позиций или объект с полем positions, "
        f"пришло {_shape(portfolio)}",
    )


def parse_equity(portfolio: object, marginal: object = None) -> float | None:
    """Размер счёта в рублях. `None` означает «не знаем», а не ноль.

    От этого числа считается дневной лимит убытка (решение 0035), поэтому
    подставленный ноль означал бы лимит ноль — остановку на первом же убытке.

    Два источника, в порядке доверия:

    1. `portfolioCurrentValue.currentValueRub` сервиса «Маржинальные
       показатели» — прямое поле «текущая стоимость портфеля». Живёт только
       на вебсокете (`.docs/broker-api/27-ws-marginal-indicators.md`),
       поэтому приходит сюда параметром, когда поток появится;
    2. сумма `currentValueRub` по **всем** записям портфеля. Деньги в нём —
       такая же запись, типа `moneyLimit`, поэтому сумма даёт деньги плюс
       позиции. ⚠️ Живым подключением не сверялось.

    **Одной записи без стоимости достаточно, чтобы отказаться от суммы.**
    Иначе размер счёта занижался бы ровно на пропущенную запись, а заниженный
    размер счёта — это заниженный лимит убытка: робот останавливается раньше,
    чем решил владелец счёта, и никто не понимает почему.

    Пустой портфель тоже даёт `None`, а не ноль: пустой ответ гораздо чаще
    означает, что мы не там ищем, чем что счёт пуст.
    """
    current = first_mapping(
        pick(marginal if isinstance(marginal, Mapping) else {}, ("portfolioCurrentValue",))
    )
    direct = number(pick(current, _VALUE_RUB))
    if direct is not None:
        return direct

    items = [item for item in positions_list(portfolio) if isinstance(item, Mapping)]
    if not items:
        return None
    total = 0.0
    for item in items:
        value = number(pick(item, _VALUE_RUB))
        if value is None:
            return None
        total += value
    return total


def _is_position(item: Mapping[str, Any]) -> bool:
    """Эта запись портфеля — позиция? Незнакомый тип — отказ, а не догадка.

    Отсутствующий `type` тоже отказ. Он выглядит безобиднее незнакомого,
    а последствия у него хуже: без типа денежная запись неотличима
    от позиции ровно теми полями, по которым мы её и читаем.
    """
    kind = _kind(text(pick(item, ("type",))))
    if kind in _IS_A_POSITION:
        return True
    if kind in _NOT_A_POSITION:
        return False
    known = ", ".join(sorted(_IS_A_POSITION | _NOT_A_POSITION))
    raise UnexpectedAnswer(
        "в портфеле есть запись неизвестного вида, и понять, позиция это "
        "или деньги, нельзя",
        f"портфель: запись с type={text(pick(item, ('type',)))!r}; "
        f"известны (без регистра и разделителей) {known}",
    )


def parse_positions(portfolio: Any) -> tuple[tuple[Position, ...], str | None]:
    """Разобрать ответ сервиса «Портфель». Возвращает позиции и номер счёта.

    ⚠️ **Позицией становится не всякая запись.** Ответ портфеля перечисляет
    пять типов (`.docs/broker-api/04-portfolio.md`), и два из них — деньги
    и лимиты: у них в поле `quantity` лежит **сумма**, а не число контрактов.
    Отбор идёт двумя белыми списками, а незнакомый тип — **отказ**, а не
    молчаливое «наверное, позиция» и не молчаливое «наверное, деньги».
    Оба молчания стоят денег, и в разные стороны: призрак закрывают,
    пропущенную позицию открывают вторым объёмом.

    Номер счёта при этом читается из **любой** записи, включая денежную:
    на плоском счёте позиций нет вовсе, а номер счёта нужен и там.
    """
    account: str | None = None
    result: list[Position] = []

    for item in positions_list(portfolio):
        if not isinstance(item, Mapping):
            continue
        account = account or text(pick(item, ("agreementId", "agreement_id")))
        if not _is_position(item):
            continue
        ticker = text(pick(item, ("ticker",)))
        if ticker is None:
            continue
        # Ноль по умолчанию здесь запрещён так же, как для денег: `Side.of()`
        # читает ноль как «позиции нет», а сверка после обрыва по такому ответу
        # входит по текущей цене поверх уже открытой позиции — двойной объём
        # и путь к принудительному закрытию брокером.
        quantity = number(pick(item, _QUANTITY))
        if quantity is None:
            raise UnexpectedAnswer(
                "не удалось прочитать количество в позиции",
                f"портфель: у позиции {ticker} не найдено поле количества "
                f"(искали {', '.join(_QUANTITY)})",
            )
        result.append(
            Position(
                ticker=ticker,
                class_code=text(pick(item, ("board", "classCode", "class_code"))),
                side=Side.of(quantity),
                quantity=quantity,
                average_price=number(pick(item, ("balancePrice", "averagePrice"))),
                current_price=number(pick(item, ("currentPrice",))),
                lot=number(pick(item, ("ratioQuantity", "lotSize"))),
                price_step=number(pick(item, ("minimumStep",))),
                currency=text(pick(item, ("currency",))),
                expires_on=moment(pick(item, ("expireDate", "expirationDate"))),
                locked_for_futures=number(pick(item, ("lockedForFutures",))),
                display_name=text(pick(item, ("displayName",))),
            )
        )
    return tuple(result), account


class Account:
    """Чтение состояния счёта. Ничего не кэширует и заявок не подаёт.

    `exchange` — код биржи, чьи рубли считаются нашими. «Лимиты» приходят
    массивами по валюте **и бирже**, и на счёте, где есть и фондовый рынок,
    и срочный, рублёвых блоков два. Кто из них наш, слой брокера решать
    не вправе: это следует из торгуемого инструмента, а инструмент знает
    сборка. Не назван — неоднозначность остаётся неоднозначностью и видна
    в техническом тексте отказа (`Money.problems`).
    """

    def __init__(self, session: BrokerSession, *, exchange: str | None = None) -> None:
        self._session = session
        self._exchange = exchange

    async def snapshot(self) -> AccountSnapshot:
        """Спросить у брокера деньги, обеспечение и фактические позиции.

        Два запроса подряд, а не параллельно: параллельный старт удваивает
        мгновенную нагрузку на ограничение частоты ради экономии долей
        секунды, которых у нас с запасом — решения принимаются раз в свечу.

        ⚠️ **Отказ любого из двух запросов выходит наверх исключением, и
        половины снимка не бывает.** Это правило, а не следствие реализации:
        снимок с деньгами, но без позиций, читался бы как «денег столько,
        позиций нет» — то есть как разрешение войти. Вызывающий обязан
        различать «брокер не ответил» и «брокер ответил, что пусто»;
        для первого у него исключение, для второго — `funds_missing()`
        и `SNAPSHOT_MAX_AGE`.

        Темп опроса задаёт вызывающий: `SNAPSHOT_INTERVAL`. Своего цикла
        здесь нет намеренно — слой брокера никого не опрашивает по
        собственному почину и знать про торговое окно не должен.
        """
        limits = await self._session.read(LIMITS_PATH)
        portfolio = await self._session.read(PORTFOLIO_PATH)

        money = parse_money(limits, exchange=self._exchange)
        positions, account = parse_positions(portfolio)
        stored = self._session.stored()

        return AccountSnapshot(
            taken_at=now_utc(),
            account=account or stored.account,
            money=money,
            positions=positions,
            equity_rub=parse_equity(portfolio),
        )
