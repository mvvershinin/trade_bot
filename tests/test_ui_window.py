"""Окно собирается, показывает данные и отвечает на действия пользователя.

Проверяется то, что ломается молча: вкладки на месте, график принял данные
и что-то нарисовал, масштабирование меняет вид, предупреждения при опасных
действиях появляются, а команда до движка доходит.

Все данные синтетические (`tests/synthetic.py`), сети нет, движка нет.
"""

from __future__ import annotations

import re

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QMessageBox

import synthetic
from helpers import RecordingPort
from market import redact
from ui.chart.painter_surface import PainterChartSurface
from ui.main_window import STREAM_BUTTON, MainWindow
from ui.models import Connection, Mode, Position, RobotState, Side, TakeGuard
from ui.ports import TerminalPort

#: Тысячи в этой программе разделены **неразрывным** пробелом (`ui/formatting.py`).
NBSP = "\u00a0"

#: Уровень тейка у позиции из `synthetic.state()`. Вынесен сюда, чтобы тесты
#: проверяли то самое число, которое видит владелец счёта, а не «какое-нибудь».
ARMED_LEVEL = f"284{NBSP}400"


@pytest.fixture()
def window(qapp):
    port = RecordingPort()
    win = MainWindow(port=port, sanitize=redact)
    win.resize(1280, 860)
    yield win
    win._timer.stop()
    # Позиция снимается до закрытия намеренно: при открытой позиции окно
    # показывает модальное предупреждение и ждёт человека — в прогоне тестов
    # это выглядит как зависший pytest. Перехват закрытия проверяется
    # отдельным тестом, а не разбором фикстуры.
    win.apply_state(RobotState())
    win.close()
    win.deleteLater()
    qapp.processEvents()


def qapp_of(widget):
    """Приложение Qt, которому принадлежит виджет.

    Нужно там, где тест меняет системную палитру по-настоящему: событие
    доставляется только через `QApplication`, а не подменой темы в объекте.
    """
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    assert app is not None, "приложения Qt нет — тест не в той обвязке"
    return app


def test_window_has_status_chart_and_two_journals(window) -> None:
    assert window.status_panel is not None
    assert window.chart is not None
    tabs = window.journals.tabs
    assert tabs.count() == 2
    assert tabs.tabText(0) == "Сделки"
    assert tabs.tabText(1) == "Решения робота"


def test_version_is_visible(window) -> None:
    """Версия видна в окне — иначе разбор жалобы начинается с гадания о сборке."""
    assert window.windowTitle().startswith("Терминал")
    assert any(char.isdigit() for char in window._clock.text())


def test_chart_draws_something(window, qapp) -> None:
    """График принял свечи и действительно что-то нарисовал.

    Проверяется не «не упало», а картинка: пустой график — это тоже не падение,
    и именно так выглядит отвалившийся отрисовщик.
    """
    window.show_chart(synthetic.chart_data())
    window.show()
    qapp.processEvents()
    image = window.chart._surface.widget().grab().toImage()
    colours = {image.pixel(x, y) for x in range(0, image.width(), 7)
               for y in range(0, image.height(), 7)}
    assert len(colours) > 8, "на графике один цвет — свечи не нарисованы"


def test_wheel_zooms_and_button_returns_to_last(window, qapp) -> None:
    window.show_chart(synthetic.chart_data(count=200))
    surface = window.chart._surface
    assert isinstance(surface, PainterChartSurface)
    window.show()
    qapp.processEvents()

    before = surface._view.span
    centre = QPointF(surface.width() / 2, surface.height() / 2)
    event = QWheelEvent(
        centre, surface.mapToGlobal(centre.toPoint()).toPointF(),
        QPoint(0, 0), QPoint(0, 120), Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False,
    )
    surface.wheelEvent(event)
    assert surface._view.span < before, "колесо не приблизило график"

    surface._view.right -= 30
    surface._view.follow = False
    window.chart.last_button.click()
    assert surface.is_following()


def test_journals_show_rows_and_summary(window) -> None:
    window.set_trades(synthetic.trades(), synthetic.summary())
    window.set_decisions(synthetic.decisions())
    assert window.journals.trades_model.rowCount() == 3
    assert window.journals.decisions_model.rowCount() == len(synthetic.decisions())
    text = window.journals.summary_label.text()
    assert "Чистая прибыль" in text and "Комиссия" in text


def test_decision_column_holds_a_reason_not_a_code(window) -> None:
    """Главное требование к журналу решений: причина словами.

    Строка вида `ERR_INSUFFICIENT_MARGIN` — не сообщение пользователю. Тест ловит
    самый вероятный способ нарушения: код ошибки, проброшенный в колонку причины.
    """
    window.set_decisions(synthetic.decisions())
    model = window.journals.decisions_model
    for row in range(model.rowCount()):
        reason = model.data(model.index(row, 2))
        assert reason and len(reason) > 12, f"причина слишком коротка: {reason!r}"
        assert not reason.isupper(), f"в колонке причины код, а не фраза: {reason!r}"
        assert " " in reason.strip(), f"в колонке причины одно слово: {reason!r}"


def test_switching_to_off_with_open_position_asks_first(window, monkeypatch) -> None:
    """Выключение при открытой позиции не проходит молча (DOMAIN.md §3)."""
    window.apply_state(synthetic.state())
    asked: list[str] = []

    def refuse(parent, title, text, *args, **kwargs):
        asked.append(text)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(refuse))
    window.mode_box.setCurrentIndex(window.mode_box.findData(Mode.OFF))

    assert asked, "переключение в «Выключен» прошло без предупреждения"
    assert "без присмотра" in asked[0] or "не закрывает" in asked[0]
    assert ("set_mode", Mode.OFF) not in window.port.calls
    assert window.mode_box.currentData() is not Mode.OFF, "список не вернулся к прежнему режиму"


def test_switching_to_off_confirmed_sends_command(window, monkeypatch) -> None:
    window.apply_state(synthetic.state())
    monkeypatch.setattr(
        QMessageBox, "warning",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    window.mode_box.setCurrentIndex(window.mode_box.findData(Mode.OFF))
    assert ("set_mode", Mode.OFF) in window.port.calls


def test_closing_with_open_position_is_intercepted(window, monkeypatch) -> None:
    window.apply_state(synthetic.state())
    monkeypatch.setattr(
        QMessageBox, "warning",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.No),
    )
    assert window.close() is False, "окно закрылось при открытой позиции без вопроса"


def test_off_mode_with_position_shows_big_warning(window) -> None:
    window.apply_state(synthetic.state(mode=Mode.OFF))
    assert window.mode_banner.isVisibleTo(window)
    assert "без присмотра" in window.mode_banner.text()


def test_off_banner_names_the_level_left_with_the_broker(window) -> None:
    """Плашка называет уровень, который остаётся жить у брокера.

    До правки 31.08.2026 она утверждала обратное: «ни тейк, ни переворот
    к ней больше не применяются». Тейк-профит — заявка **у брокера**, и режим
    «Выключен» её не снимает (решение 0008). Поверив плашке, владелец счёта
    поставит свой стоп и получит два.

    ⚠️ Режим здесь боевой, и это не мелочь. В симуляции у брокера нет ни
    позиции, ни заявки, и звать туда нельзя — текст другой, он проверяется
    отдельно (`test_simulation_does_not_send_anyone_to_the_broker`).
    """
    window.apply_state(synthetic.state(mode=Mode.OFF, simulation=False))
    text = window.mode_banner.text()
    assert ARMED_LEVEL in text, f"уровень не назван: {text!r}"
    assert "остаётся у брокера" in text
    assert "приложении брокера" in text


def test_off_modal_names_the_level_left_with_the_broker(window, monkeypatch) -> None:
    """То же в окне подтверждения — там, где решение и принимается."""
    window.apply_state(synthetic.state(simulation=False))
    shown: list[str] = []

    def refuse(parent, title, text, *args, **kwargs):
        shown.append(text)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(refuse))
    window.mode_box.setCurrentIndex(window.mode_box.findData(Mode.OFF))

    assert shown, "предупреждения не было"
    assert ARMED_LEVEL in shown[0], f"уровень не назван: {shown[0]!r}"
    assert "остаётся у брокера" in shown[0]


def test_close_modal_names_the_level_left_with_the_broker(window, monkeypatch) -> None:
    """Закрытие программы вооружённый тейк тоже не снимает."""
    window.apply_state(synthetic.state(simulation=False))
    shown: list[str] = []

    def refuse(parent, title, text, *args, **kwargs):
        shown.append(text)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(refuse))
    window.close()

    assert shown, "окно закрылось без вопроса"
    assert ARMED_LEVEL in shown[0], f"уровень не назван: {shown[0]!r}"
    assert "остаётся у брокера" in shown[0]


def test_no_warning_claims_the_take_stops_working(window, monkeypatch) -> None:
    """Все три текста собраны вместе и проверены на прежнее обещание.

    Проверка нарочно охватывает три места одним тестом: дефект был именно
    в том, что одно и то же утверждение жило в трёх копиях и разошлось
    с движком во всех трёх сразу.
    """
    shown: list[str] = []

    def refuse(parent, title, text, *args, **kwargs):
        shown.append(text)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(refuse))

    window.apply_state(synthetic.state())
    window.mode_box.setCurrentIndex(window.mode_box.findData(Mode.OFF))
    window.close()
    window.apply_state(synthetic.state(mode=Mode.OFF))
    shown.append(window.mode_banner.text())

    assert len(shown) == 3, f"собраны не все три текста: {len(shown)}"
    for text in shown:
        lowered = text.lower()
        assert "ни тейк" not in lowered, f"вернулось прежнее обещание: {text!r}"
        assert "не сработают" not in lowered, f"вернулось прежнее обещание: {text!r}"
        assert "работать не будут" not in lowered, f"вернулось прежнее обещание: {text!r}"


def test_stop_with_an_open_position_asks_the_same_question(window, monkeypatch) -> None:
    """«Стоп» при открытой позиции предупреждает так же, как «Выключен».

    Последствие одинаковое: остановленный робот позицию не переворачивает
    и по концу окна не закрывает. Прежде «Выключен» давал модальное окно
    и красную плашку, а «Стоп» — только подсказку под мышью. Разная громкость
    у одинаковых последствий читается как «здесь опасно, а здесь нет».
    """
    window.apply_state(synthetic.state(simulation=False))
    asked: list[str] = []

    def refuse(parent, title, text, *args, **kwargs):
        asked.append(text)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(refuse))
    window.stop_action.trigger()

    assert asked, "«Стоп» при открытой позиции прошёл молча"
    assert ARMED_LEVEL in asked[0], "вопрос не называет уровень у брокера"
    assert ("stop", None) not in window.port.calls, "команда ушла вопреки отказу"


def test_stop_confirmed_reaches_the_engine(window, monkeypatch) -> None:
    monkeypatch.setattr(
        QMessageBox, "warning",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    window.apply_state(synthetic.state())
    window.stop_action.trigger()
    assert ("stop", None) in window.port.calls


def test_stop_without_a_position_does_not_ask(window, monkeypatch) -> None:
    """Лишний вопрос на безопасном действии со временем перестают читать."""
    asked: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning",
        staticmethod(lambda parent, title, text, *a, **k: asked.append(text)),
    )
    window.apply_state(synthetic.state(position=None))
    window.stop_action.trigger()
    assert not asked
    assert ("stop", None) in window.port.calls


def test_the_mode_hint_says_nothing_about_the_position(window) -> None:
    """Подсказка режима — про режим. Про позицию говорит только `guard_note`.

    Она показывается вплотную к ячейке позиции и позиции не видит. Прежний
    текст пугал уровнем у брокера там, где тейк выключен (рабочая
    конфигурация), и успокаивал там, где у брокера висит заявка на выход.
    """
    hint = Mode.OFF.hint.lower()
    for claim in ("тейк", "уровень", "у брокера", "сработать"):
        assert claim not in hint, f"подсказка режима говорит про позицию: {hint!r}"

    window.apply_state(synthetic.state(mode=Mode.OFF, position=Position(
        Side.LONG, 1, 210_000.0, take=TakeGuard.NONE)))
    assert "Выставленного уровня выхода у этой позиции нет" in (
        window.status_panel.position.value.toolTip()
    )


def test_unknown_take_state_does_not_claim_there_is_no_stop(window) -> None:
    """Позиция без сведений о тейке: окно не утверждает, что стопа нет.

    Так будет выглядеть забытое поле в `app/`. Умолчание обязано быть
    безопасным: «не знаю» вместо «нет».
    """
    bare = Position(Side.SHORT, 1, 285_828.0)
    assert bare.take is TakeGuard.UNKNOWN
    window.apply_state(synthetic.state(mode=Mode.OFF, position=bare))
    text = window.mode_banner.text()
    assert "не считайте, что её нет" in text, f"окно что-то утверждает: {text!r}"


def test_halt_is_shown_big_and_names_the_reason(window) -> None:
    """Остановка робота видна крупно, а не только в журнале.

    `engine/runner.py` прямо назначает эту обязанность окну: у одной
    из остановок — отказ приёмника журнала — причина не существует больше
    нигде, потому что отказал сам журнал.
    """
    reason = "Сделка не сопоставлена ни с одной заявкой в полёте"
    window.apply_state(synthetic.state(halted=reason))
    assert window.halt_banner.isVisibleTo(window), "остановка робота не видна"
    assert reason in window.halt_banner.text()
    assert "остановлен" in window.halt_banner.text().lower()


def test_halt_is_not_confused_with_a_robot_that_is_simply_idle(window) -> None:
    """«Остановлен» и «стоит» — разные состояния, и панель их различает."""
    window.apply_state(synthetic.state(running=False))
    idle = window.status_panel.mode.value.text()
    assert "робот стоит" in idle
    assert "ОСТАНОВЛЕН" not in idle
    assert not window.halt_banner.isVisibleTo(window)

    window.apply_state(synthetic.state(running=False, halted="Исполнитель не принял заявку"))
    halted = window.status_panel.mode.value.text()
    assert "ОСТАНОВЛЕН" in halted, f"остановка неотличима от простоя: {halted!r}"
    assert "Исполнитель не принял заявку" in window.status_panel.mode.value.toolTip()


def test_halt_with_open_position_points_at_the_orders(window) -> None:
    """Остановка с позицией зовёт проверить и позицию, и список заявок.

    Осиротевший стоп переживает остановку: звать проверить только позицию
    недостаточно.
    """
    window.apply_state(synthetic.state(
        halted="Выход из лонга не удаётся 3-й раз", simulation=False))
    text = window.halt_banner.text()
    assert "список заявок" in text
    assert ARMED_LEVEL in text


def test_start_button_does_not_promise_recovery_while_halted(window) -> None:
    """«Старт» на остановленном роботе не выглядит кнопкой «починить».

    Гасить её окно не вправе — что делает «Старт» с остановленным роботом,
    решает не оно. Но и молчать нельзя: владелец счёта нажмёт её первой.
    """
    window.apply_state(synthetic.state(running=False, halted="Исполнитель не принял заявку"))
    tip = window.start_action.toolTip()
    assert "остановлен" in tip.lower()
    assert "сам он из этого состояния не выйдет" in tip


def test_halt_banner_replaces_the_mode_banner_not_stacks_with_it(window) -> None:
    """Одну и ту же позицию не описывают двумя красными абзацами подряд.

    Плашка остановки говорит про позицию всё то же, что плашка режима,
    и вдобавок называет причину. Повторённая тревога перестаёт читаться.
    """
    window.apply_state(synthetic.state(mode=Mode.OFF, halted="Исполнитель не принял заявку"))
    assert window.halt_banner.isVisibleTo(window)
    assert not window.mode_banner.isVisibleTo(window)
    # Ничего при этом не потерялось: уровень и указание на брокера на месте.
    assert ARMED_LEVEL in window.halt_banner.text()

    window.apply_state(synthetic.state(mode=Mode.OFF))
    assert window.mode_banner.isVisibleTo(window)


def test_halt_banner_disappears_when_the_robot_recovers(window) -> None:
    window.apply_state(synthetic.state(halted="Журнал решений не принял строку"))
    assert window.halt_banner.isVisibleTo(window)
    window.apply_state(synthetic.state())
    assert not window.halt_banner.isVisibleTo(window)


def test_banner_text_is_readable_on_its_own_background(window) -> None:
    """Крупное предупреждение, которое нельзя прочитать, — это его отсутствие.

    Плашки рисовались белым по заливке безусловно. В тёмной теме `warning` —
    светлый оранжевый `#ffb74d`, и «Токен брокера истекает через 6 дн.»
    получало контраст **1,73:1**.

    Про порог 3:1 и почему он здесь законен
    ---------------------------------------
    WCAG считает текст крупным начиная с 18 pt обычного или **14 pt
    полужирного** и требует для него 3:1 вместо 4,5:1. Прежнее обоснование
    («текст крупный и жирный») **не выдерживалось**: шрифт плашки был
    «системный + 2 pt», то есть 11–12 pt, и формально требовалось 4,5:1.
    Обоснование, которое не выдерживается, хуже отсутствующего: при следующей
    правке палитры оно разрешит опустить цвет до 3,0 «по правилам».

    Поэтому кегль плашки поднят до 14 pt (`_Banner.LARGE_POINTS`), и порог
    ниже проверяется вместе с ним. Развяжутся — тест упадёт.
    """
    from ui.main_window import _Banner
    from ui.theme import DARK, INK, LIGHT, contrast, text_on

    # ⚠️ Сначала — само основание порога. Опустят кегль обратно, и 3:1
    # перестанет быть законным раньше, чем это заметят по цветам.
    assert _Banner.LARGE_POINTS >= 14.0, (
        "кегль плашки опустили ниже границы «крупного» по WCAG — порог 3:1 "
        "ниже перестал быть обоснованным, нужен 4,5:1"
    )
    assert window.token_banner.font().bold(), "плашка перестала быть полужирной"
    assert window.token_banner.font().pointSizeF() >= _Banner.LARGE_POINTS, (
        "шрифт плашки меньше объявленного: "
        f"{window.token_banner.font().pointSizeF()}"
    )

    worst = 21.0
    for theme in (LIGHT, DARK):
        for background in (theme.danger, theme.warning):
            ink = text_on(background)
            ratio = contrast(ink, background)
            worst = min(worst, ratio)
            assert ratio >= 3.0, (
                f"надпись на заливке {background} нечитаема: контраст {ratio:.2f}"
            )
            assert ink in ("#ffffff", INK)

    # ⚠️ Храповик, а не круглое число. Сегодня худшее сочетание — тёмная
    # надпись на светлом `warning` (`#b26a00`), 4,36:1. До 4,5:1 оно
    # не дотягивает, и упереться в это мешает сама палитра: с белым тот же
    # цвет даёт 4,24:1, то есть лучшего выбора надписи не существует —
    # нужен другой `warning`, а он же красит заливку пауз на графике.
    # Порог поставлен под текущее значение: любая правка палитры, которая
    # ухудшит его, обязана этот тест уронить.
    assert worst >= 4.3, (
        f"худшее сочетание просело до {worst:.2f} — было 4,36. Ниже 4,5:1 "
        "мы и так живём вынужденно, дальше опускаться некуда"
    )


def test_the_contrast_formula_matches_wcag_on_known_answers() -> None:
    """`contrast` считает по WCAG, а не «примерно так же».

    Прежде на месте этой проверки стояло `ratio >= contrast("#ffffff", bg)` —
    тавтология: `text_on` и есть максимум по этому же критерию, и упасть
    такое не могло ни на какой палитре. Настоящий сторож — известные ответы,
    посчитанные вне этого кода.
    """
    from ui.theme import contrast

    assert contrast("#ffffff", "#000000") == pytest.approx(21.0)
    assert contrast("#ffffff", "#ffffff") == pytest.approx(1.0)
    # Пограничные цвета из самой рекомендации WCAG: `#767676` — самый тёмный
    # серый, ещё дающий 4,5:1 на белом, `#949494` — на чёрном.
    assert contrast("#767676", "#ffffff") == pytest.approx(4.54, abs=0.01)
    assert contrast("#949494", "#000000") == pytest.approx(6.92, abs=0.01)
    # Гамма-коррекция на месте: без неё насыщенный оранжевый уезжает,
    # а это ровно цвет предупреждений.
    assert contrast("#ffffff", "#ffb74d") == pytest.approx(1.73, abs=0.01)
    assert contrast("#ffffff", "#ef5350") == pytest.approx(3.49, abs=0.01)


def test_text_on_picks_the_readable_ink_not_the_habitual_one() -> None:
    """Известные ответы для `text_on` — те, где привычный белый неверен."""
    from ui.theme import DARK, INK, LIGHT, text_on

    assert text_on(DARK.warning) == INK, "белым по светлому оранжевому — 1,73:1"
    assert text_on(DARK.danger) == INK, "белым по #ef5350 — 3,49:1 против 5,31:1"
    assert text_on(DARK.level_take) == INK, "белым по #66bb6a — 2,36:1"
    assert text_on(DARK.level_trailing) == INK, "белым по #4dd0e1 — 1,84:1"
    assert text_on(LIGHT.danger) == "#ffffff", "тёмным по #c62828 — 3,29:1"
    assert text_on(LIGHT.level_entry) == "#ffffff"


def test_an_invalid_colour_gets_dark_ink_not_white() -> None:
    """Негодная строка цвета: белая надпись исчезла бы совсем.

    Qt на такой строке возвращает `QColor` с нулями, то есть «чёрный», и
    сравнение контрастов честно выбирает белый. Но заливка при этом
    не применяется вовсе — Qt молча выбрасывает негодное правило таблицы
    стилей, и фон остаётся системным. В светлой теме он светлый, и белая
    надпись на нём пропадает.
    """
    from ui.theme import INK, text_on

    for broken in ("", "не цвет", "#gg0000", "rgb(", "#12345"):
        assert text_on(broken) == INK, f"на негодном цвете {broken!r} выбран белый"


def test_the_live_badge_reads_at_least_as_well_as_the_simulation_one(window) -> None:
    """Отметка боевого режима не должна читаться хуже отметки симуляции.

    Спутать симуляцию с боем нельзя (ТЗ §4.4 З), а отметка боя рисовалась
    белым по `danger` безусловно: в тёмной теме это 3,49:1, тогда как
    «СИМУЛЯЦИЯ» идёт по `text_dim` и даёт 7,06:1. Безопасное состояние
    читалось лучше опасного.
    """
    from ui.theme import DARK, LIGHT, contrast, text_on

    for theme in (LIGHT, DARK):
        live = contrast(text_on(theme.danger), theme.danger)
        simulated = contrast(theme.background, theme.text_dim)
        assert live >= 4.5, f"отметка боевого режима нечитаема: {live:.2f}"
        assert live >= simulated * 0.75, (
            f"боевой режим читается заметно хуже симуляции: {live:.2f} против "
            f"{simulated:.2f}"
        )

    # ⚠️ Проверка идёт в **тёмной** теме, и это не придирка: в светлой белая
    # надпись на `#c62828` как раз верна (5,62:1), и захардкоженный белый
    # прошёл бы незамеченным. Разница видна только там, где верный ответ
    # не белый.
    from PySide6.QtGui import QColor, QPalette

    original = QPalette(qapp_of(window).palette())
    try:
        dark = QPalette(original)
        dark.setColor(QPalette.ColorRole.Window, QColor("#15181d"))
        qapp_of(window).setPalette(dark)
        qapp_of(window).processEvents()
        assert window.theme is DARK, "тема не переключилась — проверка вакуумна"

        window.apply_state(synthetic.state(simulation=False))
        sheet = window.status_panel.regime.styleSheet()
        assert f"color: {text_on(DARK.danger)}" in sheet, (
            f"цвет надписи не считается по заливке: {sheet!r}"
        )
        assert "#ffffff" not in sheet, f"в тёмной теме отметка снова белая: {sheet!r}"
    finally:
        qapp_of(window).setPalette(original)
        qapp_of(window).processEvents()
    assert window.theme is LIGHT, "тема не вернулась — фикстура протекла"


def test_theme_change_repaints_what_is_already_on_screen(window, qapp) -> None:
    """Смена темы на ходу перерисовывает уже показанную плашку.

    `apply_theme()` обещал это докстрингом, но вызывался ровно один раз —
    при старте. Плашка, показанная до переключения, оставалась в цветах
    прежней темы, а надпись на ней могла стать нечитаемой.

    Тест меняет палитру приложения по-настоящему, а не шлёт событие руками:
    `QApplication.setPalette()` доставляет окну `PaletteChange`, а вовсе
    не `ApplicationPaletteChange`, и проверка событием мимо кассы прошла бы
    на сломанном коде.
    """
    from PySide6.QtGui import QColor, QPalette

    from ui.theme import DARK, LIGHT

    window.apply_state(synthetic.state(token_days_left=6))
    assert window.token_banner.isVisibleTo(window)
    assert LIGHT.warning in window.token_banner.styleSheet()

    original = QPalette(qapp.palette())
    try:
        dark = QPalette(original)
        dark.setColor(QPalette.ColorRole.Window, QColor("#15181d"))
        qapp.setPalette(dark)
        qapp.processEvents()

        assert window.theme.dark, "смена темы не перехвачена"
        assert DARK.warning in window.token_banner.styleSheet(), (
            "плашка осталась в цветах прежней темы"
        )
    finally:
        qapp.setPalette(original)
        qapp.processEvents()

    assert not window.theme.dark, "тема не вернулась — фикстура протекла"


def test_read_only_token_blocks_start_with_explanation(window) -> None:
    window.apply_state(synthetic.state(token_read_only=True, running=False))
    assert not window.start_action.isEnabled()
    assert "только на чтение" in window.start_action.toolTip()


def test_expiring_token_is_shouted_not_whispered(window) -> None:
    window.apply_state(synthetic.state(token_days_left=6))
    assert window.token_banner.isVisibleTo(window)
    assert "6" in window.token_banner.text()
    window.apply_state(synthetic.state(token_days_left=40))
    assert not window.token_banner.isVisibleTo(window)


def test_live_mode_is_marked_red(window) -> None:
    window.apply_state(synthetic.state(simulation=False))
    assert "БОЕВОЙ" in window.status_panel.regime.text()
    assert "БОЕВОЙ РЕЖИМ" in window.windowTitle()
    window.apply_state(synthetic.state(simulation=True))
    assert "СИМУЛЯЦИЯ" in window.status_panel.regime.text()


def test_port_signals_reach_the_window(window) -> None:
    """Событие движка доходит до окна через порт, а не прямым вызовом."""
    window.port.chart_replaced.emit(synthetic.chart_data(count=30))
    window.port.trades_replaced.emit(tuple(synthetic.trades()), synthetic.summary())
    window.port.decision_appended.emit(synthetic.decisions()[0])
    window.port.state_changed.emit(synthetic.state())
    assert window.journals.trades_model.rowCount() == 3
    assert window.journals.decisions_model.rowCount() == 1
    assert window.status_panel.instrument.value.text().startswith("MXU6")


# ==========================================================================
# Подсказка ячейки «Позиция»: обстановка, а не один текст на все случаи
# ==========================================================================

def test_the_position_tooltip_does_not_lie_to_a_working_robot(window) -> None:
    """BLOCK 31.08.2026. Панель звала `guard_note` **безусловно**.

    `guard_note` написан для обстановки «робот перестал управлять» — это
    сказано в его же докстринге, — а признака режима ему никто не передавал.
    Рабочая конфигурация с выключенным тейком (ею сделаны обе конфигурации-
    сторожа сверки, PROTOTYPE.md §1): движок отдаёт `TakeGuard.NONE`, режим
    «Лонг и шорт (переворот)», робот работает. Владелец счёта наводил мышь
    на «Позицию» и читал «сторожить у брокера нечего, и робот её не закроет».

    В этой конфигурации переворот по средней — единственное, что позицию
    закрывает, и он закроет её на ближайшем обратном сигнале. Поверив окну,
    владелец счёта закрывает позицию руками у брокера. Сверки позиции
    с брокером в движке нет, движок продолжает считать позицию открытой
    и на следующем обратном сигнале подаёт рыночный выход — заявка
    **открывает позицию в обратную сторону на полный объём**, без сигнала
    и без строки в журнале.
    """
    window.apply_state(synthetic.state(
        mode=Mode.REVERSE, running=True, simulation=False,
        position=Position(Side.LONG, 1, 210_000.0, take=TakeGuard.NONE),
    ))
    tip = window.status_panel.position.value.toolTip()
    assert "робот её не закроет" not in tip, f"работающему роботу солгали: {tip!r}"
    assert "Выставленного уровня выхода у этой позиции нет" in tip, (
        f"безусловная половина утверждения пропала: {tip!r}"
    )


def test_the_position_tooltip_does_not_invite_cancelling_a_live_take(window) -> None:
    """Вторая половина: «снять её можно в приложении брокера» на работающем роботе.

    Снятой руками заявки движок не заметит — срабатывание тейка приходит ему
    только сделкой (решение 0008) — и будет считать сторожа живым, пока
    позиция идёт без уровня.
    """
    window.apply_state(synthetic.state(mode=Mode.REVERSE, running=True, simulation=False))
    tip = window.status_panel.position.value.toolTip()
    assert "приложении брокера" not in tip, f"позвали снимать заявку: {tip!r}"
    assert "остаётся у брокера" in tip, f"про живой уровень умолчали: {tip!r}"
    assert ARMED_LEVEL in tip


def test_the_position_tooltip_says_both_halves_once_the_robot_stops(window) -> None:
    """Обратная сторона: там, где робот не управляет, обе фразы обязаны быть.

    Иначе правка «просто убрать пугающее» превратилась бы в молчание там,
    где предупреждение и нужно.
    """
    for state in (
        synthetic.state(mode=Mode.OFF, running=True, simulation=False),
        synthetic.state(mode=Mode.REVERSE, running=False, simulation=False),
        synthetic.state(mode=Mode.REVERSE, running=True, simulation=False,
                        halted="Исполнитель не принял заявку"),
    ):
        window.apply_state(state)
        tip = window.status_panel.position.value.toolTip()
        assert "приложении брокера" in tip, (
            f"робот не управляет, а про снятие заявки молчат: {tip!r}"
        )

    window.apply_state(synthetic.state(
        mode=Mode.OFF, running=False, simulation=False,
        position=Position(Side.LONG, 1, 210_000.0, take=TakeGuard.NONE),
    ))
    assert "робот её не закроет" in window.status_panel.position.value.toolTip()


def test_the_position_tooltip_does_not_send_to_the_broker_in_simulation(window) -> None:
    """В симуляции у брокера нет ни позиции, ни заявки — звать туда незачем.

    Разработка и первые недели идут на симуляции. Два похода к брокеру
    впустую обесценивают единственный канал, которым окно говорит про деньги.
    """
    window.apply_state(synthetic.state(mode=Mode.OFF, running=False, simulation=True))
    tip = window.status_panel.position.value.toolTip()
    assert "приложении брокера" not in tip, f"позвали к брокеру зря: {tip!r}"
    assert "только внутри программы" in tip, f"не сказано, где живёт уровень: {tip!r}"
    assert ARMED_LEVEL in tip, "уровень перестал называться числом"


def test_simulation_does_not_send_anyone_to_the_broker(window) -> None:
    """То же в крупных плашках — их читают первыми.

    Красная плашка «остаётся у брокера… проверьте позицию и список заявок
    в приложении брокера» на симулированной позиции — это два похода
    впустую. Довод про обесценивание тревоги приведён в самом окне
    (`_update_banners`): тревога, повторённая дважды, перестаёт быть тревогой.
    """
    window.apply_state(synthetic.state(mode=Mode.OFF, simulation=True))
    banner = window.mode_banner.text()
    assert "приложении брокера" not in banner, f"позвали к брокеру зря: {banner!r}"
    assert "симуляц" in banner.lower(), f"не сказано, почему у брокера пусто: {banner!r}"
    assert ARMED_LEVEL in banner, "уровень перестал называться числом"

    window.apply_state(synthetic.state(halted="Журнал решений не принял строку",
                                       simulation=True))
    halt = window.halt_banner.text()
    assert "приложении брокера" not in halt, f"позвали к брокеру зря: {halt!r}"
    assert "симуляц" in halt.lower()


def test_the_level_chip_number_is_readable_in_the_dark_theme(qapp) -> None:
    """Число уровня на плашке у шкалы цены рисовалось белым безусловно.

    В тёмной теме `level_take` (`#66bb6a`) даёт с белым 2,36:1, а
    `level_trailing` (`#4dd0e1`) — 1,84:1: хуже того случая (1,73), ради
    которого `text_on` и написан. По этому числу владелец счёта находит
    заявку у брокера; нечитаемое число — это его отсутствие.

    Проверка по картинке, а не по исходнику: «не упало» здесь ничего
    не значит, а белая надпись на светло-зелёном — это именно картинка.
    """
    from ui.chart.painter_surface import PainterChartSurface
    from ui.models import LevelKind, PriceLevel
    from ui.theme import DARK

    surface = PainterChartSurface()
    try:
        surface.set_theme(DARK)
        surface.resize(900, 500)
        data = synthetic.chart_data(count=60)
        close = data.candles[-1].close
        surface.show_chart(data)
        surface.set_levels([
            PriceLevel(close, LevelKind.TAKE, "Тейк"),
            PriceLevel(close * 0.999, LevelKind.TRAILING, "Скользящий тейк"),
            PriceLevel(close * 1.001, LevelKind.ENTRY, "Цена входа"),
        ])
        qapp.processEvents()
        image = surface.grab().toImage()

        # В тёмной теме самый светлый штатный цвет — `text` (#e6e9ef, 239).
        # Пиксели ярче 240 по всем каналам взяться могут только из белой
        # надписи на плашке уровня.
        white = sum(
            1
            for x in range(image.width())
            for y in range(0, image.height(), 2)
            if min(image.pixelColor(x, y).red(), image.pixelColor(x, y).green(),
                   image.pixelColor(x, y).blue()) > 240
        )
        assert white == 0, (
            f"на графике {white} белых пикселей — надпись на плашке уровня "
            "снова белая, контраст 1,8–2,4:1"
        )
    finally:
        surface.deleteLater()
        qapp.processEvents()


def test_the_summary_line_follows_the_theme(window, qapp) -> None:
    """Итог под таблицей сделок перекрашивается вместе с темой.

    Его цвет прописан в таблице стилей в момент показа, и `set_theme` его
    не трогал: после переключения на тёмную оставался зелёный светлой темы —
    3,0:1 на тёмном фоне. Это единственная строка с чистой прибылью
    за период.
    """
    from PySide6.QtGui import QColor, QPalette

    from ui.theme import DARK, LIGHT

    window.set_trades(synthetic.trades(), synthetic.summary())
    assert LIGHT.success in window.journals.summary_label.styleSheet()

    original = QPalette(qapp.palette())
    try:
        dark = QPalette(original)
        dark.setColor(QPalette.ColorRole.Window, QColor("#15181d"))
        qapp.setPalette(dark)
        qapp.processEvents()
        assert DARK.success in window.journals.summary_label.styleSheet(), (
            "итог остался в цветах прежней темы"
        )
    finally:
        qapp.setPalette(original)
        qapp.processEvents()


def test_two_palette_events_repaint_the_window_once(window, qapp) -> None:
    """Смена системной темы приносит два события — перерисовка нужна одна.

    При автоматическом переключении приходят и `ApplicationPaletteChange`,
    и `PaletteChange`, оба обрабатывались. Перерисовка тяжёлая: у веб-графика
    она доходит до отрисовщика, и две подряд шли в потоке интерфейса.
    """
    from PySide6.QtCore import QCoreApplication, QEvent as QtEvent
    from PySide6.QtGui import QColor, QPalette

    calls: list[int] = []
    original_apply = window.apply_theme

    def counted() -> None:
        calls.append(1)
        original_apply()

    window.apply_theme = counted
    original = QPalette(qapp.palette())
    try:
        dark = QPalette(original)
        dark.setColor(QPalette.ColorRole.Window, QColor("#15181d"))
        qapp.setPalette(dark)
        qapp.processEvents()
        assert calls, "смена темы не перехвачена вовсе"
        assert len(calls) == 1, f"перерисовок вместо одной: {len(calls)}"

        # Повторные события той же палитры ничего не меняют.
        QCoreApplication.sendEvent(window, QtEvent(QtEvent.Type.ApplicationPaletteChange))
        QCoreApplication.sendEvent(window, QtEvent(QtEvent.Type.PaletteChange))
        assert len(calls) == 1, f"повторное событие перерисовало окно: {len(calls)}"
    finally:
        del window.apply_theme
        qapp.setPalette(original)
        qapp.processEvents()


def test_a_palette_event_before_the_window_is_built_is_ignored(window) -> None:
    """Сторож `changeEvent` смотрит на готовность окна, а не на первую панель.

    Прежний `getattr(self, "status_panel", None)` проверял не тот объект:
    панель создаётся первой, а `apply_theme()` трогает `chart` и `journals`,
    которые появляются позже. В этом промежутке защиты не было вовсе,
    хотя комментарий её обещал.
    """
    from PySide6.QtCore import QCoreApplication, QEvent as QtEvent

    assert window._ready is True, "флаг готовности не выставлен после сборки"

    # Состояние «окно ещё собирается»: панель есть, графика нет.
    window._ready = False
    chart = window.chart
    del window.chart
    try:
        assert window.status_panel is not None, "проверка потеряла смысл"
        QCoreApplication.sendEvent(window, QtEvent(QtEvent.Type.ApplicationPaletteChange))
        QCoreApplication.sendEvent(window, QtEvent(QtEvent.Type.PaletteChange))
    finally:
        window.chart = chart
        window._ready = True


def test_the_banner_does_not_interpret_markup_from_outside(window) -> None:
    """В плашку попадает строка, пришедшая снаружи, — разметке там не место.

    `state.halted` приходит из движка, а до него из ответа брокера. Символ
    `<` в такой строке съел бы половину предупреждения молча: QLabel
    по умолчанию угадывает разметку.
    """
    from PySide6.QtCore import Qt as QtCore

    assert window.halt_banner.textFormat() == QtCore.TextFormat.PlainText
    assert window.token_banner.textFormat() == QtCore.TextFormat.PlainText
    assert window.mode_banner.textFormat() == QtCore.TextFormat.PlainText

    window.apply_state(synthetic.state(
        position=None, halted="Брокер ответил <ERR_ORDER> и заявку не принял"))
    assert "<ERR_ORDER>" in window.halt_banner.text()


def test_the_settings_dialog_does_not_pile_up(qapp, monkeypatch) -> None:
    """Диалог настроек удаляется после закрытия, а не копится на окне.

    Родителем ему назначено окно, а окно не закрывается: без явного удаления
    каждое открытие настроек оставляло за собой ещё одну копию — вместе
    с полями, подписками на тему и обработчиками.
    """
    from PySide6.QtCore import QEvent as QtEvent

    import ui.main_window
    from ui.settings_dialog import SettingsDialog

    class Instant(SettingsDialog):
        def confirm(self, values) -> bool:
            # Подтверждение «было → стало» открыло бы модальное окно и повисло.
            return True

        def exec(self) -> int:
            return 0

    monkeypatch.setattr(ui.main_window, "SettingsDialog", Instant)
    window = ui.main_window.MainWindow(port=RecordingPort(), sanitize=redact)
    window._timer.stop()
    try:
        for _ in range(3):
            window.open_settings()
            qapp.sendPostedEvents(None, QtEvent.Type.DeferredDelete)
        left = window.findChildren(SettingsDialog)
        assert not left, f"на окне осталось диалогов настроек: {len(left)}"
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


# ==========================================================================
# Кнопка связи с брокером: зеркалит снимок, а не помнит нажатие
# ==========================================================================

@pytest.mark.parametrize(
    ("connection", "checked", "label"),
    [
        (Connection.UNKNOWN, False, "Подключиться"),
        (Connection.OFFLINE, False, "Подключиться"),
        (Connection.RECONNECTING, True, "Отключиться"),
        (Connection.ONLINE, True, "Отключиться"),
    ],
)
def test_the_stream_button_mirrors_the_connection_of_the_snapshot(
    window, connection: Connection, checked: bool, label: str
) -> None:
    """Нажата там, где связь есть или её добывают; разжата — где просьбы нет."""
    window.apply_state(synthetic.state(connection=connection))
    assert window.stream_action.isChecked() is checked
    assert window.stream_action.text() == label
    assert "не запуск робота" in window.stream_action.toolTip()


def test_a_connection_snapshot_is_shown_not_executed(window) -> None:
    """Петля сигналов: снимок «связи нет» не превращается в команду «отключиться».

    Без блокировки `setChecked()` поднимал `toggled`, подключённый к `port.stream`:
    снимок снимал подписку и писал в журнал «Отключено по команде из окна»,
    которой никто не давал, а снимок «связь есть» включал поток повторно.
    """
    for connection in (
        Connection.ONLINE,
        Connection.OFFLINE,
        Connection.RECONNECTING,
        Connection.UNKNOWN,
        Connection.ONLINE,
    ):
        window.apply_state(synthetic.state(connection=connection))
    assert [call for call in window.port.calls if call[0] == "stream"] == []


def test_the_button_asks_the_port_to_connect_and_then_to_disconnect(window) -> None:
    window.stream_action.trigger()
    window.stream_action.trigger()
    assert [call for call in window.port.calls if call[0] == "stream"] == [
        ("stream", True),
        ("stream", False),
    ]


def test_every_connection_state_has_a_look_for_the_button() -> None:
    """Таблица полна: новое значение `Connection` обязано получить строку."""
    assert set(STREAM_BUTTON) == set(Connection)


def test_a_closed_exchange_does_not_read_as_a_broken_connection(window) -> None:
    """«Биржа закрыта» и «Восстанавливаем связь» различимы с экрана (`B-022`).

    Владелец счёта запустил программу в субботу, полчаса смотрел
    «Восстанавливаем связь» и решил, что программу сломали ночью. Была
    суббота. Оба состояния означают «свечи не идут», и пока подпись у них
    одна, отличить рынок от поломки нечем.
    """
    window.apply_state(synthetic.state(connection=Connection.MARKET_CLOSED))
    closed = window.status_panel.connection.value.text()
    window.apply_state(synthetic.state(connection=Connection.RECONNECTING))
    broken = window.status_panel.connection.value.text()
    assert closed != broken, "закрытая биржа подписана как обрыв связи"
    assert "закрыт" in closed.lower(), closed
    assert "связ" not in closed.lower(), (
        f"подпись закрытой биржи говорит про связь, а связь тут ни при чём: {closed!r}"
    )


def test_the_closed_exchange_button_promises_no_opening_time() -> None:
    """Подсказка кнопки не называет времени открытия — она его не знает.

    Время приходит из расписания брокера, а расписание может не ответить:
    живьём оно не проверялось ни разу. Постоянный текст, назвавший час,
    соврал бы молча. Час называет строка журнала, где он либо есть,
    либо честно объявлен неизвестным.
    """
    hint = STREAM_BUTTON[Connection.MARKET_CLOSED].tooltip
    assert "журнал" in hint.lower(), "не сказано, где искать время открытия"
    assert not re.search(r"\d{1,2}[:.]\d{2}", hint), (
        f"подсказка называет время открытия, которого не знает: {hint!r}"
    )
    assert STREAM_BUTTON[Connection.MARKET_CLOSED].checked is True, (
        "поток при закрытой бирже жив и ждёт открытия — кнопка обязана быть нажата"
    )


def test_a_bare_port_refuses_the_stream_command_aloud(qapp) -> None:
    """Окно без сборки: команда связи отвечает отказом, а не тишиной."""
    port = TerminalPort()
    heard: list[str] = []
    port.failed.connect(heard.append)
    port.stream(True)
    port.stream(False)
    assert [text.split(":")[0] for text in heard] == [
        "Подключение к брокеру",
        "Отключение от брокера",
    ]
    assert all("не подключён" in text for text in heard)


# ==========================================================================
# Меню «Настройки»: календарь открывают отсюда, а не кнопкой на панели
# ==========================================================================

def _menu_titles(window) -> list[str]:
    return [action.text() for action in window.menuBar().actions()]


#: Меню окна по названию. Через поля окна, а не через `action.menu()`:
#: обёртка Python у меню, взятого из строки меню, умирает вместе с прежним
#: окном и на заново занятом адресе даёт «Internal C++ object already deleted».
_MENUS = {"Программа": "program_menu", "Настройки": "settings_menu", "Справка": "help_menu"}


def _items(window, title: str) -> list[str]:
    menu = getattr(window, _MENUS[title])
    return [item.text() for item in menu.actions() if item.text()]


def test_the_menu_bar_has_a_settings_menu_of_its_own(window) -> None:
    """Настроечных окон больше одного, и у них своё меню между двумя прежними.

    Решение владельца счёта 05.09.2026: «может, в меню добавить Настройки
    и там по пунктам уже». Складывать настроечные окна вперемешку с выходом
    и выгрузкой значит их прятать.
    """
    assert _menu_titles(window) == ["Программа", "Настройки", "Справка"]


def test_every_settings_menu_item_ends_with_an_ellipsis(window) -> None:
    """Все пункты — с многоточием: это обещание, что откроется окно.

    Пункт без многоточия читается как «сделать сразу», и владелец счёта
    ждёт от него действия, а не диалога.

    Проверка **на каждый пункт**, а не на точный список из двух: список рос
    уже дважды (календарь 05.09.2026, шаблоны настроек тогда же), и сверка
    с перечнем краснела не на нарушении правила, а на появлении соседа.
    """
    items = _items(window, "Настройки")
    assert items[:3] == [
        "Параметры робота…", "Календарь нерабочих дней…", "Шаблоны настроек…",
    ]
    assert all(item.endswith("…") for item in items), (
        f"пункт меню «Настройки» без многоточия обещает действие, а не окно: {items}"
    )


def test_the_settings_item_lives_in_one_place_only(window) -> None:
    """Из меню «Программа» настройки убраны: одно и то же в двух местах

    потом правят в одном и забывают в другом. Кнопка на панели при этом
    остаётся — параметры открывают чаще всего, и путь до них короче.
    """
    assert not [item for item in _items(window, "Программа") if "обот" in item]
    assert window.settings_action in window.toolbar.actions()


def test_the_settings_window_has_a_keyboard_shortcut(window) -> None:
    """Горячая клавиша хотя бы на параметры: меню — не единственный путь."""
    assert not window.settings_action.shortcut().isEmpty()


def test_the_calendar_sends_the_marks_to_the_engine(qapp, monkeypatch) -> None:
    """Отметки календаря уезжают движку тем же путём, что и прочие настройки.

    Свой путь до движка означал бы второе место, где меняются настройки,
    и первое же расхождение окна с движком. Проверяется, что уходит именно
    `apply_settings` и именно с отметками.
    """
    from datetime import date

    import ui.main_window
    from ui.calendar_dialog import CalendarDialog
    from ui.models import CalendarDay

    marked = (CalendarDay(day=date(2026, 6, 19), trading=False),)

    class Accepting(CalendarDialog):
        def exec(self) -> int:
            self._marks = {mark.day: mark.trading for mark in marked}
            return int(ui.main_window.QDialog.DialogCode.Accepted)

    monkeypatch.setattr(ui.main_window, "CalendarDialog", Accepting)
    port = RecordingPort()
    window = ui.main_window.MainWindow(port=port, sanitize=redact)
    window._timer.stop()
    try:
        window.open_calendar()
        sent = [value for name, value in port.calls if name == "apply_settings"]
        assert len(sent) == 1, f"настройки ушли не один раз: {port.calls}"
        assert sent[0].calendar == marked
        assert window.settings().calendar == marked
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_a_cancelled_calendar_changes_nothing(qapp, monkeypatch) -> None:
    """«Отмена» в календаре не отправляет движку ничего.

    Иначе отменённая правка попадала бы в журнал изменением настроек — и
    владелец счёта увидел бы то, чего не делал.
    """
    from datetime import date

    import ui.main_window
    from ui.calendar_dialog import CalendarDialog
    from ui.models import CalendarDay

    class Rejecting(CalendarDialog):
        def exec(self) -> int:
            self._marks = {date(2026, 6, 19): False}
            return int(ui.main_window.QDialog.DialogCode.Rejected)

    monkeypatch.setattr(ui.main_window, "CalendarDialog", Rejecting)
    port = RecordingPort()
    window = ui.main_window.MainWindow(port=port, sanitize=redact)
    window._timer.stop()
    try:
        window.open_calendar()
        assert not [name for name, _ in port.calls if name == "apply_settings"]
        assert window.settings().calendar == ()
        assert CalendarDay  # имя используется только ради явного импорта модели
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_the_calendar_dialog_does_not_pile_up(qapp, monkeypatch) -> None:
    """Календарь удаляется после закрытия — та же беда, что у окна настроек."""
    from PySide6.QtCore import QEvent as QtEvent

    import ui.main_window
    from ui.calendar_dialog import CalendarDialog

    class Instant(CalendarDialog):
        def exec(self) -> int:
            return 0

    monkeypatch.setattr(ui.main_window, "CalendarDialog", Instant)
    window = ui.main_window.MainWindow(port=RecordingPort(), sanitize=redact)
    window._timer.stop()
    try:
        for _ in range(3):
            window.open_calendar()
            qapp.sendPostedEvents(None, QtEvent.Type.DeferredDelete)
        assert not window.findChildren(CalendarDialog)
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
