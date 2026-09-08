"""Запуск окна «Терминала» на подставных данных — чтобы посмотреть глазами.

Это **не** программа. Настоящая точка входа (`app/main.py`) появится на задаче
Э1-18, когда будут движок и журналы. Здесь окно открывается с `DetachedPort`:
кнопки нажимаются, но никуда не ведут — команд слушать некому.

Файл лежит в `tools/`, а не в `app/`, намеренно. Он берёт данные из `tests/`,
а в собранную поставку каталог тестов не входит: внутри пакета `app/` такой
импорт молча уехал бы в бинарь и упал бы там `ModuleNotFoundError`. Маски сборки
в `pyproject.toml` каталог `tools/` не захватывают.

Зачем нужен: посмотреть **оформление** окна на заготовленных данных, где есть
и вход, и переворот, и сработавший тейк, — не заводя базы и не гоняя движок.
Данные те же, что в тестах.

⚠️ **Настоящие свечи и настоящие сделки показывает программа**, а не этот файл::

    python3 -m app.main

Ключ `--db` и мост `tools/db_bridge.py` жили здесь, пока не было `app/`, и сняты
вместе с ним (Э1-18). Времянка, оставленная рядом с продуктом, живёт своей
жизнью: у моста была своя средняя «для картинки», и на экране она разошлась бы
с той, которую считает торговый модуль.

Запуск из корня репозитория::

    python3 tools/demo.py                   окно на экране
    python3 tools/demo.py --shot вид.png    снимок в файл, без экрана

Второй способ работает на машине без экрана — тем же путём сделаны снимки,
которые лежат в отчётах.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # только для подписи: Qt поднимается после QT_QPA_PLATFORM
    from ui.calendar_dialog import CalendarDialog
    from ui.settings_dialog import SettingsDialog

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _load_demo_data():
    """Заготовленные данные лежат рядом с тестами: второй копии не заводим.

    Копия «демонстрационных» данных в продуктовом коде разъедется с тестовой
    через месяц, и снимок начнёт показывать не то, что проверяется.
    """
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    import synthetic  # noqa: PLC0415 — путь добавляется только что, выше нельзя

    return synthetic


def _use_dark_palette(application) -> None:
    """Тёмная палитра для машины со светлой системной темой.

    Своей темы у программы нет: окно берёт системную (`ui/theme.py` только
    читает палитру). Здесь палитра подменяется, чтобы посмотреть тёмный вид
    там, где система светлая.
    """
    from PySide6.QtGui import QColor, QPalette  # noqa: PLC0415 — как в app/main.py:
    # Qt поднимается после QT_QPA_PLATFORM, а не при разборе ключей

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 tools/demo.py",
        description="Открыть окно «Терминала» на подставных данных.",
    )
    parser.add_argument(
        "--shot",
        dest="snapshot",
        metavar="ФАЙЛ",
        help="сохранить вид окна в файл и выйти (работает без экрана)",
    )
    parser.add_argument(
        "--calendar",
        dest="calendar",
        action="store_true",
        help="открыть календарь нерабочих дней вместо главного окна",
    )
    parser.add_argument(
        "--settings",
        dest="settings",
        action="store_true",
        help="открыть окно настроек вместо главного окна",
    )
    parser.add_argument(
        "--dark",
        dest="dark",
        action="store_true",
        help="тёмная тема вместо системной",
    )
    args = parser.parse_args(argv)

    if args.snapshot:
        # Именно присвоение, а не `setdefault`. Флаг «сделать снимок» сам по себе
        # означает «без экрана», а в окружении вполне может уже стоять `xcb`
        # или `wayland` — остаток от другого запуска. `setdefault` его не тронет,
        # Qt попытается поднять нерабочий плагин и упадёт ровно там, где этот
        # режим должен быть самым надёжным.
        import os

        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    from PySide6.QtWidgets import QApplication  # noqa: PLC0415 — после QT_QPA_PLATFORM

    from broker.redaction import scrub  # noqa: PLC0415 — рядом с местом сборки окна
    from ui.main_window import MainWindow  # noqa: PLC0415 — тянет PySide6
    from ui.ports import DetachedPort  # noqa: PLC0415 — тянет PySide6

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    synthetic = _load_demo_data()

    application = QApplication(sys.argv[:1])
    if args.dark:
        _use_dark_palette(application)

    if args.calendar:
        return _finish(application, _demo_calendar(), args)

    if args.settings:
        return _finish(application, _demo_settings(), args)

    # Та же чистка, что в бою (`app/main.py`): показ не должен отличаться
    # от программы тем, что у него нет предохранителя.
    window = MainWindow(port=DetachedPort(), sanitize=scrub)
    window.resize(1440, 900)

    window.show_chart(synthetic.chart_data())
    window.apply_state(synthetic.state())
    window.set_trades(synthetic.trades(), synthetic.summary())
    window.set_decisions(synthetic.decisions())
    return _finish(application, window, args)


def _demo_settings() -> "SettingsDialog":
    """Окно настроек с правилом робота словами — для снимка и для глаз.

    Правило приходит в окно **готовой строкой** от торгового модуля через
    порт; здесь порта нет, поэтому строка берётся тем же вызовом, которым
    её собирает `app/` (`convert.strategy_rule`). Второго текста при этом
    не появляется: считает его по-прежнему модуль.

    ⚠️ Ключ нужен потому, что `--shot` главного окна снимает **главное**
    окно, а абзац живёт в модальном диалоге: увидеть глазами, помещается ли
    он и как переносится, из консоли иначе нечем.
    """
    from app import convert  # noqa: PLC0415 — рядом с местом сборки окна
    from ui.models import Settings  # noqa: PLC0415 — тянет PySide6
    from ui.settings_dialog import SettingsDialog  # noqa: PLC0415 — тянет PySide6

    values = Settings()
    dialog = SettingsDialog(values)
    dialog.set_strategy_rule(convert.strategy_rule(values))
    # Вкладка «Сигнал» — вторая: снимок обязан открываться на ней, иначе
    # смотреть было бы не на что.
    page = dialog.page_of("Сигнал")
    if page is not None:
        dialog.tabs.setCurrentWidget(page)
    dialog.resize(700, 820)
    return dialog


def _demo_calendar() -> CalendarDialog:
    """Календарь с обоими видами отметок сразу — для снимка и для глаз.

    Дни подставные и постоянные: снимок, меняющийся от даты запуска, нельзя
    сравнить с прежним. Показаны все три случая — «не торгуем» и «торгуем»
    от владельца счёта и день, названный биржей, который мышкой не снимается.
    """
    from datetime import date  # noqa: PLC0415 — рядом с местом сборки окна

    from ui.calendar_dialog import CalendarDialog  # noqa: PLC0415 — тянет PySide6
    from ui.models import CalendarDay  # noqa: PLC0415 — тянет PySide6

    dialog = CalendarDialog(
        (
            CalendarDay(day=date(2026, 6, 22), trading=False),
            CalendarDay(day=date(2026, 6, 23), trading=False),
            CalendarDay(day=date(2026, 6, 20), trading=True),
        ),
        {date(2026, 6, 12): False},
    )
    from PySide6.QtCore import QDate  # noqa: PLC0415 — тянет PySide6

    # День выбора — непомеченный: выделение перекрывает заливку отметки,
    # и снимок иначе прятал бы один из трёх видов.
    dialog.calendar.setSelectedDate(QDate(2026, 6, 25))
    dialog.resize(620, 720)
    return dialog


def _finish(application, window, args) -> int:
    """Показать окно или сохранить снимок — общий хвост обеих веток."""

    if args.snapshot:
        window.show()
        application.processEvents()
        target = pathlib.Path(args.snapshot)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Результат `save()` обязателен к проверке: он возвращает False, когда
        # каталога нет или по расширению не удалось понять формат, — и молча.
        # Отчёт, сообщающий «сохранено» там, где файла нет, хуже отсутствующего.
        if not window.grab().save(str(target)):
            print(
                f"не удалось сохранить снимок в {target}. "
                "Проверьте расширение файла (.png) и права на каталог",
                file=sys.stderr,
            )
            return 1
        print(f"снимок сохранён: {target.resolve()}")
        return 0

    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
