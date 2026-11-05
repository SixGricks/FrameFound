"use client";

// Panel tokens: the credentials the Premiere panel, the Lightroom plugin and
// the Claude (MCP) connector use.
//
// Listed next to sessions because they are the same thing from the operator's
// point of view — a way in that is currently open. The two facts that make the
// list worth reading are the *prefix* (so four tokens can be told apart) and
// *last used* (so a token nobody has touched in six months can be revoked
// without wondering what it would break).
//
// The secret is shown exactly once, and the copy affordance is deliberately
// prominent: a token the operator fails to copy is one they will mint again,
// leaving a dead credential in the list forever. For a Claude token the
// one-time card also carries the command that connects Claude Code, with the
// token already in it, because that is the only moment it can.

import { useCallback, useEffect, useState } from "react";

import { api, type PanelToken, type PanelTokenCreated } from "@/lib/api";
import { copyText } from "@/lib/clipboard";
import { relativeTime, shortDate } from "@/lib/format";

const HOSTS = [
  { value: "premiere", label: "Adobe Premiere Pro panel" },
  { value: "lightroom", label: "Lightroom Classic plugin" },
  { value: "mcp", label: "Claude (MCP connector)" },
  { value: "other", label: "Something else" },
];

function mcpUrl(): string {
  if (typeof window === "undefined") return "/api/mcp";
  return `${window.location.origin}/api/mcp`;
}

function claudeCodeCommand(token: string): string {
  return (
    `claude mcp add --transport http framefound ${mcpUrl()} ` +
    `--header "Authorization: Bearer ${token}"`
  );
}

export default function PanelTokensCard() {
  const [tokens, setTokens] = useState<PanelToken[] | null>(null);
  const [name, setName] = useState("");
  const [host, setHost] = useState("premiere");
  const [canExport, setCanExport] = useState(true);
  const [canTag, setCanTag] = useState(false);
  const [fresh, setFresh] = useState<PanelTokenCreated | null>(null);
  const [busy, setBusy] = useState(false);
  // Whether the shown-once token reached the clipboard: null until tried.
  const [copied, setCopied] = useState<boolean | null>(null);
  const [copiedCommand, setCopiedCommand] = useState<boolean | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setTokens(await api.panelTokens());
    } catch {
      setTokens([]);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  function chooseHost(value: string) {
    setHost(value);
    // What each client is for: a panel finds and imports footage (export);
    // Claude looks and proposes tags (tag). Defaults follow, and can be unticked.
    setCanExport(value !== "mcp");
    setCanTag(value === "mcp");
  }

  async function create() {
    if (!name.trim()) return;
    setBusy(true);
    setError("");
    try {
      const scopes = ["read"];
      if (canExport) scopes.push("export");
      if (canTag) scopes.push("tag");
      const created = await api.createPanelToken({ name: name.trim(), host, scopes });
      setFresh(created);
      setCopied(null);
      setCopiedCommand(null);
      setName("");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create that token");
    } finally {
      setBusy(false);
    }
  }

  const live = (tokens ?? []).filter((t) => !t.revoked);

  return (
    <>
      <div className="sectionhead">
        <h3>Panel tokens</h3>
        {tokens && <span className="faint mono">{live.length} active</span>}
      </div>

      <p className="faint" style={{ maxWidth: "62ch" }}>
        How the Premiere panel, the Lightroom plugin and Claude sign in. Each
        token is for one machine, is read-only unless you widen it, and can
        never write to your library or touch an original. Revoke one here and
        that machine stops working immediately.
      </p>

      {error && <div className="empty">{error}</div>}

      {fresh && (
        <div className="card" style={{ borderColor: "var(--sage)" }}>
          <strong>Copy this now — it will not be shown again.</strong>
          <p className="mono" style={{ overflowWrap: "anywhere", margin: "8px 0" }}>
            {fresh.token}
          </p>
          <div className="toolbar">
            <button
              className="btn btn-primary"
              onClick={async () => setCopied(await copyText(fresh.token))}
            >
              {copied === null ? "Copy token" : copied ? "Copied" : "Select and copy it by hand"}
            </button>
            <button className="btn" onClick={() => setFresh(null)}>
              Done
            </button>
          </div>
          {fresh.host === "mcp" ? (
            <>
              <p className="faint" style={{ margin: "12px 0 6px" }}>
                Connect Claude Code on your laptop with one command (the token is
                already in it):
              </p>
              <div className="pathbox">
                <span className="mono" style={{ overflowWrap: "anywhere" }}>
                  {claudeCodeCommand(fresh.token)}
                </span>
                <button
                  className="btn"
                  style={{ padding: "3px 10px" }}
                  onClick={async () =>
                    setCopiedCommand(await copyText(claudeCodeCommand(fresh.token)))
                  }
                >
                  {copiedCommand ? "Copied" : "Copy"}
                </button>
              </div>
              <p className="faint" style={{ fontSize: "0.82rem", marginBottom: 0 }}>
                The address is the one you are using right now — over Tailscale it is
                the tailnet address. Claude Desktop and the other ways in are in{" "}
                <a
                  href="https://github.com/SixGricks/FrameFound/blob/main/docs/mcp.md"
                  target="_blank"
                  rel="noreferrer noopener"
                >
                  docs/mcp.md
                </a>
                .
              </p>
            </>
          ) : (
            <p className="faint" style={{ marginBottom: 0 }}>
              Paste it into the panel&apos;s Settings, along with this server&apos;s
              address.
            </p>
          )}
        </div>
      )}

      <div className="card">
        <div className="field">
          <label htmlFor="pt-name">What machine is this for?</label>
          <input
            id="pt-name"
            className="input"
            value={name}
            maxLength={120}
            placeholder={host === "mcp" ? "Claude on DJ's laptop" : "Edit bay iMac"}
            onChange={(e) => setName(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="pt-host">Which application?</label>
          <select
            id="pt-host"
            className="select"
            value={host}
            onChange={(e) => chooseHost(e.target.value)}
          >
            {HOSTS.map((h) => (
              <option key={h.value} value={h.value}>
                {h.label}
              </option>
            ))}
          </select>
        </div>
        <label className="field" style={{ flexDirection: "row", gap: 8 }}>
          <input
            type="checkbox"
            checked={canExport}
            onChange={(e) => setCanExport(e.target.checked)}
          />
          <span>
            Allow export{" "}
            <span className="faint">
              — writing an FCP7 bin from Premiere, and grabbing full-size video frames for a
              Lightroom listing. Searching and adding files in place need only read.
            </span>
          </span>
        </label>
        <label className="field" style={{ flexDirection: "row", gap: 8 }}>
          <input
            type="checkbox"
            checked={canTag}
            onChange={(e) => setCanTag(e.target.checked)}
          />
          <span>
            Allow tagging{" "}
            <span className="faint">
              — Claude may queue tag <em>suggestions</em> for you to judge on the Tags page,
              and record your yes or no when you give it in chat. Never a file, never an
              original, and nothing becomes a fact until you decide.
            </span>
          </span>
        </label>
        <button className="btn btn-primary" disabled={busy || !name.trim()} onClick={create}>
          {busy ? "Creating…" : "Create token"}
        </button>
      </div>

      {tokens && tokens.length > 0 && (
        <div className="tablewrap card" style={{ padding: 0 }}>
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Machine</th>
                <th scope="col">Token</th>
                <th scope="col">Last used</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {tokens.map((token) => (
                <tr key={token.id} style={{ opacity: token.revoked ? 0.5 : 1 }}>
                  <th scope="row" style={{ fontWeight: 500 }}>
                    {token.name}
                    <span className="faint mono" style={{ display: "block" }}>
                      {token.host === "mcp" ? "claude" : token.host} · {token.scopes.join(", ")} ·
                      added {shortDate(token.created_at)}
                    </span>
                  </th>
                  <td className="mono faint">{token.prefix}…</td>
                  <td className="faint">
                    {token.last_used_at ? relativeTime(token.last_used_at) : "never"}
                    {token.last_used_ip && (
                      <span className="mono" style={{ display: "block", fontSize: "0.72rem" }}>
                        {token.last_used_ip}
                      </span>
                    )}
                  </td>
                  <td style={{ textAlign: "right" }}>
                    {token.revoked ? (
                      <span className="pill" data-tone="bad">
                        revoked
                      </span>
                    ) : (
                      <button
                        className="btn"
                        disabled={busy}
                        onClick={async () => {
                          // Irreversible, and the machine using it stops
                          // working at once: not a one-click action.
                          if (!window.confirm(`Revoke "${token.name}"? The machine using it loses access immediately.`)) {
                            return;
                          }
                          await api.revokePanelToken(token.id);
                          await load();
                        }}
                      >
                        Revoke
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {tokens && !tokens.length && (
        <div className="empty">
          No panel tokens yet. Create one to connect Premiere, Lightroom or Claude.
        </div>
      )}
    </>
  );
}
