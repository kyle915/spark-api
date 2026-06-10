"""BA referral program — code generation, attribution, first-shift stamp."""

import pytest

from ambassadors.models import AmbassadorReferral, ReferralCode
from ambassadors.referrals import (
    attribute_signup,
    complete_first_shift_if_referred,
    get_or_create_code,
)
from ambassadors.tests.base import AmbassadorsGraphQLTestCase


@pytest.mark.django_db
class TestReferrals(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.referrer = self.create_user(
            username="ref-referrer",
            email="referrer@test.com",
            role=self.roles["ambassador"],
        )
        self.friend = self.create_user(
            username="ref-friend",
            email="friend@test.com",
            role=self.roles["ambassador"],
        )

    def test_code_is_stable_and_unique_per_user(self):
        code1 = get_or_create_code(self.referrer)
        code2 = get_or_create_code(self.referrer)
        assert code1 == code2
        assert len(code1) == 8
        other = get_or_create_code(self.friend)
        assert other != code1
        assert ReferralCode.objects.count() == 2

    def test_attribute_signup_happy_path(self):
        code = get_or_create_code(self.referrer)
        # Case-insensitive on purpose: codes get typed on phones.
        assert attribute_signup(code.lower(), self.friend) is True
        ref = AmbassadorReferral.objects.get(referred=self.friend)
        assert ref.referrer_id == self.referrer.id
        assert ref.code_used == code
        assert ref.first_shift_completed_at is None

    def test_attribute_signup_ignores_bad_blank_and_self(self):
        code = get_or_create_code(self.referrer)
        assert attribute_signup(None, self.friend) is False
        assert attribute_signup("", self.friend) is False
        assert attribute_signup("NOPE1234", self.friend) is False
        # Self-referral never creates a row.
        assert attribute_signup(code, self.referrer) is False
        assert AmbassadorReferral.objects.count() == 0

    def test_attribute_signup_keeps_first_referral(self):
        code_a = get_or_create_code(self.referrer)
        second_referrer = self.create_user(
            username="ref-second",
            email="second@test.com",
            role=self.roles["ambassador"],
        )
        code_b = get_or_create_code(second_referrer)
        assert attribute_signup(code_a, self.friend) is True
        # A second code for the same referred user is a no-op.
        assert attribute_signup(code_b, self.friend) is False
        ref = AmbassadorReferral.objects.get(referred=self.friend)
        assert ref.referrer_id == self.referrer.id

    def test_first_shift_stamp_once(self):
        code = get_or_create_code(self.referrer)
        attribute_signup(code, self.friend)

        first = complete_first_shift_if_referred(self.friend)
        assert first is not None
        assert first.referrer_id == self.referrer.id
        assert first.first_shift_completed_at is not None

        # Later clock-outs are no-ops (already stamped).
        assert complete_first_shift_if_referred(self.friend) is None
        # Unreferred users are no-ops too.
        assert complete_first_shift_if_referred(self.referrer) is None
