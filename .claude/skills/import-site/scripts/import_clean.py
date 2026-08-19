"""Импорт блоков из <file>_clean.txt в main_site_account через backend API.

Читает отчёт парсера, а не исходник: к этому моменту он уже проверен Боссом.
Значения-заглушки вида `(пусто — ...)` и `??? не найден` считаются пустыми.

Запуск (сначала всегда с --dry-run):
    uv run python .claude/skills/import-site/scripts/import_clean.py \
        source/<file>_clean.txt --site-id 1 --dry-run
    uv run python .claude/skills/import-site/scripts/import_clean.py \
        source/<file>_clean.txt --site-id 1 --limit 1
    uv run python .claude/skills/import-site/scripts/import_clean.py \
        source/<file>_clean.txt --site-id 1

ProxyHandler({}) обязателен: в окружении заданы http_proxy/https_proxy, без
него запрос к локальному backend уходит во внешний прокси и падает.
"""

import argparse
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

BLOCK = re.compile(r"^### БЛОК (\d+)")
KV = re.compile(r"^(\w+)\s*=\s*(.*)$")
EMPTY = ("", "-", "none", "null")

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def is_empty(value: str) -> bool:
    return value.startswith("(") or value.startswith("???") or value.strip().lower() in EMPTY


def read_blocks(path: Path) -> list[dict]:
    blocks: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if m := BLOCK.match(line):
            blocks.append({"_num": int(m.group(1))})
            continue
        if blocks and (m := KV.match(line)):
            value = m.group(2).strip()
            blocks[-1][m.group(1)] = None if is_empty(value) else value
    return blocks


def post(url: str, payload: dict) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener.open(req, timeout=30) as res:
            return res.status, res.read().decode()
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body).get("detail", body)
        except json.JSONDecodeError:
            return e.code, body


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("clean_file", type=Path)
    ap.add_argument("--site-id", type=int, required=True)
    ap.add_argument("--api")
    ap.add_argument("--custom", action="store_true",
                    help="сайты без GitHub: main_site_account_custom, привязка к main_email, поле password")
    ap.add_argument("--no-smart-link", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--offset", type=int, default=0, help="пропустить N блоков — для докатки после сбоя")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    base = "http://127.0.0.1:4000/api/site-accounts-custom" if args.custom else "http://127.0.0.1:4000/api/site-accounts"
    api = args.api or base

    blocks = read_blocks(args.clean_file)[args.offset :][: args.limit]
    ok, failed = 0, []

    for b in blocks:
        payload = {
            "site_id": args.site_id,
            "smart_link": not args.no_smart_link,
            "login": b.get("login"),
            "email": b.get("email"),
            "token": b.get("token"),
            "balance": float(str(b["balance"]).replace(",", ".")) if b.get("balance") else 0,
            "aff": b.get("aff"),
        }
        if args.custom:
            payload["email_id"] = 0  # при smart_link не используется, backend берёт из main_email
            payload["password"] = b.get("password")
        else:
            payload["github_id"] = 0  # при smart_link не используется, backend берёт из main_github
        if args.dry_run:
            print(f"блок {b['_num']:>3}  {json.dumps(payload, ensure_ascii=False)}")
            continue
        status, body = post(api, payload)
        if status < 300:
            ok += 1
        else:
            failed.append((b["_num"], b.get("login"), status, body))
            print(f"блок {b['_num']:>3}  {status}  {b.get('login')}  {body}")

    if args.dry_run:
        print(f"\ndry-run: блоков {len(blocks)}")
        return

    print(f"\nвставлено {ok} из {len(blocks)}, ошибок {len(failed)}")
    for num, login, status, body in failed:
        print(f"  блок {num:>3}  {status}  {login}  {body}")


if __name__ == "__main__":
    main()
