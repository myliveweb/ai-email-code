"use client";

import { useEffect, useRef, useState } from "react";

import ClaudeIcon from "./ClaudeIcon";
import { useActiveStation } from "../lib/activeStation";

type Site = { id: number; name: string; cnt?: number | null };

export default function SiteSelect({
  sites, value, onChange, className = "cm-site-select", id,
}: {
  sites: Site[];
  value: number;
  onChange: (site: Site) => void;
  className?: string;
  id?: string;
}) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const active = useActiveStation();
  const current = sites.find((s) => s.id === value);

  useEffect(() => {
    if (!open) return;
    const outside = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", outside);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", outside);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  return (
    <div className={`cm-select ${className}`} id={id} ref={box}>
      <button
        type="button"
        className="cm-select-head"
        aria-expanded={open}
        aria-haspopup="listbox"
        onClick={() => setOpen((v) => !v)}
      >
        <span className="cm-select-cnt">{current?.cnt ?? ""}</span>
        <span className="cm-select-name">{current?.name ?? "выберите сайт"}</span>
        <span className="cm-select-mark">
          {current && current.name === active?.station && <ClaudeIcon />}
        </span>
        <span className="cm-select-arrow">▾</span>
      </button>

      {open && (
        <ul className="cm-select-list" role="listbox">
          {sites.map((s) => (
            <li key={s.id} className="cm-select-item" role="option" aria-selected={s.id === value}>
              <button
                type="button"
                className={
                  s.id === value ? "cm-select-option cm-select-option-current" : "cm-select-option"
                }
                onClick={() => { onChange(s); setOpen(false); }}
              >
                <span className="cm-select-cnt">{s.cnt ?? ""}</span>
                <span className="cm-select-name">{s.name}</span>
                <span className="cm-select-mark">
                  {s.name === active?.station && <ClaudeIcon />}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
