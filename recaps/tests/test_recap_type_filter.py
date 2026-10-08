"""Recaps list `recapType` filter, per-row `recapType`, and `recapTypeCounts`.

Classification is ``recaps.recap_types`` (the Torch routing cascade): the
template, its program, the event's program / template, then the request type;
unknown is retail. The SQL annotation must agree with the Python helper.
"""

from datetime import datetime, timedelta, timezone as _tz

import pytest

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models
from recaps.recap_types import recap_type_key


CUSTOM_RECAPS_QUERY = """
query CustomRecaps($filters: CustomRecapFiltersInput, $first: Int) {
  customRecaps(filters: $filters, first: $first) {
    totalCount
    edges { node { uuid name recapType } }
  }
}
"""

RECAPS_QUERY = """
query Recaps($filters: RecapFiltersInput, $first: Int) {
  recaps(filters: $filters, first: $first) {
    totalCount
    edges { node { uuid name recapType } }
  }
}
"""

COUNTS_QUERY = """
query Counts($filters: CustomRecapFiltersInput, $q: String) {
  recapTypeCounts(filters: $filters, q: $q) {
    total
    types { key label count }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestRecapTypeFilter(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Torch Type Co")
        self.other_tenant = self.create_tenant(name="Elsewhere Co")
        self.spark_admin = self.create_user(
            username="admin-recap-type",
            email="admin-recap-type@test.com",
            role=self.roles["spark_admin"],
        )
        now = datetime.now(_tz.utc)
        self.location = self.create_location(
            name="HQ", code="HQ", zip_code="94105", tenant=self.tenant
        )
        client_row = self.create_client(
            name="Brand", email="brand@example.com", tenant=self.tenant
        )
        distributor = self.create_distributor(
            name="Distro",
            email="distro@example.com",
            location=self.location,
            tenant=self.tenant,
        )
        retailer = self.create_retailer(
            name="Retailer",
            address="1 Main",
            store_contact="mgr",
            location=self.location,
            tenant=self.tenant,
        )
        status_done = self.create_request_status(
            name="Done", tenant=self.tenant, create_event=True
        )
        type_event = self.create_request_type("Event Activation", self.tenant)

        def _event(name, *, event_type=None, request_type=None):
            req = None
            if request_type is not None:
                req = self.create_request(
                    name=name,
                    date=now,
                    address="1 Main",
                    client=client_row,
                    distributor=distributor,
                    retailer=retailer,
                    request_type=request_type,
                    tenant=self.tenant,
                    status=status_done,
                )
            return self.create_event(
                name=name,
                tenant=self.tenant,
                date=now,
                start_time=now,
                end_time=now + timedelta(hours=2),
                request=req,
                event_type=event_type,
            )

        et = {
            name: self.create_event_type(name=name, tenant=self.tenant)
            for name in (
                "Retail Sampling",
                "Event Activation",
                "Guerilla Activation",
                "Product Seeding",
                "On-Premise",
                "Sampling",
            )
        }
        self.tenant.checkin_event_types.set(
            [
                et["Retail Sampling"],
                et["Event Activation"],
                et["Guerilla Activation"],
                et["Product Seeding"],
            ]
        )
        plain_event = _event("Walk-up shift")

        # (template, template program, recap name, approved, expected type)
        specs = (
            ("Torch THC-Retail Sampling", "Retail Sampling", "c-retail", True, "retail"),
            ("Torch THC-Event Activation", "Event Activation", "c-event", True, "event"),
            ("Torch THC · Guerilla Recap", "Guerilla Activation", "c-guerilla", False, "guerilla"),
            ("Torch THC · Product Seeding Recap", "Product Seeding", "c-seeding", True, "seeding"),
            ("Bar Night Recap", "On-Premise", "c-onprem", True, "onprem"),
            ("Mystery Recap", "Sampling", "c-unknown", True, "retail"),
        )
        self.expected = {}
        for tmpl_name, program, recap_name, approved, kind in specs:
            tmpl = recap_models.CustomRecapTemplate.objects.create(
                name=tmpl_name,
                event_type=et[program],
                tenant=self.tenant,
                created_by=self.system_user,
            )
            recap_models.CustomRecap.objects.create(
                name=recap_name,
                approved=approved,
                event=plain_event,
                tenant=self.tenant,
                custom_recap_template=tmpl,
                created_by=self.system_user,
                updated_by=self.system_user,
            )
            self.expected[recap_name] = kind

        # Legacy: the event's program outranks the request type; no program
        # and no request is retail.
        for recap_name, ev, kind in (
            ("l-event", _event("Fest", request_type=type_event), "event"),
            (
                "l-seeding",
                _event(
                    "Drop",
                    event_type=et["Product Seeding"],
                    request_type=type_event,
                ),
                "seeding",
            ),
            ("l-plain", _event("Plain"), "retail"),
        ):
            recap_models.Recap.objects.create(
                name=recap_name,
                approved=True,
                event=ev,
                created_by=self.system_user,
                updated_by=self.system_user,
            )
            self.expected[recap_name] = kind

    async def _run(self, query, variables):
        result = await self._execute_query_authenticated(
            query, variables, self.spark_admin, self.endpoint_path
        )
        assert result.errors is None, f"errored: {result.errors}"
        return result.data

    async def _names(self, query, recap_type, **extra):
        key = "customRecaps" if "customRecaps(" in query else "recaps"
        data = await self._run(
            query,
            {
                "filters": {
                    "tenantId": str(self.tenant.id),
                    "recapType": recap_type,
                    **extra,
                },
                "first": 50,
            },
        )
        conn = data[key]
        names = {e["node"]["name"] for e in conn["edges"]}
        assert conn["totalCount"] == len(names)
        for edge in conn["edges"]:
            node = edge["node"]
            assert node["recapType"] == self.expected[node["name"]], node
        return names

    @pytest.mark.asyncio
    async def test_custom_filter_per_type(self):
        assert await self._names(CUSTOM_RECAPS_QUERY, "retail") == {
            "c-retail",
            "c-unknown",
        }
        assert await self._names(CUSTOM_RECAPS_QUERY, "onprem") == {"c-onprem"}
        assert await self._names(CUSTOM_RECAPS_QUERY, "event") == {"c-event"}
        assert await self._names(CUSTOM_RECAPS_QUERY, "guerilla") == {"c-guerilla"}
        assert await self._names(CUSTOM_RECAPS_QUERY, "seeding") == {"c-seeding"}

    @pytest.mark.asyncio
    async def test_no_or_unknown_type_returns_everything(self):
        every = {k for k in self.expected if k.startswith("c-")}
        assert await self._names(CUSTOM_RECAPS_QUERY, None) == every
        assert await self._names(CUSTOM_RECAPS_QUERY, "nonsense") == every

    @pytest.mark.asyncio
    async def test_legacy_filter_uses_event_program_before_request_type(self):
        assert await self._names(RECAPS_QUERY, "event") == {"l-event"}
        assert await self._names(RECAPS_QUERY, "seeding") == {"l-seeding"}
        assert await self._names(RECAPS_QUERY, "retail") == {"l-plain"}

    @pytest.mark.asyncio
    async def test_type_filter_stacks_with_status(self):
        assert (
            await self._names(CUSTOM_RECAPS_QUERY, "guerilla", approved=True)
            == set()
        )
        assert await self._names(
            CUSTOM_RECAPS_QUERY, "guerilla", approved=False
        ) == {"c-guerilla"}

    def test_sql_classification_matches_python_helper(self):
        for model in (recap_models.CustomRecap, recap_models.Recap):
            for recap in model.objects.all():
                assert recap_type_key(recap) == self.expected[recap.name], recap.name

    @pytest.mark.asyncio
    async def test_counts_cover_both_kinds_in_chip_order(self):
        data = await self._run(
            COUNTS_QUERY, {"filters": {"tenantId": str(self.tenant.id)}}
        )
        counts = data["recapTypeCounts"]
        assert counts["types"] == [
            {"key": "retail", "label": "Retail", "count": 3},
            {"key": "onprem", "label": "On-Premise", "count": 1},
            {"key": "event", "label": "Event", "count": 2},
            {"key": "guerilla", "label": "Guerilla", "count": 1},
            {"key": "seeding", "label": "Seeding", "count": 2},
        ]
        assert counts["total"] == len(self.expected)

    @pytest.mark.asyncio
    async def test_counts_follow_list_filters_and_keep_offered_types(self):
        data = await self._run(
            COUNTS_QUERY,
            {
                "filters": {
                    "tenantId": str(self.tenant.id),
                    "approved": True,
                    "recapType": "event",
                },
                "q": "c-",
            },
        )
        counts = {t["key"]: t["count"] for t in data["recapTypeCounts"]["types"]}
        # The type filter itself never narrows the chips. Guerilla's only
        # recap is unapproved, but the walk-up offers it, so it stays at 0.
        assert counts == {
            "retail": 2,
            "onprem": 1,
            "event": 1,
            "guerilla": 0,
            "seeding": 1,
        }

    @pytest.mark.asyncio
    async def test_store_map_queue_counts_custom_only(self):
        data = await self._run(
            COUNTS_QUERY,
            {"filters": {"tenantId": str(self.tenant.id), "needsStoreMap": True}},
        )
        assert data["recapTypeCounts"]["total"] == 0

    @pytest.mark.asyncio
    async def test_counts_without_tenant_are_empty(self):
        data = await self._run(COUNTS_QUERY, {"filters": {}})
        assert data["recapTypeCounts"] == {"total": 0, "types": []}

    @pytest.mark.asyncio
    async def test_counts_scope_to_tenant(self):
        data = await self._run(
            COUNTS_QUERY, {"filters": {"tenantId": str(self.other_tenant.id)}}
        )
        assert data["recapTypeCounts"] == {"total": 0, "types": []}
