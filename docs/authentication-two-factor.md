# Authenticator-app two-factor authentication

## Security contract

The first user registered for an organization remains its administrator. Registration is atomic, but an administrator receives a short-lived `pre_auth_token` with `requires_2fa_setup=true`, not an API access token. A full bearer token is issued only after successful TOTP enrollment. Non-administrator users may opt into TOTP; users with TOTP enabled receive `requires_2fa=true` and a pre-authentication token after password verification.

Pre-authentication JWTs expire after 10 minutes by default, carry `token_type=pre_auth`, a limited `purpose`, and a random `jti`, and cannot pass normal bearer-token dependencies. Full access JWTs carry `token_type=access` and an `auth_version`; disabling 2FA increments the version to invalidate existing sessions.

TOTP implements RFC 6238 with SHA-1, six digits, 30-second periods, and a one-period clock-skew window. This is the interoperability profile used by mainstream authenticator apps. SMS is not used.

TOTP secrets are encrypted at rest with Fernet authenticated encryption. Production deployments must set `TOTP_ENCRYPTION_KEY` to a URL-safe base64-encoded 32-byte Fernet key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Recovery codes contain 96 random bits, are returned only when initially generated or regenerated, and are stored only as keyed HMAC-SHA256 hashes. Redemption is an atomic conditional update, so each code can succeed once. Regeneration deletes every prior recovery-code hash.

## Endpoints

All 2FA endpoints use `Authorization: Bearer <token>`. Setup and login verification use a pre-auth token; status and management use a full access token.

- `POST /auth/2fa/setup` starts enrollment and returns `secret` plus `provisioning_uri`.
- `POST /auth/2fa/setup/verify` verifies enrollment and returns the full token plus recovery codes.
- `POST /auth/2fa/verify` completes a login challenge with TOTP.
- `POST /auth/2fa/recovery` completes a login challenge with one recovery code.
- `GET /auth/2fa/status` reports enabled/required state and the unused recovery-code count.
- `POST /auth/2fa/recovery-codes/regenerate` requires the password and a current TOTP code.
- `POST /auth/2fa/disable` requires the password and a current TOTP code and is forbidden for administrators.

`POST /auth/register` and `POST /auth/login` return `access_token`, `requires_2fa_setup`, `requires_2fa`, and `pre_auth_token`. Only the token appropriate to the current authentication state is populated.

## Rate limits and audit events

Failed password, setup verification, TOTP, recovery, regeneration, and disable attempts are recorded in `auth_security_events`. Limits are evaluated from persistent audit rows by keyed identifier hash and source IP, so they survive process restarts. Defaults are five failed attempts per five minutes. Configure with `AUTH_RATE_LIMIT_ATTEMPTS` and `AUTH_RATE_LIMIT_WINDOW_MINUTES`.

Audit events never contain passwords, TOTP secrets, TOTP codes, recovery codes, or plaintext login identifiers. Login identifiers are stored only as keyed hashes.

## Frontend flow

The Next.js server stores full and pre-authentication tokens in separate HttpOnly, SameSite=Strict cookies. Browser JavaScript never receives either token. Registration routes administrators to setup; login routes challenged users to TOTP or recovery verification. Setup exposes the authenticator provisioning URI and manual secret. Recovery codes can be downloaded once as a local text file. Settings exposes status, optional setup for normal users, recovery-code regeneration, and disable controls. Administrators cannot disable 2FA.
