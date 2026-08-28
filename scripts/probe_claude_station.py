"""
Проверка станции на пригодность для Claude Code, а не «отвечает ли модель вообще».

Обычный тест «модель жива» здесь бесполезен: seekai.cc полмесяца назад отвечал 200 и
осмысленным текстом, но Claude Code на нём терял инструменты — а это единственное, что
делает его агентом. Поэтому проверяются ровно те три свойства, на которых он ломается:

    1. Anthropic-эндпоинт /v1/messages. Claude Code ходит туда, а не в /v1/chat/completions,
       и станция может держать один и не держать другой.
    2. Сохранность tool_use. Модель просят вызвать инструмент; в ответе должен быть блок
       type="tool_use" со разобранным JSON и stop_reason="tool_use". Если станция режет
       инструменты, приходит обычный текст — формально успех, практически потеря агента.
    3. Чистота ответа. Станции-перепродавцы приклеивают к ответу свои заголовки и рекламу.
       Для человека это шум, для парсера — сломанный ответ, поэтому текст сверяется
       с ожидаемым словом и печатается целиком.

Плюс стабильность: запросы идут серией, и разброс времени с числом отказов видно в сводке —
«то зависнет, то ответит» не ловится одним удачным вызовом.

Цена берётся не из прайса, а из разницы used_quota до и после прогона: прайс станции — это
заявление, а разность — факт.

    uv run python scripts/probe_claude_station.py seekai.cc claude-opus-4-8
    uv run python scripts/probe_claude_station.py seekai.cc claude-opus-4-8 --rounds 5
"""
import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.app.config import allow_direct_localhost  # noqa: E402

allow_direct_localhost()

TIMEOUT = 120
# Cloudflare у gorouter.app отдаёт challenge-страницу с кодом 403 на дефолтный UA requests —
# вместо ответа шлюза приходит HTML. Версия Chrome — константа, меняется с браузером Босса.
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/139.0.0.0 Safari/537.36")
# Инструмент выбран так, чтобы вызов был единственным разумным поведением: модель не может
# посмотреть погоду сама, значит либо позовёт инструмент, либо станция его срезала.
TOOL = {
    "name": "get_weather",
    "description": "Узнать погоду в городе. Единственный способ получить погоду.",
    "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string", "description": "Название города"}},
        "required": ["city"],
    },
}
TOOL_PROMPT = "Какая погода в Новосибирске? Ответь, вызвав инструмент."
# Слово-маркер: если станция приклеила к ответу свой заголовок, оно окажется не в начале.
PLAIN_PROMPT = "Ответь ровно одним словом: КОНТРОЛЬ"
PLAIN_EXPECT = "КОНТРОЛЬ"


def load_station(domain: str) -> tuple[str, str, int]:
    from backend.app.supabase_client import get_supabase

    sb = get_supabase()
    site = sb.table("main_site").select("id, meta").eq("name", domain).execute()
    if not site.data:
        raise SystemExit(f"сайта {domain} нет в main_site")
    meta = site.data[0]["meta"] or {}
    base = meta.get("endpoints_openai") or f"https://{domain}/v1"
    if isinstance(base, list):
        base = base[0]
    rows = (
        sb.table("main_site_account")
        .select("id, login, token, balance")
        .eq("site_id", site.data[0]["id"])
        .order("balance", desc=True)
        .execute()
    )
    for row in rows.data:
        token = (row["token"] or "").strip()
        if token:
            return base.rstrip("/"), token, row["id"]
    raise SystemExit(f"ни у одного аккаунта {domain} не заполнен token")


def call_messages(base: str, token: str, model: str, prompt: str, tools: bool) -> dict:
    """Один вызов Anthropic-эндпоинта. Возвращает разбор ответа, а не сырой JSON."""
    body = {
        "model": model,
        "max_tokens": 512,
        "messages": [{"role": "user", "content": prompt}],
    }
    if tools:
        body["tools"] = [TOOL]
    started = time.monotonic()
    try:
        r = requests.post(
            f"{base}/messages",
            headers={
                "x-api-key": token,
                "authorization": f"Bearer {token}",
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
                "User-Agent": UA,
            },
            json=body,
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        return {"ok": False, "elapsed": time.monotonic() - started, "error": type(e).__name__}
    elapsed = time.monotonic() - started
    if r.status_code != 200:
        return {"ok": False, "elapsed": elapsed, "error": f"HTTP {r.status_code}: {r.text[:300]}"}
    try:
        data = r.json()
    except ValueError:
        # Станции-перепродавцы отдают HTML-заглушку с кодом 200 — это не ответ модели.
        return {"ok": False, "elapsed": elapsed, "error": f"не JSON: {r.text[:300]}"}

    blocks = data.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    tool_blocks = [b for b in blocks if b.get("type") == "tool_use"]
    usage = data.get("usage") or {}
    return {
        "ok": True,
        "elapsed": elapsed,
        "text": text,
        "stop_reason": data.get("stop_reason"),
        "tool_names": [b.get("name") for b in tool_blocks],
        "tool_input": tool_blocks[0].get("input") if tool_blocks else None,
        "in_tokens": usage.get("input_tokens"),
        "out_tokens": usage.get("output_tokens"),
        "model": data.get("model"),
    }


def report_round(n: int, res: dict, kind: str) -> None:
    if not res["ok"]:
        print(f"  {n}. {kind:5} {res['elapsed']:5.1f} с  ОТКАЗ  {res['error']}")
        return
    if kind == "tool":
        got = ", ".join(res["tool_names"]) or "—"
        verdict = "tool_use" if res["tool_names"] else "ИНСТРУМЕНТ СРЕЗАН"
        extra = f" input={json.dumps(res['tool_input'], ensure_ascii=False)}" if res["tool_input"] else ""
        tail = f"  текст рядом: {res['text'].strip()[:120]!r}" if res["text"].strip() else ""
        print(f"  {n}. tool  {res['elapsed']:5.1f} с  {verdict}  ({got}) "
              f"stop={res['stop_reason']}{extra}{tail}")
    else:
        clean = res["text"].strip()
        verdict = "чисто" if clean == PLAIN_EXPECT else "ПРИМЕСЬ"
        print(f"  {n}. plain {res['elapsed']:5.1f} с  {verdict}  ответ={clean!r} "
              f"stop={res['stop_reason']} tokens={res['in_tokens']}/{res['out_tokens']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Пригодность станции для Claude Code")
    ap.add_argument("domain")
    ap.add_argument("model")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--wait", type=int, default=0, metavar="МИНУТ",
                    help="ждать, пока станция отбивает генерацию по перегрузке (503)")
    args = ap.parse_args()

    base, token, acc_id = load_station(args.domain)
    print(f"станция {args.domain}, {base}, аккаунт id={acc_id}, модель {args.model}")
    print(f"ключ …{token[-6:]}, раундов {args.rounds}\n")

    # seekai.cc отбивает всю генерацию 503 «system memory overloaded» — это состояние сервера,
    # а не отказ ключа, и проходит само. Ждать имеет смысл только его, прочие отказы окончательны.
    if args.wait:
        deadline = time.monotonic() + args.wait * 60
        while time.monotonic() < deadline:
            probe = call_messages(base, token, args.model, PLAIN_PROMPT, False)
            if probe["ok"] or "overload" not in str(probe.get("error", "")):
                break
            left = int((deadline - time.monotonic()) / 60)
            print(f"  станция занята ({probe['error'][:80]}), жду, осталось ~{left} мин")
            time.sleep(60)

    results = []
    for n in range(1, args.rounds + 1):
        for kind, prompt, tools in (
            ("tool", TOOL_PROMPT, True),
            ("plain", PLAIN_PROMPT, False),
        ):
            res = call_messages(base, token, args.model, prompt, tools)
            res["kind"] = kind
            results.append(res)
            report_round(n, res, kind)
        print()

    tool_runs = [r for r in results if r["kind"] == "tool"]
    plain_runs = [r for r in results if r["kind"] == "plain"]
    ok = [r for r in results if r["ok"]]
    times = [r["elapsed"] for r in ok]

    print("=== сводка")
    print(f"успешных ответов: {len(ok)} из {len(results)}")
    if times:
        spread = f", разброс {statistics.stdev(times):.1f} с" if len(times) > 1 else ""
        print(f"время: медиана {statistics.median(times):.1f} с, "
              f"мин {min(times):.1f}, макс {max(times):.1f}{spread}")
    kept = sum(1 for r in tool_runs if r["ok"] and r["tool_names"])
    print(f"tool_use сохранён: {kept} из {len(tool_runs)}")
    clean = sum(1 for r in plain_runs if r["ok"] and r["text"].strip() == PLAIN_EXPECT)
    print(f"ответ без примесей: {clean} из {len(plain_runs)}")

    verdict = []
    if not ok:
        # Без единого ответа судить о tool_use и примесях нельзя: 503 «system memory
        # overloaded» — это перегрузка станции, а не срезанные инструменты.
        print("вердикт: проверить не удалось, станция не ответила ни разу")
        return
    if len(ok) < len(results):
        verdict.append("нестабильна")
    if kept < len([r for r in tool_runs if r["ok"]]):
        verdict.append("режет инструменты — для Claude Code не годится")
    if clean < len([r for r in plain_runs if r["ok"]]):
        verdict.append("подмешивает текст в ответ")
    print("вердикт: " + ("; ".join(verdict) if verdict else "пригодна"))


if __name__ == "__main__":
    main()
