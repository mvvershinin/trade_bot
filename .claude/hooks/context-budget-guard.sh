#!/bin/bash
# Stop — сторож длины сессии.
#
# Замер 14.09.2026 (`.docs/quality/context-cost-2026-09-14.md`): три самые длинные
# сессии дают 64 % всего чтения кэша. Сессия стартует с 35–58k токенов и дорастает
# до 0,8–1,0M; медианный запрос в такой сессии платит за 380–540k. Моделирование
# на фактическом потоке: порог 300k срезает 61 % контекста и 44 % всего расхода,
# ценой около 1,8 перезапуска в день.
#
# Полагаться на память человека тут нельзя: статуслайн показывает процент от окна
# в 1M, и 300k выглядит как «занято всего 30 %». Занятость окна и цена запроса —
# разные величины. Хук говорит вторую вслух (правило 13).
#
# Хук НЕ блокирует и НЕ мешает работать: он только показывает строку владельцу
# счёта. Решение — сохранить состояние и /clear — принимает человек.

set -uo pipefail

INPUT=$(cat)

razobrat() {
    if command -v jq >/dev/null 2>&1; then
        printf '%s' "$INPUT" | jq -r "$1 // empty" 2>/dev/null
    elif command -v python3 >/dev/null 2>&1; then
        printf '%s' "$INPUT" | python3 -c \
"import json,sys;d=json.load(sys.stdin);print(d.get('${1#.}',''))" 2>/dev/null
    fi
}

TRANSCRIPT=$(razobrat '.transcript_path')
SID=$(razobrat '.session_id')
[ -n "${TRANSCRIPT:-}" ] && [ -f "$TRANSCRIPT" ] || exit 0

command -v python3 >/dev/null 2>&1 || {
    printf '⚠️ context-budget-guard: нет python3 — сторож длины сессии НЕ РАБОТАЕТ.\n' >&2
    exit 0
}

POROG=300000

CTX=$(python3 - "$TRANSCRIPT" <<'PY' 2>/dev/null
import json,sys
ctx=0
with open(sys.argv[1], errors='ignore') as f:
    for line in f:
        try: u=(json.loads(line).get('message') or {}).get('usage')
        except Exception: continue
        if u:
            v=(u.get('input_tokens',0) + u.get('cache_read_input_tokens',0)
               + u.get('cache_creation_input_tokens',0))
            if v: ctx=v
print(ctx)
PY
)
[ -n "${CTX:-}" ] && [ "$CTX" -gt 0 ] 2>/dev/null || exit 0
[ "$CTX" -ge "$POROG" ] || exit 0

# Не пилить одним и тем же: следующая строка — только когда выросло ещё на 100k.
OTMETKA_DIR="${CLAUDE_PROJECT_DIR:-$PWD}/.claude/.state/${SID:-nosession}"
OTMETKA="${OTMETKA_DIR}/context-warned"
mkdir -p "$OTMETKA_DIR" 2>/dev/null || true
PROSHLYJ=0
[ -f "$OTMETKA" ] && PROSHLYJ=$(cat "$OTMETKA" 2>/dev/null || echo 0)
[ "$CTX" -ge "$((PROSHLYJ + 100000))" ] || exit 0
printf '%s' "$CTX" > "$OTMETKA" 2>/dev/null || true

K=$((CTX / 1000))
TEXT="⚠️ Контекст сессии — ${K}k токенов, порог 300k пройден.
Каждый следующий запрос оплачивает все ${K}k заново: чтение кэша — 70 % расхода проекта.
Приём: допиши .docs/HANDOFF.md (что сделано, где остановился) → /clear → новая сессия читает его и продолжает.
Замер и цена: .docs/quality/context-cost-2026-09-14.md"

if command -v jq >/dev/null 2>&1; then
    jq -nc --arg t "$TEXT" '{systemMessage:$t}'
else
    python3 -c 'import json,sys;print(json.dumps({"systemMessage":sys.argv[1]}))' "$TEXT"
fi
exit 0
