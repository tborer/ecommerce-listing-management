"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { api, ApiError } from "@/lib/api";

function LoginForm() {
  const params = useSearchParams();
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [signupAllowed, setSignupAllowed] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<{ signup_allowed: boolean }>("/auth/config")
      .then((c) => setSignupAllowed(c.signup_allowed))
      .catch(() => {});
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api(`/auth/${mode}`, { method: "POST", json: { email, password } });
      const next = params.get("next");
      // Only same-site paths: "//host" and "/\\host" would leave the site.
      window.location.href = next && /^\/(?![\/\\])/.test(next) ? next : "/";
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong");
      setBusy(false);
    }
  }

  return (
    <div className="auth-box panel">
      <h1>{mode === "login" ? "Log in" : "Create account"}</h1>
      <p className="muted">Listing Manager</p>
      <form className="stack" onSubmit={submit}>
        <label className="field">
          Email
          <input type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
        <label className="field">
          Password
          <input
            type="password"
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            minLength={8}
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          {mode === "signup" && <span className="hint">At least 8 characters.</span>}
        </label>
        {error && <div className="notice bad">{error}</div>}
        <button className="btn primary" disabled={busy}>
          {busy ? "…" : mode === "login" ? "Log in" : "Create account"}
        </button>
      </form>
      {signupAllowed && (
        <p className="small muted" style={{ marginTop: 12 }}>
          {mode === "login" ? "No account yet? " : "Already have an account? "}
          <a href="#" onClick={(e) => { e.preventDefault(); setMode(mode === "login" ? "signup" : "login"); }}>
            {mode === "login" ? "Create one" : "Log in"}
          </a>
        </p>
      )}
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense>
      <LoginForm />
    </Suspense>
  );
}
