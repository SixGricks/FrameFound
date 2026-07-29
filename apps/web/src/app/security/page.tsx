"use client";

// Security & Remote Access. Written for a small-business owner rather than a
// sysadmin: modes are described by consequence ("no router changes needed"),
// and the kill switch is always one click away.

import { useCallback, useEffect, useState } from "react";

import MapsSettingsCard from "@/components/MapsSettingsCard";
import TailnetCard from "@/components/TailnetCard";
import Shell from "@/components/Shell";
import { api, type AuthSession, type RemoteAccess, type User } from "@/lib/api";
import { relativeTime } from "@/lib/format";

const MODES = [
  {
    id: "local",
    title: "Local network only",
    blurb: "Reachable from this network. Nothing is exposed to the internet.",
    tone: "ok",
  },
  {
    id: "tailscale",
    title: "Private network (Tailscale)",
    blurb: "Access from anywhere on your own devices. No router changes, no open ports.",
    tone: "ok",
  },
  {
    id: "domain",
    title: "Your own domain",
    blurb: "Public HTTPS with automatic certificates. Requires DNS and port forwarding.",
    tone: "warn",
  },
  {
    id: "tunnel",
    title: "Cloudflare Tunnel",
    blurb: "No port forwarding, but traffic passes through Cloudflare's network.",
    tone: "warn",
  },
] as const;

const CONNECTION_LABEL: Record<string, string> = {
  local: "this machine",
  lan: "your local network",
  tailnet: "your private network",
  internet: "the internet",
  unknown: "an unrecognised network",
};

export default function SecurityPage() {
  const [user, setUser] = useState<User | null>(null);
  const [remote, setRemote] = useState<RemoteAccess | null>(null);
  const [sessions, setSessions] = useState<AuthSession[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // TOTP enrolment
  const [password, setPassword] = useState("");
  const [enrolment, setEnrolment] = useState<{ uri: string; secret: string } | null>(null);
  const [code, setCode] = useState("");
  const [recovery, setRecovery] = useState<string[] | null>(null);

  // DDNS form
  const [zone, setZone] = useState("");
  const [record, setRecord] = useState("");
  const [token, setToken] = useState("");

  const load = useCallback(async () => {
    const [me, ra, ss] = await Promise.all([
      api.me(),
      api.remoteAccess(),
      api.sessions().catch(() => []),
    ]);
    setUser(me);
    setRemote(ra);
    setSessions(ss);
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

  async function guard(fn: () => Promise<unknown>, ok: string) {
    setError(null);
    try {
      await fn();
      await load();
      flash(ok);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong");
    }
  }

  if (error && !remote) return <Shell><div className="empty">{error}</div></Shell>;
  if (!remote || !user) return <Shell><div className="empty">Loading…</div></Shell>;

  return (
    <Shell>
      <div className="sectionhead" style={{ marginTop: 0 }}>
        <h2>Security &amp; remote access</h2>
        <span className="faint mono">
          you are connected from {CONNECTION_LABEL[remote.your_connection]}
        </span>
      </div>

      {error && <p style={{ color: "var(--ember)" }}>{error}</p>}
      {notice && <p style={{ color: "var(--sage)" }}>{notice}</p>}

      {/* ---- access mode ---- */}
      <div className="statgrid" style={{ marginBottom: 16 }}>
        {MODES.map((mode) => {
          const active = remote.mode === mode.id;
          return (
            <button
              key={mode.id}
              className="stat"
              style={{
                textAlign: "left",
                cursor: "pointer",
                borderColor: active ? "var(--amber-line)" : undefined,
                background: active ? "var(--amber-soft)" : undefined,
              }}
              onClick={() =>
                guard(
                  () =>
                    api.updateRemoteAccess({
                      mode: mode.id,
                      public_access_enabled: mode.id === "domain" || mode.id === "tunnel",
                    }),
                  `Access mode set to ${mode.title}`,
                )
              }
            >
              <p className="eyebrow" style={{ color: active ? "var(--amber)" : undefined }}>
                {active ? "Active" : "Choose"}
              </p>
              <div style={{ fontSize: "1.02rem", margin: "6px 0 4px" }}>{mode.title}</div>
              <p className="faint" style={{ fontSize: "0.8rem", margin: 0 }}>{mode.blurb}</p>
            </button>
          );
        })}
      </div>

      {remote.public_access_enabled && (
        <div
          className="card"
          style={{ borderColor: "var(--amber-line)", background: "var(--amber-soft)" }}
        >
          <div style={{ display: "flex", gap: 14, alignItems: "center", flexWrap: "wrap" }}>
            <div style={{ flex: 1, minWidth: 240 }}>
              <strong>This server accepts connections from the internet.</strong>
              <p className="muted" style={{ margin: "4px 0 0", fontSize: "0.86rem" }}>
                Turn two-factor authentication on before leaving it exposed.
              </p>
            </div>
            <button
              className="btn"
              style={{ borderColor: "var(--ember)", color: "var(--ember)" }}
              onClick={() => guard(api.disablePublicAccess, "Public access turned off")}
            >
              Turn off public access
            </button>
          </div>
        </div>
      )}

      {/* ---- two-factor ---- */}
      <div className="sectionhead">
        <h2>Two-factor authentication</h2>
        <span className="pill" data-tone={user.totp_enabled ? "ok" : "warn"}>
          {user.totp_enabled ? "on" : "off"}
        </span>
      </div>

      <div className="card">
        {user.totp_enabled ? (
          <>
            <p className="muted" style={{ marginTop: 0 }}>
              Sign-in requires a code from your authenticator app.
            </p>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <input
                className="input"
                type="password"
                placeholder="Your password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
              <input
                className="input mono"
                placeholder="000000"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                style={{ width: 130 }}
              />
              <button
                className="btn"
                onClick={() =>
                  guard(async () => {
                    await api.totpDisable(password, code);
                    setPassword("");
                    setCode("");
                  }, "Two-factor authentication turned off")
                }
              >
                Turn off
              </button>
            </div>
          </>
        ) : recovery ? (
          <>
            <p style={{ marginTop: 0 }}>
              <strong>Save these recovery codes now.</strong> Each works once if you lose your
              phone. They will not be shown again.
            </p>
            <div className="mono" style={{ display: "grid", gap: 4, margin: "12px 0" }}>
              {recovery.map((c) => (
                <span key={c}>{c}</span>
              ))}
            </div>
            <button className="btn btn-primary" onClick={() => setRecovery(null)}>
              I have saved them
            </button>
          </>
        ) : enrolment ? (
          <>
            <p style={{ marginTop: 0 }}>
              Add this key to your authenticator app, then enter the six-digit code it shows.
            </p>
            <div className="pathbox" style={{ marginBottom: 12 }}>
              <span>{enrolment.secret}</span>
              <button
                className="btn"
                style={{ padding: "4px 10px" }}
                onClick={() => navigator.clipboard.writeText(enrolment.secret)}
              >
                Copy
              </button>
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              <input
                className="input mono"
                placeholder="000000"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                style={{ width: 130 }}
              />
              <button
                className="btn btn-primary"
                onClick={() =>
                  guard(async () => {
                    const result = await api.totpConfirm(code);
                    setRecovery(result.recovery_codes);
                    setEnrolment(null);
                    setCode("");
                  }, "Two-factor authentication is on")
                }
              >
                Confirm
              </button>
            </div>
          </>
        ) : (
          <>
            <p className="muted" style={{ marginTop: 0 }}>
              Strongly recommended before exposing this server beyond your local network.
            </p>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <input
                className="input"
                type="password"
                placeholder="Confirm your password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
              <button
                className="btn btn-primary"
                onClick={() =>
                  guard(async () => {
                    const started = await api.totpStart(password);
                    setEnrolment({ uri: started.provisioning_uri, secret: started.secret });
                    setPassword("");
                  }, "Scan the key in your authenticator app")
                }
              >
                Set up
              </button>
            </div>
          </>
        )}
      </div>

      {/* ---- dynamic DNS ---- */}
      {remote.mode === "domain" && (
        <>
          <div className="sectionhead">
            <h2>Keep your domain pointed here</h2>
            {remote.last_error ? (
              <span className="pill" data-tone="bad">error</span>
            ) : remote.ddns_configured ? (
              <span className="pill" data-tone="ok">configured</span>
            ) : null}
          </div>
          <div className="card">
            <div style={{ display: "grid", gap: 10, maxWidth: 520 }}>
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
            </div>

            {(remote.last_ipv4 || remote.last_error) && (
              <dl className="kv" style={{ marginTop: 18 }}>
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
        </>
      )}

      {/* ---- sessions ---- */}
      <TailnetCard />

      <MapsSettingsCard />

      <div className="sectionhead">
        <h2>Signed-in devices</h2>
        <button
          className="btn"
          style={{ padding: "4px 12px" }}
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
      <div className="card" style={{ padding: 0, overflowX: "auto" }}>
        <table className="table">
          <thead>
            <tr>
              <th>Device</th>
              <th>Address</th>
              <th>Signed in</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {sessions.map((s) => (
              <tr key={s.id}>
                <td>
                  {s.user_agent?.slice(0, 52) ?? "Unknown"}
                  {s.current && (
                    <span className="pill" data-tone="ok" style={{ marginLeft: 8 }}>
                      this device
                    </span>
                  )}
                </td>
                <td className="mono faint">{s.ip ?? "—"}</td>
                <td className="faint">{relativeTime(s.created_at)}</td>
                <td>
                  {!s.current && (
                    <button
                      className="btn"
                      style={{ padding: "3px 10px" }}
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
    </Shell>
  );
}
