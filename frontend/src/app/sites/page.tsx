"use client";

import { useCallback, useEffect, useState } from "react";
import LongText from "../components/LongText";
import ClaudeIcon from "../components/ClaudeIcon";
import SiteSelect from "../components/SiteSelect";
import { useActiveStation } from "../lib/activeStation";
import { TOPIC_CLAUDE, useLiveUpdate } from "../lib/liveUpdates";
import { SITE_KEY, pickStickyId, rememberId } from "../lib/sticky";

type Site = {
  id: number;
  name: string;
  cnt: number | null;
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

type Station = {
  site_id: number;
  balance: number;
  login: string;
  can_activate: boolean;
};

function fmt(n: number) {
  return Number(n.toFixed(2)).toLocaleString("ru-RU");
}

function StatCell({ label, value, accent }: { label: string; value: string; accent?: boolean }) {
  return (
    <div className="st-stat">
      <div className="st-stat-label">{label}</div>
      <div className={accent ? "st-stat-value st-stat-value--accent" : "st-stat-value"}>{value}</div>
    </div>
  );
}

function MetaValue({ value }: { value: unknown }) {
  if (value === null || value === undefined) {
    return <span className="st-dash">—</span>;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    if (!entries.length) return <span className="st-dash">{"{}"}</span>;
    return (
      <table className="st-meta-nested">
        <tbody className="st-meta-nested-body">
          {entries.map(([k, v]) => (
            <tr key={k} className="st-meta-nested-row">
              <td className="st-meta-nested-key">
                {k}
              </td>
              <td className="st-meta-nested-value">
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
      <a href={text} target="_blank" rel="noopener noreferrer" className="st-meta-link">
        {text}
      </a>
    );
  }
  return <LongText text={text} />;
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
  // вместе с цифрами держим сайт, которому они принадлежат: иначе при переключении
  // пришлось бы гасить их setState прямо в эффекте, а это запрещено правилом
  // react-hooks/set-state-in-effect
  const [stats, setStats] = useState<{ siteId: number; data: SiteStats | null } | null>(null);
  const [addingKey, setAddingKey] = useState(false);
  const [keyName, setKeyName] = useState("");
  const [keyValue, setKeyValue] = useState("");
  const [stations, setStations] = useState<Record<string, Station>>({});
  const [switchedTo, setSwitchedTo] = useState<string | null>(null);
  const [activating, setActivating] = useState(false);
  const [claudeMsg, setClaudeMsg] = useState<string | null>(null);
  const active = useActiveStation();

  const loadStations = useCallback(() => {
    fetch("/api/claude/stations")
      .then((res) => (res.ok ? res.json() : null))
      .then((d: { stations?: Record<string, Station> } | null) => setStations(d?.stations ?? {}))
      .catch(() => {});
  }, []);

  const loadStats = useCallback((id: number, signal?: AbortSignal) => {
    fetch(`/api/sites/${id}/stats`, { signal })
      .then((res) => (res.ok ? res.json() : null))
      .then((d: SiteStats | null) => setStats({ siteId: id, data: d }))
      .catch(() => {});
  }, []);

  useEffect(() => { loadStations(); }, [loadStations]);

  // крон-скрипт закончил прогон: метки станций и балансы в базе уже другие
  useLiveUpdate(TOPIC_CLAUDE, useCallback(() => {
    loadStations();
    if (selectedId) loadStats(selectedId);
  }, [loadStations, loadStats, selectedId]));

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
  // цифры прежнего сайта не показываем: пока не пришли свои, здесь null
  const siteStats = stats && stats.siteId === selectedId ? stats.data : null;

  useEffect(() => {
    if (!selectedId) return;
    const controller = new AbortController();
    loadStats(selectedId, controller.signal);
    return () => controller.abort();
  }, [selectedId, loadStats]);

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

  const handleActivate = () => {
    if (!site) return;
    setClaudeMsg(null);
    setActivating(true);
    fetch("/api/claude/activate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ site_id: site.id }),
    })
      .then(async (res) => {
        if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || "Ошибка переключения");
        return res.json();
      })
      .then((d: { login?: string }) => {
        setSwitchedTo(site.name);
        setClaudeMsg(`Ключ ${d.login ?? ""} записан. Новую станцию подхватит следующая сессия.`);
      })
      .catch((e: Error) => setClaudeMsg(e.message))
      .finally(() => setActivating(false));
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

  if (loading) return <main id="sites" className="st-page"><p className="st-loading">Загрузка...</p></main>;

  const metaEntries = Object.entries(site?.meta ?? {});
  const station = site ? stations[site.name] : undefined;
  const activeName = switchedTo ?? active?.station ?? null;
  const isActive = !!site && site.name === activeName;
  // ничего не выводим, когда станция не годна: нет claude-opus-5, мало денег или не отвечает
  const showClaude = isActive || !!station?.can_activate;

  return (
    <main id="sites" className="st-page">
      <div className="st-toolbar">
        <SiteSelect
          className="st-site-select"
          sites={sites}
          value={selectedId}
          onChange={(s) => { setSelectedId(s.id); setEditing(false); setClaudeMsg(null); }}
        />
        {adding ? (
          <>
            <input
              autoFocus
              className="st-new-site-input"
              value={newSiteName}
              onChange={(e) => setNewSiteName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") handleAddSite();
                if (e.key === "Escape") { setAdding(false); setNewSiteName(""); }
              }}
              placeholder="Новый сайт"
            />
            <button className="st-btn-add-site" onClick={handleAddSite}>Добавить</button>
            <button className="st-btn-add-cancel" onClick={() => { setAdding(false); setNewSiteName(""); }}>Отмена</button>
          </>
        ) : (
          <button
            className="st-btn-plus"
            onClick={() => setAdding(true)}
            title="Добавить сайт"
          >
            +
          </button>
        )}
      </div>

      {error && <p className="st-error">{error}</p>}

      {!site ? (
        <p className="st-empty">Сайтов пока нет.</p>
      ) : (
        <div className="st-card">
          <div className="st-header">
            {editing ? (
              <input
                className="st-name-input"
                value={draftName}
                onChange={(e) => setDraftName(e.target.value)}
              />
            ) : (
              <>
                <h2 className="st-title">{site.name}</h2>
                <a
                  className="st-site-link"
                  href={`https://${site.name}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  title={`Открыть ${site.name}`}
                >
                  ↗
                </a>
              </>
            )}
            <button
              className="st-btn-edit"
              onClick={editing ? () => setEditing(false) : startEdit}
              title={editing ? "Отменить" : "Редактировать название и meta"}
            >
              {editing ? "Отмена" : "✎ Редактировать"}
            </button>
          </div>

          {siteStats && (
            <div className="st-stats">
              <StatCell label="Аккаунтов всего" value={String(siteStats.total_count)} accent />
              <StatCell label="GitHub" value={String(siteStats.github_count)} />
              <StatCell label="Почтовых" value={String(siteStats.email_count)} />
              <StatCell label="Баланс всего" value={fmt(siteStats.total_balance)} accent />
              <StatCell label="Баланс GitHub" value={fmt(siteStats.github_balance)} />
              <StatCell label="Баланс почтовых" value={fmt(siteStats.email_balance)} />
              {showClaude && (
                <div className="st-claude" id="st-claude" title={
                  isActive ? "Claude Code работает на этой станции" : "переключить сессию на эту станцию"
                }>
                  <ClaudeIcon
                    className={isActive ? "st-claude-icon st-claude-icon--active" : "st-claude-icon st-claude-icon--idle"}
                  />
                  {isActive ? (
                    <span className="st-claude-state">Активен</span>
                  ) : (
                    <button className="st-btn-activate" onClick={handleActivate} disabled={activating}>
                      {activating ? "Переключаю..." : "Активировать"}
                    </button>
                  )}
                  {claudeMsg && <span className="st-claude-msg">{claudeMsg}</span>}
                </div>
              )}
            </div>
          )}

          <div className="st-rules">
            <div className="st-rules-label">
              Правила получения кода
            </div>
            {editing ? (
              <div className="st-rules-form">
                <input
                  className="st-input-subject"
                  value={draftSubject}
                  onChange={(e) => setDraftSubject(e.target.value)}
                  placeholder="тема письма"
                />
                <input
                  className="st-input-anchor"
                  value={draftAnchor}
                  onChange={(e) => setDraftAnchor(e.target.value)}
                  placeholder="якорь"
                />
                <input
                  className="st-input-length"
                  value={draftDigits}
                  onChange={(e) => setDraftDigits(e.target.value)}
                  placeholder="символов в коде"
                  inputMode="numeric"
                />
                <select className="st-select-format" value={draftFormat} onChange={(e) => setDraftFormat(e.target.value)}>
                  <option value="digits">только цифры</option>
                  <option value="alnum">цифры и буквы</option>
                </select>
              </div>
            ) : (
              <table className="st-rules-table">
                <tbody className="st-rules-body">
                  <tr className="st-rules-row">
                    <td className="st-rules-key">
                      тема письма
                    </td>
                    <td className="st-rules-value">
                      {site.mail_subject || <span className="st-dash">—</span>}
                    </td>
                  </tr>
                  <tr className="st-rules-row">
                    <td className="st-rules-key">
                      якорь
                    </td>
                    <td className="st-rules-value">
                      {site.code_anchor || <span className="st-dash">—</span>}
                    </td>
                  </tr>
                  <tr className="st-rules-row">
                    <td className="st-rules-key">
                      символов в коде
                    </td>
                    <td className="st-rules-value">
                      {site.code_length ?? <span className="st-dash">—</span>}
                    </td>
                  </tr>
                  <tr className="st-rules-row">
                    <td className="st-rules-key">
                      формат кода
                    </td>
                    <td className="st-rules-value">
                      {site.code_format === "alnum" ? "цифры и буквы" : "только цифры"}
                    </td>
                  </tr>
                </tbody>
              </table>
            )}
          </div>

          {!editing && (
            <div className="st-key-add">
              {addingKey ? (
                <div className="st-key-form">
                  <input
                    autoFocus
                    className="st-key-name-input"
                    value={keyName}
                    onChange={(e) => setKeyName(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") handleAddKey();
                      if (e.key === "Escape") cancelAddKey();
                    }}
                    placeholder="ключ"
                  />
                  <input
                    className="st-key-value-input"
                    value={keyValue}
                    onChange={(e) => setKeyValue(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") handleAddKey();
                      if (e.key === "Escape") cancelAddKey();
                    }}
                    placeholder="значение"
                  />
                  <button className="st-btn-key-save" onClick={handleAddKey}>Сохранить</button>
                  <button className="st-btn-key-cancel" onClick={cancelAddKey}>Отмена</button>
                </div>
              ) : (
                <button className="st-btn-key-add" onClick={() => setAddingKey(true)}>
                  + Добавить ключ
                </button>
              )}
            </div>
          )}

          {editing ? (
            <>
              <textarea
                className="st-meta-editor"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                spellCheck={false}
              />
              <div className="st-meta-editor-actions">
                <button className="st-btn-save-meta" onClick={handleSaveMeta}>Сохранить</button>
              </div>
            </>
          ) : metaEntries.length === 0 ? (
            <p className="st-meta-empty">meta пуст.</p>
          ) : (
            <table className="st-meta-table">
              <tbody className="st-meta-body">
                {metaEntries.map(([key, value]) => (
                  <tr key={key} className="st-meta-row">
                    <td className="st-meta-key">
                      {key}
                    </td>
                    <td className="st-meta-value">
                      <MetaValue value={value} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <div className="st-card-footer">
            <button
              className="st-btn-delete"
              onClick={handleDeleteSite}
              title="Удалить сайт целиком"
            >
              Удалить сайт
            </button>
          </div>
        </div>
      )}
    </main>
  );
}
