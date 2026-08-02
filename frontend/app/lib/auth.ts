export const AUTH_COOKIE_NAME = "ai_data_access_token";
export const PRE_AUTH_COOKIE_NAME = "ai_data_pre_auth_token";

export const backendUrl = (path: string) => {
  const baseUrl = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";
  return `${baseUrl.replace(/\/$/, "")}${path}`;
};

export const authCookieOptions = {
  httpOnly: true,
  secure: process.env.NODE_ENV === "production",
  sameSite: "strict" as const,
  path: "/",
};
