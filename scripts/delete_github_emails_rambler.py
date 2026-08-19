"""Удаляет письма с темой '[GitHub] Please verify your device' из всех rambler-ящиков в main_email."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.config import allow_direct_localhost  # noqa: E402
allow_direct_localhost()

from backend.app.supabase_client import get_supabase  # noqa: E402
from rambler_imap_mail_checker import delete_mail_by_subject  # noqa: E402
from loguru import logger  # noqa: E402

SUBJECT = "[GitHub] Please verify your device"


def main():
    sb = get_supabase()
    res = (
        sb.table("main_email")
        .select("email, password")
        .ilike("email", "%@rambler.ru")
        .execute()
    )

    accounts = res.data
    logger.info(f"Найдено {len(accounts)} rambler-ящиков")

    for i, row in enumerate(accounts, 1):
        email = row["email"]
        password = row.get("password")
        if not password:
            logger.warning(f"[{i}/{len(accounts)}] {email}: нет пароля, пропуск")
            continue

        try:
            result = delete_mail_by_subject(
                login=email,
                password=password,
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
