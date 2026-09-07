"""Остановка робота в базе: `D-043` со стороны хранилища.

Отдельный файл, а не раздел в `tests/test_market_storage.py`: там проверяются
свечи и их миграции, здесь — **предохранитель**, который обязан пережить
закрытие программы. Смешивать их значило бы прятать проверку про деньги
среди проверок про минутки.

Что проверяется: причины ложатся строками, а не склеенной строкой; порядок
появления сохраняется и он же порядок снятия; одна и та же причина второй
строкой не пишется; снимается **названная одна**; текст проходит ту же чистку
от секретов, что и журнал; таблица заводится на базе прежней схемы.

⚠️ В файле лежат строки формы токена — иначе чистку секретов проверять нечем.
Все они **подделка-для-теста**: значения нарочно написаны так, что принять их
за настоящий токен нельзя, и объявление стоит здесь ради детектора секретов
из `tests/test_layers.py`. Настоящее значение в этот файл попасть не должно
никогда — детектор его больше не увидит.

⚠️ Изоляция: своя база во временном каталоге на каждую проверку, сети нет,
Qt не нужен. Проверять поодиночке:
`pytest tests/test_market_halt_store.py::имя`.
"""

from __future__ import annotations

import pathlib
import sqlite3
from datetime import datetime, timedelta

import pytest

from market import MSK, CandleStore, HaltKind, HaltRecord
from market.storage import SCHEMA_VERSION

#: Причина «из движка»: дневной лимит убытка. Текст изображает то, что
#: движок склеивает из события и причины, — заголовка отдельно у неё нет.
LIMIT = "Дневной лимит убытка. Достигнут предел −5 000 ₽ за день."

#: Причина «из слоя приложения»: программа не знает, что на счёте.
UNKNOWN = (
    "Заявка отправлена, исход неизвестен. Проверьте заявки и позицию "
    "у брокера. Робот остановлен и новых решений не принимает."
)

MORNING = datetime(2026, 9, 6, 11, 20, tzinfo=MSK)


@pytest.fixture
def base(tmp_path: pathlib.Path) -> pathlib.Path:
    """Пустая база во временном каталоге. Своя у каждой проверки."""
    return tmp_path / "halt.sqlite3"


def test_an_empty_base_says_the_robot_is_not_halted(base: pathlib.Path) -> None:
    """Пусто — не остановлен. Другого способа сказать это здесь нет."""
    with CandleStore(base) as store:
        assert store.standing_halt() == (), (
            "чистая база отдала остановку, которой в ней не заводили"
        )


def test_every_reason_gets_its_own_row_in_the_order_it_arrived(
    base: pathlib.Path,
) -> None:
    """Причин несколько — строк несколько, и порядок появления сохранён.

    ⚠️ Порядок здесь денежный, а не оформительский: снимается **самая
    ранняя** причина (`HistoryPort.resume`, `D-086`). Перевёрнутый порядок
    снял бы не ту, и заметить это можно было бы только по надписи в окне.
    """
    with CandleStore(base) as store:
        store.raise_halt(HaltRecord(HaltKind.ENGINE, LIMIT), now=MORNING)
        store.raise_halt(
            HaltRecord(HaltKind.ACCOUNT, UNKNOWN, event="Исход неизвестен"),
            now=MORNING + timedelta(minutes=3),
        )

    with CandleStore(base) as store:
        standing = store.standing_halt()

    assert [cause.reason for cause in standing] == [LIMIT, UNKNOWN], (
        "причины вернулись не в том порядке, в котором вставали: "
        f"{[cause.reason[:30] for cause in standing]}"
    )
    assert [cause.kind for cause in standing] == [HaltKind.ENGINE, HaltKind.ACCOUNT], (
        f"вид остановки потерян: {[cause.kind for cause in standing]}"
    )
    assert standing[0].raised_at == MORNING, (
        f"время появления причины не то, что записали: {standing[0].raised_at}"
    )


def test_the_same_reason_is_not_stored_twice(base: pathlib.Path) -> None:
    """Повтор той же причины строкой не ложится и отвечает `None`.

    Опрос счёта повторяет заход каждые полминуты и после остановки его
    не прекращает: без этого правила база копила бы по сто двадцать
    одинаковых причин в час.
    """
    with CandleStore(base) as store:
        first = store.raise_halt(HaltRecord(HaltKind.ACCOUNT, UNKNOWN))
        again = store.raise_halt(HaltRecord(HaltKind.ACCOUNT, UNKNOWN))

        assert first is not None, "первая причина не записана вовсе"
        assert again is None, (
            f"повтор той же причины записан второй строкой: {again}"
        )
        assert len(store.standing_halt()) == 1, (
            f"в базе больше одной строки на одну причину: {store.standing_halt()}"
        )


def test_lifting_takes_the_named_cause_and_leaves_the_rest(
    base: pathlib.Path,
) -> None:
    """Снимается названная одна. Остальные остаются стоять.

    ⚠️ Мутация: `DELETE FROM robot_halt` без `WHERE`. Владелец счёта
    согласился с убытком — и заодно, не читая, снял требование сходить
    к брокеру и посмотреть позицию, о которой программа не знает.
    """
    with CandleStore(base) as store:
        store.raise_halt(HaltRecord(HaltKind.ENGINE, LIMIT))
        store.raise_halt(HaltRecord(HaltKind.ACCOUNT, UNKNOWN))

        assert store.lift_halt(LIMIT) is True, "снятие названной причины не сработало"
        left = store.standing_halt()

    assert [cause.reason for cause in left] == [UNKNOWN], (
        "снятие одной причины задело остальные: "
        f"{[cause.reason[:30] for cause in left]}"
    )


def test_lifting_a_cause_that_does_not_stand_says_so(base: pathlib.Path) -> None:
    """Причины не стояло — ответ `False`, а не тихий успех."""
    with CandleStore(base) as store:
        store.raise_halt(HaltRecord(HaltKind.ENGINE, LIMIT))
        assert store.lift_halt(UNKNOWN) is False, (
            "снятие несуществующей причины отчиталось об успехе"
        )
        assert len(store.standing_halt()) == 1, "снялась не та причина"


def test_the_reason_goes_through_the_same_cleaning_as_the_journal(
    base: pathlib.Path,
) -> None:
    """Текст причины чистится от секретов на записи, как и журнал.

    Причина остановки приходит из отказа брокера и уезжает на диск,
    в резервную копию и в файл, который владелец счёта пришлёт в переписку.
    """
    dirty = (
        "Заявка отправлена, исход неизвестен. Ответ сервера: "
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln"
    )
    with CandleStore(base) as store:
        store.raise_halt(HaltRecord(HaltKind.ACCOUNT, dirty))
        (stored,) = store.standing_halt()

    assert "eyJhbGciOiJIUzI1NiJ9" not in stored.reason, (
        f"токен уехал в базу вместе с причиной остановки: {stored.reason!r}"
    )
    assert "исход неизвестен" in stored.reason, (
        f"чистка съела саму причину: {stored.reason!r}"
    )


def test_a_cleaned_reason_is_lifted_by_the_text_that_came_back(
    base: pathlib.Path,
) -> None:
    """Причина, прочитанная из базы, узнаётся как та же самая при снятии.

    Без этого владелец счёта нажал бы «Возобновить», получил бы снятие
    в памяти — и ту же самую остановку обратно при следующем запуске.
    Держится на идемпотентности чистки (`market.journal.redact`).
    """
    dirty = "Исход неизвестен, ответ: Bearer eyJhbGciOiJIUzI1NiJ9.eyJhIjoxfQ.sig"
    with CandleStore(base) as store:
        store.raise_halt(HaltRecord(HaltKind.ACCOUNT, dirty))
        (stored,) = store.standing_halt()
        assert store.lift_halt(stored.reason) is True, (
            "причина, прочитанная из базы, при снятии не узналась"
        )
        assert store.standing_halt() == (), "причина осталась стоять после снятия"


def test_a_base_of_the_previous_schema_gets_the_halt_table(
    base: pathlib.Path,
) -> None:
    """База схемы 5 открывается, получает таблицу остановки и новую версию.

    Переносить нечего — таблицы до этой версии не существовало вовсе,
    — но открыться такая база обязана: у владельца счёта в ней 82 тысячи
    минуток и весь журнал.
    """
    with CandleStore(base) as ready:
        ready._db.execute("DROP TABLE robot_halt")  # noqa: SLF001 — схема прежней сборки
        ready._db.execute("PRAGMA user_version = 5")  # noqa: SLF001 — та же причина

    with CandleStore(base) as store:
        store.raise_halt(HaltRecord(HaltKind.ENGINE, LIMIT))
        assert [cause.reason for cause in store.standing_halt()] == [LIMIT], (
            "на базе прежней схемы остановка не заводится"
        )
        (version,) = store._db.execute("PRAGMA user_version").fetchone()  # noqa: SLF001

    assert version == SCHEMA_VERSION, (
        f"версия схемы после открытия старой базы осталась {version}"
    )


def test_a_base_with_a_standing_halt_is_refused_by_an_older_build(
    base: pathlib.Path,
) -> None:
    """Сборка со схемой старше отказывается открывать такую базу.

    Проверка **смысла версии**, а не числа. Сборка, не знающая таблицы
    остановки, открыла бы базу со стоящим запретом и не увидела его: робот
    пошёл бы торговать. Отказ при открытии честнее тихого обхода.
    """
    with CandleStore(base) as store:
        store.raise_halt(HaltRecord(HaltKind.ACCOUNT, UNKNOWN))

    # Так выглядит база глазами сборки, у которой `SCHEMA_VERSION` меньше:
    # проверку версии делает та же строка кода, что и в бою.
    connection = sqlite3.connect(str(base))
    try:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="более новой версией"):
        CandleStore(base).close()
