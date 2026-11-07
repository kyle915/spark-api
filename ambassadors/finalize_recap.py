"""Retryable check-in finalize (DQ guard + admin/crew notify).

The CustomRecap row is already committed before finalize runs. Status is
persisted so silent post-submit gaps are visible and Cloud Tasks can retry.
"""

from __future__ import annotations

import logging

from django.utils import timezone

logger = logging.getLogger(__name__)

FINALIZE_PENDING = "pending"
FINALIZE_OK = "ok"
FINALIZE_FAILED = "failed"

# Structured metric key — greppable in Cloud Logging / Spark alerts.
METRIC_FINALIZE_FAILED = "spark.metric=finalize_recap_failed"
METRIC_FINALIZE_OK = "spark.metric=finalize_recap_ok"


def _set_status(recap_id: int, status: str, error: str = "") -> None:
    from recaps.models import CustomRecap

    CustomRecap.objects.filter(pk=recap_id).update(
        finalize_status=status,
        finalize_error=(error or "")[:2000],
        finalize_attempted_at=timezone.now(),
    )


def run_finalize_recap(recap_id: int) -> bool:
    """Run DQ + notify steps with fresh DB connections. Returns True on full ok."""
    from asgiref.sync import async_to_sync

    from ambassadors.checkin_web import notify_checkin_recap_submitted
    from events.walkin_link import try_link_orphan_event
    from recaps import models as rmodels
    from recaps.mutations import (
        _guard_recap_data_quality,
        _notify_recap_ready_for_review_to_admins,
    )
    from utils.db import close_thread_connections, fresh_db_connection
    from utils.outbound import block_if_suppressed

    if block_if_suppressed("recap submitted notifications", recap_id):
        close_thread_connections()
        try:
            fresh_db_connection(_set_status)(recap_id, FINALIZE_OK, "suppressed")
        finally:
            close_thread_connections()
        return True

    errors: list[str] = []

    def _run() -> None:
        _set_status(recap_id, FINALIZE_PENDING)
        recap = rmodels.CustomRecap.objects.select_related(
            "created_by", "event", "tenant"
        ).get(id=recap_id)
        created_by = recap.created_by
        try:
            async_to_sync(_guard_recap_data_quality)(recap)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "checkin recap: data-quality guard failed id=%s", recap_id
            )
            errors.append(f"dq:{exc}")
        try:
            async_to_sync(_notify_recap_ready_for_review_to_admins)(
                recap, created_by
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "checkin recap: notify-admins failed id=%s", recap_id
            )
            errors.append(f"notify:{exc}")
        try:
            notify_checkin_recap_submitted(recap)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "checkin recap: crew notify failed id=%s", recap_id
            )
            errors.append(f"crew:{exc}")
        event_id = getattr(recap, "event_id", None)
        if event_id:
            try:
                try_link_orphan_event(int(event_id))
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "checkin recap: walkin auto-link failed id=%s", recap_id
                )
                errors.append(f"link:{exc}")

        if errors:
            logger.error(
                "%s recap_id=%s steps=%s",
                METRIC_FINALIZE_FAILED,
                recap_id,
                ";".join(errors)[:500],
            )
            _set_status(recap_id, FINALIZE_FAILED, "; ".join(errors)[:2000])
        else:
            logger.info("%s recap_id=%s", METRIC_FINALIZE_OK, recap_id)
            _set_status(recap_id, FINALIZE_OK)

    close_thread_connections()
    try:
        fresh_db_connection(_run)()
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "%s checkin recap: finalize body failed id=%s",
            METRIC_FINALIZE_FAILED,
            recap_id,
        )
        close_thread_connections()
        try:
            fresh_db_connection(_set_status)(recap_id, FINALIZE_FAILED, str(exc))
        except Exception:  # noqa: BLE001
            pass
        return False
    finally:
        close_thread_connections()

    return not errors


def enqueue_finalize_recap(recap_id: int) -> None:
    """Enqueue finalize via Cloud Tasks, else run on a worker thread."""
    import concurrent.futures

    from utils.cloud_tasks import enqueue_or_background

    path = "/api/tasks/checkin-recap-finalize"
    payload = {"recap_id": int(recap_id)}

    def _fallback() -> None:
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                ex.submit(run_finalize_recap, recap_id).result(timeout=90)
        except Exception:  # noqa: BLE001
            logger.exception(
                "%s checkin recap: finalize thread failed id=%s",
                METRIC_FINALIZE_FAILED,
                recap_id,
            )
            try:
                from utils.db import close_thread_connections, fresh_db_connection

                close_thread_connections()
                fresh_db_connection(_set_status)(
                    recap_id, FINALIZE_FAILED, "thread failed"
                )
            except Exception:  # noqa: BLE001
                pass

    enqueue_or_background(path, payload, _fallback)
