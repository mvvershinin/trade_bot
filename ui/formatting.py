"""Превращение чисел и времени в то, что читает человек.

Здесь только оформление. Ни одна функция ничего не считает по торговым правилам:
проценты, прибыль и просадка приходят уже посчитанными, задача модуля — поставить
запятую, знак и единицу измерения.

Два соглашения, которые нельзя менять молча:

* **Время на экране — всегда МСК**, и пояс подписан явно. Машина владельца счёта
  может стоять в другом поясе, а торговое окно 10:05–11:00 — московское.
* **Разделитель дробной части — запятая.** Так пишут в отчётах по-русски,
  и так же уходит выгрузка в файл, чтобы Excel открыл её числами, а не текстом.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Москва — UTC+3 без перехода на летнее время с 2014 года. Фиксированное смещение
# берётся намеренно, вместо `zoneinfo`: база часовых поясов в Windows-сборке
# отсутствует, а `ZoneInfo("Europe/Moscow")` там падает с `ZoneInfoNotFoundError`
# в момент первой отрисовки графика — то есть у заказчика, а не у нас.
MSK = timezone(timedelta(hours=3), "МСК")

EMPTY = "—"

#: Подписи и поля свечи в строке сведений — в порядке и словами терминала
#: брокера: владелец счёта держит два окна рядом и сверяет их глазами.
#:
#: Таблица, а не четыре одинаковых куска разметки: по ней идут все обходы —
#: создание ячеек строки, подстановка чисел с перекраской и сборка такой же
#: подписи для веб-графика. Поле, забытое в одном из мест, дало бы прочерк
#: или чужое число, и молча. Полноту таблицы (все ценовые поля `Candle`
#: показаны) и привязку подписи к своему числу стережёт `tests/test_ui_panel.py`.
#:
#: Лежит здесь, а не рядом со строкой сведений (`ui/chart_panel.py`), потому
#: что вторым её потребителем стала отрисовка (`ui/chart/web_surface.py`),
#: а та импортируется панелью — обратный импорт замкнул бы круг.
PRICE_FIELDS: tuple[tuple[str, str], ...] = (
    ("ОТКР", "open"),
    ("МАКС", "high"),
    ("МИН", "low"),
    ("ЗАКР", "close"),
)


def to_msk(moment: datetime) -> datetime:
    """Момент времени в московском поясе.

    Наивное время (без пояса) считается уже московским: это единственное
    допущение, при котором данные без пояса не уезжают на три часа.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=MSK)
    return moment.astimezone(MSK)


def fmt_time(moment: datetime | None, *, seconds: bool = False) -> str:
    """Часы и минуты по Москве: `10:05`."""
    if moment is None:
        return EMPTY
    return to_msk(moment).strftime("%H:%M:%S" if seconds else "%H:%M")


def fmt_datetime(moment: datetime | None, *, seconds: bool = False) -> str:
    """Дата и время по Москве: `19.06.2026 10:05`."""
    if moment is None:
        return EMPTY
    fmt = "%d.%m.%Y %H:%M:%S" if seconds else "%d.%m.%Y %H:%M"
    return to_msk(moment).strftime(fmt)


def fmt_date(moment: datetime | None) -> str:
    if moment is None:
        return EMPTY
    return to_msk(moment).strftime("%d.%m.%Y")


def fmt_number(value: float | None, digits: int = 2) -> str:
    """Число с запятой в дробной части и пробелом между тысячами."""
    if value is None:
        return EMPTY
    text = f"{value:,.{digits}f}"
    return text.replace(",", " ").replace(".", ",")


def fmt_money(value: float | None, digits: int = 2, *, sign: bool = False) -> str:
    """Рубли: `+9 908,00 ₽`. Знак ставится, когда важно направление результата."""
    if value is None:
        return EMPTY
    body = fmt_number(abs(value), digits)
    if value < 0:
        return f"−{body} ₽"
    return f"+{body} ₽" if sign else f"{body} ₽"


def fmt_percent(value: float | None, digits: int = 2, *, sign: bool = False) -> str:
    """Проценты: `+0,50%`. Значение приходит уже в процентах, а не долей."""
    if value is None:
        return EMPTY
    body = fmt_number(abs(value), digits)
    if value < 0:
        return f"−{body}%"
    return f"+{body}%" if sign else f"{body}%"


def fmt_price(value: float | None, digits: int = 2) -> str:
    if value is None:
        return EMPTY
    return fmt_number(value, digits)


def fmt_level(value: float | None) -> str:
    """Уровень заявки: `211 050`. Целое — без дробной части.

    Отличается от `fmt_price` намеренно. Уровень тейка движок округляет
    банковским округлением **до целого** (решение 0008), и «211 050,00»
    в предупреждении читается как цена с копейками, которых там нет.
    Инструмент с дробной ценой при этом не ломается: дробная часть
    показывается, если она есть.
    """
    if value is None:
        return EMPTY
    return fmt_number(value, 0 if float(value).is_integer() else 2)


def fmt_volume(value: float | None) -> str:
    """Объём в контрактах: целое, если целое."""
    if value is None:
        return EMPTY
    if float(value).is_integer():
        return fmt_number(value, 0)
    return fmt_number(value, 2)


def fmt_share(value: float | None) -> str:
    """Доля 0…1 как проценты: `0.63` → `63%`."""
    if value is None:
        return EMPTY
    return f"{fmt_number(value * 100, 0)}%"


def fmt_count(value: float | None) -> str:
    """Счётчик: сколько штук. Пусто — прочерк, а не ноль.

    Ноль здесь означал бы «сделок было ноль», и это утверждение. Прочерк
    означает «числа не передали» — разные вещи, и путать их нельзя ровно
    там, где человек смотрит на итог.
    """
    if value is None:
        return EMPTY
    return str(int(value))


def fmt_result(value: float | None) -> str:
    """Денежный результат со знаком: `+1 881,00 ₽`, `−640,00 ₽`.

    Знак стоит в самом числе, а не только в цвете: цвет — второй носитель
    смысла, а не единственный. Кто плохо различает красное и зелёное либо
    печатает отчёт чёрно-белым, читает минус буквой.
    """
    return fmt_money(value, sign=True)


@dataclass(frozen=True, slots=True)
class SummaryField:
    """Одно число итога: имя поля, подпись человеку и оформитель.

    `key` — имя атрибута у объекта с числами. Обращение по имени, а не
    руками по одному: итог собирается в двух местах (под журналом сделок
    и в разборе выделенного участка), и подпись, поправленная в одном
    из них, дала бы два разных слова про одно и то же число.
    """

    key: str
    label: str
    render: Callable[[float | None], str]
    #: Поля, которых у прогона может не быть вовсе. Такое поле не показывается
    #: прочерком, а пропускается: «Переворотов: —» занимает место и не говорит
    #: ничего, тогда как отсутствие прибыли прочерком сказать обязано.
    omit_when_empty: bool = False

    def part(self, value: float | None) -> str:
        """Кусок итоговой строки: `Чистая прибыль: +1 881,00 ₽`."""
        return f"{self.label}: {self.render(value)}"


#: Итоговая строка целиком: какие числа, в каком порядке и какими словами.
#:
#: Таблица, а не список полей, переписанный в каждом месте показа. По ней
#: собираются обе итоговые строки — под журналом сделок (`ui/journals.py`)
#: и в разборе выделенного участка графика (`ui/chart_panel.py`), причём
#: вторая берёт подмножество первой. Совпадение слов и порядка становится
#: свойством кода, а не тем, что кто-то помнит; сторож в `tests/test_ui_legend.py`
#: сверяет получившиеся строки, а не одинаковый текст в двух местах.
#:
#: Порядок не случайный и менять его молча нельзя: сначала сколько сделок,
#: потом деньги от валовой к чистой, и чистая — последней из денег. Комиссия
#: стоит между ними отдельной подписью, потому что валовая прибыль в этом
#: проекте результатом не считается (`DOMAIN.md` §5).
SUMMARY_FIELDS: tuple[SummaryField, ...] = (
    SummaryField("trades", "Сделок", fmt_count),
    SummaryField("profitable_share", "Прибыльных", fmt_share),
    SummaryField("gross_profit_rub", "Валовая", fmt_result),
    SummaryField("commission_rub", "Комиссия", fmt_money),
    SummaryField("net_profit_rub", "Чистая прибыль", fmt_result),
    # Пусто — значит убыточных сделок не было и делить не на что.
    # «∞» здесь читалось бы как результат (`backtest.Summary`).
    SummaryField("profit_factor", "Профит-фактор", fmt_number),
    SummaryField("max_drawdown_rub", "Макс. просадка", fmt_money),
    SummaryField("reversals", "Переворотов", fmt_count, omit_when_empty=True),
)


def summary_field(key: str) -> SummaryField:
    """Поле итога по имени.

    Неизвестное имя роняет вызов, а не возвращает пустое место: поле,
    которое переименовали в таблице и забыли здесь, иначе тихо исчезло бы
    с экрана.
    """
    for field in SUMMARY_FIELDS:
        if field.key == key:
            return field
    raise KeyError(f"в таблице итога нет поля {key!r}")


def summary_parts(source: object, fields: Sequence[SummaryField]) -> list[str]:
    """Готовые куски итоговой строки: по одному на поле таблицы.

    Числа берутся у `source` по имени поля. Ничего не считается: и итог
    прогона, и итог участка приходят сюда уже сложенными.
    """
    parts = []
    for field in fields:
        value = getattr(source, field.key)
        if value is None and field.omit_when_empty:
            continue
        parts.append(field.part(value))
    return parts


def csv_number(value: float | None, digits: int = 2) -> str:
    """Число для выгрузки: запятая в дробной части, без пробелов и знака валюты.

    Пробел-разделитель тысяч здесь недопустим: Excel считает такую ячейку текстом,
    и сумма по колонке в выгруженном журнале не считается.
    """
    if value is None:
        return ""
    return f"{value:.{digits}f}".replace(".", ",")
