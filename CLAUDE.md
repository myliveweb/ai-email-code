# CLAUDE.md

Guidance for Claude Code (claude.ai/code) при работе с этим репозиторием.

## Что это за проект

Учёт аккаунтов на LLM-сайтах и автоматическая выборка кодов подтверждения из почты.
Регистрации идут через ящики Outlook (Microsoft Graph) и Rambler (IMAP). Сайт присылает
письмо с кодом, backend достаёт код по правилам, заданным для конкретного сайта.

Рабочий цикл Босса: открыть `/browse`, взять следующий свободный GitHub- или email-аккаунт,
зарегистрироваться на сайте руками в браузере, нажать «Проверить ящик», получить код,
завести запись в `main_site_account` / `main_site_account_custom`. Плюс пакетный импорт
готовых списков через скилл `import-site`.

Тестов нет. Из линтеров — только ESLint во frontend. Миграций нет, схема правится вручную
через psql.

## Команды

Backend — `uv`, Python 3.13 (`.python-version`). Frontend — `npm`, Next.js 16.

```bash
scripts/start_service.sh              # поднять backend:4000 + frontend:4100
scripts/start_service.sh --restart    # перезапустить
scripts/start_service.sh --stop       # остановить

uv sync                # зависимости из uv.lock
uv add <package>       # добавить зависимость
uv run python main.py  # песочница: проверяет коннект к Supabase

cd frontend && npm run build   # и npm run lint
```

Порты закреплены: backend 4000, frontend 4100, разрешённый диапазон 4000-4500.
Скрипт сам выставляет `NO_PROXY`, ждёт освобождения портов и снимает оба процесса
по Ctrl+C. `pkill`-паттерны в нём намеренно узкие — на машине живут другие проекты
со своими `next-server`.

Босс просил запускать сервисы этим скриптом и не держать процессы в фоне агента.

## Архитектура

Backend на FastAPI обслуживает frontend и является единственным, кто ходит в базу.
Все роуты под префиксом `/api`, потому что frontend проксирует именно его.

- `backend/app/config.py` — `Settings` на pydantic-settings (`supabase_url`, `supabase_key`,
  `api_host`, `api_port`, `frontend_origins`) + `allow_direct_localhost()`, см. раздел про proxy.
- `backend/app/supabase_client.py` — `get_supabase()` под `@lru_cache`.
- `backend/app/credentials.py` — `make_username()` / `make_password()`, генерация логина
  и пароля для регистрации на сайте. Списки слов пришли от Босса (`data/fn.txt`).
- `backend/app/main.py` (~790 строк) — всё приложение: CORS, все эндпоинты, извлечение кода.
- `frontend/` — Next.js 16, App Router, TypeScript. `next.config.ts`: `rewrites`
  `/api/:path*` → backend (адрес через `BACKEND_URL`), `allowedDevOrigins`, `turbopack.root`.
- `main.py` в корне — самостоятельный скрипт-песочница, не часть backend.
- `outlook_mail_checker.py` (Graph API) и `rambler_imap_mail_checker.py` (IMAP) — независимые
  модули с одинаковым публичным API: `get_mail_by_subject`, `get_mail_by_sender`,
  `delete_mail_by_*`, `move_mail_by_*`, `empty_junk_folder`, `check_mailbox_health`.
  Различаются только credentials: Outlook — `client_id` + `refresh_token`, Rambler —
  `login` + `password`. Оба проверяют «Входящие» и «Спам».
- `scripts/` — `check_emails.py`, `delete_github_emails_outlook.py`,
  `delete_github_emails_rambler.py`, `empty_junk_all.py`, `parse_import.py`, `start_service.sh`.
- `llm/model.py` — `ModelOllama`, обёртка над `ChatOllama`. Провайдер выбирается литералом
  (`deepseek`, `qwen`, `gemini`, `openai`, `openai_small`), имя модели — из env
  (`DEEPSEEK_MODEL_NAME`, `QWEN_MODEL_NAME`, `GEMINI_MODEL_NAME`, `OPENAI_120_MODEL_NAME`,
  `OPENAI_20_MODEL_NAME`). Всё через локальный Ollama, не через API провайдеров.
  В остальном коде пока не используется.
- `llm/bot.py` — заглушка. `tools/io_files.py` — функции-заглушки (`pass`).

Конфигурация — `.env` (не в репозитории) через `load_dotenv(find_dotenv())`. Логи — `loguru`.

## Эндпоинты

```
GET    /api/health                             supabase: ok|error
GET    /api/github/accounts?limit=             список main_github
GET    /api/github/accounts/browse             по одной записи; offset|from_id|after_id|before_id, site_id
GET    /api/github/accounts/{id}               404 нет, 410 если active=false
POST   /api/github/accounts/set-error-status   пометки брака
GET    /api/email/accounts/browse              то же для main_email; gmail=1 — только @gmail.com
POST   /api/email/accounts/set-error-status    active=false + reason
POST   /api/mail/check-mailbox                 достать код из письма
GET    /api/generate-credentials               свежая пара login/password (password_length=10)
GET    POST /api/sites                         список / создание сайта
PATCH  DELETE /api/sites/{id}                  правка (exclude_unset) / удаление, 409 если есть аккаунты
GET    /api/sites/{id}/stats                   счётчики и суммы балансов по обеим таблицам
GET POST /api/site-accounts                    аккаунты на базе GitHub
PUT DELETE /api/site-accounts/{id}
GET POST /api/site-accounts-custom             аккаунты на базе email
PUT DELETE /api/site-accounts-custom/{id}
```

`/api/github/accounts/browse` отдаёт только годные записи: `active=true`,
`email ilike %@hotmail.com`, `error_status is null`. Если передан `site_id` — исключает id,
уже привязанные к этому сайту.

`/api/email/accounts/browse` по умолчанию отсекает gmail-ящики, с `gmail=1` наоборот отдаёт
только их — на этом стоят вкладки «Почта» и «Gmail». Фильтр живёт во внутреннем `_build`,
поэтому одинаково влияет и на `total`, и на все режимы навигации.

`POST /api/site-accounts` с `smart_link: true` сам находит запись в `main_github` по `login`,
затем по `email` (`_resolve_github_row`, 404 с текстом «Умная привязка: …»). Аналогично
`_resolve_email_row` для custom. Нарушение уникальности (`APIError.code == "23505"`) → **409**.
Вставка и удаление поддерживают счётчик `main_site.cnt`.

`set-error-status` для GitHub принимает действия `Bad Rambler Email`, `Bad Github Account`,
`Suspended Github`, `Flag Site`. `Bad Rambler Email` гасит и адрес в `main_email` — причём
у rambler-аккаунтов адрес может лежать в `email`, а не в `restore_email`.

## Извлечение кода из письма

Правила живут в `main_site`: `mail_subject`, `code_anchor`, `code_length`, `code_format`
(`digits` | `alnum`). `POST /api/mail/check-mailbox` ищет ящик в `main_email`, по наличию
`client_id`/`refresh_token` выбирает Outlook или Rambler, кладёт сырой ответ в
`log/mail_check_result.txt` и вытаскивает код. `match_mode="contains"`, если тема пришла
от сайта; `"exact"` для зашитой `GITHUB_VERIFY_SUBJECT = "[GitHub] Please verify your device"`.

Якорь — не украшение: без него регулярка цепляет первое шестизначное число, а в письмах это
обычно hex-цвет из CSS или id из трекинг-ссылки. Механика в `backend/app/main.py:197-264`:

- `_ANCHOR_GAP_TIGHT` — между якорем и кодом допускает пробелы, html-теги, entities и
  пунктуацию (включая полноширинные китайские формы), но **не буквы**. Иначе якорь «код»
  дотягивается до «телефона 8391».
- `_ANCHOR_GAP_LOOSE_DIGITS` / `_ANCHOR_GAP_LOOSE_ALNUM` — запасной, более широкий проход.
- `_code_token(code_length, alnum)` — для alnum требует хотя бы одну цифру (lookahead),
  иначе в код попадают обычные слова.
- `_extract_verification_code` сортирует письма от новых к старым и прогоняет строгий паттерн
  по всем письмам, прежде чем перейти к нестрогому.

## Frontend

Три страницы, навигация в `components/Nav.tsx`.

- `src/app/page.tsx` — таблица аккаунтов выбранного сайта, обе таблицы аккаунтов сразу,
  удаление и правка по строке.
- `src/app/sites/page.tsx` — список сайтов, статистика (`StatCell`), правила и `meta`
  (`MetaValue`), создание/правка/удаление.
- `src/app/browse/page.tsx` — главный рабочий экран, ~770 строк. Табы в порядке
  «Почта» (`email`), «GitHub» (`github`), «Gmail» (`gmail`); `email` и `gmail` — одна и та же
  форма над `main_email`, различаются только параметром `gmail=1` (флаг `isEmailTab` включает
  общую логику, `gmailParam` уезжает в три fetch-а). Блоки «Запись ID», «Правила получения
  кода», «Уровень брака», «Аккаунт на сайте», компоненты `CopyBtn`, `Field`, `AccInput`,
  `CheckMailBtn`, `ErrorBtn`.
  На почтовых вкладках верхняя строка блока с данными ящика — сгенерированные `login`
  и `password` (`/api/generate-credentials`, кнопка `↻` рядом). Та же пара плюс email ящика
  сразу подставляются в inputs «Аккаунта на сайте», чтобы Босс жал только «Сохранить».
  Порядок полей там: `login`+`password`, `email`, `token`+`balance`, `aff`. На вкладке
  `github` пароля нет — в `main_site_account` нет такой колонки.
  Выбранная вкладка запоминается в localStorage (`TAB_KEY`); по умолчанию «Почта».
  Восстановление сделано в колбэке fetch-а сайтов, а не в инициализаторе `useState` или теле
  эффекта: первое даёт расхождение при гидратации, второе — ошибку
  `react-hooks/set-state-in-effect`.
- `lib/sticky.ts` — мелкий хелпер: ключи localStorage (`SITE_KEY`, `AFF_KEY`, `TAB_KEY`),
  `pickStickyId`, `rememberId`.

Удаление во фронтенде всегда через `confirm`, плюс защита на backend (409 при связанных данных).

## База

Self-hosted Supabase в Docker (compose-проект `ai-work`), не облако. `SUPABASE_URL` =
`http://127.0.0.1:54321` (Kong), Postgres — `127.0.0.1:54322`, Studio — `54323`.
**Ту же базу используют другие проекты и другие агенты** — `.env` и схему трогать только
по явной просьбе. Таблицы `main_cannel_*` и `main_video_*` принадлежат другим проектам.

Таблицы проекта (число строк — на 2026-08-20, ориентир масштаба):

- `main_github` (~457) — `id, email, pass_email, login, pass_github, restore_email,
  restore_pass, active, created_at, error_status`
- `main_email` (~678) — `id, email, password, client_id, refresh_token, graph_refresh_token,
  active, restore_email, restore_pass, secret, created_at, reason`
- `main_site` (7) — `id, name, cnt, created_at, meta jsonb, mail_subject, code_length,
  code_anchor, code_format`
- `main_site_account` (~135) — `id, email, login, balance, token, aff, note, site_id, github_id,
  created_at`
- `main_site_account_custom` (~70) — `id, site_id, email_id, email, login, password, token,
  balance, aff, note, created_at`

Уникальные индексы, из которых и берутся 409: `main_site(name)`;
`main_site_account(site_id, login) where login is not null`, `(site_id, email) where email is
not null`, `(site_id, github_id)`; `main_site_account_custom(site_id, email) where email is not
null`, `(site_id, login) where login is not null`, `(site_id, email_id)`.

`main_site.meta` — JSONB под разнородные данные уровня сайта: условия акций, требования
к выводу, доступные модели, заметки. Заведён вместо отдельных колонок, потому что набор полей
у сайтов не совпадает. Старая колонка `main_site.note` удалена — заметки уровня сайта живут
в `meta`. У обеих таблиц аккаунтов при этом своя колонка `note text` («примечания» в UI,
после `aff`) — это заметка по конкретному аккаунту, а не по сайту.

DDL — только под `supabase_admin`, пароль берётся из окружения контейнера:

```bash
docker exec supabase_db_ai-work sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" \
  psql -h 127.0.0.1 -U supabase_admin -d postgres -c "<SQL>"'
```

## Proxy

В shell окружении заданы `http_proxy`/`https_proxy` на внешний proxy. Без `no_proxy` запросы
к локальному Kong уходят в него и падают с `RemoteProtocolError: Server disconnected without
sending a response`. Лечится `allow_direct_localhost()` в `backend/app/config.py` — дописывает
`localhost,127.0.0.1` в `NO_PROXY`/`no_proxy`, не затирая уже заданные хосты. `main.py`
и `scripts/start_service.sh` делают то же. В `.env` эти переменные намеренно НЕ прописаны,
чтобы не влиять на другие проекты. Похожая ошибка коннекта — проверять proxy в первую очередь.

Из той же причины `curl` к локальным сервисам стоит звать с `--noproxy '*'`.

## Импорт списков аккаунтов

Босс ведёт учёт в текстовых файлах от руки, формат плавающий. Разбор — скилл
`.claude/skills/import-site/` (`SKILL.md`, `references/field-patterns.md`,
`references/import-flow.md`). Движок общий, под каждый документ пишется только профиль-JSON
в `scripts/profiles/`; готовые есть на apify.com, api.mhoo.cc, gorouter.app, hotgen.ai,
seekai.cc, tabitoken.com, x-llm.net.

Два правила формата: данные в начале строки, личные пометки Босса после `---`; пометка при
этом не мусор, а признак типа значения. Парсер раскладывает исходник на `_clean.txt`,
`_questions.txt`, `_errors.txt` рядом с ним. **Импорт не запускать, пока `questions` не 0** —
угадывать пароль или ключ нельзя, неверно собранное значение выглядит правдоподобно.

## Чего нет в репозитории

`source/`, `data/`, `log/` исключены: там логины, пароли и API-ключи открытым текстом.
`source/` — исходники импорта (`<site>.txt` плюс триплеты `_clean`/`_questions`/`_errors`,
`1.json`-`3.json`), `data/` — `eg.txt`, `in_email.txt`, `log/` — `mail_check_result.txt`.

Токены из этих аккаунтов Босс подставляет как API-провайдеры для Claude Code, поэтому тариф
и баланс в записях — не декоративные поля.

## Известные расхождения

`llm/model.py:47` — `send_message_structured_outputs` определён внутри `__init__`, поэтому
методом класса не является и вызвать его нельзя.

`tools/io_files.py` — `save_count_file` и `save_list_file` пустые (`pass`).

## Язык

Логи, сообщения об ошибках и системные промпты — на русском. Общение с Боссом — на русском.



