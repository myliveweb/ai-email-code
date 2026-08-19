"""
Модуль для работы с почтой Rambler (папки "Входящие" и "Спам")
через IMAP (логин/пароль) — функциональный аналог outlook_mail_checker.py,
но без OAuth: используется только чистый imaplib (стандартная библиотека).

Возможности:
    - Подключение по IMAP (imap.rambler.ru:993, SSL) с ретраями при
      сетевых сбоях / обрывах соединения (переподключение + backoff).
      Неверный логин/пароль распознаётся отдельно (ImapPermanentAuthError)
      и НЕ ретраится — с теми же данными результат не изменится.
    - Одно соединение переиспользуется на все папки в рамках одного
      вызова функции (аналог кэширования токена — здесь просто нет
      дорогого обмена токенами, поэтому кэшировать нечего, но
      повторный логин на каждую папку тоже не делается).
    - Получение писем без пагинации: одним SEARCH ALL + одним FETCH
      сразу по всем найденным UID.
    - Поиск / удаление / перемещение (soft-delete) писем по subject
      или по отправителю, с поддержкой:
        * режимов сравнения: exact / contains / startswith / regex
        * фильтра по диапазону дат получения
    - Массовые операции через нативные IMAP UID-множества
      (STORE/COPY/MOVE одной командой на все uid сразу — аналог
      Graph $batch, но без чанкинга по 20, IMAP это не требует).
    - Автоматическое определение папки "Спам" по списку кандидатов
      (папка может называться "Спам", "Spam", "Junk" и т.п. в
      зависимости от локали ящика), с декодированием modified UTF-7.
    - empty_junk_folder — полная очистка папки "Спам".
    - Автосоздание папки назначения при перемещении, если её ещё нет.
    - check_mailbox_health / check_mailboxes_bulk — проверка "жив ли ящик"
      (для поля active в БД), в том числе параллельно для сотен ящиков.

Идемпотентность удаления/перемещения: если UID уже отсутствует в папке
(письмо кем-то удалено/перемещено раньше), IMAP-сервер просто
игнорирует такой uid в STORE/COPY, не возвращая ошибку — в отличие от
Graph API, здесь для этого не нужна отдельная обработка 404.

Установка зависимостей: не требуется, используется только стандартная
библиотека Python (imaplib, email).
"""

from __future__ import annotations

import base64
import email
import imaplib
import logging
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

IMAP_HOST = "imap.rambler.ru"
IMAP_PORT = 993

# INBOX — зарезервированное имя по стандарту IMAP (RFC 3501), всегда так и называется
INBOX_FOLDER = "INBOX"

# Папка "Спам" в разных локалях/клиентах может называться по-разному —
# ищем среди списка папок ящика первое совпадение (без учёта регистра)
SPAM_FOLDER_CANDIDATES = [
    "спам", "spam", "junk", "junk e-mail", "junk-e-mail", "junk email",
    "нежелательная почта",
]

# Папка назначения по умолчанию для "мягкого удаления" (перемещения).
# Если такой папки нет — будет создана автоматически.
DEFAULT_TARGET_FOLDER = "Archive"

# Настройки ретраев (переподключение при сбоях)
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 1.0  # 1s, 2s, 4s, 8s, 16s...

# Допустимые режимы сравнения текста
MATCH_MODES = ("exact", "contains", "startswith", "regex")


class ImapAuthError(Exception):
    """Ошибка подключения/авторизации на IMAP-сервере."""


class ImapPermanentAuthError(ImapAuthError):
    """
    Неверный логин/пароль (или иная постоянная ошибка авторизации).
    В отличие от ImapAuthError, повторные попытки подключения бессмысленны —
    при следующей попытке с теми же учётными данными результат будет тем же.
    """


class ImapRequestError(Exception):
    """Ошибка выполнения IMAP-команды."""


# Подстроки (в нижнем регистре), по которым распознаём "постоянную" ошибку
# авторизации — при её появлении ретраить дальше не нужно, это только
# впустую тратит время (5 попыток с backoff ~ 30 секунд).
_PERMANENT_AUTH_MARKERS = (
    "authenticationfailed",
    "authentication failed",
    "invalid credentials",
    "invalid login",
    "invalid user name or password",
    "incorrect username or password",
    "login failed",
    "logon failure",
    "auth failure",
    "wrong password",
    "неверный пароль",
    "неверный логин",
    "неверные учетные данные",
    "неверные учётные данные",
)


def _is_permanent_auth_failure(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _PERMANENT_AUTH_MARKERS)


# ---------------------------------------------------------------------------
# Modified UTF-7 (RFC 3501) — кодирование/декодирование имён папок IMAP
# ---------------------------------------------------------------------------


def _b64_encode_chunk(chunk: str) -> str:
    raw = chunk.encode("utf-16-be")
    b64 = base64.b64encode(raw).decode("ascii").rstrip("=")
    b64 = b64.replace("/", ",")
    return "&" + b64 + "-"


def imap_utf7_encode(text: str) -> str:
    """Кодирует обычную Unicode-строку в modified UTF-7 (имя папки IMAP)."""
    result: List[str] = []
    buffer = ""
    for ch in text:
        if ch == "&":
            if buffer:
                result.append(_b64_encode_chunk(buffer))
                buffer = ""
            result.append("&-")
        elif " " <= ch <= "~":  # печатаемый ASCII
            if buffer:
                result.append(_b64_encode_chunk(buffer))
                buffer = ""
            result.append(ch)
        else:
            buffer += ch
    if buffer:
        result.append(_b64_encode_chunk(buffer))
    return "".join(result)


def imap_utf7_decode(text: str) -> str:
    """Декодирует имя папки IMAP из modified UTF-7 в обычную Unicode-строку."""
    result: List[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "&":
            j = text.find("-", i + 1)
            if j == -1:
                j = n
            chunk = text[i + 1 : j]
            if chunk == "":
                result.append("&")
            else:
                b64 = chunk.replace(",", "/")
                b64 += "=" * (-len(b64) % 4)
                try:
                    raw = base64.b64decode(b64)
                    result.append(raw.decode("utf-16-be"))
                except Exception:
                    result.append(text[i : j + 1])
            i = j + 1
        else:
            result.append(ch)
            i += 1
    return "".join(result)


def _decode_mime_header(value: Optional[str]) -> str:
    """Декодирует MIME-заголовок (Subject/From), возможно закодированный RFC 2047."""
    if not value:
        return ""
    decoded = []
    for text, enc in decode_header(value):
        if isinstance(text, bytes):
            try:
                decoded.append(text.decode(enc or "utf-8", errors="replace"))
            except (LookupError, TypeError):
                decoded.append(text.decode("utf-8", errors="replace"))
        else:
            decoded.append(text)
    return "".join(decoded)


def _ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Подключение и ретраи
# ---------------------------------------------------------------------------


def _connect(login: str, password: str, host: str = IMAP_HOST, port: int = IMAP_PORT, timeout: int = 15) -> imaplib.IMAP4_SSL:
    try:
        conn = imaplib.IMAP4_SSL(host, port, timeout=timeout)
        typ, resp = conn.login(login, password)
    except (imaplib.IMAP4.error, OSError, socket.timeout) as exc:
        message = str(exc)
        if _is_permanent_auth_failure(message):
            raise ImapPermanentAuthError(
                f"Неверный логин или пароль для {login}@{host}: {exc}"
            ) from exc
        raise ImapAuthError(f"Не удалось подключиться к {host}:{port}: {exc}") from exc

    if typ != "OK":
        message = resp[0].decode(errors="replace") if resp and isinstance(resp[0], bytes) else str(resp)
        if _is_permanent_auth_failure(message):
            raise ImapPermanentAuthError(f"Неверный логин или пароль для {login}@{host}: {message}")
        raise ImapAuthError(f"Не удалось авторизоваться на {host}: {resp}")

    return conn


def _run_with_retry(login: str, password: str, func, *args, host: str = IMAP_HOST, port: int = IMAP_PORT, **kwargs):
    """
    Открывает IMAP-соединение, выполняет func(conn, *args, **kwargs) и закрывает
    соединение. При сетевых сбоях / обрывах — переподключается и повторяет
    (до MAX_RETRIES раз, с экспоненциальным backoff).

    Если причина ошибки — неверный логин/пароль (ImapPermanentAuthError),
    повторные попытки НЕ выполняются: с теми же учётными данными результат
    при следующей попытке будет тем же, а ждать 30+ секунд ретраев бессмысленно.
    Исключение поднимается сразу же, при первой же неудачной попытке.
    """
    last_exc: Optional[Exception] = None

    for attempt in range(1, MAX_RETRIES + 1):
        conn = None
        try:
            conn = _connect(login, password, host=host, port=port)
            return func(conn, *args, **kwargs)
        except ImapPermanentAuthError as exc:
            logger.error(
                "Постоянная ошибка авторизации — повторные попытки бессмысленны, прекращаем: %s",
                exc,
            )
            raise
        except (imaplib.IMAP4.abort, imaplib.IMAP4.error, ImapAuthError, ImapRequestError, OSError, socket.timeout) as exc:
            last_exc = exc
            if attempt == MAX_RETRIES:
                logger.error("IMAP-операция не удалась после %s попыток: %s", MAX_RETRIES, exc)
                raise
            delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "Ошибка IMAP (попытка %s/%s): %s. Повтор через %.1fs",
                attempt, MAX_RETRIES, exc, delay,
            )
            time.sleep(delay)
        finally:
            if conn is not None:
                try:
                    conn.logout()
                except Exception:
                    pass

    raise last_exc  # pragma: no cover — сюда не должны попасть


# ---------------------------------------------------------------------------
# Папки: список, поиск "Спам", автосоздание
# ---------------------------------------------------------------------------

_LIST_LINE_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?:"(?P<delim>[^"]*)"|(?P<delim2>\S+))\s+(?P<name>.+)$')


def _parse_list_line(line: bytes) -> Optional[str]:
    if not line:
        return None
    m = _LIST_LINE_RE.match(line)
    if not m:
        return None
    name = m.group("name").decode(errors="replace").strip()
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1]
    return name


def _list_folders(conn: imaplib.IMAP4_SSL) -> List[str]:
    typ, data = conn.list()
    if typ != "OK":
        raise ImapRequestError("Не удалось получить список папок")
    names = []
    for line in data:
        name = _parse_list_line(line)
        if name:
            names.append(name)
    return names


def _resolve_folder(conn: imaplib.IMAP4_SSL, candidates: List[str]) -> Optional[str]:
    """Ищет среди папок ящика первую, чьё декодированное имя совпадает с одним
    из кандидатов (без учёта регистра). Возвращает "сырое" (wire) имя,
    пригодное для передачи в SELECT/COPY."""
    candidates_lower = {c.lower() for c in candidates}
    for raw_name in _list_folders(conn):
        if imap_utf7_decode(raw_name).lower() in candidates_lower:
            return raw_name
    return None


def _get_folder_map(conn: imaplib.IMAP4_SSL) -> Dict[str, Optional[str]]:
    spam_raw = _resolve_folder(conn, SPAM_FOLDER_CANDIDATES)
    if spam_raw is None:
        logger.warning(
            "Папка 'Спам' не найдена автоматически среди папок ящика — "
            "проверьте SPAM_FOLDER_CANDIDATES."
        )
    return {"inbox": INBOX_FOLDER, "spam": spam_raw}


def _ensure_folder_exists(conn: imaplib.IMAP4_SSL, folder_encoded: str) -> None:
    typ, _ = conn.select(f'"{folder_encoded}"', readonly=True)
    if typ == "OK":
        return
    typ, resp = conn.create(f'"{folder_encoded}"')
    if typ != "OK":
        raise ImapRequestError(f"Не удалось создать папку '{folder_encoded}': {resp}")


# ---------------------------------------------------------------------------
# Получение писем (без пагинации) и фильтрация
# ---------------------------------------------------------------------------


def _fetch_folder_messages(
    conn: imaplib.IMAP4_SSL,
    folder_raw: str,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> List[dict]:
    """
    Получает все письма папки одним SEARCH + одним FETCH (без пагинации).
    Возвращает список словарей: {uid, subject, from, date, folder}.
    """
    typ, _ = conn.select(f'"{folder_raw}"', readonly=True)
    if typ != "OK":
        raise ImapRequestError(f"Не удалось открыть папку '{folder_raw}'")

    typ, data = conn.uid("search", None, "ALL")
    if typ != "OK":
        raise ImapRequestError(f"Ошибка поиска писем в папке '{folder_raw}'")

    uid_list = data[0].split() if data and data[0] else []
    if not uid_list:
        return []

    uid_set = b",".join(uid_list).decode()
    typ, msg_data = conn.uid(
        "fetch", uid_set, "(BODY.PEEK[])"
    )
    if typ != "OK":
        raise ImapRequestError(f"Ошибка получения писем из папки '{folder_raw}'")

    messages: List[dict] = []
    for item in msg_data:
        if not isinstance(item, tuple):
            continue
        meta, raw_msg = item
        uid_match = re.search(rb"UID (\d+)", meta)
        if not uid_match:
            continue
        uid = uid_match.group(1).decode()

        msg = email.message_from_bytes(raw_msg)
        subject = _decode_mime_header(msg.get("Subject"))
        from_header = _decode_mime_header(msg.get("From"))
        _, sender_email = parseaddr(from_header)

        received_at: Optional[datetime] = None
        date_header = msg.get("Date")
        if date_header:
            try:
                received_at = parsedate_to_datetime(date_header)
            except (TypeError, ValueError):
                received_at = None

        if date_from is not None and (received_at is None or received_at < _ensure_aware(date_from)):
            continue
        if date_to is not None and (received_at is None or received_at > _ensure_aware(date_to)):
            continue

        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    break
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                body = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")

        messages.append({
            "uid": uid,
            "subject": subject,
            "from": sender_email,
            "date": received_at,
            "folder": folder_raw,
            "body": body,
        })

    return messages


def _matches(value: Optional[str], pattern: str, mode: str, case_sensitive: bool) -> bool:
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
    conn: imaplib.IMAP4_SSL,
    folder_raw: str,
    *,
    subject: Optional[str] = None,
    sender: Optional[str] = None,
    match_mode: str = "exact",
    case_sensitive: bool = True,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> List[dict]:
    messages = _fetch_folder_messages(conn, folder_raw, date_from=date_from, date_to=date_to)

    if subject is None and sender is None:
        return messages

    matched = []
    for message in messages:
        ok = True
        if subject is not None:
            ok = ok and _matches(message.get("subject"), subject, match_mode, case_sensitive)
        if sender is not None:
            ok = ok and _matches(message.get("from"), sender, match_mode, case_sensitive)
        if ok:
            matched.append(message)
    return matched


# ---------------------------------------------------------------------------
# Массовые операции (удаление / перемещение) через UID-множества
# ---------------------------------------------------------------------------


def _bulk_delete_uids(conn: imaplib.IMAP4_SSL, folder_raw: str, uids: List[str]) -> int:
    if not uids:
        return 0

    typ, _ = conn.select(f'"{folder_raw}"', readonly=False)
    if typ != "OK":
        raise ImapRequestError(f"Не удалось открыть папку '{folder_raw}' для удаления")

    uid_set = ",".join(uids)
    typ, resp = conn.uid("store", uid_set, "+FLAGS", "(\\Deleted)")
    if typ != "OK":
        raise ImapRequestError(f"Ошибка пометки писем на удаление в '{folder_raw}': {resp}")

    typ, resp = conn.expunge()
    if typ != "OK":
        raise ImapRequestError(f"Ошибка EXPUNGE в '{folder_raw}': {resp}")

    return len(uids)


def _bulk_move_uids(conn: imaplib.IMAP4_SSL, folder_raw: str, uids: List[str], target_folder: str) -> int:
    if not uids:
        return 0

    target_encoded = imap_utf7_encode(target_folder)
    _ensure_folder_exists(conn, target_encoded)

    typ, _ = conn.select(f'"{folder_raw}"', readonly=False)
    if typ != "OK":
        raise ImapRequestError(f"Не удалось открыть папку '{folder_raw}' для перемещения")

    uid_set = ",".join(uids)

    # UID MOVE (RFC 6851), если сервер его поддерживает — один запрос
    if "MOVE" in (conn.capabilities or ()):
        typ, resp = conn.uid("move", uid_set, f'"{target_encoded}"')
        if typ != "OK":
            raise ImapRequestError(
                f"Ошибка перемещения писем из '{folder_raw}' в '{target_folder}': {resp}"
            )
        return len(uids)

    # Fallback для серверов без MOVE: COPY + пометка \Deleted + EXPUNGE
    typ, resp = conn.uid("copy", uid_set, f'"{target_encoded}"')
    if typ != "OK":
        raise ImapRequestError(
            f"Ошибка копирования писем из '{folder_raw}' в '{target_folder}': {resp}"
        )

    typ, resp = conn.uid("store", uid_set, "+FLAGS", "(\\Deleted)")
    if typ != "OK":
        raise ImapRequestError(f"Ошибка пометки на удаление после копирования: {resp}")

    typ, resp = conn.expunge()
    if typ != "OK":
        raise ImapRequestError(f"Ошибка EXPUNGE после перемещения: {resp}")

    return len(uids)


def _delete_matching(conn: imaplib.IMAP4_SSL, folder_raw: str, **find_kwargs) -> int:
    messages = _find_messages(conn, folder_raw, **find_kwargs)
    return _bulk_delete_uids(conn, folder_raw, [m["uid"] for m in messages])


def _move_matching(conn: imaplib.IMAP4_SSL, folder_raw: str, target_folder: str, **find_kwargs) -> int:
    messages = _find_messages(conn, folder_raw, **find_kwargs)
    return _bulk_move_uids(conn, folder_raw, [m["uid"] for m in messages], target_folder)


# ---------------------------------------------------------------------------
# Внутренние "исполнители" — работают с уже открытым conn, вызываются через _run_with_retry
# ---------------------------------------------------------------------------


def _do_check_mail_by_subject(conn: imaplib.IMAP4_SSL) -> Dict[str, List[dict]]:
    result: Dict[str, List[dict]] = {}
    for label, folder_raw in _get_folder_map(conn).items():
        if folder_raw is None:
            logger.warning("Папка '%s' не найдена, пропускаем", label)
            continue
        try:
            messages = _fetch_folder_messages(conn, folder_raw)
        except ImapRequestError as exc:
            logger.warning("Пропускаем папку '%s': %s", label, exc)
            continue
        for message in messages:
            subject = message.get("subject") or "(без темы)"
            result.setdefault(subject, []).append(message)
    return result


def _do_get_mail(conn: imaplib.IMAP4_SSL, **find_kwargs) -> List[dict]:
    print('Start _do_get_mail')
    found: List[dict] = []
    for label, folder_raw in _get_folder_map(conn).items():
        if folder_raw is None:
            continue
        try:
            matched = _find_messages(conn, folder_raw, **find_kwargs)
        except ImapRequestError as exc:
            logger.warning("Пропускаем папку '%s': %s", label, exc)
            continue
        found.extend(matched)
        logger.info("Папка '%s': найдено %s писем", label, len(matched))
    return found


def _do_delete_mail(conn: imaplib.IMAP4_SSL, **find_kwargs) -> Dict[str, int]:
    deleted: Dict[str, int] = {}
    for label, folder_raw in _get_folder_map(conn).items():
        if folder_raw is None:
            deleted[label] = 0
            continue
        try:
            deleted[label] = _delete_matching(conn, folder_raw, **find_kwargs)
        except ImapRequestError as exc:
            logger.warning("Пропускаем папку '%s': %s", label, exc)
            deleted[label] = 0
    return deleted


def _do_move_mail(conn: imaplib.IMAP4_SSL, target_folder: str, **find_kwargs) -> Dict[str, int]:
    moved: Dict[str, int] = {}
    for label, folder_raw in _get_folder_map(conn).items():
        if folder_raw is None:
            moved[label] = 0
            continue
        try:
            moved[label] = _move_matching(conn, folder_raw, target_folder, **find_kwargs)
        except ImapRequestError as exc:
            logger.warning("Пропускаем папку '%s': %s", label, exc)
            moved[label] = 0
    return moved


def _do_empty_junk_folder(conn: imaplib.IMAP4_SSL) -> int:
    folder_raw = _get_folder_map(conn).get("spam")
    if folder_raw is None:
        logger.warning("Папка 'Спам' не найдена — нечего очищать")
        return 0
    return _delete_matching(conn, folder_raw)


# ---------------------------------------------------------------------------
# Публичные функции
# ---------------------------------------------------------------------------


def check_mail_by_subject(
    login: str, password: str, host: str = IMAP_HOST, port: int = IMAP_PORT,
) -> Dict[str, List[dict]]:
    """Группирует ВСЕ письма из "Входящие" и "Спам" по теме: {subject: [письма]}."""
    return _run_with_retry(login, password, _do_check_mail_by_subject, host=host, port=port)


def get_mail_by_subject(
    login: str, password: str, subject: str,
    match_mode: str = "exact", case_sensitive: bool = True,
    date_from: Optional[datetime] = None, date_to: Optional[datetime] = None,
    host: str = IMAP_HOST, port: int = IMAP_PORT,
) -> List[dict]:
    """Возвращает списком письма, чей subject соответствует критерию."""
    return _run_with_retry(
        login, password, _do_get_mail, host=host, port=port,
        subject=subject, match_mode=match_mode, case_sensitive=case_sensitive,
        date_from=date_from, date_to=date_to,
    )


def get_mail_by_sender(
    login: str, password: str, sender: str,
    match_mode: str = "exact", case_sensitive: bool = False,
    date_from: Optional[datetime] = None, date_to: Optional[datetime] = None,
    host: str = IMAP_HOST, port: int = IMAP_PORT,
) -> List[dict]:
    """Возвращает списком письма от заданного отправителя (email)."""
    return _run_with_retry(
        login, password, _do_get_mail, host=host, port=port,
        sender=sender, match_mode=match_mode, case_sensitive=case_sensitive,
        date_from=date_from, date_to=date_to,
    )


def delete_mail_by_subject(
    login: str, password: str, subject: str,
    match_mode: str = "exact", case_sensitive: bool = True,
    date_from: Optional[datetime] = None, date_to: Optional[datetime] = None,
    host: str = IMAP_HOST, port: int = IMAP_PORT,
) -> Dict[str, int]:
    """Безвозвратно удаляет письма с подходящим subject. :return: {'inbox': N, 'spam': N}."""
    return _run_with_retry(
        login, password, _do_delete_mail, host=host, port=port,
        subject=subject, match_mode=match_mode, case_sensitive=case_sensitive,
        date_from=date_from, date_to=date_to,
    )


def delete_mail_by_sender(
    login: str, password: str, sender: str,
    match_mode: str = "exact", case_sensitive: bool = False,
    date_from: Optional[datetime] = None, date_to: Optional[datetime] = None,
    host: str = IMAP_HOST, port: int = IMAP_PORT,
) -> Dict[str, int]:
    """Безвозвратно удаляет письма от заданного отправителя."""
    return _run_with_retry(
        login, password, _do_delete_mail, host=host, port=port,
        sender=sender, match_mode=match_mode, case_sensitive=case_sensitive,
        date_from=date_from, date_to=date_to,
    )


def move_mail_by_subject(
    login: str, password: str, subject: str,
    target_folder: str = DEFAULT_TARGET_FOLDER,
    match_mode: str = "exact", case_sensitive: bool = True,
    date_from: Optional[datetime] = None, date_to: Optional[datetime] = None,
    host: str = IMAP_HOST, port: int = IMAP_PORT,
) -> Dict[str, int]:
    """"Мягкое удаление": перемещает письма с подходящим subject в target_folder."""
    return _run_with_retry(
        login, password, _do_move_mail, host=host, port=port,
        target_folder=target_folder,
        subject=subject, match_mode=match_mode, case_sensitive=case_sensitive,
        date_from=date_from, date_to=date_to,
    )


def move_mail_by_sender(
    login: str, password: str, sender: str,
    target_folder: str = DEFAULT_TARGET_FOLDER,
    match_mode: str = "exact", case_sensitive: bool = False,
    date_from: Optional[datetime] = None, date_to: Optional[datetime] = None,
    host: str = IMAP_HOST, port: int = IMAP_PORT,
) -> Dict[str, int]:
    """"Мягкое удаление": перемещает письма от отправителя в target_folder."""
    return _run_with_retry(
        login, password, _do_move_mail, host=host, port=port,
        target_folder=target_folder,
        sender=sender, match_mode=match_mode, case_sensitive=case_sensitive,
        date_from=date_from, date_to=date_to,
    )


def empty_junk_folder(login: str, password: str, host: str = IMAP_HOST, port: int = IMAP_PORT) -> int:
    """Полностью очищает папку "Спам" — удаляет все письма без фильтра."""
    return _run_with_retry(login, password, _do_empty_junk_folder, host=host, port=port)


def _check_mailbox_health_detailed(
    login: str, password: str, host: str = IMAP_HOST, port: int = IMAP_PORT, timeout: int = 10,
) -> Dict[str, object]:
    """
    Внутренняя реализация health-check с указанием причины сбоя.
    :return: {"active": bool, "reason": Optional[str]}.
    """
    conn = None
    try:
        conn = _connect(login, password, host=host, port=port, timeout=timeout)
        typ, _ = conn.select(f'"{INBOX_FOLDER}"', readonly=True)
        if typ != "OK":
            return {"active": False, "reason": f"SELECT INBOX вернул {typ}"}
        return {"active": True, "reason": None}
    except (ImapAuthError, ImapRequestError, imaplib.IMAP4.error, imaplib.IMAP4.abort, OSError, socket.timeout) as exc:
        return {"active": False, "reason": str(exc)}
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass


def check_mailbox_health(
    login: str, password: str, host: str = IMAP_HOST, port: int = IMAP_PORT, timeout: int = 10,
) -> bool:
    """
    Лёгкая проверка "жив ли ящик" — для периодической простановки поля
    active в БД. Делает минимум: LOGIN + SELECT INBOX. Дешевле физически
    некуда — это буквально открытие соединения и обращение к папке.

    Намеренно БЕЗ ретраев (в отличие от _run_with_retry, используемого
    остальными функциями модуля): это разовая проверка "сейчас работает /
    не работает", а не операция, которую стоит пытаться спасти повторами —
    если сейчас не работает, считаем ящик неактивным, следующая проверка
    по расписанию перепроверит.

    Для проверки сразу многих ящиков используйте check_mailboxes_bulk —
    он делает то же самое, но параллельно.

    :return: True если ящик доступен и рабочий, False в любом другом случае
        (неверный пароль, бан, сетевая недоступность и т.п. — причина
        всегда попадает в лог на уровне INFO).
    """
    detail = _check_mailbox_health_detailed(login, password, host, port, timeout)
    if not detail["active"]:
        logger.info("Ящик %s неактивен: %s", login, detail["reason"])
    return bool(detail["active"])


def check_mailboxes_bulk(
    mailboxes: List[Dict[str, str]],
    max_workers: int = 20,
    timeout: int = 10,
) -> List[Dict[str, object]]:
    """
    Параллельно проверяет список ящиков Rambler (health-check) — удобно
    для регулярного прогона по всей базе (например, 500 старых ящиков).

    :param mailboxes: список словарей, каждый с ключами:
        'email'    — любой уникальный идентификатор для результата (можно
                     не задавать, тогда используется 'login').
        'login'    — обязателен (полный адрес почты).
        'password' — обязателен.
        'host', 'port' — опционально, если отличаются от imap.rambler.ru:993.
    :param max_workers: сколько проверок выполнять одновременно. Многие
        IMAP-серверы ограничивают число одновременных подключений С ОДНОГО
        IP (не с одного аккаунта!) — начните с 10-20, чтобы не словить
        временный бан по IP при проверке всех 500 ящиков разом.
    :param timeout: таймаут (в секундах) на подключение и команды.
    :return: список словарей [{'email': ..., 'active': bool, 'reason': Optional[str]}, ...]
        В ТОМ ЖЕ порядке, что и на входе.
    """
    results: List[Optional[Dict[str, object]]] = [None] * len(mailboxes)

    def _worker(idx: int, mailbox: Dict[str, str]) -> None:
        email_id = mailbox.get("email") or mailbox.get("login") or f"mailbox_{idx}"
        idb = mailbox.get("id")
        try:
            detail = _check_mailbox_health_detailed(
                login=mailbox["login"],
                password=mailbox["password"],
                host=mailbox.get("host", IMAP_HOST),
                port=mailbox.get("port", IMAP_PORT),
                timeout=timeout,
            )
        except Exception as exc:  # защита от неожиданных ошибок (например, отсутствующих ключей)
            logger.exception("Непредвиденная ошибка проверки ящика %s", email_id)
            detail = {"active": False, "reason": f"Непредвиденная ошибка: {exc}"}
        results[idx] = {"id": idb, "email": email_id, **detail}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_worker, i, mb) for i, mb in enumerate(mailboxes)]
        for future in as_completed(futures):
            future.result()  # пробрасывает исключения из _worker, если вдруг там баг

    return results  # type: ignore[return-value]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    LOGIN = "you@rambler.ru"
    PASSWORD = "your-password"

    # 1. Сгруппировать все письма по теме
    # mails_by_subject = check_mail_by_subject(LOGIN, PASSWORD)
    # for subj, letters in mails_by_subject.items():
    #     print(f"{subj}: {len(letters)} писем")

    # 2. Найти письма по теме (точное совпадение)
    # found = get_mail_by_subject(LOGIN, PASSWORD, "Тестовая тема")
    # found = get_mail_by_subject(LOGIN, PASSWORD, "[GitHub] Please verify your device")
    # print(found)

    # 3. Найти письма от отправителя (частичное совпадение)
    # found = get_mail_by_sender(LOGIN, PASSWORD, "spam@example.com", match_mode="contains")

    # 4. Удалить письма по теме за последние 7 дней
    # from datetime import datetime, timedelta, timezone
    # deleted = delete_mail_by_subject(
    #     LOGIN, PASSWORD, "Рекламная рассылка",
    #     match_mode="contains",
    #     date_from=datetime.now(timezone.utc) - timedelta(days=7),
    # )
    # print(deleted)  # {'inbox': 3, 'spam': 1}

    # 5. "Мягко удалить" (переместить в архив) письма от отправителя
    # moved = move_mail_by_sender(LOGIN, PASSWORD, "newsletter@example.com", target_folder="Archive")
    # print(moved)

    # 6. Полностью очистить Спам
    # deleted_count = empty_junk_folder(LOGIN, PASSWORD)
    # print(f"Удалено из спама: {deleted_count}")

    # 7. Проверка "жив ли ящик" — один
    # is_active = check_mailbox_health(LOGIN, PASSWORD)
    # print(f"Ящик активен: {is_active}")

    # 8. Проверка "жив ли ящик" — массово, параллельно (например, 500 ящиков из БД)
    # mailboxes = [
    #     {"email": "user1@rambler.ru", "login": "user1@rambler.ru", "password": "pass1"},
    #     {"email": "user2@rambler.ru", "login": "user2@rambler.ru", "password": "pass2"},
    #     # ...
    # ]
    # health = check_mailboxes_bulk(mailboxes, max_workers=20)
    # for row in health:
    #     print(row)  # {'email': ..., 'active': True/False, 'reason': None или текст ошибки}
    #     # тут же можно сразу писать row['active'] в поле active записи БД по row['email']
