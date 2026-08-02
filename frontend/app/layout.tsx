import type { Metadata } from "next";
import "./globals.css";
import LogoutButton from "./components/LogoutButton";

export const metadata: Metadata = {
  title: "AI Data Operations Employee",
  description: "Clean, validate, standardize, and improve business data.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}<LogoutButton /></body>
    </html>
  );
}
