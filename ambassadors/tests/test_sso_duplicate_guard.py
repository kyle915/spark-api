"""SSO duplicate-account guard: create_if_missing=False turns an unmatched
email into new_account_required instead of silently forking an empty
account (the Rocio Apple Hide-My-Email incident)."""
from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model

from ambassadors import inputs as amb_inputs
from ambassadors.models import Ambassador
from ambassadors.services import OAuthSignInService
from events.tests.base import EventsGraphQLTestCase

User = get_user_model()


def _identity(email, provider="apple"):
    return SimpleNamespace(
        email=email, first_name="Rocio", last_name="Matta", provider=provider,
    )


@pytest.mark.django_db(transaction=True)
class TestSsoDuplicateGuard(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.system_user = self.get_system_user()
        from tenants.models import Role
        # The service resolves the Ambassador role by fixed pk.
        from ambassadors.services import ROLE_ID
        Role.objects.get_or_create(
            pk=ROLE_ID.Ambassadors,
            defaults={
                "name": "Ambassador", "slug": Role.AMBASSADOR_SLUG,
                "created_by": self.system_user,
            },
        )

    def _finish(self, email, create_if_missing):
        inp = amb_inputs.AppleSignInInput(
            id_token="ignored", create_if_missing=create_if_missing,
        )
        return async_to_sync(OAuthSignInService._finish)(inp, _identity(email))

    def test_guarded_relay_email_returns_new_account_required(self):
        resp = self._finish("x1abc@privaterelay.appleid.com", create_if_missing=False)
        assert resp.success is False
        assert resp.new_account_required is True
        assert "hidden relay" in resp.message
        assert not User.objects.filter(
            email__iexact="x1abc@privaterelay.appleid.com"
        ).exists()

    def test_guarded_normal_email_names_the_address(self):
        resp = self._finish("nobody@example.com", create_if_missing=False)
        assert resp.new_account_required is True
        assert "nobody@example.com" in resp.message
        assert not User.objects.filter(email__iexact="nobody@example.com").exists()

    def test_legacy_clients_still_auto_create(self):
        resp = self._finish("legacy@example.com", create_if_missing=None)
        assert resp.success is True
        assert resp.new_account_required is False
        assert User.objects.filter(email__iexact="legacy@example.com").exists()
        assert Ambassador.objects.filter(
            user__email__iexact="legacy@example.com"
        ).exists()

    def test_explicit_opt_in_creates(self):
        resp = self._finish("optin@example.com", create_if_missing=True)
        assert resp.success is True
        assert User.objects.filter(email__iexact="optin@example.com").exists()

    def test_existing_user_signs_in_even_when_guarded(self):
        from tenants.models import Role
        role = Role.objects.get(slug=Role.AMBASSADOR_SLUG)
        User.objects.create_user(
            username="rocio", email="rocio.12_virgo1996@hotmail.com",
            first_name="Rocio", role=role, is_active=True,
        )
        resp = self._finish(
            "rocio.12_virgo1996@hotmail.com", create_if_missing=False
        )
        assert resp.success is True
        assert resp.new_account_required is False

    def test_inactive_account_does_not_capture_sign_in(self):
        """A deactivated relay duplicate must not win the SSO email match —
        the guarded sign-in should come back as newAccountRequired so the BA
        uses their invited email instead (audit_ba_accounts deactivates the
        empty dups; this is what makes that cleanup effective)."""
        from tenants.models import Role

        role = Role.objects.get(slug=Role.AMBASSADOR_SLUG)
        User.objects.create_user(
            username="dup",
            email="x9relay@privaterelay.appleid.com",
            first_name="Alicia",
            role=role,
            is_active=False,
        )
        resp = self._finish(
            "x9relay@privaterelay.appleid.com", create_if_missing=False
        )
        assert resp.success is False
        assert resp.new_account_required is True
