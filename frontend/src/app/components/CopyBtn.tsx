"use client";

import { useState } from "react";

export default function CopyBtn({ value }: { value: string | null }) {
  const [copied, setCopied] = useState(false);

  if (!value) return null;

  const handleCopy = () => {
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
