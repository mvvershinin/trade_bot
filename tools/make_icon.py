"""Иконка программы: `tools/terminal.ico`.

Зачем генератор, а не картинка в дереве
---------------------------------------
Иконку в `.ico` встраивает Nuitka ключом `--windows-icon-from-ico`, и без неё
у программы на рабочем столе стандартный значок неизвестного приложения —
ровно то, что владелец счёта увидит первым. Держать в git двоичный файл,
происхождение которого никто не помнит, хуже, чем тридцать строк, которые
его собирают: правку видно в diff, а не в «файл изменился».

Формат
------
`.ico` — это каталог изображений. Каждое здесь — несжатый DIB (BMP без
заголовка файла) в 32 битах BGRA, снизу вверх, плюс маска прозрачности
из нулей (при 32 битах она не используется, но по формату обязана быть).
Внешних библиотек не требуется: PIL в поставку не входит и тянуть его
ради одной иконки не за чем.

Запуск:  python tools/make_icon.py
"""

from __future__ import annotations

import pathlib
import struct

OUT = pathlib.Path(__file__).resolve().parent / "terminal.ico"

#: Размеры, которые спрашивает проводник Windows: список файлов, рабочий стол,
#: панель задач и крупные значки.
SIZES = (16, 24, 32, 48, 64, 128, 256)

#: Цвета. Фон — тёмный, как окно программы; свеча роста — зелёная,
#: свеча падения — красная. BGRA, потому что таков порядок байтов в DIB.
BACKGROUND = (0x2A, 0x22, 0x1C, 0xFF)
UP = (0x6E, 0xC2, 0x4C, 0xFF)
DOWN = (0x4C, 0x4C, 0xE0, 0xFF)


def _canvas(size: int) -> list[list[tuple[int, int, int, int]]]:
    """Картинка size×size: две свечи на тёмном фоне.

    Координаты считаются долями стороны, а не пикселями, — иначе рисунок
    для 16 и для 256 пришлось бы задавать порознь.
    """
    px = [[BACKGROUND for _ in range(size)] for _ in range(size)]

    def rect(x0: float, y0: float, x1: float, y1: float, color: tuple[int, int, int, int]) -> None:
        left, right = int(x0 * size), max(int(x1 * size), int(x0 * size) + 1)
        top, bottom = int(y0 * size), max(int(y1 * size), int(y0 * size) + 1)
        for y in range(max(top, 0), min(bottom, size)):
            for x in range(max(left, 0), min(right, size)):
                px[y][x] = color

    # Левая свеча — растущая: фитиль, тело.
    rect(0.30, 0.14, 0.36, 0.86, UP)
    rect(0.22, 0.30, 0.44, 0.70, UP)
    # Правая свеча — падающая.
    rect(0.62, 0.24, 0.68, 0.92, DOWN)
    rect(0.54, 0.44, 0.76, 0.80, DOWN)
    return px


def _dib(size: int) -> bytes:
    """DIB-запись одного размера: заголовок BITMAPINFOHEADER, пиксели, маска."""
    px = _canvas(size)
    # Высота удваивается — таково требование формата: изображение и маска.
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, size * size * 4, 0, 0, 0, 0)
    body = bytearray()
    for row in reversed(px):  # DIB идёт снизу вверх
        for b, g, r, a in row:
            body += bytes((b, g, r, a))
    mask = bytes(((size + 31) // 32) * 4 * size)  # выровнено по 4 байта на строку
    return header + bytes(body) + mask


def build() -> pathlib.Path:
    images = [_dib(s) for s in SIZES]
    out = bytearray(struct.pack("<HHH", 0, 1, len(images)))  # зарезервировано, тип 1 = icon
    offset = 6 + 16 * len(images)
    for size, data in zip(SIZES, images, strict=True):
        # 256 записывается нулём: поле однобайтовое.
        out += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for data in images:
        out += data
    OUT.write_bytes(bytes(out))
    return OUT


if __name__ == "__main__":
    path = build()
    print(f"{path} — {path.stat().st_size} Б, размеры: {', '.join(map(str, SIZES))}")
