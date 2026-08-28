"""Аналитика темпа сбора аккаунтов: сколько занимает голова и на чём стоит.

Отчёт `harvest.txt` говорит, что вышло, журнал — когда каждый шаг случился, но ни
тот, ни другой не отвечают на вопрос «правка ускорила или замедлила». Здесь журнал
разбирается на прогоны и головы, время головы раскладывается по этапам OAuth,
а прогоны группируются по версии темпа (`VERSION` в `harvest_accounts.py`, метка
`верс. N` в строке старта) — так видно, что дала конкретная правка.

    uv run python scripts/harvest_speed.py [число прогонов]

Пишет `log/harvest_speed.txt`, читает `log/harvest_journal.txt`.
"""

import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOURNAL = ROOT / "log" / "harvest_journal.txt"
OUT_FILE = ROOT / "log" / "harvest_speed.txt"
# Сколько прогонов показывать построчно. Сводка по версиям считается по всему журналу.
SHOW_RUNS = 20

# Этапы головы: подпись и куски строк журнала, по которым этап узнаётся. Порядок
# важен — время этапа это разница со временем предыдущей узнанной строки.
STAGES = (
    ("вход", ("GitHub показал форму входа",)),
    ("запрос кода", ("GitHub требует код подтверждения",)),
    ("код из ящика", ("подтверждение устройства, код",)),
    ("Authorize", ("жму Authorize",)),
    ("впустил", ("GitHub впустил", "GitHub пропустил без вопросов")),
    ("ключ", ("ключ выдан панелью", "ключ снят кнопкой")),
)

TS = re.compile(r"^(\d\d\.\d\d \d\d:\d\d:\d\d) (.*)$")
HEAD = re.compile(r"^→ (\d+) (\S+)")
# Статус «OK» латиницей, остальные по-русски — отсюда оба алфавита в классе.
DONE = re.compile(r"^(\d+) (\S+): ([A-ZА-ЯЁ ]+?)(?: \[|$)")
# Статусы удачной головы. `ОТЛОЖЕН` тоже удачный: аккаунт на станции создан,
# не доехала лишь запись в базу, и следующий прогон её дописывает.
GOOD = ("OK", "ПРОБА", "ОТЛОЖЕН")


def stamp(text: str, year: int) -> datetime:
    """Время строки журнала. Год в журнале не пишется — берём из времени файла."""
    return datetime.strptime(f"{year}.{text}", "%Y.%d.%m %H:%M:%S")


def parse(lines: list[str], year: int) -> list[dict]:
    """Журнал → список прогонов, у каждого головы со временем и этапами."""
    runs: list[dict] = []
    run: dict | None = None
    head: dict | None = None

    for raw in lines:
        m = TS.match(raw.rstrip())
        if not m:
            continue
        when, text = stamp(m.group(1), year), m.group(2).strip()

        if text.startswith("═══ старт:"):
            ver = re.search(r"верс\. (\d+)", text)
            run = {
                "at": when,
                "station": text.split("старт: ")[1].split(",")[0],
                "version": int(ver.group(1)) if ver else 1,
                "heads": [],
                "end": when,
            }
            runs.append(run)
            head = None
            continue
        if run is None:
            continue
        run["end"] = when

        got = HEAD.match(text)
        if got:
            # Второй заход той же головы — продолжение, а не новая голова: Босса
            # интересует, сколько всего съел аккаунт.
            if head and head["id"] == got.group(1) and head["done"] is None:
                head["tries"] += 1
                head["mark"] = when
                continue
            head = {
                "id": got.group(1),
                "login": got.group(2),
                "at": when,
                "mark": when,
                "tries": 1,
                "stages": {},
                "done": None,
                "status": "",
            }
            run["heads"].append(head)
            continue
        if head is None:
            continue

        fin = DONE.match(text)
        if fin and fin.group(1) == head["id"]:
            head["done"] = when
            head["status"] = fin.group(3).strip()
            continue
        for name, marks in STAGES:
            if name not in head["stages"] and any(m in text for m in marks):
                head["stages"][name] = (when - head["mark"]).total_seconds()
                head["mark"] = when
                break
    return runs


def secs(head: dict) -> float:
    return ((head["done"] or head["at"]) - head["at"]).total_seconds()


def mid(values: list[float]) -> float:
    """Медиана: одна застрявшая голова не должна портить картину прогона."""
    if not values:
        return 0.0
    row = sorted(values)
    half = len(row) // 2
    return row[half] if len(row) % 2 else (row[half - 1] + row[half]) / 2


def run_line(run: dict) -> str:
    heads = run["heads"]
    ok = [h for h in heads if h["status"] in GOOD]
    mins = (run["end"] - run["at"]).total_seconds() / 60
    speed = len(ok) / (mins / 60) if mins > 0.1 else 0
    good = mid([secs(h) for h in ok])
    bad = mid([secs(h) for h in heads if h not in ok and h["done"]])
    kinds: dict[str, int] = {}
    for h in heads:
        kinds[h["status"] or "не кончил"] = kinds.get(h["status"] or "не кончил", 0) + 1
    return (
        f"{run['at']:%d.%m %H:%M}  в{run['version']}  {run['station']:<14}"
        f" голов {len(heads):>2}  {mins:>4.0f} мин  {speed:>3.0f}/час"
        f"  удачная {good:>3.0f} с  отказ {bad:>3.0f} с"
        f"  {', '.join(f'{k} {v}' for k, v in sorted(kinds.items()))}"
    )


def by_version(runs: list[dict]) -> list[str]:
    """Сводка по версиям темпа — ради неё всё и заведено."""
    out: list[str] = []
    seen = sorted({r["version"] for r in runs})
    for ver in seen:
        mine = [r for r in runs if r["version"] == ver]
        heads = [h for r in mine for h in r["heads"]]
        ok = [h for h in heads if h["status"] in GOOD]
        mins = sum((r["end"] - r["at"]).total_seconds() for r in mine) / 60
        speed = len(ok) / (mins / 60) if mins > 0.1 else 0
        out.append(
            f"версия {ver}: прогонов {len(mine)}, голов {len(heads)}, удачных {len(ok)}"
            f" ({100 * len(ok) / len(heads) if heads else 0:.0f} %),"
            f" медиана удачной {mid([secs(h) for h in ok]):.0f} с, темп {speed:.0f} в час"
        )
        parts = []
        for name, _ in STAGES:
            vals = [h["stages"][name] for h in ok if name in h["stages"]]
            if vals:
                parts.append(f"{name} {mid(vals):.0f} с")
        if parts:
            out.append("    этапы удачной головы: " + ", ".join(parts))
    return out


def build(limit: int = SHOW_RUNS) -> str:
    if not JOURNAL.exists():
        return "журнала нет — сбор ещё не запускался\n"
    year = datetime.fromtimestamp(JOURNAL.stat().st_mtime).year
    runs = [r for r in parse(JOURNAL.read_text(encoding="utf-8", errors="replace").splitlines(), year) if r["heads"]]
    if not runs:
        return "в журнале нет ни одного прогона с головами\n"

    lines = [
        f"Темп сбора аккаунтов, {datetime.now():%d.%m.%Y %H:%M}",
        f"источник {JOURNAL.name}, прогонов в журнале {len(runs)}",
        "",
        "Сводка по версиям темпа",
    ]
    lines += by_version(runs)
    lines += ["", f"Последние прогоны (до {limit})"]
    lines += [run_line(r) for r in runs[-limit:]]
    lines += [
        "",
        "«удачная» и «отказ» — медиана времени головы; отказ включает все её заходы.",
        "Версия темпа — `VERSION` в scripts/harvest_accounts.py, метка в строке старта.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    limit = next((int(a) for a in sys.argv[1:] if a.isdigit()), SHOW_RUNS)
    text = build(limit)
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(text, encoding="utf-8")
    print(text, end="")
    print(f"→ {OUT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
