"""
Балансы станций, годных для Claude Code, и ротация его ключа.

Станции берутся из базы: это все записи main_site, у которых в
`meta.endpoints_anthropic` лежит Anthropic-совместимый адрес. Только туда
Claude Code и может ходить, поэтому список станций не зашит в код —
появится четвёртая, скрипт подхватит её сам.

Аккаунты собираются из обеих таблиц (main_site_account и
main_site_account_custom) — годится любая запись с заполненным `token`,
это и есть ключ для Claude Code. Свежий остаток читается там, где панель
New API и заполнена пара `access_token` + `panel_id` («Токен доступа» и «ID
пользователя» со страницы /profile); таким записям обновляется `balance`
в базе. Станция со своей панелью (`meta.panel_api`, у vyceai это "vyce")
читается её же сессией из браузера — Босс кладёт её в `access_token`, и тогда
панель отдаёт реальный остаток, а заодно тем же ключом забирается дневной
подарок. У остальных остаток считается по расходу самого ключа, а если и он
не читается — берётся из базы и в отчёте помечен как несвежий: иначе станции
без читаемой панели вообще не участвовали бы в выборе.

Ротация. В ~/.claude/gorouter_key лежит ключ активного аккаунта — его отдаёт
apiKeyHelper каждой новой сессии. Главная проверка прогона — станция, на которой
сессия сидит прямо сейчас: её адрес щупается запросом GET /v1/models тем самым
ключом. Пока станция отвечает, работает прежний порядок — ключ меняется, когда
у активного аккаунта осталось меньше ROTATE_BELOW_REQ **запросов**, а не долларов:
одни и те же 15 $ это 50 обращений на gorouter и 18 на tabitoken, и долларовый
порог срабатывал на дорогой станции почти втрое позже. Если же станция не ответила
(таймаут, 522, 5xx, заглушка вместо JSON) или ответила 401/403, сессия
парализована, и ключ переключается **принудительно, независимо от остатка** —
это исключение из правила порога. Цель — аккаунт, чей остаток тянет больше всего
запросов, по всем станциям сразу и без нижней границы: своего порога у кандидата
нет, иначе аккаунты с остатком ниже порога хоронились бы навсегда. Каждый кандидат
перед назначением щупается тем же запросом: мёртвый ключ или лежащая станция
сессию не поднимут.

Вместе с ключом в настройках меняется `env.ANTHROPIC_BASE_URL` на endpoint той же
станции. Ключ без своего endpoint убивает сессию: так и вышло 23.08, когда Босс
руками ушёл на tabitoken, а прежняя версия скрипта вернула туда ключ от gorouter.
Поэтому endpoint сверяется с активным ключом на каждом прогоне, а не только
в момент поворота.

Если в файле лежит ключ, не совпадающий ни с одним `token` в базе, скрипт не
трогает ничего: значит Босс переключил Claude Code на станцию, которой мы не
знаем, и возвращать сессию туда, откуда он только что ушёл, нельзя.

Рассчитан на крон: внешних зависимостей кроме requests и supabase нет,
proxy не требуется (но и не мешает).

    uv run python scripts/gorouter_balance.py
"""
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.app.config import allow_direct_localhost  # noqa: E402
from scripts.notify_ui import TOPIC_CLAUDE, notify_ui  # noqa: E402
from scripts.single import hold_lock  # noqa: E402

allow_direct_localhost()

OUT_FILE = ROOT / "log" / "gorouter_balance.txt"
# Последние удачные цифры по каждому аккаунту: когда токен отзывают, Босс всё
# равно должен видеть, сколько денег было и насколько давно.
STATE_FILE = ROOT / "log" / "gorouter_balance_state.json"
# Снимок станций для фронтенда: живость, состав моделей, лучший ключ.
STATIONS_FILE = ROOT / "log" / "claude_stations.json"

# Ключ активного аккаунта — его читает ~/.claude/bin/gorouter-key.sh (apiKeyHelper).
KEY_FILE = Path.home() / ".claude" / "gorouter_key"
# Здесь же живут резервный ключ (env.ANTHROPIC_AUTH_TOKEN) и адрес станции
# (env.ANTHROPIC_BASE_URL) — их скрипт держит согласованными с активным ключом.
SETTINGS_FILE = Path.home() / ".claude" / "settings.json"
# Порог считается в запросах, а не в деньгах: один и тот же 15 $ — это 50 обращений
# на gorouter (0.30 $) и 18 на tabitoken (0.80 $), то есть на дорогой станции порог
# срабатывал бы почти втрое позже. Самый горячий час за неделю — 198 запросов
# (23.08 10:00), значит на шаг крона в 15 минут приходится до 50; 60 — это тот пик
# плюс запас, чтобы деньги не кончились между двумя снимками.
ROTATE_BELOW_REQ = 60
# Запасной долларовый порог — только для станции, у которой в meta нет прайса:
# запросы там посчитать нечем, а остаться вовсе без порога хуже.
ROTATE_BELOW = 15.0
# Насколько кандидат должен быть выгоднее активного, чтобы уйти с ещё живого ключа.
# Порог отвечает на вопрос «когда деньги кончатся», но не на «где они кончатся
# позже»: 27.08 сессия сидела на tabitoken (0.80 $ за запрос, 5788 обращений
# на остаток) при живом gorouter, где тот же opus-5 стоит 0.30 $, а на аккаунте
# лежало 39845 обращений — в семь раз больше работы за те же деньги. Кратность,
# а не разница: она не даёт болтанки, потому что обратный переезд потребовал бы
# трёхкратного перевеса в другую сторону, а его сразу после поворота не бывает.
ROTATE_GAIN = 3.0
# Модель, без которой станция для сессии бесполезна: Босс работает именно на ней.
REQUIRED_MODEL = "claude-opus-5"
# Босс отмечает в примечании аккаунт, с которого ходит Claude Code. Метка едет
# за ключом, иначе после ротации она указывает на чужую строку.
MARK = "Claude"
MARK_SPARE = "Claude резерв"

ACCOUNT_TABLES = ("main_site_account", "main_site_account_custom")

# Путь страницы регистрации, когда образца ссылки у станции ещё нет: так выглядят
# все ссылки, скопированные с панелей семейства New API.
DEFAULT_AFF_PATH = "/sign-up"

# Средний запрос Claude Code, замеренный 2026-08-25 по логам панелей: 4490
# обращений на gorouter дали 128 505 входных и 680 выходных токенов, 1996 на
# tabitoken — 113 541 и 598. Кэша в логах нет вовсе (`cache_tokens: 0`), весь
# контекст уезжает заново каждым обращением. Цифры нужны там, где станция берёт
# за токены: число запросов из остатка иначе не выводится вовсе.
AVG_PROMPT_TOKENS = 125_000
AVG_COMPLETION_TOKENS = 650

# За сколько суток мерить наш темп работы. Три — просьба Босса; окно шире месяца
# панель всё равно не отдаёт (`时间跨度不能超过 1 个月`). Темп берётся общий по всем
# станциям, а не по аккаунту: сессия сидит на одном ключе, у остальных за трое
# суток честный ноль, и делить их остаток было бы не на что.
RATE_DAYS = 3

# Доступность станции проверяется её же Anthropic-адресом: GET /v1/models отдаёт
# список моделей, ничего не списывает и заодно говорит, жив ли ключ. Панель New API
# для этого не годится — она стоит за отдельным Cloudflare и лежит отдельно от
# моделей (у gorouter 23.08 панель отдавала 522 при рабочем API и наоборот).
PROBE_PATH = "/v1/models"
PROBE_TIMEOUT = 25
# Разовый сетевой сбой и 5xx станции неотличимы от падения, если спросить один раз.
# Именно на этом ключ мотался gorouter↔tabitoken каждые 15 минут: у крона нет
# прокси-переменных оболочки, и `Network is unreachable` прилетал на ровном месте.
PROBE_TRIES = 3
PROBE_PAUSE = 4.0
# Список моделей врёт о готовности: 23.08 у vyceai claude-opus-5 стоял в списке,
# а вызов отвечал 503 model_maintenance. Поэтому цель поворота проверяется живым
# запросом — но только цель: на станциях с тарифом за запрос (gorouter 0.30 $)
# такая проверка на каждом прогоне стоила бы под 29 $ в сутки.
CALL_TIMEOUT = 60
# Крон ходит каждые 15 минут, висеть на пробах ему нельзя: столько станций-кандидатов
# щупаем максимум, дальше отчёт скажет, что перебор оборван.
MAX_PROBES = 6

# Расход ключа отдаёт сам API, без панели и без cookies — этим и живут станции,
# где нет пары access_token + panel_id. New API: /v1/dashboard/billing/usage
# отдаёт `total_usage` в центах (сверено с панелью на трёх аккаунтах — разница
# 0.00). vyceai: /v1/me отдаёт `totalSpent` в долларах, а `balance` там — лимит
# самого ключа (0 = без лимита), не деньги аккаунта. Cookies Босс отверг сам:
# срок жизни неизвестен, и одной сессией несколькими аккаунтами станции не рулить.
USAGE_TIMEOUT = 20
# Остаток из расхода не выводится, нужна точка отсчёта: остаток, который Босс
# вписал руками, и расход на тот момент. Дальше
# остаток = остаток_калибровки − (расход_сейчас − расход_калибровки).
# Слабое место известно: пополнения и бонусы чек-ина в расходе не видны, поэтому
# правку баланса в базе скрипт считает новой калибровкой.
USAGE_EPS = 0.01
# Насколько верим цифре остатка: панель подтвердила её сейчас, расчёт по расходу
# опирается на калибровку Босса, база — это то, что кто-то вписал когда-то.
TRUST = {"panel": 0, "session": 1, "usage": 2, "db": 3}
SOURCE_MARK = {
    "panel": "",
    "session": " (по сессии панели)",
    "usage": " (по расходу ключа)",
    "db": " (из базы)",
}


def source_mark(cand: dict) -> str:
    return SOURCE_MARK.get(cand.get("source"), " (из базы)")

# Cloudflare пропускает только User-Agent Chrome Босса: с чужим UA вместо
# ответа отдаётся страница-заглушка. Мажорную версию менять при обновлении
# браузера — минорные в UA не попадают.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
TIMEOUT = 45

def aff_paths(sb, names: dict[int, str]) -> dict[int, str]:
    """Путь страницы регистрации по станциям — из ссылок, что уже есть в базе.

    По HTTP его не узнать: и `/sign-up`, и `/register` отдают одну и ту же оболочку
    SPA с кодом 200. Зато у большинства станций Босс уже скопировал готовую ссылку
    руками, и её путь — самое надёжное, что у нас есть.
    """
    paths: dict[int, str] = {}
    for table in ACCOUNT_TABLES:
        res = sb.table(table).select("site_id, aff").not_.is_("aff", "null").execute()
        for row in res.data or []:
            site_id = row.get("site_id")
            station = names.get(site_id)
            if not station or site_id in paths:
                continue
            found = re.match(
                rf"https://{re.escape(station)}(/[^?]*)\?aff=", (row.get("aff") or "").strip()
            )
            if found:
                paths[site_id] = found.group(1)
    return paths


def load_stations() -> list[dict]:
    """Станции с Anthropic-endpoint и их аккаунты с непустым token."""
    from backend.app.supabase_client import get_supabase

    sb = get_supabase()
    sites = sb.table("main_site").select("id, name, meta").order("name").execute()
    paths = aff_paths(sb, {s["id"]: s["name"] for s in sites.data or []})
    stations: list[dict] = []
    for site in sites.data or []:
        meta = site.get("meta") or {}
        endpoint = str(meta.get("endpoints_anthropic") or "").strip().rstrip("/")
        if not endpoint:
            continue
        # Станция закрыла нам доступ: и ключи, и сессии панели отвечают 401,
        # идти туда нечем. Метку `blocked` ставит Босс в мете, а не мы по ответу:
        # 401 бывает и временным, по одному прогону бан от сбоя не отличить.
        # Адрес и прайс при этом остаются в мете — знание о станции не теряем.
        if meta.get("blocked"):
            continue
        station = {
            "id": site["id"],
            "name": site["name"],
            "endpoint": endpoint,
            # панель живёт на домене станции; New API там или нет — видно по meta
            "panel_base": f"https://{site['name']}",
            # Какой панелью станция авторизуется: пусто — New API (пара
            # access_token + panel_id), "vyce" — своя панель по сессии из
            # localStorage. Признак в мете, а не в коде: по ответу панели
            # её тип не угадать, зато у сайта он известен при разборе.
            "panel_api": str(meta.get("panel_api") or "").strip().lower(),
            # Какие имена моделей писать в настройки Claude Code на этой станции:
            # main — то, на чём работает Босс, sonnet/haiku — самое дешёвое годное.
            # Решение принимается при разборе станции и лежит в meta, а не в коде:
            # состав моделей и цены у каждой свои, из списка /v1/models дешевизну
            # не вывести.
            "claude_models": meta.get("claude_models") or {},
            # Цена одного запроса на станции: (имя модели, доллары) либо None.
            # Нужна тут, а не в месте решения, чтобы порог и рейтинг считались
            # в запросах — деньги станций несравнимы между собой.
            "price": request_price(meta),
            # Куда ведёт партнёрская ссылка этой станции: путь берётся из ссылок,
            # которые уже лежат в базе, иначе `/sign-up`.
            "aff_path": paths.get(site["id"], DEFAULT_AFF_PATH),
            "accounts": [],
        }
        for table in ACCOUNT_TABLES:
            res = (
                sb.table(table)
                .select("id, login, note, token, access_token, panel_id, balance, aff")
                .eq("site_id", site["id"])
                .not_.is_("token", "null")
                .order("id")
                .execute()
            )
            for row in res.data or []:
                token = (row.get("token") or "").strip()
                if not token:
                    continue
                # панель копирует токен в буфер с хвостовыми пробелами
                access = (row.get("access_token") or "").strip()
                station["accounts"].append({
                    **row,
                    "token": token,
                    "access_token": access,
                    "table": table,
                    "key": f"{table}:{row['id']}",
                    "station": station["name"],
                })
        if station["accounts"]:
            stations.append(station)
    return stations


def api_get(session: requests.Session, base: str, path: str):
    res = session.get(
        f"{base}{path}",
        headers={"Referer": f"{base}/dashboard/overview"},
        timeout=TIMEOUT,
    )
    res.raise_for_status()
    body = res.json()
    if not body.get("success"):
        raise RuntimeError(f"{path}: {body.get('message') or 'отказ без причины'}")
    return body.get("data")


def aff_transfer(session: requests.Session, base: str, quota: int, unit: float) -> str:
    """Перевести партнёрский карман New API в основной баланс аккаунта.

    Партнёрские деньги лежат в `aff_quota` и на запросы не тратятся, пока их не
    переложат: 2026-08-25 у основного аккаунта tabitoken так залежалось 500 $ —
    вдвое больше его тогдашнего остатка. Панель делает это кнопкой, но тот же
    `POST /api/user/aff_transfer` принимает и `access_token`, так что забывать
    незачем. Отказ прогон не валит: у станции свой минимум перевода (у
    api.hcnsec.cn ¥7.30), и мелочь ниже порога просто ждёт следующего начисления.
    """
    if quota <= 0:
        return ""
    try:
        res = session.post(
            f"{base}/api/user/aff_transfer",
            json={"quota": quota},
            headers={"Referer": f"{base}/dashboard/overview"},
            timeout=TIMEOUT,
        )
        body = res.json()
    except Exception as exc:
        return f"партнёрка {quota / unit:.2f} $ не переведена: {type(exc).__name__}: {exc}"
    if not body.get("success"):
        return f"партнёрка {quota / unit:.2f} $ не переведена: {body.get('message') or 'отказ без причины'}"
    return f"партнёрка переведена в баланс: +{quota / unit:.2f} $"


def collect(row: dict, base: str) -> dict:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Authorization": f"Bearer {row['access_token']}",
        "New-Api-User": str(row["panel_id"]),
    })
    me = api_get(session, base, "/api/user/self")
    unit = float(api_get(session, base, "/api/status").get("quota_per_unit") or 500000)

    # Перевод идёт до чтения остатка, иначе в базу уехала бы цифра без партнёрских.
    aff_note = aff_transfer(session, base, int(me.get("aff_quota") or 0), unit)
    if aff_note.startswith("партнёрка переведена"):
        me = api_get(session, base, "/api/user/self")

    now = int(time.time())
    # Одним запросом берём всё окно темпа: панель отдаёт часовые ведра с `created_at`,
    # поэтому суточные цифры отбираются из тех же данных, без второго обращения.
    stats = api_get(
        session, base,
        f"/api/data/self?start_timestamp={now - RATE_DAYS * 86400}&end_timestamp={now}",
    ) or []
    by_model: dict[str, float] = {}
    spend = 0.0
    requests_24h = 0
    requests_window = 0
    for item in stats:
        count = int(item.get("count") or 0)
        requests_window += count
        if int(item.get("created_at") or 0) < now - 86400:
            continue
        amount = float(item.get("quota") or 0) / unit
        model = item.get("model_name") or "—"
        by_model[model] = by_model.get(model, 0.0) + amount
        spend += amount
        requests_24h += count

    balance = float(me.get("quota") or 0) / unit
    return {
        "display_name": me.get("display_name") or me.get("username") or "",
        "username": me.get("username") or "",
        "panel_id": me.get("id"),
        "aff_code": (me.get("aff_code") or "").strip(),
        "balance": balance,
        "used": float(me.get("used_quota") or 0) / unit,
        "requests_total": int(me.get("request_count") or 0),
        "requests_24h": requests_24h,
        "requests_window": requests_window,
        "spend_24h": spend,
        "days_left": balance / spend if spend > 0 else None,
        "by_model": by_model,
        "aff_note": aff_note,
    }

# Станция с собственной панелью (не New API) авторизует её тем же, чем браузер:
# у vyceai сессия лежит в localStorage под ключом `vyce_session` и живёт около
# недели. Босс копирует её в `access_token` записи — тогда остаток панель отдаёт
# как есть, и считать его по расходу ключа не нужно. Войти программно нельзя:
# POST /user/login требует токен Cloudflare Turnstile (одноразовый, из живой
# страницы) и отпечаток устройства.
VYCE_PANEL = "vyce"


def vyce_ask(base: str, session_key: str, path: str, method: str = "GET"):
    res = requests.request(
        method,
        f"{base}{path}",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Authorization": f"Bearer {session_key}",
            "Referer": f"{base}/dashboard",
        },
        timeout=TIMEOUT,
    )
    if res.status_code == 401:
        raise RuntimeError("401 — сессия панели истекла")
    res.raise_for_status()
    try:
        return res.json()
    except ValueError:
        raise RuntimeError(f"{path}: вместо JSON пришла страница") from None


def vyce_collect(row: dict, base: str) -> dict:
    """Снимок аккаунта по сессии панели. Суточного расхода панель не отдаёт."""
    data = vyce_ask(base, row["access_token"], "/user/dashboard")
    user = data.get("user") or {}
    stats = data.get("stats") or {}
    balance = stats.get("availableBalance")
    if balance is None:
        balance = user.get("totalBalance")
    return {
        "display_name": user.get("name") or "",
        "email": user.get("email") or "",
        "balance": float(balance or 0),
        "spent": float(stats.get("totalSpent") or 0),
        "requests": int(stats.get("totalRequests") or 0),
        "tier": str(user.get("tier") or "—"),
        "verified": bool(user.get("verified")),
        "keys": len(data.get("keys") or []),
    }


def vyce_claim_daily(base: str, session_key: str) -> str:
    """Забрать дневной подарок станции (25 $/сутки) — строкой для отчёта.

    Клейму нужен только Bearer: csrf у панели заведён под вход и регистрацию.
    Отказ прогон не валит: его дело — деньги и ключи, а окно подарка суточное,
    и прогонов за день 96, следующий доберёт.
    """
    try:
        info = vyce_ask(base, session_key, "/user/daily-reward")
    except Exception as exc:
        return f"дневной подарок: не спросить — {exc}"
    amount = float(info.get("rewardAmount") or 0)
    if not info.get("canClaim"):
        return (
            f"дневной подарок: уже взят {info.get('lastClaim') or '—'}, "
            f"стрик {info.get('streak') or 0}, следующий ~{amount:.0f} $"
        )
    try:
        res = vyce_ask(base, session_key, "/user/daily-reward/claim", method="POST")
    except Exception as exc:
        return f"!!! дневной подарок не взят: {exc}"
    got = float(res.get("amount") or res.get("rewardAmount") or amount or 0)
    streak = res.get("streak") or info.get("streak") or 1
    return f"дневной подарок взят: +{got:.2f} $, стрик {streak}"


def render_vyce_block(row: dict, snap: dict, gift: str, note: str) -> str:
    return "\n".join([
        f"Аккаунт: {snap['display_name']} ({snap['email']})  [{row['key']}]",
        f"Остаток средств       {snap['balance']:>10.2f} $",
        f"Всего использовано    {snap['spent']:>10.2f} $",
        f"Запросов всего        {snap['requests']:>10}",
        f"Тариф                 {snap['tier']:>10}"
        f"   верификация {'пройдена' if snap['verified'] else 'НЕТ'}"
        f", ключей {snap['keys']}",
        gift,
        note,
    ])


def probe_once(endpoint: str, token: str) -> dict:
    """Один запрос списка моделей. `net` отделяет нашу сеть от отказа станции."""
    try:
        res = requests.get(
            f"{endpoint}{PROBE_PATH}",
            headers={
                "x-api-key": token,
                "anthropic-version": "2023-06-01",
                "User-Agent": USER_AGENT,
            },
            timeout=PROBE_TIMEOUT,
        )
    except (requests.ConnectionError, requests.Timeout) as exc:
        # До станции не доехали вовсе: это может быть и она, и наша сеть или proxy.
        # Отличить одно от другого по одному запросу нельзя, поэтому помечаем `net`,
        # а решает вызывающий — по тому, отвечают ли остальные станции.
        return {
            "alive": False, "kind": "station", "net": True, "models": [],
            "why": f"{type(exc).__name__}: {exc}",
        }
    except Exception as exc:
        return {
            "alive": False, "kind": "station", "net": False, "models": [],
            "why": f"{type(exc).__name__}: {exc}",
        }
    if res.status_code in (401, 403):
        return {
            "alive": False,
            "kind": "key",
            "net": False,
            "why": f"HTTP {res.status_code} — ключ отозван или деньги кончились",
            "models": [],
        }
    if res.status_code >= 400:
        return {"alive": False, "kind": "station", "net": False,
                "why": f"HTTP {res.status_code}", "models": []}
    try:
        data = res.json()
    except ValueError:
        return {"alive": False, "kind": "station", "net": False,
                "why": "ответ не JSON (заглушка?)", "models": []}
    models = [str(m.get("id")) for m in (data.get("data") or []) if m.get("id")]
    if not models:
        return {"alive": False, "kind": "station", "net": False,
                "why": "список моделей пуст", "models": []}
    return {"alive": True, "kind": "ok", "net": False,
            "why": f"{len(models)} моделей", "models": models}


def probe(endpoint: str, token: str) -> dict:
    """Жива ли станция и годен ли ключ — по тому самому адресу, куда ходит Claude Code.

    `kind` отделяет отказ станции от отказа ключа: 401/403 значит, что адрес
    отвечает, а вот аккаунт исчерпан или отозван, и другие аккаунты той же
    станции пробовать стоит. Всё остальное (5xx, 522, таймаут, заглушка вместо
    JSON) — станция целиком.

    Мягкий отказ переспрашивается PROBE_TRIES раз: разовый 5xx и сетевой сбой от
    настоящего падения одним запросом неотличимы, и ровно на этом ключ мотался
    туда-обратно каждые 15 минут. 401/403 не повторяем — это окончательный ответ.
    """
    res = probe_once(endpoint, token)
    for _ in range(2, PROBE_TRIES + 1):
        if res["alive"] or res["kind"] == "key":
            return res
        time.sleep(PROBE_PAUSE)
        res = probe_once(endpoint, token)
    if not res["alive"] and res["kind"] != "key":
        res["why"] = f"{res['why']} — и так {PROBE_TRIES} раза подряд"
    return res


def probe_call(endpoint: str, token: str, model: str) -> dict:
    """Отвечает ли модель на самом деле: в списке она может стоять и не работать.

    23.08 у vyceai `claude-opus-5` числился в /v1/models, а вызов отдавал
    503 model_maintenance — сессия на такой станции поднялась бы мёртвой. Вызов
    делается только для цели поворота: на тарифе за запрос (gorouter 0.30 $)
    проверка на каждом прогоне стоила бы под 29 $ в сутки.
    """
    try:
        res = requests.post(
            f"{endpoint}/v1/messages",
            headers={
                "x-api-key": token,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
                "User-Agent": USER_AGENT,
            },
            json={
                "model": model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            },
            timeout=CALL_TIMEOUT,
        )
    except Exception as exc:
        return {"ok": False, "why": f"{type(exc).__name__}: {exc}"}
    # 429 — модель есть и работает, просто занята; для нас это годная станция
    if res.status_code < 400 or res.status_code == 429:
        return {"ok": True, "why": f"HTTP {res.status_code}"}
    why = f"HTTP {res.status_code}"
    try:
        err = (res.json().get("error") or {}).get("message")
    except ValueError:
        err = None
    return {"ok": False, "why": f"{why} {err}".strip()}


def read_usage(endpoint: str, token: str) -> tuple[float, str] | None:
    """Сколько этот ключ потратил всего, по данным самого API. (сумма в $, источник).

    Спрашиваем тем же ключом, которым ходит Claude Code, — панель и cookies не
    нужны. Два формата: New API отдаёт центы в `total_usage`, vyceai — доллары
    в `totalSpent`. Кто именно перед нами, определяем по ответу: у vyceai на
    билинговый путь приходит HTML главной страницы с кодом 200.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "x-api-key": token,
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }

    def ask(path: str):
        try:
            res = requests.get(f"{endpoint}{path}", headers=headers, timeout=USAGE_TIMEOUT)
        except Exception:
            return None
        if res.status_code >= 400:
            return None
        try:
            body = res.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    # Окно с запасом в обе стороны: нужен весь расход ключа за всё время
    end = f"{datetime.now().year + 1}-01-01"
    body = ask(f"/v1/dashboard/billing/usage?start_date=2020-01-01&end_date={end}")
    if body and body.get("total_usage") is not None:
        return float(body["total_usage"]) / 100.0, "billing/usage"
    body = ask("/v1/me")
    if body and body.get("totalSpent") is not None:
        return float(body["totalSpent"]), "/v1/me"
    return None


def derive_balance(row: dict, prev: dict, spent: float) -> tuple[float, dict, str]:
    """Остаток по расходу ключа: (остаток, точка калибровки, чем объяснить цифру).

    Калибровка заново нужна в трёх случаях: её ещё нет; в базе стоит не та цифра,
    которую записали мы (значит Босс пополнил счёт или поправил руками — расход
    пополнения не видит); расход стал меньше прежнего (счётчик станции сбросили).
    """
    db = float(row.get("balance") or 0)
    base_spent = prev.get("calib_spent")
    base_balance = prev.get("calib_balance")
    written = prev.get("written")
    if (
        base_spent is None
        or base_balance is None
        or written is None
        or abs(db - float(written)) > USAGE_EPS
        or spent < float(base_spent) - USAGE_EPS
    ):
        base_spent, base_balance = spent, db
        why = f"точка отсчёта взята из базы: {db:.2f} $ при расходе {spent:.2f} $"
    else:
        base_spent, base_balance = float(base_spent), float(base_balance)
        why = (
            f"от {base_balance:.2f} $ минус расход с калибровки "
            f"{spent - base_spent:.2f} $"
        )
    balance = max(base_balance - (spent - base_spent), 0.0)
    calib = {"calib_spent": base_spent, "calib_balance": base_balance, "spent": spent}
    return balance, calib, why


def write_balance(row: dict, balance: float) -> float:
    from backend.app.supabase_client import get_supabase

    value = round(balance, 2)
    get_supabase().table(row["table"]).update({"balance": value}).eq("id", row["id"]).execute()
    return value


def request_price(meta: dict) -> tuple[str, float] | None:
    """Цена одного запроса Claude Code на станции: имя модели и доллары.

    Тариф за запрос (`per_request`) берётся как есть — у gorouter это 0.30 $,
    у tabitoken 0.80 $, и контекст там ничего не стоит. Тариф за токены сводится
    к той же цифре по среднему запросу: без этого станции не сравнить между собой
    вовсе. Модель ищется в три шага, потому что имена у станций разъезжаются:
    сначала `claude-opus-5`, потом объявленная `claude_models.main`, потом самый
    дешёвый живой опус из прайса — у tokenbom он зовётся `claude-4.8-opus`.
    """
    price = meta.get("price")
    if not isinstance(price, dict):
        return None
    main = str((meta.get("claude_models") or {}).get("main") or "").strip()
    for name in (REQUIRED_MODEL, main):
        entry = price.get(name) if name else None
        if isinstance(entry, dict):
            amount = entry_price(entry)
            if amount:
                return name, amount
    opus = []
    for name, entry in price.items():
        if not isinstance(entry, dict) or "opus" not in name.lower():
            continue
        if str(entry.get("status") or "live") != "live":
            continue
        amount = entry_price(entry)
        if amount:
            opus.append((name, amount))
    return min(opus, key=lambda kv: kv[1]) if opus else None


def entry_price(entry: dict) -> float | None:
    per = entry.get("per_request")
    if per is not None:
        try:
            per = float(per)
        except (TypeError, ValueError):
            return None
        return per if per > 0 else None
    try:
        # Накладные токены провайдера — часть запроса: у tokenbom канал Claude
        # приклеивает системный промпт чужого coding-plan, это 6886 входа сверх наших.
        amount = (
            (AVG_PROMPT_TOKENS + float(entry.get("overhead_in") or 0))
            * float(entry["in"]) / 1e6
            + AVG_COMPLETION_TOKENS * float(entry["out"]) / 1e6
        )
    except (KeyError, TypeError, ValueError):
        return None
    return amount if amount > 0 else None


def recount_opus_req(day_rate: float = 0.0) -> list[str]:
    """Заполнить `opus_5_req` и `day_work` — запросы на остаток и дни работы.

    Считается по всем сайтам, а не только по станциям с Anthropic-адресом: цифра
    отвечает на вопрос «на сколько обращений хватит денег», и для станции без
    сессии он тоже осмыслен. Прайса нет — поле чистится, иначе в базе осталась бы
    цифра от прежней цены.

    `day_work` = запросы ÷ наш темп в день, один и тот же темп на все станции.
    Темпа нет (панели молчат, трафика за окно не было) — поле не трогаем вовсе:
    прежняя цифра честнее нуля, а измерения на этом прогоне просто не случилось.
    """
    from backend.app.supabase_client import get_supabase

    sb = get_supabase()
    lines: list[str] = []
    for site in sb.table("main_site").select("id, name, meta").order("name").execute().data or []:
        found = request_price(site.get("meta") or {})
        rows: list[dict] = []
        for table in ACCOUNT_TABLES:
            res = (
                sb.table(table)
                .select("id, login, balance, opus_5_req, day_work")
                .eq("site_id", site["id"])
                .order("id")
                .execute()
            )
            rows += [{**row, "table": table} for row in (res.data or [])]
        changed = 0
        total = 0
        for row in rows:
            want = None
            if found:
                want = int(float(row.get("balance") or 0) / found[1])
            patch: dict = {}
            if want != row.get("opus_5_req"):
                patch["opus_5_req"] = want
            if want is None:
                days = None
            elif day_rate > 0:
                # Три знака, а не один: остаток меньше суток фронтенд показывает
                # в часах, и при округлении до 0.1 дня часы прыгали бы через 2.4.
                days = round(want / day_rate, 3)
            else:
                days = row.get("day_work")
            if days != row.get("day_work"):
                patch["day_work"] = days
            if patch:
                sb.table(row["table"]).update(patch).eq("id", row["id"]).execute()
                changed += 1
            total += want or 0
        if not rows:
            continue
        if not found:
            lines.append(
                f"  {site['name']:<22} прайса нет — поле пусто "
                f"({len(rows)} аккаунтов, обновлено {changed})"
            )
            continue
        days_all = f"{total / day_rate:>7.1f} дней" if day_rate > 0 else "дни не считались"
        lines.append(
            f"  {site['name']:<22} {found[0]:<18} {found[1]:>7.4f} $/запрос   "
            f"аккаунтов {len(rows):>3}, запросов {total:>7}, {days_all}, обновлено {changed}"
        )
    return lines


def station_models(station: dict) -> dict:
    """Имена моделей для settings.json этой станции — из meta.claude_models.

    Решение принято при разборе станции и лежит в базе: состав моделей и цены
    у каждой свои, из списка /v1/models дешевизну не вывести. Нет отдельных
    sonnet и haiku — в оба поля идёт основная модель, так велел Босс.
    """
    conf = station.get("claude_models") or {}
    main = str(conf.get("main") or REQUIRED_MODEL).strip() or REQUIRED_MODEL
    return {
        "main": main,
        "sonnet": str(conf.get("sonnet") or main).strip() or main,
        "haiku": str(conf.get("haiku") or main).strip() or main,
    }


def effective_models(station: dict, available: list[str]) -> tuple[dict, list[str]]:
    """То же, но с подстановкой основной модели вместо той, которой на станции нет."""
    want = station_models(station)
    gone: list[str] = []
    for role in ("sonnet", "haiku"):
        if available and want[role] not in available:
            gone.append(want[role])
            want[role] = want["main"]
    return want, sorted(set(gone))


def update_balance(row: dict, snap: dict) -> str:
    # токены Босс вставляет руками, поэтому сначала сверяем владельца: иначе
    # опечатка в строке молча перезапишет баланс чужого аккаунта
    if snap["display_name"] and row["login"] and snap["display_name"] != row["login"]:
        return (
            f"!!! в базу не писал: у записи login={row['login']}, "
            f"а токен принадлежит {snap['display_name']} — вставлен не в ту строку"
        )
    write_balance(row, snap["balance"])
    return f"в базе обновлён {row['table']} id={row['id']} ({row['login']})"


def update_aff(row: dict, station: dict, snap: dict) -> str:
    """Проставить аккаунту его партнёрскую ссылку, если её нет или она разошлась.

    `aff_code` отдаёт только панель, поэтому ссылку получают лишь записи с парой
    `access_token` + `panel_id`. В базе у части аккаунтов лежал один код (`2PeJ`),
    а такой хвост без домена никуда не вставить — приводим к полному виду.
    Владелец сверяется, как и у баланса: токены вставляются руками.
    """
    code = (snap.get("aff_code") or "").strip()
    if not code:
        return ""
    if snap["display_name"] and row["login"] and snap["display_name"] != row["login"]:
        return ""
    want = f"https://{station['name']}{station['aff_path']}?aff={code}"
    if (row.get("aff") or "").strip() == want:
        return ""
    from backend.app.supabase_client import get_supabase

    get_supabase().table(row["table"]).update({"aff": want}).eq("id", row["id"]).execute()
    return f"партнёрская ссылка проставлена: {want}"


def render_block(row: dict, snap: dict, note: str) -> str:
    days = f"{snap['days_left']:.1f} дня" if snap["days_left"] else "—"
    lines = [
        f"Аккаунт: {snap['display_name']} ({snap['username']}, id {snap['panel_id']})"
        f"  [{row['key']}]",
        f"Остаток средств       {snap['balance']:>10.2f} $",
        f"Расход за 24 ч        {snap['spend_24h']:>10.2f} $",
        f"Запас                 {days:>14}",
        f"Всего использовано    {snap['used']:>10.2f} $",
        f"Запросов за 24 ч      {snap['requests_24h']:>10}",
        f"Запросов всего        {snap['requests_total']:>10}",
    ]
    if snap["by_model"]:
        lines.append("Расход по моделям за 24 ч:")
        for model, amount in sorted(snap["by_model"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  {model:<24}{amount:>8.2f} $")
    if snap.get("aff_note"):
        lines.append(snap["aff_note"])
    if snap.get("aff_fix"):
        lines.append(snap["aff_fix"])
    lines.append(note)
    return "\n".join(lines)


def render_fail_block(row: dict, error: str, saved: dict | None) -> str:
    lines = [
        f"Аккаунт: {row['login'] or '—'} (id {row['panel_id']}) [{row['key']}]",
        "!!! ОШИБКА ОБНОВЛЕНИЯ",
        f"    {error}",
    ]
    if saved:
        taken = datetime.fromisoformat(saved["taken_at"])
        age = (datetime.now().astimezone() - taken).total_seconds() / 3600
        lines.append(f"Последний известный остаток {saved['balance']:.2f} $")
        lines.append(f"Снят {taken:%Y-%m-%d %H:%M} — {age:.1f} ч назад")
    else:
        lines.append("Прежних цифр по этой записи нет")
    return "\n".join(lines)


def write_atomic(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def load_state() -> dict:
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    # прежние прогоны знали только gorouter и писали чистый id записи
    return {(k if ":" in k else f"main_site_account:{k}"): v for k, v in raw.items()}

def update_settings(
    station: dict, active: dict, available: list[str] | None = None
) -> list[str]:
    """Держит настройки согласованными со станцией активного ключа.

    Адрес, имена моделей и токен в env — одной записью файла: настройки
    читает работающая сессия, лишний промежуточный вариант ей ни к чему.

    Модели тут не декорация: 23.08 скрипт повернул ключ на vyceai, где нет
    `claude-opus-4-8` из настроек, и только напечатал «придётся править руками» —
    все вызовы sonnet и haiku (субагенты, сжатие контекста) упали, сессия встала.
    Поэтому имена берутся из `meta.claude_models` станции и пишутся сами.
    """
    endpoint = station["endpoint"]
    try:
        conf = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"!!! настройки не тронуты: {SETTINGS_FILE} не читается ({exc})"]
    env = conf.get("env")
    if not isinstance(env, dict):
        return ["!!! настройки не тронуты: в настройках нет блока env"]

    lines: list[str] = []
    changed = False
    if env.get("ANTHROPIC_BASE_URL") != endpoint:
        lines.append(
            f"!!! endpoint: ANTHROPIC_BASE_URL был {env.get('ANTHROPIC_BASE_URL') or 'пуст'}, "
            f"стал {endpoint} — приведён к станции активного ключа"
        )
        env["ANTHROPIC_BASE_URL"] = endpoint
        changed = True
    else:
        lines.append(f"endpoint: ANTHROPIC_BASE_URL уже {endpoint}")

    models, gone = effective_models(station, available or [])
    if gone:
        lines.append(
            f"!!! на {station['name']} нет моделей {', '.join(gone)} из meta.claude_models — "
            f"в настройки вместо них пишу {models['main']}"
        )
    for var, role in (
        ("ANTHROPIC_DEFAULT_SONNET_MODEL", "sonnet"),
        ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "haiku"),
    ):
        if env.get(var) == models[role]:
            lines.append(f"{role}: {var} уже {models[role]}")
            continue
        lines.append(
            f"!!! {role}: {var} был {env.get(var) or 'пуст'}, стал {models[role]} — "
            f"по meta.claude_models станции {station['name']}"
        )
        env[var] = models[role]
        changed = True

    # В env кладётся тот же ключ, что в файле apiKeyHelper. Проверено 25.08:
    # работающая сессия предпочитает `ANTHROPIC_AUTH_TOKEN` из настроек, а не
    # ключ помощника — после того как прогон в 12:51 вписал в env второй аккаунт,
    # запросы пошли с него (54 за час), а аккаунт из файла ключа простоял без
    # обращений с 08:00, хотя оба ключа живы. Пока там лежал чужой аккаунт, деньги
    # тратились не с того, на кого показывали и меню, и отчёт.
    row, left = active["row"], req_mark(active)
    mark = source_mark(active)
    if env.get("ANTHROPIC_AUTH_TOKEN") == row["token"]:
        lines.append(f"env-токен: {row['login']} — {left}{mark}, уже прописан")
    else:
        env["ANTHROPIC_AUTH_TOKEN"] = row["token"]
        changed = True
        lines.append(f"env-токен: прописан активный {row['login']} — {left}{mark}")

    if changed:
        mode = SETTINGS_FILE.stat().st_mode & 0o777
        write_atomic(
            SETTINGS_FILE, json.dumps(conf, ensure_ascii=False, indent=2) + "\n", mode
        )
    return lines


def mark_notes(cands: list[dict], active: dict | None) -> str:
    from backend.app.supabase_client import get_supabase

    want = {c["row"]["key"]: None for c in cands}
    if active:
        want[active["row"]["key"]] = MARK

    sb = get_supabase()
    moved: list[str] = []
    for cand in cands:
        row = cand["row"]
        target = want[row["key"]]
        note = row.get("note")
        if note == target:
            continue
        # заметки Босса по аккаунту важнее метки: пишем только в пустое поле
        # или туда, где стоит наша же метка
        if note not in (None, "", MARK, MARK_SPARE):
            continue
        sb.table(row["table"]).update({"note": target}).eq("id", row["id"]).execute()
        moved.append(f"{row['login']} ({row['station']}) → {target or 'пусто'}")
    return "метки в примечаниях: " + (", ".join(moved) if moved else "на месте")

def snapshot_stations(cands: list[dict], active_token: str) -> None:
    """Состояние станций для фронтенда: живость, модели, лучший ключ, годность.

    Пишется файлом, а не считается по запросу: живая проба стоит до PROBE_TIMEOUT
    на станцию, и страница «Сайты» ждала бы минуту. Крон обновляет снимок каждые
    15 минут — для отметки «сюда можно переключиться» этого хватает.

    Готовая метка `can_activate` считается здесь же: решение о годности принимает
    тот, кто щупал станцию, а страница только рисует по метке значок.
    """
    out: dict[str, dict] = {}
    for cand in rank(cands):
        station = cand["station"]
        if station["name"] in out or not station["endpoint"]:
            continue
        res = probe(station["endpoint"], cand["row"]["token"])
        balance = round(cand["balance"], 2)
        requests_left = cand_requests(cand)
        main = station_models(station)["main"]
        out[station["name"]] = {
            "site_id": station["id"],
            "endpoint": station["endpoint"],
            "alive": res["alive"],
            "kind": res["kind"],
            "why": res["why"],
            "models": res["models"],
            "claude_models": station_models(station),
            "balance": balance,
            # Запросы, а не деньги: порог годности считается в них, и странице
            # честнее показывать ту же единицу, в которой принято решение.
            "requests": None if requests_left is None else int(requests_left),
            "login": cand["row"]["login"],
            "account_id": cand["row"]["id"],
            "table": cand["row"]["table"],
            "fresh": cand["fresh"],
            "source": cand.get("source", "db"),
            # активна станция, а не лучший её аккаунт: сессия может сидеть
            # и на небогатом ключе, а значок «Активен» нужен у станции
            "active": any(acc["token"] == active_token for acc in station["accounts"]),
            # без основной модели станция бесполезна независимо от денег:
            # Босс работает именно на ней
            "can_activate": bool(
                res["alive"]
                and main in res["models"]
                and (
                    requests_left >= ROTATE_BELOW_REQ if requests_left is not None
                    else balance >= ROTATE_BELOW
                )
            ),
        }
    write_atomic(
        STATIONS_FILE,
        json.dumps(
            {"taken_at": datetime.now().astimezone().isoformat(timespec="seconds"),
             "rotate_below_req": ROTATE_BELOW_REQ, "stations": out},
            ensure_ascii=False, indent=2,
        ) + "\n",
    )


def cand_requests(cand: dict) -> float | None:
    """Сколько запросов Claude Code покрывает остаток кандидата.

    В этой единице считаются и порог, и рейтинг: доллары станций несравнимы —
    130 $ на tabitoken это 162 обращения, а 78 $ на gorouter уже 260. Цена берётся
    из `station["price"]`, то есть из того же `request_price`, которым считается
    `opus_5_req` в базе. Из базы поле не читаем намеренно: оно пишется в конце
    прогона и на момент ротации отстаёт на четверть часа, а решение принимается
    по свежему остатку этого прогона.

    None — у станции нет прайса в мете; такой кандидат уходит в конец рейтинга,
    а порог для него считается запасным, долларовым.
    """
    if "requests" not in cand:
        price = cand["station"].get("price")
        amount = price[1] if price else 0
        cand["requests"] = float(cand["balance"]) / amount if amount > 0 else None
    return cand["requests"]


def plural_req(n: int) -> str:
    """«1 запрос», «22 запроса», «60 запросов» — отчёт читает человек."""
    tail = n % 100
    if 11 <= tail <= 14:
        return "запросов"
    return {1: "запрос", 2: "запроса", 3: "запроса", 4: "запроса"}.get(tail % 10, "запросов")


def req_mark(cand: dict) -> str:
    """Остаток кандидата словами: запросы, а в скобках деньги для глаза Босса."""
    have = cand_requests(cand)
    if have is None:
        return f"{cand['balance']:.2f} $ (прайса нет, запросы не посчитать)"
    n = int(have)
    return f"{n} {plural_req(n)} ({cand['balance']:.2f} $)"


def rank(cands: list[dict]) -> list[dict]:
    # Впереди тот, чей остаток тянет больше обращений, а не тот, у кого больше
    # денег. Станция без прайса уходит в конец: сколько она тянет — неизвестно,
    # и целью поворота её стоит делать только когда измеримых не осталось.
    # При равных запросах впереди тот, чья цифра надёжнее: панель > расчёт по
    # расходу ключа > цифра из базы, которая могла устареть на месяцы. Аккаунты,
    # у которых панель отказала, уходят в конец — они под подозрением (бан,
    # отозванный токен).
    return sorted(
        cands,
        key=lambda c: (
            c["suspect"],
            cand_requests(c) is None,
            -(cand_requests(c) or 0.0),
            TRUST.get(c.get("source"), 3),
            c["row"]["id"],
        ),
    )


def pick_target(ranked: list[dict], exclude: str) -> tuple[dict | None, list[str], bool]:
    """Самый богатый аккаунт, чья станция прямо сейчас отвечает и умеет свою модель.

    Богатство — не единственный критерий: ключ, который вернул 401, и станция,
    которая не отвечает, сессию не поднимут. Поэтому кандидаты щупаются по порядку
    рейтинга. Станция без своей основной модели целью не становится вовсе:
    писать в настройки то, чего у неё нет, — это и есть кома, из которой Босс
    вытаскивал сессию руками.

    Третьим значением — отвечала ли по сети хоть одна станция. По нему выше
    отличают падение станции от падения нашей сети.
    """
    log: list[str] = []
    dead: set[str] = set()
    saw_answer = False
    probes = 0
    for cand in ranked:
        row, station = cand["row"], cand["station"]
        if row["token"] == exclude or cand["suspect"]:
            continue
        if station["name"] in dead:
            continue
        if probes >= MAX_PROBES:
            log.append(f"перебор оборван на {MAX_PROBES} пробах — дальше не щупал")
            break
        probes += 1
        res = probe(station["endpoint"], row["token"])
        if not res["net"]:
            saw_answer = True
        if not res["alive"]:
            log.append(f"проба {row['login']} @ {station['name']}: {res['why']}")
            if res["kind"] == "station":
                dead.add(station["name"])
            continue
        main = station_models(station)["main"]
        if main not in res["models"]:
            log.append(
                f"проба {row['login']} @ {station['name']}: жива, но {main} "
                "из meta.claude_models в списке нет — станция не годится"
            )
            dead.add(station["name"])
            continue
        call = probe_call(station["endpoint"], row["token"], main)
        if not call["ok"]:
            log.append(
                f"вызов {main} @ {station['name']}: {call['why']} — "
                "в списке модель есть, а работать не работает"
            )
            dead.add(station["name"])
            continue
        log.append(
            f"проба {row['login']} @ {station['name']}: жива, {res['why']}, "
            f"{main} отвечает ({call['why']})"
        )
        cand["models"] = res["models"]
        return cand, log, saw_answer
    return None, log, saw_answer


def rotate_keys(cands: list[dict]) -> list[str]:
    ranked = rank(cands)
    by_token = {c["row"]["token"]: c for c in cands}
    try:
        active_token = KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        active_token = ""
    current = by_token.get(active_token)

    if current is None:
        # Ключа нет ни у одной нашей записи — значит Босс переключил Claude Code
        # руками на станцию, которой мы не знаем (так было 23.08, когда gorouter
        # лёг). Переписать файл здесь означало бы молча вернуть следующую сессию
        # туда, откуда он только что ушёл, а тронуть endpoint — убить и текущую.
        return [
            f"в {KEY_FILE.name} лежит незнакомый ключ — переключено вручную, не трогаю",
            mark_notes(cands, None),
        ]

    lines: list[str] = []
    stale = source_mark(current)
    # Главная проверка прогона — станция, на которой сессия сидит прямо сейчас.
    # Пока она отвечает, работает прежний порядок: сверка остатка с порогом.
    alive = probe(current["station"]["endpoint"], active_token)
    available = alive["models"]
    where = f"{current['row']['login']} @ {current['row']['station']}"
    if alive["alive"]:
        lines.append(f"станция активного ключа: {where} отвечает, {alive['why']}")
    elif alive["kind"] == "key":
        lines.append(f"!!! станция отвечает, но ключ не принят: {where} — {alive['why']}")
    else:
        lines.append(f"!!! станция активного ключа не отвечает: {where} — {alive['why']}")

    have = cand_requests(current)
    # Прайса у станции нет — запросы посчитать нечем, судим по запасному
    # долларовому порогу: остаться вовсе без порога хуже.
    enough = (
        have >= ROTATE_BELOW_REQ if have is not None
        else current["balance"] >= ROTATE_BELOW
    )
    threshold = (
        f"при пороге {ROTATE_BELOW_REQ} запросов" if have is not None
        else f"при запасном пороге {ROTATE_BELOW:.0f} $"
    )
    gain = None
    if have is not None:
        best = next(
            (c for c in ranked if c["row"]["token"] != active_token and not c["suspect"]),
            None,
        )
        best_req = cand_requests(best) if best is not None else None
        if best_req is not None and best_req >= have * ROTATE_GAIN:
            gain = (best, best_req)
    if alive["alive"] and enough and gain is None:
        lines.append(
            f"основной: {where} — {req_mark(current)}{stale} {threshold}, менять не надо"
        )
    else:
        if alive["alive"] and gain is not None:
            best, best_req = gain
            reason = (
                f"выгода: {best['row']['login']} @ {best['row']['station']} тянет "
                f"{best_req:.0f} обращений против {have:.0f} у активного"
            )
        elif alive["alive"]:
            reason = f"осталось {req_mark(current)}{stale} — ниже порога"
        elif alive["kind"] == "key":
            reason = "ключ отозван или квота кончилась, переключаю принудительно"
        else:
            reason = "станция лежит, переключаю принудительно"
        lines.append(f"повод менять ключ: {reason}")
        target, plog, saw_answer = pick_target(ranked, active_token)
        lines.extend(plog)
        if target is None and alive["net"] and not saw_answer:
            # Ни активная станция, ни кандидаты не ответили по сети — станции сразу
            # все не падают, значит лежит наша сеть или proxy (у крона нет
            # прокси-переменных оболочки). Решать по таким данным нечего.
            lines.append(
                "!!! по сети не ответила ни одна станция — это наша сеть, а не они; "
                "ключ, настройки и метки не тронуты"
            )
            return lines
        if target is None:
            # менять некуда: разбудить сессию нечем, а испортить настройки можно
            lines.append("!!! живой станции не нашлось — ключ и endpoint не тронуты")
        elif (
            alive["alive"]
            and have is not None
            and (cand_requests(target) or 0) <= have
        ):
            # Активный оказался самым жирным во всём пуле, просто и он уже под
            # порогом. Уйти на второго — значит сесть на аккаунт беднее прежнего
            # и вернуться к тому же вопросу через четверть часа, заплатив за
            # пробы. Доедаем текущий до конца: так не остаётся огрызков.
            lines.append(
                f"лучший кандидат {target['row']['login']} @ {target['row']['station']} — "
                f"{req_mark(target)}, не больше активного; доедаю текущий, ключ не тронут"
            )
        else:
            write_atomic(KEY_FILE, target["row"]["token"] + "\n", 0o600)
            current = target
            available = target.get("models") or []
            lines.append(
                f"!!! ключ повёрнут на {target['row']['login']} @ {target['row']['station']} "
                f"({req_mark(target)}) — следующая сессия Claude Code стартует на нём"
            )

    station = current["station"]
    lines.extend(update_settings(station, current, available))
    lines.append(mark_notes(cands, current))
    return lines


def main() -> int:
    stations = load_stations()
    keys = {acc["key"] for st in stations for acc in st["accounts"]}
    # ключи раньше были именами cookie-файлов — чужое из state выкидываем
    state = {k: v for k, v in load_state().items() if k in keys}
    taken_at = datetime.now().astimezone()

    cands: list[dict] = []
    sections: list[str] = []
    readable = 0
    ok = 0
    # Темп берётся общий по всем станциям: сессия сидит на одном ключе, у остальных
    # аккаунтов за трое суток честный ноль.
    window_requests = 0
    for st in stations:
        blocks: list[str] = []
        derived: list[str] = []
        stale: list[str] = []
        expired: list[str] = []
        for row in st["accounts"]:
            if st["panel_api"] == VYCE_PANEL and row["access_token"]:
                readable += 1
                snap = None
                gift = ""
                try:
                    # подарок сначала: он поднимает остаток, который читаем ниже
                    gift = vyce_claim_daily(st["panel_base"], row["access_token"])
                    snap = vyce_collect(row, st["panel_base"])
                except Exception as exc:
                    expired.append(
                        f"  {(row['login'] or '—'):<30}{type(exc).__name__}: {exc}"
                    )
                if snap is not None:
                    blocks.append(
                        render_vyce_block(row, snap, gift, update_balance(row, snap))
                    )
                    cands.append({
                        "row": row, "station": st, "balance": snap["balance"],
                        "fresh": True, "source": "session", "suspect": False,
                    })
                    state[row["key"]] = {
                        "taken_at": taken_at.isoformat(timespec="seconds"),
                        "balance": snap["balance"],
                        "display_name": snap["display_name"],
                    }
                    ok += 1
                    continue
            if not (row["access_token"] and row["panel_id"]):
                # Панели нет — остаток считаем по расходу, который ключ отдаёт сам.
                usage = read_usage(st["endpoint"], row["token"])
                if usage is None:
                    balance = float(row.get("balance") or 0)
                    stale.append(f"  {(row['login'] or '—'):<30}{balance:>10.2f} $")
                    cands.append({
                        "row": row, "station": st, "balance": balance,
                        "fresh": False, "source": "db", "suspect": False,
                    })
                    continue
                spent, api = usage
                balance, calib, why = derive_balance(row, state.get(row["key"]) or {}, spent)
                written = write_balance(row, balance)
                derived.append(
                    f"  {(row['login'] or '—'):<30}{balance:>10.2f} $   "
                    f"израсходовано {spent:>9.2f} $   {why}"
                )
                state[row["key"]] = {
                    "taken_at": taken_at.isoformat(timespec="seconds"),
                    "balance": balance,
                    "display_name": row["login"] or "",
                    "written": written,
                    "api": api,
                    **calib,
                }
                cands.append({
                    "row": row, "station": st, "balance": balance,
                    "fresh": True, "source": "usage", "suspect": False,
                })
                continue
            readable += 1
            try:
                snap = collect(row, st["panel_base"])
            except Exception as exc:
                # Пара доступов есть, а панель отказала: аккаунт под подозрением
                # (бан, отозванный токен) либо станция целиком лежит. Целью
                # поворота он не станет, но в списке остаться обязан — иначе
                # активный ключ на лежащей станции выглядел бы незнакомым, и
                # скрипт решил бы, что Босс переключился руками.
                blocks.append(
                    render_fail_block(row, f"{type(exc).__name__}: {exc}", state.get(row["key"]))
                )
                cands.append({
                    "row": row, "station": st, "balance": float(row.get("balance") or 0),
                    "fresh": False, "source": "db", "suspect": True,
                })
                continue
            snap["aff_fix"] = update_aff(row, st, snap)
            blocks.append(render_block(row, snap, update_balance(row, snap)))
            window_requests += int(snap.get("requests_window") or 0)
            cands.append({
                "row": row, "station": st, "balance": snap["balance"],
                "fresh": True, "source": "panel", "suspect": False,
            })
            state[row["key"]] = {
                "taken_at": taken_at.isoformat(timespec="seconds"),
                "balance": snap["balance"],
                "display_name": snap["display_name"],
            }
            ok += 1

        head = f"═══ {st['name']}   endpoint {st['endpoint']}"
        if expired:
            expired.insert(
                0,
                f"{len(expired)} аккаунтов, где сессия панели не ответила — впишите "
                "в access_token свежее значение vyce_session (F12 → Application → "
                "Local Storage); пока остаток считается по расходу ключа:",
            )
            blocks.append("\n".join(expired))
        if derived:
            derived.insert(
                0,
                f"{len(derived)} аккаунтов без панели — остаток посчитан по расходу ключа "
                "(пополнения и бонусы в расходе не видны, поправка баланса в базе "
                "берётся за новую точку отсчёта):",
            )
            blocks.append("\n".join(derived))
        if stale:
            stale.insert(
                0,
                f"Ещё {len(stale)} аккаунтов, где расход не читается и панели нет — "
                "остаток из базы, свежесть не гарантирована:",
            )
            blocks.append("\n".join(stale))
        sections.append("\n\n".join([head, *blocks]) if blocks else head)

    head = [
        "Балансы станций Claude Code",
        f"Снято:   {taken_at:%Y-%m-%d %H:%M:%S %z}",
        f"Станций: {len(stations)}, аккаунтов с ключом: {len(cands)}, "
        f"свежих балансов: {ok} из {readable} по панели, "
        f"{sum(1 for c in cands if c.get('source') == 'session')} по сессии панели, "
        f"{sum(1 for c in cands if c.get('source') == 'usage')} по расходу ключа",
    ]
    if not stations:
        head.append("!!! ни у одной записи main_site не заполнен meta.endpoints_anthropic")
    rotation = rotate_keys(cands) if cands else ["!!! аккаунтов с ключом нет, ключи не тронуты"]
    sections.append("\n".join(["Ключи Claude Code:", *(f"  {line}" for line in rotation)]))
    # Считается после записи балансов: цифра запросов должна опираться на свежие деньги.
    day_rate = window_requests / RATE_DAYS
    try:
        req_lines = recount_opus_req(day_rate)
    except Exception as exc:
        req_lines = [f"  !!! не посчитано: {type(exc).__name__}: {exc}"]
    rate_note = (
        f"наш темп {day_rate:.0f} запросов в день "
        f"({window_requests} за {RATE_DAYS} суток по всем панелям)"
        if day_rate > 0
        else f"темп за {RATE_DAYS} суток не измерен — дни оставлены как были"
    )
    sections.append("\n".join([
        f"Запросов на остаток (opus_5_req, средний запрос {AVG_PROMPT_TOKENS:,} вход / "
        f"{AVG_COMPLETION_TOKENS} выход) и дней работы (day_work, {rate_note}):",
        *req_lines,
    ]))
    write_atomic(OUT_FILE, "\n\n".join(["\n".join(head), *sections]) + "\n")
    write_atomic(STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    if cands:
        try:
            active_token = KEY_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            active_token = ""
        snapshot_stations(cands, active_token)
    notify_ui(TOPIC_CLAUDE)
    print(OUT_FILE.read_text(encoding="utf-8"), end="")
    return 0 if cands and ok == readable else 1


if __name__ == "__main__":
    # Прогон дольше своего шага в кроне — второй экземпляр затрёт цифры первого.
    lock = hold_lock("gorouter_balance")
    sys.exit(0 if lock is None else main())
