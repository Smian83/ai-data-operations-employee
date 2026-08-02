"use client";

import { FormEvent, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import RecoveryCodes from "./RecoveryCodes";

type Status = { enabled: boolean; required: boolean; recovery_codes_remaining: number };

export default function TwoFactorSettings() {
  const router = useRouter();
  const [status, setStatus] = useState<Status | null>(null);
  const [action, setAction] = useState<"regenerate" | "disable" | null>(null);
  const [codes, setCodes] = useState<string[] | null>(null);
  const [message, setMessage] = useState("");

  async function load() {
    const response = await fetch("/api/auth/2fa/status");
    if (response.ok) setStatus(await response.json());
  }
  useEffect(() => { void load(); }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!action) return;
    setMessage("");
    const data = new FormData(event.currentTarget);
    const path = action === "regenerate" ? "recovery-codes/regenerate" : "disable";
    const response = await fetch(`/api/auth/2fa/${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: data.get("password"), code: data.get("code") }),
    });
    const body = await response.json().catch(() => null);
    if (!response.ok) {
      setMessage(body?.detail ?? "Request failed.");
      return;
    }
    if (body.recovery_codes) setCodes(body.recovery_codes);
    else {
      router.replace("/login");
      router.refresh();
    }
    event.currentTarget.reset();
  }

  if (!status) return <p className="text-xs text-slate-500">Loading two-factor security…</p>;
  if (codes) return <RecoveryCodes codes={codes} onContinue={() => { setCodes(null); setAction(null); void load(); }} />;

  return <div className="two-factor-settings">
    <div className="security-status"><span className={status.enabled ? "status-dot enabled" : "status-dot"}/><div><p>{status.enabled ? "Authenticator app enabled" : "Authenticator app not enabled"}</p><span>{status.required ? "Required for organization administrators" : `${status.recovery_codes_remaining} unused recovery codes`}</span></div></div>
    {!status.enabled ? <button className="ops-action" type="button" onClick={() => router.push("/2fa/setup")}>Set up authenticator app</button> : <div className="auth-actions"><button className="ops-action" type="button" onClick={() => setAction("regenerate")}>Regenerate recovery codes</button>{!status.required && <button className="ops-action ops-action-red" type="button" onClick={() => setAction("disable")}>Disable 2FA</button>}</div>}
    {action && <form className="manage-2fa-form" onSubmit={submit}><p>Confirm your password and current authenticator code to {action === "disable" ? "disable 2FA" : "replace all recovery codes"}.</p><input className="ops-input" name="password" type="password" placeholder="Password" required/><input className="ops-input" name="code" inputMode="numeric" pattern="[0-9]{6}" maxLength={6} placeholder="6-digit code" required/>{message && <div className="login-error">{message}</div>}<div className="auth-actions"><button className="glow-button login-submit" type="submit">Confirm</button><button className="ops-action" type="button" onClick={() => setAction(null)}>Cancel</button></div></form>}
  </div>;
}
