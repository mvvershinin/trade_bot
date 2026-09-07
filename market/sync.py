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
балансировщика неотличима от честного конца данных. Отсюда три правила, каждое
написано по конкретному случаю, а не «на всякий случай»:

* день, **после** которого в куске данных нет, не подтверждается сразу
  (`_covered_days`);
* последний день куска подтверждается, только если в базе есть минутка позже
  конца этого дня (`_settle_tails`);
* кусок длиннее `EMPTY_CHUNK_TRUSTED_DAYS`, не давший **ни одной** свечи,
  не подтверждается вовсе.

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
) -> LoadReport:
    """Догрузить минутные свечи за период и записать отчёт в журнал.

    `since` и `until` — календарные даты МСК, обе **включительно**: так их
    понимает ISS, и так их вводит человек.
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

    days = store.days_to_request(symbol, since, until)
    report.ranges = [
        chunk
        for range_from, range_to in contiguous_ranges(days)
        for chunk in split_range(range_from, range_to, chunk_days)
    ]
    if not report.ranges:
        _note(report, "всё уже загружено, запросов не потребовалось")
    done: list[tuple[date, date]] = []
    #: День на конце куска, который **может** подтвердиться данными следующего
    #: куска. Разбирается после обхода всех кусков, а не сразу: иначе годовая
    #: догрузка оставляла бы по одному неподтверждённому дню на каждый кусок.
    tails: dict[date, dict[date, int]] = {}

    try:
        for range_from, range_to in report.ranges:
            result = client.minutes(
                symbol,
                market=market,
                date_from=range_from,
                date_to=range_to,
                progress=progress,
            )
            report.fetched += len(result.candles)
            report.pages += result.pages
            report.requests += result.requests
            report.retries += result.retries
            report.duplicates += result.duplicates

            stats = store.put_minutes(symbol, result.candles, source)
            report.inserted += stats.inserted
            report.updated += stats.updated
            report.kept += stats.kept
            report.collapsed += stats.collapsed

            # Куску верим ровно настолько, насколько он покрыт данными.
            #
            # `client.minutes()` не обещает, что вернул весь запрошенный
            # диапазон, а обход страниц заканчивается на первой пустой —
            # и пустая страница от сбоя сервера, балансировщика или обрыва
            # внутри keep-alive неотличима от честного конца данных.
            # Отметив весь кусок, мы навсегда исключаем недокачанные дни
            # из `days_to_request`: дыра в истории остаётся невидимой,
            # прогон зелёный, а параметры подбираются на неполных данных.
            counts = _count_by_day(result.candles)
            covered, cut, broke_off = _covered_days(
                _days_between(range_from, range_to), counts, now=now
            )
            store.mark_days_requested(symbol, covered, counts=counts, now=now)
            if cut and not counts:
                report.incomplete.extend(cut)
                _note(
                    report,
                    f"кусок {range_from}—{range_to} ({len(cut)} дн.) не дал ни одной "
                    "свечи. Пусто за такой срок штатным выходным не объясняется "
                    f"(самый длинный нерабочий период биржи — около "
                    f"{EMPTY_CHUNK_TRUSTED_DAYS} дней): дни не отмечены загруженными "
                    "и будут перезапрошены",
                )
            elif cut:
                report.incomplete.extend(cut)
                _note(
                    report,
                    f"кусок {range_from}—{range_to} отдан не полностью: данных "
                    f"нет с {cut[0]}. Дни {cut[0]}—{cut[-1]} не отмечены "
                    "загруженными и будут перезапрошены при следующем запуске",
                )
            if broke_off is not None:
                if cut:
                    # Обрыв **внутри** куска: подтвердить этот день уже нечем.
                    report.incomplete.append(broke_off)
                    _note(
                        report,
                        f"День {broke_off}, на котором выдача оборвалась, тоже "
                        "не отмечен: его хвост мог не дойти, и тогда последний бар "
                        "дня — огрызок, выданный за целый",
                    )
                else:
                    tails[broke_off] = counts
            done.append((range_from, range_to))
    except Exception as error:
        # Сорвавшаяся загрузка тоже попадает в журнал — иначе в базе просто
        # окажется меньше свечей, чем нужно, и ни одной записи о том, почему.
        # Скачанные куски уже записаны и отмечены: следующий запуск продолжит
        # с места обрыва, а не с начала.
        _note(
            report,
            f"загрузка оборвалась на куске {len(done) + 1} из {len(report.ranges)}: "
            f"{type(error).__name__}: {error}",
        )
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
        datetime.combine(until + timedelta(days=1), datetime.min.time(), MSK),
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
    одна минутка позже конца этого дня, обрыв внутри него исключён.** Страницы
    ISS идут по возрастанию времени (несовпадение — `IssPagingError`), поэтому
    до следующего дня обход добрался бы только через весь предыдущий.
    Порядок строк **внутри** страницы при этом не обещан и здесь не нужен.

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
    days: list[date], counts: dict[date, int], *, now: datetime
) -> tuple[list[date], list[date], date | None]:
    """Разделить дни куска на «подтверждённые» и «за краем данных».

    Подтверждённым считается день не позже последнего дня, за который свечи
    реально пришли. Всё, что дальше, — не «выходной без сделок», а «сюда
    ответ не дошёл»: отличить эти два случая по нулю нельзя, и ошибка в пользу
    «загружено» необратима, потому что день больше не переспросят.

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
    * **на конце куска** — решает `_settle_tails` по данным в базе.
    """
    if not days:
        return [], [], None
    today = ensure_msk(now).date()
    if not counts:
        # Ни одной свечи за весь кусок. Для куска в один-три дня это законно
        # (выходные, праздники), для длинного — нет: самый долгий нерабочий
        # период биржи около десяти дней. Торгового календаря у слоя нет
        # и быть не должно, а длина куска известна и без него.
        if len(days) > EMPTY_CHUNK_TRUSTED_DAYS:
            return (
                [day for day in days if day >= today],
                [day for day in days if day < today],
                None,
            )
        return list(days), [], None
    last_with_data = max(counts)
    if last_with_data >= today:
        # Данные дошли до сегодня: дальше куску верить не во что, а сегодня
        # и позже всё равно не отмечаются закрытыми (`settled` в хранилище).
        return list(days), [], None
    covered = [day for day in days if day <= last_with_data or day >= today]
    cut = [day for day in days if last_with_data < day < today]
    return [day for day in covered if day != last_with_data], cut, last_with_data


def _count_by_day(candles: Iterable[Candle]) -> dict[date, int]:
    counts: dict[date, int] = {}
    for candle in candles:
        day = ensure_msk(candle.time).date()
        counts[day] = counts.get(day, 0) + 1
    return counts
