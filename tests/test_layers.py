"""Тесты каркаса: слои из ARCHITECTURE.md §2 на месте, объявлены в сборке,
не импортируют друг друга против направления зависимостей, и в рабочей копии
нет файла, похожего на секрет, который git согласился бы закоммитить.

Торговой логики здесь нет и быть не должно. Четыре вещи, ради которых
эти тесты существуют:

1. Слой или подпакет, не попавший в сборку, из исходников работает,
   а из установленного пакета исчезает.
2. Одноимённый дистрибутив в окружении затеняет слой репозитория: код на диске
   есть, а импортируется чужой. Шесть из семи имён слоёв заняты на PyPI.
3. Направление зависимостей ARCHITECTURE.md §2 нарушается одной строкой `import`.
4. Файл с токеном, положенный мимо `userdata/`, — это доступ к счёту в истории
   репозитория. Соглашение решения 0003 должно быть проверяемым, а не устным.
"""

from __future__ import annotations

import ast
import importlib
import importlib.metadata
import os
import pathlib
import re
import shutil
import subprocess
import tomllib
from typing import Final

import pytest

# Порядок и имена — дословно из ARCHITECTURE.md §2.
LAYERS = ("market", "broker", "strategies", "engine", "backtest", "ui", "app")

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
SELF = pathlib.Path(__file__).resolve()

# Кому что разрешено импортировать — ARCHITECTURE.md §2, блок «Зависимости».
# Пустое множество означает «ничего из слоёв проекта».
ALLOWED_IMPORTS: dict[str, set[str]] = {
    "market": set(),
    "broker": set(),
    "strategies": set(),
    "engine": {"strategies"},
    "backtest": {"engine", "strategies", "market"},
    "ui": {"engine", "backtest", "market"},
    "app": set(LAYERS) - {"app"},
}

# Каталоги, в которые обход не заходит: чужое, порождённое или намеренно вне git.
SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "reference", "userdata",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "terminal.egg-info", "dist", "build", "AppDir",
}
# `node_modules` в SKIP_DIRS намеренно НЕТ: в `.gitignore` для него правила нет,
# а чужие пакеты складывают внутрь свои `.npmrc` и токены. Пропустить каталог
# в обходе — значит выключить предохранитель ровно там, где он нужен.

# Имя, которое секретом является почти наверняка. Регистр не учитывается.
SECRET_NAME_HARD = re.compile(
    r"(token|secret|credential|passwd|password|apikey|api[_-]?key|privkey"
    r"|токен|ключ|секрет|пароль)",
    re.IGNORECASE,
)
# Имя обычного файла настроек. Само по себе ни о чём не говорит — но именно
# такие имена перечислены в решении 0003 как места, куда ляжет токен.
# Проверяется по телу, иначе тест начнёт падать на легальной конфигурации.
SECRET_NAME_SOFT = re.compile(
    r"^(settings|config|conf|auth|state|session|account|profile"
    r"|настройки|настройка|конфиг|доступ|учётк|учетк|подключени)",
    re.IGNORECASE,
)
# Расширения, которые сами по себе означают ключ или дамп памяти:
# имя файла тут не важно, `broker.key` — такой же секрет, как `token.key`.
SECRET_SUFFIX = {".pem", ".key", ".p12", ".pfx", ".dmp", ".jks", ".keystore"}

SECRET_EXT = {
    "", ".json", ".ini", ".txt", ".yaml", ".yml", ".toml", ".cfg", ".conf",
    ".env", ".bak", ".xml", ".properties",
}

# Содержимое, которое секретом является почти наверняка.
SECRET_BODY = re.compile(
    rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    rb"|\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"
    rb"|\b(access_token|refresh_token|api_key|client_secret)\b\W{1,4}[A-Za-z0-9._-]{16,}"
    rb"|Authorization\s*:\s*Bearer\s+\S{16,}"
)

# Слова, по которым видно, что внутри файла настроек лежит секрет.
# Тело читается байтами (чтобы не декодировать бинарники целиком), а в байтовом
# режиме `\b` считает границей только ASCII и IGNORECASE сворачивает только ASCII:
# перед русской буквой граница не срабатывает, «ТОКЕН» к «токен» не сводится.
# Поэтому: для латиницы — просто подстрока (заодно ловится `access_token`),
# для кириллицы — три написания явно.
TOKENISH_BODY = re.compile(
    "|".join([
        "token", "secret", "password", "passwd",
        "токен", "Токен", "ТОКЕН",
        "пароль", "Пароль", "ПАРОЛЬ",
        "секрет", "Секрет", "СЕКРЕТ",
        "ключ", "Ключ", "КЛЮЧ",
    ]).encode("utf-8"),
    re.IGNORECASE,
)

# Изоляция от настроек машины: `check-ignore` учитывает ~/.config/git/ignore
# и .git/info/exclude, которых нет в репозитории. Без изоляции тест зеленеет
# у того, у кого секрет лежит, и молчит там, где файл попадёт в историю.
GIT_ISOLATED = ["git", "-c", "core.excludesFile=/dev/null", "-c", "core.quotePath=false"]


@pytest.mark.parametrize("layer", LAYERS)
def test_layer_imports(layer: str) -> None:
    """Пакет слоя импортируется и объявляет свою зону ответственности."""
    module = importlib.import_module(layer)
    assert module.__doc__ or not __debug__, (
        f"у пакета {layer} нет строки с зоной ответственности"
    )


@pytest.mark.parametrize("layer", LAYERS)
def test_layer_name_not_taken_by_another_distribution(layer: str) -> None:
    """Имя слоя не занято посторонним дистрибутивом в этом окружении.

    Проверять результат `import` бессмысленно: `pythonpath = ["."]` ставит корень
    репозитория перед `site-packages`, поэтому под pytest всегда импортируется
    свой слой — тест был бы зелёным и при затенении. Смотреть надо на окружение
    и ловить установку в время, когда она произошла, а не когда она сломает
    сборку: в бинарь Nuitka попадёт уже чужой пакет.

    Ограничение, которое стоит помнить: копия слоя, положенная в `site-packages`
    без метаданных (`cp -r`, conda), этой проверке не видна.
    """
    providers = importlib.metadata.packages_distributions().get(layer, [])
    assert "terminal" in providers, (
        f"слой {layer} не заявлен дистрибутивом terminal — метаданные установки "
        "изменились, и проверка на затенение выродилась в тихо-зелёную"
    )
    posers = [name for name in providers if name != "terminal"]
    assert not posers, (
        f"имя слоя {layer} предоставляет посторонний дистрибутив: {posers}. "
        "Слой репозитория будет затенён в сборке и в любом запуске без pytest"
    )


def test_build_finds_exactly_the_layers() -> None:
    """Маски поиска пакетов покрывают ровно семь слоёв, и состав диска им равен.

    Список пакетов задан масками (`market*` и т.д.), а не перечислением имён:
    перечисление нерекурсивно, и подпакет `market/storage/` в него молча
    не попадает — из исходников и из editable-установки работает, в колесе
    отсутствует.

    `where` и `exclude` проверяются наравне с `include`: переход на src-layout
    с забытым переносом каталогов даёт ноль найденных пакетов и пустое колесо
    при зелёном тесте, который заведён ровно против этого.
    """
    find = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["setuptools"]["packages"]["find"]
    assert sorted(find["include"]) == sorted(f"{layer}*" for layer in LAYERS), (
        "маски поиска разошлись с составом слоёв ARCHITECTURE.md §2"
    )
    assert find.get("namespaces") is False, (
        "namespaces=false обязателен: иначе каталог без __init__.py попадёт "
        "в сборку как неявный namespace-пакет"
    )
    assert find.get("where", ["."]) == ["."], (
        f"where={find.get('where')} — состав диска считается от корня репозитория, "
        "проверка перестанет соответствовать сборке"
    )
    assert not find.get("exclude"), (
        f"exclude={find.get('exclude')} — слой может молча выпасть из сборки"
    )

    on_disk = {
        path.name
        for path in REPO_ROOT.iterdir()
        if path.is_dir()
        and (path / "__init__.py").is_file()
        and path.name not in SKIP_DIRS
        and not path.name.startswith((".", "_"))
        and path.name != "tests"
    }
    assert on_disk == set(LAYERS), (
        "состав пакетов в корне разошёлся с ARCHITECTURE.md §2: "
        f"лишние {sorted(on_disk - set(LAYERS))}, пропали {sorted(set(LAYERS) - on_disk)}"
    )


@pytest.mark.parametrize("layer", LAYERS)
def test_every_subpackage_has_init(layer: str) -> None:
    """Каталог с кодом внутри слоя обязан иметь `__init__.py`.

    При `namespaces = false` каталог без `__init__.py` пакетом не считается —
    и поиск внутрь него не спускается. Из исходников и из editable-установки
    подпакет при этом работает (PEP 420 даёт неявный namespace-пакет),
    а в колесе и в бинаре его нет. Проявится у владельца счёта на собранной
    поставке: программа не откроет базу свечей.
    """
    orphans: list[str] = []
    for directory, dirnames, filenames in os.walk(REPO_ROOT / layer):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        path = pathlib.Path(directory)
        if path == REPO_ROOT / layer:
            continue
        if any(name.endswith(".py") for name in filenames) and "__init__.py" not in filenames:
            orphans.append(str(path.relative_to(REPO_ROOT)))

    assert not orphans, (
        "каталоги с кодом без __init__.py — из исходников работают, "
        f"в собранную поставку не попадут:\n  " + "\n  ".join(sorted(orphans))
    )


@pytest.mark.parametrize("layer", LAYERS)
def test_dependency_direction(layer: str) -> None:
    """Слой не импортирует то, что ему по ARCHITECTURE.md §2 не положено.

    Одна строка `import broker` внутри `engine/` делает прогон на истории
    невозможным без подключения к брокеру — и ломает главное правило проекта
    ещё до того, как появится хоть один тест торговой логики.

    Относительные импорты не проверяются намеренно: слои — отдельные пакеты
    верхнего уровня, и пересечь границу слоя относительным импортом нельзя,
    он упрётся в `attempted relative import beyond top-level package`.

    Не ловится и ловиться статически не может: загрузка по имени, собранному
    в рантайме. Формы с константной строкой — ловятся.
    """
    allowed = ALLOWED_IMPORTS[layer]
    violations: list[str] = []
    parsed = 0

    for source in sorted((REPO_ROOT / layer).rglob("*.py")):
        if SKIP_DIRS.intersection(source.relative_to(REPO_ROOT).parts):
            continue
        try:
            text = source.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            pytest.fail(f"{source.relative_to(REPO_ROOT)} читается не как UTF-8: {exc}")
        try:
            tree = ast.parse(text, filename=str(source))
        except SyntaxError as exc:
            pytest.fail(f"{source.relative_to(REPO_ROOT)}:{exc.lineno} не разбирается: {exc.msg}")
        parsed += 1

        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module] if node.level == 0 and node.module else []
            elif isinstance(node, ast.Call):
                # Загрузка по имени: сменные модули strategies/ грузятся именно так,
                # и обычная проверка импортов их не видит. Разбираются обе формы —
                # позиционная и именованная, плюс package= у относительной загрузки.
                func = node.func
                target = getattr(func, "attr", None) or getattr(func, "id", None)
                if target in {"import_module", "__import__", "find_spec"}:
                    args = list(node.args) + [kw.value for kw in node.keywords]
                    names = [
                        arg.value.lstrip(".")
                        for arg in args
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                    ]

            for name in names:
                head = name.split(".")[0]
                if head in LAYERS and head != layer and head not in allowed:
                    violations.append(f"{source.relative_to(REPO_ROOT)}:{node.lineno} → {name}")

    assert parsed, (
        f"в слое {layer} не разобрано ни одного файла — проверка вакуумна. "
        "Каталог слоя пуст или переименован"
    )
    assert not violations, (
        f"слой {layer} импортирует не то, что ему разрешено "
        f"({sorted(allowed) or 'ничего из слоёв'}):\n  " + "\n  ".join(violations)
    )


def _looks_like_secret(path: pathlib.Path) -> bool:
    """Файл похож на секрет: по имени наверняка, либо по имени настроек и телу."""
    if path.suffix.lower() in SECRET_SUFFIX:
        return True
    if SECRET_NAME_HARD.search(path.stem) and path.suffix.lower() in SECRET_EXT:
        return True

    try:
        with path.open("rb") as handle:
            head = handle.read(8192)
    except OSError:
        return False
    if SECRET_BODY.search(head):
        return True
    # Обычное имя настроек плюс токеноподобное тело — это ровно те пять имён,
    # из-за которых принято решение 0003: settings.json, config.ini, auth.json…
    # Расширение обязательно: без него под проверку попадал любой `.py`,
    # и `broker/auth.py` со словом «token» в коде валил прогон. Файл с секретом —
    # это данные (`.json`, `.ini`, без расширения), а не исходник.
    if path.suffix.lower() not in SECRET_EXT:
        return False
    return bool(SECRET_NAME_SOFT.match(path.stem)) and bool(TOKENISH_BODY.search(head))


#: Слово, которым файл объявляет себя подделкой. Проверяется по телу, а не
#: по пути: каталог `tests/fixtures/` прежде пропускался обходом целиком,
#: и это была слепая зона на слепой зоне — `.gitignore` строкой
#: `!tests/fixtures/**` тот же каталог раз-игнорирует. Закрытыми оставались
#: только восемь имён вида `token.json`, а `real-answer.json` с настоящим
#: ответом сервиса авторизации проходил обе проверки насквозь: разработчик
#: кладёт рядом живой ответ, чтобы воспроизвести проблему подключения,
#: и `git add -A` уносит токен на 90 дней в историю репозитория.
#:
#: Признак в самом файле, а не в списке имён: список имён закрывает то,
#: что уже придумали, признак — то, что придумают завтра. Написать его
#: в файле с настоящим ответом можно только нарочно.
FAKE_DECLARATION: Final[bytes] = "подделка-для-теста".encode("utf-8")


def _declares_itself_fake(path: pathlib.Path) -> bool:
    """Файл объявляет себя подделкой — значит он законный образец."""
    try:
        with path.open("rb") as handle:
            return FAKE_DECLARATION in handle.read(8192)
    except OSError:
        return False


def _candidate_secret_files() -> tuple[list[pathlib.Path], list[pathlib.Path]]:
    """Кандидаты в секреты и файлы, которые не удалось прочитать."""
    found: list[pathlib.Path] = []
    unreadable: list[pathlib.Path] = []

    for directory, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [
            name for name in dirnames
            if name not in SKIP_DIRS and not name.endswith((".dist", ".build"))
        ]
        base = pathlib.Path(directory)

        for name in filenames:
            path = base / name
            if path.resolve() == SELF:
                continue  # сам файл теста содержит образцы паттернов
            if path.is_symlink():
                # Ссылка утекает не токеном, а путём к нему и топологией машины.
                if SECRET_NAME_HARD.search(path.stem):
                    found.append(path)
                continue
            try:
                if _looks_like_secret(path) and not _declares_itself_fake(path):
                    found.append(path)
            except OSError:
                unreadable.append(path)
    return found, unreadable


def test_secret_detector_is_not_blind(tmp_path: pathlib.Path) -> None:
    """Сам детектор работает — иначе он тихо выродится в ничего не делающий.

    В рабочей копии кандидатов сейчас нет, значит основной тест не выполняет
    ни одной содержательной ветки. Без этой канарейки поломка детектора
    выглядела бы как зелёный прогон — ровно тот дефект, за который был удалён
    прежний тест про импорт из репозитория.
    """
    def expect_secret(file_name: str, body: str, why: str) -> None:
        probe = tmp_path / file_name
        probe.write_text(body, encoding="utf-8")
        assert _looks_like_secret(probe), f"не опознан как секрет: {file_name} — {why}"

    def minus(file_name: str, body: str, why: str) -> None:
        probe = tmp_path / file_name
        probe.write_text(body, encoding="utf-8")
        assert not _looks_like_secret(probe), f"ложное срабатывание: {file_name} — {why}"

    # Опознаётся по имени, body не участвует.
    expect_secret("token.json", "{}", "секрет в самом имени")
    expect_secret("токен.txt", "неважно", "секрет в имени по-русски")
    expect_secret("broker.key", "", "расширение ключа")

    # Опознаётся по телу: имя обычное, но внутри лежит секрет.
    # Ровно те имена, из-за которых принято решение 0003.
    expect_secret("settings.json", '{"access_token": "0123456789abcdef0123"}', "имя настроек + токен внутри")
    expect_secret("config.ini", "токен = 90-дневный\n", "имя настроек + русская подпись")
    expect_secret("auth.json", '{"password": "hunter2"}', "имя настроек + пароль внутри")
    expect_secret("state.txt", "ПАРОЛЬ: 12345", "верхний регистр по-русски")
    expect_secret("настройки.ini", "токен = 90-дневный\n", "имя настроек по-русски")
    expect_secret("подключение.json", '{"токен": "abc"}', "имя подключения по-русски")

    # Не опознаётся: обычные файлы проекта. Если тест начнёт падать здесь,
    # его отключат — и он перестанет защищать что-либо вообще.
    minus("pyproject.toml", '[project]\nname = "terminal"\n', "обычная сборка")
    minus("auth.py", 'def store(token: str) -> None:\n    """Сохранить токен."""\n', "исходник, а не данные")
    minus("conftest.py", "token = 'x'\n", "исходник тестов")
    minus("config.md", "Как вводится токен\n", "документация")
    minus("settings.json", '{"period": 15}\n', "настройки без секрета")


def test_fixtures_are_judged_by_content_not_by_path(tmp_path: pathlib.Path) -> None:
    """Каталог фикстур больше не пропускается обходом; решает признак в файле.

    Прежде тут было две слепые зоны на одном каталоге: `.gitignore` строкой
    `!tests/fixtures/**` раз-игнорирует его, а обход детектора пропускал его
    целиком. Закрытыми оставались только восемь имён вида `token.json`.
    Файл `tests/fixtures/real-answer.json` с настоящим ответом сервиса
    авторизации проходил обе проверки насквозь — а кладут такой файл ровно
    тогда, когда воспроизводят проблему подключения, то есть с живым токеном.
    """
    real = tmp_path / "real-answer.json"
    real.write_text('{"access_token": "0123456789abcdef0123456789"}', encoding="utf-8")
    assert _looks_like_secret(real), "ответ сервиса не опознан как секрет"
    assert not _declares_itself_fake(real), "файл без объявления считается подделкой"

    fake = tmp_path / "answer-samples.json"
    fake.write_text(
        '{"почему": "подделка-для-теста", "access_token": "0123456789abcdef0123"}',
        encoding="utf-8",
    )
    assert _looks_like_secret(fake)
    assert _declares_itself_fake(fake), "объявленная подделка не распознана"

    # Тот образец, что лежит в репозитории на самом деле. Обе проверки
    # содержательны: если он перестанет выглядеть секретом, тест выродится;
    # если у него пропадёт объявление, основная проверка станет красной.
    shipped = REPO_ROOT / "tests" / "fixtures" / "broker-answer-samples.json"
    assert _looks_like_secret(shipped), "образец перестал выглядеть как секрет"
    assert _declares_itself_fake(shipped), "образец не объявляет себя подделкой"

    # И, главное, сам обход. Проверять только предикаты было бы недостаточно:
    # пропуск каталога жил не в них, а в `_candidate_secret_files`, и вернуть
    # его туда можно, не тронув ни одной проверенной выше строки.
    planted = REPO_ROOT / "tests" / "fixtures" / "walk-probe.json"
    planted.write_text(
        '{"access_token": "0123456789abcdef0123456789"}', encoding="utf-8"
    )
    try:
        candidates, _ = _candidate_secret_files()
    finally:
        planted.unlink()
    assert planted in candidates, (
        "обход не заглядывает в tests/fixtures/ — файл с настоящим ответом "
        "сервиса авторизации пройдёт мимо проверки и уедет в историю"
    )


def test_secret_looking_files_are_not_committable() -> None:
    """Всё, что похоже на секрет, git обязан не видеть.

    Решение 0003 говорит: файлы, которые программа создаёт сама, включая файл
    с токеном, лежат в `userdata/`. Пока это только соглашение, оно держится
    на дисциплине: первая же итерация Э1-4, положившая токен рядом с модулем,
    отдаст `git add -A` рабочий доступ к счёту на срок до 90 дней. Переписать
    историю после `push` надёжно нельзя, ротация — руками в кабинете брокера.
    """
    if shutil.which("git") is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("нет git или это не рабочая копия репозитория")

    candidates, unreadable = _candidate_secret_files()
    assert not unreadable, (
        "файлы не читаются, проверка по телу для них не выполнена:\n  "
        + "\n  ".join(str(path.relative_to(REPO_ROOT)) for path in unreadable)
    )
    if not candidates:
        return

    # `-z` обязателен: без него git экранирует кириллицу в C-подобные escape'ы
    # (`core.quotePath` по умолчанию включён), и совпадение строк не срабатывает —
    # тест падал бы ложно именно на «токен.txt», ради которого паттерн и написан.
    relative = [str(path.relative_to(REPO_ROOT).as_posix()) for path in candidates]
    result = subprocess.run(
        [*GIT_ISOLATED, "check-ignore", "-z", "--stdin"],
        cwd=REPO_ROOT,
        input="\0".join(relative),
        capture_output=True,
        text=True,
        check=False,
    )
    # 0 — что-то проигнорировано, 1 — ничего, всё остальное — отказ самого git.
    assert result.returncode in (0, 1), (
        f"git check-ignore не отработал (код {result.returncode}): "
        f"{result.stderr.strip() or 'без вывода'}"
    )
    ignored = set(result.stdout.split("\0")) - {""}
    committable = sorted(set(relative) - ignored)

    assert not committable, (
        "файлы выглядят как секрет, но git готов их закоммитить:\n  "
        + "\n  ".join(committable)
        + "\n\nПоложить в userdata/ (решение 0003) либо закрыть правилом .gitignore. "
        "Тестовые подделки — только в tests/fixtures/."
    )
