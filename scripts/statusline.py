#!/usr/bin/env python3
"""Статусная строка Claude Code: деньги активной станции, контекст, субагенты, часы.

Босс работает на наших же аккаунтах, поэтому в строке первым делом остаток и запас
по времени активного ключа — те же цифры, что в таблице аккаунтов (`balance`
и `day_work`, их считает крон-скрипт балансов). Дальше загрузка контекстного окна,
число незакрытых субагентов и часы. Каталог и модель не выводятся: Босс и так знает,
где он и на чём.

Строка рисуется на каждый ответ ассистента, поэтому цифры денег кэшируются в `/tmp`
на `CACHE_TTL`: балансер обновляет их раз в 15 минут, а лишний поход в backend
задерживает отрисовку. Пустой вывод гасит строку, поэтому все сбои проглатываются
и превращаются в прочерк.
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ACTIVE_URL = "http://127.0.0.1:4000/api/claude/active"
CACHE = Path("/tmp/claude-statusline-active.json")
CACHE_TTL = 60
# Хвост транскрипта, по которому считаются субагенты: файл сессии дорастает до
# десятков мегабайт, а незакрытые вызовы лежат в самом конце.
TAIL_BYTES = 400_000
# Как субагент назван в транскрипте: в 2.1.x это `Agent`, в документации и прежних
# версиях — `Task`. Проверено по журналам сессий: пишется `Agent`.
AGENT_NAMES = ("Agent", "Task")
SEP = " · "
# Имена дня и месяца своими списками, а не через `%a`/`%b`: у крона и у оболочки Босса
# локаль разная, и на C-локали в строку уехало бы `Wed`.
DAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
MONTHS = (
    "янв",
    "фев",
    "мар",
    "апр",
    "мая",
    "июн",
    "июл",
    "авг",
    "сен",
    "окт",
    "ноя",
    "дек",
)
# Ярко-жёлтый, ярко-красный и ярко-белый (90-е коды): обычные 33/31/37 на тёмной
# теме Босса тусклые.
YELLOW = "\033[93m"
RED = "\033[91m"
WHITE = "\033[97m"
# Умеренный зелёный: блок станции — справка, а не предупреждение, и жёлтый с красным
# в строке уже заняты порогами денег и контекста.
GREEN = "\033[38;2;106;153;85m"
OFF = "\033[0m"
CRAB = "🦀"


def active() -> tuple[dict | None, bool]:
    """Ответ `/api/claude/active` и признак того, что цифра из кэша устарела.

    Один поход в backend на всю строку: деньги и станцию берём из одного ответа.
    Кэш не выбрасывается по сроку, а только обновляется: когда backend лежит,
    прочерк вместо денег хуже вчерашней цифры, поэтому она печатается со звёздочкой.
    """
    cached = None
    try:
        if CACHE.exists():
            cached = json.loads(CACHE.read_text(encoding="utf-8"))
            if time.time() - CACHE.stat().st_mtime < CACHE_TTL:
                return cached, False
    except Exception:
        cached = None
    try:
        # Локальный backend через внешний proxy недостижим — обходим окружение.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(ACTIVE_URL, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        CACHE.write_text(json.dumps(data), encoding="utf-8")
        return data, False
    except Exception:
        return cached, cached is not None


def money(data: dict | None, stale: bool) -> str:
    """`536 $ / 4 д 6 ч` — остаток активного ключа и на сколько его хватит."""
    return f"{show(data)} *" if stale else show(data)


def station(data: dict | None) -> str:
    """`🦀 gorouter.app / login` — на чьи деньги идёт сессия, тем же цветом, что в меню."""
    if not data:
        return ""
    name = data.get("station") or "станция не наша"
    login = data.get("login") or "ключ вне базы"
    return f"{GREEN}{CRAB} {name} / {login}{OFF}"


def show(data: dict | None) -> str:
    if not data:
        return "— $"
    if not data.get("known"):
        return "ключ не наш"
    bal = data.get("balance")
    left = f"{float(bal):.0f} $" if bal is not None else "— $"
    return f"{left} / {lasts(data.get('day_work'))}"


def lasts(days: float | None) -> str:
    """Запас по времени, с 5 часов жёлтый, с одного — красный: пора поворачивать ключ."""
    text = span(days)
    if not days:
        return text
    hours = float(days) * 24
    color = RED if hours < 1 else YELLOW if hours < 5 else ""
    return f"{color}{text}{OFF}" if color else text


def span(days: float | None) -> str:
    """Дни работы целыми единицами: «2.4 дня» глазами читается как 2 дня 4 часа."""
    if not days:
        return "—"
    total = round(float(days) * 24 * 60)
    d, h, m = total // 1440, (total % 1440) // 60, total % 60
    if d:
        return f"{d} д {h} ч" if h else f"{d} д"
    return (f"{h} ч {m} мин" if m else f"{h} ч") if h else f"{m} мин"


def subagents(path: str | None) -> str:
    """Сколько субагентов запущено и не отчитались: вызовы `Agent` без результата."""
    if not path:
        return ""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - TAIL_BYTES))
            tail = f.read().decode("utf-8", errors="replace")
    except Exception:
        return ""
    live = pending(tail)
    return f"субагент {live}" if live else ""


def pending(tail: str) -> int:
    """Незакрытые вызовы субагентов в хвосте транскрипта."""
    started: set[str] = set()
    done: set[str] = set()
    for line in tail.splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        for block in (rec.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") in AGENT_NAMES:
                started.add(str(block.get("id")))
            elif block.get("type") == "tool_result":
                done.add(str(block.get("tool_use_id")))
    return len(started - done)


def clock(now: datetime) -> str:
    """`07:42 ср 26 авг` — часы белым, день недели и дата обычным цветом."""
    return f"{WHITE}{now:%H:%M}{OFF} {DAYS[now.weekday()]} {now.day} {MONTHS[now.month - 1]}"


def ctx(used: float | None) -> str:
    """`ctx 42 %`, с 80 % жёлтым, с 90 % красным: до порогов цвет только мешает."""
    if used is None:
        return "ctx —"
    val = float(used)
    color = RED if val >= 90 else YELLOW if val >= 80 else ""
    return f"ctx {color}{val:.0f} %{OFF if color else ''}"


def main() -> int:
    try:
        data_in = json.loads(sys.stdin.read() or "{}")
    except Exception:
        data_in = {}

    data, stale = active()
    parts = [money(data, stale)]
    parts.append(ctx((data_in.get("context_window") or {}).get("used_percentage")))
    agents = subagents(data_in.get("transcript_path"))
    if agents:
        parts.append(agents)
    parts.append(clock(datetime.now()))
    where = station(data)
    if where:
        parts.append(where)
    print(SEP.join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
