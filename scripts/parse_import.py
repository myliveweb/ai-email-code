"""Разбор рукописного файла импорта на блоки.

Правило: значение для базы стоит в начале строки, всё после `---` — рукописные
пометки Босса, в базу не идут.

На выходе три файла:
  _clean.txt     — блоки, разобранные без вопросов
  _questions.txt — блоки, где остались неясности
  _errors.txt    — найденные пометки брака (bad / suspended) для main_github
"""

import re
import sys
from pathlib import Path

SEP = re.compile(r"^\s*[=\-]{5,}\s*$")
LOGIN = re.compile(r"^([A-Za-z0-9._-]{3,})\s+---\s*GitHub\b\s*(.*)$")
TOKEN = re.compile(r"^(sk-[A-Za-z0-9_-]{20,})\b")
BALANCE = re.compile(r"^([\d]+(?:[.,]\d+)?)\s*\$\s*$")
AFF = re.compile(r"^(https://\S*sign-up\?aff=\w+)\b")
BARE_ID = re.compile(r"^([A-Za-z0-9._%+-]+(?:@[A-Za-z0-9.-]+\.[A-Za-z]{2,})?)$")
MARKED_ID = re.compile(
    r"^([A-Za-z0-9._%+-]+(?:@[A-Za-z0-9.-]+\.[A-Za-z]{2,})?)\s+---\s*(bad email|bad|suspended)\b",
    re.IGNORECASE,
)
DEDUP_PAIR = re.compile(r"^([A-Za-z0-9._-]+)\s+-\s+([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})$")
NOISE = re.compile(
    r"^(x-llm\.net|https://x-llm\.net/(pricing|v1)|My Key|\d+\s+моделей.*)(\s+---.*)?$"
)

ACTION = {"bad": "Bad Rambler Email", "bad email": "Bad Rambler Email", "suspended": "Suspended Github"}


def parse_block(lines: list[str]) -> dict:
    rec: dict = {
        "login": None,
        "email": None,
        "token": None,
        "balance": None,
        "aff": None,
        "_doubts": [],
        "_errors": [],
        "_unknown": [],
    }

    for ln in lines:
        s = ln.strip()
        if not s:
            continue

        if m := LOGIN.match(s):
            if rec["login"]:
                rec["_unknown"].append(f"{s}   <-- !!! второй login в блоке")
                continue
            rec["login"] = m.group(1)
            continue

        if m := TOKEN.match(s):
            rec["token"] = m.group(1)
            continue

        if m := BALANCE.match(s):
            rec["balance"] = float(m.group(1).replace(",", "."))
            continue

        if m := AFF.match(s):
            rec["aff"] = m.group(1)
            continue

        if m := MARKED_ID.match(s):
            rec["_errors"].append((m.group(1), ACTION[m.group(2).lower()]))
            continue

        if NOISE.match(s) or BARE_ID.match(s) or DEDUP_PAIR.match(s):
            continue

        rec["_unknown"].append(s)

    return rec


def render(rec: dict, num: int, start: int, end: int) -> str:
    out = [
        f"### БЛОК {num}   (строки {start}-{end} исходника)",
        f"login   = {rec['login'] or '??? не найден'}",
        f"email   = {rec['email'] or '(пусто — доберём умной привязкой по login)'}",
        f"token   = {rec['token'] or '??? не найден'}",
        f"balance = {rec['balance'] if rec['balance'] is not None else '??? не найден'}",
        f"aff     = {rec['aff'] or '??? не найдена'}",
    ]
    for d in rec["_doubts"]:
        out.append(f"#   ??? {d}")
    for u in rec["_unknown"]:
        out.append(f"#   ??? НЕ ПОНЯЛ, что это: {u}")
    return "\n".join(out)


def split_blocks(lines: list[str]) -> list[tuple[int, int, list[str]]]:
    blocks: list[tuple[int, int, list[str]]] = []
    cur: list[str] = []
    start = 1
    for i, ln in enumerate(lines, 1):
        if SEP.match(ln):
            if any(x.strip() for x in cur):
                blocks.append((start, i - 1, cur))
            cur, start = [], i + 1
            continue
        cur.append(ln)
    if any(x.strip() for x in cur):
        blocks.append((start, len(lines), cur))
    return blocks


def main() -> None:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "source/x-llm.net.txt")
    lines = src.read_text(encoding="utf-8").splitlines()
    blocks = split_blocks(lines)

    clean: list[str] = []
    ask: list[str] = []
    errors: list[str] = []
    n_clean = n_ask = 0

    for num, (s, e, body) in enumerate(blocks, 1):
        rec = parse_block(body)
        rendered = render(rec, num, s, e)
        incomplete = not (rec["login"] and rec["token"] and rec["aff"] and rec["balance"] is not None)
        if rec["_doubts"] or rec["_unknown"] or incomplete:
            n_ask += 1
            ask.append(rendered)
            ask.append("# --- как выглядит в исходнике: ---")
            ask.extend(f"# | {x}" for x in body if x.strip())
            ask.append("")
        else:
            n_clean += 1
            clean.append(rendered)
            clean.append("")
        for ident, action in rec["_errors"]:
            kind = "email" if "@" in ident else "login"
            errors.append(f"{ident:40} {kind:6} -> {action}   (блок {num}, строки {s}-{e})")

    src.with_name(f"{src.stem}_clean{src.suffix}").write_text(
        "\n".join([
            f"# {src.name}: готово к импорту — {n_clean} блоков из {len(blocks)}",
            "# Разделитель блоков: строка из 5+ символов '=' или '-'",
            "# Пометки после '---' отброшены, в базу не идут",
            "",
            *clean,
        ]),
        encoding="utf-8",
    )
    src.with_name(f"{src.stem}_questions{src.suffix}").write_text(
        "\n".join([
            f"# {src.name}: остались вопросы — {n_ask} блоков из {len(blocks)}",
            "",
            *ask,
        ]),
        encoding="utf-8",
    )
    src.with_name(f"{src.stem}_errors{src.suffix}").write_text(
        "\n".join([
            f"# {src.name}: пометки брака для main_github — {len(errors)} шт.",
            "# bad / bad email -> Bad Rambler Email (ищется по restore_email)",
            "# suspended       -> Suspended Github (ищется по email)",
            "# !!! где идентификатор — login, нужно сначала найти строку в main_github по login",
            "",
            *errors,
        ]),
        encoding="utf-8",
    )
    print(f"clean={n_clean} questions={n_ask} errors={len(errors)}")


if __name__ == "__main__":
    main()
