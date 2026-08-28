"""Проверка модели настоящей задачей проекта, а не «привет, как дела».

Модели даётся то, чем занят backend: из письма достать отправителя, код
подтверждения и срок действия, вернуть чистый JSON. Слабые модели проходят
вежливый диалог и валятся на структурном выводе — а нужен именно он.

В письме нарочно лежат посторонние шестизначные числа: hex-цвет из CSS и id
трекинг-ссылки. Модель, которая хватает первое попавшееся, проваливается так же,
как проваливалась регулярка без якоря. Правильный ответ ровно один:
code=483920, minutes=10, sender содержит vyceai.

Модели гоняются ПОСЛЕДОВАТЕЛЬНО и малым числом. Причина в цене ошибки: на
aihubmix параллельный прогон 49 моделей сжёг весь аккаунтный лимит на алфавитно
первых именах, и до Gemini очередь не дошла. На части станций за прогон «на
живость» дают случайный бан.

    uv run --no-project python .claude/skills/analyze-site/scripts/bench_model.py \\
        https://api.42w.shop/v1 sk-xxx glm-5.2 [ещё модели] [--pause 6]
"""
import json
import os
import re
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
TIMEOUT = 120
MAX_TOKENS = 300

EMAIL = """From: VyceAI Security <noreply@vyceai.com>
Subject: Your verification code

<div style="background:#4a90e2;color:#1c2f41">
  <p>Hello! Someone requested access to your account.</p>
  <p>Your verification code is <b>483&#8203;920</b></p>
  <p>The code expires in 10 minutes. If this wasn't you, ignore this message.</p>
  <a href="https://track.vyceai.com/c/718264?u=42">Unsubscribe</a>
  <p>Support: +1 555 0199</p>
</div>"""

PROMPT = (
    "Из письма ниже извлеки данные и верни ТОЛЬКО JSON без пояснений и без markdown:\n"
    '{"sender": "<адрес отправителя>", "code": "<код подтверждения>", '
    '"minutes": <через сколько минут истекает, число>}\n\n'
    "Письмо:\n" + EMAIL
)

EXPECT_CODE = "483920"
EXPECT_MINUTES = 10
EXPECT_SENDER = "vyceai"

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def ask(base: str, key: str, model: str) -> tuple[int, object]:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": MAX_TOKENS,
        "temperature": 0,
    }
    req = urllib.request.Request(
        f"{base.rstrip('/')}/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_CTX) as r:
            raw, status = r.read().decode("utf-8", "replace"), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read().decode("utf-8", "replace"), e.code
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"
    try:
        return status, json.loads(raw)
    except Exception:
        return status, raw[:600]


def parse_json(text: str) -> dict | None:
    s = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", s, re.S)
    if fence:
        s = fence.group(1).strip()
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None
    try:
        got = json.loads(m.group(0))
        return got if isinstance(got, dict) else None
    except Exception:
        return None


def verdict(model: str, status: int, res: object) -> None:
    print(f"\n{'-' * 74}\n{model}   HTTP {status}")
    if not isinstance(res, dict):
        print(f"  ОШИБКА: {res}")
        return
    if "error" in res and not res.get("choices"):
        err = res["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        print(f"  ОШИБКА: {msg}")
        print("  no_available_channel — канал у владельца отвалился; "
              "does not exist — имя в каталоге не совпало с именем в шлюзе; "
              "insufficient_user_quota (403) — деньги кончились.")
        return

    rid = str(res.get("id", ""))
    usage = res.get("usage") or {}
    pin, out = usage.get("prompt_tokens"), usage.get("completion_tokens")
    text = ((res.get("choices") or [{}])[0].get("message") or {}).get("content") or ""

    print(f"  id={rid}  prompt_tokens={pin} completion_tokens={out}")
    if "fake" in rid.lower():
        print("  !!! ЗАГЛУШКА: id вида chatcmpl-fake-<timestamp>. Это не ответ модели, "
              "хотя HTTP 200 и content заполнен.")
    if isinstance(pin, int) and pin > 600:
        print(f"  !!! НАКЛАДНЫЕ ТОКЕНЫ: промпт около 190 токенов, списано {pin}. "
              "Провайдер приклеивает свой текст — цена за миллион ничего не значит.")

    got = parse_json(text)
    if got is None:
        if not text.strip():
            tail = " — весь лимит вывода ушёл в скрытые рассуждения" if out and out >= MAX_TOKENS - 10 else ""
            print(f"  пустой ответ{tail}")
        else:
            print("  не JSON")
            if "<think" in text:
                print("  причина: модель рассуждает вслух, чистый JSON не отдаёт")
        print(f"  raw: {text[:400]!r}")
        return

    code = str(got.get("code", "")).replace(" ", "").replace("-", "")
    minutes = got.get("minutes")
    sender = str(got.get("sender", "")).lower()
    ok_code = code == EXPECT_CODE
    ok_min = str(minutes) == str(EXPECT_MINUTES)
    ok_send = EXPECT_SENDER in sender

    if ok_code and ok_min and ok_send:
        print("  ВСЁ ВЕРНО — модель годна")
        return
    print(f"  JSON ок, но code={code!r} minutes={minutes!r} sender={sender!r}")
    if not ok_code:
        print("  подхватила посторонее число (hex-цвет #4a90e2 или id ссылки 718264) — "
              "структуру держит, задачу не решает")
    print(f"  raw: {text[:400]!r}")


def main() -> int:
    args = sys.argv[1:]
    pause = 3.0
    if "--pause" in args:
        i = args.index("--pause")
        pause = float(args[i + 1])
        del args[i:i + 2]
    if len(args) < 3:
        print(__doc__)
        return 1
    base, key, models = args[0], args[1].strip(), args[2:]

    print(f"эталон: code={EXPECT_CODE} minutes={EXPECT_MINUTES} sender~{EXPECT_SENDER}")
    print(f"моделей: {len(models)}, последовательно, пауза {pause} с")
    for n, model in enumerate(models):
        if n:
            time.sleep(pause)
        status, res = ask(base, key, model)
        verdict(model, status, res)
    print("\nСверить request_count в /api/user/self до и после этого прогона — "
          "именно разница показывает, сколько запросов у нас реально есть.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
