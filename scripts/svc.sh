#!/usr/bin/env bash
set -euo pipefail

# Обёртка для запуска с ноутбука по ssh: сервисы уходят в tmux-сессию и живут
# после выхода из ssh, а `start_service.sh` держит их на переднем плане.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SESSION=svc

case "${1:---restart}" in
  --restart)
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    tmux new -d -s "$SESSION" "cd '$ROOT' && scripts/start_service.sh --restart"
    echo "сессия $SESSION поднята, посмотреть: tmux attach -t $SESSION"
    ;;
  --stop)
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    "$ROOT/scripts/start_service.sh" --stop
    ;;
  *)
    echo "использование: scripts/svc.sh [--restart | --stop]" >&2
    exit 1
    ;;
esac
