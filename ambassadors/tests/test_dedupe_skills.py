"""Coverage for the `dedupe_skills` management command.

The global Skill table accumulated ~9 copies of each default (createTenant
re-created the list per tenant until #772). The command merges duplicates by
case-insensitive name: AmbassadorSkill links are repointed to the lowest-id
keeper (redundant double-links dropped), then the duplicate rows deleted.
"""

import io

import pytest
from django.core.management import call_command

from ambassadors.models import AmbassadorSkill, Skill
from ambassadors.tests.base import AmbassadorsGraphQLTestCase


@pytest.mark.django_db
class TestDedupeSkills(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.system_user = self.get_system_user()
        self.roles = self.setup_default_roles()
        self.ba_a = self.create_ambassador(
            self.create_user(
                username="dedupe-a",
                email="dedupe-a@test.com",
                role=self.roles["ambassador"],
            )
        )
        self.ba_b = self.create_ambassador(
            self.create_user(
                username="dedupe-b",
                email="dedupe-b@test.com",
                role=self.roles["ambassador"],
            )
        )

        def make_skill(name):
            return Skill.objects.create(name=name, created_by=self.system_user)

        # "Teamwork" twice (case-variant dup) + "Leadership" twice.
        self.team_keep = make_skill("Teamwork")
        self.team_dup = make_skill("teamwork")
        self.lead_keep = make_skill("Leadership")
        self.lead_dup = make_skill("Leadership")
        # An unduplicated skill must be untouched.
        self.solo = make_skill("Problem Solving")

        def link(ba, skill):
            return AmbassadorSkill.objects.create(
                ambassador=ba, skill=skill, created_by=self.system_user
            )

        # A → teamwork-dup only: must be REPOINTED to the keeper.
        self.link_repoint = link(self.ba_a, self.team_dup)
        # B → keeper AND dup: the dup link must be DROPPED (no double-link).
        self.link_b_keep = link(self.ba_b, self.team_keep)
        self.link_b_dup = link(self.ba_b, self.team_dup)
        # A → leadership dup: repointed.
        self.link_lead = link(self.ba_a, self.lead_dup)

    def test_dry_run_changes_nothing(self):
        out = io.StringIO()
        call_command("dedupe_skills", stdout=out)
        assert Skill.objects.count() == 5
        assert AmbassadorSkill.objects.count() == 4
        assert "would remove 2 duplicate row(s)" in out.getvalue()

    def test_execute_merges_repoints_and_drops(self):
        out = io.StringIO()
        call_command("dedupe_skills", execute=True, stdout=out)

        # Dups gone, keepers + solo remain.
        remaining = set(Skill.objects.values_list("id", flat=True))
        assert remaining == {self.team_keep.id, self.lead_keep.id, self.solo.id}

        # A's teamwork link repointed to the keeper.
        self.link_repoint.refresh_from_db()
        assert self.link_repoint.skill_id == self.team_keep.id
        # A's leadership link repointed.
        self.link_lead.refresh_from_db()
        assert self.link_lead.skill_id == self.lead_keep.id

        # B keeps exactly ONE teamwork link (the redundant dup link dropped).
        b_team_links = AmbassadorSkill.objects.filter(
            ambassador=self.ba_b, skill_id=self.team_keep.id
        )
        assert b_team_links.count() == 1
        assert not AmbassadorSkill.objects.filter(
            id=self.link_b_dup.id
        ).exists()

        # Nothing points at a deleted skill.
        assert not AmbassadorSkill.objects.exclude(
            skill_id__in=remaining
        ).exists()

    def test_idempotent_after_execute(self):
        call_command("dedupe_skills", execute=True, stdout=io.StringIO())
        out = io.StringIO()
        call_command("dedupe_skills", execute=True, stdout=out)
        assert "No duplicate skill names" in out.getvalue()
        assert Skill.objects.count() == 3
