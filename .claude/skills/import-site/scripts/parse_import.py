"""Разбор рукописного текстового файла импорта на блоки, по профилю.

Главное правило Босса: значение для базы стоит в НАЧАЛЕ строки, всё после
разделителя пометки (по умолчанию `---`) — его рукописный комментарий.
Поэтому строка сначала режется на head (данные) и note (пометка), поля
ищутся в head, а сигналы вида `bad` / `suspended` — в note.

Запуск:
    uv run python .claude/skills/import-site/scripts/parse_import.py \
        source/<file>.txt .claude/skills/import-site/scripts/profiles/<name>.json

На выходе рядом с исходником:
    _clean.txt     — блоки, разобранные без вопросов
    _questions.txt — блоки с неясностями + их исходные строки
    _errors.txt    — пометки брака для main_github
"""

import json
import re
import sys
from pathlib import Path


def compile_profile(p: dict) -> dict:
    p = dict(p)
    p["_sep"] = re.compile(p.get("separator", r"^\s*[=\-]{5,}\s*$"))
    p["_marker"] = p.get("note_marker", "---")
    names: list[str] = []
    missing: dict[str, str] = {}
    for name, spec in p.get("fields", {}).items():
        spec["_re"] = re.compile(spec["regex"])
        spec["_note_re"] = re.compile(spec["note_regex"], re.I) if spec.get("note_regex") else None
        spec["_targets"] = spec.get("targets") or {name: spec.get("group", 1)}
        for target in spec.get("const", {}):
            if target not in names:
                names.append(target)
            missing.setdefault(target, "??? не найден")
        for target in spec["_targets"]:
            if target not in names:
                names.append(target)
            missing.setdefault(target, spec.get("if_missing", "??? не найден"))
    p["_names"] = names
    p["_missing"] = missing
    for spec in p.get("marks", []):
        spec["_re"] = re.compile(spec["regex"])
        spec["_note_re"] = re.compile(spec["note_regex"], re.I)
    p["_noise"] = [re.compile(x) for x in p.get("noise", [])]
    p["_bare"] = re.compile(r"^[A-Za-z0-9._%+-]+(?:@[A-Za-z0-9.-]+\.[A-Za-z]{2,})?$")
    return p


def split_note(line: str, marker: str) -> tuple[str, str]:
    head, _, note = line.partition(marker)
    return head.strip(), note.strip()


def cast(value: str, kind: str | None):
    if kind == "float":
        return float(value.replace(",", "."))
    if kind == "int":
        return int(value)
    return value


def parse_block(lines: list[str], p: dict) -> dict:
    fields = p.get("fields", {})
    rec: dict = {name: None for name in p["_names"]}
    rec["_errors"] = []
    rec["_unknown"] = []

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        head, note = split_note(line, p["_marker"])
        if not head:
            continue

        matched = False
        for spec in fields.values():
            m = spec["_re"].match(head)
            if not m:
                continue
            if spec["_note_re"] and not spec["_note_re"].search(note):
                continue
            matched = True
            for target, group in spec["_targets"].items():
                if rec[target] is not None and spec.get("on_repeat", "overwrite") != "overwrite":
                    if spec["on_repeat"] == "flag":
                        rec["_unknown"].append(f"{line}   <-- !!! второй {target} в блоке")
                    continue
                rec[target] = cast(m.group(group), spec.get("cast"))
            for target, value in spec.get("const", {}).items():
                if rec[target] is None:
                    rec[target] = value
            break
        if matched:
            continue

        for spec in p.get("marks", []):
            m = spec["_re"].match(head)
            if m and spec["_note_re"].search(note):
                rec["_errors"].append((m.group(spec.get("group", 1)), spec["action"]))
                matched = True
                break
        if matched:
            continue

        if any(rx.match(head) for rx in p["_noise"]):
            continue
        if p.get("ignore_bare_identifiers", True) and p["_bare"].match(head):
            continue

        rec["_unknown"].append(line)

    return rec


def split_blocks(lines: list[str], sep: re.Pattern) -> list[tuple[int, int, list[str]]]:
    blocks: list[tuple[int, int, list[str]]] = []
    cur: list[str] = []
    start = 1
    for i, ln in enumerate(lines, 1):
        if sep.match(ln):
            if any(x.strip() for x in cur):
                blocks.append((start, i - 1, cur))
            cur, start = [], i + 1
            continue
        cur.append(ln)
    if any(x.strip() for x in cur):
        blocks.append((start, len(lines), cur))
    return blocks


def render(rec: dict, p: dict, num: int, start: int, end: int) -> str:
    width = max((len(n) for n in p["_names"]), default=7)
    out = [f"### БЛОК {num}   (строки {start}-{end} исходника)"]
    for name in p["_names"]:
        value = rec[name] if rec[name] is not None else p["_missing"][name]
        out.append(f"{name:<{width}} = {value}")
    for u in rec["_unknown"]:
        out.append(f"#   ??? НЕ ПОНЯЛ, что это: {u}")
    return "\n".join(out)


def main() -> None:
    src = Path(sys.argv[1])
    profile = compile_profile(json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")))
    lines = src.read_text(encoding="utf-8").splitlines()
    blocks = split_blocks(lines, profile["_sep"])
    required = profile.get("required", profile["_names"])

    clean: list[str] = []
    ask: list[str] = []
    marks: dict[tuple[str, str], dict] = {}

    for num, (s, e, body) in enumerate(blocks, 1):
        rec = parse_block(body, profile)
        all_none = all(rec[f] is None for f in profile["_names"])
        if all_none and not rec["_unknown"] and not rec["_errors"]:
            continue
        rendered = render(rec, profile, num, s, e)
        if rec["_unknown"] or any(rec[f] is None for f in required):
            ask.append(rendered)
            ask.append("# --- как выглядит в исходнике: ---")
            ask.extend(f"# | {x}" for x in body if x.strip())
            ask.append("")
        else:
            clean.append(rendered)
            clean.append("")
        for ident, action in rec["_errors"]:
            key = (ident, action)
            if key in marks:
                marks[key]["count"] += 1
                continue
            kind = "email" if "@" in ident else "login"
            marks[key] = {
                "count": 1,
                "text": f"{ident:40} {kind:6} -> {action}   (впервые в блоке {num}, строки {s}-{e})",
            }

    n_ask = sum(1 for x in ask if x.startswith("### БЛОК"))
    n_clean = sum(1 for x in clean if x.startswith("### БЛОК"))

    def write(suffix: str, header: list[str], body: list[str]) -> None:
        src.with_name(f"{src.stem}_{suffix}{src.suffix}").write_text(
            "\n".join([*header, "", *body]), encoding="utf-8"
        )

    write("clean", [
        f"# {src.name}: готово к импорту — {n_clean} блоков из {len(blocks)}",
        f"# Разделитель блоков: {profile.get('separator')}",
        f"# Пометки после '{profile['_marker']}' отброшены, в базу не идут",
    ], clean)
    write("questions", [f"# {src.name}: остались вопросы — {n_ask} блоков из {len(blocks)}"], ask)
    error_lines = [
        m["text"] + (f"   встречается {m['count']} раз" if m["count"] > 1 else "")
        for m in marks.values()
    ]

    write("errors", [
        f"# {src.name}: пометки брака для main_github — {len(error_lines)} шт. (дубли свёрнуты)",
        "# см. references/import-flow.md, раздел про error_status",
    ], error_lines)
    print(f"clean={n_clean} questions={n_ask} errors={len(error_lines)}")


if __name__ == "__main__":
    main()
