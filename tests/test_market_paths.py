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


# -- переменная папки данных действует только в тестовом прогоне (ревью, находка 9)

@pytest.mark.parametrize(
    ("marker", "frozen", "honoured"),
    [("1", False, True), (None, False, False), ("0", False, False), ("1", True, False)],
)
def test_userdata_variable_works_only_in_a_test_run_from_sources(
    tmp_path: pathlib.Path, monkeypatch, marker: str | None, frozen: bool, honoured: bool,
) -> None:
    """Случайная `TERMINAL_USERDATA` в поставке не уводит к другой базе и замку.

    Действует только вместе с `TERMINAL_TEST_RUN=1` и только из исходников;
    иначе папка — обычная, а `ignored_override` даёт строку для журнала.
    """
    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.setenv(paths.USERDATA_ENV, str(tmp_path / "чужая"))
    if marker is None:
        monkeypatch.delenv(paths.TEST_RUN_ENV, raising=False)
    else:
        monkeypatch.setenv(paths.TEST_RUN_ENV, marker)
    monkeypatch.setattr(paths, "_is_frozen", lambda: frozen)
    standard = (
        pathlib.Path(paths.sys.executable).resolve().parent / "userdata" if frozen
        else pathlib.Path(paths.__file__).resolve().parent.parent / "userdata"
    )

    if honoured:
        assert paths.userdata_dir() == (tmp_path / "чужая").resolve()
        assert paths.ignored_override() == ""
    else:
        assert paths.userdata_dir() == standard
        assert paths.default_db_path().parent == standard
        assert paths.USERDATA_ENV in paths.ignored_override()


def test_userdata_variable_does_not_act_inside_an_appimage(
    tmp_path: pathlib.Path, monkeypatch,
) -> None:
    """AppImage — поставка, даже если оба признака прогона выставлены.

    Папка — рядом с образом, как у любого запуска AppImage, а не та, что
    в переменной: иначе забытая переменная увела бы собранную программу
    к базе без стоящей остановки робота и к другому замку «одна копия».
    """
    image = tmp_path / "Terminal.AppImage"
    monkeypatch.setenv("APPIMAGE", str(image))
    monkeypatch.setenv(paths.USERDATA_ENV, str(tmp_path / "чужая"))
    monkeypatch.setenv(paths.TEST_RUN_ENV, "1")
    monkeypatch.setattr(paths, "_is_frozen", lambda: False)

    assert paths.userdata_dir() == tmp_path.resolve() / "userdata"
    assert paths.USERDATA_ENV in paths.ignored_override()


def test_no_variable_no_line(monkeypatch) -> None:
    """Переменной нет — и сказать нечего: строка не должна появляться на каждом запуске."""
    monkeypatch.delenv(paths.USERDATA_ENV, raising=False)
    assert paths.ignored_override() == ""


def test_a_base_named_by_a_key_is_named_in_the_line(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Стережёт: база, названная ключом вне папки данных, называется в строке.

    `D-124`: у консольных запусков база бывает названа ключом (`--db`).
    Фраза «база берётся из папки данных» была бы там неправдой — ровно там,
    где человек ищет, куда делись его свечи.
    """
    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.setenv(paths.USERDATA_ENV, str(tmp_path / "чужая"))
    monkeypatch.delenv(paths.TEST_RUN_ENV, raising=False)
    elsewhere = tmp_path / "своя" / "candles.sqlite3"
    line = paths.ignored_override(elsewhere)
    assert str(elsewhere) in line, line
    assert "база, журналы и остановка робота берутся из" not in line, line
    usual = paths.ignored_override(paths.default_db_path())
    assert usual == paths.ignored_override(), "база в папке данных — прежняя фраза"
