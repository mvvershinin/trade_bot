"""Мутации: ломаем поведение и смотрим, ловит ли это проверка."""
import pathlib, subprocess, shutil

ROOT = pathlib.Path(__file__).resolve().parent
PORT = "tests/test_app_port.py"
WATCH = "tests/test_app_observe_port.py"
POINT = "tests/test_market_point.py tests/test_market_iss.py"

MUTATIONS = [
    ("проход не спрашивает биржу", "app/port.py",
     "            self._ask_point_value(symbol)\n", "            pass\n", PORT),
    ("ответ биржи не применяется", "app/port.py",
     "        self.apply_settings(\n            self._values.replace(ruble_per_point=rubles, ruble_per_point_source=told)\n        )\n",
     "        return\n", PORT),
    ("беда биржи молчит", "app/port.py",
     "        self._point.trouble = reason\n        self.note(",
     "        self._point.trouble = reason\n        return\n        self.note(", PORT),
    ("беда повторяется на каждом проходе", "app/port.py",
     "        if reason == self._point.trouble:\n            return\n", "", PORT),
    ("передышки нет", "app/port.py",
     "        if now - self._point.asked_at.get(symbol, float(\"-inf\")) < POINT_VALUE_GAP:\n            return\n",
     "", PORT),
    ("порт ходит на биржу без проводки", "app/port.py",
     "        if self._point.ask is None:\n            return\n        if self._point.task is not None",
     "        if self._point.task is not None", PORT),
    ("wait не ждёт ответа биржи", "app/port.py",
     "        for _ in range(_WAIT_ROUNDS):\n            if self._point.task is not None:\n                await _quiet(self._point.task)\n",
     "        for _ in range(1):\n            if False:\n                pass\n", PORT),
    ("чужой инструмент применяется", "app/port.py",
     "        if shown != symbol:\n            return\n", "", PORT),
    ("текст про предохранители вернулся к прежней лжи", "app/port.py",
     'NO_GUARDS = (\n    "Потолок объёма, дневной лимит убытка и запас свободных средств "',
     'NO_GUARDS = (\n    "Потолок объёма и проверка свободных средств в движке отсутствуют. "\n    "Хвост: "',
     PORT),
    ("глубина снова задаёт живой ход", "app/port.py",
     "            convert.run_costs(frame.values),\n        )\n",
     "            convert.run_costs(frame.values),\n            self._days,\n            self._until,\n        )\n",
     WATCH),
    ("пересборка живого хода выключена целиком", "app/port.py",
     "        if self._watch.observer is not None and key != self._watch.key:",
     "        if False:", WATCH),
    ("стоимость пункта = стоимость шага", "market/iss.py",
     "        return self.step_price / self.price_step",
     "        return self.step_price", POINT),
    ("нет STEPPRICE — подставляется единица", "market/point.py",
     "    if rubles is None or rubles <= 0:\n        return PointValue(secid, None, _no_step_price(spec, rubles), spec)\n",
     "    if rubles is None or rubles <= 0:\n        return PointValue(secid, 1.0, _source(spec), spec)\n",
     POINT),
    ("поиск обрывается на первом рынке", "market/point.py",
     "        except IssUnknownInstrument:\n            continue\n",
     "        except IssUnknownInstrument:\n            break\n", POINT),
    ("в строке происхождения стоит момент запроса", "market/point.py",
     "    step = _number(spec.price_step)\n",
     "    import time as _t\n    step = _number(spec.price_step) + str(_t.monotonic())\n",
     POINT),
    ("IMTIME снова становится строкой None", "market/iss.py",
     '        quoted_at=str(row.get("IMTIME") or ""),\n',
     '        quoted_at=str(row.get("IMTIME", "")),\n', POINT),
    ("отрицательная стоимость шага проходит", "market/point.py",
     "    if rubles is None or rubles <= 0:", "    if rubles is None:", POINT),
]


def clean() -> None:
    for cache in ROOT.rglob("__pycache__"):
        if ".venv" not in str(cache):
            shutil.rmtree(cache, ignore_errors=True)


def run(target: str) -> tuple[int, str]:
    clean()
    done = subprocess.run(
        [".venv/bin/pytest", *target.split(), "-q", "-p", "no:randomly", "--no-header"],
        cwd=ROOT, capture_output=True, text=True,
    )
    tail = done.stdout.strip().splitlines()[-1] if done.stdout else "нет вывода"
    return done.returncode, tail


for name, path, old, new, target in MUTATIONS:
    file = ROOT / path
    text = file.read_text(encoding="utf-8")
    if text.count(old) != 1:
        print(f"ПРОПУСК  {name}: якорь встречается {text.count(old)} раз")
        continue
    file.write_text(text.replace(old, new), encoding="utf-8")
    try:
        code, tail = run(target)
    finally:
        file.write_text(text, encoding="utf-8")
    print(f"{'ПОЙМАНО' if code else 'УШЛО!!!'}  {name}: {tail}")
clean()
