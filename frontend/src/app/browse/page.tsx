"use client";

import { useEffect, useState } from "react";
import { SITE_KEY, AFF_KEY, pickStickyId, rememberId } from "../lib/sticky";

// одна форма под обе вкладки: github-поля приходят с /api/github/accounts/browse,
// password и secret — с /api/email/accounts/browse
type Account = {
  id: number;
  login?: string | null;
  pass_github?: string | null;
  email?: string | null;
  pass_email?: string | null;
  password?: string | null;
  restore_email?: string | null;
  restore_pass?: string | null;
  secret?: string | null;
};

type Tab = "github" | "email";

type BrowseData = {
  account: Account;
  total: number;
  offset: number;
};

type Site = {
  id: number;
  name: string;
  mail_subject: string | null;
  code_anchor: string | null;
  code_length: number | null;
  code_format: string | null;
};

type SiteAccountOption = {
  id: number;
  login: string | null;
  email: string | null;
  aff: string | null;
};

function CopyBtn({ value, onCopy }: { value?: string | null; onCopy?: (value: string) => void }) {
  const [copied, setCopied] = useState(false);

  if (!value) return null;

  const handleCopy = () => {
    onCopy?.(value);
    navigator.clipboard.writeText(value).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    });
  };

  return (
    <button
      onClick={handleCopy}
      title="Скопировать"
      style={{
        marginLeft: 8,
        cursor: "pointer",
        background: "none",
        border: "none",
        fontSize: "1rem",
        color: copied ? "green" : "var(--foreground)",
      }}
    >
      {copied ? "\u2713" : "\u2398"}
    </button>
  );
}

function Field({ label, value, onCopy }: { label: string; value?: string | null; onCopy?: (value: string) => void }) {
  return (
    <td style={{ padding: "6px 16px 6px 0" }}>
      <span style={{ opacity: 0.6, fontSize: "0.85rem" }}>{label}</span>
      <br />
      <span>{value ?? "—"}</span>
      <CopyBtn value={value} onCopy={onCopy} />
    </td>
  );
}

function AccInput({
  placeholder,
  value,
  onChange,
  flex,
}: {
  placeholder: string;
  value: string;
  onChange: (v: string) => void;
  flex: number;
}) {
  return (
    <div style={{ position: "relative", display: "flex", flex }}>
      <input
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        style={{ flex: 1, paddingRight: 26 }}
      />
      {value && (
        <button
          onClick={() => onChange("")}
          title="Очистить"
          style={{
            position: "absolute",
            right: 6,
            top: "50%",
            transform: "translateY(-50%)",
            background: "none",
            border: "none",
            padding: 0,
            lineHeight: 1,
            fontSize: "0.9rem",
            cursor: "pointer",
            color: "var(--text-muted)",
          }}
        >
          {"\u2715"}
        </button>
      )}
    </div>
  );
}

function CheckMailBtn({
  email,
  type,
  subject,
  codeAnchor,
  codeLength,
  codeFormat,
  disabled,
}: {
  email?: string | null;
  type: "outlook" | "rambler";
  subject?: string | null;
  codeAnchor?: string | null;
  codeLength?: number | null;
  codeFormat?: string | null;
  disabled?: boolean;
}) {
  const [status, setStatus] = useState<"idle" | "loading" | "ok" | "error">("idle");
  const [msg, setMsg] = useState("");
  const [code, setCode] = useState<string | null>(null);

  if (!email) return null;

  const handleCheck = (e: React.MouseEvent<HTMLButtonElement>) => {
    const emailFromAttr = e.currentTarget.getAttribute("data-email");
    setStatus("loading");
    setCode(null);
    fetch("/api/mail/check-mailbox", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: emailFromAttr,
        type,
        subject: subject || null,
        code_anchor: codeAnchor || null,
        code_length: codeLength ?? null,
        code_format: codeFormat || null,
      }),
    })
      .then((res) => res.json())
      .then((d: { status: string; message: string; code?: string }) => {
        setStatus(d.status === "ok" ? "ok" : "error");
        setCode(d.code || null);
        setMsg(d.code ? `Код: ${d.code}` : d.message);
      })
      .catch((err: Error) => { setStatus("error"); setMsg(err.message); });
  };

  return (
    <td style={{ padding: "6px 16px 6px 0", verticalAlign: "bottom" }}>
      <button
        onClick={handleCheck}
        disabled={disabled || status === "loading"}
        data-email={email}
        title={disabled ? "У сайта не задана тема письма" : undefined}
        style={{ cursor: "pointer", fontSize: "0.85rem", minWidth: 150, whiteSpace: "nowrap" }}
      >
        {status === "loading" ? "..." : "Проверить ящик"}
      </button>
      {status !== "idle" && status !== "loading" && (
        <div style={{ fontSize: "0.75rem", marginTop: 4, color: status === "ok" ? "green" : "crimson" }}>
          {msg}
          {code && <CopyBtn value={code} />}
        </div>
      )}
    </td>
  );
}

function ErrorBtn({
  value,
  field,
  action,
  endpoint = "/api/github/accounts/set-error-status",
  onMarked,
}: {
  value?: string | null;
  field: "email" | "restore_email";
  action: string;
  endpoint?: string;
  onMarked: () => void;
}) {
  const [status, setStatus] = useState<"idle" | "loading" | "done" | "error">("idle");

  const handleClick = (e: React.MouseEvent) => {
    e.preventDefault();
    if (!value) return;
    setStatus("loading");
    fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [field]: value, action }),
    })
      .then((res) => {
        if (!res.ok) throw new Error();
        setStatus("done");
        onMarked();
      })
      .catch(() => setStatus("error"));
  };

  return (
    <a
      href="#"
      onClick={handleClick}
      style={{
        color: status === "done" ? "green" : status === "error" ? "crimson" : "var(--foreground)",
        textDecoration: "underline",
        cursor: value ? "pointer" : "not-allowed",
        opacity: value ? 1 : 0.4,
      }}
    >
      {status === "loading" ? "..." : status === "done" ? `✓ ${action}` : status === "error" ? `✕ ${action}` : action}
    </a>
  );
}

export default function BrowsePage() {
  const [tab, setTab] = useState<Tab>("github");
  const [data, setData] = useState<BrowseData | null>(null);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [jumpId, setJumpId] = useState("");
  const [jumpError, setJumpError] = useState<string | null>(null);
  const [jumpFading, setJumpFading] = useState(false);
  const [cursorMode, setCursorMode] = useState(false);
  const [sites, setSites] = useState<Site[]>([]);
  const [selectedSite, setSelectedSite] = useState("");
  const [selectedSiteId, setSelectedSiteId] = useState<number>(0);
  const [newSiteName, setNewSiteName] = useState("");
  const [siteAccounts, setSiteAccounts] = useState<SiteAccountOption[]>([]);
  const [selectedAffId, setSelectedAffId] = useState<number>(0);
  const [acc, setAcc] = useState({ login: "", email: "", token: "", balance: "", aff: "" });
  const [smartLink, setSmartLink] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/sites")
      .then((res) => res.json())
      .then((d: Site[]) => {
        setSites(d);
        const site = d.find((s) => s.id === pickStickyId(d, SITE_KEY));
        if (site) { setSelectedSite(site.name); setSelectedSiteId(site.id); }
      })
      .catch(() => {});
  }, []);

  useEffect(() => { rememberId(SITE_KEY, selectedSiteId); }, [selectedSiteId]);
  useEffect(() => { rememberId(AFF_KEY, selectedAffId); }, [selectedAffId]);

  useEffect(() => {
    if (!selectedSiteId) return;
    const controller = new AbortController();
    fetch(`/api/site-accounts?site_id=${selectedSiteId}`, { signal: controller.signal })
      .then((res) => res.json())
      .then((d: SiteAccountOption[]) => {
        setSiteAccounts(d);
        setSelectedAffId(pickStickyId(d, AFF_KEY));
      })
      .catch(() => {});
    return () => controller.abort();
  }, [selectedSiteId]);

  const handleAddSite = () => {
    const name = newSiteName.trim();
    if (!name) return;
    fetch("/api/sites", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    })
      .then((res) => res.json())
      .then((site: Site) => {
        setSites((prev) => [...prev, site].sort((a, b) => a.name.localeCompare(b.name)));
        setSelectedSite(site.name);
        setSelectedSiteId(site.id);
        setNewSiteName("");
      })
      .catch(() => {});
  };

  const browseBase = tab === "github" ? "/api/github/accounts/browse" : "/api/email/accounts/browse";

  const switchTab = (next: Tab) => {
    if (next === tab) return;
    setTab(next);
    setData(null);
    setOffset(0);
    setCursorMode(false);
    setJumpError(null);
    setSaveError(null);
    setAcc({ login: "", email: "", token: "", balance: "", aff: "" });
  };

  const handleSaveAccount = () => {
    if (!data || !selectedSiteId) return;
    setSaveError(null);
    const isGithub = tab === "github";
    const url = isGithub ? "/api/site-accounts" : "/api/site-accounts-custom";
    const body = {
      site_id: selectedSiteId,
      ...(isGithub ? { github_id: data.account.id, smart_link: smartLink } : { email_id: data.account.id }),
      login: acc.login || null,
      email: acc.email || null,
      token: acc.token || null,
      balance: parseFloat(acc.balance.replace(",", ".")) || 0,
      aff: acc.aff || null,
    };
    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(async (res) => {
        if (!res.ok) {
          const body = await res.json().catch(() => null);
          throw new Error(body?.detail || "Ошибка сохранения");
        }
        return res.json();
      })
      .then(() => {
        setAcc({ login: "", email: "", token: "", balance: "", aff: "" });
        // шаг по id, а не по offset: сохранённый аккаунт выпал из выборки по сайту,
        // и offset+1 перескочил бы через следующий
        stepByCursor("after");
      })
      .catch((e: Error) => setSaveError(e.message));
  };

  useEffect(() => {
    const controller = new AbortController();
    const siteParam = selectedSiteId ? `&site_id=${selectedSiteId}` : "";
    fetch(`${browseBase}?offset=${offset}${siteParam}`, { signal: controller.signal })
      .then((res) => {
        if (!res.ok) throw new Error(`Backend: ${res.status}`);
        return res.json();
      })
      .then((d: BrowseData) => {
        setData(d);
        setJumpId(String(d.account.id));
        setJumpError(null);
        setCursorMode(false);
      })
      .catch((e: Error) => {
        if (e.name !== "AbortError") setError(e.message);
      });
    return () => controller.abort();
  }, [offset, selectedSiteId, browseBase]);

  const handleJump = () => {
    const id = parseInt(jumpId, 10);
    if (!id || id < 1) return;
    setJumpError(null);
    fetch(`${browseBase}?from_id=${id}&site_id=${selectedSiteId}`)
      .then((res) => {
        if (res.status === 404) throw new Error("Записей больше нет");
        if (!res.ok) throw new Error(`Backend: ${res.status}`);
        return res.json();
      })
      .then((d: BrowseData) => {
        setData((prev) => prev ? { ...prev, account: d.account } : prev);
        setJumpId(String(d.account.id));
        if (d.account.id !== id) {
          setJumpError(`ID ${id} не подходит по фильтру, показан ${d.account.id}`);
          setJumpFading(false);
          setTimeout(() => setJumpFading(true), 4000);
          setTimeout(() => { setJumpError(null); setJumpFading(false); }, 5000);
        }
        setCursorMode(true);
      })
      .catch((e: Error) => setJumpError(e.message));
  };

  const stepByCursor = (dir: "after" | "before") => {
    if (!data) return;
    const param = dir === "after" ? `after_id=${data.account.id}` : `before_id=${data.account.id}`;
    fetch(`${browseBase}?${param}&site_id=${selectedSiteId}`)
      .then((res) => {
        if (!res.ok) throw new Error(`Backend: ${res.status}`);
        return res.json();
      })
      .then((d: BrowseData) => {
        setData((prev) => prev ? { ...prev, account: d.account } : prev);
        setJumpId(String(d.account.id));
        setCursorMode(true);
      })
      .catch(() => {});
  };

  const handleNext = () => {
    if (!data) return;
    if (cursorMode) {
      stepByCursor("after");
    } else {
      setOffset((o) => o + 1);
    }
  };

  const handlePrev = () => {
    if (!data) return;
    if (cursorMode) {
      stepByCursor("before");
    } else {
      setOffset((o) => o - 1);
    }
  };

  const tabBar = (
    <div style={{ display: "flex", gap: 4, marginBottom: 20, borderBottom: "1px solid var(--border)" }}>
      {([["github", "GitHub"], ["email", "Почта"]] as [Tab, string][]).map(([key, label]) => (
        <button
          key={key}
          onClick={() => switchTab(key)}
          style={{
            border: "none",
            borderBottom: tab === key ? "2px solid var(--accent)" : "2px solid transparent",
            background: "none",
            padding: "8px 16px",
            fontSize: "0.95rem",
            fontWeight: tab === key ? 600 : 400,
            color: tab === key ? "var(--accent)" : "var(--text-muted)",
            cursor: "pointer",
          }}
        >
          {label}
        </button>
      ))}
    </div>
  );

  if (error) return <main style={{ padding: "2rem" }}>{tabBar}<p style={{ color: "crimson" }}>Ошибка: {error}</p></main>;
  if (!data) return <main style={{ padding: "2rem" }}>{tabBar}<p>Загрузка...</p></main>;

  const { account, total } = data;

  const siteRules = sites.find((s) => s.id === selectedSiteId) ?? null;
  const rawAff = siteAccounts.find((a) => a.id === selectedAffId)?.aff?.trim() || "";
  const affUrl = /^https?:\/\//i.test(rawAff) ? rawAff : rawAff ? `https://${rawAff}` : "";

  return (
    <main style={{ padding: "2rem" }}>
      {tabBar}
      <div style={{ display: "flex", alignItems: "center", gap: 16, marginBottom: 16 }}>
        <select
          value={selectedSite}
          onChange={(e) => {
            const site = sites.find((s) => s.name === e.target.value);
            setSelectedSite(e.target.value);
            if (site) setSelectedSiteId(site.id);
          }}
        >
          {sites.map((s) => (
            <option key={s.id} value={s.name}>{s.name}</option>
          ))}
        </select>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <input
            type="text"
            value={newSiteName}
            onChange={(e) => setNewSiteName(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") handleAddSite(); }}
            placeholder="Новый сайт"
          />
          <button onClick={handleAddSite}>Добавить</button>
          {selectedSite && (
            <a
              href={`https://${selectedSite}`}
              target="_blank"
              rel="noopener noreferrer"
              style={{ marginLeft: 4, fontSize: "1.1rem" }}
              title={`Открыть ${selectedSite}`}
            >
              ↗
            </a>
          )}
        </div>
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 16 }}>
        <span style={{ fontSize: "0.85rem", color: "var(--text-muted)" }}>Реферал</span>
        <select
          value={selectedAffId}
          onChange={(e) => setSelectedAffId(parseInt(e.target.value, 10))}
          disabled={!siteAccounts.length}
        >
          {siteAccounts.length ? (
            siteAccounts.map((a) => (
              <option key={a.id} value={a.id}>
                {a.login || a.email || `id=${a.id}`}
              </option>
            ))
          ) : (
            <option value={0}>— нет аккаунтов —</option>
          )}
        </select>
        {affUrl && (
          <a
            href={affUrl}
            target="_blank"
            rel="noopener noreferrer"
            style={{ marginLeft: 4, fontSize: "1.1rem" }}
            title={`Перейти: ${affUrl}`}
          >
            ↗
          </a>
        )}
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
        <h2 style={{ margin: 0 }}>Запись ID</h2>
        <input
          type="number"
          value={jumpId}
          onChange={(e) => setJumpId(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") handleJump(); }}
          style={{ width: 80, fontSize: "1.1rem", fontWeight: "bold" }}
          min={1}
        />
        <button
          onClick={handleJump}
          title="Перейти"
          style={{ border: "none", background: "none", color: "var(--accent)", fontSize: "1.2rem", padding: 4 }}
        >
          ➜
        </button>
      </div>
      {jumpError && <p style={{ color: "#ff6b6b", margin: "4px 0", transition: "opacity 1s", opacity: jumpFading ? 0 : 1 }}>{jumpError}</p>}
      <p style={{ color: "var(--text-muted)", marginBottom: "1rem", fontSize: "0.85rem" }}>
        {offset + 1} из {total} ({tab === "github" ? "active, @hotmail.com" : "active"})
      </p>

      {tab === "email" && (
        <div style={{ marginBottom: "1rem" }}>
          <h3 style={{ margin: "0 0 6px" }}>Правила получения кода</h3>
          <div style={{ display: "flex", gap: 24, fontSize: "0.85rem" }}>
            <span>
              <span style={{ color: "var(--text-muted)" }}>тема письма: </span>
              {siteRules?.mail_subject || <span style={{ opacity: 0.5 }}>не задана</span>}
            </span>
            <span>
              <span style={{ color: "var(--text-muted)" }}>якорь: </span>
              {siteRules?.code_anchor || <span style={{ opacity: 0.5 }}>не задан</span>}
            </span>
            <span>
              <span style={{ color: "var(--text-muted)" }}>символов в коде: </span>
              {siteRules?.code_length ?? <span style={{ opacity: 0.5 }}>не задано</span>}
            </span>
            <span>
              <span style={{ color: "var(--text-muted)" }}>формат: </span>
              {siteRules?.code_format === "alnum" ? "цифры и буквы" : "только цифры"}
            </span>
          </div>
        </div>
      )}

      <div style={{ background: "var(--card-bg)", borderRadius: 8, boxShadow: "var(--card-shadow)", padding: 16 }}>
      {tab === "github" ? (
      <table style={{ width: "auto" }}>
        <tbody>
          <tr>
            <Field label="login" value={account.login} onCopy={(v) => setAcc((a) => ({ ...a, login: v }))} />
            <Field label="pass_github" value={account.pass_github} />
          </tr>
          <tr>
            <Field label="email" value={account.email} onCopy={(v) => setAcc((a) => ({ ...a, email: v }))} />
            <Field label="pass_email" value={account.pass_email} />
            <CheckMailBtn key={account.login} email={account.email} type="outlook" />
          </tr>
          <tr>
            <Field label="restore_email" value={account.restore_email} />
            <Field label="restore_pass" value={account.restore_pass} />
            <CheckMailBtn key={account.login} email={account.restore_email} type="rambler" />
          </tr>
        </tbody>
      </table>
      ) : (
      <table style={{ width: "auto" }}>
        <tbody>
          <tr>
            <Field label="email" value={account.email} onCopy={(v) => setAcc((a) => ({ ...a, email: v }))} />
            <Field label="password" value={account.password} />
            <CheckMailBtn
              key={`outlook-${account.id}`}
              email={account.email}
              type="outlook"
              subject={siteRules?.mail_subject}
              codeAnchor={siteRules?.code_anchor}
              codeLength={siteRules?.code_length}
              codeFormat={siteRules?.code_format}
              disabled={!siteRules?.mail_subject}
            />
          </tr>
          <tr>
            <Field label="restore_email" value={account.restore_email} />
            <Field label="restore_pass" value={account.restore_pass} />
            <CheckMailBtn
              key={`rambler-${account.id}`}
              email={account.restore_email}
              type="rambler"
              subject={siteRules?.mail_subject}
              codeAnchor={siteRules?.code_anchor}
              codeLength={siteRules?.code_length}
              codeFormat={siteRules?.code_format}
              disabled={!siteRules?.mail_subject}
            />
          </tr>
          <tr>
            <Field label="secret" value={account.secret} />
          </tr>
        </tbody>
      </table>
      )}
      </div>

      <div style={{ marginTop: "1.5rem", display: "flex", gap: 12 }}>
        <button onClick={handlePrev} disabled={!cursorMode && offset === 0}>
          ← Назад
        </button>
        <button onClick={handleNext} disabled={!cursorMode && offset >= total - 1}>
          Вперёд →
        </button>
      </div>

      <div style={{ marginTop: "2rem" }}>
        <h3>Уровень брака</h3>
        <div style={{ display: "flex", gap: 16, marginTop: 8 }}>
          {tab === "github" ? (
            <>
              <ErrorBtn key={`rambler-${account.login}`} value={account.restore_email} field="restore_email" action="Bad Rambler Email" onMarked={() => stepByCursor("after")} />
              <ErrorBtn key={`badgithub-${account.login}`} value={account.email} field="email" action="Bad Github Account" onMarked={() => stepByCursor("after")} />
              <ErrorBtn key={`suspended-${account.login}`} value={account.email} field="email" action="Suspended Github" onMarked={() => stepByCursor("after")} />
              <ErrorBtn key={`flag-${account.login}`} value={account.email} field="email" action="Flag Site" onMarked={() => stepByCursor("after")} />
            </>
          ) : (
            <>
              {["Bad Email", "Suspended Email", "Flag Site"].map((action) => (
                <ErrorBtn
                  key={`${action}-${account.id}`}
                  value={account.email}
                  field="email"
                  action={action}
                  endpoint="/api/email/accounts/set-error-status"
                  onMarked={() => stepByCursor("after")}
                />
              ))}
            </>
          )}
        </div>
      </div>

      <div style={{ marginTop: "2rem", background: "var(--card-bg)", borderRadius: 8, boxShadow: "var(--card-shadow)", padding: 16 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <h3>Аккаунт на сайте</h3>
          {tab === "github" && (
          <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: "0.8rem", color: "var(--text-muted)", cursor: "pointer" }}>
            <input
              type="checkbox"
              checked={smartLink}
              onChange={(e) => setSmartLink(e.target.checked)}
              style={{ width: 14, height: 14, accentColor: "var(--accent)", padding: 0 }}
            />
            Умная привязка
          </label>
          )}
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
          <AccInput
            placeholder="login_site"
            value={acc.login}
            onChange={(v) => setAcc((a) => ({ ...a, login: v }))}
            flex={1}
          />
          <AccInput
            placeholder="email_site"
            value={acc.email}
            onChange={(v) => setAcc((a) => ({ ...a, email: v }))}
            flex={1}
          />
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
          <AccInput
            placeholder="token"
            value={acc.token}
            onChange={(v) => setAcc((a) => ({ ...a, token: v }))}
            flex={4}
          />
          <AccInput
            placeholder="balance"
            value={acc.balance}
            onChange={(v) => setAcc((a) => ({ ...a, balance: v }))}
            flex={1}
          />
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 8, alignItems: "center" }}>
          <AccInput
            placeholder="aff (URL)"
            value={acc.aff}
            onChange={(v) => setAcc((a) => ({ ...a, aff: v }))}
            flex={1}
          />
          {acc.aff && (
            <a href={acc.aff} target="_blank" rel="noopener noreferrer" style={{ fontSize: "1.1rem" }} title="Открыть aff">↗</a>
          )}
        </div>
        <div style={{ marginTop: 12 }}>
          <button onClick={handleSaveAccount}>Сохранить</button>
        </div>
        {saveError && <p style={{ color: "#ff6b6b", marginTop: 8, fontSize: "0.8rem" }}>{saveError}</p>}
      </div>
    </main>
  );
}
