"use client";

// Tailscale enrolment. This is the recommended way to reach FrameFound from
// outside the house: no forwarded ports, no certificate to manage, nothing
// exposed to the internet, and it works behind CGNAT.
//
// The address shown here is *learned*, not configured — recorded the first
// time a request actually arrives over the tailnet. So it is never wrong: it
// cannot be displayed until it has been demonstrated to work.

import { useEffect, useState } from "react";

import { api, type RemoteAccess } from "@/lib/api";
import { shortDate } from "@/lib/format";

const STEPS = [
  {
    title: "Install Tailscale on this server",
    body: "curl -fsSL https://tailscale.com/install.sh | sh",
    note: "Then run `sudo tailscale up`. Running it on the host rather than in a container means access survives the stack being down — which is exactly when you want to reach it.",
  },
  {
    title: "Install Tailscale on your phone or laptop",
    body: "From the app store, or tailscale.com/download",
    note: "Sign in with the same account. Both devices join the same private network.",
  },
  {
    title: "Open FrameFound on its tailnet address",
    body: "http://framefound",
    note: "Once you have done that once, the exact address appears below and you can bookmark it.",
  },
];

export default function TailnetCard() {
  const [status, setStatus] = useState<RemoteAccess | null>(null);

  useEffect(() => {
    api.remoteAccess().then(setStatus).catch(() => setStatus(null));
  }, []);

  if (!status) return null;

  const connected = status.on_tailnet_now;
  const known = Boolean(status.tailnet_host);

  return (
    <>
      <div className="sectionhead">
        <h2>Private access (Tailscale)</h2>
        <span className="pill" data-tone={connected ? "ok" : undefined}>
          {connected ? "you are on the tailnet" : "not connected"}
        </span>
      </div>

      <div className="card">
        <p className="faint" style={{ marginTop: 0, fontSize: "0.86rem" }}>
          The safest way to reach FrameFound from outside. Nothing is exposed to
          the internet, no router ports are forwarded, and it works behind
          carrier-grade NAT where a public domain cannot. Only devices you
          enrol can connect — the app still asks for a password on top.
        </p>

        {known ? (
          <>
            <div className="field">
              <span>Your address</span>
              <div className="pathbox">
                <span>{status.tailnet_url}</span>
                <button
                  className="btn"
                  style={{ padding: "3px 10px" }}
                  onClick={() => navigator.clipboard?.writeText(status.tailnet_url)}
                >
                  Copy
                </button>
              </div>
              <small className="faint">
                Confirmed working {shortDate(status.tailnet_seen_at)} — this was
                recorded from a real connection, not guessed from settings.
              </small>
            </div>
          </>
        ) : (
          <ol style={{ paddingLeft: 20, margin: "8px 0 0" }}>
            {STEPS.map((step) => (
              <li key={step.title} style={{ marginBottom: 14 }}>
                <strong>{step.title}</strong>
                <div className="pathbox" style={{ margin: "6px 0" }}>
                  <span>{step.body}</span>
                </div>
                <span className="faint" style={{ fontSize: "0.82rem" }}>
                  {step.note}
                </span>
              </li>
            ))}
          </ol>
        )}

        <p className="faint" style={{ fontSize: "0.82rem", marginBottom: 0 }}>
          Prefer to run it in the stack rather than on the host? Set{" "}
          <code className="mono">TAILSCALE_AUTHKEY</code> and start the profile:{" "}
          <code className="mono">docker compose --profile tailnet up -d</code>.
          Full guide in{" "}
          <a
            href="https://github.com/SixGricks/FrameFound/blob/main/docs/remote-access.md"
            target="_blank"
            rel="noreferrer noopener"
          >
            docs/remote-access.md
          </a>
          .
        </p>
      </div>
    </>
  );
}
