"use client";

// App shell: nav + session guard. Any page wrapped in <Shell> redirects to
// /login when the session is gone, so no page has to think about auth.
//
// The nav reached ten items, which is more than a single row reads as. It is
// now split by what the operator is doing: finding things, versus running the
// system. The four "find" destinations stay visible because they are what the
// product is for; the six administrative ones move behind one menu, since
// nobody visits Storage twice in a session.
//
// The Manage menu sits OUTSIDE <nav className="navlinks"> deliberately. That
// element scrolls horizontally on narrow screens (overflow-x: auto), and an
// absolutely-positioned dropdown inside an overflow container is clipped by
// it — the panel rendered and was invisible, which is exactly how it shipped
// broken the first time.

import { useEffect, useRef, useState, type ReactNode } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

import { api, type SystemAlert, type User } from "@/lib/api";

const FIND = [
  { href: "/", label: "Search" },
  { href: "/browse", label: "Browse" },
  { href: "/places", label: "Places" },
  { href: "/tags", label: "Tags" },
  { href: "/people", label: "People" },
  { href: "/slideshows", label: "Slideshows" },
  { href: "/listings", label: "Listings" },
  { href: "/showcase", label: "Showcase" },
];

const MANAGE = [
  { href: "/libraries", label: "Libraries", hint: "What is being catalogued" },
  { href: "/drive", label: "Drive", hint: "Sort listing folders in Google Drive" },
  { href: "/storage", label: "Storage", hint: "Drives and where things live" },
  { href: "/basemaps", label: "Basemaps", hint: "Offline maps, one file each" },
  { href: "/duplicates", label: "Duplicates", hint: "Reclaimable space" },
  { href: "/processing", label: "Processing", hint: "Queues and recent failures" },
  { href: "/health", label: "System", hint: "Health and versions" },
  { href: "/security", label: "Security", hint: "Access, 2FA, maps keys" },
];

// Remembered across page mounts. The shell is mounted by every page (there is
// no route-group layout), so each navigation used to re-check the session and
// blank the header and content until /auth/me answered — two extra requests
// and a flash on every click. The known user renders at once and is
// re-validated in the background; alerts refresh on a clock, not per page.
let knownUser: User | null = null;
let knownAlerts: SystemAlert[] = [];
let alertsFetchedAt = 0;
const ALERTS_EVERY_MS = 60_000;

function isActive(href: string, pathname: string): boolean {
  return href === "/" ? pathname === "/" : pathname.startsWith(href);
}

export default function Shell({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [user, setUser] = useState<User | null>(knownUser);
  const [checked, setChecked] = useState(knownUser !== null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [alerts, setAlerts] = useState<SystemAlert[]>(knownAlerts);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let live = true;
    api
      .me()
      .then((me) => {
        knownUser = me;
        if (live) setUser(me);
      })
      .catch(() => {
        knownUser = null;
        router.replace("/login");
      })
      .finally(() => {
        if (live) setChecked(true);
      });
    return () => {
      live = false;
    };
  }, [router]);

  // Problems that stop work get a line on every page. The NAS was unmounted
  // for 39 days while the only signs of it sat on pages nobody opened. Asked
  // once a minute while the tab is visible, not once per navigation.
  useEffect(() => {
    if (!user) return;
    let live = true;
    const refresh = async () => {
      if (document.hidden) return;
      try {
        const found = await api.alerts();
        knownAlerts = found;
        alertsFetchedAt = Date.now();
        if (live) setAlerts(found);
      } catch {
        /* the banner is best effort */
      }
    };
    if (Date.now() - alertsFetchedAt > ALERTS_EVERY_MS) refresh();
    const timer = setInterval(refresh, ALERTS_EVERY_MS);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [user]);

  // Close on route change, so the menu never lingers over the page it opened.
  useEffect(() => {
    setMenuOpen(false);
  }, [pathname]);

  // Click-away and Escape. A menu that can only be closed by choosing
  // something is a trap, particularly on a phone.
  useEffect(() => {
    if (!menuOpen) return;
    function onPointerDown(event: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        setMenuOpen(false);
      }
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") setMenuOpen(false);
    }
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

  async function signOut() {
    await api.logout().catch(() => undefined);
    knownUser = null;
    knownAlerts = [];
    alertsFetchedAt = 0;
    router.replace("/login");
  }

  if (!checked) return <div className="shell" />;
  if (!user) return null;

  const manageActive = MANAGE.some((link) => isActive(link.href, pathname));

  return (
    <div className="shell">
      <a className="skiplink" href="#main">
        Skip to content
      </a>
      <header className="topbar">
        <Link href="/" className="brand">
          FrameFound
        </Link>
        <nav className="navlinks" aria-label="Sections">
          {FIND.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              className="navlink"
              data-active={isActive(link.href, pathname)}
              aria-current={isActive(link.href, pathname) ? "page" : undefined}
            >
              {link.label}
            </Link>
          ))}
        </nav>

        <div className="menu" ref={menuRef}>
          <button
            type="button"
            className="navlink menu-trigger"
            data-active={manageActive}
            aria-expanded={menuOpen}
            aria-haspopup="menu"
            onClick={() => setMenuOpen((open) => !open)}
          >
            Manage
            <span aria-hidden="true" className="menu-caret">
              ▾
            </span>
          </button>
          {menuOpen && (
            <div className="menu-panel" role="menu">
              {MANAGE.map((link) => (
                <Link
                  key={link.href}
                  href={link.href}
                  role="menuitem"
                  className="menu-item"
                  data-active={isActive(link.href, pathname)}
                >
                  <span>{link.label}</span>
                  <small className="faint">{link.hint}</small>
                </Link>
              ))}
            </div>
          )}
        </div>
        <div className="topbar-end">
          <span className="mono">{user.email}</span>
          <button className="btn" onClick={signOut} style={{ padding: "6px 12px" }}>
            Sign out
          </button>
        </div>
      </header>
      <main className="page" id="main" tabIndex={-1}>
        {alerts.map((alert) => (
          <div className="alertbar" data-level={alert.level} role="status" key={alert.title}>
            <strong>{alert.title}</strong>
            <span>{alert.detail}</span>
            {!pathname.startsWith(alert.href) && <Link href={alert.href}>Details →</Link>}
          </div>
        ))}
        {children}
      </main>
    </div>
  );
}
