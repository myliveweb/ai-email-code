"""Удаляет письма с темой '[GitHub] Please verify your device' из всех hotmail-ящиков в main_email."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.config import allow_direct_localhost  # noqa: E402
allow_direct_localhost()

from backend.app.supabase_client import get_supabase  # noqa: E402
from outlook_mail_checker import delete_mail_by_subject  # noqa: E402
from loguru import logger  # noqa: E402

SUBJECT = "[GitHub] Please verify your device"


def main():
    sb = get_supabase()
    res = (
        sb.table("main_email")
        .select("email, client_id, graph_refresh_token")
        .ilike("email", "%@hotmail.com")
        .eq("active", True)
        .execute()
    )

    accounts = res.data
    logger.info(f"Найдено {len(accounts)} hotmail-ящиков")

    for i, row in enumerate(accounts, 1):
        email = row["email"]
        client_id = row.get("client_id")
        refresh_token = row.get("graph_refresh_token")
        if not client_id or not refresh_token:
            logger.warning(f"[{i}/{len(accounts)}] {email}: нет client_id или graph_refresh_token, пропуск")
            continue

        try:
            result = delete_mail_by_subject(
                client_id=client_id,
                refresh_token=refresh_token,
                subject=SUBJECT,
                match_mode="exact",
            )
            total = sum(result.values())
            if total:
                logger.info(f"[{i}/{len(accounts)}] {email}: удалено {result}")
            else:
                logger.debug(f"[{i}/{len(accounts)}] {email}: писем не найдено")
        except Exception as exc:
            logger.error(f"[{i}/{len(accounts)}] {email}: ошибка — {exc}")

    logger.info("Готово")


if __name__ == "__main__":
    main()
