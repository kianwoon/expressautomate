"""The agency's own staff list, for pickers that name a colleague."""

import uuid

import httpx
import pytest
from sqlalchemy import text

from app.main import app
from tests.conftest import AdminSessionLocal, cleanup_tenant, seed_tenant_with_user
from tests.test_opportunities_api import sign_in


@pytest.fixture
async def client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as c:
        yield c


async def test_it_lists_everyone_in_my_agency() -> None:
    tenant_id, user_id = await seed_tenant_with_user()
    other_id = uuid.uuid4()
    async with AdminSessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, role) "
                "VALUES (:id, :tenant_id, :email, :role)"
            ),
            {
                "id": other_id,
                "tenant_id": tenant_id,
                "email": f"{other_id.hex[:8]}@agency.sg",
                "role": "recruiter",
            },
        )
        await session.commit()

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", follow_redirects=False
        ) as client:
            sign_in(client, user_id, tenant_id)
            response = await client.get("/api/members")

        assert response.status_code == 200
        ids = {member["id"] for member in response.json()}
        assert ids == {str(user_id), str(other_id)}
    finally:
        await cleanup_tenant(tenant_id)


async def test_it_never_lists_another_agency() -> None:
    """Two tenants, each with users; A must not see B's."""
    tenant_a, user_a = await seed_tenant_with_user()
    tenant_b, user_b = await seed_tenant_with_user()

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", follow_redirects=False
        ) as client:
            sign_in(client, user_a, tenant_a)
            response = await client.get("/api/members")

        assert response.status_code == 200
        ids = {member["id"] for member in response.json()}
        assert ids == {str(user_a)}
        assert str(user_b) not in ids
    finally:
        await cleanup_tenant(tenant_a, tenant_b)


async def test_preferred_name_wins_over_display_name() -> None:
    """A picker that ignored preferred_name would call someone by a name they
    had explicitly replaced."""
    tenant_id, user_id = await seed_tenant_with_user()
    async with AdminSessionLocal() as session:
        await session.execute(
            text(
                "UPDATE users SET display_name = :display, preferred_name = :preferred "
                "WHERE id = :id"
            ),
            {"display": "Jonathan Tan", "preferred": "Jon", "id": user_id},
        )
        await session.commit()

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", follow_redirects=False
        ) as client:
            sign_in(client, user_id, tenant_id)
            response = await client.get("/api/members")

        assert response.status_code == 200
        [member] = response.json()
        assert member["name"] == "Jon"
    finally:
        await cleanup_tenant(tenant_id)


async def test_a_user_with_no_names_falls_back_to_the_email_local_part() -> None:
    """So the picker never renders a blank row."""
    # display_name and preferred_name both NULL, email "raj@agency.sg" -> "raj"
    tenant_id, user_id = await seed_tenant_with_user()
    async with AdminSessionLocal() as session:
        await session.execute(
            text("UPDATE users SET email = :email WHERE id = :id"),
            {"email": "raj@agency.sg", "id": user_id},
        )
        await session.commit()

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", follow_redirects=False
        ) as client:
            sign_in(client, user_id, tenant_id)
            response = await client.get("/api/members")

        assert response.status_code == 200
        [member] = response.json()
        assert member["name"] == "raj"
    finally:
        await cleanup_tenant(tenant_id)


async def test_it_reports_the_owner_role() -> None:
    tenant_id, user_id = await seed_tenant_with_user(role="owner")

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", follow_redirects=False
        ) as client:
            sign_in(client, user_id, tenant_id)
            response = await client.get("/api/members")

        assert response.status_code == 200
        [member] = response.json()
        assert member["role"] == "owner"
    finally:
        await cleanup_tenant(tenant_id)
