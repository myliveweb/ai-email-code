"use client";

import { useState } from "react";

const LIMIT = 100;

export default function LongText({ text }: { text: string }) {
  const [open, setOpen] = useState(false);

  if (text.length <= LIMIT) return <span className="cm-longtext-text">{text}</span>;

  return (
    <span className="cm-longtext">
      <span className={open ? "cm-longtext-text" : "cm-longtext-text cm-longtext-clamp"}>
        {text}
      </span>
      <button onClick={() => setOpen(!open)} className="cm-longtext-toggle">
        {open ? "свернуть" : "показать все"}
      </button>
    </span>
  );
}
