import pathlib, subprocess, shutil
ROOT = pathlib.Path(__file__).resolve().parent
PORT = "tests/test_app_port.py"
MUTATIONS = [
    ("порт ходит на биржу без проводки", "app/port.py",
     "        if self._point.ask is None:\n            return\n        if self._point.task is not None",
     "        if self._point.task is not None", PORT),
    ("wait не ждёт ответа биржи", "app/port.py",
     "        for _ in range(_WAIT_ROUNDS):\n            if self._point.task is not None:\n                await _quiet(self._point.task)\n",
     "        for _ in range(1):\n            if False:\n                pass\n", PORT),
    ("wait делает один круг", "app/port.py",
     "_WAIT_ROUNDS = 4", "_WAIT_ROUNDS = 1", PORT),
]
def clean():
    for cache in ROOT.rglob("__pycache__"):
        if ".venv" not in str(cache):
            shutil.rmtree(cache, ignore_errors=True)
for name, path, old, new, target in MUTATIONS:
    file = ROOT / path
    text = file.read_text(encoding="utf-8")
    if text.count(old) != 1:
        print(f"ПРОПУСК  {name}: якорь {text.count(old)} раз"); continue
    file.write_text(text.replace(old, new), encoding="utf-8")
    clean()
    try:
        done = subprocess.run([".venv/bin/pytest", target, "-q", "-p", "no:randomly", "--no-header"],
                              cwd=ROOT, capture_output=True, text=True, timeout=600)
        tail = done.stdout.strip().splitlines()[-1] if done.stdout else "нет вывода"
        code = done.returncode
    finally:
        file.write_text(text, encoding="utf-8")
    print(f"{'ПОЙМАНО' if code else 'УШЛО!!!'}  {name}: {tail}")
clean()
