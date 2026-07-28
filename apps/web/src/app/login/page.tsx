"use client";

// Minimal session login. The real design system arrives in M6 — this page
// exists so the M3 proof of concept is usable end-to-end in a browser.

import { useState } from "react";
import { useRouter } from "next/navigation";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const resp = await fetch("/api/v1/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    setBusy(false);
    if (resp.ok) {
      router.push("/browse");
    } else {
      const body = await resp.json().catch(() => null);
      setError(body?.error?.message ?? "Sign-in failed");
    }
  }

  return (
    <main style={styles.page}>
      <form onSubmit={submit} style={styles.card}>
        <h1 style={{ margin: 0, fontSize: 22 }}>FrameFound</h1>
        <p style={{ color: "#8b95a5", marginTop: 4 }}>Sign in to your media catalog</p>
        <label style={styles.label}>
          Email
          <input
            style={styles.input}
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="username"
            required
          />
        </label>
        <label style={styles.label}>
          Password
          <input
            style={styles.input}
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </label>
        {error && <p style={{ color: "#ff6b6b", margin: "8px 0 0" }}>{error}</p>}
        <button style={styles.button} disabled={busy} type="submit">
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}

const styles: Record<string, React.CSSProperties> = {
  page: {
    minHeight: "100dvh",
    display: "grid",
    placeItems: "center",
    background: "#0f1420",
    color: "#e8ecf3",
    fontFamily: "system-ui, sans-serif",
  },
  card: {
    background: "#161d2e",
    padding: "32px",
    borderRadius: 12,
    width: "min(360px, 90vw)",
    display: "flex",
    flexDirection: "column",
    gap: 12,
  },
  label: { display: "flex", flexDirection: "column", gap: 4, fontSize: 14 },
  input: {
    padding: "10px 12px",
    borderRadius: 8,
    border: "1px solid #2a3550",
    background: "#0f1420",
    color: "#e8ecf3",
    fontSize: 15,
  },
  button: {
    marginTop: 8,
    padding: "10px 12px",
    borderRadius: 8,
    border: "none",
    background: "#4a9eff",
    color: "#08101f",
    fontWeight: 600,
    fontSize: 15,
    cursor: "pointer",
  },
};
