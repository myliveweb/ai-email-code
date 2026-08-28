"""Где взять модель: ищет её по main_site.meta и показывает цену и деньги на аккаунтах.

    uv run python scripts/find_model.py deepseek

Структурный прайс лежит в meta.price ({"in","out"|"per_request","status"}), но у части
сайтов модели упомянуты только в прозе (meta.groups у ai.fujcloud.com) — поэтому вторым
проходом ищем подстроку в текстовых значениях meta и показываем такие сайты отдельно.
"""

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.app.config import allow_direct_localhost  # noqa: E402

# urllib читает http_proxy из окружения, и запрос к локальному backend уходит во внешний
# proxy, где обрывается на RemoteDisconnected. Лечится тем же способом, что и в остальных
# скриптах проекта.
allow_direct_localhost()

API = "http://127.0.0.1:4000/api"


def get(path: str):
    return json.load(urllib.request.urlopen(f"{API}{path}", timeout=20))


def price_str(p: dict, currency: str) -> str:
    if "per_request" in p:
        return f"{p['per_request']} {currency}/запрос"
    return f"{p.get('in')}/{p.get('out')} {currency} за 1M"


def prose_hits(meta: dict, needle: str) -> list[str]:
    hits = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(node, str) and needle in node.lower():
            hits.append(f"{path}: {node}")

    walk({k: v for k, v in meta.items() if k != "price"}, "")
    return hits


def main(needle: str) -> None:
    needle = needle.lower()
    for site in get("/sites"):
        meta = site.get("meta") or {}
        currency = meta.get("currency", "?")
        matched = {k: v for k, v in (meta.get("price") or {}).items() if needle in k.lower()}
        prose = prose_hits(meta, needle)
        if not matched and not prose:
            continue

        stats = get(f"/sites/{site['id']}/stats")
        print(f"\n=== {site['name']}  ({stats['total_count']} акк., "
              f"{stats['total_balance']:.2f} {currency})")
        if endpoint := meta.get("endpoints_openai"):
            print(f"    {endpoint}")
        for name, p in sorted(matched.items(), key=lambda kv: kv[1].get("in", 0)):
            status = p.get("status", "?")
            tier = f" tier={p['tier']}" if p.get("tier") not in (None, "free") else ""
            print(f"    {name:26} {price_str(p, currency):24} {status}{tier}")
        for hit in prose:
            print(f"    (в прозе) {hit}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Использование: find_model.py <часть имени модели>")
    main(sys.argv[1])
