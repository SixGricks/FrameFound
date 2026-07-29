"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

import { api } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [totpCode, setTotpCode] = useState("");
  const [needsCode, setNeedsCode] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(email, password, needsCode ? totpCode : undefined);
      router.push("/");
    } catch (err) {
      const message = err instanceof Error ? err.message : "Sign-in failed";
      // The server asks for a second factor only after the password checks
      // out, so revealing the prompt here leaks nothing.
      if (message.toLowerCase().includes("authenticator")) {
        setNeedsCode(true);
        setError(null);
      } else {
        setError(message);
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="authpage">
      <form className="authcard" onSubmit={submit}>
        <p className="brand" style={{ fontSize: "1.5rem" }}>
          FrameFound
        </p>
        <p className="muted" style={{ margin: "10px 0 0", fontSize: "0.9rem" }}>
          Sign in to your media catalog.
        </p>

        <label className="field">
          Email
          <input
            className="input"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="username"
            required
            autoFocus
          />
        </label>
        <label className="field">
          Password
          <input
            className="input"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </label>

        {needsCode && (
          <label className="field">
            Authenticator code
            <input
              className="input mono"
              inputMode="numeric"
              autoComplete="one-time-code"
              placeholder="000000"
              value={totpCode}
              onChange={(e) => setTotpCode(e.target.value)}
              required
              autoFocus
            />
            <span className="faint" style={{ fontSize: "0.76rem" }}>
              Lost your phone? Enter one of your recovery codes.
            </span>
          </label>
        )}

        {error && (
          <p style={{ color: "var(--ember)", fontSize: "0.86rem", marginBottom: 0 }}>{error}</p>
        )}

        <button
          className="btn btn-primary"
          style={{ width: "100%", marginTop: 22, padding: "12px" }}
          disabled={busy}
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
