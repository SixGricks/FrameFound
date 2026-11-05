"use client";

// Security & remote access, written for a small-business owner rather than a
// sysadmin. It opens with a checkup — each line a plain sentence about one
// thing, with the button that fixes it — then four sections reached from a
// jump bar: getting in from away, signing in, devices and tokens, and the
// outside services FrameFound may talk to. The rarely used access modes
// (own domain, Cloudflare) live under "Advanced"; the kill switch never does.

import { useCallback, useEffect, useState } from "react";
import qrcode from "qrcode-generator";

import AiEditCard from "@/components/AiEditCard";
import GdriveCard from "@/components/GdriveCard";
import MapsSettingsCard from "@/components/MapsSettingsCard";
import PanelTokensCard from "@/components/PanelTokensCard";
import Shell from "@/components/Shell";
import TailnetCard from "@/components/TailnetCard";
import {
  api,
  type AiEditSettings,
  type AuthSession,
  type PanelToken,
  type RemoteAccess,
  type TailscaleState,
  type User,
} from "@/lib/api";
import { copyText } from "@/lib/clipboard";
import { relativeTime } from "@/lib/format";
import { usePageTitle } from "@/lib/hooks";

const MODES = [
  {
    id: "local",
    title: "Office network only",
    blurb: "Reachable on the office network (and over Tailscale). Nothing is exposed to the internet.",
  },
  {
    id: "domain",
    title: "Your own domain",
    blurb: "Public HTTPS with automatic certificates. Needs DNS and router port forwarding.",
  },
  {
    id: "tunnel",
    title: "Cloudflare Tunnel",
    blurb: "No port forwarding, but traffic passes through Cloudflare's network.",
  },
] as const;

const CONNECTION_LABEL: Record<string, string> = {
  local: "this server itself",
  lan: "the office network",
  tailnet: "Tailscale",
  internet: "the internet",
  unknown: "an unrecognised network",
};

const SECTIONS = [
  { id: "away", label: "Access from away" },
  { id: "signin", label: "Signing in" },
  { id: "devices", label: "Devices & tokens" },
  { id: "services", label: "Connected services" },
];

/** "Chrome on Windows" from a user-agent string; the raw string as a title. */
function deviceName(ua: string | null): string {
  if (!ua) return "Unknown device";
  const browser = /Edg\//.test(ua)
    ? "Edge"
    : /Firefox\//.test(ua)
      ? "Firefox"
      : /Chrome\//.test(ua)
        ? "Chrome"
        : /Safari\//.test(ua)
          ? "Safari"
          : /^curl\//.test(ua)
            ? "curl (command line)"
            : /python|httpx/i.test(ua)
              ? "A script"
              : "A browser";
  const os = /iPhone/.test(ua)
    ? "iPhone"
    : /iPad/.test(ua)
      ? "iPad"
      : /Android/.test(ua)
        ? "Android"
        : /Windows/.test(ua)
          ? "Windows"
          : /Mac OS X|Macintosh/.test(ua)
            ? "Mac"
            : /Linux/.test(ua)
              ? "Linux"
              : "";
  return os ? `${browser} on ${os}` : browser;
}

function QrCode({ text }: { text: string }) {
  const qr = qrcode(0, "M");
  qr.addData(text);
  qr.make();
  return (
    // A generated data: URL — nothing for next/image to optimise or fetch.
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={qr.createDataURL(5, 2)}
      alt="QR code for your authenticator app"
      width={qr.getModuleCount() * 5 + 20}
      style={{ background: "#fff", borderRadius: 6, padding: 4 }}
    />
  );
}

type Check = { tone: "ok" | "warn" | "bad"; text: string; action?: string; href: string };

function Checkup({ checks }: { checks: Check[] }) {
  const open = checks.filter((c) => c.tone !== "ok").length;
  return (
    <div className="card checkup">
      <p className="eyebrow" style={{ marginTop: 0 }}>
        {open === 0 ? "Everything looks good" : `${open} thing${open === 1 ? "" : "s"} worth a look`}
      </p>
      <ul>
        {checks.map((c) => (
          <li key={c.text}>
            <span className="checkup-dot" data-tone={c.tone} aria-hidden />
            <span style={{ flex: 1 }}>{c.text}</span>
            {c.action && (
              <a className="btn" style={{ padding: "3px 10px" }} href={c.href}>
                {c.action}
              </a>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function SecurityPage() {
  const [user, setUser] = useState<User | null>(null);
  const [remote, setRemote] = useState<RemoteAccess | null>(null);
  const [tailscale, setTailscale] = useState<TailscaleState | null>(null);
  const [sessions, setSessions] = useState<AuthSession[]>([]);
  const [tokens, setTokens] = useState<PanelToken[]>([]);
  const [ai, setAi] = useState<AiEditSettings | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  usePageTitle("Security");

  // TOTP enrolment
  const [password, setPassword] = useState("");
  const [enrolment, setEnrolment] = useState<{ uri: string; secret: string } | null>(null);
  const [code, setCode] = useState("");
  const [recovery, setRecovery] = useState<string[] | null>(null);
  const [showSecret, setShowSecret] = useState(false);

  // DDNS form
  const [zone, setZone] = useState("");
  const [record, setRecord] = useState("");
  const [token, setToken] = useState("");

  const load = useCallback(async () => {
    const [me, ra, ss, tk, ts, aiSettings] = await Promise.all([
      api.me(),
      api.remoteAccess(),
      api.sessions().catch(() => []),
      api.panelTokens().catch(() => []),
      api.tailscale().catch(() => null),
      api.aiEditSettings().catch(() => null),
    ]);
    setUser(me);
    setRemote(ra);
    setSessions(ss);
    setTokens(tk);
    setTailscale(ts);
    setAi(aiSettings);
    setZone(ra.ddns_zone);
    setRecord(ra.ddns_record || ra.domain);
  }, []);

  useEffect(() => {
    load().catch((e) => setError(e.message));
  }, [load]);

  function flash(message: string) {
    setNotice(message);
    setTimeout(() => setNotice(null), 4000);
  }

  // One action at a time: a double click must not submit a one-time code
  // twice or switch the access mode before the first switch has answered.
  const [busy, setBusy] = useState(false);
  async function guard(fn: () => Promise<unknown>, ok: string) {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await fn();
      await load();
      flash(ok);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  }

  if (error && !remote) return <Shell><div className="empty">{error}</div></Shell>;
  if (!remote || !user) return <Shell><div className="empty">Loading…</div></Shell>;

  const isAdmin = user.role === "admin";
  const liveTokens = tokens.filter((t) => !t.revoked);
  const checks: Check[] = [
    tailscale?.state === "connected"
      ? { tone: "ok", text: `Reachable from anywhere over Tailscale at ${tailscale.short_url}.`, href: "#away" }
      : {
          tone: "warn",
          text: "Not reachable from outside the office yet — sign the server in to Tailscale.",
          action: "Set up",
          href: "#away",
        },
    remote.public_access_enabled
      ? {
          tone: "bad",
          text: "Open to the internet. Fine with two-factor on; turn it off if you didn't mean to.",
          action: "Review",
          href: "#advanced",
        }
      : { tone: "ok", text: "Not reachable from the open internet.", href: "#away" },
    user.totp_enabled
      ? { tone: "ok", text: "Two-factor sign-in is on for your account.", href: "#signin" }
      : {
          tone: remote.public_access_enabled ? "bad" : "warn",
          text: "Two-factor sign-in is off — a stolen password is enough to get in.",
          action: "Turn on",
          href: "#signin",
        },
    {
      tone: "ok",
      text: `${sessions.length} signed-in device${sessions.length === 1 ? "" : "s"} and ${liveTokens.length} panel token${liveTokens.length === 1 ? "" : "s"}.`,
      action: "See them",
      href: "#devices",
    },
  ];
  if (ai?.configured) {
    const used = ai.monthly_budget_usd > 0 ? ai.spent_this_month_usd / ai.monthly_budget_usd : 1;
    checks.push({
      tone: used >= 1 ? "bad" : used >= 0.8 ? "warn" : "ok",
      text: `Claude API: $${ai.spent_this_month_usd.toFixed(2)} of the $${ai.monthly_budget_usd.toFixed(2)} monthly budget used.`,
      action: used >= 0.8 ? "Adjust" : undefined,
      href: "#services",
    });
  }

  return (
    <Shell>
      <div className="sectionhead" style={{ marginTop: 0 }}>
        <h2>Security &amp; remote access</h2>
        <span className="faint">
          you are connected from {CONNECTION_LABEL[remote.your_connection]}
        </span>
      </div>

      {error && <p style={{ color: "var(--ember)" }}>{error}</p>}
      {notice && <p style={{ color: "var(--sage)" }}>{notice}</p>}

      <Checkup checks={checks} />

      <nav className="jumpbar" aria-label="Sections">
        {SECTIONS.map((s) => (
          <a key={s.id} href={`#${s.id}`}>
            {s.label}
          </a>
        ))}
      </nav>

      {remote.public_access_enabled && (
        <div
          className="card"
          style={{ borderColor: "var(--amber-line)", background: "var(--amber-soft)" }}
        >
          <div style={{ display: "flex", gap: 14, alignItems: "center", flexWrap: "wrap" }}>
            <div style={{ flex: 1, minWidth: 240 }}>
              <strong>This server accepts connections from the internet.</strong>
              <p className="muted" style={{ margin: "4px 0 0", fontSize: "0.86rem" }}>
                Tailscale works without this. Turn it off unless you set up your own domain on
                purpose.
              </p>
            </div>
            <button
              className="btn"
              style={{ borderColor: "var(--ember)", color: "var(--ember)" }}
              disabled={busy || !isAdmin}
              onClick={() => guard(api.disablePublicAccess, "Public access turned off")}
            >
              Turn off internet access
            </button>
          </div>
        </div>
      )}

      {/* ---- access from away ---- */}
      <section id="away" className="secsection">
        <div className="sectionhead">
          <h2>Access from away</h2>
        </div>
        <TailnetCard isAdmin={isAdmin} />

        <details id="advanced" className="card disclosure">
          <summary>
            Advanced: open FrameFound to the internet{" "}
            <span className="faint">(own domain or Cloudflare Tunnel — most people never need this)</span>
          </summary>
          <div className="statgrid" style={{ margin: "12px 0" }}>
            {MODES.map((mode) => {
              const active =
                remote.mode === mode.id || (mode.id === "local" && remote.mode === "tailscale");
              return (
                <button
                  key={mode.id}
                  className="stat"
                  style={{
                    textAlign: "left",
                    cursor: active ? "default" : "pointer",
                    borderColor: active ? "var(--amber-line)" : undefined,
                    background: active ? "var(--amber-soft)" : undefined,
                  }}
                  disabled={busy || active || !isAdmin}
                  onClick={() => {
                    const exposes = mode.id !== "local";
                    if (
                      exposes &&
                      !window.confirm(
                        `Switch to ${mode.title}? This opens FrameFound to the internet. Turn on two-factor sign-in first.`,
                      )
                    ) {
                      return;
                    }
                    guard(
                      () =>
                        api.updateRemoteAccess({ mode: mode.id, public_access_enabled: exposes }),
                      `Switched to ${mode.title}`,
                    );
                  }}
                >
                  <p className="eyebrow" style={{ color: active ? "var(--amber)" : undefined }}>
                    {active ? "In use" : "Switch to this"}
                  </p>
                  <div style={{ fontSize: "1.02rem", margin: "6px 0 4px" }}>{mode.title}</div>
                  <p className="faint" style={{ fontSize: "0.8rem", margin: 0 }}>
                    {mode.blurb}
                  </p>
                </button>
              );
            })}
          </div>

          {remote.mode === "domain" && (
            <div style={{ display: "grid", gap: 10, maxWidth: 520 }}>
              <strong>Keep your domain pointed here</strong>
              <label className="field">
                Cloudflare zone (your domain)
                <input
                  className="input"
                  value={zone}
                  onChange={(e) => setZone(e.target.value)}
                  placeholder="example.com"
                />
              </label>
              <label className="field">
                Record to update
                <input
                  className="input"
                  value={record}
                  onChange={(e) => setRecord(e.target.value)}
                  placeholder="media.example.com"
                />
              </label>
              <label className="field">
                API token
                <input
                  className="input"
                  type="password"
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                  placeholder={remote.ddns_configured ? "•••••• (saved)" : "Zone:DNS:Edit token"}
                />
                <span className="faint" style={{ fontSize: "0.76rem" }}>
                  Use a scoped token with Zone:DNS:Edit — never your global API key.
                </span>
              </label>
              <div style={{ display: "flex", gap: 8 }}>
                <button
                  className="btn btn-primary"
                  disabled={busy}
                  onClick={() =>
                    guard(async () => {
                      await api.updateRemoteAccess({
                        ddns_provider: "cloudflare",
                        ddns_zone: zone,
                        ddns_record: record,
                        ...(token ? { ddns_token: token } : {}),
                      });
                      setToken("");
                    }, "Saved")
                  }
                >
                  Save
                </button>
                <button
                  className="btn"
                  disabled={busy}
                  onClick={() =>
                    guard(async () => {
                      const result = await api.testDns();
                      flash(result.message);
                    }, "Tested")
                  }
                >
                  Test connection
                </button>
              </div>
              {(remote.last_ipv4 || remote.last_error) && (
                <dl className="kv">
                  <dt>Public IP</dt>
                  <dd className="mono">{remote.last_ipv4 || "—"}</dd>
                  <dt>Checked</dt>
                  <dd>{relativeTime(remote.last_checked_at || null)}</dd>
                  <dt>Updated</dt>
                  <dd>{relativeTime(remote.last_updated_at || null)}</dd>
                  {remote.last_error && (
                    <>
                      <dt>Error</dt>
                      <dd style={{ color: "var(--ember)" }}>{remote.last_error}</dd>
                    </>
                  )}
                </dl>
              )}
            </div>
          )}
        </details>
      </section>

      {/* ---- signing in ---- */}
      <section id="signin" className="secsection">
        <div className="sectionhead">
          <h2>Signing in</h2>
          <span className="pill" data-tone={user.totp_enabled ? "ok" : "warn"}>
            two-factor {user.totp_enabled ? "on" : "off"}
          </span>
        </div>

        <div className="card">
          {user.totp_enabled ? (
            <>
              <p className="muted" style={{ marginTop: 0 }}>
                Signing in asks for a six-digit code from your authenticator app as well as
                your password. To turn it off, confirm both.
              </p>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                <input
                  className="input"
                  type="password"
                  placeholder="Your password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
                <input
                  className="input mono"
                  placeholder="000000"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  style={{ width: 130 }}
                />
                <button
                  className="btn"
                  disabled={busy || !password || !code}
                  onClick={() =>
                    guard(async () => {
                      await api.totpDisable(password, code);
                      setPassword("");
                      setCode("");
                    }, "Two-factor sign-in turned off")
                  }
                >
                  Turn off
                </button>
              </div>
            </>
          ) : recovery ? (
            <>
              <p style={{ marginTop: 0 }}>
                <strong>Save these recovery codes somewhere safe.</strong> Each one gets you in
                once if you lose your phone. They will not be shown again.
              </p>
              <div className="mono" style={{ display: "grid", gap: 4, margin: "12px 0" }}>
                {recovery.map((c) => (
                  <span key={c}>{c}</span>
                ))}
              </div>
              <div className="toolbar">
                <button
                  className="btn"
                  onClick={async () =>
                    flash(
                      (await copyText(recovery.join("\n")))
                        ? "Codes copied"
                        : "Couldn't copy — select them and copy by hand",
                    )
                  }
                >
                  Copy codes
                </button>
                <button className="btn btn-primary" onClick={() => setRecovery(null)}>
                  I have saved them
                </button>
              </div>
            </>
          ) : enrolment ? (
            <div style={{ display: "flex", gap: 20, flexWrap: "wrap", alignItems: "flex-start" }}>
              <QrCode text={enrolment.uri} />
              <div style={{ flex: 1, minWidth: 240 }}>
                <ol style={{ paddingLeft: 18, marginTop: 0 }}>
                  <li>
                    Open your authenticator app (Google Authenticator, 1Password, Authy…) and
                    scan this code. On this phone?{" "}
                    <a href={enrolment.uri}>Open it in the app instead</a>.
                  </li>
                  <li style={{ marginTop: 6 }}>Type the six-digit code it shows:</li>
                </ol>
                <div style={{ display: "flex", gap: 8 }}>
                  <input
                    className="input mono"
                    placeholder="000000"
                    inputMode="numeric"
                    autoComplete="one-time-code"
                    autoFocus
                    value={code}
                    onChange={(e) => setCode(e.target.value)}
                    style={{ width: 130 }}
                  />
                  <button
                    className="btn btn-primary"
                    disabled={busy || code.trim().length < 6}
                    onClick={() =>
                      guard(async () => {
                        const result = await api.totpConfirm(code);
                        setRecovery(result.recovery_codes);
                        setEnrolment(null);
                        setCode("");
                      }, "Two-factor sign-in is on")
                    }
                  >
                    Confirm
                  </button>
                </div>
                <p className="faint" style={{ fontSize: "0.8rem" }}>
                  Can&apos;t scan?{" "}
                  <button
                    className="linkish"
                    onClick={() => setShowSecret((v) => !v)}
                  >
                    {showSecret ? "Hide the key" : "Type the key in by hand"}
                  </button>
                </p>
                {showSecret && (
                  <div className="pathbox">
                    <span>{enrolment.secret}</span>
                    <button
                      className="btn"
                      style={{ padding: "4px 10px" }}
                      onClick={async () =>
                        flash(
                          (await copyText(enrolment.secret))
                            ? "Key copied"
                            : "Couldn't copy — select the key and copy it by hand",
                        )
                      }
                    >
                      Copy
                    </button>
                  </div>
                )}
              </div>
            </div>
          ) : (
            <>
              <p className="muted" style={{ marginTop: 0 }}>
                Two-factor sign-in asks for a code from your phone as well as your password, so
                a leaked password alone can&apos;t get anyone in. It takes about a minute.
              </p>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                <input
                  className="input"
                  type="password"
                  placeholder="Confirm your password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
                <button
                  className="btn btn-primary"
                  disabled={busy || !password}
                  onClick={() =>
                    guard(async () => {
                      const started = await api.totpStart(password);
                      setEnrolment({ uri: started.provisioning_uri, secret: started.secret });
                      setPassword("");
                    }, "Scan the code with your authenticator app")
                  }
                >
                  Turn on two-factor
                </button>
              </div>
            </>
          )}
        </div>
      </section>

      {/* ---- devices and tokens ---- */}
      <section id="devices" className="secsection">
        <div className="sectionhead">
          <h2>Devices &amp; tokens</h2>
          <button
            className="btn"
            style={{ padding: "4px 12px" }}
            disabled={busy || sessions.length < 2}
            onClick={() =>
              guard(async () => {
                const { revoked } = await api.revokeOtherSessions();
                flash(`Signed out ${revoked} other device${revoked === 1 ? "" : "s"}`);
              }, "Done")
            }
          >
            Sign out everywhere else
          </button>
        </div>
        <p className="faint" style={{ marginTop: 0, maxWidth: "62ch" }}>
          Every browser that is signed in right now. Don&apos;t recognise one? Sign it out —
          and change your password.
        </p>
        <div className="card" style={{ padding: 0, overflowX: "auto" }}>
          <table className="table">
            <thead>
              <tr>
                <th>Device</th>
                <th>From</th>
                <th>Signed in</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {sessions.map((s) => (
                <tr key={s.id}>
                  <td title={s.user_agent ?? undefined}>
                    {deviceName(s.user_agent)}
                    {s.current && (
                      <span className="pill" data-tone="ok" style={{ marginLeft: 8 }}>
                        this device
                      </span>
                    )}
                  </td>
                  <td className="mono faint">{s.ip ?? "—"}</td>
                  <td className="faint">{relativeTime(s.created_at)}</td>
                  <td style={{ textAlign: "right" }}>
                    {!s.current && (
                      <button
                        className="btn"
                        style={{ padding: "3px 10px" }}
                        disabled={busy}
                        onClick={() => guard(() => api.revokeSession(s.id), "Signed out")}
                      >
                        Sign out
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <PanelTokensCard />
      </section>

      {/* ---- connected services ---- */}
      <section id="services" className="secsection">
        <div className="sectionhead">
          <h2>Connected services</h2>
        </div>
        <p className="faint" style={{ marginTop: 0, maxWidth: "62ch" }}>
          Optional outside services. Each is off until you add its key here, and each says
          exactly what it sends before you turn it on.
        </p>
        <AiEditCard />
        <MapsSettingsCard />
        <GdriveCard />
      </section>
    </Shell>
  );
}
