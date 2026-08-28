"use client";

import { useCallback, useEffect, useState } from "react";
import CopyBtn from "./components/CopyBtn";
import LongText from "./components/LongText";
import SiteSelect from "./components/SiteSelect";
import { useActiveStation } from "./lib/activeStation";
import { TOPIC_CLAUDE, useLiveUpdate } from "./lib/liveUpdates";
import { SITE_KEY, pickStickyId, rememberId } from "./lib/sticky";

type Site = {
  id: number;
  name: string;
  cnt: number | null;
};

type SiteAccount = {
  id: number;
  login: string | null;
  email: string | null;
  password: string | null;
  token: string | null;
  balance: number;
  opus_5_req: number | null;
  day_work: number | null;
  pass_github: string | null;
  aff: string | null;
  access_token: string | null;
  panel_id: number | null;
  note: string | null;
  kind: "github" | "custom";
};

// В базе баланс лежит как отдали панели — до шести знаков; в таблице нужны два.
const fmtBalance = (n: number) => n.toFixed(2);

// Часы и минуты в десятичных долях суток глазами не читаются: «2.4 дня» это не
// 2 дня 4 часа, а 2 дня 10 часов. Поэтому дробь разворачивается в целые единицы.
const plural = (n: number, one: string, few: string, many: string) => {
  const mod100 = n % 100;
  if (mod100 >= 11 && mod100 <= 14) return many;
  const mod10 = n % 10;
  if (mod10 === 1) return one;
  if (mod10 >= 2 && mod10 <= 4) return few;
  return many;
};

const fmtDays = (n: number | null) => {
  if (n === null) return "—";
  const total = Math.round(n * 24 * 60);
  const days = Math.floor(total / 1440);
  const hours = Math.floor((total % 1440) / 60);
  const mins = total % 60;
  if (days) {
    const head = `${days} ${plural(days, "день", "дня", "дней")}`;
    return hours ? `${head} ${hours} ч` : head;
  }
  if (hours) return mins ? `${hours} ч ${mins} мин` : `${hours} ч`;
  return `${mins} мин`;
};

export default function Home() {
  const [sites, setSites] = useState<Site[]>([]);
  const [selectedSiteId, setSelectedSiteId] = useState<number>(0);
  const [accounts, setAccounts] = useState<SiteAccount[]>([]);
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editRow, setEditRow] = useState<SiteAccount | null>(null);
  const [editBalance, setEditBalance] = useState("");
  const active = useActiveStation();

  useEffect(() => {
    fetch("/api/sites")
      .then((res) => res.json())
      .then((d: Site[]) => {
        setSites(d);
        if (d.length) setSelectedSiteId(pickStickyId(d, SITE_KEY));
      })
      .catch(() => {});
  }, []);

  useEffect(() => { rememberId(SITE_KEY, selectedSiteId); }, [selectedSiteId]);

  const loadAccounts = useCallback(() => {
    if (!selectedSiteId) return;
    Promise.all([
      fetch(`/api/site-accounts?site_id=${selectedSiteId}`).then((r) => r.json()),
      fetch(`/api/site-accounts-custom?site_id=${selectedSiteId}`).then((r) => r.json()),
    ])
      .then(([gh, custom]: [SiteAccount[], SiteAccount[]]) =>
        setAccounts([
          ...gh.map((a) => ({ ...a, password: null, kind: "github" as const })),
          ...custom.map((a) => ({ ...a, pass_github: null, kind: "custom" as const })),
        ]),
      )
      .catch(() => {});
  }, [selectedSiteId]);

  useEffect(() => { loadAccounts(); }, [loadAccounts]);

  // крон-скрипт переписал балансы и метки в примечаниях — дёргаем таблицу
  useLiveUpdate(TOPIC_CLAUDE, loadAccounts);

  const rowKey = (acc: SiteAccount) => `${acc.kind}-${acc.id}`;
  // Логин аккаунта, на котором сидит сессия Claude Code, красится цветом краба:
  // Боссу надо видеть его в самой таблице, а не сверять с меню.
  const isActive = (acc: SiteAccount) =>
    active?.account_id === acc.id &&
    active?.table ===
      (acc.kind === "custom" ? "main_site_account_custom" : "main_site_account");
  const apiPath = (acc: SiteAccount) =>
    acc.kind === "custom" ? "/api/site-accounts-custom" : "/api/site-accounts";

  const handleDelete = (acc: SiteAccount) => {
    const query = acc.kind === "github" ? `?site_id=${selectedSiteId}` : "";
    fetch(`${apiPath(acc)}/${acc.id}${query}`, { method: "DELETE" })
      .then(() => setAccounts((prev) => prev.filter((a) => rowKey(a) !== rowKey(acc))))
      .catch(() => {});
  };

  const handleEdit = (acc: SiteAccount) => {
    setEditingKey(rowKey(acc));
    setEditRow({ ...acc });
    setEditBalance(fmtBalance(acc.balance));
  };

  const handleSave = () => {
    if (!editRow) return;
    // баланс держим строкой всё время правки: разбор на каждом нажатии съедал
    // разделитель и дробную часть было не набрать
    const row = { ...editRow, balance: parseFloat(editBalance.replace(",", ".")) || 0 };
    fetch(`${apiPath(editRow)}/${editRow.id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        login: row.login,
        email: row.email,
        password: row.password,
        token: row.token,
        balance: row.balance,
        aff: row.aff,
        access_token: row.access_token,
        panel_id: row.panel_id,
        note: row.note,
      }),
    })
      .then(() => {
        setAccounts((prev) => prev.map((a) => (rowKey(a) === editingKey ? row : a)));
        setEditingKey(null);
        setEditRow(null);
      })
      .catch(() => {});
  };


  const renderTable = (title: string, rows: SiteAccount[], isCustom: boolean) => (
    <section className="hm-section">
      <h2 className="hm-section-title">
        {title} <span className="hm-section-count">({rows.length})</span>
      </h2>
      <div className="hm-table-card">
        <table className="hm-table">
          <thead className="hm-thead">
            <tr className="hm-head-row">
              <th className="hm-th hm-th-id">ID</th>
              <th className="hm-th hm-th-login">login</th>
              {isCustom ? (
                <th className="hm-th hm-th-email">email</th>
              ) : (
                <th className="hm-th hm-th-pass-github" title="Пароль GitHub-аккаунта из main_github — им же вход через OAuth">pass_github</th>
              )}
              {isCustom && <th className="hm-th hm-th-password">password</th>}
              <th className="hm-th hm-th-token">token</th>
              <th className="hm-th hm-th-balance">balance</th>
              <th className="hm-th hm-th-opus-req" title="Сколько запросов Claude Code покрывает остаток">запросов</th>
              <th className="hm-th hm-th-day-work" title="На сколько дней хватит остатка при нашем среднем темпе за 3 суток">дней</th>
              <th className="hm-th hm-th-aff">aff</th>
              <th className="hm-th hm-th-access-token">токен доступа</th>
              <th className="hm-th hm-th-panel-id">ID в панели</th>
              <th className="hm-th hm-th-note">примечания</th>
              <th className="hm-th hm-th-actions"></th>
            </tr>
          </thead>
          <tbody className="hm-tbody">
            {rows.map((acc) => (
              <tr
                key={rowKey(acc)}
                className={
                  editingKey === rowKey(acc) ? "hm-row hm-row-editing" : "hm-row"
                }
              >
                {editingKey === rowKey(acc) && editRow ? (
                  <>
                    <td className="hm-td hm-td-id">{editRow.id}</td>
                    <td className="hm-td hm-td-login"><input className="hm-input hm-input-login" value={editRow.login || ""} onChange={(e) => setEditRow({ ...editRow, login: e.target.value })} /></td>
                    {isCustom ? (
                      <td className="hm-td hm-td-email"><input className="hm-input hm-input-email" value={editRow.email || ""} onChange={(e) => setEditRow({ ...editRow, email: e.target.value })} /></td>
                    ) : (
                      <td className="hm-td hm-td-pass-github">{editRow.pass_github ?? "—"}</td>
                    )}
                    {isCustom && <td className="hm-td hm-td-password"><input className="hm-input hm-input-password" value={editRow.password || ""} onChange={(e) => setEditRow({ ...editRow, password: e.target.value })} /></td>}
                    <td className="hm-td hm-td-token"><input className="hm-input hm-input-token" value={editRow.token || ""} onChange={(e) => setEditRow({ ...editRow, token: e.target.value })} /></td>
                    <td className="hm-td hm-td-balance"><input className="hm-input hm-input-balance hm-input-narrow" value={editBalance} onChange={(e) => setEditBalance(e.target.value)} /></td>
                    <td className="hm-td hm-td-opus-req">{editRow.opus_5_req ?? "—"}</td>
                    <td className="hm-td hm-td-day-work">{fmtDays(editRow.day_work)}</td>
                    <td className="hm-td hm-td-aff"><input className="hm-input hm-input-aff" value={editRow.aff || ""} onChange={(e) => setEditRow({ ...editRow, aff: e.target.value })} /></td>
                    <td className="hm-td hm-td-access-token"><input className="hm-input hm-input-access-token" value={editRow.access_token || ""} onChange={(e) => setEditRow({ ...editRow, access_token: e.target.value })} /></td>
                    <td className="hm-td hm-td-panel-id"><input className="hm-input hm-input-panel-id hm-input-narrow" value={editRow.panel_id ?? ""} onChange={(e) => setEditRow({ ...editRow, panel_id: parseInt(e.target.value, 10) || null })} /></td>
                    <td className="hm-td hm-td-note"><input className="hm-input hm-input-note" value={editRow.note || ""} onChange={(e) => setEditRow({ ...editRow, note: e.target.value })} /></td>
                    <td className="hm-td hm-td-actions">
                      <button onClick={handleSave} title="Сохранить" className="hm-btn-save">✓</button>
                    </td>
                  </>
                ) : (
                  <>
                    <td className="hm-td hm-td-id">{acc.id}</td>
                    <td className="hm-td hm-td-login">{acc.login ? <span className="hm-cell-inline"><span className={isActive(acc) ? "hm-cell-value hm-login-value hm-login-value--active" : "hm-cell-value hm-login-value"} title={isActive(acc) ? "На этом ключе сидит сессия Claude Code" : undefined}>{acc.login}</span><CopyBtn value={acc.login} /></span> : "—"}</td>
                    {isCustom ? (
                      <td className="hm-td hm-td-email">{acc.email ? <span className="hm-cell-inline"><span className="hm-cell-value hm-email-value">{acc.email}</span><CopyBtn value={acc.email} /></span> : "—"}</td>
                    ) : (
                      <td className="hm-td hm-td-pass-github">{acc.pass_github ? <span className="hm-cell-inline"><span className="hm-cell-value hm-pass-github-value">{acc.pass_github}</span><CopyBtn value={acc.pass_github} /></span> : "—"}</td>
                    )}
                    {isCustom && <td className="hm-td hm-td-password">{acc.password ? <span className="hm-cell-inline"><span className="hm-cell-value hm-password-value">{acc.password}</span><CopyBtn value={acc.password} /></span> : "—"}</td>}
                    <td className="hm-td hm-td-token">
                      {acc.token ? (
                        <span className="hm-cell-inline">
                          <span className="hm-cell-value hm-token-value">{acc.token}</span>
                          <CopyBtn value={acc.token} />
                        </span>
                      ) : "—"}
                    </td>
                    <td className="hm-td hm-td-balance">{fmtBalance(acc.balance)}</td>
                    <td className="hm-td hm-td-opus-req">{acc.opus_5_req ?? "—"}</td>
                    <td className="hm-td hm-td-day-work">{fmtDays(acc.day_work)}</td>
                    <td className="hm-td hm-td-aff">{acc.aff ? <a className="hm-aff-link" href={acc.aff} target="_blank" rel="noopener noreferrer">↗</a> : "—"}</td>
                    <td className="hm-td hm-td-access-token">
                      {acc.access_token ? (
                        <span className="hm-cell-inline">
                          <span className="hm-cell-value hm-access-token-value">{acc.access_token}</span>
                          <CopyBtn value={acc.access_token} />
                        </span>
                      ) : "—"}
                    </td>
                    <td className="hm-td hm-td-panel-id">{acc.panel_id ?? "—"}</td>
                    <td className="hm-td hm-td-note">{acc.note ? <LongText text={acc.note} /> : "—"}</td>
                    <td className="hm-td hm-td-actions">
                      <button onClick={() => handleEdit(acc)} title="Редактировать" className="hm-btn-edit">✏️</button>
                      <button onClick={() => handleDelete(acc)} title="Удалить" className="hm-btn-del">🗑️</button>
                    </td>
                  </>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );

  const customRows = accounts.filter((a) => a.kind === "custom");
  const githubRows = accounts.filter((a) => a.kind === "github");

  return (
    <main id="home">
      <div className="hm-site-picker">
        <SiteSelect
          className="hm-site-select"
          id="hm-site-select"
          sites={sites}
          value={selectedSiteId}
          onChange={(s) => setSelectedSiteId(s.id)}
        />
      </div>

      {accounts.length === 0 && <p className="hm-empty">Нет записей для этого сайта</p>}

      {customRows.length > 0 && renderTable("Почта", customRows, true)}
      {githubRows.length > 0 && renderTable("GitHub", githubRows, false)}
    </main>
  );
}