"""Сборка свечей нужного таймфрейма из минутных.

Откуда взято
------------
Ядро — функция `aggregate()` из архивного загрузчика
`reference/stand/moexdata.py` (выравнивание границы по `minute // step * step`
для размеров до часа и по `hour // (step/60)` для часа и выше; open от первой
минутки бара, close от последней, high/low — экстремумы, объём — сумма).
Поведение сохранено дословно: именно эта сборка построчно совпала с эталонным
набором. Добавлено то, чего в черновике не было и без чего слой опасен:
tz-aware время, явный отказ при дублирующихся минутках (иначе объём
удваивается молча), приведение времени минутки к началу минуты — чтобы дубль
ловился и тогда, когда источник прислал его с секундами, — учёт числа
собранных минуток и признак незакрытого бара.

Правило границ и времени свечи
------------------------------
**Свеча таймфрейма N минут с началом T собирается из минутных свечей, начало
которых лежит в полуинтервале [T, T + N).**

Для N = 5 свеча 10:05 включает минутки 10:05, 10:06, 10:07, 10:08 и 10:09.
Минутка 10:10 в неё **не** входит — она открывает следующую свечу.

Начало T кратно N: для N до часа отсчёт идёт от начала часа, для N от часа
и выше — от начала суток.

**У свечи хранится начало.** Время закрытия = начало + N: свеча 10:05
при N = 5 закрывается в 10:10. Торговое окно движок считает по времени
**закрытия** и с **строгими** неравенствами (ARCHITECTURE.md §6), поэтому
ошибка в границе бара здесь превращается в другой список сделок там.

Собираем **только из минуток**. Пересборка таймфрейма из уже пересобранных
свечей запрещена и невозможна: на вход принимаются только свечи с
таймфреймом 1 минута, всё остальное — отказ.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Literal

from market.candles import Candle, Timeframe, ensure_msk, floor_to_minute

__all__ = ["bar_start", "build_bars"]


def bar_start(moment: datetime, timeframe: Timeframe) -> datetime:
    """Начало бара, которому принадлежит момент времени.

    Граница считается от начала часа (для размеров до часа включительно)
    или от начала суток (для размеров от часа) — так же, как в архивном
    загрузчике.
    """
    moment = ensure_msk(moment)
    step = timeframe.minutes
    if step <= 60:
        floor_minute = (moment.minute // step) * step if step < 60 else 0
        return moment.replace(minute=floor_minute, second=0, microsecond=0)
    hours = step // 60
    return moment.replace(
        hour=(moment.hour // hours) * hours, minute=0, second=0, microsecond=0
    )


def build_bars(
    minutes: Iterable[Candle],
    timeframe: Timeframe,
    *,
    known_until: datetime | None = None,
    drop_unsettled: bool = False,
    on_duplicate: Literal["error", "skip"] = "error",
) -> list[Candle]:
    """Свечи таймфрейма `timeframe` из минутных свечей `minutes`.

    Правило границ — в шапке модуля.

    :param known_until:
        момент, до которого (не включая) минутные данные заведомо полны.
        Бар, чьё время закрытия больше этого момента, помечается
        `unsettled=True`: интервал ещё не закончился либо докачан не весь.
        `None` — «не знаем»; тогда незакрытых баров нет, потому что признак
        не на чем основать. Врать в эту сторону нельзя: пометка `unsettled`
        по догадке так же вредна, как её отсутствие.
    :param drop_unsettled:
        не отдавать незакрытые бары вовсе.
    :param on_duplicate:
        что делать, если одна и та же минута пришла дважды. По умолчанию —
        отказ: молчаливое сложение объёмов даёт правдоподобные, но неверные
        числа, и найти это потом не по чему. `"skip"` — оставить первую.

        Дубль ищется по **минуте**, а не по моменту: `10:05:00` от биржи
        и `10:05:07` из потока брокера — одна и та же минутка
        (`market.candles.floor_to_minute`). Сравнение «как пришло» пропустило
        бы её дважды и удвоило объём бара.

    Возвращает список, упорядоченный по времени. Порядок входа значения
    не имеет: минутки сортируются до сборки, иначе `open` и `close` бара
    зависели бы от того, в каком порядке страницы пришли с сервера.
    """
    seen: dict[datetime, Candle] = {}
    for candle in minutes:
        if candle.timeframe.minutes != 1:
            raise ValueError(
                f"на вход сборки подана свеча {candle.timeframe.name}, а не минутная. "
                "Таймфрейм собирается только из минуток: пересборка из уже "
                "пересобранных свечей накапливает ошибку округления границ"
            )
        moment = floor_to_minute(candle.time)
        if moment in seen:
            if on_duplicate == "error":
                raise ValueError(
                    f"свеча за {moment:%Y-%m-%d %H:%M} пришла дважды. Сложение "
                    "объёмов дало бы неверный бар без единого следа в данных"
                )
            continue
        seen[moment] = candle

    if known_until is not None:
        known_until = ensure_msk(known_until)

    bars: list[Candle] = []
    current_key: datetime | None = None
    o = h = l = c = 0.0
    volume = 0.0
    filled = 0

    def flush() -> None:
        nonlocal current_key
        assert current_key is not None
        candle = Candle(
            time=current_key,
            open=o,
            high=h,
            low=l,
            close=c,
            volume=volume,
            timeframe=timeframe,
            filled_minutes=filled,
            unsettled=(
                known_until is not None
                and current_key + timeframe.delta > known_until
            ),
        )
        if not (drop_unsettled and candle.unsettled):
            bars.append(candle)
        current_key = None

    for moment in sorted(seen):
        candle = seen[moment]
        key = bar_start(moment, timeframe)
        if key != current_key:
            if current_key is not None:
                flush()
            current_key = key
            o, h, l, c = candle.open, candle.high, candle.low, candle.close
            volume = candle.volume
            filled = 1
        else:
            h = max(h, candle.high)
            l = min(l, candle.low)
            c = candle.close
            volume += candle.volume
            filled += 1
    if current_key is not None:
        flush()
    return bars
