"""Регистрация аккаунтов на станции New API и сбор ключей без рук.

Ручной цикл Босса: разлогиниться в GitHub, войти под следующим логином, открыть
партнёрскую ссылку станции, нажать «Зайти через GitHub», создать API-ключ,
скопировать «Токен доступа» и «ID пользователя», вписать всё на фронтенде.
Разлогиниваться здесь не нужно вовсе: каждый аккаунт идёт в своей сессии
agent-browser, где GitHub не залогинен, и форму входа он показывает сам —
рабочий Chrome Босса при этом не трогается.

Ключ панель отдаёт по Bearer только маской (см. память проекта), поэтому все
запросы к панели идут **изнутри страницы**, под её cookie-сессией. Маска там
та же: полное значение снимается кнопкой копирования на странице ключей —
перед кликом подменяется `navigator.clipboard.writeText`, и снятое сверяется
с маской по краям.

    uv run python scripts/harvest_accounts.py [домены станций] [--count N] [--dry]
                                              [--github <id>] [--inviter <id>]
                                              [--minutes N]

Станций можно передать сколько угодно, через пробел: прогон идёт по ним по порядку
и переходит к следующей, когда под текущую свободных GitHub-голов не осталось.
Так пул выбирается до дна без правки крона: новая станция — просто ещё одно имя
в строке задания.

Каждый аккаунт идёт через свой прокси из `HARVEST_PROXIES` по кругу: GitHub
считает подозрительным десятки разных логинов с одного адреса, а пул аккаунтов
у Босса один.

Прогон рассчитан на крон и потому не падает ни от чего: неудачный аккаунт
получает второй заход с другого адреса пула, а если и он не прошёл — голова
просто пропускается. Ни ящик, ни аккаунт негодными не помечаются и отсрочек
не получают: письмо GitHub недосылает без всякой связи с почтой.
"""

import base64
import json
import os
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
from scripts.gorouter_checkin import (  # noqa: E402
    GITHUB_VERIFY_SUBJECT,
    QUOTA_PER_UNIT,
    ref_for,
    write_atomic,
)
from scripts.gorouter_checkin import ab as ab_raw  # noqa: E402
from scripts.single import hold_lock  # noqa: E402

load_dotenv(find_dotenv())
allow_direct_localhost()

BACKEND = "http://127.0.0.1:4000"
OUT_FILE = ROOT / "log" / "harvest.txt"
# Отчёт последнего прогона перезаписывается, а ключи и токены доступа уникальны
# и повторно их взять нечем — поэтому каждый прогон дописывается ещё и в историю.
HISTORY_FILE = ROOT / "log" / "harvest_history.txt"
# Очередь незаписанных голов. Аккаунт на станции создаётся раньше записи в базе,
# и пока backend лежит, ключ с токеном доступа существуют только в памяти прогона:
# 26.08 так осталось в логе девять созданных аккаунтов по 70 $. Теперь голова ждёт
# здесь, а следующий прогон начинает с того, что дописывает очередь.
PENDING_FILE = ROOT / "log" / "harvest_pending.json"
# Журнал прогонов: отчёт показывает итог, а журнал — ход дела со временем каждого
# шага. Под кроном стоять рядом и смотреть некому, а «застрял» от «не пустил»
# отличается только по времени между строками.
JOURNAL = ROOT / "log" / "harvest_journal.txt"
# Подрезка журнала: 48 прогонов в сутки пишут около тысячи строк, и без предела
# файл за год дойдёт до десятков мегабайт.
JOURNAL_KEEP = 20000
# Версия темпа: она уезжает в строку старта журнала, и по ней `harvest_speed.py`
# разделяет прогоны на до и после правки. Иначе замеры «стало быстрее» пришлось бы
# сверять по времени суток руками. Поднимать при каждой правке ожиданий и пауз.
VERSION = 6
KEY_NAME = "claude"
# Чего ждать после перехода. `networkidle` требует полутора секунд полной тишины
# в сети, а SPA станции и GitHub всё это время что-то подтягивают — на замере
# 26.08 ожидание отдавало по 20-30 секунд на переход при уже готовой странице.
# `load` возвращается по готовности документа, а недорисованную страницу добирает
# сам цикл: он переспрашивает снимок каждые `STEP` секунд, а где нужен конкретный
# текст — стоит явное `wait --text`.
READY = "load"
# Ожидание письма GitHub: три взгляда в ящик с шагом `CODE_STEP`, потом один
# «Re-send» и ещё три. Больше не бомбим: обычно код приходит за пять секунд,
# а ящик никуда не денется — выборка случайная, и в другой раз этот аккаунт
# пройдёт с первого захода. Прежние шесть раундов держали голову до пяти минут.
CODE_TRIES = 3
CODE_STEP = 4
# Окно, в котором письмо считается «нашим»: у головы на втором заходе в ящике лежит
# письмо первого, и с окном в минуты подставился бы его код.
CODE_WINDOW = 90
# Короткая пауза после клика: SPA дорисовывается уже после готовности документа,
# но не секунды. Дальше решает не сон, а `wait --text` и снимок.
PAUSE = 1.5
# Пауза между попытками в циклах ожидания страницы.
STEP = 2
# Заходов на один аккаунт за прогон. Первый отказ ничего не доказывает: у GitHub
# бывает разовый 500, у станции — застрявший OAuth, а письмо он недосылает без
# всякой связи с ящиком. Каждый заход идёт с нового адреса пула.
ATTEMPTS = 2
# Статусы, после которых повторный заход осмыслен. Во всех остальных отказах
# аккаунт на станции уже создан, и второй заход завёл бы ему второй ключ.
RETRY_STATUSES = ("НЕ ВОШЁЛ", "ОШИБКА")
# Дедлайн прогона: голова занимает около минуты. Под кроном прогон обязан
# кончиться сам, освободив место следующему, — иначе тот выйдет по блокировке.
# Шаг крона 15 минут, поэтому 13: пятнадцать голов по 32 с укладываются в восемь,
# а запас идёт на отказные головы, которые тянут по две минуты.
MAX_MINUTES = 13
# «Ключи API» в навигации станции: там же живёт кнопка «Скопировать».
# В консоли путь другой (`/console/*`), но у ключей он именно верхнего уровня.
KEYS_PATH = "/keys"

# Партнёрские деньги Босс складывает на одну запись станции, а не размазывает
# по всем: 40 $ за голову на gorouter капают инвайтеру. Отсюда ссылка берётся
# у конкретного аккаунта, а не у первого попавшегося с заполненным `aff`.
INVITER = {"gorouter.app": 80, "tabitoken.com": 60}

# Пул прокси из `HARVEST_PROXIES`: один аккаунт — один адрес, по кругу. Иначе
# GitHub видит десятки разных логинов с одного IP, а пул аккаунтов у Босса
# единственный и терять его нельзя. Пусто — идём общим `PROXY_URL`, как чек-ин.
_SESSION_PROXY: dict[str, str] = {}


def note(text: str) -> None:
    """Строка со временем в журнал и в stdout.

    Ошибка записи глотается: журнал нужен, чтобы смотреть за прогоном, а не чтобы
    его ронять — головы всё равно видны в логе крона.
    """
    line = f"{datetime.now():%d.%m %H:%M:%S} {text}"
    print(line, flush=True)
    try:
        JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        with JOURNAL.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def trim_journal() -> None:
    """Оставить в журнале последние `JOURNAL_KEEP` строк.

    Проверка по размеру, а не по числу строк: читать файл целиком каждый прогон
    только ради подсчёта незачем.
    """
    try:
        if not JOURNAL.exists() or JOURNAL.stat().st_size < 4_000_000:
            return
        tail = JOURNAL.read_text(encoding="utf-8", errors="replace").splitlines()
        write_atomic(JOURNAL, "\n".join(tail[-JOURNAL_KEEP:]) + "\n")
    except Exception:
        pass


class Journal(list):
    """Шаги головы: в журнал уходят сразу, со своим временем.

    Обычным списком они попадали бы в журнал пачкой в конце головы — с одним
    временем на все, и застрявший шаг было бы не отличить от быстрого.
    """

    def append(self, text: str) -> None:
        super().append(text)
        note(f"    {text}")


def proxy_pool() -> list[str]:
    """Адреса из `HARVEST_PROXIES` в виде `http://user:pass@host:port`.

    Продавец отдаёт их строкой `host:port:user:pass`, и переписывать это руками
    в URL — верный способ наделать опечаток, поэтому оба вида принимаются.

    Разделителем считается любой пробел наравне с запятой и точкой с запятой:
    списки приходят и строкой через запятую, и столбиком из файла продавца.
    Пока пробел разделителем не был, пул из `log/proxy_check_good.txt` уехал
    в Chrome одной строкой `--proxy-server` целиком, и прогон не открыл ни одной
    страницы, сообщая при этом «адрес не пустил».
    """
    out = []
    for raw in re.split(r"[\s,;]+", os.getenv("HARVEST_PROXIES") or ""):
        item = raw.strip().strip('"')
        if not item:
            continue
        if "://" not in item:
            parts = item.split(":")
            item = (
                f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
                if len(parts) == 4
                else f"http://{item}"
            )
        out.append(item)
    return out


def ab(session: str, *args: str, timeout: int = 120) -> str:
    """agent-browser в сессии, через закреплённый за ней прокси."""
    return ab_raw(session, *args, timeout=timeout, proxy=_SESSION_PROXY.get(session))


def proxy_order(pool: list[str], turn: int) -> list[str]:
    """Адреса для заходов на одну голову: каждый заход с нового.

    Отказал не ящик, а адрес: письмо GitHub недосылает без всякой связи с почтой,
    зато к IP он придирчив. Поэтому повторный заход берёт следующий адрес пула,
    а тот, что уже не пустил в этой голове, не повторяется, пока пул не короче
    числа заходов.
    """
    return [pool[(turn + k) % len(pool)] for k in range(ATTEMPTS)]


def retry_db(fn, what: str, tries: int = 3):
    """Обращение к базе с повтором: под кроном сетевой чих не должен валить прогон."""
    for i in range(tries):
        try:
            return fn()
        except Exception as exc:
            note(f"{what}: {exc}")
            if i + 1 < tries:
                time.sleep(5)
    return None


_MAILBOX: dict[str, tuple[str, str] | None] = {}


def mailbox(email: str) -> tuple[str, str] | None:
    """Пара `client_id` + `graph_refresh_token` ящика, один раз на прогон.

    Прежний `fetch_device_code` перечитывал строку `main_email` перед каждой
    попыткой — при опросе ящика каждые четыре секунды это лишний поход в базу
    внутри бюджета ожидания.
    """
    if email in _MAILBOX:
        return _MAILBOX[email]
    from backend.app.supabase_client import get_supabase

    got: tuple[str, str] | None = None
    try:
        row = get_supabase().table("main_email").select("*").eq("email", email).limit(1).execute()
        if row.data:
            cid, tok = row.data[0].get("client_id"), row.data[0].get("graph_refresh_token")
            if cid and tok:
                got = (str(cid), str(tok))
    except Exception as exc:
        note(f"    ящик {email} не прочитался: {exc}")
    _MAILBOX[email] = got
    return got


def js(session: str, expr: str, timeout: int = 90):
    """Выполнить выражение на открытой странице и разобрать результат.

    Скрипт уезжает base64, иначе кавычки и фигурные скобки не доживают до
    браузера. agent-browser печатает результат как JSON-строку, поэтому
    разбирать приходится дважды: внешнюю обёртку и сам ответ панели.
    """
    b64 = base64.b64encode(f"({expr})".encode()).decode()
    raw = ab(session, "eval", "-b", b64, timeout=timeout).strip()
    for _ in range(2):
        if isinstance(raw, (dict, list)):
            return raw
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw
    return raw


# Токен доступа новой панели на сессию браузера: добывается один раз, дальше
# подмешивается в каждый запрос.
_BEARER: dict[str, str] = {}


def bearer(session: str) -> str:
    """Токен новой панели: `POST /api/user/auth/refresh` под cookie-сессией.

    У старого фронта New API запросы шли по cookie плюс `New-Api-User`, у нового
    (tabitoken) — по `Authorization: Bearer`, а сам токен живёт в памяти стора
    и в localStorage не попадает. Взять его можно тем же путём, которым его
    обновляет SPA. Станция без этой ручки отдаёт не-JSON или 404 — тогда пусто,
    и запрос идёт по-старому.
    """
    if session in _BEARER:
        return _BEARER[session]
    expr = (
        "fetch('/api/user/auth/refresh',{method:'POST',credentials:'include'})"
        ".then(r=>r.text())"
    )
    tok = ""
    out = js(session, expr)
    if isinstance(out, str):
        try:
            out = json.loads(out)
        except json.JSONDecodeError:
            out = None
    if isinstance(out, dict):
        data = out.get("data") if isinstance(out.get("data"), dict) else out
        tok = str(data.get("access_token") or "")
    _BEARER[session] = tok
    if tok:
        note("    панель нового вида, взял токен через /api/user/auth/refresh")
    return tok


def api(session: str, path: str, method: str = "GET", body: dict | None = None, tries: int = 4):
    """Запрос к панели изнутри страницы — под её cookie-сессией.

    Заголовок `New-Api-User` старая панель требует и от своей же страницы, поэтому
    id берётся оттуда же, откуда его берёт SPA: из `localStorage.user`. У новой
    панели этого ключа нет вовсе и нужен `Authorization: Bearer` (см. `bearer()`);
    лишние заголовки ни одна из двух не проверяет, поэтому ставятся оба. Ответ
    не словарь — значит вместо JSON пришла страница nginx, и запрос повторяется.
    """
    auth = bearer(session)
    opts = "method:'%s',credentials:'include',headers:{'New-Api-User':String(u.id||'')" % method
    if auth:
        opts += ",'Authorization':'Bearer %s'" % auth
    if body is not None:
        opts += ",'Content-Type':'application/json'},body:%s" % json.dumps(
            json.dumps(body, ensure_ascii=False)
        )
    else:
        opts += "}"
    expr = (
        "(()=>{const u=JSON.parse(localStorage.getItem('user')||'{}');"
        f"return fetch('{path}',{{{opts}}})"
        ".then(r=>r.text().then(t=>JSON.stringify({__code:r.status,__body:t})))})()"
    )
    res = None
    for i in range(tries):
        out = js(session, expr)
        # Код ответа нужен именно в журнале: 429 от станции говорит, что она
        # считает регистрации с одного адреса, и по времени между такими строками
        # видно её окно. Без кода 429 выглядел бы как «панель ответила не JSON».
        if isinstance(out, dict) and "__code" in out:
            code, body = out.get("__code"), out.get("__body")
            try:
                res = json.loads(body) if isinstance(body, str) else body
            except (json.JSONDecodeError, TypeError):
                res = body
            if isinstance(code, int) and code >= 400:
                short = (body or "")[:160] if isinstance(body, str) else ""
                note(f"    станция: {method} {path} → {code} {short}")
        else:
            res = out
        if isinstance(res, dict):
            return res
        if i + 1 < tries:
            time.sleep(STEP)
    return res


# Что станция говорит вместо своей страницы. 429 у New API приходит и текстом
# страницы, и тостом SPA, поэтому ищется по словам, а не по коду ответа.
STATION_ALERTS = (
    "429",
    "Too Many Requests",
    "rate limit",
    "请求过于频繁",
    "操作过于频繁",
    "too frequent",
    "Internal Server Error",
    "502 Bad Gateway",
)


def station_says(session: str) -> str:
    """Жалоба станции со страницы, строкой из её же текста."""
    try:
        text = ab(session, "eval", "document.body.innerText.slice(0,600)")
    except Exception:
        return ""
    low = text.lower()
    for mark in STATION_ALERTS:
        if mark.lower() not in low:
            continue
        line = next((s.strip() for s in text.splitlines() if mark.lower() in s.lower()), mark)
        return line[:200]
    return ""




def masked_match(masked: str, full: str) -> bool:
    """Совпадает ли снятое значение с маской панели по краям.

    Маска вида `sk-wpkd**********AwYy`. Сверка нужна, потому что кнопок
    копирования на странице столько же, сколько ключей, и нажать можно чужую
    строку. Префикс `sk-` снимается с обеих сторон: в таблице он есть, а в
    ответе `/api/token/` его нет.
    """
    def bare(v: str) -> str:
        return v[3:] if v.startswith("sk-") else v

    masked, full = bare(masked), bare(full)
    head = masked.split("*", 1)[0]
    tail = masked.rsplit("*", 1)[-1]
    return bool(head) and full.startswith(head) and full.endswith(tail)


def load_station(domain: str, inviter: int | None) -> dict:
    """Станция, её партнёрская ссылка и уже занятые github_id."""
    from backend.app.supabase_client import get_supabase

    sb = get_supabase()
    row = sb.table("main_site").select("id, name, meta").eq("name", domain).limit(1).execute()
    if not row.data:
        raise SystemExit(f"станции {domain} нет в main_site")
    site = row.data[0]
    if (site.get("meta") or {}).get("blocked"):
        raise SystemExit(f"{domain} помечена meta.blocked — идти туда нечем")

    accs = (
        sb.table("main_site_account")
        .select("id, aff, github_id")
        .eq("site_id", site["id"])
        .execute()
    ).data or []
    want = inviter or INVITER.get(domain)
    links = [str(a["aff"]) for a in accs if a["id"] == want and a.get("aff")]
    if not links:
        links = [str(a["aff"]) for a in accs if a.get("aff") and "?aff=" in str(a["aff"])]
    if not links:
        raise SystemExit(f"у {domain} нет партнёрской ссылки — регистрироваться некуда")

    taken = {int(a["github_id"]) for a in accs if a.get("github_id")}
    return {
        "site_id": site["id"],
        "name": domain,
        "base": f"https://{domain}",
        "aff": links[0],
        "taken": taken,
    }


def free_github(station: dict, count: int, only_id: int | None) -> list[dict]:
    """Свободные GitHub-аккаунты — те же условия, что у /api/github/accounts/browse.

    Берутся вразнобой, а не первые по id. Отсрочек и пометок неудачникам не
    завели намеренно: недошедшее письмо GitHub к ящику не привязано, и блокировать
    за него аккаунт незачем. Но выборка по id упиралась бы каждым прогоном в одну
    и ту же голову — при шаге крона в полчаса это десятки заходов в сутки на один
    логин, и вот это GitHub уже примет за перебор пароля. Случайный порядок решает
    и то, и другое: пропустили — идём дальше, а вернёмся когда придётся.
    """
    from backend.app.supabase_client import get_supabase

    q = (
        get_supabase()
        .table("main_github")
        .select("id, login, pass_github, email")
        .eq("active", True)
        .is_("error_status", "null")
        .ilike("email", "%@hotmail.com")
        .order("id")
    )
    if only_id:
        q = q.eq("id", only_id)
    rows = [r for r in (q.execute().data or []) if r["id"] not in station["taken"]]
    rows = [dict(r) for r in rows if r.get("login") and r.get("pass_github")]
    if only_id:
        return rows[:count]
    return random.sample(rows, min(count, len(rows)))


def bad_gateway(session: str) -> bool:
    """Отдала ли станция страницу nginx вместо своей.

    gorouter стреляет `500 Internal Server Error` вперемешку с нормальными
    ответами — и на статике, и на `/api/oauth/state`, и на странице возврата
    из GitHub. Лечится перезагрузкой: код авторизации при этом не тратится,
    потому что SPA до него не дошла.
    """
    body = ab(session, "eval", "document.body.innerText.slice(0,120)")
    return "Internal Server Error" in body or "502 Bad Gateway" in body


def reload_page(session: str) -> None:
    ab(session, "reload")
    ab(session, "wait", "--load", READY)
    time.sleep(PAUSE)


def start_oauth(session: str, station: dict, log: list[str]) -> bool:
    """Нажать «Продолжить с GitHub» и убедиться, что станция ушла на GitHub.

    `/api/oauth/state` у gorouter отдаёт nginx 500 примерно на каждой третьей
    попытке (проверено 2026-08-25, 8 запросов подряд: 3 отказа). Страница тогда
    показывает тост «Не удалось начать вход через GitHub» и остаётся на месте,
    так что клик приходится повторять.
    """
    host = station["name"]
    for attempt in range(8):
        if bad_gateway(session):
            reload_page(session)
            continue
        snap = ab(session, "snapshot", "-i")
        ref = ref_for(snap, "GitHub", role="button") or ref_for(snap, "GitHub", role="link")
        if not ref:
            # холодная сессия через прокси открывает станцию не с первой секунды
            ab(session, "wait", "--text", "GitHub", timeout=40)
            time.sleep(PAUSE)
            snap = ab(session, "snapshot", "-i")
            ref = ref_for(snap, "GitHub", role="button") or ref_for(snap, "GitHub", role="link")
        if not ref:
            where = ab(session, "eval", "location.href").strip()
            what = ab(session, "eval", "document.body.innerText.slice(0,200)").strip()
            log.append(f"кнопки GitHub нет на {where}: {what[:160]}")
            return False
        ab(session, "click", f"@{ref}")
        time.sleep(STEP)
        url = ab(session, "eval", "location.href")
        if host not in url or "/dashboard" in url or "/console" in url:
            if attempt:
                log.append(f"OAuth начался с {attempt + 1}-й попытки")
            return True
        said = station_says(session)
        if said:
            log.append(f"станция после клика: {said}")
        time.sleep(STEP)
    said = station_says(session)
    log.append(f"станция не начала OAuth: {said or '/api/oauth/state отдаёт 500'}")
    return False


def read_code(creds: tuple[str, str], since: datetime) -> str | None:
    """Один взгляд в ящик: код подтверждения устройства или ничего."""
    from backend.app.main import _extract_verification_code
    from outlook_mail_checker import get_mail_by_subject

    try:
        mails = get_mail_by_subject(
            client_id=creds[0],
            refresh_token=creds[1],
            subject=GITHUB_VERIFY_SUBJECT,
            match_mode="exact",
            date_from=since,
        )
    except Exception:
        return None
    return _extract_verification_code(mails, 6, None, "digits")


def wait_device_code(session: str, gh: dict, log: list[str]) -> str | None:
    """Дождаться кода подтверждения устройства: два подхода по `CODE_TRIES`.

    Обычно письмо приходит за пять секунд. Три взгляда в ящик, один «Re-send the
    authentication code», ещё три — и всё. Дальше GitHub бомбить нельзя, а ждать
    незачем: ящик никуда не денется, выборка случайная, и в другой раз этот
    аккаунт пройдёт с первого захода.

    Отсечка `since` берётся от начала ожидания минус `CODE_WINDOW`: у головы,
    пошедшей на второй заход, в ящике лежит письмо первого — с окном в минуты
    подставился бы его код. Полторы минуты назад — это запас на расхождение часов
    и на письмо, помеченное чуть раньше нашего клика, но уже не прошлый заход.
    Радикальный путь — чистить ящик `delete_mail_by_subject` перед заходом; он
    надёжнее, но это два лишних похода в Graph на голову, а окна хватает.
    """
    email = gh.get("email")
    creds = mailbox(email) if email else None
    if not creds:
        log.append(f"у ящика {email} нет доступов Graph, кода не будет")
        return None

    since = datetime.now(timezone.utc) - timedelta(seconds=CODE_WINDOW)
    for approach in (1, 2):
        for _ in range(CODE_TRIES):
            code = read_code(creds, since)
            if code:
                return code
            time.sleep(CODE_STEP)
        if approach == 1:
            snap = ab(session, "snapshot", "-i")
            ref = ref_for(snap, "Re-send", "Resend", role="button") or ref_for(
                snap, "Re-send", "Resend", role="link"
            )
            if not ref:
                log.append("письма нет, кнопки Re-send на странице тоже")
                return None
            log.append("письма нет, жму Re-send один раз")
            ab(session, "click", f"@{ref}")
    log.append("письма нет и после Re-send, беру следующий аккаунт")
    return None


# Жалобы GitHub, которые надо видеть в журнале дословно: по ним отличается
# подпалённый адрес прокси от неверного пароля и от требования второго фактора.
GITHUB_ALERTS = (
    "Incorrect username or password",
    "There have been several failed attempts",
    "flagged",
    "suspended",
    "Sorry, something went wrong",
    "two-factor",
    "unable to verify",
    "Please wait a while",
)

# Жалобы, после которых заход бесполезен: замок GitHub снимается временем, и
# второй заход с другого адреса получает тот же текст — проверено 2026-08-26 на
# аккаунте 311 (два новых адреса, тот же отказ). Ретрай тут только тратит минуты.
LOCK_MARKS = (
    "There have been several failed attempts",
    "Please wait a while",
    "suspended",
    "flagged",
)


def locked(said: str) -> bool:
    low = said.lower()
    return any(m.lower() in low for m in LOCK_MARKS)


# Отказала станция, а не адрес: до GitHub дело не дошло вовсе, значит второй
# заход с другого прокси меняет ровно ничего и стоит полторы минуты живого
# браузера. Прогон 27.08 на tabitoken: 9 строк «/api/oauth/state отдаёт 500»,
# и каждая получила бесполезный повтор.
STATION_MARKS = (
    "станция не начала OAuth",
    "кнопки GitHub нет",
)
# Статус такой головы: аккаунта на станции нет вовсе (OAuth не начинался),
# значит и пометка «дописать в базу руками» ей не нужна.
NO_STATION = "ОТКАЗ СТАНЦИИ"


def station_fault(log: list[str]) -> str:
    """Строка журнала, из которой видно, что не пустила станция. Иначе пусто."""
    for line in log:
        for mark in STATION_MARKS:
            if mark.lower() in line.lower():
                return line
    return ""


def github_says(session: str) -> str:
    """Жалоба GitHub со страницы, если она там есть.

    Берётся строкой из текста страницы, а не пересказом: «several failed attempts
    … from this IP» и «Incorrect username or password» лечатся по-разному, и
    спутать их нельзя.
    """
    try:
        text = ab(session, "eval", "document.body.innerText.slice(0,800)")
    except Exception:
        return ""
    low = text.lower()
    # Экран кода подтверждения жалобой не считается: он логируется своей строкой,
    # а слово two-factor стоит там лишь в подписи «Having problems?».
    if "device verification" in low:
        return ""
    for mark in GITHUB_ALERTS:
        if mark.lower() not in low:
            continue
        line = next((s.strip() for s in text.splitlines() if mark.lower() in s.lower()), mark)
        return line[:200]
    return ""


def oauth_register(session: str, gh: dict, station: dict, log: list[str]) -> bool:
    """Пройти партнёрскую ссылку и войти через GitHub OAuth.

    Регистрация у New API происходит сама на первом входе, отдельной формы нет.
    Цикл тот же, что у чек-ина: логин, редкий код устройства, Authorize.
    """
    host = station["name"]
    ab(session, "open", station["aff"])
    ab(session, "wait", "--load", READY)
    time.sleep(PAUSE)

    if not start_oauth(session, station, log):
        return False
    ab(session, "wait", "--load", READY)
    time.sleep(PAUSE)

    url = ""
    retries = 0
    asked = False
    # Итераций больше, чем шагов у самого OAuth: ждать переходы теперь `load`,
    # а не `networkidle`, поэтому недорисованную страницу добирает этот цикл.
    for _ in range(20):
        url = ab(session, "eval", "location.href")
        if f"{host}/dashboard" in url or f"{host}/console" in url or f"{host}/profile" in url:
            log.append("GitHub пропустил без вопросов, OK" if not asked else "GitHub впустил, OK")
            return True
        if host in url and bad_gateway(session):
            reload_page(session)
            continue
        # Возврат из GitHub упал, и станция выкинула на форму входа. Перезагрузка
        # тут не лечит: код авторизации уже потрачен на неудачном обмене, а
        # повторный клик по кнопке начинает OAuth заново — GitHub в этой сессии
        # уже залогинен, так что второй заход проходит без формы.
        if f"{host}/sign-in" in url or f"{host}/login" in url:
            retries += 1
            if retries > 3:
                break
            log.append(f"станция вернула на вход, начинаю OAuth заново ({retries})")
            if not start_oauth(session, station, log):
                return False
            ab(session, "wait", "--load", READY)
            time.sleep(PAUSE)
            continue
        snap = ab(session, "snapshot", "-i")

        u = ref_for(snap, "Username or email address", role="textbox")
        p = ref_for(snap, '"Password"', role="textbox")
        b = ref_for(snap, '"Sign in"', role="button")
        if u and p and b:
            asked = True
            log.append("GitHub показал форму входа, ввожу логин и пароль")
            ab(session, "fill", f"@{u}", gh["login"])
            ab(session, "fill", f"@{p}", gh["pass_github"])
            ab(session, "click", f"@{b}")
            ab(session, "wait", "--load", READY)
            time.sleep(STEP)
            said = github_says(session)
            if said:
                log.append(f"GitHub ответил: {said}")
                if locked(said):
                    return False
            continue

        code_ref = ref_for(snap, "verification code", "код подтверждения", role="textbox")
        if code_ref or "device verification" in snap.lower():
            asked = True
            log.append(f"GitHub требует код подтверждения письмом на {gh.get('email')}")
            code = wait_device_code(session, gh, log)
            if not code:
                # Ящик пуст не только когда письмо задержалось: GitHub этим же
                # экраном отвечает на подозрение к аккаунту и письма тогда не
                # присылает вовсе. Без текста страницы эти случаи не различить.
                seen = ab(session, "eval", "document.body.innerText.slice(0,400)")
                log.append(f"код подтверждения устройства не пришёл; страница: {seen.strip()[:300]}")
                return False
            log.append(f"подтверждение устройства, код {code}")
            if code_ref:
                ab(session, "fill", f"@{code_ref}", code)
            else:
                ab(session, "keyboard", "type", code)
            ab(session, "wait", "--load", READY)
            time.sleep(STEP)
            continue

        auth = ref_for(snap, "Authorize", "Авторизовать", role="button")
        if auth:
            asked = True
            log.append("GitHub просит подтвердить доступ, жму Authorize")
            ab(session, "click", f"@{auth}")
            ab(session, "wait", "--load", READY)
            time.sleep(STEP)
            continue

        # Ни формы, ни кода, ни Authorize — значит на странице что-то своё:
        # у станции лимит регистраций, у GitHub жалоба. Молча ждать в этом месте
        # и означало «непонятно, почему прогон встал».
        said = station_says(session) if host in url else github_says(session)
        if said:
            log.append(f"{'станция' if host in url else 'GitHub'}: {said}")
            # Замок GitHub снимается только временем: и второй заход с другого
            # адреса получает тот же текст (проверено 2026-08-26 на аккаунте 311).
            # Держать голову дальше нечем, а пачке она мешает.
            if locked(said):
                return False
        time.sleep(STEP)

    text = ab(session, "eval", "document.body.innerText.slice(0,300)")
    log.append(f"застряли на {url.strip()}: {text.strip()[:250]}")
    return False


def list_keys(session: str) -> list[dict]:
    res = api(session, f"/api/token/?p=0&size=20&keyword={KEY_NAME}")
    data = res.get("data") if isinstance(res, dict) else None
    if isinstance(data, dict):
        data = data.get("items") or data.get("records") or []
    return [r for r in (data or []) if isinstance(r, dict)]


def key_by_clipboard(session: str, base: str, masked: str, log: list[str]) -> str | None:
    """Снять ключ кнопкой копирования, подменив запись в буфер.

    Читать `navigator.clipboard.readText()` не нужно (и разрешения на это
    у headless-браузера может не быть): подменяем сам `writeText` и берём
    значение, которое страница собиралась положить в буфер.

    Кнопка копирования — безымянная иконка сразу за кнопкой с маской, поэтому
    ищется она не по тексту, а по соседству: в снапшоте строка `button
    "sk-…**…"` и следующая за ней строка с `ref=`. Так же берётся ref самой
    маски — она тоже отдаёт ключ в буфер, это запасной клик.
    """
    for _ in range(8):
        ab(session, "open", base + KEYS_PATH)
        ab(session, "wait", "--load", READY)
        time.sleep(PAUSE)
        if not bad_gateway(session):
            break
    else:
        log.append(f"страница {KEYS_PATH} отдаёт 500")
        return None
    # таблица рисуется после загрузки: без ожидания снапшот пуст
    ab(session, "wait", "--text", KEY_NAME, timeout=40)
    time.sleep(PAUSE)

    js(
        session,
        "Object.defineProperty(navigator.clipboard,'writeText',"
        "{value:v=>{window.__cap=v;return Promise.resolve()},configurable:true}),"
        "window.__cap=null,'ok'",
    )
    lines = ab(session, "snapshot", "-i").splitlines()
    refs: list[str] = []
    for i, line in enumerate(lines):
        if not re.search(r'button "sk-\w+\*+\w+"', line):
            continue
        shown = re.search(r'"(sk-[^"]+)"', line)
        if not shown or not masked_match(masked, shown.group(1)):
            continue
        nxt = re.search(r"ref=(e\d+)", lines[i + 1]) if i + 1 < len(lines) else None
        own = re.search(r"ref=(e\d+)", line)
        refs += [m.group(1) for m in (nxt, own) if m]
    if not refs:
        log.append(f"строки с маской {masked} на странице ключей нет")
        return None

    for ref in refs:
        ab(session, "click", f"@{ref}")
        time.sleep(1.5)
        got = js(session, "window.__cap")
        if isinstance(got, str) and got and masked_match(masked, got):
            return got.strip()
    log.append(f"кнопки нажаты ({len(refs)}), значение под маску {masked} не подошло")
    return None


def create_key(session: str, base: str, log: list[str]) -> str | None:
    """Создать ключ и вернуть его полное значение."""
    res = api(
        session,
        "/api/token/",
        "POST",
        {
            "name": KEY_NAME,
            "remain_quota": QUOTA_PER_UNIT,
            "expired_time": -1,
            "unlimited_quota": True,
            "model_limits_enabled": False,
            "model_limits": "",
            "allow_ips": "",
            "group": "default",
        },
    )
    if not (isinstance(res, dict) and res.get("success")):
        log.append(f"ключ не создан: {str(res)[:200]}")
        return None

    rows = [r for r in list_keys(session) if r.get("name") == KEY_NAME]
    if not rows:
        log.append("ключ создан, но в списке его нет")
        return None
    row = max(rows, key=lambda r: r.get("id") or 0)
    value = str(row.get("key") or "")
    if not value:
        log.append("в списке ключей нет поля key")
        return None
    if "*" not in value:
        log.append("cookie-сессия отдала ключ целиком")
        return value if value.startswith("sk-") else f"sk-{value}"

    # Полное значение панель отдаёт отдельным запросом — тем же, что дёргает
    # кнопка копирования на странице ключей (путь подсмотрен в XHR, метод POST:
    # у GET на этом пути «Invalid URL»).
    res = api(session, f"/api/token/{row.get('id')}/key", "POST")
    data = res.get("data") if isinstance(res, dict) else None
    got = str((data or {}).get("key") or "") if isinstance(data, dict) else ""
    if got and masked_match(value, got):
        log.append("ключ выдан панелью целиком")
        return got if got.startswith("sk-") else f"sk-{got}"

    full = key_by_clipboard(session, base, value, log)
    if full:
        log.append("ключ снят кнопкой копирования")
    return full


def account_info(session: str, log: list[str]) -> dict | None:
    """«ID пользователя», «Токен доступа» и баланс — те же три поля, что Босс
    копирует со страницы /profile руками."""
    res = api(session, "/api/user/self")
    data = res.get("data") if isinstance(res, dict) else None
    if not isinstance(data, dict) or not data.get("id"):
        log.append(f"/api/user/self не отдал профиль: {str(res)[:200]}")
        return None

    access = str(data.get("access_token") or "").strip()
    if not access:
        gen = api(session, "/api/user/token")
        access = str((gen or {}).get("data") or "").strip() if isinstance(gen, dict) else ""
    if not access:
        log.append("токен доступа не выдан")

    unit = QUOTA_PER_UNIT
    status = api(session, "/api/status")
    if isinstance(status, dict) and isinstance(status.get("data"), dict):
        unit = float(status["data"].get("quota_per_unit") or QUOTA_PER_UNIT)
    return {
        "panel_id": int(data["id"]),
        "login": data.get("username") or data.get("display_name"),
        "access_token": access,
        "usd": round(float(data.get("quota") or 0) / unit, 2),
    }


def pending_load() -> list[dict]:
    try:
        items = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
        return items if isinstance(items, list) else []
    except Exception:
        return []


def pending_add(payload: dict, station: dict, log: list[str]) -> None:
    """Отложить голову до следующего прогона.

    Дедуп по паре сайт + github_id: та же голова могла пойти на второй заход
    и завести второй ключ, а в базу нужна одна запись — последняя снятая.
    """
    same = (payload["site_id"], payload["github_id"])
    items = [
        i
        for i in pending_load()
        if ((i.get("payload") or {}).get("site_id"), (i.get("payload") or {}).get("github_id"))
        != same
    ]
    items.append(
        {
            "station": station["name"],
            "login": payload["login"],
            "at": datetime.now().isoformat(timespec="seconds"),
            "payload": payload,
        }
    )
    try:
        write_atomic(PENDING_FILE, json.dumps(items, ensure_ascii=False, indent=1))
        log.append(f"отложено в очередь, допишет следующий прогон (в ней {len(items)})")
    except Exception as exc:
        log.append(f"очередь не записалась: {exc}")


def pending_flush() -> bool:
    """Дописать отложенные головы. `False` — backend молчит, новых голов не берём.

    Порядок по прямому требованию Босса: прогон сперва приводит в порядок то,
    что не доехало до базы, и только потом заводит новые аккаунты. Смысл тот же,
    что у порядка: пока база недостижима, новая голова всё равно ляжет в очередь,
    а деньги на станции будут копиться без учёта.
    """
    items = pending_load()
    if not items:
        return True
    note(f"очередь: отложено {len(items)}, дописываю до новых голов")
    left: list[dict] = []
    for i, it in enumerate(items):
        log: list[str] = []
        got = post_account(it.get("payload") or {}, log)
        tail = f" — {log[0]}" if log else ""
        if got is None:
            # Backend один: если он молчит на первой голове, промолчит и на всех
            # остальных, а каждая попытка стоит до 30 секунд таймаута.
            left.extend(items[i:])
            note(f"очередь: backend не ответил, осталось {len(left)}{tail}")
            break
        note(f"очередь: {it.get('login')} — {'дописан' if got else 'отказ, выбрасываю'}{tail}")
    if len(left) != len(items):
        try:
            write_atomic(PENDING_FILE, json.dumps(left, ensure_ascii=False, indent=1))
        except Exception as exc:
            note(f"очередь не переписалась: {exc}")
    return not left


def save_account(station: dict, gh: dict, info: dict, key: str, log: list[str]) -> str:
    """Запись через наш backend, а не в Supabase напрямую: он же пересчитывает
    main_site.cnt и ловит нарушение уникальности как 409.

    Возвращает `ok`, `queued` (backend молчит, голова отложена) или `no` (отказ
    по делу — повтор его не изменит).
    """
    payload = {
        "site_id": station["site_id"],
        "github_id": gh["id"],
        "login": info["login"] or gh["login"],
        "email": gh.get("email"),
        "token": key,
        "balance": info["usd"],
        "access_token": info["access_token"] or None,
        "panel_id": info["panel_id"],
    }
    got = post_account(payload, log)
    if got is None:
        pending_add(payload, station, log)
        return "queued"
    return "ok" if got else "no"


def post_account(payload: dict, log: list[str]) -> bool | None:
    """`True` — записано, `None` — backend не ответил, `False` — отказ по делу.

    Разделение нужно очереди: молчание лечится повтором, а 409 и 400 — нет,
    и такая голова висела бы в очереди вечно.
    """
    try:
        with httpx.Client(trust_env=False, timeout=30) as c:
            r = c.post(f"{BACKEND}/api/site-accounts", json=payload)
    except httpx.HTTPError as exc:
        log.append(f"backend недоступен: {exc}")
        return None
    if r.status_code >= 400:
        log.append(f"backend {r.status_code}: {r.text[:200]}")
        return False
    return True


def run_one(gh: dict, station: dict, dry: bool, proxy: str = "") -> dict:
    """Один GitHub-аккаунт: сбой на нём не должен уносить остальные."""
    log: list[str] = Journal()
    head = {"gh_id": gh["id"], "gh_login": gh["login"], "station": station["name"]}
    session = f"hv{gh['id']}-{int(time.time())}"
    if proxy:
        _SESSION_PROXY[session] = proxy
        head["proxy"] = proxy.split("@")[-1]
    try:
        if not oauth_register(session, gh, station, log):
            return {**head, "status": "НЕ ВОШЁЛ", "log": log}
        info = account_info(session, log)
        if not info:
            return {**head, "status": "НЕТ ПРОФИЛЯ", "log": log}
        key = create_key(session, station["base"], log)
        if not key:
            return {**head, "status": "НЕТ КЛЮЧА", "log": log, "info": info}
        if dry:
            log.append("--dry: в базу не писал")
            return {**head, "status": "ПРОБА", "log": log, "info": info, "key": key}
        saved = save_account(station, gh, info, key, log)
        if saved != "ok":
            status = "ОТЛОЖЕН" if saved == "queued" else "НЕ СОХРАНЁН"
            return {**head, "status": status, "log": log, "info": info, "key": key}
        return {**head, "status": "OK", "log": log, "info": info, "key": key}
    except Exception as exc:
        log.append(f"сбой: {exc}")
        return {**head, "status": "ОШИБКА", "log": log}
    finally:
        # Закрытие сессии тоже умеет падать (бинаря нет, диск занят), а прогон
        # из-за уборки валиться не должен: сессия всё равно своя на каждый заход.
        try:
            ab(session, "close", timeout=60)
        except Exception as exc:
            log.append(f"сессия не закрылась: {exc}")
        _SESSION_PROXY.pop(session, None)


def render(rows: list[dict], station: dict, started: datetime) -> str:
    ok = [r for r in rows if r["status"] in ("OK", "ПРОБА")]
    held = [r for r in rows if r["status"] == "ОТЛОЖЕН"]
    lines = [
        f"Сбор аккаунтов на {station['name']}, "
        f"{started:%Y-%m-%d %H:%M} — {datetime.now():%H:%M}",
        f"Заведено {len(ok)} из {len(rows)}"
        + (f", отложено до записи {len(held)}" if held else "")
        + f", ссылка {station['aff']}",
        "",
    ]
    for r in rows:
        line = f"{r['gh_id']:>4} {r['gh_login']:<24} {r['status']:<12}"
        info = r.get("info") or {}
        if info:
            line += f" panel_id {info['panel_id']:<8} {info['usd']:>8.2f} $"
        if r.get("key"):
            line += f" {r['key']}"
        lines.append(line)
        if r.get("proxy"):
            lines.append(f"      через {r['proxy']}")
        # Токен доступа печатается только у незаписанных: аккаунт на станции уже
        # создан, и без этой строки повторно его взять нечем — панель отдаёт токен
        # лишь своей cookie-сессии, то есть пришлось бы заново гонять OAuth.
        if r["status"] not in ("OK", "ПРОБА") and (r.get("info") or {}).get("access_token"):
            lines.append(f"      токен доступа {r['info']['access_token']}")
        for note in r["log"]:
            lines.append(f"      {note}")
    return "\n".join(lines) + "\n"


def harvest_one(gh: dict, station: dict, dry: bool, pool: list[str], turn: int) -> dict:
    """Заходы на один аккаунт: наружу отсюда не выходит ни один сбой.

    `run_one` ловит своё, но упасть может и то, что вокруг: битая строка лога,
    пропавший agent-browser. Под кроном прогон обязан дойти до конца списка,
    поэтому в худшем случае голова становится строкой отчёта, а не концом прогона.
    """
    notes: list[str] = []
    row: dict = {}
    for att, proxy in enumerate(proxy_order(pool, turn)):
        note(f"→ {gh['id']} {gh.get('login') or '?'} <{gh.get('email') or '?'}>"
             f" заход {att + 1} через {proxy.split('@')[-1]}")
        try:
            row = run_one(gh, station, dry, proxy)
        except Exception as exc:
            row = {
                "gh_id": gh["id"],
                "gh_login": gh.get("login") or "?",
                "station": station["name"],
                "status": "ОШИБКА",
                "proxy": proxy.split("@")[-1],
                "log": [f"сбой вне захода: {type(exc).__name__}: {exc}"],
            }
        notes += [f"заход {att + 1}: {n}" for n in row["log"]] if att else row["log"]
        if row["status"] not in RETRY_STATUSES:
            break
        # Замок GitHub временем не обойти, и второй адрес его не открывает.
        if any(locked(n) for n in row["log"]):
            break
        fault = station_fault(row["log"])
        if fault:
            row["status"] = NO_STATION
            notes.append("отказала станция, а не адрес — второй заход не делаю")
            note("    отказала станция, а не адрес — второй заход не делаю")
            break
        if att + 1 < ATTEMPTS:
            miss = f"адрес {proxy.split('@')[-1]} не пустил, пробую с другого"
            notes.append(miss)
            note(f"    {miss}")
    row["log"] = notes
    return row


def save_report(rows: list[dict], station: dict, started: datetime, final: bool = False) -> None:
    """Отчёт после каждой головы, чтобы обрыв прогона не унёс уже снятые ключи.

    В `harvest.txt` лежит последний прогон, в историю он дописывается: файл
    последнего прогона крон перезапишет, а ключ и токен доступа невосстановимы.
    Ошибка записи прогон не валит — головы всё равно печатаются в лог крона.
    """
    try:
        text = render(rows, station, started)
        write_atomic(OUT_FILE, text)
        if final and rows:
            HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            with HISTORY_FILE.open("a", encoding="utf-8") as f:
                f.write(text + "\n")
    except Exception as exc:
        note(f"отчёт не записан: {exc}")


def run_station(
    station: dict,
    picked: list[dict],
    started: datetime,
    limit: int,
    dry: bool,
    pool: list[str],
) -> list[dict]:
    """Головы одной станции. Отчёт пишется по ней же, дедлайн общий на прогон."""
    note(f"═══ старт: {station['name']}, голов {len(picked)}, верс. {VERSION}, ссылка {station['aff']}")
    # Какие именно головы взяты — строкой сразу: выборка случайная, и без этого
    # по журналу не понять, шла ли речь об одном и том же аккаунте два прогона подряд.
    note("выбраны: " + ", ".join(f"{g['id']}/{g.get('login') or '?'}" for g in picked))
    rows: list[dict] = []
    for i, gh in enumerate(picked):
        if datetime.now() - started > timedelta(minutes=limit):
            note(f"дедлайн {limit} мин: остальных берёт следующий прогон")
            break

        row = harvest_one(gh, station, dry, pool, i)
        # Неудача не значит ничего плохого ни про ящик, ни про аккаунт: GitHub
        # недосылает письма кому попало. Поэтому просто идём к следующей голове —
        # ни пометок в базе, ни отсрочек. Аккаунт, у которого запись на станции
        # уже есть, а в базе нет, — единственное, что требует рук.
        if row["status"] not in ("OK", "ПРОБА", "ОТЛОЖЕН", NO_STATION, *RETRY_STATUSES):
            row["log"].append("аккаунт на станции создан — дописать в базу руками")

        rows.append(row)
        info = row.get("info") or {}
        note(
            f"{row['gh_id']} {row['gh_login']}: {row['status']}"
            + (f" [{row['proxy']}]" if row.get("proxy") else "")
            + (f" panel_id {info['panel_id']} баланс {info['usd']:.2f} $" if info else "")
        )
        save_report(rows, station, started)

    save_report(rows, station, started, final=True)
    return rows


def main() -> int:
    started = datetime.now()
    args = sys.argv[1:]
    dry = "--dry" in args

    def opt(name: str) -> int | None:
        if name in args:
            i = args.index(name)
            if i + 1 < len(args) and args[i + 1].isdigit():
                return int(args[i + 1])
        return None

    # Станций в аргументах может быть сколько угодно, и они идут по порядку: пул
    # GitHub-голов общий, а свободных под каждую станцию своё число. Кончились
    # головы под первую — прогон переходит ко второй, а не простаивает до конца
    # суток. Одна станция в аргументах ведёт себя как прежде.
    domains = [a for a in args if not a.startswith("--") and "." in a] or ["gorouter.app"]
    count = opt("--count") or 1
    limit = opt("--minutes") or MAX_MINUTES
    trim_journal()
    # Сперва порядок в том, что уже снято, и только потом новые головы.
    if not pending_flush():
        note("backend не отвечает: сперва надо дописать очередь, новых голов не беру")
        return 0
    # Без прокси не ходим вовсе: с домашнего адреса GitHub считает десятки разных
    # логинов подозрительными, а пул аккаунтов у Босса единственный.
    pool = proxy_pool()
    if not pool:
        note("HARVEST_PROXIES пуст — с домашнего адреса GitHub положит пул аккаунтов, выхожу")
        return 1

    note(f"прокси в пуле: {len(pool)} — {', '.join(p.split('@')[-1] for p in pool)}")
    done: list[dict] = []
    for domain in domains:
        if datetime.now() - started > timedelta(minutes=limit):
            note(f"дедлайн {limit} мин: до {domain} прогон не дошёл")
            break
        left = count - len([r for r in done if r["status"] in ("OK", "ПРОБА", "ОТЛОЖЕН")])
        if left <= 0:
            break
        station = retry_db(
            lambda d=domain: load_station(d, opt("--inviter")), f"{domain} не прочиталась"
        )
        if not station:
            continue
        picked = retry_db(
            lambda s=station, n=left: free_github(s, n, opt("--github")), "выборка не вышла"
        )
        if not picked:
            note(f"свободных GitHub-аккаунтов под {domain} нет — иду дальше")
            continue
        done += run_station(station, picked, started, limit, dry, pool)
    ok = [r for r in done if r["status"] in ("OK", "ПРОБА", "ОТЛОЖЕН")]
    held = [r for r in done if r["status"] == "ОТЛОЖЕН"]
    rows = done
    mins = (datetime.now() - started).total_seconds() / 60
    speed = len(ok) / (mins / 60) if mins else 0
    note(
        f"═══ конец: заведено {len(ok)} из {len(rows)} за {mins:.0f} мин"
        + (f", в очереди на запись {len(held)}" if held else "")
        + f" ({speed:.0f} в час), отчёт {OUT_FILE}"
    )
    # Темп считается сразу: сравнивать версии по журналу руками — работа, которую
    # никто делать не станет, а без неё не видно, ускорила правка или замедлила.
    try:
        from scripts.harvest_speed import OUT_FILE as SPEED_FILE
        from scripts.harvest_speed import build

        SPEED_FILE.write_text(build(), encoding="utf-8")
        note(f"темп посчитан: {SPEED_FILE}")
    except Exception as exc:
        note(f"темп не посчитан: {exc}")
    return 0


if __name__ == "__main__":
    # Живой браузер и запись в базу: два экземпляра сразу заведут один и тот же
    # GitHub-аккаунт на станции дважды.
    lock = hold_lock("harvest_accounts")
    if lock is None:
        sys.exit(0)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        # Под кроном трейсбек читать некому, а ненулевой код возврата ничего не
        # объясняет. Причина нужна строкой в том же логе, где идут головы.
        note(f"прогон оборвался: {type(exc).__name__}: {exc}")
        sys.exit(1)
