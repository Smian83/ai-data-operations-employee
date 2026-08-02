import { NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, PRE_AUTH_COOKIE_NAME, authCookieOptions, backendUrl } from "../../../lib/auth";

export async function POST(request: Request) {
  try {
    const body = await request.json();
    const response = await fetch(backendUrl("/auth/register"), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body), cache: "no-store",
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) return NextResponse.json({ detail: payload?.detail ?? "Unable to create your account." }, { status: response.status });
    if (typeof payload?.pre_auth_token !== "string" || !payload.requires_2fa_setup) {
      return NextResponse.json({ detail: "The server returned an invalid registration response." }, { status: 502 });
    }
    const result = NextResponse.json({ requires_2fa_setup: true });
    result.cookies.set(PRE_AUTH_COOKIE_NAME, payload.pre_auth_token, authCookieOptions);
    result.cookies.set(AUTH_COOKIE_NAME, "", { ...authCookieOptions, maxAge: 0 });
    return result;
  } catch {
    return NextResponse.json({ detail: "The authentication service is unavailable." }, { status: 503 });
  }
}
