"""Регистрация аккаунтов на станции New API по почте — без браузера и без GitHub.

`harvest_accounts.py` водит живой Chromium через GitHub OAuth: иначе на gorouter
и tabitoken не войти. Но часть станций пускает по обычной паре логин-пароль,
и когда у них ещё и выключена капча (`turnstile_check`), весь цикл сводится
к четырём HTTP-запросам. Голова тогда стоит секунды, а не минуту, прокси не нужен
вовсе, и топливом идёт пул `main_email` (~500 свободных ящиков), а не GitHub.

Первая цель — api.hcnsec.cn: капчи нет, OAuth нет, старт 4000 $ и 2000 $
инвайтеру за голову. `ai.fujcloud.com` этим путём не берётся — там
`turnstile_check` включён, и без живой страницы токен капчи не получить; скрипт
такую станцию отказывает на старте, а не пробует и не гадает.

Бывает и короче: у qkmss.com выключено `email_verification`, то есть станция
почту не спрашивает вовсе — тогда письма не ждём, ящик из `main_email` берётся
только под учёт (индекс `(site_id, email_id)` требует своей строки на голову),
а в записи `email` остаётся пустым: у аккаунта на станции почты действительно
нет. Спрашивается это у панели (`gate()`), а не размечается руками.

    uv run python scripts/harvest_email.py [домен станции] [--count N] [--dry]
                                           [--email <id>] [--inviter <id>]
                                           [--minutes N]

Порядок на одну голову: взять свободный ящик → попросить у станции код на почту →
достать код из письма правилами самого сайта (`main_site.mail_subject`,
`code_anchor`, `code_length`, `code_format`) → зарегистрироваться с партнёрским
кодом инвайтера → войти → создать API-ключ и снять его полное значение → забрать
«Токен доступа» → записать всё через backend.

Как и у сборщика по GitHub, аккаунт на станции появляется раньше записи в базе,
поэтому голова, которую backend не принял по молчанию, уезжает в очередь
`log/harvest_email_pending.json`, а следующий прогон начинает с её дописывания.
"""

import argparse
import json
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import find_dotenv, load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.app.config import allow_direct_localhost  # noqa: E402
from scripts.gorouter_checkin import QUOTA_PER_UNIT, UA, write_atomic  # noqa: E402
from scripts.notify_ui import TOPIC_CLAUDE, notify_ui  # noqa: E402
from scripts.single import hold_lock  # noqa: E402

load_dotenv(find_dotenv())
allow_direct_localhost()

BACKEND = "http://127.0.0.1:4000"
OUT_FILE = ROOT / "log" / "harvest_email.txt"
# Отчёт последнего прогона перезаписывается, а ключ и токен доступа уникальны
# и повторно их взять нечем: панель отдаёт ключ только в момент создания.
HISTORY_FILE = ROOT / "log" / "harvest_email_history.txt"
PENDING_FILE = ROOT / "log" / "harvest_email_pending.json"
JOURNAL = ROOT / "log" / "harvest_email_journal.txt"
JOURNAL_KEEP = 20000
KEY_NAME = "claude"

# Партнёрские деньги складываются на одну запись станции, а не размазываются
# по всем: на hcnsec это 2000 $ за голову. Ключ — домен, значение — id записи
# в `main_site_account_custom`, чей `aff` уходит в регистрацию.
INVITER = {"api.hcnsec.cn": 100, "qkmss.com": 102}

# Пауза между просьбами кода: станция отвечает «发送过于频繁，请等待 12 秒»,
# то есть лимит на отправку письма считается по адресу, а не по ящику.
SEND_GAP = 13
# Когда письма не ждём вовсе, ждать нечего: пауза остаётся только чтобы
# регистрации не шли в одну секунду.
QUICK_GAP = 2
# Ожидание письма: три взгляда в ящик с шагом `CODE_STEP`. Код приходит за
# несколько секунд, а живёт 10 минут — ждать дольше незачем, выборка случайная,
# и в другой раз этот ящик пройдёт с первого захода.
CODE_TRIES = 3
CODE_STEP = 4
# Окно, в котором письмо считается нашим: у ящика, уже гулявшего по станциям,
# в спаме лежат прошлые письма с той же темой.
CODE_WINDOW = 120
# Дедлайн прогона: голова укладывается в 10-20 секунд, но письма бывают медленные.
MAX_MINUTES = 13
HTTP_TIMEOUT = 30

# Регистрация New API: логин от 4 знаков, пароль от 8. Свои списки слов проекта
# (`backend.app.credentials`) дают человекообразную пару — её же Босс получает
# кнопкой на `/browse`, и в записи она выглядит так же, как заведённые руками.
PASS_LEN = 12


def note(text: str) -> None:
    """Строка со временем в журнал и в stdout."""
    line = f"{datetime.now():%d.%m %H:%M:%S} {text}"
    print(line, flush=True)
    try:
        JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        with JOURNAL.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


class Journal(list):
    """Список строк головы, который заодно пишет их в журнал сразу.

    Пачкой в конце головы все строки получили бы одно время — время её конца,
    а по журналу как раз и смотрят, между какими шагами повисло.
    """

    def append(self, item: str) -> None:  # type: ignore[override]
        super().append(item)
        note(f"    {item}")


def trim_journal() -> None:
    """Подрезать журнал: файл дописывается вечно, а читают из него хвост."""
    try:
        if JOURNAL.exists() and JOURNAL.stat().st_size > 4_000_000:
            lines = JOURNAL.read_text(encoding="utf-8", errors="replace").splitlines()
            write_atomic(JOURNAL, "\n".join(lines[-JOURNAL_KEEP:]) + "\n")
    except OSError as exc:
        note(f"журнал не подрезался: {exc}")


def retry_db(call, tries: int = 3):
    """Обращение к базе с повторами: под кроном разовый обрыв не повод падать."""
    last: Exception | None = None
    for attempt in range(tries):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 — причина печатается строкой
            last = exc
            time.sleep(2 * (attempt + 1))
    raise last if last else RuntimeError("база не ответила")


def load_station(domain: str) -> dict:
    """Станция с правилами кода и списком занятых ящиков.

    Правила извлечения кода берутся из самой записи сайта, а не зашиваются:
    у каждой станции своя тема письма и свой якорь, и заводит их тот же человек,
    что заводит станцию. Пустые правила здесь не отказ: станция может почту
    не спрашивать вовсе — это решает `gate()`, у которого есть ответ панели.
    """
    with httpx.Client(trust_env=False, timeout=HTTP_TIMEOUT) as c:
        sites = c.get(f"{BACKEND}/api/sites").json()
    row = next((s for s in sites if s["name"] == domain), None)
    if not row:
        raise SystemExit(f"станции {domain} нет в main_site")
    meta = row.get("meta") or {}
    rules = {k: row.get(k) for k in ("mail_subject", "code_anchor", "code_length", "code_format")}
    with httpx.Client(trust_env=False, timeout=HTTP_TIMEOUT) as c:
        taken = {
            r.get("email_id")
            for r in c.get(f"{BACKEND}/api/site-accounts-custom", params={"site_id": row["id"]}).json()
            if r.get("email_id")
        }
    return {
        "name": domain,
        "site_id": row["id"],
        "base": f"https://{domain}",
        "meta": meta,
        "rules": rules,
        "taken": taken,
        "needs_code": True,
    }


def gate(station: dict) -> str:
    """Пускает ли станция этим путём. Пустая строка — пускает, иначе причина.

    Спрашивается у самой панели, а не размечается в базе руками: владелец
    выключает парольную регистрацию и включает капчу когда захочет, и узнать
    об этом по отказу регистрации дороже — голова уже сожгла код на почте.
    Здесь же выясняется, нужен ли код вообще, и только при `email_verification`
    требуются правила разбора письма: без якоря регулярка вытащит из письма
    первое подходящее число, а в письмах это обычно id ссылки.
    """
    try:
        with client(station["base"]) as c:
            data = c.get("/api/status").json().get("data") or {}
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        return f"панель не ответила: {exc}"
    if not data.get("register_enabled"):
        return "регистрация закрыта владельцем"
    if not data.get("password_register_enabled"):
        return "парольная регистрация выключена, остаётся только OAuth"
    if data.get("turnstile_check"):
        return "включена капча Turnstile — нужен живой браузер, этим путём не взять"
    station["needs_code"] = bool(data.get("email_verification"))
    if not station["needs_code"]:
        note("    станция почту не спрашивает — письма не ждём, ящик идёт только под учёт")
        return ""
    rules = station["rules"]
    if not rules["mail_subject"] or not rules["code_anchor"]:
        return (
            f"станция требует код на почту, а правила разбора у {station['name']} "
            "не заведены (mail_subject/code_anchor)"
        )
    return ""


def client(base: str, cookies: httpx.Cookies | None = None) -> httpx.Client:
    """Клиент к панели: свой User-Agent и Referer, cookies живут между вызовами.

    `trust_env=False` намеренно: внешний proxy оболочки станции не нужен,
    а у крона его переменных всё равно нет — иначе прогоны расходились бы
    поведением.
    """
    return httpx.Client(
        base_url=base,
        cookies=cookies,
        follow_redirects=True,
        timeout=HTTP_TIMEOUT,
        trust_env=False,
        headers={
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{base}/",
            "Origin": base,
        },
    )


def inviter_row(station: dict, only_id: int | None) -> dict | None:
    """Запись-инвайтер станции: партнёрские деньги должны копиться на одной."""
    acc_id = only_id or INVITER.get(station["name"])
    if not acc_id:
        return None
    with httpx.Client(trust_env=False, timeout=HTTP_TIMEOUT) as c:
        rows = c.get(
            f"{BACKEND}/api/site-accounts-custom", params={"site_id": station["site_id"]}
        ).json()
    return next((r for r in rows if r["id"] == acc_id), None)


def inviter_aff(station: dict, only_id: int | None) -> str:
    """Партнёрский код инвайтера: хвост `?aff=` у назначенной записи станции."""
    row = inviter_row(station, only_id) or {}
    m = re.search(r"aff=([A-Za-z0-9_-]+)", row.get("aff") or "")
    return m.group(1) if m else ""


def aff_path(station: dict, only_id: int | None) -> str:
    """Путь страницы регистрации: берётся из ссылки, что уже лежит у инвайтера.

    По HTTP его не узнать — и `/register`, и `/sign-up` отдают одну оболочку SPA
    с кодом 200, а ссылка без верного пути никуда не вставится.
    """
    row = inviter_row(station, only_id) or {}
    m = re.search(r"https?://[^/]+(/[^?]*)\?aff=", row.get("aff") or "")
    return m.group(1) if m else "/register"


def cash_inviter(station: dict, only_id: int | None) -> None:
    """Переложить партнёрский карман инвайтера в его баланс.

    2000 $ за голову приходят в `aff_quota`, а оттуда на запросы не тратятся, пока
    их не переведут. Балансер этого не сделает: он ходит только по станциям
    с `meta.endpoints_anthropic`, а у картиночных станций его нет.
    """
    row = inviter_row(station, only_id)
    if not row or not row.get("access_token") or not row.get("panel_id"):
        return
    head = {
        "Authorization": f"Bearer {str(row['access_token']).strip()}",
        "New-Api-User": str(row["panel_id"]),
    }
    try:
        with client(station["base"]) as c:
            me = (c.get("/api/user/self", headers=head).json().get("data") or {})
            quota = int(me.get("aff_quota") or 0)
            if quota <= 0:
                return
            done = c.post("/api/user/aff_transfer", json={"quota": quota}, headers=head).json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        note(f"партнёрский карман не переведён: {exc}")
        return
    money = quota / QUOTA_PER_UNIT
    if done.get("success"):
        note(f"партнёрка инвайтера переведена в баланс: +{money:.2f} $")
    else:
        # У станции свой минимум перевода — мелочь ниже порога ждёт начислений.
        note(f"партнёрка {money:.2f} $ не переведена: {str(done.get('message'))[:120]}")


def free_email(station: dict, count: int, only_id: int | None) -> list[dict]:
    """Свободные ящики: живые и не занятые на этой станции.

    Берутся вразнобой, а не первые по id: неудачные головы ничем не помечаются,
    и выборка по порядку упиралась бы каждым прогоном в один и тот же ящик.
    Ящикам-заглушкам (`no-email@warning.com` и близнецы) здесь делать нечего —
    они и так `active=false`.

    Доступы Graph и адрес на hotmail требуются только там, где надо читать
    письмо: станции, которая почту не спрашивает, ящик нужен единственно как
    строка учёта, и сужать пул до читаемых незачем.
    """
    from backend.app.supabase_client import get_supabase

    def fetch():
        q = (
            get_supabase()
            .table("main_email")
            .select("id, email, password, client_id, graph_refresh_token")
            .eq("active", True)
            .is_("reason", "null")
            .order("id")
        )
        if station["needs_code"]:
            q = q.ilike("email", "%@hotmail.com")
        if only_id:
            q = q.eq("id", only_id)
        return q.execute().data or []

    rows = [
        dict(r)
        for r in retry_db(fetch)
        if r["id"] not in station["taken"]
        and (not station["needs_code"] or (r.get("client_id") and r.get("graph_refresh_token")))
    ]
    if only_id:
        return rows[:count]
    return random.sample(rows, min(count, len(rows)))


def credentials() -> tuple[str, str]:
    """Пара логин-пароль теми же словами, что даёт кнопка на `/browse`.

    `make_username` уже дописывает год, но словарь конечен, а логин панель
    требует уникальным на станцию — на второй сотне голов имена столкнулись бы,
    поэтому в хвост идут ещё две цифры.
    """
    from backend.app.credentials import make_password, make_username

    return f"{make_username()}{random.randint(10, 99)}", make_password(PASS_LEN)


def ask_code(c: httpx.Client, email: str, log: list[str]) -> bool:
    """Попросить станцию отправить код на почту.

    Отказ «уже занято» разбирается отдельно: это не поломка, а ящик, которым
    на станции уже регистрировались, и голову надо просто пропустить.
    """
    try:
        r = c.get("/api/verification", params={"email": email})
        data = r.json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        log.append(f"код не заказан: {exc}")
        return False
    if data.get("success"):
        return True
    msg = str(data.get("message") or "")[:160]
    log.append(f"станция отказала в коде: {msg or r.status_code}")
    return False


def wait_code(box: dict, station: dict, since: datetime, log: list[str]) -> str | None:
    """Код подтверждения из письма станции: `CODE_TRIES` взглядов в ящик.

    Ищется правилами самого сайта, а не своей регулярной: у hcnsec код шестизначный
    и с буквами (`95611d`), у другой станции будет иначе, а письмо у обеих ложится
    в спам. Разбор в `backend.app.main` уже умеет якорь и полноширинную пунктуацию —
    второй такой же тут не нужен.
    """
    from backend.app.main import _extract_verification_code
    from outlook_mail_checker import get_mail_by_subject

    rules = station["rules"]
    for attempt in range(1, CODE_TRIES + 1):
        try:
            mails = get_mail_by_subject(
                client_id=box["client_id"],
                refresh_token=box["graph_refresh_token"],
                subject=str(rules["mail_subject"]),
                match_mode="contains",
                date_from=since,
            )
        except Exception as exc:  # noqa: BLE001 — Graph падает по-разному
            log.append(f"ящик не прочитался: {exc}")
            return None
        code = _extract_verification_code(
            mails,
            int(rules["code_length"] or 6),
            rules["code_anchor"],
            str(rules["code_format"] or "digits"),
        )
        if code:
            log.append(f"код из письма: {code} (взгляд {attempt})")
            return code
        if attempt < CODE_TRIES:
            time.sleep(CODE_STEP)
    log.append(f"письма со кодом нет после {CODE_TRIES} взглядов")
    return None


def register(c: httpx.Client, box: dict, login: str, password: str, code: str, aff: str, log) -> bool:
    """Завести аккаунт. Партнёрский код едет тем же запросом — иначе не привяжется.

    Пустые `email` и `verification_code` — не забывчивость: станция с выключенным
    `email_verification` почту не спрашивает вовсе, и подсунутый ей адрес только
    привязал бы ящик к аккаунту без нужды.
    """
    payload = {
        "username": login,
        "password": password,
        "password2": password,
        "email": box["email"] if code else "",
        "verification_code": code,
        "aff_code": aff,
    }
    try:
        data = c.post("/api/user/register", json=payload).json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        log.append(f"регистрация не прошла: {exc}")
        return False
    if not data.get("success"):
        log.append(f"станция отказала в регистрации: {str(data.get('message'))[:160]}")
        return False
    log.append(f"зарегистрирован {login} / {password}" + (f", инвайтер aff={aff}" if aff else ", без инвайтера"))
    return True


def sign_in(c: httpx.Client, login: str, password: str, log) -> dict | None:
    """Войти и вернуть профиль: cookies панели остаются в клиенте.

    У панели нового вида (qkmss) в ответе не профиль, а `{"user": …,
    "access_token": <JWT>}`, и дальше она слушает `Authorization: Bearer <JWT>`,
    а не cookie-сессию. JWT ставится в заголовки клиента сразу — иначе все
    следующие вызовы получили бы 401 при полностью удачном входе.
    """
    try:
        data = c.post("/api/user/login", json={"username": login, "password": password}).json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        log.append(f"вход не прошёл: {exc}")
        return None
    if not data.get("success"):
        log.append(f"станция не впустила: {str(data.get('message'))[:160]}")
        return None
    body = data.get("data") or {}
    jwt = str(body.get("access_token") or "").strip()
    if jwt:
        c.headers["Authorization"] = f"Bearer {jwt}"
        log.append("панель нового вида, дальше иду по её JWT")
    return body.get("user") or body


def profile(c: httpx.Client, panel_id: int, log) -> dict | None:
    """`/api/user/self` под cookie-сессией: остаток, партнёрский код, id."""
    try:
        data = c.get("/api/user/self", headers={"New-Api-User": str(panel_id)}).json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        log.append(f"профиль не прочитался: {exc}")
        return None
    if not data.get("success"):
        log.append(f"профиль не отдан: {str(data.get('message'))[:160]}")
        return None
    return data.get("data") or {}


def make_key(c: httpx.Client, panel_id: int, log) -> str:
    """Создать API-ключ и снять его полное значение.

    Полного значения нет ни в списке ключей, ни в карточке — везде маска вида
    `wpkd**********AwYy`. Отдаёт его только `POST /api/token/<id>/key`, поэтому
    порядок такой: создать → найти свой id в списке → спросить значение.
    Путь со слешем: у qkmss `POST /api/token` отвечает 307, и хотя редирект
    сохраняет метод с телом, лишний круг тут ни к чему.
    """
    head = {"New-Api-User": str(panel_id)}
    body = {
        "name": KEY_NAME,
        "remain_quota": 0,
        "expired_time": -1,
        "unlimited_quota": True,
        "model_limits_enabled": False,
        "model_limits": "",
        "allow_ips": "",
        "group": "",
    }
    try:
        data = c.post("/api/token/", json=body, headers=head).json()
        if not data.get("success"):
            log.append(f"ключ не создан: {str(data.get('message'))[:160]}")
            return ""
        rows = c.get("/api/token/", params={"p": 1, "size": 10}, headers=head).json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        log.append(f"ключ не создан: {exc}")
        return ""
    items = (rows.get("data") or {}).get("items") or rows.get("data") or []
    row = next((r for r in items if isinstance(r, dict) and r.get("name") == KEY_NAME), None)
    if not row:
        log.append("ключ создан, но в списке не найден — снять значение нечем")
        return ""
    try:
        got = c.post(f"/api/token/{row['id']}/key", headers=head).json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        log.append(f"значение ключа не отдано: {exc}")
        return ""
    # `data` приходит объектом `{"key": "…"}`, а не строкой: у первой головы 27.08
    # в базу уехал `sk-{'key': …}`, и запись пришлось править руками.
    body_data = got.get("data") if got.get("success") else None
    key = str((body_data or {}).get("key") if isinstance(body_data, dict) else body_data or "").strip()
    if not key:
        log.append(f"значение ключа не отдано: {str(got.get('message'))[:160]}")
        return ""
    key = key if key.startswith("sk-") else f"sk-{key}"
    log.append(f"ключ снят: {key[:9]}…{key[-4:]}")
    return key


def access_token(c: httpx.Client, panel_id: int, log) -> str:
    """«Токен доступа» со страницы профиля.

    `GET /api/user/token` не читает токен, а **перегенерирует** его, гася прежний.
    На свежей голове это безопасно (прежним никто не ходил), на живом аккаунте
    такой запрос отобрал бы доступ у мониторинга балансов — потому и зовётся
    он здесь один раз, сразу после регистрации.
    """
    try:
        data = c.get("/api/user/token", headers={"New-Api-User": str(panel_id)}).json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        log.append(f"токен доступа не снят: {exc}")
        return ""
    token = str(data.get("data") or "").strip() if data.get("success") else ""
    if not token:
        log.append(f"токен доступа не снят: {str(data.get('message'))[:160]}")
    return token


def post_account(row: dict, log) -> bool | None:
    """Записать голову в базу. `True` записано, `None` backend молчал, `False` отказ.

    Разведено намеренно: таймаут и Connection refused лечатся повтором, а 409
    и 400 — нет, и такая голова висела бы в очереди вечно.
    """
    try:
        with httpx.Client(trust_env=False, timeout=HTTP_TIMEOUT) as c:
            r = c.post(f"{BACKEND}/api/site-accounts-custom", json=row)
    except httpx.HTTPError as exc:
        log.append(f"backend не ответил: {exc}")
        return None
    if r.status_code < 300:
        log.append(f"записано в базу, id {r.json().get('id')}")
        return True
    if r.status_code >= 500:
        log.append(f"backend ответил {r.status_code} — повторю в следующий прогон")
        return None
    log.append(f"backend отказал {r.status_code}: {r.text[:160]}")
    return False


def pending_load() -> list[dict]:
    try:
        return json.loads(PENDING_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def pending_add(row: dict) -> None:
    """Отложить голову, которую backend не принял по молчанию.

    Дедуп по паре сайт + ящик: голова могла пойти вторым заходом и завести
    второй ключ, а в базу нужен последний снятый.
    """
    rows = [
        r
        for r in pending_load()
        if not (r.get("site_id") == row.get("site_id") and r.get("email_id") == row.get("email_id"))
    ]
    rows.append(row)
    try:
        write_atomic(PENDING_FILE, json.dumps(rows, ensure_ascii=False, indent=2))
    except OSError as exc:
        note(f"очередь не записалась: {exc}")


def pending_flush() -> tuple[int, bool]:
    """Дописать отложенные головы. Возвращает число дописанных и «backend жив».

    Зовётся до выборки новых голов: сперва порядок в том, что уже снято.
    Если backend молчит на первой же голове, новых брать незачем — они лягут
    в ту же очередь, а каждая попытка стоит до `HTTP_TIMEOUT`.
    """
    rows = pending_load()
    if not rows:
        return 0, True
    note(f"в очереди {len(rows)} недописанных голов, дописываю")
    left, done, alive = [], 0, True
    for row in rows:
        if not alive:
            left.append(row)
            continue
        log = Journal()
        got = post_account({k: v for k, v in row.items() if k != "log"}, log)
        if got:
            done += 1
        elif got is None:
            alive = False
            left.append(row)
        # `False` — отказ по делу (409, 400): повтор ничего не изменит, выбрасываем.
    write_atomic(PENDING_FILE, json.dumps(left, ensure_ascii=False, indent=2))
    return done, alive


def run_one(station: dict, box: dict, aff: str, dry: bool) -> dict:
    """Одна голова: код на почту → регистрация → вход → ключ → токен → база."""
    log = Journal()
    out = {"email": box["email"], "email_id": box["id"], "status": "ОШИБКА", "log": log}
    log.append(f"ящик {box['email']} (id {box['id']})")
    login, password = credentials()
    out["login"], out["password"] = login, password
    if dry:
        out["status"] = "ПРОБА"
        log.append(f"проба: завёл бы {login} / {password}" + (f", aff={aff}" if aff else ""))
        return out

    since = datetime.now(timezone.utc) - timedelta(seconds=CODE_WINDOW)
    with client(station["base"]) as c:
        code = ""
        if station["needs_code"]:
            if not ask_code(c, box["email"], log):
                out["status"] = "НЕ ЗАКАЗАН КОД"
                return out
            code = wait_code(box, station, since, log) or ""
            if not code:
                out["status"] = "НЕТ КОДА"
                return out
        if not register(c, box, login, password, code, aff, log):
            out["status"] = "НЕ ЗАРЕГИСТРИРОВАН"
            return out
        who = sign_in(c, login, password, log)
        if who is None:
            out["status"] = "НЕ ВОШЁЛ"
            return out
        panel_id = int(who.get("id") or 0)
        out["panel_id"] = panel_id
        me = profile(c, panel_id, log) or who
        balance = round(float(me.get("quota") or 0) / QUOTA_PER_UNIT, 4)
        out["balance"] = balance
        log.append(f"panel_id {panel_id}, остаток {balance} $")
        out["token"] = make_key(c, panel_id, log)
        out["access_token"] = access_token(c, panel_id, log)

    row = {
        "site_id": station["site_id"],
        "email_id": box["id"],
        # Пусто, когда станция почту не спрашивает: адрес в записи означал бы,
        # что аккаунт к нему привязан, а он не привязан ни к чему.
        "email": box["email"] if station["needs_code"] else None,
        "login": login,
        "password": password,
        "token": out["token"],
        "balance": balance,
        "aff": (
            f"{station['base']}{station.get('aff_path', '/register')}?aff={me.get('aff_code')}"
            if me.get("aff_code")
            else None
        ),
        "access_token": out["access_token"],
        "panel_id": panel_id,
        "note": KEY_NAME if station["needs_code"] else f"{KEY_NAME}, почта не привязана — станция её не спрашивает",
    }
    got = post_account(row, log)
    if got:
        out["status"] = "ГОТОВ"
    elif got is None:
        pending_add(row)
        out["status"] = "ОТЛОЖЕН"
    else:
        out["status"] = "НЕ ЗАПИСАН"
    return out


GOOD = ("ГОТОВ", "ОТЛОЖЕН")


def render(station: dict, rows: list[dict], flushed: int) -> str:
    """Отчёт прогона. Ключ и токен доступа печатаются у незаписанных голов целиком:
    повторно их взять нечем — панель отдаёт значение ключа только при создании.
    """
    good = sum(1 for r in rows if r["status"] in GOOD)
    out = [
        f"Сбор по почте на {station['name']} — {datetime.now():%d.%m.%Y %H:%M}",
        f"голов {len(rows)}, удачных {good}",
    ]
    if flushed:
        out.append(f"дописано из очереди прошлых прогонов: {flushed}")
    for r in rows:
        out.append("")
        out.append(f"[{r['status']}] {r['email']} / {r.get('login', '—')}")
        if r["status"] not in GOOD:
            for extra in ("token", "access_token"):
                if r.get(extra):
                    out.append(f"    {extra}: {r[extra]}")
            if r.get("panel_id"):
                out.append(f"    пароль {r.get('password')}, panel_id {r['panel_id']} — "
                           "аккаунт на станции есть, дописать в базу руками")
        for line in r["log"]:
            out.append(f"    {line}")
    return "\n".join(out) + "\n"


def save_report(text: str) -> None:
    """Отчёт после каждой головы: `harvest_email.txt` перезапишет следующий прогон,
    а снятый ключ невосстановим — потому же второй копией идёт история.
    """
    try:
        OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(OUT_FILE, text)
        with HISTORY_FILE.open("a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError as exc:
        note(f"отчёт не записался: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Регистрация на станции по почте, без браузера")
    ap.add_argument("domain", nargs="?", default="api.hcnsec.cn", help="домен станции")
    ap.add_argument("--count", type=int, default=5, help="сколько голов за прогон")
    ap.add_argument("--dry", action="store_true", help="ничего не заводить, только показать")
    ap.add_argument("--email", type=int, help="взять один ящик по id из main_email")
    ap.add_argument("--inviter", type=int, help="id записи-инвайтера вместо зашитой")
    ap.add_argument("--minutes", type=int, default=MAX_MINUTES, help="дедлайн прогона")
    args = ap.parse_args()

    lock = hold_lock("harvest_email")
    if lock is None:
        return 0
    trim_journal()
    note(f"=== старт: {args.domain}, голов {args.count}" + (", проба" if args.dry else ""))

    station = load_station(args.domain)
    refused = gate(station)
    if refused:
        note(f"станция не берётся этим путём: {refused}")
        return 1

    flushed, alive = (0, True) if args.dry else pending_flush()
    if not alive:
        note("backend молчит — новых голов не беру, очередь дописывать нечем")
        save_report(render(station, [], flushed))
        return 1

    aff = "" if args.dry else inviter_aff(station, args.inviter)
    station["aff_path"] = "/register" if args.dry else aff_path(station, args.inviter)
    boxes = free_email(station, args.count, args.email)
    if not boxes:
        note("свободных ящиков под эту станцию нет")
        return 1
    note("головы: " + ", ".join(f"{b['id']}/{b['email']}" for b in boxes))

    deadline = time.monotonic() + args.minutes * 60
    rows: list[dict] = []
    for i, box in enumerate(boxes):
        if time.monotonic() > deadline:
            note(f"дедлайн {args.minutes} мин — остальные головы следующему прогону")
            break
        if i and not args.dry:
            # Станция считает частоту писем по адресу, а не по ящику: без паузы
            # второй голове придёт «发送过于频繁». Где писем нет — и ждать нечего.
            time.sleep(SEND_GAP if station["needs_code"] else QUICK_GAP)
        try:
            rows.append(run_one(station, box, aff, args.dry))
        except Exception as exc:  # noqa: BLE001 — под кроном трейсбек читать некому
            note(f"голова {box['email']} упала: {exc}")
            rows.append({"email": box["email"], "status": "ОШИБКА", "log": [str(exc)[:200]]})
        save_report(render(station, rows, flushed))

    good = sum(1 for r in rows if r["status"] in GOOD)
    note(f"=== конец: удачных {good} из {len(rows)}")
    if good and not args.dry:
        cash_inviter(station, args.inviter)
        notify_ui(TOPIC_CLAUDE)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — прогон обязан кончиться сам
        note(f"прогон упал: {exc}")
        raise SystemExit(1) from exc
