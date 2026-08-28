#!/usr/bin/env python3
"""Проба: взять Turnstile-токен в браузере и отметиться чек-ином по Bearer.

Смысл в том, чтобы обойтись без входа в панель. Вход через GitHub OAuth на
tabitoken сейчас не начинается вовсе (страница пишет «Не удалось начать вход
через GitHub», четыре попытки подряд), и из-за этого 195 аккаунтов не отмечены.
Но `POST /api/user/checkin` авторизуется токеном доступа, а не cookie-сессией,
и просит только Turnstile-токен — его виджет выдаёт на любой странице домена.
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.app.config import allow_direct_localhost  # noqa: E402

allow_direct_localhost()

from scripts.gorouter_checkin import ab, panel_client  # noqa: E402

DOMAIN = sys.argv[1] if len(sys.argv) > 1 else "tabitoken.com"
BASE = f"https://{DOMAIN}"

RENDER = """
(() => {
  window.__tk = null; window.__tkerr = null;
  const go = () => {
    let box = document.getElementById('tkbox');
    if (!box) { box = document.createElement('div'); box.id = 'tkbox'; document.body.appendChild(box); }
    turnstile.render('#tkbox', {
      sitekey: '%s',
      callback: t => { window.__tk = t; },
      'error-callback': e => { window.__tkerr = String(e); },
    });
  };
  if (window.turnstile) { go(); return 'виджет уже был'; }
  const s = document.createElement('script');
  s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
  s.onload = go;
  s.onerror = () => { window.__tkerr = 'скрипт не загрузился'; };
  document.head.appendChild(s);
  return 'скрипт добавлен';
})()
"""


def main() -> int:
    from backend.app.supabase_client import get_supabase

    sb = get_supabase()
    site = sb.table("main_site").select("id").eq("name", DOMAIN).limit(1).execute().data[0]
    acc = (
        sb.table("main_site_account")
        .select("id,login,access_token,panel_id")
        .eq("site_id", site["id"])
        .not_.is_("access_token", "null")
        .limit(1)
        .execute()
    ).data[0]

    with panel_client(BASE, acc["access_token"], acc["panel_id"]) as c:
        status = c.get(f"{BASE}/api/status").json()["data"]
        sitekey = status.get("turnstile_site_key")
        state = c.get(f"{BASE}/api/user/checkin").json()["data"]["stats"]
    print(f"{acc['id']} {acc['login']}: сегодня отмечен = {state['checked_in_today']}, sitekey {sitekey}")

    session = f"tk{int(time.time())}"
    try:
        ab(session, "open", f"{BASE}/sign-in")
        ab(session, "wait", "--load", "networkidle")
        time.sleep(2)
        print("рендер:", ab(session, "eval", RENDER % sitekey).strip()[:120])
        token = ""
        for _ in range(20):
            time.sleep(3)
            got = ab(session, "eval", "window.__tk || window.__tkerr || ''").strip().strip('"')
            if got and got not in ("null", ""):
                token = got
                break
        print("токен:", (token[:40] + "…") if len(token) > 40 else token or "не получен")
        if not token or len(token) < 20:
            return 1
    finally:
        ab(session, "close", timeout=60)

    with panel_client(BASE, acc["access_token"], acc["panel_id"]) as c:
        for where in ("query", "header", "body"):
            if where == "query":
                r = c.post(f"{BASE}/api/user/checkin", params={"turnstile": token})
            elif where == "header":
                r = c.post(f"{BASE}/api/user/checkin", headers={"turnstile": token})
            else:
                r = c.post(f"{BASE}/api/user/checkin", json={"turnstile": token})
            print(where, r.status_code, r.text[:200])
            if r.status_code == 200 and '"success":true' in r.text:
                return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
