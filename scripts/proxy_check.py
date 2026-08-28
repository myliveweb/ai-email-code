"""Оценка пула прокси перед тем, как пускать его на сбор аккаунтов.

    uv run python scripts/proxy_check.py [--source api|env] [--limit N] [--threads N]

Отвечает на три вопроса, от которых зависит годность пула, и ни на один больше:
где стоит выходной адрес, пускает ли по нему GitHub и сколько это стоит по времени.

Гео важно не из любопытства: GitHub с британского адреса не открывает даже свою
страницу (Cloudflare), проверено на прежнем пуле, — поэтому адрес не из США негоден
независимо от скорости. Проверка идёт до `github.com/login`, а не до `github.com`:
именно эту страницу открывает сбор, и именно на ней ловится «доступ ограничен».

Ходим `curl_cffi` с `impersonate="chrome"`: сбор работает живым Chromium, и мерить
надо тем же отпечатком TLS — у `requests` он чужой, Cloudflare отвечает иначе,
и замер соврал бы в обе стороны.

Отчёт — `log/proxy_check.txt`, годные адреса строкой для `HARVEST_PROXIES` —
`log/proxy_check_good.txt`.
"""

from __future__ import annotations

import argparse
import os
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from curl_cffi import requests as cffi
from dotenv import find_dotenv, load_dotenv
from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "log"
REPORT = LOG_DIR / "proxy_check.txt"
GOOD_LIST = LOG_DIR / "proxy_check_good.txt"

API_BASE = "https://api.proxyscrape.com"
# Гео спрашивается у ip-api: лимит 45 запросов в минуту он считает по адресу
# спрашивающего, а спрашивает каждый прокси со своего — в лимит не упираемся.
GEO_URL = "http://ip-api.com/json/?fields=status,country,countryCode,city,isp,as,proxy,hosting"
GITHUB_URL = "https://github.com/login"
TIMEOUT = 25
# Больше десяти одновременных соединений премиум-подписка не даёт (`max_connections`),
# и упереться в её же лимит значит намерить чужую проблему.
THREADS = 8


@dataclass
class Probe:
    """Итог по одному адресу. Пустые поля значат «не дошли до этой проверки»."""

    proxy: str
    exit_ip: str = ""
    country: str = ""
    city: str = ""
    isp: str = ""
    hosting: bool = False
    geo_ms: int = 0
    github_code: int = 0
    github_ms: int = 0
    github_note: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def alive(self) -> bool:
        return bool(self.exit_ip)

    @property
    def us(self) -> bool:
        return self.country == "US"

    @property
    def good(self) -> bool:
        """Годен = живой, американский и пустил на страницу входа GitHub."""
        return self.alive and self.us and self.github_code == 200


def api_pool(limit: int) -> tuple[list[str], str, str]:
    """Список адресов и общая пара логин/пароль из подписки proxyscrape."""
    key = os.getenv("PROXYSCRAPE_KEY", "").strip()
    sub = os.getenv("PROXYSCRAPE_SUB", "").strip()
    if not key or not sub:
        sys.exit("нет PROXYSCRAPE_KEY или PROXYSCRAPE_SUB в .env")

    head = {"api-token": key}
    base = f"{API_BASE}/v4/account/{sub}/datacenter_shared"
    over = cffi.get(f"{base}/overview", headers=head, timeout=TIMEOUT).json()["data"]
    svc = over["services"]["datacenter_shared"]
    user, pwd = svc["proxy_username"], svc["proxy_password"]

    raw = cffi.get(
        f"{base}/proxy-list",
        params={"type": "displayproxies", "protocol": "http", "format": "normal", "status": "all"},
        headers=head,
        timeout=TIMEOUT,
    ).text
    hosts = [s.strip() for s in raw.splitlines() if re.fullmatch(r"\d+\.\d+\.\d+\.\d+:\d+", s.strip())]
    left = over["bandwidth"] - over.get("bandwidth_used", 0)
    logger.info(
        "подписка proxyscrape: адресов {}, трафика осталось {:.2f} ГБ, пробник {}",
        len(hosts),
        left / 1e9,
        "да" if over.get("is_trial") else "нет",
    )
    return [f"{h}:{user}:{pwd}" for h in hosts[: limit or None]], user, pwd


def env_pool(limit: int) -> list[str]:
    """Пул, на котором сбор работает прямо сейчас, — чтобы сравнивать с чем-то."""
    raw = os.getenv("HARVEST_PROXIES", "")
    parts = [p for p in re.split(r"[\s,]+", raw) if p]
    return parts[: limit or None]


def proxy_url(spec: str) -> str:
    """`host:port:user:pass` продавца → адрес, понятный curl."""
    f = spec.split(":")
    if len(f) == 4:
        return f"http://{f[2]}:{f[3]}@{f[0]}:{f[1]}"
    if len(f) == 2:
        return f"http://{f[0]}:{f[1]}"
    raise ValueError(f"не разобрать адрес прокси: {spec}")


def check_one(spec: str) -> Probe:
    p = Probe(proxy=spec)
    try:
        url = proxy_url(spec)
    except ValueError as e:
        p.errors.append(str(e))
        return p
    proxies = {"http": url, "https": url}

    began = time.monotonic()
    try:
        r = cffi.get(GEO_URL, proxies=proxies, timeout=TIMEOUT, impersonate="chrome")
        p.geo_ms = int((time.monotonic() - began) * 1000)
        d = r.json()
        p.exit_ip = d.get("query") or spec.split(":")[0]
        p.country = d.get("countryCode") or ""
        p.city = d.get("city") or ""
        p.isp = (d.get("isp") or d.get("as") or "")[:40]
        p.hosting = bool(d.get("hosting"))
    except Exception as e:  # noqa: BLE001 — причина нужна строкой в отчёт, а не трейсбеком
        p.errors.append(f"гео: {type(e).__name__}: {e}".replace("\n", " ")[:160])
        return p

    began = time.monotonic()
    try:
        r = cffi.get(GITHUB_URL, proxies=proxies, timeout=TIMEOUT, impersonate="chrome")
        p.github_ms = int((time.monotonic() - began) * 1000)
        p.github_code = r.status_code
        p.github_note = github_says(r.status_code, r.text)
    except Exception as e:  # noqa: BLE001
        p.errors.append(f"github: {type(e).__name__}: {e}".replace("\n", " ")[:160])
    return p


def github_says(code: int, body: str) -> str:
    """Дословный смысл ответа. «200» мало: страница входа и заглушка Cloudflare
    приходят одним кодом, а лечатся по-разному — вторая не лечится вовсе."""
    low = body.lower()
    if "sign in to github" in low or 'name="login"' in low or "login_field" in low:
        return "форма входа"
    if "cf-mitigated" in low or "just a moment" in low or "cf-browser-verification" in low:
        return "заглушка Cloudflare"
    if "access to this site has been restricted" in low or "rate limit" in low:
        return "доступ ограничен"
    if code == 429:
        return "слишком много запросов с адреса"
    if 300 <= code < 400:
        return "перенаправление"
    return f"страница без формы входа, {len(body)} байт"


def render(probes: list[Probe], source: str, secs: float) -> str:
    alive = [p for p in probes if p.alive]
    good = [p for p in probes if p.good]
    us = [p for p in alive if p.us]
    ok_gh = [p for p in alive if p.github_code == 200]

    out: list[str] = []
    out.append(f"Проверка пула прокси ({source}), {time.strftime('%d.%m %H:%M:%S')}, {secs:.0f} с")
    out.append(f"адресов: {len(probes)}; ответили: {len(alive)}; из США: {len(us)}; пустил GitHub: {len(ok_gh)}; годных: {len(good)}")

    countries: dict[str, int] = {}
    for p in alive:
        countries[p.country or "?"] = countries.get(p.country or "?", 0) + 1
    if countries:
        out.append("страны: " + ", ".join(f"{k} — {v}" for k, v in sorted(countries.items(), key=lambda x: -x[1])))

    nets = {".".join(p.exit_ip.split(".")[:3]) for p in alive if p.exit_ip}
    out.append(f"различных подсетей /24 среди ответивших: {len(nets)}")

    if ok_gh:
        times = sorted(p.github_ms for p in ok_gh)
        out.append(
            "время до страницы входа GitHub: медиана {} мс, быстрейший {} мс, худший {} мс".format(
                int(statistics.median(times)), times[0], times[-1]
            )
        )
    notes: dict[str, int] = {}
    for p in alive:
        notes[p.github_note or "не дошли"] = notes.get(p.github_note or "не дошли", 0) + 1
    out.append("что ответил GitHub: " + ", ".join(f"{k} — {v}" for k, v in sorted(notes.items(), key=lambda x: -x[1])))
    out.append("")
    return "\n".join(out)


def rows(probes: list[Probe]) -> str:
    """Построчно: сперва отказные, потом годные. Читают отчёт ради отказных."""
    order = sorted(probes, key=lambda p: (p.good, p.github_ms or 10**9))
    out = ["Построчно (сначала отказные):"]
    for p in order:
        host = p.proxy.split(":")[0]
        mark = "годен  " if p.good else "негоден"
        geo = f"{p.country or '??'} {p.city or ''}".strip()
        gh = f"{p.github_code} {p.github_note}" if p.github_code else "GitHub не ответил"
        line = f"  {mark} {host:<16} {geo:<22} {p.isp:<32} {p.github_ms or 0:>6} мс  {gh}"
        if p.errors:
            line += " | " + "; ".join(p.errors)
        out.append(line)
    return "\n".join(out) + "\n"


def main() -> int:
    load_dotenv(find_dotenv())
    ap = argparse.ArgumentParser(description="проверка пула прокси перед сбором аккаунтов")
    ap.add_argument("--source", choices=("api", "env"), default="api", help="откуда брать адреса")
    ap.add_argument("--limit", type=int, default=0, help="проверить только первые N адресов")
    ap.add_argument("--threads", type=int, default=THREADS, help=f"одновременных проверок (по умолчанию {THREADS})")
    args = ap.parse_args()

    pool = api_pool(args.limit)[0] if args.source == "api" else env_pool(args.limit)
    if not pool:
        sys.exit("пул пуст: нечего проверять")

    began = time.monotonic()
    logger.info("проверяю {} адресов в {} потоков", len(pool), args.threads)
    with ThreadPoolExecutor(max_workers=args.threads) as pool_exec:
        probes = list(pool_exec.map(check_one, pool))
    secs = time.monotonic() - began

    LOG_DIR.mkdir(exist_ok=True)
    text = render(probes, args.source, secs) + rows(probes)
    REPORT.write_text(text, encoding="utf-8")
    good = [p.proxy for p in probes if p.good]
    GOOD_LIST.write_text(",".join(good) + "\n", encoding="utf-8")
    print(text)
    logger.info("отчёт: {}; годных адресов {} — строкой в {}", REPORT, len(good), GOOD_LIST)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
