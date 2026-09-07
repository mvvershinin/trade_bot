"""Где программа складывает то, что создаёт сама (решение 0003).

Ошибка здесь не падает, а тихо теряет: файл токена, база свечей и оба журнала
стираются при выходе, владелец счёта вводит 90-дневный токен заново
при каждом запуске, а история перекачивается с нуля.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from market import paths


def test_from_sources_userdata_is_next_to_the_layers(monkeypatch) -> None:
    monkeypatch.delenv("APPIMAGE", raising=False)
    root = pathlib.Path(paths.__file__).resolve().parent.parent
    assert paths.userdata_dir() == root / "userdata"
    assert paths.default_db_path().name == paths.DB_FILE_NAME


def test_appimage_anchor_is_the_file_not_the_mount(tmp_path: pathlib.Path, monkeypatch) -> None:
    """Якорь для AppImage — путь самого файла .AppImage.

    Образ монтируется только на чтение, и `__file__` указывает внутрь него:
    `mkdir` там даст «read-only file system». `APPDIR` — это монтирование,
    брать его нельзя.
    """
    image_file = tmp_path / "Терминал.AppImage"
    image_file.write_bytes(b"")
    monkeypatch.setenv("APPIMAGE", str(image_file))
    monkeypatch.setenv("APPDIR", "/tmp/.mount_readonly")

    assert paths.userdata_dir() == tmp_path / "userdata"


def test_ensure_creates_directory_and_probes_write(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "userdata"
    assert paths.ensure_userdata_dir(target) == target
    assert target.is_dir()
    assert not (target / ".write-probe").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="под корень запись проходит куда угодно")
def test_unwritable_directory_is_a_loud_error(tmp_path: pathlib.Path) -> None:
    """Непишущаяся папка — явный отказ, а не тихая запись в никуда.

    То же на Windows: распакованная в Program Files папка не пишется,
    и виртуализации файлов для 64-битного процесса нет.
    """
    target = tmp_path / "только-чтение"
    target.mkdir()
    target.chmod(0o500)
    try:
        with pytest.raises(OSError, match="нельзя писать"):
            paths.ensure_userdata_dir(target)
    finally:
        target.chmod(0o700)
