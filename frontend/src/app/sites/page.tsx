"use client";

import { useEffect, useState } from "react";
import { SITE_KEY, pickStickyId, rememberId } from "../lib/sticky";

type Site = {
  id: number;
  name: string;
  meta: Record<string, unknown>;
  mail_subject: string | null;
  code_anchor: string | null;
  code_length: number | null;
  code_format: string | null;
};

type SiteStats = {
  github_count: number;
  email_count: number;
  total_count: number;
  github_balance: number;
  email_balance: number;
  total_balance: number;
};

function fmt(n: number) {
  return Number(n.toFixed(2)).toLocaleString("ru-RU");
}

function StatCell({ label, value, accent }: { label: string; value: string; accent?: boolean }) {
  return (
    <div>
      <div style={{ color: "var(--text-muted)", fontSize: "0.75rem", marginBottom: 2 }}>{label}</div>
      <div style={{ fontSize: "1.3rem", fontWeight: 600, color: accent ? "var(--accent)" : undefined }}>{value}</div>
    </div>
  );
}

function MetaValue({ value }: { value: unknown }) {
  if (value === null || value === undefined) {
    return <span style={{ opacity: 0.5 }}>—</span>;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    if (!entries.length) return <span style={{ opacity: 0.5 }}>{"{}"}</span>;
    return (
      <table style={{ width: "auto", borderCollapse: "collapse" }}>
        <tbody>
          {entries.map(([k, v]) => (
            <tr key={k}>
              <td style={{ padding: "2px 12px 2px 0", verticalAlign: "top", color: "var(--text-muted)", fontSize: "0.8rem", whiteSpace: "nowrap" }}>
                {k}
              </td>
              <td style={{ padding: "2px 0", verticalAlign: "top" }}>
                <MetaValue value={v} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    );
  }
  const text = String(value);
  if (/^https?:\/\//.test(text)) {
    return (
      <a href={text} target="_blank" rel="noopener noreferrer" style={{ color: "var(--accent)" }}>
        {text}
      </a>
    );
  }
  return <span>{text}</span>;
}

export default function SitesPage() {
  const [sites, setSites] = useState<Site[]>([]);
  const [selectedId, setSelectedId] = useState<number>(0);
  const [newSiteName, setNewSiteName] = useState("");
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [draftName, setDraftName] = useState("");
  const [draftSubject, setDraftSubject] = useState("");
  const [draftAnchor, setDraftAnchor] = useState("");
  const [draftDigits, setDraftDigits] = useState("");
  const [draftFormat, setDraftFormat] = useState("digits");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [stats, setStats] = useState<SiteStats | null>(null);
  const [addingKey, setAddingKey] = useState(false);
  const [keyName, setKeyName] = useState("");
  const [keyValue, setKeyValue] = useState("");

  useEffect(() => {
    fetch("/api/sites")
      .then((res) => {
        if (!res.ok) throw new Error(`Backend: ${res.status}`);
        return res.json();
      })
      .then((d: Site[]) => {
        setSites(d);
        if (d.length) setSelectedId(pickStickyId(d, SITE_KEY));
        setLoading(false);
      })
      .catch((e: Error) => { setError(e.message); setLoading(false); });
  }, []);

  useEffect(() => { rememberId(SITE_KEY, selectedId); }, [selectedId]);

  const site = sites.find((s) => s.id === selectedId) ?? null;

  useEffect(() => {
    if (!selectedId) { setStats(null); return; }
    const controller = new AbortController();
    setStats(null);
    fetch(`/api/sites/${selectedId}/stats`, { signal: controller.signal })
      .then((res) => (res.ok ? res.json() : null))
      .then((d: SiteStats | null) => setStats(d))
      .catch(() => {});
    return () => controller.abort();
  }, [selectedId]);

  const handleAddSite = () => {
    const name = newSiteName.trim();
    if (!name) return;
    setError(null);
    fetch("/api/sites", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    })
      .then(async (res) => {
        if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || "Ошибка добавления");
        return res.json();
      })
      .then((created: Site) => {
        setSites((prev) => [...prev, created].sort((a, b) => a.name.localeCompare(b.name)));
        setSelectedId(created.id);
        setNewSiteName("");
        setAdding(false);
      })
      .catch((e: Error) => setError(e.message));
  };

  const startEdit = () => {
    if (!site) return;
    setDraftName(site.name);
    setDraftSubject(site.mail_subject ?? "");
    setDraftAnchor(site.code_anchor ?? "");
    setDraftDigits(site.code_length === null ? "" : String(site.code_length));
    setDraftFormat(site.code_format ?? "digits");
    setDraft(JSON.stringify(site.meta ?? {}, null, 2));
    setEditing(true);
    setError(null);
  };

  const handleSaveMeta = () => {
    if (!site) return;
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(draft || "{}");
    } catch {
      setError("Некорректный JSON");
      return;
    }
    const name = draftName.trim();
    if (!name) {
      setError("Название не может быть пустым");
      return;
    }
    const digits = draftDigits.trim();
    if (digits && !/^\d+$/.test(digits)) {
      setError("Количество символов — целое число");
      return;
    }
    setError(null);
    fetch(`/api/sites/${site.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name,
        meta: parsed,
        mail_subject: draftSubject.trim() || null,
        code_anchor: draftAnchor.trim() || null,
        code_length: digits ? parseInt(digits, 10) : null,
        code_format: draftFormat,
      }),
    })
      .then(async (res) => {
        if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || "Ошибка сохранения");
        return res.json();
      })
      .then((updated: Site) => {
        setSites((prev) =>
          prev
            .map((s) => (s.id === updated.id ? { ...s, ...updated } : s))
            .sort((a, b) => a.name.localeCompare(b.name)),
        );
        setEditing(false);
      })
      .catch((e: Error) => setError(e.message));
  };

  const cancelAddKey = () => {
    setAddingKey(false);
    setKeyName("");
    setKeyValue("");
  };

  const handleAddKey = () => {
    if (!site) return;
    const key = keyName.trim();
    if (!key) {
      setError("Название ключа не может быть пустым");
      return;
    }
    setError(null);
    // PATCH заменяет meta целиком, поэтому мержим на клиенте
    const meta = { ...(site.meta ?? {}), [key]: keyValue };
    fetch(`/api/sites/${site.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ meta }),
    })
      .then(async (res) => {
        if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || "Ошибка сохранения");
        return res.json();
      })
      .then((updated: Site) => {
        setSites((prev) => prev.map((s) => (s.id === updated.id ? { ...s, meta: updated.meta } : s)));
        cancelAddKey();
      })
      .catch((e: Error) => setError(e.message));
  };

  const handleDeleteSite = () => {
    if (!site) return;
    if (!confirm(`Удалить сайт «${site.name}»? Действие необратимо.`)) return;
    setError(null);
    fetch(`/api/sites/${site.id}`, { method: "DELETE" })
      .then(async (res) => {
        if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || "Ошибка удаления");
        return res.json();
      })
      .then(() => {
        const rest = sites.filter((s) => s.id !== site.id);
        setSites(rest);
        setSelectedId(rest.length ? rest[0].id : 0);
        setEditing(false);
      })
      .catch((e: Error) => setError(e.message));
  };

  if (loading) return <main style={{ padding: "2rem" }}><p>Загрузка...</p></main>;

  const metaEntries = Object.entries(site?.meta ?? {});

  return (
    <main style={{ padding: "2rem" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 24 }}>
        <select value={selectedId} onChange={(e) => { setSelectedId(Number(e.target.value)); setEditing(false); }}>
          {sites.map((s) => (
            <option key={s.id} value={s.id}>{s.name}</option>
          ))}
        </select>
        {adding ? (
          <>
            <input
              autoFocus
              value={newSiteName}
              onChange={(e) => setNewSiteName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") handleAddSite();
                if (e.key === "Escape") { setAdding(false); setNewSiteName(""); }
              }}
              placeholder="Новый сайт"
            />
            <button onClick={handleAddSite}>Добавить</button>
            <button onClick={() => { setAdding(false); setNewSiteName(""); }}>Отмена</button>
          </>
        ) : (
          <button
            onClick={() => setAdding(true)}
            title="Добавить сайт"
            style={{ fontSize: "1.1rem", lineHeight: 1, padding: "2px 10px" }}
          >
            +
          </button>
        )}
      </div>

      {error && <p style={{ color: "#ff6b6b", marginBottom: 12, fontSize: "0.85rem" }}>{error}</p>}

      {!site ? (
        <p style={{ color: "var(--text-muted)" }}>Сайтов пока нет.</p>
      ) : (
        <div style={{ background: "var(--card-bg)", borderRadius: 8, boxShadow: "var(--card-shadow)", padding: 20 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16 }}>
            {editing ? (
              <input
                value={draftName}
                onChange={(e) => setDraftName(e.target.value)}
                style={{ fontSize: "1.3rem", fontWeight: 600, flex: 1, maxWidth: 360 }}
              />
            ) : (
              <>
                <h2 style={{ margin: 0 }}>{site.name}</h2>
                <a
                  href={`https://${site.name}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  title={`Открыть ${site.name}`}
                  style={{ fontSize: "1.1rem" }}
                >
                  ↗
                </a>
              </>
            )}
            <button
              onClick={editing ? () => setEditing(false) : startEdit}
              title={editing ? "Отменить" : "Редактировать название и meta"}
              style={{ marginLeft: "auto", fontSize: "0.85rem" }}
            >
              {editing ? "Отмена" : "✎ Редактировать"}
            </button>
          </div>

          {stats && (
            <div
              style={{
                display: "flex",
                gap: 40,
                flexWrap: "wrap",
                padding: "14px 0",
                borderTop: "1px solid var(--border)",
                borderBottom: "1px solid var(--border)",
                marginBottom: 20,
              }}
            >
              <StatCell label="Аккаунтов всего" value={String(stats.total_count)} accent />
              <StatCell label="GitHub" value={String(stats.github_count)} />
              <StatCell label="Почтовых" value={String(stats.email_count)} />
              <StatCell label="Баланс всего" value={fmt(stats.total_balance)} accent />
              <StatCell label="Баланс GitHub" value={fmt(stats.github_balance)} />
              <StatCell label="Баланс почтовых" value={fmt(stats.email_balance)} />
            </div>
          )}

          <div style={{ marginBottom: 20 }}>
            <div style={{ color: "var(--text-muted)", fontSize: "0.75rem", marginBottom: 6 }}>
              Правила получения кода
            </div>
            {editing ? (
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                <input
                  value={draftSubject}
                  onChange={(e) => setDraftSubject(e.target.value)}
                  placeholder="тема письма"
                  style={{ width: 420 }}
                />
                <input
                  value={draftAnchor}
                  onChange={(e) => setDraftAnchor(e.target.value)}
                  placeholder="якорь"
                  style={{ width: 240 }}
                />
                <input
                  value={draftDigits}
                  onChange={(e) => setDraftDigits(e.target.value)}
                  placeholder="символов в коде"
                  inputMode="numeric"
                  style={{ width: 140 }}
                />
                <select value={draftFormat} onChange={(e) => setDraftFormat(e.target.value)}>
                  <option value="digits">только цифры</option>
                  <option value="alnum">цифры и буквы</option>
                </select>
              </div>
            ) : (
              <table style={{ width: "auto", borderCollapse: "collapse" }}>
                <tbody>
                  <tr>
                    <td style={{ padding: "2px 24px 2px 0", color: "var(--text-muted)", fontSize: "0.8rem" }}>
                      тема письма
                    </td>
                    <td style={{ padding: "2px 0" }}>
                      {site.mail_subject || <span style={{ opacity: 0.5 }}>—</span>}
                    </td>
                  </tr>
                  <tr>
                    <td style={{ padding: "2px 24px 2px 0", color: "var(--text-muted)", fontSize: "0.8rem" }}>
                      якорь
                    </td>
                    <td style={{ padding: "2px 0" }}>
                      {site.code_anchor || <span style={{ opacity: 0.5 }}>—</span>}
                    </td>
                  </tr>
                  <tr>
                    <td style={{ padding: "2px 24px 2px 0", color: "var(--text-muted)", fontSize: "0.8rem" }}>
                      символов в коде
                    </td>
                    <td style={{ padding: "2px 0" }}>
                      {site.code_length ?? <span style={{ opacity: 0.5 }}>—</span>}
                    </td>
                  </tr>
                  <tr>
                    <td style={{ padding: "2px 24px 2px 0", color: "var(--text-muted)", fontSize: "0.8rem" }}>
                      формат кода
                    </td>
                    <td style={{ padding: "2px 0" }}>
                      {site.code_format === "alnum" ? "цифры и буквы" : "только цифры"}
                    </td>
                  </tr>
                </tbody>
              </table>
            )}
          </div>

          {!editing && (
            <div style={{ marginBottom: 14 }}>
              {addingKey ? (
                <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                  <input
                    autoFocus
                    value={keyName}
                    onChange={(e) => setKeyName(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") handleAddKey();
                      if (e.key === "Escape") cancelAddKey();
                    }}
                    placeholder="ключ"
                    style={{ width: 200 }}
                  />
                  <input
                    value={keyValue}
                    onChange={(e) => setKeyValue(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") handleAddKey();
                      if (e.key === "Escape") cancelAddKey();
                    }}
                    placeholder="значение"
                    style={{ width: 420 }}
                  />
                  <button onClick={handleAddKey}>Сохранить</button>
                  <button onClick={cancelAddKey}>Отмена</button>
                </div>
              ) : (
                <button onClick={() => setAddingKey(true)} style={{ fontSize: "0.85rem" }}>
                  + Добавить ключ
                </button>
              )}
            </div>
          )}

          {editing ? (
            <>
              <textarea
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                spellCheck={false}
                style={{
                  width: "100%",
                  minHeight: 320,
                  fontFamily: "var(--font-geist-mono), monospace",
                  fontSize: "0.85rem",
                  resize: "vertical",
                }}
              />
              <div style={{ marginTop: 12 }}>
                <button onClick={handleSaveMeta}>Сохранить</button>
              </div>
            </>
          ) : metaEntries.length === 0 ? (
            <p style={{ color: "var(--text-muted)", fontSize: "0.85rem" }}>meta пуст.</p>
          ) : (
            <table style={{ width: "auto", borderCollapse: "collapse" }}>
              <tbody>
                {metaEntries.map(([key, value]) => (
                  <tr key={key} style={{ borderTop: "1px solid var(--border)" }}>
                    <td style={{ padding: "10px 24px 10px 0", verticalAlign: "top", fontWeight: 600, whiteSpace: "nowrap" }}>
                      {key}
                    </td>
                    <td style={{ padding: "10px 0", verticalAlign: "top" }}>
                      <MetaValue value={value} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 28 }}>
            <button
              onClick={handleDeleteSite}
              title="Удалить сайт целиком"
              style={{
                fontSize: "0.85rem",
                color: "#ff6b6b",
                borderColor: "#ff6b6b",
                background: "transparent",
              }}
            >
              Удалить сайт
            </button>
          </div>
        </div>
      )}
    </main>
  );
}
