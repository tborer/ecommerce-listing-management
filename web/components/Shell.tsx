"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

const NAV = [
  { href: "/dashboard", label: "Dashboard" },
  { href: "/settings", label: "Settings" },
  { href: "/connections", label: "Connections" },
];

export default function Shell({ children }: { children: React.ReactNode }) {
  const path = usePathname();
  const [email, setEmail] = useState<string | null>(null);

  useEffect(() => {
    api<{ email: string }>("/auth/me").then((me) => setEmail(me.email)).catch(() => {});
  }, []);

  async function logout() {
    await api("/auth/logout", { method: "POST" }).catch(() => {});
    window.location.href = "/login";
  }

  if (!email) return <div className="container muted">Loading…</div>;
  return (
    <>
      <header className="topbar">
        <span className="brand">Listing Manager</span>
        <nav>
          {NAV.map((n) => (
            <Link key={n.href} href={n.href} className={path === n.href ? "active" : ""}>
              {n.label}
            </Link>
          ))}
        </nav>
        <span className="muted small">{email}</span>
        <button className="btn sm" onClick={logout}>Log out</button>
      </header>
      <main className="container">{children}</main>
    </>
  );
}
