"use client";

import { useEffect, useState } from "react";
import CopyBtn from "./components/CopyBtn";
import { SITE_KEY, pickStickyId, rememberId } from "./lib/sticky";

type Site = {
  id: number;
  name: string;
};

type SiteAccount = {
  id: number;
  login: string | null;
  email: string | null;
  password: string | null;
  token: string | null;
  balance: number;
  aff: string | null;
  note: string | null;
  kind: "github" | "custom";
};

export default function Home() {
  const [sites, setSites] = useState<Site[]>([]);
  const [selectedSiteId, setSelectedSiteId] = useState<number>(0);
  const [accounts, setAccounts] = useState<SiteAccount[]>([]);
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editRow, setEditRow] = useState<SiteAccount | null>(null);
  const [editBalance, setEditBalance] = useState("");

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

  useEffect(() => {
    if (!selectedSiteId) return;
    Promise.all([
      fetch(`/api/site-accounts?site_id=${selectedSiteId}`).then((r) => r.json()),
      fetch(`/api/site-accounts-custom?site_id=${selectedSiteId}`).then((r) => r.json()),
    ])
      .then(([gh, custom]: [SiteAccount[], SiteAccount[]]) =>
        setAccounts([
          ...gh.map((a) => ({ ...a, password: null, kind: "github" as const })),
          ...custom.map((a) => ({ ...a, kind: "custom" as const })),
        ]),
      )
      .catch(() => {});
  }, [selectedSiteId]);

  const rowKey = (acc: SiteAccount) => `${acc.kind}-${acc.id}`;
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
    setEditBalance(String(acc.balance));
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


  const renderTable = (title: string, rows: SiteAccount[], withPassword: boolean) => (
    <section style={{ marginBottom: 28 }}>
      <h2 style={{ fontSize: "1rem", margin: "0 0 8px" }}>
        {title} <span style={{ color: "var(--text-muted)", fontWeight: 400 }}>({rows.length})</span>
      </h2>
      <div style={{ background: "var(--card-bg)", borderRadius: 8, boxShadow: "var(--card-shadow)", overflow: "hidden" }}>
        <table>
          <thead>
            <tr>
              <th>login</th>
              <th>email</th>
              {withPassword && <th>password</th>}
              <th>token</th>
              <th>balance</th>
              <th>aff</th>
              <th>примечания</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((acc) => (
              <tr key={rowKey(acc)}>
                {editingKey === rowKey(acc) && editRow ? (
                  <>
                    <td><input value={editRow.login || ""} onChange={(e) => setEditRow({ ...editRow, login: e.target.value })} /></td>
                    <td><input value={editRow.email || ""} onChange={(e) => setEditRow({ ...editRow, email: e.target.value })} /></td>
                    {withPassword && <td><input value={editRow.password || ""} onChange={(e) => setEditRow({ ...editRow, password: e.target.value })} /></td>}
                    <td><input value={editRow.token || ""} onChange={(e) => setEditRow({ ...editRow, token: e.target.value })} /></td>
                    <td><input style={{ width: 70 }} value={editBalance} onChange={(e) => setEditBalance(e.target.value)} /></td>
                    <td><input value={editRow.aff || ""} onChange={(e) => setEditRow({ ...editRow, aff: e.target.value })} /></td>
                    <td><input value={editRow.note || ""} onChange={(e) => setEditRow({ ...editRow, note: e.target.value })} /></td>
                    <td>
                      <button onClick={handleSave} title="Сохранить" style={{ border: "none", background: "none", color: "var(--accent)", fontSize: "1.1rem", padding: 4 }}>✓</button>
                    </td>
                  </>
                ) : (
                  <>
                    <td>{acc.login ? <>{acc.login}<CopyBtn value={acc.login} /></> : "—"}</td>
                    <td>{acc.email ? <>{acc.email}<CopyBtn value={acc.email} /></> : "—"}</td>
                    {withPassword && <td>{acc.password ? <>{acc.password}<CopyBtn value={acc.password} /></> : "—"}</td>}
                    <td>
                      {acc.token ? (
                        <span style={{ display: "flex", alignItems: "center" }}>
                          <span style={{ maxWidth: 180, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{acc.token}</span>
                          <CopyBtn value={acc.token} />
                        </span>
                      ) : "—"}
                    </td>
                    <td>{acc.balance}</td>
                    <td>{acc.aff ? <a href={acc.aff} target="_blank" rel="noopener noreferrer">↗</a> : "—"}</td>
                    <td style={{ maxWidth: 260, whiteSpace: "pre-wrap" }}>{acc.note || "—"}</td>
                    <td style={{ whiteSpace: "nowrap" }}>
                      <button onClick={() => handleEdit(acc)} title="Редактировать" style={{ border: "none", background: "none", color: "var(--text-muted)", fontSize: "1rem", padding: 4 }}>✏️</button>
                      <button onClick={() => handleDelete(acc)} title="Удалить" style={{ border: "none", background: "none", color: "var(--text-muted)", fontSize: "1rem", padding: 4, marginLeft: 2 }}>🗑️</button>
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
    <main style={{ padding: "2rem" }}>
      <div style={{ marginBottom: 20 }}>
        <select
          value={selectedSiteId}
          onChange={(e) => setSelectedSiteId(Number(e.target.value))}
        >
          {sites.map((s) => (
            <option key={s.id} value={s.id}>{s.name}</option>
          ))}
        </select>
      </div>

      {accounts.length === 0 && <p style={{ color: "var(--text-muted)" }}>Нет записей для этого сайта</p>}

      {customRows.length > 0 && renderTable("Почта", customRows, true)}
      {githubRows.length > 0 && renderTable("GitHub", githubRows, false)}
    </main>
  );
}