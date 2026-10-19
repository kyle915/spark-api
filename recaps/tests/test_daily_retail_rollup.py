"""Executive daily retail roll-up: canonical set, dedupe, gating, preview."""

from __future__ import annotations

import datetime
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from digest.management.commands import send_daily_retail_rollup as send_mod
from digest.models import TenantDailyRollup, TenantDailyRollupSend
from events import models as event_models
from recaps import models as recap_models
from recaps.daily_retail_rollup import (
    RollupRow,
    _quote_text,
    _store_title,
    _top_sku,
    approval_window,
    build_daily_retail_rollup,
    previous_day,
)
from recaps.envelopes import DailyRetailRollupMailer

PT = ZoneInfo("America/Los_Angeles")
DAY = datetime.date(2026, 10, 12)


def _pt(day: datetime.date, hour: int) -> datetime.datetime:
    return datetime.datetime.combine(day, datetime.time(hour), tzinfo=PT)


@pytest.mark.django_db(transaction=True)
class TestDailyRetailRollup(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(
            name="Torch THC", slug="torch-thc", request_url_name="keee-torch-thc"
        )
        self.location = self.create_location(
            name="Columbus", code="CMH", zip_code="43215", tenant=self.tenant
        )
        self.client_row = self.create_client(
            name="Brand", email="brand@example.com", tenant=self.tenant
        )
        self.distributor = self.create_distributor(
            name="Distro", email="d@example.com", location=self.location, tenant=self.tenant
        )
        self.retailer = self.create_retailer(
            name="Total Wine", address="1 Main", store_contact="mgr",
            location=self.location, tenant=self.tenant,
        )
        self.status = self.create_request_status(
            name="Scheduled", tenant=self.tenant, create_event=True
        )
        self.retail_type = self.create_request_type("Retail Sampling", self.tenant)
        self.event_type = self.create_request_type("Event Activation", self.tenant)
        et = event_models.EventType.objects.create(
            name="et torch", slug="et-torch-daily", tenant=self.tenant, created_by=self.sys
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Torch Retail", event_type=et, tenant=self.tenant, created_by=self.sys
        )
        self.field_type = recap_models.CustomRecapFieldType.objects.create(
            name="Number", created_by=self.sys
        )
        self.section = recap_models.RecapSection.objects.create(
            name="Sampling", tenant=self.tenant, created_by=self.sys
        )
        self._fields: dict[str, recap_models.CustomField] = {}

    def _field(self, name: str):
        if name not in self._fields:
            self._fields[name] = recap_models.CustomField.objects.create(
                name=name, required=False, custom_recap_template=self.template,
                custom_field_type=self.field_type, recap_section=self.section,
                created_by=self.sys,
            )
        return self._fields[name]

    def _recap(self, name, *, approved_at, event_day=DAY, request_type=None, fields=(), **extra):
        when = datetime.datetime.combine(event_day, datetime.time(18), tzinfo=datetime.timezone.utc)
        req = self.create_request(
            name=f"req {name}", date=when, address="1 Main St, Columbus, OH 43215",
            client=self.client_row, distributor=self.distributor, retailer=self.retailer,
            request_type=request_type or self.retail_type, tenant=self.tenant, status=self.status,
        )
        event = req.event_set.first() or self.create_event(name=f"ev {name}", tenant=self.tenant)
        event_models.Event.objects.filter(id=event.id).update(
            date=when, request=req, address="1 Main St, Columbus, OH 43215"
        )
        recap = recap_models.CustomRecap.objects.create(
            name=name, approved=True, approved_at=approved_at, event_id=event.id,
            tenant=self.tenant, custom_recap_template=self.template, created_by=self.sys,
            external_ba_name="Jamie BA", **extra,
        )
        for fname, value in fields:
            recap_models.CustomFieldValue.objects.create(
                custom_recap=recap, custom_field=self._field(fname), value=value, created_by=self.sys
            )
        return recap

    def _live(self, sampled: str, cans: str, packs: str):
        return (
            ("Total number of consumers sampled", sampled),
            ("How many single cans did consumers purchase?", cans),
            ("How many packs did consumers purchase?", packs),
            ("Sample format", '["4oz pour"]'),
            ("Quotes from Consumers", "Tastes like a lemonade stand in July, would buy again."),
            ("Ask consumers what competitor products they drink. And if so, which one.", "Cann, Wynk"),
        )

    def _seed_day(self):
        yesterday_noon = _pt(DAY, 12)
        self._recap("Total Wine #1234", approved_at=yesterday_noon, fields=self._live("20", "1", "2"))
        self._recap(
            "Kroger #55", approved_at=_pt(DAY, 23), event_day=DAY - datetime.timedelta(days=3),
            fields=self._live("10", "0", "1"),
        )
        # Dry demo: 2 sold ÷ 8 people engaged, even with no consumers sampled.
        self._recap(
            "Binny's #7", approved_at=yesterday_noon,
            fields=(
                ("Dry demo? (no product tasted)", "Yes"),
                ("People engaged", "8"),
                ("How many packs did consumers purchase?", "2"),
                ("Sample format", '["No samples (dry demo)"]'),
            ),
        )
        # Out of the day / set.
        self._recap("Old approval", approved_at=_pt(DAY - datetime.timedelta(days=1), 12),
                    fields=self._live("50", "5", "5"))
        self._recap("Next day", approved_at=_pt(DAY + datetime.timedelta(days=1), 0),
                    fields=self._live("50", "5", "5"))
        self._recap("Festival", approved_at=yesterday_noon, request_type=self.event_type,
                    fields=self._live("90", "0", "0"))
        self._recap("Archived", approved_at=yesterday_noon, fields=self._live("40", "4", "0"),
                    archived_at=yesterday_noon)
        self._recap("Agency Store #9", approved_at=yesterday_noon, fields=self._live("30", "3", "0"),
                    exclude_from_aggregates=True, is_third_party=True)

    def test_previous_day_and_window_use_pacific_time(self):
        now = datetime.datetime(2026, 10, 13, 6, 30, tzinfo=datetime.timezone.utc)  # Oct 12 23:30 PT
        assert previous_day(now) == datetime.date(2026, 10, 11)
        start, end = approval_window(DAY)
        assert start.isoformat() == "2026-10-12T00:00:00-07:00"
        assert end.isoformat() == "2026-10-13T00:00:00-07:00"

    def test_canonical_day_set_and_math(self):
        self._seed_day()
        rollup = build_daily_retail_rollup(self.tenant.id, DAY)

        assert [r.store for r in rollup.rows] == ["Kroger #55", "Binny's #7", "Total Wine #1234"]
        assert rollup.rows[0].event_date == DAY - datetime.timedelta(days=3)
        dry = rollup.rows[1]
        assert dry.dry_demo and dry.consumers_sampled == 8 and dry.units_sold == 2
        assert dry.counted is True
        # Product 4/30 plus dry 2/8.
        assert rollup.day["units_sold"] == 6
        assert rollup.day["consumers_sampled"] == 38
        assert rollup.day["conversion_pct"] == round(6 / 38 * 100, 1)
        assert rollup.day["dry_demos"] == 1
        assert rollup.day["dry_people_engaged"] == 8
        assert [r.store for r in rollup.third_party_rows] == ["Agency Store #9"]
        assert rollup.third_party_rows[0].units_sold == 3
        assert rollup.top_store.store == "Total Wine #1234"
        assert rollup.quotes and rollup.quotes[0].text.startswith("Tastes like")
        assert rollup.rows[2].competitor == "Cann, Wynk"
        assert rollup.rows[2].sample_format == "4oz pour"

    def test_email_renders_rows_links_and_sections(self):
        self._seed_day()
        rollup = build_daily_retail_rollup(self.tenant.id, DAY)
        mailer = DailyRetailRollupMailer(
            recipients=["kyle@igniteproductions.co"], tenant_name="Torch THC",
            rollup=rollup, preview=True,
        )
        env = mailer.envelope()
        html = env.render_template()
        assert env.subject == "[Preview] Torch THC · Retail sampling daily · Mon, Oct 12"
        assert "Total Wine #1234" in html and "Kroger #55" in html
        assert "3rd party · not in totals" in html and "Agency Store #9" in html
        assert "Archived" not in html and "Festival" not in html
        assert "https://client.igniteproductions.co/r/" in html
        assert "admin.igniteproductions.co/recap" not in html
        assert "BA app" not in html
        assert "8 people engaged" in html
        assert "No consumers-sampled count" not in html
        assert "No people-engaged count" not in html
        assert "dry demo" not in html.lower()
        assert "No samples" in html

    def test_command_off_by_default_sends_nothing(self):
        self._seed_day()
        with mock.patch.object(send_mod.DailyRetailRollupMailer, "send_now") as send:
            call_command("send_daily_retail_rollup", "--date", DAY.isoformat())
            call_command("send_daily_retail_rollup", "--tenant", "keee-torch-thc", "--date", DAY.isoformat())
        send.assert_not_called()

    def test_preview_goes_only_to_internal_address_and_never_stamps(self):
        self._seed_day()
        TenantDailyRollup.objects.create(
            tenant=self.tenant, enabled=False, recipients=["ryan@torchdrinks.com"]
        )
        with mock.patch.object(send_mod.DailyRetailRollupMailer, "send_now") as send, \
                mock.patch.object(send_mod, "DailyRetailRollupMailer", wraps=send_mod.DailyRetailRollupMailer) as cls:
            call_command(
                "send_daily_retail_rollup", "--tenant", "torch-thc", "--date", DAY.isoformat(),
                "--preview-to", "kyle@igniteproductions.co",
            )
        assert send.call_count == 1
        assert cls.call_args.kwargs["recipients"] == ["kyle@igniteproductions.co"]
        assert cls.call_args.kwargs["preview"] is True
        assert not TenantDailyRollupSend.objects.exists()
        with pytest.raises(CommandError):
            call_command(
                "send_daily_retail_rollup", "--tenant", "torch-thc",
                "--preview-to", "ryan@torchdrinks.com",
            )

    def test_enabled_sends_once_per_day_and_skips_empty_days(self):
        self._seed_day()
        TenantDailyRollup.objects.create(
            tenant=self.tenant, enabled=True,
            recipients=["ryan@torchdrinks.com", "matt@torchdrinks.com"],
        )
        with mock.patch.object(send_mod.DailyRetailRollupMailer, "send_now") as send:
            call_command("send_daily_retail_rollup", "--date", DAY.isoformat())
            call_command("send_daily_retail_rollup", "--date", DAY.isoformat())
            call_command("send_daily_retail_rollup", "--date", "2026-10-01")
        assert send.call_count == 1
        stamp = TenantDailyRollupSend.objects.get()
        assert stamp.report_date == DAY and stamp.recap_count == 3

    def _scheduled_fire(self, utc: datetime.datetime) -> int:
        with mock.patch.object(send_mod.DailyRetailRollupMailer, "send_now") as send, \
                mock.patch.object(send_mod.timezone, "now", return_value=utc):
            call_command(
                "send_daily_retail_rollup", "--not-before", "12", "--not-after", "14"
            )
        return send.call_count

    def test_noon_window_sends_once_in_pdt(self):
        self._seed_day()
        TenantDailyRollup.objects.create(
            tenant=self.tenant, enabled=True, recipients=["ryan@torchdrinks.com"]
        )
        eleven_am_pdt = datetime.datetime(2026, 10, 13, 18, 0, tzinfo=datetime.timezone.utc)
        hour = datetime.timedelta(hours=1)
        assert self._scheduled_fire(eleven_am_pdt) == 0
        assert self._scheduled_fire(eleven_am_pdt + hour) == 1
        assert self._scheduled_fire(eleven_am_pdt + 2 * hour) == 0
        assert self._scheduled_fire(eleven_am_pdt + 3 * hour) == 0
        assert TenantDailyRollupSend.objects.get().report_date == DAY

    def test_noon_window_backup_fire_sends_when_noon_run_dropped(self):
        self._seed_day()
        TenantDailyRollup.objects.create(
            tenant=self.tenant, enabled=True, recipients=["ryan@torchdrinks.com"]
        )
        one_pm_pdt = datetime.datetime(2026, 10, 13, 20, 0, tzinfo=datetime.timezone.utc)
        two_pm_pdt = one_pm_pdt + datetime.timedelta(hours=1)
        assert self._scheduled_fire(two_pm_pdt) == 0
        assert self._scheduled_fire(one_pm_pdt) == 1

    def test_noon_window_in_pst_after_dst_ends(self):
        day = datetime.date(2026, 11, 2)
        TenantDailyRollup.objects.create(
            tenant=self.tenant, enabled=True, recipients=["ryan@torchdrinks.com"]
        )
        nineteen_utc = datetime.datetime(2026, 11, 3, 19, 0, tzinfo=datetime.timezone.utc)
        with mock.patch.object(send_mod, "build_daily_retail_rollup") as build:
            build.return_value.has_content = False
            assert self._scheduled_fire(nineteen_utc) == 0
            build.assert_not_called()
            assert self._scheduled_fire(nineteen_utc + datetime.timedelta(hours=1)) == 0
            assert build.call_args[0][1] == day

    def test_failed_send_releases_the_day(self):
        self._seed_day()
        TenantDailyRollup.objects.create(
            tenant=self.tenant, enabled=True, recipients=["ryan@torchdrinks.com"]
        )
        with mock.patch.object(
            send_mod.DailyRetailRollupMailer, "send_now", side_effect=RuntimeError("smtp down")
        ):
            call_command("send_daily_retail_rollup", "--date", DAY.isoformat())
        assert not TenantDailyRollupSend.objects.exists()

    def test_set_command_dry_run_then_apply_seeds_torch_list_off(self):
        call_command("set_daily_retail_rollup", "--tenant", "keee-torch-thc")
        assert not TenantDailyRollup.objects.exists()
        call_command("set_daily_retail_rollup", "--tenant", "keee-torch-thc", "--enabled", "false", "--apply")
        row = TenantDailyRollup.objects.get()
        assert row.enabled is False
        assert row.recipients == [
            "ryan@torchdrinks.com", "matt@torchdrinks.com", "collin@torchenterprise.com",
        ]
        assert row.timezone == "America/Los_Angeles"
        call_command("set_daily_retail_rollup", "--tenant", "keee-torch-thc", "--enabled", "true", "--apply")
        row.refresh_from_db()
        assert row.enabled is True and len(row.recipients) == 3


def _row(products: list[str]) -> RollupRow:
    return RollupRow(
        kind="custom", recap_id=1, store="S", event_date=DAY, market="", ba="",
        consumers_sampled=10, units_sold=1, dry_demo=False, counted=True,
        sample_format="", products=products, competitor="",
    )


def test_display_helpers_tidy_titles_quotes_and_top_sku():
    assert _store_title("Retail Sampling - Total Wine & More (Orlando) #909") == (
        "Total Wine & More (Orlando) #909"
    )
    assert _store_title("Colonial Liquor & Wine · 2026-10-10") == "Colonial Liquor & Wine"
    assert _store_title("Charboozie Before and After Bar,") == "Charboozie Before and After Bar"
    assert _store_title("") == "Retail demo"
    assert _quote_text('"" I can\'t wait to try it. ""') == "I can\'t wait to try it."
    assert _quote_text("\u201cToo expensive for a single can!\u201d") == "Too expensive for a single can!"
    rows = [
        _row(["Seltzer 10mg — Strawberry Lemonade 10mg 4-Pack", "Field marketing — Nonactive"]),
        _row(["Strawberry Lemonade 10mg 4-Pack"]),
        _row(["Field marketing — Nonactive"]),
        _row(["Black Cherry 10mg", "Nonactive"]),
    ]
    assert _top_sku(rows) == ("Strawberry Lemonade 10mg 4-Pack", 2)
    assert _top_sku([_row(["Nonactive"])]) is None
