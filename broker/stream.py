"""Поток свечей от брокера: подписка и снимки минуты.

Что этот модуль делает и чего не делает
---------------------------------------
Делает ровно одно: держит сокет, подписывается на инструмент и отдаёт наружу
снимки свечи в том виде, в каком их прислал брокер. Правил закрытия бара,
разбора устаревших данных и решений здесь нет и быть не должно — им место
в `app/`, потому что это политика источника, а не разговор с брокером.

Почему снимок, а не свеча
-------------------------
Канал называется «последняя свеча» и шлёт **одну и ту же минуту многократно**,
каждый раз целиком, нарастающим итогом. Замер 04.09.2026 на MXU6: минута
пришла 8–14 раз, `volume` монотонно рос, `low` монотонно падал, `high` держался.
Признака «свеча закрыта» в сообщении **нет ни в каком виде** — ни поля, ни флага.

Отсюда имя `CandleSnapshot`: это состояние минуты на момент отправки, а не
законченная свеча. Кто и когда объявит минуту закрытой — решает вызывающий.

⚠️ Расхождения документации с сервером, проверенные живым запросом 04.09.2026
-----------------------------------------------------------------------------
* поле подписки называется **`subscribeType`**, а не `subscriberType`, как
  написано в PDF-версии документации: с документным именем сервер отвечает
  `{"errors":[{"type":"REQUIRED_FIELD","field":"subscribeType"}]}`;
* поле времени в ответе — **`dateTime`**, а не `dateTimeUtc`, и ответ **плоский**,
  без вложенных контейнеров `candleStick` / `instrument`, обещанных в PDF;
* `dateTime` свечи — это **начало минуты**, а не время отправки: все снимки
  одной минуты несут одну и ту же метку. Часы сервера по этому каналу
  не сверить (для этого есть канал котировок).

Страница сайта оказалась верна, PDF устарел. Снимок документации —
[`.docs/broker-api/20-ws-last-candle.md`](../.docs/broker-api/20-ws-last-candle.md).

Слой
----
`broker/` не импортирует ни один слой проекта (`ARCHITECTURE.md` §2), поэтому
наружу отдаётся собственный тип, а не свеча слоя данных. Перекладку делает `app/`
одной явной строкой, которую видно в ревью.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import websockets

from broker.errors import BrokerError, NoConnection, from_status
from broker.parsing import aware_moment
from broker.redaction import LOGGER_NAME
from broker.session import BrokerSession

#: Сокет рыночных данных. Один на все четыре канала, различает `dataType`.
MARKET_DATA_WS: Final[str] = (
    "wss://ws.broker.ru/trade-api-market-data-connector/api/v1/market-data/ws"
)

#: Значения `subscribeType`: подписка и отписка.
SUBSCRIBE: Final[int] = 0
UNSUBSCRIBE: Final[int] = 1

#: Значение `dataType` для свечей. Остальные каналы того же сокета:
#: 0 — стакан, 2 — обезличенные сделки, 3 — котировки.
CANDLES: Final[int] = 1

#: Ответ на подписку и ответ с данными различаются полем `responseType`.
SUBSCRIBED: Final[str] = "CandleStickSuccess"
CANDLE: Final[str] = "CandleStick"

#: Логгер, который получает `websockets` для этого сокета: **наше поддерево**,
#: а не `websockets.client`, выбираемый библиотекой по умолчанию.
#:
#: ⚠️ Это не косметика имени. Библиотека печатает **каждый** заголовок
#: рукопожатия обычным `debug` (`websockets/client.py:294-297`, проверено
#: по исходнику 17.0.1), а среди заголовков — `Authorization: Bearer
#: <рабочий токен>`, сутки полного доступа к счёту. Одного
#: `logging.basicConfig(level=DEBUG)` при разборе обрыва довольно, чтобы
#: токен лёг на диск.
#:
#: Ветку `websockets` слой брокера накрывать не вправе: она общая для процесса,
#: а настройка чужих логгеров — обязанность `app/` (`ARCHITECTURE.md` §2,
#: разбор — в докстринге `redaction.install`). Пока имя логгера оставалось
#: библиотечным, гарантия «токен не попадёт в лог» держалась на одной строке
#: `install_redaction((LOGGER_NAME, *FOREIGN_LOGGERS))` в сборке `app/`,
#: и процесс, собранный мимо неё, писал бы токен в лог молча.
#:
#: Имя под `broker` переносит записи рукопожатия в поддерево, на которое
#: чистка ставится при импорте слоя (`broker/__init__.py`), то есть раньше
#: любого сетевого вызова и независимо от того, кто собрал программу.
#: `FOREIGN_LOGGERS` этим не отменяется: `websockets` остаётся там ради
#: чужих сокетов и версий, где логгер задать нельзя.
SOCKET_LOGGER: Final[str] = f"{LOGGER_NAME}.stream.socket"


@dataclass(frozen=True, slots=True)
class CandleSnapshot:
    """Состояние минуты на момент отправки — как пришло от брокера.

    `opened_at` — **начало** минуты, с часовым поясом (брокер шлёт UTC).
    `turnover` — оборот в валюте инструмента, **не количество контрактов**:
    в примере документации по обезличенным сделкам `volume` 612 при цене 305,89
    и количестве 2, то есть цена × штуки. Пересчёт в контракты — забота
    вызывающего, здесь значение хранится как пришло.
    """

    ticker: str
    class_code: str
    timeframe: str
    opened_at: datetime
    open: float
    high: float
    low: float
    close: float
    turnover: float


class StreamRejected(BrokerError):
    """Брокер отказал в подписке или прислал свечу, которую нельзя разобрать.

    Повтор без правки запроса помогает только при `NO_DATE` — данных
    по инструменту сейчас нет (миниплан Э1-5, §4.6: не обрыв, повтор
    по паузе). `NOT_FOUND`, `BAD_REQUEST`, `UNAUTHORIZED` и чужой формат
    свечи повтором не лечатся: поток снимается, нужен человек.
    """

    def __init__(
        self,
        human: str,
        technical: str = "",
        *,
        retryable: bool = False,
        details: str = "",
    ) -> None:
        super().__init__(human, technical, details=details)
        self.retryable = retryable


#: Код отказа, при котором подписку стоит повторить: данных пока нет.
NO_DATA: Final[str] = "NO_DATE"


def _moment(text: object) -> datetime:
    """`dateTime` брокера → момент с часовым поясом. Наивное время — отказ.

    Разбор общий с историческими свечами (`broker/parsing.aware_moment`,
    там же и объяснение, почему наивное время отвергается, а не приводится).
    Общий он намеренно: поток и догрузка кладут свечи в одну и ту же базу,
    и правило про пояс, разъехавшееся между ними, дало бы сдвиг на три часа
    ровно в тех минутах, которые догружены после обрыва.
    """
    return aware_moment(text)


def _frame_kind(raw: str | bytes) -> str:
    """Вид и длина кадра — **без единого знака из него**.

    Кадр пришёл из сети, и в `human`/`technical` содержимому из сети хода нет
    (шапка `broker/errors.py`: тело ответа однажды уже выплеснулось владельцу
    счёта в консоль страницей разметки вместо сообщения).
    """
    kind = "байты" if isinstance(raw, bytes) else "текст"
    return f"{kind}, длина {len(raw)}"


def _text_of(raw: str | bytes) -> str:
    """Кадр как текст — для поля `details`, единственного, куда сырое пускают."""
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw


def _parsed(raw: str | bytes) -> dict[str, Any]:
    """Кадр сокета → сообщение брокера. Негодный кадр — отказ **с повтором**.

    Негодных видов два: кадр не разбирается как JSON вовсе (обрывок, текстовый
    служебный кадр, испорченная передача) и кадр — годный JSON, но не объект
    (массив, строка, число). Прежде первый выходил наружу `JSONDecodeError`,
    а второй — `AttributeError` на `message.get("errors")`, и ни то ни другое
    не было отказом слоя.

    Цена этого названа ревью 05.09.2026. Вызывающий (`app/live_feed._ride`)
    считает всё, что не `BrokerError`, бедой программы и снимает поток
    **окончательно**: один битый кадр стоил всех минут до конца сеанса, причём
    дыру такого размера догрузка закрыла бы при следующем подключении —
    которого уже не будет, потому что повтора нет и нужен человек с кнопкой.

    Отсюда `retryable=True`: подписка переоткрывается, правым краем ряда
    занимается новый заход, пропущенное добирает догрузка. Молча пропустить
    кадр — не то же самое: `None` в этой функции означает «служебное сообщение,
    и мы знаем какое», а незнакомый кадр означает, что канал понят неверно;
    молчание превратило бы это в пустой график без единого объяснения.
    """
    try:
        message = json.loads(raw)
    except (ValueError, TypeError) as error:
        # `JSONDecodeError` — не JSON, `UnicodeDecodeError` — не UTF-8;
        # обе от `ValueError`. `TypeError` — кадр не строка и не байты.
        raise StreamRejected(
            "Брокер прислал в поток свечей кадр, который не разбирается как "
            "сообщение. Программа переоткроет подписку.",
            f"поток свечей: кадр не JSON ({_frame_kind(raw)}): {type(error).__name__}",
            retryable=True,
            details=_text_of(raw),
        ) from None
    if not isinstance(message, dict):
        raise StreamRejected(
            "Брокер прислал в поток свечей сообщение незнакомого вида. "
            "Программа переоткроет подписку.",
            f"поток свечей: сообщение не объект, а {type(message).__name__} "
            f"({_frame_kind(raw)})",
            retryable=True,
            details=_text_of(raw),
        )
    return message


def _snapshot(payload: dict[str, Any]) -> CandleSnapshot:
    """Сообщение брокера → снимок. Недостающее поле — отказ вслух.

    Молчаливая подстановка нуля здесь недопустима: свеча с нулевым `low`
    выглядит как касание любого уровня выхода, и тейк «сработал» бы там,
    где сделки не было.
    """
    try:
        return CandleSnapshot(
            ticker=str(payload["ticker"]),
            class_code=str(payload["classCode"]),
            timeframe=str(payload["timeFrame"]),
            opened_at=_moment(payload["dateTime"]),
            open=float(payload["open"]),
            high=float(payload["high"]),
            low=float(payload["low"]),
            close=float(payload["close"]),
            turnover=float(payload["volume"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        known = ", ".join(sorted(payload)) or "пусто"
        raise StreamRejected(
            f"Брокер прислал свечу в незнакомом виде: {error}. "
            f"Пришедшие поля: {known}."
        ) from error


@asynccontextmanager
async def candle_stream(
    session: BrokerSession,
    *,
    ticker: str,
    class_code: str,
    timeframe: str = "M1",
    url: str = MARKET_DATA_WS,
) -> AsyncIterator[Any]:
    """Открытый сокет с уже поданной подпиской. Читать — `async for` по нему.

    ⚠️ Это контекст, а не генератор снимков, и разница не косметическая.
    Первая версия была генератором с `async with websockets.connect(...)`
    внутри, и на остановке падало:

        Exception ignored in: <async_generator object candle_snapshots>
        RuntimeError: Timeout should be used inside a task

    Генератор закрывается не там, где открывался: `websockets` при закрытии
    ставит `asyncio.timeout_at`, а тот требует задачу — и не находит её.
    Поэтому сокет открывает и закрывает **потребитель**, в своей задаче:
    отмена проходит обычным путём, закрытие случается там же, где открытие.

    Переподключения здесь нет намеренно: обрыв — событие, о котором должен
    узнать вызывающий (окно показывает «связи нет»). Молчаливое
    переподключение внутри спрятало бы обрыв от того, кому он важнее всего.

    ⚠️ Сеть и рукопожатие переводятся в отказы слоя, а не выходят наружу
    исключениями транспорта. Вызывающему (`app/`) библиотека `websockets`
    по границам не положена (`tests/test_app_boundaries.py`), а различать
    «обрыв — повторить» и «ошибка программы — остановиться» он обязан.
    Поэтому: отказ рукопожатия с HTTP-кодом — по таблице `from_status`
    (401 — токен не принят, повтор не поможет; 5xx — неполадка у брокера,
    повторим), остальное сетевое — `NoConnection`. Всё, что не сеть
    (`RuntimeError` от базы, разбор), проходит насквозь как есть.
    """
    token = await session.access_token()
    request = {
        "subscribeType": SUBSCRIBE,
        "dataType": CANDLES,
        "instruments": [{"classCode": class_code, "ticker": ticker}],
        "timeFrame": timeframe,
    }

    # ⚠️ Заголовок собирается из двух кусков, а не одной f-строкой.
    # Причина не в красоте: сторож секретов (`tests/test_layers.py`) считает
    # похожим на утечку любой файл, где рядом стоят слово-заголовок и длинная
    # строка после него. Отличить образец от живого значения он не может
    # по построению — и правильно делает, цена ошибки тут доступ к счёту.
    #
    # ⚠️ Значение живёт в словаре, который затирается в `finally`, а не
    # в голой локальной переменной, и это тоже не стиль. Оба отказа
    # рукопожатия ниже поднимаются **внутри этого кадра**; кадр уезжает
    # в `__traceback__` поднятого отказа и живёт, пока живёт трассировка.
    # Всякий, кто печатает переменные кадра, — `pytest -l`, отладчик,
    # сборщик отчётов об ошибках, будущий технический лог — видел строку
    # `Bearer <рабочий токен>` целиком. Воспроизведено 04.09.2026 на отказах
    # 401 и 503; 401 — самый вероятный первый отказ (токен не принят),
    # и цикл переподключения в `app/` приводит к нему повторно.
    # Тот же приём и по той же причине — `session.py::_authorized`.
    headers = {"Authorization": "Bearer " + token.secret.reveal()}
    try:
        async with websockets.connect(
            url,
            additional_headers=headers,
            # Логгер рукопожатия — наш, см. `SOCKET_LOGGER`.
            logger=logging.getLogger(SOCKET_LOGGER),
        ) as socket:
            await socket.send(json.dumps(request))
            try:
                yield socket
            except (websockets.exceptions.WebSocketException, OSError) as error:
                raise NoConnection(_transport(error)) from None
    except websockets.exceptions.InvalidStatus as error:
        raise from_status(
            error.response.status_code, where="поток свечей: рукопожатие"
        ) from None
    except (websockets.exceptions.WebSocketException, OSError) as error:
        raise NoConnection(_transport(error)) from None
    finally:
        # Выполняется и на пути исключения, поднятого обработчиком выше:
        # `finally` отрабатывает до того, как новый отказ покинет кадр.
        headers.clear()


def _transport(error: BaseException) -> str:
    """Технический текст сетевого отказа: тип и слова библиотеки, без заголовков."""
    return f"поток свечей: {type(error).__name__}: {error}"


def snapshot_of(raw: str | bytes) -> CandleSnapshot | None:
    """Сообщение сокета → снимок, либо `None` для служебных.

    Отказ брокера поднимается исключением: `NO_DATE`, `NOT_FOUND`,
    `INCORRECT_JSON`, `BAD_REQUEST`, `UNAUTHORIZED`. Повтор той же подписки
    ни одного из них не лечит, поэтому это не «попробуем ещё раз», а отказ.

    Кадр, который не разбирается вовсе, — тоже отказ, но **с повтором**:
    разбор и причина в `_parsed`.
    """
    message = _parsed(raw)

    errors = message.get("errors")
    if errors:
        # Голова списка берётся только если она словарь: `errors` из строк
        # дала бы `AttributeError` на `.get` — то есть ту же беду, что чинит
        # `_parsed`, но на шаг позже и в самом отказе брокера.
        head = errors[0] if isinstance(errors, list) and errors else None
        first: dict[str, Any] = head if isinstance(head, dict) else {}
        text = str(first.get("message", "без объяснения"))
        # Сайт называет поле `code`; живой отказ на неверное имя поля пришёл
        # с `type` (`REQUIRED_FIELD`, проверено 04.09.2026). Читаются оба.
        code = str(first.get("code") or first.get("type") or "без кода")
        raise StreamRejected(
            f"Брокер отказал в подписке: {text} ({code})",
            f"подписка на свечи: {code}",
            retryable=code == NO_DATA,
        )

    if message.get("responseType") != CANDLE:
        return None
    return _snapshot(message)
