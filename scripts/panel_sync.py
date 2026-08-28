"""Заполнить баланс и партнёрскую ссылку по паре доступов к панели.

Разделение труда с Боссом: он вписывает то, чего в API нет — логин, почту,
сам API-ключ (`token`), «Токен доступа» и «ID пользователя» со страницы /profile.
Остальное панель знает про себя сама, и переписывать это руками незачем.

API-ключ здесь не заполняется намеренно: New API отдаёт его замаскированным
(`GQgq**********Gnof`) и в списке `/api/token/`, и в карточке ключа — полное
значение панель показывает один раз при создании и больше никогда.

Партнёрская ссылка приводится к единому виду `https://<станция>/sign-up?aff=<код>`:
у части записей в базе лежал только код (`2PeJ`), а такой хвост без домена
никуда не вставить.

    uv run python scripts/panel_sync.py [домены станций] [--dry]
"""

import re
import sys
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import find_dotenv, load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.app.config import allow_direct_localhost  # noqa: E402
from scripts.notify_ui import TOPIC_CLAUDE, notify_ui  # noqa: E402

load_dotenv(find_dotenv())
allow_direct_localhost()

OUT_FILE = ROOT / "log" / "panel_sync.txt"

# Cloudflare у этих панелей пропускает только User-Agent Chrome Босса, с чужим
# вместо JSON приходит заглушка. Константу править при обновлении браузера.
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
TABLES = ("main_site_account", "main_site_account_custom")
QUOTA_PER_UNIT = 500000
TIMEOUT = 45
DEFAULT_AFF_PATH = "/sign-up"


def load_rows(only_sites: set[str]) -> list[dict]:
    """Записи с полной парой доступов к панели из обеих таблиц аккаунтов."""
    from backend.app.supabase_client import get_supabase

    sb = get_supabase()
    sites = {s["id"]: s for s in sb.table("main_site").select("id, name, meta").execute().data}

    rows = []
    for table in TABLES:
        res = (
            sb.table(table)
            .select("id, site_id, login, email, balance, aff, access_token, panel_id")
            .not_.is_("access_token", "null")
            .not_.is_("panel_id", "null")
            .order("id")
            .execute()
        )
        for r in res.data or []:
            site = sites.get(r["site_id"])
            token = (r["access_token"] or "").strip()
            if not site or not token:
                continue
            if only_sites and site["name"] not in only_sites:
                continue
            rows.append({
                **r,
                "table": table,
                "station": site["name"],
                "base": f"https://{site['name']}",
                "token": token,
            })
    return rows


def aff_paths(only_sites: set[str]) -> dict[str, str]:
    """Путь страницы регистрации по станциям — из ссылок, что уже есть в базе.

    По HTTP его не узнать: и `/sign-up`, и `/register` отдают одну и ту же
    оболочку SPA с кодом 200. Зато у большинства станций Босс уже скопировал
    готовую ссылку руками, и её путь — самое надёжное, что у нас есть.
    Станциям без образца достаётся `/sign-up`: так выглядят все ссылки,
    скопированные с панелей этого семейства.
    """
    from backend.app.supabase_client import get_supabase

    sb = get_supabase()
    sites = {s["id"]: s["name"] for s in sb.table("main_site").select("id, name").execute().data}
    paths: dict[str, str] = {}
    for table in TABLES:
        res = sb.table(table).select("site_id, aff").not_.is_("aff", "null").execute()
        for r in res.data or []:
            station = sites.get(r["site_id"])
            if not station or station in paths:
                continue
            if only_sites and station not in only_sites:
                continue
            m = re.match(rf"https://{re.escape(station)}(/[^?]*)\?aff=", (r["aff"] or "").strip())
            if m:
                paths[station] = m.group(1)
    return paths


def recount(only_sites: set[str], dry: bool) -> list[dict]:
    """Привести `main_site.cnt` к настоящему числу аккаунтов сайта.

    Счётчик в базе ведёт backend, но прибавляет и убавляет он только на вставке
    и удалении записи из `main_site_account`: аккаунты из `main_site_account_custom`
    он не видел никогда, а записи, заведённые сырым SQL, проходят мимо него вовсе.
    Отсюда расхождение на большинстве сайтов. Считаем обе таблицы и переписываем.
    """
    from backend.app.supabase_client import get_supabase

    sb = get_supabase()
    sites = sb.table("main_site").select("id, name, cnt").order("id").execute().data or []
    live: dict[int, int] = {}
    for table in TABLES:
        for r in sb.table(table).select("site_id").execute().data or []:
            live[r["site_id"]] = live.get(r["site_id"], 0) + 1

    out = []
    for s in sites:
        if only_sites and s["name"] not in only_sites:
            continue
        real = live.get(s["id"], 0)
        if real == s["cnt"]:
            continue
        out.append({"station": s["name"], "was": s["cnt"], "now": real})
        if not dry:
            sb.table("main_site").update({"cnt": real}).eq("id", s["id"]).execute()
    return out


def ask_panel(row: dict) -> dict:
    """Профиль аккаунта из New API. `New-Api-User` обязателен, без него 401."""
    with httpx.Client(
        headers={
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Authorization": f"Bearer {row['token']}",
            "New-Api-User": str(row["panel_id"]),
            "Referer": f"{row['base']}/dashboard/overview",
        },
        timeout=TIMEOUT,
        trust_env=True,
    ) as c:
        body = c.get(f"{row['base']}/api/user/self").json()
    if not body.get("success"):
        raise RuntimeError(body.get("message") or "панель отказала без причины")
    data = body.get("data") or {}
    return {
        "display_name": data.get("display_name") or data.get("username") or "",
        "username": data.get("username") or "",
        "balance": float(data.get("quota") or 0) / QUOTA_PER_UNIT,
        "aff_code": (data.get("aff_code") or "").strip(),
        "aff_count": int(data.get("aff_count") or 0),
        "aff_earned": float(data.get("aff_history_quota") or 0) / QUOTA_PER_UNIT,
    }


def aff_link(station: str, path: str, code: str) -> str:
    return f"https://{station}{path}?aff={code}"


def sync_row(row: dict, paths: dict[str, str], dry: bool) -> dict:
    """Один аккаунт: спросить панель, вписать баланс и ссылку, если разошлись."""
    out = {"id": row["id"], "table": row["table"], "station": row["station"],
           "login": row["login"], "changes": [], "note": ""}
    try:
        panel = ask_panel(row)
    except Exception as exc:
        out["note"] = f"панель не ответила: {exc}"
        return out

    # Токены вставляются руками, и опечатка молча перепишет чужую строку.
    # Поэтому пара доступов должна привести к тому логину, что стоит в записи.
    seen = {panel["display_name"].lower(), panel["username"].lower()}
    mine = (row["login"] or "").strip().lower()
    if mine and mine not in seen:
        out["note"] = (f"панель отдала «{panel['display_name']}», а в записи "
                       f"«{row['login']}» — не трогаю")
        return out
    out["panel_login"] = panel["display_name"]
    out["aff_count"] = panel["aff_count"]
    out["aff_earned"] = panel["aff_earned"]

    patch = {}
    before = float(row["balance"] or 0)
    if abs(before - panel["balance"]) >= 0.005:
        patch["balance"] = panel["balance"]
        out["changes"].append(f"баланс {before:.2f} -> {panel['balance']:.2f} $")

    if panel["aff_code"]:
        want = aff_link(row["station"], paths.get(row["station"], DEFAULT_AFF_PATH),
                        panel["aff_code"])
        if (row["aff"] or "").strip() != want:
            patch["aff"] = want
            out["changes"].append(f"aff {row['aff'] or '—'} -> {want}")
    else:
        out["note"] = "партнёрского кода панель не дала"

    if patch and not dry:
        from backend.app.supabase_client import get_supabase

        get_supabase().table(row["table"]).update(patch).eq("id", row["id"]).execute()
    return out


def render(rows: list[dict], counts: list[dict], started: datetime, dry: bool) -> str:
    touched = [r for r in rows if r["changes"]]
    failed = [r for r in rows if r["note"]]
    stations = sorted({r["station"] for r in rows})
    head = "сверка без записи" if dry else "записано в базу"
    lines = [
        f"Сверка аккаунтов с панелями, {started:%Y-%m-%d %H:%M} ({head})",
        f"Станций {len(stations)}, записей {len(rows)}, "
        f"поправлено {len(touched)}, не прочитано {len(failed)}",
    ]
    for station in stations:
        part = [r for r in rows if r["station"] == station]
        earned = sum(r.get("aff_earned") or 0 for r in part)
        invited = sum(r.get("aff_count") or 0 for r in part)
        lines += [
            "",
            f"═══ {station}   записей {len(part)}, "
            f"приглашено {invited}, партнёрка принесла {earned:.2f} $",
            "",
        ]
        for r in part:
            mark = "  " if r["changes"] else "= "
            lines.append(f"{mark}{r['id']:>4} {r['login'] or '—':<26} "
                         + ("; ".join(r["changes"]) if r["changes"]
                            else (r["note"] or "всё сходится")))
            if r["changes"] and r["note"]:
                lines.append(f"        {r['note']}")
    if counts:
        lines += ["", "═══ счётчик аккаунтов (main_site.cnt)", ""]
        for c in counts:
            lines.append(f"  {c['station']:<22} {c['was']} -> {c['now']}")
    return "\n".join(lines) + "\n"


def main() -> int:
    started = datetime.now()
    args = sys.argv[1:]
    dry = "--dry" in args
    only_sites = {a for a in args if not a.startswith("--")}

    paths = aff_paths(only_sites)
    rows = [sync_row(r, paths, dry) for r in load_rows(only_sites)]
    for r in rows:
        if r["changes"] or r["note"]:
            print(f"{r['id']:>4} {r['station']:<18} {r['login'] or '—':<26} "
                  + ("; ".join(r["changes"]) or r["note"]), flush=True)

    counts = recount(only_sites, dry)
    for c in counts:
        print(f"     {c['station']:<18} cnt {c['was']} -> {c['now']}", flush=True)

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(render(rows, counts, started, dry), encoding="utf-8")
    print(f"отчёт: {OUT_FILE}")
    if (any(r["changes"] for r in rows) or counts) and not dry:
        notify_ui(TOPIC_CLAUDE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
