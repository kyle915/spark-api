"""
End-to-end + N+1 guard for the CustomRecap list-card scalar fields.

The whole point of moving soldUnits / consumersSampled / heroImageUrl /
customRecapFilesCount server-side is to let the web recaps LIST stop
over-fetching the customField + customRecapFiles arrays. That only pays off
if the new scalar resolvers read from the queryset's PREFETCH cache rather
than firing a query per row — otherwise we'd trade a big payload for an
O(rows) query storm.

`custom_recaps` builds its queryset via CustomRecapQueriesService.get_queryset(),
which explicitly prefetch_related()s `custom_field_value` (+ its custom_field)
and `custom_recap_files`; the connection is materialized by
connection_from_queryset_async (plain Django count()/list(), NOT the
strawberry-django optimizer), so those prefetches survive even though this
query selects only the scalars. These tests prove that empirically:

  * the resolved scalar VALUES match the frontend math, and
  * the total query count does NOT grow with the number of rows — request
    2 recaps vs 6 recaps and the count is identical.

Both tests run synchronously and drive the async schema via async_to_sync so
that Django's CaptureQueriesContext (whose __enter__ touches the connection)
can wrap the GraphQL execution without tripping SynchronousOnlyOperation.
"""

import pytest
from datetime import datetime, timedelta, timezone as _tz

from asgiref.sync import async_to_sync
from django.db import connection
from django.test.utils import CaptureQueriesContext, override_settings

from ambassadors.models import FileType
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models


# Query asks ONLY for the new scalars (plus uuid) — deliberately NOT the
# customField / customRecapFiles arrays. This is exactly the slimmed query
# the frontend will switch to; if the resolvers fell back to per-row DB
# reads, the query count would scale with `first`.
CARD_QUERY = """
query CustomRecaps($tenantId: ID, $first: Int) {
  customRecaps(filters: { tenantId: $tenantId }, first: $first) {
    totalCount
    edges {
      node {
        uuid
        soldUnits
        consumersSampled
        heroImageUrl
        customRecapFilesCount
      }
    }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestCustomRecapCardAggregatesNoNPlus1(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()

        self.tenant = self.create_tenant(name="Liquid Death")
        self.spark_admin = self.create_user(
            username="admin-card-aggr",
            email="admin-card-aggr@test.com",
            role=self.roles["spark_admin"],
        )

        self.now = datetime.now(_tz.utc)
        self.event_type = self.create_event_type(name="Sampling", tenant=self.tenant)
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="LD Units Template",
            event_type=self.event_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )
        self.file_type = FileType.objects.create(
            name="image", created_by=self.system_user
        )
        self.field_type = recap_models.CustomRecapFieldType.objects.create(
            name="number", created_by=self.system_user
        )
        self.section = recap_models.RecapSection.objects.create(
            name="Sales", tenant=self.tenant, created_by=self.system_user
        )

        # Two unit fields (cans + packs) and one consumers-sampled field.
        self.cans_field = self._make_field("Single Cans")
        self.packs_field = self._make_field("Packs Sold")
        self.sampled_field = self._make_field("Total number of consumers sampled")
        # A noise field whose name must NOT count toward soldUnits.
        self.willing_field = self._make_field("Willing to purchase")

    def _make_field(self, name):
        return recap_models.CustomField.objects.create(
            name=name,
            custom_recap_template=self.template,
            custom_field_type=self.field_type,
            recap_section=self.section,
            created_by=self.system_user,
        )

    def _make_recap(self, idx):
        """One custom recap with cans=100, packs=20, sampled=350, willing=999
        and two files (one jpg image + one pdf). Expected derived values:
        soldUnits=120, consumersSampled=350, heroImageUrl ends .jpg,
        customRecapFilesCount=2."""
        event = self.create_event(
            name=f"LD Event {idx:03d}",
            tenant=self.tenant,
            date=self.now + timedelta(days=idx),
        )
        recap = recap_models.CustomRecap.objects.create(
            name=f"LD custom recap {idx:03d}",
            approved=True,
            event=event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        for field, value in (
            (self.cans_field, "100"),
            (self.packs_field, "20"),
            (self.sampled_field, "350"),
            (self.willing_field, "999"),
        ):
            recap_models.CustomFieldValue.objects.create(
                value=value,
                custom_recap=recap,
                custom_field=field,
                created_by=self.system_user,
            )
        # PDF first (so the resolver must skip it), then the jpg hero.
        recap_models.CustomRecapFile.objects.create(
            name="report.pdf",
            url=f"recaps/{idx}/report.pdf",
            file_type=self.file_type,
            custom_recap=recap,
            created_by=self.system_user,
        )
        recap_models.CustomRecapFile.objects.create(
            name="hero.jpg",
            url=f"recaps/{idx}/hero.jpg",
            file_type=self.file_type,
            custom_recap=recap,
            created_by=self.system_user,
        )
        return recap

    def _make_recaps(self, count):
        return [self._make_recap(i) for i in range(count)]

    def _make_recaps_from(self, start, end):
        for i in range(start, end):
            self._make_recap(i)

    def _execute_card_query(self, first):
        """Run the slim card query synchronously (drives the async schema).

        GS_BUCKET_NAME is overridden so utils.gcs.public_url builds a real
        https://storage.googleapis.com/... URL (it returns None on an empty
        bucket name); without it heroImageUrl would always be None in tests.
        """
        with override_settings(GS_BUCKET_NAME="test-bucket"):
            return async_to_sync(self._execute_query_authenticated)(
                CARD_QUERY,
                {"tenantId": str(self.tenant.id), "first": first},
                self.spark_admin,
                self.endpoint_path,
            )

    def test_scalar_values_match_frontend_math(self):
        self._make_recaps(3)
        result = self._execute_card_query(100)
        assert result.errors is None, f"errored: {result.errors}"
        conn = result.data["customRecaps"]
        assert conn["totalCount"] == 3
        for edge in conn["edges"]:
            node = edge["node"]
            assert node["soldUnits"] == 120, node  # 100 cans + 20 packs
            assert node["consumersSampled"] == 350, node
            assert node["customRecapFilesCount"] == 2, node
            # Hero is the jpg, not the pdf that precedes it.
            assert node["heroImageUrl"] is not None, node
            assert node["heroImageUrl"].endswith("hero.jpg"), node
            # The "willing to purchase" field must not leak into soldUnits.
            assert node["soldUnits"] != 1119

    def test_query_count_constant_regardless_of_row_count(self):
        """The cornerstone N+1 guard: 2 rows vs 6 rows -> SAME query count.

        Each recap has its own field-value rows and file rows; if the
        resolvers read per-row instead of from the prefetch cache, the
        6-row query would issue several MORE queries than the 2-row one.
        Equality proves the reads hit the prefetch cache.
        """
        # --- small page: 2 recaps ---
        self._make_recaps(2)
        with CaptureQueriesContext(connection) as small_ctx:
            small = self._execute_card_query(100)
        assert small.errors is None, f"errored: {small.errors}"
        assert small.data["customRecaps"]["totalCount"] == 2
        small_count = len(small_ctx.captured_queries)

        # --- larger page: add 4 more (6 total) ---
        self._make_recaps_from(2, 6)
        with CaptureQueriesContext(connection) as big_ctx:
            big = self._execute_card_query(100)
        assert big.errors is None, f"errored: {big.errors}"
        assert big.data["customRecaps"]["totalCount"] == 6
        big_count = len(big_ctx.captured_queries)

        # Constant query count -> the new scalar resolvers add ZERO per-row
        # queries (they read straight from the prefetch cache). A failure
        # here means one of the resolvers regressed into an N+1.
        assert big_count == small_count, (
            f"N+1 detected: 2 rows took {small_count} queries, "
            f"6 rows took {big_count}. Captured (6-row):\n"
            + "\n".join(q["sql"][:160] for q in big_ctx.captured_queries)
        )
