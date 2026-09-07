"""Библиотека шаблонов настроек: названный набор, который применяют целиком.

Просьба владельца счёта 05.09.2026, дословно: «рядом с настройками кнопка
"Шаблоны настроек" — открываем, просмотр, статистика (период и прибыль-убыток,
количество сделок), применить. Сохранить текущие со значениями».

Шаблон — **не просто именованный набор**, а набор со своей историей: человек
смотрит на список и видит не «Вариант 3», а «на чём это дало сколько». Сами
цифры здесь не считаются и не хранятся: они берутся из записанных прогонов
(`ui/backend.py`), потому что считать их отдельно значило бы завести вторую
правду о деньгах.

Где лежит
---------
`userdata/settings-templates.json` — рядом с настройками, базой свечей и файлом
токена ([решение 0003](../.docs/decisions/0003-runtime-data-location.md)).
Папка переносится целиком, и «резервная копия = копия папки» обязана включать
шаблоны. Права `0600` и атомарная запись — те же, что у настроек
(`ui/userdata_file.py`).

Четыре случая, на которые формат обязан отвечать заранее
--------------------------------------------------------
**1. Файл не читается вовсе.** Библиотека пуста, а файл **не переписывается**:
он откладывается в сторону тем же приёмом, что и настройки. Молча стереть
чужой файл нельзя.

**2. В шаблоне негодное значение** («период средней»: «пятнадцать»). Шаблон
отвергается **целиком**, и об этом говорится словами. Здесь нельзя поступить
как с файлом настроек, где негодное поле теряет себя одно: настройки читать
обязательно — программе надо с чем-то стартовать, — а шаблон применять
не обязательно. Шаблон, применённый наполовину, худшее из возможного: человек
думает, что вернул проверенный набор, а вернул половину.

**3. В шаблоне нет настройки, появившейся позже.** Она берётся **умолчанием
программы** — не текущим значением из окна. Довод: шаблон сохранялся тогда,
когда этой настройки не было вовсе, то есть работал он с её умолчанием;
подставить текущее значение значило бы собрать набор, которого не было нигде,
и он менялся бы от того, что случайно стоит в окне. Поле при этом **названо**
(`Template.missing`), и окно говорит о нём до применения.

**4. В шаблоне есть ключ, которого эта сборка не знает** (файл от более новой
сборки). Ключ **сохраняется** и пишется обратно, но не применяется — применить
можно только то, что программа понимает. Оба факта названы (`Template.unknown`),
и окно показывает их перед применением: иначе человек считал бы, что вернул
набор целиком.

Обмен наборами: экспорт, импорт и папка примеров
------------------------------------------------
Решение 0051 (`.docs/decisions/`, «примеры едут своей папкой»).
Один набор настроек — не папка, и переносить его копированием всей поставки
неправильно. Отсюда три вещи в этом модуле:

* `export_templates` — записать выбранные наборы отдельным файлом **того же
  формата**. Отдельного формата обмена нет намеренно: второй формат означал бы
  второго читателя, а четыре случая ниже пришлось бы держать в обоих;
* `read_for_import` — прочитать **чужой** файл. Отличие от `Library.read`
  ровно два: встроенный набор не подмешивается, и негодный файл **не
  откладывается в сторону** — чужой файл трогать нельзя вовсе, он лежит
  где угодно и принадлежит не нам;
* `merge_templates` — сложить прочитанное с тем, что уже есть. **Добавление,
  а не замена**: импорт, стирающий список, однажды сотрёт единственный набор,
  который работал. Совпадение имён разрешается вопросом, а не молча.

Примеры едут своей папкой (`examples_dir`), рядом с программой и только
на чтение. Рабочий файл владельца счёта они не трогают: обновление поставки
не имеет права спорить с его правками.

⚠️ **Пример обязан нести своё происхождение** (`Template.origin`) — на каком
отрезке и на каком инструменте он отобран. Без этой строки набор читается как
рекомендация, а рекомендацией он не является: подбор на прошлом дважды
проиграл бездействию, и валовая за год на настройках владельца счёта
составила +32 ₽ на 1213 сделках.

⚠️ Имена ключей — латиницей (правило 6 CLAUDE.md), имя шаблона — любое, его
пишет человек и читает только человек.
"""

from __future__ import annotations

import enum
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Final

from ui.formatting import MSK
from ui.models import Settings
from ui.settings_codec import BadValue, encode_fields, field_codecs
from ui.userdata_file import write_atomically

__all__ = [
    "BUILTIN_NAME",
    "EXAMPLES_DIR_NAME",
    "FORMAT_VERSION",
    "TEMPLATES_FILE_NAME",
    "Library",
    "LoadedTemplates",
    "Merged",
    "NameClash",
    "Template",
    "builtin_template",
    "example_files",
    "examples_dir",
    "export_templates",
    "merge_templates",
    "read_for_import",
]

#: Имя файла библиотеки внутри `userdata/`.
TEMPLATES_FILE_NAME: Final[str] = "settings-templates.json"

#: Папка примеров рядом с программой. Латиницей — правило 6 CLAUDE.md:
#: кириллическое имя каталога ломает пути в консоли, в сборке под Windows
#: и в любой выдаче, где оно показывается escape-последовательностями.
EXAMPLES_DIR_NAME: Final[str] = "examples"

#: Версия формата. Читатель обязан уметь сказать «файл новее меня».
FORMAT_VERSION: Final[int] = 1

_KEY_VERSION: Final[str] = "format_version"
_KEY_TEMPLATES: Final[str] = "templates"
_KEY_NAME: Final[str] = "name"
_KEY_SAVED: Final[str] = "saved_at"
_KEY_VALUES: Final[str] = "settings"
_KEY_ORIGIN: Final[str] = "origin"

#: Имя встроенного шаблона. Он не хранится в файле и не удаляется: это
#: умолчания продукта, за которыми стоят замеры и сверка с прототипом
#: 127 сделок из 127. Он показывается первым — за пользовательскими наборами
#: пока не стоит ничего.
BUILTIN_NAME: Final[str] = "Умолчания проекта"

#: Сколько знаков имени шаблона показывается и хранится. Ограничение
#: не от жадности: имя уходит в заголовок строки таблицы и в фразу отказа.
NAME_LIMIT: Final[int] = 60


@dataclass(frozen=True, slots=True)
class Template:
    """Названный набор настроек.

    :param origin: откуда набор взялся — на каком отрезке и на каком
        инструменте отобран. Пусто у набора, который человек сохранил сам:
        он знает, откуда тот взялся. У **примера** пусто быть не должно —
        без этой строки пример читается как рекомендация, а он ею
        не является (решение 0051, следствие 5).
    :param missing: поля, которых в шаблоне не было, — взяты умолчанием
        программы. Пусто у шаблона, сохранённого этой сборкой.
    :param unknown: ключи, которых эта сборка не знает; сохранены в файле
        и **не применяются**.
    :param builtin: встроенный шаблон — умолчания продукта. Не удаляется
        и в файл не пишется.
    """

    name: str
    values: Settings
    saved_at: datetime | None = None
    origin: str = ""
    missing: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    builtin: bool = False
    #: Незнакомые настройки целиком — чтобы записать их обратно нетронутыми.
    #: Не участвует в сравнении: два шаблона равны по имени и значениям.
    extras: dict[str, object] = field(default_factory=dict, compare=False, repr=False)

    @property
    def complete(self) -> bool:
        """Шаблон применяется целиком, без оговорок."""
        return not self.missing and not self.unknown


@dataclass(frozen=True, slots=True)
class LoadedTemplates:
    """Что прочиталось из файла и что при этом стоит сказать вслух.

    `troubles` — шаблоны потеряны или могли быть потеряны: файл не прочёлся,
    шаблон отвергнут целиком, формат новее нашего. Каждая такая строка обязана
    дойти до человека: молча показать короткий список — значит соврать о том,
    что у него есть.
    """

    templates: tuple[Template, ...] = ()
    troubles: tuple[str, ...] = ()


def builtin_template() -> Template:
    """Умолчания продукта готовым шаблоном. Виден первым и не удаляется."""
    return Template(name=BUILTIN_NAME, values=Settings(), builtin=True)


class Library:
    """Чтение и запись `userdata/settings-templates.json`.

    Состояния не держит: читает файл целиком и записывает файл целиком.
    Библиотека открывается раз в неделю и состоит из десятка записей —
    платить сложностью за частичную запись здесь не за что, а «записан
    весь файл или прежний» проще объяснить человеку.
    """

    def __init__(self, directory: Path) -> None:
        """:param directory: папка `userdata/`."""
        self._path = directory / TEMPLATES_FILE_NAME

    @property
    def path(self) -> Path:
        """Полный путь к файлу. Показывается человеку как есть."""
        return self._path

    # ------------------------------------------------------------- чтение

    def read(self) -> LoadedTemplates:
        """Прочитать библиотеку. Встроенный шаблон идёт первым всегда."""
        builtin = builtin_template()
        if not self._path.exists():
            return LoadedTemplates((builtin,))
        parsed, troubles = _parse(self._path)
        if parsed is None:
            return LoadedTemplates((builtin,), self._quarantine(troubles[0]))
        return LoadedTemplates((builtin, *parsed), troubles)

    def _quarantine(self, why: str) -> tuple[str, ...]:
        """Отложить испорченный файл в сторону и сказать, куда именно.

        Файл не удаляется и не переписывается: это единственный след наборов,
        которые человек подбирал руками. Тот же приём и та же причина, что
        у файла настроек (`app/settings_store.py`).

        ⚠️ Так поступают **только со своим** файлом. Чужой файл, поданный
        на импорт, не откладывается и не переименовывается вовсе: он лежит
        где угодно и принадлежит не нам (`read_for_import`).
        """
        stamp = datetime.now(tz=MSK).strftime("%Y%m%d-%H%M%S")
        spare = self._path.with_name(f"{self._path.stem}.broken-{stamp}.json")
        try:
            self._path.rename(spare)
        except OSError as failure:
            return (
                f"Библиотека шаблонов {self._path} не читается: {why}. "
                f"Отложить её в сторону тоже не удалось ({failure.strerror or failure}). "
                "Показан только встроенный набор. ⚠️ Первое же сохранение "
                "шаблона перезапишет этот файл — если наборы из него нужны, "
                "скопируйте его сейчас.",
            )
        return (
            f"Библиотека шаблонов не читается: {why}. Она сохранена "
            f"как {spare} и не тронута — открыть её можно любым текстовым "
            "редактором. Показан только встроенный набор.",
        )

    # ------------------------------------------------------------- запись

    def write(self, templates: tuple[Template, ...]) -> str:
        """Записать библиотеку. Пустая строка — записано, иначе фраза отказа.

        Отказ **возвращается, а не бросается**: запись зовётся из слота Qt,
        и исключение оттуда означало бы трассировку в консоль и молчащее окно.

        Встроенный шаблон в файл не попадает: он не данные человека,
        а умолчания продукта, и следующая сборка обязана взять свои, а не
        записанные когда-то.
        """
        kept = tuple(one for one in templates if not one.builtin)
        try:
            write_atomically(self._path, _body_of(kept))
        except OSError as error:
            return (
                f"Шаблоны не сохранены в {self._path}: {error}. В этом сеансе "
                "список действует, но при следующем запуске программа возьмёт "
                "прежний. Проверьте, что папка существует и в неё можно писать."
            )
        return ""


# --------------------------------------------------------- укладка в файл

def _body_of(templates: Sequence[Template]) -> str:
    """Текст файла библиотеки. Один укладчик на запись и на выгрузку.

    Второй укладчик означал бы второй формат: экспортированный файл перестал
    бы читаться обычным чтением, и четыре случая из шапки модуля пришлось бы
    держать в двух местах.
    """
    payload = {
        _KEY_VERSION: FORMAT_VERSION,
        _KEY_TEMPLATES: [
            {
                _KEY_NAME: one.name,
                _KEY_SAVED: (
                    one.saved_at.isoformat() if one.saved_at is not None else ""
                ),
                _KEY_ORIGIN: one.origin,
                # ⚠️ Незнакомые ключи кладутся ПЕРВЫМИ и потому не могут
                # перебить своими значениями то, что программа понимает.
                # Порядок здесь — правило безопасности, а не оформление.
                _KEY_VALUES: {**one.extras, **encode_fields(one.values)},
            }
            for one in templates
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def export_templates(path: Path, templates: Sequence[Template]) -> str:
    """Выгрузить наборы отдельным файлом. Пустая строка — записано.

    Формат тот же, что у библиотеки: выгруженный файл читается обратно
    обычным импортом, без переводчика.

    Встроенный набор здесь **не отбрасывается**, в отличие от записи
    библиотеки: выгрузить умолчания программы, чтобы сравнить их с чужими
    на другой машине, — законное действие. В файле он окажется обычным
    набором, а встроенным на той машине останется её собственный.

    Отказ **возвращается**, а не бросается: зовётся из слота Qt.
    """
    try:
        write_atomically(path, _body_of(tuple(templates)))
    except OSError as error:
        return (
            f"Набор не выгружен в {path}: {error}. Проверьте, что папка "
            "существует и в неё можно писать."
        )
    return ""


# ------------------------------------------------------------ чтение файла

def _parse(path: Path) -> tuple[tuple[Template, ...] | None, tuple[str, ...]]:
    """Разобрать файл шаблонов. `None` вместо списка — файл негоден целиком.

    Что делать с негодным файлом, решает вызывающий, и решения разные:
    свой файл откладывается в сторону (`Library._quarantine`), чужой —
    не трогается вовсе (`read_for_import`). Поэтому здесь только разбор
    и слова о том, что случилось.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return None, (_why(error),)
    if not isinstance(payload, dict):
        return None, ("в файле не библиотека шаблонов",)
    raw = payload.get(_KEY_TEMPLATES)
    if not isinstance(raw, list):
        return None, (f"в файле нет раздела «{_KEY_TEMPLATES}» со списком",)
    troubles = list(_version_notes(payload.get(_KEY_VERSION), path))
    templates: list[Template] = []
    for number, item in enumerate(raw, start=1):
        one, trouble = _template_of(item, number)
        if one is not None:
            templates.append(one)
        if trouble:
            troubles.append(trouble)
    return tuple(templates), tuple(troubles)


def read_for_import(path: Path) -> LoadedTemplates:
    """Прочитать **чужой** файл наборов: пример из поставки или файл с диска.

    Два отличия от `Library.read`, и оба намеренные:

    * встроенный набор не подмешивается — здесь показывают то, что в файле,
      а не то, что есть у программы;
    * негодный файл **не откладывается в сторону и не переименовывается**.
      Файл лежит где угодно и принадлежит не нам; переложить чужой файл —
      значит потерять его для того, кто его прислал.
    """
    if not path.is_file():
        return LoadedTemplates((), (f"Файл {path} не найден.",))
    parsed, troubles = _parse(path)
    if parsed is None:
        return LoadedTemplates(
            (),
            (
                f"Файл {path} не прочитан: {troubles[0]}. Он оставлен как есть — "
                "программа чужие файлы не трогает. Ничего не импортировано.",
            ),
        )
    if not parsed:
        return LoadedTemplates((), (f"В файле {path} нет ни одного набора.", *troubles))
    return LoadedTemplates(parsed, troubles)


# ---------------------------------------------------------- папка примеров

def examples_dir() -> Path:
    """Папка примеров, приехавшая вместе с программой. Только на чтение.

    ⚠️ Здесь **не** используется переменная `APPIMAGE`, в отличие от
    `market.paths.userdata_dir`, и это не забывчивость. Там ищут место, куда
    можно **писать**, — оно обязано быть снаружи образа. Здесь ищут то, что
    приехало **внутри** поставки: у Nuitka каталог данных лежит рядом
    с исполняемым файлом, у AppImage — внутри монтирования, и `sys.executable`
    указывает ровно туда. Взять путь рядом с файлом `.AppImage` значило бы
    искать примеры там, где их нет.
    """
    if bool(getattr(sys, "frozen", False)) or "__compiled__" in globals():
        return Path(sys.executable).resolve().parent / EXAMPLES_DIR_NAME
    return Path(__file__).resolve().parent.parent / EXAMPLES_DIR_NAME


def example_files(directory: Path | None = None) -> tuple[Path, ...]:
    """Файлы примеров по порядку имён. Нет папки — пусто, а не отказ.

    Папку могли удалить: решение 0051 прямо это разрешает («если пользователь
    захочет — удалит»). Отсутствие примеров не поломка программы.
    """
    directory = directory if directory is not None else examples_dir()
    try:
        return tuple(sorted(one for one in directory.glob("*.json") if one.is_file()))
    except OSError:
        return ()


# ----------------------------------------------------- сложение с тем, что есть

class NameClash(enum.Enum):
    """Что делать с импортируемым набором, чьё имя уже занято.

    Молчаливого варианта здесь нет ни одного: замена без спроса однажды
    сотрёт единственный набор, который работал.
    """

    #: Заменить свой набор импортированным. Прежние значения не вернуть.
    REPLACE = "replace"
    #: Оставить оба: импортированный получит свободное имя рядом.
    KEEP_BOTH = "keep_both"
    #: Не брать импортированный вовсе.
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class Merged:
    """Список после импорта и что об этом сказать человеку.

    `notes` — не украшение: импорт меняет список наборов, по которым человек
    торгует, и он обязан прочитать, что именно изменилось, а не догадываться
    по длине списка.
    """

    templates: tuple[Template, ...] = ()
    notes: tuple[str, ...] = ()
    added: int = 0
    replaced: int = 0
    skipped: int = 0


def merge_templates(
    existing: Sequence[Template],
    incoming: Sequence[Template],
    resolve: Callable[[Template], NameClash],
) -> Merged:
    """Сложить импортируемое с тем, что уже есть. **Добавление, а не замена.**

    ⚠️ Главное правило импорта: ни один набор владельца счёта не исчезает
    без его прямого ответа. Список, стёртый импортом, однажды унесёт
    единственный набор, который работал, — и восстановить его будет нечем.

    :param resolve: что делать при совпадении имён. Зовётся **только** на
        совпадении и ровно один раз на каждое; ответ даёт человек.
        Совпадение с встроенным набором сюда не попадает: заменить умолчания
        программы нельзя, такой набор берётся под свободным именем и об этом
        говорится словами.
    """
    kept = [one for one in existing if not one.builtin]
    taken = {one.name for one in existing}
    result = list(kept)
    notes: list[str] = []
    added = replaced = skipped = 0
    for one in incoming:
        if one.name not in taken:
            result.append(one)
            taken.add(one.name)
            added += 1
            continue
        if any(other.builtin and other.name == one.name for other in existing):
            fresh = _free_name(one.name, taken)
            result.append(replace(one, name=fresh))
            taken.add(fresh)
            added += 1
            notes.append(
                f"Имя «{one.name}» занято встроенным набором — умолчаниями "
                f"программы, заменить их нельзя. Импортированный набор добавлен "
                f"под именем «{fresh}»."
            )
            continue
        choice = resolve(one)
        if choice is NameClash.SKIP:
            skipped += 1
            notes.append(f"Набор «{one.name}» пропущен: ваш оставлен как был.")
        elif choice is NameClash.REPLACE:
            result = [one if other.name == one.name else other for other in result]
            replaced += 1
            notes.append(f"Набор «{one.name}» заменён импортированным.")
        else:
            fresh = _free_name(one.name, taken)
            result.append(replace(one, name=fresh))
            taken.add(fresh)
            added += 1
            notes.append(f"Набор «{one.name}» добавлен под именем «{fresh}».")
    return Merged(tuple(result), tuple(notes), added, replaced, skipped)


def _free_name(name: str, taken: set[str]) -> str:
    """Свободное имя рядом с занятым: «Имя (2)», «Имя (3)» и так далее.

    ⚠️ Хвост с номером обязан поместиться в `NAME_LIMIT`: чтение обрезает
    имя до этой длины, и обрезанный номер вернул бы то самое совпадение,
    от которого имя и уводили.
    """
    number = 2
    while True:
        tail = f" ({number})"
        fresh = name[: NAME_LIMIT - len(tail)].rstrip() + tail
        if fresh not in taken:
            return fresh
        number += 1


def _template_of(item: object, number: int) -> tuple[Template | None, str]:
    """Один шаблон из файла. Негодный отвергается **целиком** и со словами."""
    if not isinstance(item, dict):
        return None, (
            f"Запись {number} в библиотеке шаблонов пропущена: это не набор "
            "настроек. Остальные шаблоны прочитаны."
        )
    name = item.get(_KEY_NAME)
    if not isinstance(name, str) or not name.strip():
        return None, (
            f"Запись {number} в библиотеке шаблонов пропущена: у неё нет имени. "
            "Остальные шаблоны прочитаны."
        )
    name = name.strip()[:NAME_LIMIT]
    raw = item.get(_KEY_VALUES)
    if not isinstance(raw, dict):
        return None, (
            f"Шаблон «{name}» пропущен: в нём нет настроек. Остальные шаблоны "
            "прочитаны."
        )
    table = field_codecs()
    values = Settings()
    changes: dict[str, object] = {}
    for field_name, codec in table.items():
        if field_name not in raw:
            continue
        try:
            changes[field_name] = codec.load(raw[field_name])
        except BadValue as error:
            # ⚠️ Отказ **целиком**, а не потеря одного поля. Половина набора,
            # выданная за набор, — та самая ошибка, ради которой применение
            # сделано «всё или ничего».
            return None, (
                f"Шаблон «{name}» не прочитан целиком и потому не показан: "
                f"настройка «{field_name}» негодная — {error}. Применить его "
                "наполовину нельзя: вы считали бы, что вернули проверенный "
                "набор. Исправьте значение в файле или сохраните шаблон заново."
            )
    missing = tuple(sorted(set(table) - set(raw)))
    extras = {key: value for key, value in raw.items() if key not in table}
    saved_at = _moment_of(item.get(_KEY_SAVED))
    return (
        Template(
            name=name,
            values=values.replace(**changes),
            saved_at=saved_at,
            origin=_origin_of(item.get(_KEY_ORIGIN)),
            missing=missing,
            unknown=tuple(sorted(extras)),
            extras=extras,
        ),
        "",
    )


#: Сколько знаков происхождения хранится. Строка уходит в столбец таблицы
#: и в подсказку; длиннее — это уже не «на чём отобран», а рассказ.
ORIGIN_LIMIT: Final[int] = 200


def _origin_of(raw: object) -> str:
    """Откуда набор взялся. Не строка — пусто, а не выдумка.

    Пустое происхождение честнее сочинённого: окно скажет, что не сказано,
    на чём набор отобран, и это верное утверждение.
    """
    if not isinstance(raw, str):
        return ""
    return raw.strip()[:ORIGIN_LIMIT]


def _moment_of(raw: object) -> datetime | None:
    """Когда сохранён. Негодная отметка времени — `None`, а не сегодня.

    Подставленное «сегодня» читалось бы как «шаблон свежий», и это неправда
    ровно там, где человек выбирает между двумя похожими наборами.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _version_notes(version: object, path: Path) -> tuple[str, ...]:
    """Что сказать про версию формата. Молчание — только на своей версии."""
    if version == FORMAT_VERSION:
        return ()
    if (
        isinstance(version, int)
        and not isinstance(version, bool)
        and version > FORMAT_VERSION
    ):
        return (
            f"Библиотека шаблонов {path} сделана более новой сборкой программы "
            f"(формат {version}, эта сборка знает {FORMAT_VERSION}). Прочитано "
            "то, что понятно; остальное оставлено в файле нетронутым.",
        )
    return (
        f"У библиотеки шаблонов {path} не назван формат (ожидался "
        f"{FORMAT_VERSION}). Прочитана как есть — проверьте значения перед "
        "применением.",
    )


def _why(error: Exception) -> str:
    """Почему файл не прочёлся — по-русски и без кода библиотеки."""
    if isinstance(error, json.JSONDecodeError):
        return (
            f"файл испорчен — разбор оборвался на строке {error.lineno}, "
            f"знак {error.colno}"
        )
    if isinstance(error, UnicodeDecodeError):
        return "файл записан не в той кодировке: ожидался обычный текст UTF-8"
    if isinstance(error, OSError):
        return f"файл не открылся ({error.strerror or error})"
    return str(error)
