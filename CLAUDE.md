# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Состояние проекта

Ранний каркас, ещё ни одного коммита в git. README пустой, тестов нет. Из линтеров есть только ESLint во frontend.

## Команды

Backend — `uv`, Python 3.13 (`.python-version`). Frontend — `npm`, Next.js 16.

```bash
uv sync                # установить зависимости из uv.lock
uv add <package>       # добавить зависимость (правит pyproject.toml + uv.lock)
uv run python main.py  # скрипт-песочница: проверяет коннект к Supabase

# backend, порт 4000
uv run uvicorn backend.app.main:app --host 127.0.0.1 --port 4000 --reload

# frontend, порт 4100 (из каталога frontend/)
npm run dev
npm run build
npm run lint
```

Порты закреплены: backend 4000, frontend 4100. Разрешённый диапазон для проекта — 4000-4500.

## Архитектура

Backend на FastAPI обслуживает frontend и является единственным, кто ходит в базу.

- `backend/app/config.py` — `Settings` на pydantic-settings (`supabase_url`, `supabase_key`, `api_host`, `api_port`, `frontend_origins`) + `allow_direct_localhost()`, см. раздел про proxy.
- `backend/app/supabase_client.py` — `get_supabase()` под `@lru_cache`, единственный экземпляр клиента на процесс.
- `backend/app/main.py` — приложение FastAPI, CORS под frontend, endpoints: `GET /api/health` (отдаёт `supabase: ok|error`) и `GET /api/github/accounts?limit=` (читает `main_github`). Все роуты живут под префиксом `/api`, потому что frontend проксирует именно его.
- `frontend/` — Next.js 16, App Router, TypeScript. `next.config.ts` содержит `rewrites` с `/api/:path*` на backend (адрес переопределяется через `BACKEND_URL`), `allowedDevOrigins` для localhost и 127.0.0.1, `turbopack.root`. Страница `src/app/page.tsx` — заглушка, тянет аккаунты из backend и показывает таблицей.
- `main.py` — самостоятельный скрипт в корне, не часть backend. Держит свой supabase client.
- `llm/model.py` — `ModelOllama`: обёртка над `ChatOllama` (LangChain). Провайдер выбирается строковым литералом (`deepseek`, `qwen`, `gemini`, `openai`, `openai_small`), имя конкретной модели читается из переменных окружения `DEEPSEEK_MODEL_NAME`, `QWEN_MODEL_NAME`, `GEMINI_MODEL_NAME`, `OPENAI_120_MODEL_NAME`, `OPENAI_20_MODEL_NAME`. Все модели идут через локальный Ollama, а не через API провайдеров.
- `llm/bot.py` — заглушка (`llm_bot = {}`).
- `llm/__init__.py` — реэкспортирует `llm_bot` и `ModelOllama`.

Конфигурация читается из `.env` (не в репозитории) через `load_dotenv(find_dotenv())`. Логирование — `loguru`.

## Supabase

Supabase — self-hosted в Docker (compose-проект `ai-work`), а не облачный. `SUPABASE_URL` = `http://127.0.0.1:54321` (Kong), Postgres — `127.0.0.1:54322`, Studio — `54323`. Эту же базу используют другие проекты и другие агенты, поэтому `.env` и схему трогать только по явной просьбе.

В shell окружении заданы `http_proxy`/`https_proxy` на внешний proxy. Без `no_proxy` запросы к локальному Kong уходят в этот proxy и падают с `RemoteProtocolError: Server disconnected without sending a response`. Лечится `allow_direct_localhost()` в `backend/app/config.py` — она дописывает `localhost,127.0.0.1` в `NO_PROXY`/`no_proxy`, не затирая уже заданные хосты. `main.py` вызывает её же. В `.env` эти переменные намеренно НЕ прописаны, чтобы не влиять на другие проекты. Если появится похожая ошибка коннекта — проверять proxy в первую очередь.


## Известные расхождения

`llm/model.py:47` — `send_message_structured_outputs` определён внутри `__init__`, поэтому методом класса не является и вызвать его нельзя.

## Язык

Логи, сообщения об ошибках и системные промпты — на русском.
