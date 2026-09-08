"""Точка входа: `python3 -m app.main`.

Здесь собирается программа — кто кого создаёт, кто кого слушает, в каком порядке
всё останавливается. Торговых решений тут нет ни одного: их принимает `engine/`,
данные даёт `market/`, показывает `ui/`.

Запуск только модулем
---------------------
``python3 -m app.main`` из корня рабочей копии. Запуск файлом
(``python3 app/main.py``) кладёт в путь поиска каталог `app/`, а не корень,
и `import market` падает с «No module named 'market'» — ошибка выглядит так,
будто слоя не существует, и уводит искать не туда (ARCHITECTURE.md §2).
Лечить её вставкой `sys.path` нельзя: такой костыль со временем начинает тянуть
конкретные модули в обход портов.

Порядок запуска и остановки — из [решения 0005](../.docs/decisions/0005-concurrency-model.md)
--------------------------------------------------------------------------------
Каждая строка ниже стоит там, где стоит, по замеру, а не по вкусу:

1. ``QT_API=pyside6`` **до** импорта qasync. qasync перебирает привязки
   в порядке PyQt5 → PyQt6 → PySide2 → PySide6 и берёт первую найденную:
   на машине с установленным PyQt5 программа получила бы два разных Qt
   в одном процессе.
2. ``QApplication`` → ``qasync.QEventLoop(app)`` → ``asyncio.set_event_loop``.
   Цикл asyncio **и есть** цикл Qt: ``run_forever()`` вызывает ``app.exec()``.
3. ``app.setQuitOnLastWindowClosed(False)``. Иначе закрытие окна остановит цикл
   на середине завершения, и дозапись журнала, закрытие базы и снятие заявок
   останутся недоделанными.
4. **Один** ``loop.run_until_complete(main())``, всё завершение — внутри этой
   корутины, ``loop.close()`` в ``finally``. ``qasync.run()`` не используется:
   это ``asyncio.run``, а он входит в цикл ещё раз ради ``shutdown_asyncgens``,
   и на программе с QtWebEngine внутри это даёт SIGSEGV или зависание
   (PYSIDE-3451, замер в решении 0005).

Чего эта программа сегодня не делает
------------------------------------
Не подаёт заявок и не торгует. К брокеру подключается только по кнопке
окна или ключу ``--stream``, и только за котировками: они дописываются
в базу, робот их не видит. Она читает свечи из базы и показывает **прогон
робота по истории**: те же движок и стратегия, что пойдут в бой, на тех же
портах. Почему так и что из этого следует —
в докстринге `app/port.py`.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import os
import pathlib
import signal
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # ⚠️ только для подписей: настоящие импорты слоёв идут
    # внутри `_run`, после того как выставлены QT_API и QT_QPA_PLATFORM.
    # Наверху они подняли бы Qt при разборе ключей и при `--help`.
    from PySide6.QtGui import QPixmap
    from PySide6.QtWidgets import QApplication

    from app.live_feed import LiveLink
    from app.logs import LogSetup
    from app.port import HistoryPort
    from app.settings_store import Loaded, SettingsStore
    from market import MarketWorker
    from ui.main_window import MainWindow
    from ui.models import DecisionLevel, Settings

__all__ = ["main"]

def _shot_wish(args: argparse.Namespace) -> "_Shot":
    """Ключи командной строки → просьба о снимке. Одна строка в главной корутине.

    Отдельной функцией, а не сборкой на месте: главная корутина стоит вплотную
    к пределу длины (`tests/test_function_size.py`), и разбор ключей — не то,
    ради чего читают сборку программы.
    """
    return _Shot(pathlib.Path(args.snapshot), args.shot_of, args.shot_tab)


@dataclasses.dataclass(frozen=True, slots=True)
class _Shot:
    """Просьба о снимке: куда сохранить, что снять и какую вкладку показать.

    Одной вещью, а не тремя аргументами подряд: три строки в подписи легко
    переставить местами, и перепутанные «что» и «куда» дали бы файл с именем
    вкладки. Заодно это одно место, где видно, из чего просьба состоит.
    """

    target: pathlib.Path
    what: str = "window"
    tab: str = ""


#: Пауза перед снимком открытого окна, миллисекунды.
#:
#: Не «на всякий случай»: `exec()` начинает свой цикл событий, и первому же
#: таймеру в нём достаётся окно, которое Qt ещё не разложил. Снимок вышел бы
#: с недорисованными полями — то есть картинкой, по которой ничего не проверишь.
_SHOT_PAUSE_MS = 250

log = logging.getLogger(__name__)


def _positive(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(
            "число дней не может быть отрицательным; 0 означает «всю историю»"
        )
    return value


def _at_least_one(text: str) -> int:
    """Сколько строк показать. Ноль здесь означал бы «покажи ничего».

    Отдельный разбор, а не `_positive`: у числа дней ноль означает «всю
    историю», а у длины выдачи такого смысла нет — `LIMIT 0` вернул бы
    пустой список, а `LIMIT -1` в SQLite означает «без предела»
    (`market/storage.py::_limit_probe`). Два молчаливых ответа на одну
    опечатку лучше заменить отказом с фразой.
    """
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(
            f"показать {value} прогонов нельзя: число должно быть от единицы"
        )
    return value


#: Как человек пишет момент времени в командной строке. Русский порядок —
#: первым: программа и её сообщения русские, и день.месяц.год здесь ожиданнее.
_MOMENT_FORMATS = ("%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d %H:%M", "%Y-%m-%d")


def _moment(text: str):
    """Момент времени МСК из строки. Дата без времени — конец этого дня.

    Часовой пояс проставляется явно: наивное время уехало бы на три часа
    там, где машина владельца счёта стоит не в Москве, и уехало бы молча.
    """
    from datetime import datetime, time, timedelta, timezone  # noqa: PLC0415 — разбор
    # ключа идёт до Qt: модуль обязан импортироваться, ничего тяжёлого не подняв

    msk = timezone(timedelta(hours=3), "MSK")
    for fmt in _MOMENT_FORMATS:
        try:
            value = datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
        if "%H" not in fmt:  # дата без времени — весь день целиком
            value = datetime.combine(value.date(), time(23, 59, 59))
        return value.replace(tzinfo=msk)
    raise argparse.ArgumentTypeError(
        f"момент «{text}» непонятен. Пишите 28.08.2026 или 28.08.2026 11:05"
    )


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python3 -m app.main",
        description="«Терминал» — прогон робота по истории из базы свечей.",
    )
    _add_shot_arguments(parser)
    parser.add_argument(
        "--db", dest="database", metavar="FILE",
        help="файл базы свечей; по умолчанию — тот, что назначен market/paths.py",
    )
    parser.add_argument(
        "--symbol", dest="symbol", metavar="CODE",
        help="инструмент, по умолчанию MXU6",
    )
    parser.add_argument(
        "--days", dest="days", type=_positive, default=None, metavar="N",
        help="сколько последних календарных дней истории показать и прогнать, "
             "считая день последней свечи: 1 — только он; 0 — всю историю. "
             "Без ключа берётся глубина из настроек программы; ключ перебивает "
             "её на этот запуск и в файл настроек не пишется",
    )
    # Взаимоисключающие намеренно. Пара «--light --dark» в одной строке
    # не имеет верного прочтения: победа последнего зависит от порядка,
    # которого человек не держит в голове, а молчаливая победа одного из них
    # даёт окно, которого не просили. Пусть argparse откажет вслух.
    palette = parser.add_mutually_exclusive_group()
    palette.add_argument(
        "--dark", dest="dark", action="store_true",
        help="тёмная палитра. Она и так стоит по умолчанию; ключ оставлен "
             "затем, чтобы старые записи запуска не сломались",
    )
    palette.add_argument(
        "--light", dest="light", action="store_true",
        help="светлая палитра вместо тёмной. По умолчанию окно тёмное — "
             "просьба владельца счёта 06.09.2026",
    )
    parser.add_argument(
        "--until", dest="until", type=_moment, metavar="WHEN",
        help="по какой момент показывать историю: 28.08.2026 или 28.08.2026 11:05 "
             "(время московское). По умолчанию — по последнюю свечу в базе",
    )
    parser.add_argument(
        "--stream", dest="stream", action="store_true",
        help="подписаться на поток котировок брокера и дописывать свечи в базу. "
             "Робот при этом НЕ торгует и решений не принимает: показывается "
             "только приход данных. Нужен токен в userdata/, прав на чтение "
             "достаточно. ⚠️ Это НЕ боевой режим: заявок программа не подаёт",
    )
    parser.add_argument(
        "--stream-class", dest="stream_class", metavar="CODE", default="SPBFUT",
        help="код класса инструмента для подписки; по умолчанию SPBFUT "
             "(срочный рынок МосБиржи)",
    )
    _add_journal_arguments(parser)
    _add_fetch_arguments(parser)
    args = parser.parse_args(argv)
    if (args.shot_of != "window" or args.shot_tab) and not args.snapshot:
        # Отказ вслух, а не молчаливое «ключ ничего не сделал». Ключ без
        # `--shot` — это просьба снять окно, которую некуда сохранить.
        parser.error(
            "ключи --shot-of и --shot-tab работают только вместе с --shot: "
            "снимок некуда сохранить"
        )
    return args


def _add_shot_arguments(parser: argparse.ArgumentParser) -> None:
    """Ключи снимка экрана. Отдельной функцией — как журнал и загрузка.

    ⚠️ `--shot-of` заведён по цене дефекта. Снимки окна выбора алгоритма
    делались `tools/demo.py`, где каталог подаётся диалогу прямо в конструктор:
    настоящая дорога — порт, сигнал, окно — снимком не проверялась ни разу,
    и пустой список дожил до владельца счёта 09.09.2026. Правило 12
    `CLAUDE.md`: проверка идёт тем путём, которым пойдёт человек.
    """
    parser.add_argument(
        "--shot", dest="snapshot", metavar="FILE",
        help="сохранить вид окна в файл и выйти (работает на машине без экрана)",
    )
    parser.add_argument(
        "--shot-of", dest="shot_of", default="window", metavar="WHAT",
        choices=("window", "settings", "algorithm"),
        help="что именно снимать ключом --shot: window — главное окно "
             "(по умолчанию), settings — окно настроек, algorithm — окно "
             "выбора торгового алгоритма. Окна при этом открываются тем же "
             "путём, каким их открывает человек: щелчком по кнопке настоящей "
             "программы, а не постройкой диалога вручную",
    )
    parser.add_argument(
        "--shot-tab", dest="shot_tab", default="", metavar="NAME",
        help="какую вкладку окна настроек показать на снимке, например "
             "«Сигнал». Без ключа снимается та, что открылась",
    )


def _add_journal_arguments(parser: argparse.ArgumentParser) -> None:
    """Ключ `--runs`: что я запускал. Окно при этом не открывается.

    Ответ на вопрос владельца счёта «почему такой убыток» начинается
    с вопроса «а что именно вы гнали». До этого ключа ответить было нечем:
    число из отчёта жило отдельно от условий, при которых получено.
    """
    from app.runs import RUNS_SHOWN  # noqa: PLC0415 — тот же приём, что
    # у ключей загрузки ниже: слой поднимается при разборе ключей, а не при
    # импорте модуля, и Qt при этом не трогается. Число нужно прямо здесь —
    # оно попадает в текст `--help`, и второе такое же число, написанное
    # рядом руками, разошлось бы с настоящим молча

    parser.add_argument(
        "--runs", dest="runs", type=_at_least_one, nargs="?", const=RUNS_SHOWN,
        default=None, metavar="N",
        help="показать последние прогоны робота по истории и выйти: с какими "
             "настройками сделан каждый, на каком отрезке и что вышло. "
             f"Без числа — последние {RUNS_SHOWN}. Ничего не грузит и не меняет",
    )


def _add_fetch_arguments(parser: argparse.ArgumentParser) -> None:
    """Ключи загрузки истории с биржи. Окно при этом не открывается.

    Один обязательный ключ и три уточнения с умолчаниями: владелец счёта
    не программист, и `--fetch MXZ6` обязан работать без остальных трёх.
    Разбор и цифры — в `app/fetch.py` и `market/history.py`.
    """
    from market import DEFAULT_DEPTH_DAYS, MARKETS  # noqa: PLC0415 — разбор ключей
    # идёт до Qt; `market/` его не тянет, но правило импорта слоёв здесь одно

    group = parser.add_argument_group(
        "загрузка истории с МосБиржи",
        "Открытые данные биржи, бесплатно и без токена. Окно не открывается: "
        "программа грузит историю в базу, печатает итог и выходит. Нужно после "
        "смены инструмента в настройках: своих свечей у нового инструмента "
        "в базе нет, и график будет пуст, пока история не загружена.",
    )
    group.add_argument(
        "--fetch", dest="fetch", metavar="CODE",
        help="загрузить историю инструмента CODE (например MXZ6) и выйти",
    )
    group.add_argument(
        "--inspect", dest="inspect", metavar="CODE",
        help="показать, что уже лежит в базе по инструменту CODE, и выйти: "
             "сколько дней, какие плотные, какие огрызки, с какого дня рядом "
             "можно торговать. Ничего не грузит и не меняет",
    )
    group.add_argument(
        "--fetch-days", dest="fetch_days", type=_positive,
        default=DEFAULT_DEPTH_DAYS, metavar="N",
        help=f"сколько последних календарных дней грузить, считая сегодняшний; "
             f"по умолчанию {DEFAULT_DEPTH_DAYS}; 0 — всё, что отдаёт биржа",
    )
    group.add_argument(
        "--fetch-since", dest="fetch_since", type=_moment, metavar="WHEN",
        help="с какой даты грузить: 01.08.2026. Перебивает --fetch-days. "
             "Дату начала ряда стоит выбрать один раз и больше не менять: "
             "средняя, посчитанная с другой границы, даёт другие сделки",
    )
    group.add_argument(
        "--fetch-market", dest="fetch_market", default="futures",
        choices=sorted(MARKETS),
        help="рынок: futures — срочный (фьючерсы), shares — акции; "
             "по умолчанию futures",
    )


def wants_dark(args: argparse.Namespace) -> bool:
    """Тёмная ли палитра при этих ключах. Отдельной функцией — чтобы стеречь.

    Просьба владельца счёта 06.09.2026: «надо тёмную тему по умолчанию
    сделать». До этого окно брало тему системы, и на собранной программе
    под Windows она **вышла разной на двух запусках подряд** (замер сборщика
    06.09.2026): системная тема читается по яркости фона палитры, а палитра
    на старте успевает прийти не всегда. Умолчание убирает и разнобой,
    и вопрос «почему сегодня по-другому».

    ⚠️ Функция заведена не ради красоты, а потому что решение, оставленное
    строкой `if not args.light` внутри `main`, **стеречь нечем**: проверка
    `_arguments([]).light is False` зеленеет и тогда, когда умолчание
    вернулось к системной теме, — она читает значение ключа, а не решение.
    Проверено мутацией 06.09.2026: первая редакция сторожа её не поймала.
    """
    return not args.light


def _use_dark_palette(application) -> None:
    """Тёмная палитра для машины со светлой системной темой.

    Своей темы у программы нет: окно берёт системную — `ui/theme.py` палитру
    только читает. Здесь она подменяется, чтобы тёмный вид можно было
    посмотреть там, где система светлая.

    ⚠️ Зовётся **до** сборки окна: тема читается при создании виджетов,
    и подмена после `MainWindow(...)` дошла бы не до всех.
    """
    from PySide6.QtGui import QColor, QPalette  # noqa: PLC0415 — Qt поднимается
    # только внутри `main()`, после QT_QPA_PLATFORM; наверху он поднялся бы
    # при разборе ключей и при `--help`

    palette = QPalette()
    ink, paper, panel = QColor("#e8e6e1"), QColor("#141317"), QColor("#1b1a20")
    for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base):
        palette.setColor(role, paper)
    palette.setColor(QPalette.ColorRole.AlternateBase, panel)
    palette.setColor(QPalette.ColorRole.Button, panel)
    for role in (
        QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText, QPalette.ColorRole.ToolTipText,
    ):
        palette.setColor(role, ink)
    palette.setColor(QPalette.ColorRole.ToolTipBase, panel)
    palette.setColor(QPalette.ColorRole.Mid, QColor("#8a8590"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#d9a441"))
    palette.setColor(QPalette.ColorRole.HighlightedText, paper)
    application.setStyle("Fusion")
    application.setPalette(palette)


def _speak_utf8() -> None:
    """Заставить вывод принимать русский текст независимо от локали машины.

    ⚠️ **Пойманное живьём падение, замер 06.09.2026.** Собранная программа
    без переменной `LANG` падала на своей же первой строке:

        UnicodeEncodeError: 'ascii' codec can't encode characters
        in position 0-3: ordinal not in range(128)

    Python берёт кодировку вывода у локали, а весь текст этой программы
    русский. С `LANG` всё работало, без него нет — и это не экзотика:
    запуск из ярлыка рабочего стола, из планировщика заданий и внутри
    контейнера сплошь и рядом идёт с пустым окружением.

    `errors="replace"` намеренно: потерять букву в сообщении лучше,
    чем уронить программу на попытке его напечатать. Молчаливый отказ
    вывода хуже кривой буквы ровно один раз — когда сообщение было важным,
    а его не стало вовсе.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # поток подменён — например, тестом
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # поток без перенастройки — не беда
            pass


def main(argv: list[str] | None = None) -> int:
    """Собрать и запустить программу. Возвращает код возврата процесса."""
    _speak_utf8()
    args = _arguments(argv)
    # ⚠️ Лог заводится **до всех веток**, включая загрузку истории и `--runs`:
    # они тоже пишут в логгеры. Без этой строки любое предупреждение любого
    # слоя летело владельцу счёта в консоль сырым текстом (`D-021`).
    # Здесь же встаёт чистка секретов — тем же вызовом, на все ветки сразу.
    from app.logs import setup_logging  # noqa: PLC0415 — слои после разбора ключей

    setup_logging()

    if args.fetch or args.inspect or args.runs is not None:
        # ⚠️ Ветка стоит ДО первого касания Qt намеренно: загрузка истории
        # обязана работать на машине без экрана, а поднятый QApplication
        # без окна оставляет за собой процесс, который нечем закрыть.
        return _without_window(args)

    if args.snapshot:
        # Именно присвоение, а не `setdefault`: флаг «сделать снимок» сам
        # по себе означает «без экрана», а в окружении вполне может остаться
        # `wayland` от другого запуска — Qt поднимет нерабочий плагин и упадёт
        # ровно там, где этот режим должен быть самым надёжным.
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    # ⚠️ Замок «одна копия» стоит ЗДЕСЬ, **до первого касания Qt**, и это
    # `B-028`. Раньше он стоял после `QApplication` — ради того, чтобы отказ
    # можно было показать окном. Окном он показывается и отсюда (`_say_already_
    # running` строит окно сам), а вот обратное не работало: любая беда Qt —
    # не поднявшийся плагин платформы, отсутствующий экран, зависшее
    # подключение — съедала отказ целиком, и вторая копия молчала. Владелец
    # счёта поймал это живьём 06.09.2026: окна нет, вывода нет, Ctrl+C
    # не помогает. Молчание в этой программе — самостоятельный дефект.
    from app.single_instance import OneCopy, Purpose  # noqa: PLC0415 — слои
    from market import default_db_path  # noqa: PLC0415 — Qt не трогает

    database = pathlib.Path(args.database) if args.database else default_db_path()
    single = OneCopy(database.parent)
    verdict = single.take(Purpose.WINDOW)
    if not verdict.taken:
        _say_already_running(verdict.trouble)
        return 1

    # ⚠️ До первого импорта qasync. См. шапку модуля, пункт 1.
    os.environ["QT_API"] = "pyside6"

    import qasync  # noqa: PLC0415 — после QT_API
    from PySide6.QtWidgets import QApplication  # noqa: PLC0415 — после QT_QPA_PLATFORM

    # Слой окна — тоже после QT_API: `ui/` тянет PySide6 за собой.
    from ui.version import version  # noqa: PLC0415

    application = QApplication(sys.argv[:1])
    application.setApplicationName("Терминал")
    application.setApplicationDisplayName("Терминал")
    application.setApplicationVersion(version())
    # См. шапку модуля, пункт 3: закрытие окна не останавливает цикл.
    application.setQuitOnLastWindowClosed(False)

    if wants_dark(args):
        _use_dark_palette(application)

    loop = qasync.QEventLoop(application)
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(
            _run(application, args, database, note=verdict.trouble)
        )
    finally:
        # Единственный выход из цикла. Второго входа не будет — см. пункт 4.
        loop.close()
        single.release()


def _without_window(args: argparse.Namespace) -> int:
    """Ветки, которые окна не открывают: `--runs`, `--inspect`, `--fetch`.

    ⚠️ Замок «одна копия» берёт **только загрузка**, и правило здесь одно:
    его берёт тот, кто пишет в папку данных (решение 0039).

    * `--runs` и `--inspect` только читают и печатают. Спросить «а что
      я вчера гонял», не закрывая окно, — обычное дело, и запрещать это
      значило бы наказать человека за предохранитель;
    * `--fetch` кладёт в базу тысячи свечей разом. Живой поток котировок
      будет её ждать, а таймаут `sqlite3` по умолчанию 5 секунд — то есть
      поток получит «database is locked» и потеряет минуты.

    Слои импортируются внутри, так же, как во всём модуле: они поднимаются
    после разбора ключей, а не при `--help`, и Qt при этом не трогается.
    """
    from app.fetch import run_from_arguments, show_inventory  # noqa: PLC0415
    from app.runs import show_runs  # noqa: PLC0415
    from app.single_instance import OneCopy, Purpose  # noqa: PLC0415
    from market import default_db_path  # noqa: PLC0415

    database = pathlib.Path(args.database) if args.database else default_db_path()
    if args.runs is not None:
        return show_runs(database, limit=args.runs, out=sys.stdout)
    if args.inspect:
        return show_inventory(database, args.inspect, out=sys.stdout)

    single = OneCopy(database.parent)
    verdict = single.take(Purpose.COMMAND)
    if verdict.trouble:  # занято — или папка замок держать не умеет
        print(verdict.trouble, file=sys.stderr)
    if not verdict.taken:
        return 1
    try:
        return run_from_arguments(args, database)
    finally:
        single.release()


def _say_already_running(text: str) -> None:
    """Сказать человеку, что программа уже запущена. Не кодом и не молчанием.

    Двумя путями сразу, потому что запускают двумя путями. Из консоли и из
    `Запустить.bat` человек видит stderr; двойным щелчком по значку — не
    видит ничего, и там нужно окно.

    ⚠️ **Порядок обязателен, и это `B-028`.** Печать идёт первой и с
    немедленным сбросом буфера, **до** любого касания Qt. Пока замок стоял
    после `QApplication`, беда Qt съедала отказ целиком: владелец счёта
    06.09.2026 увидел ни окна, ни строки, ни реакции на Ctrl+C, и решил,
    что программа сломана, — при том, что предохранитель сработал верно.

    ⚠️ Окно не показывается в режиме без экрана: модальное окно `offscreen`
    ждало бы нажатия вечно, то есть повесило бы прогон с `--shot`. И любой
    отказ Qt здесь глушится: отказ второй копии обязан остаться фразой,
    а не превратиться в трассировку поверх фразы.
    """
    print(text, file=sys.stderr, flush=True)
    if os.environ.get("QT_QPA_PLATFORM") in {"offscreen", "minimal"}:
        return
    try:
        _show_already_running(text)
    except Exception:  # фраза уже напечатана; добить её трассировкой нечем
        log.exception("окно с отказом второй копии не показалось")


def _show_already_running(text: str) -> None:
    """То же самое окном — для запуска значком, где консоли нет.

    ⚠️ **Ctrl+C обязан работать, пока окно открыто.** Модальный `exec()` —
    это цикл событий Qt на языке C, и питон внутри него байткод не исполняет:
    обработчик сигнала не срабатывает, и нажатие в консоли не даёт ничего.
    Владелец счёта на это и напоролся. Отсюда таймер вхолостую: он возвращает
    управление питону двадцать раз в минуту, и Ctrl+C закрывает окно.

    `QApplication` создаётся здесь, если его ещё нет: замок берётся **до**
    Qt (см. `main`), и на этом пути приложения может не существовать вовсе.
    """
    from PySide6.QtCore import QTimer  # noqa: PLC0415 — после QT_QPA_PLATFORM
    from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: PLC0415

    application = QApplication.instance() or QApplication(sys.argv[:1])
    box = QMessageBox()
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle("Терминал уже запущен")
    box.setText(text)
    heartbeat = QTimer(box)
    heartbeat.timeout.connect(lambda: None)
    heartbeat.start(200)
    try:
        signal.signal(signal.SIGINT, lambda *_: box.close())
    except (ValueError, OSError):  # не главный поток или система не даёт
        pass
    # ⚠️ Ссылка на приложение живёт до конца показа намеренно: без неё
    # сборщик мусора вправе убрать только что созданный `QApplication`
    # прямо под открытым окном.
    assert application is not None
    box.exec()
    heartbeat.stop()


async def _run(
    application,
    args: argparse.Namespace,
    database: pathlib.Path,
    *,
    note: str = "",
) -> int:
    """Главная корутина: сборка, работа, завершение. Всё внутри неё.

    ⚠️ Порт получает **две** проводки наружу, и обе делаются здесь, а не
    внутри него: чем спрашивать у биржи карточку инструмента
    (`HistoryPort.attach_exchange`) и чем включать поток котировок
    (`_live_feed`). В порт они не встроены намеренно — порт, ходящий в сеть
    сам, потащил бы туда каждую проверку, которая его собирает: девять секунд
    ожидания на проверку и красный прогон на машине без интернета.

    Цена отсутствия первой проводки названа замером: в движок уходит
    умолчание 1 ₽ за пункт, верное для одного контракта из шести (`B-021`).
    По `RIU6` итог занижен в 1,74 раза, по `BRV6` — в 869 раз, и ошибка
    тихая: список сделок тот же самый, деньги другие.
    """
    # Все пять — после того, как `main()` выставил QT_API и QT_QPA_PLATFORM.
    from app.port import HistoryPort  # noqa: PLC0415
    from broker.redaction import scrub  # noqa: PLC0415
    from market import MarketWorker  # noqa: PLC0415
    from ui.main_window import MainWindow  # noqa: PLC0415
    from ui.models import DecisionLevel  # noqa: PLC0415

    if not _userdata_ready(database.parent):
        return 1
    store, loaded, values, depth = _stored_settings(database.parent, args)

    # ⚠️ Чистка секретов ставится ЗДЕСЬ, при сборке, а не внутри слоя данных.
    # `market/` не имеет права импортировать `broker/` (ARCHITECTURE.md §2),
    # и своими силами он знает только формы токена — `Bearer …`, поля `*_token`,
    # голый JWT. Реестр живых значений принадлежит `broker/`, и без этой строки
    # работает только второй рубеж. Путь, ради которого это делается, реальный:
    # причина решения может прийти из отказа исполнителя, а `engine/` кладёт
    # в строку журнала текст исключения целиком.
    worker = MarketWorker(database, sanitize=scrub)
    #: Порт может не появиться вовсе — если база не открылась. `finally` ниже
    #: обязан это различать, иначе на негодной базе завершение падает само.
    port: HistoryPort | None = None
    live: LiveLink | None = None
    try:
        # ⚠️ Всё, что ниже, стоит ВНУТРИ `try` намеренно. Раньше открытие базы
        # и сборка окна стояли до него: база, оказавшаяся не базой (чужой файл,
        # обрывок загрузки, база от более новой сборки), давала владельцу счёта
        # трассировку вместо фразы, а поток данных оставался незакрытым
        # с открытым соединением SQLite.
        #
        # ⚠️ Это **проверка**, а не условие работы (`B-029`): файл на месте —
        # отказать вслух, если он не база; файла нет — не создавать зря, окно
        # скажет «базы нет», а не «в базе нет свечей». Базу заведёт сам поток
        # данных, когда она понадобится (`MarketWorker._ready_store`).
        if database.exists():
            try:
                await worker.open()
            except Exception as error:  # noqa: BLE001 — фраза важнее типа
                print(_unreadable(database, error), file=sys.stderr)
                return 1

        # ⚠️ Та же чистка, что у слоя данных, и **тем же именем** во всех трёх
        # местах. У текста журнала четыре выхода — ячейка таблицы, подсказка,
        # строка состояния окна и выгрузка в файл, — и в базу он не заходит
        # ни разу: в окно его везут сигналы порта. Рубеж на границе ставит
        # порт (`HistoryPort._send`), второй рубеж — выгрузка. Три разных
        # чистки на три места разошлись бы молча.
        port = HistoryPort(worker, values=values, days=depth,
                           until=args.until, sanitize=scrub)
        port.attach_exchange(worker.point_value)

        window = MainWindow(port=port, settings=values, sanitize=scrub)
        window.resize(1440, 900)

        closed = asyncio.Event()
        application.lastWindowClosed.connect(closed.set)
        _on_interrupt(closed)

        port.announce(database)
        await _restore_halt(port, database)
        _wire_settings_and_log(port, store, loaded, values, level=DecisionLevel.WARNING)
        if note:  # папка данных не держит замок «одна копия» (решение 0039)
            port.note("Один экземпляр программы", note, DecisionLevel.WARNING)
        port.refresh("запуск программы")
        window.show()
        live = _live_feed(port, worker, database.parent, values, args)
        if args.snapshot:
            return await _snapshot(application, window, port, _shot_wish(args))
        await closed.wait()
        return 0
    finally:
        await _close_live(live)
        if port is not None:
            await port.aclose()
        await worker.close()


async def _restore_halt(port: HistoryPort, database: pathlib.Path) -> None:
    """Поднять остановку робота, оставшуюся от прошлого запуска.

    ⚠️ **До первого прогона и до показа окна.** Робот, вставший вчера
    по дневному лимиту или на неизвестном исходе команды брокеру, обязан
    остаться остановленным. До 07.09.2026 перезапуск программы снимал запрет
    молча (`D-043`) — а перезапуск это первое, что делает человек, которому
    не дали кнопки: предохранитель отменялся тем самым движением, которым
    его пытались обойти.

    ⚠️ **Базы нет — вопроса нет.** Условие то же, что у открытия потока
    данных выше, и по той же причине: остановки в несуществующей базе быть
    не может, а сам вопрос **завёл бы файл** на чистой машине (`B-029`,
    `MarketWorker._ready_store`).

    Отдельной функцией, а не двумя строками в `_run`: главная корутина стоит
    вплотную к пределу числа операторов (`PLR0915`), и довод про порядок
    вызова — не то, ради чего читают сборку программы.
    """
    if not database.exists():
        return
    await port.restore_halt()


def _userdata_ready(userdata: pathlib.Path) -> bool:
    """Папка данных существует и в неё можно писать — иначе отказ фразой.

    Отказ видимый и окончательный: без места под базу и журналы программу
    запускать нельзя. Проба на запись обязательна — путь, уехавший на
    файловую систему только для чтения, потерял бы свечи и журнал сделок
    молча (`market/paths.py::ensure_userdata_dir`).
    """
    from market import ensure_userdata_dir  # noqa: PLC0415 — слои после QT_API

    try:
        ensure_userdata_dir(userdata)
    except OSError as error:
        print(str(error), file=sys.stderr)
        return False
    return True


def _stored_settings(
    userdata: pathlib.Path, args: argparse.Namespace
) -> tuple[SettingsStore, Loaded, Settings, int]:
    """Настройки с диска, поправки из командной строки и глубина показа.

    ⚠️ Настройки **читаются из файла**, а не создаются умолчаниями. До
    05.09.2026 здесь стояло `Settings()`: каждый запуск начинался с чистого
    листа, и подобранный владельцем счёта набор жил ровно до закрытия окна.

    Ключи командной строки перебивают файл, а не наоборот, и в файл не
    пишутся (`D-026`): человек, назвавший инструмент или глубину ключом,
    назвал их для этого запуска — переписывать ему настройки за это нельзя.
    """
    from app.settings_store import SettingsStore  # noqa: PLC0415 — слои
    # поднимаются после того, как `main()` выставил QT_API; здесь тот же приём,
    # что во всём модуле

    store = SettingsStore(userdata)
    loaded = store.load()
    values = loaded.values
    if args.symbol:
        values = values.replace(instrument=args.symbol)
    depth = args.days if args.days is not None else values.depth_days
    return store, loaded, values, depth


def _say_where_the_log_goes(port: HistoryPort, setup: LogSetup, *, level: DecisionLevel) -> None:
    """Сказать в журнале, куда пишется технический лог и что с ним не так.

    Молчание здесь дорого стоит в один день — в тот, когда лог понадобится:
    человек пойдёт искать его в папке, которую называл, а он всё это время
    писался в другую, потому что названная не открылась.
    """
    if setup.trouble:
        port.note("Технический журнал", setup.trouble, level)
    if setup.path is not None:
        port.note(
            "Технический журнал",
            f"Подробности работы пишутся в {setup.path}. Это не журнал сделок "
            "и не журнал решений — те лежат в базе и выгружаются кнопкой "
            "под таблицами.",
        )


def _wire_settings_and_log(
    port: HistoryPort,
    store: SettingsStore,
    loaded: Loaded,
    values: Settings,
    *,
    level: DecisionLevel,
) -> None:
    """Всё, что связывает файл настроек и технический лог с портом.

    Порядок не произвольный, и каждый шаг стоит там, где стоит:

    1. **лог переезжает** в папку, названную в настройках, — до первой строки
       журнала. Вызов идемпотентный, обработчик на корне остаётся один
       (`app/logs.py`); позже начало сеанса писалось бы не туда, куда просил
       человек;
    2. **говорится, куда он уехал** — вместе с тем, что при этом не удалось;
    3. **говорится, что вышло при чтении** настроек — до первой записи,
       иначе она затрёт файл, о котором ещё не сказано ни слова;
    4. **включается запись** применённых настроек.
    """
    from app.logs import setup_logging  # noqa: PLC0415 — слои после разбора ключей

    _say_where_the_log_goes(port, setup_logging(values.log_directory), level=level)
    _say_what_was_read(port, loaded, store, level=level)
    _keep_settings(port, store, level=level)


def _keep_settings(
    port: HistoryPort, store: SettingsStore, *, level: DecisionLevel
) -> None:
    """Записывать настройки в файл, как только порт их принял.

    Слушается `settings_applied` — эхо порта, а не сигнал окна: в файл обязано
    попасть то, что **принято**, а не то, что нажато. Отвергнутые настройки
    порт эхом не отдаёт.

    Здесь же порту сообщается новая глубина показа: она живёт в настройках
    программы, а не в настройках движка (`D-026`), и без этой строки менялась
    бы только до перезапуска. Прогон следом порт заводит сам.

    ⚠️ Отказ записи не бросается, а становится строкой журнала. Слот Qt,
    из которого летит исключение, оставляет владельца счёта с трассировкой
    в консоли и молчащим окном.
    """
    def remember(values: Settings) -> None:
        port.set_depth(values.depth_days)
        failure = store.save(values)
        if failure:
            port.note("Настройки не сохранены", failure, level)

    port.settings_applied.connect(remember)


def _say_what_was_read(
    port: HistoryPort, loaded: Loaded, store: SettingsStore, *, level: DecisionLevel
) -> None:
    """Рассказать в журнале, что вышло при чтении файла настроек.

    Молчать нельзя ни в одном из случаев. Пропавшее поле, чужой ключ, файл
    от более новой сборки, отложенный в сторону обломок — всё это про деньги:
    настройка, вернувшаяся к умолчанию, меняет сделки, а владелец счёта
    об этом не узнает никак иначе.
    """
    for trouble in loaded.troubles:
        port.note("Настройки прочитаны не полностью", trouble, level)
    for note in loaded.notes:
        port.note("Настройки прочитаны", note)
    if not loaded.troubles and not loaded.notes:
        port.note(
            "Настройки прочитаны",
            f"Файл {store.path} прочитан целиком, все значения взяты из него.",
        )


async def _close_live(live: LiveLink | None) -> None:
    """Снять живой поток и подключение к брокеру. Первыми в завершении.

    ⚠️ Порядок не произвольный: поток котировок пишет в базу, значит снимается
    раньше самой базы — иначе запись пойдёт в закрывающееся хранилище.
    Остаток завершения (прогон, потом база) стоит в `_run` на виду, потому что
    его порядок стережёт `tests/test_app_boundaries.py`, и стережёт по делу
    (решение 0005, п. 4).

    `None` — сборка до звена не дошла (база не открылась). Звено без
    подключения — обычное состояние, если кнопку не нажимали: закрывается
    ровно то, что было построено (`LiveLink.aclose`).
    """
    if live is None:
        return
    await live.aclose()


def _live_feed(
    port: HistoryPort,
    worker: MarketWorker,
    userdata: pathlib.Path,
    values: Settings,
    args: argparse.Namespace,
) -> LiveLink:
    """Связь с брокером: по кнопке окна или ключу `--stream`. При сборке — проводка.

    ⚠️ Узкий срез задачи Э1-5. Поток пишет минутки в базу и обновляет
    график; **движок при этом не задействован вовсе** — ни решений, ни заявок.
    Чего срезу не хватает до настоящей работы, разобрано в минипланe Э1-5
    (§7, «Доведение среза»).

    Здесь не строится ни сессия, ни поток: они появляются при первом
    включении (`LiveLink`), и до него токен не трогается. Ключ `--stream`
    о брокере не знает ничего — он нажимает кнопку за человека тем же
    `port.stream(True)`, что и окно, поэтому отказ по ключу выглядит так же,
    как отказ по кнопке: строкой в журнале, а не трассировкой.

    ⚠️ Чистка секретов ставится **здесь, при сборке**, безусловно, а не
    в момент включения. Токена она не трогает и стоит дёшево, зато гарантия
    «до первого сетевого вызова» не зависит от того, каким путём этот вызов
    случится: сегодня единственный путь — поток, завтра — догрузка с биржи
    (миниплан §4.6). Покрываются и чужие логгеры, включая `websockets`:
    библиотека печатает заголовки рукопожатия на DEBUG, а среди них
    `Authorization: Bearer …` (проверено по исходнику 17.0.1). Уровень
    по умолчанию INFO, и без этой строки дыра закрыта случайностью настройки.
    """
    from app.live_feed import LiveLink  # noqa: PLC0415
    from broker import FOREIGN_LOGGERS, LOGGER_NAME, install_redaction  # noqa: PLC0415

    install_redaction((LOGGER_NAME, *FOREIGN_LOGGERS))
    link = LiveLink(
        port,
        worker,
        userdata=userdata,
        ticker=values.instrument,
        class_code=args.stream_class,
    )
    # ⚠️ Обе проводки разом, одной командой. Раздельные означали бы случай
    # «поток включается, но за сменой инструмента в окне не идёт» — это `B-020`:
    # тикер задавался при сборке, `apply_settings` его не трогал, и после смены
    # инструмента поток продолжал писать старый, молча.
    port.attach_stream(link.switch, retarget=link.retarget)
    if args.stream:
        port.stream(True)
    return link


def _unreadable(database: pathlib.Path, error: Exception) -> str:
    """Почему база не открылась — фразой, а не трассировкой.

    Тот же разговор, что ведёт порт при отсутствии базы: назван файл, названа
    причина, сказано, что делать. Код и тип исключения сюда не выносятся —
    `file is not a database` владельцу счёта не говорит ничего.
    """
    import sqlite3  # noqa: PLC0415 — только ради разбора причины

    if isinstance(error, sqlite3.DatabaseError):
        why = (
            "это не база свечей «Терминала»: файл существует, но прочитать его "
            "как базу не удалось. Возможно, выбран не тот файл или база "
            "повреждена"
        )
    else:
        why = str(error)
    return (
        f"Не удалось открыть базу свечей {database}: {why}.\n"
        "Программа не запущена. Укажите другой файл ключом --db или уберите "
        "этот: без базы окно откроется и скажет, что свечей нет."
    )


async def _snapshot(
    application: QApplication, window: MainWindow, port: HistoryPort, shot: _Shot
) -> int:
    """Сохранить вид окна в файл. Работает на машине без экрана.

    ⚠️ `_Shot.what` снимает не только главное окно, и заведён он по цене дефекта.
    Пустой список алгоритмов и указание «выберите один из списка», выбирать
    в котором было не из чего, дожили до владельца счёта 09.09.2026 потому,
    что проверялись снимками из `tools/demo.py`, **где каталог подаётся
    диалогу прямо в конструктор**. Настоящая дорога — порт, сигнал, окно —
    снимком не проверялась ни разу. Правило 12 `CLAUDE.md`: проверка идёт
    тем путём, которым пойдёт человек.
    """
    await port.wait()
    # Прогон рассылает сигналы; окну надо дать их разобрать и разложить
    # виджеты, иначе снимок получится с недорисованным графиком.
    for _ in range(3):
        application.processEvents()
        await asyncio.sleep(0)
    shot.target.parent.mkdir(parents=True, exist_ok=True)
    picture = window.grab() if shot.what == "window" else _shot_of_dialog(
        application, window, shot.what, tab=shot.tab
    )
    if picture is None:
        print(
            f"не удалось снять окно «{shot.what}»: программа его не открыла. "
            "Если это окно выбора алгоритма — вероятнее всего кнопка выбора "
            "выключена, потому что каталог алгоритмов до окна не доехал",
            file=sys.stderr,
        )
        return 1
    # Результат `save()` обязателен к проверке: он возвращает False молча —
    # например, когда по расширению не удалось понять формат. Отчёт,
    # сообщающий «сохранено» там, где файла нет, хуже отсутствующего.
    if not picture.save(str(shot.target)):
        print(
            f"не удалось сохранить снимок в {shot.target}. Проверьте расширение "
            "файла (.png) и права на каталог",
            file=sys.stderr,
        )
        return 1
    print(f"снимок сохранён: {shot.target.resolve()}")
    return 0


def _shot_of_dialog(
    application: QApplication, window: MainWindow, what: str, *, tab: str = ""
) -> QPixmap | None:
    """Снимок окна, которое программа открывает **сама**, а не мы за неё.

    Ни один диалог здесь не строится. Открывается настоящее окно настроек
    (`MainWindow.open_settings`), а окно выбора — **щелчком по той самой
    кнопке**, которую жмёт человек. Поэтому снимок доказывает и то, что
    каталог доехал, и то, что кнопка не выключена: выключенная `click()`
    не срабатывает вовсе (`QAbstractButton::click` выходит на неактивной
    кнопке), окно не откроется, и снимка не будет — отказом, а не пустой
    картинкой.

    Возврат `None` означает «окно не открылось», и молчать об этом нельзя:
    файл, сохранённый с главным окном вместо запрошенного, — это ровно тот
    подложный снимок, из-за которого дефект и дожил до владельца счёта.

    ⚠️ Работа идёт таймерами, потому что `exec()` крутит **свой** цикл
    событий: изнутри него до нас управление не вернётся, и снять окно можно
    только тем, что исполнится в этом же цикле.
    """
    from PySide6.QtCore import QTimer  # noqa: PLC0415 — слои Qt после QT_API

    from ui.settings_dialog import SettingsDialog  # noqa: PLC0415 — то же

    taken: list[QPixmap] = []

    def take_the_topmost() -> None:
        """Снять самое верхнее модальное окно и закрыть его."""
        top = application.activeModalWidget()
        if top is None:
            return
        taken.append(top.grab())
        top.close()

    def inside_the_settings() -> None:
        """Мы внутри цикла окна настроек: снять его либо пойти глубже.

        ⚠️ Проверка типа — не про подписи. Наверху могло оказаться не то
        окно (предупреждение, подтверждение), и снимок такого под именем
        «настройки» был бы подлогом того же рода, что и снимок из демонстрации.
        """
        dialog = application.activeModalWidget()
        if not isinstance(dialog, SettingsDialog):
            return
        if tab and (page := dialog.page_of(tab)) is not None:
            dialog.tabs.setCurrentWidget(page)
        if what == "algorithm":
            QTimer.singleShot(_SHOT_PAUSE_MS, take_the_topmost)
            dialog.algorithm_button.click()
        else:
            taken.append(dialog.grab())
        dialog.close()

    QTimer.singleShot(_SHOT_PAUSE_MS, inside_the_settings)
    window.open_settings()
    return taken[0] if taken else None


def _on_interrupt(closed: asyncio.Event) -> None:
    """Ctrl+C в консоли — такое же завершение, как закрытие окна.

    Обработчик ставится через `signal.signal`, а не `loop.add_signal_handler`:
    цикл здесь не обычный asyncio, и на Windows второго способа нет вовсе.
    Питон исполнит обработчик между байткодами — часы в окне тикают раз
    в секунду, значит ждать дольше секунды не придётся.
    """
    try:
        signal.signal(signal.SIGINT, lambda *_: closed.set())
    except (ValueError, OSError):  # не главный поток или система не даёт
        return


if __name__ == "__main__":
    raise SystemExit(main())
