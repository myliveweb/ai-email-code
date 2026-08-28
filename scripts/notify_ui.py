"""Толчок открытым страницам: данные по теме обновились.

Крон-скрипт заканчивает работу и стучит в backend, а тот рассылает весть открытым
страницам через SSE. Без этого отчёт висит устаревшим до перехода по вкладкам:
скрипт крутится каждые 10-15 минут, а страница у Босса открыта часами.

Молчаливая ошибка — намеренно: backend может быть не поднят, и это не повод валить
прогон, чьё дело — цифры в базе и файлы отчётов.
"""

import os

import httpx
from loguru import logger

TOPIC_CLAUDE = "claude"
TOPIC_LINUXDO = "linuxdo"


def notify_ui(topic: str) -> None:
    host = os.getenv("API_HOST", "127.0.0.1")
    port = os.getenv("API_PORT", "4000")
    try:
        # trust_env=False: в окружении задан внешний proxy, через него локальный
        # backend недостижим (см. раздел Proxy в CLAUDE.md)
        res = httpx.post(
            f"http://{host}:{port}/api/events/notify",
            json={"topic": topic},
            timeout=5,
            trust_env=False,
        )
        listeners = res.json().get("listeners") if res.status_code == 200 else "?"
        logger.info(f"Фронтенду отправлено событие {topic}, слушателей: {listeners}")
    except Exception as exc:
        logger.debug(f"Событие {topic} не доставлено (backend не поднят?): {exc}")
