"use client";

import { useCallback, useEffect, useState } from "react";
import SiteSelect from "../components/SiteSelect";
import { SITE_KEY, AFF_KEY, TAB_KEY, pickStickyId, rememberId } from "../lib/sticky";

// одна форма под обе вкладки: github-поля приходят с /api/github/accounts/browse,
// password — с /api/email/accounts/browse
type Account = {
  id: number;
  login?: string | null;
  pass_github?: string | null;
  email?: string | null;
  pass_email?: string | null;
  password?: string | null;
  restore_email?: string | null;
  restore_pass?: string | null;
};

type Tab = "github" | "email" | "gmail";

type BrowseData = {
  account: Account;
  total: number;
  offset: number;
};

type Site = {
  id: number;
  name: string;
  cnt: number | null;
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
      className={`br-copy-btn${copied ? " br-copy-btn-copied" : ""}`}
    >
      {copied ? "\u2713" : "\u2398"}
    </button>
  );
}

function Field({ label, value, onCopy }: { label: string; value?: string | null; onCopy?: (value: string) => void }) {
  return (
    <td className="br-field">
      <span className="br-field-label">{label}</span>
      <br />
      <span className="br-field-value">{value ?? "—"}</span>
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
    <div className="br-acc-input" style={{ flex }}>
      <input
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="br-acc-input-field"
      />
      {value && (
        <button
          onClick={() => onChange("")}
          title="Очистить"
          className="br-acc-clear"
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
    <td className="br-check-cell">
      <button
        onClick={handleCheck}
        disabled={disabled || status === "loading"}
        data-email={email}
        title={disabled ? "У сайта не задана тема письма" : undefined}
        className="br-check-btn"
      >
        {status === "loading" ? "..." : "Проверить ящик"}
      </button>
      {status !== "idle" && status !== "loading" && (
        <div className={`br-check-msg ${status === "ok" ? "br-check-msg-ok" : "br-check-msg-error"}`}>
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

  const stateClass = status === "done" ? " br-error-link-done" : status === "error" ? " br-error-link-error" : "";

  return (
    <a
      href="#"
      onClick={handleClick}
      className={`br-error-link${stateClass}${value ? "" : " br-error-link-off"}`}
    >
      {status === "loading" ? "..." : status === "done" ? `✓ ${action}` : status === "error" ? `✕ ${action}` : action}
    </a>
  );
}

export default function BrowsePage() {
  const [tab, setTab] = useState<Tab>("email");
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
  const [acc, setAcc] = useState({ login: "", email: "", password: "", token: "", balance: "", aff: "", accessToken: "", panelId: "" });
  const [gen, setGen] = useState({ login: "", password: "" });
  const [smartLink, setSmartLink] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  // вкладку восстанавливаем в колбэке уже идущего запроса сайтов: на сервере localStorage
  // нет, а инициализатор useState дал бы расхождение при гидратации, setState прямо в теле
  // эффекта — ошибку react-hooks/set-state-in-effect
  useEffect(() => {
    fetch("/api/sites")
      .then((res) => res.json())
      .then((d: Site[]) => {
        const storedTab = localStorage.getItem(TAB_KEY);
        if (storedTab === "email" || storedTab === "github" || storedTab === "gmail") setTab(storedTab);
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
  // вкладки «Почта» и «Gmail» — одна и та же форма над main_email, отличаются только
  // доменом: gmail=1 отдаёт ровно gmail-ящики, без параметра они наоборот отсекаются
  const gmailParam = tab === "gmail" ? "&gmail=1" : "";
  const isEmailTab = tab === "email" || tab === "gmail";

  const switchTab = (next: Tab) => {
    if (next === tab) return;
    setTab(next);
    localStorage.setItem(TAB_KEY, next);
    setData(null);
    setOffset(0);
    setCursorMode(false);
    setJumpError(null);
    setSaveError(null);
    setAcc({ login: "", email: "", password: "", token: "", balance: "", aff: "", accessToken: "", panelId: "" });
  };

  const handleSaveAccount = () => {
    if (!data || !selectedSiteId) return;
    setSaveError(null);
    const isGithub = tab === "github";
    const url = isGithub ? "/api/site-accounts" : "/api/site-accounts-custom";
    const body = {
      site_id: selectedSiteId,
      ...(isGithub
        ? { github_id: data.account.id, smart_link: smartLink }
        : { email_id: data.account.id, password: acc.password || null }),
      login: acc.login || null,
      email: acc.email || null,
      token: acc.token || null,
      balance: parseFloat(acc.balance.replace(",", ".")) || 0,
      aff: acc.aff || null,
      access_token: acc.accessToken || null,
      panel_id: parseInt(acc.panelId, 10) || null,
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
        setAcc({ login: "", email: "", password: "", token: "", balance: "", aff: "", accessToken: "", panelId: "" });
        // шаг по id, а не по offset: сохранённый аккаунт выпал из выборки по сайту,
        // и offset+1 перескочил бы через следующий
        stepByCursor("after");
      })
      .catch((e: Error) => setSaveError(e.message));
  };

  // на вкладке GitHub аккаунт на сайте заводится под логином и ящиком самой записи.
  // Зовётся из колбэков fetch-а, а не из эффекта: setAcc прямо в теле эффекта даёт
  // react-hooks/set-state-in-effect
  const fillFromRecord = useCallback((a: Account) => {
    setAcc((prev) => ({ ...prev, login: a.login ?? "", email: a.email ?? "" }));
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const siteParam = selectedSiteId ? `&site_id=${selectedSiteId}` : "";
    fetch(`${browseBase}?offset=${offset}${siteParam}${gmailParam}`, { signal: controller.signal })
      .then((res) => {
        if (!res.ok) throw new Error(`Backend: ${res.status}`);
        return res.json();
      })
      .then((d: BrowseData) => {
        setData(d);
        setJumpId(String(d.account.id));
        setJumpError(null);
        setCursorMode(false);
        if (!isEmailTab) fillFromRecord(d.account);
      })
      .catch((e: Error) => {
        if (e.name !== "AbortError") setError(e.message);
      });
    return () => controller.abort();
  }, [offset, selectedSiteId, browseBase, gmailParam, isEmailTab, fillFromRecord]);

  const generateCreds = useCallback((email?: string) => {
    fetch("/api/generate-credentials")
      .then((res) => res.json())
      .then((d: { login: string; password: string }) => {
        setGen(d);
        setAcc((a) => ({ ...a, login: d.login, password: d.password, ...(email !== undefined ? { email } : {}) }));
      })
      .catch(() => {});
  }, []);

  const browsedId = data?.account.id;
  const browsedEmail = data?.account.email ?? "";

  // на почтовых вкладках аккаунт на сайте регистрируется под свежей парой, а email
  // берётся у самого ящика — заполняем всё сразу, чтобы Босс только жал «Сохранить»
  useEffect(() => {
    if (!isEmailTab || !browsedId) return;
    generateCreds(browsedEmail);
  }, [isEmailTab, browsedId, browsedEmail, generateCreds]);

  const handleJump = () => {
    const id = parseInt(jumpId, 10);
    if (!id || id < 1) return;
    setJumpError(null);
    fetch(`${browseBase}?from_id=${id}&site_id=${selectedSiteId}${gmailParam}`)
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
        if (!isEmailTab) fillFromRecord(d.account);
      })
      .catch((e: Error) => setJumpError(e.message));
  };

  const stepByCursor = (dir: "after" | "before") => {
    if (!data) return;
    const param = dir === "after" ? `after_id=${data.account.id}` : `before_id=${data.account.id}`;
    fetch(`${browseBase}?${param}&site_id=${selectedSiteId}${gmailParam}`)
      .then((res) => {
        if (!res.ok) throw new Error(`Backend: ${res.status}`);
        return res.json();
      })
      .then((d: BrowseData) => {
        setData((prev) => prev ? { ...prev, account: d.account } : prev);
        setJumpId(String(d.account.id));
        setCursorMode(true);
        if (!isEmailTab) fillFromRecord(d.account);
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
    <div className="br-tabs" id="br-tabs">
      {([["email", "Почта"], ["github", "GitHub"], ["gmail", "Gmail"]] as [Tab, string][]).map(([key, label]) => (
        <button
          key={key}
          onClick={() => switchTab(key)}
          className={`br-tab br-tab-${key}${tab === key ? " br-tab-active" : ""}`}
        >
          {label}
        </button>
      ))}
    </div>
  );

  if (error) return <main className="br-page" id="browse">{tabBar}<p className="br-error">Ошибка: {error}</p></main>;
  if (!data) return <main className="br-page" id="browse">{tabBar}<p className="br-loading">Загрузка...</p></main>;

  const { account, total } = data;

  const siteRules = sites.find((s) => s.id === selectedSiteId) ?? null;
  const rawAff = siteAccounts.find((a) => a.id === selectedAffId)?.aff?.trim() || "";
  const affUrl = /^https?:\/\//i.test(rawAff) ? rawAff : rawAff ? `https://${rawAff}` : "";

  return (
    <main className="br-page" id="browse">
      {tabBar}
      <div className="br-toolbar">
        <SiteSelect
          className="br-site-select"
          id="br-site-select"
          sites={sites}
          value={selectedSiteId}
          onChange={(s) => { setSelectedSite(s.name); setSelectedSiteId(s.id); }}
        />
        <div className="br-site-add">
          <input
            className="br-site-new"
            id="br-site-new"
            type="text"
            value={newSiteName}
            onChange={(e) => setNewSiteName(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") handleAddSite(); }}
            placeholder="Новый сайт"
          />
          <button className="br-site-add-btn" onClick={handleAddSite}>Добавить</button>
          {selectedSite && (
            <a
              href={`https://${selectedSite}`}
              target="_blank"
              rel="noopener noreferrer"
              className="br-site-link"
              title={`Открыть ${selectedSite}`}
            >
              ↗
            </a>
          )}
        </div>
      </div>

      <div className="br-aff-row">
        <span className="br-aff-label">Реферал</span>
        <select
          className="br-aff-select"
          id="br-aff-select"
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
            className="br-aff-link"
            title={`Перейти: ${affUrl}`}
          >
            ↗
          </a>
        )}
      </div>

      <div className="br-jump">
        <h2 className="br-jump-title">Запись ID</h2>
        <input
          className="br-jump-input"
          id="br-jump-input"
          type="number"
          value={jumpId}
          onChange={(e) => setJumpId(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") handleJump(); }}
          min={1}
        />
        <button
          onClick={handleJump}
          title="Перейти"
          className="br-jump-go"
        >
          ➜
        </button>
      </div>
      {jumpError && <p className={`br-jump-error${jumpFading ? " br-jump-error-fading" : ""}`}>{jumpError}</p>}
      <p className="br-counter">
        {offset + 1} из {total} ({tab === "github" ? "active, @hotmail.com" : tab === "gmail" ? "active, только @gmail.com" : "active, без @gmail.com"})
      </p>

      {isEmailTab && (
        <div className="br-rules" id="br-rules">
          <h3 className="br-rules-title">Правила получения кода</h3>
          <div className="br-rules-list">
            <span className="br-rules-item">
              <span className="br-rules-label">тема письма: </span>
              {siteRules?.mail_subject || <span className="br-rules-empty">не задана</span>}
            </span>
            <span className="br-rules-item">
              <span className="br-rules-label">якорь: </span>
              {siteRules?.code_anchor || <span className="br-rules-empty">не задан</span>}
            </span>
            <span className="br-rules-item">
              <span className="br-rules-label">символов в коде: </span>
              {siteRules?.code_length ?? <span className="br-rules-empty">не задано</span>}
            </span>
            <span className="br-rules-item">
              <span className="br-rules-label">формат: </span>
              {siteRules?.code_format === "alnum" ? "цифры и буквы" : "только цифры"}
            </span>
          </div>
        </div>
      )}

      <div className="br-card" id="br-record">
      {tab === "github" ? (
      <table className="br-table br-table-github">
        <tbody className="br-table-body">
          <tr className="br-row">
            <Field label="login" value={account.login} onCopy={(v) => setAcc((a) => ({ ...a, login: v }))} />
            <Field label="pass_github" value={account.pass_github} />
          </tr>
          <tr className="br-row">
            <Field label="email" value={account.email} onCopy={(v) => setAcc((a) => ({ ...a, email: v }))} />
            <Field label="pass_email" value={account.pass_email} />
            <CheckMailBtn key={account.login} email={account.email} type="outlook" />
          </tr>
          <tr className="br-row">
            <Field label="restore_email" value={account.restore_email} />
            <Field label="restore_pass" value={account.restore_pass} />
            <CheckMailBtn key={account.login} email={account.restore_email} type="rambler" />
          </tr>
        </tbody>
      </table>
      ) : (
      <table className="br-table br-table-email">
        <tbody className="br-table-body">
          <tr className="br-row">
            <Field label="login" value={gen.login} />
            <Field label="password" value={gen.password} />
            <td className="br-gen-cell">
              <button
                onClick={() => generateCreds()}
                title="Сгенерировать заново"
                className="br-gen-refresh"
              >
                ↻
              </button>
            </td>
          </tr>
          <tr className="br-row">
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
          <tr className="br-row">
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
        </tbody>
      </table>
      )}
      </div>

      <div className="br-nav">
        <button className="br-nav-prev" onClick={handlePrev} disabled={!cursorMode && offset === 0}>
          ← Назад
        </button>
        <button className="br-nav-next" onClick={handleNext} disabled={!cursorMode && offset >= total - 1}>
          Вперёд →
        </button>
      </div>

      <div className="br-flaws" id="br-flaws">
        <h3 className="br-flaws-title">Уровень брака</h3>
        <div className="br-flaws-list">
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
          {/* имя ключа на сайте всегда одно и то же — копируем, чтобы не набирать руками */}
          <span className="br-mykey">
            My Key
            <CopyBtn value="My Key" />
          </span>
        </div>
      </div>

      <div className="br-card br-acc-card" id="br-acc">
        <div className="br-acc-head">
          <h3 className="br-acc-title">Аккаунт на сайте</h3>
          {tab === "github" && (
          <label className="br-smart-link">
            <input
              type="checkbox"
              checked={smartLink}
              onChange={(e) => setSmartLink(e.target.checked)}
              className="br-smart-link-box"
            />
            Умная привязка
          </label>
          )}
        </div>
        <div className="br-acc-row br-acc-row-first">
          <AccInput
            placeholder="login_site"
            value={acc.login}
            onChange={(v) => setAcc((a) => ({ ...a, login: v }))}
            flex={1}
          />
          {isEmailTab ? (
            <AccInput
              placeholder="password_site"
              value={acc.password}
              onChange={(v) => setAcc((a) => ({ ...a, password: v }))}
              flex={1}
            />
          ) : (
            <AccInput
              placeholder="email_site"
              value={acc.email}
              onChange={(v) => setAcc((a) => ({ ...a, email: v }))}
              flex={1}
            />
          )}
        </div>
        {isEmailTab && (
          <div className="br-acc-row br-acc-row-email">
            <AccInput
              placeholder="email_site"
              value={acc.email}
              onChange={(v) => setAcc((a) => ({ ...a, email: v }))}
              flex={1}
            />
          </div>
        )}
        <div className="br-acc-row br-acc-row-token">
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
        <div className="br-acc-row br-acc-row-aff">
          <AccInput
            placeholder="aff (URL)"
            value={acc.aff}
            onChange={(v) => setAcc((a) => ({ ...a, aff: v }))}
            flex={1}
          />
          {acc.aff && (
            <a href={acc.aff} target="_blank" rel="noopener noreferrer" className="br-acc-aff-link" title="Открыть aff">↗</a>
          )}
        </div>
        <div className="br-acc-row br-acc-row-panel">
          <AccInput
            placeholder="токен доступа"
            value={acc.accessToken}
            onChange={(v) => setAcc((a) => ({ ...a, accessToken: v }))}
            flex={4}
          />
          <AccInput
            placeholder="ID на сайте"
            value={acc.panelId}
            onChange={(v) => setAcc((a) => ({ ...a, panelId: v }))}
            flex={1}
          />
        </div>
        <div className="br-acc-save">
          <button className="br-btn-save" id="br-btn-save" onClick={handleSaveAccount}>Сохранить</button>
        </div>
        {saveError && <p className="br-save-error">{saveError}</p>}
      </div>
    </main>
  );
}
