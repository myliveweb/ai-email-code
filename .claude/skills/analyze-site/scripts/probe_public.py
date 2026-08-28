"""Просев LLM-сайта без регистрации: только публичные GET.

Единственный источник правды на этом шаге — /api/status панели New API/One API.
Он отдаёт около семидесяти полей без ключа: единицы счёта, условия входа, флаги
щедрости и объявления владельца. Каталог /v1/models почти везде закрыт без ключа,
поэтому судить по нему нельзя — проверено на тринадцати живых доменах.

Объявления печатаются целиком и намеренно последними: условия акций, коды
регистрации, множители групп и запреты живут именно там, а не в описании
каталога провайдеров.

Зависимостей нет — только stdlib, чтобы скрипт работал из любого окружения:

    uv run --no-project python .claude/skills/analyze-site/scripts/probe_public.py api.42w.shop
"""
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"

# Часть станций за Cloudflare пропускает только браузерный UA: с чужим вместо
# JSON приходит HTML-заглушка. Версия — та же константа, что в
# scripts/gorouter_balance.py, менять при обновлении браузера Босса.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
TIMEOUT = 25

# Флаги входа и щедрости. Печатаются только включённые, иначе вывод не читается.
FLAGS = [
    "register_enabled", "password_register_enabled", "password_login_enabled",
    "email_verification", "turnstile_check", "pow_enabled",
    "linuxdo_oauth", "linuxdo_minimum_trust_level", "github_oauth",
    "github_minimum_account_age_days",
    "telegram_oauth", "oidc_enabled", "wechat_login",
    "register_invite_code_required", "invite_code_enabled",
    "invitation_code_enabled", "registration_code_enabled",
    "open_registration_invite_enabled",
    "checkin_enabled", "games_enabled", "self_use_mode_enabled",
    "demo_site_enabled", "docs_link", "top_up_link", "stripe_unit_price",
]
# Прозаические поля владельца. Читать обязательно, см. SKILL.md.
TEXT_KEYS = ("announcements", "api_info", "faq", "footer_html")

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def get(url: str) -> tuple[int, str]:
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_CTX) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:400]
    except Exception as e:  # DNS, таймаут, обрыв туннеля Cloudflare
        return 0, f"{type(e).__name__}: {e}"


def strip_html(value) -> str:
    s = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else str(value)
    s = re.sub(r"<[^>]+>", " ", s)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&#39;", "'"), ("&quot;", '"')):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


def probe(host: str) -> dict:
    host = host.strip().rstrip("/").replace("https://", "").replace("http://", "")
    out: dict = {"host": host}

    code, raw = get(f"https://{host}/api/status")
    try:
        d = json.loads(raw)["data"]
    except Exception:
        out["dead"] = f"HTTP {code}: {raw[:120]}"
        return out

    out["name"] = d.get("system_name") or "-"
    out["version"] = d.get("version") or "-"
    # One API узнаётся по короткому /api/status с top_up_link и без passkey_*.
    out["panel"] = "New API" if any(k.startswith("passkey") for k in d) else "One API"
    unit = d.get("quota_per_unit")
    out["unit"] = unit
    out["rate"] = d.get("usd_exchange_rate")
    out["price"] = d.get("price")
    out["flags"] = {k: d[k] for k in FLAGS if k in d and d[k] not in (False, "", None, 0)}
    out["text"] = [
        f"[{k}] {strip_html(d[k])}" for k in TEXT_KEYS
        if d.get(k) and len(strip_html(d[k])) > 4
    ]

    # Каталог моделей: почти везде закрыт, но проверить дёшево.
    mcode, mraw = get(f"https://{host}/v1/models")
    try:
        out["models"] = len(json.loads(mraw).get("data") or [])
    except Exception:
        out["models"] = f"закрыт (HTTP {mcode})"
    return out


def report(r: dict) -> None:
    print("=" * 78)
    if "dead" in r:
        print(f"{r['host']}  — панели нет: {r['dead']}")
        return
    print(f"{r['host']}  {r['name']}  [{r['panel']} {r['version']}]")
    unit = r["unit"]
    money = f"quota_per_unit={unit}"
    if unit:
        money += f" (1 $ = {unit} единиц)"
    if r["rate"] is not None:
        money += f", usd_exchange_rate={r['rate']}"
    if r["price"] is not None:
        money += f", price={r['price']}"
    print(f"  деньги: {money}")
    print(f"  флаги:  {r['flags'] or 'все выключены'}")
    print(f"  модели без ключа: {r['models']}")
    for t in r["text"]:
        print(f"  {t}")
    if not r["text"]:
        print("  объявлений нет — условия щедрости придётся смотреть изнутри панели")


def main() -> int:
    hosts = sys.argv[1:]
    if not hosts:
        print(__doc__)
        return 1
    # Публичные GET безопасны: лимит запросов к моделям они не расходуют,
    # поэтому здесь параллель уместна — в отличие от bench_model.py.
    with ThreadPoolExecutor(max_workers=min(8, len(hosts))) as ex:
        for r in ex.map(probe, hosts):
            report(r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
