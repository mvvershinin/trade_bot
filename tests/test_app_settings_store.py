"""Настройки переживают перезапуск — и не пропадают молча, когда файл испорчен.

До 05.09.2026 файла настроек не было вовсе: `app/main.py` создавал `Settings()`
при каждом запуске. Владелец счёта подобрал ночью набор, на котором он в плюсе,
и цифры пришлось снимать со снимков экрана вручную.

Каждая проверка ниже стережёт одно поведение, и оно названо первой строкой
докстринга. Ни одна не подаёт на вход то же, что ожидает на выходе: там, где
проверяется сохранение, ожидание строится **из полей класса**, а не из копии
входа.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import typing
from datetime import date, time

import pytest

from app.settings_store import FORMAT_VERSION, SETTINGS_FILE_NAME, SettingsStore
from strategies import registry
from ui.models import (
    FIELD_CAPTIONS,
    AfterTakeProfit,
    AverageKind,
    CalendarDay,
    MinuteBarLimit,
    MinutePriceOrder,
    ReversalMoment,
    Settings,
    TimeExitKind,
)
from ui.settings_codec import field_codecs

#: Набор, у которого **каждое** поле отличается от умолчания. Списком, а не
#: `replace` от умолчаний: проверка «пережило перезапуск» на наборе, совпавшем
#: с умолчанием хотя бы одним полем, это поле не проверяет вовсе — оно
#: совпадёт и при полностью потерянном файле.
DIFFERENT = Settings(
    instrument="MXH7",
    timeframe="15 минут",
    depth_days=45,
    history_depth_days=120,
    expiry_halt_days=5,
    # Алгоритм в сборке один (решение 0063): поле совпадает с умолчанием
    # и стережётся отдельно, на подставном втором (`_COVERED_APART`).
    strategy_id=Settings().strategy_id,
    average_period=21,
    average_kind=AverageKind.SMA,
    # Требования алгоритма — они же умолчания (решение 0063): при чтении
    # файла другое значение выставляется обратно. Стережётся отдельно.
    reversal_moment=ReversalMoment.SAME_BAR,
    after_take_profit=AfterTakeProfit.WAIT_FOR_SIGNAL,
    # ⚠️ Фиксация прибыли включена, хотя умолчание тоже «включена»,
    # и это вынужденно: пара «фиксация / способ» выражает ОДИН выбор из трёх,
    # и сочетания «не фиксируем + скользящий уровень» у настроек не бывает
    # (решение 0060, `app/settings_store.py::_one_way_to_take_profit`).
    # Отличаться от умолчания обоими полями сразу образец не может; второе
    # поле проверяется отдельно — см. `_COVERED_APART`.
    take_profit_enabled=True,
    take_profit_pct=1.25,
    trailing_enabled=True,
    trailing_start_pct=1.5,
    trailing_offset_pct=0.75,
    trailing_step_pct=0.11,
    window_start=time(9, 30),
    window_end=time(11, 30),
    close_on_time_end=False,
    volume=4,
    # ПРЕДОХРАНИТЕЛЬ ВЫКЛЮЧЕН НА ЭТАПЕ (D-113)
    # volume_cap_enabled=True,
    # volume_cap=9,
    # daily_loss_limit_enabled=True,
    # daily_loss_limit_pct=3.5,
    # free_funds_reserve_enabled=True,
    # free_funds_reserve_pct=12.5,
    commission_per_side_rub=None,
    price_step=25.0,
    ruble_per_point=1.73774,
    ruble_per_point_source="биржа, RIU6, 04.09.2026 07:00",
    slippage_steps=1.5,
    minute_order=MinutePriceOrder.ADVERSE_FIRST,
    minute_bar_limit=MinuteBarLimit.FIFTEEN,
    # Выход по концу окна (Ф3 задачи З8). Предельная форма — требование
    # алгоритма и умолчание; стережётся отдельно (`_COVERED_APART`).
    time_exit_order=TimeExitKind.LIMIT,
    time_exit_limit_steps=7,
    time_exit_wait_bars=3,
    log_directory="/tmp/терминал-логи",
    # Оба вида отметки сразу: «не торгуем» на будний день и «торгуем»
    # на субботу. Один вид проверял бы половину: перевод «да/нет» в файл
    # и обратно можно испортить в одну сторону и не заметить.
    calendar=(
        CalendarDay(day=date(2026, 6, 12), trading=False),
        CalendarDay(day=date(2026, 11, 7), trading=True),
    ),
)


@pytest.fixture()
def store(tmp_path: pathlib.Path) -> SettingsStore:
    return SettingsStore(tmp_path)


def _names() -> list[str]:
    return [field.name for field in dataclasses.fields(Settings)]


# ------------------------------------------------------------- перезапуск

#: Поле, которому отличающегося от умолчания значения в образце не досталось,
#: и проверка, которая стережёт его сохранение отдельно.
#:
#: ⚠️ Список поимённый и проверяемый: названная проверка обязана существовать
#: в этом файле. Тихо вычеркнуть поле из канарейки нельзя — так однажды
#: отключилась проверка сохранения имени алгоритма (правка 14.09.2026),
#: и мутация «подменять выбранное при каждом старте» не роняла ничего.
_COVERED_APART = {
    "take_profit_enabled": "test_the_take_switch_survives_a_restart_on_its_own",
    "strategy_id": "test_the_algorithm_and_its_demanded_fields_survive_a_restart",
    "reversal_moment": "test_the_algorithm_and_its_demanded_fields_survive_a_restart",
    "time_exit_order": "test_the_algorithm_and_its_demanded_fields_survive_a_restart",
}


def test_the_algorithm_and_its_demanded_fields_survive_a_restart(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Имя алгоритма и поля, которых он требует, переживают перезапуск.

    Алгоритм в сборке один (решение 0063), и его требования — умолчания:
    в общем образце этим трём полям не досталось второго значения. Здесь
    реестр получает **подставной** второй алгоритм без требований (`D-078`:
    настоящая таблица не трогается), и файл с ним читается как записан.

    Мутации, обязанные ронять проверку: подменять выбранный алгоритм
    умолчанием при каждом старте; не сохранять момент переворота или форму
    заявки на выход.
    """
    twin = dataclasses.replace(registry.default_entry(), id="twin", demands=())
    monkeypatch.setattr(registry, "_ENTRIES", (registry.default_entry(), twin))
    values = Settings(
        strategy_id="twin", reversal_moment=ReversalMoment.NEXT_BAR,
        time_exit_order=TimeExitKind.MARKET,
    )
    store = SettingsStore(tmp_path)
    assert store.save(values) == ""
    read = SettingsStore(tmp_path).load()
    assert read.troubles == (), read.troubles
    assert read.values.strategy_id == "twin"
    assert read.values.reversal_moment is ReversalMoment.NEXT_BAR
    assert read.values.time_exit_order is TimeExitKind.MARKET


def test_the_take_switch_survives_a_restart_on_its_own(store: SettingsStore) -> None:
    """Выключенная фиксация прибыли переживает перезапуск.

    Отдельной проверкой, потому что в общем образце этому полю не досталось
    второго значения (`_COVERED_APART`): пара «фиксация / способ» выражает
    один выбор из трёх. Поле, которое не сохраняется, вернулось бы
    к умолчанию «фиксируем» — и робот поставил бы уровень, которого человек
    не просил.
    """
    assert store.save(Settings(take_profit_enabled=False)) == ""
    read = SettingsStore(store.path.parent).load()
    assert read.troubles == ()
    assert read.values.take_profit_enabled is False


def test_the_sample_differs_from_the_defaults_in_every_field() -> None:
    """Образец для проверок отличается от умолчания **каждым** полем.

    Без этой проверки все остальные вакуумны наполовину: поле, случайно
    совпавшее с умолчанием, «переживает перезапуск» и при полностью
    потерянном файле.
    """
    # ⚠️ С 05.10.2026 алгоритм в сборке один (решение 0063), и поле
    # `strategy_id` с умолчанием совпадает. Это сказано вслух, а не вычеркнуто
    # молча: поле стоит в `_COVERED_APART`, и его сохранение стережёт
    # отдельная проверка на подставном втором алгоритме.
    default = Settings()
    same = [name for name in _names() if getattr(DIFFERENT, name) == getattr(default, name)]
    assert sorted(same) == sorted(_COVERED_APART), (
        "поля образца совпали с умолчанием, и проверка сохранения их не видит: "
        + ", ".join(sorted(set(same) - set(_COVERED_APART)))
        + "; названные исключением перестали совпадать: "
        + ", ".join(sorted(set(_COVERED_APART) - set(same)))
    )
    missing = [name for name in _COVERED_APART.values() if name not in globals()]
    assert not missing, (
        "исключение ссылается на проверку, которой в этом файле нет: "
        + ", ".join(missing)
    )


def test_an_unknown_algorithm_in_the_file_is_replaced_out_loud(
    store: SettingsStore,
) -> None:
    """Файл более новой сборки: алгоритм заменён на умолчание, и об этом сказано.

    Отказать здесь не на что опереть — прежних настроек в момент запуска
    не существует, программа только открывается. Но подменить **правило
    принятия решений** молча нельзя: строка обязана уйти в `troubles`, то есть
    дойти до журнала решений предупреждением (правило 13 `CLAUDE.md`).

    Мутация, обязанная ронять проверку: вернуть значение как есть.
    """
    assert store.save(Settings().replace(strategy_id="atr_channel")) == ""
    read = SettingsStore(store.path.parent).load()
    assert read.values.strategy_id == registry.DEFAULT_ID, (
        "незнакомый алгоритм оставлен в настройках — робот работать им не сможет"
    )
    assert any("atr_channel" in trouble for trouble in read.troubles), (
        "подмена алгоритма прошла молча"
    )
    assert not read.notes, "оговорка ушла не в тот список: её могут не показать"


def test_a_method_without_the_take_in_the_file_is_replaced_out_loud(
    store: SettingsStore,
) -> None:
    """Файл прежней сборки: «скользящий при выключенной фиксации» — вслух.

    Способов фиксировать прибыль два, применяется один (решение 0060).
    Сочетание «способ скользящий, а фиксация выключена» не означает ничего:
    уровня выхода у позиции в нём нет вовсе (`EngineSettings.plan`), зато
    движок проверяет пару «порог/отступ» и вправе отвергнуть настройки целиком
    из-за чисел, которых никто не читает. Окно такого больше не отдаёт,
    а в файле прежней сборки оно лежит.

    ⚠️ Подмена поведения не меняет: уровня не было и не будет. Молчать о ней
    всё равно нельзя — снят переключатель, который человек ставил руками
    (правило 13 `CLAUDE.md`).

    Мутация, обязанная ронять проверку: принять сочетание как есть, без строки
    в `troubles`.
    """
    assert store.save(Settings(
        take_profit_enabled=False, trailing_enabled=True,
        trailing_start_pct=0.5, trailing_offset_pct=0.2,
    )) == ""
    read = SettingsStore(store.path.parent).load()

    assert read.values.trailing_enabled is False, (
        "способ оставлен выбранным при выключенной фиксации — движок отвергнет "
        "настройки из-за чисел, которых не читает"
    )
    assert read.values.take_profit_enabled is False, (
        "подмена включила фиксацию прибыли: это уже смена поведения робота, "
        "а не приведение настроек в порядок"
    )
    assert any(
        "скользящий уровень" in trouble.lower() for trouble in read.troubles
    ), "снятый переключатель прошёл молча"
    assert not read.notes, "оговорка ушла не в тот список: её могут не показать"
    # ⚠️ Числа способа при этом не трогаются: они хранятся, чтобы включение
    # вернуло подобранное, а не умолчание.
    assert read.values.trailing_start_pct == pytest.approx(0.5)
    assert read.values.trailing_offset_pct == pytest.approx(0.2)


def test_a_chosen_method_with_the_take_on_is_left_alone(store: SettingsStore) -> None:
    """Канарейка: «фиксация включена, способ скользящий» — законный выбор.

    Это и есть выбор «скользящий уровень» (решение 0060), а не сочетание
    двух галочек. Подмена здесь означала бы, что программа отменяет выбранный
    человеком способ при каждом запуске.
    """
    assert store.save(Settings(
        take_profit_enabled=True, trailing_enabled=True,
        trailing_start_pct=0.5, trailing_offset_pct=0.2,
    )) == ""
    read = SettingsStore(store.path.parent).load()
    assert read.values.trailing_enabled is True, "выбранный способ отменён"
    assert read.troubles == (), f"законный выбор объявлен неполадкой: {read.troubles}"


def test_a_known_algorithm_in_the_file_is_left_alone(store: SettingsStore) -> None:
    """Канарейка предыдущей проверки: знакомое имя не трогается и не ворчит.

    Без неё проверка зеленела бы на подмене, которая срабатывает **всегда**,
    то есть на программе, которая никогда не читает выбор человека.
    """
    assert store.save(Settings().replace(strategy_id=registry.DEFAULT_ID)) == ""
    read = SettingsStore(store.path.parent).load()
    assert read.values.strategy_id == registry.DEFAULT_ID
    assert read.troubles == ()


def test_settings_survive_a_restart_field_by_field(store: SettingsStore) -> None:
    """Что записали, то и прочитали — поле в поле, все двадцать шесть."""
    assert store.save(DIFFERENT) == ""
    read = SettingsStore(store.path.parent).load()
    assert read.troubles == ()
    mismatched = {
        name: (getattr(DIFFERENT, name), getattr(read.values, name))
        for name in _names()
        if getattr(DIFFERENT, name) != getattr(read.values, name)
    }
    assert not mismatched, f"поля не пережили перезапуск: {mismatched}"


def test_every_field_of_the_settings_has_a_way_to_be_written() -> None:
    """Поле, заведённое завтра, сохраняется само — таблица укладки полна.

    Проверка не на сегодняшний список полей, а на то, что список **выведен**
    из класса. Тип без укладки роняет `field_codecs()` с объяснением; молчаливого
    пропуска поля быть не может.
    """
    assert sorted(field_codecs()) == sorted(_names())


def test_a_type_without_a_codec_is_refused_aloud() -> None:
    """Незнакомый тип поля — отказ с объяснением, а не поле, которое не пишется."""
    from ui.settings_codec import codec_of

    with pytest.raises(TypeError, match="сохранять не умеет"):
        codec_of(complex)


# ------------------------------------------------------------- испорченное

def test_a_broken_file_is_put_aside_and_said_aloud(store: SettingsStore) -> None:
    """Нечитаемый файл не переписывается: он откладывается, и об этом говорят.

    Испорченный файл — единственный след настроек, которые подбирали руками;
    молча вернуть умолчания и затереть его — худшее из возможного.
    """
    store.save(DIFFERENT)
    broken = "{это не json"
    store.path.write_text(broken, encoding="utf-8")

    read = SettingsStore(store.path.parent).load()
    assert read.values == Settings(), "негодный файл обязан дать умолчания"
    assert read.troubles, "потеря настроек прошла молча"
    spares = sorted(store.path.parent.glob("settings.broken-*.json"))
    assert len(spares) == 1, "испорченный файл не сохранён"
    assert spares[0].read_text(encoding="utf-8") == broken, (
        "отложенный файл изменён — восстанавливать из него нечего"
    )
    assert str(spares[0]) in read.troubles[0], (
        "в журнал не попал путь к отложенному файлу: найти его будет негде"
    )
    assert not store.path.exists()


def test_one_bad_value_does_not_lose_the_other_settings(store: SettingsStore) -> None:
    """Негодное поле теряет себя, а не весь набор."""
    store.save(DIFFERENT)
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["settings"]["average_period"] = "пятнадцать"
    store.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    read = SettingsStore(store.path.parent).load()
    assert read.values.average_period == Settings().average_period
    assert read.values.instrument == DIFFERENT.instrument, (
        "из-за одного негодного поля потерян весь файл"
    )
    assert read.values.window_start == DIFFERENT.window_start
    assert any(f"«{FIELD_CAPTIONS['average_period']}»" in trouble for trouble in read.troubles), (
        "поле подменено умолчанием молча"
    )


def test_yes_in_a_number_field_is_refused_not_taken_as_one(store: SettingsStore) -> None:
    """`true` в числовом поле — отказ, а не единица.

    `bool` — подкласс `int`, и без явной проверки «да» в поле объёма стало бы
    одним контрактом: настройка, которой владелец счёта не делал.
    """
    store.save(DIFFERENT)
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["settings"]["volume"] = True
    store.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    read = SettingsStore(store.path.parent).load()
    assert read.values.volume == Settings().volume
    assert any(f"«{FIELD_CAPTIONS['volume']}»" in trouble for trouble in read.troubles)


def test_an_unknown_value_of_a_choice_is_refused(store: SettingsStore) -> None:
    """Незнакомое значение списка — отказ, а не первый элемент."""
    store.save(DIFFERENT)
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["settings"]["average_kind"] = "smma"
    store.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    read = SettingsStore(store.path.parent).load()
    assert read.values.average_kind is Settings().average_kind
    assert any(f"«{FIELD_CAPTIONS['average_kind']}»" in trouble for trouble in read.troubles)


# --------------------------------------------------- новое и исчезнувшее

def test_a_setting_missing_from_the_file_takes_its_default_and_says_so(
    store: SettingsStore,
) -> None:
    """Настройка, появившаяся в новой сборке, берёт умолчание — и это сказано."""
    store.save(DIFFERENT)
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    del payload["settings"]["slippage_steps"]
    store.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    read = SettingsStore(store.path.parent).load()
    assert read.values.slippage_steps == Settings().slippage_steps
    assert read.values.price_step == DIFFERENT.price_step
    assert any(f"«{FIELD_CAPTIONS['slippage_steps']}»" in note for note in read.notes)


def test_a_setting_this_build_does_not_know_is_written_back(store: SettingsStore) -> None:
    """Чужой ключ переживает работу старой сборки: его не выбрасывают.

    Откатились на сборку постарше, поработали, вернулись — настройки новой
    сборки на месте. Выбросить незнакомый ключ значило бы уничтожить чужие
    данные за то, что мы их не понимаем.
    """
    store.save(DIFFERENT)
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["settings"]["hedging_enabled"] = True
    store.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    later = SettingsStore(store.path.parent)
    read = later.load()
    assert any("hedging_enabled" in note for note in read.notes)
    assert later.save(read.values) == ""
    kept = json.loads(store.path.read_text(encoding="utf-8"))
    assert kept["settings"]["hedging_enabled"] is True, "чужая настройка стёрта"


def test_a_file_from_a_newer_build_is_reported_not_silently_half_read(
    store: SettingsStore,
) -> None:
    """Формат новее нашего — предупреждение, а не молчаливый разбор половины."""
    store.save(DIFFERENT)
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["format_version"] = FORMAT_VERSION + 1
    store.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    read = SettingsStore(store.path.parent).load()
    assert read.troubles, "разница форматов прошла молча"
    assert read.values.instrument == DIFFERENT.instrument


# --------------------------------------------------------------- файл и права

def test_the_file_is_no_wider_than_the_token_beside_it(store: SettingsStore) -> None:
    """Права файла настроек — `0600`, как у файла токена в той же папке."""
    store.save(DIFFERENT)
    assert store.path.name == SETTINGS_FILE_NAME
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_the_first_run_says_the_file_is_not_there_yet(store: SettingsStore) -> None:
    """Отсутствие файла — не отказ, но и не молчание: это первый запуск."""
    read = store.load()
    assert read.values == Settings()
    assert read.troubles == ()
    assert read.notes and "не создан" in read.notes[0]


def test_a_failed_write_keeps_the_previous_file(tmp_path: pathlib.Path) -> None:
    """Отказ записи возвращает фразу и **не трогает** прежний файл."""
    good = SettingsStore(tmp_path)
    good.save(DIFFERENT)
    kept = good.path.read_text(encoding="utf-8")

    # Каталог, которого не может существовать: путь ведёт «внутрь» файла.
    blocked = SettingsStore(tmp_path / "settings.json" / "внутрь")
    failure = blocked.save(Settings())
    assert failure, "отказ записи прошёл молча"
    assert "не сохранены" in failure
    assert good.path.read_text(encoding="utf-8") == kept, "прежний файл потерян"


def test_the_keys_in_the_file_are_the_field_names(store: SettingsStore) -> None:
    """Ключи файла — имена полей латиницей, второго списка имён нет.

    Второй список имён разошёлся бы с первым молча: поле, переименованное
    в `Settings`, читалось бы из файла по старому ключу и всегда брало бы
    умолчание.
    """
    store.save(DIFFERENT)
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["format_version"] == FORMAT_VERSION
    assert sorted(payload["settings"]) == sorted(_names())
    assert all(key.isascii() for key in payload["settings"])


def test_the_written_file_is_readable_text() -> None:
    """Проверка на себя: `Settings` объявлен так, что типы полей разрешимы.

    `from __future__ import annotations` превращает аннотации в строки,
    и укладка полагается на `typing.get_type_hints`. Забытый импорт типа
    в `ui/models.py` уронил бы её — но только в момент сохранения, у владельца
    счёта, а не здесь.
    """
    assert typing.get_type_hints(Settings)


#: Файл настроек владельца счёта, снятый 05.09.2026 (`userdata/settings.json`).
#: Здесь он лежит **дословно**, потому что переезд таблицы укладки в `ui/`
#: 05.09.2026 обязан был не тронуть его формат ни на знак: настройки
#: подбирались вечером и цифры в них — деньги, а не пример.
#:
#: Секретов в файле нет ни одного: настройки — это периоды, проценты и время.
#:
#: ⚠️ Поля, заведённые **после** снимка, дописываются сюда с умолчанием
#: (`strategy_id` — 08.09.2026, `minute_order` и `minute_bar_limit` —
#: 03.10.2026). Иначе проверка
#: «перезапись не портит файл»
#: падала бы на каждом новом поле и сообщала бы не о формате, а о том, что
#: полей стало больше, — а это и так видно по другим сторожам.
OWNER_FILE = """{
  "format_version": 1,
  "settings": {
    "after_take_profit": "stop",
    "average_kind": "sma",
    "average_period": 15,
    "calendar": {
      "2026-09-19": true,
      "2026-10-01": false,
      "2026-10-03": true
    },
    "close_on_time_end": false,
    "commission_per_side_rub": 14.0,
    "confirm_bars": 3,
    "daily_loss_limit_enabled": false,
    "daily_loss_limit_pct": 2.0,
    "depth_days": 90,
    "expiry_halt_days": 1,
    "filter_enabled": false,
    "free_funds_reserve_enabled": false,
    "free_funds_reserve_pct": 30.0,
    "history_depth_days": 90,
    "instrument": "MXU6",
    "log_directory": "",
    "minute_bar_limit": 5,
    "minute_order": "near_first",
    "on_price_equals_average": "skip",
    "price_step": 1.0,
    "reversal_moment": "same_bar",
    "ruble_per_point": 1.0,
    "ruble_per_point_source": "биржей — карточка MXU6",
    "slippage_steps": 1.0,
    "strategy_id": "ema_reverse",
    "take_profit_enabled": true,
    "take_profit_pct": 0.2,
    "threshold_percent": 0.4,
    "time_exit_limit_steps": 10,
    "time_exit_order": "market",
    "time_exit_wait_bars": 1,
    "timeframe": "5 минут",
    "trailing_enabled": true,
    "trailing_offset_pct": 0.05,
    "trailing_start_pct": 0.4,
    "trailing_step_pct": 0.02,
    "volume": 1,
    "volume_cap": 5,
    "volume_cap_enabled": false,
    "window_end": "12:00:00",
    "window_start": "10:20:00"
  }
}
"""


def _migrated(text: str) -> str:
    """Файл владельца в нынешнем виде: так его запишет «ОК» после чтения.

    Поля убранного алгоритма уходят, имя алгоритма и форма выхода — те,
    что выставлены при чтении (решение 0063). Строками, а не через `json`:
    проверка ниже сверяет файл знак в знак.
    """
    gone = ('"confirm_bars"', '"filter_enabled"', '"on_price_equals_average"',
            '"threshold_percent"')
    lines = [line for line in text.split("\n") if not line.strip().startswith(gone)]
    return (
        "\n".join(lines)
        .replace('"strategy_id": "ema_reverse"', '"strategy_id": "ma_reverse_always"')
        .replace('"time_exit_order": "market"', '"time_exit_order": "limit"')
    )


#: Тот же файл, переписанный нынешней сборкой.
OWNER_FILE_NOW = _migrated(OWNER_FILE)


def test_an_old_file_with_the_removed_algorithm_reads_as_the_only_one(tmp_path) -> None:
    """Стережёт: файл с `ema_reverse` читается как «Реверс с постоянной позицией».

    Решение 0063. Три поведения: имя выставлено, строка про убранный алгоритм
    ушла в журнал (`troubles`), файл при чтении **не переписан** — ни байтом,
    ни временем записи. Переписывает его только «ОК».

    Мутации, обязанные ронять проверку: отказ вместо подмены (тогда строка
    «в этой сборке нет»), подмена молча, запись файла при чтении.
    """
    path = tmp_path / SETTINGS_FILE_NAME
    path.write_text(OWNER_FILE, encoding="utf-8")
    before = path.stat().st_mtime_ns
    loaded = SettingsStore(tmp_path).load()
    assert loaded.values.strategy_id == "ma_reverse_always"
    said = " ".join(loaded.troubles)
    assert (
        "Алгоритм «Реверс по скользящей средней» убран, работает "
        "«Реверс с постоянной позицией»."
    ) in said, said
    assert "в этой сборке нет" not in said, said
    assert loaded.values.time_exit_order is TimeExitKind.LIMIT
    assert not any("threshold_percent" in note for note in loaded.notes), (
        f"поля убранного алгоритма названы незнакомыми: {loaded.notes}"
    )
    assert path.read_text(encoding="utf-8") == OWNER_FILE, "файл переписан при чтении"
    assert path.stat().st_mtime_ns == before, "файл записан при чтении"


def test_a_real_settings_file_survives_a_read_and_a_write(tmp_path) -> None:
    """Стережёт: настоящий файл владельца счёта не портится чтением и записью.

    Читается без единой оговорки и переписывается знак в знак. Проверка
    заведена при переезде таблицы укладки в `ui/settings_codec.py`
    (05.09.2026): формат тогда не менялся, но «не менялся» — это утверждение,
    и стоит оно вечера подобранных настроек. Тест мутационный: поменяй ключ,
    порядок, отступ или вид времени — и файл перестанет совпадать.
    """
    path = tmp_path / SETTINGS_FILE_NAME
    path.write_text(OWNER_FILE_NOW, encoding="utf-8")
    store = SettingsStore(tmp_path)

    loaded = store.load()
    assert loaded.troubles == (), "настоящий файл прочитался с оговорками"
    # ⚠️ Одна оговорка законна и обязательна: в файле лежат ключи потолка
    # объёма, а ПРЕДОХРАНИТЕЛЬ ВЫКЛЮЧЕН НА ЭТАПЕ (D-113). Принять их молча —
    # дефект (правило 13 `CLAUDE.md`). Тревожной она не становится: галочка
    # в этом файле снята, поведение робота не изменилось ничем.
    assert len(loaded.notes) == 1, (
        f"в настоящем файле нашлось непрочитанное поле: {loaded.notes}"
    )
    assert "потолок объёма" in loaded.notes[0], loaded.notes[0]
    assert loaded.values.average_period == 15
    assert loaded.values.take_profit_pct == pytest.approx(0.2)
    assert loaded.values.window_start.hour == 10 and loaded.values.window_start.minute == 20
    assert len(loaded.values.calendar) == 3

    assert store.save(loaded.values) == ""
    assert path.read_text(encoding="utf-8") == OWNER_FILE_NOW, (
        "перезапись изменила файл настроек владельца счёта"
    )


# ------------------------------- выключенные на этапе предохранители (D-113)

def _file_with_guards(folder: pathlib.Path, *, armed: bool) -> SettingsStore:
    """Файл настроек прежней сборки: шесть ключей трёх предохранителей."""
    body = {
        "format_version": FORMAT_VERSION,
        "settings": {
            "average_period": 20,
            "volume": 3,
            "volume_cap_enabled": armed,
            "volume_cap": 7,
            "daily_loss_limit_enabled": armed,
            "daily_loss_limit_pct": 2.5,
            "free_funds_reserve_enabled": armed,
            "free_funds_reserve_pct": 40.0,
        },
    }
    (folder / SETTINGS_FILE_NAME).write_text(
        json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return SettingsStore(folder)


def test_an_old_file_with_the_guards_still_opens_the_program(tmp_path) -> None:
    """Стережёт: файл прежней сборки не роняет запуск и не теряет остальное.

    ПРЕДОХРАНИТЕЛЬ ВЫКЛЮЧЕН НА ЭТАПЕ (D-113). У людей на дисках лежат файлы,
    где шесть ключей трёх предохранителей записаны. Отказ разбора означал бы
    «программа не запускается, потому что в файле стоит потолок объёма»,
    а рядом в файле лежит всё, что человек подбирал руками.
    """
    store = _file_with_guards(tmp_path, armed=True)
    loaded = store.load()
    assert loaded.values.average_period == 20, "остальные настройки потерялись"
    assert loaded.values.volume == 3


def test_an_armed_guard_in_an_old_file_is_not_accepted_in_silence(tmp_path) -> None:
    """Стережёт МОЛЧАНИЕ: включённая в файле защита перестала работать — скажи.

    Самый дорогой случай всей правки. Владелец счёта ставил галочку руками,
    открывает программу — и защиты нет. Принять это молча значит оставить его
    в уверенности, что робот остановится по дневному лимиту.

    Строка идёт в `troubles`, то есть доходит до журнала решений
    **предупреждением**, а не обычной оговоркой: `Loaded.troubles` — это
    «настройки потеряны или могли быть потеряны».
    """
    loaded = _file_with_guards(tmp_path, armed=True).load()
    said = [line for line in loaded.troubles if "предохранител" in line]
    assert said, (
        "включённый в файле предохранитель принят молча — владелец счёта "
        f"вправе считать, что защита работает. Оговорки: {loaded.troubles}"
    )
    whole = said[0].lower()
    for guard in ("потолок объёма", "дневной лимит убытка", "запас свободных средств"):
        assert guard in whole, f"не назван предохранитель «{guard}»: {said[0]!r}"
    assert "не следит" in whole, "не сказано последствие: робот за этим не следит"


def test_a_cleared_guard_in_an_old_file_is_a_note_and_not_an_alarm(tmp_path) -> None:
    """Стережёт вторую половину: снятая галочка — оговорка, а не тревога.

    Поведение робота от неё не изменилось ничем, и кричать здесь значит
    приучить не читать предупреждения. Но и промолчать нельзя: ключи в файле
    остались, и человек вправе знать, почему их больше не видно в окне.
    """
    loaded = _file_with_guards(tmp_path, armed=False).load()
    assert not loaded.troubles, (
        f"снятая галочка поднята до тревоги: {loaded.troubles}"
    )
    said = [line for line in loaded.notes if "предохранител" in line]
    assert said, f"о выключенных предохранителях не сказано ничего: {loaded.notes}"


def test_the_guard_keys_are_not_explained_as_a_newer_build(tmp_path) -> None:
    """Стережёт неправду: сборка не новее, предохранители выключены у нас.

    Общая оговорка про незнакомые ключи говорит «скорее всего файл сделан
    более новой сборкой программы». Отправить человека искать несуществующее
    обновление — это не «сказал», а «соврал».
    """
    loaded = _file_with_guards(tmp_path, armed=False).load()
    for line in loaded.notes + loaded.troubles:
        if "более новой сборкой" in line:
            for key in ("volume_cap", "daily_loss_limit", "free_funds_reserve"):
                assert key not in line, (
                    f"ключ «{key}» объяснён более новой сборкой: {line!r}"
                )


def test_the_numbers_of_the_switched_off_guards_survive_a_write(tmp_path) -> None:
    """Стережёт возврат `D-113`: цифры предохранителей переживают выключение.

    Их подбирал человек. Выбросить их за то, что предохранитель выключен
    на этапе, значит потребовать подбирать заново после возврата.
    """
    store = _file_with_guards(tmp_path, armed=True)
    loaded = store.load()
    assert store.save(loaded.values) == ""
    written = json.loads(
        (tmp_path / SETTINGS_FILE_NAME).read_text(encoding="utf-8")
    )["settings"]
    assert written["volume_cap"] == 7
    assert written["daily_loss_limit_pct"] == 2.5
    assert written["free_funds_reserve_pct"] == 40.0
    assert written["volume_cap_enabled"] is True


def test_a_guard_returned_to_the_build_is_no_longer_called_switched_off() -> None:
    """Стережёт обещание возврата `D-113`: вернувшийся предохранитель не зовётся выключенным.

    Список выключенных считается **от самой сборки**: предохранитель попадает
    в строку про старый файл, только если поля с его именем в таблице полей нет.
    Когда `D-113` вернёт поле в окно, строка о нём обязана замолчать сама —
    без правки, о которой некому будет вспомнить. Иначе после возврата
    человек прочтёт «в этой сборке его НЕТ» про работающий потолок объёма.

    Вход подставной: таблица полей, в которой потолок объёма уже вернулся,
    а два других предохранителя ещё нет. Контрольная половина — о двух
    невернувшихся строка по-прежнему есть: «молчать всегда» тест не проходит.
    """
    from app.settings_store import _guards_off_in_the_file

    raw: dict[str, object] = {
        "volume_cap_enabled": True,
        "volume_cap": 7,
        "daily_loss_limit_enabled": True,
        "daily_loss_limit_pct": 2.5,
        "free_funds_reserve_enabled": True,
        "free_funds_reserve_pct": 40.0,
    }
    table = {**field_codecs(), "volume_cap_enabled": None, "volume_cap": None}
    notes, troubles = _guards_off_in_the_file(raw, table)
    said = " ".join(notes + troubles).lower()
    assert "потолок объёма" not in said, (
        "потолок объёма вернулся в сборку, а строка про старый файл всё ещё "
        f"называет его выключенным: {said!r}"
    )
    for guard in ("дневной лимит убытка", "запас свободных средств"):
        assert guard in said, (
            f"невернувшийся предохранитель «{guard}» больше не назван: {said!r}"
        )


# ------------------------------------------------ порядок цен минутки (З9 Ф4)

def test_a_broken_minute_order_is_replaced_out_loud(store: SettingsStore) -> None:
    """Стережёт: негодный порядок цен минутки в файле — умолчание и оговорка.

    Оговорка идёт в `troubles`, то есть до журнала решений предупреждением;
    молча подменённое значение — дефект (правило 13).
    """
    assert store.save(Settings(minute_order=MinutePriceOrder.ADVERSE_FIRST)) == ""
    body = json.loads(store.path.read_text(encoding="utf-8"))
    body["settings"]["minute_order"] = "zigzag"
    store.path.write_text(json.dumps(body), encoding="utf-8")

    read = SettingsStore(store.path.parent).load()

    assert read.values.minute_order is MinutePriceOrder.NEAR_FIRST
    caption = f"«{FIELD_CAPTIONS['minute_order']}»"
    assert any(caption in line and "zigzag" in line for line in read.troubles), (
        read.troubles
    )


def test_a_file_without_the_minute_order_is_read_and_left_as_it_is(
    store: SettingsStore,
) -> None:
    """Стережёт: файл прежней сборки без ключа читается, оговорка — и не переписан.

    Переписать файл настроек имеет право только «Применить» человека.
    """
    assert store.save(Settings(average_period=21)) == ""
    body = json.loads(store.path.read_text(encoding="utf-8"))
    del body["settings"]["minute_order"]
    before = json.dumps(body, ensure_ascii=False, indent=2)
    store.path.write_text(before, encoding="utf-8")

    read = SettingsStore(store.path.parent).load()

    assert read.troubles == (), read.troubles
    assert read.values.average_period == 21
    assert read.values.minute_order is MinutePriceOrder.NEAR_FIRST
    assert any(f"«{FIELD_CAPTIONS['minute_order']}»" in line for line in read.notes), read.notes
    assert store.path.read_text(encoding="utf-8") == before


# ------------------------------- наибольшая свеча для проверки по минуткам (B-058)

@pytest.mark.parametrize("broken", [30, True, "5", None])
def test_a_broken_minute_bar_limit_is_replaced_out_loud(
    store: SettingsStore, broken: object
) -> None:
    """Стережёт: негодный порог в файле — умолчание и оговорка с ключом и значением.

    30 — размер свечи, на котором прогон останавливается (`B-058`); `true`
    для Python равно единице и без отдельной проверки молча стало бы
    «1 минутой» (`ui/settings_codec.py::_enum_codec`). Молча подменённое
    значение — дефект (правило 13).
    """
    assert store.save(Settings(minute_bar_limit=MinuteBarLimit.FIFTEEN)) == ""
    body = json.loads(store.path.read_text(encoding="utf-8"))
    body["settings"]["minute_bar_limit"] = broken
    store.path.write_text(json.dumps(body), encoding="utf-8")

    read = SettingsStore(store.path.parent).load()

    assert read.values.minute_bar_limit is MinuteBarLimit.FIVE
    shown_raw = json.dumps(broken)
    caption = f"«{FIELD_CAPTIONS['minute_bar_limit']}»"
    assert any(
        caption in line and shown_raw in line for line in read.troubles
    ), read.troubles


def test_a_file_without_the_minute_bar_limit_is_read_and_left_as_it_is(
    store: SettingsStore,
) -> None:
    """Стережёт: файл прежней сборки без ключа читается с умолчанием и не переписан."""
    assert store.save(Settings(average_period=21)) == ""
    body = json.loads(store.path.read_text(encoding="utf-8"))
    del body["settings"]["minute_bar_limit"]
    before = json.dumps(body, ensure_ascii=False, indent=2)
    store.path.write_text(before, encoding="utf-8")

    read = SettingsStore(store.path.parent).load()

    assert read.troubles == (), read.troubles
    assert read.values.average_period == 21
    assert read.values.minute_bar_limit is MinuteBarLimit.FIVE
    assert any(f"«{FIELD_CAPTIONS['minute_bar_limit']}»" in line for line in read.notes), read.notes
    assert store.path.read_text(encoding="utf-8") == before
