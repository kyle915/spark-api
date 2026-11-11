"""Public program link: off by default, revocable, caller-scoped, and
narrow in what it exposes."""

from __future__ import annotations

import json

import pytest
from asgiref.sync import sync_to_async
from django.test import Client

from config.schema_client import schema_clients
from recaps.program_tokens import make_program_token
from tenants.models import Tenant
from tenants.tests.base import BaseGraphQLTestCase

SET_M = """
mutation Set($input: SetProgramShareEnabledInput!) {
  setProgramShareEnabled(input: $input) { success message enabled token }
}
"""
GET_Q = """
query Link($tid: ID) { programShareLink(tenantId: $tid) { enabled token } }
"""


@pytest.mark.django_db(transaction=True)
class TestProgramShare(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.tenant = self.create_tenant(name="Share Brand")
        self.other = self.create_tenant(name="Other Brand")

    def _client_user(self, email, tenant):
        user = self.create_user(
            username=email, email=email, role=self.roles["client"],
            password="password123",
        )
        self.create_tenanted_user(user=user, tenant=tenant)
        return user

    def _get(self, tenant_id):
        return Client().get(f"/api/public/program/{make_program_token(tenant_id)}")

    def test_off_by_default_link_is_not_found(self):
        assert self.tenant.program_share_enabled is False
        res = self._get(self.tenant.id)
        assert res.status_code == 404
        assert "Share Brand" not in res.content.decode()

    def test_tampered_token_is_rejected(self):
        res = Client().get("/api/public/program/not-a-token")
        assert res.status_code == 400

    def test_enabled_link_returns_only_the_narrow_payload(self):
        Tenant.objects.filter(id=self.tenant.id).update(program_share_enabled=True)
        res = self._get(self.tenant.id)
        assert res.status_code == 200
        body = json.loads(res.content)
        assert body["brand"] == "Share Brand"
        assert set(body) == {"brand", "generatedAt", "totals", "markets", "photos"}
        assert set(body["totals"]) == {
            "activations",
            "events",
            "consumersReached",
            "samplesDistributed",
            "productsSold",
            "states",
        }
        assert res["X-Robots-Tag"] == "noindex, nofollow"

    def test_switching_off_revokes_the_link(self):
        Tenant.objects.filter(id=self.tenant.id).update(program_share_enabled=True)
        assert self._get(self.tenant.id).status_code == 200
        Tenant.objects.filter(id=self.tenant.id).update(program_share_enabled=False)
        assert self._get(self.tenant.id).status_code == 404

    @pytest.mark.asyncio
    async def test_client_toggles_only_their_own_brand(self):
        me = await sync_to_async(self._client_user)("me@share.com", self.tenant)

        off = await self._execute_mutation(
            GET_Q, {"tid": str(self.tenant.id)}, self.endpoint_path, user=me
        )
        assert off.errors is None, off.errors
        assert off.data["programShareLink"] == {"enabled": False, "token": None}

        res = await self._execute_mutation(
            SET_M,
            # Asks for the OTHER brand: the server pins a client to their own.
            {"input": {"tenantId": str(self.other.id), "enabled": True}},
            self.endpoint_path,
            user=me,
        )
        assert res.errors is None, res.errors
        out = res.data["setProgramShareEnabled"]
        assert out["success"] is True and out["enabled"] is True and out["token"]

        flags = await sync_to_async(
            lambda: dict(Tenant.objects.values_list("name", "program_share_enabled"))
        )()
        assert flags["Share Brand"] is True
        assert flags["Other Brand"] is False

        got = await sync_to_async(
            lambda: Client().get(f"/api/public/program/{out['token']}")
        )()
        assert got.status_code == 200
        assert json.loads(got.content)["brand"] == "Share Brand"
