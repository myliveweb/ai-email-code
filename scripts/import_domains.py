#!/usr/bin/env python3
"""Свести списки доменов .eu.cc Босса в `main_domain` и проверить их живой DNS-пробой.

Учёт Босс ведёт текстовыми файлами: одни — списки желаний, другие — расписки
о прошедшей регистрации (имя начинается с `ok`). Владение по этим файлам
не восстановить: колонка `status` в `domains.csv` врёт на 24 записях из 137,
поэтому решает делегирование зоны, а не подпись в файле.

    uv run python scripts/import_domains.py [--src КАТАЛОГ] [--dry] [--no-dns]

Статусы: `owned` — наш (расписка `ok-*` либо OWNED с делегированием),
`unknown` — делегирован на NS регистратора, но ни в одном списке «ok» его нет
(так же выглядел бы домен другого клиента Gname), `foreign` — делегирован
на чужие NS, `free` — зона молчит, `lapsed` — расписка есть, а делегирования нет.
"""
import argparse
import csv
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from dotenv import find_dotenv, load_dotenv
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.app.config import allow_direct_localhost  # noqa: E402

load_dotenv(find_dotenv())
allow_direct_localhost()

SRC_DEFAULT = Path.home() / "Загрузки" / "last"
NS_CACHE = ROOT / "log" / "domains_ns.json"
REPORT = ROOT / "log" / "domains_import.txt"

DOM = re.compile(r"\b([a-z0-9][a-z0-9-]{0,61}\.eu\.cc)\b", re.I)
# имя файла, начинающееся с `ok`, Босс ставит распиской: регистрация прошла
OK_PREFIXES = ("ok-register", "ok_priority_queue")
FILE_DATE = re.compile(r"(\d{2})-(\d{2})-(\d{4})")
PRICE = re.compile(r"\$\s*(\d+(?:\.\d+)?)")
RESALE = re.compile(r"\$\s*(\d+)\s*-\s*\$?\s*(\d+)")
OWN_NS = ("share-dns.com", "share-dns.net")  # штатная пара Gname
RESOLVERS = ("1.1.1.1", "9.9.9.9", "8.8.8.8")


def collect(src: Path) -> dict[str, dict]:
    """Разобрать каталог с файлами Босса в записи по домену."""
    rows: dict[str, dict] = {}

    def add(dom: str) -> dict:
        return rows.setdefault(dom.lower(), {"domain": dom.lower(), "sources": []})

    csv_path = src / "domains.csv"
    if csv_path.exists():
        with csv_path.open(encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                dom = (r.get("domain") or "").strip()
                if not dom:
                    continue
                cur = add(dom)
                cur["sources"].append(csv_path.name)
                cur["category"] = (r.get("category") or "").strip() or None
                cur["market"] = (r.get("market") or "").strip() or None
                pri = (r.get("priority") or "").strip()
                cur["priority"] = int(pri) if pri.isdigit() else None
                cur["csv_status"] = (r.get("status") or "").strip().upper() or None
                band = RESALE.search(r.get("resale_estimate") or "")
                if band:
                    cur["resale_min"] = float(band.group(1))
                    cur["resale_max"] = float(band.group(2))
                note = (r.get("notes") or "").strip()
                if note:
                    cur["note"] = note

    for f in sorted(src.iterdir()):
        if not f.is_file() or f.name == "domains.csv":
            continue
        if f.suffix.lower() not in {".txt", ".csv", ".md"}:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning(f"не прочитан {f.name}: {exc}")
            continue
        ok = f.name.startswith(OK_PREFIXES)
        stamp = file_date(f)
        for line in text.splitlines():
            found = DOM.findall(line)
            if not found:
                continue
            # данные в начале строки, личные пометки Босса после `---`
            tail = line.split("---", 1)[1].strip() if "---" in line else ""
            for dom in found:
                cur = add(dom)
                cur["sources"].append(f.name)
                if ok:
                    cur["registered"] = True
                    cur.setdefault("registered_at", stamp)
                if tail:
                    apply_tail(cur, tail)

    for r in rows.values():
        r["sources"] = sorted(set(r["sources"]))
        r.setdefault("registered", False)
    return rows


def file_date(f: Path) -> str:
    """Дата регистрации: из имени файла, иначе по времени правки."""
    m = FILE_DATE.search(f.name)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return datetime.fromtimestamp(f.stat().st_mtime).date().isoformat()


def apply_tail(cur: dict, tail: str) -> None:
    """Хвост после `---` — не мусор, а признак: цена значит «выставлен на продажу»."""
    cur["note"] = tail if not cur.get("note") else f"{cur['note']}; {tail}"
    price = PRICE.search(tail)
    if price and not RESALE.search(tail):
        cur["for_sale"] = True
        cur["sale_price"] = float(price.group(1))
        if re.search(r"gname", tail, re.I):
            cur["marketplace"] = "GName Market"


def ns_lookup(domain: str) -> list[str] | None:
    """NS домена. Пустой список — зона молчит, None — не ответил ни один резолвер."""
    for res in RESOLVERS:
        try:
            out = subprocess.run(
                ["dig", "+short", "+time=3", "+tries=1", "NS", domain, f"@{res}"],
                capture_output=True, text=True, timeout=12, check=False).stdout
        except (subprocess.SubprocessError, OSError):
            continue
        lines = [x.strip(" .") for x in out.split("\n") if x.strip() and "error" not in x]
        if lines:
            return sorted(lines)
        if out.strip() == "":
            return []
    return None


def resolve_all(rows: dict[str, dict], use_dns: bool) -> None:
    cache = {}
    if NS_CACHE.exists():
        cache = json.loads(NS_CACHE.read_text(encoding="utf-8"))
    if not use_dns:
        for dom, r in rows.items():
            hit = cache.get(dom)
            r["ns"] = hit["ns"] if hit else None
            r["ns_checked_at"] = hit["at"] if hit else None
        return

    def job(dom: str) -> tuple[str, list[str] | None]:
        return dom, ns_lookup(dom)

    with ThreadPoolExecutor(max_workers=40) as pool:
        done = dict(pool.map(job, rows))
    now = datetime.now(UTC).isoformat()
    for dom, r in rows.items():
        found = done[dom]
        if found is None:  # резолверы молчат — держимся прежнего знания
            hit = cache.get(dom)
            r["ns"] = hit["ns"] if hit else None
            r["ns_checked_at"] = hit["at"] if hit else None
            continue
        r["ns"] = found
        r["ns_checked_at"] = now
        cache[dom] = {"ns": found, "at": now}
    NS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    NS_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def classify(r: dict) -> str:
    ns = r.get("ns")
    ours_ns = bool(ns) and all(any(n.endswith(s) for s in OWN_NS) for n in ns)
    if r["registered"]:
        return "owned" if ns else "lapsed"
    if not ns:
        return "free"
    if not ours_ns:
        return "foreign"
    # NS регистратора — так же выглядел бы домен любого другого клиента Gname
    return "owned" if r.get("csv_status") == "OWNED" else "unknown"


def payload(r: dict) -> dict:
    ns = r.get("ns")
    ours_ns = bool(ns) and all(any(n.endswith(s) for s in OWN_NS) for n in ns)
    return {
        "domain": r["domain"],
        "status": r["status"],
        "ns": ns,
        "ns_checked_at": r.get("ns_checked_at"),
        "registrar": "Gname" if ours_ns else None,
        "category": r.get("category"),
        "market": r.get("market"),
        "priority": r.get("priority"),
        "resale_min": r.get("resale_min"),
        "resale_max": r.get("resale_max"),
        "for_sale": bool(r.get("for_sale")),
        "marketplace": r.get("marketplace"),
        "sale_price": r.get("sale_price"),
        "note": r.get("note"),
        "sources": r["sources"],
        "registered_at": r.get("registered_at"),
        "updated_at": datetime.now(UTC).isoformat(),
    }


def write_db(rows: list[dict]) -> int:
    from backend.app.supabase_client import get_supabase

    sb = get_supabase()
    saved = 0
    for i in range(0, len(rows), 200):
        chunk = [payload(r) for r in rows[i:i + 200]]
        sb.table("main_domain").upsert(chunk, on_conflict="domain").execute()
        saved += len(chunk)
        logger.info(f"записано {saved} из {len(rows)}")
    return saved


def render(rows: list[dict]) -> str:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    names = {"owned": "наши", "unknown": "нужно подтверждение Босса",
             "foreign": "чужие", "free": "свободны", "lapsed": "расписка есть, зона молчит"}
    out = [f"Домены .eu.cc — разбор {datetime.now().strftime('%d.%m %H:%M')}",
           f"всего в списках: {len(rows)}", ""]
    for st, n in sorted(counts.items(), key=lambda x: -x[1]):
        out.append(f"  {n:5}  {st:8} — {names.get(st, st)}")
    sale = [r for r in rows if r.get("for_sale")]
    if sale:
        out += ["", f"выставлены на продажу ({len(sale)}):"]
        out += [f"  {r['domain']:24} {r.get('sale_price')} $ "
                f"{r.get('marketplace') or ''}".rstrip() for r in sorted(
                    sale, key=lambda r: -(r.get("sale_price") or 0))]
    unknown = [r["domain"] for r in rows if r["status"] == "unknown"]
    if unknown:
        out += ["", f"делегированы на NS Gname, но расписки нет ({len(unknown)}):"]
        out += ["  " + ", ".join(unknown)]
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=SRC_DEFAULT)
    ap.add_argument("--dry", action="store_true", help="не писать в базу")
    ap.add_argument("--no-dns", action="store_true", help="взять NS из кэша")
    args = ap.parse_args()

    if not args.src.is_dir():
        logger.error(f"каталог не найден: {args.src}")
        return 1

    rows = collect(args.src)
    logger.info(f"собрано доменов: {len(rows)}")
    resolve_all(rows, use_dns=not args.no_dns)
    for r in rows.values():
        r["status"] = classify(r)

    ordered = sorted(rows.values(), key=lambda r: r["domain"])
    report = render(ordered)
    print(report)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(report, encoding="utf-8")

    if args.dry:
        logger.info("--dry: в базу не пишу")
        return 0
    write_db(ordered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
