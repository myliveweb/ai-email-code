"""Очищает папку Спам во всех hotmail и rambler ящиках."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.config import allow_direct_localhost  # noqa: E402
allow_direct_localhost()

from backend.app.supabase_client import get_supabase  # noqa: E402
from outlook_mail_checker import empty_junk_folder as outlook_empty_junk  # noqa: E402
from rambler_imap_mail_checker import empty_junk_folder as rambler_empty_junk  # noqa: E402
from loguru import logger  # noqa: E402


def clean_outlook():
    sb = get_supabase()
    res = (
        sb.table("main_email")
        .select("email, client_id, graph_refresh_token")
        .ilike("email", "%@hotmail.com")
        .eq("active", True)
        .execute()
    )
    accounts = res.data
    logger.info(f"[Outlook] Найдено {len(accounts)} hotmail-ящиков")

    total_deleted = 0
    for i, row in enumerate(accounts, 1):
        email = row["email"]
        client_id = row.get("client_id")
        refresh_token = row.get("graph_refresh_token")
        if not client_id or not refresh_token:
            continue
        try:
            count = outlook_empty_junk(client_id=client_id, refresh_token=refresh_token)
            total_deleted += count
            if count:
                logger.info(f"[{i}/{len(accounts)}] {email}: удалено из спама {count}")
        except Exception as exc:
            logger.error(f"[{i}/{len(accounts)}] {email}: ошибка — {exc}")

    logger.info(f"[Outlook] Готово. Всего удалено из спама: {total_deleted}")


def clean_rambler():
    sb = get_supabase()
    res = (
        sb.table("main_email")
        .select("email, password")
        .ilike("email", "%@rambler.ru")
        .eq("active", True)
        .execute()
    )
    accounts = res.data
    logger.info(f"[Rambler] Найдено {len(accounts)} rambler-ящиков")

    total_deleted = 0
    for i, row in enumerate(accounts, 1):
        email = row["email"]
        password = row.get("password")
        if not password:
            continue
        try:
            count = rambler_empty_junk(login=email, password=password)
            total_deleted += count
            if count:
                logger.info(f"[{i}/{len(accounts)}] {email}: удалено из спама {count}")
        except Exception as exc:
            logger.error(f"[{i}/{len(accounts)}] {email}: ошибка — {exc}")

    logger.info(f"[Rambler] Готово. Всего удалено из спама: {total_deleted}")


if __name__ == "__main__":
    clean_rambler()
    clean_outlook()
