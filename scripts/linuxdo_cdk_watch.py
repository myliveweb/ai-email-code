"""
Разведчик раздач на linux.do: что дают, сколько осталось и куда идти забирать.

Форум большой и целиком китайский, поэтому скрипт делает за Босса три вещи:
отбирает из тега 福利羊毛 только темы про LLM-станции, вытаскивает из них ссылки
`cdk.linux.do/receive/<uuid>` (сам код в постах не пишут — он выдаётся сервисом
раздачи), и по каждой ссылке узнаёт, жива ли раздача. В отчёт попадают цифры
и русские пометки, а не иероглифы.

Две сессии, обе от Босса, обе обязательны:

    data/linux.do_cookies.json      — форум (важен `_t`, живёт ~2 месяца)
    data/cdk.linux.do_cookies.json  — сервис раздач (`linux_do_cdk_session_id`)

Ходить нужно через curl_cffi с отпечатком Chrome. Cloudflare у linux.do смотрит
не только на User-Agent, но и на TLS-фингерпринт: у requests и urllib он чужой,
и вместо JSON приходит `403 cf-mitigated: challenge` — нерегулярно, что особенно
путает. С impersonate="chrome" ответ стабильный.

Скрипт ничего не забирает. Получение кода расходует раздачу и делается руками
по ссылке из отчёта.

Рассчитан на крон:

    uv run python scripts/linuxdo_cdk_watch.py
"""
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.parse import quote

from curl_cffi import requests as cffi

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.app.config import allow_direct_localhost  # noqa: E402
from backend.app.supabase_client import get_supabase  # noqa: E402
from llm.translator import translate, translate_posts  # noqa: E402
from scripts.notify_ui import TOPIC_LINUXDO, notify_ui  # noqa: E402
from scripts.single import hold_lock  # noqa: E402

allow_direct_localhost()

FORUM = "https://linux.do"
CDK = "https://cdk.linux.do"
TAG = "福利羊毛"
PAGES = 3
# Возраст темы — только чтобы не опрашивать сервис о сотнях старых uuid. Судит
# о живости раздачи не дата темы, а сам cdk (`is_completed`, `end_time`): у CUN.AI
# окно было 8 часов, но темы в теге появляются раз в несколько дней, и при окне
# в 3 дня отчёт выходил пустым — проверять было нечего.
MAX_AGE_DAYS = 14
OUT_FILE = ROOT / "log" / "linuxdo_cdk.txt"
# То же самое структурой — это читает вкладка «linux.do» во frontend.
JSON_FILE = ROOT / "log" / "linuxdo_cdk.json"
# Закрытые раздачи помним, чтобы не спрашивать сервис о них на каждом прогоне.
STATE_FILE = ROOT / "log" / "linuxdo_cdk_state.json"
FORUM_COOKIES = ROOT / "data" / "linux.do_cookies.json"
CDK_COOKIES = ROOT / "data" / "cdk.linux.do_cookies.json"
PAUSE = 1.5
# `cf_clearance` в обоих экспортах выдан под браузер Босса и к его User-Agent привязан:
# со своим UA curl_cffi получает `403 cf-mitigated: challenge` даже с верным отпечатком
# TLS. Мажорную версию менять при обновлении Chrome, как в gorouter_balance.py.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
# `cf_clearance` привязан не только к User-Agent, но и к внешнему адресу, с которого он
# выдан: Босс ходит на форум через прокси, и из Москвы напрямую Cloudflare отвечает
# `403 cf-mitigated: challenge` при вполне живых cookies. В оболочке прокси задан
# переменными окружения, а у крона их нет — отсюда брались 403 каждые десять минут.
# Поэтому адрес берётся из `.env` явно, а не из окружения.
PROXY_URL = os.getenv("PROXY_URL") or ""

# Тема годится, если в заголовке есть хоть одно из этих слов.
GOOD = (
    "公益", "兑换码", "额度", "api", "中转", "key", "镜像", "注册送", "邀请码",
    "claude", "opus", "gpt", "token", "codex", "kiro", "grok", "gemini", "deepseek",
    "图片", "绘图", "画图", "生图", "跑图", "视频", "image", "video", "sora", "veo",
    "nano-banana", "midjourney", "flux",
)
# И не годится, если есть эти: тег 福利羊毛 наполовину состоит из раздач VPN-трафика
# и бытовых купонов, к LLM отношения не имеющих.
BAD = (
    "节点", "机场", "订阅链接", "汉堡", "甜筒", "咖啡", "外卖", "话费", "视频会员",
    "网易云", "美团", "汽水", "京东", "淘宝",
)
# Пометки вместо перевода: Боссу нужен смысл раздачи, а не текст заголовка.
LABELS = {
    "兑换码": "промокод на баланс",
    "邀请码": "инвайт-код",
    "注册送": "бонус за регистрацию",
    "额度券": "купон на квоту",
    "激活码": "ключ активации",
    "抽奖": "лотерея",
    "永久": "навсегда",
    "公益站": "公益-станция (раздаёт бесплатно)",
    "中转": "прокси-шлюз (перепродажа)",
    "镜像": "зеркало",
    "倍率": "множитель тарифа",
    "已领完": "уже разобрано",
    "已结束": "закончилось",
    "拉闸": "владелец закрыл доступ",
    "用完": "запас исчерпан",
}
# Метки, которые сами говорят, что идти уже некуда: такая тема опускается в самый низ,
# даже если сервис раздач про неё ничего не знает.
CLOSED_MARKS = ("уже разобрано", "закончилось", "владелец закрыл доступ", "запас исчерпан")
# Окно свежести для верхней таблицы. Живую раздачу подтверждает сервис, а по темам без
# него судить приходится по возрасту и отсутствию меток закрытия. Семь суток — половина
# окна отбора: при трёх верхняя таблица почти всегда пуста, темп тега скромный.
HOT_DAYS = 7
# Прогонов 144 в сутки, и нужны они только как история темпа тега. Полгода — предел,
# за которым цифры уже ни о чём: у Босса растёт trust level, тем в теге станет больше.
RUN_KEEP_DAYS = 180
# Сколько знаков тела держим в базе. Простыни на форуме — это правила, реклама в подписи
# и таблицы моделей; смысл раздачи всегда в начале поста.
BODY_LIMIT = 8000
# Сколько новых тел переводим за прогон. Тело — отдельный запрос к модели по 20-40 секунд,
# а шаг крона 10 минут: остаток доберут следующие прогоны, темы живут днями.
BODY_PER_RUN = 6
MONEY = re.compile(r"(\d+(?:\.\d+)?)\s*(?:刀|\$|＄|美元|美金|u)", re.I)
PIECES = re.compile(r"(\d+)\s*[张份个]")
CDK_LINK = re.compile(r"https?://cdk\.linux\.do/receive/([0-9a-f-]{36})")
# Домены, которые в постах есть всегда и станциями не являются.
SKIP_DOMAINS = (
    "linux.do", "ldstatic.com", "github.com", "t.me", "imgur", "qq.com",
    "alipay", "bilibili", "youtube", "google", "cloudflare", "githubusercontent",
    "workers.dev",
)
DOMAIN = re.compile(r"https?://([a-z0-9.-]+\.[a-z]{2,})", re.I)
# Разметка Discourse в `cooked`: блочные теги дают перевод строки, картинки — пометку,
# остальные теги выбрасываются. Служебное (script/style) уходит целиком со своим текстом.
HTML_DROP = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)
HTML_IMG = re.compile(r"<img[^>]*>", re.I)
HTML_BREAK = re.compile(r"</(?:p|div|li|tr|h[1-6]|blockquote|pre)>|<br\s*/?>", re.I)
HTML_TAG = re.compile(r"<[^>]+>")
FLAT_SPACE = re.compile(r"[ \t\u00a0]+")
BLANK_RUN = re.compile(r"\n{3,}")


def make_session(cookie_file: Path) -> cffi.Session:
    if not cookie_file.exists():
        raise SystemExit(f"нет файла cookies: {cookie_file}")
    jar = json.loads(cookie_file.read_text())
    s = cffi.Session(impersonate="chrome", trust_env=False)
    if PROXY_URL:
        s.proxies = {"http": PROXY_URL, "https": PROXY_URL}
    for c in jar:
        # Домен обязателен: без него curl не узнаёт в ответном Set-Cookie ту же самую
        # запись и завёл бы вторую — на форум ушли бы две `_t`, и обе стали бы негодны.
        s.cookies.set(c["name"], c["value"], domain=c.get("domain") or ".linux.do")
    s.headers.update({
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "Referer": FORUM + "/",
    })
    s.cookie_file = cookie_file
    return s


def persist_cookies(session: cffi.Session) -> None:
    """Вернуть в файл значения, которые сервер поменял за время прогона.

    Discourse ротирует `_t` при каждом обращении и старое значение вскоре гасит.
    Экспорт из браузера поэтому живёт минуты, а не заявленные в нём месяцы: до
    следующего запуска крона токен уже мёртв, и отчёт собирался анонимом. Дописывая
    новое значение обратно, сессия продлевает себя сама.
    """
    cookie_file: Path | None = getattr(session, "cookie_file", None)
    if cookie_file is None:
        return
    fresh = {c.name: c for c in session.cookies.jar}
    saved = json.loads(cookie_file.read_text())
    changed = False
    for item in saved:
        got = fresh.pop(item["name"], None)
        if got is None or got.value == item["value"]:
            continue
        item["value"] = got.value
        if got.expires:
            item["expirationDate"] = float(got.expires)
        changed = True
    for name, got in fresh.items():
        entry = {"name": name, "value": got.value, "domain": got.domain, "path": got.path}
        if got.expires:
            entry["expirationDate"] = float(got.expires)
        saved.append(entry)
        changed = True
    if not changed:
        return
    tmp = cookie_file.with_suffix(cookie_file.suffix + ".tmp")
    tmp.write_text(json.dumps(saved, ensure_ascii=False, indent=2))
    tmp.replace(cookie_file)


def api(session: cffi.Session, url: str, tries: int = 3):
    """GET с повтором: Cloudflare изредка отдаёт challenge даже верному клиенту."""
    for attempt in range(tries):
        try:
            r = session.get(url, timeout=30)
        except Exception as exc:  # сеть моргнула — та же тактика, что и с 403
            if attempt == tries - 1:
                return {"err": str(exc)}
            time.sleep(5)
            continue
        if r.status_code == 200:
            persist_cookies(session)
            try:
                return r.json()
            except ValueError:
                return {"err": "не JSON"}
        if attempt == tries - 1:
            return {"err": f"HTTP {r.status_code}"}
        time.sleep(6)


def reject_reason(title: str) -> str | None:
    """Причина отсева темы или None у годной.

    Причина уезжает в базу: словари GOOD/BAD приходится править, и по причинам видно,
    что именно они выбросили. Из файлов это знание испарялось каждые десять минут.
    """
    low = title.lower()
    for bad in BAD:
        if bad in low:
            return f"слово {bad}"
    if not any(good in low for good in GOOD):
        return "нет слов GOOD"
    return None


def hints(text: str) -> list[str]:
    out = [ru for cn, ru in LABELS.items() if cn in text]
    money, pieces = numbers(text)
    if money:
        out.insert(0, "суммы: " + ", ".join(f"{m:g} $" for m in money[:3]))
    if pieces:
        out.insert(0, f"штук: {pieces}")
    return out


def numbers(text: str) -> tuple[list[float], int | None]:
    """Суммы по убыванию и наибольшее число штук — те же цифры, что и в метках.

    В базу они едут числами рядом с метками: из строки «суммы: 500 $» не отсортировать,
    а «где больше денег» — главный вопрос к этому отчёту.
    """
    money = sorted({float(m) for m in MONEY.findall(text)}, reverse=True)
    pieces = sorted({int(p) for p in PIECES.findall(text)}, reverse=True)
    return money, (pieces[0] if pieces else None)


def station(text: str) -> str:
    """Домен станции из поста. Первый чужой домен — обычно он и есть."""
    for host in DOMAIN.findall(text):
        low = host.lower()
        if not any(skip in low for skip in SKIP_DOMAINS):
            return low
    return ""


def topics(forum) -> tuple[list[dict], list[dict], int]:
    """Темы тега: отобранные, отсеянные с причиной и число просмотренных."""
    edge = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    out, dropped, seen = [], [], set()
    for page in range(1, PAGES + 1):
        url = f"{FORUM}/tag/{quote(TAG)}/l/latest.json?page={page}"
        data = api(forum, url)
        if "err" in data:
            print(f"страница {page}: {data['err']}")
            continue
        for t in data.get("topic_list", {}).get("topics", []):
            if t["id"] in seen:
                continue
            seen.add(t["id"])
            born = datetime.fromisoformat(t["created_at"].replace("Z", "+00:00"))
            why = reject_reason(t["title"]) or (
                f"старше {MAX_AGE_DAYS} дней" if born < edge else None)
            if why:
                dropped.append({"id": t["id"], "title": t["title"], "born": born, "why": why})
                continue
            out.append({"id": t["id"], "title": t["title"], "born": born})
        time.sleep(PAUSE)
    return out, dropped, len(seen)


def first_post(forum, topic_id: int) -> str:
    data = api(forum, f"{FORUM}/t/{topic_id}.json?track_visit=false")
    if "err" in data:
        return ""
    posts = data.get("post_stream", {}).get("posts") or []
    return posts[0].get("cooked", "") if posts else ""


def plain_post(cooked: str) -> str:
    """Первый пост простым текстом: абзацы сохранены, разметка выброшена.

    Переводить и показывать нужно текст, а в `cooked` половина знаков — обвязка Discourse
    (lightbox у картинок, onebox у ссылок, теги эмодзи). Текст ссылок при этом остаётся:
    в постах адрес станции обычно и написан текстом, по нему же ищется домен.
    """
    text = HTML_DROP.sub(" ", cooked)
    text = HTML_IMG.sub("\n[картинка]\n", text)
    text = HTML_BREAK.sub("\n", text)
    text = unescape(HTML_TAG.sub("", text))
    text = "\n".join(FLAT_SPACE.sub(" ", line).strip() for line in text.split("\n"))
    return BLANK_RUN.sub("\n\n", text).strip()[:BODY_LIMIT]


def giveaway(cdk, uuid: str) -> dict:
    """Состояние раздачи. Это чтение метаданных, код оно не расходует."""
    data = api(cdk, f"{CDK}/api/v1/projects/{uuid}")
    if "err" in data:
        return {"err": data["err"]}
    if data.get("error_msg"):
        return {"err": data["error_msg"]}
    return data.get("data") or data


def our_sites() -> dict[str, int]:
    """Домен → id сайта в main_site. Домен нужен для отчёта, id — для ссылки в базе."""
    try:
        rows = get_supabase().table("main_site").select("id, name").execute().data or []
    except Exception as exc:
        print(f"main_site недоступен: {exc}")
        return {}
    return {r["name"].lower().removeprefix("https://").strip("/"): r["id"] for r in rows}


def load_closed() -> set[str]:
    """Погашенные раздачи. Рабочий носитель — база, файл остался для чтения глазами."""
    rows = (
        get_supabase().table("linux_do_cdk").select("uuid").eq("closed", True).execute().data
        or []
    )
    return {r["uuid"] for r in rows}


def load_bodies(ids: list[int]) -> dict[int, dict]:
    """Готовые переводы тел из базы — это и есть кэш: перевод стоит запроса к модели.

    Ключ свежести — `ru_body_of`, sha1 текста, с которого перевод сделан. Совпал — берём
    как есть; разошёлся (пост поправили) — переводим заново: правка первого поста обычно
    и означает «уже разобрали» или «продлил окно», то есть ровно то, что нужно знать.
    """
    if not ids:
        return {}
    rows = (
        get_supabase().table("linux_do_topic")
        .select("topic_id, ru_body, ru_body_of, ru_body_at")
        .in_("topic_id", ids)
        .execute()
        .data
        or []
    )
    return {r["topic_id"]: r for r in rows if r.get("ru_body")}


def save_topics(rows: list[dict], dropped: list[dict]) -> None:
    """Отобранные темы — слиянием, отсеянные — только новыми записями.

    Отсеянные пишутся `ignore_duplicates`, потому что причина отсева может появиться
    у темы, которая раньше была отобрана (постарела, поправили словарь). Слияние
    затёрло бы ей перевод, метки и группу — то есть всё, за чем её и брали.
    """
    sb = get_supabase()
    if rows:
        sb.table("linux_do_topic").upsert(rows, on_conflict="topic_id").execute()
    if dropped:
        sb.table("linux_do_topic").upsert(
            [
                {
                    "topic_id": d["id"],
                    "title": d["title"],
                    "born": d["born"].isoformat(),
                    "url": f"{FORUM}/t/{d['id']}",
                    "kind": "rejected",
                    "rejected": d["why"],
                }
                for d in dropped
            ],
            on_conflict="topic_id",
            ignore_duplicates=True,
        ).execute()


def topic_row(card: dict, sites: dict[str, int], stamp: str) -> dict:
    """Карточка вкладки → строка `linux_do_topic`. `first_seen_at` не передаём намеренно:
    при слиянии он затёрся бы, а знать, когда тема попалась впервые, полезно."""
    dom = card["station"] or None
    return {
        "topic_id": card["topic_id"],
        "title": card["title"],
        "born": card["born"],
        "url": card["url"],
        "kind": card["group"],
        "hot": card["hot"],
        "rejected": None,
        "station": dom,
        "site_id": sites.get(dom) if dom else None,
        "marks": card["marks"],
        "max_amount": card["max_amount"],
        "pieces": card["pieces"],
        "ru_useful": card["useful"] or None,
        "ru_literal": card["literal"] or None,
        "ru_model": card["model"] or None,
        "ru_of_title": card["title"] if card["useful"] else None,
        "ru_at": stamp if card["useful"] else None,
        "body": card["body"] or None,
        "ru_body": card["body_ru"] or None,
        "ru_body_of": card["body_of"] or None,
        "ru_body_at": card["body_at"] or None,
        "last_seen_at": stamp,
    }


def save_cdks(rows: list[dict]) -> None:
    if rows:
        get_supabase().table("linux_do_cdk").upsert(rows, on_conflict="uuid").execute()


def save_run(who: dict, my_tl: int, seen_total: int, picked: int,
             with_links: int, closed: int) -> None:
    """Строка истории плюс уборка: прогонов 144 в сутки, полгода — предел полезного."""
    sb = get_supabase()
    sb.table("linux_do_run").insert({
        "account": who.get("username", "?"),
        "trust_level": my_tl,
        "seen_total": seen_total,
        "picked": picked,
        "with_links": with_links,
        "closed_known": closed,
    }).execute()
    edge = (datetime.now(timezone.utc) - timedelta(days=RUN_KEEP_DAYS)).isoformat()
    sb.table("linux_do_run").delete().lt("started_at", edge).execute()


def moment(value: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def when(value: str) -> str:
    dt = moment(value)
    return dt.astimezone().strftime("%d.%m %H:%M") if dt else (value or "?")


def describe(g: dict, my_tl: int) -> tuple[str, list[str]]:
    """Строки отчёта плюс приговор: live (идти) | over (уже никогда) | unknown."""
    if "err" in g:
        return "unknown", [f"состояние неизвестно: {g['err']}"]
    lines = []
    total = g.get("total_items")
    if total is not None:
        lines.append(f"кодов всего: {total}")
    price = g.get("price")
    if price:
        lines.append(f"цена номинала: {price}")
    need = g.get("minimum_trust_level")
    if need is not None:
        mark = "проходим" if my_tl >= need else f"НЕ проходим, у нас TL{my_tl}"
        lines.append(f"нужен trust level {need} — {mark}")
    if g.get("allow_same_ip") is False:
        lines.append("один код на IP")
    lines.append(f"окно: {when(g.get('start_time'))} → {when(g.get('end_time'))}")

    now = datetime.now(timezone.utc)
    end = moment(g.get("end_time"))
    start = moment(g.get("start_time"))
    # `is_completed` говорит только про исчерпание кодов: у раздач CUN.AI с окном,
    # закрывшимся две недели назад, флаг так и остался false. Поэтому срок судим сами.
    if end and end < now:
        lines.append("окно закрылось")
        return "over", lines
    if g.get("is_completed"):
        lines.append("коды разобраны")
        return "over", lines
    if need is not None and my_tl < need:
        return "unknown", lines
    if start and start > now:
        lines.append("ещё не началась")
        return "unknown", lines
    return "live", lines


def main() -> None:
    forum = make_session(FORUM_COOKIES)
    cdk = make_session(CDK_COOKIES)
    cdk.headers.update({"Referer": CDK + "/"})

    me = api(forum, f"{FORUM}/session/current.json")
    if "err" in me:
        error_date = datetime.now()
        raise SystemExit(
            f"{error_date}: сессия форума не авторизована ({me['err']}) — обновить "
            f"{FORUM_COOKIES}. Форум в браузере после экспорта лучше закрыть: он крутит "
            f"`_t` со своей стороны и гасит выписанное нами значение"
        )
    who = me.get("current_user", {})
    my_tl = who.get("trust_level", 0)

    closed_before = load_closed()
    sites = our_sites()
    now_iso = datetime.now().astimezone().isoformat()

    live_blocks, dead_blocks, other_blocks = [], [], []
    items, cdk_rows = [], []
    picked, dropped, seen_total = topics(forum)
    # Перевод одной пачкой до цикла: пачка дешевле, чем заголовок за запрос, а кэш
    # переводчика гасит повторы между прогонами крона.
    ru = translate([t["title"] for t in picked])
    ru_bodies = load_bodies([t["id"] for t in picked])
    with_links = 0
    for t in picked:
        post = first_post(forum, t["id"])
        body = plain_post(post)
        digest = hashlib.sha1(body.encode()).hexdigest() if body else ""
        was = ru_bodies.get(t["id"]) or {}
        kept = was.get("ru_body", "") if was.get("ru_body_of") == digest else ""
        uuids = list(dict.fromkeys(CDK_LINK.findall(post)))
        dom = station(post)
        marks = hints(t["title"] + " " + post)
        money, pieces = numbers(t["title"] + " " + post)
        head = [
            f"тема: {t['title']}",
            f"{FORUM}/t/{t['id']}  (создана {t['born'].astimezone():%d.%m %H:%M})",
        ]
        if t["title"] in ru:
            head.insert(1, f"перевод: {ru[t['title']]['useful']}")
        if dom:
            known = "уже в main_site" if dom in sites else "в базе нет"
            head.append(f"станция: {dom} — {known}")
        if marks:
            head.append("метки: " + "; ".join(marks))

        card = {
            "topic_id": t["id"],
            "title": t["title"],
            "useful": (ru.get(t["title"]) or {}).get("useful", ""),
            "literal": (ru.get(t["title"]) or {}).get("literal", ""),
            "model": (ru.get(t["title"]) or {}).get("model", ""),
            "url": f"{FORUM}/t/{t['id']}",
            "born": t["born"].astimezone().isoformat(),
            "station": dom,
            "known": bool(dom and dom in sites),
            "marks": marks,
            "max_amount": money[0] if money else None,
            "pieces": pieces,
            "body": body,
            "body_ru": kept,
            "body_of": digest if kept else "",
            "body_at": was.get("ru_body_at", "") if kept else "",
            "state": [],
            "cdk": "",
        }

        # Раздачи через cdk нет, но станция и денежные метки есть — такая тема всё равно
        # стоит взгляда: бонус за регистрацию выдаётся на самом сайте, без сервиса раздач.
        if not uuids:
            shut = any(m in CLOSED_MARKS for m in marks)
            if dom and dom not in sites and marks and not shut:
                other_blocks.append("\n".join(head))
                items.append(card | {"group": "other"})
            elif shut:
                # Владелец сам написал, что закрыл или что запас кончился — сервис раздач
                # про такую тему ничего не знает, но идти туда уже некуда.
                items.append(card | {"group": "dead"})
            else:
                # В текстовый отчёт такая тема не идёт, а на вкладку идёт: перевод уже
                # сделан, и Боссу дешевле прочесть строку, чем открывать форум.
                items.append(card | {"group": "plain"})
            continue
        with_links += 1

        fresh_uuid = False
        for uuid in uuids:
            if uuid in closed_before:
                continue
            fresh_uuid = True
            g = giveaway(cdk, uuid)
            verdict, lines = describe(g, my_tl)
            block = head + lines + [f"забрать: {CDK}/receive/{uuid}"]
            (live_blocks if verdict == "live" else dead_blocks).append("\n".join(block))
            items.append(card | {
                "group": "live" if verdict == "live" else "dead",
                "state": lines,
                "cdk": f"{CDK}/receive/{uuid}",
            })
            cdk_rows.append({
                "uuid": uuid,
                "topic_id": t["id"],
                "url": f"{CDK}/receive/{uuid}",
                "verdict": verdict,
                "closed": verdict == "over",
                "state": lines,
                "start_time": g.get("start_time") or None,
                "end_time": g.get("end_time") or None,
                "min_trust": g.get("minimum_trust_level"),
                "is_completed": g.get("is_completed"),
                "total_items": g.get("total_items"),
                "price": str(g["price"]) if g.get("price") else None,
                "checked_at": now_iso,
            })
            if verdict == "over":
                closed_before.add(uuid)
            time.sleep(PAUSE)
        if not fresh_uuid:
            items.append(card | {"group": "dead", "state": ["раздача закрыта на прошлых прогонах"]})

    # Тела переводим после обхода: каждое — отдельный запрос к модели, и за прогон их берётся
    # столько, сколько влезает в шаг крона. Перевод раскладывается по всем карточкам темы:
    # у темы с двумя раздачами карточек две, а первый пост у них один.
    need: dict[int, str] = {}
    for c in items:
        if c["body"] and not c["body_ru"]:
            need.setdefault(c["topic_id"], c["body"])
    fresh_ru = translate_posts(dict(list(need.items())[:BODY_PER_RUN]))
    for c in items:
        got = fresh_ru.get(c["topic_id"])
        if got:
            c["body_ru"] = got
            c["body_of"] = hashlib.sha1(c["body"].encode()).hexdigest()
            c["body_at"] = now_iso

    stamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    ru_ready = len({c["topic_id"] for c in items if c["body_ru"]})
    report = [
        f"Раздачи linux.do на {stamp}. Аккаунт {who.get('username', '?')}, TL{my_tl}.",
        f"Просмотрено тем: {seen_total}, про LLM и свежих: {len(picked)}, "
        f"отсеяно: {len(dropped)}, со ссылкой на раздачу: {with_links}. "
        f"Закрытых помним: {len(closed_before)}.",
        f"Тела постов переведены у {ru_ready} тем из {len(picked)}, "
        f"на этом прогоне переведено {len(fresh_ru)}.",
        "",
    ]
    if live_blocks:
        report.append(f"=== ЖИВЫЕ: {len(live_blocks)} ===")
        report.append("")
        report.append("\n\n".join(live_blocks))
    else:
        report.append("Живых раздач нет.")
    if dead_blocks:
        report += ["", f"=== закрытые и недоступные: {len(dead_blocks)} ===", "",
                   "\n\n".join(dead_blocks)]
    if other_blocks:
        report += ["", f"=== станции без сервиса раздач, глянуть глазами: "
                       f"{len(other_blocks)} ===", "", "\n\n".join(other_blocks)]

    OUT_FILE.parent.mkdir(exist_ok=True)
    OUT_FILE.write_text("\n".join(report) + "\n")
    # Верхняя таблица вкладки: живая раздача — всегда, тема без сервиса раздач — пока свежая.
    # Считается здесь, а не во фронте: возраст и метки закрытия известны только тут.
    edge = datetime.now().astimezone() - timedelta(days=HOT_DAYS)
    for c in items:
        c["hot"] = c["group"] == "live" or (c["group"] == "other" and moment(c["born"]) >= edge)
    order = {"live": 0, "other": 1, "plain": 2, "dead": 3}
    # Сначала свежие, потом стабильной сортировкой по группе — внутри «живых»,
    # «глянуть глазами» и «закрытых» порядок остаётся от новых к старым.
    items.sort(key=lambda c: c["born"], reverse=True)
    items.sort(key=lambda c: order[c["group"]])
    JSON_FILE.write_text(json.dumps({
        "stamp": datetime.now().astimezone().isoformat(),
        "account": who.get("username", "?"),
        "trust_level": my_tl,
        "seen_total": seen_total,
        "picked": len(picked),
        "with_links": with_links,
        "closed": len(closed_before),
        "items": items,
    }, ensure_ascii=False, indent=1))
    STATE_FILE.write_text(json.dumps({"closed": sorted(closed_before)}, indent=1))
    # База — рабочий носитель, файлы выше остались для чтения глазами и как страховка.
    # Одна строка на тему: у темы с несколькими раздачами берём лучшую группу (порядок
    # тот же, что на вкладке), `hot` — если горяча хоть одна её карточка.
    best: dict[int, dict] = {}
    for c in items:
        cur = best.get(c["topic_id"])
        if cur is None:
            best[c["topic_id"]] = dict(c)
            continue
        if order[c["group"]] < order[cur["group"]]:
            cur["group"] = c["group"]
        cur["hot"] = cur["hot"] or c["hot"]
    save_topics([topic_row(c, sites, now_iso) for c in best.values()], dropped)
    save_cdks(cdk_rows)
    save_run(who, my_tl, seen_total, len(picked), with_links, len(closed_before))
    notify_ui(TOPIC_LINUXDO)
    print("\n".join(report))


if __name__ == "__main__":
    # Шаг крона 10 минут, перевод заголовков и опрос сервиса раздач бывают дольше.
    if hold_lock("linuxdo_cdk_watch") is None:
        sys.exit(0)
    main()
