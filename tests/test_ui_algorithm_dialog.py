"""Окно выбора торгового алгоритма и всплывающее описание правила.

Задача З4 миниплана `strategy-modules-switchable.md`, редакция 08.09.2026.
Просьба владельца счёта дословно: «надо в отдельном окне выбор алгоритма —
там кнопку подробнее с всплывающим описанием».

Что здесь стережётся и почему именно это
----------------------------------------
1. **Описание не обрезается.** Длина переменная — 391 точка при умолчаниях,
   459 при включённом фильтре против пилы. Замер 06.09.2026 показал, чем
   кончается ярлык фиксированного размера: длинное сообщение в полоске хода
   обрезалось на 2256 точках против 576, и человек читал половину фразы.
2. **Кнопка «Подробнее» работает до выбора.** Описание — единственный способ
   узнать, что делает алгоритм, не открывая исходник. Кнопка, показывающая
   только выбранное, требовала бы сперва сменить торговое правило.
3. **Молчания нет.** Список из одного пункта, пустой каталог, выбранный
   алгоритм, которого в сборке нет, — каждый случай назван словами
   (правило 13 `CLAUDE.md`).
4. **Текст приходит снаружи как есть.** Окно его не сочиняет и не сокращает:
   собирает описание сам алгоритм, `ui/` торговых слоёв не импортирует.
"""

from __future__ import annotations

import pytest
from helpers import settle_qt
from PySide6.QtCore import QRect, Qt
from PySide6.QtWidgets import QDialogButtonBox, QLabel

from ui.algorithm_dialog import (
    ALONE_NOTE,
    EMPTY_NOTE,
    AlgorithmDetails,
    AlgorithmDialog,
    details_preamble,
)
from ui.models import AlgorithmOption

#: Правило абзацами — длиной с настоящее описание и заведомо длиннее окна.
#:
#: Настоящее собирает алгоритм (`strategies/ema_reverse.py::describe`), и его
#: содержание проверяется там. Здесь важна только длина: окно обязано
#: выдержать текст, который в него не помещается.
LONG_DETAILS = "\n\n".join(
    f"Абзац {number}. " + "Длинное предложение описания правила. " * 6
    for number in range(1, 9)
)

FIRST = AlgorithmOption(
    id="ema_reverse",
    title="Реверс по средней",
    summary="Реверс по средней: закрытие выше средней — робот хочет быть в лонге.",
    details=LONG_DETAILS,
    chosen=True,
)

SECOND = AlgorithmOption(
    id="atr_channel",
    title="Канал ATR",
    summary="Канал ATR: выход за верхнюю границу — робот хочет быть в лонге.",
    details="Правило канала абзацами.\n\n• Выход за границу — лонг.",
    chosen=False,
)


@pytest.fixture()
def make_choice(qapp):
    """Строитель окна выбора: создаёт с нужным каталогом и сам убирает.

    ⚠️ Уборка — `settle_qt`, а не `processEvents()`. Голая прокрутка очереди
    отложенное удаление не исполняет: окно остаётся живым и видимым и уезжает
    в следующий тест. Тот же довод записан у фикстуры окна настроек.
    """
    built: list[AlgorithmDialog] = []

    def build(*options: AlgorithmOption) -> AlgorithmDialog:
        window = AlgorithmDialog(options)
        built.append(window)
        return window

    yield build

    for window in built:
        window.deleteLater()
    settle_qt(qapp)


@pytest.fixture()
def make_details(qapp):
    """Строитель окна описания. Уборка своя, по той же причине."""
    built: list[AlgorithmDetails] = []

    def build(option: AlgorithmOption) -> AlgorithmDetails:
        window = AlgorithmDetails(option)
        built.append(window)
        return window

    yield build

    for window in built:
        window.deleteLater()
    settle_qt(qapp)


# ------------------------------------------------------------------- список


def test_the_list_names_every_algorithm_of_the_catalogue(make_choice) -> None:
    """Каждая строка каталога — строка списка, с названием и своим именем."""
    window = make_choice(FIRST, SECOND)
    assert window.list.count() == 2
    assert window.list.item(0).text() == "Реверс по средней"
    assert window.list.item(1).text() == "Канал ATR"
    assert window.list.item(1).data(Qt.ItemDataRole.UserRole) == "atr_channel"


def test_the_chosen_algorithm_is_the_selected_one(make_choice) -> None:
    """Окно открывается на том алгоритме, который стоит в настройках.

    Мутация, обязанная ронять проверку: открывать список на первой строке
    вместо выбранной. Человек увидел бы выделенным не тот алгоритм, которым
    работает робот, и «ОК» сменил бы правило, ничего не трогая.
    """
    window = make_choice(FIRST, SECOND)
    window.set_chosen("atr_channel")
    assert window.list.currentRow() == 1
    assert window.chosen_id() == "atr_channel"


def test_an_unknown_choice_selects_nothing_and_says_so(make_choice) -> None:
    """Выбран алгоритм, которого в списке нет: пустое выделение и объяснение.

    Подстановка первого попавшегося вернула бы по «ОК» имя, которого владелец
    счёта не выбирал, — смену торгового правила без его ведома.
    """
    window = make_choice(FIRST)
    window.set_chosen("atr_channel")
    assert window.chosen_id() == ""
    assert "atr_channel" in window.summary.text()
    assert not window.details_button.isEnabled(), (
        "«Подробнее» жмётся при пустом выделении — показывать нечего"
    )


def test_the_summary_follows_the_selection(make_choice) -> None:
    """Строка под списком рассказывает про выделенный алгоритм, а не про первый."""
    window = make_choice(FIRST, SECOND)
    window.list.setCurrentRow(0)
    assert window.summary.text() == FIRST.summary
    window.list.setCurrentRow(1)
    assert window.summary.text() == SECOND.summary


def test_the_summary_is_plain_text_not_markup(make_choice) -> None:
    """Разметку окно не угадывает: «<» съел бы всё, что за ним."""
    marked = AlgorithmOption(
        id="x", title="X", summary="закрытие < средней — шорт", details="d",
    )
    window = make_choice(marked)
    window.set_chosen("x")
    assert window.summary.textFormat() == Qt.TextFormat.PlainText
    assert "<" in window.summary.text()


# --------------------------------------------------------- состав каталога


def test_a_catalogue_of_one_says_that_this_is_on_purpose(make_choice) -> None:
    """Список из одного пункта читается как поломка — если об этом промолчать.

    В реестре сегодня один алгоритм, и второй не пишется по прямому решению
    владельца счёта. Окно обязано сказать это словами, а не показать одну
    строку и пустое место.
    """
    window = make_choice(FIRST)
    said = [label.text() for label in window.findChildren(QLabel)]
    assert ALONE_NOTE in said, "окно не сказало, что алгоритм в сборке один"


def test_a_catalogue_of_two_does_not_say_it(make_choice) -> None:
    """Канарейка предыдущей проверки: строка обязана исчезать при выборе из двух.

    Строка, которая стоит всегда, ничего не сообщает, а проверка на неё
    зеленела бы при любом каталоге.
    """
    window = make_choice(FIRST, SECOND)
    said = [label.text() for label in window.findChildren(QLabel)]
    assert ALONE_NOTE not in said


def test_an_empty_catalogue_says_it_did_not_arrive(make_choice) -> None:
    """Каталог не приехал: сказать это, а не показать пустой список.

    Пустое окно читается как «алгоритмов нет вовсе». Правило 13 `CLAUDE.md`.
    """
    window = make_choice()
    said = [label.text() for label in window.findChildren(QLabel)]
    assert EMPTY_NOTE in said
    assert not window.details_button.isEnabled()


def test_an_empty_catalogue_does_not_order_a_choice_that_cannot_be_made(
    make_choice,
) -> None:
    """Тупик: окно велело выбрать из списка, которого нет. Так больше нельзя.

    Владелец счёта 09.09.2026 прочитал в этом окне дословно: «Сейчас
    в настройках стоит алгоритм „ema_reverse“, а в этой сборке его нет.
    Выберите один из списка — иначе робот работать не сможет», — при пустом
    списке. Указание, которое невозможно выполнить, хуже молчания: молчание
    заставляет искать, а такое указание заставляет искать не там.

    Проверяется **три** вещи разом, и все три — про выход из тупика:
    требования выбора нет; названо, что сейчас в настройках; «ОК» выключен,
    потому что принимать нечего.

    Мутация, обязанная ронять проверку: убрать развилку по `self._options`
    из `_show_summary` — прежний текст вернётся дословно.
    """
    window = make_choice()
    window.set_chosen("ema_reverse")
    said = window.summary.text()
    assert "Выберите один из списка" not in said, (
        f"окно снова требует выбрать из пустого списка: {said!r}"
    )
    assert "ema_reverse" in said, (
        "окно не сказало, какой алгоритм стоит в настройках сейчас"
    )
    accept = window.buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert not accept.isEnabled(), (
        "«ОК» при пустом списке жмётся и не делает ничего — нажатие, "
        "после которого не происходит и не говорится ничего"
    )
    assert "Отменой" in accept.toolTip(), (
        "выключенный «ОК» не говорит, как выйти из окна"
    )


def test_a_missing_algorithm_in_a_real_catalogue_still_asks_for_a_choice(
    make_choice,
) -> None:
    """Канарейка предыдущей: список есть — указание выбрать из него уместно.

    Без этой проверки развилку можно было бы «починить», убрав требование
    выбора вообще, — и человек с устаревшим файлом настроек остался бы
    без единственного действия, которое ему помогает.
    """
    window = make_choice(FIRST)
    window.set_chosen("atr_channel")
    said = window.summary.text()
    assert "Выберите один из списка" in said, (
        f"список есть, а выбрать из него окно не предлагает: {said!r}"
    )
    assert window.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled(), (
        "«ОК» выключен там, где выбрать есть из чего"
    )


def _height_the_text_needs(label: QLabel, text: str | None = None) -> int:
    """Сколько точек нужно тексту при нынешней ширине ярлыка.

    Текст можно передать чужой — тогда меряется он, а не показанный сейчас.
    Так подбирается правило, которое в окно заведомо не влезает
    (`_a_rule_taller_than`), — **до** того, как окно с ним построено.

    ⚠️ Считается по шрифту, а не спрашивается у Qt. `QLabel.heightForWidth`
    в разложенном окне возвращает **уже отведённую** высоту, а не нужную:
    проверка на нём зеленела при заведомо сжатых надписях (замер 09.09.2026).
    То же и с пересечением прямоугольников — компоновщик раздаёт их
    непересекающимися, просто вдвое ниже нужного, а текст рисуется поверх
    соседа, выйдя за свой.
    """
    return label.fontMetrics().boundingRect(
        QRect(0, 0, label.width(), 0),
        int(Qt.TextFlag.TextWordWrap),
        label.text() if text is None else text,
    ).height()


#: Сколько раз подбор длины правила успевает удвоить текст, прежде чем
#: признать поломку мерки. Шестнадцать удвоений — это 65 536 повторов фразы,
#: то есть больше двух мегабайт: столько не потребуется ни при каком шрифте,
#: а вот шрифт нулевой высоты крутил бы такой цикл вечно.
RULE_DOUBLINGS = 16


def _a_rule_taller_than(label: QLabel, points: int) -> str:
    """Правило одной фразой, которому при ширине `label` нужно больше `points`.

    ⚠️ Длина здесь **считается по шрифту той машины, где идёт прогон**, а не
    зашита множителем. Зашитый множитель и есть та самая зависимость от чужих
    шрифтов, на которой проверка упала на бегунке CI 09.09.2026, вечером того
    же дня, при полностью исправной программе: та же строка более узким шрифтом укладывается
    в меньшее число строк, в окно **влезает**, и растить его не за чем.
    Замер той же минуты: `QT_FONT_DPI=72` даёт на этой машине высоту строки
    13 точек против 17, и текст, которому нужно было 102 точки, обходится 52.

    Множитель, подобранный «чтобы уж точно не влезло», лечит симптом до
    первого шрифта пошире, и следующий множитель придётся снова удваивать.
    """
    phrase = "Очень длинное правило одной фразой. "
    text = phrase
    for _ in range(RULE_DOUBLINGS):
        if _height_the_text_needs(label, text) > points:
            return text
        text += text
    raise AssertionError(
        f"правило доросло до {len(text)} знаков и всё ещё умещается "
        f"в {points} точек: у шрифта нулевая высота либо мерка сломана"
    )


def _lay_out(window: AlgorithmDialog) -> None:
    """Разложить окно сейчас, а не когда-нибудь: меряем уже расставленное."""
    layout = window.layout()
    assert layout is not None, "у окна нет компоновки — мерить нечего"
    layout.activate()


def test_the_window_grows_under_a_long_rule_instead_of_squeezing_it(
    make_choice,
) -> None:
    """Окно растёт под текст, а не режет его и не кладёт строки друг на друга.

    Замер 09.09.2026, найдено глазами на снимке: строка про непришедший
    список легла **поверх** строки про выбранную настройку, и прочитать
    нельзя было ни ту, ни другую. Причина — ярлык с переносом сообщает
    компоновщику минимум в одну строку, а высота окна была задана числом.

    Правило одной фразой приходит **снаружи**, от самого алгоритма
    (`strategies/`), и его длину окно не выбирает: сегодня она в две строки,
    завтра у другого алгоритма в шесть.

    Стережётся здесь **инвариант, а не число точек**, и это правка того же
    вечера, после падения на бегунке CI (прогон 34354445362):

    1. окно с длинным правилом выше, чем **оно же** с коротким. Мерка —
       второе такое же окно, а не зашитая константа: константу надо с чем-то
       сравнивать, и сравнение упирается в шрифты хозяина машины;
    2. ни одной надписи не отведено меньше, чем нужно её тексту по шрифту.
       Первого пункта мало: окно, выросшее на десять точек вместо ста,
       сжимает текст ровно так же.

    Насколько длинное правило считается длинным, тоже не зашито: строка
    подбирается так, чтобы ей одной было мало всей высоты короткого окна
    (`_a_rule_taller_than`). Тогда содержимому заведомо нужно больше, чем
    у окна есть, при любом шрифте — а без такого подбора при узком шрифте
    текст умещается и растить окно **правильно** не надо.

    Мутации, обязанные ронять проверку:
    * убрать вызов `_fit_the_window` из `_show_summary` — окно не вырастет
      вовсе, падают оба пункта;
    * растить окно на постоянную добавку вместо `heightForWidth` — первый
      пункт пройдёт, второй обязан упасть.
    """
    terse = AlgorithmOption(
        id="terse", title="Немногословный",
        summary="Правило одной фразой.",
        details="Правило абзацами.",
    )
    modest = make_choice(terse)
    modest.set_chosen("terse")
    _lay_out(modest)
    under_a_short_rule = modest.height()

    wordy = AlgorithmOption(
        id="wordy", title="Многословный",
        summary=_a_rule_taller_than(modest.summary, under_a_short_rule),
        details="Правило абзацами.",
    )
    window = make_choice(wordy)
    window.set_chosen("wordy")
    _lay_out(window)

    assert window.summary.width() == modest.summary.width(), (
        "строка под списком получила в двух окнах разную ширину "
        f"({window.summary.width()} и {modest.summary.width()}) — длина "
        "правила подбиралась не по той ширине, на которой его покажут"
    )
    assert window.height() > under_a_short_rule, (
        f"окно осталось прежней высоты ({window.height()}) под правилом, "
        f"которому одному нужно больше {under_a_short_rule} точек — "
        "ровно столько занимает всё окно с коротким правилом"
    )
    shown = [
        label for label in window.findChildren(QLabel)
        if label.text() and not label.isHidden()
    ]
    assert len(shown) >= 3, "надписей меньше, чем должно быть: проверка вакуумна"
    cramped = [
        (label.text()[:40], label.height(), _height_the_text_needs(label))
        for label in shown
        if _height_the_text_needs(label) > label.height()
    ]
    assert not cramped, (
        "надписи не помещаются в отведённую им высоту и лягут поверх соседей "
        f"(текст, дано, нужно): {cramped}"
    )


def test_an_empty_catalogue_says_what_to_do_next(make_choice) -> None:
    """Мало сказать «списка нет» — надо сказать, что делать. Правило 13.

    Выход из этого окна ровно один — «Отмена», и он назван; дальше названо
    и то, чем список можно получить. Прежняя редакция обрывалась на «выбрать
    сейчас не из чего», и следующий шаг человек искал сам.
    """
    window = make_choice()
    said = [label.text() for label in window.findChildren(QLabel)]
    assert any("Отменой" in text for text in said), (
        "окно не сказало, как из него выйти, ничего не сломав"
    )
    assert any("перезапустите программу" in text for text in said), (
        "окно не сказало, чем добыть список"
    )


# ------------------------------------------------------------- «Подробнее»


def test_the_details_show_the_whole_text_without_cutting_it(make_details) -> None:
    """Описание показано целиком, до последнего знака.

    ⚠️ Главная проверка файла. Ярлык в окне фиксированного размера обрезал бы
    хвост молча — так уже было с полоской хода 06.09.2026. Здесь текст длиннее
    окна заведомо, и сверяется он **целиком**, а не по первой строке.
    """
    window = make_details(FIRST)
    assert window.text() == LONG_DETAILS
    assert window.text().endswith(LONG_DETAILS[-40:]), "хвост описания потерян"


def test_the_details_scroll_when_the_text_does_not_fit(qapp, make_details) -> None:
    """Текст, не влезший в окно, доступен прокруткой, а не потерян.

    Мутация, обязанная ронять проверку: заменить поле с прокруткой на ярлык.
    """
    window = make_details(FIRST)
    window.resize(420, 320)
    window.show()
    settle_qt(qapp)
    bar = window.body.verticalScrollBar()
    assert bar.maximum() > 0, (
        "длинное описание помещается целиком — проверка вакуумна либо "
        "прокрутки в окне нет вовсе"
    )
    window.close()


def test_the_details_wrap_instead_of_running_off_the_edge(make_details) -> None:
    """Длинные предложения переносятся по ширине окна, а не уезжают вправо.

    Горизонтальная прокрутка вместо переноса — это текст, который читают
    по одной строке за раз, то есть не читают.
    """
    window = make_details(FIRST)
    assert window.body.lineWrapMode() == window.body.LineWrapMode.WidgetWidth


def test_the_details_are_read_only_but_copyable(make_details) -> None:
    """Описание переносят в переписку: выделение мышью есть, правка запрещена.

    Редактируемое поле на этом месте выглядело бы настройкой, которую можно
    поменять, — а поменять её нельзя: текст собирает алгоритм.
    """
    window = make_details(FIRST)
    assert window.body.isReadOnly()
    assert window.body.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse


def test_the_details_say_whose_numbers_they_show() -> None:
    """Чьи числа в описании — сказано над ним, а не подразумевается.

    У невыбранного алгоритма полей в окне нет вовсе, и показываются его
    собственные умолчания. Без этой строки человек прочёл бы чужие числа
    как свои.
    """
    assert "нынешними" in details_preamble(FIRST)
    assert "умолчаниями" in details_preamble(SECOND)
    assert details_preamble(FIRST) != details_preamble(SECOND)


def test_the_details_open_for_an_algorithm_that_is_not_chosen(
    qapp, make_choice, monkeypatch
) -> None:
    """«Подробнее» показывает выделенный алгоритм, а не выбранный в настройках.

    Ради этого кнопка и стоит рядом со списком: прочитать правило нужно
    **до** выбора. Кнопка, показывающая только выбранное, требовала бы сперва
    сменить торговое правило, чтобы узнать, стоит ли его менять.
    """
    window = make_choice(FIRST, SECOND)
    window.set_chosen("ema_reverse")
    window.list.setCurrentRow(1)

    shown: list[str] = []

    class Instant(AlgorithmDetails):
        def exec(self) -> int:
            shown.append(self.text())
            return 0

    monkeypatch.setattr("ui.algorithm_dialog.AlgorithmDetails", Instant)
    window.show_details()
    assert shown == [SECOND.details], (
        "«Подробнее» показало описание не того алгоритма, который выделен"
    )


# ------------------------------------------------------------- клавиатура


def test_enter_means_ok_and_not_details(make_choice) -> None:
    """Enter в окне выбора означает «ОК», а не «Подробнее».

    Кнопка по умолчанию в Qt перехватывает Enter. Если бы ею стала
    «Подробнее», клавиатурный обход упирался бы в описание вместо выбора.
    """
    window = make_choice(FIRST, SECOND)
    assert not window.details_button.isDefault()
    assert not window.details_button.autoDefault()
