"""README.txt Linux-поставки (tar.gz) объясняет, как обновить программу, не потеряв данные.

Проверяется **файл, который пишет сборщик** (`tools/linux_package.package`).
Вход подставной: вместо результата Nuitka — пустая папка во временном
каталоге; `ROOT`, `SOURCE`, `TARGET` подменены, `build/` и `dist/` проекта
не трогаются. Архив пишется туда же, во временный каталог.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from tools import linux_package

UPDATE_HEADING = "Как обновить программу до новой версии"
DATA_HEADING = "Куда программа пишет свои данные"

#: «Распакуйте в новую папку, перенесите в неё userdata» рядом с «поверх» —
#: две инструкции, и человек выберет не ту. Любым словом: новую, отдельную,
#: другую, пустую, чистую папку; перенести.
OLD_WAY = re.compile(r"(?:нов|отдельн|друг|пуст|чист)\w*\s+(?:\w+\s+)?папк|перенес", re.IGNORECASE)


def _packaged_readme(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> str:
    source = tmp_path / "build" / "main.dist"
    source.mkdir(parents=True)
    (source / "Terminal").write_bytes(b"")
    monkeypatch.setattr(linux_package, "ROOT", tmp_path)
    monkeypatch.setattr(linux_package, "SOURCE", source)
    monkeypatch.setattr(linux_package, "TARGET", tmp_path / "dist" / "Terminal")
    assert linux_package.package() == 0
    return (tmp_path / "dist" / "Terminal" / "README.txt").read_text(encoding="utf-8")


def test_the_packaged_linux_readme_tells_to_unpack_over_and_keep_userdata(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Стережёт D-080: README велит распаковать поверх, не удалять userdata, вернуть из копии."""
    text = _packaged_readme(tmp_path, monkeypatch)
    assert UPDATE_HEADING in text, "в README поставки нет раздела об обновлении"
    section = " ".join(text.split(UPDATE_HEADING, 1)[1].split("\n\n\n", 1)[0].split())
    for phrase in (
        "распакуйте новый архив поверх прежней папки",
        "не внутри неё",
        'Папку "userdata" не удаляйте',
        "скопируйте её обратно из резервной копии",
        "В резервной копии лежит ключ к счёту: храните её как пароль",
        "не на рабочем столе с синхронизацией и не на чужой флешке",
        "«Данных нет»",
        'Вернуться на старую версию с той же папкой "userdata" нельзя',
    ):
        assert phrase in section, f"в разделе обновления нет «{phrase}»:\n{section}"
    old_way = OLD_WAY.findall(section)
    assert not old_way, f"в разделе вторая инструкция {old_way}:\n{section}"


def test_the_packaged_linux_readme_tells_to_move_the_folder_not_copy_it(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Перенос на другой компьютер — папкой целиком, старую удалить: ключ к счёту едет с ней.

    Проверяется раздел о данных, где человек ищет, как переехать, а не весь
    файл: фраза о переносе в другом разделе при «скопируйте» здесь — две
    инструкции. Копирования в разделе нет никаким словом. Мутации: вернулась
    «скопируйте папку "Terminal" целиком»; пропало «и удалите старую»;
    раздел велит скопировать, а «перенесите» уехало в другой раздел.
    """
    text = _packaged_readme(tmp_path, monkeypatch)
    assert DATA_HEADING in text, "в README поставки нет раздела о данных"
    section = " ".join(text.split(DATA_HEADING, 1)[1].split("\n\n\n", 1)[0].split())
    assert 'перенесите папку "Terminal" целиком и удалите старую' in section, section
    copying = re.findall(r"скопир\w*", section, re.IGNORECASE)
    assert not copying, f"раздел о данных велит копировать {copying}:\n{section}"
