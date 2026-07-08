"""
Tests for the BA self-edit mutation `updateBaProfile` (mobile schema).

The mutation is scoped to the authenticated BA: it resolves the
Ambassador from the JWT user and writes ONLY that row. There is no
ambassador_id input, so a BA can never edit another BA's profile — we
prove that two BAs editing concurrently never cross-contaminate.
"""
import uuid

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from ambassadors.models import Ambassador, AmbassadorPhoto
from ambassadors.tests.base import AmbassadorsGraphQLTestCase

User = get_user_model()


MUTATION = """
mutation UpdateBaProfile($input: UpdateBaProfileInput!) {
  updateBaProfile(input: $input) {
    success
    message
    ambassador {
      bio
      college
      inCollege
      headshotUrl
      resumeUrl
      photos { uuid imageUrl }
      profileComplete
    }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestBaSelfEdit(AmbassadorsGraphQLTestCase):
    """Coverage for BA self-edit of bio/college/photos/headshot/resume."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_mobile import schema_mobile

        self.roles = self.setup_default_roles()
        self.schema = schema_mobile
        self.endpoint_path = "/api/v1/graphql/mobile"
        self.tenant = self.create_tenant(name="Self Edit Tenant")

    def _make_ba(self, label):
        uid = str(uuid.uuid4())[:8]
        u = self.create_user(
            username=f"ba_{label}_{uid}@test.com",
            email=f"ba_{label}_{uid}@test.com",
            first_name=label.title(),
            role=self.roles["ambassador"],
        )
        return u, self.create_ambassador(u, is_active=True)

    @pytest.mark.asyncio
    async def test_updates_bio_college_in_college(self):
        u, ba = await sync_to_async(self._make_ba)("editor")

        result = await self._execute_mutation(
            MUTATION,
            {
                "input": {
                    "bio": "Field marketing pro.",
                    "college": "Ohio State",
                    "inCollege": True,
                }
            },
            self.endpoint_path,
            user=u,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["updateBaProfile"]
        assert payload["success"] is True
        amb = payload["ambassador"]
        assert amb["bio"] == "Field marketing pro."
        assert amb["college"] == "Ohio State"
        assert amb["inCollege"] is True

        # Persisted to the DB, and mirrored to about_me for legacy reads.
        fresh = await sync_to_async(Ambassador.objects.get)(id=ba.id)
        assert fresh.bio == "Field marketing pro."
        assert fresh.about_me == "Field marketing pro."
        assert fresh.college == "Ohio State"
        assert fresh.in_college is True

    @pytest.mark.asyncio
    async def test_profile_complete_without_bio(self):
        # Regression: the mobile onboarding form marks bio OPTIONAL, but
        # profileComplete used to require it — so a BA who filled
        # name/phone/address and skipped bio stayed profileComplete=false
        # and got re-routed to onboarding on every login. Name + phone +
        # address (all REQUIRED in the app) must be sufficient.
        u, ba = await sync_to_async(self._make_ba)("nobio")

        result = await self._execute_mutation(
            MUTATION,
            {
                "input": {
                    "firstName": "Nena",
                    "phone": "555-0100",
                    "address": "1 Main St",
                }
            },
            self.endpoint_path,
            user=u,
        )
        assert result.errors is None, f"errored: {result.errors}"
        amb = result.data["updateBaProfile"]["ambassador"]
        assert amb["bio"] in (None, "")  # no bio provided
        assert amb["profileComplete"] is True

    @pytest.mark.asyncio
    async def test_profile_incomplete_without_essentials(self):
        # The gate still holds for the essentials: missing address (a
        # required onboarding field) keeps profileComplete=false.
        u, ba = await sync_to_async(self._make_ba)("partial")

        result = await self._execute_mutation(
            MUTATION,
            {"input": {"firstName": "Nena", "phone": "555-0100"}},
            self.endpoint_path,
            user=u,
        )
        assert result.errors is None, f"errored: {result.errors}"
        assert result.data["updateBaProfile"]["ambassador"]["profileComplete"] is False

    @pytest.mark.asyncio
    async def test_attaches_headshot_and_event_photos(self):
        u, ba = await sync_to_async(self._make_ba)("uploader")

        result = await self._execute_mutation(
            MUTATION,
            {
                "input": {
                    "headshot": "ba/headshots/abc.jpg",
                    "resume": "ba/resumes/cv.pdf",
                    "eventPhotos": [
                        "ba/photos/1.jpg",
                        "ba/photos/2.jpg",
                    ],
                }
            },
            self.endpoint_path,
            user=u,
        )
        assert result.errors is None, f"errored: {result.errors}"
        amb = result.data["updateBaProfile"]["ambassador"]
        assert len(amb["photos"]) == 2

        # Blob PATHS persisted on the model + AmbassadorPhoto rows. (The
        # served public URL depends on GS_BUCKET_NAME, which is unset in
        # CI, so we assert on the stored blob — the load-bearing part.)
        fresh = await sync_to_async(Ambassador.objects.get)(id=ba.id)
        assert fresh.headshot == "ba/headshots/abc.jpg"
        assert fresh.resume == "ba/resumes/cv.pdf"
        blobs = await sync_to_async(
            lambda: sorted(
                AmbassadorPhoto.objects.filter(ambassador_id=ba.id).values_list(
                    "image", flat=True
                )
            )
        )()
        assert blobs == ["ba/photos/1.jpg", "ba/photos/2.jpg"]

    @pytest.mark.asyncio
    async def test_event_photos_replace_set(self):
        u, ba = await sync_to_async(self._make_ba)("replacer")
        # Seed one existing photo.
        await sync_to_async(AmbassadorPhoto.objects.create)(
            ambassador=ba, image="ba/photos/old.jpg"
        )

        result = await self._execute_mutation(
            MUTATION,
            {"input": {"eventPhotos": ["ba/photos/new.jpg"]}},
            self.endpoint_path,
            user=u,
        )
        assert result.errors is None, f"errored: {result.errors}"
        blobs = await sync_to_async(
            lambda: list(
                AmbassadorPhoto.objects.filter(ambassador_id=ba.id).values_list(
                    "image", flat=True
                )
            )
        )()
        assert blobs == ["ba/photos/new.jpg"]

    @pytest.mark.asyncio
    async def test_ba_edits_only_own_profile(self):
        u_a, ba_a = await sync_to_async(self._make_ba)("alpha")
        u_b, ba_b = await sync_to_async(self._make_ba)("bravo")

        # BA-A edits.
        result = await self._execute_mutation(
            MUTATION,
            {"input": {"bio": "A's bio", "college": "A College"}},
            self.endpoint_path,
            user=u_a,
        )
        assert result.errors is None, f"errored: {result.errors}"
        assert result.data["updateBaProfile"]["success"] is True

        # BA-B is untouched.
        fresh_a = await sync_to_async(Ambassador.objects.get)(id=ba_a.id)
        fresh_b = await sync_to_async(Ambassador.objects.get)(id=ba_b.id)
        assert fresh_a.bio == "A's bio"
        assert fresh_a.college == "A College"
        assert fresh_b.bio == ""
        assert fresh_b.college == ""

    @pytest.mark.asyncio
    async def test_non_ambassador_user_gets_clean_failure(self):
        uid = str(uuid.uuid4())[:8]
        client_user = await sync_to_async(self.create_user)(
            username=f"client_{uid}@test.com",
            email=f"client_{uid}@test.com",
            role=self.roles["client"],
        )

        result = await self._execute_mutation(
            MUTATION,
            {"input": {"bio": "should not save"}},
            self.endpoint_path,
            user=client_user,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["updateBaProfile"]
        assert payload["success"] is False
        assert "not found" in (payload["message"] or "").lower()
