"""Ежедневный чек-ин на станциях New API: панель начисляет 5-10 $ за вход в кабинет.

Кнопка «Войдите сейчас» живёт на /profile, в секции «Ежедневный вход»,
а POST /api/user/checkin закрыт Cloudflare Turnstile. Поэтому единственный путь —
живой браузер: вход через GitHub OAuth в изолированной сессии на каждый аккаунт.

Имя файла историческое: gorouter.app давно не единственная станция с чек-ином.
Список станций не зашит — их находит `load_stations()` по базе и по ответу панели,
поэтому появится ещё одна с тем же чек-ином, и скрипт подхватит её сам.

Рассчитан на крон и работает без человека: аккаунты берутся прямо из Supabase
(backend на :4000 может быть погашен), путь к agent-browser абсолютный, прокси
браузеру передаётся из PROXY_URL в .env, ошибка на одном аккаунте не роняет
остальные, код возврата всегда 0 — иначе крон завалит почту письмами.

    uv run python scripts/gorouter_checkin.py [id аккаунтов] [домен станции]
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import find_dotenv, load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.app.config import allow_direct_localhost  # noqa: E402
from scripts.single import hold_lock  # noqa: E402

load_dotenv(find_dotenv())
allow_direct_localhost()

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
QUOTA_PER_UNIT = 500000
GITHUB_VERIFY_SUBJECT = "[GitHub] Please verify your device"

# Прогон одного аккаунта — это живой Chromium, OAuth и минута-две времени. Ниже
# этого порога награда не покрывает возню: у ai.mrcwoods.com чек-ин даёт 1000-2000
# quota, то есть меньше половины цента. Станция с таким подарком пропускается,
# но остаётся в отчёте строкой — чтобы решение было видно, а не выглядело потерей.
MIN_REWARD = 0.50

# Станции режут `/api/oauth/state` по IP-адресу: около девяти обращений, дальше 429
# с `Retry-After` ~1000 секунд (замер 27.08 — gorouter RA=1011, tabitoken RA=961).
# Пока окно закрыто, кнопка «Продолжить с GitHub» просто не уводит на GitHub, и без
# паузы прогон впустую жжёт аккаунты: 27.08 из 545 отметились 128 — ровно столько,
# сколько выпадает при девяти входах на семнадцатиминутное окно.
OAUTH_LIMIT = "окно OAuth закрыто (429 по IP)"
RATE_WAIT = 1020
RATE_STRIKES = 2

OUT_FILE = ROOT / "log" / "gorouter_checkin.txt"
STATE_FILE = ROOT / "log" / "gorouter_checkin_state.json"

# PATH у крона беден, а agent-browser стоит через linuxbrew.
AB_BIN = shutil.which("agent-browser") or "/home/linuxbrew/.linuxbrew/bin/agent-browser"
# Все удачные прогоны шли через внешний прокси из окружения Босса, и крон должен
# ходить тем же путём: с домашнего адреса Turnstile может повести себя иначе.
# Берём PROXY_URL из .env, а не http_proxy: тот в .env намеренно не задан, чтобы
# не влиять на другие проекты общей базы.
PROXY = (os.getenv("BROWSER_PROXY") or os.getenv("PROXY_URL") or "").strip().strip('"')
# Chromium идёт headless, но X-сессия Босса при живом рабочем столе доступна.
os.environ.setdefault("DISPLAY", ":0")


def ab(session: str, *args: str, timeout: int = 120, proxy: str | None = None) -> str:
    """Вызов agent-browser в конкретной сессии.

    `proxy` нужен сбору аккаунтов: там на каждый аккаунт свой адрес из пула,
    чтобы GitHub не видел десятки разных логинов с одного IP. Не передан —
    берётся общий `PROXY` из окружения, как было.
    """
    cmd = [AB_BIN, "--session", session]
    via = PROXY if proxy is None else proxy
    if via:
        cmd += ["--proxy", via, "--proxy-bypass", "127.0.0.1,localhost"]
    cmd += args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"!! agent-browser {args[0] if args else ''} молчал {timeout} с"
    return (p.stdout or "") + (p.stderr or "")


def ref_for(snapshot: str, *needles: str, role: str = "") -> str | None:
    """Найти @eN у первой строки снапшота, содержащей любую из подстрок.

    Роль обязательна для кнопок и полей: у обёрточного div в снапшоте лежит
    склеенный текст всей страницы, и он матчится на любой ярлык раньше элемента.
    """
    for line in snapshot.splitlines():
        low = line.lower()
        if role and f"- {role} " not in low:
            continue
        if any(n.lower() in low for n in needles):
            m = re.search(r"\[(?:[^\]]*?)ref=(e\d+)", line)
            if m:
                return m.group(1)
    return None


def panel_client(base: str, token: str, panel_id: int) -> httpx.Client:
    return httpx.Client(
        headers={
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Authorization": f"Bearer {token}",
            "New-Api-User": str(panel_id),
            "Referer": f"{base}/dashboard/overview",
        },
        timeout=45,
        trust_env=True,
    )


def checkin_state(base: str, token: str, panel_id: int) -> dict:
    """Состояние аккаунта. Внешний прокси иногда рвёт соединение, отсюда повторы."""
    for attempt in range(4):
        try:
            with panel_client(base, token, panel_id) as c:
                self_ = c.get(f"{base}/api/user/self").json()["data"]
                stats = c.get(f"{base}/api/user/checkin").json()["data"]["stats"]
            break
        except (httpx.HTTPError, KeyError, TypeError):
            if attempt == 3:
                raise
            time.sleep(5)
    return {
        "quota": self_["quota"],
        "usd": round(self_["quota"] / QUOTA_PER_UNIT, 2),
        "display_name": self_["display_name"],
        "checked_in_today": stats["checked_in_today"],
        "total_checkins": stats["total_checkins"],
        "records": stats["records"],
    }


def checkin_offer(base: str, token: str, panel_id: int) -> dict:
    """Есть ли на станции чек-ин и сколько он даёт.

    Спрашиваем саму панель, а не метку в базе: `checkin_enabled` в /api/status и
    вилка `min_quota`/`max_quota` в /api/user/checkin — это то, что станция думает
    о себе прямо сейчас, и новую станцию не придётся размечать руками.
    """
    with panel_client(base, token, panel_id) as c:
        status = (c.get(f"{base}/api/status").json().get("data") or {})
        unit = float(status.get("quota_per_unit") or QUOTA_PER_UNIT)
        offer = (c.get(f"{base}/api/user/checkin").json().get("data") or {})
    enabled = bool(status.get("checkin_enabled")) and bool(offer.get("enabled"))
    return {
        "enabled": enabled,
        "unit": unit,
        "min_usd": float(offer.get("min_quota") or 0) / unit,
        "max_usd": float(offer.get("max_quota") or 0) / unit,
    }


def load_stations() -> list[dict]:
    """Станции с чек-ином и их аккаунты — прямо из базы.

    Под кроном backend на :4000 может быть погашен, поэтому Supabase напрямую.
    Кандидат — сайт, у которого есть аккаунты с парой доступов к панели и с
    привязкой к GitHub (иначе в кабинет не войти), и который не помечен
    `meta.blocked`. Годность решает сама панель, см. `checkin_offer`.
    """
    from backend.app.supabase_client import get_supabase

    sb = get_supabase()
    sites = sb.table("main_site").select("id, name, meta").order("id").execute().data or []

    stations = []
    for site in sites:
        if (site.get("meta") or {}).get("blocked"):
            continue
        res = (
            sb.table("main_site_account")
            .select("id, login, access_token, panel_id, github_id")
            .eq("site_id", site["id"])
            .not_.is_("access_token", "null")
            .not_.is_("panel_id", "null")
            .not_.is_("github_id", "null")
            .order("id")
            .execute()
        )
        if not res.data:
            continue

        gh_ids = [a["github_id"] for a in res.data]
        gh = (
            sb.table("main_github")
            .select("id, login, pass_github, email")
            .in_("id", gh_ids)
            .execute()
        )
        gh_rows = {g["id"]: g for g in gh.data}

        base = f"https://{site['name']}"
        accounts = []
        for a in res.data:
            # панель копирует токен в буфер с хвостовыми пробелами
            token = (a["access_token"] or "").strip()
            g = gh_rows.get(a["github_id"]) or {}
            if not token or not g.get("login") or not g.get("pass_github"):
                continue
            accounts.append(
                {
                    "acc_id": a["id"],
                    "login": a["login"],
                    "panel_id": a["panel_id"],
                    "token": token,
                    "gh_login": g["login"],
                    "gh_pass": g["pass_github"],
                    "gh_email": g.get("email"),
                    "station": site["name"],
                    "base": base,
                }
            )
        if accounts:
            stations.append({"name": site["name"], "base": base, "accounts": accounts})
    return stations


def fetch_device_code(email: str | None, tries: int = 6, since: datetime | None = None) -> str | None:
    """Код подтверждения устройства GitHub из ящика Outlook.

    Под кроном ходить в backend нельзя, поэтому Graph API читается напрямую.

    `tries` — сколько раз перечитать ящик: GitHub рапортует об отправке сразу,
    а письмо у него доходит и через пять минут, так что сбору аккаунтов нужно
    терпение куда больше шести попыток. `since` отсекает письма прошлых заходов:
    у повторно взятого аккаунта в ящике лежит вчерашний код, и без отсечки он
    подставился бы вместо свежего.
    """
    if not email:
        return None
    from backend.app.main import _extract_verification_code
    from backend.app.supabase_client import get_supabase
    from outlook_mail_checker import get_mail_by_subject

    row = get_supabase().table("main_email").select("*").eq("email", email).limit(1).execute()
    if not row.data:
        return None
    client_id = row.data[0].get("client_id")
    refresh_token = row.data[0].get("graph_refresh_token")
    if not client_id or not refresh_token:
        return None

    for _ in range(tries):
        time.sleep(6)
        try:
            mails = get_mail_by_subject(
                client_id=client_id,
                refresh_token=refresh_token,
                subject=GITHUB_VERIFY_SUBJECT,
                match_mode="exact",
                date_from=since,
            )
        except Exception:
            continue
        code = _extract_verification_code(mails, 6, None, "digits")
        if code:
            return code
    return None


def open_profile(session: str, base: str, log: list[str]) -> bool:
    """Открыть /profile — там живёт «Ежедневный вход».

    Прямой переход иногда отбрасывает на обзор, тогда идём через меню аватара.
    """
    ab(session, "open", f"{base}/profile")
    ab(session, "wait", "--load", "networkidle")
    time.sleep(4)
    if "/profile" in ab(session, "eval", "location.href"):
        return True

    snap = ab(session, "snapshot", "-i")
    m = re.search(r'button "[A-ZА-Яa-zа-я]" \[expanded=false, ref=(e\d+)', snap)
    if not m:
        log.append("не нашёл меню аватара")
        return False
    ab(session, "click", f"@{m.group(1)}")
    time.sleep(2)
    item = ref_for(ab(session, "snapshot", "-i"), "Профиль", "Profile", role="menuitem")
    if not item:
        log.append("не нашёл пункт «Профиль» в меню")
        return False
    ab(session, "click", f"@{item}")
    ab(session, "wait", "--load", "networkidle")
    time.sleep(4)
    return "/profile" in ab(session, "eval", "location.href")


def claim_checkin(session: str, acc: dict, log: list[str]) -> bool:
    """Нажать «Войдите сейчас» и дождаться награды, не закрывая браузер.

    Turnstile докручивается сам за 5-10 секунд; красный тост «пройдите проверку» —
    косметика, чекбокс трогать не нужно. Поэтому просто опрашиваем баланс.
    """
    if not open_profile(session, acc["base"], log):
        log.append("не открылся /profile")
        return False

    snap = ab(session, "snapshot", "-i")
    btn = ref_for(snap, "Войдите сейчас", "Check-in", "签到", role="button")
    if not btn:
        if "Зарегистрирован" in snap or "已签到" in snap:
            return True
        log.append("не нашёл кнопку «Войдите сейчас» на /profile")
        return False
    ab(session, "click", f"@{btn}")

    for i in range(8):
        time.sleep(8)
        if checkin_state(acc["base"], acc["token"], acc["panel_id"])["checked_in_today"]:
            return True
        if i == 4:
            snap = ab(session, "snapshot", "-i")
            cb = ref_for(snap, "человек", "human", role="checkbox")
            if cb:
                log.append("Turnstile попросил чекбокс, нажал")
                ab(session, "click", f"@{cb}")
    # Начисление приходит и через несколько минут после клика, уже после закрытия
    # браузера — проверено на восьми аккаунтах 2026-08-22. Клик сделан, это не провал.
    log.append("клик сделан, награда придёт с задержкой")
    return True


def browser_login(acc: dict, log: list[str]) -> bool:
    """Вход в панель через GitHub OAuth, затем чек-ин на /profile."""
    session = f"ck{acc['acc_id']}-{int(time.time())}"
    base, host = acc["base"], acc["station"]
    try:
        ab(session, "open", f"{base}/sign-in")
        ab(session, "wait", "--load", "networkidle")
        time.sleep(2)
        snap = ab(session, "snapshot", "-i")
        ref = ref_for(snap, "Продолжить с GitHub", "Continue with GitHub", role="button")
        if not ref:
            log.append("не нашёл кнопку GitHub на /sign-in")
            return False
        ab(session, "click", f"@{ref}")
        ab(session, "wait", "--load", "networkidle")
        time.sleep(3)

        oauth_tries = 0
        for _ in range(24):
            url = ab(session, "eval", "location.href")
            if f"{host}/dashboard" in url or f"{host}/profile" in url:
                return claim_checkin(session, acc, log)
            snap = ab(session, "snapshot", "-i")
            # `/api/oauth/state` отдаёт 500 примерно на каждой третьей попытке, и сбор
            # аккаунтов живёт ровно на повторах: `start_oauth` в harvest_accounts.py
            # кликает до восьми раз с перезагрузкой и проходит. Провал ловим по URL,
            # а не по тосту «Не удалось начать вход»: тост гаснет за пару секунд и
            # в снимок почти никогда не попадает — в прогоне 27.08 из 329 потерянных
            # аккаунтов его не увидели ни разу.
            stuck = f"{host}/sign-in" in url and bool(
                ref_for(snap, "Продолжить с GitHub", "Continue with GitHub", role="button")
            )
            if stuck:
                oauth_tries += 1
                # Долбить бессмысленно: станция режет `/api/oauth/state` по IP —
                # около девяти обращений, дальше 429 с `Retry-After` ~1000 секунд
                # (замер 27.08: gorouter 9 запросов до отказа, RA=1011; tabitoken
                # RA=961). Один повтор на случай честной осечки, дальше выходим
                # и ждём остывания снаружи, целым прогоном.
                if oauth_tries > 1:
                    log.append(OAUTH_LIMIT)
                    return False
                ab(session, "reload")
                ab(session, "wait", "--load", "networkidle")
                time.sleep(2)
                again = ref_for(
                    ab(session, "snapshot", "-i"),
                    "Продолжить с GitHub",
                    "Continue with GitHub",
                    role="button",
                )
                if again:
                    ab(session, "click", f"@{again}")
                    ab(session, "wait", "--load", "networkidle")
                    time.sleep(3)
                continue

            u = ref_for(snap, "Username or email address", role="textbox")
            p = ref_for(snap, '"Password"', role="textbox")
            b = ref_for(snap, '"Sign in"', role="button")
            if u and p and b:
                ab(session, "fill", f"@{u}", acc["gh_login"])
                ab(session, "fill", f"@{p}", acc["gh_pass"])
                ab(session, "click", f"@{b}")
                ab(session, "wait", "--load", "networkidle")
                time.sleep(5)
                continue

            code_ref = ref_for(snap, "verification code", "код подтверждения", role="textbox")
            if code_ref or "device verification" in snap.lower():
                # Письмо GitHub доходит и через минуту: десять заходов по шесть
                # секунд вместо шести. Указание Босса 27.08 — «там на время идёт,
                # там даже целая минута нужна».
                code = fetch_device_code(acc["gh_email"], tries=10)
                if not code:
                    log.append("код подтверждения устройства не пришёл")
                    return False
                log.append(f"подтверждение устройства, код {code}")
                if code_ref:
                    ab(session, "fill", f"@{code_ref}", code)
                else:
                    ab(session, "keyboard", "type", code)
                ab(session, "wait", "--load", "networkidle")
                time.sleep(4)
                continue

            auth_ref = ref_for(snap, "Authorize", "Авторизовать", role="button")
            if auth_ref:
                ab(session, "click", f"@{auth_ref}")
                ab(session, "wait", "--load", "networkidle")
                time.sleep(4)
                continue

            time.sleep(4)

        text = ab(session, "eval", "document.body.innerText.slice(0,400)")
        log.append(f"застряли на {url.strip()}: {text.strip()[:300]}")
        return False
    finally:
        ab(session, "close", timeout=60)


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    os.chmod(path, 0o644)


def run_account(acc: dict) -> dict:
    """Один аккаунт. Исключение здесь не должно уносить остальные восемнадцать."""
    log: list[str] = []
    head = {"acc_id": acc["acc_id"], "login": acc["login"], "station": acc["station"]}
    try:
        before = checkin_state(acc["base"], acc["token"], acc["panel_id"])
    except Exception as exc:
        return {**head, "status": "ОШИБКА",
                "log": [f"баланс не читается: {exc}"], "gained": 0.0}

    if before["checked_in_today"]:
        return {**head, "status": "УЖЕ",
                "before": before["usd"], "after": before["usd"], "gained": 0.0, "log": log}

    try:
        ok = browser_login(acc, log)
    except Exception as exc:
        ok = False
        log.append(f"сбой браузера: {exc}")
    try:
        after = checkin_state(acc["base"], acc["token"], acc["panel_id"])
    except Exception as exc:
        log.append(f"итоговый баланс не прочитан: {exc}")
        after = before

    if after["checked_in_today"]:
        status = "OK"
    elif ok:
        status = "КЛИК"
    else:
        status = "НЕ ОТМЕЧЕН"
    return {
        **head,
        "status": status,
        "before": before["usd"],
        "after": after["usd"],
        "gained": round((after["quota"] - before["quota"]) / QUOTA_PER_UNIT, 2),
        "log": log,
    }


def render(rows: list[dict], skipped: list[str], started: datetime) -> str:
    done = [r for r in rows if r["status"] in ("OK", "УЖЕ")]
    gained = round(sum(r.get("gained", 0.0) for r in rows), 2)
    total = round(sum(r.get("after", 0.0) for r in rows), 2)
    stations = sorted({r["station"] for r in rows})
    lines = [
        f"Чек-ин станций, {started:%Y-%m-%d %H:%M} — {datetime.now():%H:%M}",
        f"Станций {len(stations)}, отмечено {len(done)} из {len(rows)}, "
        f"начислено +{gained} $, суммарный баланс {total} $",
    ]
    for note in skipped:
        lines.append(f"пропущено: {note}")

    for station in stations:
        part = [r for r in rows if r["station"] == station]
        st_gained = round(sum(r.get("gained", 0.0) for r in part), 2)
        st_total = round(sum(r.get("after", 0.0) for r in part), 2)
        st_done = [r for r in part if r["status"] in ("OK", "УЖЕ")]
        lines += [
            "",
            f"═══ {station}   отмечено {len(st_done)} из {len(part)}, "
            f"+{st_gained} $, баланс {st_total} $",
            "",
        ]
        for r in part:
            line = f"{r['acc_id']:>3} {r['login']:<24} {r['status']:<12}"
            if "after" in r:
                line += f" {r['before']:>8.2f} -> {r['after']:>8.2f} $ (+{r.get('gained', 0)})"
            if r["log"]:
                line += " | " + "; ".join(r["log"])
            lines.append(line)
    return "\n".join(lines) + "\n"


def main() -> int:
    started = datetime.now()
    args = set(sys.argv[1:])
    only_ids = {a for a in args if a.isdigit()}
    only_sites = args - only_ids
    try:
        stations = load_stations()
    except Exception as exc:
        write_atomic(OUT_FILE, f"{started:%Y-%m-%d %H:%M} аккаунты не прочитаны: {exc}\n")
        print(f"аккаунты не прочитаны: {exc}")
        return 0
    if only_sites:
        stations = [s for s in stations if s["name"] in only_sites]

    rows: list[dict] = []
    accounts: list[dict] = []
    skipped: list[str] = []
    strikes = 0
    retry: list[int] = []
    for station in stations:
        picked = station["accounts"]
        if only_ids:
            picked = [a for a in picked if str(a["acc_id"]) in only_ids]
        if not picked:
            continue
        # Годность станции спрашиваем один раз на станцию, первым же её аккаунтом:
        # ответ общий, а лишний запрос на каждый аккаунт ничего не добавит.
        probe = picked[0]
        try:
            offer = checkin_offer(station["base"], probe["token"], probe["panel_id"])
        except Exception as exc:
            skipped.append(f"{station['name']} — панель не ответила про чек-ин: {exc}")
            continue
        if not offer["enabled"]:
            skipped.append(f"{station['name']} — чек-ина нет")
            continue
        if offer["max_usd"] < MIN_REWARD:
            skipped.append(
                f"{station['name']} — чек-ин даёт до {offer['max_usd']:.4f} $, "
                f"меньше порога {MIN_REWARD} $"
            )
            continue
        print(f"═══ {station['name']}: {len(picked)} акк., "
              f"чек-ин {offer['min_usd']:.2f}-{offer['max_usd']:.2f} $", flush=True)
        accounts += picked

        for acc in picked:
            row = run_account(acc)
            rows.append(row)
            print(f"{row['acc_id']} {row['login']}: {row['status']}"
                  + (f" {row['before']} -> {row['after']} $ (+{row['gained']})" if "after" in row else "")
                  + (f" | {'; '.join(row['log'])}" if row["log"] else ""), flush=True)
            # Два закрытых окна подряд — это не аккаунты виноваты, а IP в отказе.
            # Ждём остывания и возвращаем потерянные в конец очереди: без этого
            # прогон проходит всю станцию, отметив каждого девятого.
            if OAUTH_LIMIT in row["log"]:
                strikes += 1
                retry.append(row["acc_id"])
                if strikes >= RATE_STRIKES:
                    strikes = 0
                    print(f"··· окно OAuth закрыто, ждём {RATE_WAIT // 60} мин", flush=True)
                    time.sleep(RATE_WAIT)
            else:
                strikes = 0

        # Второй заход по тем, кого срезало окно: к этому времени оно уже открыто.
        again = [a for a in picked if a["acc_id"] in set(retry)]
        retry = []
        for acc in again:
            row = run_account(acc)
            rows = [r for r in rows if r["acc_id"] != acc["acc_id"]]
            row["log"].append("второй заход")
            rows.append(row)
            print(f"{row['acc_id']} {row['login']}: {row['status']} (повтор)"
                  + (f" {row['before']} -> {row['after']} $ (+{row['gained']})" if "after" in row else "")
                  + (f" | {'; '.join(row['log'])}" if row["log"] else ""), flush=True)
            if OAUTH_LIMIT in row["log"]:
                strikes += 1
                if strikes >= RATE_STRIKES:
                    strikes = 0
                    print(f"··· окно OAuth закрыто, ждём {RATE_WAIT // 60} мин", flush=True)
                    time.sleep(RATE_WAIT)
            else:
                strikes = 0

    # Начисление приходит с задержкой в минуты: «КЛИК» почти всегда оказывается
    # успехом, если пересмотреть его позже. Один повторный опрос дешевле, чем
    # ложная тревога в отчёте.
    late = [r for r in rows if r["status"] == "КЛИК"]
    if late:
        time.sleep(120)
        by_id = {a["acc_id"]: a for a in accounts}
        for r in late:
            acc = by_id[r["acc_id"]]
            try:
                s = checkin_state(acc["base"], acc["token"], acc["panel_id"])
            except Exception:
                continue
            if s["checked_in_today"]:
                r["status"] = "OK"
                r["after"] = s["usd"]
                r["gained"] = round(r["after"] - r["before"], 2)
                r["log"].append("награда пришла с задержкой")

    ab("cleanup", "close", "--all", timeout=90)
    write_atomic(OUT_FILE, render(rows, skipped, started))
    write_atomic(STATE_FILE, json.dumps(
        {"checked": f"{started:%Y-%m-%d %H:%M}", "skipped": skipped, "rows": rows},
        ensure_ascii=False, indent=1))
    print(f"отчёт: {OUT_FILE}")
    return 0


if __name__ == "__main__":
    # Прогон одного аккаунта — живой браузер; два экземпляра сразу и в базу пишут
    # вдвоём, и Chromium плодят парами.
    lock = hold_lock("gorouter_checkin")
    if lock is None:
        sys.exit(0)
    try:
        sys.exit(main())
    except Exception as exc:  # крон не должен получать письмо об ошибке
        print(f"прогон упал целиком: {exc}")
        sys.exit(0)
