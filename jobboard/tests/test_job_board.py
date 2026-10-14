"""Public BA job board: listing, booking, emails, admin controls, check-in."""

import io
import json
from datetime import datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from asgiref.sync import sync_to_async
from config.schema_client import schema_clients
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client
from django.utils import timezone

from ambassadors.checkin_web import get_or_create_checkin_ambassador
from ambassadors.models import AmbassadorEvent
from events import models as em
from events.plan_results import auto_attach_on_approval, build_plan_results
from events.tests.base import EventsGraphQLTestCase
from jobboard.config import TORCH_JOB_BOARD
from jobboard.gigs import board_for_tenant, list_gigs
from jobboard.mail import (
    JobBoardBookedMailer,
    JobBoardBookingInternalMailer,
    JobBoardCancelledMailer,
)
from jobboard.models import JobBoard, JobBoardBooking, JobBoardGig
from jobboard.schema import (
    CancelJobBoardBookingInput,
    PostJobBoardPlanInput,
    SetJobBoardGigInput,
    _cancel,
    _post_plan,
    _set_gig,
    build_admin_board,
)
from jobs.models import AmbassadorJob
from recaps import models as rm
from utils.mailer import Mailer, is_placeholder_recipient_email

MIAMI = ZoneInfo("America/New_York")


@pytest.fixture
def sent():
    envelopes = []

    def _capture(self, *args, **kwargs):
        envelopes.append((type(self), self.envelope()))

    with patch.object(Mailer, "send", autospec=True, side_effect=_capture), patch.object(
        Mailer, "send_now", autospec=True, side_effect=_capture
    ):
        yield envelopes


def _of(sent, cls):
    return [env for kind, env in sent if kind is cls]


@pytest.mark.django_db(transaction=True)
class TestJobBoard(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        cache.clear()
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(
            name="Torch THC",
            slug="torch-thc",
            request_url_name="keee-torch-thc",
            checkin_code="TH-2HRV3D",
            checkin_resources=[
                {"label": "BA Sampling Guide", "url": "/training/torch/ba-sampling-guide.pdf", "kind": "pdf"},
                {"label": "Torch Demo Playbook", "url": "/training/torch/torch-demo-playbook.pdf", "kind": "pdf"},
                {"label": "Product Sales Sheets", "url": "/training/torch/product-sales-sheets.pdf", "kind": "pdf"},
            ],
        )
        self.other = self.create_tenant(name="Other Brand", slug="other-brand")
        self.request_type = em.RequestType.objects.create(
            tenant=self.tenant, name="Event Activation", created_by=self.sys
        )
        self.approved = em.EventStatus.objects.create(
            tenant=self.tenant, name="Approved", slug="approved", created_by=self.sys
        )
        self.cancelled = em.EventStatus.objects.create(
            tenant=self.tenant, name="Cancelled", slug="cancelled", created_by=self.sys
        )
        self.retail_type = self.create_event_type("Retail Sampling", self.tenant)
        self.activation_type = self.create_event_type("Torch THC-Event Activation", self.tenant)
        self.admin = self.create_user(
            username="ops",
            email="ops@igniteproductions.co",
            role=self.roles["spark_admin"],
            is_staff=True,
        )
        self.brand_user = self.create_user(
            username="alec", email="alec@torchdrinks.com", role=self.roles["client"]
        )
        self.create_tenanted_user(self.brand_user, self.tenant)
        self.today = timezone.localdate()
        self.board = board_for_tenant(self.tenant)

    # ── fixtures ──

    def _gig(
        self,
        name="Wynwood Art Walk",
        days_out=7,
        start=time(16, 0),
        end=time(20, 0),
        count=2,
        tenant=None,
        activity="event_activation",
        with_plan=True,
        address="250 NW 24th St, Miami, FL 33127",
        status=None,
    ):
        tenant = tenant or self.tenant
        day = self.today + timedelta(days=days_out)
        starts = datetime.combine(day, start, tzinfo=MIAMI)
        ends = datetime.combine(day, end, tzinfo=MIAMI)
        request_type = (
            self.request_type
            if tenant == self.tenant
            else em.RequestType.objects.create(tenant=tenant, name="Event Activation", created_by=self.sys)
        )
        request = em.Request.objects.create(
            name=name,
            date=starts,
            start_time=starts,
            end_time=ends,
            address=address,
            notes=f"BA count: {count}",
            request_type=request_type,
            tenant=tenant,
            created_by=self.sys,
        )
        if with_plan:
            em.FieldMarketingEvent.objects.create(
                tenant=tenant,
                request=request,
                market="miami",
                activity=activity,
                name=name,
                starts_on=day,
                address=address,
                sku_names=["Black Cherry 10mg", "Nonactive"],
                sampling_format="pour",
                needs_field_support=True,
                ambassador_count=count,
                support_scope="Sampling tent at the north entrance",
                status="submitted",
                created_by=self.sys,
            )
        return em.Event.objects.create(
            name=name,
            date=starts,
            start_time=starts,
            end_time=ends,
            address=address,
            tenant=tenant,
            request=request,
            status=status or (self.approved if tenant == self.tenant else None),
            event_type=self.retail_type if tenant == self.tenant else None,
            created_by=self.sys,
        )

    def _apply(self, gig_ids, *, name="Jordan Rivera", phone="(305) 555-0142", email="jordan@gmail.com", **extra):
        body = {"name": name, "phone": phone, "email": email, "gigIds": [str(g) for g in gig_ids], **extra}
        return Client().post(
            f"/api/public/jobs/{self.board.token}/apply",
            data=json.dumps(body),
            content_type="application/json",
        )

    def _board(self, token=None):
        return Client().get(f"/api/public/jobs/{token or self.board.token}")

    # ── listing ──

    def test_lists_only_upcoming_plan_gigs_for_torch(self):
        listed = self._gig("Wynwood Art Walk")
        self._gig("Retail demo", with_plan=False)
        self._gig("Last week", days_out=-7)
        self._gig("Called off", status=self.cancelled)
        gone = self._gig("Deleted plan")
        em.FieldMarketingEvent.objects.filter(request=gone.request).update(deleted_at=timezone.now())
        hidden = self._gig("Hidden one")
        JobBoardGig.objects.create(tenant=self.tenant, event=hidden, open_shifts=2, listed=False)
        zero = self._gig("No shifts")
        JobBoardGig.objects.create(tenant=self.tenant, event=zero, open_shifts=0)

        res = self._board()
        assert res.status_code == 200
        data = res.json()
        assert [g["venue"] for g in data["gigs"]] == ["Wynwood Art Walk"]
        gig = data["gigs"][0]
        assert gig["id"] == str(listed.uuid)
        assert gig["market"] == "Miami, FL"
        assert gig["typeLabel"] == "Event Activation"
        assert gig["timeLabel"].startswith("4:00 PM – 8:00 PM E")
        assert gig["spotsLeft"] == 2 and gig["openShifts"] == 2
        assert gig["skus"] == ["Black Cherry 10mg", "Nonactive"]
        assert data["brand"]["label"] == "Torch THC"
        # Street address stays out of the public payload.
        assert "250 NW 24th St" not in res.content.decode()

    def test_full_gig_shows_full_and_sorted_by_date(self):
        later = self._gig("Later gig", days_out=9, count=1)
        sooner = self._gig("Sooner gig", days_out=3, count=1)
        self._apply([later.uuid])
        data = self._board().json()
        assert [g["venue"] for g in data["gigs"]] == ["Sooner gig", "Later gig"]
        assert data["gigs"][1]["full"] is True and data["gigs"][1]["spotsLeft"] == 0
        assert data["gigs"][0]["id"] == str(sooner.uuid)

    def test_public_board_exposes_no_applicant_pii(self, sent):
        gig = self._gig()
        self._apply([gig.uuid], name="Jordan Rivera", phone="3055550142", email="jordan@gmail.com", note="I have a car")
        body = self._board().content.decode()
        for secret in ("Jordan", "Rivera", "3055550142", "555-0142", "jordan@gmail.com", "I have a car"):
            assert secret not in body

    def test_other_tenants_have_no_board(self):
        assert board_for_tenant(self.other) is None
        stray = JobBoard.objects.create(tenant=self.other)
        assert self._board(stray.token).status_code == 404
        assert self._board("not-a-token").status_code == 404
        self._gig("Other brand plan", tenant=self.other)
        assert self._board().json()["gigs"] == []

    # ── booking ──

    def test_apply_books_shift_and_emails_ba_and_ignite(self, sent):
        gig = self._gig()
        res = self._apply([gig.uuid], note="Can bring a tent")
        assert res.status_code == 200, res.content
        [result] = res.json()["results"]
        assert result["status"] == "booked"

        booking = JobBoardBooking.objects.get(event=gig)
        assert booking.phone == "3055550142" and booking.email == "jordan@gmail.com"
        ae = AmbassadorEvent.objects.get(event=gig, ambassador=booking.ambassador)
        assert ae.is_approved is True
        aj = AmbassadorJob.objects.get(ambassador=booking.ambassador, job__event=gig)
        assert aj.status.slug == "accepted"
        assert booking.confirmation.ba_email == "jordan@gmail.com"
        assert booking.confirmation.send_reminders is True

        [ba] = _of(sent, JobBoardBookedMailer)
        day = (self.today + timedelta(days=7)).strftime("%a, %b %-d")
        assert ba.subject == f"You're booked: Torch THC · Event Activation · {day}"
        assert ba.to_emails == ["jordan@gmail.com"]
        assert "staffing@igniteproductions.co" in ba.from_email
        html = ba.render_template()
        assert "https://client.igniteproductions.co/checkin/TH-2HRV3D" in html
        assert "250 NW 24th St, Miami, FL 33127" in html
        for label in ("BA Sampling Guide", "Torch Demo Playbook", "Product Sales Sheets"):
            assert label in html
        assert "What to bring" in html
        assert "Sampling tent at the north entrance" not in html
        assert "spark ba app" not in html.lower()

        [internal] = _of(sent, JobBoardBookingInternalMailer)
        assert internal.to_emails == list(TORCH_JOB_BOARD.internal_emails)
        assert set(internal.to_emails) == {
            "events@igniteproductions.co",
            "nevena@igniteproductions.co",
            "kyle@igniteproductions.co",
        }
        assert internal.headers["Reply-To"] == "jordan@gmail.com"
        ihtml = internal.render_template()
        for bit in ("Jordan Rivera", "(305) 555-0142", "jordan@gmail.com", "Wynwood Art Walk", "1 of 2 open", "Can bring a tent"):
            assert bit in ihtml

    def test_no_overbooking(self, sent):
        gig = self._gig(count=1)
        first = self._apply([gig.uuid])
        second = self._apply([gig.uuid], name="Sam Lee", phone="3055550199", email="sam@gmail.com")
        assert first.json()["results"][0]["status"] == "booked"
        assert second.json()["results"][0]["status"] == "full"
        assert AmbassadorEvent.objects.filter(event=gig, is_approved=True).count() == 1

    def test_admin_assigned_ba_counts_against_open_shifts(self, sent):
        gig = self._gig(count=1)
        staffed, _ = get_or_create_checkin_ambassador(
            first_name="Ops", last_name="Pick", phone="3055550100", email=None
        )
        AmbassadorEvent.objects.create(
            ambassador=staffed,
            event=gig,
            tenant=self.tenant,
            is_approved=True,
            created_by=self.sys,
            updated_by=self.sys,
        )
        assert list_gigs(self.tenant)[0].spots_left == 0
        assert self._apply([gig.uuid]).json()["results"][0]["status"] == "full"

    def test_duplicate_booking_blocked_by_phone_or_email(self, sent):
        gig = self._gig(count=3)
        self._apply([gig.uuid])
        again = self._apply([gig.uuid])
        by_email = self._apply([gig.uuid], phone="3055550777")
        assert again.json()["results"][0]["status"] == "already"
        assert by_email.json()["results"][0]["status"] == "already"
        assert JobBoardBooking.objects.filter(event=gig, cancelled_at__isnull=True).count() == 1

    def test_overlapping_gig_is_a_conflict_partial_success(self, sent):
        a = self._gig("Afternoon", start=time(14, 0), end=time(18, 0))
        b = self._gig("Overlap", start=time(17, 0), end=time(21, 0))
        c = self._gig("Next day", days_out=8)
        results = {r["id"]: r["status"] for r in self._apply([a.uuid, b.uuid, c.uuid]).json()["results"]}
        assert results == {str(a.uuid): "booked", str(b.uuid): "conflict", str(c.uuid): "booked"}

    def test_validation_and_honeypot(self, sent):
        gig = self._gig()
        assert self._apply([gig.uuid], phone="555-0142").status_code == 400
        assert self._apply([gig.uuid], email="nope").status_code == 400
        assert self._apply([gig.uuid], name="").status_code == 400
        assert self._apply([]).status_code == 400
        bot = self._apply([gig.uuid], website="http://spam")
        assert bot.status_code == 200 and bot.json()["results"] == []
        assert not JobBoardBooking.objects.exists()

    def test_unlisted_or_unknown_gig_is_unavailable(self, sent):
        gig = self._gig()
        JobBoardGig.objects.create(tenant=self.tenant, event=gig, open_shifts=2, listed=False)
        other = self._gig("Other brand", tenant=self.other)
        results = self._apply([gig.uuid, other.uuid]).json()["results"]
        assert [r["status"] for r in results] == ["unavailable", "unavailable"]

    # ── admin ──

    def test_admin_board_edit_and_cancel_reopens_slot(self, sent):
        gig = self._gig(count=1)
        self._apply([gig.uuid])
        board = build_admin_board(self.tenant)
        assert board.url == f"https://client.igniteproductions.co/jobs/{self.board.token}"
        [row] = board.gigs
        assert row.open_shifts == 1 and row.spots_left == 0
        assert row.bookings[0].phone == "(305) 555-0142"

        updated = _set_gig(
            self.admin,
            SetJobBoardGigInput(tenant_id=str(self.tenant.id), gig_id=str(gig.uuid), open_shifts=3),
        )
        assert updated.open_shifts == 3 and updated.spots_left == 2
        assert JobBoardGig.objects.get(event=gig).updated_by == self.admin

        booking = JobBoardBooking.objects.get(event=gig)
        _cancel(self.admin, CancelJobBoardBookingInput(tenant_id=str(self.tenant.id), booking_id=str(booking.uuid)))
        booking.refresh_from_db()
        assert booking.cancelled_at is not None
        assert not AmbassadorEvent.objects.filter(event=gig).exists()
        assert not AmbassadorJob.objects.filter(job__event=gig).exists()
        assert booking.confirmation.cancelled_at is not None
        assert list_gigs(self.tenant)[0].spots_left == 3
        [cancel] = _of(sent, JobBoardCancelledMailer)
        assert cancel.to_emails == ["jordan@gmail.com"]
        assert "cancelled" in cancel.render_template().lower()

        # The same BA can book again once the slot reopens.
        assert self._apply([gig.uuid]).json()["results"][0]["status"] == "booked"

    def test_board_and_booking_email_show_only_public_notes(self, sent):
        gig = self._gig()
        em.Request.objects.filter(id=gig.request_id).update(
            notes="*Please book Carlos Espana and Esmeralda Alvarado*"
        )
        em.FieldMarketingEvent.objects.filter(request=gig.request).update(
            notes="Please schedule Carlos, Pedro and Esmeralda",
            support_scope="I will send Carlos and Esmeralda a sheet",
        )
        body = self._board().content.decode()
        assert self._board().json()["gigs"][0]["brief"] == ""
        for internal in ("Carlos", "Esmeralda", "Sampling tent"):
            assert internal not in body

        updated = _set_gig(
            self.admin,
            SetJobBoardGigInput(
                tenant_id=str(self.tenant.id),
                gig_id=str(gig.uuid),
                public_notes="  Park in lot C. Black Torch tee.  ",
            ),
        )
        assert updated.public_notes == "Park in lot C. Black Torch tee."
        assert updated.open_shifts == 2 and updated.listed is True
        assert self._board().json()["gigs"][0]["brief"] == "Park in lot C. Black Torch tee."

        self._apply([gig.uuid])
        [ba] = _of(sent, JobBoardBookedMailer)
        html = ba.render_template()
        assert "Park in lot C. Black Torch tee." in html
        assert "250 NW 24th St, Miami, FL 33127" in html
        for internal in ("Carlos", "Esmeralda", "Pedro"):
            assert internal not in html

    def test_gig_city_comes_from_address_with_unit_after_zip(self):
        self._gig(
            "LOAD IN / SET UP for RARE Steak Championship Houston",
            address="10015 N Eldridge Pkwy, Houston, TX 77065 Unit C336",
        )
        [gig] = self._board().json()["gigs"]
        assert gig["market"] == "Houston, TX"
        assert "Eldridge" not in self._board().content.decode()

    def test_gig_without_parseable_address_falls_back_to_plan_market(self):
        self._gig("Pop-up", address="Somewhere downtown")
        assert self._board().json()["gigs"][0]["market"] == "Miami"

    def test_brand_users_cannot_reach_admin_controls(self):
        gig = self._gig()
        with pytest.raises(Exception):
            _set_gig(
                self.brand_user,
                SetJobBoardGigInput(tenant_id=str(self.tenant.id), gig_id=str(gig.uuid), open_shifts=9),
            )
        assert not JobBoardGig.objects.exists()

    @pytest.mark.asyncio
    async def test_graphql_job_board_query_is_admin_only(self):
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        await sync_to_async(self._gig)()
        query = """
        query JB($tenantId: ID) {
          jobBoard(tenantId: $tenantId) { available url gigs { venue openShifts spotsLeft bookings { name } } }
        }
        """
        admin = await self._execute_mutation_authenticated(query, {"tenantId": str(self.tenant.id)}, user=self.admin)
        assert admin.errors is None, admin.errors
        assert admin.data["jobBoard"]["available"] is True
        assert admin.data["jobBoard"]["gigs"][0]["venue"] == "Wynwood Art Walk"
        brand = await self._execute_mutation_authenticated(query, {"tenantId": str(self.tenant.id)}, user=self.brand_user)
        assert brand.errors is None, brand.errors
        assert brand.data["jobBoard"] == {"available": False, "url": None, "gigs": []}

    # ── walk-up clock-in ──

    def test_walkup_identify_lands_on_booked_gig(self, sent):
        gig = self._gig(days_out=0, start=time(23, 0), end=time(23, 30))
        self._apply([gig.uuid])
        res = Client().post(
            "/api/public/checkin/TH-2HRV3D/identify",
            data=json.dumps(
                {
                    "firstName": "Jordan Rivera",
                    "phone": "(305) 555-0142",
                    "email": "jordan@gmail.com",
                    "eventDate": self.today.isoformat(),
                    "eventTypeId": str(self.activation_type.id),
                    "address": "250 NW 24th St, Miami, FL 33127",
                }
            ),
            content_type="application/json",
        )
        assert res.status_code == 200, res.content
        gig.refresh_from_db()
        assert gig.event_type_id == self.activation_type.id
        assert em.Event.objects.filter(tenant=self.tenant).count() == 1
        ae = AmbassadorEvent.objects.get(event=gig)
        assert ae.is_approved is True

    # ── walk-up → plan gig ("Is this one of today's scheduled events?") ──

    def _plan_gig(self, **kwargs):
        gig = self._gig(days_out=0, start=time(23, 0), end=time(23, 30), **kwargs)
        em.Event.objects.filter(id=gig.id).update(event_type=self.activation_type)
        gig.refresh_from_db()
        return gig

    def _identify(self, code="TH-2HRV3D", event_type=None, **extra):
        body = {
            "firstName": "Sam Lee",
            "phone": "(305) 555-0199",
            "eventDate": self.today.isoformat(),
            "eventTypeId": str((event_type or self.activation_type).id),
            **extra,
        }
        return Client().post(
            f"/api/public/checkin/{code}/identify",
            data=json.dumps(body),
            content_type="application/json",
        )

    def _plan_gigs(self, code="TH-2HRV3D", event_type=None):
        return Client().post(
            f"/api/public/checkin/{code}/plan-gigs",
            data=json.dumps(
                {
                    "eventDate": self.today.isoformat(),
                    "eventTypeId": str((event_type or self.activation_type).id),
                }
            ),
            content_type="application/json",
        )

    def test_walkup_pick_list_shows_days_plan_gigs_without_street(self):
        gig = self._plan_gig(name="Dania Beach Food & Wine")
        self._gig(name="Next week fest", days_out=7)
        res = self._plan_gigs()
        assert res.status_code == 200
        gigs = res.json()["gigs"]
        assert [g["id"] for g in gigs] == [str(gig.id)]
        assert gigs[0]["name"] == "Dania Beach Food & Wine"
        assert gigs[0]["area"] == "Miami, FL"
        assert "250 NW" not in res.content.decode()
        assert self._plan_gigs(event_type=self.retail_type).json()["gigs"] == []

    def test_walkup_pick_joins_the_plan_event_from_any_address(self):
        gig = self._plan_gig()
        res = self._identify(planEventId=str(gig.id), address="1 Ocean Dr, Miami Beach, FL 33139")
        assert res.status_code == 200, res.content
        assert res.json()["event"]["uuid"] == str(gig.uuid)
        assert em.Event.objects.filter(tenant=self.tenant).count() == 1
        assert AmbassadorEvent.objects.filter(event=gig).count() == 1

    def test_walkup_no_answer_skips_plan_and_stale_pick_errors(self):
        gig = self._plan_gig()
        res = self._identify(planEventId="none", address="99 Biscayne Blvd, Miami, FL 33132")
        assert res.status_code == 200, res.content
        walkin = em.Event.objects.filter(tenant=self.tenant).exclude(id=gig.id).get()
        assert walkin.request_id is None

        stale = self._identify(planEventId="999999", address="99 Biscayne Blvd, Miami, FL 33132")
        assert stale.status_code == 400
        assert stale.json()["error"] == "plan_gig_unavailable"

    def test_walkup_same_city_joins_only_an_unambiguous_gig(self):
        gig = self._plan_gig()
        res = self._identify(address="99 Biscayne Blvd, Miami, FL 33132")
        assert res.status_code == 200, res.content
        assert em.Event.objects.filter(tenant=self.tenant).count() == 1
        assert AmbassadorEvent.objects.filter(event=gig).exists()

        self._plan_gig(name="Second Miami gig", address="1 SE 1st Ave, Miami, FL 33131")
        cache.clear()
        res = self._identify(phone="(305) 555-0111", address="500 Brickell Ave, Miami, FL 33131")
        assert res.status_code == 200, res.content
        assert em.Event.objects.filter(tenant=self.tenant, request__isnull=True).count() == 1

    def test_walkup_retail_store_never_joins_a_plan_by_city(self):
        gig = self._gig(days_out=0, start=time(23, 0), end=time(23, 30), activity="sales_support")
        res = self._identify(event_type=self.retail_type, address="99 Biscayne Blvd, Miami, FL 33132")
        assert res.status_code == 200, res.content
        assert not AmbassadorEvent.objects.filter(event=gig).exists()
        assert em.Event.objects.filter(tenant=self.tenant).count() == 2

    def test_team_recap_link_joins_plan_gig_and_agency_link_never_does(self):
        self.tenant.checkin_team_code = "TH-RHTGP9"
        self.tenant.checkin_recap_code = "TH-AGENCY"
        self.tenant.save(update_fields=["checkin_team_code", "checkin_recap_code"])
        gig = self._plan_gig()
        assert self._plan_gigs(code="TH-AGENCY").json()["gigs"] == []
        assert len(self._plan_gigs(code="TH-RHTGP9").json()["gigs"]) == 1
        res = self._identify(code="TH-RHTGP9", planEventId=str(gig.id), phone="")
        assert res.status_code == 200, res.content
        assert AmbassadorEvent.objects.filter(event=gig).count() == 1
        assert em.Event.objects.filter(tenant=self.tenant).count() == 1

    def test_walkup_recap_on_plan_event_reaches_plan_actuals(self):
        gig = self._plan_gig()
        assert self._identify(planEventId=str(gig.id)).status_code == 200
        ae = AmbassadorEvent.objects.get(event=gig)
        template = rm.CustomRecapTemplate.objects.create(
            name="Torch THC-Event Activation", event_type=self.activation_type,
            tenant=self.tenant, created_by=self.sys,
        )
        recap = rm.CustomRecap.objects.create(
            name="Fest recap", event=gig, tenant=self.tenant, ambassador=ae.ambassador,
            custom_recap_template=template, approved=True, created_by=self.sys,
        )
        line = em.ProductType.objects.create(tenant=self.tenant, name="Cans", created_by=self.sys)
        rm.CustomRecapProductSample.objects.create(
            custom_recap=recap,
            product=em.Product.objects.create(
                tenant=self.tenant, product_type=line, name="Black Cherry 10mg", created_by=self.sys
            ),
            quantity=95,
        )
        link = auto_attach_on_approval(kind="custom", recap_id=recap.id, actor=self.admin)
        plan = em.FieldMarketingEvent.objects.get(request_id=gig.request_id)
        assert link is not None and link.plan_id == plan.id
        assert build_plan_results(plan, is_admin=True)["counted"] == 1

    # ── waiting plans ──

    def _request_status(self, slug, name=None, **extra):
        return em.RequestStatus.objects.get_or_create(
            tenant=self.tenant,
            slug=slug,
            defaults={"name": name or slug.title(), "created_by": self.sys, **extra},
        )[0]

    def _waiting(self, name="RARE Steak Championship", slug="pending", days_out=2, count=3):
        status = self._request_status(slug) if slug else None
        self._request_status("approved")
        day = self.today + timedelta(days=days_out)
        starts = datetime.combine(day, time(17, 0), tzinfo=MIAMI)
        request = em.Request.objects.create(
            name=name,
            date=starts,
            start_time=starts,
            end_time=starts + timedelta(hours=4),
            address="1 Main St, Houston, TX 77002",
            notes=f"BA count: {count}",
            request_type=self.request_type,
            tenant=self.tenant,
            status=status,
            requestor_email="alec@torchdrinks.com",
            created_by=self.brand_user,
        )
        if status is None:
            em.Request.objects.filter(id=request.id).update(status=None)
            request.refresh_from_db()
        return em.FieldMarketingEvent.objects.create(
            tenant=self.tenant,
            request=request,
            market="houston",
            activity="event_activation",
            name=name,
            starts_on=day,
            address=request.address,
            needs_field_support=True,
            ambassador_count=count,
            status="submitted",
            created_by=self.brand_user,
        )

    def test_waiting_list_says_why_each_plan_is_off_the_board(self):
        self._waiting("Pending one", slug="pending", count=3)
        self._waiting("Queued one", slug="queue", count=2)
        self._waiting("Approved no event", slug="approved", count=1)
        self._waiting("Archived one", slug="archived")
        rows = {p.name: p for p in build_admin_board(self.tenant).pending}
        assert rows["Pending one"].reason == "pending"
        assert rows["Pending one"].action_label == "Approve & post"
        assert rows["Pending one"].open_shifts == 3
        assert rows["Queued one"].reason_label == "In Queue for Ignite: not approved yet"
        assert rows["Queued one"].action_label == "Approve & post"
        assert rows["Approved no event"].action_label == "Post to board"
        assert rows["Archived one"].action_label is None
        assert rows["Archived one"].reason_label == "Request is Archived"
        assert self._board().json()["gigs"] == []

    @pytest.mark.asyncio
    async def test_approve_and_post_lists_gig_and_sends_normal_approval(self, sent):
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        plan = await sync_to_async(self._waiting)(slug="queue", count=3)
        mutation = """
        mutation P($input: PostJobBoardPlanInput!) {
          postJobBoardPlan(input: $input) { success message }
        }
        """
        variables = {"input": {"tenantId": str(self.tenant.id), "planId": str(plan.uuid)}}
        brand = await self._execute_mutation_authenticated(mutation, variables, user=self.brand_user)
        assert brand.errors
        res = await self._execute_mutation_authenticated(mutation, variables, user=self.admin)
        assert res.errors is None, res.errors
        assert res.data["postJobBoardPlan"]["message"] == (
            "RARE Steak Championship is on the board with 3 open shifts."
        )

        def _check():
            request = em.Request.objects.select_related("status").get(id=plan.request_id)
            assert request.status.slug == "approved" and request.approved_by == self.admin
            assert em.RequestActivityLog.objects.filter(
                request=request, metadata__via="job_board"
            ).exists()
            [gig] = self._board().json()["gigs"]
            assert gig["venue"] == "RARE Steak Championship"
            assert gig["openShifts"] == 3 and gig["spotsLeft"] == 3
            assert build_admin_board(self.tenant).pending == []

        await sync_to_async(_check)()
        assert any(
            "alec@torchdrinks.com" in env.to_emails for _, env in sent
        ), "the submitter gets the same approval email approveRequest sends"

    def test_post_to_board_for_approved_request_sends_nothing(self, sent):
        plan = self._waiting(slug="approved", count=2)
        _, approved, _ = _post_plan(
            self.admin, PostJobBoardPlanInput(tenant_id=str(self.tenant.id), plan_id=str(plan.uuid))
        )
        assert approved is False
        assert em.Event.objects.filter(request_id=plan.request_id).count() == 1
        assert self._board().json()["gigs"][0]["openShifts"] == 2
        assert sent == []

    def test_archived_request_cannot_be_posted(self):
        plan = self._waiting(slug="archived")
        with pytest.raises(Exception, match="Request is Archived"):
            _post_plan(
                self.admin, PostJobBoardPlanInput(tenant_id=str(self.tenant.id), plan_id=str(plan.uuid))
            )
        assert not em.Event.objects.filter(request_id=plan.request_id).exists()

    def test_setup_command_posts_waiting_plans_silently(self, sent):
        a = self._waiting("Load in", slug="pending", count=2)
        b = self._waiting("Main day", slug="queue", days_out=3, count=4)
        self._waiting("Archived one", slug="archived")

        out = io.StringIO()
        call_command("setup_job_board", "--post-waiting", stdout=out)
        log = out.getvalue()
        assert "WOULD POST (dry run) 2 waiting plan(s)" in log
        assert "status='queue'" in log and "In Queue for Ignite" in log
        assert not em.Event.objects.filter(tenant=self.tenant).exists()

        out = io.StringIO()
        call_command("setup_job_board", "--post-waiting", "--apply", stdout=out)
        assert "POSTING 2 waiting plan(s)" in out.getvalue()
        for plan in (a, b):
            request = em.Request.objects.select_related("status").get(id=plan.request_id)
            assert request.status.slug == "approved"
            assert em.Event.objects.filter(request=request).count() == 1
        gigs = self._board().json()["gigs"]
        assert [(g["venue"], g["openShifts"]) for g in gigs] == [("Load in", 2), ("Main day", 4)]
        assert sent == []
        # Re-running finds nothing left to post.
        out = io.StringIO()
        call_command("setup_job_board", "--post-waiting", "--apply", stdout=out)
        assert "POSTING 0 waiting plan(s)" in out.getvalue()

    def test_plan_request_without_status_can_be_posted(self, sent):
        plan = self._waiting("Iron City Halloween", slug=None, count=2)
        [row] = build_admin_board(self.tenant).pending
        assert row.reason == "no_status"
        assert row.reason_label == "No status: never reached Approvals"
        assert row.action_label == "Approve & post"

        out = io.StringIO()
        call_command("setup_job_board", "--post-waiting", "--apply", stdout=out)
        assert "status=None" in out.getvalue()
        assert "Request statuses: approved" in out.getvalue()
        request = em.Request.objects.select_related("status").get(id=plan.request_id)
        assert request.status.slug == "approved"
        assert [(g["venue"], g["openShifts"]) for g in self._board().json()["gigs"]] == [
            ("Iron City Halloween", 2)
        ]
        assert sent == []

    def test_new_request_starts_pending_when_tenant_has_no_flagged_default(self):
        pending = self._request_status("pending", is_default=False)
        plan = self._waiting("Fresh plan", slug=None)
        request = em.Request.objects.create(
            name="Another",
            date=timezone.now(),
            address="1 Main St, Houston, TX 77002",
            request_type=self.request_type,
            tenant=self.tenant,
            created_by=self.sys,
        )
        assert request.status_id == pending.id
        assert plan.request.status_id is None

    def test_default_status_flag_is_per_tenant(self):
        mine = self._request_status("pending", is_default=True)
        theirs = em.RequestStatus.objects.create(
            tenant=self.other, name="Pending", slug="pending", is_default=True, created_by=self.sys
        )
        mine.refresh_from_db()
        assert mine.is_default is True and theirs.is_default is True
        second = em.RequestStatus.objects.create(
            tenant=self.tenant, name="Queue", slug="queue", is_default=True, created_by=self.sys
        )
        mine.refresh_from_db()
        assert mine.is_default is False and second.is_default is True

    # ── setup command ──

    def test_setup_command_mints_once_and_keeps_token(self):
        JobBoard.objects.filter(tenant=self.tenant).delete()
        self._gig("Wynwood Art Walk")

        out = io.StringIO()
        call_command("setup_job_board", stdout=out)
        assert "DRY-RUN" in out.getvalue() and "Wynwood Art Walk" in out.getvalue()
        assert not JobBoard.objects.filter(tenant=self.tenant).exists()

        call_command("setup_job_board", "--apply", stdout=io.StringIO())
        token = JobBoard.objects.get(tenant=self.tenant).token

        out = io.StringIO()
        call_command("setup_job_board", "--tenant", "keee-torch-thc", "--apply", stdout=out)
        assert JobBoard.objects.get(tenant=self.tenant).token == token
        assert f"https://client.igniteproductions.co/jobs/{token}" in out.getvalue()

        with pytest.raises(CommandError):
            call_command("setup_job_board", "--tenant", "other-brand", stdout=io.StringIO())


def test_walkup_stub_emails_are_placeholders():
    assert is_placeholder_recipient_email("checkin-3055550142@walkup.spark") is True
    assert is_placeholder_recipient_email("jordan@gmail.com") is False
