"""README.txt Windows-поставки объясняет, как обновить программу, не потеряв свечи.

Владелец счёта обновляется распаковкой нового архива поверх прежней папки
(D-080). Без раздела «Как обновить программу до новой версии» он удалит
старую папку вместе с «userdata». Проверяется **файл, который
пишет сборщик поставки** (`tools/windows_package.package`), а не строка
в исходнике: сломанная запись файла с целой строкой иначе зеленела бы.

Вход подставной: вместо результата Nuitka — пустая папка во временном
каталоге, сборка и `dist/` проекта не трогаются.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from tools import windows_package

#: Заголовок раздела, который ищет владелец счёта.
UPDATE_HEADING = "Как обновить программу до новой версии"

#: Смысл раздела по решению владельца счёта 11.10.2026; пробелы схлопнуты.
UPDATE_PHRASES = (
    "распаковать новый архив поверх прежней папки",
    "не в саму «Terminal»",
    "Папку «userdata» не удаляйте",
    "скопируйте её обратно из резервной копии",
    "В резервной копии лежит ключ к счёту: храните её как пароль",
    "не на рабочем столе с синхронизацией и не на чужой флешке",
    "«Данных нет»",
    "Вернуться на старую версию с той же папкой «userdata» нельзя",
)

#: Прежний способ — «распакуйте в новую папку, перенесите в неё userdata» —
#: рядом с «поверх» даёт две инструкции, и человек выберет не ту. Любым
#: словом: новую, отдельную, другую, пустую, чистую папку; перенести.
OLD_WAY = re.compile(r"(?:нов|отдельн|друг|пуст|чист)\w*\s+(?:\w+\s+)?папк|перенес", re.IGNORECASE)


def _packaged_readme(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Собрать поставку из пустой папки и вернуть README.txt текстом."""
    source = tmp_path / "build" / "main.dist"
    source.mkdir(parents=True)
    (source / "Terminal.exe").write_bytes(b"")
    monkeypatch.setattr(windows_package, "ROOT", tmp_path)
    monkeypatch.setattr(windows_package, "SOURCE", source)
    monkeypatch.setattr(windows_package, "TARGET", tmp_path / "dist" / "Terminal")
    assert windows_package.package() == 0
    raw = (tmp_path / "dist" / "Terminal" / "README.txt").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "README без BOM: «Блокнот» покажет кракозябры"
    return raw[3:].decode("utf-8").replace("\r\n", "\n")


def test_the_packaged_readme_tells_to_unpack_over_the_old_folder_and_keep_userdata(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Стережёт D-080: README велит распаковать поверх, не удалять «userdata», вернуть из копии."""
    text = _packaged_readme(tmp_path, monkeypatch)
    assert UPDATE_HEADING in text, "в README поставки нет раздела об обновлении"
    section = " ".join(text.split(UPDATE_HEADING, 1)[1].split("\n\n\n", 1)[0].split())
    for phrase in UPDATE_PHRASES:
        assert phrase in section, f"в разделе обновления нет «{phrase}»:\n{section}"
    old_way = OLD_WAY.findall(section)
    assert not old_way, f"в разделе осталась старая инструкция {old_way}:\n{section}"


#: Раздел, где человек ищет, как переехать на другой компьютер.
DATA_HEADING = "Куда программа пишет свои данные"


def test_the_packaged_readme_tells_to_move_the_folder_not_copy_it(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Перенос на другой компьютер — папкой целиком, старую удалить: ключ к счёту едет с ней.

    Ответ владельца счёта 12.10.2026, как у README Linux. Мутации: вернулась
    «скопируйте папку «Terminal» целиком»; пропало «и удалите старую».
    """
    text = _packaged_readme(tmp_path, monkeypatch)
    assert DATA_HEADING in text, "в README поставки нет раздела о данных"
    section = " ".join(text.split(DATA_HEADING, 1)[1].split("\n\n\n", 1)[0].split())
    assert "перенесите папку «Terminal» целиком и удалите старую" in section, section
    copying = re.findall(r"скопир\w*", section, re.IGNORECASE)
    assert not copying, f"раздел о данных велит копировать {copying}:\n{section}"


def test_the_packaged_readme_names_the_version_being_packaged(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Стережёт B-061: README поставки называет версию сборки, а не зашитую «0.1.0»."""
    monkeypatch.setattr(windows_package, "APP_VERSION", "9.8.7")
    text = _packaged_readme(tmp_path, monkeypatch)
    assert "Версия 9.8.7." in text, "README поставки не называет версию сборки"
    assert "{version}" not in text, "в README осталась незаполненная подстановка"
