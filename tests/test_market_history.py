"""Загрузка истории по требованию человека: `market/history.py`.

Что здесь стережётся, по одной строке на тест
---------------------------------------------
* повторная загрузка не плодит дублей — ни строк, ни минуток;
* пустой ответ биржи не выдаётся за успех;
* несуществующий код объясняется человеком, и качать при этом не идут вовсе;
* прогрев не короче минимума, и минимум выведен из периода средней, а не назван;
* число `WARMUP_PERIOD` не разошлось с периодом модуля стратегии;
* порядок разбора «что не так» — правило, а не оформление.

⚠️ **Сеть в этом файле обезврежена целиком**: транспорт биржи снимает
`the_real_transport_is_disarmed`, сокет наружу закрыт общим сторожем
`tests/conftest.py::no_internet`. Оба не декоративные — 05.09.2026 тест
другого файла молча скачал 55 настоящих минуток с `iss.moex.com`,
и прогон остался зелёным. Тест, ходящий в интернет,
падает от чужого сбоя и зеленеет от чужой удачи — доказывать им нечего.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta

import pytest
from market_helpers import FakeTransport, iss_body, minute, no_sleep

from market import (
    DEFAULT_WARMUP_BARS,
    FUTURES,
    M5,
    MSK,
    SHARES,
    WARMUP_PERIOD,
    WARMUP_RESIDUAL,
    Candle,
    CandleStore,
    HistoryOutcome,
    HistoryRequest,
    HttpxTransport,
    IssClient,
    IssTransportError,
    Source,
    load_history,
    minute_border,
    warmup_bars,
)

DAY = date(2026, 6, 17)
#: Момент «сейчас» для всех прогонов: заведомо позже загружаемого отрезка,
#: иначе дни не отметятся закрытыми и бары останутся незакрытыми.
NOW = datetime(2026, 6, 25, 12, 0, tzinfo=MSK)


@pytest.fixture(autouse=True)
def the_real_transport_is_disarmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Настоящий транспорт биржи обезврежен на весь файл.

    ⚠️ Имя — не `no_internet`, и это `B-034`. Так фикстура и называлась,
    ровно как автоматический сторож `tests/conftest.py::no_internet`,
    и потому **отменяла его на весь файл**: сокет наружу переставал быть
    закрыт, а `conftest` при этом объявлял обход невозможным. Здесь
    обезврежен транспорт биржи — один слой, а не сеть целиком, — и имя
    говорит именно это. Сокетный сторож работает вторым рубежом.
    """

    def refuse(self: object, url: str, *, timeout: float) -> bytes:
        raise AssertionError(f"тест полез в настоящий интернет: {url}")

    monkeypatch.setattr(HttpxTransport, "get", refuse)


def borders_body(*rows: tuple[str, str, int]) -> bytes:
    """Ответ `candleborders`: границы истории по интервалам.

    Форма взята с живого ответа ISS 05.09.2026: блок `borders` плюс блок
    `durations`, который разбору не нужен и здесь присутствует ровно затем,
    чтобы подставной ответ не был проще настоящего.
    """
    return json.dumps(
        {
            "borders": {
                "columns": ["begin", "end", "interval"],
                "data": [list(row) for row in rows],
            },
            "durations": {"columns": ["interval", "duration"], "data": [[1, 60]]},
        }
    ).encode("utf-8")


#: Живой инструмент: минутки с 01.06.2026 по 30.06.2026.
ALIVE = borders_body(
    ("2026-06-01 10:00:00", "2026-06-30 23:49:59", 1),
    ("2026-06-01 00:00:00", "2026-06-30 00:00:00", 24),
)
#: Ответ на код, которого на этом рынке нет: блок есть, строк нет.
#: Проверено живым запросом 05.09.2026 — ISS отвечает `200`, а не `404`.
UNKNOWN = borders_body()


def session(day: date, count: int) -> list[Candle]:
    """Подряд идущие минутки одного дня, с 10:00."""
    start = datetime.combine(day, datetime.min.time(), MSK) + timedelta(hours=10)
    return [minute(start + timedelta(minutes=i), open=100.0 + i) for i in range(count)]


def exchange(
    borders: bytes, pages: Sequence[bytes] = ()
) -> Callable[[str], bytes]:
    """Подставная биржа: справка о глубине и страницы свечей по порядку."""
    queue = list(pages)
    empty = iss_body([])

    def handler(url: str) -> bytes:
        if "candleborders" in url:
            return borders
        return queue.pop(0) if queue else empty

    return handler


def loaded(
    path: pathlib.Path,
    handler: Callable[[str], bytes],
    *,
    since: date | None = DAY,
    until: date = date(2026, 6, 19),
    symbol: str = "MXU6",
    market: object = FUTURES,
    warmup: int = DEFAULT_WARMUP_BARS,
) -> HistoryOutcome:
    """Одна загрузка в эту базу подставной биржей. Возвращает исход."""
    client = IssClient(FakeTransport(handler), sleep=no_sleep, pause=0)
    request = HistoryRequest(
        symbol=symbol, market=market, until=until, since=since,  # type: ignore[arg-type]
        warmup=warmup, timeframe=M5, now=NOW,
    )
    with CandleStore(path) as store:
        return load_history(store, client, request)


def rows(path: pathlib.Path, symbol: str = "MXU6") -> int:
    with CandleStore(path) as store:
        return store.coverage(symbol).count


# -- прогрев: число выведено, а не названо ---------------------------------


def test_warmup_is_derived_from_the_average_period_not_hardcoded() -> None:
    """Прогрев считается по формуле затухания EMA, а не взят из головы."""
    assert warmup_bars(15) == 52, "период 15: 0.875**52 < 0.001, а 0.875**51 — нет"
    assert 0.875 ** warmup_bars(15) < WARMUP_RESIDUAL
    assert 0.875 ** (warmup_bars(15) - 1) > WARMUP_RESIDUAL
    # Другой период — другое число: формула, а не подогнанная константа.
    assert warmup_bars(30) > warmup_bars(15) > warmup_bars(5)


def test_warmup_is_never_shorter_than_the_strategy_needs_for_a_signal() -> None:
    """Ниже `период + 1` бара сигнала нет вовсе — прогрев не вправе быть меньше."""
    for period in range(1, 60):
        assert warmup_bars(period) >= period + 1


def test_the_warmup_period_matches_the_strategy_module() -> None:
    """Слой данных повторяет период средней числом — разойтись им нельзя.

    Импортировать `strategies` из `market/` нельзя (ARCHITECTURE.md §2),
    и в `app/` тоже (`test_app_boundaries.py`). Связь двух чисел держит
    ровно этот тест: поменяли период в стратегии — прогон покраснеет здесь.
    """
    from strategies import DEFAULT_PERIOD

    assert WARMUP_PERIOD == DEFAULT_PERIOD
    assert DEFAULT_WARMUP_BARS == warmup_bars(DEFAULT_PERIOD)


@pytest.mark.parametrize("bad", [0, -1])
def test_a_nonsense_period_is_refused(bad: int) -> None:
    with pytest.raises(ValueError, match="период"):
        warmup_bars(bad)


@pytest.mark.parametrize("bad", [0.0, 1.0, -0.5, 2.0])
def test_a_nonsense_residual_is_refused(bad: float) -> None:
    with pytest.raises(ValueError, match="остатка"):
        warmup_bars(15, residual=bad)


# -- существование инструмента ---------------------------------------------


def test_a_missing_instrument_is_told_apart_from_an_empty_period() -> None:
    """Пустой `borders` — это «кода нет», и качать после него не идут вовсе."""
    transport = FakeTransport(exchange(UNKNOWN, [iss_body(session(DAY, 400))]))
    client = IssClient(transport, sleep=no_sleep, pause=0)
    border, why = minute_border(client, "MXЫ6", market=FUTURES)
    assert border is None
    assert why == "", "ответ получен — причины отказа быть не должно"
    assert len(transport.urls) == 1, "за свечами ходили, хотя кода на бирже нет"


def test_an_unknown_ticker_is_explained_and_nothing_is_downloaded(
    tmp_path: pathlib.Path,
) -> None:
    """Опечатка в коде: человеку сказано что проверить, база не тронута."""
    path = tmp_path / "candles.sqlite3"
    outcome = loaded(path, exchange(UNKNOWN, [iss_body(session(DAY, 400))]))
    assert not outcome.known
    assert outcome.report is None, "загрузка пошла, хотя инструмента нет"
    trouble = outcome.problem() or ""
    assert "не знает инструмента" in trouble
    assert "опечатка" in trouble
    assert "--fetch-market shares" in trouble, "про чужой рынок не сказано"
    assert rows(path) == 0


def test_the_wrong_market_gets_the_same_answer_naming_the_market(
    tmp_path: pathlib.Path,
) -> None:
    """Акция, запрошенная на срочном рынке, — тот же пустой `borders`."""
    outcome = loaded(
        tmp_path / "c.sqlite3", exchange(UNKNOWN), symbol="SBER", market=FUTURES
    )
    assert "срочный рынок" in (outcome.problem() or "")


def test_an_unreachable_depth_service_does_not_stop_the_download(
    tmp_path: pathlib.Path,
) -> None:
    """Справка о глубине — удобство. Не ответила — грузим, а не отказываем.

    Иначе вся догрузка истории попадает в зависимость от доступности ещё
    одной ручки ISS, которая к самим свечам отношения не имеет.
    """
    def handler(url: str) -> bytes:
        if "candleborders" in url:
            raise IssTransportError("справочник недоступен")
        body: bytes = iss_body(session(DAY, 400)) if "start=0" in url else iss_body([])
        return body

    path = tmp_path / "c.sqlite3"
    outcome = loaded(path, handler)
    assert outcome.known, "невозможность спросить приняли за отсутствие инструмента"
    assert outcome.border_error, "причина отказа справки не записана"
    assert outcome.report is not None and outcome.report.fetched == 400
    assert outcome.problem() is None


def test_all_history_without_a_known_start_is_refused_not_guessed(
    tmp_path: pathlib.Path,
) -> None:
    """«Вся история» и неизвестная дата начала — отказ, а не догадка про год."""
    def handler(url: str) -> bytes:
        if "candleborders" in url:
            raise IssTransportError("справочник недоступен")
        raise AssertionError("за свечами пошли без известной даты начала")

    outcome = loaded(tmp_path / "c.sqlite3", handler, since=None)
    trouble = outcome.problem() or ""
    assert "--fetch-days" in trouble and "--fetch-since" in trouble


def test_a_period_outside_the_instrument_history_is_not_downloaded(
    tmp_path: pathlib.Path,
) -> None:
    """Период целиком раньше листинга: сказано, с какой даты минутки есть."""
    def handler(url: str) -> bytes:
        if "candleborders" in url:
            return ALIVE
        raise AssertionError("пошли за свечами вне истории инструмента")

    outcome = loaded(
        tmp_path / "c.sqlite3", handler,
        since=date(2020, 1, 1), until=date(2020, 1, 31),
    )
    trouble = outcome.problem() or ""
    assert "целиком вне минутной истории" in trouble
    assert "01.06.2026" in trouble, "не названа дата, с которой данные есть"


def test_the_request_is_trimmed_to_the_declared_depth(tmp_path: pathlib.Path) -> None:
    """Запрос за край истории подрезается, и подрезка видна в исходе."""
    outcome = loaded(
        tmp_path / "c.sqlite3",
        exchange(ALIVE, [iss_body(session(DAY, 400))]),
        since=date(2025, 1, 1),
    )
    assert outcome.since == date(2026, 6, 1), "не подрезали к началу истории"
    assert outcome.clamped


# -- пустой ответ не выдаётся за успех -------------------------------------


def test_an_empty_answer_is_not_passed_off_as_success(tmp_path: pathlib.Path) -> None:
    """Инструмент есть, период в пределах истории, свечей ноль — это отказ."""
    path = tmp_path / "c.sqlite3"
    outcome = loaded(path, exchange(ALIVE))
    assert outcome.report is not None and outcome.report.fetched == 0
    trouble = outcome.problem() or ""
    assert "ни одной свечи" in trouble
    assert "не в опечатке" in trouble, "человека отправили искать не ту беду"
    assert rows(path) == 0


def test_a_short_series_is_called_out_as_too_cold_for_the_average(
    tmp_path: pathlib.Path,
) -> None:
    """Данные загрузились, но баров меньше прогрева — это не «готово»."""
    path = tmp_path / "c.sqlite3"
    outcome = loaded(path, exchange(ALIVE, [iss_body(session(DAY, 30))]))
    assert outcome.report is not None and outcome.report.fetched == 30
    assert outcome.bars == 6, "тридцать минуток дают шесть пятиминуток"
    assert not outcome.warm
    trouble = outcome.problem() or ""
    assert "для прогрева средней этого мало" in trouble
    assert str(DEFAULT_WARMUP_BARS) in trouble, "не названо, сколько баров нужно"
    assert rows(path) == 30, "свечи всё равно записаны — данные не выбрасываются"


def test_a_long_enough_series_has_no_complaints(tmp_path: pathlib.Path) -> None:
    """Достаточный ряд: жалоб нет, бары посчитаны по базе."""
    outcome = loaded(tmp_path / "c.sqlite3", exchange(ALIVE, [iss_body(session(DAY, 400))]))
    assert outcome.problem() is None
    assert outcome.warm and outcome.bars == 80


# -- повтор ----------------------------------------------------------------


def test_running_it_twice_does_not_double_the_data(tmp_path: pathlib.Path) -> None:
    """Запустил дважды — минуток столько же, а не вдвое.

    Проверяются обе защиты сразу: учёт дней не даёт перезапросить закрытый
    день, а запись той же минутки тем же источником ложится поверх себя же,
    а не второй строкой.
    """
    path = tmp_path / "c.sqlite3"
    first = loaded(path, exchange(ALIVE, [iss_body(session(DAY, 400))]))
    after_first = rows(path)
    second = loaded(path, exchange(ALIVE, [iss_body(session(DAY, 400))]))
    assert after_first == 400
    assert rows(path) == after_first, "повтор удвоил данные"
    assert first.bars == second.bars
    assert second.report is not None and second.report.inserted == 0


def test_the_same_candles_written_twice_land_in_the_same_rows(
    tmp_path: pathlib.Path,
) -> None:
    """Даже когда день перезапрашивается, минутка не становится второй строкой.

    Отдельно от теста выше намеренно: там дубль отсекает учёт дней, здесь
    учёт обойдён (день ещё не закончился), и держит только правило записи.
    """
    path = tmp_path / "c.sqlite3"
    candles = session(DAY, 400)
    with CandleStore(path) as store:
        store.put_minutes("MXU6", candles, Source.ISS)
        once = store.coverage("MXU6").count
        store.put_minutes("MXU6", candles, Source.ISS)
        assert store.coverage("MXU6").count == once == 400


# -- порядок разбора -------------------------------------------------------


def test_the_unknown_ticker_is_explained_before_the_empty_period(
    tmp_path: pathlib.Path,
) -> None:
    """Порядок звеньев — правило: с опечаткой нельзя советовать взять период шире.

    Оба условия выполнены одновременно: кода на бирже нет **и** свечей ноль.
    Ответ обязан быть про опечатку, а не про выходные.
    """
    outcome = loaded(tmp_path / "c.sqlite3", exchange(UNKNOWN))
    trouble = outcome.problem() or ""
    assert "не знает инструмента" in trouble
    assert "выходные" not in trouble


def test_shares_and_futures_go_to_different_addresses(tmp_path: pathlib.Path) -> None:
    """Рынок доезжает до адреса: акции и срочный — разные ветки ISS."""
    seen: list[str] = []

    def handler(url: str) -> bytes:
        seen.append(url)
        return ALIVE if "candleborders" in url else iss_body([])

    loaded(tmp_path / "c.sqlite3", handler, symbol="SBER", market=SHARES)
    assert all("/stock/markets/shares/" in url for url in seen), seen
