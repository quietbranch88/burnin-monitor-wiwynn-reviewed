from datetime import UTC, datetime, timedelta

import jwt
import pytest
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models import User
from sqlalchemy import update

REGISTER = {"username": "new.engineer", "email": "new@example.com", "password": "S3cure-pass"}


async def test_register_creates_viewer_role(client):
    response = await client.post("/api/v1/auth/register", json=REGISTER)
    assert response.status_code == 201
    body = response.json()
    assert body["role"] == "viewer" and body["username"] == "new.engineer"
    assert "hashed_password" not in body and "password" not in body


async def test_register_rejects_duplicates_and_weak_input(client):
    assert (await client.post("/api/v1/auth/register", json=REGISTER)).status_code == 201
    duplicate = await client.post("/api/v1/auth/register", json=REGISTER | {"email": "other@example.com"})
    assert duplicate.status_code == 409
    short = await client.post("/api/v1/auth/register", json=REGISTER | {"username": "x2", "password": "short"})
    assert short.status_code == 422
    assert {e["field"] for e in short.json()["errors"]} == {"username", "password"}


async def test_a_token_with_extra_characters_after_its_signature_is_refused(client, accounts):
    # PyJWT's base64 decoding ignores what follows a padding "=", so such a token would still
    # verify; it must not, because the frontend writes the token it accepts into its page.
    token = accounts.viewer.headers["Authorization"].removeprefix("Bearer ")
    assert (await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})).status_code == 200
    planted = token + "=</script><script>alert(1)//"
    assert (await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {planted}"})).status_code == 401


@pytest.mark.usefixtures("accounts")
async def test_login_returns_jwt_and_me_uses_it(client):
    response = await client.post("/api/v1/auth/login", data={"username": "engineer", "password": "engineer-password"})
    assert response.status_code == 200
    token = response.json()["access_token"]
    assert response.json()["token_type"] == "bearer" and response.json()["expires_in"] == 480 * 60
    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200 and me.json()["role"] == "user"


@pytest.mark.usefixtures("accounts")
async def test_login_failures(client):
    wrong = await client.post("/api/v1/auth/login", data={"username": "engineer", "password": "nope-nope"})
    assert wrong.status_code == 401
    unknown = await client.post("/api/v1/auth/login", data={"username": "ghost", "password": "whatever1"})
    assert unknown.status_code == 401


async def test_inactive_user_cannot_login_or_use_token(client, accounts):
    headers = accounts.engineer.headers
    async with SessionLocal() as session:
        await session.execute(update(User).where(User.id == accounts.engineer.user.id).values(is_active=False))
        await session.commit()
    login = await client.post("/api/v1/auth/login", data={"username": "engineer", "password": "engineer-password"})
    wrong = await client.post("/api/v1/auth/login", data={"username": "engineer", "password": "not-the-password"})
    # The right password on a disabled account gets the same answer as a wrong one.
    assert login.status_code == wrong.status_code == 401 and login.json()["detail"] == wrong.json()["detail"]
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401


async def test_missing_or_tampered_token_is_rejected(client, accounts):
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    token = accounts.viewer.headers["Authorization"] + "x"
    assert (await client.get("/api/v1/auth/me", headers={"Authorization": token})).status_code == 401


async def test_register_refuses_a_requested_role(client):
    response = await client.post("/api/v1/auth/register", json=REGISTER | {"role": "admin"})
    assert response.status_code == 422 and response.json()["errors"][0]["field"] == "role"
    login = await client.post("/api/v1/auth/login", data={"username": REGISTER["username"], "password": REGISTER["password"]})
    assert login.status_code == 401  # no account was created


async def test_expired_token_is_rejected(client, accounts):
    settings = get_settings()
    past = datetime.now(UTC) - timedelta(minutes=5)
    token = jwt.encode(
        {"sub": str(accounts.admin.user.id), "ver": 0, "role": "admin", "iat": past - timedelta(hours=1), "exp": past},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    response = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


async def test_logout_ends_every_session_of_that_user(client, accounts):
    first = accounts.engineer.headers
    login = await client.post("/api/v1/auth/login", data={"username": "engineer", "password": "engineer-password"})
    second = {"Authorization": f"Bearer {login.json()['access_token']}"}  # e.g. another tab or device
    assert (await client.get("/api/v1/auth/me", headers=second)).status_code == 200
    assert (await client.post("/api/v1/auth/logout", headers=first)).status_code == 204
    for headers in (first, second):
        assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401
    assert (await client.get("/api/v1/auth/me", headers=accounts.viewer.headers)).status_code == 200  # other users unaffected
    again = await client.post("/api/v1/auth/login", data={"username": "engineer", "password": "engineer-password"})
    fresh = {"Authorization": f"Bearer {again.json()['access_token']}"}
    assert (await client.get("/api/v1/auth/me", headers=fresh)).status_code == 200


async def test_a_token_without_a_version_is_refused(client, accounts):
    settings = get_settings()
    now = datetime.now(UTC)
    token = jwt.encode({"sub": str(accounts.admin.user.id), "role": "admin", "iat": now, "exp": now + timedelta(minutes=5)},
                       settings.jwt_secret, algorithm=settings.jwt_algorithm)
    assert (await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})).status_code == 401
