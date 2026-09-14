#!/bin/bash
# PreToolUse: Bash — предохранитель расхода контекста.
#
# Замер 14.09.2026 (`.docs/quality/context-cost-2026-09-14.md`): `Bash` кладёт
# в контекст 58 % всего, что туда попадает от инструментов. Внутри этого:
# 13 % заходов несут 32 % всей выдачи — дампы файлов без ограничителя. Контекст
# перечитывается каждым следующим запросом, поэтому лишний дамп оплачивается
# не один раз, а столько раз, сколько осталось запросов в сессии.
#
# ⚠️ Проверки на ПОВТОРНОЕ чтение здесь нет, и это решение по замеру, а не недоделка.
# Первая редакция такую проверку имела: файл, уже читавшийся в сессии и с тех пор
# не менявшийся, блокировался. Замер 14.09.2026 показал, что ловить нечего —
# дословно совпадающих выдач среди 1731 оказалось 10 штук, 0,2 % объёма. А все 226
# «повторных открытий» были РАЗНЫМИ кусками одного файла, то есть новым содержимым.
# Проверка запрещала бы ровно то, что сама же советует в тексте отказа. Сторож,
# срабатывающий только ложно, хуже отсутствующего.
#
# Блокировка — exit 2 со stderr: работает независимо от версии схемы JSON у хуков.
#
# Предохранитель, который при собственной поломке молча пропускает всё, — хуже
# отсутствующего. Поэтому разбор входа дублирован, а о невозможности разобрать
# хук говорит вслух (правило 13: молчание — самостоятельный дефект).

set -uo pipefail

INPUT=$(cat)

if command -v jq >/dev/null 2>&1; then
    CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)
    SID=$(printf '%s' "$INPUT" | jq -r '.session_id // "nosession"' 2>/dev/null)
elif command -v python3 >/dev/null 2>&1; then
    read -r CMD SID < <(printf '%s' "$INPUT" | python3 -c \
'import json,sys
d=json.load(sys.stdin)
print(d.get("tool_input",{}).get("command","").replace("\n"," "), d.get("session_id","nosession"))' 2>/dev/null)
    case "$RAZBOR" in *OGR=1*) OGRANICHEN=1 ;; esac
    case "$RAZBOR" in *NEYASNO=1*) NEYASNO=1 ;; *) NEYASNO=0 ;; esac
    FILES=$(printf '%s\n' "$RAZBOR" | tail -n +2)
else
    printf '⚠️ bash-output-guard: нет ни jq, ни python3 — предохранитель расхода НЕ РАБОТАЕТ.\n' >&2
    exit 0
fi

[ -n "${CMD:-}" ] || exit 0

# Запись файла — не чтение. Heredoc и перенаправление пропускаем целиком:
# `cat > файл <<'EOF'` это запись, и блокировать её нельзя.
case "$CMD" in
    *'<<'*|*'>'*) exit 0 ;;
esac

POROG_BYTES=20000          # больше этого дамп без ограничителя не пропускаем
# Ограничитель где-нибудь в команде — значит выдача уже сужена.
OGRANICHEN=0
case "$CMD" in
    *'head'*|*'tail'*|*'wc '*|*'grep'*|*'| jq'*) OGRANICHEN=1 ;;
esac

deny() {
    printf '⛔ ПРЕДОХРАНИТЕЛЬ РАСХОДА КОНТЕКСТА\n\n%s\n\n%s\n' "$1" \
"Замер 14.09.2026: выдача Bash — 58 % всего, что инструменты кладут в контекст,
и каждый следующий запрос сессии перечитывает её заново. Разбор —
.docs/quality/context-cost-2026-09-14.md" >&2
    exit 2
}

# Кандидаты на чтение. Разбор идёт по звеньям команды: звено считается чтением,
# если начинается с читающей команды, и тогда из него берутся все существующие файлы.
#
# ⚠️ Регулярка `(cat|sed -n[^|]*)[[:space:]]+(…)` первой редакции ловила `cat файл`
# и пропускала `sed -n '1,5p' файл` — жадная часть съедала имя файла целиком.
# Сторож молча не видел половины случаев, ради которых поставлен.
if command -v python3 >/dev/null 2>&1; then
    RAZBOR=$(printf '%s' "$CMD" | python3 -c '
import os,re,shlex,sys
CHITAET={"cat","sed","head","tail","less","more","nl","tac"}
cmd=sys.stdin.read()
# Узкий диапазон sed — это ограничитель, а не дамп: sed -n "10,20p" даёт 11 строк.
ogr=0
for a,b in re.findall(r"sed\s+-n\s+[\x27\"]?(\d+),(\d+)p", cmd):
    if int(b)-int(a) <= 500: ogr=1
out=[]
neyasno=0
for zveno in re.split(r"\||;|&&|\|\||\n", cmd):
    try: slova=shlex.split(zveno)
    except ValueError: slova=zveno.split()
    if not slova: continue
    i=0
    while i<len(slova) and (slova[i] in ("cd","sudo","time","env") or "=" in slova[i]):
        i+=2 if slova[i]=="cd" else 1
    if i>=len(slova) or os.path.basename(slova[i]) not in CHITAET: continue
    for s in slova[i+1:]:
        if s.startswith("-"): continue
        # Хук видит команду ДО подстановки переменных шелла: `cat $F` приходит
        # так и есть. Развернуть нельзя (это eval), значит размер файла неизвестен.
        if "$" in s or "`" in s: neyasno=1; continue
        if os.path.isfile(s): out.append(s)
print("OGR=%d NEYASNO=%d" % (ogr, neyasno))
print("\n".join(dict.fromkeys(out)))
' 2>/dev/null)
    case "$RAZBOR" in *OGR=1*) OGRANICHEN=1 ;; esac
    case "$RAZBOR" in *NEYASNO=1*) NEYASNO=1 ;; *) NEYASNO=0 ;; esac
    FILES=$(printf '%s\n' "$RAZBOR" | tail -n +2)
else
    NEYASNO=0
    FILES=$(printf '%s\n' "$CMD" | grep -oE 'cat[[:space:]]+([^|;&[:space:]]+)' 2>/dev/null \
        | awk '{print $NF}' | sort -u)
    printf '⚠️ bash-output-guard: нет python3 — разбор упрощён, sed/head не проверяются.\n' >&2
fi

# Файл назван переменной — размер неизвестен, значит и потолок выдачи неизвестен.
# Дыру нашла встречная проверка сессии life/.mounted 14.09.2026: `cat $F` проходил
# насквозь, потому что литерального файла с именем "$F" на диске нет и звено молча
# отбрасывалось. Развернуть переменную предохранитель не может, но может потребовать
# ограничитель — тогда потолок выдачи известен независимо от того, что за файл.
if [ "${NEYASNO:-0}" -eq 1 ] && [ "$OGRANICHEN" -eq 0 ]; then
    deny "Файл назван переменной — предохранитель не видит, что читается и какого размера.
Развернуть переменную он не может: это было бы исполнением команды.

Поставь ограничитель, и потолок выдачи станет известен независимо от файла:
| head -n 50, или диапазон sed -n '1,50p', или grep по признаку.
Если файл действительно нужен целиком — скажи это явно: | head -n 100000."
fi

for F in $FILES; do
    case "$F" in -*|'') continue ;; esac
    [ -f "$F" ] || continue
    SZ=$(stat -c %s "$F" 2>/dev/null) || continue
    MT=$(stat -c %Y "$F" 2>/dev/null) || continue

    # Случай 1: дамп крупного файла без ограничителя.
    if [ "$OGRANICHEN" -eq 0 ] && [ "$SZ" -gt "$POROG_BYTES" ]; then
        deny "Файл $F — $SZ байт, читается целиком без ограничителя.
Это примерно $((SZ / 4)) токенов в контекст, и они останутся там до конца сессии.

Возьми нужное: | head -n 50, или диапазон sed -n '1,50p', или grep по признаку.
Нужен объём, а не содержимое — wc -l.
Если файл действительно нужен целиком — скажи это явно: | head -n 100000."
    fi

done

exit 0
