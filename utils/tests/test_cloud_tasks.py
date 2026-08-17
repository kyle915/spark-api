"""Cloud Tasks enqueue + fallback when CLOUD_TASKS_* is unset."""

from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from utils.cloud_tasks import enqueue, enqueue_or_background


_CLOUD_TASKS_OFF = dict(
    GS_PROJECT_ID="",
    CLOUD_TASKS_QUEUE="",
    CLOUD_TASKS_LOCATION="",
    CLOUD_TASKS_HANDLER_BASE_URL="",
    CLOUD_TASKS_SECRET="",
)


@override_settings(**_CLOUD_TASKS_OFF)
def test_enqueue_returns_false_when_unset():
    assert enqueue("/api/tasks/recap-approved-notify", {"recap_id": 1}) is False


@override_settings(**_CLOUD_TASKS_OFF)
def test_fallback_runs_inline_when_cloud_tasks_unset():
    ran = []
    enqueue_or_background(
        "/api/tasks/recap-approved-notify",
        {"recap_id": 727, "recap_kind": "custom"},
        lambda: ran.append("sent"),
    )
    assert ran == ["sent"]


@override_settings(**_CLOUD_TASKS_OFF)
def test_fallback_inline_does_not_start_a_thread():
    with patch("threading.Thread") as thread_cls:
        enqueue_or_background("/api/tasks/x", {}, lambda: None)
    thread_cls.assert_not_called()


@pytest.mark.django_db
@override_settings(**_CLOUD_TASKS_OFF)
def test_fallback_can_touch_orm_when_cloud_tasks_unset():
    """Production approve path: queue off, fallback must not raise."""

    def _count_users():
        return get_user_model().objects.count()

    enqueue_or_background("/api/tasks/recap-approved-notify", {}, _count_users)


@pytest.mark.django_db
def test_thread_fallback_closes_connections_and_can_query(monkeypatch):
    """Configured queue + failed enqueue: worker thread must use its own connection."""
    import threading

    monkeypatch.setattr("utils.cloud_tasks._is_enabled", lambda: True)
    monkeypatch.setattr("utils.cloud_tasks.enqueue", lambda *a, **k: False)

    done = threading.Event()
    errors: list[BaseException] = []

    def _fallback():
        try:
            get_user_model().objects.count()
        except BaseException as exc:  # noqa: BLE001 — capture for the test thread
            errors.append(exc)
        finally:
            done.set()

    with patch("threading.Thread", wraps=threading.Thread) as thread_cls:
        enqueue_or_background("/api/tasks/recap-approved-notify", {}, _fallback)
        assert thread_cls.called

    assert done.wait(timeout=5)
    assert errors == []
