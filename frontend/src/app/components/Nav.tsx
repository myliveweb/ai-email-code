"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const NAV_ITEMS = [
  { href: "/sites", label: "Сайты" },
  { href: "/", label: "Аккаунты" },
  { href: "/browse", label: "Менеджмент" },
];

export default function Nav() {
  const pathname = usePathname();

  return (
    <nav style={{ display: "flex", gap: 24, padding: "14px 2rem", background: "var(--nav-bg)", borderBottom: "1px solid var(--border)" }}>
      {NAV_ITEMS.map((item) => (
        <Link
          key={item.href}
          href={item.href}
          style={{
            textDecoration: "none",
            fontWeight: pathname === item.href ? "600" : "400",
            color: pathname === item.href ? "var(--accent)" : "var(--text-muted)",
            fontSize: "0.9rem",
            transition: "color 0.15s",
          }}
        >
          {item.label}
        </Link>
      ))}
    </nav>
  );
}
