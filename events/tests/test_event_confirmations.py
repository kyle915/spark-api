"""Coverage for the event-confirmation send + its 24h/3h sweep.

Pins the things that would be silently wrong in production:

1. **The reminders must not double-send.** The sweep runs every 15 minutes; a
   dedup that only filters the queryset would let two overlapping runs both
   pass the check and email the BA twice.
2. **"24 hours before" must be the EVENT's 24 hours, not UTC's.**
   ``settings.TIME_ZONE`` is UTC, so anything derived from a local date rolls
   over a day early at 5pm Pacific. There's a test that puts the clock exactly
   in that trap.
3. **The sweep must not reach anyone it wasn't told to.** Cancelled, opted-out,
   past and late-booked rows are all excluded.
4. **The email must still look like the one Ignite was sending by hand** —
   subject shape, the LD "1p - 4p" clock style, and bare SKU names.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.core.management import call_command
from django.utils import timezone as djtz

from events.event_confirmations import (
    absolute_public_url,
    build_context,
    build_subject,
    confirmation_product_options,
    due_reminders,
    format_time_range,
    recap_url_for,
    send_confirmation_stage,
    training_url_for,
)
from events.models import (
    EventConfirmation,
    EventConfirmationSend,
    Product,
    ProductType,
    TimeZone,
)
from tenants.models import Tenant

CHICAGO = ZoneInfo("America/Chicago")
PACIFIC = ZoneInfo("America/Los_Angeles")

# Where the mail actually goes out — patched in every test so nothing leaves.
SEND_PATH = "events.event_confirmations.EventConfirmationMailer.send_now"


def _system_user():
    """Tenant.created_by is NOT NULL, so every tenant needs an author."""
    from django.contrib.auth import get_user_model
    from tenants.tests.base import ensure_role

    User = get_user_model()
    existing = User.objects.filter(username="system").first()
    if existing:
        return existing
    role = ensure_role("System")
    user = User.objects.create_user(
        username="system",
        email="system@spark.local",
        first_name="System",
        role=role,
        is_superuser=True,
        is_staff=True,
        is_active=True,
    )
    if not role.created_by:
        role.created_by = user
        role.save()
    return user


def _tenant(**kwargs) -> Tenant:
    defaults = dict(
        name="Liquid Death",
        checkin_code="LD-TNBJ8K",
        checkin_training_url="https://admin.igniteproductions.co/training/LD-FZUWXT",
        created_by=_system_user(),
    )
    defaults.update(kwargs)
    return Tenant.objects.create(**defaults)


def _confirmation(tenant, *, starts_at, ends_at=None, tz_name="America/Chicago", **kwargs):
    tz_row, _ = TimeZone.objects.get_or_create(
        name=tz_name, code="CT", offset=-300
    )
    defaults = dict(
        tenant=tenant,
        timezone=tz_row,
        ba_name="Deond Thomas",
        ba_email="deond762@example.com",
        store_name="Jewel Osco",
        address="4042 W Foster Ave, Chicago, IL 60630, USA",
        event_type_label="Retail Sampling",
        starts_at=starts_at,
        ends_at=ends_at,
        products=[
            "Sparkling Water — Squeezed-to-Death",
            "Sparkling Water — Severed Lime",
            "Sparkling Water — Rootbeer Wrath",
        ],
        send_reminders=True,
    )
    defaults.update(kwargs)
    return EventConfirmation.objects.create(**defaults)


# ---------------------------------------------------------------------------
# Email content — must still match the hand-sent reference
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestConfirmationContent:
    def test_subject_and_card_match_the_reference_email(self):
        tenant = _tenant()
        c = _confirmation(
            tenant,
            starts_at=datetime(2026, 8, 1, 13, 0, tzinfo=CHICAGO),
            ends_at=datetime(2026, 8, 1, 16, 0, tzinfo=CHICAGO),
        )

        assert build_subject(c) == (
            "Your Liquid Death Retail Sampling – Jewel Osco | 08/01/2026 | 1p - 4p"
        )

        ctx = build_context(c, EventConfirmation.STAGE_T3)
        assert ctx["eyebrow"] == "LIQUID DEATH • RETAIL SAMPLING"
        assert ctx["date_label"] == "08/01/2026"
        assert ctx["time_label"] == "1p - 4p"
        # Bare SKU names — the picker's "Category — " prefix is display noise.
        assert ctx["products_label"] == (
            "Squeezed-to-Death, Severed Lime, Rootbeer Wrath"
        )
        # Links are READ from the tenant, never hardcoded.
        assert ctx["recap_url"].endswith("/checkin/LD-TNBJ8K")
        assert ctx["training_url"].endswith("/training/LD-FZUWXT")

    def test_subject_is_identical_across_stages_so_they_thread(self):
        tenant = _tenant()
        c = _confirmation(
            tenant,
            starts_at=datetime(2026, 8, 1, 13, 0, tzinfo=CHICAGO),
            ends_at=datetime(2026, 8, 1, 16, 0, tzinfo=CHICAGO),
        )
        subjects = {build_subject(c) for _ in EventConfirmation.STAGE_CHOICES}
        assert len(subjects) == 1

    def test_only_the_intro_changes_between_stages(self):
        tenant = _tenant()
        c = _confirmation(tenant, starts_at=datetime(2026, 8, 1, 13, 0, tzinfo=CHICAGO))
        booked = build_context(c, EventConfirmation.STAGE_BOOKED)["intro_html"]
        t24 = build_context(c, EventConfirmation.STAGE_T24)["intro_html"]
        t3 = build_context(c, EventConfirmation.STAGE_T3)["intro_html"]
        assert "booked" in booked
        assert "tomorrow" in t24
        assert "in a few hours" in t3

    def test_template_renders_without_leaking_template_syntax(self):
        """A multi-line ``{# #}`` renders as literal text in Django — it would
        land in the email body AND the plain-text part."""
        from events.event_confirmations import EventConfirmationMailer

        tenant = _tenant()
        c = _confirmation(
            tenant,
            starts_at=datetime(2026, 8, 1, 13, 0, tzinfo=CHICAGO),
            ends_at=datetime(2026, 8, 1, 16, 0, tzinfo=CHICAGO),
        )
        env = EventConfirmationMailer(c, EventConfirmation.STAGE_T3).envelope()
        html = env.render_template()
        for artifact in ("{#", "#}", "{%", "{{"):
            assert artifact not in html
        assert "Open Recap Form" in html
        assert "Review Training Site" in html
        # Real <a href> with a full https URL — a styled <td> with no href
        # (or href="" / a relative /training/ path) is what made the iPhone
        # button look tappable and do nothing.
        assert 'href="https://client.igniteproductions.co/training/LD-FZUWXT"' in html
        assert 'href="https://client.igniteproductions.co/checkin/LD-TNBJ8K"' in html
        assert "spark.igniteproductions.co" not in html
        assert "admin.igniteproductions.co" not in html
        assert env.from_email == "Ignite Productions <staffing@igniteproductions.co>"

    @pytest.mark.parametrize(
        "start,end,expected",
        [
            ((13, 0), (16, 0), "1p - 4p"),
            ((10, 0), (13, 0), "10a - 1p"),
            ((17, 30), (20, 0), "5:30p - 8p"),
            ((9, 0), None, "9a"),
        ],
    )
    def test_ld_clock_style(self, start, end, expected):
        s = datetime(2026, 8, 1, *start, tzinfo=CHICAGO)
        e = datetime(2026, 8, 1, *end, tzinfo=CHICAGO) if end else None
        assert format_time_range(s, e) == expected


# ---------------------------------------------------------------------------
# Training / recap URLs — must be absolute https, never spark, never empty href
# ---------------------------------------------------------------------------

class TestPublicEmailUrls:
    def test_absolute_public_url_rewrites_spark_admin_and_relative_paths(self):
        assert absolute_public_url(
            "https://spark.igniteproductions.co/training/LD-FZUWXT"
        ) == "https://client.igniteproductions.co/training/LD-FZUWXT"
        assert absolute_public_url(
            "https://admin.igniteproductions.co/training/LD-FZUWXT"
        ) == "https://client.igniteproductions.co/training/LD-FZUWXT"
        assert absolute_public_url("/training/LD-FZUWXT") == (
            "https://client.igniteproductions.co/training/LD-FZUWXT"
        )
        assert absolute_public_url(
            "https://client.igniteproductions.co/training/LD-FZUWXT"
        ) == "https://client.igniteproductions.co/training/LD-FZUWXT"
        assert absolute_public_url("") == ""
        assert absolute_public_url("#") == ""

    @pytest.mark.django_db
    def test_training_url_uses_stored_absolute_link(self):
        tenant = _tenant()
        assert training_url_for(tenant) == (
            "https://client.igniteproductions.co/training/LD-FZUWXT"
        )
        assert recap_url_for(tenant) == (
            "https://client.igniteproductions.co/checkin/LD-TNBJ8K"
        )

    @pytest.mark.django_db
    def test_training_url_falls_back_to_training_hub(self):
        from academy.models import TrainingHub

        tenant = _tenant(checkin_training_url="")
        assert training_url_for(tenant) == ""
        TrainingHub.objects.create(
            tenant=tenant,
            code="LD-HUBFALL",
            title="Liquid Death — BA Training",
            is_active=True,
        )
        assert training_url_for(tenant) == (
            "https://client.igniteproductions.co/training/LD-HUBFALL"
        )

    @pytest.mark.django_db
    def test_training_url_empty_when_tenant_has_neither(self):
        tenant = _tenant(checkin_training_url="", checkin_code="")
        assert training_url_for(tenant) == ""
        assert recap_url_for(tenant) == ""


# ---------------------------------------------------------------------------
# Tenant-aware SKU picker — the one brand-specific piece of the feature
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestTenantProductOptions:
    def test_liquid_death_keeps_the_spark_form_list(self):
        """LD's picker and /spark-form/ighn-liquid-death must not drift."""
        tenant = _tenant()
        options = confirmation_product_options(tenant)
        assert len(options) == 32
        assert options[0].startswith("Sparkling Water — ")
        assert "Sparkling Water — Feastables Peanut Butter Cup" in options

    def test_torch_without_a_catalog_falls_back_to_the_onboard_list(self):
        """An unseeded Torch tenant still offers Torch SKUs, never LD water."""
        tenant = _tenant(
            name="Torch THC", slug="torch-thc", checkin_code="TH-2HRV3D"
        )
        options = confirmation_product_options(tenant)
        assert len(options) == 48
        assert options[0].startswith("Iced Tea 10mg — ")
        assert "Seltzer 60mg High Potency — Black Cherry 60mg 12oz" in options
        assert "10G — TORCH STRAWBERRY LEMONADE 10G" in options
        assert "10G — TORCH BLACK CHERRY 10G" in options
        assert "10G — TORCH WATERMELON 10MG" in options
        assert not any("Sparkling Water" in o for o in options)

    def test_a_live_catalog_wins_over_the_onboard_fallback(self):
        """Once Torch's Product rows exist, the picker reads THEM — an admin
        edit to the catalog shows up without a deploy."""
        tenant = _tenant(name="Torch THC", slug="torch-thc")
        user = _system_user()
        line = ProductType.objects.create(
            tenant=tenant, name="Seltzer 5mg Lite", created_by=user
        )
        Product.objects.create(
            tenant=tenant,
            product_type=line,
            name="Black Cherry 5mg 12oz",
            created_by=user,
        )
        assert confirmation_product_options(tenant) == [
            "Seltzer 5mg Lite — Black Cherry 5mg 12oz"
        ]

    def test_an_unknown_tenant_with_no_catalog_gets_an_empty_picker(self):
        tenant = _tenant(name="Feel Free")
        assert confirmation_product_options(tenant) == []


@pytest.mark.django_db
class TestTorchConfirmationContent:
    """The same email, Torch-branded — nothing Liquid Death may leak through."""

    def _torch_confirmation(self):
        tenant = _tenant(
            name="Torch THC",
            slug="torch-thc",
            checkin_code="TH-2HRV3D",
            checkin_training_url="",
        )
        return _confirmation(
            tenant,
            starts_at=datetime(2026, 8, 1, 13, 0, tzinfo=CHICAGO),
            ends_at=datetime(2026, 8, 1, 16, 0, tzinfo=CHICAGO),
            store_name="Binny's Beverage Depot",
            products=[
                "Iced Tea 10mg — Raspberry 10mg 12oz",
                "Seltzer 5mg Lite — Black Cherry 5mg 12oz",
            ],
        )

    def test_subject_and_eyebrow_are_torch_not_liquid_death(self):
        c = self._torch_confirmation()
        assert build_subject(c) == (
            "Your Torch THC Retail Sampling – "
            "Binny's Beverage Depot | 08/01/2026 | 1p - 4p"
        )
        ctx = build_context(c, EventConfirmation.STAGE_BOOKED)
        assert ctx["eyebrow"] == "TORCH THC • RETAIL SAMPLING"
        assert "Torch THC retail sampling" in ctx["intro_html"]
        assert "Liquid Death" not in build_subject(c)
        assert "Liquid Death" not in ctx["intro_html"]

    def test_products_strip_the_line_prefix_and_links_mint_client_host(self):
        c = self._torch_confirmation()
        ctx = build_context(c, EventConfirmation.STAGE_BOOKED)
        assert ctx["products_label"] == (
            "Raspberry 10mg 12oz, Black Cherry 5mg 12oz"
        )
        # The standing BA clock code, READ — never reminted, never admin.
        assert ctx["recap_url"] == (
            "https://client.igniteproductions.co/checkin/TH-2HRV3D"
        )
        # Torch has no training site configured: no button, not a dead link.
        assert ctx["training_url"] == ""

    def test_the_rendered_email_has_no_liquid_death_in_it(self):
        from events.event_confirmations import EventConfirmationMailer

        c = self._torch_confirmation()
        html = EventConfirmationMailer(
            c, EventConfirmation.STAGE_BOOKED
        ).envelope().render_template()
        assert "TORCH THC" in html
        assert "Liquid Death" not in html
        assert 'href="https://client.igniteproductions.co/checkin/TH-2HRV3D"' in html
        assert "spark.igniteproductions.co" not in html
        assert "admin.igniteproductions.co" not in html
        # No training URL → the block is omitted, not rendered empty.
        assert "Review Training Site" not in html


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestSendIdempotency:
    def test_a_stage_sends_at_most_once(self):
        tenant = _tenant()
        c = _confirmation(
            tenant, starts_at=djtz.now() + timedelta(days=3)
        )
        with patch(SEND_PATH) as send:
            first = send_confirmation_stage(c, EventConfirmation.STAGE_BOOKED)
            second = send_confirmation_stage(c, EventConfirmation.STAGE_BOOKED)
            third = send_confirmation_stage(c, EventConfirmation.STAGE_BOOKED)

        assert first.sent is True
        assert second.sent is False and second.reason == "already-sent"
        assert third.sent is False
        assert send.call_count == 1
        assert EventConfirmationSend.objects.filter(confirmation=c).count() == 1

    def test_stages_are_independent(self):
        tenant = _tenant()
        c = _confirmation(tenant, starts_at=djtz.now() + timedelta(days=3))
        with patch(SEND_PATH) as send:
            for stage in (
                EventConfirmation.STAGE_BOOKED,
                EventConfirmation.STAGE_T24,
                EventConfirmation.STAGE_T3,
            ):
                assert send_confirmation_stage(c, stage).sent is True
        assert send.call_count == 3
        assert EventConfirmationSend.objects.filter(confirmation=c).count() == 3

    def test_a_failed_send_is_retried_then_gives_up(self):
        tenant = _tenant()
        c = _confirmation(tenant, starts_at=djtz.now() + timedelta(days=3))
        with patch(SEND_PATH, side_effect=RuntimeError("resend down")) as send:
            for _ in range(EventConfirmationSend.MAX_ATTEMPTS):
                assert send_confirmation_stage(c, EventConfirmation.STAGE_T24).sent is False
            exhausted = send_confirmation_stage(c, EventConfirmation.STAGE_T24)

        assert send.call_count == EventConfirmationSend.MAX_ATTEMPTS
        assert exhausted.reason == "attempts-exhausted"
        row = EventConfirmationSend.objects.get(
            confirmation=c, stage=EventConfirmation.STAGE_T24
        )
        assert row.sent_at is None
        assert "resend down" in row.last_error

    def test_dry_run_neither_sends_nor_claims(self):
        tenant = _tenant()
        c = _confirmation(tenant, starts_at=djtz.now() + timedelta(days=3))
        with patch(SEND_PATH) as send:
            result = send_confirmation_stage(
                c, EventConfirmation.STAGE_T24, dry_run=True
            )
        assert result.sent is False
        assert send.call_count == 0
        assert EventConfirmationSend.objects.filter(confirmation=c).count() == 0


# ---------------------------------------------------------------------------
# Timezone-correct windows
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestReminderWindows:
    def test_t24_and_t3_fire_against_the_events_own_instant(self):
        tenant = _tenant()
        starts = datetime(2026, 8, 1, 13, 0, tzinfo=CHICAGO)
        c = _confirmation(tenant, starts_at=starts)
        # Backdate creation so the late-booking guard doesn't suppress anything.
        EventConfirmation.objects.filter(pk=c.pk).update(
            created_at=starts - timedelta(days=10)
        )
        c.refresh_from_db()

        # Two days out: nothing due yet.
        assert due_reminders(now=starts - timedelta(hours=48)) == []
        # Exactly 24h out: the t24 reminder, and only that.
        due = due_reminders(now=starts - timedelta(hours=24))
        assert [s for _, s in due] == [EventConfirmation.STAGE_T24]
        # Exactly 3h out: t3 (t24 is stale past the grace window).
        due = due_reminders(now=starts - timedelta(hours=3))
        assert [s for _, s in due] == [EventConfirmation.STAGE_T3]
        # After it starts: nothing.
        assert due_reminders(now=starts + timedelta(minutes=1)) == []

    def test_the_5pm_pacific_utc_date_rollover_does_not_shift_the_window(self):
        """settings.TIME_ZONE is UTC, so at 5pm Pacific the UTC date is already
        tomorrow. A window computed off a local DATE fires a day early here;
        one computed off the aware instant does not."""
        tenant = _tenant()
        # 10am Pacific the next day.
        starts = datetime(2026, 8, 2, 10, 0, tzinfo=PACIFIC)
        c = _confirmation(tenant, starts_at=starts, tz_name="America/Los_Angeles")
        EventConfirmation.objects.filter(pk=c.pk).update(
            created_at=starts - timedelta(days=10)
        )
        c.refresh_from_db()

        # 5:30pm Pacific the evening before: UTC has already rolled to Aug 2,
        # but the shift is still 16.5h away — no reminder is due.
        evening_before = datetime(2026, 8, 1, 17, 30, tzinfo=PACIFIC)
        assert evening_before.astimezone(ZoneInfo("UTC")).date().day == 2
        assert due_reminders(now=evening_before) == []

        # It becomes due exactly 24h before the shift's own instant.
        due = due_reminders(now=starts - timedelta(hours=24))
        assert [s for _, s in due] == [EventConfirmation.STAGE_T24]

    def test_dst_boundary_measures_24_real_hours_not_24_wall_clock_hours(self):
        """US DST ends 2026-11-01, so "10am the day before" a 10am Nov 1 shift
        is 25 REAL hours earlier, not 24.

        The reminder has to fire 24 real hours out. Note the arithmetic below:
        adding a timedelta to a ZoneInfo-aware datetime is WALL-CLOCK
        arithmetic (it moves the naive fields and keeps the zone), so
        ``starts - timedelta(hours=24)`` is the 25-hours-earlier instant. Doing
        the subtraction in UTC is what actually means "24 hours before".
        """
        tenant = _tenant()
        starts = datetime(2026, 11, 1, 10, 0, tzinfo=CHICAGO)
        c = _confirmation(tenant, starts_at=starts)
        EventConfirmation.objects.filter(pk=c.pk).update(
            created_at=starts - timedelta(days=10)
        )
        c.refresh_from_db()

        utc_start = starts.astimezone(ZoneInfo("UTC"))
        same_clock_day_before = datetime(2026, 10, 31, 10, 0, tzinfo=CHICAGO)
        # Same wall clock the previous day == 25 real hours out. Too early.
        assert utc_start - same_clock_day_before.astimezone(
            ZoneInfo("UTC")
        ) == timedelta(hours=25)
        assert due_reminders(now=same_clock_day_before) == []

        # 24 real hours out: due.
        due = due_reminders(now=utc_start - timedelta(hours=24))
        assert [s for _, s in due] == [EventConfirmation.STAGE_T24]

    def test_a_stale_reminder_is_dropped_past_the_grace_window(self):
        tenant = _tenant()
        starts = datetime(2026, 8, 1, 13, 0, tzinfo=CHICAGO)
        c = _confirmation(tenant, starts_at=starts)
        EventConfirmation.objects.filter(pk=c.pk).update(
            created_at=starts - timedelta(days=10)
        )
        c.refresh_from_db()
        # 10h before the shift is 14h past the t24 moment — beyond the 6h grace,
        # so we do NOT send a "tomorrow" email ten hours out.
        due = due_reminders(now=starts - timedelta(hours=10))
        assert due == []


# ---------------------------------------------------------------------------
# Who the sweep is allowed to reach
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestSweepExclusions:
    def _due_at_t24(self, **kwargs):
        tenant = _tenant()
        starts = djtz.now() + timedelta(hours=24)
        c = _confirmation(tenant, starts_at=starts, **kwargs)
        EventConfirmation.objects.filter(pk=c.pk).update(
            created_at=starts - timedelta(days=5)
        )
        c.refresh_from_db()
        return c

    def test_baseline_is_due(self):
        self._due_at_t24()
        assert len(due_reminders()) == 1

    def test_opted_out_rows_are_never_reminded(self):
        self._due_at_t24(send_reminders=False)
        assert due_reminders() == []

    def test_cancelled_rows_are_skipped(self):
        c = self._due_at_t24()
        c.cancelled_at = djtz.now()
        c.save(update_fields=["cancelled_at"])
        assert due_reminders() == []

    def test_an_already_claimed_stage_is_not_re_offered(self):
        c = self._due_at_t24()
        EventConfirmationSend.objects.create(
            confirmation=c, stage=EventConfirmation.STAGE_T24
        )
        assert due_reminders() == []

    def test_a_late_booking_does_not_fire_a_reminder_it_missed(self):
        """Book a BA 2h before their shift and the booked email is the only
        sensible message — without the guard the sweep would fire the
        'in a few hours' reminder right behind it."""
        tenant = _tenant()
        c = _confirmation(tenant, starts_at=djtz.now() + timedelta(hours=2))
        assert c.created_at is not None
        assert due_reminders() == []


# ---------------------------------------------------------------------------
# The management command
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestSweepCommand:
    def test_dry_run_reports_without_sending(self):
        tenant = _tenant()
        starts = djtz.now() + timedelta(hours=24)
        c = _confirmation(tenant, starts_at=starts)
        EventConfirmation.objects.filter(pk=c.pk).update(
            created_at=starts - timedelta(days=5)
        )

        out = StringIO()
        with patch(SEND_PATH) as send:
            call_command("send_event_confirmations", "--dry-run", stdout=out)

        log = out.getvalue()
        assert send.call_count == 0
        assert EventConfirmationSend.objects.count() == 0
        assert "would send" in log
        assert "deond762@example.com" in log

    def test_live_run_sends_once_and_a_second_run_is_a_no_op(self):
        tenant = _tenant()
        starts = djtz.now() + timedelta(hours=24)
        c = _confirmation(tenant, starts_at=starts)
        EventConfirmation.objects.filter(pk=c.pk).update(
            created_at=starts - timedelta(days=5)
        )

        with patch(SEND_PATH) as send:
            call_command("send_event_confirmations", stdout=StringIO())
            assert send.call_count == 1
            # The 15-minute cadence means this happens constantly.
            call_command("send_event_confirmations", stdout=StringIO())
            assert send.call_count == 1

        row = EventConfirmationSend.objects.get(
            confirmation=c, stage=EventConfirmation.STAGE_T24
        )
        assert row.sent_at is not None

    def test_empty_table_is_a_clean_no_op(self):
        """The state prod is in the moment this ships — nothing to send, and
        the sweep must say so rather than erroring."""
        out = StringIO()
        with patch(SEND_PATH) as send:
            call_command("send_event_confirmations", stdout=out)
        assert send.call_count == 0
        assert "Nothing due" in out.getvalue()

    def test_preview_sends_a_sample_and_persists_nothing(self):
        """--preview-to must not leave a row the sweep could later pick up, and
        must not stamp any real BA's ledger."""
        out = StringIO()
        with patch(SEND_PATH) as send:
            call_command(
                "send_event_confirmations",
                "--preview-to", "someone@example.com",
                "--preview-stage", "all",
                stdout=out,
            )
        assert send.call_count == 3
        assert EventConfirmation.objects.count() == 0
        assert EventConfirmationSend.objects.count() == 0
        assert "3/3 sent" in out.getvalue()

    def test_preview_rejects_a_non_email(self):
        err = StringIO()
        with patch(SEND_PATH) as send:
            call_command(
                "send_event_confirmations",
                "--preview-to", "not-an-email",
                stdout=StringIO(), stderr=err,
            )
        assert send.call_count == 0
        assert "Not an email" in err.getvalue()

    def test_max_sends_caps_a_run_and_says_what_it_deferred(self):
        tenant = _tenant()
        starts = djtz.now() + timedelta(hours=24)
        for i in range(4):
            # Staggered EARLIER, so all four t24 moments are already in the
            # past and every row is genuinely due on this run.
            c = _confirmation(
                tenant, starts_at=starts - timedelta(minutes=i),
                ba_email=f"ba{i}@example.com",
            )
            EventConfirmation.objects.filter(pk=c.pk).update(
                created_at=starts - timedelta(days=5)
            )

        out = StringIO()
        with patch(SEND_PATH) as send:
            call_command("send_event_confirmations", "--max-sends", "2", stdout=out)

        assert send.call_count == 2
        log = out.getvalue()
        assert "4 reminders due" in log
        # Silent truncation would read as "all clear" in the Actions log.
        assert "2 deferred" in log
