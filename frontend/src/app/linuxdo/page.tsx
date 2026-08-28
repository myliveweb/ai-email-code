"use client";

import { useCallback, useEffect, useState } from "react";

import { TOPIC_LINUXDO, useLiveUpdate } from "../lib/liveUpdates";

type Item = {
  topic_id: number;
  title: string;
  useful: string;
  literal: string;
  url: string;
  born: string;
  station: string;
  known: boolean;
  marks: string[];
  // перевод первого поста; оригинал-иероглифы эндпоинт не отдаёт
  body: string;
  state: string[];
  cdk: string;
  group: "live" | "other" | "dead" | "plain";
  hot: boolean;
};

type Report = {
  stamp: string;
  account: string;
  trust_level: number;
  seen_total: number;
  picked: number;
  with_links: number;
  closed: number;
  items: Item[];
};

// Верхняя таблица собирается по признаку hot, остальные — по группе, и hot из них
// исключается: иначе живая раздача попала бы в отчёт дважды.
const TABLES: { key: string; title: string; pick: (i: Item) => boolean }[] = [
  { key: "hot", title: "Идти в первую очередь — свежее и не закрытое", pick: (i) => i.hot },
  {
    key: "other",
    title: "Станции без сервиса раздач — постарше, глянуть глазами",
    pick: (i) => i.group === "other" && !i.hot,
  },
  { key: "plain", title: "Остальные темы про LLM", pick: (i) => i.group === "plain" },
  { key: "dead", title: "Закрытые и недоступные", pick: (i) => i.group === "dead" },
];

function when(iso: string) {
  const d = new Date(iso);
  return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function Mark({ text }: { text: string }) {
  return <span className="ld-mark">{text}</span>;
}

function Row({ item }: { item: Item }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState(false);
  const tags = [...item.marks, ...item.state];
  return (
    <>
      <tr className={`ld-row ld-row-${item.group}${open || text ? " ld-row-open" : ""}`}>
        <td className="ld-cell ld-cell-when">{when(item.born)}</td>
        <td className="ld-cell ld-cell-topic">
          <a className="ld-topic-link" href={item.url} target="_blank" rel="noopener noreferrer">
            {item.useful || item.title}
          </a>
        </td>
        <td className="ld-cell ld-cell-station">
          {item.station && (
            <>
              <a
                className="ld-station-link"
                href={`https://${item.station}`}
                target="_blank"
                rel="noopener noreferrer"
              >
                {item.station}
              </a>
              <span className={item.known ? "ld-station-known" : "ld-station-new"}>
                {item.known ? " · уже в базе" : " · в базе нет"}
              </span>
            </>
          )}
        </td>
        <td className="ld-cell ld-cell-marks">
          <div className="ld-marks">
            {tags.map((t) => (
              <Mark key={t} text={t} />
            ))}
          </div>
        </td>
        <td className="ld-cell ld-cell-code">
          {item.cdk && (
            <a className="ld-code-link" href={item.cdk} target="_blank" rel="noopener noreferrer">
              забрать код →
            </a>
          )}
        </td>
        <td className="ld-cell ld-cell-toggle">
          {item.body && (
            <button className="ld-toggle ld-toggle-text" onClick={() => setText(!text)}>
              текст {text ? "▴" : "▾"}
            </button>
          )}
          <button className="ld-toggle" onClick={() => setOpen(!open)}>
            оригинал {open ? "▴" : "▾"}
          </button>
        </td>
      </tr>
      {text && (
        <tr className="ld-row-body">
          <td className="ld-cell ld-cell-body-pad" />
          <td className="ld-cell ld-cell-body" colSpan={5}>
            {item.body}
          </td>
        </tr>
      )}
      {open && (
        <tr className="ld-row-original">
          <td className="ld-cell ld-cell-original-pad" />
          <td className="ld-cell ld-cell-original" colSpan={5}>
            <div className="ld-original-title">{item.title}</div>
            {item.literal && <div className="ld-original-literal">{item.literal}</div>}
          </td>
        </tr>
      )}
    </>
  );
}

export default function LinuxdoPage() {
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    fetch("/api/linuxdo/report")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(setReport)
      .catch((e) => setError(String(e.message ?? e)));
  }, []);

  useEffect(() => { load(); }, [load]);

  // разведка форума закончила прогон — перечитываем отчёт, страница остаётся на месте
  useLiveUpdate(TOPIC_LINUXDO, load);

  if (error) {
    return (
      <main id="linuxdo" className="ld-page ld-page-error">
        Отчёт недоступен: {error}
      </main>
    );
  }
  if (!report) {
    return (
      <main id="linuxdo" className="ld-page ld-page-loading">
        Читаю отчёт…
      </main>
    );
  }

  return (
    <main id="linuxdo" className="ld-page">
      <h1 className="ld-title">Раздачи linux.do</h1>
      <div className="ld-meta">
        Отчёт от {when(report.stamp)} · аккаунт {report.account}, TL{report.trust_level} · просмотрено тем{" "}
        {report.seen_total}, про LLM и свежих {report.picked}, со ссылкой на раздачу {report.with_links} · закрытых
        помним {report.closed}
      </div>

      {TABLES.map(({ key, title, pick }) => {
        const rows = report.items.filter(pick);
        if (!rows.length) return null;
        return (
          <section key={key} id={`ld-section-${key}`} className={`ld-section ld-section-${key}`}>
            <h2 className="ld-section-title">
              {title} · {rows.length}
            </h2>
            <table className={`ld-table ld-table-${key}`}>
              <thead className="ld-thead">
                <tr className="ld-head-row">
                  <th className="ld-th ld-th-when">Когда</th>
                  <th className="ld-th ld-th-topic">Тема</th>
                  <th className="ld-th ld-th-station">Станция</th>
                  <th className="ld-th ld-th-marks">Метки</th>
                  <th className="ld-th ld-th-code">Код</th>
                  <th className="ld-th ld-th-toggle" />
                </tr>
              </thead>
              <tbody className="ld-tbody">
                {rows.map((item) => (
                  <Row key={`${item.topic_id}-${item.cdk}`} item={item} />
                ))}
              </tbody>
            </table>
          </section>
        );
      })}

      {!report.items.some((i) => i.hot) && (
        <div className="ld-empty">Прямо сейчас доступного нет — это нормальный темп тега.</div>
      )}
    </main>
  );
}
