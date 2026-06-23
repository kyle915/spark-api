"""Ignite admins can create global/tenant-less catalog entities.

Regression for "It looks like you are not a member of this tenant." when an
Ignite admin adds a town (Location). Locations are global (no tenant FK) and
the create form sends no tenant_id, so BaseMutationService.set_user_and_tenant
must NOT run a tenant-membership lookup for an admin. Admin status is resolved
the DB-backed way (_is_admin_access via resolve_request_user_access), so an
@igniteproductions.co user who is NOT a literal TenantUser member still gets
through — while non-admins are unaffected.
"""
from __future__ import annotations

import pytest
from asgiref.sync import sync_to_async

from events import inputs, models
from events.mutations import LocationMutationService
from events.tests.base import EventsGraphQLTestCase


class _Req:
    def __init__(self, user):
        self.user = user


class _Ctx:
    def __init__(self, user):
        self.request = _Req(user)


class _Info:
    """Minimal stand-in for strawberry.Info — set_user_and_tenant only touches
    info.context.request.user."""

    def __init__(self, user):
        self.context = _Ctx(user)


@pytest.mark.django_db(transaction=True)
class TestAdminCreatesGlobalLocation(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        # Ignite admin by EMAIL only: role is "client", NOT spark-admin, and
        # the user is not a TenantUser member of anything — exactly Kyle's
        # case. Access must come from the @igniteproductions.co domain rule.
        self.ignite_admin = self.create_user(
            username="ignite-founder",
            email="founder@igniteproductions.co",
            role=self.roles["client"],
        )

    @pytest.mark.asyncio
    async def test_ignite_admin_adds_town_without_tenant_membership(self):
        info = _Info(self.ignite_admin)
        data = inputs.CreateLocationInput(name="Canton", code="MI", zip="48187")

        loc = await LocationMutationService.process_create_or_update(data, info)

        assert loc.pk is not None
        assert loc.name == "Canton"
        assert await sync_to_async(
            models.Location.objects.filter(name="Canton").exists
        )()
