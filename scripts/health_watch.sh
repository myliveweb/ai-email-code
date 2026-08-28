#!/usr/bin/env bash
# Сторож backend: раз в минуту из крона щупает /api/ping и поднимает сервис, если тот
# молчит. 26.08 backend лежал полтора часа, и прогон сбора потерял девять аккаунтов
# по 70 $: полный ключ панель отдаёт один раз, а записать его было некуда.
#
#   scripts/health_watch.sh [--once]
#
# `--once` — один взгляд без повторов, для проверки руками. Пишет log/health_watch.txt
# только при событиях: живой backend не оставляет строк вовсе.
# `touch log/health_watch.off` глушит сторожа: иначе после ручного --stop он поднимет
# сервис обратно через минуту.

# Без `set -e`: неудачный curl это рабочая ситуация, а не повод выйти.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# У крона PATH это /usr/bin:/bin, а npm с node лежат в linuxbrew, uv — в ~/.local/bin.
# Без этой строки start_service.sh поднимет backend и не найдёт frontend.
export PATH="$HOME/.local/bin:/home/linuxbrew/.linuxbrew/bin:/usr/local/bin:/usr/bin:/bin"
# Локальный backend через внешний proxy недостижим, см. раздел Proxy в CLAUDE.md.
export NO_PROXY="localhost,127.0.0.1,${NO_PROXY:-}"
export no_proxy="$NO_PROXY"

BACKEND_PORT=4000
FRONTEND_PORT=4100
URL="http://127.0.0.1:$BACKEND_PORT/api/ping"
FRONT_URL="http://127.0.0.1:$FRONTEND_PORT/"
LOG="$ROOT/log/health_watch.txt"
BOOT_LOG="$ROOT/log/health_watch_service.txt"
OFF_FILE="$ROOT/log/health_watch.off"
LOCK="$ROOT/log/health_watch.lock"
# Три взгляда с паузой: разовый таймаут бывает у живого сервиса под нагрузкой,
# а перезапуск по нему стоил бы обрыва идущего прогона сбора.
TRIES=3
PAUSE=3
# Сколько ждать подъёма. uvicorn встаёт за пару секунд, но --reload и импорт
# приложения на холодную занимают больше.
BOOT_WAIT=30

mkdir -p "$ROOT/log"
say() { echo "$(date '+%d.%m %H:%M:%S') $*" >>"$LOG"; }

alive() { curl -s --noproxy '*' -m 5 "$URL" | grep -q '"alive"'; }

listening() { ss -ltn 2>/dev/null | grep -q "127.0.0.1:$BACKEND_PORT"; }
front_up() { curl -s --noproxy '*' -m 5 -o /dev/null -w '%{http_code}' "$FRONT_URL" 2>/dev/null; }

# Кто именно держит порт. В журнале это отделяет наш uvicorn от чужого процесса,
# заехавшего на 4000: во втором случае перезапуск сервиса не поможет вовсе, потому
# что start_service.sh снимает только свои узкие pkill-паттерны.
holder() {
  local pids
  pids="$(lsof -t -i :"$BACKEND_PORT" -sTCP:LISTEN 2>/dev/null | sort -u | tr '\n' ' ')"
  [[ -n "$pids" ]] || { echo "порт никто не слушает"; return; }
  local pid out=""
  for pid in $pids; do
    out+="pid $pid ($(tr '\0\n\t' '   ' </proc/"$pid"/cmdline 2>/dev/null | cut -c1-90)); "
  done
  echo "${out%; }"
}

# Сколько соединений скопилось в очереди приёма. Непустая очередь при слушающем
# сокете и есть подпись зависания: процесс жив, порт открыт, но accept никто
# не делает. Ровно так 26.08 backend простоял полтора часа.
backlog() { ss -ltn 2>/dev/null | awk -v p=":$BACKEND_PORT\$" '$4 ~ p {print $2; exit}'; }

# Состояние базы. Спрашивается только когда backend уже молчит: в норме это ноль
# лишних запросов, а в журнале появляется разница между «лежит наш процесс» и «лёг
# весь стенд». Kong отдаёт 200 на /rest/v1/ и без ключа, поэтому секреты не нужны.
# Перезапускать базу сторож не пробует и не будет: контейнеры общие с другими
# проектами, у них своя restart policy `unless-stopped` и healthcheck pg_isready.
db_state() {
  local code health
  code="$(curl -s --noproxy '*' -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:54321/rest/v1/ 2>/dev/null)"
  health="$(docker inspect supabase_db_ai-work --format '{{.State.Health.Status}}' 2>/dev/null)"
  echo "база: Kong отдаёт $code, контейнер supabase_db_ai-work — ${health:-состояние не прочитать}"
}

# Чем именно кончилась проба. Строка идёт в журнал, и в ней вся разница между
# «процесс умер» и «процесс жив, но не отвечает»: первое даёт отказ соединения,
# второе — таймаут при слушающем порте. Лечится это одинаково, а вот читать
# журнал потом без этой подробности бессмысленно.
why() {
  local code
  code="$(curl -s --noproxy '*' -m 5 -o /dev/null -w '%{http_code}' "$URL" 2>/dev/null)"
  if listening; then
    if [[ "$code" == "000" ]]; then
      echo "порт $BACKEND_PORT слушается, но за 5 с ни байта ответа — процесс завис на graceful shutdown или в блокирующем вызове; в очереди приёма ждут соединений: $(backlog); держит порт: $(holder); $(db_state)"
    else
      echo "порт $BACKEND_PORT отвечает кодом $code вместо ping — на порту не наш backend либо приложение отдаёт ошибку; держит порт: $(holder); $(db_state)"
    fi
  else
    echo "порт $BACKEND_PORT не слушает никто — процесс uvicorn умер или его сняли; $(db_state)"
  fi
}

if [[ "${1:-}" == "--once" ]]; then
  if alive; then
    echo "backend на $BACKEND_PORT отвечает ping, frontend на $FRONTEND_PORT отдаёт код $(front_up)"
    echo "порт backend держит: $(holder)"
    echo "$(db_state)"
    exit 0
  fi
  echo "backend на $BACKEND_PORT не отвечает: $(why)"
  echo "поднять руками: scripts/start_service.sh --restart"
  exit 1
fi

# Дальше идёт ветка крона, и только ей нужны заглушка и замок. Проба `--once` выше них
# намеренно: при `health_watch.off` или при уже идущем подъёме она молча выходила бы
# с кодом 0, а молчание тут читается как «сервис жив» — то есть проба руками врала бы
# ровно в те минуты, когда её и запускают.
[[ -e "$OFF_FILE" ]] && exit 0

# Один сторож за раз: подъём сервиса занимает секунды, и второй экземпляр поверх
# первого снял бы только что поднятый backend.
exec 9>"$LOCK"
flock -n 9 || exit 0

began="$(date +%s)"
for _ in $(seq 1 "$TRIES"); do
  alive && exit 0
  sleep "$PAUSE"
done

say "backend молчит $TRIES взгляда подряд за $(( $(date +%s) - began )) с ($URL, таймаут 5 с на взгляд): $(why)"
say "перезапускаю сервис: scripts/start_service.sh --restart, вывод подъёма в ${BOOT_LOG##*/}"
# setsid: иначе процессы сервиса остаются детьми крон-задания и уходят с ним.
setsid nohup "$ROOT/scripts/start_service.sh" --restart >>"$BOOT_LOG" 2>&1 &

for _ in $(seq 1 "$BOOT_WAIT"); do
  sleep 1
  if alive; then
    say "backend поднялся и отвечает ping — $(( $(date +%s) - began )) с от начала проверки, frontend на $FRONTEND_PORT отдаёт код $(front_up) (000 значит, что Next ещё собирается, это норма первые полминуты)"
    exit 0
  fi
done

say "backend не поднялся за ${BOOT_WAIT} с после перезапуска: $(why)"
say "смотреть ${BOOT_LOG##*/} — там вывод start_service.sh целиком; следующий взгляд крона через минуту, попытка повторится сама"
exit 1
