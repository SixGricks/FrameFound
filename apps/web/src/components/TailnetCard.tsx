"use client";

// Tailscale: the way to reach FrameFound from outside the office. No
// forwarded ports, nothing exposed to the internet, works behind CGNAT.
//
// The server talks to the host's Tailscale through its socket
// (ddns/tailscale_local.py), so this card shows the real state and joining
// the tailnet is one button: it asks the host for a sign-in link and opens
// it. Signing in needs the owner's Tailscale account, so the button grants
// nothing on its own.

import { useCallback, useEffect, useRef, useState } from "react";

import { api, type TailscaleState } from "@/lib/api";
import { copyText } from "@/lib/clipboard";

function CopyRow({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="field" style={{ marginBottom: 10 }}>
      <span>{label}</span>
      <div className="pathbox">
        <span>{value}</span>
        <button
          className="btn"
          style={{ padding: "3px 10px" }}
          onClick={async () => setCopied(await copyText(value))}
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
    </div>
  );
}

const PILL: Record<string, { tone?: string; text: string }> = {
  connected: { tone: "ok", text: "connected" },
  needs_login: { tone: "warn", text: "needs sign-in" },
  needs_approval: { tone: "warn", text: "waiting for approval" },
  stopped: { tone: "warn", text: "turned off" },
  starting: { text: "starting…" },
  not_installed: { text: "not installed" },
  unavailable: { tone: "bad", text: "not answering" },
};

export default function TailnetCard({ isAdmin }: { isAdmin: boolean }) {
  const [ts, setTs] = useState<TailscaleState | null>(null);
  const [busy, setBusy] = useState(false);
  const [waiting, setWaiting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    try {
      const next = await api.tailscale();
      setTs(next);
      return next;
    } catch {
      setTs(null);
      return null;
    }
  }, []);

  useEffect(() => {
    load();
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [load]);

  // After the sign-in page opens, watch for the server joining.
  function watchForConnection() {
    setWaiting(true);
    if (timer.current) clearInterval(timer.current);
    let ticks = 0;
    timer.current = setInterval(async () => {
      ticks += 1;
      const next = await load();
      if ((next && next.state === "connected") || ticks > 100) {
        if (timer.current) clearInterval(timer.current);
        setWaiting(false);
      }
    }, 3000);
  }

  async function signIn() {
    setError(null);
    // Opened now, inside the click, so a popup blocker allows it; pointed at
    // the sign-in page once the server has the link.
    const tab = window.open("", "_blank");
    setBusy(true);
    try {
      const next = await api.tailscaleLogin();
      setTs(next);
      if (next.login_url) {
        if (tab) tab.location.href = next.login_url;
        else window.location.href = next.login_url;
        watchForConnection();
      } else {
        tab?.close();
      }
    } catch (err) {
      tab?.close();
      setError(err instanceof Error ? err.message : "Could not reach Tailscale");
    } finally {
      setBusy(false);
    }
  }

  if (!ts) return null;
  const pill = PILL[ts.state] ?? { text: ts.state };

  return (
    <div className="card">
      <div className="sectionhead" style={{ marginTop: 0 }}>
        <h3 style={{ margin: 0 }}>Reach FrameFound away from the office</h3>
        <span className="pill" data-tone={pill.tone}>
          Tailscale · {pill.text}
        </span>
      </div>

      {ts.state === "connected" ? (
        <>
          <p className="faint" style={{ marginTop: 4, fontSize: "0.86rem" }}>
            This server is on your private Tailscale network
            {ts.tailnet ? ` (${ts.tailnet})` : ""}. On your phone or laptop,
            install Tailscale, sign in with the same account, then open the
            address below. Nothing is exposed to the internet.
          </p>
          <CopyRow label="Your address, from anywhere" value={ts.short_url || ts.url} />
          {ts.url && ts.url !== ts.short_url && <CopyRow label="Full address" value={ts.url} />}
          {ts.mcp_url && <CopyRow label="For Claude (MCP connector)" value={ts.mcp_url} />}
          <div className="toolbar" style={{ marginBottom: 0 }}>
            <a className="btn" href={ts.download} target="_blank" rel="noreferrer noopener">
              Get Tailscale for your phone or laptop
            </a>
            <a className="btn" href={ts.admin_console} target="_blank" rel="noreferrer noopener">
              Manage devices
            </a>
          </div>
        </>
      ) : ts.state === "needs_login" || ts.state === "stopped" || ts.state === "starting" ? (
        <>
          <p className="faint" style={{ marginTop: 4, fontSize: "0.86rem" }}>
            {ts.state === "stopped"
              ? "Tailscale is installed on this server but turned off."
              : "Tailscale is installed on this server. Sign it in to your Tailscale account and you can open FrameFound from anywhere — no router changes, nothing exposed to the internet."}
          </p>
          <button
            className="btn btn-primary"
            style={{ fontSize: "1rem", padding: "10px 18px" }}
            disabled={busy || !isAdmin}
            onClick={signIn}
          >
            {busy
              ? "Opening the sign-in page…"
              : ts.state === "stopped"
                ? "Turn Tailscale on"
                : "Sign in to Tailscale"}
          </button>
          {!isAdmin && (
            <p className="faint" style={{ fontSize: "0.8rem" }}>
              An administrator account is needed to sign the server in.
            </p>
          )}
          {waiting && (
            <p className="faint" style={{ fontSize: "0.82rem" }}>
              Finish signing in on the page that opened. This card updates by itself
              once the server has joined.{" "}
              {ts.login_url && (
                <a href={ts.login_url} target="_blank" rel="noreferrer noopener">
                  Open the sign-in page again
                </a>
              )}
            </p>
          )}
        </>
      ) : ts.state === "needs_approval" ? (
        <>
          <p className="faint" style={{ marginTop: 4, fontSize: "0.86rem" }}>
            Signed in. Your tailnet asks an administrator to approve new devices
            — approve “{ts.host_name || "framefound"}” in the Tailscale admin console.
          </p>
          <a className="btn btn-primary" href={ts.admin_console} target="_blank" rel="noreferrer noopener">
            Open the Tailscale admin console
          </a>
        </>
      ) : (
        <>
          <p className="faint" style={{ marginTop: 4, fontSize: "0.86rem" }}>
            {ts.state === "unavailable"
              ? ts.message
              : "Tailscale isn't installed on this server yet. Run this once on the server, then come back here to sign it in:"}
          </p>
          {ts.state === "not_installed" && (
            <CopyRow
              label="On the server"
              value="curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up --hostname framefound && sudo tailscale set --operator=$(id -un 1000)"
            />
          )}
        </>
      )}

      {error && (
        <p style={{ color: "var(--ember)", fontSize: "0.86rem", marginBottom: 0 }}>{error}</p>
      )}
    </div>
  );
}
