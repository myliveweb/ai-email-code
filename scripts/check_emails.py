"""
Проверка здоровья ящиков из data/in_email.txt.
Hotmail — через outlook_mail_checker (сначала graph_refresh_token, потом refresh_token).
Rambler — через rambler_imap_mail_checker.
Результат: перезаписывает data/in_email.txt только живыми ящиками.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.app.config import allow_direct_localhost
allow_direct_localhost()

from backend.app.supabase_client import get_supabase
from outlook_mail_checker import check_mailboxes_bulk as outlook_bulk
from rambler_imap_mail_checker import check_mailboxes_bulk as rambler_bulk


def main():
    input_file = Path(__file__).resolve().parent.parent / "data" / "in_email.txt"
    lines = input_file.read_text().strip().splitlines()

    hotmail_lines = []
    rambler_lines = []
    for line in lines:
        parts = line.split(":")
        email = parts[0]
        if email.endswith("@rambler.ru"):
            rambler_lines.append(parts)
        else:
            hotmail_lines.append(parts)

    print(f"Всего: {len(lines)} | Hotmail: {len(hotmail_lines)} | Rambler: {len(rambler_lines)}")

    # Для hotmail — берём из БД graph_refresh_token
    sb = get_supabase()
    hotmail_emails = [p[0] for p in hotmail_lines]
    db_res = sb.table("main_email").select(
        "email, client_id, refresh_token, graph_refresh_token"
    ).in_("email", hotmail_emails).execute()
    db_map = {r["email"]: r for r in db_res.data}

    # Сначала пробуем graph_refresh_token
    mailboxes_graph = []
    for parts in hotmail_lines:
        email = parts[0]
        db_row = db_map.get(email, {})
        grt = db_row.get("graph_refresh_token") or ""
        cid = parts[2] if len(parts) > 2 and parts[2] else db_row.get("client_id", "")
        if grt and cid:
            mailboxes_graph.append({
                "email": email,
                "client_id": cid,
                "refresh_token": grt,
                "tenant_id": "common",
            })
        else:
            mailboxes_graph.append(None)

    # Проверяем те, у кого есть graph_refresh_token
    to_check_graph = [(i, mb) for i, mb in enumerate(mailboxes_graph) if mb is not None]
    graph_input = [mb for _, mb in to_check_graph]

    print(f"\n--- Hotmail: проверяю {len(graph_input)} ящиков через graph_refresh_token ---")
    if graph_input:
        graph_results = outlook_bulk(graph_input, max_workers=10)
    else:
        graph_results = []

    # Собираем результаты первого прохода
    hotmail_status = {}  # email -> active
    failed_graph = []  # индексы в hotmail_lines, где graph не сработал
    for res in graph_results:
        hotmail_status[res["email"]] = res["active"]
        if not res["active"]:
            failed_graph.append(res["email"])
            print(f"  GRAPH FAIL: {res['email']} — {res.get('reason', '?')}")

    active_graph = sum(1 for v in hotmail_status.values() if v)
    print(f"  graph_refresh_token: {active_graph} живых, {len(failed_graph)} мёртвых")

    # Для мёртвых пробуем обычный refresh_token
    if failed_graph:
        mailboxes_rt = []
        for parts in hotmail_lines:
            email = parts[0]
            if email not in failed_graph:
                continue
            rt = parts[3] if len(parts) > 3 and parts[3] else ""
            cid = parts[2] if len(parts) > 2 and parts[2] else ""
            if rt and cid:
                mailboxes_rt.append({
                    "email": email,
                    "client_id": cid,
                    "refresh_token": rt,
                    "tenant_id": "common",
                })

        if mailboxes_rt:
            print(f"\n--- Hotmail: пробую {len(mailboxes_rt)} через refresh_token ---")
            rt_results = outlook_bulk(mailboxes_rt, max_workers=10)
            for res in rt_results:
                if res["active"]:
                    hotmail_status[res["email"]] = True
                    print(f"  RT OK: {res['email']}")
                else:
                    print(f"  RT FAIL: {res['email']} — {res.get('reason', '?')}")

    # Rambler
    rambler_status = {}
    if rambler_lines:
        rambler_input = []
        for parts in rambler_lines:
            email = parts[0]
            password = parts[1] if len(parts) > 1 else ""
            rambler_input.append({
                "email": email,
                "login": email,
                "password": password,
            })
        print(f"\n--- Rambler: проверяю {len(rambler_input)} ящиков ---")
        rambler_results = rambler_bulk(rambler_input, max_workers=5)
        for res in rambler_results:
            rambler_status[res["email"]] = res["active"]
            status = "OK" if res["active"] else f"FAIL — {res.get('reason', '?')}"
            print(f"  {res['email']}: {status}")

    # Итог
    all_status = {**hotmail_status, **rambler_status}
    alive = [email for email, active in all_status.items() if active]
    dead = [email for email, active in all_status.items() if not active]

    print(f"\n{'='*60}")
    print(f"ИТОГО: {len(alive)} живых, {len(dead)} мёртвых из {len(lines)}")
    if dead:
        print(f"\nМёртвые:")
        for e in sorted(dead):
            print(f"  {e}")

    # Перезаписываем файл только живыми
    alive_set = set(alive)
    alive_lines = [line for line in lines if line.split(":")[0] in alive_set]
    input_file.write_text("\n".join(alive_lines) + "\n")
    print(f"\nФайл перезаписан: {len(alive_lines)} строк")


if __name__ == "__main__":
    main()
