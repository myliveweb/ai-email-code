"""Часовой над станциями, на которые можно пересадить Claude Code.

Обе интересные станции лежат по разным причинам, и обе могут подняться без предупреждения:
gorouter.app (0.30 $/запрос, 1682 $ на 19 аккаунтах) отдаёт 522 — умер origin за Cloudflare;
seekai.cc (0.225 $/запрос на claude-opus-4-8, 6372 $) отвечает 503 system_memory_overloaded —
сервер владельца упёрся в память и не пускает генерацию вовсе. Разница между ними в 3.5 раза
против нашего рабочего tabitoken (0.80 $), поэтому дежурить у них стоит.

Проверки выбраны так, чтобы ничего не стоили:

    gorouter — /api/user/self с парой access_token + New-Api-User. Живой origin отдаёт JSON,
               мёртвый — 522 или таймаут. Денег не списывает.
    seekai   — /v1/messages минимальным запросом. 503 по перегрузке приходит за 0.7 с до
               обращения к модели, то есть бесплатно; если же станция ожила, спишется один
               запрос, и это ровно та новость, за которой мы дежурим.

Печатает строку состояния в log/station_watch.txt и отдельную пометку ИЗМЕНЕНИЕ, когда
состояние станции отличается от прошлого прогона (log/station_watch_state.json). Смысл именно
в пометке: ровный поток «лежит» читать никто не будет, а перемену увидеть надо сразу.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.app.config import allow_direct_localhost  # noqa: E402

allow_direct_localhost()

from backend.app.supabase_client import get_supabase  # noqa: E402
from scripts.single import hold_lock  # noqa: E402

REPORT = ROOT / "log" / "station_watch.txt"
STATE = ROOT / "log" / "station_watch_state.json"
TIMEOUT = 45
# Cloudflare у этих станций пропускает только браузерный User-Agent; версия — константа,
# её меняют при обновлении Chrome Босса.
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/139.0.0.0 Safari/537.36")


def probe_newapi_panel(domain: str) -> str:
    """Живость origin по панели: у первой записи с парой access_token + panel_id."""
    sb = get_supabase()
    site = sb.table("main_site").select("id").eq("name", domain).execute()
    if not site.data:
        return "нет в main_site"
    rows = (
        sb.table("main_site_account")
        .select("id, access_token, panel_id")
        .eq("site_id", site.data[0]["id"])
        .not_.is_("access_token", "null")
        .not_.is_("panel_id", "null")
        .limit(1)
        .execute()
    )
    if not rows.data:
        return "нет пары access_token/panel_id"
    row = rows.data[0]
    try:
        r = requests.get(
            f"https://{domain}/api/user/self",
            headers={
                "Authorization": f"Bearer {(row['access_token'] or '').strip()}",
                "New-Api-User": str(row["panel_id"]),
                "User-Agent": UA,
            },
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        return f"лежит ({type(e).__name__})"
    if r.status_code != 200:
        return f"лежит (HTTP {r.status_code})"
    quota = ((r.json().get("data") or {}).get("quota") or 0) / 500000
    return f"ЖИВА (баланс {quota:.2f} $)"


def probe_messages(domain: str, model: str) -> str:
    """Живость шлюза по Anthropic-эндпоинту — тому самому, куда ходит Claude Code."""
    sb = get_supabase()
    site = sb.table("main_site").select("id, meta").eq("name", domain).execute()
    if not site.data:
        return "нет в main_site"
    meta = site.data[0]["meta"] or {}
    base = meta.get("endpoints_openai") or f"https://{domain}/v1"
    if isinstance(base, list):
        base = base[0]
    rows = (
        sb.table("main_site_account")
        .select("token")
        .eq("site_id", site.data[0]["id"])
        .order("balance", desc=True)
        .limit(5)
        .execute()
    )
    token = next(((r["token"] or "").strip() for r in rows.data if (r["token"] or "").strip()), "")
    if not token:
        return "нет token"
    try:
        r = requests.post(
            f"{base.rstrip('/')}/messages",
            headers={
                "x-api-key": token,
                "authorization": f"Bearer {token}",
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
                "User-Agent": UA,
            },
            json={"model": model, "max_tokens": 8,
                  "messages": [{"role": "user", "content": "скажи ОК"}]},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        return f"лежит ({type(e).__name__})"
    if r.status_code == 503 and "overload" in r.text:
        return "лежит (503 перегрузка памяти)"
    if r.status_code != 200:
        return f"лежит (HTTP {r.status_code})"
    return "ЖИВА (модель ответила)"


def main() -> None:
    checks = {
        "gorouter.app": lambda: probe_newapi_panel("gorouter.app"),
        "seekai.cc": lambda: probe_messages("seekai.cc", "claude-opus-4-8"),
    }
    old = json.loads(STATE.read_text()) if STATE.exists() else {}
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"=== {now}"]
    new = {}
    for name, check in checks.items():
        state = check()
        new[name] = state
        lines.append(f"{name:16} {state}")
        was = old.get(name)
        if was and was != state:
            lines.append(f"  >>> ИЗМЕНЕНИЕ: было «{was}»")
        if state.startswith("ЖИВА") and (not was or not was.startswith("ЖИВА")):
            lines.append(f"  >>> {name} ПОДНЯЛАСЬ — пора пересаживать Claude Code")
    text = "\n".join(lines) + "\n"
    REPORT.write_text(text)
    STATE.write_text(json.dumps(new, ensure_ascii=False, indent=2))
    print(text, end="")


if __name__ == "__main__":
    # Часовой ходит на лежачие станции — там таймауты, прогон может пережить свой час.
    if hold_lock("station_watch") is None:
        sys.exit(0)
    main()
