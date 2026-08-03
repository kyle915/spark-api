"""Coverage for recaps/management/commands/audit_event_name_parse.py — the
read-only diagnostic that explains the Field Sampling Report's market gap.

The report resolves a market by PARSING ``Event.name``, so volume on an
event named any other way vanishes from every per-market total while still
counting in the unfiltered ``overall``. These tests pin the two causes
apart, because they need different fixes:

  * a name with no `` — `` separator (the standing check-in link's
    ``"8/2/2026 - Austin"``) — reported under ``unparsed_shapes``;
  * a name that parses cleanly to a label the caller didn't ask for
    (``"Tampa — ..."`` vs the report's ``"Tampa / St. Pete"``) — reported
    under ``markets`` with ``in_requested_list: false``.

Also pins the coarse shape grouping, which is the whole reason the output
is readable: thousands of walk-in titles differing by date AND by market or
street address have to collapse to ONE row.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone as _tz
from io import StringIO

import pytest
from django.core.management import call_command

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models
from recaps.management.commands.audit_event_name_parse import (
    _coarse_shape,
    _shape,
)

WHEN = datetime(2026, 7, 28, 18, 0, tzinfo=_tz.utc)  # inside the window below
START = "2026-07-23"
END = "2026-08-02"
OUTSIDE = datetime(2026, 3, 4, 18, 0, tzinfo=_tz.utc)

FIVE_METROS = "Miami,Ft. Lauderdale,Tampa / St. Pete,Austin,San Antonio"


class TestShapeSignatures:
    """The grouping key has to collapse the volatile parts of a title."""

    def test_walkin_titles_share_one_coarse_shape(self):
        # Market mode and address mode, different dates — one row.
        names = [
            "8/2/2026 - Austin",
            "7/24/2026 - Miami",
            "8/1/2026 - 123 Main St",
        ]
        assert len({_coarse_shape(n) for n in names}) == 1

    def test_canonical_shape_stays_distinct_from_walkin(self):
        assert _coarse_shape("Miami — Wynwood · 9/24") != _coarse_shape(
            "8/2/2026 - Austin"
        )

    def test_variant_label_keeps_words_and_redacts_digits(self):
        assert _shape("8/2/2026 - Austin") == "#/#/# - Austin"


@pytest.mark.django_db(transaction=True)
class TestAuditEventNameParse(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Feel Free")
        self.tenant.slug = "feel-free"
        self.tenant.save(update_fields=["slug"])
        self.sampling_type = self.create_event_type(
            name="Field Sampling", tenant=self.tenant
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Sampling Recap",
            event_type=self.sampling_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )
        product_type = event_models.ProductType.objects.create(
            name="Beverage", tenant=self.tenant, created_by=self.system_user
        )
        self.product = event_models.Product.objects.create(
            name="Classic",
            product_type=product_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )

    def _sampled(self, name, qty, *, when=WHEN, address="", event_type=None):
        """One event + recap + structured per-SKU quantity."""
        event = self.create_event(
            name=name,
            tenant=self.tenant,
            event_type=event_type or self.sampling_type,
            date=when,
            start_time=when,
            address=address,
        )
        recap = recap_models.CustomRecap.objects.create(
            name=f"recap for {name}",
            event=event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.system_user,
        )
        recap_models.CustomRecapProductSample.objects.create(
            custom_recap=recap,
            product=self.product,
            quantity=qty,
            created_by=self.system_user,
        )
        return event

    def _run(self, **extra):
        opts = {"tenant": "feel-free", "start": START, "end": END,
                "markets": FIVE_METROS}
        opts.update(extra)
        out = StringIO()
        call_command("audit_event_name_parse", stdout=out, **opts)
        raw = out.getvalue()
        blob = raw.split("ENPARSE_JSON_START", 1)[1].split("ENPARSE_JSON_END", 1)[0]
        return json.loads(blob)

    def test_canonical_name_counts_as_a_requested_market(self):
        self._sampled("Miami — Wynwood · 7/24", 100)
        data = self._run()

        assert data["totals"]["parsed_samples"] == 100
        assert data["totals"]["unparsed_samples"] == 0
        assert data["totals"]["gap_samples"] == 0
        miami = next(m for m in data["markets"] if m["market"] == "Miami")
        assert miami["in_requested_list"] is True
        assert miami["samples"] == 100

    def test_walkin_name_lands_in_the_gap_not_in_a_market(self):
        self._sampled("8/2/2026 - Austin", 40, address="Austin")
        data = self._run()

        assert data["totals"]["parsed_samples"] == 0
        assert data["totals"]["unparsed_samples"] == 40
        assert data["totals"]["gap_samples"] == 40
        assert data["markets"] == []
        assert len(data["unparsed_shapes"]) == 1
        shape = data["unparsed_shapes"][0]
        assert shape["samples"] == 40
        assert shape["examples"] == ["8/2/2026 - Austin"]
        # The market mode puts the market in Event.address, which is what
        # makes a structured fix possible at all.
        assert shape["sample_addresses"] == ["Austin"]

    def test_unrequested_label_parses_but_still_misses_the_report(self):
        """"Tampa" (the brand's own template spelling) is NOT
        "Tampa / St. Pete" (what the report asks for) — a real gap even
        though the name parses perfectly."""
        self._sampled("Tampa — Ybor City · 7/30", 70)
        data = self._run()

        assert data["totals"]["parsed_samples"] == 70
        assert data["totals"]["unparsed_samples"] == 0
        # Parses, yet still absent from the five-metro sum.
        assert data["totals"]["unrequested_market_samples"] == 70
        assert data["totals"]["gap_samples"] == 70
        tampa = next(m for m in data["markets"] if m["market"] == "Tampa")
        assert tampa["in_requested_list"] is False
        assert tampa["case_insensitive_match"] is False

    def test_gap_is_the_sum_of_both_causes(self):
        self._sampled("Miami — Wynwood · 7/24", 100)   # counted
        self._sampled("8/2/2026 - Austin", 40)         # unparseable
        self._sampled("Tampa — Ybor City · 7/30", 70)  # unrequested label
        data = self._run()

        t = data["totals"]
        assert t["samples"] == 210
        assert t["gap_samples"] == 110 == t["unparsed_samples"] + t[
            "unrequested_market_samples"
        ]

    def test_many_walkins_collapse_to_one_shape_row(self):
        for day, market in ((24, "Miami"), (28, "Austin"), (30, "Tampa")):
            self._sampled(f"7/{day}/2026 - {market}", 10, address=market)
        self._sampled("8/1/2026 - 123 Main St", 10, address="123 Main St")
        data = self._run()

        assert data["unparsed_shape_count"] == 1
        shape = data["unparsed_shapes"][0]
        assert shape["recaps"] == 4
        assert shape["samples"] == 40
        # Readable per-market variants survive inside the single row.
        assert len(shape["variants"]) == 4

    def test_creator_and_provenance_are_reported(self):
        """Answers "who/what created these" — a standing-link event has no
        parent Request, an imported one does."""
        self._sampled("8/2/2026 - Austin", 40)
        data = self._run()

        shape = data["unparsed_shapes"][0]
        assert shape["standalone"] == 1
        assert shape["via_request"] == 0
        assert shape["created_by"]  # [[email, count], ...]
        assert shape["first_created_at"] and shape["last_created_at"]

    def test_window_excludes_events_outside_it(self):
        self._sampled("8/2/2026 - Austin", 40)
        self._sampled("3/4/2026 - Austin", 999, when=OUTSIDE)
        data = self._run()

        assert data["totals"]["samples"] == 40

    def test_event_type_scope(self):
        other = self.create_event_type(name="Retail Sampling", tenant=self.tenant)
        self._sampled("8/2/2026 - Austin", 40)
        self._sampled("8/2/2026 - Elsewhere", 500, event_type=other)

        assert self._run()["totals"]["samples"] == 540
        scoped = self._run(event_type="Field Sampling")
        assert scoped["totals"]["samples"] == 40
