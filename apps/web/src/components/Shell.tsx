"use client";

// App shell: nav + session guard. Any page wrapped in <Shell> redirects to
// /login when the session is gone, so no page has to think about auth.

import { useEffect, useState, type ReactNode } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

import { api, type User } from "@/lib/api";

const LINKS = [
  { href: "/", label: "Search" },
  { href: "/browse", label: "Browse" },
  { href: "/libraries", label: "Libraries" },
  { href: "/processing", label: "Processing" },
  { href: "/health", label: "System" },
  { href: "/security", label: "Security" },
];

export default function Shell({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [user, setUser] = useState<User | null>(null);
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    api
      .me()
      .then(setUser)
      .catch(() => router.replace("/login"))
      .finally(() => setChecked(true));
  }, [router]);

  async function signOut() {
    await api.logout().catch(() => undefined);
    router.replace("/login");
  }

  if (!checked) return <div className="shell" />;
  if (!user) return null;

  return (
    <div className="shell">
      <header className="topbar">
        <Link href="/" className="brand">
          FrameFound
        </Link>
        <nav className="navlinks">
          {LINKS.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              className="navlink"
              data-active={
                link.href === "/" ? pathname === "/" : pathname.startsWith(link.href)
              }
            >
              {link.label}
            </Link>
          ))}
        </nav>
        <div className="topbar-end">
          <span className="mono">{user.email}</span>
          <button className="btn" onClick={signOut} style={{ padding: "6px 12px" }}>
            Sign out
          </button>
        </div>
      </header>
      <main className="page">{children}</main>
    </div>
  );
}
