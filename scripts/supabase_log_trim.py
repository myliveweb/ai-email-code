#!/usr/bin/env python3
"""Держит логи Supabase Analytics в рамках: обрезает журнал Kong, когда он раздулся.

Источник `cloudflare.logs.prod` — это построчный журнал каждого HTTP-запроса к Supabase.
За пять недель он набрал 43 млн строк и 59 ГБ, то есть съел место быстрее, чем сами данные
проекта (139 МБ). Мы этот журнал никогда не читали, поэтому его обрезка потерь не несёт,
а вот заполненный под ноль корень останавливает и базу, и сбор аккаунтов.
"""

import subprocess
import sys
from datetime import datetime
from pathlib import Path

CONTAINER = "supabase_db_ai-work"
SOURCE = "cloudflare.logs.prod"
LIMIT_GB = 8.0
LOG = Path(__file__).resolve().parent.parent / "log" / "supabase_log_trim.txt"


def psql(db: str, sql: str) -> str:
    out = subprocess.run(
        ["docker", "exec", CONTAINER, "psql", "-U", "postgres", "-d", db, "-tAc", sql],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "psql вернул ошибку")
    return out.stdout.strip()


def main() -> int:
    token = psql("_supabase", f"select token from _analytics.sources where name = '{SOURCE}'")
    if not token:
        print(f"источник {SOURCE} не найден — нечего чистить")
        return 0

    table = "_analytics.log_events_" + token.replace("-", "_")
    size_bytes = int(psql("_supabase", f"select pg_total_relation_size('{table}')"))
    size_gb = size_bytes / 1024**3

    if size_gb < LIMIT_GB:
        print(f"{table}: {size_gb:.2f} ГБ — ниже порога {LIMIT_GB} ГБ, не трогаем")
        return 0

    psql("_supabase", f"TRUNCATE {table}")
    freed = size_gb
    line = f"{datetime.now():%Y-%m-%d %H:%M} обрезан {table}, освобождено {freed:.2f} ГБ\n"
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(line)
    print(line.strip())
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"не вышло: {exc}", file=sys.stderr)
        sys.exit(1)
