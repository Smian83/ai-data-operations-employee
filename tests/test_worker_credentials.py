"""Tests for app.core.encryption (round-trip, tamper detection) and the
CredentialProvider abstraction (DatabaseCredentialProvider): the engine-
facing contract is get_credentials(data_source) -> dict, backed today by
an encrypted DB table but swappable without touching engine/handler code."""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.encryption import CredentialEncryptionError, decrypt_credentials, encrypt_credentials
from app.models.data_source import DataSource
from app.worker.credentials import CredentialNotFoundError, DatabaseCredentialProvider


def _register(client: TestClient, org_name: str, email: str) -> dict:
    resp = client.post(
        "/auth/register",
        json={
            "organization_name": org_name,
            "email": email,
            "password": "correct-horse-battery",
            "full_name": "Test User",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _auth_headers(client: TestClient, org_name: str, email: str) -> dict:
    token = _register(client, org_name, email)["access_token"]
    return {"Authorization": f"Bearer {token}"}


# --- Encryption primitive -----------------------------------------------------


def test_encrypt_decrypt_round_trip() -> None:
    secret = {"username": "svc_account", "password": "hunter2"}
    encrypted = encrypt_credentials(secret)
    assert secret["password"].encode() not in encrypted  # never stored in plaintext
    assert decrypt_credentials(encrypted) == secret


def test_decrypt_tampered_payload_raises() -> None:
    encrypted = bytearray(encrypt_credentials({"a": 1}))
    encrypted[-1] ^= 0xFF  # flip a bit -> breaks the HMAC
    with pytest.raises(CredentialEncryptionError):
        decrypt_credentials(bytes(encrypted))


# --- DatabaseCredentialProvider ------------------------------------------------


def test_provider_round_trip(client: TestClient, db_session) -> None:
    headers = _auth_headers(client, "Org Cred A", "cred-a@example.com")
    ds_resp = client.post(
        "/data-sources",
        json={"name": "Warehouse", "source_type": "postgres", "connection_metadata": {"host": "db.internal"}},
        headers=headers,
    )
    ds = db_session.get(DataSource, uuid.UUID(ds_resp.json()["id"]))

    provider = DatabaseCredentialProvider(db_session)
    provider.set_credentials(ds.organization_id, ds.id, {"username": "u", "password": "p"})
    db_session.commit()

    resolved = provider.get_credentials(ds)
    assert resolved == {"username": "u", "password": "p"}


def test_provider_raises_when_no_credentials_configured(client: TestClient, db_session) -> None:
    headers = _auth_headers(client, "Org Cred B", "cred-b@example.com")
    ds_resp = client.post(
        "/data-sources",
        json={"name": "Warehouse", "source_type": "postgres", "connection_metadata": {}},
        headers=headers,
    )
    ds = db_session.get(DataSource, uuid.UUID(ds_resp.json()["id"]))

    provider = DatabaseCredentialProvider(db_session)
    with pytest.raises(CredentialNotFoundError):
        provider.get_credentials(ds)


def test_provider_set_credentials_upserts(client: TestClient, db_session) -> None:
    headers = _auth_headers(client, "Org Cred C", "cred-c@example.com")
    ds_resp = client.post(
        "/data-sources",
        json={"name": "Warehouse", "source_type": "postgres", "connection_metadata": {}},
        headers=headers,
    )
    ds = db_session.get(DataSource, uuid.UUID(ds_resp.json()["id"]))

    provider = DatabaseCredentialProvider(db_session)
    provider.set_credentials(ds.organization_id, ds.id, {"password": "first"})
    db_session.commit()
    provider.set_credentials(ds.organization_id, ds.id, {"password": "second"})
    db_session.commit()

    assert provider.get_credentials(ds) == {"password": "second"}


def test_credentials_never_appear_in_connection_metadata(client: TestClient) -> None:
    """Sanity check that Module 3's connection_metadata secret-key rejection
    still works unmodified: live credentials belong ONLY in the Module 4
    credentials table, never here."""
    headers = _auth_headers(client, "Org Cred D", "cred-d@example.com")
    resp = client.post(
        "/data-sources",
        json={"name": "Bad", "source_type": "postgres", "connection_metadata": {"password": "leaked"}},
        headers=headers,
    )
    assert resp.status_code == 422
