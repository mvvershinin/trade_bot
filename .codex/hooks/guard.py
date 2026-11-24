#!/usr/bin/env python3
"""Bridge Codex hook JSON to the project's existing shell guards.

Shell guards are copied, not executed from .claude/. This adapter changes only
the harness interface and working-directory handling, not trading policy.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def deny(reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": reason[:8000],
    }}, ensure_ascii=False))


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else 'pre'
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError('hook input is not an object')
    except (ValueError, TypeError) as exc:
        if mode == 'pre':
            deny(f'Не удалось разобрать вход предохранителя Codex: {exc}')
        return
    tool_input = payload.get('tool_input') or {}
    if not isinstance(tool_input, dict):
        deny('Некорректный tool_input: предохранители не проверили команду.')
        return
    command = tool_input.get('command', tool_input.get('cmd', ''))
    if isinstance(command, list):
        # Legacy shell arguments: do not lose flags/parts while adapting.
        command = ' '.join(str(part) for part in command)
    if not isinstance(command, str):
        deny('Некорректный формат команды: предохранители не отработали.')
        return
    if mode == 'pre' and not command:
        return
    payload['tool_input'] = {**tool_input, 'command': command}
    cwd = Path(payload.get('cwd') or ROOT)
    if not cwd.is_absolute():
        cwd = ROOT / cwd
    workdir = tool_input.get('workdir')
    if workdir:
        requested = Path(workdir)
        cwd = requested if requested.is_absolute() else cwd / requested
    if not cwd.is_dir():
        deny('Рабочая папка команды недоступна: предохранители не проверили пути.')
        return
    env = {**os.environ, 'CODEX_TRADE_ROOT': str(ROOT)}
    checks = [('live-trading-guard.sh', []), ('mutation-guard.sh', ['commit']),
              ('bash-output-guard.sh', [])] if mode == 'pre' else [('mutation-guard.sh', ['finish'])]
    for script, args in checks:
        try:
            result = subprocess.run(
                ['bash', str(ROOT / '.codex/hooks' / script), *args],
                input=json.dumps(payload), text=True, capture_output=True,
                cwd=cwd, env=env, timeout=7, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            if mode == 'pre':
                deny(f'Предохранитель {script} не выполнен: {exc}')
            else:
                print(json.dumps({'systemMessage': f'Предохранитель {script} не выполнен: {exc}'}, ensure_ascii=False))
            return
        if result.returncode:
            deny(result.stderr.strip() or f'Предохранитель {script}: код {result.returncode}')
            return
        if mode != 'pre' and result.stderr.strip():
            print(json.dumps({'systemMessage': result.stderr.strip()[:8000]}, ensure_ascii=False))


if __name__ == '__main__':
    main()
