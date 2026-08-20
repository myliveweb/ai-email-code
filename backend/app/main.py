import json
import re
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pydantic import BaseModel
from postgrest.exceptions import APIError

from backend.app.config import settings
from backend.app.credentials import make_password, make_username
from backend.app.supabase_client import get_supabase

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from outlook_mail_checker import get_mail_by_subject as outlook_get_mail
from rambler_imap_mail_checker import get_mail_by_subject as rambler_get_mail

app = FastAPI(title="AI Email Code API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.frontend_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


class HealthResponse(BaseModel):
    status: str
    supabase: str


class GithubAccount(BaseModel):
    id: int
    login: str | None = None
    email: str | None = None
    active: bool | None = None
    created_at: str | None = None


class GithubAccountFull(BaseModel):
    id: int
    login: str | None = None
    pass_github: str | None = None
    email: str | None = None
    pass_email: str | None = None
    restore_email: str | None = None
    restore_pass: str | None = None


class BrowseResponse(BaseModel):
    account: GithubAccountFull
    total: int
    offset: int


@app.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    try:
        get_supabase().table("main_github").select("id").limit(1).execute()
    except Exception as exc:
        logger.error(f"Supabase недоступна: {exc}")
        return HealthResponse(status="ok", supabase="error")

    return HealthResponse(status="ok", supabase="ok")


@app.get("/api/github/accounts", response_model=list[GithubAccount])
def github_accounts(limit: int = Query(default=10, ge=1, le=100)) -> list[GithubAccount]:
    try:
        res = (
            get_supabase()
            .table("main_github")
            .select("id, login, email, active, created_at")
            .order("id")
            .limit(limit)
            .execute()
        )
    except Exception as exc:
        logger.error(f"Не удалось прочитать main_github: {exc}")
        raise HTTPException(status_code=502, detail="База данных недоступна") from exc

    return [GithubAccount(**row) for row in res.data]


@app.get("/api/github/accounts/browse", response_model=BrowseResponse)
def browse_account(
    offset: int = Query(default=0, ge=0),
    after_id: int | None = Query(default=None),
    before_id: int | None = Query(default=None),
    from_id: int | None = Query(default=None),
    site_id: int | None = Query(default=None),
) -> BrowseResponse:
    try:
        sb = get_supabase()

        excluded_ids: set[int] = set()
        if site_id is not None:
            linked = sb.table("main_site_account").select("github_id").eq("site_id", site_id).execute()
            excluded_ids = {r["github_id"] for r in linked.data}

        base_query = (
            sb.table("main_github")
            .select("id", count="exact")
            .eq("active", True)
            .ilike("email", "%@hotmail.com")
            .is_("error_status", "null")
        )
        if excluded_ids:
            base_query = base_query.not_.in_("id", list(excluded_ids))
        count_res = base_query.execute()
        total = count_res.count

        fields = "id, login, pass_github, email, pass_email, restore_email, restore_pass"

        def _build(q):
            q = q.eq("active", True).ilike("email", "%@hotmail.com").is_("error_status", "null")
            if excluded_ids:
                q = q.not_.in_("id", list(excluded_ids))
            return q

        if after_id is not None:
            res = (
                _build(sb.table("main_github").select(fields))
                .gt("id", after_id)
                .order("id")
                .limit(1)
                .execute()
            )
        elif before_id is not None:
            res = (
                _build(sb.table("main_github").select(fields))
                .lt("id", before_id)
                .order("id", desc=True)
                .limit(1)
                .execute()
            )
        elif from_id is not None:
            res = (
                _build(sb.table("main_github").select(fields))
                .gte("id", from_id)
                .order("id")
                .limit(1)
                .execute()
            )
        else:
            res = (
                _build(sb.table("main_github").select(fields))
                .order("id")
                .range(offset, offset)
                .execute()
            )
    except Exception as exc:
        logger.error(f"browse error: {exc}")
        raise HTTPException(status_code=502, detail="База данных недоступна") from exc

    if not res.data:
        raise HTTPException(status_code=404, detail="Записей больше нет")

    return BrowseResponse(
        account=GithubAccountFull(**res.data[0]),
        total=total,
        offset=offset,
    )


@app.get("/api/github/accounts/{account_id}", response_model=BrowseResponse | None)
def get_account_by_id(account_id: int) -> BrowseResponse:
    try:
        sb = get_supabase()
        res = (
            sb.table("main_github")
            .select("id, login, pass_github, email, pass_email, restore_email, restore_pass, active")
            .eq("id", account_id)
            .execute()
        )
    except Exception as exc:
        logger.error(f"get by id error: {exc}")
        raise HTTPException(status_code=502, detail="База данных недоступна") from exc

    if not res.data:
        raise HTTPException(status_code=404, detail="Запись не найдена")

    row = res.data[0]
    if not row.get("active"):
        raise HTTPException(status_code=410, detail="Данные недоступны")

    row.pop("active", None)
    return BrowseResponse(
        account=GithubAccountFull(**row),
        total=0,
        offset=0,
    )


GITHUB_VERIFY_SUBJECT = "[GitHub] Please verify your device"
MAIL_CHECK_RESULT_FILE = Path(__file__).resolve().parents[2] / "log" / "mail_check_result.txt"

_VERIFICATION_CODE_RE = re.compile(r"Verification code:\s*(\d{6})")

# между якорем и кодом обычно только «пустой» текст: пробелы, двоеточие, html-теги,
# html-entity. Буквы в этот зазор не пускаем — иначе якорь «код» дотянется до
# «код ... телефона 8391» и подставит чужие цифры.
# полноширинная пунктуация нужна для китайских писем: «您的验证码为：»
_HTML_TAG = r"<[^<>]{0,200}>"
_ANCHOR_GAP_TIGHT = (
    r"(?:\s|&nbsp;|&#\d{2,6};|" + _HTML_TAG + r"|[:=~\-–—.,*|>«»\"'\[\](){}#：，。、；！　＝－])*"
)
# запасной зазор на случай, когда между якорем и кодом всё же есть слова.
# для буквенно-цифрового кода слова в зазоре недопустимы — шаблон схватил бы
# кусок самого слова, но html-теги пропускать надо: в них есть буквы (<strong>)
_ANCHOR_GAP_LOOSE_DIGITS = r"\D{0,40}?"
_ANCHOR_GAP_LOOSE_ALNUM = r"(?:" + _HTML_TAG + r"|[^A-Za-z0-9]){0,40}?"


def _code_token(code_length: int | None, alnum: bool) -> str:
    if not alnum:
        if code_length:
            return rf"(?<!\d)(\d{{{code_length}}})(?!\d)"
        return r"(?<!\d)(\d{3,12})(?!\d)"
    n = code_length or 6
    # обычное слово той же длины отсекается требованием хотя бы одной цифры внутри
    has_digit = rf"(?=[A-Za-z0-9]{{0,{n - 1}}}\d)"
    return rf"(?<![A-Za-z0-9]){has_digit}([A-Za-z0-9]{{{n}}})(?![A-Za-z0-9])"


def _build_code_patterns(
    anchor: str | None,
    code_length: int | None,
    code_format: str | None,
) -> list[re.Pattern]:
    alnum = code_format == "alnum"
    token = _code_token(code_length, alnum)
    if anchor:
        flags = re.IGNORECASE | re.DOTALL
        head = re.escape(anchor.strip())
        loose = _ANCHOR_GAP_LOOSE_ALNUM if alnum else _ANCHOR_GAP_LOOSE_DIGITS
        return [re.compile(head + gap + token, flags) for gap in (_ANCHOR_GAP_TIGHT, loose)]
    if alnum:
        # без якоря буквенно-цифровой код не отличить от обрывка ссылки или id,
        # поэтому ищем только рядом со словом code/код
        return [re.compile(r"(?:code|код)\w*" + _ANCHOR_GAP_TIGHT + token, re.IGNORECASE | re.DOTALL)]
    if code_length:
        return [re.compile(token)]
    return [_VERIFICATION_CODE_RE]


def _extract_verification_code(
    messages: list,
    code_length: int | None = None,
    code_anchor: str | None = None,
    code_format: str | None = None,
) -> str | None:
    patterns = _build_code_patterns(code_anchor, code_length, code_format)
    sorted_msgs = sorted(messages, key=lambda m: m.get("date") or "", reverse=True)
    # строгий шаблон прогоняется по всем письмам раньше, чем в дело идёт запасной
    for pattern in patterns:
        for msg in sorted_msgs:
            body = msg.get("body") or msg.get("bodyPreview") or ""
            m = pattern.search(body)
            if m:
                return m.group(1)
    return None


class CheckMailboxRequest(BaseModel):
    email: str
    type: str  # "outlook" | "rambler"
    subject: str | None = None
    code_length: int | None = None
    code_anchor: str | None = None
    code_format: str | None = None  # "digits" | "alnum"


class CheckMailboxResponse(BaseModel):
    status: str
    message: str
    code: str | None = None
    file: str


@app.post("/api/mail/check-mailbox", response_model=CheckMailboxResponse)
def check_mailbox(req: CheckMailboxRequest) -> CheckMailboxResponse:
    sb = get_supabase()

    subject = req.subject or GITHUB_VERIFY_SUBJECT
    # тему сайта Босс вводит руками и она может быть частью настоящей темы письма,
    # тогда как гитхабовская известна дословно
    match_mode = "contains" if req.subject else "exact"

    if req.type == "outlook":
        res = sb.table("main_email").select("*").eq("email", req.email).limit(1).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail=f"Email {req.email} не найден в main_email")
        row = res.data[0]
        client_id = row.get("client_id")
        refresh_token = row.get("graph_refresh_token")
        if not client_id or not refresh_token:
            raise HTTPException(status_code=422, detail="Нет client_id или refresh_token для этого email")
        try:
            result = outlook_get_mail(
                client_id=client_id,
                refresh_token=refresh_token,
                subject=subject,
                match_mode=match_mode,
            )
        except Exception as exc:
            error_text = f"Outlook ошибка для {req.email}: {exc}"
            MAIL_CHECK_RESULT_FILE.write_text(error_text, encoding="utf-8")
            logger.error(error_text)
            return CheckMailboxResponse(status="error", message=str(exc), file=str(MAIL_CHECK_RESULT_FILE))

    elif req.type == "rambler":
        res = sb.table("main_email").select("*").eq("email", req.email).limit(1).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail=f"Email {req.email} не найден в main_email")
        row = res.data[0]
        login = row.get("email")
        password = row.get("password")
        if not login or not password:
            raise HTTPException(status_code=422, detail="Нет email или password для этого ящика")
        try:
            result = rambler_get_mail(
                login=login,
                password=password,
                subject=subject,
                match_mode=match_mode,
            )
        except Exception as exc:
            error_text = f"Rambler ошибка для {req.email}: {exc}"
            MAIL_CHECK_RESULT_FILE.write_text(error_text, encoding="utf-8")
            logger.error(error_text)
            return CheckMailboxResponse(status="error", message=str(exc), file=str(MAIL_CHECK_RESULT_FILE))
    else:
        raise HTTPException(status_code=400, detail=f"Неизвестный тип: {req.type}")

    output = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    MAIL_CHECK_RESULT_FILE.write_text(output, encoding="utf-8")
    logger.info(f"Результат проверки {req.email} ({req.type}): {len(result)} писем, записано в {MAIL_CHECK_RESULT_FILE}")

    verification_code = _extract_verification_code(
        result, req.code_length, req.code_anchor, req.code_format
    )

    return CheckMailboxResponse(
        status="ok",
        message=f"Найдено писем: {len(result)}",
        code=verification_code,
        file=str(MAIL_CHECK_RESULT_FILE),
    )


class ErrorStatusRequest(BaseModel):
    email: str | None = None
    restore_email: str | None = None
    login: str | None = None
    action: str  # "Bad Rambler Email" | "Bad Github Account" | "Suspended Github" | "Flag Site"


@app.post("/api/github/accounts/set-error-status")
def set_error_status(req: ErrorStatusRequest):
    sb = get_supabase()

    if req.action == "Bad Rambler Email":
        restore_email = req.restore_email
        if req.login and not restore_email:
            res = (
                sb.table("main_github")
                .select("restore_email, email")
                .eq("login", req.login)
                .execute()
            )
            if not res.data:
                raise HTTPException(status_code=404, detail=f"login '{req.login}' не найден в main_github")
            row = res.data[0]
            # у рамблеровских аккаунтов рамблер лежит в email, а restore_email пуст
            target = row["restore_email"] or row["email"]
            sb.table("main_github").update(
                {"active": False, "error_status": "Bad Rambler Email"}
            ).eq("login", req.login).execute()
            if target:
                sb.table("main_email").update({"active": False}).eq("email", target).execute()
            return {"status": "ok", "message": f"main_github по login + main_email ({target or 'адрес не найден'})"}

        if not restore_email:
            raise HTTPException(status_code=400, detail="нужен restore_email или login для этого действия")
        res = sb.table("main_github").select("id").eq("restore_email", restore_email).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Запись не найдена в main_github")
        sb.table("main_github").update(
            {"active": False, "error_status": "Bad Rambler Email"}
        ).eq("restore_email", restore_email).execute()

        sb.table("main_email").update(
            {"active": False}
        ).eq("email", restore_email).execute()

        return {"status": "ok", "message": "main_github + main_email: active=false"}

    elif req.action == "Bad Github Account":
        if not req.email:
            raise HTTPException(status_code=400, detail="email обязателен для этого действия")
        res = sb.table("main_github").select("id").eq("email", req.email).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Запись не найдена в main_github")
        sb.table("main_github").update(
            {"active": False, "error_status": "Bad Github Account"}
        ).eq("email", req.email).execute()

        return {"status": "ok", "message": "main_github: active=false, error_status='Bad Github Account'"}

    elif req.action in ("Suspended Github", "Flag Site"):
        if not req.email:
            raise HTTPException(status_code=400, detail="email обязателен для этого действия")
        res = sb.table("main_github").select("id").eq("email", req.email).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Запись не найдена в main_github")
        sb.table("main_github").update(
            {"error_status": req.action}
        ).eq("email", req.email).execute()

        return {"status": "ok", "message": f"error_status='{req.action}'"}

    else:
        raise HTTPException(status_code=400, detail=f"Неизвестное действие: {req.action}")


class EmailAccountFull(BaseModel):
    id: int
    email: str | None = None
    password: str | None = None
    restore_email: str | None = None
    restore_pass: str | None = None
    secret: str | None = None


class EmailBrowseResponse(BaseModel):
    account: EmailAccountFull
    total: int
    offset: int


@app.get("/api/email/accounts/browse", response_model=EmailBrowseResponse)
def browse_email_account(
    offset: int = Query(default=0, ge=0),
    after_id: int | None = Query(default=None),
    before_id: int | None = Query(default=None),
    from_id: int | None = Query(default=None),
    site_id: int | None = Query(default=None),
    gmail: bool = Query(default=False),
) -> EmailBrowseResponse:
    try:
        sb = get_supabase()

        excluded_ids: set[int] = set()
        if site_id is not None:
            linked = (
                sb.table("main_site_account_custom")
                .select("email_id")
                .eq("site_id", site_id)
                .execute()
            )
            excluded_ids = {r["email_id"] for r in linked.data if r["email_id"]}

        def _build(q):
            q = q.eq("active", True)
            q = q.ilike("email", "%@gmail.com") if gmail else q.not_.ilike("email", "%@gmail.com")
            if excluded_ids:
                q = q.not_.in_("id", list(excluded_ids))
            return q

        total = _build(sb.table("main_email").select("id", count="exact")).execute().count

        fields = "id, email, password, restore_email, restore_pass, secret"

        if after_id is not None:
            res = _build(sb.table("main_email").select(fields)).gt("id", after_id).order("id").limit(1).execute()
        elif before_id is not None:
            res = _build(sb.table("main_email").select(fields)).lt("id", before_id).order("id", desc=True).limit(1).execute()
        elif from_id is not None:
            res = _build(sb.table("main_email").select(fields)).gte("id", from_id).order("id").limit(1).execute()
        else:
            res = _build(sb.table("main_email").select(fields)).order("id").range(offset, offset).execute()
    except Exception as exc:
        logger.error(f"email browse error: {exc}")
        raise HTTPException(status_code=502, detail="База данных недоступна") from exc

    if not res.data:
        raise HTTPException(status_code=404, detail="Записей больше нет")

    return EmailBrowseResponse(account=EmailAccountFull(**res.data[0]), total=total, offset=offset)


class EmailErrorStatusRequest(BaseModel):
    email: str
    action: str


@app.post("/api/email/accounts/set-error-status")
def set_email_error_status(req: EmailErrorStatusRequest):
    sb = get_supabase()
    res = sb.table("main_email").select("id").eq("email", req.email).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail=f"Email {req.email} не найден в main_email")
    sb.table("main_email").update(
        {"active": False, "reason": req.action}
    ).eq("email", req.email).execute()
    return {"status": "ok", "message": f"main_email: active=false, reason='{req.action}'"}


@app.get("/api/generate-credentials")
def generate_credentials(password_length: int = Query(default=10, ge=6, le=64)):
    return {"login": make_username(), "password": make_password(password_length)}


@app.get("/api/sites")
def list_sites():
    sb = get_supabase()
    res = sb.table("main_site").select("id, name, meta, mail_subject, code_anchor, code_length, code_format").order("name").execute()
    return res.data


class AddSiteRequest(BaseModel):
    name: str
    meta: dict = {}


@app.post("/api/sites")
def add_site(req: AddSiteRequest):
    sb = get_supabase()
    res = sb.table("main_site").insert({"name": req.name, "meta": req.meta}).execute()
    return res.data[0]


class UpdateSiteRequest(BaseModel):
    name: str | None = None
    meta: dict | None = None
    mail_subject: str | None = None
    code_anchor: str | None = None
    code_length: int | None = None
    code_format: str | None = None


@app.patch("/api/sites/{site_id}")
def update_site(site_id: int, req: UpdateSiteRequest):
    payload = req.model_dump(exclude_unset=True)
    if not payload:
        raise HTTPException(status_code=400, detail="Нечего обновлять")
    sb = get_supabase()
    res = sb.table("main_site").update(payload).eq("id", site_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail=f"Сайт id={site_id} не найден")
    return res.data[0]


@app.get("/api/sites/{site_id}/stats")
def site_stats(site_id: int):
    sb = get_supabase()
    github = sb.table("main_site_account").select("balance").eq("site_id", site_id).execute().data
    custom = sb.table("main_site_account_custom").select("balance").eq("site_id", site_id).execute().data
    return {
        "github_count": len(github),
        "email_count": len(custom),
        "total_count": len(github) + len(custom),
        "github_balance": sum(r["balance"] or 0 for r in github),
        "email_balance": sum(r["balance"] or 0 for r in custom),
        "total_balance": sum(r["balance"] or 0 for r in github) + sum(r["balance"] or 0 for r in custom),
    }


@app.delete("/api/sites/{site_id}")
def delete_site(site_id: int):
    sb = get_supabase()
    github = sb.table("main_site_account").select("id").eq("site_id", site_id).limit(1).execute().data
    custom = sb.table("main_site_account_custom").select("id").eq("site_id", site_id).limit(1).execute().data
    if github or custom:
        raise HTTPException(
            status_code=409,
            detail="Сначала удалите аккаунты этого сайта",
        )
    res = sb.table("main_site").delete().eq("id", site_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail=f"Сайт id={site_id} не найден")
    return {"deleted": site_id}


class SiteAccountRequest(BaseModel):
    site_id: int
    github_id: int
    login: str | None = None
    email: str | None = None
    token: str | None = None
    balance: float = 0
    aff: str | None = None
    note: str | None = None
    smart_link: bool = False


def _resolve_github_row(sb, login: str | None, email: str | None) -> dict:
    for field, value in (("login", login), ("email", email)):
        if not value:
            continue
        res = (
            sb.table("main_github")
            .select("id, login, email")
            .eq(field, value)
            .order("id")
            .limit(1)
            .execute()
        )
        if res.data:
            return res.data[0]
    raise HTTPException(
        status_code=404,
        detail="Умная привязка: в main_github нет записи с таким login или email",
    )


@app.post("/api/site-accounts")
def create_site_account(req: SiteAccountRequest):
    sb = get_supabase()
    github_id, login, email = req.github_id, req.login, req.email
    if req.smart_link:
        row = _resolve_github_row(sb, req.login, req.email)
        github_id = row["id"]
        login = login or row["login"]
        email = email or row["email"]
    try:
        res = sb.table("main_site_account").insert({
            "site_id": req.site_id,
            "github_id": github_id,
            "login": login,
            "email": email,
            "token": req.token,
            "balance": req.balance,
            "aff": req.aff,
            "note": req.note,
        }).execute()
    except APIError as e:
        if e.code == "23505":
            raise HTTPException(
                status_code=409,
                detail=f"На этом сайте уже есть аккаунт с login={login} или email={email}",
            ) from e
        raise
    sb.table("main_site").update(
        {"cnt": sb.table("main_site").select("cnt").eq("id", req.site_id).execute().data[0]["cnt"] + 1}
    ).eq("id", req.site_id).execute()
    return res.data[0]


class CustomAccountRequest(BaseModel):
    site_id: int
    email_id: int = 0
    login: str | None = None
    email: str | None = None
    password: str | None = None
    token: str | None = None
    balance: float = 0
    aff: str | None = None
    note: str | None = None
    smart_link: bool = False


def _resolve_email_row(sb, email: str | None) -> dict:
    if email:
        res = (
            sb.table("main_email")
            .select("id, email")
            .eq("email", email)
            .order("id")
            .limit(1)
            .execute()
        )
        if res.data:
            return res.data[0]
    raise HTTPException(
        status_code=404,
        detail=f"Умная привязка: в main_email нет записи с email={email}",
    )


@app.post("/api/site-accounts-custom")
def create_custom_account(req: CustomAccountRequest):
    sb = get_supabase()
    email_id, email = req.email_id, req.email
    if req.smart_link:
        row = _resolve_email_row(sb, req.email)
        email_id = row["id"]
        email = email or row["email"]
    try:
        res = sb.table("main_site_account_custom").insert({
            "site_id": req.site_id,
            "email_id": email_id,
            "login": req.login,
            "email": email,
            "password": req.password,
            "token": req.token,
            "balance": req.balance,
            "aff": req.aff,
            "note": req.note,
        }).execute()
    except APIError as e:
        if e.code == "23505":
            raise HTTPException(
                status_code=409,
                detail=f"На этом сайте уже есть аккаунт с email={email} или login={req.login}",
            ) from e
        raise
    return res.data[0]


@app.get("/api/site-accounts-custom")
def list_custom_accounts(site_id: int = Query(...)):
    sb = get_supabase()
    res = (
        sb.table("main_site_account_custom")
        .select("id, login, email, password, token, balance, aff, note, email_id")
        .eq("site_id", site_id)
        .order("id")
        .execute()
    )
    return res.data


@app.get("/api/site-accounts")
def list_site_accounts(site_id: int = Query(...)):
    sb = get_supabase()
    res = (
        sb.table("main_site_account")
        .select("id, login, email, token, balance, aff, note, github_id")
        .eq("site_id", site_id)
        .order("id")
        .execute()
    )
    return res.data


class UpdateSiteAccountRequest(BaseModel):
    login: str | None = None
    email: str | None = None
    token: str | None = None
    balance: float = 0
    aff: str | None = None
    note: str | None = None


@app.put("/api/site-accounts/{account_id}")
def update_site_account(account_id: int, req: UpdateSiteAccountRequest):
    sb = get_supabase()
    sb.table("main_site_account").update({
        "login": req.login,
        "email": req.email,
        "token": req.token,
        "balance": req.balance,
        "aff": req.aff,
        "note": req.note,
    }).eq("id", account_id).execute()
    return {"status": "ok"}


@app.delete("/api/site-accounts/{account_id}")
def delete_site_account(account_id: int, site_id: int = Query(...)):
    sb = get_supabase()
    sb.table("main_site_account").delete().eq("id", account_id).execute()
    cnt_res = sb.table("main_site").select("cnt").eq("id", site_id).execute()
    if cnt_res.data:
        new_cnt = max(0, cnt_res.data[0]["cnt"] - 1)
        sb.table("main_site").update({"cnt": new_cnt}).eq("id", site_id).execute()
    return {"status": "ok"}


class UpdateCustomAccountRequest(BaseModel):
    login: str | None = None
    email: str | None = None
    password: str | None = None
    token: str | None = None
    balance: float = 0
    aff: str | None = None
    note: str | None = None


@app.put("/api/site-accounts-custom/{account_id}")
def update_custom_account(account_id: int, req: UpdateCustomAccountRequest):
    sb = get_supabase()
    sb.table("main_site_account_custom").update({
        "login": req.login,
        "email": req.email,
        "password": req.password,
        "token": req.token,
        "balance": req.balance,
        "aff": req.aff,
        "note": req.note,
    }).eq("id", account_id).execute()
    return {"status": "ok"}


@app.delete("/api/site-accounts-custom/{account_id}")
def delete_custom_account(account_id: int):
    sb = get_supabase()
    sb.table("main_site_account_custom").delete().eq("id", account_id).execute()
    return {"status": "ok"}
