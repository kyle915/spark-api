"""`seed_brand_group_demo`: the FAMUS / Alcohol Armor / Wook Water walkthrough.

What matters: the demo is built through the real request → event → walk-up
recap → approval path, it never sends anything (email, push, Cloud Task,
calendar, Sheets, job board), it is idempotent, and `--purge` leaves zero
residue while leaving real rows alone.
"""

from __future__ import annotations

import io
from unittest import mock

import pytest
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError

from ambassadors.models import Ambassador
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import (
    Event,
    Request,
    RequestCoBrand,
    RequestProduct,
    RequestType,
    Retailer,
    State,
    TimeZone,
)
from jobs.models import Job
from recaps import co_activation
from recaps.models import (
    CustomFieldValue,
    CustomRecap,
    CustomRecapFile,
    CustomRecapProductSample,
    FileType,
)
from recaps.pdf import build_recap_pdf_html
from tenants import brand_group_demo
from tenants.brand_group_demo import DEMO_DOMAIN, PREFIX, SOURCE_LABEL
from tenants.brand_groups import build_brand_group_compare
from tenants.models import Tenant, User
from utils.mailer import MailDrivers
from utils.outbound import outbound_suppressed, suppress_outbound


def _run(*args) -> str:
    out = io.StringIO()
    call_command("seed_brand_group_demo", *args, stdout=out)
    return out.getvalue()


def _counts() -> dict[str, int]:
    return {
        "requests": Request.objects.count(),
        "request_products": RequestProduct.objects.count(),
        "co_brands": RequestCoBrand.objects.count(),
        "events": Event.objects.count(),
        "recaps": CustomRecap.objects.count(),
        "values": CustomFieldValue.objects.count(),
        "samples": CustomRecapProductSample.objects.count(),
        "files": CustomRecapFile.objects.count(),
        "retailers": Retailer.objects.count(),
        "ambassadors": Ambassador.objects.count(),
        "users": User.objects.count(),
        "jobs": Job.objects.count(),
    }


@pytest.mark.django_db(transaction=True)
class TestSeedBrandGroupDemo(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        sys_user = self.get_system_user()
        call_command(
            "setup_brand_group", apply=True, skip_images=True, stdout=io.StringIO()
        )
        FileType.objects.get_or_create(
            name="image", defaults={"created_by": sys_user, "extension": ".jpg"}
        )
        for code, name in (("GA", "Georgia"), ("TX", "Texas"), ("CO", "Colorado")):
            State.objects.get_or_create(
                code=code, defaults={"name": name, "created_by": sys_user}
            )
        for name, code, offset in (
            ("Eastern", "EDT", -4),
            ("Central", "CDT", -5),
            ("Mountain", "MDT", -6),
        ):
            TimeZone.objects.get_or_create(
                code=code,
                defaults={"name": name, "offset": offset, "created_by": sys_user},
            )
        self.famus = Tenant.objects.get(slug="famus-energy")
        self.aa = Tenant.objects.get(slug="alcohol-armor")
        self.ww = Tenant.objects.get(slug="wook-water")
        self.real_request = Request.objects.create(
            name="Real Kroger demo",
            address="1 Real St, Atlanta, GA 30324",
            request_type=RequestType.objects.filter(tenant=self.famus).first(),
            tenant=self.famus,
            created_by=sys_user,
        )

        self.uploaded: list[str] = []
        patches = [
            mock.patch.object(
                brand_group_demo.gcs,
                "upload_bytes",
                side_effect=lambda blob, *a, **k: self.uploaded.append(blob),
            ),
            mock.patch.object(
                brand_group_demo.gcs, "download_blob_bytes", return_value=None
            ),
            mock.patch.object(brand_group_demo.gcs, "delete_blob", return_value=True),
            mock.patch.object(MailDrivers, "send", autospec=True),
            mock.patch("resend.Emails.send"),
            mock.patch("utils.cloud_tasks.enqueue", return_value=False),
            mock.patch("ambassadors.push._send_push_to_user_sync"),
            mock.patch("events.signals.sync_event_to_all_connected_users"),
            mock.patch("utils.sheets_mirror.upsert_request_row"),
        ]
        self.mocks = [p.start() for p in patches]
        (
            self.driver_send,
            self.resend_send,
            self.enqueue,
            self.push,
            self.calendar,
            self.sheets,
        ) = self.mocks[3:]
        yield
        for p in patches:
            p.stop()

    def _assert_nothing_sent(self):
        for sink in (
            self.driver_send,
            self.resend_send,
            self.enqueue,
            self.push,
            self.calendar,
            self.sheets,
        ):
            assert not sink.called, sink
        assert mail.outbox == []
        assert Job.objects.count() == 0

    def test_dry_run_writes_nothing(self):
        before = _counts()
        log = _run()
        assert "would create" in log and "DRY RUN" in log
        assert _counts() == before
        self._assert_nothing_sent()

    def test_seed_builds_linked_approved_activations_and_sends_nothing(self):
        log = _run("--apply")
        self._assert_nothing_sent()
        assert "emails attempted: 0" in log

        demo = CustomRecap.objects.filter(source_label=SOURCE_LABEL)
        assert demo.count() == 11
        assert not demo.filter(approved=False).exists()
        assert all(r.name.startswith(PREFIX) for r in demo)
        assert all(r.ambassador.user.email.endswith(f"@{DEMO_DOMAIN}") for r in demo)
        assert all(
            r.ambassador.user.get_full_name().startswith("Demo BA ·") for r in demo
        )
        by_brand = {
            t.slug: demo.filter(tenant=t).count()
            for t in (self.famus, self.aa, self.ww)
        }
        assert by_brand == {"famus-energy": 4, "alcohol-armor": 3, "wook-water": 4}

        publix = demo.filter(name=f"{PREFIX}Publix #1543")
        assert {r.tenant.slug: r.total_engagements for r in publix} == {
            "wook-water": 66,
            "famus-energy": 49,
        }
        host = publix.get(tenant=self.ww)
        famus_slice = publix.get(tenant=self.famus)
        assert famus_slice.co_activation_parent_id == host.id
        assert co_activation.co_activation_brand_names(famus_slice) == ["Wook Water"]
        assert (
            sum(s.quantity for s in famus_slice.custom_recap_product_sample.all()) == 49
        )
        assert (
            famus_slice.custom_recap_files.count()
            == host.custom_recap_files.count()
            == 6
        )

        three_way = demo.filter(name=f"{PREFIX}Piedmont Park Fall Music Fest")
        assert {r.tenant.slug: r.total_engagements for r in three_way} == {
            "famus-energy": 380,
            "alcohol-armor": 240,
            "wook-water": 450,
        }
        coact_request = Request.objects.get(
            tenant=self.famus, name=f"{PREFIX}Piedmont Park Fall Music Fest"
        )
        assert set(
            RequestCoBrand.objects.filter(request=coact_request).values_list(
                "tenant__slug", flat=True
            )
        ) == {
            "alcohol-armor",
            "wook-water",
        }
        assert coact_request.status.slug == "approved"
        assert all(blob.split("/")[-1].startswith("demo-") for blob in self.uploaded)

        html = build_recap_pdf_html(famus_slice, [], public_share=True)
        assert "Co-activation with" in html and "Wook Water" in html
        assert "<span>DEMO</span>" in html and "Demo recap" in html

        compare = build_brand_group_compare(self.famus.brand_group, None, None)
        assert compare["co_activations"] == 2
        assert {b["name"]: b["recaps"] for b in compare["brands"]} == {
            "Alcohol Armor": 3,
            "FAMUS Energy": 4,
            "Wook Water": 4,
        }
        famus_row = next(b for b in compare["brands"] if b["name"] == "FAMUS Energy")
        assert (famus_row["consumers_sampled"], famus_row["units_sold"]) == (
            72 + 49,
            11 + 9,
        )

        again = _run("--apply")
        assert "= exists" in again and "+ created" not in again
        assert CustomRecap.objects.filter(source_label=SOURCE_LABEL).count() == 11

    def test_purge_leaves_zero_residue_and_keeps_real_rows(self):
        before = _counts()
        _run("--apply")
        assert _counts() != before
        blobs = list(self.uploaded)

        dry = _run("--purge")
        assert "would purge" in dry
        assert CustomRecap.objects.filter(source_label=SOURCE_LABEL).count() == 11

        _run("--purge", "--apply")
        assert _counts() == before
        assert Request.objects.filter(id=self.real_request.id).exists()
        assert not User.objects.filter(email__iendswith=f"@{DEMO_DOMAIN}").exists()
        deleted = {c.args[0] for c in brand_group_demo.gcs.delete_blob.call_args_list}
        assert deleted == set(blobs)
        self._assert_nothing_sent()

    def test_purge_refuses_when_real_work_hangs_off_demo_rows(self):
        _run("--apply")
        demo_event = Event.objects.filter(name=f"{PREFIX}Kroger #421").first()
        real_ba = self.create_ambassador(
            self.create_user(
                username="real-ba@test.com",
                email="real-ba@test.com",
                role=self.roles["ambassador"],
            )
        )
        CustomRecap.objects.create(
            name="Real recap",
            event=demo_event,
            tenant=self.famus,
            ambassador=real_ba,
            custom_recap_template=CustomRecap.objects.filter(event=demo_event)
            .first()
            .custom_recap_template,
            created_by=real_ba.user,
        )
        with pytest.raises(CommandError, match="refusing to purge"):
            _run("--purge", "--apply")
        assert CustomRecap.objects.filter(source_label=SOURCE_LABEL).count() == 11


def test_suppress_outbound_blocks_mail_and_is_scoped():
    drivers = MailDrivers()
    driver = mock.Mock()
    drivers.drivers = {drivers.driver: driver}
    envelope = mock.Mock(subject="Recap approved")
    with suppress_outbound() as blocked:
        assert outbound_suppressed()
        drivers.send(envelope)
    assert blocked == ["email: Recap approved"]
    assert not driver.send.called
    assert not outbound_suppressed()
    drivers.send(envelope)
    driver.send.assert_called_once_with(envelope)
