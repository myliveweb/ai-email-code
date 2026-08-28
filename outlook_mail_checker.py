"""
Модуль для работы с почтой Outlook (папки "Входящие" и "Спам")
через Microsoft Graph API с авторизацией по refresh_token.

Возможности:
    - Обмен refresh_token -> access_token с кэшированием токена.
    - Автоматические ретраи при 429 (throttling) и 5xx ошибках Graph,
      с учётом заголовка Retry-After.
    - Получение писем без пагинации (folder totalItemCount -> $top).
    - Поиск / удаление / перемещение (soft-delete) писем по subject
      или по отправителю, с поддержкой:
        * режимов сравнения: exact / contains / startswith / regex
        * фильтра по диапазону дат получения
    - Массовые операции (удаление, перемещение) через Graph $batch API
      (чанками по 20 запросов), с идемпотентной обработкой 404
      (письмо уже удалено/перемещено -> не считается ошибкой).
    - empty_junk_folder — полная очистка папки "Спам".
    - check_mailbox_health / check_mailboxes_bulk — проверка "жив ли ящик"
      (для поля active в БД), в том числе параллельно для сотен ящиков.

Требуется зарегистрированное Azure AD приложение с разрешениями
Mail.ReadWrite (delegated) и выданным refresh_token для нужного scope.

Установка зависимостей:
    pip install requests
"""

from __future__ import annotations

import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests


logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
DEFAULT_SCOPE = "https://graph.microsoft.com/.default offline_access"

# Папки, которые нужно проверить. Можно заменить на реальные folder id,
# если используются пользовательские папки.
FOLDERS_TO_CHECK = ("inbox", "junkemail")

# Папка назначения по умолчанию для "мягкого удаления" (перемещения)
DEFAULT_TARGET_FOLDER = "archive"

# Поля письма, которые нас интересуют (уменьшает объём ответа)
MESSAGE_SELECT_FIELDS = "id,subject,from,receivedDateTime,bodyPreview,body,webLink"

# Graph API ограничивает $top значением 999 за один запрос
GRAPH_MAX_TOP = 999

# Graph $batch поддерживает максимум 20 запросов в одном батче
BATCH_CHUNK_SIZE = 20

# Настройки ретраев
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 1.0  # 1s, 2s, 4s, 8s, 16s...
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Буфер (в секундах), за который до истечения токена он считается "протухшим"
TOKEN_EXPIRY_LEEWAY = 60

# Допустимые режимы сравнения текста
MATCH_MODES = ("exact", "contains", "startswith", "regex")


class GraphAuthError(Exception):
    """Ошибка получения access_token по refresh_token."""


class GraphRequestError(Exception):
    """Ошибка обращения к Microsoft Graph API."""


@dataclass
class _CachedToken:
    access_token: str
    expires_at: float  # time.monotonic() timestamp


# Кэш токенов в памяти процесса, ключ — (client_id, tenant_id, scope, refresh_token).
# refresh_token обязателен в ключе: одно Azure AD приложение обслуживает много
# ящиков, поэтому без него токен первого ящика подхватывался бы для всех остальных.
_token_cache: Dict[tuple, _CachedToken] = {}


def _request_with_retry(
    method: str,
    url: str,
    *,
    headers: Optional[dict] = None,
    params: Optional[dict] = None,
    data: Optional[dict] = None,
    json_body: Optional[dict] = None,
    timeout: int = 15,
) -> requests.Response:
    """
    Выполняет HTTP-запрос с ретраями при 429/5xx.

    Для 429 и 503 учитывает заголовок Retry-After, если он есть.
    Иначе использует экспоненциальный backoff.

    :param data: form-encoded тело запроса (например, для token endpoint).
    :param json_body: JSON-тело запроса (например, для $batch).
    """
    last_exc: Optional[Exception] = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                data=data,
                json=json_body,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            last_exc = exc
            logger.warning("Сетевая ошибка (попытка %s/%s): %s", attempt, MAX_RETRIES, exc)
            if attempt == MAX_RETRIES:
                raise
            time.sleep(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
            continue

        if response.status_code not in RETRYABLE_STATUS_CODES:
            return response

        if attempt == MAX_RETRIES:
            return response  # отдаём последний ответ, вызывающий код разберётся по raise_for_status

        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
        else:
            delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))

        logger.warning(
            "Graph вернул %s (попытка %s/%s), повтор через %.1fs",
            response.status_code, attempt, MAX_RETRIES, delay,
        )
        time.sleep(delay)

    if last_exc:
        raise last_exc
    return response


def get_access_token(
    client_id: str,
    refresh_token: str,
    tenant_id: str = "common",
    client_secret: Optional[str] = None,
    scope: str = DEFAULT_SCOPE,
    timeout: int = 15,
    force_refresh: bool = False,
) -> str:
    """
    Обменивает refresh_token на access_token через Azure AD v2.0 token endpoint.
    Использует кэш в памяти процесса: если ранее полученный токен ещё
    действителен, новый запрос не выполняется.

    :param force_refresh: Игнорировать кэш и получить новый токен принудительно.
    :return: access_token (строка).
    :raises GraphAuthError: если токен не удалось получить.
    """
    cache_key = (client_id, tenant_id, scope, refresh_token)

    if not force_refresh:
        cached = _token_cache.get(cache_key)
        if cached and cached.expires_at > time.monotonic():
            logger.debug("Используем закэшированный access_token")
            return cached.access_token

    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

    data = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": scope,
    }
    if client_secret:
        data["client_secret"] = client_secret

    try:
        response = _request_with_retry("POST", token_url, data=data, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise GraphAuthError(f"Не удалось получить access_token: {exc}") from exc

    payload = response.json()
    access_token = payload.get("access_token")
    if not access_token:
        raise GraphAuthError(f"В ответе токен-эндпоинта нет access_token: {payload}")

    expires_in = payload.get("expires_in", 3600)
    expires_at = time.monotonic() + max(expires_in - TOKEN_EXPIRY_LEEWAY, 0)
    _token_cache[cache_key] = _CachedToken(access_token=access_token, expires_at=expires_at)

    return access_token


def _format_graph_datetime(dt: datetime) -> str:
    """Приводит datetime к формату, который понимает $filter Graph API (UTC, ISO 8601)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_folder_message_count(
    access_token: str,
    folder: str,
    timeout: int = 15,
) -> int:
    """Возвращает totalItemCount папки (общее количество писем)."""
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"{GRAPH_BASE_URL}/me/mailFolders/{folder}"
    params = {"$select": "totalItemCount"}

    response = _request_with_retry("GET", url, headers=headers, params=params, timeout=timeout)
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise GraphRequestError(
            f"Ошибка получения количества писем в папке '{folder}': {exc}"
        ) from exc

    return response.json().get("totalItemCount", 0)


def _fetch_folder_messages(
    access_token: str,
    folder: str,
    *,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    timeout: int = 15,
) -> List[dict]:
    """
    Получает все письма из указанной папки одним запросом (без пагинации):
    сначала узнаём totalItemCount, затем запрашиваем $top=count.

    :param date_from: если указано, только письма, полученные ПОСЛЕ этой даты (включительно).
    :param date_to: если указано, только письма, полученные ДО этой даты (включительно).
        totalItemCount используется как верхняя граница $top даже при фильтрации
        по дате — итоговая выборка не может быть больше общего числа писем в папке.
    """
    headers = {"Authorization": f"Bearer {access_token}"}

    total_count = _get_folder_message_count(access_token, folder, timeout=timeout)
    if total_count == 0:
        return []

    top = min(total_count, GRAPH_MAX_TOP)
    if total_count > GRAPH_MAX_TOP:
        logger.warning(
            "В папке '%s' %s писем, но без пагинации за один запрос можно "
            "получить максимум %s. Будут получены только первые %s.",
            folder, total_count, GRAPH_MAX_TOP, top,
        )

    params = {"$select": MESSAGE_SELECT_FIELDS, "$top": str(top)}

    filter_parts = []
    if date_from is not None:
        filter_parts.append(f"receivedDateTime ge {_format_graph_datetime(date_from)}")
    if date_to is not None:
        filter_parts.append(f"receivedDateTime le {_format_graph_datetime(date_to)}")
    if filter_parts:
        params["$filter"] = " and ".join(filter_parts)

    url = f"{GRAPH_BASE_URL}/me/mailFolders/{folder}/messages"

    response = _request_with_retry("GET", url, headers=headers, params=params, timeout=timeout)
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise GraphRequestError(
            f"Ошибка запроса писем из папки '{folder}': {exc}"
        ) from exc

    messages = response.json().get("value", [])
    # Graph отдаёт body словарём {contentType, content}, а bodyPreview обрезан на 255
    # символах — код подтверждения на этом рубеже разрывался пополам. Разворачиваем
    # тело в строку здесь, чтобы дальше по коду письмо было однородным.
    for msg in messages:
        if isinstance(msg.get("body"), dict):
            msg["body"] = msg["body"].get("content") or ""
    return messages


def _matches(value: Optional[str], pattern: str, mode: str, case_sensitive: bool) -> bool:
    """Проверяет, соответствует ли value паттерну pattern в заданном режиме."""
    if value is None:
        return False
    if mode not in MATCH_MODES:
        raise ValueError(f"Неизвестный match_mode: {mode!r}, допустимо: {MATCH_MODES}")

    if mode == "regex":
        flags = 0 if case_sensitive else re.IGNORECASE
        return re.search(pattern, value, flags) is not None

    v = value if case_sensitive else value.lower()
    p = pattern if case_sensitive else pattern.lower()

    if mode == "exact":
        return v == p
    if mode == "contains":
        return p in v
    if mode == "startswith":
        return v.startswith(p)
    return False  # недостижимо


def _find_messages(
    access_token: str,
    folder: str,
    *,
    subject: Optional[str] = None,
    sender: Optional[str] = None,
    match_mode: str = "exact",
    case_sensitive: bool = True,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    timeout: int = 15,
) -> List[dict]:
    """
    Возвращает письма из папки, подходящие под критерии subject и/или sender
    (если оба заданы — применяются оба, логическое И).
    """
    messages = _fetch_folder_messages(
        access_token, folder, date_from=date_from, date_to=date_to, timeout=timeout
    )

    if subject is None and sender is None:
        return messages

    matched = []
    for message in messages:
        ok = True
        if subject is not None:
            ok = ok and _matches(message.get("subject"), subject, match_mode, case_sensitive)
        if sender is not None:
            sender_email = ((message.get("from") or {}).get("emailAddress") or {}).get("address")
            ok = ok and _matches(sender_email, sender, match_mode, case_sensitive)
        if ok:
            matched.append(message)

    return matched


def _execute_batch(access_token: str, items: List[dict], timeout: int = 30) -> Dict[str, int]:
    """
    Выполняет операции через Graph $batch API чанками по BATCH_CHUNK_SIZE штук.
    items: [{"id": "0", "method": "DELETE", "url": "/me/messages/xxx"}, ...]
    :return: {id: http_status_code} для каждого суб-запроса.
    """
    if not items:
        return {}

    headers = {"Authorization": f"Bearer {access_token}"}
    results: Dict[str, int] = {}

    for i in range(0, len(items), BATCH_CHUNK_SIZE):
        chunk = items[i : i + BATCH_CHUNK_SIZE]
        response = _request_with_retry(
            "POST",
            f"{GRAPH_BASE_URL}/$batch",
            headers=headers,
            json_body={"requests": chunk},
            timeout=timeout,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise GraphRequestError(f"Ошибка batch-запроса: {exc}") from exc

        for sub_response in response.json().get("responses", []):
            results[sub_response["id"]] = sub_response.get("status", 0)

    return results


def _bulk_delete(access_token: str, message_ids: List[str]) -> int:
    """
    Удаляет письма пачками через $batch. 404 (уже удалено) считается успехом
    (идемпотентность) и не логируется как ошибка.
    :return: количество успешно удалённых (включая уже отсутствовавшие) писем.
    """
    if not message_ids:
        return 0

    items = [
        {"id": str(idx), "method": "DELETE", "url": f"/me/messages/{msg_id}"}
        for idx, msg_id in enumerate(message_ids)
    ]
    statuses = _execute_batch(access_token, items)

    success = 0
    for idx, msg_id in enumerate(message_ids):
        status = statuses.get(str(idx), 0)
        if 200 <= status < 300:
            success += 1
        elif status == 404:
            success += 1
            logger.info("Письмо %s уже было удалено ранее (404), пропускаем", msg_id)
        else:
            logger.warning("Не удалось удалить письмо %s (status=%s)", msg_id, status)

    return success


def _bulk_move(access_token: str, message_ids: List[str], target_folder: str) -> int:
    """
    Перемещает письма пачками через $batch (мягкое удаление).
    404 (письмо уже недоступно/перемещено) считается успехом.
    :return: количество успешно перемещённых писем.
    """
    if not message_ids:
        return 0

    items = [
        {
            "id": str(idx),
            "method": "POST",
            "url": f"/me/messages/{msg_id}/move",
            "body": {"destinationId": target_folder},
            "headers": {"Content-Type": "application/json"},
        }
        for idx, msg_id in enumerate(message_ids)
    ]
    statuses = _execute_batch(access_token, items)

    success = 0
    for idx, msg_id in enumerate(message_ids):
        status = statuses.get(str(idx), 0)
        if 200 <= status < 300:
            success += 1
        elif status == 404:
            success += 1
            logger.info("Письмо %s не найдено при перемещении (404), пропускаем", msg_id)
        else:
            logger.warning("Не удалось переместить письмо %s (status=%s)", msg_id, status)

    return success


def _delete_matching(access_token: str, folder: str, **find_kwargs) -> int:
    messages = _find_messages(access_token, folder, **find_kwargs)
    return _bulk_delete(access_token, [m["id"] for m in messages])


def _move_matching(access_token: str, folder: str, target_folder: str, **find_kwargs) -> int:
    messages = _find_messages(access_token, folder, **find_kwargs)
    return _bulk_move(access_token, [m["id"] for m in messages], target_folder)


# ---------------------------------------------------------------------------
# Публичные функции
# ---------------------------------------------------------------------------


def check_mail_by_subject(
    client_id: str,
    refresh_token: str,
    tenant_id: str = "common",
    client_secret: Optional[str] = None,
    folders: tuple = FOLDERS_TO_CHECK,
) -> Dict[str, List[dict]]:
    """
    Проверяет почту в папках "Входящие" и "Спам" и группирует ВСЕ письма по теме.
    :return: Словарь вида {subject: [письмо1, письмо2, ...]}.
    """
    access_token = get_access_token(
        client_id=client_id, refresh_token=refresh_token,
        tenant_id=tenant_id, client_secret=client_secret,
    )

    result: Dict[str, List[dict]] = {}
    for folder in folders:
        try:
            messages = _fetch_folder_messages(access_token, folder)
        except GraphRequestError as exc:
            logger.warning("Пропускаем папку '%s': %s", folder, exc)
            continue
        for message in messages:
            subject = message.get("subject") or "(без темы)"
            result.setdefault(subject, []).append(message)

    return result


def get_mail_by_subject(
    client_id: str,
    refresh_token: str,
    subject: str,
    match_mode: str = "exact",
    case_sensitive: bool = True,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    tenant_id: str = "common",
    client_secret: Optional[str] = None,
    folders: tuple = FOLDERS_TO_CHECK,
) -> List[dict]:
    """
    Возвращает списком письма, чей subject соответствует заданному критерию,
    из папок "Входящие" и "Спам" (или из folders).

    :param match_mode: 'exact' | 'contains' | 'startswith' | 'regex'.
    :param date_from: если указано — только письма, полученные не раньше этой даты.
    :param date_to: если указано — только письма, полученные не позже этой даты.
    """
    access_token = get_access_token(
        client_id=client_id, refresh_token=refresh_token,
        tenant_id=tenant_id, client_secret=client_secret,
    )

    found: List[dict] = []
    for folder in folders:
        try:
            matched = _find_messages(
                access_token, folder,
                subject=subject, match_mode=match_mode, case_sensitive=case_sensitive,
                date_from=date_from, date_to=date_to,
            )
        except GraphRequestError as exc:
            logger.warning("Пропускаем папку '%s': %s", folder, exc)
            continue
        found.extend(matched)
        logger.info("Папка '%s': найдено %s писем по subject='%s'", folder, len(matched), subject)

    return found


def get_mail_by_sender(
    client_id: str,
    refresh_token: str,
    sender: str,
    match_mode: str = "exact",
    case_sensitive: bool = False,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    tenant_id: str = "common",
    client_secret: Optional[str] = None,
    folders: tuple = FOLDERS_TO_CHECK,
) -> List[dict]:
    """
    Возвращает списком письма от заданного отправителя (email),
    из папок "Входящие" и "Спам" (или из folders).

    :param sender: email отправителя (или его часть/паттерн — см. match_mode).
    :param match_mode: 'exact' | 'contains' | 'startswith' | 'regex'.
    """
    access_token = get_access_token(
        client_id=client_id, refresh_token=refresh_token,
        tenant_id=tenant_id, client_secret=client_secret,
    )

    found: List[dict] = []
    for folder in folders:
        try:
            matched = _find_messages(
                access_token, folder,
                sender=sender, match_mode=match_mode, case_sensitive=case_sensitive,
                date_from=date_from, date_to=date_to,
            )
        except GraphRequestError as exc:
            logger.warning("Пропускаем папку '%s': %s", folder, exc)
            continue
        found.extend(matched)
        logger.info("Папка '%s': найдено %s писем от '%s'", folder, len(matched), sender)

    return found


def delete_mail_by_subject(
    client_id: str,
    refresh_token: str,
    subject: str,
    match_mode: str = "exact",
    case_sensitive: bool = True,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    tenant_id: str = "common",
    client_secret: Optional[str] = None,
    folders: tuple = FOLDERS_TO_CHECK,
) -> Dict[str, int]:
    """
    Удаляет (безвозвратно, минуя "Удалённые") все письма с подходящим subject
    в папках "Входящие" и "Спам". Удаление выполняется пачками через $batch.
    :return: {folder: количество_удалённых_писем}.
    """
    access_token = get_access_token(
        client_id=client_id, refresh_token=refresh_token,
        tenant_id=tenant_id, client_secret=client_secret,
    )

    deleted: Dict[str, int] = {}
    for folder in folders:
        try:
            deleted[folder] = _delete_matching(
                access_token, folder,
                subject=subject, match_mode=match_mode, case_sensitive=case_sensitive,
                date_from=date_from, date_to=date_to,
            )
        except GraphRequestError as exc:
            logger.warning("Пропускаем папку '%s': %s", folder, exc)
            deleted[folder] = 0

    return deleted


def delete_mail_by_sender(
    client_id: str,
    refresh_token: str,
    sender: str,
    match_mode: str = "exact",
    case_sensitive: bool = False,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    tenant_id: str = "common",
    client_secret: Optional[str] = None,
    folders: tuple = FOLDERS_TO_CHECK,
) -> Dict[str, int]:
    """
    Удаляет все письма от заданного отправителя в папках "Входящие" и "Спам".
    :return: {folder: количество_удалённых_писем}.
    """
    access_token = get_access_token(
        client_id=client_id, refresh_token=refresh_token,
        tenant_id=tenant_id, client_secret=client_secret,
    )

    deleted: Dict[str, int] = {}
    for folder in folders:
        try:
            deleted[folder] = _delete_matching(
                access_token, folder,
                sender=sender, match_mode=match_mode, case_sensitive=case_sensitive,
                date_from=date_from, date_to=date_to,
            )
        except GraphRequestError as exc:
            logger.warning("Пропускаем папку '%s': %s", folder, exc)
            deleted[folder] = 0

    return deleted


def move_mail_by_subject(
    client_id: str,
    refresh_token: str,
    subject: str,
    target_folder: str = DEFAULT_TARGET_FOLDER,
    match_mode: str = "exact",
    case_sensitive: bool = True,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    tenant_id: str = "common",
    client_secret: Optional[str] = None,
    folders: tuple = FOLDERS_TO_CHECK,
) -> Dict[str, int]:
    """
    "Мягкое удаление": перемещает письма с подходящим subject в target_folder
    (по умолчанию 'archive') вместо безвозвратного удаления.
    :param target_folder: well-known имя папки ('archive', 'deleteditems', ...)
        либо конкретный folder id.
    :return: {folder: количество_перемещённых_писем}.
    """
    access_token = get_access_token(
        client_id=client_id, refresh_token=refresh_token,
        tenant_id=tenant_id, client_secret=client_secret,
    )

    moved: Dict[str, int] = {}
    for folder in folders:
        try:
            moved[folder] = _move_matching(
                access_token, folder, target_folder,
                subject=subject, match_mode=match_mode, case_sensitive=case_sensitive,
                date_from=date_from, date_to=date_to,
            )
        except GraphRequestError as exc:
            logger.warning("Пропускаем папку '%s': %s", folder, exc)
            moved[folder] = 0

    return moved


def move_mail_by_sender(
    client_id: str,
    refresh_token: str,
    sender: str,
    target_folder: str = DEFAULT_TARGET_FOLDER,
    match_mode: str = "exact",
    case_sensitive: bool = False,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    tenant_id: str = "common",
    client_secret: Optional[str] = None,
    folders: tuple = FOLDERS_TO_CHECK,
) -> Dict[str, int]:
    """
    "Мягкое удаление": перемещает письма от заданного отправителя
    в target_folder (по умолчанию 'archive').
    :return: {folder: количество_перемещённых_писем}.
    """
    access_token = get_access_token(
        client_id=client_id, refresh_token=refresh_token,
        tenant_id=tenant_id, client_secret=client_secret,
    )

    moved: Dict[str, int] = {}
    for folder in folders:
        try:
            moved[folder] = _move_matching(
                access_token, folder, target_folder,
                sender=sender, match_mode=match_mode, case_sensitive=case_sensitive,
                date_from=date_from, date_to=date_to,
            )
        except GraphRequestError as exc:
            logger.warning("Пропускаем папку '%s': %s", folder, exc)
            moved[folder] = 0

    return moved


def empty_junk_folder(
    client_id: str,
    refresh_token: str,
    tenant_id: str = "common",
    client_secret: Optional[str] = None,
) -> int:
    """
    Полностью очищает папку "Спам" (junkemail) — удаляет все письма без фильтра.
    :return: количество удалённых писем.
    """
    access_token = get_access_token(
        client_id=client_id, refresh_token=refresh_token,
        tenant_id=tenant_id, client_secret=client_secret,
    )
    return _delete_matching(access_token, "junkemail")


def _check_mailbox_health_detailed(
    client_id: str,
    refresh_token: str,
    tenant_id: str = "common",
    client_secret: Optional[str] = None,
    timeout: int = 10,
) -> Dict[str, object]:
    """
    Внутренняя реализация health-check с указанием причины сбоя.
    :return: {"active": bool, "reason": Optional[str]}.
    """
    try:
        access_token = get_access_token(
            client_id=client_id,
            refresh_token=refresh_token,
            tenant_id=tenant_id,
            client_secret=client_secret,
            timeout=timeout,
            force_refresh=True,  # не полагаемся на потенциально устаревший кэш
        )
    except GraphAuthError as exc:
        return {"active": False, "reason": f"Ошибка обмена токена: {exc}"}

    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        # Через _request_with_retry (а не сырой requests.get): при массовой
        # параллельной проверке сотен ящиков транзиентный 429 от Graph не
        # должен ошибочно помечать рабочий ящик как неактивный.
        response = _request_with_retry(
            "GET", f"{GRAPH_BASE_URL}/me/mailFolders/inbox",
            headers=headers, params={"$select": "id"}, timeout=timeout,
        )
    except requests.RequestException as exc:
        return {"active": False, "reason": f"Сетевая ошибка: {exc}"}

    if response.status_code == 200:
        return {"active": True, "reason": None}

    reason = f"Graph вернул {response.status_code}: {response.text[:200]}"
    return {"active": False, "reason": reason}


def check_mailbox_health(
    client_id: str,
    refresh_token: str,
    tenant_id: str = "common",
    client_secret: Optional[str] = None,
    timeout: int = 10,
) -> bool:
    """
    Лёгкая проверка "жив ли ящик" — для периодической простановки поля
    active в БД. Делает минимум: обмен refresh_token -> access_token
    (ловит невалидный/отозванный refresh_token, неверный client_id) +
    один дешёвый запрос метаданных папки Inbox (ловит баны, отключённые
    лицензии, заблокированные ящики — то, что валидный токен сам по себе
    не покажет).

    Для проверки сразу многих ящиков используйте check_mailboxes_bulk —
    он делает то же самое, но параллельно.

    :return: True если ящик доступен и рабочий, False в любом другом случае.
    """
    return bool(
        _check_mailbox_health_detailed(
            client_id, refresh_token, tenant_id, client_secret, timeout,
        )["active"]
    )


def check_mailboxes_bulk(
    mailboxes: List[Dict[str, str]],
    max_workers: int = 20,
    timeout: int = 10,
) -> List[Dict[str, object]]:
    """
    Параллельно проверяет список ящиков Outlook (health-check) — удобно
    для регулярного прогона по всей базе (например, 500 старых ящиков).

    :param mailboxes: список словарей, каждый с ключами:
        'email'          — любой уникальный идентификатор для результата
                            (email, id записи в БД и т.п.), опционально —
                            если не задан, используется client_id.
        'client_id'      — обязателен.
        'refresh_token'  — обязателен.
        'tenant_id'      — опционально, по умолчанию 'common'.
        'client_secret'  — опционально.
    :param max_workers: сколько проверок выполнять одновременно. Слишком
        большое значение может привести к throttling (429) от Azure AD /
        Graph, особенно если много ящиков используют один и тот же
        client_id — начните с 10-20 и увеличивайте только если в логах
        нет предупреждений о повторах.
    :param timeout: таймаут (в секундах) на один HTTP-запрос.
    :return: список словарей [{'email': ..., 'active': bool, 'reason': Optional[str]}, ...]
        В ТОМ ЖЕ порядке, что и на входе.
    """
    # cnt = 555
    # save_count_file(cnt)

    results: List[Optional[Dict[str, object]]] = [None] * len(mailboxes)

    def _worker(idx: int, mailbox: Dict[str, str]) -> None:
        email = mailbox.get("email") or mailbox.get("client_id") or f"mailbox_{idx}"
        idb = mailbox.get("id")
        try:
            detail = _check_mailbox_health_detailed(
                client_id=mailbox["client_id"],
                refresh_token=mailbox["refresh_token"],
                tenant_id=mailbox.get("tenant_id", "common"),
                client_secret=mailbox.get("client_secret"),
                timeout=timeout,
            )
        except Exception as exc:  # защита от неожиданных ошибок (например, отсутствующих ключей)
            logger.exception("Непредвиденная ошибка проверки ящика %s", email)
            detail = {"active": False, "reason": f"Непредвиденная ошибка: {exc}"}
        results[idx] = {"id": idb, "email": email, **detail}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_worker, i, mb) for i, mb in enumerate(mailboxes)]
        for future in as_completed(futures):
            future.result()  # пробрасывает исключения из _worker, если вдруг там баг

    return results  # type: ignore[return-value]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # rooucsdox7268@hotmail.com
    CLIENT_ID = "8b4ba9dd-3ea5-4e5f-86f1-ddba2230dcf2"
    REFRESH_TOKEN = os.environ["OUTLOOK_REFRESH_TOKEN"]
    TENANT_ID = "common"  # или конкретный tenant id

    # 1. Сгруппировать все письма по теме
    # mails_by_subject = check_mail_by_subject(CLIENT_ID, REFRESH_TOKEN, TENANT_ID)
    # for subj, letters in mails_by_subject.items():
    #     print(f"{subj}: {len(letters)} писем")

    # 2. Найти письма по теме (точное совпадение)
    # found = get_mail_by_subject(CLIENT_ID, REFRESH_TOKEN, "Тестовая тема", tenant_id=TENANT_ID)

    # 3. Найти письма от отправителя (частичное совпадение, без учёта регистра)
    # found = get_mail_by_sender(
    #     CLIENT_ID, REFRESH_TOKEN, "spam@example.com",
    #     match_mode="contains", tenant_id=TENANT_ID,
    # )

    # 4. Удалить письма по теме за последние 7 дней
    # from datetime import datetime, timedelta, timezone
    # deleted = delete_mail_by_subject(
    #     CLIENT_ID, REFRESH_TOKEN, "Рекламная рассылка",
    #     match_mode="contains",
    #     date_from=datetime.now(timezone.utc) - timedelta(days=7),
    #     tenant_id=TENANT_ID,
    # )
    # print(deleted)  # {'inbox': 3, 'junkemail': 1}

    # 5. "Мягко удалить" (переместить в архив) письма от отправителя
    # moved = move_mail_by_sender(
    #     CLIENT_ID, REFRESH_TOKEN, "newsletter@example.com",
    #     target_folder="archive", tenant_id=TENANT_ID,
    # )
    # print(moved)

    # 6. Полностью очистить Спам
    # deleted_count = empty_junk_folder(CLIENT_ID, REFRESH_TOKEN, TENANT_ID)
    # print(f"Удалено из спама: {deleted_count}")

    # 7. Проверка "жив ли ящик" — один
    # is_active = check_mailbox_health(CLIENT_ID, REFRESH_TOKEN, TENANT_ID)
    # print(f"Ящик активен: {is_active}")

    # 8. Проверка "жив ли ящик" — массово, параллельно (например, 500 ящиков из БД)
    # mailboxes = [
    #     {"email": "user1@outlook.com", "client_id": CLIENT_ID, "refresh_token": "rt1", "tenant_id": "common"},
    #     {"email": "user2@outlook.com", "client_id": CLIENT_ID, "refresh_token": "rt2", "tenant_id": "common"},
    #     # ...
    # ]
    # health = check_mailboxes_bulk(mailboxes, max_workers=20)
    # for row in health:
    #     print(row)  # {'email': ..., 'active': True/False, 'reason': None или текст ошибки}
    #     # тут же можно сразу писать row['active'] в поле active записи БД по row['email']
