"""
Tests for registration, login, and /auth/me.

Covers the tenant-isolation guarantees explicitly required for Module 2:
- same normalized email can exist in two different orgs
- same normalized email cannot exist twice in one org
- login only succeeds with the correct organization_slug
- a token's org_id must match the loaded user's organization_id
- inactive users cannot log in or access /auth/me
- password hashes are never exposed in responses
"""
from fastapi.testclient import TestClient


def _register(client: TestClient, **overrides) -> dict:
    payload = {
        "organization_name": "Acme Corp",
        "email": "Owner@Example.com ",  # deliberately unnormalized
        "password": "correct-horse",
        "full_name": "Owner Person",
    }
    payload.update(overrides)
    return client.post("/auth/register", json=payload)


# --- Registration ------------------------------------------------------------


def test_register_creates_org_and_returns_token(client: TestClient) -> None:
    resp = _register(client)
    assert resp.status_code == 201
    body = resp.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"
    assert "password" not in body
    assert "hashed_password" not in body


def test_register_normalizes_email_and_derives_slug(client: TestClient) -> None:
    resp = _register(client, organization_name="Acme Corp")
    assert resp.status_code == 201
    token = resp.json()["access_token"]

    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "owner@example.com"  # lowercased + trimmed


def test_register_duplicate_slug_rejected_409(client: TestClient) -> None:
    first = _register(client, organization_name="Acme Corp", email="a@example.com")
    assert first.status_code == 201

    second = _register(client, organization_name="Acme Corp", email="b@example.com")
    assert second.status_code == 409


def test_register_explicit_slug_must_be_canonical(client: TestClient) -> None:
    resp = _register(client, organization_slug="Not Valid!!")
    assert resp.status_code == 422


def test_register_password_too_short_rejected(client: TestClient) -> None:
    resp = _register(client, password="short")
    assert resp.status_code == 422


def test_register_password_exceeding_72_bytes_rejected(client: TestClient) -> None:
    # 73 'a' characters = 73 bytes in UTF-8
    resp = _register(client, password="a" * 73)
    assert resp.status_code == 422


def test_register_password_never_truncated(client: TestClient) -> None:
    """A password that's exactly at the boundary must be usable in full for
    login — proves we never silently truncate."""
    password = "a" * 72  # exactly 72 bytes, the bcrypt limit
    resp = _register(client, organization_name="Boundary Co", password=password)
    assert resp.status_code == 201

    login_resp = client.post(
        "/auth/login",
        json={
            "organization_slug": "boundary-co",
            "email": "owner@example.com",
            "password": password,
        },
    )
    assert login_resp.status_code == 200


# --- Tenant isolation ----------------------------------------------------------


def test_same_email_allowed_across_different_orgs(client: TestClient) -> None:
    r1 = _register(client, organization_name="Org One", email="shared@example.com")
    r2 = _register(client, organization_name="Org Two", email="shared@example.com")
    assert r1.status_code == 201
    assert r2.status_code == 201


def test_same_email_rejected_within_same_org(client: TestClient) -> None:
    r1 = _register(client, organization_name="Solo Org", email="dup@example.com")
    assert r1.status_code == 201

    r2 = client.post(
        "/auth/register",
        json={
            "organization_name": "Solo Org 2",
            "organization_slug": "solo-org",  # force same org slug
            "email": "dup@example.com",
            "password": "correct-horse",
        },
    )
    # Same slug -> same org -> 409 before we ever get to the email collision,
    # which is the correct behavior (slug collision is checked first).
    assert r2.status_code == 409


def test_same_email_rejected_twice_in_same_org_at_db_level(
    client: TestClient, db_session
) -> None:
    """The register endpoint only ever creates one user per (new) org, so
    the (organization_id, email) uniqueness constraint itself is exercised
    directly at the DB layer here."""
    from sqlalchemy.exc import IntegrityError

    from app.core.security import hash_password
    from app.models.organization import Organization
    from app.models.user import User

    org = Organization(name="Constraint Org", slug="constraint-org")
    db_session.add(org)
    db_session.commit()
    db_session.refresh(org)

    user1 = User(
        organization_id=org.id,
        email="dup@example.com",
        hashed_password=hash_password("correct-horse"),
    )
    db_session.add(user1)
    db_session.commit()

    user2 = User(
        organization_id=org.id,
        email="dup@example.com",
        hashed_password=hash_password("correct-horse"),
    )
    db_session.add(user2)
    try:
        db_session.commit()
        assert False, "expected IntegrityError for duplicate (org_id, email)"
    except IntegrityError:
        db_session.rollback()


def test_login_requires_correct_organization_slug(client: TestClient) -> None:
    _register(client, organization_name="Right Org", email="user@example.com")

    wrong_org_login = client.post(
        "/auth/login",
        json={
            "organization_slug": "does-not-exist",
            "email": "user@example.com",
            "password": "correct-horse",
        },
    )
    assert wrong_org_login.status_code == 401

    right_org_login = client.post(
        "/auth/login",
        json={
            "organization_slug": "right-org",
            "email": "user@example.com",
            "password": "correct-horse",
        },
    )
    assert right_org_login.status_code == 200


def test_login_wrong_password_rejected(client: TestClient) -> None:
    _register(client, organization_name="Pw Org", email="user@example.com")
    resp = client.post(
        "/auth/login",
        json={
            "organization_slug": "pw-org",
            "email": "user@example.com",
            "password": "wrong-password",
        },
    )
    assert resp.status_code == 401


def test_login_nonexistent_user_rejected(client: TestClient) -> None:
    _register(client, organization_name="Real Org")
    resp = client.post(
        "/auth/login",
        json={
            "organization_slug": "real-org",
            "email": "nobody@example.com",
            "password": "correct-horse",
        },
    )
    assert resp.status_code == 401


# --- Token / /auth/me ----------------------------------------------------------


def test_me_requires_token(client: TestClient) -> None:
    resp = client.get("/auth/me")
    assert resp.status_code == 401


def test_me_rejects_garbage_token(client: TestClient) -> None:
    resp = client.get("/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401


def test_me_returns_current_user_with_valid_token(client: TestClient) -> None:
    reg = _register(client, organization_name="Me Org", email="me@example.com")
    token = reg.json()["access_token"]

    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "me@example.com"
    assert "hashed_password" not in body
    assert "password" not in body


def test_token_org_id_must_match_loaded_user_organization(
    client: TestClient, db_session
) -> None:
    """If a user's organization_id changes after a token was issued, the
    token must be rejected — the org_id embedded in the token is checked
    against the freshly loaded user, not just trusted."""
    from app.models.organization import Organization
    from app.models.user import User
    from app.core.security import create_access_token

    reg = _register(client, organization_name="Mismatch Org", email="mismatch@example.com")
    assert reg.status_code == 201

    user = db_session.query(User).filter(User.email == "mismatch@example.com").one()
    other_org = Organization(name="Other Org", slug="other-org-mismatch")
    db_session.add(other_org)
    db_session.commit()
    db_session.refresh(other_org)

    # Craft a token asserting a DIFFERENT org_id than the user actually has.
    tampered_token = create_access_token(subject=user.id, organization_id=other_org.id)

    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {tampered_token}"})
    assert resp.status_code == 401


# --- Inactive users ----------------------------------------------------------


def test_inactive_user_cannot_login(client: TestClient, db_session) -> None:
    from app.models.user import User

    reg = _register(client, organization_name="Inactive Org", email="inactive@example.com")
    assert reg.status_code == 201

    user = db_session.query(User).filter(User.email == "inactive@example.com").one()
    user.is_active = False
    db_session.commit()

    resp = client.post(
        "/auth/login",
        json={
            "organization_slug": "inactive-org",
            "email": "inactive@example.com",
            "password": "correct-horse",
        },
    )
    assert resp.status_code == 403


def test_inactive_user_cannot_access_me(client: TestClient, db_session) -> None:
    from app.models.user import User

    reg = _register(client, organization_name="Inactive Org 2", email="inactive2@example.com")
    token = reg.json()["access_token"]

    user = db_session.query(User).filter(User.email == "inactive2@example.com").one()
    user.is_active = False
    db_session.commit()

    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


# --- No password/hash leakage anywhere ----------------------------------------


def test_password_hash_never_in_any_auth_response_body(client: TestClient) -> None:
    reg = _register(client, organization_name="Leak Check Org", email="leak@example.com")
    token = reg.json()["access_token"]

    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    for resp in (reg, me):
        text = resp.text.lower()
        assert "hashed_password" not in text
        assert "correct-horse" not in text  # the plaintext password itself
