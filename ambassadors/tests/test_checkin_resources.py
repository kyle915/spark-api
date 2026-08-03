"""BA resource buttons on the check-in page.

The page used to surface exactly ONE reference link per brand
(``Tenant.checkin_training_url``). Feel Free needs two resources that are not
the same kind of thing — a training deck the BA reads, and a photo-release QR
the BA DISPLAYS so a consumer can scan it off their screen — so the single
field is generalised into an ordered ``Tenant.checkin_resources`` list carrying
a ``kind``.

What can break quietly, and is therefore what these tests pin:

* a brand configured BEFORE the list existed must keep its card. Liquid Death's
  is the only one, it is set via the legacy field, and "their training card
  disappeared" is the kind of regression nobody notices from the Feel Free side
  of the change — so both the data migration's shape and the runtime fallback
  are asserted, including the exact label and subtitle the page hard-coded;
* ``trainingUrl`` must keep shipping alongside ``resources``, because the API
  deploys BEFORE the front-end and the live page still reads the old key;
* the normaliser is the ONLY gate between a JSON blob and an ``href`` on a page
  that needs no authentication, so ``javascript:``, ``data:`` and
  protocol-relative ``//host`` urls must never reach the payload;
* one malformed row must not take the page down — a BA mid-shift needs the
  clock-out button far more than they need a tidy resource list.
"""

from __future__ import annotations

import pytest

from ambassadors import checkin_web
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from tenants.models import MAX_CHECKIN_RESOURCES, normalize_checkin_resources

# What `set_checkin_resources` seeds for Feel Free.
FF_RESOURCES = [
    {
        "label": "BA Training Guide",
        "kind": "pdf",
        "url": "https://admin.igniteproductions.co/training/ff/ba-training-guide.pdf",
        "note": "Summer street sampling deck · 23 slides",
    },
    {
        "label": "Photo Release Form",
        "kind": "image",
        "url": "https://admin.igniteproductions.co/training/ff/photo-release-qr.png",
        "note": "Show this — the consumer scans it to sign",
    },
]

LEGACY_URL = "https://admin.igniteproductions.co/training/LD-FZUWXT"


class TestNormalizeCheckinResources:
    """Pure-function tests — no DB needed."""

    @pytest.mark.parametrize(
        "url",
        [
            "javascript:alert(1)",
            "JavaScript:alert(1)",
            "data:text/html;base64,PHNjcmlwdD4=",
            "//evil.example.com/x",
            "vbscript:msgbox(1)",
            "file:///etc/passwd",
            "mailto:x@y.com",
        ],
    )
    def test_unsafe_urls_are_dropped(self, url):
        assert normalize_checkin_resources([{"label": "x", "url": url}]) == []

    @pytest.mark.parametrize(
        "url",
        [
            "https://admin.igniteproductions.co/training/ff/a.pdf",
            "http://example.com/a.png",
            "/training/ff/photo-release-qr.png",
        ],
    )
    def test_safe_urls_survive(self, url):
        assert len(normalize_checkin_resources([{"label": "x", "url": url}])) == 1

    def test_label_and_url_are_both_required(self):
        assert normalize_checkin_resources([{"label": "", "url": "https://e.com"}]) == []
        assert normalize_checkin_resources([{"label": "x", "url": ""}]) == []

    def test_unknown_kind_degrades_to_link(self):
        """A typo should open in a new tab, not drop the resource entirely."""
        got = normalize_checkin_resources(
            [{"label": "x", "kind": "iframe", "url": "https://e.com"}]
        )
        assert got[0]["kind"] == "link"

    def test_kinds_are_preserved_and_case_insensitive(self):
        got = normalize_checkin_resources(
            [
                {"label": "a", "kind": "PDF", "url": "https://e.com/a"},
                {"label": "b", "kind": " image ", "url": "https://e.com/b"},
            ]
        )
        assert [r["kind"] for r in got] == ["pdf", "image"]

    @pytest.mark.parametrize("junk", [None, "", "nope", {}, 42, ["str"], [None], [[]]])
    def test_junk_coerces_to_empty_list(self, junk):
        """Never raise: a malformed blob must not 500 the check-in page."""
        assert normalize_checkin_resources(junk) == []

    def test_one_bad_row_does_not_discard_the_good_ones(self):
        got = normalize_checkin_resources(
            [
                {"label": "good", "kind": "pdf", "url": "https://e.com/a"},
                {"label": "bad", "kind": "link", "url": "javascript:alert(1)"},
                {"label": "also good", "kind": "image", "url": "/x.png"},
            ]
        )
        assert [r["label"] for r in got] == ["good", "also good"]

    def test_order_is_preserved(self):
        got = normalize_checkin_resources(FF_RESOURCES)
        assert [r["label"] for r in got] == ["BA Training Guide", "Photo Release Form"]

    def test_list_is_capped(self):
        many = [{"label": f"r{i}", "url": f"https://e.com/{i}"} for i in range(20)]
        assert len(normalize_checkin_resources(many)) == MAX_CHECKIN_RESOURCES

    def test_long_text_is_truncated_not_rejected(self):
        got = normalize_checkin_resources(
            [{"label": "L" * 500, "note": "N" * 500, "url": "https://e.com"}]
        )
        assert len(got) == 1
        assert len(got[0]["label"]) == 80
        assert len(got[0]["note"]) == 120

    def test_note_is_omitted_when_blank(self):
        got = normalize_checkin_resources(
            [{"label": "x", "url": "https://e.com", "note": "   "}]
        )
        assert "note" not in got[0]


@pytest.mark.django_db(transaction=True)
class TestCheckinResourcesPayload(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Feel Free Resources")

    def test_no_config_means_no_buttons(self):
        """Every brand that configures nothing keeps looking exactly as it did."""
        assert checkin_web.build_checkin_resources(self.tenant) == []
        ctx = checkin_web.build_tenant_context(self.tenant)
        assert ctx["resources"] == []
        assert ctx["trainingUrl"] == ""

    def test_missing_tenant_is_safe(self):
        assert checkin_web.build_checkin_resources(None) == []

    def test_legacy_training_url_still_renders_a_card(self):
        """LD's card must survive the switch to the list, unchanged.

        The label and subtitle are asserted verbatim because they are what the
        page hard-coded before this field existed — a brand's card silently
        changing wording is a regression, just a quiet one.
        """
        self.tenant.checkin_training_url = LEGACY_URL
        self.tenant.save(update_fields=["checkin_training_url"])

        got = checkin_web.build_checkin_resources(self.tenant)
        assert got == [
            {
                "label": "BA reference & training",
                "kind": "link",
                "url": LEGACY_URL,
                "note": "Field guide, video, product sheets",
            }
        ]

    def test_explicit_list_wins_over_the_legacy_field(self):
        self.tenant.checkin_training_url = LEGACY_URL
        self.tenant.checkin_resources = FF_RESOURCES
        self.tenant.save(update_fields=["checkin_training_url", "checkin_resources"])

        got = checkin_web.build_checkin_resources(self.tenant)
        assert [r["label"] for r in got] == [
            "BA Training Guide",
            "Photo Release Form",
        ]
        assert [r["kind"] for r in got] == ["pdf", "image"]

    def test_tenant_context_ships_resources_and_the_legacy_key(self):
        """Both keys, because the API deploys ahead of the front-end."""
        self.tenant.checkin_training_url = LEGACY_URL
        self.tenant.checkin_resources = FF_RESOURCES
        self.tenant.save(update_fields=["checkin_training_url", "checkin_resources"])

        ctx = checkin_web.build_tenant_context(self.tenant)
        assert len(ctx["resources"]) == 2
        assert ctx["trainingUrl"] == LEGACY_URL

    def test_a_poisoned_row_never_reaches_the_payload(self):
        self.tenant.checkin_resources = [
            {"label": "Bad", "kind": "link", "url": "javascript:alert(1)"},
            {"label": "Photo Release Form", "kind": "image", "url": "/x.png"},
        ]
        self.tenant.save(update_fields=["checkin_resources"])

        ctx = checkin_web.build_tenant_context(self.tenant)
        assert [r["label"] for r in ctx["resources"]] == ["Photo Release Form"]

    def test_garbage_in_the_column_does_not_break_the_page(self):
        """A hand-edited column must degrade to "no buttons", never to a 500."""
        for junk in ("not a list", {"label": "x"}, 42, [None, "x"]):
            self.tenant.checkin_resources = junk
            self.tenant.save(update_fields=["checkin_resources"])
            ctx = checkin_web.build_tenant_context(self.tenant)
            assert ctx["resources"] == []


@pytest.mark.django_db(transaction=True)
class TestSetCheckinResourcesCommand(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Feel Free Command")

    def _run(self, **kwargs):
        import io

        from django.core.management import call_command

        out = io.StringIO()
        call_command("set_checkin_resources", stdout=out, **kwargs)
        return out.getvalue()

    def test_dry_run_writes_nothing(self):
        self._run(tenant="Feel Free Command")
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_resources is None

    def test_apply_seeds_the_feel_free_preset(self):
        self._run(tenant="Feel Free Command", apply=True)
        self.tenant.refresh_from_db()
        assert [r["kind"] for r in self.tenant.checkin_resources] == ["pdf", "image"]
        assert [r["label"] for r in self.tenant.checkin_resources] == [
            "BA Training Guide",
            "Photo Release Form",
        ]

    def test_re_running_is_idempotent(self):
        self._run(tenant="Feel Free Command", apply=True)
        out = self._run(tenant="Feel Free Command", apply=True)
        assert "Already set" in out
        self.tenant.refresh_from_db()
        assert len(self.tenant.checkin_resources) == 2

    def test_clear_falls_back_to_the_legacy_field(self):
        self.tenant.checkin_training_url = LEGACY_URL
        self.tenant.save(update_fields=["checkin_training_url"])
        self._run(tenant="Feel Free Command", apply=True)
        self._run(tenant="Feel Free Command", apply=True, clear=True)

        self.tenant.refresh_from_db()
        # None, not [] — so the tenant behaves exactly like an untouched one.
        assert self.tenant.checkin_resources is None
        assert checkin_web.build_checkin_resources(self.tenant)[0]["url"] == LEGACY_URL

    def test_a_rejected_entry_refuses_the_whole_write(self):
        """Seeding 1 of 2 buttons without saying so is how a brand ends up
        half-configured in the field."""
        import json

        from django.core.management.base import CommandError

        payload = json.dumps(
            [
                {"label": "ok", "kind": "link", "url": "https://e.com"},
                {"label": "", "kind": "link", "url": ""},
            ]
        )
        with pytest.raises(CommandError):
            self._run(tenant="Feel Free Command", resources=payload, apply=True)

        self.tenant.refresh_from_db()
        assert self.tenant.checkin_resources is None

    def test_unknown_tenant_has_no_preset_to_guess(self):
        from django.core.management.base import CommandError

        self.create_tenant(name="Nobody Brand Here")
        with pytest.raises(CommandError):
            self._run(tenant="Nobody Brand Here")
