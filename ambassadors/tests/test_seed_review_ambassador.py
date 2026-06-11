"""seed_review_ambassador — idempotent app-store review BA provisioning."""
import io

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from ambassadors.models import Ambassador
from ambassadors.tests.base import AmbassadorsGraphQLTestCase

User = get_user_model()


@pytest.mark.django_db(transaction=True)
class TestSeedReviewAmbassador(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()

    def _run(self, **kw):
        out = io.StringIO()
        call_command("seed_review_ambassador", stdout=out, **kw)
        return out.getvalue()

    def test_creates_fresh_active_ba_that_can_login(self):
        self._run(email="review@test.com", password="Kyle93$$")
        user = User.objects.get(email__iexact="review@test.com")
        assert user.is_active
        assert user.check_password("Kyle93$$")
        assert user.role.slug == "ambassador"
        amb = Ambassador.objects.get(user=user)
        assert amb.is_active
        if hasattr(user, "requires_password_change"):
            assert user.requires_password_change is False

    def test_repairs_existing_user_password_and_attaches_ba(self):
        # Pre-existing non-BA user (e.g. an admin) with a different password.
        existing = self.create_user(
            username="review2@test.com",
            email="review2@test.com",
            role=self.roles["spark_admin"],
        )
        existing.set_password("old-pw")
        existing.save()
        assert not Ambassador.objects.filter(user=existing).exists()

        out = self._run(email="review2@test.com", password="NewPass1$")
        existing.refresh_from_db()
        assert existing.check_password("NewPass1$")
        assert existing.is_active
        assert existing.role.slug == "ambassador"
        assert Ambassador.objects.get(user=existing).is_active
        assert "prior role 'spark-admin'" in out

    def test_idempotent_second_run(self):
        self._run(email="review3@test.com", password="Kyle93$$")
        self._run(email="review3@test.com", password="Kyle93$$")
        assert (
            Ambassador.objects.filter(user__email__iexact="review3@test.com").count()
            == 1
        )
