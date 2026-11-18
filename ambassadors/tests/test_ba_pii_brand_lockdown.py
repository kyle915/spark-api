"""
Brand users can't list or scrape Brand Ambassador (BA) personal data.

Kyle: "We don't want brands to be able to scrape any BA info."

The web app talks to ONE schema (clients) for Ignite admins and brand users
alike, so every check here runs through the real ``schema_clients`` with a
brand ("client") user and an Ignite admin side by side:

- BA listings (activeAmbassadors, ambassadors, availableAmbassadors,
  pendingAmbassadors, ambassador, groups, invitations, gig history, profile
  detail, attendances, favorites, job applications, walk-ups, Wingspan
  payments, users) come back empty / null for the brand user and populated
  for the admin.
- BA personal fields reached through a brand's OWN recap or event
  (Ambassador.phone/address/aboutMe/bio/college/coordinates/tShirtSize/
  resumeUrl/location, the BA's user email/username, EventAmbassador.email)
  come back blank for the brand user, real for the admin; the BA's display
  name and headshot stay.
- activeAmbassadors is always paged (no more one-page-of-everything).
- The brand's expense-receipts CSV carries no BA email.
"""

from datetime import datetime, timedelta, timezone as _tz
from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async

from ambassadors.models import (
    Ambassador,
    AmbassadorEvent,
    AmbassadorGroup,
    AmbassadorInvitation,
    Attendance,
)
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models

BA_PHONE = "512-555-0142"
BA_ADDRESS = "1200 Barton Springs Rd, Austin, TX 78704"
BA_EMAIL = "megan.born@example.com"


def _edges(conn):
    return [e["node"] for e in (conn or {}).get("edges", [])]


LISTINGS = """
query Listings($uuid: ID) {
  activeAmbassadors(first: 50) { totalCount edges { node { uuid phone } } }
  ambassadors(first: 50) { edges { node { uuid } } }
  availableAmbassadors(first: 50) { edges { node { uuid } } }
  pendingAmbassadors(first: 50) { edges { node { uuid } } }
  ambassador(uuid: $uuid) { uuid phone }
  ambassadorGroups(first: 50) { edges { node { uuid } } }
  sentInvitations(first: 50) { edges { node { uuid email } } }
  attendances(first: 50) { edges { node { uuid coordinates } } }
}
"""

ACTIVE_NO_ARGS = """
query { activeAmbassadors { totalCount edges { node { uuid } } } }
"""

PROFILE_DETAIL = """
query Detail($uuid: ID!, $tenantId: ID) {
  ambassadorProfileDetail(ambassadorUuid: $uuid, tenantId: $tenantId) {
    fullName email phone
  }
  ambassadorGigHistory(ambassadorUuid: $uuid, tenantId: $tenantId) {
    eventUuid
  }
}
"""

FAVORITES = """
query Favs($tenantId: ID) { favoriteAmbassadors(tenantId: $tenantId) { uuid email } }
"""

USERS = """
query Users($tenantId: ID) {
  users(first: 100, filters: { tenantId: $tenantId }) {
    edges { node { uuid email username ambassador { phone address } } }
  }
}
"""

USER_ONE = """
query One($uuid: ID) { user(uuid: $uuid) { uuid email } }
"""

WALKUPS = """
query { walkupShifts { ambassadorEventUuid ambassadorEmail ambassadorPhone } }
"""

WINGSPAN = """
query { wingspanPayments { id contractorEmail } }
"""

RECAP_BA = """
query Recap($uuid: ID!) {
  recap(uuid: $uuid) {
    uuid
    ambassador {
      uuid phone address aboutMe bio college inCollege coordinates
      tShirtSize resumeUrl headshotUrl rating
      location { id }
      user { firstName lastName email username }
    }
  }
}
"""

CUSTOM_RECAP_BA = """
query CustomRecap($uuid: ID!) {
  customRecap(uuid: $uuid) {
    uuid
    ambassador {
      uuid phone address
      user { firstName lastName email username }
    }
  }
}
"""

EVENT_ROSTER = """
query Ev($uuid: ID!) {
  event(uuid: $uuid) { uuid ambassadors { uuid name email } }
}
"""


class _BaPiiFixture(AmbassadorsGraphQLTestCase):
    """Shared fixture: one brand, its client user + colleague, an Ignite
    admin, and a BA (with full personal data) booked and recapped there."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()

        self.tenant = self.create_tenant(name="PII Brand")

        self.admin = self.create_user(
            username="pii-admin",
            email="pii-admin@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="pii-client",
            email="pii-client@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        self.colleague = self.create_user(
            username="pii-colleague",
            email="pii-colleague@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.colleague, self.tenant)

        # The BA works this brand (TenantedUser + a booking + recaps).
        self.ba_user = self.create_user(
            username=BA_EMAIL,
            email=BA_EMAIL,
            role=self.roles["ambassador"],
        )
        self.ba_user.first_name = "Megan"
        self.ba_user.last_name = "Born"
        self.ba_user.save(update_fields=["first_name", "last_name"])
        self.create_tenanted_user(self.ba_user, self.tenant)
        self.ba = self.create_ambassador(
            self.ba_user,
            address=BA_ADDRESS,
            coordinates=[30.26, -97.77],
            phone=BA_PHONE,
            about_me="About me",
            bio="Bio text",
            college="UT Austin",
            in_college=True,
            t_shirt_size="M",
            resume="ba/resumes/megan.pdf",
            headshot="ba/headshots/megan.jpg",
            rating=5,
        )
        # A pending (not yet approved) BA for pendingAmbassadors.
        pending_user = self.create_user(
            username="pending-ba@example.com",
            email="pending-ba@example.com",
            role=self.roles["ambassador"],
        )
        self.create_tenanted_user(pending_user, self.tenant)
        self.pending_ba = self.create_ambassador(pending_user, is_active=False)

        now = datetime.now(_tz.utc)
        self.event = self.create_event(
            name="HEB Barton Creek",
            tenant=self.tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        self.ae = AmbassadorEvent.objects.create(
            ambassador=self.ba,
            event=self.event,
            tenant=self.tenant,
            is_approved=True,
            created_by=self.sys,
        )
        Attendance.objects.create(
            ambassador=self.ba,
            event=self.event,
            clock_time=now,
            coordinates=[30.26, -97.77],
        )
        from ambassadors.models import GroupType

        group_type = GroupType.objects.create(name="Crew", created_by=self.sys)
        AmbassadorGroup.objects.create(
            name="Austin crew",
            group_type=group_type,
            tenant=self.tenant,
            created_by=self.sys,
        )
        AmbassadorInvitation.objects.create(
            email="invitee@example.com",
            tenant=self.tenant,
            invited_by=self.admin,
            expires_at=now + timedelta(days=7),
            created_by=self.sys,
        )
        from jobs.models import TenantFavoriteAmbassador

        TenantFavoriteAmbassador.objects.create(
            tenant=self.tenant, ambassador=self.ba
        )

        self.recap = recap_models.Recap.objects.create(
            name="Legacy recap",
            approved=True,
            event=self.event,
            ambassador=self.ba,
            created_by=self.sys,
            updated_by=self.sys,
        )
        event_type = self.create_event_type(name="Sampling", tenant=self.tenant)
        template = recap_models.CustomRecapTemplate.objects.create(
            name="Template",
            event_type=event_type,
            tenant=self.tenant,
            created_by=self.sys,
        )
        self.custom_recap = recap_models.CustomRecap.objects.create(
            name="Custom recap",
            approved=True,
            event=self.event,
            tenant=self.tenant,
            ambassador=self.ba,
            custom_recap_template=template,
            created_by=self.sys,
            updated_by=self.sys,
        )

    def _user(self, who):
        return {"client": self.client_user, "admin": self.admin, "ba": self.ba_user}[who]

    async def _run(self, query, variables=None, who="client"):
        return await self._execute_mutation(
            query, variables or {}, self.endpoint_path, user=self._user(who)
        )



@pytest.mark.django_db(transaction=True)
class TestBaPiiBrandLockdown(_BaPiiFixture):
    # ── listings ─────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_brand_user_lists_no_ambassadors(self):
        result = await self._run(LISTINGS, {"uuid": str(self.ba.uuid)}, "client")
        assert result.errors is None, result.errors
        data = result.data
        assert data["activeAmbassadors"]["totalCount"] == 0
        for key in (
            "activeAmbassadors",
            "ambassadors",
            "availableAmbassadors",
            "pendingAmbassadors",
            "ambassadorGroups",
            "sentInvitations",
            "attendances",
        ):
            assert _edges(data[key]) == [], key
        assert data["ambassador"] is None

    @pytest.mark.asyncio
    async def test_admin_still_lists_ambassadors(self):
        result = await self._run(LISTINGS, {"uuid": str(self.ba.uuid)}, "admin")
        assert result.errors is None, result.errors
        data = result.data
        active = {n["uuid"]: n for n in _edges(data["activeAmbassadors"])}
        assert active[str(self.ba.uuid)]["phone"] == BA_PHONE
        assert str(self.ba.uuid) in {n["uuid"] for n in _edges(data["ambassadors"])}
        assert str(self.pending_ba.uuid) in {
            n["uuid"] for n in _edges(data["pendingAmbassadors"])
        }
        assert data["ambassador"]["phone"] == BA_PHONE
        assert _edges(data["ambassadorGroups"])
        assert [n["email"] for n in _edges(data["sentInvitations"])] == [
            "invitee@example.com"
        ]
        attendance = _edges(data["attendances"])
        assert attendance and attendance[0]["coordinates"] == [30.26, -97.77]

    @pytest.mark.asyncio
    async def test_active_ambassadors_is_always_paged(self):
        def seed():
            for i in range(205):
                u = self.create_user(
                    username=f"bulk-ba-{i}",
                    email=f"bulk-ba-{i}@example.com",
                    role=self.roles["ambassador"],
                )
                Ambassador.objects.create(
                    user=u, is_active=True, created_by=self.sys
                )

        await sync_to_async(seed)()
        result = await self._run(ACTIVE_NO_ARGS, who="admin")
        assert result.errors is None, result.errors
        conn = result.data["activeAmbassadors"]
        assert conn["totalCount"] >= 206
        assert len(conn["edges"]) == 200

    @pytest.mark.asyncio
    async def test_profile_detail_and_gig_history(self):
        variables = {"uuid": str(self.ba.uuid), "tenantId": str(self.tenant.id)}
        brand = await self._run(PROFILE_DETAIL, variables, "client")
        assert brand.errors is None, brand.errors
        assert brand.data["ambassadorProfileDetail"] is None
        assert brand.data["ambassadorGigHistory"] == []

        admin = await self._run(PROFILE_DETAIL, variables, "admin")
        assert admin.errors is None, admin.errors
        assert admin.data["ambassadorProfileDetail"]["phone"] == BA_PHONE
        assert admin.data["ambassadorProfileDetail"]["email"] == BA_EMAIL
        assert admin.data["ambassadorGigHistory"]

    @pytest.mark.asyncio
    async def test_favorites(self):
        variables = {"tenantId": str(self.tenant.id)}
        brand = await self._run(FAVORITES, variables, "client")
        assert brand.errors is None, brand.errors
        assert brand.data["favoriteAmbassadors"] == []

        admin = await self._run(FAVORITES, variables, "admin")
        assert admin.errors is None, admin.errors
        assert [f["email"] for f in admin.data["favoriteAmbassadors"]] == [BA_EMAIL]

    @pytest.mark.asyncio
    async def test_users_hides_bas_from_brand(self):
        variables = {"tenantId": str(self.tenant.id)}
        brand = await self._run(USERS, variables, "client")
        assert brand.errors is None, brand.errors
        emails = {n["email"] for n in _edges(brand.data["users"])}
        # Colleagues stay (with their emails); BAs are gone.
        assert "pii-colleague@test.com" in emails
        assert BA_EMAIL not in emails
        assert "pending-ba@example.com" not in emails

        one = await self._run(USER_ONE, {"uuid": str(self.ba_user.uuid)}, "client")
        assert one.errors, "brand user must not read a BA's user record"

        admin = await self._run(USERS, variables, "admin")
        assert admin.errors is None, admin.errors
        by_email = {n["email"]: n for n in _edges(admin.data["users"])}
        assert by_email[BA_EMAIL]["ambassador"]["phone"] == BA_PHONE

    @pytest.mark.asyncio
    async def test_walkups_and_wingspan_are_admin_only(self):
        def seed():
            AmbassadorEvent.objects.filter(pk=self.ae.pk).update(
                source=AmbassadorEvent.SOURCE_WALKUP
            )

        await sync_to_async(seed)()
        brand = await self._run(WALKUPS, who="client")
        assert brand.errors is None, brand.errors
        assert brand.data["walkupShifts"] == []

        admin = await self._run(WALKUPS, who="admin")
        assert admin.errors is None, admin.errors
        rows = admin.data["walkupShifts"]
        assert rows and rows[0]["ambassadorEmail"] == BA_EMAIL
        assert rows[0]["ambassadorPhone"] == BA_PHONE

        from wingspan import client as ws_client

        class _P:
            id = "p1"
            contractor_name = "Megan Born"
            contractor_email = BA_EMAIL
            amount = 120.0
            status = "paid"
            pay_date = None
            period_id = None
            memo = None

        async def fake_list_payments(**_kw):
            return [_P()]

        with patch.object(ws_client, "list_payments", fake_list_payments):
            brand = await self._run(WINGSPAN, who="client")
            assert brand.errors is None, brand.errors
            assert brand.data["wingspanPayments"] == []
            admin = await self._run(WINGSPAN, who="admin")
            assert admin.errors is None, admin.errors
            assert admin.data["wingspanPayments"][0]["contractorEmail"] == BA_EMAIL

    # ── BA fields on the brand's own recap / event ───────────────────

    @pytest.mark.asyncio
    async def test_recap_ambassador_fields_blank_for_brand(self):
        with patch("ambassadors.types.public_url", lambda b: f"https://cdn.test/{b}"):
            result = await self._run(
                RECAP_BA, {"uuid": str(self.recap.uuid)}, "client"
            )
        assert result.errors is None, result.errors
        amb = result.data["recap"]["ambassador"]
        # Display name, avatar and rating stay.
        assert amb["user"]["firstName"] == "Megan"
        assert amb["user"]["lastName"] == "Born"
        assert amb["headshotUrl"]
        assert amb["rating"] == 5
        # Everything personal is blank.
        assert amb["phone"] is None
        assert amb["address"] is None
        assert amb["aboutMe"] is None
        assert amb["bio"] == ""
        assert amb["college"] == ""
        assert amb["inCollege"] is False
        assert amb["coordinates"] == []
        assert amb["tShirtSize"] is None
        assert amb["resumeUrl"] is None
        assert amb["location"] is None
        assert amb["user"]["email"] == ""
        assert amb["user"]["username"] == ""

    @pytest.mark.asyncio
    async def test_recap_ambassador_fields_real_for_admin(self):
        with patch("ambassadors.types.public_url", lambda b: f"https://cdn.test/{b}"):
            result = await self._run(
                RECAP_BA, {"uuid": str(self.recap.uuid)}, "admin"
            )
        assert result.errors is None, result.errors
        amb = result.data["recap"]["ambassador"]
        assert amb["phone"] == BA_PHONE
        assert amb["address"] == BA_ADDRESS
        assert amb["bio"] == "Bio text"
        assert amb["college"] == "UT Austin"
        assert amb["tShirtSize"] == "M"
        assert amb["coordinates"] == [30.26, -97.77]
        assert amb["resumeUrl"]
        assert amb["user"]["email"] == BA_EMAIL
        assert amb["user"]["username"] == BA_EMAIL

    @pytest.mark.asyncio
    async def test_custom_recap_ambassador_fields(self):
        variables = {"uuid": str(self.custom_recap.uuid)}
        brand = await self._run(CUSTOM_RECAP_BA, variables, "client")
        assert brand.errors is None, brand.errors
        amb = brand.data["customRecap"]["ambassador"]
        assert amb["user"]["firstName"] == "Megan"
        assert amb["phone"] is None and amb["address"] is None
        assert amb["user"]["email"] == "" and amb["user"]["username"] == ""

        admin = await self._run(CUSTOM_RECAP_BA, variables, "admin")
        assert admin.errors is None, admin.errors
        amb = admin.data["customRecap"]["ambassador"]
        assert amb["phone"] == BA_PHONE
        assert amb["user"]["email"] == BA_EMAIL

    @pytest.mark.asyncio
    async def test_event_roster_email_blank_for_brand(self):
        variables = {"uuid": str(self.event.uuid)}
        brand = await self._run(EVENT_ROSTER, variables, "client")
        assert brand.errors is None, brand.errors
        roster = brand.data["event"]["ambassadors"]
        assert roster and roster[0]["name"] == "Megan Born"
        assert roster[0]["email"] == ""

        admin = await self._run(EVENT_ROSTER, variables, "admin")
        assert admin.errors is None, admin.errors
        assert admin.data["event"]["ambassadors"][0]["email"] == BA_EMAIL

    @pytest.mark.asyncio
    async def test_email_standing_in_for_a_name_is_hidden_from_brand(self):
        """A walk-up BA with no name on file: the roster name used to fall
        back to the login email (which embeds the phone number)."""

        def seed():
            self.ba_user.first_name = ""
            self.ba_user.last_name = ""
            self.ba_user.save(update_fields=["first_name", "last_name"])

        await sync_to_async(seed)()
        variables = {"uuid": str(self.event.uuid)}
        brand = await self._run(EVENT_ROSTER, variables, "client")
        assert brand.errors is None, brand.errors
        assert brand.data["event"]["ambassadors"][0]["name"] == "Ambassador"

        admin = await self._run(EVENT_ROSTER, variables, "admin")
        assert admin.data["event"]["ambassadors"][0]["name"] == BA_EMAIL

    @pytest.mark.asyncio
    async def test_brand_colleague_email_untouched(self):
        """Masking is for BAs only: a brand user's own team keeps emails."""
        result = await self._run(
            USER_ONE, {"uuid": str(self.colleague.uuid)}, "client"
        )
        assert result.errors is None, result.errors
        assert result.data["user"]["email"] == "pii-colleague@test.com"

    @pytest.mark.asyncio
    async def test_ba_sees_own_fields(self):
        """A BA reading their own record keeps their personal data."""
        result = await self._run(
            "query { me { email username ambassador { phone address } } }",
            who="ba",
        )
        assert result.errors is None, result.errors
        me = result.data["me"]
        assert me["email"] == BA_EMAIL
        assert me["ambassador"]["phone"] == BA_PHONE

    # ── exports ──────────────────────────────────────────────────────

    @pytest.mark.asyncio
    @pytest.mark.parametrize("who", ["client", "admin"])
    async def test_expense_receipts_csv_ba_email(self, who):
        """exportExpenseReceipts: the brand's CSV has the BA's name but no
        email (nor an email standing in for a missing name); admins keep it."""
        rows = [
            {
                "event_date": "2026-11-01",
                "ba_name": "Megan Born",
                "ba_email": BA_EMAIL,
                "event_name": "HEB",
                "address": "",
                "amount": 10.0,
                "amount_recorded": None,
                "amount_adjusted": False,
                "corpo_card": False,
                "files": [],
                "recap_uuid": "r1",
            },
            {
                "event_date": "2026-11-02",
                "ba_name": "walkup-5125550142@checkin.example",
                "ba_email": "walkup-5125550142@checkin.example",
                "event_name": "HEB",
                "address": "",
                "amount": 5.0,
                "amount_recorded": None,
                "amount_adjusted": False,
                "corpo_card": False,
                "files": [],
                "recap_uuid": "r2",
            },
        ]
        mutation = """
        mutation Exp($input: ExportExpenseReceiptsInput!) {
          exportExpenseReceipts(input: $input) { success message csvText }
        }
        """
        variables = {
            "input": {
                "startDate": "2026-11-01",
                "endDate": "2026-11-30",
                "tenantId": str(self.tenant.id),
            }
        }
        with patch(
            "recaps.receipts_export.collect_expense_rows", lambda *a, **k: rows
        ), patch(
            "recaps.receipts_export.build_receipts_bundle_pdf",
            lambda **k: b"%PDF",
        ), patch("utils.gcs.upload_bytes", lambda *a, **k: None), patch(
            "utils.gcs.public_url", lambda blob: f"https://x/{blob}"
        ):
            result = await self._run(mutation, variables, who)
        assert result.errors is None, result.errors
        payload = result.data["exportExpenseReceipts"]
        assert payload["success"], payload["message"]
        csv_text = payload["csvText"]
        assert "Megan Born" in csv_text
        if who == "client":
            assert BA_EMAIL not in csv_text
            assert "5125550142" not in csv_text
        else:
            assert BA_EMAIL in csv_text


STAFFING = """
query Staff($eventId: ID!) {
  eventStaffing(eventId: $eventId) { assigned suggestions { uuid email phone } }
  staffingSuggestions(eventId: $eventId) { baId name }
}
"""

LIVE_BOARD = """
query Board($tenantId: ID) {
  liveShiftBoard(tenantId: $tenantId) {
    eventUuid
    assigned { name lat lng locationAt locationSource accuracyMeters }
  }
}
"""

MISSING = """
query Missing($tenantId: ID) {
  missingRecapEvents(tenantId: $tenantId, lookbackDays: 30) {
    eventUuid
    assignedAmbassadors { name email }
  }
}
"""

NOTES = """
query Notes {
  ambassadorNotes(first: 50) { edges { node { uuid note } } }
}
"""


@pytest.mark.django_db(transaction=True)
class TestBaPiiBrandLockdownOps(_BaPiiFixture):
    """Staffing, live board, missing recaps and notes for the same fixture."""

    @pytest.mark.asyncio
    async def test_staffing_is_admin_only(self):
        variables = {"eventId": str(self.event.uuid)}
        brand = await self._run(STAFFING, variables, "client")
        assert brand.errors is None, brand.errors
        assert brand.data["eventStaffing"] is None
        assert brand.data["staffingSuggestions"] == []

        admin = await self._run(STAFFING, variables, "admin")
        assert admin.errors is None, admin.errors
        assert admin.data["eventStaffing"]["assigned"] == 1

    @pytest.mark.asyncio
    async def test_live_board_hides_ba_location_from_brand(self):
        from ambassadors.models import LocationPing

        def seed():
            LocationPing.objects.create(
                ambassador=self.ba,
                event=self.event,
                lat=30.26,
                lng=-97.77,
                accuracy_meters=5.0,
                recorded_at=datetime.now(_tz.utc),
                source="foreground",
            )

        await sync_to_async(seed)()
        variables = {"tenantId": str(self.tenant.id)}
        admin = await self._run(LIVE_BOARD, variables, "admin")
        assert admin.errors is None, admin.errors
        admin_rows = [a for s in admin.data["liveShiftBoard"] for a in s["assigned"]]
        assert admin_rows, "fixture BA should be on today's board"
        assert any(a["lat"] is not None for a in admin_rows)

        brand = await self._run(LIVE_BOARD, variables, "client")
        assert brand.errors is None, brand.errors
        rows = [a for s in brand.data["liveShiftBoard"] for a in s["assigned"]]
        assert rows, "brand still sees who is on the shift"
        assert {a["name"] for a in rows} == {"Megan Born"}
        for a in rows:
            assert a["lat"] is None and a["lng"] is None
            assert a["locationAt"] is None and a["accuracyMeters"] is None
            assert a["locationSource"] is None

    @pytest.mark.asyncio
    async def test_missing_recaps_hide_ba_email_from_brand(self):
        def seed():
            past = datetime.now(_tz.utc) - timedelta(days=2)
            ev = self.create_event(
                name="Past shift",
                tenant=self.tenant,
                date=past,
                start_time=past,
                end_time=past + timedelta(hours=3),
            )
            AmbassadorEvent.objects.create(
                ambassador=self.ba,
                event=ev,
                tenant=self.tenant,
                is_approved=True,
                created_by=self.sys,
            )

        await sync_to_async(seed)()
        variables = {"tenantId": str(self.tenant.id)}
        admin = await self._run(MISSING, variables, "admin")
        assert admin.errors is None, admin.errors
        admin_bas = [
            a for e in admin.data["missingRecapEvents"] for a in e["assignedAmbassadors"]
        ]
        assert BA_EMAIL in {a["email"] for a in admin_bas}

        brand = await self._run(MISSING, variables, "client")
        assert brand.errors is None, brand.errors
        bas = [a for e in brand.data["missingRecapEvents"] for a in e["assignedAmbassadors"]]
        assert bas, "brand still sees which shift is missing a recap"
        assert all(a["email"] is None for a in bas)
        assert {a["name"] for a in bas} == {"Megan Born"}

    @pytest.mark.asyncio
    async def test_ambassador_notes_hidden_from_brand(self):
        from ambassadors.models import AmbassadorNote

        def seed():
            AmbassadorNote.objects.create(
                note="Late twice", tenant=self.tenant, ambassador=self.ba,
                created_by=self.admin,
            )

        await sync_to_async(seed)()
        brand = await self._run(NOTES, who="client")
        assert brand.errors is None, brand.errors
        assert _edges(brand.data["ambassadorNotes"]) == []

        admin = await self._run(NOTES, who="admin")
        assert admin.errors is None, admin.errors
        assert [n["note"] for n in _edges(admin.data["ambassadorNotes"])] == [
            "Late twice"
        ]
