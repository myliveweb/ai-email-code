"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import ClaudeIcon from "./ClaudeIcon";
import { useActiveStation } from "../lib/activeStation";

const NAV_ITEMS = [
  { href: "/sites", label: "Сайты" },
  { href: "/", label: "Аккаунты" },
  { href: "/browse", label: "Менеджмент" },
  { href: "/linuxdo", label: "linux.do" },
];

export default function Nav() {
  const pathname = usePathname();
  const active = useActiveStation();

  return (
    <nav id="nav" className="cm-nav">
      <div className="cm-nav-links">
        {NAV_ITEMS.map((item) => (
          <Link
            key={item.href}
            href={item.href}
            className={
              pathname === item.href ? "cm-nav-link cm-nav-link-active" : "cm-nav-link"
            }
          >
            {item.label}
          </Link>
        ))}
      </div>

      <div className="cm-nav-active" id="cm-nav-active" title="станция и аккаунт Claude Code">
        <ClaudeIcon />
        {active && (
          <span className="cm-nav-active-text">
            <span className="cm-nav-active-station">{active.station ?? "станция не наша"}</span>
            <span className="cm-nav-active-login">{active.login ?? "ключ вне базы"}</span>
          </span>
        )}
      </div>
    </nav>
  );
}
