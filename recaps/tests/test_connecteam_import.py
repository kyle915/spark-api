"""Regression coverage for the Connecteam import EXECUTION path.

`execute_connecteam_import_from_bytes` referenced a nonexistent `input`
(so Python resolved it to the built-in `input()` function) instead of its
`name` parameter. Every import therefore crashed right after parsing with
"'builtin_function_or_method' object has no attribute 'name'". The parser
unit tests only cover parsing/matching and never ran this path, so it
shipped unnoticed. This test runs the execution path end-to-end (parser +
matcher mocked) and asserts a recap is created from the `name` argument.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async

from events.models import EventType
from jobs.tests.base import JobsGraphQLTestCase
from recaps import models as recap_models
from recaps.mutation_parts.connecteam import (
    execute_connecteam_import_from_bytes,
)


@pytest.mark.django_db(transaction=True)
class TestConnecteamImportExecute(JobsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Connecteam Tenant")
        self.user = self.create_user(
            username="ct_admin@test.com",
            email="ct_admin@test.com",
            role=self.roles["spark_admin"],
            password="testpass123",
        )
        self.create_tenanted_user(user=self.user, tenant=self.tenant)
        system_user = self.get_system_user()
        self.event = self.create_event(
            name="Jewel Osco 3442",
            tenant=self.tenant,
            address="1 Test St",
        )
        self.event_type = EventType.objects.create(
            name="Sampling",
            slug="ct-sampling",
            tenant=self.tenant,
            created_by=system_user,
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Connecteam Template",
            event_type=self.event_type,
            tenant=self.tenant,
            created_by=system_user,
        )

    @pytest.mark.asyncio
    async def test_import_uses_name_param_not_builtin(self):
        # A parsed PDF with at least one labeled pair so execution proceeds
        # past the "no fields found" early return into the recap-create path
        # (where the `input.name` bug lived). No embedded images to keep the
        # test off the storage backend.
        parsed = SimpleNamespace(
            raw_pairs=[("Store", "Jewel Osco")],
            page_texts=["Store:: Jewel Osco"],
            images=[],
        )
        with (
            patch("recaps.connecteam.parse_pdf_bytes", return_value=parsed),
            patch("recaps.connecteam.match_fields", return_value=[]),
        ):
            resp = await execute_connecteam_import_from_bytes(
                user=self.user,
                event=self.event,
                template=self.template,
                pdf_bytes=b"%PDF-1.4 fake",
                name="My Import Title",
                input_obj=None,
            )

        # Before the fix this raised
        # "'builtin_function_or_method' object has no attribute 'name'".
        assert resp.success is True, getattr(resp, "message", "")

        recap = await sync_to_async(
            recap_models.CustomRecap.objects.filter(
                name="My Import Title"
            ).first
        )()
        assert recap is not None
        assert recap.event_id == self.event.id
