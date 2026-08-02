import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, PRE_AUTH_COOKIE_NAME, authCookieOptions, backendUrl } from "../../../../lib/auth";

async function forward(request: Request, segments: string[], method: "GET" | "POST") {
  const cookieStore = await cookies();
  const token = cookieStore.get(PRE_AUTH_COOKIE_NAME)?.value ?? cookieStore.get(AUTH_COOKIE_NAME)?.value;
  if (!token) return NextResponse.json({ detail: "Authentication is required." }, { status: 401 });
  try {
    const body = method === "POST" ? await request.text() : undefined;
    const response = await fetch(backendUrl(`/auth/2fa/${segments.join("/")}`), {
      method,
      headers: { Authorization: `Bearer ${token}`, ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body || undefined,
      cache: "no-store",
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) return NextResponse.json({ detail: payload?.detail ?? "Two-factor authentication request failed." }, { status: response.status });
    const result = NextResponse.json(payload);
    if (typeof payload?.access_token === "string") {
      result.cookies.set(AUTH_COOKIE_NAME, payload.access_token, authCookieOptions);
      result.cookies.set(PRE_AUTH_COOKIE_NAME, "", { ...authCookieOptions, maxAge: 0 });
    }
    if (segments.join("/") === "disable") {
      result.cookies.set(AUTH_COOKIE_NAME, "", { ...authCookieOptions, maxAge: 0 });
    }
    return result;
  } catch {
    return NextResponse.json({ detail: "The authentication service is unavailable." }, { status: 503 });
  }
}

export async function GET(request: Request, context: { params: Promise<{ path: string[] }> }) {
  return forward(request, (await context.params).path, "GET");
}
export async function POST(request: Request, context: { params: Promise<{ path: string[] }> }) {
  return forward(request, (await context.params).path, "POST");
}
