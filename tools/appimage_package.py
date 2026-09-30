r"""Собрать `Terminal.AppImage` из `build/main.dist` — решения 0001/0022.

Запуск после `tools/build.py` и `tools/make_icon.py --png`:

    python3 tools/appimage_package.py \
        --dist build/main.dist --appdir build/Terminal.AppDir \
        --out dist --version 0.2.0 \
        --appimagetool /work/appimagetool-x86_64.AppImage \
        --runtime /work/runtime-x86_64 \
        --icon tools/terminal.png

`appimagetool` и `runtime` (type2) скачиваются и проверяются по sha256
отдельно, до вызова этого скрипта — см. `.github/workflows/release.yml`,
джоб `build-linux`. Ничего не должно тянуться молча во время сборки.

`appimagetool` сам является AppImage: если `/dev/fuse` недоступен (обычный
случай контейнера сборки), перед запуском выставить
`APPIMAGE_EXTRACT_AND_RUN=1` в окружении — так делает release.yml.

Имя выходного файла стабильно — `Terminal.AppImage`, без версии в имени
(решение 0002 п. 3: меняющееся имя выглядит для репутационных механизмов
как новая неизвестная программа при каждом релизе).
"""
from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess

DESKTOP = """[Desktop Entry]
Type=Application
Name=Терминал
Comment=Автоматическая торговля на Московской бирже
Exec=Terminal
Icon=terminal
Categories=Office;Finance;
Terminal=false
"""

APPRUN = """#!/bin/sh
# AppRun запускается из точки монтирования (только чтение). userdata
# ищется программой рядом с самим файлом .AppImage через переменную
# APPIMAGE, которую appimagetool/type2-runtime выставляет сама —
# market/paths.py уже это читает. Здесь ничего дополнительно
# прокидывать не нужно.
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/Terminal" "$@"
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", required=True, type=pathlib.Path)
    parser.add_argument("--appdir", required=True, type=pathlib.Path)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--appimagetool", required=True, type=pathlib.Path)
    parser.add_argument("--runtime", required=True, type=pathlib.Path)
    parser.add_argument("--icon", required=True, type=pathlib.Path)
    args = parser.parse_args()

    if args.appdir.exists():
        shutil.rmtree(args.appdir)
    args.appdir.mkdir(parents=True)

    # Всё дерево Nuitka standalone едет внутрь AppDir как есть.
    for item in args.dist.iterdir():
        target = args.appdir / item.name
        if item.is_dir():
            shutil.copytree(item, target, symlinks=True)
        else:
            shutil.copy2(item, target)

    # Следы прогонов на машине сборки — там настройки и, возможно, токен.
    # Та же защита, что в linux_package.py/windows_package.py.
    leftovers = args.appdir / "userdata"
    if leftovers.exists():
        shutil.rmtree(leftovers)
        print("снята userdata/ от прогонов на машине сборки")

    (args.appdir / "Terminal.desktop").write_text(DESKTOP, encoding="utf-8")
    apprun = args.appdir / "AppRun"
    apprun.write_text(APPRUN, encoding="utf-8")
    apprun.chmod(0o755)

    shutil.copy2(args.icon, args.appdir / "terminal.png")

    binary = args.appdir / "Terminal"
    if binary.is_file():
        binary.chmod(binary.stat().st_mode | 0o111)

    args.out.mkdir(parents=True, exist_ok=True)
    out_file = args.out / "Terminal.AppImage"
    if out_file.exists():
        out_file.unlink()

    env = dict(os.environ, VERSION=args.version)
    cmd = [
        str(args.appimagetool),
        "--runtime-file", str(args.runtime),
        str(args.appdir), str(out_file),
    ]
    print(" ".join(cmd))
    code = subprocess.call(cmd, env=env)
    if code != 0:
        return code

    out_file.chmod(out_file.stat().st_mode | 0o111)
    print(f"{out_file}: {out_file.stat().st_size / 1024 / 1024:.0f} МБ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
