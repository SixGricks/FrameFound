"use client";

// App shell: nav + session guard. Any page wrapped in <Shell> redirects to
// /login when the session is gone, so no page has to think about auth.
//
// The nav outgrew a single row at ten items, so it is split by what the
// operator is doing: finding things, versus running the system. The "find"
// destinations (FIND, below) stay visible because they are what the product
// is for; the administrative ones (MANAGE) sit behind one menu, since nobody
// visits Storage twice in a session. Both lists have grown since — nine
// finding and nine managing at the last count (AI models joined Manage;
// Cuts joined Find, as a making page like Slideshows and Listings, after the
// row was measured not to scroll at 1280 px), eighteen in all. Every page has to be in one
// of them, and FIND already scrolls sideways under about 1100 px, so anything
// that is not a daily destination belongs in MANAGE.
//
// A row that is cut off must say so: nine links fitted at 1280 px exactly,
// and a little narrower the last one was cut with no scrollbar (it is
// hidden) and no fade (that was for phones). The row is measured, and while
// there is more of it to the right it is marked `data-more`, which draws the
// fade at any width (globals.css).
//
// The Manage menu sits OUTSIDE <nav className="navlinks"> deliberately. That
// element scrolls horizontally on narrow screens (overflow-x: auto), and an
// absolutely-positioned dropdown inside an overflow container is clipped by
// it — the panel rendered and was invisible, which is exactly how it shipped
// broken the first time.

import { useEffect, useRef, useState, type ReactNode } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

import { ChatButton } from "@/components/ChatPanel";
import { ApiError, api, type SystemAlert, type User } from "@/lib/api";
import { chat } from "@/lib/chatStore";

const FIND = [
  { href: "/", label: "Search" },
  { href: "/browse", label: "Browse" },
  { href: "/places", label: "Places" },
  { href: "/tags", label: "Tags" },
  { href: "/people", label: "People" },
  { href: "/slideshows", label: "Slideshows" },
  { href: "/cuts", label: "Cuts" },
  { href: "/listings", label: "Listings" },
  { href: "/showcase", label: "Showcase" },
];

const MANAGE = [
  { href: "/libraries", label: "Libraries", hint: "What is being catalogued" },
  { href: "/drive", label: "Drive", hint: "Sort listing folders in Google Drive" },
  { href: "/storage", label: "Storage", hint: "Drives and where things live" },
  {
    href: "/models",
    label: "AI models",
    hint: "Models that run on your own machine — add, compare, assign",
  },
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

/** Where a key moves the focus among `count` menu items from `at` (-1: the
 *  Manage button itself), or null for a key the menu leaves alone. */
function menuStep(key: string, at: number, count: number): number | null {
  if (count === 0) return null;
  switch (key) {
    case "ArrowDown":
      return at < 0 ? 0 : (at + 1) % count;
    case "ArrowUp":
      return at < 0 ? count - 1 : (at - 1 + count) % count;
    case "Home":
      return 0;
    case "End":
      return count - 1;
    default:
      return null;
  }
}

function menuItems(menu: HTMLElement | null): HTMLElement[] {
  return Array.from(menu?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? []);
}

export default function Shell({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [user, setUser] = useState<User | null>(knownUser);
  const [checked, setChecked] = useState(knownUser !== null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [alerts, setAlerts] = useState<SystemAlert[]>(knownAlerts);
  const menuRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  // Which item takes the focus once the panel is drawn, when the menu was
  // opened from the keyboard with an arrow.
  const focusOnOpen = useRef<"first" | "last" | null>(null);
  const navRef = useRef<HTMLElement>(null);
  const [navMore, setNavMore] = useState(false);

  // Whether the Find row has links out of sight to its right: when its box
  // changes (the window, the chat panel opening beside the page) and as it
  // is scrolled, so the fade goes once the last link is in view.
  const shown = checked && user !== null;
  useEffect(() => {
    const nav = navRef.current;
    if (!shown || !nav) return;
    // Two pixels of slack: the widths are whole numbers and the scroll
    // position is not (a zoomed page), so "at the end" is seldom exactly 0.
    const measure = () => setNavMore(nav.scrollWidth - nav.clientWidth - nav.scrollLeft > 2);
    measure();
    const watcher = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    watcher?.observe(nav);
    nav.addEventListener("scroll", measure, { passive: true });
    return () => {
      watcher?.disconnect();
      nav.removeEventListener("scroll", measure);
    };
  }, [shown]);

  useEffect(() => {
    let live = true;
    api
      .me()
      .then((me) => {
        knownUser = me;
        // The chat panel does nothing, and asks the server nothing, until
        // it is told who is signed in. A no-op when it already knows.
        chat.signIn(me.id);
        if (live) setUser(me);
      })
      .catch((err) => {
        knownUser = null;
        // The session is over (not merely unreachable): a conversation must
        // not stay in the tab's memory for whoever signs in next, nor go on
        // asking after a turn with a session that no longer exists.
        if (err instanceof ApiError && err.status === 401) chat.signOut();
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

  useEffect(() => {
    if (!menuOpen || !focusOnOpen.current) return;
    const items = menuItems(menuRef.current);
    (focusOnOpen.current === "first" ? items[0] : items[items.length - 1])?.focus();
    focusOnOpen.current = null;
  }, [menuOpen]);

  // Click-away and Escape. A menu that can only be closed by choosing
  // something is a trap, particularly on a phone.
  //
  // It is marked up as a menu (role="menu"), so it answers the keys a menu
  // does: the arrows, Home and End move between its items, and Escape puts
  // the focus back on Manage. Escape used to remove the focused item with
  // the panel and leave the focus on nothing, so the next Tab started again
  // at the top of the page.
  useEffect(() => {
    if (!menuOpen) return;
    function onPointerDown(event: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        setMenuOpen(false);
      }
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") {
        // Marked as used, so a page listening for Esc (the Tags review
        // clears its selection on it) leaves this one to the menu.
        event.preventDefault();
        const inside = !!menuRef.current?.contains(document.activeElement);
        setMenuOpen(false);
        if (inside) triggerRef.current?.focus();
        return;
      }
      // Only while the focus is in the menu: arrows elsewhere on the page
      // (a slider, a text field) are not the menu's.
      if (!menuRef.current?.contains(document.activeElement)) return;
      const items = menuItems(menuRef.current);
      const next = menuStep(event.key, items.indexOf(document.activeElement as HTMLElement), items.length);
      if (next === null) return;
      event.preventDefault();
      items[next]?.focus();
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
    // The page is not reloaded on the way out, so what the chat panel holds
    // in memory would still be there for the next person to sign in here.
    chat.signOut();
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
        <nav className="navlinks" aria-label="Sections" ref={navRef} data-more={navMore ? "" : undefined}>
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
            ref={triggerRef}
            onClick={() => setMenuOpen((open) => !open)}
            onKeyDown={(event) => {
              // Closed, an arrow opens the menu on its first or last item.
              // Open, the menu's own key handling above moves the focus.
              if (menuOpen || (event.key !== "ArrowDown" && event.key !== "ArrowUp")) return;
              event.preventDefault();
              focusOnOpen.current = event.key === "ArrowDown" ? "first" : "last";
              setMenuOpen(true);
            }}
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
        {/* A child of the bar itself, like the menu: under 900 px it drops to
            the nav's row, which a control inside topbar-end could not do. */}
        <ChatButton />
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
