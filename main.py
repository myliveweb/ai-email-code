import os
import sys
import time
from supabase import create_client, Client
from dotenv import find_dotenv, load_dotenv
from loguru import logger

from backend.app.config import allow_direct_localhost

load_dotenv(find_dotenv())
allow_direct_localhost()

start_time = time.time()

url: str = os.getenv("SUPABASE_URL")
key: str = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(url, key)


def main():
    logger.success("Запуск...")
    print(sys.version)

    supabase.table("main_email").select("id").limit(1).execute()
    logger.success("Подключение к Supabase установлено")

    logger.success(f"Выполнено за {time.time() - start_time:.2f} сек")


if __name__ == "__main__":
    main()
