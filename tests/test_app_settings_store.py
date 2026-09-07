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
from ui.models import (
    AfterTakeProfit,
    AverageKind,
    CalendarDay,
    OnPriceEqualsAverage,
    ReversalMoment,
    Settings,
)
from ui.settings_codec import field_codecs

#: Набор, у которого **каждое** поле отличается от умолчания. Списком, а не
#: `replace` от умолчаний: проверка «пережило перезапуск» на наборе, совпавшем
#: с умолчанием хотя бы одним полем, это поле не проверяет вовсе — оно
#: совпадёт и при полностью потерянном файле.
DIFFERENT = Settings(
    instrument="MXZ6",
    timeframe="15 минут",
    depth_days=45,
    history_depth_days=120,
    average_period=21,
    average_kind=AverageKind.SMA,
    filter_enabled=True,
    threshold_percent=0.35,
    confirm_bars=3,
    reversal_moment=ReversalMoment.SAME_BAR,
    after_take_profit=AfterTakeProfit.WAIT_FOR_SIGNAL,
    on_price_equals_average=OnPriceEqualsAverage.TREAT_AS_LONG,
    take_profit_enabled=False,
    take_profit_pct=1.25,
    trailing_enabled=True,
    trailing_start_pct=1.5,
    trailing_offset_pct=0.75,
    trailing_step_pct=0.11,
    window_start=time(9, 30),
    window_end=time(11, 30),
    close_on_time_end=False,
    volume=4,
    volume_cap_enabled=True,
    volume_cap=9,
    daily_loss_limit_enabled=True,
    daily_loss_limit_pct=3.5,
    free_funds_reserve_enabled=True,
    free_funds_reserve_pct=12.5,
    commission_per_side_rub=None,
    price_step=25.0,
    ruble_per_point=1.73774,
    ruble_per_point_source="биржа, RIU6, 04.09.2026 07:00",
    slippage_steps=1.5,
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

def test_the_sample_differs_from_the_defaults_in_every_field() -> None:
    """Образец для проверок отличается от умолчания **каждым** полем.

    Без этой проверки все остальные вакуумны наполовину: поле, случайно
    совпавшее с умолчанием, «переживает перезапуск» и при полностью
    потерянном файле.
    """
    default = Settings()
    same = [name for name in _names() if getattr(DIFFERENT, name) == getattr(default, name)]
    assert not same, (
        "поля образца совпали с умолчанием, и проверка сохранения их не видит: "
        + ", ".join(same)
    )


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
    assert any("average_period" in trouble for trouble in read.troubles), (
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
    assert any("volume" in trouble for trouble in read.troubles)


def test_an_unknown_value_of_a_choice_is_refused(store: SettingsStore) -> None:
    """Незнакомое значение списка — отказ, а не первый элемент."""
    store.save(DIFFERENT)
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["settings"]["average_kind"] = "smma"
    store.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    read = SettingsStore(store.path.parent).load()
    assert read.values.average_kind is Settings().average_kind
    assert any("average_kind" in trouble for trouble in read.troubles)


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
    assert any("slippage_steps" in note for note in read.notes)


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
    "filter_enabled": false,
    "free_funds_reserve_enabled": false,
    "free_funds_reserve_pct": 30.0,
    "history_depth_days": 90,
    "instrument": "MXU6",
    "log_directory": "",
    "on_price_equals_average": "skip",
    "price_step": 1.0,
    "reversal_moment": "same_bar",
    "ruble_per_point": 1.0,
    "ruble_per_point_source": "биржей — карточка MXU6",
    "slippage_steps": 1.0,
    "take_profit_enabled": true,
    "take_profit_pct": 0.2,
    "threshold_percent": 0.4,
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


def test_a_real_settings_file_survives_a_read_and_a_write(tmp_path) -> None:
    """Стережёт: настоящий файл владельца счёта не портится чтением и записью.

    Читается без единой оговорки и переписывается знак в знак. Проверка
    заведена при переезде таблицы укладки в `ui/settings_codec.py`
    (05.09.2026): формат тогда не менялся, но «не менялся» — это утверждение,
    и стоит оно вечера подобранных настроек. Тест мутационный: поменяй ключ,
    порядок, отступ или вид времени — и файл перестанет совпадать.
    """
    path = tmp_path / SETTINGS_FILE_NAME
    path.write_text(OWNER_FILE, encoding="utf-8")
    store = SettingsStore(tmp_path)

    loaded = store.load()
    assert loaded.troubles == (), "настоящий файл прочитался с оговорками"
    assert loaded.notes == (), "в настоящем файле нашлось непрочитанное поле"
    assert loaded.values.average_period == 15
    assert loaded.values.take_profit_pct == pytest.approx(0.2)
    assert loaded.values.window_start.hour == 10 and loaded.values.window_start.minute == 20
    assert len(loaded.values.calendar) == 3

    assert store.save(loaded.values) == ""
    assert path.read_text(encoding="utf-8") == OWNER_FILE, (
        "перезапись изменила файл настроек владельца счёта"
    )
