"""Статусная строка: запас по времени и счётчик субагентов.

Строку рисует Claude Code на каждый ответ, руками её не переберёшь — а обе функции
считают по данным, которые легко перепутать: доли суток и хвост транскрипта.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.statusline import clock, ctx, lasts, money, pending, show, span, station  # noqa: E402


def test_lasts_красит_остаток_времени():
    assert lasts(None) == "—"
    assert lasts(2.4) == "2 д 9 ч"
    assert lasts(5.5 / 24) == "5 ч 30 мин"  # ровно 5 часов ещё не тревога
    assert lasts(4.9 / 24) == "\033[93m4 ч 54 мин\033[0m"
    assert lasts(0.9 / 24) == "\033[91m54 мин\033[0m"


def test_ctx_красит_только_за_порогами():
    assert ctx(None) == "ctx —"
    assert ctx(42.3) == "ctx 42 %"
    assert ctx(79.9) == "ctx 80 %"  # округление до 80 цвет ещё не даёт: порог по значению
    assert ctx(80) == "ctx \033[93m80 %\033[0m"
    assert ctx(89.4) == "ctx \033[93m89 %\033[0m"
    assert ctx(90) == "ctx \033[91m90 %\033[0m"


def test_clock_день_недели_и_дата_без_года():
    # 26.08.2026 — среда. Часы покрашены ярко-белым, дата обычным цветом.
    assert clock(datetime(2026, 8, 26, 7, 42)) == "\033[97m07:42\033[0m ср 26 авг"
    assert clock(datetime(2026, 1, 1, 23, 5)) == "\033[97m23:05\033[0m чт 1 янв"


def test_span_разворачивает_доли_суток_в_целые_единицы():
    # «2.4 дня» глазами читается как 2 дня 4 часа, а это 2 дня 9 с половиной часов.
    assert span(2.4) == "2 д 9 ч"
    assert span(3.0) == "3 д"
    assert span(0.28) == "6 ч 43 мин"
    assert span(0.01) == "14 мин"
    assert span(None) == "—"
    assert span(0) == "—"


def test_show_денег():
    assert show(None) == "— $"
    assert show({"known": False}) == "ключ не наш"
    assert show({"known": True, "balance": 536.41, "day_work": 4.25}) == "536 $ / 4 д 6 ч"
    # Балансер ещё не считал запас — цифра денег всё равно нужна.
    assert show({"known": True, "balance": 70, "day_work": None}) == "70 $ / —"


def test_money_помечает_звёздочкой_цифру_из_старого_кэша():
    data = {"known": True, "balance": 536.41, "day_work": 4.25}
    assert money(data, False) == "536 $ / 4 д 6 ч"
    assert money(data, True) == "536 $ / 4 д 6 ч *"
    assert money(None, False) == "— $"


def test_station_показывает_станцию_и_аккаунт_крабом():
    off = "\033[0m"
    green = "\033[38;2;106;153;85m"
    assert station({"station": "gorouter.app", "login": "hoxu"}) == f"{green}🦀 gorouter.app / hoxu{off}"
    # Ключ вне базы: станция неизвестна, но блок всё равно нужен — это сигнал.
    assert station({"known": False}) == f"{green}🦀 станция не наша / ключ вне базы{off}"
    # Backend молчал и кэша нет — блока нет вовсе, прочерк тут ничего не сообщает.
    assert station(None) == ""


def line(content: list[dict]) -> str:
    return json.dumps({"message": {"content": content}})


def test_pending_считает_только_незакрытых_субагентов():
    tail = "\n".join(
        [
            line([{"type": "tool_use", "name": "Agent", "id": "a1"}]),
            line([{"type": "tool_use", "name": "Agent", "id": "a2"}]),
            line([{"type": "tool_result", "tool_use_id": "a1"}]),
            # Не субагент: обычные инструменты в счёт не идут.
            line([{"type": "tool_use", "name": "Bash", "id": "b1"}]),
            "мусорная строка, не json",
        ]
    )
    assert pending(tail) == 1


def test_pending_без_субагентов():
    assert pending("") == 0
    assert pending(line([{"type": "tool_use", "name": "Read", "id": "r1"}])) == 0
