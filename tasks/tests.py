"""A retried recap-approved task must not re-run the PDF render."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import async_to_sync
from django.test import RequestFactory, override_settings

from recaps import models
from tasks.views import recap_approved_notify_view

SECRET = "tasks-test-secret"


def _call(retry_count: str | None, *, html_only: bool = False):
    headers = {"HTTP_X_TASKS_SECRET": SECRET}
    if retry_count is not None:
        headers["HTTP_X_CLOUDTASKS_TASKRETRYCOUNT"] = retry_count
    request = RequestFactory().post(
        "/api/tasks/recap-approved-notify",
        data=json.dumps({"recap_id": 1443, "recap_kind": "custom", "html_only": html_only}),
        content_type="application/json",
        **headers,
    )
    recap = SimpleNamespace(id=1443)
    manager = MagicMock()
    manager.select_related.return_value.get.return_value = recap
    ensure_pdf = AsyncMock()
    notify = AsyncMock()
    with override_settings(CLOUD_TASKS_SECRET=SECRET), patch.object(
        models.CustomRecap, "objects", manager
    ), patch("recaps.mutations._ensure_recap_pdf_for_notify", ensure_pdf), patch(
        "recaps.mutations._notify_recap_approved_to_rmm_or_clients", notify
    ):
        response = async_to_sync(recap_approved_notify_view)(request)
    return response, ensure_pdf, notify


@pytest.mark.parametrize("retry_count", [None, "0"])
def test_first_attempt_renders_pdf(retry_count):
    response, ensure_pdf, notify = _call(retry_count)

    assert response.status_code == 200
    ensure_pdf.assert_awaited_once()
    assert notify.await_args.kwargs["html_only"] is False


def test_retry_after_crash_sends_link_only():
    response, ensure_pdf, notify = _call("1")

    assert response.status_code == 200
    ensure_pdf.assert_not_awaited()
    assert notify.await_args.kwargs["html_only"] is True


def test_garbage_retry_header_is_treated_as_first_attempt():
    _, ensure_pdf, _ = _call("abc")

    ensure_pdf.assert_awaited_once()
