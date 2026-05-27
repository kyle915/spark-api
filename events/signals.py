"""
Django signals for Google Calendar synchronization and mobile push.

Calendar sync (existing) keeps Google Calendar in step with Event /
AmbassadorEvent rows. Push notifications (new) fire across three
moments BAs care about:

  - shift-offer: BA was invited to an Event (AmbassadorEvent created)
  - activation reminder: 15 min before Event.start_time, once the
    invite is accepted (is_approved=True)
  - recap nudge: 4 hours after Event.end_time, if the BA hasn't filed
    a Recap yet (worker re-checks state at fire time)

All push paths are best-effort — the queue layer can fail (no Redis on
Cloud Run by default) without aborting Event/AmbassadorEvent saves.
"""
import datetime
import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from events.models import Event, Request
from ambassadors.models import AmbassadorEvent
from events.tasks import sync_event_to_all_connected_users
from utils.queues import Queues

logger = logging.getLogger(__name__)
queues: Queues = Queues()

# How far before start_time we ping the BA. Keep aligned with what the
# mobile app's location tracker considers "activation window."
ACTIVATION_REMINDER_LEAD = datetime.timedelta(minutes=15)

# How long after end_time we wait before nudging an unfiled recap.
# Long enough that BAs who file from the parking lot don't get pinged;
# short enough that the nudge still feels relevant.
RECAP_NUDGE_DELAY = datetime.timedelta(hours=4)

# How far before start_time we send the pre-shift checklist push.
# Two hours gives BAs enough time to grab uniform + materials and head
# out, but not so far ahead that they forget by the time they arrive.
PRE_SHIFT_CHECKLIST_LEAD = datetime.timedelta(hours=2)


@receiver(post_save, sender=Event)
def sync_event_on_create_or_update(sender, instance: Event, created: bool, **kwargs):
    user = instance.created_by
    # If the user is an ambassador, skip the sync
    if user and user.role and user.role._is_ambassador:
        logger.info(
            f"Event {instance.id} created by ambassador, skipping sync (will be handled by AmbassadorEvent)")
        return

    # Calendar sync is best-effort. Cloud Run has no Redis, so RQ enqueue
    # will raise — we don't want that to bubble up and abort Event.save()
    # (which would break approve_request, create_event, etc).
    try:
        queues.default.add(sync_event_to_all_connected_users, instance.id)
    except Exception as exc:
        logger.warning(
            f"Skipping calendar sync for event {instance.id} — queue unavailable: {exc}"
        )


@receiver(post_save, sender=AmbassadorEvent)
def sync_event_for_ambassador(sender, instance: AmbassadorEvent, created: bool, **kwargs):
    # Same best-effort posture — don't let calendar sync abort the
    # ambassador-event save path.
    try:
        from events.jobs.google_calendar_jobs import EventGoogleCalendarJob
        job: EventGoogleCalendarJob = EventGoogleCalendarJob(instance.event_id)
        job.send_to_ambassadors()
    except Exception as exc:
        logger.warning(
            f"Skipping ambassador calendar sync for event {instance.event_id}: {exc}"
        )


@receiver(post_save, sender=AmbassadorEvent)
def push_on_ambassador_event_change(
    sender, instance: AmbassadorEvent, created: bool, **kwargs
):
    """Fan out push notifications for shift offers + activation + recap.

    Best-effort — wrapped so any failure (no Redis, no devices, no
    user, missing event start_time) is logged and dropped.
    """
    try:
        from ambassadors.push import (
            enqueue_push,
            schedule_push_at,
            schedule_recap_nudge_at,
        )

        ambassador = getattr(instance, "ambassador", None)
        user = getattr(ambassador, "user", None) if ambassador else None
        if not user:
            return

        event = getattr(instance, "event", None)
        if not event:
            return

        event_name = (event.name or "your upcoming shift")[:80]
        # Offer-only payload: includes ambassadorEventUuid so the mobile
        # push tap handler mounts the ShiftOfferScreen for accept/decline.
        # Use this ONLY for the initial invite — once approved, tapping
        # the activation reminder should land on the Shifts tab, not
        # re-open the offer screen.
        offer_data = {
            "screen": "shifts",
            "eventUuid": str(event.uuid),
            "ambassadorEventUuid": str(instance.uuid),
        }
        # Reminder-only payload: no ambassadorEventUuid, so the mobile
        # tap handler falls through to data.screen and routes to the
        # Shifts tab via navigationRef.
        reminder_data = {
            "screen": "shifts",
            "eventUuid": str(event.uuid),
        }

        if created:
            # Shift offer — invited but not yet approved.
            enqueue_push(
                user.id,
                title="New shift offered",
                body=event_name,
                data=offer_data,
            )

        # If the invite has been approved AND the event has a start_time,
        # schedule the activation reminder + recap nudge. update_or_create
        # paths hit post_save with created=False, so we wire from both.
        if instance.is_approved and event.start_time:
            # Pre-shift checklist: 2h before start. Nudges BAs to grab
            # uniform + materials + check the briefing before they head
            # out. Generic copy reusable across brands; per-tenant body
            # text can come from event.notes once we wire that path.
            schedule_push_at(
                event.start_time - PRE_SHIFT_CHECKLIST_LEAD,
                user.id,
                title="Pre-shift checklist",
                body=(
                    f"Shift in 2h: {event_name}. "
                    "Open the briefing and grab your uniform + materials."
                ),
                data={**reminder_data, "kind": "pre_shift_checklist"},
            )
            schedule_push_at(
                event.start_time - ACTIVATION_REMINDER_LEAD,
                user.id,
                title="Your shift starts in 15 minutes",
                body=event_name,
                data=reminder_data,
            )
            if event.end_time:
                schedule_recap_nudge_at(
                    event.end_time + RECAP_NUDGE_DELAY,
                    user.id,
                    ambassador.id,
                    event.id,
                    title="Don't forget your recap",
                    body=f"Submit your recap for {event_name}",
                    data={
                        "screen": "recap",
                        "eventUuid": str(event.uuid),
                    },
                )
    except Exception as exc:
        logger.warning(
            "push wiring failed for ambassador_event=%s: %s", instance.id, exc
        )


# ----------------------------------------------------------------------
# Auto-create Pending Job on Request approval
# ----------------------------------------------------------------------
#
# When admin flips a request to "approved", we want every Event under
# that request to land in the admin Jobs page Pending queue. The Job
# lifecycle (Pending → Posted → Filled) ships separately; this hook
# just bridges the request approval to the job creation.
#
# Idempotent: skips events that already have a Job. Best-effort: a
# Job creation failure logs and continues — the approval itself
# already shipped, we don't want to roll it back.
def create_pending_jobs_for_request(request: Request) -> int:
    """Create a Pending Job for every Event under an approved request.

    Idempotent (skips events that already have a Job) and best-effort
    (per-event failures are logged, never raised). Returns the count of
    jobs created.

    Why this is a standalone function and not only the post_save signal:
    the Request post_save fires BEFORE the resolver materializes the
    Event — approve_request / create_request both save the request, THEN
    call ``Event.objects.from_request``. At signal time the request has
    no events yet, so the signal alone never creates anything. Resolvers
    call this explicitly right after creating the event; the signal also
    delegates here for any path that re-saves an already-evented request.
    """
    created_count = 0
    try:
        status = getattr(request, "status", None)
        slug = (getattr(status, "slug", None) or "").lower()
        if slug != "approved":
            return 0

        from jobs.models import Job, STATUS_PENDING
        from jobs.models import JobTitle, Rate

        # Reverse accessor for Event.request is `event_set` (the FK has no
        # related_name). The old code used `instance.events`, which raised
        # AttributeError and was swallowed — so the auto-job never fired on
        # any path. Use event_set. Count per request is small (1-3 events).
        events = list(request.event_set.all())
        if not events:
            return 0

        # Default JobTitle + Rate for the tenant. We just need *a*
        # pointer to populate the non-null FKs; the admin edits these
        # during posting. Picking the first JobTitle/Rate for the
        # tenant; fall back to the first global one.
        def _first_for(model, tenant_id):
            try:
                qs = model.objects.filter(tenant_id=tenant_id).order_by("id")
                row = qs.first()
                if row:
                    return row
            except Exception:
                pass
            try:
                return model.objects.order_by("id").first()
            except Exception:
                return None

        default_title = _first_for(JobTitle, request.tenant_id)
        default_rate = _first_for(Rate, request.tenant_id)
        if not default_title or not default_rate:
            logger.warning(
                "auto_create_job: tenant %s missing JobTitle or Rate — skipping",
                request.tenant_id,
            )
            return 0

        for ev in events:
            try:
                if Job.objects.filter(event_id=ev.id).exists():
                    continue
                Job.objects.create(
                    tenant_id=request.tenant_id,
                    event_id=ev.id,
                    name=(ev.name or request.name or "Activation")[:200],
                    address=ev.address or request.address or "",
                    start_date=ev.start_time,
                    end_date=ev.end_time,
                    job_title=default_title,
                    rate=default_rate,
                    lifecycle_status=STATUS_PENDING,
                    favorites_only=True,
                    public=False,
                    closed=False,
                    national=False,
                    ongoing=False,
                    created_by_id=getattr(request, "approved_by_id", None)
                    or request.created_by_id,
                    updated_by_id=getattr(request, "approved_by_id", None)
                    or request.created_by_id,
                )
                created_count += 1
            except Exception as exc:
                logger.warning(
                    "auto_create_job: failed for event=%s: %s",
                    getattr(ev, "id", None),
                    exc,
                )
    except Exception as exc:
        logger.warning(
            "auto_create_job: unexpected failure for request=%s: %s",
            getattr(request, "id", None),
            exc,
        )
    return created_count


@receiver(post_save, sender=Request)
def auto_create_pending_job_on_request_approval(
    sender, instance: Request, created: bool, **kwargs
):
    # Thin wrapper around create_pending_jobs_for_request. Note this fires
    # before the resolver materializes the Event, so on the create/approve
    # paths it's a no-op (no events yet) and the resolver calls the helper
    # directly. Kept for any path that re-saves an already-evented request.
    create_pending_jobs_for_request(instance)


# ----------------------------------------------------------------------
# Google Sheets master-tracker mirror
# ----------------------------------------------------------------------
#
# When a Request changes, mirror the row to the tenant's linked Sheet
# (see Tenant.linked_sheet_url). Push happens via django-rq so the
# user-facing save doesn't wait on a Sheets API round-trip; falls
# back to inline sync on dev/test where there's no Redis.
@receiver(post_save, sender=Request)
def mirror_request_to_sheets(sender, instance: Request, created: bool, **kwargs):
    try:
        from utils.sheets_mirror import upsert_request_row
        try:
            queues.default.add(upsert_request_row, instance)
        except Exception:
            # No queue (dev/test or Redis down) — do it inline so the
            # behavior is at least visible. Wrapped so a Sheets API
            # error still doesn't bubble up.
            try:
                upsert_request_row(instance)
            except Exception as exc:
                logger.warning(
                    "sheets mirror inline failed for request=%s: %s",
                    getattr(instance, "id", None),
                    exc,
                )
    except Exception as exc:
        logger.warning(
            "sheets mirror dispatch failed for request=%s: %s",
            getattr(instance, "id", None),
            exc,
        )
