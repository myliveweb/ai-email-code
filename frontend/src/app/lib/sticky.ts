export const SITE_KEY = "selectedSiteId";
export const AFF_KEY = "selectedAffAccountId";

// выбор запоминается по id, но восстанавливается только если такая запись ещё
// есть в списке: сайт или аккаунт могли удалить между визитами
export function pickStickyId(items: { id: number }[], key: string): number {
  const stored = Number(localStorage.getItem(key));
  if (stored && items.some((i) => i.id === stored)) return stored;
  return items[0]?.id ?? 0;
}

export function rememberId(key: string, id: number) {
  if (id) localStorage.setItem(key, String(id));
}
