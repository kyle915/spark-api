"""Coverage for the `dedupe_skills` management command.

History: createTenant re-created the global DEFAULT_SKILLS list on every
tenant creation until #772, leaving 9 copies of each default. The command
merged them in production (180 links repointed, 45 redundant links dropped,
40 duplicate rows deleted), and migration 0030 then added a case-insensitive
unique constraint on Skill.name.

The constraint now makes duplicate fixtures impossible to create, so the
merge path can no longer be exercised in tests (it was verified against the
real data before the constraint landed). What remains testable — and what we
pin here — is that the command no-ops cleanly on a constraint-guarded table.
The command itself is kept for environments that haven't migrated yet (the
0030 docstring points at it).
"""

import io

import pytest
from django.core.management import call_command

from ambassadors.models import Skill
from ambassadors.tests.base import AmbassadorsGraphQLTestCase


@pytest.mark.django_db
class TestDedupeSkills(AmbassadorsGraphQLTestCase):
    def test_clean_table_is_a_noop(self):
        system_user = self.get_system_user()
        for name in ["Teamwork", "Leadership", "Problem Solving"]:
            Skill.objects.create(name=name, created_by=system_user)

        out = io.StringIO()
        call_command("dedupe_skills", execute=True, stdout=out)
        assert "No duplicate skill names" in out.getvalue()
        assert Skill.objects.count() == 3
