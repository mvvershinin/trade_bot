"""Кнопка «Загрузить историю…», глубина в настройках и вкладки настроек.

Что здесь стережётся и почему именно это
-----------------------------------------
Замер 06.09.2026 живым запуском собранного `.exe` в виртуалке Windows без
питона: программа работала и советовала команду `python3 -m app.main --fetch`,
которой на машине владельца счёта нет и не будет. Свежепоставленная программа
показывала пустой график и совет, ведущий в никуда (`B-024`).

Отсюда три группы проверок, и все три — про то, что человек видит и делает
мышкой:

* **диалог загрузки** говорит числами, а не «вы уверены?»: что уже в базе,
  что именно будет запрошено и чем это обернётся. Слепое «вы уверены?»
  приучает жать «да» не читая;
* **порт** грузит вне потока окна, даёт отменить и **всегда** отвечает —
  и на успехе, и на отказе, и на отмене;
* **вкладки настроек** не теряют полей: каждое поле `Settings` лежит ровно
  на одной вкладке, и подтверждение «было → стало» переживает разделение.

⚠️ Тесты текстов здесь проверяют **утверждения**, а не формулировки:
«в строке названо число дней», «сказано, что сделки изменятся». Проверка
дословного текста ломалась бы на любой правке запятой и потому очень быстро
перестала бы что-либо стеречь.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from PySide6.QtCore import QDate, QPoint, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QAbstractSpinBox, QComboBox

from market import (
    MSK,
    CandleStore,
    HttpxTransport,
    IssClient,
    MarketWorker,
    Source,
)
from market.history import DEFAULT_DEPTH_DAYS
from market.journal import redact
from ui.history_dialog import (
    SHARED_WARNING,
    TRADES_CHANGE,
    HistoryDialog,
    fill_note,
)
from ui.models import (
    HistoryFacts,
    HistoryLoadOutcome,
    HistoryLoadRequest,
    Settings,
)
from ui.settings_dialog import PLACEMENT, SettingsDialog

from app.port import HistoryPort

from helpers import finish_what_is_left, settle_qt
from market_helpers import FakeTransport, iss_body, minute, no_sleep

TODAY = date(2026, 9, 6)


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
    """Ответ `candleborders` — тот же вид, что у живой ISS 05.09.2026."""
    return json.dumps(
        {
            "borders": {
                "columns": ["begin", "end", "interval"],
                "data": [list(row) for row in rows],
            },
            "durations": {"columns": ["interval", "duration"], "data": [[1, 60]]},
        }
    ).encode("utf-8")


def alive_borders(today: date) -> bytes:
    """Инструмент, у которого минутки есть за последние двести дней."""
    begin = today - timedelta(days=200)
    return borders_body(
        (f"{begin:%Y-%m-%d} 10:00:00", f"{today:%Y-%m-%d} 23:49:59", 1),
        (f"{begin:%Y-%m-%d} 00:00:00", f"{today:%Y-%m-%d} 00:00:00", 24),
    )


def session(day: date, count: int = 60) -> list:
    """Подряд идущие минутки одного дня, с 10:00 МСК."""
    start = datetime.combine(day, time(10, 0), MSK)
    return [minute(start + timedelta(minutes=i), open=100.0 + i) for i in range(count)]


def exchange(
    borders: bytes, pages: Sequence[bytes] = (), *, delay: float = 0.0
) -> Callable[[str], bytes]:
    """Подставная биржа: справка о глубине и страницы свечей по порядку."""
    queue = list(pages)
    empty = iss_body([])

    def handler(url: str) -> bytes:
        if delay:
            import time as clock

            clock.sleep(delay)
        if "candleborders" in url:
            return borders
        return queue.pop(0) if queue else empty

    return handler


def picky_exchange(
    borders: bytes, have: Sequence[object]
) -> tuple[Callable[[str], bytes], list[tuple[date, date]]]:
    """Биржа, которая **смотрит на границы запроса**, и список этих границ.

    Обычная подставная биржа отдаёт заготовленную страницу на любой запрос,
    и на ней неразличимы «день перезапросили» и «день пропустили»: свечи
    приезжают в обоих случаях. Разницу между «догрузить» и «заново» без этого
    проверить нечем — тест зеленел бы и на коде, который отметки не снимает.
    """
    asked: list[tuple[date, date]] = []
    minutes = list(have)

    def handler(url: str) -> bytes:
        if "candleborders" in url:
            return borders
        query = parse_qs(urlparse(url).query)
        start = int(query["start"][0])
        since = date.fromisoformat(query["from"][0])
        until = date.fromisoformat(query["till"][0])
        if start == 0:
            asked.append((since, until))
        if start:  # вторая страница — всегда пустая: конец выдачи
            return iss_body([])
        return iss_body([
            candle for candle in minutes if since <= candle.time.date() <= until
        ])

    return handler, asked


# ===========================================================================
# Диалог: числа вместо «вы уверены?»
# ===========================================================================

FILLED = HistoryFacts(
    symbol="MXU6",
    first=date(2026, 6, 7),
    last=date(2026, 9, 5),
    trading_days=74,
    dense_days=71,
    sparse_days=3,
    minutes=41_000,
)


def _dialog(facts: HistoryFacts = FILLED, settings: Settings | None = None):
    """Диалог загрузки с известной «сегодняшней» датой.

    Дата задаётся явно: без неё отрезок в подписи зависит от дня прогона,
    и тест начинает проверять календарь, а не окно.
    """
    return HistoryDialog(facts, settings or Settings(), today=TODAY)


def test_the_dialog_names_what_is_already_in_the_base(qapp) -> None:
    """Опись базы — числами: сколько дней, с какого по какой, сколько плотных.

    Требование владельца счёта 06.09.2026: «на попапе с вы уверены предупреди
    о последствиях». Ответ на «стоит ли грузить» без чисел дать нельзя,
    а слепое «вы уверены?» приучает жать «да» не читая.
    """
    dialog = _dialog()
    try:
        said = dialog.fill.text()
        assert "74" in said, f"не названо число торговых дней: {said!r}"
        assert "07.06.2026" in said and "05.09.2026" in said, (
            f"не названы границы того, что уже в базе: {said!r}"
        )
        assert "71" in said and "3" in said, (
            f"не названы плотные дни и огрызки: {said!r}"
        )
    finally:
        dialog.deleteLater()


def test_the_default_is_to_add_what_is_missing_not_to_reload(qapp) -> None:
    """Умолчание — «догрузить недостающее», а не «загрузить заново».

    Кнопка, по умолчанию перезапрашивающая всё, однажды будет нажата
    случайно, и случится ровно то, чего владелец счёта боится: «чтобы
    не перетирать если данные есть напрасно» — его слова 06.09.2026.
    """
    dialog = _dialog()
    try:
        assert dialog.add_missing.isChecked(), "умолчанием выбрано не догружение"
        assert not dialog.reload.isChecked()
        request = dialog.request()
        assert request is not None
        assert request.replace is False, (
            "просьба по умолчанию перезапрашивает уже загруженные дни"
        )
        assert request.since is None, (
            "просьба по умолчанию несёт дату начала — значит это «заново»"
        )
    finally:
        dialog.deleteLater()


def test_both_choices_warn_that_the_trades_will_change(qapp) -> None:
    """Предупреждение про изменение сделок стоит у **обоих** вариантов.

    Соблазн написать его только у «заново» велик — там страшнее. Но среднюю
    двигает и дыра, закрытая посреди ряда: ряд с другими свечами даёт другую
    среднюю и другие входы. Владелец счёта, выбравший «безопасный» вариант,
    обязан знать это до нажатия, а не после.
    """
    dialog = _dialog()
    try:
        safe = dialog.missing_note.text()
        risky = dialog.reload_note.text()
        for name, said in (("догрузить", safe), ("заново", risky)):
            assert "сделк" in said.lower(), (
                f"у варианта «{name}» не сказано, что изменятся сделки: {said!r}"
            )
            assert "средн" in said.lower(), (
                f"у варианта «{name}» не названа причина — средняя: {said!r}"
            )
            assert "прогон" in said.lower(), (
                f"у варианта «{name}» не сказано про записанные прогоны: {said!r}"
            )
    finally:
        dialog.deleteLater()


def test_the_shared_warning_is_written_once_and_used_twice(qapp) -> None:
    """Один текст на оба варианта, а не два похожих.

    Два разных описания одного последствия читаются как два разных
    последствия, и правят потом одно из них.
    """
    dialog = _dialog()
    try:
        assert TRADES_CHANGE in dialog.missing_note.text(), (
            "у безопасного варианта своя формулировка последствия"
        )
        assert TRADES_CHANGE in dialog.reload_note.text(), (
            "у варианта «заново» своя формулировка того же последствия"
        )
        assert SHARED_WARNING.startswith(TRADES_CHANGE), (
            "развёрнутый текст перестал начинаться с общей фразы — значит "
            "их уже два"
        )
    finally:
        dialog.deleteLater()


def test_only_the_reload_promises_to_replace_the_broker_minutes(qapp) -> None:
    """Про замену минуток брокера биржевыми говорит только «заново».

    И это согласовано с кодом: обычная догрузка не перезапрашивает дни,
    которые уже отмечены загруженными, а «заново» снимает отметки и просит
    отрезок целиком — там замена и происходит (`CandleStore.put_minutes`,
    ранг источника).
    """
    dialog = _dialog()
    try:
        assert "брокер" in dialog.reload_note.text().lower()
        assert "брокер" not in dialog.missing_note.text().lower(), (
            "безопасный вариант обещает замену минуток, которой в нём нет"
        )
        assert "не удал" in dialog.reload_note.text().lower(), (
            "не сказано, что ничего не удаляется"
        )
    finally:
        dialog.deleteLater()


def test_the_summary_names_the_span_in_numbers(qapp) -> None:
    """Что именно будет запрошено — числами, прямо над кнопкой «Загрузить»."""
    dialog = _dialog(settings=Settings(history_depth_days=30))
    try:
        said = dialog.summary.text()
        assert "MXU6" in said
        assert "30 дн." in said, f"не названо число дней отрезка: {said!r}"
        assert f"{TODAY:%d.%m.%Y}" in said, f"не названа правая граница: {said!r}"
        assert "08.08.2026" in said, (
            f"левая граница посчитана не по глубине из настроек: {said!r}"
        )
    finally:
        dialog.deleteLater()


def test_the_depth_comes_from_the_settings_not_from_a_constant(qapp) -> None:
    """Глубина берётся из настроек, а своей константы у окна нет.

    До 06.09.2026 в программе жили три числа глубины, и ни одно не совпадало
    с другим: показ 90 в настройках, загрузка 30 в слое данных и 90, зашитое
    в диалоге прогона (`D-068`). Владелец счёта ставил 180 — прогон предлагал
    90, а скачивалось 30.
    """
    dialog = _dialog(settings=Settings(history_depth_days=180))
    try:
        request = dialog.request()
        assert request is not None
        assert request.days == 180, "глубина взята не из настроек"
        assert "180 дн." in dialog.summary.text()
    finally:
        dialog.deleteLater()


def test_the_chosen_date_reaches_the_request(qapp) -> None:
    """«Заново с даты…»: выбранная дата уходит в просьбу, и с ней `replace`."""
    dialog = _dialog()
    try:
        dialog.reload.setChecked(True)
        dialog.since.setDate(QDate(2026, 7, 1))
        request = dialog.request()
        assert request is not None
        assert request.replace is True
        assert request.since == date(2026, 7, 1)
        assert "01.07.2026" in dialog.summary.text()
    finally:
        dialog.deleteLater()


def test_the_date_field_is_dead_while_the_plain_load_is_chosen(qapp) -> None:
    """Поле даты живёт только у «заново»: иначе оно обещает то, чего не будет."""
    dialog = _dialog()
    try:
        assert not dialog.since.isEnabled()
        dialog.reload.setChecked(True)
        assert dialog.since.isEnabled()
    finally:
        dialog.deleteLater()


def test_a_click_on_the_dead_date_field_picks_its_choice(qapp) -> None:
    """Стережёт `B-030`: щелчок по полю выбирает его переключатель.

    Слова владельца счёта 06.09.2026: «неактивное поле не меняется». Поле
    стоит справа от переключателя и читается как часть одной строки; человек
    целится в него, а не в кружок диаметром в семь точек. Пока щелчок
    не делал ничего, окно выглядело сломанным.

    ⚠️ Щелчок идёт **настоящим** нажатием мыши через `QTest`, а не вызовом
    `setChecked`: проверка вызова стерегла бы наш же вызов. Проверка
    «после щелчка поле включено» тоже не годится — она зеленеет, если
    поле просто оставить включённым всегда, а это другое поведение
    и другое обещание человеку.
    """
    from PySide6.QtTest import QTest

    dialog = _dialog()
    try:
        dialog.show()  # без показа геометрии нет, и щёлкать некуда
        assert dialog.add_missing.isChecked(), "начальное состояние не то"
        assert not dialog.since.isEnabled(), "поле и так живое — проверка вакуумна"

        QTest.mouseClick(dialog.since, Qt.MouseButton.LeftButton)

        assert dialog.reload.isChecked(), (
            "щелчок по полю не выбрал «Загрузить заново с даты…»: человек "
            "жмёт на поле и не получает ничего"
        )
        assert dialog.since.isEnabled(), "поле осталось мёртвым после выбора"
    finally:
        dialog.deleteLater()
        settle_qt(qapp)  # показанный диалог не должен пережить свой тест


def test_a_click_on_the_already_chosen_field_changes_nothing(qapp) -> None:
    """Обратная сторона: выбранный вариант щелчком по полю не сбрасывается."""
    from PySide6.QtTest import QTest

    dialog = _dialog()
    try:
        dialog.show()
        dialog.reload.setChecked(True)
        QTest.mouseClick(dialog.since, Qt.MouseButton.LeftButton)
        assert dialog.reload.isChecked(), "щелчок сбросил уже выбранный вариант"
    finally:
        dialog.deleteLater()
        settle_qt(qapp)  # показанный диалог не должен пережить свой тест


def test_an_unreadable_base_says_so_instead_of_saying_it_is_empty() -> None:
    """«Базу не прочитали» и «истории нет» — разные утверждения.

    Пустые числа при отказе базы читались бы как «в базе ничего нет»,
    и владелец счёта пошёл бы качать заново то, что у него уже есть.
    """
    said = fill_note(HistoryFacts(symbol="MXU6", trouble="Базу прочитать не удалось: диск"))
    assert "не удалось" in said
    assert "нет ни одной свечи" not in said


def test_an_empty_base_is_called_normal_for_a_fresh_program() -> None:
    """Пустая база — обычное состояние поставки, а не поломка."""
    said = fill_note(HistoryFacts(symbol="MXZ6"))
    assert "MXZ6" in said
    assert "ни одной свечи" in said
    assert "обычное" in said, (
        "пустая база подана как беда, хотя это состояние свежей поставки"
    )


def test_the_wheel_does_not_change_the_date_without_focus(qapp) -> None:
    """Колесо мыши над полем даты значение не меняет, пока нет фокуса.

    Владелец счёта уже ловил это на себе в настройках 05.09.2026: «пока
    я скроллю мышкой, я могу случайно поменять значение и не заметить».
    Диалог загрузки — то же самое поле в том же окне.
    """
    dialog = _dialog()
    try:
        dialog.reload.setChecked(True)
        dialog.show()
        qapp.processEvents()
        before = dialog.since.date()
        assert dialog.since in dialog.guarded_fields, "поле даты не под защитой"
        _roll(qapp, dialog.since)
        assert dialog.since.date() == before, "колесо изменило дату без фокуса"
    finally:
        dialog.close()
        dialog.deleteLater()
        settle_qt(qapp)  # показанный диалог не должен пережить свой тест


def _roll(qapp, field) -> None:
    """Крутнуть колесо над полем — как это делает мышь при прокрутке окна."""
    event = QWheelEvent(
        QPoint(5, 5), field.mapToGlobal(QPoint(5, 5)),
        QPoint(0, 120), QPoint(0, 120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    )
    qapp.sendEvent(field, event)
    qapp.processEvents()


# ===========================================================================
# Вкладки настроек: ни одно поле не потеряно
# ===========================================================================

def test_every_settings_field_is_placed_on_exactly_one_tab() -> None:
    """Каждое поле `Settings` названо в раскладке ровно один раз.

    Полей тридцать пять. Поле, забытое в раскладке, не падает и не светится:
    оно просто не показывается, а на «Применить» возвращается к умолчанию —
    ровно так жили фильтр против пилы и закрытие по концу окна. Добавление
    тридцать шестого поля обязано ронять этот тест.
    """
    import dataclasses

    declared = [field.name for field in dataclasses.fields(Settings)]
    placed = [row[0] for row in PLACEMENT]
    assert sorted(placed) == sorted(set(placed)), (
        "поле названо в раскладке дважды: "
        f"{sorted({name for name in placed if placed.count(name) > 1})}"
    )
    assert set(declared) == set(placed), (
        f"не на вкладке: {sorted(set(declared) - set(placed))}; "
        f"вкладка для несуществующего поля: {sorted(set(placed) - set(declared))}"
    )


def test_the_widget_of_every_field_lives_on_the_tab_the_table_names(qapp) -> None:
    """Раскладка и окно согласованы: виджет лежит на названной вкладке.

    Без этой проверки таблица — просто комментарий: поле можно перенести
    на другую вкладку и забыть поправить строку, и расхождение будет молчать.
    """
    dialog = SettingsDialog(Settings())
    try:
        wrong: list[str] = []
        for name, tab, widget_name in PLACEMENT:
            page = dialog.page_of(tab)
            assert page is not None, f"вкладки «{tab}» в окне нет вовсе"
            assert widget_name, f"у поля {name} не назван виджет"
            widget = getattr(dialog, widget_name, None)
            assert widget is not None, (
                f"поле {name} обещает виджет `{widget_name}`, которого в окне нет"
            )
            if not page.isAncestorOf(widget):
                wrong.append(f"{name} ({widget_name}) обещан на «{tab}»")
        assert not wrong, "виджет лежит не на той вкладке: " + "; ".join(wrong)
    finally:
        dialog.deleteLater()


def test_the_placement_check_is_not_blind(qapp) -> None:
    """Канарейка предыдущей проверки: подложенное расхождение обязано падать.

    Проверка «виджет на своей вкладке» проходила бы и на окне без вкладок
    вовсе. Здесь она натравливается на заведомо неверную строку раскладки,
    и обязана эту строку найти.
    """
    dialog = SettingsDialog(Settings())
    try:
        page = dialog.page_of("Программа")
        assert page is not None
        assert not page.isAncestorOf(dialog.instrument), (
            "поле «Инструмент» оказалось на вкладке «Программа» — тогда "
            "проверка раскладки не различает вкладки вовсе"
        )
    finally:
        dialog.deleteLater()


def test_a_change_on_a_tab_that_was_never_opened_is_in_the_confirmation(qapp) -> None:
    """Изменение на неоткрытой вкладке попадает в «было → стало».

    Подтверждение изменений (решение 0042) заведено ровно против того, чтобы
    правка по деньгам прошла незамеченной. Вкладки прячут поля с глаз —
    и если бы окно читало только показанную вкладку, они спрятали бы и правку.
    """
    dialog = SettingsDialog(Settings())
    try:
        money_tab = next(
            index for index in range(dialog.tabs.count())
            if dialog.tabs.tabText(index) == "Деньги"
        )
        assert dialog.tabs.currentIndex() != money_tab, (
            "вкладка «Деньги» открыта с самого начала — проверка вакуумна"
        )
        dialog.volume.setValue(7)
        assert dialog.values().volume == 7, (
            "правка на неоткрытой вкладке не дошла до набора настроек"
        )
        assert dialog.values() != dialog.applied(), (
            "окно не видит разницы, то есть подтверждение её не покажет"
        )
    finally:
        dialog.deleteLater()


def test_the_wheel_is_guarded_on_every_tab(qapp) -> None:
    """Защита от колеса накрывает поля **всех** вкладок, а не только первой.

    `guard_wheel` обходит окно целиком, поэтому скрытая вкладка тоже под
    защитой. Проверяется это, а не сам обход: лениво собранные страницы
    вкладок остались бы без защиты, и дыра была бы не видна.
    """
    dialog = SettingsDialog(Settings())
    try:
        guarded = set(dialog.guarded_fields)
        naked: list[str] = []
        for index in range(dialog.tabs.count()):
            page = dialog.tabs.widget(index)
            for kind in (QAbstractSpinBox, QComboBox):
                for field in page.findChildren(kind):
                    if field not in guarded:
                        naked.append(f"{dialog.tabs.tabText(index)}: {field.objectName()}")
        assert not naked, "поля без защиты от колеса: " + ", ".join(naked)
        assert len(guarded) > 15, "под защитой подозрительно мало полей"
    finally:
        dialog.deleteLater()


def test_the_two_depths_are_named_by_different_words(qapp) -> None:
    """Две глубины подписаны разными словами, а не одним «глубина».

    Два поля со словом «глубина» рядом — готовая путаница, и цена ей
    названа: владелец счёта поставит 180 в поле показа и будет ждать года
    данных, которых никто не скачал (`D-068`).
    """
    dialog = SettingsDialog(Settings())
    try:
        load = dialog.history_depth_days.toolTip()
        show = dialog.depth_days.toolTip()
        assert "скачать" in load.lower() or "качать" in load.lower(), (
            f"подсказка глубины загрузки не говорит про скачивание: {load!r}"
        )
        assert "показ" in show.lower(), (
            f"подсказка глубины показа не говорит про показ: {show!r}"
        )
        assert load != show
        assert dialog.history_depth_days.minimum() >= 1, (
            "у глубины загрузки достижим ноль — «вся история» у биржи "
            "не выражается числом, и отрезок нельзя назвать до нажатия"
        )
    finally:
        dialog.deleteLater()


def test_showing_deeper_than_loading_is_said_out_loud(qapp) -> None:
    """Показ глубже загрузки — не ошибка, но об этом сказано.

    Молчание здесь читается как «данных нет», а данных просто не скачали:
    ровно так владелец счёта 05.09.2026 полдня искал, почему видит историю
    с 6 августа при контракте с 17 июня.
    """
    dialog = SettingsDialog(Settings())
    try:
        dialog.set_values(Settings(depth_days=180, history_depth_days=90))
        said = dialog.depth_note.text()
        assert "90 дней" in said, f"не названо, сколько скачивается: {said!r}"
        dialog.set_values(Settings(depth_days=30, history_depth_days=90))
        quiet = dialog.depth_note.text()
        assert "90 дней" not in quiet, (
            "предупреждение горит и там, где показ мельче загрузки: "
            f"{quiet!r}"
        )
    finally:
        dialog.deleteLater()


def test_the_new_setting_survives_a_round_trip(qapp) -> None:
    """Глубина загрузки доезжает из набора в окно и обратно."""
    dialog = SettingsDialog(Settings())
    try:
        dialog.set_values(Settings(history_depth_days=45))
        assert dialog.values().history_depth_days == 45
    finally:
        dialog.deleteLater()


def test_a_change_of_the_load_depth_reaches_the_journal() -> None:
    """Смена глубины загрузки попадает в журнал решений отдельной строкой.

    ТЗ §4.4 А требует строку с прежним и новым значением на **каждое**
    изменение поля окна. Поле, забытое в таблице `_WINDOW_TOLD`, менялось бы
    молча: владелец счёта поменял бы параметр, а в журнале осталось бы
    «значения совпали с прежними».
    """
    from app import convert

    told = convert.window_changes(
        Settings(history_depth_days=90), Settings(history_depth_days=30)
    )
    joined = " ".join(told)
    assert "90" in joined and "30" in joined, (
        f"изменение глубины загрузки не описано: {told}"
    )
    assert "загруз" in joined.lower(), (
        f"строка не отличает загрузку от показа: {told}"
    )


def test_the_backtest_dialog_takes_its_span_from_the_settings(qapp) -> None:
    """Диалог прогона предлагает отрезок по глубине показа из настроек.

    Третье из трёх чисел глубины (`D-068`): в диалоге прогона было зашито
    90 дней. Владелец счёта ставил в настройках 180, а прогон всё равно
    предлагал 90 — и объяснить это по экрану было нечем.

    Дней считается **включительно**: «10 дней» — это десять дней вместе
    с последним, а не одиннадцать.
    """
    from ui.backtest_dialog import BacktestDialog
    from ui.models import BacktestOptions, InstrumentInfo

    last = datetime(2026, 9, 5, 23, 45, tzinfo=MSK)
    options = BacktestOptions(instruments=(InstrumentInfo(
        symbol="MXU6",
        first=datetime(2026, 1, 5, 10, 0, tzinfo=MSK),
        last=last,
        minutes=50_000,
    ),))
    dialog = BacktestDialog(options, Settings(depth_days=10))
    try:
        assert dialog.since.date() == QDate(2026, 8, 27), (
            f"отрезок предложен не по настройкам: {dialog.since.date()}"
        )
        assert dialog.until.date() == QDate(2026, 9, 5)
    finally:
        dialog.deleteLater()

    whole = BacktestDialog(options, Settings(depth_days=0))
    try:
        assert whole.since.date() == QDate(2026, 1, 5), (
            "«вся история» в настройках не дала весь охват базы"
        )
    finally:
        whole.deleteLater()


def test_the_load_depth_default_agrees_with_the_data_layer() -> None:
    """Умолчание глубины в настройках и в слое данных — одно число.

    Число повторено в двух местах намеренно: `market/` не зависит ни от
    одного слоя проекта, и импортировать поле окна оттуда нельзя. Связь
    держит этот тест — разойдутся, прогон покраснеет, а не промолчит.
    До 06.09.2026 они и были разными: 90 в окне и 30 в загрузке (`D-068`).
    """
    assert Settings().history_depth_days == DEFAULT_DEPTH_DAYS


# ===========================================================================
# Порт: загрузка вне потока окна, отмена, ответ всегда
# ===========================================================================

# ---------------------------------------------------------------------------
# Сторожа фикстуры `loop`: цикл переживает чужой и собственный мусор
# ---------------------------------------------------------------------------
#
# ⚠️ Сама фикстура живёт в `tests/conftest.py` — одна на весь прогон.
# Сторожа остались здесь, потому что здесь и ловилась поломка; правка
# фикстуры без прогона этого раздела ничем не проверена.
#
# ⚠️ Проверяется не «фикстура написана правильно», а поведение, за которое
# заплачено: **тест падает не там, где сломано**. Полный прогон 06.09.2026
# в шестнадцать процессов ронял то `test_the_reload_forgets_the_marks…`,
# то `test_the_button_makes_the_base_out_of_nothing`, то
# `test_the_load_runs_outside_the_event_loop` — по одному за прогон и каждый
# раз другой, при том что поодиночке проходили все. Ломал их сосед, случайно
# доставшийся тому же воркеру.


@pytest.fixture
def a_neighbour_left_a_window(qapp):
    """Сосед показал виджет и попросил удалить, событий не прокрутив.

    ⚠️ Порядок фикстур в подписи теста здесь значения не имеет, и прежнее
    объяснение («мусор обязан появиться до того, как поднимется цикл»)
    было неверным: цикл запускается в **теле** теста, к этому моменту обе
    фикстуры уже подняты. Проверено 06.09.2026 — со снятой защитой тест
    падает при любом из двух порядков. Объяснение исправлено, а не тест:
    ложный довод в докстринге уводит следующего, кто станет тут править.

    Своё убирает сама: удаление, которое мы завели, не должно уехать
    в следующий тест — ровно то, из-за чего вся эта возня и началась.
    """
    from PySide6.QtWidgets import QWidget

    happened: list[str] = []
    stray = QWidget()
    stray.resize(120, 80)
    stray.show()
    stray.destroyed.connect(lambda *_: happened.append("сосед убран"))
    stray.deleteLater()  # событие DeferredDelete осталось в очереди
    yield happened
    settle_qt(qapp)


def test_a_neighbours_undeleted_window_does_not_stop_the_loop(
    a_neighbour_left_a_window, loop
) -> None:
    """Отложенное удаление чужого окна не выбрасывает нас из своего цикла.

    Точная запись того, что ловилось живьём. `qasync` крутит не свой цикл,
    а цикл приложения: `run_forever` зовёт `QApplication.exec()`. Удаление,
    оставленное соседом в очереди, исполняется уже внутри него, Qt объявляет
    «закрылось последнее окно» и по умолчанию выходит из цикла — на середине
    нашей корутины. `qasync` докладывает это как `Event loop stopped before
    Future completed`, то есть падением **нашего** теста по вине предыдущего,
    и каждый прогон падает другой.

    Проверка не вакуумна: рядом стоит утверждение, что удаление соседа
    вообще случилось. Не случилось — ломать было нечего.

    Мутация: убрать `qapp.setQuitOnLastWindowClosed(False)` из фикстуры
    `loop` — тест обязан покраснеть.
    """
    happened = a_neighbour_left_a_window

    async def counts_to_the_end() -> int:
        done = 0
        for _ in range(50):
            await asyncio.sleep(0)
            done += 1
        return done

    assert loop.run_until_complete(counts_to_the_end()) == 50, (
        "корутина не досчитала: цикл вышел раньше, чем она кончилась"
    )
    assert "сосед убран" in happened, (
        "виджет соседа не удалился вовсе — значит ломать было нечего "
        "и проверка вакуумна"
    )


def test_a_bare_process_events_leaves_a_deleted_window_alive(qapp) -> None:
    """`processEvents()` отложенное удаление не исполняет, `settle_qt` — да.

    Замер под уборку фикстуры `loop`. Разница неочевидна и стоила поломки:
    тесты этого файла заканчиваются связкой `deleteLater(); processEvents()`
    и выглядят прибранными, а окно после неё **живо** и уезжает в чужой
    тест — где его удаление и случится, внутри чужого цикла событий.

    Мутация: убрать `sendPostedEvents(..., DeferredDelete)` из `settle_qt` —
    тест обязан покраснеть.
    """
    from PySide6.QtWidgets import QWidget

    gone: list[int] = []
    widget = QWidget()
    widget.resize(60, 40)
    widget.show()
    widget.destroyed.connect(lambda *_: gone.append(1))
    widget.deleteLater()

    qapp.processEvents()
    assert not gone, (
        "processEvents() удалил окно сам — тогда уборке нечего добавлять "
        "и проверка ниже вакуумна"
    )

    settle_qt(qapp)
    assert gone, (
        "после уборки окно всё ещё живо: оно уедет в следующий тест и "
        "удалится там, внутри чужого цикла событий"
    )


def test_a_window_closed_inside_the_loop_does_not_stop_it(loop, qapp) -> None:
    """Окно, закрывшееся **во время** работы, тоже цикл не останавливает.

    Ровно то, что обещает `app/main.py` строкой
    `setQuitOnLastWindowClosed(False)`: закрытие последнего окна не выход
    из программы. Здесь эта строка нужна и потому, что модальные окна
    в тестах ниже открываются и закрываются прямо в работающем цикле.

    Мутация: убрать `qapp.setQuitOnLastWindowClosed(False)` из фикстуры
    `loop` — тест обязан покраснеть.
    """
    from PySide6.QtWidgets import QWidget

    async def shows_and_closes_a_window() -> str:
        widget = QWidget()
        widget.resize(120, 80)
        widget.show()
        await asyncio.sleep(0)
        widget.close()
        widget.deleteLater()
        for _ in range(30):
            await asyncio.sleep(0.001)
        return "дошло до конца"

    assert loop.run_until_complete(shows_and_closes_a_window()) == "дошло до конца", (
        "закрытие последнего окна остановило цикл — как раз то, что "
        "app/main.py запрещает"
    )


@pytest.fixture
def nothing_shown_may_outlive_the_test():
    """Сторож уборки цикла: показанное окно теста разрушено здесь, а не у соседа.

    ⚠️ Стоит в подписи теста **перед** `loop` намеренно, и здесь порядок
    значим по-настоящему: `pytest` разбирает фикстуры в обратном порядке,
    значит проверка исполняется уже после уборки цикла — а следит она
    именно за уборкой. Поменять местами — и она посмотрит на момент
    до неё, то есть не проверит ничего.
    """
    gone: list[str] = []
    yield gone
    assert gone == ["окно теста удалено"], (
        "показанное окно пережило уборку цикла. Разрушится оно в первом же "
        "чужом цикле событий — а для теста, который цикл крутит, это "
        "«закрылось последнее окно» и выход на середине корутины"
    )


def test_the_loop_teardown_destroys_the_windows_the_test_left(
    nothing_shown_may_outlive_the_test, loop, qapp
) -> None:
    """Уборка фикстуры доводит до конца отложенное удаление, а не откладывает его.

    Половина фикстуры, про которую забывают: `deleteLater()` без прокрутки
    очереди не делает ничего, окно уезжает живым в следующий тест. Проверка
    смотрит на видимое следствие — сигнал `destroyed`, — а не на то, что
    в теле фикстуры написана строка.

    Окно намеренно создаётся **вне** работающего цикла: внутри цикла
    отложенное удаление исполнилось бы само, и ломать было бы нечего.

    Мутация: убрать `settle_qt(qapp)` из уборки фикстуры `loop`
    в `tests/conftest.py` — прогон обязан покраснеть на разборе фикстуры.
    """
    from PySide6.QtWidgets import QWidget

    widget = QWidget()
    widget.resize(120, 80)
    widget.show()
    widget.destroyed.connect(
        lambda *_: nothing_shown_may_outlive_the_test.append("окно теста удалено")
    )
    widget.deleteLater()

    assert widget.isVisible(), "окно не показано — для Qt оно не окно, и проверка вакуумна"
    assert not nothing_shown_may_outlive_the_test, (
        "окно удалилось до уборки — тогда проверка смотрит не на неё"
    )


@pytest.fixture
def nothing_may_be_left_running():
    """После уборки фикстуры `loop` ни одной висящей задачи не остаётся.

    ⚠️ Стоит в подписи теста **перед** `loop` намеренно: `pytest` разбирает
    фикстуры в обратном порядке, значит эта проверка исполняется уже после
    уборки цикла. Поменять местами — и она посмотрит на живой цикл, то есть
    не проверит ничего.
    """
    watched: list[asyncio.Task] = []
    yield watched
    left = [task for task in watched if not task.done()]
    assert not left, (
        f"после теста осталось висеть задач: {len(left)}. Сборщик мусора "
        "закроет их позже, уже в чужом тесте, и красным станет он"
    )


def test_the_loop_fixture_leaves_no_task_running_after_a_test_abandons_one(
    nothing_may_be_left_running, loop
) -> None:
    """Фикстура **зовёт** уборку, а не просто имеет её в файле.

    Отдельный тест от соседнего, и вот почему: тот дёргает помощник руками
    и остаётся зелёным, если вызов из фикстуры убрать вовсе. Проверено
    мутацией 06.09.2026 — снос строки прошёл молча.

    Мутация: убрать `finish_what_is_left(made)` из уборки фикстуры `loop` —
    прогон обязан покраснеть на разборе фикстуры.
    """
    tidied: list[str] = []

    async def never_finishes() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            tidied.append("порт закрыт")

    async def abandons_it() -> None:
        nothing_may_be_left_running.append(loop.create_task(never_finishes()))
        await asyncio.sleep(0)

    loop.run_until_complete(abandons_it())
    assert not tidied, "задача кончилась сама — бросать было нечего"
    assert nothing_may_be_left_running, "задача не завелась — проверка вакуумна"


def test_the_fixture_finishes_the_tasks_a_failed_test_abandoned(loop) -> None:
    """Брошенная задача доводится до конца здесь, а не в чужом тесте.

    Незаконченная корутина живёт до сборки мусора, а там её `close()`
    поднимает `GeneratorExit` уже вне всякого цикла. `pytest` считает
    предупреждения ошибками, поэтому красным становится **случайный сосед**:
    06.09.2026 так падал `test_ui_notices.py` трассировкой, целиком лежащей
    в этом файле.

    Проверяется не «список задач пуст», а что `finally` внутри корутины
    отработал: именно там закрываются порт и поток данных.

    ⚠️ Тест зовёт помощник **напрямую**, поэтому стережёт его поведение,
    а не то, что фикстура его зовёт. Проводку стережёт соседний тест —
    `test_the_loop_fixture_leaves_no_task_running_after_a_test_abandons_one`;
    без него снос вызова из фикстуры прошёл бы молча (проверено мутацией).

    ⚠️ Цикл берётся из общей фикстуры, а не собирается здесь руками. Своя
    сборка была — и была без защиты от чужого отложенного удаления, то есть
    ровно тем случаем, из-за которого правку и делали. Рукописных копий
    в дереве не осталось ни одной, и это стережёт
    `test_no_test_file_builds_its_own_qasync_loop`.

    Мутация: выпотрошить тело `finish_what_is_left` — тест обязан
    покраснеть.
    """
    tidied: list[str] = []

    async def never_finishes() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            tidied.append("порт закрыт")

    async def abandons_it() -> None:
        loop.create_task(never_finishes())
        await asyncio.sleep(0)

    loop.run_until_complete(abandons_it())
    assert not tidied, "задача успела закончиться сама — проверка вакуумна"
    assert [task for task in asyncio.all_tasks(loop) if not task.done()], (
        "брошенной задачи не осталось — ломать нечего"
    )

    finish_what_is_left(loop)

    assert tidied == ["порт закрыт"], (
        "уборка не довела брошенную задачу до её `finally`: закрытие "
        "порта и потока данных не случилось"
    )
    assert not [task for task in asyncio.all_tasks(loop) if not task.done()], (
        "задача осталась висеть и взорвётся в чужом тесте"
    )


def test_no_test_file_builds_its_own_qasync_loop() -> None:
    """Цикл `qasync` собирается в одном месте — в общей фикстуре обвязки.

    Находка ревью 06.09.2026, и цена её названа: рукописных сборок было
    шесть, защиты от чужого отложенного удаления не было ни в одной, а
    обоснование звучало как «цикл крутит только один файл». Копия, которую
    поправили в одном месте из шести, — это защита, про которую все думают,
    что она стоит везде.

    Проверка разбором, а не поиском по строке: `ast` видит вызов
    `qasync.QEventLoop(...)` и там, где он написан внутри функции.

    Мутация: собрать `qasync.QEventLoop(qapp)` в любом файле `tests/test_*.py`
    — проверка обязана назвать файл и строку.
    """
    import ast

    found: list[str] = []
    for path in sorted(pathlib.Path(__file__).resolve().parent.glob("test_*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            if ast.unparse(node.func).endswith("qasync.QEventLoop"):
                found.append(f"{path.name}, строка {node.lineno}")

    assert not found, (
        "цикл qasync собран руками мимо общей фикстуры `loop` "
        "(tests/conftest.py). Такая копия не знает про "
        "`setQuitOnLastWindowClosed(False)`, и чужое отложенное удаление "
        "выбросит её из цикла на середине корутины:\n  " + "\n  ".join(found)
    )


class Heard:
    """Всё, что порт сказал окну про загрузку."""

    def __init__(self, port: HistoryPort) -> None:
        self.facts: list[HistoryFacts] = []
        self.progress: list[tuple[int, str]] = []
        self.finished: list[HistoryLoadOutcome] = []
        self.failures: list[str] = []
        self.notes: list[object] = []
        port.history_facts_ready.connect(self.facts.append)
        port.history_progress.connect(lambda p, what: self.progress.append((p, what)))
        port.history_finished.connect(self.finished.append)
        port.failed.connect(self.failures.append)
        port.decision_appended.connect(self.notes.append)


def _run(loop, database: pathlib.Path, handler, work) -> tuple[HistoryPort, Heard]:
    """Поднять порт на подставной бирже и выполнить работу в цикле событий."""

    async def main() -> tuple[HistoryPort, Heard]:
        client = IssClient(FakeTransport(handler), sleep=no_sleep, pause=0)
        async with MarketWorker(database, iss=client) as worker:
            port = HistoryPort(worker, values=Settings(instrument="MXU6"),
                               days=0, sanitize=redact)
            heard = Heard(port)
            try:
                await work(port, worker)
            finally:
                await port.aclose()
            return port, heard

    return loop.run_until_complete(main())


@pytest.fixture
def empty_database(tmp_path: pathlib.Path) -> pathlib.Path:
    """Пустая база — файл есть, свечей в нём нет."""
    path = tmp_path / "candles.sqlite3"
    with CandleStore(path):
        pass
    return path


@pytest.fixture
def bare_folder(tmp_path: pathlib.Path) -> pathlib.Path:
    """Папка данных **без единого файла** — распакованная поставка.

    ⚠️ Именно этого не было в проверках до 06.09.2026, и потому `B-029`
    прожил незамеченным: `empty_database` файл базы **создаёт**, а поток
    данных, собранный поверх, открывается без разговоров. Проверка кнопки
    зеленела, а кнопка не работала.
    """
    place = tmp_path / "userdata"
    place.mkdir()
    return place / "candles.sqlite3"


def _run_as_main(loop, database: pathlib.Path, handler, work):
    """То же, что `_run`, но поток данных собран **как в `app/main.py`**.

    Разница ровно одна и она в этом вся: `open()` зовётся только тогда,
    когда файл базы уже есть (`app/main.py`, ветка `if database.exists()`).
    На чистой машине его нет, и поток остаётся неоткрытым — а кнопка
    обязана работать всё равно.
    """
    async def main():
        client = IssClient(FakeTransport(handler), sleep=no_sleep, pause=0)
        worker = MarketWorker(database, iss=client)
        if database.exists():
            await worker.open()
        port = HistoryPort(worker, values=Settings(instrument="MXU6"),
                           days=0, sanitize=redact)
        heard = Heard(port)
        try:
            await work(port, worker)
        finally:
            await port.aclose()
            await worker.close()
        return port, heard

    return loop.run_until_complete(main())


def test_the_button_makes_the_base_out_of_nothing(loop, bare_folder) -> None:
    """Стережёт `B-029` целиком: пустая папка → кнопка → свечи в базе.

    Ровно тот путь, которым идёт владелец счёта: распаковал поставку,
    запустил, нажал «Загрузить историю». До 06.09.2026 он упирался
    в отказ «поток данных не запущен: сначала `await worker.open()`» —
    файла базы нет, значит поток не открыт, а завести базу нечем, кроме
    этой самой кнопки. Замкнутый круг; базу владельцу счёта клали руками.

    ⚠️ Проверка «диалог открылся» это не заменяет: она зеленела всё то
    время, пока кнопка не работала.
    """
    assert not bare_folder.exists(), "файл базы есть — проверка стала вакуумной"
    day = TODAY - timedelta(days=3)
    handler = exchange(alive_borders(TODAY), [iss_body(session(day, 300))])

    async def work(port, worker) -> None:
        port.load_history(HistoryLoadRequest(symbol="MXU6", days=7))
        await _settle(port)

    port, heard = _run_as_main(loop, bare_folder, handler, work)

    assert bare_folder.exists(), "база сама себя не завела"
    with CandleStore(bare_folder) as store:
        assert store.coverage("MXU6").count > 0, "свечей в базе так и не появилось"
    assert heard.finished, "порт не сказал, чем кончилась загрузка"
    assert heard.finished[-1].ok, heard.finished[-1].trouble


def test_the_dialog_reads_the_base_that_does_not_exist_yet(loop, bare_folder) -> None:
    """Стережёт вторую половину `B-029`: опись до открытия потока.

    Диалог загрузки спрашивает опись первым делом — до того, как что-либо
    открыто. Владелец счёта увидел на её месте кусок кода про `await`.
    Здесь проверяется, что пришла **опись** («ни одной свечи»), а не отказ.
    """
    handler = exchange(alive_borders(TODAY))

    async def work(port, worker) -> None:
        port.request_history_facts("MXU6")
        await _settle_facts(port)

    port, heard = _run_as_main(loop, bare_folder, handler, work)

    assert heard.facts, "опись базы в окно не пришла вовсе"
    facts = heard.facts[-1]
    assert not facts.trouble, f"вместо описи пришёл отказ: {facts.trouble}"
    assert not facts.known, "пустая база объявлена наполненной"


def test_the_button_puts_candles_into_an_empty_base(loop, empty_database) -> None:
    """Главное: нажатие кнопки приносит свечи в пустую базу.

    Это тот самый путь, которым пойдёт владелец счёта на Windows: пустая
    папка данных, запуск окна, нажатие кнопки. До 06.09.2026 этого пути
    не существовало вовсе — история грузилась только командой в консоли,
    которой у него нет.
    """
    # Триста минуток — шестьдесят пятиминутных баров: больше, чем нужно
    # средней для прогрева (52 при периоде 15). На более коротком ряду
    # загрузка честно кончается словами «данных мало», и тест проверял бы
    # разбор беды, а не то, что свечи доехали.
    day = TODAY - timedelta(days=3)
    handler = exchange(alive_borders(TODAY), [iss_body(session(day, 300))])

    async def work(port, worker) -> None:
        port.load_history(HistoryLoadRequest(symbol="MXU6", days=7))
        await _settle(port)

    port, heard = _run(loop, empty_database, handler, work)

    with CandleStore(empty_database) as store:
        assert store.coverage("MXU6").count > 0, "свечей в базе так и не появилось"
    assert heard.finished, "порт не сказал, чем кончилась загрузка"
    assert heard.finished[-1].ok, heard.finished[-1].trouble


def test_the_load_runs_outside_the_event_loop(loop, empty_database) -> None:
    """Пока идёт загрузка, цикл событий жив: окно не замирает.

    Девяносто дней минуток — это тысячи запросов при ограничителе пять
    в секунду, то есть минуты. Синхронная загрузка в слоте Qt заморозила бы
    окно целиком, и владелец счёта снял бы процесс — вместе с открытой
    позицией.
    """
    day = TODAY - timedelta(days=3)
    handler = exchange(
        alive_borders(TODAY), [iss_body(session(day))], delay=0.05
    )
    ticks: list[int] = []

    async def tick() -> None:
        while True:
            ticks.append(1)
            await asyncio.sleep(0.005)

    async def work(port, worker) -> None:
        beat = asyncio.ensure_future(tick())
        port.load_history(HistoryLoadRequest(symbol="MXU6", days=7))
        await _settle(port)
        beat.cancel()

    _run(loop, empty_database, handler, work)
    assert len(ticks) > 3, (
        "во время загрузки цикл событий не крутился — окно было бы замершим"
    )


def test_the_progress_reaches_the_window(loop, empty_database) -> None:
    """Ход работы виден: без него ожидание неотличимо от поломки.

    Владелец счёта уже принимал закрытую биржу за поломку программы
    и потерял на этом полчаса (`B-022`).
    """
    day = TODAY - timedelta(days=3)
    handler = exchange(alive_borders(TODAY), [iss_body(session(day))])

    async def work(port, worker) -> None:
        port.load_history(HistoryLoadRequest(symbol="MXU6", days=7))
        await _settle(port)

    port, heard = _run(loop, empty_database, handler, work)
    assert heard.progress, "ход загрузки в окно не приходил ни разу"
    percent, said = heard.progress[-1]
    assert 0 <= percent <= 100
    assert "свечей" in said, f"в подписи хода нет числа свечей: {said!r}"


def test_a_refusal_names_the_reason_in_words(loop, empty_database) -> None:
    """Отказ объясняется словами и приходит **сразу**, а не молчанием.

    Собранный нами ряд (`@MX`) грузить неоткуда: такого кода у биржи нет
    (решение 0049). Молчаливый отказ выглядел бы как «кнопка не работает».
    """
    handler = exchange(alive_borders(TODAY))

    async def work(port, worker) -> None:
        port.load_history(HistoryLoadRequest(symbol="@MX", days=7))
        await asyncio.sleep(0)

    port, heard = _run(loop, empty_database, handler, work)
    assert heard.finished, "на отказе окно не получило ответа вовсе"
    outcome = heard.finished[-1]
    assert not outcome.ok
    assert "@" in outcome.trouble and "биржи" in outcome.trouble, outcome.trouble
    assert heard.failures, "отказ не показан строкой в окне"


def test_an_empty_instrument_is_refused_with_a_reason(loop, empty_database) -> None:
    """Пустой код инструмента — отказ с объяснением, а не пустая загрузка."""
    handler = exchange(alive_borders(TODAY))

    async def work(port, worker) -> None:
        port.load_history(HistoryLoadRequest(symbol="   ", days=7))
        await asyncio.sleep(0)

    port, heard = _run(loop, empty_database, handler, work)
    assert heard.finished and not heard.finished[-1].ok
    assert "нструмент" in heard.finished[-1].trouble


def test_the_facts_of_the_base_reach_the_dialog(loop, tmp_path) -> None:
    """Опись базы считает слой данных, а не окно.

    Плотные дни и огрызки разделяет `market.inventory` по доле от самого
    плотного дня. Второй такой расчёт в окне разошёлся бы с первым молча.
    """
    path = tmp_path / "filled.sqlite3"
    with CandleStore(path) as store:
        store.put_minutes("MXU6", session(date(2026, 6, 10), 60), Source.ISS)
        store.put_minutes("MXU6", session(date(2026, 6, 11), 3), Source.ISS)
    handler = exchange(alive_borders(TODAY))

    async def work(port, worker) -> None:
        port.request_history_facts("MXU6")
        await _settle_facts(port)

    port, heard = _run(loop, path, handler, work)
    assert heard.facts, "опись базы в окно не пришла"
    facts = heard.facts[-1]
    assert facts.symbol == "MXU6"
    assert facts.dense_days == 1 and facts.sparse_days == 1, facts
    assert facts.minutes == 63


async def _settle_facts(port: HistoryPort, rounds: int = 200) -> None:
    """Дождаться чтения описи базы: оно идёт в потоке данных, а не здесь."""
    for _ in range(rounds):
        task = port._history.facts
        if task is not None and task.done():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("опись базы не прочиталась за отведённое время")


async def _settle(port: HistoryPort, rounds: int = 200) -> None:
    """Дождаться конца загрузки, не завися от её длительности."""
    for _ in range(rounds):
        task = port._history.load
        if task is not None and task.done():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("загрузка не закончилась за отведённое время")


# ===========================================================================
# Окно: кнопка есть, ответ приходит всегда
# ===========================================================================

def _window(qapp, monkeypatch):
    """Главное окно на записывающем порту, без торговой части."""
    from helpers import RecordingPort
    from ui import backend
    from ui.main_window import MainWindow

    monkeypatch.setattr(backend, "current", lambda: None)
    port = RecordingPort()
    window = MainWindow(port=port, sanitize=redact)
    window._timer.stop()
    return window, port


def test_the_menu_has_the_load_button_and_it_asks_the_port(qapp, monkeypatch) -> None:
    """Пункт меню есть, и он спрашивает у порта опись базы по инструменту.

    Это единственный путь к свечам для того, у кого нет консоли, — то есть
    для владельца счёта. Кнопка, не дошедшая до порта, выглядит как
    неработающая программа.
    """
    window, port = _window(qapp, monkeypatch)
    try:
        titles = [action.text() for action in window.program_menu.actions()]
        assert any("сторию" in title for title in titles), (
            f"в меню «Программа» нет загрузки истории: {titles}"
        )
        window.history_action.trigger()
        qapp.processEvents()
        assert ("request_history_facts", "MXU6") in port.calls, (
            f"нажатие не дошло до порта: {port.calls}"
        )
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_the_answer_is_shown_even_when_the_load_failed(qapp, monkeypatch) -> None:
    """Итог загрузки показывается всегда — и на отказе тоже.

    Молчание после долгого ожидания читается как «ничего не произошло»,
    и человек жмёт кнопку второй раз. Отдельная проверка, потому что соблазн
    показывать окно только на успехе велик.
    """
    from PySide6.QtWidgets import QMessageBox

    window, _ = _window(qapp, monkeypatch)
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox, "exec",
        lambda box: shown.append((box.text(), box.informativeText())) or 0,
    )
    try:
        window.show_history_result(HistoryLoadOutcome(
            symbol="MXU6", headline="Загрузка не удалась",
            trouble="Биржа не ответила.",
        ))
        qapp.processEvents()
        assert shown, "на отказе окно не сказало ничего"
        headline, body = shown[-1]
        assert "не удалась" in headline
        assert "не ответила" in body
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_the_progress_bar_closes_when_the_load_ends(qapp, monkeypatch) -> None:
    """Полоска хода закрывается концом загрузки, а не остаётся навсегда.

    Закрывает её `history_finished`, который приходит и на успехе, и на
    отказе, и на отмене. Закрытие «по отчёту» оставило бы полоску на экране
    в тех случаях, где отчёта нет.
    """
    from PySide6.QtWidgets import QMessageBox

    window, _ = _window(qapp, monkeypatch)
    monkeypatch.setattr(QMessageBox, "exec", lambda box: 0)
    try:
        window._start_loading("MXU6")
        assert window._loading is not None
        assert not window.history_action.isEnabled(), (
            "кнопка загрузки осталась нажимаемой во время загрузки"
        )
        window.show_history_result(HistoryLoadOutcome(symbol="MXU6", ok=True))
        qapp.processEvents()
        assert window._loading is None, "полоска хода осталась на экране"
        assert window.history_action.isEnabled()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_cancelling_the_progress_bar_reaches_the_port(qapp, monkeypatch) -> None:
    """Кнопка «Отменить» на полоске доходит до порта.

    Без отмены девяностодневная загрузка держала бы окно модальным до конца,
    и единственным выходом был бы снятый процесс — вместе с открытой позицией.
    """
    window, port = _window(qapp, monkeypatch)
    try:
        window._start_loading("MXU6")
        assert window._loading is not None
        window._loading.canceled.emit()
        qapp.processEvents()
        assert ("cancel_history_load", None) in port.calls, (
            f"отмена не дошла до порта: {port.calls}"
        )
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


# ===========================================================================
# Модальное окно, открытое сигналом порта, не рвёт соседнюю задачу
# ===========================================================================

class _NestedLoopDialog:
    """Подставной диалог: `exec()` поднимает **вложенный цикл событий Qt**.

    Ровно это и делает настоящий модальный диалог, и ровно в этом беда:
    под `qasync` цикл Qt и есть цикл asyncio, поэтому во вложенном цикле
    шагают соседние задачи. Подставной вместо настоящего взят затем, чтобы
    тест не зависел ни от полей окна прогона, ни от того, что там нажато.
    """

    opened = 0

    def __init__(self, *_: object, **__: object) -> None:
        type(self).opened += 1

    def exec(self) -> int:
        from PySide6.QtCore import QEventLoop, QTimer
        from PySide6.QtWidgets import QDialog

        inner = QEventLoop()
        QTimer.singleShot(30, inner.quit)
        inner.exec()
        return int(QDialog.DialogCode.Rejected)

    def request(self) -> None:
        return None

    def deleteLater(self) -> None:
        return None


def test_a_modal_opened_by_a_port_signal_does_not_kill_a_running_task(
    loop, qapp, monkeypatch
) -> None:
    """Модальное окно, открытое по сигналу порта, не рвёт соседнюю задачу.

    Поймано живьём 06.09.2026 (`B-026`): владелец счёта запустил прогон
    по истории, окно встало насмерть, в журнале —

        RuntimeError: Cannot enter into task <Task HistoryPort._refresh()>
        while another task <Task HistoryPort._backtest_options()>
        is being executed.

    Три звена: порт испускает сигнал прямым `emit` из главного потока, значит
    обработчик исполняется **внутри шага корутины**; обработчик открывает
    модальное окно; `exec()` поднимает вложенный цикл событий, в котором
    asyncio пытается шагнуть соседней задачей — и отказывает. Задача умирает,
    диалог данных не получает, окно стоит.

    ⚠️ Сторож «диалог открылся» этого не ловит: он зелен и на сломанном коде,
    потому что при простое соседней задачи просто нет. Ломать надо **при
    работающей** задаче — поэтому здесь рядом крутится своя.

    Мутация: вернуть в `show_backtest_dialog` прямой вызов открытия вместо
    `_open_modal` — тест обязан покраснеть.
    """
    from helpers import RecordingPort
    from ui import backend
    from ui.main_window import MainWindow
    from ui.models import BacktestOptions

    monkeypatch.setattr(backend, "current", lambda: None)
    monkeypatch.setattr("ui.main_window.BacktestDialog", _NestedLoopDialog)
    _NestedLoopDialog.opened = 0

    port = RecordingPort()
    window = MainWindow(port=port, sanitize=redact)
    window._timer.stop()

    trouble: list[dict] = []
    loop.set_exception_handler(lambda _loop, context: trouble.append(context))

    async def busy() -> str:
        """Соседняя задача — та самая, которую в бою рвало на куски."""
        for _ in range(30):
            await asyncio.sleep(0.005)
        return "дошла до конца"

    async def main() -> asyncio.Task[str]:
        neighbour = asyncio.ensure_future(busy())
        await asyncio.sleep(0.01)  # дать соседке начать
        # Испускается изнутри работающей задачи — как это делает порт
        # (`HistoryPort._backtest_options`).
        port.backtest_options_ready.emit(BacktestOptions())
        await asyncio.sleep(0.5)
        return neighbour

    try:
        neighbour = loop.run_until_complete(main())
        assert _NestedLoopDialog.opened == 1, (
            "диалог так и не открылся — тест проверяет не то, что написано"
        )
        assert not trouble, (
            "цикл событий сообщил об отказе, пока было открыто модальное окно: "
            + "; ".join(str(item.get("message")) for item in trouble)
        )
        assert neighbour.done(), (
            "соседняя задача не дошла до конца: её шаг сорвался внутри "
            "вложенного цикла модального окна"
        )
        assert neighbour.result() == "дошла до конца"
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


# ===========================================================================
# Слой данных: «не тронем» значит не тронули
# ===========================================================================

def test_a_plain_load_does_not_re_ask_the_days_already_settled(tmp_path) -> None:
    """Догрузка не перезапрашивает дни, отмеченные загруженными.

    Это ровно то, что обещает диалог. Обещание, разошедшееся с кодом, хуже
    отсутствующего: владелец счёта откажется от нужной загрузки или,
    наоборот, потеряет данные, которым доверял.
    """
    path = tmp_path / "marks.sqlite3"
    day = date(2026, 6, 10)
    with CandleStore(path) as store:
        store.mark_days_requested(
            "MXU6", [day], counts={day: 60},
            now=datetime.combine(day + timedelta(days=1), time(0, 0), MSK),
        )
        assert store.days_to_request("MXU6", day, day) == [], (
            "день не отмечен — проверка вакуумна"
        )
        forgotten = store.forget_day_marks("MXU6", day, day)
        assert forgotten == 1
        assert store.days_to_request("MXU6", day, day) == [day], (
            "снятие отметок не вернуло день в запрос — «заново» ничего не сделает"
        )


def test_forgetting_the_day_marks_does_not_delete_a_single_candle(tmp_path) -> None:
    """Снятие отметок не трогает свечи. Худшее последствие — лишний запрос.

    Отдельная проверка, потому что это единственное место, где кнопка
    владельца счёта что-то стирает в базе. Стирается **учёт запросов**,
    а не история: пути удаления настоящих свечей в программе нет вовсе.
    """
    path = tmp_path / "candles.sqlite3"
    day = date(2026, 6, 10)
    with CandleStore(path) as store:
        store.put_minutes("MXU6", session(day, 60), Source.ISS)
        before = store.coverage("MXU6").count
        store.forget_day_marks("MXU6", day - timedelta(days=30), day)
        assert store.coverage("MXU6").count == before, (
            "снятие отметок унесло свечи — этого обещания диалог не давал"
        )


def test_the_reload_forgets_the_marks_and_the_plain_load_does_not(
    loop, tmp_path
) -> None:
    """Разница двух вариантов проверяется тем, что уехало на биржу.

    День, отмеченный загруженным, обычная догрузка **не запрашивает вовсе** —
    ровно это диалог и обещает. «Заново» снимает отметку, и день попадает
    в запрос. Проверяется не текст, а границы запросов, ушедших к бирже:
    текст, разошедшийся с поведением, хуже отсутствующего.
    """
    path = tmp_path / "two-ways.sqlite3"
    day = TODAY - timedelta(days=2)
    with CandleStore(path) as store:
        store.mark_days_requested(
            "MXU6", [day], counts={day: 60},
            now=datetime.combine(TODAY, time(0, 0), MSK),
        )
    handler, asked = picky_exchange(alive_borders(TODAY), session(day, 60))

    async def plain(port, worker) -> None:
        port.load_history(HistoryLoadRequest(symbol="MXU6", days=5))
        await _settle(port)

    _run(loop, path, handler, plain)
    assert asked, "к бирже не ушло ни одного запроса — проверка вакуумна"
    assert not any(since <= day <= until for since, until in asked), (
        f"обычная догрузка перезапросила уже отмеченный день: {asked}"
    )
    with CandleStore(path) as store:
        assert store.coverage("MXU6").count == 0, (
            "обычная догрузка притащила свечи дня, который просила не трогать"
        )
        assert day in store.settled_days("MXU6")

    handler, asked = picky_exchange(alive_borders(TODAY), session(day, 60))

    async def again(port, worker) -> None:
        port.load_history(
            HistoryLoadRequest(symbol="MXU6", days=5, since=day, replace=True)
        )
        await _settle(port)

    _run(loop, path, handler, again)
    assert any(since <= day <= until for since, until in asked), (
        f"«заново» не перезапросило отмеченный день: {asked}"
    )
    with CandleStore(path) as store:
        assert store.coverage("MXU6").count == 60, (
            "«заново» не принесло свечей перезапрошенного дня"
        )
