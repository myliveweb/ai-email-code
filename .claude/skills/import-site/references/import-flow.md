# Импорт разобранных блоков в базу

## Модель

`main_site_account` — один аккаунт на одном сайте. Один блок из файла = одна строка.

```
id         bigint  PK
site_id    bigint  NOT NULL  -> main_site(id)
github_id  bigint  NOT NULL  -> main_github(id)   без DEFAULT, нулём быть не может
login      text              логин на сайте
email      text              email на сайте
token      text              API-ключ
balance    numeric
aff        text              своя партнёрская ссылка
```

Уникальность (индексы созданы 2026-08-17):

```
main_site_account_site_login_key   UNIQUE (site_id, login)      WHERE login IS NOT NULL
main_site_account_site_email_key   UNIQUE (site_id, email)      WHERE email IS NOT NULL
main_site_account_site_github_key  UNIQUE (site_id, github_id)
```

В пределах одного сайта дублей логина, email и github-аккаунта нет. На другом сайте те же значения допустимы — это осознанное решение Босса, сайтов будет несколько. Частичные индексы по login/email нужны, чтобы NULL не блокировал вставку, когда поле в документе отсутствует.

`github_id` остаётся `NOT NULL`. Не предлагай сделать его nullable, этот вариант отклонён. Сайты, где регистрация шла не через GitHub, идут в параллельную таблицу — см. следующий раздел.

## Сайты без GitHub: `main_site_account_custom`

Создана 2026-08-17 под api.mhoo.cc, где ни один аккаунт не заведён через GitHub. Та же модель, но вместо `github_id` — `email_id` с FK на `main_email(id)`, плюс колонка `password` открытым текстом: пароль на сайте не совпадает с паролем от ящика, вывести его из `main_email` нельзя (проверено на трёх адресах — в документе `Metropoliten911!`, в `main_email.password` совсем другое, `restore_pass` пуст). Босс разрешил хранить открыто.

```
id, site_id -> main_site(id), email_id -> main_email(id), email, login, password, token, balance, aff, created_at
```

Уникальность — та же тройка: `(site_id, email)`, `(site_id, login)` (частичные, `WHERE ... IS NOT NULL`), `(site_id, email_id)`.

Умная привязка здесь ищет только по email: `_resolve_email_row` + `create_custom_account` в `backend/app/main.py`, эндпоинты `POST`/`GET /api/site-accounts-custom`. Логина в таких документах обычно нет вовсе — идентификатор аккаунта это email, поэтому в профиле парсера нужен `ignore_bare_identifiers: false`.

Импорт — тот же скрипт с флагом `--custom`:

```bash
uv run python $S source/<file>_clean.txt --site-id 6 --custom --dry-run
uv run python $S source/<file>_clean.txt --site-id 6 --custom --limit 1
uv run python $S source/<file>_clean.txt --site-id 6 --custom --offset 1
```

Флаг меняет URL на `/api/site-accounts-custom` и шлёт `email_id` + `password` вместо `github_id`. `main_site.cnt` при этом не инкрементится: счётчик заведён под GitHub-таблицу, и смешивать в нём две таблицы нельзя, пока Босс не скажет, что он должен считать.

**Один email может быть на сайте дважды** — например, зарегистрирован и кнопкой Google, и кнопкой GitHub, с разными балансами и ключами. Это две разные записи в двух разных таблицах: GitHub-регистрация в `main_site_account`, остальные в `main_site_account_custom`. Уникальный индекс внутри одной таблицы этому не мешает. Различает их только пометка после `---` (`--- Кнопка Google` / `--- Кнопка GitHub`), поэтому такие блоки нельзя импортировать пачкой — их надо разводить руками.

### Один документ на две таблицы

Чаще бывает не вперемешку, а секциями: Босс дописывает файл сверху вниз, и в какой-то момент перешёл на регистрацию через GitHub. Тогда посреди документа стоит блок из одного слова — `GitHub` — и всё, что ниже, идёт в `main_site_account`, всё, что выше, в `main_site_account_custom`. Так было на seekai.cc: строка 222, блоки 2-15 в custom, блоки 17-29 в GitHub-таблицу.

Такой маркер сам по себе не аккаунт, поэтому он честно уезжает в `_questions.txt` — не пытайся его подавить. Прочитай его как границу и раздели импорт по `--offset`:

```bash
uv run python $S source/<file>_clean.txt --site-id 7 --custom --limit 14   # секция до маркера
uv run python $S source/<file>_clean.txt --site-id 7 --offset 14           # секция после
```

Считай `--offset` по фактическому порядку блоков в `_clean.txt`, а не по их номерам: номера сквозные по исходнику и в чистом файле идут с пропусками (на seekai.cc нет 1 и 16, так что блок 17 — это пятнадцатая запись, `--offset 14`). Прогони `--dry-run` на обеих половинах и глазами сверь, что граница попала между нужными блоками.

## Умная привязка

В документах email обычно нет — есть только логин. `github_id` при этом обязателен. Решает `smart_link`.

`POST /api/site-accounts` с `smart_link: true`:

1. ищет строку в `main_github` по `login`, затем (если не нашёл) по `email`;
2. берёт `id` найденной строки как `github_id`, игнорируя то, что пришло в запросе;
3. при нескольких совпадениях берёт минимальный `id`;
4. если не нашёл — `404`, запись не сохраняется;
5. недостающее из пары login/email добирает из найденной строки.

Реализация — `_resolve_github_row` и `create_site_account` в `backend/app/main.py`. Нарушение уникальности превращается в `409` с текстом, какой login/email уже занят.

**Сверь логины с `main_github` до импорта, а не по факту `404`.** Часть аккаунтов Босса зарегистрирована не через GitHub, а на отдельные Gmail — таких нет ни в `main_github`, ни в `main_email`, и умной привязке зацепиться не за что. Пока для них нет своей таблицы (см. решение по модели), их надо не импортировать, а показать Боссу списком: он скажет, пропустить или заводить. Один `SELECT ... WHERE login IN (...)` по логинам из `_clean.txt` экономит разбор частично уехавшего импорта.

## Порядок импорта

**1. Убедиться, что сайт есть в `main_site`.**

```bash
curl --noproxy '*' -s http://127.0.0.1:4000/api/sites
curl --noproxy '*' -s -X POST http://127.0.0.1:4000/api/sites \
  -H 'Content-Type: application/json' -d '{"name":"x-llm.net"}'
```

`site_id` подставляется во все записи импорта. Если сайта нет — спроси Босса, создавать ли, а не создавай молча: имя сайта в документе может быть написано иначе, чем он хочет видеть в базе.

Строки уровня сайта — условия акции и требования к выводу, доступные модели, endpoint API, произвольные заметки — в `main_site_account` не идут. Им место в `main_site.meta` (`jsonb`, по умолчанию `{}`). Известный ключ — `endpoints_openai` (OpenAI-совместимый endpoint, раньше был отдельной колонкой `endpoint`). Правится без psql:

```bash
curl --noproxy '*' -s -X PATCH http://127.0.0.1:4000/api/sites/5 \
  -H 'Content-Type: application/json' \
  -d '{"meta":{"endpoints_openai":"https://gorouter.app/v1","note":"$70 cred + Daily check in"}}'
```

`meta` заменяется целиком, а не мержится — перед правкой прочитай текущее значение через `GET /api/sites`. Такой блок уже встречался: блок 1 в `source/gorouter.app.txt` — это не аккаунт, а описание сайта.

Известные ключи `meta`: `endpoints_openai`, `currency` (`USD`, `CNY` — валюта балансов, если не доллар), `price` (тарифы моделей), `note`. Таблица цен, повторяющаяся в каждом блоке, — это тоже уровень сайта, а не аккаунта. Формат `price`, согласованный на seekai.cc: ключ — имя модели, цены за 1M токенов в валюте сайта.

```json
{"price": {"gpt-5.6-sol": {"in": 1.5, "out": 12, "cache": 0.15},
           "gpt-5-6-terra": {"in": 0, "out": 0},
           "DeepSeek-V4-Flash-0731": {"per_request": 0.1}}}
```

`cache` опускается, если цены кэша нет; у пооперационной тарификации вместо `in`/`out` — `per_request`. В профиле парсера строки тарифов гони в `noise` — но осторожно с полями `password` и `login`: имена моделей выглядят как значения, а поля проверяются раньше `noise`.

**2. Прогнать импорт скриптом.**

```bash
S=.claude/skills/import-site/scripts/import_clean.py
uv run python $S source/<file>_clean.txt --site-id 1 --dry-run   # посмотреть payload
uv run python $S source/<file>_clean.txt --site-id 1 --limit 1   # одна запись
uv run python $S source/<file>_clean.txt --site-id 1 --offset 1  # остальные
```

Скрипт читает `_clean.txt` (а не исходник), заглушки вида `(пусто — ...)` считает пустыми, шлёт `smart_link: true` и печатает отчёт «вставлено N из M» со списком ошибок. `--offset` нужен для докатки после сбоя. `github_id: 0` в payload обязателен по схеме запроса, но при `smart_link` не используется.

Ручной эквивалент одной записи:

```bash
curl --noproxy '*' -s -X POST http://127.0.0.1:4000/api/site-accounts \
  -H 'Content-Type: application/json' \
  -d '{"site_id":1,"github_id":0,"smart_link":true,"login":"boevhk51v",
       "token":"sk-...","balance":2.0,"aff":"https://..."}'
```

**3. Проверить итог в базе.** Полезный запрос: `count(*)`, `count(distinct github_id)`, `count(email)` — если email заполнены не у всех, умная привязка сработала не везде. И сверить `main_site.cnt` с фактическим числом строк.

Ошибки не глотать: `404` (нет такого аккаунта в `main_github`) и `409` (дубль на этом сайте) — это данные, которые Босс должен увидеть.

`--noproxy '*'` обязателен: в окружении заданы `http_proxy`/`https_proxy`, без него запрос к локальному backend уходит во внешний прокси и падает.

## Пометки брака из `_errors.txt`

Отдельный проход после импорта, через `POST /api/github/accounts/set-error-status`.

`bad` / `bad email` → действие `Bad Rambler Email`:

```json
{"login": "aleksandrovna7rt9x", "action": "Bad Rambler Email"}
```

Backend находит строку в `main_github` по логину, ставит `active=false` и `error_status`, затем гасит почтовый ящик в `main_email`. Адрес ящика берётся как `restore_email or email`: у рамблеровских аккаунтов рамблер лежит в основном `email`, а `restore_email` пуст. Это подтверждено Боссом и покрыто в коде — на срезе 457 строк `main_github` у 75 рамблер в `email`, а `restore_email` заполнен только у 205.

Если в `_errors.txt` идентификатор — email, шли `{"email": ..., "action": ...}`; поле `restore_email` тоже принимается.

`suspended` → действие `Suspended Github`: ищет по `email`, ставит только `error_status`, `active` не трогает.

Перед применением пометок проверь, что записи вообще есть в базе. В прошлый раз одного помеченного аккаунта (`brianpittmanvigd@hotmail.com`) не оказалось ни в `main_github`, ни в `main_email` — искал и по подстроке. Такое нужно докладывать, а не молча пропускать: у Босса запись есть на бумаге, значит расхождение важно.

## DDL

Схема общая с другими проектами и агентами — менять только по явной просьбе.

Таблицы `main_*` принадлежат `supabase_admin`, роль `postgres` получает `permission denied` на `CREATE INDEX` / `ALTER TABLE`. Рабочая команда (Босс разрешил брать пароль из окружения контейнера):

```bash
docker exec supabase_db_ai-work sh -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U supabase_admin -d postgres -c "<SQL>"'
```
