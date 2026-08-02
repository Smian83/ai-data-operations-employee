"use client";

import { usePathname, useRouter } from "next/navigation";
import { useState } from "react";

export default function LogoutButton() {
  const pathname = usePathname();
  const router = useRouter();
  const [loading, setLoading] = useState(false);

  if (pathname === "/login" || pathname === "/signup" || pathname.startsWith("/2fa/")) return null;

  async function logout() {
    setLoading(true);
    try {
      await fetch("/api/auth/logout", { method: "POST" });
    } finally {
      router.replace("/login");
      router.refresh();
    }
  }

  return (
    <button className="logout-button" type="button" onClick={logout} disabled={loading}>
      {loading ? "Signing out…" : "Log out"}
    </button>
  );
}
