"""Regression: check-in finalize must not reuse a dead Cloud SQL handle.

Spark alert ``OperationalError:ambassadors.checkin_web:_finalize_recap_offthread``
(first seen 2026-08-07, still paging e.g. recap id=2565) came from the old
``asyncio.run`` + ``sync_to_async`` path bouncing the CustomRecap load onto
asgiref's long-lived SingleThreadExecutor — a thread Django's request-boundary
connection cleanup never reaches. After Cloud SQL dropped the idle connection
the wrapper kept the closed psycopg handle and every later finalize failed
with ``the connection is closed`` until the Cloud Run instance recycled.

The recap row itself is already committed before finalize runs, so BAs were
not left hanging — only data-quality + notify mail were skipped. This suite
locks the fresh-connection worker path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.db import connections
from django.utils import timezone

from ambassadors.checkin_web import _finalize_recap_offthread
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps.models import CustomRecap, CustomRecapTemplate


@pytest.mark.django_db(transaction=True)
class TestFinalizeRecapOffthreadDb(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.actor = self.get_system_user()
        self.tenant = self.create_tenant(name="Finalize DB Hygiene Co")
        self.etype = self.create_event_type("Retail Sampling", self.tenant)
        self.template = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="Retail Sampling Recap",
            event_type=self.etype,
            created_by=self.actor,
        )
        self.event = self.create_event(
            name="Store demo",
            tenant=self.tenant,
            address="1 Test St",
            event_type=self.etype,
            date=timezone.now(),
        )
        ba_user = self.create_user(
            username="ba-finalize@test.com",
            email="ba-finalize@test.com",
            role=self.roles["ambassador"],
            first_name="Bailey",
            last_name="Ambassador",
        )
        self.ba = self.create_ambassador(ba_user, phone="5550100123")
        self.recap = CustomRecap.objects.create(
            name=self.event.name,
            submitted_at=timezone.now(),
            event=self.event,
            ambassador=self.ba,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.ba.user,
        )

    def _poison_asgiref_executor_connection(self) -> None:
        """Close the psycopg handle on asgiref's shared SingleThreadExecutor.

        That is the exact state the pre-fix ``sync_to_async`` load hit after a
        Cloud SQL idle drop — Django's wrapper still looked open, the socket
        was not.
        """

        def _poison() -> None:
            conn = connections["default"]
            conn.ensure_connection()
            conn.connection.close()

        async def _run() -> None:
            await sync_to_async(_poison)()

        async_to_sync(_run)()

    def test_finalize_loads_recap_despite_dead_executor_handle(self):
        self._poison_asgiref_executor_connection()

        async def _guard_ok(recap):
            assert recap.id == self.recap.id

        async def _notify_ok(recap, created_by):
            assert recap.id == self.recap.id
            assert created_by is not None

        with (
            patch(
                "recaps.mutations._guard_recap_data_quality",
                new=AsyncMock(side_effect=_guard_ok),
            ) as guard_mock,
            patch(
                "recaps.mutations._notify_recap_ready_for_review_to_admins",
                new=AsyncMock(side_effect=_notify_ok),
            ) as notify_mock,
            patch(
                "ambassadors.checkin_web.notify_checkin_recap_submitted"
            ) as crew_mock,
        ):
            # Must not raise; must reach all three best-effort steps.
            _finalize_recap_offthread(self.recap.id)

        assert guard_mock.await_count == 1
        assert notify_mock.await_count == 1
        crew_mock.assert_called_once()
        assert crew_mock.call_args.args[0].id == self.recap.id

        # Don't leak a closed wrapper into later tests on this process.
        connections["default"].close()

    def test_finalize_swallows_outer_failures_without_raising(self):
        with patch(
            "utils.db.fresh_db_connection",
            side_effect=RuntimeError("forced finalize boom"),
        ):
            _finalize_recap_offthread(self.recap.id)  # must not raise
