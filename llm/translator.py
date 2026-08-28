"""
Переводчик китайского на русский для отчётов по linux.do.

Форум, с которого мы берём раздачи, целиком китайский, и машинный перевод там
бесполезен: половина смысла лежит в сленге, который переводится буквально и
получается бессмыслица. `拉闸` — не «дёрнуть рубильник», а «владелец закрыл
доступ»; `机场` — не «аэропорт», а VPN-сервис. Поэтому переводит модель,
и в промпте ей выдан глоссарий форума.

Два стиля, оба нужны:

    literal — дословный, порядок слов сохранён. Нужен как контроль: по нему
              видно, где «полезный» перевод отошёл от текста и начал сочинять.
    useful  — как сказал бы русский, который сам следит за раздачами. Именно
              он идёт в отчёт.

Тело первого поста переводит `translate_posts()` — по одному посту за запрос и одним
стилем: у заголовка контроль дословным переводом нужен (он короткий, и по расхождению
видно сочинительство), а простыню в двух вариантах Босс читать не станет. Кэша у постов
здесь нет: перевод и хэш исходника лежат в базе рядом с темой, и переводится только
правленый пост — правка как раз обычно и означает «уже разобрали».

Модель выбрана A/B/C-тестом на пяти живых заголовках (22.08.2026):

    ag/gemini-3.7-flash-medium  22 с, 4258 токенов — единственная, кто пишет
                                живым русским («Лавочка закрыта»), взята основной
    cx/gpt-5.6-luna             15 с, 1364 токена — точна, но местами сочиняет;
                                оставлена резервом на случай 503 у основной
    xai/grok-4.6                59 с, 7840 токенов — втрое дороже и путает 8月
                                (август) с «8 месяцев»; отклонена

Китайские модели (deepseek-v4, qwen3.6/3.8, glm-5.3) на наших станциях в тот день
не отвечали вовсе — HTTP/2 reset, 503 и model_disabled. Проверить их стоит позже:
на китайском тексте у них должно быть преимущество.

Ключ и адрес шлюза берутся из базы, а не из .env: аккаунты станций живут
в `main_site_account_custom`, и там же меняются, когда станция отваливается.
"""
import json
import re
from pathlib import Path

from curl_cffi import requests as cffi
from loguru import logger

from backend.app.supabase_client import get_supabase

# Станция и модели. Порядок в MODELS — порядок попыток: станции падают в 503
# охотнее, чем хотелось бы, и терять из-за этого весь отчёт незачем.
STATION = "anymodel.org"
MODELS = ("ag/gemini-3.7-flash-medium", "cx/gpt-5.6-luna")
# Переведённое помним: крон крутится каждые 15 минут, а темы в теге живут днями.
# Без кэша те же 16 заголовков переводились бы 96 раз в сутки.
CACHE_FILE = Path(__file__).resolve().parent.parent / "log" / "linuxdo_titles_ru.json"
TIMEOUT = 300
# Заголовков за раз. Больше — дешевле, но при обрыве теряется вся пачка.
BATCH = 12
# Сколько знаков поста отдаём модели. Посты с раздачами короткие, а простыни на 20 тысяч
# знаков — это правила форума и реклама в подписи: смысл раздачи лежит в начале.
POST_LIMIT = 6000

GLOSSARY = """薅羊毛 — выжимать бонусы и промокоды; 福利 — раздача, халява
拉闸 — «дёрнуть рубильник», владелец закрыл доступ
号池 — пул аккаунтов; 口子 — лазейка, доступ; 倍率 — множитель тарифа
公益站 — станция, раздающая доступ бесплатно; 中转 — прокси-шлюз, перепродажа чужого API
佬友 / 佬 — обращение к участникам форума, «братцы»; L站 — сам форум linux.do
兑换码 — промокод на баланс; 额度 — квота, лимит; 刀 — доллар; 张 / 次 — штук, раз
机场 — VPN-сервис (к LLM отношения не имеет); 已无 / 已领完 — уже разобрали
跑路 — владелец сбежал; 白嫖 — пользоваться бесплатно; 跑图 — генерировать картинки"""

PROMPT = f"""Ты переводишь заголовки тем с китайского форума linux.do на русский.
Тема форума: раздачи доступа к LLM-станциям (перепродажа API OpenAI, Claude, Gemini),
промокоды на баланс, инвайты, бесплатные группы моделей. Много интернет-сленга.

Сленг, который надо раскрывать смыслом, а не буквой:
{GLOSSARY}

Для каждого заголовка верни два перевода:
"literal" — дословный, с сохранением порядка слов, чтобы было видно исходную структуру;
"useful" — как сказал бы русский человек, который следит за такими раздачами: коротко,
по делу, сленг раскрыт смыслом, числа и суммы сохранены, служебные скобки в наличии.

Даты читай китайскими: 8月 — август, а не «8 месяцев».

Верни ТОЛЬКО JSON-массив: [{{"n":1,"literal":"…","useful":"…"}}, …] без пояснений."""

POST_PROMPT = f"""Ты переводишь на русский первый пост темы с китайского форума linux.do.
Тема форума: раздачи доступа к LLM-станциям (перепродажа API OpenAI, Claude, Gemini),
промокоды на баланс, инвайты, бесплатные группы моделей. Много интернет-сленга.

Сленг, который надо раскрывать смыслом, а не буквой:
{GLOSSARY}

Переводи так, как написал бы русский, который сам следит за такими раздачами: по делу,
сленг раскрыт смыслом. Сохрани всё, что нужно, чтобы пойти и забрать: адреса, домены,
ссылки, промокоды, суммы, числа, сроки, условия и требования к аккаунту. Абзацы и списки
исходника сохрани. Даты читай китайскими: 8月 — август, а не «8 месяцев».
Ничего не выдумывай и не додумывай: чего в посте нет, того нет.

Верни ТОЛЬКО перевод, без пояснений, без заголовков «Перевод:» и без разметки ```."""


def _credentials() -> tuple[str, str]:
    """Адрес шлюза и ключ живого аккаунта станции."""
    sb = get_supabase()
    site = sb.table("main_site").select("id, meta").eq("name", STATION).execute().data
    if not site:
        raise RuntimeError(f"в main_site нет станции {STATION}")
    base = (site[0].get("meta") or {}).get("endpoints_openai")
    if not base:
        raise RuntimeError(f"у {STATION} в meta нет endpoints_openai")
    rows = (
        sb.table("main_site_account_custom")
        .select("token")
        .eq("site_id", site[0]["id"])
        .execute()
        .data
    )
    for row in rows:
        if row.get("token"):
            return base.rstrip("/"), row["token"].strip()
    raise RuntimeError(f"у аккаунтов {STATION} нет token — переводить нечем")


def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except ValueError:
            pass
    return {}


def _save_cache(cache: dict) -> None:
    CACHE_FILE.parent.mkdir(exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1))


def _parse(text: str) -> list[dict]:
    """JSON-массив из ответа. Модели любят обернуть его в ```json."""
    body = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        data = json.loads(body)
    except ValueError:
        match = re.search(r"\[.*\]", body, re.S)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return []
    return data if isinstance(data, list) else []


def _chat(base: str, key: str, model: str, system: str, user: str, limit: int) -> str:
    session = cffi.Session(impersonate="chrome", trust_env=False)
    r = session.post(
        f"{base}/chat/completions",
        timeout=TIMEOUT,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.3,
            "max_tokens": limit,
        },
    )
    if r.status_code != 200:
        raise RuntimeError(f"{model}: HTTP {r.status_code} {r.text[:200]}")
    data = r.json()
    if "choices" not in data:
        raise RuntimeError(f"{model}: ответ без choices {str(data)[:200]}")
    return data["choices"][0]["message"]["content"] or ""


def _ask(base: str, key: str, model: str, titles: list[str]) -> list[dict]:
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(titles, 1))
    return _parse(_chat(base, key, model, PROMPT, numbered, 4000))


def translate(titles: list[str]) -> dict[str, dict]:
    """
    {заголовок: {"literal": …, "useful": …}} для всех, кого удалось перевести.

    Молчит и возвращает что есть: отчёт по раздачам нужнее перевода, и падать
    из-за недоступной станции ему незачем.
    """
    cache = _load_cache()
    fresh = [t for t in dict.fromkeys(titles) if t not in cache]
    if not fresh:
        return {t: cache[t] for t in titles if t in cache}

    try:
        base, key = _credentials()
    except RuntimeError as exc:
        logger.warning(f"перевод недоступен: {exc}")
        return {t: cache[t] for t in titles if t in cache}

    for start in range(0, len(fresh), BATCH):
        chunk = fresh[start:start + BATCH]
        for model in MODELS:
            try:
                rows = _ask(base, key, model, chunk)
            except Exception as exc:
                logger.warning(f"перевод {model}: {exc}")
                continue
            if not rows:
                logger.warning(f"перевод {model}: пустой разбор ответа")
                continue
            for row in rows:
                try:
                    title = chunk[int(row["n"]) - 1]
                except (KeyError, ValueError, IndexError):
                    continue
                useful = (row.get("useful") or "").strip()
                if useful:
                    cache[title] = {
                        "literal": (row.get("literal") or "").strip(),
                        "useful": useful,
                        "model": model,
                    }
            break

    _save_cache(cache)
    return {t: cache[t] for t in titles if t in cache}


def translate_posts(posts: dict[int, str]) -> dict[int, str]:
    """{id темы: текст} → {id темы: перевод} для тех, кого удалось перевести.

    Пост переводится по одному за запрос: тела бывают по несколько тысяч знаков,
    и пачкой они не влезают в ответ, а при обрыве терялись бы все сразу. Кэша здесь
    нет — вызывающий сам решает, что переводить: у тем перевод лежит в базе рядом
    с хэшем исходника, и заново переводится только правленый пост.
    """
    if not posts:
        return {}
    try:
        base, key = _credentials()
    except RuntimeError as exc:
        logger.warning(f"перевод постов недоступен: {exc}")
        return {}

    out: dict[int, str] = {}
    for topic_id, text in posts.items():
        for model in MODELS:
            try:
                got = _chat(base, key, model, POST_PROMPT, text[:POST_LIMIT], 8000).strip()
            except Exception as exc:
                logger.warning(f"перевод поста {topic_id} через {model}: {exc}")
                continue
            got = re.sub(r"^```(?:\w+)?|```$", "", got, flags=re.M).strip()
            if got:
                out[topic_id] = got
                break
            logger.warning(f"перевод поста {topic_id} через {model}: пустой ответ")
    return out
