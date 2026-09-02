"""Нотис по упрямым головам: кто из свободных не берётся и почему.

Ничего не блокирует и не помечает в базе — только читает журналы сбора и
пишет сводку. Блокировать вправе только Босс и только руками.
"""

import json
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path("/home/sergey/pet/uv/ai-email-code")
sys.path.insert(0, str(ROOT))

from backend.app.config import allow_direct_localhost  # noqa: E402

allow_direct_localhost()
from backend.app.supabase_client import get_supabase  # noqa: E402

SITES = {"gorouter.app": 5, "tabitoken.com": 4}
LOGS = [ROOT / "log" / n for n in ("harvest_cron.txt", "harvest_journal.txt", "harvest_history.txt")]
OUT = ROOT / "log" / "harvest_stubborn.txt"
# Тот же список машиночитаемо: `harvest_accounts.py` читает его и даёт проблемной
# голове ровно один заход — без второго прокси и без Re-send. Пометок в базе нет,
# голова остаётся живой и свободной; насиловать её мы просто перестаём.
PROBLEM_FILE = ROOT / "log" / "harvest_problem.json"
VERDICTS = ("OK", "НЕ ВОШЁЛ", "НЕТ КЛЮЧА", "ОТКАЗ СТАНЦИИ", "НЕТ КОДА", "ПРОПУСК")

# Вина головы, а не станции. `ОТКАЗ СТАНЦИИ` сюда не входит намеренно: это лимит
# `/api/oauth/state` по нашему IP, ящик и пароль в нём не участвуют, и записывать
# такую голову в проблемные значит оговорить рабочий аккаунт.
OWN_FAULT = ("НЕ ВОШЁЛ", "НЕТ КОДА")
REPEAT_FROM = 2  # с какого по счёту своего отказа голова считается проблемной

MAILS = ("hotmail.com", "rambler.ru")

sb = get_supabase()

taken = {}
for name, sid in SITES.items():
    rows = sb.table("main_site_account").select("github_id").eq("site_id", sid).execute().data
    taken[name] = {r["github_id"] for r in rows if r["github_id"] is not None}

pool = []
start = 0
while True:
    rows = (
        sb.table("main_github")
        .select("id, login, email, active, error_status")
        .order("id")
        .range(start, start + 999)
        .execute()
        .data
    )
    pool += rows
    if len(rows) < 1000:
        break
    start += 1000
alive = [r for r in pool if r["active"] and not r["error_status"]]


def grep(pattern: str) -> list[str]:
    out = []
    for f in LOGS:
        if not f.exists():
            continue
        res = subprocess.run(
            ["grep", "-a", "-h", "-E", pattern, str(f)], capture_output=True, text=True
        )
        out += [ln for ln in res.stdout.splitlines() if ln.strip()]
    return out


lines = [
    "НОТИС: свободные головы, которые не берутся автосбором",
    f"составлен {datetime.now():%Y-%m-%d %H:%M}",
    "",
    "Никто не заблокирован и не помечен: active и error_status не тронуты.",
    "Блокировать вправе только Босс и только вручную.",
    "",
    f"ПОВТОРНЫЙ ОТКАЗ — своих отказов ({' или '.join(OWN_FAULT)}) набралось "
    f"{REPEAT_FROM} и больше, ни одного OK. Такая голова считается проблемным e-mail.",
    "ОТКАЗ СТАНЦИИ в этот счёт не идёт: это лимит /api/oauth/state по нашему IP,",
    "ящик в нём не участвует.",
    "",
]

problem: list[str] = []
problem_ids: dict[int, str] = {}

for name in SITES:
    free = [r for r in alive if r["id"] not in taken[name]]
    seen = [r for r in free if (r["email"] or "").lower().endswith(MAILS)]
    other = [r for r in free if r not in seen]
    lines += [
        f"=== {name}: свободно {len(free)} — автосбору видно {len(seen)} "
        f"(hotmail через Graph, rambler по IMAP), вне пула {len(other)}",
        "",
    ]
    for r in sorted(seen, key=lambda x: x["id"]):
        hits = grep(rf"\b{r['login']}\b")
        verd = Counter()
        dates = []
        for ln in hits:
            for v in VERDICTS:
                if f": {v}" in ln or f" {v} [" in ln:
                    verd[v] += 1
            m = re.match(r"(\d\d\.\d\d \d\d:\d\d)", ln)
            if m:
                dates.append(m.group(1))
        reasons = ", ".join(f"{v}×{c}" for v, c in verd.most_common()) or "в журналах не найдена"
        last = dates[-1] if dates else "—"
        own = sum(verd[v] for v in OWN_FAULT)
        mark = ""
        if own >= REPEAT_FROM and not verd["OK"]:
            mark = f"  ← ПОВТОРНЫЙ ОТКАЗ ×{own}, проблемный e-mail"
            problem.append(f"  {r['id']:>4} {r['login']:<26} {r['email']:<32} {name}  своих отказов {own}")
            problem_ids[int(r["id"])] = f"{reasons} (последний {last})"
        lines.append(
            f"  {r['id']:>4} {r['login']:<26} заходов {len(hits):>3}  "
            f"последний {last}  {reasons}{mark}"
        )
    lines.append("")

lines += ["=== СВОДКА ПРОБЛЕМНЫХ E-MAIL (повторный отказ, решение за Боссом)", ""]
lines += problem or ["  пусто — повторных своих отказов нет"]
lines += [
    "",
    f"Автосбор берёт этот список из {PROBLEM_FILE.name}: одна попытка на голову, "
    "без второго адреса и без Re-send.",
    "",
]

PROBLEM_FILE.write_text(
    json.dumps(
        {"составлен": f"{datetime.now():%Y-%m-%d %H:%M}", "ids": problem_ids},
        ensure_ascii=False,
        indent=1,
    ),
    encoding="utf-8",
)

OUT.write_text("\n".join(lines), encoding="utf-8")
print("\n".join(lines))
print(f"\nзаписано: {OUT}")
