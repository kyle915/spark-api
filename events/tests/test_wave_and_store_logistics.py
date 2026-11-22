"""End-to-end GraphQL tests for Account Map Wave Builder (`createRequestsWave`),
Store Logistics Notes (`saveStoreLogisticsNotes` + shift briefing surfacing),
universal `resolveStoreKey`, and `rebookLastCrew` physical-store matching."""

from __future__ import annotations

import datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone as dj_tz

from ambassadors.models import Ambassador, AmbassadorEvent
from events import models as em
from events.event_confirmations import build_context
from events.map_points import address_hash
from events.tests.base import EventsGraphQLTestCase
from jobs.models import Job, JobTitle, Rate, RateType
from recaps.models import CustomRecap, CustomRecapTemplate
from recaps.store_places import tenant_places
from store_logistics.models import StoreLogisticsNote
from utils.ai_text import AiUnavailable

WAVE_MUTATION = """
mutation CreateRequestsWaveMutation($input: CreateRequestsWaveInput!) {
  createRequestsWave(input: $input) {
    success
    message
    totalRows
    successCount
    failedCount
    skippedCount
    rolledBack
    errors
    rows {
      rowNumber
      success
      skipped
      message
      requestId
      requestUuid
    }
  }
}
"""

SAVE_LOGISTICS_MUTATION = """
mutation SaveStoreLogisticsNotesMutation($input: SaveStoreLogisticsNotesInput!) {
  saveStoreLogisticsNotes(input: $input) {
    success
    message
    storeKey
    logisticsNotes
    logisticsNotesUpdatedAt
  }
}
"""

STORE_PROFILE_QUERY = """
query StoreProfileWithLogistics($tenantId: ID!, $storeKey: String!) {
  storeProfile(tenantId: $tenantId, storeKey: $storeKey) {
    storeKey
    name
    address
    logisticsNotes
    logisticsNotesUpdatedAt
    visits {
      eventId
      eventUuid
      title
      date
      status
    }
  }
}
"""

RESOLVE_STORE_KEY_QUERY = """
query ResolveStoreKeyQuery(
  $tenantId: ID!
  $storeKey: String
  $requestId: ID
  $recapId: ID
  $storeName: String
  $address: String
) {
  resolveStoreKey(
    tenantId: $tenantId
    storeKey: $storeKey
    requestId: $requestId
    recapId: $recapId
    storeName: $storeName
    address: $address
  )
}
"""

JOB_BRIEFING_QUERY = """
query JobBriefingForEventQuery($eventUuid: ID!) {
  jobBriefingForEvent(eventUuid: $eventUuid) {
    title
    body
  }
}
"""

REBOOK_CREW_MUTATION = """
mutation RebookLastCrewTest($input: RebookLastCrewInput!) {
  rebookLastCrew(input: $input) {
    success
    message
    assignedCount
    failedCount
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestWaveRequestsAndStoreLogistics(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"

        self.tenant = self.create_tenant(name="Wave Brand")
        self.other = self.create_tenant(name="Other Brand")

        self.admin = self.create_user(
            username="wave-admin",
            email="wave-admin@igniteproductions.co",
            role=self.roles["spark_admin"],
            is_staff=True,
        )
        self.client_user = self.create_user(
            username="wave-client",
            email="wave-client@brand.test",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        self.other_client = self.create_user(
            username="other-client",
            email="other-client@other.test",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.other_client, self.other)

        # Timezones (standard + DST)
        self.tz_pdt, _ = em.TimeZone.objects.get_or_create(
            code="PDT", defaults={"name": "Pacific Daylight Time", "offset": -7}
        )
        self.tz_pst, _ = em.TimeZone.objects.get_or_create(
            code="PST", defaults={"name": "Pacific Standard Time", "offset": -8}
        )
        self.tz_edt, _ = em.TimeZone.objects.get_or_create(
            code="EDT", defaults={"name": "Eastern Daylight Time", "offset": -4}
        )
        self.tz_cdt, _ = em.TimeZone.objects.get_or_create(
            code="CDT", defaults={"name": "Central Daylight Time", "offset": -5}
        )
        self.tz_mdt, _ = em.TimeZone.objects.get_or_create(
            code="MDT", defaults={"name": "Mountain Daylight Time", "offset": -6}
        )

        self.state_ca, _ = em.State.objects.get_or_create(
            code="CA", defaults={"name": "California", "created_by": self.admin}
        )
        self.state_ny, _ = em.State.objects.get_or_create(
            code="NY", defaults={"name": "New York", "created_by": self.admin}
        )

        # Request & Event statuses + types
        self.req_approved = em.RequestStatus.objects.create(
            tenant=self.tenant,
            name="Approved",
            slug="approved",
            is_default=True,
            created_by=self.admin,
        )
        self.ev_approved = em.EventStatus.objects.create(
            tenant=self.tenant,
            name="Approved",
            slug="approved",
            is_default=True,
            created_by=self.admin,
        )
        self.other_req_approved = em.RequestStatus.objects.create(
            tenant=self.other,
            name="Approved",
            slug="approved",
            is_default=True,
            created_by=self.admin,
        )
        self.other_ev_approved = em.EventStatus.objects.create(
            tenant=self.other,
            name="Approved",
            slug="approved",
            is_default=True,
            created_by=self.admin,
        )

        self.req_type = em.RequestType.objects.create(
            tenant=self.tenant, name="Retail Sampling", created_by=self.admin
        )
        self.ev_type = em.EventType.objects.create(
            tenant=self.tenant,
            name="Retail Sampling",
            is_default=True,
            created_by=self.admin,
        )
        self.other_req_type = em.RequestType.objects.create(
            tenant=self.other, name="Retail Sampling", created_by=self.admin
        )

        prod_type = em.ProductType.objects.create(
            tenant=self.tenant, name="Sparkling", created_by=self.admin
        )
        self.product = em.Product.objects.create(
            tenant=self.tenant,
            product_type=prod_type,
            name="Mango Lime 12oz",
            created_by=self.admin,
        )

        # Seed default JobTitle + Rate so create_pending_jobs_for_request creates Jobs
        self.job_title = JobTitle.objects.create(
            tenant=self.tenant, name="Brand Ambassador", created_by=self.admin
        )
        rate_type = RateType.objects.create(
            tenant=self.tenant, name="Hourly", created_by=self.admin
        )
        self.rate = Rate.objects.create(
            tenant=self.tenant,
            rate_type=rate_type,
            amount=Decimal("30.00"),
            created_by=self.admin,
        )

    @pytest.mark.asyncio
    async def test_wave_dry_run_then_live_schedule_with_timezone_dedup_and_jobs(self):
        # Pre-seed Store Logistics Note on the Chino store address so the newly
        # scheduled wave automatically picks it up on its Job briefing!
        addr_chino = "12013 Central Ave, Chino, CA 91710"
        await sync_to_async(StoreLogisticsNote.objects.create)(
            tenant=self.tenant,
            place_key=f"addr:{address_hash(addr_chino)}",
            store_name="Albertsons 524",
            address=addr_chino,
            notes="Check in with Maria at Customer Service; set table by cooler #2.",
            updated_by=self.admin,
        )

        wave_items = [
            {
                "storeName": "Albertsons 524",
                "address": addr_chino,
                "city": "UnseededChinoSuburb",
                "state": "CA",
                "latitude": 34.0122,
                "longitude": -117.6889,
                "date": "2026-05-15",
                "startTime": "12:00",
                "endTime": "16:00",
            },
            # Duplicate of row 1 in the same wave -> skipped cleanly
            {
                "storeName": "Albertsons 524",
                "address": addr_chino,
                "state": "CA",
                "date": "2026-05-15",
                "startTime": "12:00",
                "endTime": "16:00",
            },
            # East coast store + overnight hours (22:00 -> 01:00)
            {
                "storeName": "7-Eleven #3201",
                "address": "1608 Broadway St, New York, NY 10019",
                "city": "New York",
                "state": "NY",
                "latitude": 40.7616,
                "longitude": -73.9847,
                "date": "2026-05-16",
                "startTime": "22:00",
                "endTime": "01:00",
            },
        ]

        # 1. Dry-run preview creates zero DB rows
        dry = await self._execute_mutation(
            WAVE_MUTATION,
            {
                "input": {
                    "tenantId": str(self.tenant.id),
                    "requestTypeId": str(self.req_type.id),
                    "productIds": [str(self.product.id)],
                    "notes": "Bring branded tablecloth",
                    "dryRun": True,
                    "items": wave_items,
                }
            },
            user=self.client_user,
        )
        assert dry.errors is None, dry.errors
        payload = dry.data["createRequestsWave"]
        assert payload["success"] is True, payload
        assert (payload["successCount"], payload["skippedCount"], payload["failedCount"]) == (
            2,
            1,
            0,
        )
        assert await sync_to_async(
            em.Request.objects.filter(tenant=self.tenant).count
        )() == 0

        # 2. Live wave creates 2 approved Requests, Events, RequestProducts, and Jobs
        live = await self._execute_mutation(
            WAVE_MUTATION,
            {
                "input": {
                    "tenantId": str(self.tenant.id),
                    "requestTypeId": str(self.req_type.id),
                    "productIds": [str(self.product.id)],
                    "notes": "Bring branded tablecloth",
                    "dryRun": False,
                    "items": wave_items,
                }
            },
            user=self.client_user,
        )
        assert live.errors is None, live.errors
        live_payload = live.data["createRequestsWave"]
        assert live_payload["success"] is True
        assert (
            live_payload["successCount"],
            live_payload["skippedCount"],
            live_payload["failedCount"],
        ) == (2, 1, 0)

        def _verify_created():
            reqs = list(
                em.Request.objects.filter(tenant=self.tenant)
                .select_related("timezone", "status")
                .order_by("id")
            )
            assert len(reqs) == 2
            ca_req, ny_req = reqs
            assert ca_req.status.slug == "approved"
            assert ca_req.timezone.code == "PDT"
            assert ny_req.timezone.code == "EDT"
            # Overnight NY shift: 22:00 EDT (02:00 UTC next day) -> 01:00 EDT (05:00 UTC next day)
            assert ny_req.end_time > ny_req.start_time
            assert (ny_req.end_time - ny_req.start_time) == datetime.timedelta(hours=3)
            assert em.RequestProduct.objects.filter(request=ca_req, product=self.product).exists()

            ca_event = em.Event.objects.get(request=ca_req)
            ca_job = Job.objects.get(event=ca_event)
            # Dynamic store logistics notes must NOT be baked into persisted Job columns
            assert (ca_job.briefing_body or "") == ""
            return str(ca_event.uuid)

        ca_event_uuid = await sync_to_async(_verify_created)()

        # 3. Re-submitting the exact same wave skips both already-scheduled stores
        repeat = await self._execute_mutation(
            WAVE_MUTATION,
            {
                "input": {
                    "tenantId": str(self.tenant.id),
                    "requestTypeId": str(self.req_type.id),
                    "items": [wave_items[0], wave_items[2]],
                }
            },
            user=self.client_user,
        )
        assert repeat.errors is None, repeat.errors
        rep_payload = repeat.data["createRequestsWave"]
        assert (rep_payload["successCount"], rep_payload["skippedCount"]) == (0, 2)

        # 4. Querying jobBriefingForEvent surfaces store logistics notes dynamically,
        # and editing or clearing the store note leaves zero stale text behind.
        brief = await self._execute_mutation(
            JOB_BRIEFING_QUERY,
            {"eventUuid": ca_event_uuid},
            user=self.client_user,
        )
        assert brief.errors is None, brief.errors
        assert "Maria at Customer Service" in brief.data["jobBriefingForEvent"]["body"]

        def _update_note(new_text: str):
            StoreLogisticsNote.objects.filter(tenant=self.tenant).update(notes=new_text)

        await sync_to_async(_update_note)("Enter via north loading bay; ring bell twice.")
        brief_edited = await self._execute_mutation(
            JOB_BRIEFING_QUERY,
            {"eventUuid": ca_event_uuid},
            user=self.client_user,
        )
        assert "north loading bay" in brief_edited.data["jobBriefingForEvent"]["body"]
        assert "Maria at Customer Service" not in brief_edited.data["jobBriefingForEvent"]["body"]

        await sync_to_async(_update_note)("")
        brief_cleared = await self._execute_mutation(
            JOB_BRIEFING_QUERY,
            {"eventUuid": ca_event_uuid},
            user=self.client_user,
        )
        assert brief_cleared.data["jobBriefingForEvent"]["body"] == ""

    @pytest.mark.asyncio
    async def test_wave_rollback_on_error_and_cross_tenant_rejection(self):
        # Cross-tenant client is rejected
        denied = await self._execute_mutation(
            WAVE_MUTATION,
            {
                "input": {
                    "tenantId": str(self.other.id),
                    "requestTypeId": str(self.other_req_type.id),
                    "items": [
                        {
                            "storeName": "Target",
                            "address": "100 Main St, Austin, TX 78701",
                            "state": "TX",
                            "date": "2026-06-01",
                            "startTime": "11:00",
                            "endTime": "15:00",
                        }
                    ],
                }
            },
            user=self.client_user,
        )
        assert denied.errors is None, denied.errors
        assert denied.data["createRequestsWave"]["success"] is False
        assert await sync_to_async(
            em.Request.objects.filter(tenant=self.other).count
        )() == 0

        # Invalid second row rolls back valid first row when rollbackOnError=True
        rolled = await self._execute_mutation(
            WAVE_MUTATION,
            {
                "input": {
                    "tenantId": str(self.tenant.id),
                    "requestTypeId": str(self.req_type.id),
                    "rollbackOnError": True,
                    "items": [
                        {
                            "storeName": "Good Store",
                            "address": "500 Pine St, Seattle, WA 98101",
                            "state": "WA",
                            "date": "2026-06-05",
                            "startTime": "10:00",
                            "endTime": "14:00",
                        },
                        {
                            "storeName": "Bad Date Store",
                            "address": "600 Pine St, Seattle, WA 98101",
                            "state": "WA",
                            "date": "not-a-date",
                            "startTime": "10:00",
                            "endTime": "14:00",
                        },
                    ],
                }
            },
            user=self.client_user,
        )
        assert rolled.errors is None, rolled.errors
        rp = rolled.data["createRequestsWave"]
        assert rp["success"] is False, rp
        assert rp["rolledBack"] is True, rp
        assert rp["successCount"] == 0
        assert await sync_to_async(
            em.Request.objects.filter(tenant=self.tenant).count
        )() == 0

    @pytest.mark.asyncio
    async def test_store_logistics_notes_resolve_store_key_confirmations_and_rebook_crew(self):
        def _seed_store_and_crew():
            addr = "742 Evergreen Terrace, Los Angeles, CA 90001"
            past_dt = dj_tz.now() - datetime.timedelta(days=14)
            future_dt = dj_tz.now() + datetime.timedelta(days=14)

            # Seed an AccountListEntry with initial CSV notes to test fallback + sync
            entry = em.AccountListEntry.objects.create(
                tenant=self.tenant,
                dedupe_key=address_hash(addr),
                address_hash=address_hash(addr),
                name="Whole Foods LA",
                address=addr,
                city="Los Angeles",
                state="CA",
                notes="Initial CSV note: park on level P2.",
            )

            past_req = em.Request.objects.create(
                tenant=self.tenant,
                created_by=self.admin,
                status=self.req_approved,
                request_type=self.req_type,
                timezone=self.tz_pdt,
                name="Past Visit",
                retailer_name="Whole Foods LA",
                address=addr,
                date=past_dt,
                start_time=past_dt,
                end_time=past_dt + datetime.timedelta(hours=4),
            )
            past_ev = em.Event.objects.create(
                tenant=self.tenant,
                request=past_req,
                event_type=self.ev_type,
                status=self.ev_approved,
                timezone=self.tz_pdt,
                name="Past Visit",
                address=addr,
                date=past_dt,
                start_time=past_dt,
                end_time=past_dt + datetime.timedelta(hours=4),
                created_by=self.admin,
            )

            future_req = em.Request.objects.create(
                tenant=self.tenant,
                created_by=self.admin,
                status=self.req_approved,
                request_type=self.req_type,
                timezone=self.tz_pdt,
                name="Upcoming Visit",
                retailer_name="Whole Foods Market LA",
                address=addr,
                date=future_dt,
                start_time=future_dt,
                end_time=future_dt + datetime.timedelta(hours=4),
            )
            future_ev = em.Event.objects.create(
                tenant=self.tenant,
                request=future_req,
                event_type=self.ev_type,
                status=self.ev_approved,
                timezone=self.tz_pdt,
                name="Upcoming Visit",
                address=addr,
                date=future_dt,
                start_time=future_dt,
                end_time=future_dt + datetime.timedelta(hours=4),
                created_by=self.admin,
            )

            tpl = CustomRecapTemplate.objects.create(
                tenant=self.tenant,
                event_type=self.ev_type,
                name="Retail Template",
                created_by=self.admin,
            )
            approved_recap = CustomRecap.objects.create(
                tenant=self.tenant,
                event=past_ev,
                custom_recap_template=tpl,
                name="Past Visit Recap",
                approved=True,
                total_engagements=100,
                created_by=self.admin,
            )

            ba_user = self.create_user(
                username="crew-ba-1",
                email="crew-ba-1@ba.test",
                role=self.roles["ambassador"],
                first_name="Maya",
                last_name="Lin",
            )
            ba = Ambassador.objects.create(user=ba_user, created_by=self.admin)
            AmbassadorEvent.objects.create(
                ambassador=ba,
                event=past_ev,
                tenant=self.tenant,
                is_approved=True,
                created_by=self.admin,
            )

            store_key = tenant_places(self.tenant.id).index.store_for_request(past_req.pk)
            return {
                "store_key": store_key,
                "addr": addr,
                "entry_id": entry.id,
                "past_req_id": past_req.id,
                "past_req_uuid": str(past_req.uuid),
                "recap_uuid": str(approved_recap.uuid),
                "future_ev_id": future_ev.id,
                "ba_id": ba.id,
            }

        seeded = await sync_to_async(_seed_store_and_crew)()
        store_key = seeded["store_key"]

        # 1. Before saving StoreLogisticsNote, storeProfile falls back to AccountListEntry.notes
        with patch(
            "utils.ai_text.generate_json", side_effect=AiUnavailable("off")
        ):
            prof_initial = await self._execute_mutation(
                STORE_PROFILE_QUERY,
                {"tenantId": str(self.tenant.id), "storeKey": store_key},
                user=self.client_user,
            )
        assert prof_initial.errors is None, prof_initial.errors
        assert (
            prof_initial.data["storeProfile"]["logisticsNotes"]
            == "Initial CSV note: park on level P2."
        )

        # 2. Cross-tenant user cannot save notes on this store
        bad_save = await self._execute_mutation(
            SAVE_LOGISTICS_MUTATION,
            {
                "input": {
                    "tenantId": str(self.tenant.id),
                    "storeKey": store_key,
                    "notes": "Hijack attempt",
                }
            },
            user=self.other_client,
        )
        assert bad_save.errors is None, bad_save.errors
        assert bad_save.data["saveStoreLogisticsNotes"]["success"] is False

        # 3. Brand user saves updated Store Logistics Notes -> syncs AccountListEntry & EventConfirmation brief
        updated_note = "Check in at receiving dock B; ask for Marcus; bring 2 bags of ice."
        saved = await self._execute_mutation(
            SAVE_LOGISTICS_MUTATION,
            {
                "input": {
                    "tenantId": str(self.tenant.id),
                    "storeKey": store_key,
                    "notes": updated_note,
                }
            },
            user=self.client_user,
        )
        assert saved.errors is None, saved.errors
        assert saved.data["saveStoreLogisticsNotes"]["success"] is True
        assert saved.data["saveStoreLogisticsNotes"]["logisticsNotes"] == updated_note

        def _check_sync_and_confirmation():
            entry = em.AccountListEntry.objects.get(id=seeded["entry_id"])
            assert entry.notes == updated_note

            conf = em.EventConfirmation.objects.create(
                tenant=self.tenant,
                ba_name="Maya Lin",
                ba_email="crew-ba-1@ba.test",
                store_name="Whole Foods LA",
                address=seeded["addr"],
                starts_at=dj_tz.now() + datetime.timedelta(days=2),
                ends_at=dj_tz.now() + datetime.timedelta(days=2, hours=4),
                timezone_name="America/Los_Angeles",
                event_type_label="Retail Sampling",
            )
            ctx = build_context(conf, em.EventConfirmation.STAGE_BOOKED)
            assert updated_note in ctx["brief"]

        await sync_to_async(_check_sync_and_confirmation)()

        # 4. resolveStoreKey resolves requestId (PK + UUID), recapId, address, and cross-tenant returns None
        for kwargs in (
            {"requestId": str(seeded["past_req_id"])},
            {"requestId": seeded["past_req_uuid"]},
            {"recapId": seeded["recap_uuid"]},
            {"address": seeded["addr"]},
        ):
            res = await self._execute_mutation(
                RESOLVE_STORE_KEY_QUERY,
                {"tenantId": str(self.tenant.id), **kwargs},
                user=self.client_user,
            )
            assert res.errors is None, res.errors
            assert res.data["resolveStoreKey"] == store_key

        cross_res = await self._execute_mutation(
            RESOLVE_STORE_KEY_QUERY,
            {"tenantId": str(self.tenant.id), "requestId": str(seeded["past_req_id"])},
            user=self.other_client,
        )
        assert cross_res.data["resolveStoreKey"] is None

        # 5. Rebook last crew onto the upcoming visit via StoreVisit.eventId
        with patch(
            "utils.ai_text.generate_json", side_effect=AiUnavailable("off")
        ):
            prof_after = await self._execute_mutation(
                STORE_PROFILE_QUERY,
                {"tenantId": str(self.tenant.id), "storeKey": store_key},
                user=self.admin,
            )
        upcoming_visit = next(
            v
            for v in prof_after.data["storeProfile"]["visits"]
            if v["status"] == "upcoming"
        )
        assert upcoming_visit["eventId"] == str(seeded["future_ev_id"])

        # Cross-tenant client cannot rebook
        denied_rebook = await self._execute_mutation(
            REBOOK_CREW_MUTATION,
            {"input": {"eventId": upcoming_visit["eventId"]}},
            user=self.other_client,
        )
        assert denied_rebook.data["rebookLastCrew"]["success"] is False

        # Admin rebooks last crew -> books Maya Lin onto future_ev!
        rebooked = await self._execute_mutation(
            REBOOK_CREW_MUTATION,
            {"input": {"eventId": upcoming_visit["eventId"]}},
            user=self.admin,
        )
        assert rebooked.errors is None, rebooked.errors
        assert rebooked.data["rebookLastCrew"]["success"] is True
        assert rebooked.data["rebookLastCrew"]["assignedCount"] == 1
        assert await sync_to_async(
            AmbassadorEvent.objects.filter(
                event_id=seeded["future_ev_id"],
                ambassador_id=seeded["ba_id"],
                is_approved=True,
            ).exists
        )()

    @pytest.mark.asyncio
    async def test_split_state_timezones_list_dedupe_keys_and_tenant_event_type_isolation(self):
        # 1. AccountListEntry with lat/lng saved via both list:<uuid> and list:<dedupe_key> (64-char sha256)
        def _seed_unvisited_entry():
            return em.AccountListEntry.objects.create(
                tenant=self.tenant,
                name="Publix Pensacola",
                address="2180 W Nine Mile Rd",
                city="Pensacola",
                state="FL",
                zip="32534",
                lat=30.5331,
                lng=-87.3112,
                position_status=em.AccountListEntry.POSITION_PROVIDED,
                address_hash=address_hash("2180 W Nine Mile Rd, Pensacola, FL 32534"),
                dedupe_key="a" * 64,
                notes="CSV note",
            )

        entry = await sync_to_async(_seed_unvisited_entry)()

        # Save via list:<dedupe_key> (emitted by sample_next.py)
        dedupe_store_key = f"list:{entry.dedupe_key}"
        saved_dedupe = await self._execute_mutation(
            SAVE_LOGISTICS_MUTATION,
            {
                "input": {
                    "tenantId": str(self.tenant.id),
                    "storeKey": dedupe_store_key,
                    "notes": "Ask for grocery manager on aisle 1.",
                }
            },
            user=self.client_user,
        )
        assert saved_dedupe.errors is None, saved_dedupe.errors
        assert saved_dedupe.data["saveStoreLogisticsNotes"]["success"] is True

        # Save via list:<uuid> (emitted by accountStores.ts)
        uuid_store_key = f"list:{entry.uuid}"
        saved_uuid = await self._execute_mutation(
            SAVE_LOGISTICS_MUTATION,
            {
                "input": {
                    "tenantId": str(self.tenant.id),
                    "storeKey": uuid_store_key,
                    "notes": "Updated via UUID key: check in at customer service.",
                }
            },
            user=self.client_user,
        )
        assert saved_uuid.errors is None, saved_uuid.errors
        assert saved_uuid.data["saveStoreLogisticsNotes"]["success"] is True

        # storeProfile works on unvisited list:<dedupe_key> and reflects the saved note
        prof_list = await self._execute_mutation(
            STORE_PROFILE_QUERY,
            {"tenantId": str(self.tenant.id), "storeKey": dedupe_store_key},
            user=self.client_user,
        )
        assert prof_list.errors is None, prof_list.errors
        assert (
            prof_list.data["storeProfile"]["logisticsNotes"]
            == "Updated via UUID key: check in at customer service."
        )

        # 2. Split-timezone states resolve via coordinates (Pensacola FL -> CDT, Knoxville TN -> EDT, El Paso TX -> MDT)
        split_wave = await self._execute_mutation(
            WAVE_MUTATION,
            {
                "input": {
                    "tenantId": str(self.tenant.id),
                    "requestTypeId": str(self.req_type.id),
                    "items": [
                        {
                            "storeName": "Publix Pensacola",
                            "address": "2180 W Nine Mile Rd, Pensacola, FL 32534",
                            "city": "Pensacola",
                            "state": "FL",
                            "latitude": 30.5331,
                            "longitude": -87.3112,
                            "date": "2026-07-18",
                            "startTime": "12:00",
                            "endTime": "16:00",
                        },
                        {
                            "storeName": "Food City Knoxville",
                            "address": "1950 Western Ave, Knoxville, TN 37921",
                            "city": "Knoxville",
                            "state": "TN",
                            "latitude": 35.9658,
                            "longitude": -83.9496,
                            "date": "2026-07-18",
                            "startTime": "12:00",
                            "endTime": "16:00",
                        },
                        {
                            "storeName": "Albertsons El Paso",
                            "address": "3100 N Mesa St, El Paso, TX 79902",
                            "city": "El Paso",
                            "state": "TX",
                            "latitude": 31.7802,
                            "longitude": -106.5034,
                            "date": "2026-07-18",
                            "startTime": "12:00",
                            "endTime": "16:00",
                        },
                    ],
                }
            },
            user=self.client_user,
        )
        assert split_wave.errors is None, split_wave.errors
        assert split_wave.data["createRequestsWave"]["successCount"] == 3

        def _check_split_timezones():
            by_name = {
                r.retailer_name: r.timezone.code
                for r in em.Request.objects.filter(tenant=self.tenant).select_related("timezone")
            }
            assert by_name["Publix Pensacola"] == "CDT"
            assert by_name["Food City Knoxville"] == "EDT"
            assert by_name["Albertsons El Paso"] == "MDT"

        await sync_to_async(_check_split_timezones)()

        # 3. Tenant with zero EventTypes (`self.other`) leaves Event.event_type_id=None
        # instead of borrowing `self.tenant`'s EventType
        other_wave = await self._execute_mutation(
            WAVE_MUTATION,
            {
                "input": {
                    "tenantId": str(self.other.id),
                    "requestTypeId": str(self.other_req_type.id),
                    "items": [
                        {
                            "storeName": "Other Brand Store",
                            "address": "500 Congress Ave, Austin, TX 78701",
                            "state": "TX",
                            "date": "2026-07-19",
                            "startTime": "11:00",
                            "endTime": "15:00",
                        }
                    ],
                }
            },
            user=self.other_client,
        )
        assert other_wave.errors is None, other_wave.errors
        assert other_wave.data["createRequestsWave"]["successCount"] == 1

        def _check_other_event_type():
            ev = em.Event.objects.get(tenant=self.other)
            assert ev.event_type_id is None

        await sync_to_async(_check_other_event_type)()
