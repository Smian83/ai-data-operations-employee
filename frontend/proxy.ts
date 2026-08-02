import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE_NAME, PRE_AUTH_COOKIE_NAME, backendUrl } from "./app/lib/auth";

const protectedPaths = new Set([
  "/", "/upload-data", "/data-sources", "/cleaning", "/standardization",
  "/match-merge", "/validation", "/approval-queue", "/reports", "/audit-log", "/settings",
]);

export async function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;
  const token = request.cookies.get(AUTH_COOKIE_NAME)?.value;
  const preAuthToken = request.cookies.get(PRE_AUTH_COOKIE_NAME)?.value;
  let authenticated = false;

  if (token) {
    try {
      const response = await fetch(backendUrl("/auth/me"), {
        headers: { Authorization: `Bearer ${token}` },
        cache: "no-store",
      });
      authenticated = response.ok;
    } catch {
      authenticated = false;
    }
  }

  if ((pathname === "/login" || pathname === "/signup") && authenticated) {
    return NextResponse.redirect(new URL("/", request.url));
  }

  if ((pathname === "/2fa/setup" || pathname === "/2fa/verify") && !authenticated && !preAuthToken) {
    return NextResponse.redirect(new URL("/login", request.url));
  }

  if ((pathname === "/2fa/setup" || pathname === "/2fa/verify") && authenticated && pathname === "/2fa/verify") {
    return NextResponse.redirect(new URL("/", request.url));
  }

  if (protectedPaths.has(pathname) && !authenticated) {
    const response = NextResponse.redirect(new URL("/login", request.url));
    if (token) response.cookies.delete(AUTH_COOKIE_NAME);
    return response;
  }

  return NextResponse.next();
}

export const config = {
  matcher: ["/", "/login", "/signup", "/2fa/setup", "/2fa/verify", "/upload-data", "/data-sources", "/cleaning", "/standardization", "/match-merge", "/validation", "/approval-queue", "/reports", "/audit-log", "/settings"],
};
