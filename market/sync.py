"""Догрузка пропущенного: при запуске программа сама добирает историю.

Вручную ничего делать не нужно — это требование, а не удобство: пропущенный
отрезок означает дыру в средней и другой список сделок, а заметить его
по графику нельзя.

Как решается «что качать»
-------------------------
Наивное «взять последнюю свечу в базе и качать после неё» ломается тремя
способами, и все три встречаются:

* **дыра в середине.** Загрузили январь, потом август — февраль…июль в базе
  нет, а «последняя свеча» есть. Хвостовая догрузка их не увидит никогда;
* **недокачанный сегодняшний день.** Программу закрыли в середине сессии.
  Последняя свеча есть, а половина дня — нет, и выглядит она как обычный
  вечерний перерыв;
* **выходные и праздники.** У них свечей нет и не будет. Если считать
  «нет свечей» за «не загружено», программа будет переспрашивать про каждый
  выходной при каждом запуске — за год это сотни пустых запросов к бирже.

Поэтому учитывается **факт запроса по дням** (`data_day` в хранилище), а не
наличие свечей. Догружаются те дни периода, за которые ещё не спрашивали,
плюс те, что на момент запроса ещё не закончились.

Чему в ответе сервера верить нельзя
-----------------------------------
Обход страниц ISS кончается на **первой пустой**, и пустая страница от сбоя
балансировщика неотличима от честного конца данных. Отсюда правила, каждое
написано по конкретному случаю, а не «на всякий случай»:

* день, **после** которого в куске данных нет, не подтверждается сразу
  (`_covered_days`);
* последний день куска подтверждается, только если в базе есть минутка позже
  конца этого дня (`_settle_tails`);
* кусок длиннее `EMPTY_CHUNK_TRUSTED_DAYS`, не давший **ни одной** свечи,
  не подтверждается вовсе.

День доказывается тем, что **поток минут прошёл дальше него** (`D-149`).
Довод — порядок выдачи: ISS отдаёт минуты по возрастанию времени, и свеча
более позднего дня приходит только после всего предыдущего. `IssPagingError`
этого не стережёт: обход сравнивает лишь первые минуты страниц. Без этого
правила день на краю куска не закрывался никогда — у закрытого контракта
минут позже его последнего дня в базе нет по решению 0061, а выходные
на конце куска оставались «за краем данных» — и каждое «Догрузить» тянуло
их с биржи заново. Поэтому:

* **запрос дотягивается до места, где поток пройдёт.** Цепочка неотмеченных
  дней — по ближайший более поздний день с минутами в базе в пределах периода;
  цепочка до конца периода закрытого контракта — по его последний день торгов,
  не дальше сегодня (`proof_until`). Дни-доказательства дописываются
  к **последнему куску** цепочки: в отдельном куске их нет в базе, и доказать
  было бы нечем. Их минуты идут **только в счёт покрытия**: в базу
  не пишутся, дни не отмечаются;
* **кусок, данные которого кончились раньше его конца**, передаёт свой
  последний день с данными следующему куску той же цепочки: тот начинается
  с него, конец не сдвигается. Кусок, начатый с переданного дня и оборвавшийся
  на нём же, дальше не передаёт — иначе он повторялся бы без конца;
* **запрос-доказательство отмечает только то, что поток прошёл.** Пустой ответ
  на него — не «выходные», а «ответ не дошёл»: за его днями данные заведомо
  есть. Оборванный ответ не отмечает день обрыва. Доверие короткому пустому
  куску без доказательства не меняется.

Отчёт
-----
Каждая догрузка заканчивается записью в журнал: сколько запрошено, сколько
пришло, сколько записано, сколько отвергнуто правилом источника, сколько
минуток отсутствует, какие разрывы **внутри дня** найдены и сколько разрывов
пришлось на ночь и выходные. Без этих чисел недокачанная история неотличима
от полной.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from market.candles import MSK, Candle, ensure_msk
from market.gaps import count_missing_minutes, find_gaps
from market.iss import FetchResult, IssClient, Market
from market.reports import LoadReport
from market.storage import CandleStore, Source

__all__ = [
    "sync_minutes",
    "contiguous_ranges",
    "split_range",
    "GAP_THRESHOLD_MINUTES",
    "CHUNK_DAYS",
    "EMPTY_CHUNK_TRUSTED_DAYS",
]

#: Порог, с которого пропуск внутри дня попадает в журнал отдельной записью.
#: Ниже него — минуты без сделок; их общее число всё равно попадает в отчёт
#: полем `missing_minutes`, так что «тихой потери» не возникает.
#:
#: ⚠️ **Порог мерится длиной бара рабочего таймфрейма, а не длиной перерыва
#: биржи.** Здесь стояло 30 минут (прятало дыру в полокна), потом 16 —
#: «первое значение выше вечернего клиринга». Второе не работает по двум
#: причинам сразу:
#:
#: * **запас нулевой.** Вечерний клиринг 18:50–19:05 даёт ровно 15 минут
#:   только если сделка была и в 18:49, и в 19:05. На MXU6 пусты 26 % минутных
#:   слотов: сделка в 18:48 — и пропуск уже 16 минут, запись в журнале.
#:   Свойство «штатные перерывы не попадают» держалось на синтетических
#:   непрерывных рядах тестов, а не на данных;
#: * **интересна не длина перерыва биржи, а число потерянных решений.**
#:   Пропуск в 10 минут — это два пятиминутных бара, которых у движка нет.
#:   Он не менее важен, чем пропуск в 20.
#:
#: Отсюда умолчание: **пять минут — один бар рабочего таймфрейма** (размер
#: свечи 5 минут, умолчание ТЗ). Вызывающий, работающий на другом размере,
#: передаёт свой: `gap_threshold_minutes=timeframe.minutes`.
#:
#: Штатные перерывы биржи при таком пороге в журнал попадают — и это принято
#: сознательно: отличить их от дыры без торгового календаря нельзя, а календаря
#: у слоя данных нет. Ночной перерыв и выходные не попадают: они помечены
#: `crosses_date` и в журнал разрывов не пишутся вовсе (см. `sync_minutes`).
GAP_THRESHOLD_MINUTES = 5

#: На сколько дней максимум режется один запрос к бирже.
#: Год минуток — это четыре сотни страниц подряд; при обрыве на предпоследней
#: без нарезки пропадает вся работа. Каждый кусок пишется в базу и отмечается
#: сразу, поэтому повторный запуск продолжает с места обрыва, а не с начала.
CHUNK_DAYS = 30

#: Насколько длинному куску без единой свечи ещё можно поверить.
#:
#: Ответ «200, `data` пустой» приходит и когда инструмента на доске нет,
#: и при смене режима торгов, и при обслуживании. Политика повторов это
#: не ловит: повторяются сбои, а пустая страница — законный конец обхода.
#: Отметить такой кусок загруженным целиком значит закрыть эти дни навсегда:
#: следующий запуск скажет «всё уже загружено, запросов не потребовалось»,
#: и восстановление останется только через удаление базы.
#:
#: Для куска в один-три дня «пусто» законно — выходные, праздники. Для куска
#: в тридцать дней незаконно: самый длинный нерабочий период российской биржи
#: (новогодние каникулы) — около десяти дней. Календарь для такого вывода
#: не нужен, нужна только длина.
EMPTY_CHUNK_TRUSTED_DAYS = 10


def contiguous_ranges(days: Sequence[date]) -> list[tuple[date, date]]:
    """Сгруппировать дни в непрерывные отрезки — чтобы качать пачками."""
    ranges: list[tuple[date, date]] = []
    for day in sorted(days):
        if ranges and day - ranges[-1][1] == timedelta(days=1):
            ranges[-1] = (ranges[-1][0], day)
        else:
            ranges.append((day, day))
    return ranges


def split_range(since: date, until: date, chunk_days: int) -> list[tuple[date, date]]:
    """Порезать отрезок дат на куски не длиннее `chunk_days`."""
    if chunk_days < 1:
        raise ValueError("кусок короче одного дня не имеет смысла")
    chunks: list[tuple[date, date]] = []
    start = since
    while start <= until:
        stop = min(start + timedelta(days=chunk_days - 1), until)
        chunks.append((start, stop))
        start = stop + timedelta(days=1)
    return chunks


#: Сутки — шаг по календарю.
_DAY = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class _Fetch:
    """Чем и что качать — одно на всю загрузку, а не шесть аргументов у каждого шага."""

    store: CandleStore
    client: IssClient
    symbol: str
    market: Market
    source: Source
    progress: Callable[[FetchResult], None] | None


@dataclass(frozen=True, slots=True)
class _Chunk:
    """Один запрос к бирже: загружаемые дни и дни-доказательства за ними (`D-149`).

    `start`…`end` — загружаемые дни: их минуты пишутся, их отмечают.
    Дни после `end` по `proof_end` — дни-доказательства: спрашиваются, чтобы
    поток минут прошёл дальше `end` **в этом же запросе**. Их минуты идут только
    в счёт покрытия — в базу не пишутся, дни не отмечаются.
    `carried` — дни, переданные предыдущим куском: его последний день с данными
    и пустые дни за ним. Первый из них — `start`.
    """

    start: date
    end: date
    proof_end: date
    carried: tuple[date, ...] = ()

    @property
    def proves(self) -> bool:
        """Запрос-доказательство: за его днями данные заведомо есть.

        День за концом — день с минутами в базе либо последний день торгов;
        переданный день — данные за ним в этой же загрузке уже пришли. Пустой
        ответ на такой запрос — не выходные, а «ответ не дошёл».
        """
        return self.proof_end > self.end or bool(self.carried)


def sync_minutes(
    store: CandleStore,
    client: IssClient,
    symbol: str,
    *,
    market: Market,
    since: date,
    until: date,
    source: Source = Source.ISS,
    now: datetime | None = None,
    gap_threshold_minutes: int = GAP_THRESHOLD_MINUTES,
    chunk_days: int = CHUNK_DAYS,
    progress: Callable[[FetchResult], None] | None = None,
    write_report: bool = True,
    proof_until: date | None = None,
) -> LoadReport:
    """Догрузить минутные свечи за период и записать отчёт в журнал.

    `since` и `until` — календарные даты МСК, обе **включительно**: так их
    понимает ISS, и так их вводит человек.

    `proof_until` — до какого дня запрос вправе заходить за `until`, чтобы
    доказать последний день периода (`D-149`): последний день торгов закрытого
    контракта. Минуты этих дней в базу не пишутся. `None` — за `until` не ходить.
    """
    now = ensure_msk(now or datetime.now(MSK))
    if until < since:
        raise ValueError(f"период задом наперёд: {since} .. {until}")

    report = LoadReport(
        symbol=symbol,
        source=source.value,
        requested_from=since,
        requested_to=until,
        started_at=now,
    )
    how = _Fetch(store, client, symbol, market, source, progress)
    beyond = (
        min(proof_until, now.date())
        if proof_until is not None and proof_until > until else None
    )
    chunks = _plan(how, store.days_to_request(symbol, since, until),
                   until=until, chunk_days=chunk_days, beyond=beyond)
    report.ranges = [(chunk.start, chunk.end) for chunk in chunks]
    report.proofs = [(chunk.end + _DAY, chunk.proof_end)
                     for chunk in chunks if chunk.proof_end > chunk.end]
    if not chunks:
        _note(report, "всё уже загружено, запросов не потребовалось")
    done = 0
    #: День на конце куска, который **может** подтвердиться данными следующего
    #: куска. Разбирается после обхода всех кусков, а не сразу: иначе годовая
    #: догрузка оставляла бы по одному неподтверждённому дню на каждый кусок.
    tails: dict[date, dict[date, int]] = {}

    try:
        for index, chunk in enumerate(chunks):
            counts = _fetch_chunk(how, chunk, report)
            # Куску верим ровно настолько, насколько он покрыт данными.
            #
            # `client.minutes()` не обещает, что вернул весь запрошенный
            # диапазон, а обход страниц заканчивается на первой пустой —
            # и пустая страница от сбоя сервера, балансировщика или обрыва
            # внутри keep-alive неотличима от честного конца данных.
            # Отметив весь кусок, мы навсегда исключаем недокачанные дни
            # из `days_to_request`: дыра в истории остаётся невидимой,
            # прогон зелёный, а параметры подбираются на неполных данных.
            covered, cut, broke_off = _covered_days(chunk, counts, now=now)
            store.mark_days_requested(symbol, covered, counts=counts, now=now)
            if not _hand_over(chunks, index, broke_off, cut, report):
                _account(report, chunk, (cut, broke_off), counts, tails)
            done += 1
    except Exception as error:
        # Сорвавшаяся загрузка тоже попадает в журнал — иначе в базе просто
        # окажется меньше свечей, чем нужно, и ни одной записи о том, почему.
        # Скачанные куски уже записаны и отмечены: следующий запуск продолжит
        # с места обрыва, а не с начала.
        _note(
            report,
            f"загрузка оборвалась на куске {done + 1} из {len(chunks)}: "
            f"{type(error).__name__}: {error}",
        )
        # Дни, переданные сорвавшемуся куску, не отмечены и не разобраны.
        report.incomplete.extend(chunks[done].carried if done < len(chunks) else ())
        # Куски, скачанные до обрыва, уже записаны — значит, и разобраться
        # с их хвостами надо сейчас, иначе повторный запуск качает их заново.
        try:
            _settle_tails(store, symbol, tails, report, now=now)
        except Exception as second:  # noqa: BLE001 - первая причина важнее
            _note(report, f"учёт хвостов не доведён до конца: {second!r}")
        report.incomplete.sort()
        report.finished_at = datetime.now(MSK)
        if write_report:
            store.write_load_report(report, now=report.finished_at)
        raise

    _settle_tails(store, symbol, tails, report, now=now)
    report.incomplete.sort()

    times = store.minute_times(
        symbol,
        datetime.combine(since, datetime.min.time(), MSK),
        datetime.combine(until + _DAY, datetime.min.time(), MSK),
    )
    report.missing_minutes = count_missing_minutes(times)
    # В журнал отдельными записями идут только разрывы **внутри дня**.
    # Ночь и выходные дают разрыв каждый календарный день: за год это под
    # три сотни записей, в которых тонут те несколько, ради которых журнал
    # и ведётся. Их число — отдельным полем, а не в общей куче.
    found = find_gaps(times, min_minutes=gap_threshold_minutes)
    report.gaps = [gap for gap in found if not gap.crosses_date]
    report.overnight_gaps = sum(1 for gap in found if gap.crosses_date)
    report.first_time = times[0] if times else None
    report.last_time = times[-1] if times else None
    report.finished_at = datetime.now(MSK)

    if write_report:
        store.write_load_report(report, now=report.finished_at)
    return report


def _plan(
    how: _Fetch, days: Sequence[date], *, until: date, chunk_days: int, beyond: date | None
) -> list[_Chunk]:
    """Куски запроса: цепочки неотмеченных дней, нарезанные по `chunk_days`.

    Дни-доказательства — к **последнему** куску цепочки, а не к цепочке
    до нарезки: при длине цепочки, кратной `chunk_days`, они ушли бы одни
    в свой кусок, и доказывать последний день цепочки было бы нечем.
    """
    pending = set(days)
    chunks: list[_Chunk] = []
    for first, last in contiguous_ranges(days):
        pieces = split_range(first, last, chunk_days)
        chunks += [_Chunk(start, end, end) for start, end in pieces[:-1]]
        start, end = pieces[-1]
        proof_end = _proof_end(how, end, pending, until, beyond)
        chunks.append(_Chunk(start, end, proof_end or end))
    return chunks


def _proof_end(
    how: _Fetch, last: date, pending: set[date], until: date, beyond: date | None
) -> date | None:
    """До какого дня дотянуть запрос, чтобы поток минут прошёл дальше `last`.

    В пределах периода — по ближайший день с минутами в базе: дни между ним
    и `last` уже отмечены. Встретился неотмеченный день — это начало следующей
    цепочки, её запрос и подтвердит `last` (`_settle_tails`). За концом
    периода — по `beyond`, последний день торгов, если он есть. `None` —
    доказывать нечем, запрос не удлиняется.
    """
    later = how.store.has_minutes_after(how.symbol, _day_end(last))
    day = last + _DAY
    while day <= until:
        if day in pending:
            return None
        if later and how.store.minute_times(how.symbol, _day_end(day - _DAY), _day_end(day)):
            return day
        day += _DAY
    return beyond if beyond is not None and beyond >= day else None


def _fetch_chunk(how: _Fetch, chunk: _Chunk, report: LoadReport) -> dict[date, int]:
    """Спросить кусок у биржи и записать загружаемые дни. Счёт свечей — по всем дням."""
    result = how.client.minutes(
        how.symbol,
        market=how.market,
        date_from=chunk.start,
        date_to=chunk.proof_end,
        progress=how.progress,
    )
    report.fetched += len(result.candles)
    report.pages += result.pages
    report.requests += result.requests
    report.retries += result.retries
    report.duplicates += result.duplicates
    # Минуты дней-доказательств — только в счёт покрытия: за `until`
    # закрытого контракта в базе минут быть не должно (решение 0061).
    own = [candle for candle in result.candles if ensure_msk(candle.time).date() <= chunk.end]
    report.proof_candles += len(result.candles) - len(own)
    stats = how.store.put_minutes(how.symbol, own, how.source)
    report.inserted += stats.inserted
    report.updated += stats.updated
    report.kept += stats.kept
    report.collapsed += stats.collapsed
    return _count_by_day(result.candles)


def _hand_over(
    chunks: list[_Chunk], index: int, broke_off: date | None, cut: list[date], report: LoadReport
) -> bool:
    """Данные куска кончились раньше его конца — передать край следующему куску.

    Выходные на конце тридцатидневного куска оставались «за краем данных»
    навсегда: подтвердить их было нечем, и каждая загрузка спрашивала их
    снова. Следующий кусок той же цепочки начинается с последнего дня
    с данными — поток пройдёт через пустые дни сам. Конец следующего куска
    не сдвигается. `False` — передавать некому: следующего куска нет, он
    не встык, либо этот кусок сам начат с переданного дня и на нём же
    оборвался — повторять его значило бы ходить по кругу.
    """
    if broke_off is None or not cut or index + 1 >= len(chunks):
        return False
    chunk, following = chunks[index], chunks[index + 1]
    if following.start != chunk.end + _DAY or (chunk.carried and broke_off <= chunk.start):
        return False
    chunks[index + 1] = replace(following, start=broke_off, carried=(broke_off, *cut))
    report.ranges[index + 1] = (broke_off, following.end)
    return True


def _account(
    report: LoadReport,
    chunk: _Chunk,
    edge: tuple[list[date], date | None],
    counts: dict[date, int],
    tails: dict[date, dict[date, int]],
) -> None:
    """Записать в отчёт дни, которые ответ не доказал. `edge` — `cut` и `broke_off`."""
    cut, broke_off = edge
    if cut and not counts:
        report.incomplete.extend(cut)
        _note(report, _empty_text(chunk, cut))
    elif cut:
        report.incomplete.extend(cut)
        _note(
            report,
            f"кусок {chunk.start}—{chunk.end} отдан не полностью: данных "
            f"нет с {cut[0]}. Дни {cut[0]}—{cut[-1]} не отмечены "
            "загруженными и будут перезапрошены при следующем запуске",
        )
    if broke_off is None:
        return
    if cut:
        # Обрыв **внутри** куска: подтвердить этот день уже нечем.
        report.incomplete.append(broke_off)
        _note(
            report,
            f"День {broke_off}, на котором выдача оборвалась, тоже "
            "не отмечен: его хвост мог не дойти, и тогда последний бар "
            "дня — огрызок, выданный за целый",
        )
    elif chunk.proof_end > chunk.end:
        # Запрос дотянут дальше дня, а поток дальше него не прошёл.
        report.incomplete.append(broke_off)
        _note(
            report,
            f"день {broke_off}: запрос дотянут по {chunk.proof_end}, чтобы "
            "подтвердить его целиком, но свечей позже него не пришло. День "
            "не отмечен загруженным и будет перезапрошен",
        )
    else:
        tails[broke_off] = counts


def _empty_text(chunk: _Chunk, cut: list[date]) -> str:
    """Пояснение к куску без единой свечи: почему пустоте не поверили."""
    if chunk.proves:
        return (
            f"запрос {chunk.start}—{chunk.proof_end} не дал ни одной свечи, хотя "
            "за его днями данные есть — в базе или в этой же загрузке. Ответ "
            f"не дошёл: дни {cut[0]}—{cut[-1]} не отмечены загруженными и будут "
            "перезапрошены"
        )
    return (
        f"кусок {chunk.start}—{chunk.end} ({len(cut)} дн.) не дал ни одной "
        "свечи. Пусто за такой срок штатным выходным не объясняется "
        f"(самый длинный нерабочий период биржи — около "
        f"{EMPTY_CHUNK_TRUSTED_DAYS} дней): дни не отмечены загруженными "
        "и будут перезапрошены"
    )


def _note(report: LoadReport, text: str) -> None:
    """Дописать строку к пояснению отчёта, а не затереть прежнюю.

    Кусков в загрузке много, и каждый может рассказать своё. Присваивание
    оставляло в журнале только последнее из сказанного.
    """
    report.note = f"{report.note}. {text}" if report.note else text


def _day_end(day: date) -> datetime:
    """Полночь **после** этого дня, МСК."""
    return datetime.combine(day + timedelta(days=1), datetime.min.time(), MSK)


def _settle_tails(
    store: CandleStore,
    symbol: str,
    tails: dict[date, dict[date, int]],
    report: LoadReport,
    *,
    now: datetime,
) -> None:
    """Разобрать дни, стоявшие на конце куска.

    Кусок кончился ровно там, где кончились данные. Это либо честный конец
    истории, либо обрыв **внутри** последнего дня — по самому ответу они
    неразличимы, и раньше такой день молча подтверждался: обрыв «26.08 12:31»
    делал бар 12:30 из двух минуток вместо пяти закрытым и полноправным
    участником средней.

    Различить их можно данными, которые уже лежат в базе: **если есть хоть
    одна минутка позже конца этого дня, обрыв внутри него исключён.** ISS
    отдаёт минуты по возрастанию времени, поэтому до следующего дня обход
    добрался бы только через весь предыдущий. `IssPagingError` этого
    не стережёт: обход сравнивает лишь первые минуты страниц.

    Чего это стоит: последний день загруженного отрезка остаётся
    неподтверждённым, пока не появятся данные следующего дня, и один кусок
    в один день перезапрашивается при каждом запуске. В боевом ходу это
    сегодняшний день, который и так перезапрашивается всегда.

    ⚠️ **Остаточный риск, который правило не закрывает.** Минутка позже конца
    дня могла попасть в базу другой загрузкой — например, следующий день
    скачали раньше. Тогда обрыв внутри этого дня подтверждением не опровергнут,
    а день всё равно закроется. Правило принято сознательно: без него день,
    за которым данных больше нет, не подтвердится никогда, и осторожность
    превратится в вечный перезапрос.
    """
    for day in sorted(tails):
        if store.has_minutes_after(symbol, _day_end(day)):
            store.mark_days_requested(symbol, [day], counts=tails[day], now=now)
            continue
        report.incomplete.append(day)
        _note(
            report,
            f"день {day} — последний, за который пришли данные, и подтвердить "
            "его нечем: свечей позже него в базе нет, а обрыв выдачи внутри "
            "дня от честного конца истории по ответу не отличается. День "
            "не отмечен загруженным и будет перезапрошен",
        )


def _days_between(since: date, until: date) -> list[date]:
    return [since + timedelta(days=i) for i in range((until - since).days + 1)]


def _covered_days(
    chunk: _Chunk, counts: dict[date, int], *, now: datetime
) -> tuple[list[date], list[date], date | None]:
    """Разделить загружаемые дни куска на «подтверждённые» и «за краем данных».

    Подтверждённым считается день раньше последнего дня, за который свечи
    реально пришли. Всё, что дальше, — не «выходной без сделок», а «сюда
    ответ не дошёл»: отличить эти два случая по нулю нельзя, и ошибка в пользу
    «загружено» необратима, потому что день больше не переспросят.

    Свеча дня-доказательства (`_Chunk.proof_end`) значит, что поток прошёл
    дальше всех загружаемых дней: подтверждены все. Сами дни-доказательства
    не возвращаются ни в одном списке — их не отмечают и не перезапрашивают.

    Хвост из будущих дней (кусок захватил сегодня и дальше) сюда не попадает:
    его и так отсекает `settled` в хранилище.

    Третьим возвращается **день, на котором кончились данные** (или `None`,
    если кусок целиком в сегодня и дальше, либо не дал ни одной свечи).
    Он не подтверждается здесь ни при каких условиях: обход страниц кончается
    на первой пустой, то есть обрыв случается **внутри** этого дня, а не между
    днями. Его хвост мог не дойти — и тогда учёт объявил бы день загруженным,
    а `CandleStore.bars` поверил бы учёту и выдал бы последний бар дня как
    закрытый, хотя это огрызок.

    Дальше судьба этого дня зависит от того, где он оказался:

    * **в середине куска** (после него есть дни, за которые ответа не было) —
      подтвердить нечем, день уходит в `incomplete`;
    * **на конце куска** — решает `_settle_tails` по данным в базе; у куска
      с днями-доказательствами — не подтверждается: поток до них не дошёл.
    """
    days = _days_between(chunk.start, chunk.end)
    today = ensure_msk(now).date()
    if not counts:
        # Ни одной свечи за весь кусок. Для куска в один-три дня это законно
        # (выходные, праздники), для длинного — нет: самый долгий нерабочий
        # период биржи около десяти дней. Торгового календаря у слоя нет
        # и быть не должно, а длина куска известна и без него. Запросу-
        # доказательству пустым быть нельзя вовсе: за его днями данные есть.
        if chunk.proves or len(days) > EMPTY_CHUNK_TRUSTED_DAYS:
            return (
                [day for day in days if day >= today],
                [day for day in days if day < today],
                None,
            )
        return days, [], None
    last_with_data = max(counts)
    if last_with_data >= today or last_with_data > chunk.end:
        # Данные дошли до сегодня либо до дня-доказательства: поток прошёл
        # дальше всех загружаемых дней. Сегодня и позже всё равно
        # не отмечаются закрытыми (`settled` в хранилище).
        return days, [], None
    covered = [day for day in days if day < last_with_data or day >= today]
    cut = [day for day in days if last_with_data < day < today]
    return covered, cut, last_with_data


def _count_by_day(candles: Iterable[Candle]) -> dict[date, int]:
    counts: dict[date, int] = {}
    for candle in candles:
        day = ensure_msk(candle.time).date()
        counts[day] = counts.get(day, 0) + 1
    return counts
