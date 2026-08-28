"use client";

import { useEffect, useRef } from "react";

// Один EventSource на вкладку: крон-скрипты через backend присылают тему, страница
// перезапрашивает только свои данные. Перезагрузки страницы нет — обновляется
// таблица, потому что данные лежат в state.
let source: EventSource | null = null;
const listeners = new Map<string, Set<() => void>>();

function ensureSource() {
  if (source || typeof window === "undefined") return;
  source = new EventSource("/api/events");
  source.addEventListener("update", (e) => {
    const topic = (e as MessageEvent<string>).data;
    listeners.get(topic)?.forEach((cb) => cb());
  });
  // разрыв (перезапуск backend) EventSource переподключает сам
}

export function useLiveUpdate(topic: string, onUpdate: () => void) {
  const latest = useRef(onUpdate);

  useEffect(() => { latest.current = onUpdate; }, [onUpdate]);

  useEffect(() => {
    ensureSource();
    const cb = () => latest.current();
    const set = listeners.get(topic) ?? new Set<() => void>();
    set.add(cb);
    listeners.set(topic, set);
    return () => { set.delete(cb); };
  }, [topic]);
}

export const TOPIC_CLAUDE = "claude";
export const TOPIC_LINUXDO = "linuxdo";
