"use client";

import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";

export default function LoginPage() {
  const router = useRouter();
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setLoading(true);
    const data = new FormData(event.currentTarget);

    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          organization_slug: data.get("organization_slug"),
          email: data.get("email"),
          password: data.get("password"),
        }),
      });
      const payload = await response.json().catch(() => null);

      if (!response.ok) {
        setError(payload?.detail ?? "Unable to sign in. Please try again.");
        return;
      }
      if (payload.requires_2fa_setup) router.replace("/2fa/setup");
      else if (payload.requires_2fa) router.replace("/2fa/verify");
      else router.replace("/");
      router.refresh();
    } catch {
      setError("Unable to reach the authentication service. Please try again.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="login-shell">
      <section className="login-card card" aria-labelledby="login-title">
        <div className="login-mark" aria-hidden="true">AI</div>
        <p className="login-eyebrow">AI Data Operations</p>
        <h1 id="login-title">Welcome back</h1>
        <p className="login-copy">Sign in to continue to your workspace.</p>

        <form className="login-form" onSubmit={submit}>
          <label>
            Organization
            <input className="ops-input" name="organization_slug" autoComplete="organization" placeholder="acme-corp" required disabled={loading} />
          </label>
          <label>
            Email
            <input className="ops-input" name="email" type="email" autoComplete="email" placeholder="you@company.com" required disabled={loading} />
          </label>
          <label>
            Password
            <input className="ops-input" name="password" type="password" autoComplete="current-password" required disabled={loading} />
          </label>

          {error && <div className="login-error" role="alert">{error}</div>}
          <button className="glow-button login-submit" type="submit" disabled={loading}>
            {loading ? <><span className="login-spinner" aria-hidden="true" /> Signing in…</> : "Sign in"}
          </button>
        </form>
        <p className="auth-switch">New here? <a href="/signup">Create an account</a></p>
      </section>
    </main>
  );
}
