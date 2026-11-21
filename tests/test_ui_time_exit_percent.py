"""Отступ предельной цены выхода по концу окна — процентом, а не шагами цены.

Решение 0067, фаза 1.2 плана координатора 04.11.2026. Что стережётся,
по одному поведению на проверку:

* старый ключ файла `time_exit_limit_steps` не пропадает молча: владелец
  счёта получает **одно** предупреждение с подписью нового поля, прежним
  значением и тем, что взято вместо него; старый ключ не выдаётся
  за «незнакомый от более новой сборки» и при записи из файла уходит;
* поле окна — дробное, в процентах, с границами из той же пары чисел,
  что лежит в `ui/models.py`, и дробное значение доезжает до `Settings`
  без округления до целого; умолчание — 2 %, выбор владельца счёта.

Дорога предупреждения от `Loaded.troubles` до журнала решений — общая
для всех потерянных полей (`app/main.py::_say_what_was_read`) и стережётся
своими проверками; здесь — что строка в `troubles` есть и она одна.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from PySide6.QtWidgets import QDoubleSpinBox

from app.settings_store import SettingsStore
from tests.test_ui_settings import make_dialog  # noqa: F401 — фикстура
from ui.models import FIELD_CAPTIONS, TIME_EXIT_LIMIT_PCT_LIMITS, Settings

#: Старый ключ файла и новое поле — латиницей, как в файле.
OLD_KEY = "time_exit_limit_steps"
NEW_KEY = "time_exit_limit_pct"


def _file_with_the_old_key(folder: pathlib.Path, steps: int) -> SettingsStore:
    """Файл, записанный сборкой до решения 0067: шаги есть, процента нет."""
    store = SettingsStore(folder)
    assert store.save(Settings()) == ""
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    del payload["settings"][NEW_KEY]
    payload["settings"][OLD_KEY] = steps
    store.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return store


def test_the_old_steps_key_is_said_once_as_a_loss_and_leaves_the_file(tmp_path) -> None:
    """Стережёт: шаги в файле — одно предупреждение о новом поле, ключ уходит.

    ⚠️ Мутации, обязанные ронять проверку: в `ui/settings_codec.py::read_fields`
    поле, сменившее ключ, снова идёт в «пропущенные» (предупреждения нет —
    молчание о потерянном значении); из `app/settings_store.py` убрано
    исключение `REPLACED_KEYS` (старый ключ назван «незнакомым от более новой
    сборки» и возвращается в файл при каждой записи).
    """
    store = _file_with_the_old_key(tmp_path, steps=13)
    loaded = store.load()

    caption = f"«{FIELD_CAPTIONS[NEW_KEY]}»"
    lines = [line for line in loaded.troubles if caption in line]
    assert len(lines) == 1, (
        f"о прежнем отступе в шагах сказано {len(lines)} раз: {loaded.troubles}"
    )
    line = lines[0]
    assert "шагах цены: 13" in line, f"прежнее значение не названо: {line}"
    assert "«2 %»" in line, f"не сказано, что взято вместо него: {line}"
    assert OLD_KEY not in line and NEW_KEY not in line, (
        f"имя поля латиницей человеку: {line}"
    )
    assert loaded.values.time_exit_limit_pct == Settings().time_exit_limit_pct

    notes = " ".join(loaded.notes)
    assert OLD_KEY not in notes, f"старый ключ назван незнакомым: {notes}"
    assert caption not in notes, f"о том же поле вторая, общая строка: {notes}"

    assert store.save(loaded.values) == ""
    written = json.loads(store.path.read_text(encoding="utf-8"))["settings"]
    assert OLD_KEY not in written, "старый ключ вернулся в файл при записи"
    assert written[NEW_KEY] == pytest.approx(2.0), written.get(NEW_KEY)


def test_the_offset_field_is_a_fraction_of_a_percent_within_the_shared_limits(
    make_dialog,  # noqa: F811 — фикстура
) -> None:
    """Стережёт: поле дробное, в процентах, границы — `TIME_EXIT_LIMIT_PCT_LIMITS`.

    Целое поле молча округлило бы 2,5 % до 2 или 3, и в журнале изменений
    стояло бы не то число, которое владелец счёта вписал. Границы — одна
    пара на окно и на проверку при «Применить».
    """
    assert Settings().time_exit_limit_pct == pytest.approx(2.0), (
        "умолчание отступа — 2 %, выбор владельца счёта (решение 0067)"
    )
    dialog = make_dialog(Settings(time_exit_limit_pct=2.5))
    box = dialog.time_exit_limit
    assert isinstance(box, QDoubleSpinBox), type(box)
    assert (box.minimum(), box.maximum()) == pytest.approx(TIME_EXIT_LIMIT_PCT_LIMITS)
    assert box.suffix().strip() == "%", box.suffix()
    assert dialog.values().time_exit_limit_pct == pytest.approx(2.5)
