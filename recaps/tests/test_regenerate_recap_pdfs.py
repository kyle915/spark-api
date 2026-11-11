"""`regenerate_recap_pdfs`: narrow scope, dry-run by default, paged."""

from __future__ import annotations

import json
from datetime import datetime
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models

HELPERS = "recaps.mutation_parts.pdf_helpers"


@pytest.mark.django_db(transaction=True)
class TestRegenerateRecapPdfs(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Regen Brand")
        self.et = event_models.EventType.objects.create(
            name="Retail Sampling", slug="regen-retail", tenant=self.tenant,
            created_by=self.sys,
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Regen Retail", event_type=self.et, tenant=self.tenant,
            created_by=self.sys,
        )

    def _recap(self, *, day, month=10, approved=True, archived=False):
        when = timezone.make_aware(datetime(2026, month, day, 12))
        event = event_models.Event.objects.create(
            name=f"Store {month}-{day}", tenant=self.tenant, address="1 Main St",
            date=when, event_type=self.et, created_by=self.sys, updated_by=self.sys,
        )
        return recap_models.CustomRecap.objects.create(
            name=f"r{month}-{day}", approved=approved, submitted_at=when, event=event,
            tenant=self.tenant, custom_recap_template=self.template,
            archived_at=timezone.now() if archived else None, created_by=self.sys,
        )

    def _run(self, **kwargs):
        out = StringIO()
        call_command(
            "regenerate_recap_pdfs", start="2026-10-01", end="2026-10-31",
            stdout=out, **kwargs,
        )
        text = out.getvalue()
        report = json.loads(text.rsplit("JSON_RESULT: ", 1)[1])
        return text, report

    def test_dry_run_lists_and_renders_nothing(self):
        recap = self._recap(day=5)
        with patch(f"{HELPERS}._list_spark_generated_pdfs", return_value=[object()]), patch(
            f"{HELPERS}._render_and_store_recap_pdf_sync"
        ) as render:
            text, report = self._run()
        render.assert_not_called()
        assert f"would rebuild recap {recap.id}" in text
        assert report["apply"] is False and report["rebuilt"] == 0
        assert report["with_spark_pdf"] == 1

    def test_apply_rebuilds_only_approved_live_recaps_in_window_that_have_a_pdf(self):
        has_pdf = self._recap(day=5)
        no_pdf = self._recap(day=6)
        self._recap(day=7, approved=False)
        self._recap(day=8, archived=True)
        self._recap(day=9, month=9)  # outside the window

        def spark_pdfs(recap):
            return [object()] if recap.id == has_pdf.id else []

        with patch(f"{HELPERS}._list_spark_generated_pdfs", side_effect=spark_pdfs), patch(
            f"{HELPERS}._render_and_store_recap_pdf_sync", return_value=object()
        ) as render:
            _text, report = self._run(apply=True)

        assert [c.args[0].id for c in render.call_args_list] == [has_pdf.id]
        assert render.call_args.kwargs == {"force": True}
        assert report["rebuilt"] == 1 and report["failed"] == 0
        assert report["skipped_no_spark_pdf"] == 1
        assert report["next_after_id"] is None
        assert no_pdf.id not in [c.args[0].id for c in render.call_args_list]

    def test_pages_and_resumes(self):
        recaps = [self._recap(day=d) for d in (1, 2, 3)]
        with patch(f"{HELPERS}._list_spark_generated_pdfs", return_value=[object()]), patch(
            f"{HELPERS}._render_and_store_recap_pdf_sync", return_value=object()
        ) as render:
            _t, first = self._run(apply=True, limit=2)
            assert first["rebuilt"] == 2
            assert first["next_after_id"] == recaps[1].id
            _t, second = self._run(apply=True, limit=2, after_id=first["next_after_id"])
        assert second["rebuilt"] == 1 and second["next_after_id"] is None
        assert [c.args[0].id for c in render.call_args_list] == [r.id for r in recaps]

    def test_a_failed_render_is_counted_and_does_not_stop_the_run(self):
        a, b = self._recap(day=1), self._recap(day=2)

        def render(recap, force=False):
            if recap.id == a.id:
                raise RuntimeError("boom")
            return object()

        with patch(f"{HELPERS}._list_spark_generated_pdfs", return_value=[object()]), patch(
            f"{HELPERS}._render_and_store_recap_pdf_sync", side_effect=render
        ):
            text, report = self._run(apply=True)
        assert report["failed"] == 1 and report["rebuilt"] == 1
        assert f"FAILED recap {a.id}" in text and f"rebuilt recap {b.id}" in text

    def test_bad_dates_are_rejected(self):
        with pytest.raises(CommandError):
            call_command("regenerate_recap_pdfs", start="2026-10-31", end="2026-10-01")
        with pytest.raises(CommandError):
            call_command("regenerate_recap_pdfs", start="nope", end="2026-10-01")
