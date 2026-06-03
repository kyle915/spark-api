"""
Coverage for the role-aware CTA in the magic-link email.

A new BA reads the sign-in email on their phone. The original template made
the WEB link (`{{ link }}` → admin web) the big green button and the app
deep-link (`spark://magic/<token>`) only a secondary button — so a BA tapping
the obvious button landed on the admin web, which has no BA home.

The fix is role-aware: the mailer takes an `app_primary` flag. When set (the
caller passes it for ambassador/BA recipients), the PRIMARY green button is the
app deep-link and the web link drops to a small "open in your browser"
fallback. Admins/clients leave it off, so the web link stays primary. These
tests assert the rendered HTML at the envelope level (no email is sent, no
network) — the most direct check of the contract.
"""

from __future__ import annotations

import re

import pytest

from tenants.envelopes import MagicLinkMailer
from tenants.tests.base import BaseGraphQLTestCase

WEB_LINK = "https://spark-new-admin.web.app/magic/tok-abc123"
APP_LINK = "spark://magic/tok-abc123"


def _render(mailer: MagicLinkMailer) -> str:
    """Render the magic-link template through the envelope (no send)."""
    return mailer.envelope().render_template()


def _primary_cta_href(html: str) -> str:
    """Return the href of the PRIMARY (big green #c5f546) CTA button.

    The primary button is the only <a> whose enclosing <td> carries the
    bgcolor="#c5f546" brand-green background; the secondary app button (when
    present) is on a dark pill, and the fallback link is plain text. We find
    the green cell and pull the first href inside it.
    """
    # Grab the markup from the green CTA cell to the closing anchor.
    cell = re.search(
        r'bgcolor="#c5f546".*?<a\s+href="([^"]+)"',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert cell, "Could not locate the primary (#c5f546) CTA button in the email"
    return cell.group(1)


@pytest.mark.django_db
class TestMagicLinkRoleAwareCta(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.ambassador_role = self.create_role(name="Ambassador", slug="ambassador")
        self.admin_role = self.create_role(name="Spark Admin", slug="spark-admin")
        self.ba_user = self.create_user(
            username="ba@example.com",
            email="ba@example.com",
            role=self.ambassador_role,
            first_name="Bailey",
        )
        self.admin_user = self.create_user(
            username="admin@example.com",
            email="admin@example.com",
            role=self.admin_role,
            first_name="Avery",
        )

    # ── BA / ambassador: app deep-link is the PRIMARY CTA ───────────────

    def test_ba_email_app_link_is_primary_cta(self):
        mailer = MagicLinkMailer(
            user=self.ba_user,
            link=WEB_LINK,
            mobile_link=APP_LINK,
            app_primary=True,
            expires_minutes=30,
        )
        html = _render(mailer)
        # The big green button points at the APP, not the web admin.
        assert _primary_cta_href(html) == APP_LINK
        # The web link is still present as a fallback so a BA on a desktop or
        # without the app installed is never stranded.
        assert WEB_LINK in html
        assert "Open in your browser" in html

    # ── admin / client: web link stays the PRIMARY CTA ──────────────────

    def test_admin_email_web_link_stays_primary_cta(self):
        mailer = MagicLinkMailer(
            user=self.admin_user,
            link=WEB_LINK,
            mobile_link=APP_LINK,
            app_primary=False,
            expires_minutes=30,
        )
        html = _render(mailer)
        # Big green button is the WEB link (admins/clients work on the web).
        assert _primary_cta_href(html) == WEB_LINK
        # The app deep-link is still offered as the near-equal secondary CTA.
        assert APP_LINK in html
        assert "Open in the Spark BA app" in html

    # ── app_primary is a no-op without a mobile link (never strand) ─────

    def test_app_primary_without_mobile_link_falls_back_to_web_primary(self):
        # Even if a caller passes app_primary=True, with no mobile_link the
        # envelope must keep the web link primary rather than render a broken
        # primary button.
        mailer = MagicLinkMailer(
            user=self.ba_user,
            link=WEB_LINK,
            mobile_link=None,
            app_primary=True,
            expires_minutes=30,
        )
        assert mailer.app_primary is False
        html = _render(mailer)
        assert _primary_cta_href(html) == WEB_LINK
