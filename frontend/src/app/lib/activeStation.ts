"use client";

import { useCallback, useEffect, useState } from "react";

import { TOPIC_CLAUDE, useLiveUpdate } from "./liveUpdates";

export type ActiveStation = {
  station: string | null;
  login: string | null;
  account_id?: number | null;
  table?: string | null;
  balance?: number | null;
  known: boolean;
};

// один запрос на загрузку страницы: значение нужно и меню, и списку сайтов
let cached: Promise<ActiveStation> | null = null;

function load(): Promise<ActiveStation> {
  cached ??= fetch("/api/claude/active").then((r) => r.json());
  return cached;
}

export function useActiveStation(): ActiveStation | null {
  const [active, setActive] = useState<ActiveStation | null>(null);

  useEffect(() => {
    let alive = true;
    load().then((data) => { if (alive) setActive(data); }).catch(() => {});
    return () => { alive = false; };
  }, []);

  // ключ мог повернуться в кроне: сбрасываем общий кэш и берём заново
  useLiveUpdate(TOPIC_CLAUDE, useCallback(() => {
    cached = null;
    load().then(setActive).catch(() => {});
  }, []));

  return active;
}
