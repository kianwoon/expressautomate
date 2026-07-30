import uuid

from app.services.storage.r2 import client_logo_key


def test_client_logo_key_is_tenant_prefixed():
    """The `{tenant_id}/` prefix is what a tenant purge sweeps by. A key
    without it survives an erasure request."""
    tenant = uuid.UUID("11111111-1111-1111-1111-111111111111")
    client = uuid.UUID("22222222-2222-2222-2222-222222222222")
    assert client_logo_key(tenant, client) == f"{tenant}/clients/{client}/logo"
