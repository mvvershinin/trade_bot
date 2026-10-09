"""Действующий контракт по периоду: какой фьючерс когда был ближним.

Зачем это существует
--------------------
MXU6 истёк 17.09.2026, а программа продолжала считать его своим: какой
контракт торгуется сейчас, нигде не хранилось, кроме строки в настройках.
Владелец счёта 27.09.2026 (решение 0061): на каждом отрезке времени
считается и показывается контракт, который тогда жил настоящей жизнью;
старые помечаются архивными и не удаляются; хвост нового контракта до его
рубежа не скачивается, кроме прогрева длиной в период средней.

Здесь — таблица `contract` в рабочей базе и всё, что её наполняет и читает.

Где рубеж
---------
**Правило не новое.** Рубеж — `market.chain.roll_day` (решение 0049):
первый день нового контракта после последнего дня, в который старый был
активнее. Объёмы — дневные свечи биржи (`IssClient.daily_volumes`),
а не минуты хвоста. Проверка, что правило одно: начала периодов здесь
совпадают с `legs_of_chain(...)[i].since` на той же цепочке — это тест.

Замер 27.09.2026 по дневным свечам ISS: 16.09 MXU6 278 346 против MXZ6
188 522, 17.09 — 132 031 против 385 057. Рубеж MXZ6 — **17.09.2026**,
период MXU6 кончается 16.09.

Что здесь НЕ делается
---------------------
* **Не режется прогон.** `pieces` отдаёт куски периода по контрактам;
  подать их движку с прогревом и закрыть позицию на стыке — фаза Ф3, `app/`.
* **Не решается переход в бою.** Робот останавливается, человек
  подтверждает (решение 0016) — это окно и брокер.
* **Не угадывается период средней.** Прогрев — обязательный аргумент
  `load_contract_minutes`, его называет вызывающий из настроек (правило 16).
"""

from __future__ import annotations

import logging
import pathlib
import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum

from market.candles import M5, MINUTE, MSK, Candle, Timeframe
from market.chain import Leg, daily_volume, roll_day
from market.iss import FetchResult, IssClient, IssUnknownInstrument, Market
from market.reports import LoadReport
from market.storage import CandleStore, ContractRow, Source
from market.sync import sync_minutes
from market.synthetic import is_synthetic, refuse_synthetic_for_trading

log = logging.getLogger(__name__)

__all__ = [
    "DAILY_DEPTH_DAYS",
    "WARMUP_LOOK_BACK_DAYS",
    "AdoptReport",
    "ChainNotQuarterly",
    "ContractError",
    "RefreshedRows",
    "ContractLoad",
    "ContractRequest",
    "Expiry",
    "ExpiryVerdict",
    "LegDays",
    "adopt_chain",
    "asset_of",
    "chain_around",
    "chain_covering",
    "check_days",
    "confirm_quarterly",
    "current_contract",
    "expiry_verdict",
    "load_contract_minutes",
    "monthly_assets",
    "periods_of_chain",
    "pieces",
    "refresh_contracts",
    "rows_of_asset",
    "runs_of",
]

#: Сколько календарных дней дневных свечей берётся по каждому контракту.
#: Контракт MX обращается около года (MXM5: 06.06.2024 — 19.06.2025 по
#: описанию биржи), пятьсот дней — вся его жизнь и одна страница ISS.
#: Число техническое: на рубеж оно не влияет, пока покрывает перекрытие пары.
DAILY_DEPTH_DAYS = 500

#: Дальше этого прогрев назад не ищется, в календарных днях. Ищется по дню,
#: пропуская выходные и праздники; две недели — дольше любого перерыва
#: срочного рынка. Не набрался прогрев — это видно в `ContractLoad.problem`.
WARMUP_LOOK_BACK_DAYS = 14


class ContractError(ValueError):
    """Таблица контрактов не отвечает на вопрос — и сказать это надо вслух."""


class ChainNotQuarterly(ContractError):
    """У актива есть месячные контракты: квартальная цепочка для него ложна.

    Отдельный вид отказа, а не просто `ContractError`: вызывающий на нём
    не повторяет уточнение укороченной цепочкой, а уходит на загрузку
    по дням, как до решения 0061.

    `monthly` — месячный код, который биржа назвала (BRX6), и его последний
    день обращения. По ним `refresh_contracts` оставляет в таблице след
    (`D-125`): признак «актив месячный» переживает перезапуск программы.
    """

    def __init__(self, message: str, *, monthly: str = "",
                 last_trade_day: date | None = None) -> None:
        super().__init__(message)
        self.monthly = monthly
        self.last_trade_day = last_trade_day


# ---------------------------------------------------------------------------
# Соседи по цепочке
# ---------------------------------------------------------------------------

#: Месяцы экспирации квартальных фьючерсов Мосбиржи: март, июнь, сентябрь,
#: декабрь. Регламент биржи, а не настройка (правило 16 разрешает константу).
QUARTER_MONTHS = "HMUZ"

#: Код актива — две буквы, **вторая бывает строчной**: `Si`, `Eu`, `Su`
#: (живой запрос ISS 28.09.2026 по всем фьючерсам FORTS: у Si, Eu, MX, RI,
#: GD, CR, MM, SR — только H, M, U, Z; у BR и NG — месячные). Прежний
#: шаблон `[A-Z]{2}` не признавал `SiZ6` квартальным (`D-122`).
_QUARTERLY = re.compile(r"^([A-Z][A-Za-z])([HMUZ])(\d)$")

#: Месяцы экспирации по коду биржи, январь…декабрь. Регламент биржи.
MONTH_CODES = "FGHJKMNQUVXZ"

#: Код **месячного** контракта — месяц вне `QUARTER_MONTHS`. Такой код
#: попадает в таблицу `contract` одним способом: его назвала биржа при
#: отказе квартальной цепочки (`ChainNotQuarterly`), и он там — признак
#: «у актива месячные контракты» (`monthly_assets`, `D-125`).
_MONTHLY = re.compile(r"^([A-Z][A-Za-z])([FGJKNQVX])(\d)$")


def monthly_assets(rows: Sequence[ContractRow]) -> set[str]:
    """Активы, у которых таблица знает месячный контракт (BR по BRX6).

    Признак хранится **строкой таблицы**, а не отдельным полем: схема не
    меняется, а сама строка — правда биржи (код и его срок), а не наша
    пометка. Квартальную цепочку она не портит: `rows_of_asset` месячных
    кодов не берёт.
    """
    return {
        found.group(1) for row in rows if (found := _MONTHLY.match(row.symbol)) is not None
    }


def asset_of(symbol: str) -> str:
    """Базовый актив квартального кода: `MXZ6` → `MX`, `RIZ6` → `RI`.

    Таблица `contract` хранит цепочки **всех** загруженных активов разом:
    загрузка RIZ6 пишет RIM6…RIH7 рядом с MXU6…MXZ6. Каждый читатель
    таблицы обязан смотреть только на цепочку своего актива — иначе периоды
    двух активов «налезают», а тестер режет прогон RI кусками MX.

    :raises ContractError: код не квартальный фьючерс (`chain_around`).
    """
    found = _QUARTERLY.match(symbol)
    if found is None:
        raise ContractError(
            f"«{symbol}» не похож на код квартального фьючерса (например, MXZ6): "
            "базовый актив по нему не определить"
        )
    return found.group(1)


def rows_of_asset(rows: Sequence[ContractRow], asset: str) -> list[ContractRow]:
    """Строки таблицы, принадлежащие цепочке `asset`."""
    return [
        row for row in rows
        if (found := _QUARTERLY.match(row.symbol)) is not None and found.group(1) == asset
    ]


def confirm_quarterly(client: IssClient, symbol: str) -> None:
    """Убедиться у биржи, что у актива `symbol` нет месячных контрактов.

    Квартальная цепочка (`chain_around`) для BR ложна: у нефти контракт
    каждый месяц, и между BRU6 и BRZ6 живут BRV6 и BRX6. Проверка — описание
    **месячного** кода прямо перед `symbol` (для MXZ6 — MXX6): биржа его
    не знает — месячных нет. Живой запрос 27.09.2026: MXX6, RIX6, GDX6 —
    пустое описание, BRX6 — есть.

    Сбой сети подтверждением не считается и летит дальше как есть.

    :raises ChainNotQuarterly: промежуточный месячный код биржа знает.
    :raises ContractError: код не квартальный.
    """
    asset = asset_of(symbol)
    found = _QUARTERLY.match(symbol)
    assert found is not None  # проверено `asset_of`
    month, year = found.group(2), found.group(3)
    between = f"{asset}{MONTH_CODES[MONTH_CODES.index(month) - 1]}{year}"
    try:
        last = client.last_trade_day(between)
    except IssUnknownInstrument:
        return
    raise ChainNotQuarterly(
        f"у актива {asset} есть месячные контракты (биржа знает {between}): "
        f"квартальная цепочка вокруг {symbol} для него ложна, рубежи по ней "
        "считать нельзя. История грузится по дням, склейки по контрактам нет",
        monthly=between,
        last_trade_day=last,
    )


def chain_around(symbol: str, *, before: int = 2, after: int = 1) -> list[str]:
    """Квартальные коды вокруг `symbol`, в порядке экспирации.

    `MXZ6` → `MXM6, MXU6, MXZ6, MXH7`.

    Зачем **два** предыдущих, а не один: у первого контракта цепочки начала
    периода нет по построению (`periods_of_chain`), и `pieces` его
    выбрасывает. С одним предыдущим период уходящего контракта на графике
    не появился бы вовсе. Следующий нужен, чтобы увидеть новый рубеж.

    Год — одна цифра, как в коде биржи: `MXH0` идёт за `MXZ9`.

    :raises ContractError: код не квартальный фьючерс вида `MXZ6` — соседей
        угадывать нельзя, а качать хвост «на всякий случай» решение 0061
        запрещает.
    """
    found = _QUARTERLY.match(symbol)
    if found is None:
        raise ContractError(
            f"«{symbol}» не похож на код квартального фьючерса (например, MXZ6): "
            "соседние контракты по нему не вычислить, а значит, не найти "
            "и рубеж, с которого грузить историю"
        )
    base, month, year = found.group(1), found.group(2), int(found.group(3))
    index = year * len(QUARTER_MONTHS) + QUARTER_MONTHS.index(month)
    size = 10 * len(QUARTER_MONTHS)
    codes = []
    for shift in range(-before, after + 1):
        step = (index + shift) % size
        codes.append(
            f"{base}{QUARTER_MONTHS[step % len(QUARTER_MONTHS)]}"
            f"{step // len(QUARTER_MONTHS)}"
        )
    return codes


#: Дальше этого цепочка назад не удлиняется, в кварталах. Год в коде биржи —
#: одна цифра: восемь лет назад от MXZ6 — MXZ8 2018-го, а MXZ8 2028-го биржа
#: может уже знать. Не настройка — следствие формата кода.
_FARTHEST_QUARTERS_BACK = 4 * 8


def chain_covering(symbol: str, since: date, today: date) -> list[str]:
    """Цепочка вокруг `symbol`, удлинённая назад так, чтобы покрыть день `since`.

    `chain_around` берёт два предыдущих контракта — этого хватает, чтобы
    датировать уходящий. Отрезок с 17.06 при коде MXZ6 упирается в MXM6,
    а начало его периода датирует только MXH6: без него `pieces` выкинул бы
    MXM6, и 17.06 остался бы днём без контракта. Число кварталов — вывод
    из дат, а не настройка: от квартала `since` до квартала `symbol` плюс
    один контракт, датирующий первый.

    :raises ContractError: код не квартальный (`chain_around`).
    """
    found = _QUARTERLY.match(symbol)
    if found is None:
        return chain_around(symbol)  # отказ — словами `chain_around`
    month, digit = found.group(2), int(found.group(3))
    # Год кода — по сегодняшнему: биржа выставляет контракты на год-два
    # вперёд, значит MXZ9 в 2026-м — это 2019-й, а MXH7 — 2027-й.
    year = today.year - today.year % 10 + digit
    if year > today.year + 1:
        year -= 10
    expiry = year * 4 + QUARTER_MONTHS.index(month)
    first = since.year * 4 + (since.month - 1) // 3
    back = min(max(2, expiry - first + 1), _FARTHEST_QUARTERS_BACK)
    return chain_around(symbol, before=back)


# ---------------------------------------------------------------------------
# Периоды по рубежам
# ---------------------------------------------------------------------------

def periods_of_chain(
    volumes: Sequence[tuple[str, Mapping[date, float]]],
) -> list[ContractRow]:
    """Периоды ближней жизни контрактов цепочки — по рубежам `roll_day`.

    На вход — контракты **в порядке экспирации** со своими дневными объёмами.
    У первого начало не установлено: его датировал бы предыдущий, которого
    в цепочке нет, — ровно как в `legs_of_chain`. Конец каждого — день перед
    рубежом следующего. У последнего, чей рубеж найден, конца нет: он
    действующий.

    Рубеж **последней** пары может не найтись: следующий контракт ещё
    не обогнал ближний. Это не ошибка, а обычное состояние между экспирациями:
    ближний остаётся действующим, у следующего нет ни начала, ни конца.
    Рубеж, не нашедшийся в середине цепочки, — ошибка: это недокачанная
    история, и молча резать её нельзя.

    :raises ContractError: цепочка пуста, рубеж в середине не нашёлся либо
        рубежи идут не по возрастанию.
    """
    if not volumes:
        raise ContractError("цепочка пуста: периоды считать не из чего")
    starts: list[date] = []
    for index, ((old_symbol, old), (new_symbol, new)) in enumerate(
        zip(volumes, volumes[1:], strict=False)
    ):
        day = roll_day(old, new)
        if day is None:
            if index + 2 == len(volumes):
                break  # следующий ещё не обогнал ближний
            raise ContractError(
                f"рубеж между «{old_symbol}» и «{new_symbol}» не нашёлся, "
                "а за ними цепочка продолжается: похоже на недокачанные объёмы"
            )
        if starts and day <= starts[-1]:
            raise ContractError(
                f"рубеж «{new_symbol}» ({day}) не позже предыдущего "
                f"({starts[-1]}): цепочка не в порядке экспирации"
            )
        starts.append(day)
    rows: list[ContractRow] = []
    for index, (symbol, _) in enumerate(volumes):
        since = starts[index - 1] if 0 < index <= len(starts) else None
        until = starts[index] - timedelta(days=1) if index < len(starts) else None
        rows.append(ContractRow(symbol=symbol, active_from=since, active_to=until))
    return rows


class RefreshedRows(list[ContractRow]):
    """Строки таблицы после уточнения — и что человеку сказать сверх них.

    Список, а не новый тип ответа: кто ждёт `list[ContractRow]`, получает его
    же. `said` — строки для человека: коды, которых биржа не знает (`B-054`).
    """

    def __init__(self, rows: Iterable[ContractRow], said: Sequence[str] = ()) -> None:
        super().__init__(rows)
        self.said: tuple[str, ...] = tuple(said)


def _known_last_days(
    client: IssClient, symbols: Sequence[str], said: list[str]
) -> dict[str, date | None]:
    """Последний день обращения по кодам цепочки; незнакомые бирже — с края прочь.

    Незнакомый код с края (дальний, ещё не вышедший, или старый) выпадает
    вслух, остальные остаются. Посреди цепочки выбросить нельзя: рубеж
    посчитался бы между несоседними контрактами. Порядок — как в `symbols`.

    :raises ContractError: незнакомый код посреди цепочки либо знакомых нет.
    """
    known: dict[str, date | None] = {}
    unknown: list[str] = []
    for symbol in symbols:
        try:
            known[symbol] = client.last_trade_day(symbol)
        except IssUnknownInstrument:
            unknown.append(symbol)
    if not known:
        raise ContractError(
            f"биржа не знает ни одного кода цепочки {', '.join(symbols)}: "
            "уточнять таблицу контрактов не по чему. Проверьте коды"
        )
    kept = list(known)
    first, last = symbols.index(kept[0]), symbols.index(kept[-1])
    inside = [symbol for symbol in unknown if first < symbols.index(symbol) < last]
    if inside:
        raise ContractError(
            f"биржа не знает {', '.join(inside)} посреди цепочки "
            f"{', '.join(symbols)}: рубежи вокруг него не посчитать. "
            "Проверьте коды"
        )
    for symbol in unknown:
        text = (
            f"Биржа не знает контракта {symbol}: срок и период по нему "
            "не уточнены, остальные коды цепочки уточнены. Если это дальний "
            "контракт, он, вероятно, ещё не вышел в обращение"
        )
        log.warning("%s", text)
        said.append(text)
    return known


def refresh_contracts(
    store: CandleStore,
    client: IssClient,
    symbols: Sequence[str],
    *,
    market: Market,
    now: datetime | None = None,
) -> RefreshedRows:
    """Уточнить периоды у биржи: последний день обращения и дневные объёмы.

    Цепочка может быть короткой — две последние пары хватает, чтобы увидеть
    новый рубеж. Начало первого контракта в ней не известно, и запись его
    не сотрёт: `CandleStore.put_contracts` пустым известное не заменяет.
    Ходит в сеть; из окна — через поток данных, как любая загрузка.
    `now` — момент проверки; `None` — сейчас. Задаётся в тестах.

    Все коды — одного актива, и цепочка подтверждается у биржи квартальной
    (`confirm_quarterly`) **до** записи: квартальных строк месячного актива
    в таблице не появляется. Появляется одна — месячный код, названный
    биржей в отказе (`monthly_assets`, `D-125`).

    ⚠️ Объёмы берутся **по вчера**, не по сегодня. Сегодняшняя дневная свеча
    не закрыта: утром новый контракт может обогнать старый, к вечеру отстать.
    Рубеж, поставленный по неполному дню, `put_contracts` назавтра не снимет
    (пустое известное не стирает), и старый ближний так и числился бы
    архивным.

    Код, которого биржа не знает, с края цепочки выпадает, остальные
    уточняются (`B-054`): чаще всего это дальний контракт, ещё не вышедший
    в список. Выпавший называется строкой в технический лог и в ответе
    (`RefreshedRows.said`) — текст для человека.

    :raises ChainNotQuarterly: у актива есть месячные контракты.
    :raises ContractError: коды разных активов либо не квартальные; биржа
        не знает кода посреди цепочки либо ни одного кода.
    """
    now = now or datetime.now(MSK)
    today = now.astimezone(MSK).date()
    assets = {asset_of(symbol) for symbol in symbols}
    if len(assets) != 1:
        raise ContractError(
            f"цепочка {', '.join(symbols)} — не один актив: периоды разных "
            "активов в одну цепочку не складываются"
        )
    # Проверяется месяц перед **первым** кодом: он в прошлом, и биржа его
    # точно перечислила бы, будь он. Дальний (BRG7) мог ещё не выйти в список.
    try:
        confirm_quarterly(client, symbols[0])
    except ChainNotQuarterly as refused:
        # Квартальных строк не пишется ни одной; пишется только названный
        # биржей месячный код — след, по которому признак «актив месячный»
        # переживает перезапуск (`D-125`).
        #
        # ⚠️ Сбой записи следа не подменяет отказ: вызывающий по
        # `ChainNotQuarterly` уходит на загрузку по дням, а `sqlite3.Error`
        # вместо него оборвал бы загрузку целиком. Сбой говорится вслух —
        # в технический лог и в текст самого отказа (правило 13).
        try:
            store.put_contracts(
                [ContractRow(refused.monthly, last_trade_day=refused.last_trade_day)],
                now=now,
            )
        except sqlite3.Error as failure:
            log.warning("след месячного актива %s не записан: %s", refused.monthly, failure)
            raise ChainNotQuarterly(
                f"{refused}. Признак «актив месячный» в таблицу контрактов "
                f"не записан ({failure}): после перезапуска программа спросит "
                "биржу снова",
                monthly=refused.monthly,
                last_trade_day=refused.last_trade_day,
            ) from failure
        raise refused
    said: list[str] = []
    last_days = _known_last_days(client, symbols, said)
    chain: list[tuple[str, dict[date, float]]] = []
    closed = today - timedelta(days=1)
    for symbol, last in last_days.items():
        till = min(closed, last) if last is not None else closed
        chain.append(
            (
                symbol,
                client.daily_volumes(
                    symbol,
                    market=market,
                    date_from=till - timedelta(days=DAILY_DEPTH_DAYS),
                    date_to=till,
                ),
            )
        )
    rows = [
        ContractRow(
            symbol=row.symbol,
            last_trade_day=last_days[row.symbol],
            active_from=row.active_from,
            active_to=row.active_to,
        )
        for row in periods_of_chain(chain)
    ]
    store.put_contracts(rows, now=now)
    wanted = set(symbols)
    return RefreshedRows((row for row in store.contracts() if row.symbol in wanted), said)


# ---------------------------------------------------------------------------
# Чтение таблицы
# ---------------------------------------------------------------------------

def current_contract(store: CandleStore, *, today: date, asset: str) -> ContractRow:
    """Контракт актива `asset`, ближний сейчас: начало есть, конца нет. Ровно один.

    Цепочки других активов не смотрятся: RIZ6 действующим рядом с MXZ6 —
    не противоречие таблицы, а другой актив.

    ⚠️ Истёкший «действующий» — не редкость, а ожидаемое состояние после
    переноса цепочки без биржи: у MXU6 конца нет, потому что про MXZ6 база
    ещё не знает. Отдать его молча — значит снова торговать истёкшим кодом,
    ровно та беда, с которой всё началось. Поэтому отказ вслух.
    Последний день обращения неизвестен — тоже отказ: проверить нечем,
    а это ровно состояние после переноса цепочки без биржи.

    :raises ContractError: действующего нет, их несколько, срок не проверен
        либо контракт истёк.
    """
    open_rows = [row for row in rows_of_asset(store.contracts(), asset) if row.current]
    if not open_rows:
        raise ContractError(
            f"в базе нет действующего контракта {asset}: цепочки этого актива "
            "в таблице контрактов нет или все её периоды закрыты. Уточните её "
            "у биржи загрузкой истории"
        )
    if len(open_rows) > 1:
        names = ", ".join(row.symbol for row in open_rows)
        raise ContractError(
            f"действующих контрактов несколько: {names}. Период открыт "
            "у каждого — таблица противоречит сама себе"
        )
    row = open_rows[0]
    if row.last_trade_day is None:
        raise ContractError(
            f"«{row.symbol}» числится действующим, но срок его обращения "
            "не проверен у биржи: так выглядит перенос цепочки без биржи, "
            "и контракт мог давно истечь. Уточните таблицу у биржи"
        )
    if row.last_trade_day < today:
        raise ContractError(
            f"«{row.symbol}» числится действующим, но обращался по "
            f"{row.last_trade_day:%d.%m.%Y}. Следующий контракт базе не известен — "
            "уточните таблицу у биржи вместе с ним"
        )
    return row


class Expiry(Enum):
    """Можно ли роботу работать этим кодом — по таблице контрактов.

    Порядок значимости сверху вниз. Первые три — **отказ**: поток котировок
    не включается, заявка не может уйти никаким путём. `NEAR` — остановка
    робота перед экспирацией (ТЗ §4.6, решение 0016). `UNKNOWN` — срок
    не проверен, и об этом говорится вслух: остановка перед экспирацией
    на таком коде не сработает.
    """

    SYNTHETIC = "synthetic"
    EXPIRED = "expired"
    ARCHIVED = "archived"
    NEAR = "near"
    UNKNOWN = "unknown"
    OK = "ok"

    @property
    def refused(self) -> bool:
        """Этим кодом нельзя ни подписываться, ни торговать."""
        return self in {Expiry.SYNTHETIC, Expiry.EXPIRED, Expiry.ARCHIVED}


@dataclass(frozen=True, slots=True)
class ExpiryVerdict:
    """Ответ `expiry_verdict`: вид и фраза для человека (пусто при `OK`)."""

    kind: Expiry
    text: str = ""


def expiry_verdict(
    rows: Sequence[ContractRow] | None, symbol: str, *, today: date, halt_days: int
) -> ExpiryVerdict:
    """Можно ли работать кодом `symbol` сегодня, и сколько до экспирации.

    Цепочка проверок, порядок — данные значимости (`Expiry`): собранный ряд
    отвергается без таблицы вовсе; истёкший и архивный — по таблице;
    близкая экспирация — по `last_trade_day` строки **этого** кода, а не
    действующего контракта. Остановка, когда до последнего дня обращения
    осталось `halt_days` дней или меньше: `0` — встать в сам последний день.

    `rows is None` — таблица ещё не прочитана: говорить про срок нечего,
    кроме собранного ряда.
    """
    try:
        refuse_synthetic_for_trading(symbol)
    except ValueError as error:
        return ExpiryVerdict(Expiry.SYNTHETIC, str(error))
    row = next((one for one in rows or () if one.symbol == symbol), None)
    last = row.last_trade_day if row is not None else None
    if last is not None and last < today:
        return ExpiryVerdict(
            Expiry.EXPIRED,
            f"{symbol} истёк: последний день обращения {last:%d.%m.%Y}. Код "
            "у брокера больше не торгуется; поставьте действующий контракт "
            "в поле «Инструмент».",
        )
    # Архивный — **до** проверки срока: конец периода ставит рубеж следующего
    # контракта, и последний день обращения ему не нужен. Перенос цепочки
    # без биржи оставляет срок пустым, а период закрытым — и такой код
    # не должен проходить как «срок неизвестен, работаем».
    if row is not None and row.active_to is not None and row.active_to < today:
        return ExpiryVerdict(
            Expiry.ARCHIVED,
            f"{symbol} — архивный контракт: ближним он был по "
            f"{row.active_to:%d.%m.%Y}, дальше торговля идёт следующим. Робот "
            "работает только действующим контрактом (решение 0061); переход — "
            "кнопкой на плашке или полем «Инструмент».",
        )
    if last is None:
        return ExpiryVerdict(
            Expiry.UNKNOWN,
            f"Срок обращения {symbol} программе не известен: таблица контрактов "
            "его не знает или он не проверен у биржи. Возможно, контракт уже "
            "истёк. Остановка перед экспирацией на этом коде не сработает — "
            "уточните срок у биржи: «Загрузить историю…» в меню «Программа».",
        )
    left = (last - today).days
    if left <= halt_days:
        return ExpiryVerdict(
            Expiry.NEAR,
            f"Экспирация {symbol}: последний день обращения {last:%d.%m.%Y}, "
            f"настройка велит встать за {halt_days} дн. до него. Робот "
            "на новый контракт сам не переходит (решение 0016): переключите "
            "инструмент, затем снимите остановку.",
        )
    return ExpiryVerdict(Expiry.OK)


def pieces(store: CandleStore, since: date, until: date, *, asset: str) -> list[Leg]:
    """Куски отрезка дат по контрактам актива `asset`: на каждом — свой ближний.

    Только цепочка своего актива: куски MX в прогон RIZ6 не попадают,
    и налезание периодов MX и RI противоречием не считается.

    Вход прогона по склейке (фаза Ф3). Кусок — пересечение отрезка с периодом
    контракта; у действующего конец периода — конец отрезка. Контракт без
    начала периода в куски не попадает: он либо первый в цепочке и датирует
    только следующий, либо ещё не стал ближним.

    Дни отрезка, не покрытые ни одним периодом, в кусках **отсутствуют**:
    подставлять туда соседний контракт значило бы считать по тому, что тогда
    не жило настоящей жизнью.

    :raises ContractError: отрезок задом наперёд, цепочки актива в таблице
        нет либо периоды двух контрактов налезают друг на друга.
    """
    if until < since:
        raise ContractError(f"отрезок задом наперёд: {since} … {until}")
    table = store.contracts()
    if asset in monthly_assets(table):
        # `D-127`: совет «загрузите историю» здесь был бы неправдой — у
        # месячного актива загрузка идёт по дням и цепочки не создаёт.
        raise ContractError(
            f"у актива {asset} месячные контракты: склейка по квартальной "
            "цепочке для него не строится, и загрузка истории её не создаст"
        )
    own = rows_of_asset(table, asset)
    if not any(row.active_from is not None for row in own):
        raise ContractError(
            f"в таблице контрактов нет цепочки {asset}: резать период по "
            "контрактам не по чему — её создаёт загрузка истории по "
            "квартальному коду актива (месяц H, M, U или Z), если биржа "
            "подтвердит, что месячных контрактов у него нет"
        )
    dated = sorted(
        (row.active_from, row.symbol, row)
        for row in own
        if row.active_from is not None
    )
    for (_, _, earlier), (later_from, _, later) in zip(dated, dated[1:], strict=False):
        if earlier.active_to is None or earlier.active_to >= later_from:
            raise ContractError(
                f"периоды «{earlier.symbol}» и «{later.symbol}» налезают друг "
                "на друга: один день не может принадлежать двум контрактам"
            )
    result: list[Leg] = []
    for active_from, symbol, row in dated:
        start = max(since, active_from)
        end = min(until, row.active_to) if row.active_to is not None else until
        if start <= end:
            result.append(Leg(symbol=symbol, since=start, until=end))
    return result


# ---------------------------------------------------------------------------
# Загрузка минут контракта: с рубежа, плюс прогрев, без хвоста
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ContractLoad:
    """Что вышло из загрузки минут одного контракта."""

    symbol: str
    #: Первый день тела: рубеж контракта либо начало куска, если оно позже.
    start: date
    #: Первый день, за которым ходили на биржу. Раньше него хвост не качался.
    since: date
    #: Сколько закрытых баров набралось перед `start` и сколько просили.
    warm_bars: int
    warmup_bars: int
    reports: list[LoadReport] = field(default_factory=list)
    #: Последний день тела — `ContractRequest.until`.
    until: date | None = None

    @property
    def problem(self) -> str | None:
        """Прогрев не набрался — одна строка человеку. `None` — набрался."""
        if self.warm_bars >= self.warmup_bars:
            return None
        return (
            f"{self.symbol}: перед {self.start:%d.%m.%Y} набралось "
            f"{self.warm_bars} закрытых баров из {self.warmup_bars} — средняя "
            "начнёт с недогретого значения"
        )


def _bars_before(
    store: CandleStore, symbol: str, timeframe: Timeframe, day: date, edge: date
) -> int:
    """Закрытые бары контракта с начала `day` до начала `edge`."""
    return len(
        store.bars(
            symbol,
            timeframe,
            since=datetime.combine(day, datetime.min.time(), MSK),
            until=datetime.combine(edge, datetime.min.time(), MSK),
            drop_unsettled=True,
        )
    )


@dataclass(frozen=True, slots=True)
class ContractRequest:
    """Что загрузить по контракту.

    Описанием, как `HistoryRequest`: условия путешествуют вместе и порознь
    смысла не имеют.
    """

    symbol: str
    market: Market
    until: date
    #: Сколько закрытых баров нужно перед рубежом — период средней из
    #: настроек (правило 16). Умолчания нет намеренно.
    warmup_bars: int
    timeframe: Timeframe = M5
    look_back_days: int = WARMUP_LOOK_BACK_DAYS
    #: Момент для учёта «день закончился». `None` — сейчас.
    now: datetime | None = None
    #: Ход загрузки — на каждой странице биржи, **в потоке данных**.
    progress: Callable[[FetchResult], None] | None = field(
        default=None, compare=False
    )
    #: С какого дня тело: кусок периода, начатый позже рубежа (B-069).
    #: Раньше рубежа не бывает — прижимается к нему. `None` — с рубежа.
    since: date | None = None


def load_contract_minutes(
    store: CandleStore, client: IssClient, request: ContractRequest
) -> ContractLoad:
    """Минуты контракта с его рубежа по `until` — и прогрев перед рубежом.

    Хвост до рубежа **не скачивается** (решение 0061), кроме прогрева:
    `request.warmup_bars` закрытых баров `request.timeframe` перед `active_from`. Число
    называет вызывающий — это период средней из настроек (правило 16).

    `request.since` позже рубежа — тело с него, и прогрев перед ним: кусок
    отрезка, начатого посреди периода контракта (B-069).

    ⚠️ **Прогрев качается целыми днями.** Биржа отдаёт минуты по датам,
    и учёт загруженного (`data_day`, от которого зависит закрытость бара)
    ведётся по дням. Меньше торгового дня взять нельзя; поэтому идём назад
    от рубежа по одному дню, пропуская выходные, и останавливаемся, как
    только баров хватило. Обычно это один день: у MX в сессии около
    170 пятиминуток. Ровно столько баров из скачанного берёт прогон
    (фаза Ф3), а не загрузка.

    :raises ContractError: контракта нет в таблице либо у него нет начала.
    :raises ValueError: прогрев меньше одного бара.
    """
    symbol, warmup_bars, now = request.symbol, request.warmup_bars, request.now
    if warmup_bars < 1:
        raise ValueError(f"прогрев меньше одного бара: {warmup_bars}")
    row = next((row for row in store.contracts() if row.symbol == symbol), None)
    if row is None or row.active_from is None:
        raise ContractError(
            f"у «{symbol}» нет начала периода в таблице контрактов: откуда "
            "начинать загрузку, неизвестно, а хвост до рубежа не качается"
        )
    start = max(row.active_from, request.since or row.active_from)
    load = ContractLoad(symbol, start, start, 0, warmup_bars, until=request.until)
    if request.until >= start:
        load.reports.append(
            sync_minutes(store, client, symbol, market=request.market,
                         since=start, until=request.until, now=now,
                         progress=request.progress)
        )
    day = start - timedelta(days=1)
    while day >= start - timedelta(days=request.look_back_days):
        load.warm_bars = _bars_before(store, symbol, request.timeframe, load.since, start)
        if load.warm_bars >= warmup_bars:
            break
        load.reports.append(
            sync_minutes(store, client, symbol, market=request.market,
                         since=day, until=day, now=now,
                         progress=request.progress)
        )
        load.since = day
        day -= timedelta(days=1)
    load.warm_bars = _bars_before(store, symbol, request.timeframe, load.since, start)
    return load


# ---------------------------------------------------------------------------
# Сверка дней с биржей после загрузки (B-069)
# ---------------------------------------------------------------------------

def runs_of(days: Sequence[date], missing: Iterable[date]) -> list[tuple[date, date]]:
    """Подряд идущие в `days` дни из `missing` — отрезками, обе границы включительно.

    «Подряд» — по последовательности `days`, а не по календарю: 07.09–16.09
    по торговым дням биржи — один отрезок, выходные внутри его не рвут.
    """
    wanted = set(missing)
    found: list[tuple[date, date]] = []
    first: date | None = None
    last: date | None = None
    for day in sorted(days):
        if day in wanted:
            first = first or day
            last = day
            continue
        if first is not None and last is not None:
            found.append((first, last))
        first = last = None
    if first is not None and last is not None:
        found.append((first, last))
    return found


@dataclass(frozen=True, slots=True)
class LegDays:
    """Сверка одного куска: дни, когда биржа торговала, и каких из них нет в базе."""

    leg: Leg
    #: Дни куска с объёмом по дневным свечам биржи.
    exchange: tuple[date, ...]
    #: Дни биржи, за которые в базе нет ни одной минуты, — отрезками.
    missing: tuple[tuple[date, date], ...] = ()
    #: Сколько дней биржи в базе есть.
    present: int = 0


def check_days(
    store: CandleStore, client: IssClient, legs: Sequence[Leg], *, market: Market
) -> list[LegDays]:
    """Сверить каждый кусок с дневными свечами биржи; с пропусков снять отметки.

    Загрузка спрашивает биржу по дням и отмечает спрошенное (`data_day`).
    Отмеченный день без минут — это либо праздник, либо сбой, и различить
    их может только биржа: дневная свеча с объёмом значит «торговали».
    Такой день без минут в базе — пропуск, и отметка с него снимается, чтобы
    следующая «Догрузить недостающее» спросила его снова (B-069: MXU6
    05.09–16.09 прошёл в прогон молча).

    Ходит в сеть; сравнение — по дням, не по минутам: неполный день
    пропуском не считается.
    """
    checked: list[LegDays] = []
    for leg in legs:
        volumes = client.daily_volumes(
            leg.symbol, market=market, date_from=leg.since, date_to=leg.until
        )
        traded = sorted(
            day for day, volume in volumes.items()
            if volume > 0 and leg.since <= day <= leg.until
        )
        have = set(store.trading_days(leg.symbol))
        missing = runs_of(traded, (day for day in traded if day not in have))
        for first, last in missing:
            store.forget_day_marks(leg.symbol, first, last)
        checked.append(LegDays(
            leg=leg,
            exchange=tuple(traded),
            missing=tuple(missing),
            present=sum(1 for day in traded if day in have),
        ))
    return checked


# ---------------------------------------------------------------------------
# Перенос цепочки из отдельной базы
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class AdoptReport:
    """Что перенёс `adopt_chain`: по контракту — минуты и отмеченные дни."""

    minutes: dict[str, int] = field(default_factory=dict)
    written: dict[str, int] = field(default_factory=dict)
    days: dict[str, int] = field(default_factory=dict)
    contracts: list[ContractRow] = field(default_factory=list)


def _close_by_the_next(store: CandleStore, symbol: str, *, now: datetime) -> None:
    """Закрыть период последнего перенесённого, если таблица знает рубеж следующего.

    `D-123`: перенос без биржи оставляет последний контракт (MXU6) открытым
    и без срока. Если в таблице уже есть контракт того же актива с более
    поздним началом периода (MXZ6 с 17.09), MXU6 ближним после этого рубежа
    не был: конец его периода — день перед рубежом. Без этого таблица
    держала бы два «действующих», а срок MXU6 читался бы как неизвестный.
    Следующего с рубежом нет — период остаётся открытым: про срок скажет
    `expiry_verdict` («уточните срок у биржи»).
    """
    rows = rows_of_asset(store.contracts(), asset_of(symbol))
    own = next((row for row in rows if row.symbol == symbol), None)
    if own is None or own.active_from is None or own.active_to is not None:
        return
    later = [
        row.active_from for row in rows
        if row.active_from is not None and row.active_from > own.active_from
    ]
    if later:
        # Конец — не позже последнего дня обращения, если он известен: при
        # разрыве в таблице (MXH5…MXM5 перенесены, а следующий известный
        # рубеж — MXU6 в 06.2026) «день перед рубежом» растянул бы MXM5
        # на год, и год он числился бы ближним.
        end = min(later) - timedelta(days=1)
        if own.last_trade_day is not None:
            end = min(end, own.last_trade_day)
        store.put_contracts([ContractRow(symbol, active_to=end)], now=now)


def _read_minutes(source: sqlite3.Connection, symbol: str) -> dict[Source, list[Candle]]:
    """Минуты контракта из базы-источника, по происхождению."""
    grouped: dict[Source, list[Candle]] = {}
    for ts, open_, high, low, close, volume, origin in source.execute(
        "SELECT ts, open, high, low, close, volume, source FROM minute_candle "
        "WHERE symbol = ? ORDER BY ts",
        (symbol,),
    ):
        grouped.setdefault(Source(str(origin)), []).append(
            Candle(
                time=datetime.fromtimestamp(int(ts), timezone.utc).astimezone(MSK),
                open=float(open_), high=float(high), low=float(low),
                close=float(close), volume=float(volume),
                timeframe=MINUTE, filled_minutes=1,
            )
        )
    return grouped


def adopt_chain(
    source_path: pathlib.Path,
    store: CandleStore,
    symbols: Sequence[str],
    *,
    now: datetime | None = None,
) -> AdoptReport:
    """Перенести минуты контрактов из отдельной базы в рабочую и заполнить периоды.

    Источник — `userdata/chain.sqlite3`: минуты MXM5…MXU6 там настоящие,
    с биржи. Он открывается **только на чтение** и не через `CandleStore`:
    тот поднял бы схему файла при открытии, то есть переписал бы чужую базу
    ради чтения. Собранный ряд (`@…`) не переносится — это не инструмент.

    Вместе с минутами переносятся отметки «день загружен» (`data_day`):
    без них бары на краю каждого дня считались бы незакрытыми, и прогон
    по перенесённой истории терял бы их молча (`CandleStore.bars`).

    Периоды считаются по дневным объёмам **из перенесённых минут** — тем же
    `roll_day`. Про контракт, которого в источнике нет (MXZ6), перенос
    не знает: последний перенесённый останется действующим, пока
    `refresh_contracts` не уточнит таблицу у биржи.

    :raises ContractError: среди кодов есть собранный ряд.
    :raises sqlite3.OperationalError: файла источника нет.
    """
    synthetic = [symbol for symbol in symbols if is_synthetic(symbol)]
    if synthetic:
        raise ContractError(
            f"{', '.join(synthetic)} — собранный ряд, а не контракт: переносятся "
            "только настоящие свечи биржи"
        )
    if len({asset_of(symbol) for symbol in symbols}) != 1:
        raise ContractError(
            f"цепочка {', '.join(symbols)} — не один актив: переносится "
            "цепочка одного актива за раз"
        )
    moment = now or datetime.now(MSK)
    report = AdoptReport()
    source = sqlite3.connect(f"{source_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        for symbol in symbols:
            for origin, candles in _read_minutes(source, symbol).items():
                stats = store.put_minutes(symbol, candles, origin)
                report.minutes[symbol] = report.minutes.get(symbol, 0) + len(candles)
                report.written[symbol] = report.written.get(symbol, 0) + stats.written
            marks = {
                date.fromisoformat(str(day)): int(count)
                for day, count in source.execute(
                    "SELECT day, candles FROM data_day WHERE symbol = ? AND settled = 1",
                    (symbol,),
                )
            }
            store.mark_days_requested(symbol, marks, counts=marks, now=moment)
            report.days[symbol] = len(marks)
    finally:
        source.close()
    volumes = [(symbol, daily_volume(store, symbol)) for symbol in symbols]
    store.put_contracts(periods_of_chain(volumes), now=moment)
    _close_by_the_next(store, symbols[-1], now=moment)
    wanted = set(symbols)
    report.contracts = [row for row in store.contracts() if row.symbol in wanted]
    return report
