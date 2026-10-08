"""README.txt Windows-поставки объясняет, как обновить программу, не потеряв свечи.

Владелец счёта обновляется распаковкой нового архива. Без раздела
«Как обновить программу до новой версии» он либо распакует поверх и останется
без папки «userdata», либо скачает свечи заново. Проверяется **файл, который
пишет сборщик поставки** (`tools/windows_package.package`), а не строка
в исходнике: сломанная запись файла с целой строкой иначе зеленела бы.

Вход подставной: вместо результата Nuitka — пустая папка во временном
каталоге, сборка и `dist/` проекта не трогаются.
"""

from __future__ import annotations

import pathlib

import pytest

from tools import windows_package

#: Заголовок раздела, который ищет владелец счёта.
UPDATE_HEADING = "Как обновить программу до новой версии"


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


def test_the_packaged_readme_tells_how_to_update_without_losing_candles(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Стережёт: в README поставки есть раздел обновления — с переносом «userdata» и копией."""
    text = _packaged_readme(tmp_path, monkeypatch)
    assert UPDATE_HEADING in text, "в README поставки нет раздела об обновлении"
    section = text.split(UPDATE_HEADING, 1)[1].split("\n\n\n", 1)[0]
    for step in (
        "Закройте «Терминал»",
        "Скопируйте папку «userdata» куда-нибудь про запас",
        "Перенесите в неё папку «userdata» из старой папки",
    ):
        assert step in section, f"в разделе обновления нет шага «{step}»:\n{section}"
    assert "Вернуться на старую версию с той же папкой «userdata» нельзя" in section, (
        f"нет предупреждения, что старая версия не откроет обновлённую базу:\n{section}"
    )


def test_the_packaged_readme_names_the_version_being_packaged(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Стережёт B-061: README поставки называет версию сборки, а не зашитую «0.1.0»."""
    monkeypatch.setattr(windows_package, "APP_VERSION", "9.8.7")
    text = _packaged_readme(tmp_path, monkeypatch)
    assert "Версия 9.8.7." in text, "README поставки не называет версию сборки"
    assert "{version}" not in text, "в README осталась незаполненная подстановка"
