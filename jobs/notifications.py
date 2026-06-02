"""At-post-time "new gig near you" push.

When an admin posts a job to the BA board (post_job, post_event_to_board,
or the auto-post path in events/signals.create_pending_jobs_for_request),
we immediately nudge eligible BAs that a gig just dropped near them.

This is the *per-post* counterpart to the once-a-day digest
(`jobs/management/commands/send_new_gig_digest.py`). The two have
different cadences and don't double-fire within a single post: this
helper dedupes per-call, and a freshly-posted job only triggers one
at-post push (here) plus, separately, the next daily digest. We mirror
the digest's recipient gathering + eligibility gates exactly so the two
agree on who's reachable.

Eligibility (identical to the digest + the my_available_jobs board):
  - BA has an active PushDevice (only reachable devices).
  - AmbassadorJobPreference.notify_new_gigs is True (default True when no
    preference row exists).
  - favorites_only gate: if the job is favorites_only, the BA must be on
    the tenant's TenantFavoriteAmbassador roster. (Non-favorites jobs are
    visible cross-tenant on the board, so we don't add a tenant-membership
    filter — that would narrow reach below what the board already shows.)
  - The BA hasn't already applied to this job.

Distance with a state fallback (the data gap mitigation):
  - Event.coordinates and many Ambassador.coordinates are frequently NULL
    (no geocoder populates them). Proximity alone would reach almost
    nobody, so:
      * If BOTH the job's event coordinates and the BA's coordinates are
        present, include the BA only when the great-circle distance is
        <= NEARBY_RADIUS_MILES, and put the rounded distance in the
        payload.
      * Otherwise fall back to preferred-state matching (same state logic
        as the daily digest): include the BA when the job's event state
        matches one of their preferred_state_codes (empty = all states).
        These are flagged as non-distance matches (no distanceMiles).
  - A BA is never pushed twice for the same job in one call.

Best-effort: a push failure for one BA is logged and skipped; it never
aborts the rest, and it never breaks the post that triggered it.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Proximity threshold for the at-post push. Kept module-level so tests and
# future tuning have one knob.
NEARBY_RADIUS_MILES = 30.0


def notify_nearby_bas_of_new_gig(job) -> int:
    """Push "new gig near you" to eligible BAs for a freshly-posted job.

    `job` is a jobs.models.Job that has just transitioned to POSTED.
    Returns the number of BAs we enqueued a push for. Fully best-effort:
    any failure is swallowed (logged) so a bad token or a transient queue
    issue never breaks the job post.

    Synchronous + DB-touching: call it inside the same sync_to_async block
    that saved the job (mutations) or directly from the sync signal path.
    """
    try:
        return _notify(job)
    except Exception:
        logger.exception(
            "new-gig-nearby push failed for job=%s", getattr(job, "id", None)
        )
        return 0


def _notify(job) -> int:
    from ambassadors.models import Ambassador, PushDevice
    from ambassadors.push import enqueue_push
    from ambassadors.staffing import _haversine_miles
    from jobs import models as jm

    # Only POSTED jobs reach the board; guard so a stray caller can't push
    # for a pending/filled/canceled job.
    if getattr(job, "lifecycle_status", None) != jm.Job.STATUS_POSTED:
        return 0

    event = getattr(job, "event", None)
    job_coords = getattr(event, "coordinates", None) if event else None
    # State code for the fallback path (mirrors the digest's state logic).
    job_state_code = (
        getattr(getattr(event, "state", None), "code", None) if event else None
    )
    job_state_code = (job_state_code or "").strip().upper() or None

    # A short venue label for the push body. Prefer the event name, fall
    # back to the job name, then a generic word.
    venue = (
        (getattr(event, "name", None) if event else None)
        or getattr(job, "name", None)
        or "a venue"
    )

    # Only BAs with an active push device are reachable (same as digest).
    device_user_ids = set(
        PushDevice.objects.filter(is_active=True).values_list("user_id", flat=True)
    )
    if not device_user_ids:
        return 0

    ambs = list(
        Ambassador.objects.filter(user_id__in=device_user_ids).select_related("user")
    )
    if not ambs:
        return 0

    amb_ids = [a.id for a in ambs]

    prefs_by_amb = {
        p.ambassador_id: p
        for p in jm.AmbassadorJobPreference.objects.filter(ambassador_id__in=amb_ids)
    }

    # Tenant favorites — only needed to satisfy the favorites_only gate for
    # this job's tenant, so scope the lookup to that tenant.
    fav_amb_ids: set[int] = set()
    if job.favorites_only:
        fav_amb_ids = set(
            jm.TenantFavoriteAmbassador.objects.filter(
                tenant_id=job.tenant_id, ambassador_id__in=amb_ids
            ).values_list("ambassador_id", flat=True)
        )

    # BAs who already applied to THIS job — skip them.
    applied_amb_ids = set(
        jm.JobApplication.objects.filter(
            job_id=job.id, ambassador_id__in=amb_ids
        ).values_list("ambassador_id", flat=True)
    )

    sent = 0
    seen: set[int] = set()  # dedupe: never push the same BA twice in this call
    for amb in ambs:
        if amb.id in seen:
            continue

        if amb.id in applied_amb_ids:
            continue

        pref = prefs_by_amb.get(amb.id)
        if pref is not None and not pref.notify_new_gigs:
            continue

        # favorites_only gate — mirror the board / digest exactly.
        if job.favorites_only and amb.id not in fav_amb_ids:
            continue

        # ---- Distance with state fallback ----
        distance = None
        if job_coords:
            distance = _haversine_miles(job_coords, amb.coordinates)

        if distance is not None:
            # Both coordinates present -> strict proximity gate.
            if distance > NEARBY_RADIUS_MILES:
                continue
            title = "New gig near you"
            body = (
                f"A gig just posted ~{int(round(distance))} mi away "
                f"— {venue}. Tap to view."
            )
            data = {
                "screen": "jobs",
                "kind": "new_gig_nearby",
                "jobUuid": str(job.uuid),
                "distanceMiles": int(round(distance)),
            }
        else:
            # Coords missing on either side -> preferred-state fallback so
            # the push still reaches plausibly-relevant BAs.
            states = set(
                (pref.preferred_state_codes or []) if pref is not None else []
            )
            states = {s.strip().upper() for s in states if s and s.strip()}
            if states:
                if not job_state_code or job_state_code not in states:
                    continue
            # No preferred states set (empty = all states) -> include.
            where = f" in {job_state_code}" if job_state_code else ""
            title = "New gig near you"
            body = (
                f"A new gig just posted{where} — {venue}. Tap to view and apply."
            )
            data = {
                "screen": "jobs",
                "kind": "new_gig_nearby",
                "jobUuid": str(job.uuid),
            }

        seen.add(amb.id)
        try:
            enqueue_push(amb.user_id, title=title, body=body, data=data)
            sent += 1
        except Exception:
            logger.exception(
                "new-gig-nearby push failed amb=%s user=%s job=%s",
                amb.id, amb.user_id, job.id,
            )

    logger.info(
        "new-gig-nearby: enqueued %s push(es) for job=%s (favorites_only=%s, "
        "has_event_coords=%s)",
        sent, job.id, job.favorites_only, bool(job_coords),
    )
    return sent
