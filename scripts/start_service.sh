#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# запросы к локальному Kong и к backend не должны уходить во внешний proxy из окружения
export NO_PROXY="localhost,127.0.0.1,${NO_PROXY:-}"
export no_proxy="$NO_PROXY"

BACKEND_PORT=4000
FRONTEND_PORT=4100

# `|| true` обязателен: у lsof без совпадений код возврата 1, и под set -e
# присваивание из такой подстановки роняет скрипт
port_pids() { lsof -t -i :"$1" -sTCP:LISTEN 2>/dev/null | sort -u || true; }
port_busy() { [[ -n "$(port_pids "$1")" ]]; }

usage() {
  cat <<'TXT'
Использование: scripts/start_service.sh [--start | --stop | --restart]

  --start     (по умолчанию) поднять backend:4000 и frontend:4100
  --stop      остановить оба
  --restart   остановить и поднять заново
TXT
}

stop_services() {
  local stopped=0
  if port_busy "$BACKEND_PORT" || port_busy "$FRONTEND_PORT"; then stopped=1; fi

  # паттерны намеренно узкие: на машине живут другие проекты со своими
  # next-server, широкий pkill снёс бы и их
  pkill -TERM -f 'uvicorn backend\.app\.main:app' 2>/dev/null || true
  pkill -TERM -f "next dev --port $FRONTEND_PORT" 2>/dev/null || true

  for _ in $(seq 1 12); do
    port_busy "$BACKEND_PORT" || port_busy "$FRONTEND_PORT" || break
    sleep 0.5
  done

  local port pids
  for port in "$BACKEND_PORT" "$FRONTEND_PORT"; do
    pids="$(port_pids "$port")"
    [[ -n "$pids" ]] || continue
    echo "порт $port не отпустили по SIGTERM, добиваю: $(echo "$pids" | tr '\n' ' ')"
    kill -KILL $pids 2>/dev/null || true
  done

  if [[ "$stopped" == 1 ]]; then
    echo "остановлено"
  else
    echo "нечего останавливать, порты уже свободны"
  fi
}

start_services() {
  local port
  for port in "$BACKEND_PORT" "$FRONTEND_PORT"; do
    if port_busy "$port"; then
      echo "порт $port занят (pid: $(port_pids "$port" | tr '\n' ' ')), используйте --restart" >&2
      exit 1
    fi
  done

  uv run uvicorn backend.app.main:app --host 127.0.0.1 --port "$BACKEND_PORT" --reload &
  local backend_pid=$!

  (cd frontend && npm run dev) &
  local frontend_pid=$!

  # один Ctrl+C должен снимать оба процесса, иначе порт остаётся занят
  trap 'kill "$backend_pid" "$frontend_pid" 2>/dev/null; wait; exit 0' INT TERM

  echo "backend  http://127.0.0.1:$BACKEND_PORT  (pid $backend_pid)"
  echo "frontend http://127.0.0.1:$FRONTEND_PORT  (pid $frontend_pid)"
  echo "Ctrl+C останавливает оба"

  wait -n
  echo "Один из процессов упал, останавливаю второй"
  kill "$backend_pid" "$frontend_pid" 2>/dev/null || true
  wait
}

case "${1:---start}" in
  --start) start_services ;;
  --stop) stop_services ;;
  --restart) stop_services; start_services ;;
  -h|--help) usage ;;
  *) echo "неизвестный ключ: $1" >&2; usage >&2; exit 1 ;;
esac
