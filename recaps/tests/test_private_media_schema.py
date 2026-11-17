"""Private media through the REAL client schema with the FE's full field sets.

Copies of the web app's CustomRecapsListQuery (recaps list / approvals) and
CustomRecapsQueryDetailsQuery (+ the new ``customField.mediaUrl``) run with
PRIVATE_MEDIA_ENABLED off and on:

  * on: every private URL is an /api/public/media/o/ link that decodes to the
    right key, IMAGE custom-field values get a ``mediaUrl``;
  * minting never signs (signing is patched to raise) — no IAM/GCS call per
    row;
  * the SQL query count is identical on vs off (no optimizer/deferral
    surprises), and wall time is printed for the PR (run with -s).
"""

import time

import pytest
from asgiref.sync import async_to_sync
from datetime import datetime, timedelta, timezone as _tz
from django.db import connection
from django.test.utils import CaptureQueriesContext, override_settings

from ambassadors.models import FileType
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models
from utils import gcs
from utils.gcs import extract_blob_name_from_url

LIST_QUERY = """
query CustomRecapsListQuery(
    $first: Int
    $after: String
    $last: Int
    $before: String
    $filters: CustomRecapFiltersInput
    $q: String
  ) {
    customRecaps(
      first: $first
      after: $after
      last: $last
      before: $before
      filters: $filters
      q: $q
    ) {
      totalCount
      pageInfo {
        hasNextPage
        hasPreviousPage
        startCursor
        endCursor
      }
      edges {
        cursor
        node {
          id
          uuid
          name
          createdAt
          totalEngagements
          approved
          approvedAt
          approvedBy {
            firstName
            lastName
            email
          }
          isThirdParty
          excludeFromAggregates
          sourceLabel
          coActivation {
            brandNames
          }
          storeMappingStatus
          shareToken
          sharedAt
          archivedAt
          eventDate
          eventAddress
          eventRetailer
          eventState
          ambassador {
            id
            user {
              firstName
              lastName
            }
          }
          customRecapTemplate {
            id
            name
          }
          recapType
          soldUnits
          consumersSampled
          isDryDemo
          peopleEngaged
          heroImageUrl
          customRecapFilesCount
          accountSpend
          baDisplayName
        }
      }
    }
  }
"""

DETAIL_QUERY = """
query CustomRecapsQueryDetailsQuery($uuid: ID) {
    customRecap(uuid: $uuid) {
      id
      uuid
      name
      submittedAt
      createdAt
      totalEngagements
      soldUnits
      consumersSampled
      isDryDemo
      peopleEngaged
      recapType
      fillingForAmbassador
      late
      incomplete
      approved
      approvedAt
      approvedBy {
        firstName
        lastName
        email
      }
      metricEdits {
        id
        batch
        fieldKey
        fieldLabel
        oldValue
        newValue
        reason
        editedByName
        editedAt
      }
      isThirdParty
      excludeFromAggregates
      sourceLabel
      coActivation {
        brandNames
      }
      retailerProposal {
        id
        name
        confidence
        sourceLabel
        reason
        isNewRetailer
      }
      typedStoreName
      typedStoreAddress
      storeMappingStatus
      storeSuggestions
      shareToken
      sharedAt
      archivedAt
      archiveReason
      archivedBy {
        firstName
        lastName
        email
      }
      clientSignoffStatus
      clientSignoffComment
      clientSignoffAt
      usedCorpoCard
      eventDate
      eventAddress
      eventVenue
      eventRetailer
      eventCity
      eventState
      event {
        id
        name
        date
        tenant {
          id
          uuid
        }
      }
      ambassador {
        id
        user {
          firstName
          lastName
        }
      }
      externalBaName
      job {
        name
      }
      retailer {
        name
      }
      location {
        name
      }
      state {
        name
      }
      customRecapTemplate {
        id
        name
        layout
        tenant {
          id
          uuid
        }
      }
      customRecapProductSample {
        id
        quantity
        product {
          id
          name
          image
        }
      }
      customRecapSalePerformance {
        id
        price
        product {
          id
          name
          image
        }
        typeOfGood {
          name
        }
      }
      customRecapFiles {
        id
        name
        url
        displayUrl
        approved
        fileRecapCategory {
          name
        }
        fileType {
          name
        }
      }
      customField {
        id
        name
        value
        mediaUrl
        options
        placeholder
        customFieldType {
          name
        }
        customFieldValueId
        recapSection {
          name
        }
      }
    }
  }
"""


API = "https://api.test"
OFF = dict(GS_BUCKET_NAME="bkt", PRIVATE_MEDIA_ENABLED=False)
ON = dict(
    GS_BUCKET_NAME="bkt",
    PRIVATE_MEDIA_ENABLED=True,
    PRIVATE_MEDIA_BASE_URL=API,
    PRIVATE_MEDIA_TOKEN_KEYS="k1:test",
)
ROWS = 40
FILES_PER_RECAP = 6


@pytest.mark.django_db(transaction=True)
class TestPrivateMediaThroughSchema(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db, monkeypatch):
        from config.schema_client import schema_clients

        def boom(*a, **k):
            raise AssertionError("URL minting must not sign / call IAM")

        monkeypatch.setattr(gcs, "generate_download_url", boom)
        monkeypatch.setattr(gcs, "_iam_signing_args_for_default_credentials", boom)

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Private Media Co")
        self.admin = self.create_user(
            username="admin-private-media",
            email="admin-private-media@test.com",
            role=self.roles["spark_admin"],
        )
        self.now = datetime.now(_tz.utc)
        self.event_type = self.create_event_type(name="Sampling", tenant=self.tenant)
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="T", event_type=self.event_type, tenant=self.tenant,
            created_by=self.system_user,
        )
        self.file_type = FileType.objects.create(name="image", created_by=self.system_user)
        self.section = recap_models.RecapSection.objects.create(
            name="S", tenant=self.tenant, created_by=self.system_user
        )
        number = recap_models.CustomRecapFieldType.objects.create(
            name="number", created_by=self.system_user
        )
        image = recap_models.CustomRecapFieldType.objects.create(
            name="image", created_by=self.system_user
        )
        self.sampled = recap_models.CustomField.objects.create(
            name="Total number of consumers sampled", custom_recap_template=self.template,
            custom_field_type=number, recap_section=self.section, created_by=self.system_user,
        )
        self.photo_field = recap_models.CustomField.objects.create(
            name="Table setup photo", custom_recap_template=self.template,
            custom_field_type=image, recap_section=self.section, created_by=self.system_user,
        )

    def _make_recap(self, idx):
        event = self.create_event(
            name=f"E{idx}", tenant=self.tenant, date=self.now + timedelta(days=idx)
        )
        recap = recap_models.CustomRecap.objects.create(
            name=f"R{idx}", approved=True, event=event, tenant=self.tenant,
            custom_recap_template=self.template, created_by=self.system_user,
            updated_by=self.system_user,
        )
        for field, value in (
            (self.sampled, "50"),
            (self.photo_field, f"recaps/{recap.uuid}/table.heic"),
        ):
            recap_models.CustomFieldValue.objects.create(
                value=value, custom_recap=recap, custom_field=field,
                created_by=self.system_user,
            )
        for n in range(FILES_PER_RECAP):
            ext = "heic" if n == 0 else "jpg"
            recap_models.CustomRecapFile.objects.create(
                name=f"p{n}.{ext}", url=f"recaps/{recap.uuid}/p{n}.{ext}",
                file_type=self.file_type, custom_recap=recap, created_by=self.system_user,
            )
        return recap

    def _run(self, query, variables, flags):
        with override_settings(**flags):
            with CaptureQueriesContext(connection) as ctx:
                t0 = time.perf_counter()
                result = async_to_sync(self._execute_query_authenticated)(
                    query, variables, self.admin, self.endpoint_path
                )
                elapsed = time.perf_counter() - t0
        assert result.errors is None, result.errors
        return result.data, len(ctx.captured_queries), elapsed

    @override_settings(**ON)  # decode tokens below with the same keyring
    def test_list_query_full_fe_field_set(self):
        for i in range(ROWS):
            self._make_recap(i)
        variables = {"first": 100, "filters": {"tenantId": str(self.tenant.id)}}
        # Warm caches/imports so on/off timings compare like for like.
        self._run(LIST_QUERY, variables, OFF)
        off, off_q, off_t = self._run(LIST_QUERY, variables, OFF)
        on, on_q, on_t = self._run(LIST_QUERY, variables, ON)
        print(f"\n[list {ROWS} rows] off: {off_q} queries {off_t*1000:.0f}ms | on: {on_q} queries {on_t*1000:.0f}ms")

        assert on_q == off_q
        edges_off = off["customRecaps"]["edges"]
        edges_on = on["customRecaps"]["edges"]
        assert len(edges_on) == ROWS
        for e_off, e_on in zip(edges_off, edges_on):
            hero_off = e_off["node"]["heroImageUrl"]
            hero_on = e_on["node"]["heroImageUrl"]
            assert hero_off.startswith("https://storage.googleapis.com/bkt/recaps/")
            assert hero_on.startswith(f"{API}/api/public/media/o/")
            assert hero_on.rsplit("/", 1)[-1] == hero_off.rsplit("/", 1)[-1]
            assert extract_blob_name_from_url(hero_on) == extract_blob_name_from_url(hero_off)

    @override_settings(**ON)
    def test_detail_query_full_fe_field_set(self):
        recap = self._make_recap(0)
        variables = {"uuid": str(recap.uuid)}
        self._run(DETAIL_QUERY, variables, OFF)
        off, off_q, off_t = self._run(DETAIL_QUERY, variables, OFF)
        on, on_q, on_t = self._run(DETAIL_QUERY, variables, ON)
        print(f"\n[detail {FILES_PER_RECAP} files] off: {off_q} queries {off_t*1000:.0f}ms | on: {on_q} queries {on_t*1000:.0f}ms")
        assert on_q == off_q

        files = on["customRecap"]["customRecapFiles"]
        assert len(files) == FILES_PER_RECAP
        for f in files:
            for key in ("url", "displayUrl"):
                assert f[key].startswith(f"{API}/api/public/media/o/"), f
        heic = next(f for f in files if f["name"] == "p0.heic")
        assert heic["url"].endswith("/p0.heic")
        assert heic["displayUrl"].endswith("/p0.jpg")
        assert extract_blob_name_from_url(heic["displayUrl"]) == f"recaps/{recap.uuid}/p0.jpg"

        by_name = {f["name"]: f for f in on["customRecap"]["customField"]}
        photo = by_name["Table setup photo"]
        assert photo["value"] == f"recaps/{recap.uuid}/table.heic"  # raw key unchanged
        assert photo["mediaUrl"].startswith(f"{API}/api/public/media/o/")
        assert extract_blob_name_from_url(photo["mediaUrl"]) == f"recaps/{recap.uuid}/table.jpg"
        assert by_name["Total number of consumers sampled"]["mediaUrl"] is None

        off_photo = {f["name"]: f for f in off["customRecap"]["customField"]}["Table setup photo"]
        assert off_photo["mediaUrl"] == f"https://storage.googleapis.com/bkt/recaps/{recap.uuid}/table.jpg"
