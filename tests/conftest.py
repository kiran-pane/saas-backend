import uuid

import pytest

from app.core.tenancy import context as tenancy_context


@pytest.fixture(autouse=True)
def _reset_context():
    yield
    tenancy_context._current_tenant.set(None)
    tenancy_context._current_user.set(None)


@pytest.fixture
def tenant_id() -> uuid.UUID:
    tid = uuid.uuid4()
    tenancy_context.set_tenant(tid)
    return tid
