"""Example unit test for the role-hierarchy cycle guard — extend this
suite as rbac_service grows. Requires a live test DB; wire a pytest
fixture with a transactional rollback-per-test session before running
against real Postgres."""
import pytest


@pytest.mark.skip(reason="Wire a test DB session fixture before enabling")
async def test_would_create_cycle_detects_self_reference():
    pass
