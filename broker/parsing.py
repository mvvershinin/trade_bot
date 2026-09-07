"""Чтение полей из ответа брокера: осторожно и без догадок.

Ответы БКС описаны в документации таблицами, и часть имён в этих таблицах
набрана с опечатками (`cbpl.init`, `cbp1Planned`, `cbplusedForOrders`).
Какое написание приходит на самом деле, по таблице установить нельзя.
Отсюда правила разбора, общие для всего слоя:

1. поле ищется по нескольким написаниям и без учёта регистра;
2. если не нашлось ни одного — значение `None`, **а не ноль и не единица**;
3. `None` доходит до вызывающего и попадает в список недостающих полей.

Третье правило — главное. Разбор, подставляющий значение по умолчанию,
превращает «брокер не прислал размер обеспечения» в «обеспечение равно нулю»,
и программа входит в позицию, которую нечем держать.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping


def pick(source: Mapping[str, Any] | None, names: Iterable[str]) -> Any:
    """Найти поле по любому из написаний, без учёта регистра."""
    if not isinstance(source, Mapping):
        return None
    lowered = {str(key).lower(): value for key, value in source.items()}
    for name in names:
        found = lowered.get(name.lower())
        if found is not None:
            return found
    return None


def number(value: Any) -> float | None:
    """Число или `None`. `True` числом не считается: это чужой тип, влезший в поле."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(" ", "").replace(",", "."))
        except ValueError:
            return None
    return None


def text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def moment(value: Any) -> datetime | None:
    """Время по ISO 8601. Документация БКС шлёт `…Z`, `fromisoformat` ждёт смещение."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def aware_moment(value: object) -> datetime:
    """Время по ISO 8601 → момент **с поясом**. Наивное время — отказ.

    Строгий брат `moment`: тот возвращает `None` на всём, чего не понял,
    и годится там, где отсутствие значения — законный ответ. Здесь оно
    незаконно: время свечи — это её ключ.

    Брокер шлёт `2024-11-10T10:30:00.000Z`, и замена `Z` даёт момент с поясом.
    Но если пояса когда-нибудь не окажется, `astimezone` ниже по течению
    истолковал бы момент как **местный**: на московской машине это сдвиг
    на три часа, молча, — ровно та беда, от которой торговое окно защищается
    строгими границами. Поэтому наивное время не приводится, а отвергается.

    Отказ — `ValueError`: вызывающий переводит его в свой отказ слоя
    (`StreamRejected` в потоке, `UnexpectedAnswer` в исторических свечах),
    потому что человеческий текст у них разный.
    """
    raw = str(value).strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"время {value!r} без часового пояса")
    return parsed


def first_mapping(container: Any) -> Mapping[str, Any] | None:
    """Контейнер документации бывает объектом, бывает массивом объектов."""
    if isinstance(container, Mapping):
        return container
    if isinstance(container, list):
        for item in container:
            if isinstance(item, Mapping):
                return item
    return None
