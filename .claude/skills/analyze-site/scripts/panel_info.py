"""Что видно изнутри панели по «Токену доступа» и «ID пользователя».

Босс копирует обе строки со страницы /profile. Одного токена не хватает: New API
требует ещё заголовок `New-Api-User: <panel_id>`, и с чужим id тот же токен
получает 401. Ни один эндпоинт id по токену не отдаёт — отсюда и просьба к Боссу.
One API (aihubmix) обходится одним токеном, поэтому panel_id необязателен.

Главное, за чем сюда идут: /api/user/self/groups показывает группы моделей с их
множителями, и именно здесь видна группа с множителем 0 — та форма щедрости, при
которой счёт не ведётся вовсе. Публично этого не узнать.

    uv run --no-project python .claude/skills/analyze-site/scripts/panel_info.py \\
        api.42w.shop <access_token> [panel_id] [--make-key] [--group=complimentary]

Группа у ключа важнее, чем кажется: ключ наследует множитель своей группы, а не
группы профиля. Профиль на api.42w.shop сидит в `default` с ratio 2, тогда как
весь смысл станции — `complimentary` с ratio 0. Без --group ключ создаётся
в группе профиля и тратит деньги там, где тратить не надо.
"""
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
TIMEOUT = 30

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def call(base: str, path: str, token: str, panel_id: str | None,
         body: dict | None = None) -> tuple[int, object]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    if panel_id:
        headers["New-Api-User"] = str(panel_id)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{base}{path}", headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_CTX) as r:
            raw = r.read().decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read().decode("utf-8", "replace"), e.code
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"
    try:
        return status, json.loads(raw)
    except Exception:
        return status, raw[:300]


def money(quota, unit) -> str:
    if not isinstance(quota, (int, float)):
        return str(quota)
    if not unit:
        return f"{quota} единиц (quota_per_unit неизвестен — в доллары не перевести)"
    return f"{quota} единиц = ${quota / unit:.2f}"


def main() -> int:
    args = [a for a in sys.argv[1:]
            if a != "--make-key" and not a.startswith("--group=")]
    make_key = "--make-key" in sys.argv
    group = next((a.split("=", 1)[1] for a in sys.argv[1:]
                  if a.startswith("--group=")), None)
    if len(args) < 2:
        print(__doc__)
        return 1
    host = args[0].rstrip("/").replace("https://", "").replace("http://", "")
    # Панель копирует токен с хвостовыми пробелами — это уже стоило нам 401.
    token = args[1].strip()
    panel_id = args[2].strip() if len(args) > 2 else None
    base = f"https://{host}"

    unit = None
    st, status = call(base, "/api/status", token, None)
    if isinstance(status, dict):
        unit = (status.get("data") or {}).get("quota_per_unit")
        print(f"quota_per_unit = {unit}")

    st, res = call(base, "/api/user/self", token, panel_id)
    if st != 200 or not isinstance(res, dict) or not res.get("data"):
        print(f"\n/api/user/self → HTTP {st}: {res}")
        if st == 401 and not panel_id:
            print("401 без panel_id — похоже на New API. Попросить у Босса "
                  "«ID пользователя» со страницы /profile и передать третьим аргументом.")
        if st == 403:
            print("403 у New API означает insufficient_user_quota — деньги кончились, "
                  "а не проблема с токеном.")
        return 2

    u = res["data"]
    print(f"\nпрофиль: id={u.get('id')} login={u.get('username')!r} "
          f"display_name={u.get('display_name')!r} group={u.get('group')!r}")
    print(f"баланс:  {money(u.get('quota'), unit)}")
    print(f"истрачено: {money(u.get('used_quota'), unit)}")
    print(f"запросов сделано: {u.get('request_count')}   aff_code={u.get('aff_code')!r}")
    print("Число запросов запомнить: вердикт о реальном лимите строится на разнице "
          "request_count до и после прогона моделей.")

    st, res = call(base, "/api/user/self/groups", token, panel_id)
    print(f"\nгруппы моделей (HTTP {st}):")
    data = res.get("data") if isinstance(res, dict) else None
    if isinstance(data, dict):
        for name, info in data.items():
            ratio = info.get("ratio") if isinstance(info, dict) else info
            desc = info.get("desc", "") if isinstance(info, dict) else ""
            flag = "  ← множитель 0, баланс не списывается" if ratio == 0 else ""
            print(f"  {name:20} ratio={ratio}  {desc}{flag}")
    else:
        print(f"  {res}")

    print("\nпартнёрка:")
    for path in ("/api/user/aff", "/api/user/invite", "/api/user/aff_history"):
        st, res = call(base, path, token, panel_id)
        short = json.dumps(res, ensure_ascii=False)[:160] if isinstance(res, (dict, list)) else str(res)[:160]
        print(f"  {path:26} HTTP {st}  {short}")
    print("  404 на всех трёх означает, что партнёрки нет — это не ошибка запроса.")

    st, res = call(base, "/api/token/?p=0&size=100", token, panel_id)
    keys = res.get("data") if isinstance(res, dict) else None
    if isinstance(keys, dict):
        keys = keys.get("items") or keys.get("records")
    if isinstance(keys, list):
        print(f"\nключей уже создано: {len(keys)}")
        for k in keys[:5]:
            print(f"  {k.get('name')!r} {k.get('key')} status={k.get('status')}")
    else:
        print(f"\n/api/token/ → HTTP {st}: {res}")

    if make_key:
        body = {"name": f"analyze-{int(time.time())}", "remain_quota": 0,
                "expired_time": -1, "unlimited_quota": True,
                "model_limits_enabled": False, "model_limits": "",
                "allow_ips": "", "group": group or u.get("group") or "default"}
        st, res = call(base, "/api/token/", token, panel_id, body=body)
        full = (res.get("data") or {}).get("key") if isinstance(res, dict) else None
        print(f"\nсоздание ключа в группе {body['group']!r} → HTTP {st}")
        if full:
            print(f"  sk-{full}" if not str(full).startswith("sk-") else f"  {full}")
            print("  Полный ключ отдаётся единственный раз — сохранить сразу.")
        else:
            print(f"  {res}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
