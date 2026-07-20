"""
Shared validation helpers for Module 3 request schemas: the secret-key
denylist for connection_metadata, and name normalization.
"""

# Case-insensitive substring match against JSON keys, checked recursively
# through nested dicts/lists. Best-effort static check, not a security
# guarantee — real credential storage belongs in a future encrypted
# secrets module.
SECRET_KEY_DENYLIST = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "accesskey",
    "private_key",
    "privatekey",
    "client_secret",
    "credential",
    "credentials",
    "ssh_key",
    "cert",
    "certificate",
    "pem",
)


def find_secret_like_key(value: object, _path: str = "") -> str | None:
    """Recursively search a JSON-like structure for a key that looks like a
    secret. Returns the dotted path to the first offending key, or None."""
    if isinstance(value, dict):
        for key, sub_value in value.items():
            key_lower = str(key).lower()
            current_path = f"{_path}.{key}" if _path else str(key)
            if any(bad in key_lower for bad in SECRET_KEY_DENYLIST):
                return current_path
            found = find_secret_like_key(sub_value, current_path)
            if found:
                return found
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found = find_secret_like_key(item, f"{_path}[{index}]")
            if found:
                return found
    return None


def normalize_name(name: str) -> str:
    """Trim whitespace. Casing is preserved for display — uniqueness
    checks are case-insensitive at the query/index layer, not here."""
    return name.strip()
