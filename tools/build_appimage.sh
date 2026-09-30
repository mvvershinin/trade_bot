#!/bin/sh
# Собрать Terminal.AppImage от начала до конца — один рецепт, который
# бежит и в CI (`.github/workflows/release.yml`, джоб build-linux, внутри
# `container: ubuntu:22.04`), и локально в том же самом голом контейнере
# для проверки перед тегом. Один файл — чтобы «проверено локально» и
# «идёт в CI» были буквально одной и той же командой, а не Dockerfile,
# разложенным на отдельные шаги YAML, который локально не проверить целиком.
#
# Решение 0001: Nuitka standalone (не --onefile), старая LTS-база (glibc
# 2.35 подтверждён objdump -T в отчёте .docs/packaging/packaging-expert-
# 2026-09-30-1106.md), без UPX. Версия Nuitka зафиксирована в uv.lock
# (`uv sync --locked`), а не берётся молча свежей.
#
# Запуск внутри голого ubuntu:22.04 (без предварительно поставленного
# python3/git — сам ставит всё, что нужно), из корня рабочей копии:
#
#     sh tools/build_appimage.sh
#
# Переменная TERMINAL_VERSION — версия, которая едет в pyproject.toml,
# заголовок окна и журнал решений (тот же механизм, что tools/build.py
# и tools/set_version.py). По умолчанию 0.1.0, как везде в проекте.
#
# Результат: dist/Terminal.AppImage.
set -eu

TERMINAL_VERSION="${TERMINAL_VERSION:-0.1.0}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$ROOT/build/appimage-tools"
export DEBIAN_FRONTEND=noninteractive
export UV_PYTHON_DOWNLOADS=never
export PYTHONUTF8=1
export LANG=C.UTF-8

echo "== 1/7 системные пакеты =="
apt-get update -qq
apt-get install -y --no-install-recommends \
    software-properties-common curl ca-certificates gnupg \
    build-essential patchelf ccache zlib1g-dev \
    file
add-apt-repository -y ppa:deadsnakes/ppa
apt-get update -qq
apt-get install -y --no-install-recommends python3.12 python3.12-dev python3.12-venv
rm -rf /var/lib/apt/lists/*

echo "== 2/7 uv 0.12.7 (та же версия, что в остальных джобах release.yml) =="
curl -LsSf https://astral.sh/uv/0.12.7/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

cd "$ROOT"
uv python pin 3.12
uv sync --locked --all-groups

echo "== 3/7 версия из TERMINAL_VERSION в pyproject.toml =="
uv run python tools/set_version.py "$TERMINAL_VERSION"
uv sync --all-groups
FOUND="$(uv run --no-sync python -c "from importlib import metadata; print(metadata.version('terminal'))")"
echo "importlib.metadata.version('terminal') = $FOUND"
if [ "$FOUND" != "$TERMINAL_VERSION" ]; then
    echo "версия в метаданных пакета ('$FOUND') разошлась с ожидаемой ('$TERMINAL_VERSION') — заголовок окна и журнал решений соврали бы молча, останавливаюсь" >&2
    exit 1
fi

echo "== 4/7 сборка Nuitka =="
uv run python tools/build.py

echo "== 5/7 иконка для AppDir (PNG, без Qt-офскрина) =="
uv run python tools/make_icon.py --png

echo "== 6/7 appimagetool и type2-runtime — скачать и сверить sha256 =="
mkdir -p "$WORK"
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage"
APPIMAGETOOL_SHA256="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64"
RUNTIME_SHA256="2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d"

curl -LsSf -o "$WORK/appimagetool-x86_64.AppImage" "$APPIMAGETOOL_URL"
echo "$APPIMAGETOOL_SHA256  $WORK/appimagetool-x86_64.AppImage" | sha256sum -c -
chmod +x "$WORK/appimagetool-x86_64.AppImage"

curl -LsSf -o "$WORK/runtime-x86_64" "$RUNTIME_URL"
echo "$RUNTIME_SHA256  $WORK/runtime-x86_64" | sha256sum -c -

echo "== 7/7 AppDir + AppImage =="
# appimagetool сам является AppImage: без /dev/fuse (обычный контейнер
# сборки, без --device и без --privileged) запускает себя через
# извлечение во временную папку — штатный режим appimagetool, не костыль.
export APPIMAGE_EXTRACT_AND_RUN=1
uv run python tools/appimage_package.py \
    --dist build/main.dist \
    --appdir build/Terminal.AppDir \
    --out dist \
    --version "$TERMINAL_VERSION" \
    --appimagetool "$WORK/appimagetool-x86_64.AppImage" \
    --runtime "$WORK/runtime-x86_64" \
    --icon tools/terminal.png

ls -la dist/Terminal.AppImage
