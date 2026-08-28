# Backend, база, эндпоинты

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
  `delete_github_emails_rambler.py`, `empty_junk_all.py`, `parse_import.py`,
  `gorouter_balance.py`, `gorouter_checkin.py`, `panel_sync.py`, `station_watch.py`,
  `linuxdo_cdk_watch.py`, `notify_ui.py`, `single.py`,
  `find_model.py`, `harvest_accounts.py`, `harvest_email.py`, `start_service.sh`.
- `scripts/single.py` — `hold_lock(name)` на `flock` поверх `log/<name>.lock`: прогон, который
  не уложился в шаг крона, иначе получает второй экземпляр поверх живого, и тот, кто финиширует
  позже, затирает свежие цифры своими — собранными раньше. Так 25.08 `gorouter_balance.py`
  шёл 23 минуты при шаге 15. `flock`, а не pid-файл: блокировку снимает ядро при любом конце
  прогона. Стоит у `gorouter_balance.py`, `gorouter_checkin.py`, `station_watch.py`,
  `linuxdo_cdk_watch.py`; возвращённый файл надо держать в переменной до конца прогона.
- `llm/model.py` — `ModelOllama`, обёртка над `ChatOllama`. Провайдер выбирается литералом
  (`deepseek`, `qwen`, `gemini`, `openai`, `openai_small`), имя модели — из env
  (`DEEPSEEK_MODEL_NAME`, `QWEN_MODEL_NAME`, `GEMINI_MODEL_NAME`, `OPENAI_120_MODEL_NAME`,
  `OPENAI_20_MODEL_NAME`). Всё через локальный Ollama, не через API провайдеров.
  В остальном коде пока не используется.
- `llm/bot.py` — заглушка. `tools/io_files.py` — функции-заглушки (`pass`).
- `llm/translator.py` — перевод китайских заголовков форума через шлюз anymodel.org,
  подробности в разделе про разведку linux.do.

Конфигурация — `.env` (не в репозитории) через `load_dotenv(find_dotenv())`. Логи — `loguru`.

## Эндпоинты

```
GET    /api/health                             supabase: ok|error
GET    /api/ping                               {"alive": true}, без базы — для сторожа
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
GET    /api/linuxdo/report                     последний отчёт разведки linux.do из `linux_do_*`
GET    /api/claude/active                      станция и аккаунт активного ключа Claude Code
GET    /api/claude/stations                    снимок станций: живость, модели, годность
POST   /api/claude/activate                    переключить ключ и endpoint на станцию
GET    /api/events                             поток SSE для открытых страниц
POST   /api/events/notify                      {"topic": …} — разослать весть страницам
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
Вставка и удаление в обеих таблицах зовут `_recount_site()`: он пересчитывает `main_site.cnt`
по **обеим** таблицам аккаунтов, а не прибавляет и убавляет единицу. Прежний счётчик двигался
только на операциях с `main_site_account`, а `custom` не видел никогда — 2026-08-24 расходились
11 сайтов из 15 (у hotgen.ai стоял 0 при 39 аккаунтах). Пересчёт заодно подбирает записи,
заведённые сырым SQL: такая вставка идёт мимо backend и счётчик не двигает вовсе.

`set-error-status` для GitHub принимает действия `Bad Rambler Email`, `Bad Github Account`,
`Suspended Github`, `Flag Site`. `Bad Rambler Email` гасит и адрес в `main_email` — причём
у rambler-аккаунтов адрес может лежать в `email`, а не в `restore_email`.

## Извлечение кода из письма

Правила живут в `main_site`: `mail_subject`, `code_anchor`, `code_length`, `code_format`
(`digits` | `alnum`). `POST /api/mail/check-mailbox` ищет ящик в `main_email`, по наличию
`client_id`/`refresh_token` выбирает Outlook или Rambler, кладёт сырой ответ в
`log/mail_check_result.txt` и вытаскивает код. `match_mode="contains"`, если тема пришла
от сайта; `"exact"` для зашитой `GITHUB_VERIFY_SUBJECT = "[GitHub] Please verify your device"`.

Разбирается **полное тело** письма, а не `bodyPreview`: Graph режет превью на 255 символах,
и у письма GitHub с логином длиннее 22 символов обрез приходился на середину кода —
в тексте оставалось `Verification code: 141`. Поэтому `MESSAGE_SELECT_FIELDS` просит `body`,
а `_fetch_folder_messages` разворачивает его из `{contentType, content}` в строку; превью
осталось запасным вариантом. Ловушка редкая (в `main_github` таких логинов пять из 457)
и потому долго не всплывала — на неё есть тест.

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

## База

Self-hosted Supabase в Docker (compose-проект `ai-work`), не облако. `SUPABASE_URL` =
`http://127.0.0.1:54321` (Kong), Postgres — `127.0.0.1:54322`, Studio — `54323`.
**Ту же базу используют другие проекты и другие агенты** — `.env` и схему трогать только
по явной просьбе. Таблицы `main_cannel_*` и `main_video_*` принадлежат другим проектам.

Таблицы проекта (число строк — на 2026-08-20, у `linux_do_*` на 2026-08-25; ориентир масштаба):

- `main_github` (~457) — `id, email, pass_email, login, pass_github, restore_email,
  restore_pass, active, created_at, error_status`
- `main_email` (~678) — `id, email, password, client_id, refresh_token, graph_refresh_token,
  active, restore_email, restore_pass, secret, created_at, reason`
- `main_site` (15) — `id, name, cnt, created_at, meta jsonb, mail_subject, code_length,
  code_anchor, code_format`
- `main_site_account` (~140) — `id, email, login, balance, token, aff, access_token, panel_id,
  note, site_id, github_id, created_at`
- `main_site_account_custom` (~87) — `id, site_id, email_id, email, login, password, token,
  balance, aff, access_token, panel_id, note, created_at`
- `linux_do_topic` (90, из них 74 отсеянных) — `topic_id` (pk, id темы форума), `title, born,
  url`, `kind` (`live` | `other` | `plain` | `dead` | `rejected`), `hot`, `rejected` (причина
  отсева или null у отобранной), `station`, `site_id` → `main_site`, `marks jsonb`,
  `max_amount, pieces`, `ru_useful, ru_literal, ru_model, ru_of_title, ru_at`,
  `body` (первый пост простым текстом), `ru_body` (его перевод), `ru_body_of` (sha1 текста,
  с которого перевод сделан), `ru_body_at`, `first_seen_at, last_seen_at`
- `linux_do_cdk` (6) — `id, uuid` (unique), `topic_id` → `linux_do_topic` (**nullable**),
  `url, verdict` (`live` | `over` | `unknown`), `closed`, `state jsonb`, `start_time, end_time,
  min_trust, is_completed, total_items, price, checked_at`
- `linux_do_run` (4) — `id, started_at, account, trust_level, seen_total, picked, with_links,
  closed_known`
- `main_domain` (1782, на 2026-08-27) — домены `.eu.cc` Босса: `id, domain` (unique),
  `status` (`owned` | `unknown` | `foreign` | `free` | `lapsed`), `ns jsonb, ns_checked_at`,
  `registrar`, `category, market, priority, resale_min, resale_max`, `for_sale, marketplace,
  sale_price`, `note`, `sources jsonb`, `registered_at, expires_at`, `created_at, updated_at`

Разведка linux.do держит одну строку на тему и отдельную на раздачу, а вкладке нужен плоский
список карточек — тема с двумя раздачами даёт две; обратную сборку делает эндпоинт.
`first_seen_at` в апсерт **не передаётся вовсе**: PostgREST кладёт в `ON CONFLICT DO UPDATE SET`
только переданные колонки, поэтому при вставке срабатывает `default now()`, а при слиянии поле
остаётся прежним. Отсеянные темы пишутся с `ignore_duplicates=True`: тема могла быть отобрана
раньше и выпасть после правки словаря `GOOD`/`BAD`, а слияние затёрло бы ей перевод, метки
и группу. `topic_id` у раздачи nullable, потому что закрытые uuid переживают свою тему: та
выпадает из окна 14 дней, а знание «эта раздача погашена» терять нельзя. Прогоны чистит сам
скрипт (`RUN_KEEP_DAYS = 180`), отдельного крона нет — их 144 в сутки, и старше полугода цифры
ни о чём: у Босса растёт trust level и состав тега меняется.

Уникальные индексы, из которых и берутся 409: `main_site(name)`;
`main_site_account(site_id, login) where login is not null`, `(site_id, email) where email is
not null`, `(site_id, github_id)`; `main_site_account_custom(site_id, email) where email is not
null`, `(site_id, login) where login is not null`, `(site_id, email_id)`.

Аккаунты без почты (сайт принимает только логин и пароль — так на qkmss.com) вешаются на
ящики-заглушки в `main_email`: id 703-706, адреса `no-email@warning.com`,
`no-email2@…`-`no-email4@…`, пароль `no-password`, `active=false`. Заглушек четыре, а не одна,
из-за индекса `(site_id, email_id)`: он не частичный, поэтому один и тот же `email_id` внутри
сайта можно использовать ровно раз. Зато между сайтами они переиспользуются — четырёх хватает,
пока у станции не больше четырёх аккаунтов без почты. `active=false` держит их вне
`/api/email/accounts/browse`: иначе заглушка выдалась бы Боссу как свободный ящик.

`main_site.meta` — JSONB под разнородные данные уровня сайта: условия акций, требования
к выводу, доступные модели, заметки. Заведён вместо отдельных колонок, потому что набор полей
у сайтов не совпадает. Старая колонка `main_site.note` удалена — заметки уровня сайта живут
в `meta`. У обеих таблиц аккаунтов при этом своя колонка `note text` («примечания» в UI,
после `aff`) — это заметка по конкретному аккаунту, а не по сайту.

Прайс в `meta` пишется машиночитаемо, чтобы по всем сайтам сразу отвечать на вопрос «где взять
такую-то модель и хватит ли там денег». Форма — `meta.price`, ключ = точный id модели для API,
значение = `{"in": …, "out": …}` в валюте за 1M токенов либо `{"per_request": …}`, плюс
`"status"` (`live` | `degraded` | `offline` | `disabled`), `"tier"` (если модель только по
подписке) и `"overhead_in"` (накладные токены провайдера, если он их приклеивает). Рядом —
`currency`, `pricing_type`, `prompt_cache`, `endpoints_openai`, `checked`. Прозаические
пояснения живут в отдельных ключах и в `price` не лезут. Эталон — `vyceai.com` (id 11), там
заполнены все 26 моделей. У `seekai.cc` в `price` нет `status`, у `ai.fujcloud.com` прайс
устроен через `groups`/`ratio` в донгах, и модели там упомянуты только в прозе.

Поиск по этой структуре — `scripts/find_model.py <часть имени модели>`: обходит `/api/sites`,
печатает совпадения из `price` с ценой и статусом, число аккаунтов и суммарный баланс сайта,
а модели, найденные только в прозаических ключах, показывает отдельной строкой `(в прозе)`.

`access_token text` и `panel_id int` в обеих таблицах аккаунтов — необязательные, заполняются
только там, где панель сайта такое умеет (пока это New API на gorouter.app). Пара нужна, чтобы
читать баланс аккаунта без cookies: `Authorization: Bearer <access_token>` +
`New-Api-User: <panel_id>`. Одного токена мало — панель требует id, и с чужим id тот же токен
получает 401, а ни один эндпоинт id по токену не отдаёт. Оба значения Босс копирует со страницы
`/profile` («Токен доступа» и «ID пользователя»); токен один на аккаунт, повторная генерация
гасит предыдущий.

У станций со своей панелью (`meta.panel_api`) в том же `access_token` лежит **сессия панели**,
а `panel_id` пуст: у vyceai это значение ключа `vyce_session` из localStorage браузера.
Смысл поля тот же — читать деньги аккаунта без cookies, — а формат другой, потому что панель
не New API.

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

Обратный случай — linux.do: там прокси, наоборот, обязателен. `cf_clearance` выдан под
внешний адрес прокси Босса, и напрямую из Москвы Cloudflare отвечает `403 cf-mitigated:
challenge` при полностью живых cookies. В оболочке прокси задан переменными окружения, у крона
их нет — отсюда `linuxdo_cdk_watch.py` брал 403 каждые десять минут, пока не стал читать
`PROXY_URL` из `.env` сам и передавать его сессиям явно. Признак именно этой поломки —
`cf-ray` с окончанием `-DME` (Москва) вместо `-EWR`.

### Сторож backend

`scripts/health_watch.sh` в кроне `* * * * *` щупает `GET /api/ping` и при молчании
поднимает сервис (`setsid nohup start_service.sh --restart`, иначе процессы остались бы
детьми крон-задания). Живой backend не оставляет строк вовсе; события — в
`log/health_watch.txt`, вывод самого подъёма — в `log/health_watch_service.txt`.
`touch log/health_watch.off` глушит сторожа: без этого после ручного `--stop` он вернул бы
сервис через минуту. Проба руками — `scripts/health_watch.sh --once`; её блок стоит **выше**
заглушки и замка намеренно: иначе при `health_watch.off` или при уже идущем подъёме проба
выходила бы молча с кодом 0, а молчание тут читается как «сервис жив» — то есть врала бы
ровно в те минуты, когда её и запускают.

Ручка отдельная от `/api/health` намеренно: та ходит в Supabase, и её медленный ответ
выглядел бы как смерть процесса, а перезапуском backend лежащая база не лечится.
Три взгляда с паузой 3 с, и только потом рестарт — разовый таймаут бывает и у живого
сервиса, а перезапуск оборвал бы идущий прогон сбора. Прогоны не наложатся: `flock`
на `log/health_watch.lock`.

Строки журнала пишутся подробно, потому что читают их задним числом и одним взглядом.
`why()` называет не «не отвечает», а конкретную поломку: «порт 4000 не слушает никто»
(процесс умер), «порт слушается, но за 5 с ни байта ответа» — с числом соединений
в accept-очереди (`backlog()`) и командной строкой держателя порта (`holder()`), — либо
«отвечает кодом N вместо ping». Разница не косметическая: первое и второе лечатся
перезапуском, третье означает чужой процесс на 4000, которого узкие `pkill`-паттерны
`start_service.sh` не снимут вовсе. К каждой строке приписано следующее действие — какой
файл смотреть, какую команду дать руками. Порты вынесены в `BACKEND_PORT`/`FRONTEND_PORT`
и подставляются в текст: зашитые числа врали при проверке скрипта копией на другом порту.

Слепое пятно осознанное: сторож щупает только `/api/ping`, поэтому backend, живой на ping
и зависший на запросах с базой, он не увидит. Пробой с базой это не закрыть — лежащая
Supabase перезапуском backend не лечится, и сторож молотил бы сервис зря.

Состояние базы в строку отказа всё же подмешивается (`db_state()`), но **только когда
backend уже молчит**: в норме это ноль лишних запросов, а задним числом видно разницу
между «лёг наш процесс» и «лёг весь стенд». Проба — `GET http://127.0.0.1:54321/rest/v1/`
(Kong отдаёт 200 и без ключа, секреты не нужны) плюс `docker inspect` здоровья
`supabase_db_ai-work`. Перезапускать базу сторож не пробует: контейнеры общие с другими
проектами, у них своя `unless-stopped` и healthcheck `pg_isready` каждые 10 с, а зависший
Postgres рестартом маскируется, а не лечится.

**Backend не падает, а зависает** — 26.08 поймано вживую: процесс жив, порт слушается,
в accept-очереди 9 соединений, ответов нет. Причина в паре `--reload` + SSE: при
перезапуске от правки файла uvicorn закрывает приём и ждёт завершения активных запросов,
а поток `/api/events` у открытой страницы не кончается никогда. Флага
`--timeout-graceful-shutdown 5` в `start_service.sh` не было, а `_shutdown` из
`backend/app/main.py` эту ловушку не закрывает: lifespan-shutdown наступает **после**
ожидания запросов. Отсюда же 30-секундные таймауты сбора и потерянные головы: снаружи
такой backend неотличим от живого. С флагом reload при открытом SSE проходит за 2 с.

## Чего нет в репозитории

`source/`, `data/`, `log/` исключены: там логины, пароли и API-ключи открытым текстом.
`source/` — исходники импорта (`<site>.txt` плюс триплеты `_clean`/`_questions`/`_errors`,
`1.json`-`3.json`), `data/` — `eg.txt`, `in_email.txt`, `log/` — `mail_check_result.txt`.

`backup/` — снимки базы и стороджа (`postgres.dump`, `postgres.sql.gz`, `globals.sql`,
`storage.tar.gz` в каталоге по времени). Там данные всех проектов общей Supabase, поэтому
папка тоже исключена; команды снятия и восстановления — в `backup/README.md`.

Токены из этих аккаунтов Босс подставляет как API-провайдеры для Claude Code, поэтому тариф
и баланс в записях — не декоративные поля.

## Известные расхождения

`llm/model.py:47` — `send_message_structured_outputs` определён внутри `__init__`, поэтому
методом класса не является и вызвать его нельзя.

`tools/io_files.py` — `save_count_file` и `save_list_file` пустые (`pass`).

