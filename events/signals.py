"""
Django signals for Google Calendar synchronization and mobile push.

Calendar sync (existing) keeps Google Calendar in step with Event /
AmbassadorEvent rows. Push notifications fire at moments BAs care about.
The two fired from THIS signal:

  - shift-offer: BA was invited to an Event (AmbassadorEvent created) —
    sent immediately via enqueue_push (inline fallback when Redis is down).
  - pre-shift checklist: ~2h before Event.start_time, once approved —
    scheduled via schedule_push_at.

The "your shift starts soon" activation reminder and the "don't forget
your recap" nudge are NO LONGER fired here. They used to be scheduled at
AmbassadorEvent-creation time via django-rq, but there is no rqscheduler
in prod so they never fired. They are now driven by wall-clock crons that
send inline (no worker):
  - send_activation_reminders → /internal/cron/activation-reminders
  - send_recap_nudges → /internal/cron/recap-nudges

All push paths are best-effort — the queue layer can fail (no Redis on
Cloud Run by default) without aborting Event/AmbassadorEvent saves.
"""
import datetime
import logging

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone as django_timezone

from events.models import Event, Request
from ambassadors.models import AmbassadorEvent
from events.tasks import sync_event_to_all_connected_users
from utils.queues import Queues

logger = logging.getLogger(__name__)
queues: Queues = Queues()

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
    # Calendar sync makes LIVE Google API calls (OAuth token refresh +
    # event create/update). google-auth's requests transport defaults to a
    # 120s timeout and httplib2 has none, so a stalled call would BLOCK this
    # post_save — i.e. freeze the invite/accept mutation that triggered the
    # save for up to ~2 minutes (a hung socket doesn't raise, so the
    # try/except can't save us). It's best-effort decoration, so fire it off
    # the request thread: the save (and the invite) returns instantly and
    # the calendar push happens in the background.
    #
    # Only the event_id (an int) is captured — never the model instance —
    # so the thread can't touch a stale/detached object. The background ORM
    # reads run under fresh_db_connection (a pooled thread keeps a
    # thread-local connection Django's request cleanup never closes).
    event_id = instance.event_id

    def _run() -> None:
        from utils.db import fresh_db_connection

        def _sync() -> None:
            from events.jobs.google_calendar_jobs import EventGoogleCalendarJob

            EventGoogleCalendarJob(event_id).send_to_ambassadors()

        try:
            fresh_db_connection(_sync)()
        except Exception as exc:  # noqa: BLE001 — best-effort, never propagate
            logger.warning(
                "Ambassador calendar sync failed for event %s: %s", event_id, exc
            )

    try:
        import threading

        threading.Thread(
            target=_run, name=f"cal-sync-ae-{event_id}", daemon=True
        ).start()
    except Exception as exc:  # noqa: BLE001 — spawning failed; skip, never block
        logger.warning(
            "Could not start calendar sync thread for event %s: %s", event_id, exc
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
        # schedule the pre-shift checklist. update_or_create paths hit
        # post_save with created=False, so we wire from both.
        #
        # NOTE: the activation reminder ("your shift starts soon") and the
        # recap nudge ("don't forget your recap") used to be scheduled here
        # too, via schedule_push_at / schedule_recap_nudge_at. Those never
        # fired — there is no rqscheduler running in prod, so the django-rq
        # scheduled jobs were silently dropped. They are now driven by
        # wall-clock crons that send inline (no worker), which is the single
        # source of truth for both:
        #   - activation reminder → events/management/commands/
        #       send_activation_reminders.py  (/internal/cron/activation-reminders)
        #   - recap nudge → recaps/management/commands/send_recap_nudges.py
        #       (/internal/cron/recap-nudges)
        # Do NOT re-add scheduled sends here.
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
    except Exception as exc:
        logger.warning(
            "push wiring failed for ambassador_event=%s: %s", instance.id, exc
        )


# ----------------------------------------------------------------------
# Auto-post jobs to the BA board on Request approval
# ----------------------------------------------------------------------
#
# When admin flips a request to "approved", we want every open-gig Event
# under that request to land LIVE on the BA job board (my_available_jobs),
# which only surfaces jobs with lifecycle_status == 'posted'. Previously
# the auto-created Job sat in 'pending' and never reached the board, so an
# admin had to manually re-post each one. Kyle asked for auto-post.
#
# Open gig vs. pre-assigned: a request whose scheduling_status is
# "already_scheduled" is already booked with the store — there's no open
# slot to staff, so we leave that Job 'pending' (admin can still post it
# by hand). Anything else (needs_scheduling, or unknown/legacy null) is
# treated as an open gig and posted to the board.
#
# Posting mirrors the post_job / post_event_to_board mutations exactly:
# lifecycle_status='posted', posted_at=now(), public=True. We keep
# favorites_only=True (the model's default posted state — gated to the
# tenant's favorites until an admin clicks "Open to all"). The daily
# new-gig digest (send_new_gig_digest) picks these up via posted_at; it's
# a batched once-a-day cron, not a per-save push, so posting here does not
# double-notify.
#
# Idempotent: skips events that already have a Job. Best-effort: a
# Job creation failure logs and continues — the approval itself
# already shipped, we don't want to roll it back.
def create_pending_jobs_for_request(request: Request) -> int:
    """Auto-create + post a Job for every open-gig Event under an
    approved request.

    Returns the count of jobs created. Idempotent (skips events that
    already have a Job) and best-effort (per-event failures are logged,
    never raised).

    Open-gig Events are created directly in the 'posted' lifecycle state
    so they appear on the BA job board immediately. Requests already
    booked with the store (scheduling_status == "already_scheduled")
    skip auto-post and keep their Job 'pending' for manual handling.

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

        # Open gig vs. pre-assigned. "already_scheduled" means the demo is
        # already booked with the store — no open slot to staff, so don't
        # auto-post it to the board. Everything else (needs_scheduling, or
        # a legacy/unknown null) is an open gig we want on the board.
        from events.models import SchedulingStatus

        is_open_gig = (
            getattr(request, "scheduling_status", None)
            != SchedulingStatus.ALREADY_SCHEDULED
        )

        # STATUS_PENDING / STATUS_POSTED live on the Job model as class
        # attributes (Job.STATUS_POSTED = "posted"), NOT module-level
        # exports — importing them directly raised ImportError, which
        # (being caught below) is a big reason the auto-job never actually
        # created anything. Reference them via the class instead.
        from jobs.models import Job, JobTitle, Rate

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

        # Open gigs go straight to 'posted' so they appear on the BA job
        # board (my_available_jobs gates on lifecycle_status == 'posted').
        # Pre-assigned (already_scheduled) requests stay 'pending'. When
        # posting, mirror post_job / post_event_to_board: set posted_at and
        # public=True. favorites_only stays True (default posted state —
        # gated to favorites until an admin "Open to all"s it).
        if is_open_gig:
            lifecycle_status = Job.STATUS_POSTED
            posted_at = django_timezone.now()
            public = True
        else:
            lifecycle_status = Job.STATUS_PENDING
            posted_at = None
            public = False

        posted_jobs = []
        for ev in events:
            try:
                if Job.objects.filter(event_id=ev.id).exists():
                    continue
                new_job = Job.objects.create(
                    tenant_id=request.tenant_id,
                    event_id=ev.id,
                    name=(ev.name or request.name or "Activation")[:200],
                    address=ev.address or request.address or "",
                    start_date=ev.start_time,
                    end_date=ev.end_time,
                    job_title=default_title,
                    rate=default_rate,
                    lifecycle_status=lifecycle_status,
                    posted_at=posted_at,
                    favorites_only=True,
                    public=public,
                    closed=False,
                    national=False,
                    ongoing=False,
                    created_by_id=getattr(request, "approved_by_id", None)
                    or request.created_by_id,
                    updated_by_id=getattr(request, "approved_by_id", None)
                    or request.created_by_id,
                )
                created_count += 1
                if is_open_gig:
                    posted_jobs.append(new_job)
            except Exception as exc:
                logger.warning(
                    "auto_create_job: failed for event=%s: %s",
                    getattr(ev, "id", None),
                    exc,
                )

        # At-post-time geo-proximity push for each freshly auto-posted gig
        # (open gigs only; pre-assigned/already_scheduled jobs stay pending
        # and don't reach the board). Best-effort — never breaks approval.
        if posted_jobs:
            try:
                from jobs.notifications import notify_nearby_bas_of_new_gig

                for posted in posted_jobs:
                    notify_nearby_bas_of_new_gig(posted)
            except Exception as exc:
                logger.warning(
                    "auto_create_job: new-gig-nearby push dispatch failed "
                    "for request=%s: %s",
                    getattr(request, "id", None),
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
