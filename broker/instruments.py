"""Справочник инструментов: шаг цены, лот, стоимость шага, дата погашения.

ТЗ §4.2: «программа сама подставляет шаг цены, лот и стоимость шага —
вручную это не вводится». Источник — сервис `trade-api-information-service`
БКС, запрос по списку тикеров.

⚠️ **Дата экспирации срочного контракта.** ТЗ §4.2 требует её видеть в окне.
В ответе справочника поля с таким именем нет: документация называет
`maturityDate` — «дата погашения». В ответе **портфеля** отдельное поле
`expireDate` есть. Совпадают ли они для фьючерса, по документации сказать
нельзя, и на живом подключении это не проверялось. Поэтому здесь
`maturity` — то, что прислал справочник, без переименования в «экспирацию»,
а предупреждение о близкой экспирации строится по позиции из портфеля.

Живой запрос 04.09.2026 по `MXU6` вернул `"maturityDate": "20260917"`, что
сходится с известной датой экспирации MIX-9.26 (17.09.2026). То есть поле
для предупреждения **есть и заполнено**; подключает его `app/`, не этот слой.

⚠️ **Форма ответа: живой сервер прислал не то, что описано в документации —
исправлено 05.09.2026.**

И официальный PDF, и страница сайта описывают ответ как объект с контейнером
`instruments` («Массив инструментов», обязательный). Живой запрос 04.09.2026
`POST /api/v1/instruments/by-tickers` с телом `{"tickers":["MXU6"]}` вернул
**голый массив** без всякой обёртки (`.docs/broker-api/19-instruments.md`,
раздел «Проверено живым запросом»).

Разбор ждал только документированной формы и на живом ответе возвращал
**пустой кортеж**, молча. Это худший вид отказа: справочник — единственный
путь, которым в программу попадают шаг цены, стоимость шага и дата
экспирации, и «ничего не нашлось» читалось бы как «нет такого тикера».

Теперь читаются **обе** формы, а **третья — отказ вслух**: объект без поля
`instruments` считается сменой формата, а не поводом вернуть пустоту.
Порядок доверия соблюдён: живой сервер главнее и сайта, и PDF.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from broker.errors import NotFound, UnexpectedAnswer
from broker.parsing import moment, number, pick, text
from broker.session import INSTRUMENTS_BY_TICKERS_PATH, BrokerSession


@dataclass(frozen=True, slots=True)
class Instrument:
    """Карточка инструмента — то, что нельзя вводить руками."""

    ticker: str
    class_code: str | None
    display_name: str | None
    instrument_type: str | None
    currency: str | None
    lot: float | None
    price_step: float | None
    step_price: float | None
    scale: int | None
    maturity: datetime | None
    base_asset: str | None

    @property
    def complete_for_trading(self) -> bool:
        """Хватает ли карточки, чтобы считать объём и цену.

        Без шага цены нельзя округлить уровень тейка, без лота — посчитать
        объём, без стоимости шага — перевести движение цены в рубли.
        Неполная карточка — повод остановиться, а не подставить единицу.
        """
        return None not in (self.lot, self.price_step, self.step_price)

    def missing(self) -> tuple[str, ...]:
        absent: list[str] = []
        if self.lot is None:
            absent.append("размер лота")
        if self.price_step is None:
            absent.append("шаг цены")
        if self.step_price is None:
            absent.append("стоимость шага цены")
        return tuple(absent)


def parse_instruments(payload: Any) -> tuple[Instrument, ...]:
    """Разобрать ответ справочника. Незнакомая форма — отказ, а не пустота."""
    result: list[Instrument] = []
    for item in _listed(payload):
        if not isinstance(item, Mapping):
            continue
        ticker = text(pick(item, ("ticker",)))
        if ticker is None:
            continue
        scale_value = number(pick(item, ("scale",)))
        result.append(
            Instrument(
                ticker=ticker,
                class_code=_first_board(item),
                display_name=text(pick(item, ("displayName", "shortName"))),
                instrument_type=text(pick(item, ("instrumentType", "type"))),
                currency=text(pick(item, ("tradingCurrency", "currency"))),
                lot=number(pick(item, ("lotSize",))),
                price_step=number(pick(item, ("minimumStep",))),
                step_price=number(pick(item, ("stepPrice",))),
                scale=int(scale_value) if scale_value is not None else None,
                maturity=_maturity(pick(item, ("maturityDate",))),
                base_asset=text(pick(item, ("baseAsset", "baseAssetTicker"))),
            )
        )
    return tuple(result)


def _listed(payload: object) -> list[Any]:
    """Список инструментов из ответа — в любой из двух известных форм.

    Формы ровно две, и обе не выдуманы:

    * **голый массив** — так ответил живой сервер 04.09.2026;
    * **объект с полем `instruments`** — так описывают ответ и официальный
      PDF, и страница сайта.

    Третья форма — отказ вслух. Прежде здесь стояло `return ()`, и живой
    ответ брокера молча превращался в «ничего не нашлось»: справочник
    не работал ни разу, а выглядело это как отсутствие тикера.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        listed = pick(payload, ("instruments",))
        if isinstance(listed, list):
            return listed
        known = ", ".join(sorted(str(key) for key in payload)) or "пусто"
        raise UnexpectedAnswer(
            "справочник инструментов ответил не тем, чего программа ждёт",
            f"справочник: ответ-объект без списка `instruments`; пришли поля: {known}",
        )
    raise UnexpectedAnswer(
        "справочник инструментов ответил не тем, чего программа ждёт",
        f"справочник: тело ответа имеет тип {type(payload).__name__}, "
        "ожидались массив или объект",
    )


def _maturity(value: object) -> datetime | None:
    """Дата погашения. Всегда **с поясом** — либо ничего.

    ⚠️ Поле приходит в двух видах, и это не догадка: схема объявляет его
    `date-time`, а живой сервер 04.09.2026 прислал `"20260917"` — дату
    без времени и без пояса. `fromisoformat` понимает обе записи, но вторая
    даёт **наивный** момент.

    Наивное и осведомлённое значение в одном поле — мина: вызывающий,
    посчитавший «сколько дней до экспирации» вычитанием из `now_utc()`,
    падал бы с `TypeError` ровно на том контракте, который сейчас в работе,
    и работал бы на выдуманном из тестов. Поэтому поле приводится к одному
    виду: наивной дате приписывается UTC.

    ⚠️ **Что этим сказано и чего не сказано.** UTC приписан не потому,
    что мы знаем зону, а потому, что поле обязано быть однородным. Точность
    значения — **сутки**, и на внутридневных решениях его использовать
    нельзя: полночь UTC — это три часа ночи по Москве. Для предупреждения
    «скоро истекает контракт» этого достаточно с запасом в дни.
    """
    parsed = moment(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _first_board(item: Mapping[str, Any]) -> str | None:
    """Код класса лежит в массиве бордов, а не полем верхнего уровня."""
    boards = pick(item, ("boards",))
    if isinstance(boards, list):
        for board in boards:
            if isinstance(board, Mapping):
                code = text(pick(board, ("classCode", "class_code")))
                if code:
                    return code
    return text(pick(item, ("classCode", "board")))


class Instruments:
    """Справочник инструментов. Только чтение."""

    def __init__(self, session: BrokerSession) -> None:
        self._session = session

    async def by_tickers(self, tickers: Iterable[str]) -> tuple[Instrument, ...]:
        wanted = [ticker.strip() for ticker in tickers if ticker and ticker.strip()]
        if not wanted:
            return ()
        payload = await self._session.read(
            INSTRUMENTS_BY_TICKERS_PATH, method="POST", json={"tickers": wanted}
        )
        return parse_instruments(payload)

    async def one(self, ticker: str) -> Instrument:
        found = await self.by_tickers([ticker])
        for item in found:
            if item.ticker.strip().upper() == ticker.strip().upper():
                return item
        raise NotFound(f"справочник: тикер {ticker} не найден")
