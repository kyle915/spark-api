"""Magic-link email must be web-only — no Spark BA app CTA.

There is no Spark BA app. Invite / sign-in emails should only offer the
admin/client web magic link, never a ``spark://`` deep-link or copy that
references a BA app.
"""

from __future__ import annotations

import re

import pytest

from tenants.envelopes import MagicLinkMailer
from tenants.tests.base import BaseGraphQLTestCase

WEB_LINK = "https://admin.igniteproductions.co/magic/tok-abc123"


def _render(mailer: MagicLinkMailer) -> str:
    """Render the magic-link template through the envelope (no send)."""
    return mailer.envelope().render_template()


def _primary_cta_href(html: str) -> str:
    """Return the href of the PRIMARY (big green #c5f546) CTA button."""
    cell = re.search(
        r'bgcolor="#c5f546".*?<a\s+href="([^"]+)"',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert cell, "Could not locate the primary (#c5f546) CTA button in the email"
    return cell.group(1)


@pytest.mark.django_db
class TestMagicLinkWebOnlyCta(BaseGraphQLTestCase):
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

    def test_ba_email_web_link_is_primary_and_no_ba_app_cta(self):
        mailer = MagicLinkMailer(
            user=self.ba_user,
            link=WEB_LINK,
            expires_minutes=30,
        )
        html = _render(mailer)
        assert _primary_cta_href(html) == WEB_LINK
        assert WEB_LINK in html
        assert "Open in the Spark BA app" not in html
        assert "Open in the Spark app" not in html
        assert "spark://" not in html

    def test_admin_email_web_link_only(self):
        mailer = MagicLinkMailer(
            user=self.admin_user,
            link=WEB_LINK,
            expires_minutes=30,
        )
        html = _render(mailer)
        assert _primary_cta_href(html) == WEB_LINK
        assert "Open in the Spark BA app" not in html
        assert "spark://" not in html

    def test_plain_text_alternative_has_no_ba_app_cta(self):
        mailer = MagicLinkMailer(
            user=self.admin_user,
            link=WEB_LINK,
            expires_minutes=30,
        )
        plain = mailer.envelope().render_text()
        assert WEB_LINK in plain
        assert "Open in the Spark BA app" not in plain
        assert "spark://" not in plain

