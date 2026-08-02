import { NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, PRE_AUTH_COOKIE_NAME, authCookieOptions, backendUrl } from "../../../lib/auth";

type LoginBody = {
  organization_slug?: unknown;
  email?: unknown;
  password?: unknown;
};

export async function POST(request: Request) {
  let body: LoginBody;

  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ detail: "Enter your organization, email, and password." }, { status: 400 });
  }

  if (
    typeof body.organization_slug !== "string" ||
    typeof body.email !== "string" ||
    typeof body.password !== "string"
  ) {
    return NextResponse.json({ detail: "Enter your organization, email, and password." }, { status: 400 });
  }

  try {
    const response = await fetch(backendUrl("/auth/login"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        organization_slug: body.organization_slug,
        email: body.email,
        password: body.password,
      }),
      cache: "no-store",
    });
    const payload = await response.json().catch(() => null);

    if (!response.ok) {
      return NextResponse.json(
        { detail: payload?.detail ?? "Unable to sign in. Please try again." },
        { status: response.status },
      );
    }

    if (typeof payload?.access_token !== "string" && typeof payload?.pre_auth_token !== "string") {
      return NextResponse.json({ detail: "The server returned an invalid login response." }, { status: 502 });
    }

    const result = NextResponse.json({ authenticated: Boolean(payload.access_token), requires_2fa_setup: Boolean(payload.requires_2fa_setup), requires_2fa: Boolean(payload.requires_2fa) });
    if (payload.access_token) {
      result.cookies.set(AUTH_COOKIE_NAME, payload.access_token, authCookieOptions);
      result.cookies.set(PRE_AUTH_COOKIE_NAME, "", { ...authCookieOptions, maxAge: 0 });
    }
    if (payload.pre_auth_token) {
      result.cookies.set(PRE_AUTH_COOKIE_NAME, payload.pre_auth_token, authCookieOptions);
      result.cookies.set(AUTH_COOKIE_NAME, "", { ...authCookieOptions, maxAge: 0 });
    }
    return result;
  } catch {
    return NextResponse.json(
      { detail: "The authentication service is unavailable. Please try again." },
      { status: 503 },
    );
  }
}
