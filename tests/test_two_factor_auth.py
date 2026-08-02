"""Security and lifecycle tests for authenticator-app TOTP authentication."""
from fastapi.testclient import TestClient

from app.core.security import decrypt_totp_secret, hash_recovery_code, totp_code
from app.models.auth_security import AuthRecoveryCode, AuthSecurityEvent
from app.models.user import User


def register_admin(client: TestClient, name: str = "Secure Org"):
    return client.post("/auth/register", json={
        "organization_name": name, "email": "owner@example.com",
        "password": "correct-horse", "full_name": "Owner",
    })


def setup_admin(client: TestClient, registration):
    pre_auth = registration.json()["pre_auth_token"]
    headers = {"Authorization": f"Bearer {pre_auth}"}
    setup = client.post("/auth/2fa/setup", headers=headers)
    verify = client.post("/auth/2fa/setup/verify", headers=headers, json={"code": totp_code(setup.json()["secret"])})
    return setup, verify


def test_admin_cannot_use_pre_auth_token_as_full_session(client: TestClient):
    registration = register_admin(client)
    assert registration.json()["requires_2fa_setup"] is True
    response = client.get("/auth/me", headers={"Authorization": f"Bearer {registration.json()['pre_auth_token']}"})
    assert response.status_code == 401


def test_setup_encrypts_secret_and_returns_single_view_recovery_codes(client: TestClient, db_session):
    registration = register_admin(client)
    setup, verified = setup_admin(client, registration)
    assert verified.status_code == 200
    assert len(verified.json()["recovery_codes"]) == 10
    user = db_session.query(User).filter_by(email="owner@example.com").one()
    assert user.totp_enabled is True
    assert setup.json()["secret"].encode() not in user.totp_secret_encrypted
    assert decrypt_totp_secret(user.totp_secret_encrypted) == setup.json()["secret"]
    stored = db_session.query(AuthRecoveryCode).filter_by(user_id=user.id).all()
    assert len(stored) == 10
    assert all(code.code_hash not in verified.json()["recovery_codes"] for code in stored)


def test_login_requires_totp_then_issues_full_token(client: TestClient):
    registration = register_admin(client)
    setup, _ = setup_admin(client, registration)
    login = client.post("/auth/login", json={"organization_slug":"secure-org","email":"owner@example.com","password":"correct-horse"})
    assert login.json()["requires_2fa"] is True
    assert login.json()["access_token"] is None
    verified = client.post("/auth/2fa/verify", headers={"Authorization":f"Bearer {login.json()['pre_auth_token']}"}, json={"code":totp_code(setup.json()["secret"])})
    assert verified.status_code == 200
    assert client.get("/auth/me", headers={"Authorization":f"Bearer {verified.json()['access_token']}"}).status_code == 200


def test_recovery_code_is_one_time_and_hashed(client: TestClient, db_session):
    registration = register_admin(client)
    _, verified = setup_admin(client, registration)
    code = verified.json()["recovery_codes"][0]
    login = client.post("/auth/login", json={"organization_slug":"secure-org","email":"owner@example.com","password":"correct-horse"})
    headers = {"Authorization":f"Bearer {login.json()['pre_auth_token']}"}
    first = client.post("/auth/2fa/recovery", headers=headers, json={"recovery_code":code})
    second = client.post("/auth/2fa/recovery", headers=headers, json={"recovery_code":code})
    assert first.status_code == 200
    assert second.status_code == 401
    stored = db_session.query(AuthRecoveryCode).filter_by(code_hash=hash_recovery_code(code)).one()
    db_session.refresh(stored)
    assert stored.used_at is not None


def test_admin_cannot_disable_2fa(client: TestClient):
    registration = register_admin(client)
    setup, verified = setup_admin(client, registration)
    response = client.post("/auth/2fa/disable", headers={"Authorization":f"Bearer {verified.json()['access_token']}"}, json={"password":"correct-horse","code":totp_code(setup.json()["secret"])})
    assert response.status_code == 403


def test_recovery_regeneration_invalidates_old_codes(client: TestClient, db_session):
    registration = register_admin(client)
    setup, verified = setup_admin(client, registration)
    old = verified.json()["recovery_codes"][0]
    response = client.post("/auth/2fa/recovery-codes/regenerate", headers={"Authorization":f"Bearer {verified.json()['access_token']}"}, json={"password":"correct-horse","code":totp_code(setup.json()["secret"])})
    assert response.status_code == 200
    assert old not in response.json()["recovery_codes"]
    assert db_session.query(AuthRecoveryCode).filter_by(code_hash=hash_recovery_code(old)).count() == 0


def test_totp_failures_are_rate_limited_and_audited(client: TestClient, db_session):
    registration = register_admin(client)
    setup, _ = setup_admin(client, registration)
    login = client.post("/auth/login", json={"organization_slug":"secure-org","email":"owner@example.com","password":"correct-horse"})
    headers = {"Authorization":f"Bearer {login.json()['pre_auth_token']}"}
    for _ in range(5):
        assert client.post("/auth/2fa/verify", headers=headers, json={"code":"000000"}).status_code == 401
    assert client.post("/auth/2fa/verify", headers=headers, json={"code":totp_code(setup.json()["secret"])}).status_code == 429
    assert db_session.query(AuthSecurityEvent).filter_by(event_type="2fa_verify", success=False).count() == 5


def test_normal_user_can_enable_and_disable_optional_2fa(client: TestClient, db_session):
    from app.core.security import create_access_token, hash_password
    from app.models.organization import Organization

    org = Organization(name="Member Org", slug="member-org")
    user = User(organization=org, email="member@example.com", hashed_password=hash_password("correct-horse"), is_superuser=False)
    db_session.add_all([org, user])
    db_session.commit()
    db_session.refresh(user)
    access = create_access_token(user.id, user.organization_id, auth_version=user.auth_version)
    headers = {"Authorization": f"Bearer {access}"}
    setup = client.post("/auth/2fa/setup", headers=headers)
    enabled = client.post("/auth/2fa/setup/verify", headers=headers, json={"code": totp_code(setup.json()["secret"])})
    assert enabled.status_code == 200
    enabled_headers = {"Authorization": f"Bearer {enabled.json()['access_token']}"}
    disabled = client.post("/auth/2fa/disable", headers=enabled_headers, json={"password":"correct-horse", "code":totp_code(setup.json()["secret"])})
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert client.get("/auth/me", headers=enabled_headers).status_code == 401
