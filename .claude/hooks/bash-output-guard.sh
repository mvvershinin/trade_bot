#!/bin/bash
# PreToolUse: Bash — предохранитель расхода контекста.
#
# 13 % заходов несут 32 % всей выдачи — дампы файлов без ограничителя. Контекст
# перечитывается каждым следующим запросом, поэтому лишний дамп оплачивается
# не один раз, а столько раз, сколько осталось запросов в сессии.
# Замер — `.docs/quality/context-cost-2026-09-14.md`.
#
# ⚠️ РЕДАКЦИЯ 2, 14.09.2026. Первая была выключена на 59 % команд и молчала об этом.
# Причина — гейт «есть `>` или `<<` где-нибудь в строке → пропустить всё»: он
# задумывался как «это запись файла, не чтение», но срабатывал на посторонних
# частях команды. Замер по 2413 заходам: `2>` или `2>&1` в хвосте — 43 % команд,
# heredoc где-то в команде — 15 %, запись в конце — 1 %. То есть сторож молчал
# ровно на том, как команды обычно и пишутся. Нашла встречная проверка сессии
# `trihology-c6`, воспроизведено здесь дословно.
#
# Отсюда устройство разбора: **посторонняя часть команды не выключает проверку
# у всей команды.**
#   • хвосты stderr (`2>&1`, `2>файл`) срезаются до разбора — это не запись данных;
#   • тела heredoc вырезаются до разбора, иначе документ, внутри которого написано
#     `cat файл`, читается как команда чтения;
#   • команда режется на утверждения (`;`, `&&`, `||`), запись отключает проверку
#     только у СВОЕГО утверждения;
#   • внутри утверждения — звенья конвейера, ограничитель в любом звене сужает
#     весь конвейер.
#
# Блокировка — exit 2 со stderr: работает независимо от версии схемы JSON у хуков.
#
# ⚠️ Проверки на ПОВТОРНОЕ чтение здесь нет, и это решение по замеру, а не недоделка.
# Дословно совпадающих выдач среди 1731 оказалось 10 штук, 0,2 % объёма, и все они
# мельче порога — сторож не мог поймать ни одного и срабатывал бы только ложно.
# Подтверждено на втором наборе независимо: 0,23 %.

set -uo pipefail

INPUT=$(cat)

# python3 нужен и для разбора команды, и как запасной разбор входа. Без него
# предохранитель не работает, и он обязан сказать это вслух: предохранитель,
# молча пропускающий всё, хуже отсутствующего.
if ! command -v python3 >/dev/null 2>&1; then
    printf '⚠️ bash-output-guard: нет python3 — ПРЕДОХРАНИТЕЛЬ РАСХОДА НЕ РАБОТАЕТ.\n' >&2
    exit 0
fi

CMD=$(printf '%s' "$INPUT" | python3 -c \
'import json,sys
try: print(json.load(sys.stdin).get("tool_input",{}).get("command",""), end="")
except Exception: pass' 2>/dev/null)

[ -n "${CMD:-}" ] || exit 0

POROG_BYTES=20000

RESHENIE=$(CMD="$CMD" POROG="$POROG_BYTES" python3 <<'PY' 2>/dev/null
import os, re, shlex

cmd = os.environ.get("CMD", "")
porog = int(os.environ.get("POROG", "20000"))

CHITAET = {"cat", "sed", "head", "tail", "less", "more", "nl", "tac"}
SUZHAET = {"head", "tail", "wc", "grep", "egrep", "fgrep", "rg", "jq", "sort", "uniq"}
PRISTAVKI = {"cd", "sudo", "time", "env", "nohup", "exec"}

# 1. Тела heredoc — не команда, а данные. Вырезаем до всякого разбора.
def bez_heredoc(s: str) -> str:
    out, i = [], 0
    for m in re.finditer(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", s):
        out.append(s[i:m.start()])
        konec = re.search(r"^\s*%s\s*$" % re.escape(m.group(2)), s[m.end():], re.M)
        i = m.end() + (konec.end() if konec else len(s) - m.end())
    out.append(s[i:])
    return " ".join(out)

# 2. Хвосты stderr — не запись данных. `2>/dev/null` есть у 43 % команд.
def bez_stderr(s: str) -> str:
    return re.sub(r"\d?>&\d|\b2>>?\s*\S+", " ", s)

# 3. Резка на утверждения и звенья — вне кавычек.
def rezat(s: str, seps: tuple) -> list:
    части, tek, kav, i = [], [], None, 0
    while i < len(s):
        c = s[i]
        if kav:
            if c == kav: kav = None
            tek.append(c); i += 1; continue
        if c in "'\"":
            kav = c; tek.append(c); i += 1; continue
        for sep in seps:
            if s.startswith(sep, i):
                части.append("".join(tek)); tek = []; i += len(sep); break
        else:
            tek.append(c); i += 1
    части.append("".join(tek))
    return [p for p in части if p.strip()]

def slova(z: str) -> list:
    try: return shlex.split(z)
    except ValueError: return z.split()

def glagol(z: str) -> str:
    sl = slova(z); i = 0
    while i < len(sl) and (sl[i] in PRISTAVKI or "=" in sl[i].split("/")[0]):
        i += 2 if sl[i] == "cd" else 1
    return os.path.basename(sl[i]) if i < len(sl) else ""

text = bez_stderr(bez_heredoc(cmd))

for utv in rezat(text, (";", "&&", "||", "\n")):
    # Запись отключает проверку только у СВОЕГО утверждения.
    if re.search(r"(?<![0-9])>>?", utv):
        continue
    zvenya = rezat(utv, ("|",))
    if not zvenya:
        continue
    # Ограничитель в любом звене сужает весь конвейер.
    ogr = any(glagol(z) in SUZHAET for z in zvenya)
    for a, b in re.findall(r"sed\s+-n\s+['\"]?(\d+),(\d+)p", utv):
        if int(b) - int(a) <= 500: ogr = True
    if ogr:
        continue
    for z in zvenya:
        if glagol(z) not in CHITAET:
            continue
        for s in slova(z)[1:]:
            if s.startswith("-"):
                continue
            # Команда приходит ДО подстановки переменных шелла. Развернуть нельзя —
            # это было бы исполнением. Значит размер неизвестен, и вместо «какой
            # это файл» требуем «ограничь выдачу».
            if "$" in s or "`" in s:
                print("VAR"); raise SystemExit
            if os.path.isfile(s) and os.path.getsize(s) > porog:
                print("BIG\t%s\t%d" % (s, os.path.getsize(s))); raise SystemExit
print("OK")
PY
)

otkaz() {
    printf '⛔ ПРЕДОХРАНИТЕЛЬ РАСХОДА КОНТЕКСТА\n\n%s\n\n%s\n' "$1" \
"Замер 14.09.2026: выдача Bash — 58 % всего, что инструменты кладут в контекст,
и каждый следующий запрос сессии перечитывает её заново. Разбор —
.docs/quality/context-cost-2026-09-14.md" >&2
    exit 2
}

case "${RESHENIE:-OK}" in
VAR)
    otkaz "Файл назван переменной — предохранитель не видит, что читается и какого размера.
Развернуть переменную он не может: это было бы исполнением команды.

Поставь ограничитель, и потолок выдачи станет известен независимо от файла:
| head -n 50, или диапазон sed -n '1,50p', или grep по признаку.
Если файл действительно нужен целиком — скажи это явно: | head -n 100000."
    ;;
BIG*)
    F=$(printf '%s' "$RESHENIE" | cut -f2)
    SZ=$(printf '%s' "$RESHENIE" | cut -f3)
    otkaz "Файл $F — $SZ байт, читается целиком без ограничителя.

Содержимого ты так всё равно не получишь: Claude Code режет выдачу Bash примерно
на 18 000 символах и подменяет заглушкой «Output too large». Заход будет потрачен,
в контекст попадёт обрывок, и читать придётся заново — уже прицельно.

Возьми нужное сразу: | head -n 50, или диапазон sed -n '1,50p', или grep по признаку.
Нужен объём, а не содержимое — wc -l.
Если файл действительно нужен целиком — скажи это явно: | head -n 100000."
    ;;
esac

exit 0
