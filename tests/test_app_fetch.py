"""Ключи `--fetch` и `--inspect`: то, что владелец счёта набирает руками.

Что здесь стережётся, по одной строке на тест
---------------------------------------------
* `--fetch CODE` без остальных ключей работает и грузит тридцать дней;
* правый край берётся по МСК, а не по часам машины;
* повторный запуск не удваивает данные и не врёт про это в итоге;
* опечатка в коде объясняется словами, и код возврата не ноль;
* итог загрузки называет все числа отчёта, а не «готово»;
* обрыв связи — фраза и продолжение с места обрыва, а не трассировка;
* `--inspect` печатает опись и ничего не пишет в базу;
* ключи разбираются `app/main.py`, а не переписаны здесь заново.

⚠️ **Сеть обезврежена на весь файл.** Транспорт биржи подставной везде;
любая попытка выйти наружу падает громко. Токен не участвует никак —
загрузка истории идёт по открытым данным, без него.
"""

from __future__ import annotations

import argparse
import io
import json
import pathlib
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta

import pytest
from market_helpers import FakeTransport, iss_body, minute, no_sleep

from app.fetch import fetch_history, run_from_arguments, show_inventory
from app.main import main as program_main
from market import (
    DEFAULT_DEPTH_DAYS,
    FUTURES,
    MARKETS,
    MSK,
    Candle,
    CandleStore,
    FetchResult,
    HistoryRequest,
    HttpxTransport,
    IssClient,
    IssTransportError,
    Source,
)

DAY = date(2026, 6, 17)


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
    return json.dumps(
        {"borders": {"columns": ["begin", "end", "interval"],
                     "data": [list(row) for row in rows]}}
    ).encode("utf-8")


ALIVE = borders_body(("2026-06-01 10:00:00", "2026-06-30 23:49:59", 1))
UNKNOWN = borders_body()


def session(day: date, count: int) -> list[Candle]:
    start = datetime.combine(day, datetime.min.time(), MSK) + timedelta(hours=10)
    return [minute(start + timedelta(minutes=i), open=100.0 + i) for i in range(count)]


#: Коды, которые подставная биржа знает, — в её собственном регистре.
KNOWN = ("MXU6", "MXZ6", "MXM6", "SBER", "SiZ6")


def description_body(secid: str | None) -> bytes:
    """Описание инструмента, как у ISS; `None` — биржа кода не знает."""
    data = [] if secid is None else [["SECID", "Краткий код", secid, "string"]]
    return json.dumps(
        {"description": {"columns": ["name", "title", "value", "type"], "data": data}}
    ).encode("utf-8")


def described(url: str) -> bytes:
    """Ответ на описание: ISS регистр не различает и называет свой `SECID`.

    Живой запрос 29.09.2026: `siz6`, `SIZ6`, `SiZ6` — одно описание, `SiZ6`.
    """
    typed = url.split("/securities/", 1)[1].split(".json", 1)[0]
    return description_body({code.lower(): code for code in KNOWN}.get(typed.lower()))


def exchange(borders: bytes, pages: Sequence[bytes] = ()) -> Callable[[str], bytes]:
    queue = list(pages)
    empty = iss_body([])

    def handler(url: str) -> bytes:
        # ⚠️ До очереди: иначе описание съело бы первую страницу свечей.
        if "iss.only=description" in url:
            return described(url)
        if "candleborders" in url:
            return borders
        return queue.pop(0) if queue else empty

    return handler


def fake_client(handler: Callable[[str], bytes]) -> IssClient:
    return IssClient(FakeTransport(handler), sleep=no_sleep, pause=0)


def arguments(**overrides: object) -> argparse.Namespace:
    """Разбор настоящими ключами программы, а не подделка `Namespace`.

    Тест, собирающий `Namespace` руками, проверяет сам себя: он не заметит
    ни переименованного ключа, ни пропавшего умолчания.
    """
    from app.main import _arguments

    argv = ["--fetch", str(overrides.pop("fetch", "MXU6"))]
    for name, value in overrides.items():
        argv += [f"--{name.replace('_', '-')}", str(value)]
    return _arguments(argv)


def rows(path: pathlib.Path, symbol: str = "MXU6") -> int:
    with CandleStore(path) as store:
        return store.coverage(symbol).count


# -- ключи -----------------------------------------------------------------


def test_a_bare_fetch_key_is_enough_to_load(tmp_path: pathlib.Path) -> None:
    """`--fetch MXU6` без единого уточнения грузит и записывает.

    Владелец счёта не программист: остальные три ключа обязаны иметь
    умолчания, а не требоваться.
    """
    args = arguments()
    assert args.fetch_days == DEFAULT_DEPTH_DAYS
    assert args.fetch_market == "futures"
    assert args.fetch_since is None


def test_the_right_edge_is_moscow_time_not_the_machine_clock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Правый край и глубина считаются по МСК: машина может стоять не в Москве.

    Ловится подменой самого запроса: наивное «сегодня по часам машины»
    на машине в UTC+7 даёт другую дату, и отрезок уезжает на сутки.
    """
    seen: list[HistoryRequest] = []

    def spy(database, request, *, out, client=None):
        seen.append(request)
        return 0

    monkeypatch.setattr("app.fetch.fetch_history", spy)
    run_from_arguments(arguments(), tmp_path / "c.sqlite3", out=io.StringIO())
    today = datetime.now(MSK).date()
    assert seen[0].until == today
    assert seen[0].since == today - timedelta(days=DEFAULT_DEPTH_DAYS - 1)


def test_zero_days_means_all_the_exchange_has(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """`--fetch-days 0` — «вся глубина», то же значение, что у `--days 0`."""
    seen: list[HistoryRequest] = []

    def spy(database, request, *, out, client=None):
        seen.append(request)
        return 0

    monkeypatch.setattr("app.fetch.fetch_history", spy)
    run_from_arguments(arguments(fetch_days=0), tmp_path / "c.sqlite3", out=io.StringIO())
    assert seen[0].since is None, "ноль дней обязан означать «с начала истории»"


def test_an_explicit_date_beats_the_depth_in_days(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """`--fetch-since` перебивает `--fetch-days`: дату называют, чтобы её взяли."""
    seen: list[HistoryRequest] = []

    def spy(database, request, *, out, client=None):
        seen.append(request)
        return 0

    monkeypatch.setattr("app.fetch.fetch_history", spy)
    run_from_arguments(
        arguments(fetch_days=3, fetch_since="01.08.2026"),
        tmp_path / "c.sqlite3", out=io.StringIO(),
    )
    assert seen[0].since == date(2026, 8, 1)


def test_the_market_key_picks_the_market(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    seen: list[HistoryRequest] = []

    def spy(database, request, *, out, client=None):
        seen.append(request)
        return 0

    monkeypatch.setattr("app.fetch.fetch_history", spy)
    run_from_arguments(
        arguments(fetch="SBER", fetch_market="shares"),
        tmp_path / "c.sqlite3", out=io.StringIO(),
    )
    assert seen[0].market.market == "shares"


def test_the_fetch_key_keeps_the_case_of_the_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """`--fetch SiZ6` грузит `SiZ6`, а не `SIZ6` (`D-128`).

    ISS регистр не различает и сама отвечает `SiZ6` (живой запрос
    29.09.2026), а база различает: `SIZ6` лёг бы отдельным инструментом.
    """
    seen: list[HistoryRequest] = []

    def spy(database, request, *, out, client=None):
        seen.append(request)
        return 0

    monkeypatch.setattr("app.fetch.fetch_history", spy)
    run_from_arguments(arguments(fetch="SiZ6"), tmp_path / "c.sqlite3", out=io.StringIO())
    assert seen[0].symbol == "SiZ6"


def test_inspect_keeps_the_case_of_the_code(tmp_path: pathlib.Path) -> None:
    """`--inspect siz6` находит минуты `SiZ6`: код биржи, а не набранный (`D-128`)."""
    path = tmp_path / "c.sqlite3"
    with CandleStore(path) as store:
        store.put_minutes("SiZ6", session(DAY, 400), Source.ISS)
    out = io.StringIO()
    code = show_inventory(path, "siz6", out=out, client=fake_client(exchange(ALIVE)))
    assert code == 0, out.getvalue()
    assert "SiZ6 — что лежит в базе" in out.getvalue()


def test_an_unknown_market_is_refused_by_the_key_itself() -> None:
    """Опечатка в рынке — отказ разбора ключей, а не запрос в никуда."""
    with pytest.raises(SystemExit):
        arguments(fetch_market="фьючерсы")


# -- загрузка --------------------------------------------------------------


def test_the_summary_names_every_number_not_just_done(tmp_path: pathlib.Path) -> None:
    """Итог перечисляет числа отчёта: без них недокачанное неотличимо от полного."""
    out = io.StringIO()
    path = tmp_path / "c.sqlite3"
    code = fetch_history(
        path,
        HistoryRequest(symbol="MXU6", market=FUTURES, until=date(2026, 6, 19),
                       since=DAY, now=datetime(2026, 6, 25, tzinfo=MSK)),
        out=out,
        client=fake_client(exchange(ALIVE, [iss_body(session(DAY, 400))])),
    )
    text = out.getvalue()
    assert code == 0
    for word in ("инструмент", "глубина биржи", "запрошено", "пришло с биржи",
                 "записано", "минут без свечи", "баров Min5", "заняло"):
        assert word in text, f"в итоге нет строки «{word}»:\n{text}"
    assert "400" in text
    assert rows(path) == 400


def test_running_it_twice_says_zero_new_and_keeps_the_count(
    tmp_path: pathlib.Path,
) -> None:
    """Повтор: данных столько же, и в итоге честно сказано «0 новых»."""
    path = tmp_path / "c.sqlite3"
    request = HistoryRequest(
        symbol="MXU6", market=FUTURES, until=date(2026, 6, 19), since=DAY,
        now=datetime(2026, 6, 25, tzinfo=MSK),
    )
    first = io.StringIO()
    fetch_history(path, request, out=first,
                  client=fake_client(exchange(ALIVE, [iss_body(session(DAY, 400))])))
    after_first = rows(path)
    second = io.StringIO()
    fetch_history(path, request, out=second,
                  client=fake_client(exchange(ALIVE, [iss_body(session(DAY, 400))])))
    assert after_first == 400
    assert rows(path) == after_first, "повтор удвоил данные"
    assert "0 новых" in second.getvalue(), second.getvalue()


def test_a_typo_in_the_ticker_is_explained_and_the_code_is_not_zero(
    tmp_path: pathlib.Path,
) -> None:
    """Несуществующий код: слова про опечатку и чужой рынок, возврат не ноль."""
    out = io.StringIO()
    path = tmp_path / "c.sqlite3"
    code = fetch_history(
        path,
        HistoryRequest(symbol="MXЫ6", market=FUTURES, until=date(2026, 6, 19),
                       since=DAY),
        out=out, client=fake_client(exchange(UNKNOWN)),
    )
    text = out.getvalue()
    assert code == 1, "негодный код принят молча"
    assert "не знает инструмента 'MXЫ6'" in text
    assert "Traceback" not in text
    assert rows(path, "MXЫ6") == 0


def test_a_code_from_another_market_points_at_the_market_key(
    tmp_path: pathlib.Path,
) -> None:
    """Код биржа знает, а на срочном рынке его нет: подсказан ключ рынка."""
    out = io.StringIO()
    code = fetch_history(
        tmp_path / "c.sqlite3",
        HistoryRequest(symbol="sber", market=FUTURES, until=date(2026, 6, 19),
                       since=DAY),
        out=out, client=fake_client(exchange(UNKNOWN)),
    )
    assert code == 1
    assert "--fetch-market shares" in out.getvalue(), out.getvalue()


@pytest.mark.parametrize(
    ("typed", "real", "market"),
    [("sber", "SBER", "shares"), ("siz6", "SiZ6", "futures"), ("mxu6", "MXU6", "futures")],
)
def test_the_typed_code_becomes_the_exchange_code(
    tmp_path: pathlib.Path, typed: str, real: str, market: str
) -> None:
    """Стережёт: `--fetch sber` пишет в базу `SBER`, а не отдельный `sber` (`D-128`).

    Код приводится к `SECID` из ответа биржи, а не поднятием регистра:
    `siz6` обязан стать `SiZ6`, а не `SIZ6`. Мутация, обязанная ронять
    проверку: убрать приведение в `fetch_history`.
    """
    path = tmp_path / "c.sqlite3"
    transport = FakeTransport(exchange(ALIVE, [iss_body(session(DAY, 400))]))
    out = io.StringIO()
    code = fetch_history(
        path,
        HistoryRequest(symbol=typed, market=MARKETS[market], until=date(2026, 6, 19),
                       since=DAY, now=datetime(2026, 6, 25, tzinfo=MSK)),
        out=out, client=IssClient(transport, sleep=no_sleep, pause=0),
    )
    assert code == 0, out.getvalue()
    assert rows(path, real) == 400, out.getvalue()
    assert rows(path, typed) == 0, "свечи легли под набранным кодом"
    candle_urls = [url for url in transport.urls if "/candles.json" in url]
    assert candle_urls and all(f"/{real}/" in url for url in candle_urls), candle_urls
    assert f"Загрузка истории {real} " in out.getvalue()


def test_an_empty_period_is_not_reported_as_done(tmp_path: pathlib.Path) -> None:
    """Ноль свечей — не «готово»: сказано, что пришло пусто и что делать."""
    out = io.StringIO()
    code = fetch_history(
        tmp_path / "c.sqlite3",
        HistoryRequest(symbol="MXU6", market=FUTURES, until=date(2026, 6, 19),
                       since=DAY, now=datetime(2026, 6, 25, tzinfo=MSK)),
        out=out, client=fake_client(exchange(ALIVE)),
    )
    assert code == 1
    assert "ни одной свечи" in out.getvalue()
    assert "Готово" not in out.getvalue()


def test_a_broken_connection_is_a_phrase_not_a_traceback(
    tmp_path: pathlib.Path,
) -> None:
    """Обрыв связи: фраза и обещание продолжить, а не трассировка на экран."""
    def handler(url: str) -> bytes:
        if "iss.only=description" in url:
            return described(url)
        if "candleborders" in url:
            return ALIVE
        raise IssTransportError("соединение разорвано")

    out = io.StringIO()
    code = fetch_history(
        tmp_path / "c.sqlite3",
        HistoryRequest(symbol="MXU6", market=FUTURES, until=date(2026, 6, 19),
                       since=DAY, now=datetime(2026, 6, 25, tzinfo=MSK)),
        out=out, client=fake_client(handler),
    )
    text = out.getvalue()
    assert code == 1
    assert "Загрузка прервана" in text
    assert "продолжит" in text
    assert "Traceback" not in text


def test_the_progress_line_counts_across_chunks_not_from_zero(
    tmp_path: pathlib.Path,
) -> None:
    """Счётчик хода работы складывает куски, а не сбрасывается на каждом.

    `FetchResult` создаётся заново на каждый кусок, и без накопления человек
    на годовой загрузке двенадцать раз увидит счёт с нуля.
    """
    from app.fetch import Ticker

    out = io.StringIO()
    ticker = Ticker(out, "MXU6", live=False)

    first = FetchResult(loaded=500, pages=20)
    second = FetchResult(loaded=300, pages=20)
    ticker(first)
    ticker(second)
    ticker.close()
    assert "800 свечей" in out.getvalue(), out.getvalue()
    assert "страниц 40" in out.getvalue(), out.getvalue()


# -- опись -----------------------------------------------------------------


def test_inspect_prints_the_picture_and_writes_nothing(
    tmp_path: pathlib.Path,
) -> None:
    """`--inspect` только читает: размер файла базы после него тот же."""
    path = tmp_path / "c.sqlite3"
    with CandleStore(path) as store:
        store.put_minutes("MXU6", session(DAY, 400), Source.ISS)
        store.mark_days_requested(
            "MXU6", [DAY], now=datetime(2026, 6, 25, tzinfo=MSK)
        )
    before = path.read_bytes()
    out = io.StringIO()
    code = show_inventory(path, "mxu6", out=out, client=fake_client(exchange(ALIVE)))
    text = out.getvalue()
    assert code == 0
    assert "MXU6 — что лежит в базе" in text
    assert "самый плотный день" in text
    assert "торговать можно" in text
    assert "медиана минут" in text, "помесячной таблицы нет"
    assert path.read_bytes() == before, "опись изменила базу"


def test_inspect_without_a_database_says_so(tmp_path: pathlib.Path) -> None:
    """Файла базы нет — это отдельная беда, не «свечей нет»."""
    out = io.StringIO()
    code = show_inventory(tmp_path / "no-such.sqlite3", "MXU6", out=out)
    assert code == 1
    assert "Базы свечей нет" in out.getvalue()
    assert "--fetch MXU6" in out.getvalue()


def test_inspect_of_an_unloaded_instrument_points_at_the_fetch_key(
    tmp_path: pathlib.Path,
) -> None:
    """База есть, инструмента в ней нет: сказано, чем его загрузить."""
    path = tmp_path / "c.sqlite3"
    with CandleStore(path) as store:
        store.put_minutes("MXU6", session(DAY, 10), Source.ISS)
    out = io.StringIO()
    code = show_inventory(path, "MXZ6", out=out, client=fake_client(exchange(ALIVE)))
    assert code == 1
    assert "нет ни одной свечи" in out.getvalue()
    assert "--fetch MXZ6" in out.getvalue()


def test_the_program_takes_the_inspect_key_without_opening_a_window(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`main()` уходит в опись до первого касания Qt.

    Проверяется тем, что Qt в этой ветке не поднимается вовсе: подмена
    `qasync` на негодный модуль не мешает ключу отработать.
    """
    import sys

    monkeypatch.setitem(sys.modules, "qasync", None)
    monkeypatch.setattr("app.fetch.IssClient", lambda: fake_client(exchange(ALIVE)))
    path = tmp_path / "c.sqlite3"
    with CandleStore(path) as store:
        store.put_minutes("MXU6", session(DAY, 400), Source.ISS)
    assert program_main(["--inspect", "mxu6", "--db", str(path)]) == 0


def test_inspect_of_a_code_the_exchange_does_not_know_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    """Опечатка в `--inspect` — отказ словами, а не «свечей нет»."""
    path = tmp_path / "c.sqlite3"
    with CandleStore(path) as store:
        store.put_minutes("MXU6", session(DAY, 10), Source.ISS)
    out = io.StringIO()
    code = show_inventory(path, "MXЫ6", out=out, client=fake_client(exchange(ALIVE)))
    assert code == 1
    assert "не знает инструмента 'MXЫ6'" in out.getvalue(), out.getvalue()


def test_inspect_of_a_stitched_series_does_not_ask_the_exchange(
    tmp_path: pathlib.Path,
) -> None:
    """`@MX` бирже не известен: опись ищет его как набран, в сеть не ходит."""
    path = tmp_path / "c.sqlite3"
    with CandleStore(path) as store:
        store.put_minutes("@MX", session(DAY, 400), Source.ISS)
    transport = FakeTransport(exchange(ALIVE))
    out = io.StringIO()
    code = show_inventory(
        path, "@MX", out=out, client=IssClient(transport, sleep=no_sleep, pause=0)
    )
    assert code == 0, out.getvalue()
    assert transport.urls == []


def offline(url: str) -> bytes:
    """Биржа недоступна: любой запрос — обрыв связи."""
    raise IssTransportError("соединение разорвано")


@pytest.mark.parametrize("typed", ["MXU6", "mxu6"])
def test_inspect_without_the_exchange_finds_the_code_in_the_base(
    tmp_path: pathlib.Path, typed: str
) -> None:
    """Без связи с биржей верный код находится в базе.

    Опись только читает базу: фраза, что код не сверен, — а не отказ после повторов.
    """
    path = tmp_path / "c.sqlite3"
    with CandleStore(path) as store:
        store.put_minutes("MXU6", session(DAY, 400), Source.ISS)
    out = io.StringIO()
    code = show_inventory(path, typed, out=out, client=fake_client(offline))
    text = out.getvalue()
    assert code == 0, text
    assert "MXU6 — что лежит в базе" in text
    assert "Биржа недоступна" in text
    assert "не сверен" in text


def test_inspect_finds_a_key_stored_in_another_case(tmp_path: pathlib.Path) -> None:
    """`--inspect siz6` находит `SIZ6` и говорит, что регистр в базе другой.

    Si до `D-128` лежит под `SIZ6`, а биржа называет его `SiZ6`.
    """
    path = tmp_path / "c.sqlite3"
    with CandleStore(path) as store:
        store.put_minutes("SIZ6", session(DAY, 400), Source.ISS)
    out = io.StringIO()
    code = show_inventory(path, "siz6", out=out, client=fake_client(exchange(ALIVE)))
    text = out.getvalue()
    assert code == 0, text
    assert "SIZ6 — что лежит в базе" in text
    assert "в другом регистре: SIZ6" in text
    assert "SiZ6" in text, "код биржи не назван"
    with CandleStore(path) as store:
        assert store.symbols() == ["SIZ6"], "база переименована"


def test_inspect_lists_every_key_that_differs_only_in_case(
    tmp_path: pathlib.Path,
) -> None:
    """Два ключа без учёта регистра — не выбор наугад, а перечисление."""
    path = tmp_path / "c.sqlite3"
    with CandleStore(path) as store:
        store.put_minutes("SIZ6", session(DAY, 400), Source.ISS)
        store.put_minutes("Siz6", session(DAY, 10), Source.ISS)
    out = io.StringIO()
    code = show_inventory(path, "siz6", out=out, client=fake_client(offline))
    text = out.getvalue()
    assert code == 1, text
    assert "SIZ6" in text
    assert "Siz6" in text
    assert "что лежит в базе" not in text



def test_inspect_prefers_the_exact_key_to_its_case_twins(tmp_path: pathlib.Path) -> None:
    """Точный ключ есть — он и показывается, двойник в другом регистре не мешает.

    Поиск без учёта регистра — запасной путь, когда точного ключа нет.
    Без первенства точного совпадения база с `MXU6` и случайным `mxu6`
    отказала бы в описи самого обычного кода.
    Мутация, обязанная ронять (проверено 29.09.2026, до этого теста не
    ловилась ничем): убрать `if symbol in symbols: return [symbol]`
    из `_stored_key`.
    """
    path = tmp_path / "c.sqlite3"
    with CandleStore(path) as store:
        store.put_minutes("MXU6", session(DAY, 400), Source.ISS)
        store.put_minutes("mxu6", session(DAY, 10), Source.ISS)
    out = io.StringIO()
    code = show_inventory(path, "MXU6", out=out, client=fake_client(exchange(ALIVE)))
    text = out.getvalue()
    assert code == 0, text
    assert "MXU6 — что лежит в базе" in text
    assert "несколько ключей" not in text and "в другом регистре" not in text, text

# -- сшивка ближних контрактов: `python3 -m app.fetch --stitch` ---------------
#
# Что стережётся, по строке на тест:
# * ряд под биржевым кодом не собирается — отказ словами и код возврата 1;
# * умолчание базы — не рабочая база программы;
# * итог печатает разрыв цены на каждом стыке числом, а не «готово»;
# * `--no-fetch` в сеть не ходит вовсе;
# * пустая база — отказ с перечислением контрактов, а не пустой ряд.


def chain_store(path: pathlib.Path) -> None:
    """Три контракта в базе с двумя чистыми рубежами: 03.06 и 06.06.2026."""
    plan = {
        "AAA": ({0: 500, 1: 500, 2: 100}, 100.0),
        "BBB": ({0: 10, 1: 50, 2: 400, 3: 500, 4: 500, 5: 100}, 110.0),
        "CCC": ({3: 10, 4: 50, 5: 400, 6: 500, 7: 500}, 120.0),
    }
    with CandleStore(path) as store:
        for symbol, (days, price) in plan.items():
            store.put_minutes(
                symbol,
                [
                    minute(
                        datetime(2026, 6, 1, 10, 5, tzinfo=MSK) + timedelta(days=offset),
                        open=price,
                        volume=float(volume),
                    )
                    for offset, volume in days.items()
                ],
                Source.ISS,
            )


def test_stitch_loads_the_legs_under_the_exchange_codes(tmp_path: pathlib.Path) -> None:
    """Стережёт: `--legs mxm6,mxu6` качает и ищет `MXM6`, `MXU6` (`D-128`).

    Мутация, обязанная ронять проверку: убрать приведение кодов в `build_chain`.
    """
    from app.fetch import ChainRequest, build_chain

    path = tmp_path / "chain.sqlite3"
    transport = FakeTransport(exchange(ALIVE))
    out = io.StringIO()
    code = build_chain(
        path,
        ChainRequest(symbol="@MX", legs=["mxm6", "mxu6"], depth=3),
        out=out,
        client=IssClient(transport, sleep=no_sleep, pause=0),
    )
    asked = [url for url in transport.urls if "candleborders" in url]
    assert any("/MXM6/" in url for url in asked), asked
    assert any("/MXU6/" in url for url in asked), asked
    assert not any("/mxm6/" in url or "/mxu6/" in url for url in asked), asked
    assert code == 1  # свечей подставная биржа не дала
    assert "нет ни одной свечи по: MXM6, MXU6" in out.getvalue(), out.getvalue()


def test_stitch_refuses_a_leg_the_exchange_does_not_know(tmp_path: pathlib.Path) -> None:
    """Опечатка в `--legs` — отказ словами до первой загрузки."""
    from app.fetch import ChainRequest, build_chain

    transport = FakeTransport(exchange(ALIVE))
    out = io.StringIO()
    code = build_chain(
        tmp_path / "chain.sqlite3",
        ChainRequest(symbol="@MX", legs=["MXM6", "MXЫ6"], depth=3),
        out=out,
        client=IssClient(transport, sleep=no_sleep, pause=0),
    )
    assert code == 1
    assert "не знает инструмента 'MXЫ6'" in out.getvalue(), out.getvalue()
    assert not [url for url in transport.urls if "/candles.json" in url]


def test_stitch_refuses_an_exchange_code_as_the_name(tmp_path: pathlib.Path) -> None:
    """Под именем «MXU6» собранный ряд неотличим от настоящих свечей."""
    from app.fetch import ChainRequest, build_chain

    out = io.StringIO()
    code = build_chain(
        tmp_path / "chain.sqlite3",
        ChainRequest(symbol="MXU6", legs=["AAA", "BBB"], fetch=False),
        out=out,
    )
    assert code == 1
    assert "код инструмента биржи" in out.getvalue()


def test_stitch_default_base_is_not_the_working_one() -> None:
    """Ряд по умолчанию ложится не в `candles.sqlite3`: оттуда его увидит окно."""
    from app.fetch import CHAIN_DB_FILE_NAME
    from market.paths import DB_FILE_NAME

    assert CHAIN_DB_FILE_NAME != DB_FILE_NAME


def test_stitch_prints_the_price_gap_of_every_seam(tmp_path: pathlib.Path) -> None:
    """Разрыв цены на стыке печатается числом: это единственное отличие ряда."""
    from app.fetch import ChainRequest, build_chain

    database = tmp_path / "chain.sqlite3"
    chain_store(database)
    out = io.StringIO()
    code = build_chain(
        database,
        ChainRequest(symbol="@MX", legs=["AAA", "BBB", "CCC"], fetch=False),
        out=out,
    )
    printed = out.getvalue()
    assert code == 0, printed
    assert "BBB→CCC" in printed
    assert "+10.0" in printed and "+9.09 %" in printed
    assert "AAA" not in printed.split("стык")[0].split("контракт")[1]


def test_stitch_without_fetch_never_touches_the_network(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--no-fetch` собирает из того, что уже в базе, и на биржу не ходит.

    Настоящий транспорт на весь файл обезврежен, и выход наружу уронил бы
    прогон вслух — но проверка нужна отдельно: молчаливый поход на биржу
    превращает сборку ряда в получасовую операцию.
    """
    from app.fetch import ChainRequest, build_chain

    database = tmp_path / "chain.sqlite3"
    chain_store(database)
    out = io.StringIO()

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("сборка ряда полезла на биржу при --no-fetch")

    monkeypatch.setattr(IssClient, "candles", refuse)
    monkeypatch.setattr(IssClient, "borders", refuse)
    code = build_chain(
        database,
        ChainRequest(symbol="@MX", legs=["AAA", "BBB", "CCC"], fetch=False),
        out=out,
        client=IssClient(),
    )
    assert code == 0, out.getvalue()


def test_stitch_names_the_contracts_it_has_no_minutes_for(tmp_path: pathlib.Path) -> None:
    """Пустая база — отказ с перечислением контрактов, а не пустой ряд."""
    from app.fetch import ChainRequest, build_chain

    out = io.StringIO()
    code = build_chain(
        tmp_path / "chain.sqlite3",
        ChainRequest(symbol="@MX", legs=["AAA", "BBB"], fetch=False),
        out=out,
    )
    assert code == 1
    assert "AAA, BBB" in out.getvalue()


# -- переменная папки данных вне теста (D-124) -----------------------------


def _outside_a_test_run(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """Переменная папки данных выставлена, а признака тестового прогона нет."""
    from market import paths

    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.setenv(paths.USERDATA_ENV, str(tmp_path / "чужая"))
    monkeypatch.delenv(paths.TEST_RUN_ENV, raising=False)


def test_the_fetch_key_says_the_data_variable_does_not_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys
) -> None:
    """Стережёт: `--fetch` говорит в stderr, что переменная папки данных не действует.

    `D-124`: окно говорило это журналом, а `--fetch` молчал — человек,
    выставивший переменную, искал бы загруженные свечи не там.
    Мутация, обязанная ронять проверку: убрать `say_ignored_override`
    из `run_from_arguments`.
    """
    from market import paths

    _outside_a_test_run(monkeypatch, tmp_path)
    monkeypatch.setattr("app.fetch.fetch_history", lambda *a, **k: 0)
    out = io.StringIO()
    run_from_arguments(arguments(), tmp_path / "c.sqlite3", out=out)
    assert paths.USERDATA_ENV in capsys.readouterr().err
    assert paths.USERDATA_ENV not in out.getvalue(), "строка попала в выдачу, а не в stderr"


def test_the_chain_entry_says_the_data_variable_does_not_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys
) -> None:
    """Стережёт: `python3 -m app.fetch` говорит в stderr о недействующей переменной.

    Мутация, обязанная ронять проверку: убрать `say_ignored_override`
    из `app.fetch.main`.
    """
    from app import fetch
    from market import paths

    _outside_a_test_run(monkeypatch, tmp_path)
    monkeypatch.setattr(fetch, "build_chain", lambda *a, **k: 0)
    fetch.main(["--legs", "MXM6,MXU6", "--db", str(tmp_path / "chain.sqlite3"), "--no-fetch"])
    assert paths.USERDATA_ENV in capsys.readouterr().err
