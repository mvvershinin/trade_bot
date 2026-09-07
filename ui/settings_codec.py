"""Настройки ↔ JSON: одна таблица укладки, по типу поля, а не по его имени.

Зачем отдельный модуль
----------------------
Файлов с настройками стало **два**: `userdata/settings.json`
(`app/settings_store.py`) и `userdata/settings-templates.json` — библиотека
шаблонов, которую открывает окно (`ui/templates.py`). Оба кладут в файл один
и тот же набор полей `Settings`, и вторая таблица укладки разошлась бы
с первой молча: настройка, заведённая завтра, сохранялась бы в настройках
и терялась в шаблонах.

Почему таблица живёт в `ui/`, а не в `app/`
-------------------------------------------
По направлению зависимостей (ARCHITECTURE.md §2): `ui/` не имеет права
импортировать `app/`, а библиотека шаблонов открывается из окна. Тот же довод
и тот же приём, что у `market/paths.py`, который держит расположение папки
`userdata/` в слое данных, а не в сборке.

Торгового правила здесь нет ни одного: модуль умеет превратить значение поля
в число, строку или список и обратно. Что это значение означает для робота,
решает `engine/`.

Как устроена полнота
--------------------
Список полей `Settings` здесь не повторяется ни разу: имена и типы берутся
у самого класса (`dataclasses.fields` плюс `typing.get_type_hints`), ключ
в файле равен имени поля, а как уложить значение — решает **таблица по типу**.
Отсюда следствие, ради которого это и сделано: поле, заведённое завтра,
сохраняется само. Забыть его негде.

Тип, которого в таблице нет, — **громкий отказ**, а не пропуск поля: молчаливый
пропуск означал бы настройку, которая выглядит сохранённой и не сохраняется.
Что таблица покрывает все типы `Settings`, стережёт тест.

⚠️ Ключи в файле — **имена полей латиницей** (правило 6 CLAUDE.md). Русские
ключи здесь были бы вторым списком имён, который разойдётся с первым молча.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import typing
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, time
from typing import Final

from ui.models import CalendarDay, Settings

__all__ = [
    "BadValue",
    "Codec",
    "codec_of",
    "encode_fields",
    "field_codecs",
    "read_fields",
    "shown",
]


class BadValue(ValueError):
    """Значение поля не годится. Текст — фраза для журнала, а не код."""



# ---------------------------------------------------------------------------
# Таблица укладки значений: по типу поля, а не по его имени
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Codec:
    """Как поле этого типа кладётся в файл и как достаётся обратно."""

    dump: Callable[[object], object]
    load: Callable[[object], object]


def _as_str(raw: object) -> str:
    if not isinstance(raw, str):
        raise BadValue(f"ожидалась строка, а записано {shown(raw)}")
    return raw


def _as_int(raw: object) -> int:
    # `bool` — подкласс `int`, и без этой проверки `true` в файле молча стало бы
    # единицей: объём 1 контракт из значения «да».
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise BadValue(f"ожидалось целое число, а записано {shown(raw)}")
    return raw


def _as_float(raw: object) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise BadValue(f"ожидалось число, а записано {shown(raw)}")
    return float(raw)


def _as_bool(raw: object) -> bool:
    if not isinstance(raw, bool):
        raise BadValue(f"ожидалось «да» или «нет», а записано {shown(raw)}")
    return raw


def _as_money(raw: object) -> float | None:
    """Тариф комиссии. `null` — «не задан», и это не то же самое, что ноль."""
    if raw is None:
        return None
    return _as_float(raw)


def _as_time(raw: object) -> time:
    try:
        return time.fromisoformat(_as_str(raw))
    except ValueError:
        raise BadValue(
            f"ожидалось время вида 10:05, а записано {shown(raw)}"
        ) from None


#: Длина негодного значения в тексте отказа. Строка на экран целиком —
#: это чужой файл в журнале решений; обрезка оставляет достаточно, чтобы
#: значение узнать.
_SHOWN_LIMIT: Final[int] = 40


def shown(raw: object) -> str:
    """Негодное значение в тексте отказа — коротко и без кавычек-ёлочек внутри."""
    text = json.dumps(raw, ensure_ascii=False, default=str)
    if len(text) <= _SHOWN_LIMIT:
        return text
    return text[: _SHOWN_LIMIT - 3] + "…"


def _as_calendar(raw: object) -> tuple[CalendarDay, ...]:
    """Календарь из файла: `{"2026-06-12": false}` → отметки владельца счёта.

    Ключ — дата, значение — торгуем ли. Такой вид выбран, чтобы файл читался
    глазами: в `userdata/settings.json` человек заглядывает ровно тогда, когда
    что-то пошло не так, и список пар «дата → да/нет» он поймёт без пояснений.

    ⚠️ Одна испорченная строка отвергает **весь календарь**, а не пропускает
    день молча. Это не строгость ради строгости: молчаливый пропуск означал бы
    день, который владелец счёта пометил нерабочим, а робот в него вышел
    торговать. Отказ виден строкой в журнале, и файл при этом цел.
    """
    if not isinstance(raw, dict):
        raise BadValue(
            f"ожидался список дней вида {{«2026-06-12»: false}}, а записано {shown(raw)}"
        )
    marks: list[CalendarDay] = []
    for key, trading in raw.items():
        try:
            day = date.fromisoformat(_as_str(key))
        except ValueError:
            raise BadValue(
                f"ожидалась дата вида 2026-06-12, а записано {shown(key)}"
            ) from None
        if not isinstance(trading, bool):
            raise BadValue(
                f"у дня {key} ожидалось «да» или «нет», а записано {shown(trading)}"
            )
        marks.append(CalendarDay(day=day, trading=trading))
    return tuple(sorted(marks, key=lambda mark: mark.day))


def _dump_calendar(value: object) -> object:
    """Календарь наружу словарём «дата → торгуем ли». Порядок — по дате."""
    if not isinstance(value, tuple) or not all(
        isinstance(mark, CalendarDay) for mark in value
    ):
        raise TypeError(f"поле объявлено календарём, а хранит {value!r}")
    return {
        mark.day.isoformat(): mark.trading
        for mark in sorted(value, key=lambda mark: mark.day)
    }


def _dump_time(value: object) -> object:
    """Время наружу строкой `10:05`. Не время — поломка таблицы, а не данных."""
    if not isinstance(value, time):
        raise TypeError(f"поле объявлено временем, а хранит {value!r}")
    return value.isoformat()


def _dump_enum(value: object) -> object:
    """Перечисление наружу своим значением, а не именем и не подписью."""
    if not isinstance(value, enum.Enum):
        raise TypeError(f"поле объявлено перечислением, а хранит {value!r}")
    return value.value


def _as_is(value: object) -> object:
    """Строки, числа и «да/нет» JSON умеет сам."""
    return value


#: Укладка по типу. Ключи — сами типы, а не их имена: имя типа пишется
#: строкой и опечатка в нём тихо выключила бы поле.
_CODECS: Final[dict[object, Codec]] = {
    str: Codec(dump=_as_is, load=_as_str),
    int: Codec(dump=_as_is, load=_as_int),
    float: Codec(dump=_as_is, load=_as_float),
    bool: Codec(dump=_as_is, load=_as_bool),
    time: Codec(dump=_dump_time, load=_as_time),
    float | None: Codec(dump=_as_is, load=_as_money),
    tuple[CalendarDay, ...]: Codec(dump=_dump_calendar, load=_as_calendar),
}


def _enum_codec(kind: type[enum.Enum]) -> Codec:
    """Укладка перечисления: наружу — значение, обратно — разбор по значению.

    Незнакомое значение — отказ поля, а не подмена первым элементом: «тип
    средней: SMMA» из файла более новой сборки обязан быть сказан вслух,
    иначе робот молча посчитает другую линию.
    """

    def load(raw: object) -> enum.Enum:
        try:
            return kind(raw)
        except ValueError:
            known = ", ".join(str(item.value) for item in kind)
            raise BadValue(
                f"значение {shown(raw)} программе неизвестно; она знает: {known}"
            ) from None

    return Codec(dump=_dump_enum, load=load)


def codec_of(kind: object) -> Codec:
    """Укладка для типа поля. Незнакомый тип — отказ вслух, а не пропуск поля."""
    codec = _CODECS.get(kind)
    if codec is not None:
        return codec
    if isinstance(kind, type) and issubclass(kind, enum.Enum):
        return _enum_codec(kind)
    raise TypeError(
        f"настройку типа {kind!r} программа сохранять не умеет. Добавьте укладку "
        "в таблицу `_CODECS` в ui/settings_codec.py — иначе поле будет "
        "выглядеть сохранённым и пропадать при перезапуске"
    )


def field_codecs() -> dict[str, Codec]:
    """Поля `Settings` и укладка каждого. Список берётся у самого класса."""
    hints = typing.get_type_hints(Settings)
    return {
        field.name: codec_of(hints[field.name])
        for field in dataclasses.fields(Settings)
    }


def read_fields(
    raw: Mapping[str, object], table: Mapping[str, Codec], start: Settings
) -> tuple[dict[str, object], list[str], list[str]]:
    """Поле за полем: что прочиталось, что отвергнуто, чего в файле нет.

    Негодное поле теряет **себя**, а не весь файл: остальные настройки
    остаются прочитанными, а про это говорится отдельной строкой с именем
    поля и с тем, что подставлено вместо него.
    """
    changes: dict[str, object] = {}
    refused: list[str] = []
    missing: list[str] = []
    for name, codec in table.items():
        if name not in raw:
            missing.append(name)
            continue
        try:
            changes[name] = codec.load(raw[name])
        except BadValue as error:
            refused.append(
                f"Настройка «{name}» в файле негодная: {error}. Взято прежнее "
                f"значение {getattr(start, name)!r}; остальные настройки "
                "прочитаны."
            )
    return changes, refused, missing


def encode_fields(values: Settings) -> dict[str, object]:
    """Настройки → то, что кладётся в JSON. Обходом полей, а не списком.

    Отсюда и берётся обещание «поле, заведённое завтра, сохранится само»:
    место, где поле можно забыть, здесь ровно одно — таблица укладки по типу,
    и незнакомый тип она называет вслух (`codec_of`).
    """
    return {
        name: codec.dump(getattr(values, name))
        for name, codec in field_codecs().items()
    }
