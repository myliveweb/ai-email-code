"""Приёмка новых ящиков: разобрать список, проверить живость, залить в `main_email`.

Форматы входа распознаются сами, потому что поставщики отдают по-разному:
  * `email:password` или `email:password:secret` — Rambler, доступ по IMAP.
    Третье поле — ответ на секретный вопрос, в базе это `secret`.
  * `mail|pass|refresh_token|client_id` — Hotmail/Outlook, доступ по Graph.
  * JSON-массив объектов с любыми из ключей email/password/refresh_token/
    graph_refresh_token/client_id — тот же Hotmail, только выдача в JSON.

Живость проверяется тем же кодом, что и рабочий сбор: Hotmail через Graph
(`outlook_mail_checker`), Rambler через IMAP (`rambler_imap_mail_checker`).
Мёртвый ящик в базу не попадает вовсе — иначе автосбор будет тратить на него
заходы, а голова числиться свободной.

    uv run python scripts/intake_emails.py data/in_new.txt          # только оценка
    uv run python scripts/intake_emails.py data/in_new.txt --save    # залить живые
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.app.config import allow_direct_localhost  # noqa: E402

allow_direct_localhost()

from backend.app.supabase_client import get_supabase  # noqa: E402
from outlook_mail_checker import check_mailboxes_bulk as outlook_bulk  # noqa: E402
from rambler_imap_mail_checker import check_mailboxes_bulk as rambler_bulk  # noqa: E402

KEYS = {
    "email": ("email", "mail", "login", "username", "user"),
    "password": ("password", "pass", "pwd"),
    "refresh_token": ("refresh_token", "refreshtoken", "refresh"),
    "graph_refresh_token": ("graph_refresh_token", "graphrefreshtoken", "graph_refresh"),
    "client_id": ("client_id", "clientid", "client"),
    "secret": ("secret", "answer", "secret_answer"),
}


def pick(obj: dict, field: str) -> str | None:
    low = {str(k).lower().replace("-", "_"): v for k, v in obj.items()}
    for name in KEYS[field]:
        if low.get(name):
            return str(low[name]).strip()
    return None


def parse(raw: str) -> list[dict]:
    """Строки любого из трёх видов → одинаковые записи для базы."""
    out: list[dict] = []
    text = raw.strip()
    if text.startswith(("[", "{")):
        data = json.loads(text)
        for obj in data if isinstance(data, list) else [data]:
            out.append(
                {
                    "email": pick(obj, "email"),
                    "password": pick(obj, "password"),
                    "refresh_token": pick(obj, "refresh_token"),
                    "graph_refresh_token": pick(obj, "graph_refresh_token"),
                    "client_id": pick(obj, "client_id"),
                    "secret": pick(obj, "secret"),
                }
            )
        return out

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            p = [x.strip() for x in line.split("|")]
            out.append(
                {
                    "email": p[0],
                    "password": p[1] if len(p) > 1 else None,
                    "refresh_token": p[2] if len(p) > 2 else None,
                    "graph_refresh_token": None,
                    "client_id": p[3] if len(p) > 3 else None,
                    "secret": None,
                }
            )
            continue
        p = [x.strip() for x in line.split(":")]
        out.append(
            {
                "email": p[0],
                "password": p[1] if len(p) > 1 else None,
                "refresh_token": None,
                "graph_refresh_token": None,
                "client_id": None,
                "secret": p[2] if len(p) > 2 else None,
            }
        )
    return out


def check(rows: list[dict]) -> None:
    """Метит каждую запись полями `ok` и `why`. Graph-токен, прошедший проверку,
    кладётся в `graph_refresh_token`: автосбор читает именно его."""
    ram = [r for r in rows if (r["email"] or "").lower().endswith("@rambler.ru")]
    hot = [r for r in rows if r not in ram]

    if ram:
        res = rambler_bulk(
            [{"email": r["email"], "login": r["email"], "password": r["password"]} for r in ram],
            max_workers=5,
        )
        by = {v.get("email"): v for v in res}
        for r in ram:
            v = by.get(r["email"], {})
            r["ok"], r["why"] = bool(v.get("active")), v.get("reason") or ""

    for r in hot:
        r["ok"], r["why"] = False, "нет client_id или токена"
    for field in ("graph_refresh_token", "refresh_token"):
        wait = [r for r in hot if not r["ok"] and r["client_id"] and r[field]]
        if not wait:
            continue
        res = outlook_bulk(
            [{"email": r["email"], "client_id": r["client_id"], "refresh_token": r[field]}
             for r in wait],
            max_workers=5,
        )
        by = {v.get("email"): v for v in res}
        for r in wait:
            v = by.get(r["email"], {})
            if v.get("active"):
                r["ok"], r["why"] = True, f"Graph по {field}"
                r["graph_refresh_token"] = r[field]
            else:
                r["why"] = f"{field}: {v.get('reason') or 'отказ'}"


def save(rows: list[dict]) -> None:
    """Живые ящики в `main_email`. Дубли по email пропускаются: партии приходят
    с пересечениями, а перезапись стёрла бы уже проверенный токен."""
    sb = get_supabase()
    emails = [r["email"] for r in rows]
    have = {
        str(x["email"])
        for x in sb.table("main_email").select("email").in_("email", emails).execute().data
    }
    added = 0
    for r in rows:
        if r["email"] in have:
            print(f"  уже в базе: {r['email']}")
            continue
        sb.table("main_email").insert(
            {
                "email": r["email"],
                "password": r["password"],
                "client_id": r["client_id"],
                "refresh_token": r["refresh_token"],
                "graph_refresh_token": r["graph_refresh_token"],
                "secret": r["secret"],
                "active": True,
            }
        ).execute()
        added += 1
    print(f"  залито: {added}")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return
    rows = parse(Path(args[0]).read_text(encoding="utf-8"))
    rows = [r for r in rows if r["email"]]
    if not rows:
        print("в файле не нашлось ни одного ящика")
        return

    print(f"разобрано записей: {len(rows)}")
    if "--no-check" in sys.argv[1:]:
        for r in rows:
            r["ok"], r["why"] = True, "без проверки"
    else:
        check(rows)

    for r in rows:
        mark = "живой" if r["ok"] else "мёртвый"
        print(f"  {mark:8} {r['email']:40} {r['why']}")

    alive = [r for r in rows if r["ok"]]
    print(f"\nживых {len(alive)} из {len(rows)}")

    if "--save" not in sys.argv[1:]:
        print("это была только оценка, для заливки добавьте --save")
        return
    if not alive:
        print("заливать нечего")
        return
    save(alive)


if __name__ == "__main__":
    main()
