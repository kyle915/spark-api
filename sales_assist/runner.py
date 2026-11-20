"""Orchestration for the sales-assist cron: renewals, prospects, link review.

Safety, in one place:

* **Dry run by default.** Close is written to only when the caller passes
  ``apply=True`` AND ``settings.SALES_ASSIST_ENABLED`` is true AND
  ``CLOSE_API_KEY`` is set. Anything less is a dry run: the drafts are
  written into the audit log (``SalesAssistDraft``, status ``dry_run``) for
  review and nothing leaves Spark.
* **Drafts only.** The Close client can create a note and an email with
  status ``draft``; it has no way to send, update or delete
  (``close_client._assert_safe``).
* **Idempotent.** One CREATED draft per key — ``renewal:<tenant>:<start>:<end>``
  or ``prospect:<lead>`` — enforced by a unique constraint, and re-checked
  against the lead's existing Close notes (each note carries its key as a
  marker) in case the audit row is ever lost.
* **Cost caps** per run: prospects, renewals, AI calls and estimated USD
  (settings ``SALES_ASSIST_MAX_*``). A burst of new leads larger than
  ``SALES_ASSIST_IMPORT_BURST`` is treated as a bulk import and skipped.
"""

from __future__ import annotations

import difflib
import logging
from datetime import date

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from sales_assist import comparables, prospect, renewal
from sales_assist.close_client import (
    CloseClient,
    CloseError,
    CloseNotConfigured,
    UnsafeCloseWrite,
    first_contact_email,
    parse_close_datetime,
)
from sales_assist.guards import AiBudget
from sales_assist.models import (
    CloseLink,
    SalesAssistCursor,
    SalesAssistDraft,
    SalesAssistRun,
)
from sales_assist.research import research_lead

logger = logging.getLogger(__name__)

CURSOR_PROSPECTS = "prospects.since"


def marker(key: str) -> str:
    return f"[spark-sales-assist:{key}]"


def writes_enabled(apply: bool, client=None) -> tuple[bool, str]:
    """``client`` is an already-built Close client (tests inject one)."""
    if not apply:
        return False, "dry run (apply not requested)"
    if not getattr(settings, "SALES_ASSIST_ENABLED", False):
        return False, "dry run (SALES_ASSIST_ENABLED is off)"
    if client is None and not (getattr(settings, "CLOSE_API_KEY", "") or "").strip():
        return False, "dry run (CLOSE_API_KEY is not set)"
    return True, ""


def _client(client=None):
    if client is not None:
        return client
    try:
        return CloseClient()
    except CloseNotConfigured:
        return None


def _already_created(key: str) -> bool:
    return SalesAssistDraft.objects.filter(idem_key=key, status=SalesAssistDraft.STATUS_CREATED).exists()


def _marker_in_close(client, lead_id: str, key: str) -> bool:
    try:
        notes = client.lead_notes(lead_id)
    except CloseError:
        return False
    m = marker(key)
    return any(m in (n.get("note") or "") for n in notes)


def _record(run, **fields) -> SalesAssistDraft:
    est = fields.pop("budget_delta", None)
    if est is not None:
        fields["est_cost_usd"] = round(est, 4)
    try:
        with transaction.atomic():
            return SalesAssistDraft.objects.create(run=run, **fields)
    except IntegrityError:
        fields["status"] = SalesAssistDraft.STATUS_SKIPPED
        fields["reason"] = "already drafted (concurrent run)"
        return SalesAssistDraft.objects.create(run=run, **fields)


def _write_to_close(client, lead_id: str, key: str, *, note: str, to: str, subject: str,
                    body: str, contact_id: str | None) -> tuple[str, str, str]:
    """``(note_id, email_id, problem)``; never sends."""
    note_id = email_id = problem = ""
    created = client.create_note(lead_id, note)
    note_id = str(created.get("id") or "")
    if to:
        try:
            email = client.create_draft_email(
                lead_id, to=[to], subject=subject, body_text=body, contact_id=contact_id
            )
            email_id = str(email.get("id") or "")
        except (CloseError, UnsafeCloseWrite) as exc:
            problem = f"note made; draft email failed: {exc}"
    else:
        problem = "note made; no contact email, so no draft email"
    return note_id, email_id, problem


def _start_run(action: str, applied: bool) -> SalesAssistRun:
    return SalesAssistRun.objects.create(action=action, applied=applied)


def _finish_run(run: SalesAssistRun, budget: AiBudget, summary: dict) -> dict:
    summary["ai_calls"] = budget.calls
    summary["est_cost_usd"] = round(budget.usd, 4)
    run.summary = summary
    run.ai_calls = budget.calls
    run.est_cost_usd = budget.usd_decimal
    run.finished_at = timezone.now()
    run.save()
    summary["run_id"] = run.id
    return summary


# ── renewals (existing clients) ─────────────────────────────────────────


def due_links(today: date, mode: str, tenant_id: int | None = None):
    qs = CloseLink.objects.filter(active=True).select_related("tenant").order_by("tenant_id")
    if tenant_id:
        return list(qs.filter(tenant_id=tenant_id))
    if mode == "due":
        lead_days = int(getattr(settings, "SALES_ASSIST_RENEWAL_LEAD_DAYS", 45))
        return [
            link for link in qs
            if link.renewal_date and 0 <= (link.renewal_date - today).days <= lead_days
        ]
    return list(qs)


def run_renewals(*, apply: bool, today: date | None = None, tenant_id: int | None = None,
                 start: date | None = None, end: date | None = None, mode: str = "quarterly",
                 client=None) -> dict:
    today = today or timezone.localdate()
    writes, why = writes_enabled(apply, client)
    run = _start_run("renewals", writes)
    budget = AiBudget.from_settings()
    if start is None or end is None:
        start, end = renewal.last_complete_quarter(today)
    close = _client(client) if (writes or client is not None or (getattr(settings, "CLOSE_API_KEY", "") or "").strip()) else None
    cap = int(getattr(settings, "SALES_ASSIST_MAX_RENEWALS_PER_RUN", 30))
    rows: list[dict] = []
    links = due_links(today, mode, tenant_id)
    for link in links[:cap]:
        key = f"renewal:{link.tenant_id}:{start.isoformat()}:{end.isoformat()}"
        base = dict(
            kind=SalesAssistDraft.KIND_RENEWAL, idem_key=key, tenant=link.tenant,
            close_lead_id=link.close_lead_id, lead_name=link.tenant.name,
            period_start=start, period_end=end,
        )
        if _already_created(key):
            rows.append({"tenant": link.tenant.name, "status": "skipped", "reason": "already drafted"})
            continue
        before = budget.usd
        try:
            facts = renewal.quarter_facts(link.tenant_id, start, end)
        except Exception as exc:  # noqa: BLE001
            logger.exception("sales-assist: facts failed tenant=%s", link.tenant_id)
            _record(run, status=SalesAssistDraft.STATUS_FAILED, reason=f"facts failed: {type(exc).__name__}", **base)
            rows.append({"tenant": link.tenant.name, "status": "failed", "reason": "facts failed"})
            continue
        if not facts.get("approved_recaps"):
            _record(run, status=SalesAssistDraft.STATUS_SKIPPED, reason="no approved recaps in the period", facts=facts, **base)
            rows.append({"tenant": link.tenant.name, "status": "skipped", "reason": "no approved recaps"})
            continue

        to, contact_id, first_name = link.contact_email, None, ""
        opportunity = link.close_opportunity_id
        if close is not None:
            try:
                lead = close.get_lead(link.close_lead_id)
                email, cid, fname = first_contact_email(lead)
                if not to:
                    to, contact_id = email, cid
                first_name = fname if (not link.contact_email or email == link.contact_email) else ""
            except CloseError as exc:
                if writes:
                    _record(run, status=SalesAssistDraft.STATUS_FAILED, reason=str(exc)[:280], facts=facts, **base)
                    rows.append({"tenant": link.tenant.name, "status": "failed", "reason": str(exc)})
                    continue

        paragraph, source = renewal.narrative(facts, budget)
        texts = renewal.build_texts(
            facts, paragraph, first_name=first_name, marker=marker(key), opportunity=opportunity
        )
        common = dict(
            facts={**facts, "narrative_source": source}, note_text=texts["note"], email_to=to,
            email_subject=texts["subject"], email_body=texts["body"],
            ai_calls=1 if source == "model" else 0, budget_delta=budget.usd - before, **base,
        )
        if not writes:
            _record(run, status=SalesAssistDraft.STATUS_DRY_RUN, reason=why, **common)
            rows.append({"tenant": link.tenant.name, "status": "dry_run", "narrative": source})
            continue
        if _marker_in_close(close, link.close_lead_id, key):
            _record(run, status=SalesAssistDraft.STATUS_SKIPPED, reason="already in Close (marker found)", **common)
            rows.append({"tenant": link.tenant.name, "status": "skipped", "reason": "marker in Close"})
            continue
        try:
            note_id, email_id, problem = _write_to_close(
                close, link.close_lead_id, key, note=texts["note"], to=to,
                subject=texts["subject"], body=texts["body"], contact_id=contact_id,
            )
        except (CloseError, UnsafeCloseWrite) as exc:
            _record(run, status=SalesAssistDraft.STATUS_FAILED, reason=str(exc)[:280], **common)
            rows.append({"tenant": link.tenant.name, "status": "failed", "reason": str(exc)})
            continue
        _record(run, status=SalesAssistDraft.STATUS_CREATED, reason=problem, close_note_id=note_id,
                close_email_id=email_id, **common)
        rows.append({"tenant": link.tenant.name, "status": "created", "note": note_id, "email": email_id})

    summary = {
        "action": "renewals", "mode": mode, "applied": writes, "why_dry_run": why,
        "period": [start.isoformat(), end.isoformat()], "links_considered": len(links),
        "capped": len(links) > cap, "rows": rows,
    }
    return _finish_run(run, budget, summary)


# ── prospects (new leads) ───────────────────────────────────────────────


def _tenant_names() -> list[str]:
    from tenants.models import Tenant

    return [n for n in Tenant.objects.values_list("name", flat=True) if n]


def _is_existing_client(lead_name: str, tenant_names: list[str]) -> str:
    name = (lead_name or "").casefold().strip()
    for t in tenant_names:
        if name and (name == t.casefold() or difflib.SequenceMatcher(None, name, t.casefold()).ratio() >= 0.92):
            return t
    return ""


def run_prospects(*, apply: bool, lead_id: str | None = None, since: str | None = None,
                  client=None, fetcher=None, today: date | None = None) -> dict:
    writes, why = writes_enabled(apply, client)
    close = _client(client)
    if close is None:
        return {"action": "prospects", "applied": False, "skipped": "close-not-configured",
                "detail": "Set CLOSE_API_KEY on Cloud Run to enable new-lead drafts."}
    run = _start_run("prospects", writes)
    budget = AiBudget.from_settings()
    today = today or timezone.localdate()
    summary: dict = {"action": "prospects", "applied": writes, "why_dry_run": why, "rows": []}

    if lead_id:
        leads = [close.get_lead(lead_id)]
    else:
        cursor, _ = SalesAssistCursor.objects.get_or_create(name=CURSOR_PROSPECTS)
        since_dt = parse_close_datetime(since or cursor.value)
        recent = close.recent_leads(limit=100)
        if since_dt is None:
            newest = max((lead.get("date_created") or "" for lead in recent), default="")
            if writes and newest:
                cursor.value = newest
                cursor.save()
            summary["cursor_initialized"] = newest
            summary["detail"] = "First run: cursor set to the newest lead; only leads created after it are drafted."
            return _finish_run(run, budget, summary)
        leads = [
            lead for lead in sorted(recent, key=lambda x: x.get("date_created") or "")
            if (parse_close_datetime(lead.get("date_created")) or since_dt) > since_dt
        ]
        burst = int(getattr(settings, "SALES_ASSIST_IMPORT_BURST", 40))
        if len(leads) >= burst:
            summary["skipped_bulk_import"] = len(leads)
            if writes:
                cursor.value = leads[-1].get("date_created") or cursor.value
                cursor.save()
            return _finish_run(run, budget, summary)
        status_ids = {s.strip() for s in (getattr(settings, "SALES_ASSIST_PROSPECT_STATUS_IDS", "") or "").split(",") if s.strip()}
        if status_ids:
            summary["filtered_out_by_status"] = len([lead for lead in leads if lead.get("status_id") not in status_ids])
            # The cursor still moves past filtered leads (below), so they are not re-read.
        cap = int(getattr(settings, "SALES_ASSIST_MAX_PROSPECTS_PER_RUN", 10))
        summary["new_leads"] = len(leads)
        processed = leads[:cap]
        summary["deferred_to_next_run"] = len(leads) - len(processed)
        leads = processed
        if status_ids:
            leads_to_draft = [lead for lead in leads if lead.get("status_id") in status_ids]
        else:
            leads_to_draft = leads
        if writes and processed:
            cursor.value = processed[-1].get("date_created") or cursor.value
            cursor.save()
        leads = leads_to_draft

    tenant_names = _tenant_names()
    library = None
    for lead in leads:
        lid = lead.get("id") or ""
        name = (lead.get("display_name") or lead.get("name") or "").strip()
        key = f"prospect:{lid}"
        base = dict(kind=SalesAssistDraft.KIND_PROSPECT, idem_key=key, close_lead_id=lid, lead_name=name)
        row = {"lead": name, "lead_id": lid}
        if _already_created(key):
            summary["rows"].append({**row, "status": "skipped", "reason": "already drafted"})
            continue
        client_match = _is_existing_client(name, tenant_names)
        if client_match:
            _record(run, status=SalesAssistDraft.STATUS_SKIPPED, reason=f"existing client ({client_match})", **base)
            summary["rows"].append({**row, "status": "skipped", "reason": "existing client"})
            continue
        before = budget.usd
        calls_before = budget.calls
        research = research_lead(lead, budget, fetcher=fetcher)
        if not research.get("summary"):
            _record(run, status=SalesAssistDraft.STATUS_SKIPPED, reason=research.get("error") or "no research",
                    facts={"research": research}, budget_delta=budget.usd - before,
                    ai_calls=budget.calls - calls_before, **base)
            summary["rows"].append({**row, "status": "skipped", "reason": research.get("error")})
            continue
        if not research["summary"]["is_consumer_brand"]:
            _record(run, status=SalesAssistDraft.STATUS_SKIPPED, reason="not a consumer brand (by its own site)",
                    facts={"research": research}, budget_delta=budget.usd - before,
                    ai_calls=budget.calls - calls_before, **base)
            summary["rows"].append({**row, "status": "skipped", "reason": "not a consumer brand"})
            continue
        if library is None:
            library = comparables.build_library(today, budget=budget, allow_ai=True)
        comps = comparables.pick_comparables(
            library,
            category=research["summary"]["category"],
            retailers=research["summary"]["retailers"],
            exclude_names=[name],
        )
        email, contact_id, first_name = first_contact_email(lead)
        facts = prospect.build_facts(research, comps, first_name=first_name)
        draft, problem = prospect.compose(
            facts, budget, forbidden_names=[t for t in tenant_names if t.casefold() != name.casefold()],
            forbidden_numbers=comps["forbidden_numbers"],
        )
        audit_facts = {"research": research, "draft_facts": facts, "used_tenant_ids": comps["used_tenant_ids"]}
        if draft is None:
            _record(run, status=SalesAssistDraft.STATUS_FAILED, reason=problem, facts=audit_facts,
                    budget_delta=budget.usd - before, ai_calls=budget.calls - calls_before, **base)
            summary["rows"].append({**row, "status": "failed", "reason": problem})
            continue
        note = prospect.build_note(name, research, draft, marker=marker(key), has_email=bool(email))
        common = dict(facts=audit_facts, note_text=note, email_to=email, email_subject=draft["subject"],
                      email_body=draft["email_body"], budget_delta=budget.usd - before,
                      ai_calls=budget.calls - calls_before, **base)
        if not writes:
            _record(run, status=SalesAssistDraft.STATUS_DRY_RUN, reason=why, **common)
            summary["rows"].append({**row, "status": "dry_run"})
            continue
        if _marker_in_close(close, lid, key):
            _record(run, status=SalesAssistDraft.STATUS_SKIPPED, reason="already in Close (marker found)", **common)
            summary["rows"].append({**row, "status": "skipped", "reason": "marker in Close"})
            continue
        try:
            note_id, email_id, problem = _write_to_close(
                close, lid, key, note=note, to=email, subject=draft["subject"],
                body=draft["email_body"], contact_id=contact_id,
            )
        except (CloseError, UnsafeCloseWrite) as exc:
            _record(run, status=SalesAssistDraft.STATUS_FAILED, reason=str(exc)[:280], **common)
            summary["rows"].append({**row, "status": "failed", "reason": str(exc)})
            continue
        _record(run, status=SalesAssistDraft.STATUS_CREATED, reason=problem, close_note_id=note_id,
                close_email_id=email_id, **common)
        summary["rows"].append({**row, "status": "created", "note": note_id, "email": email_id})
    return _finish_run(run, budget, summary)


# ── tenant <-> Close link review ────────────────────────────────────────


def suggest_links(client=None) -> dict:
    """Fuzzy-name suggestions for unlinked tenants. READ-ONLY: never links."""
    from tenants.models import Tenant

    close = _client(client)
    if close is None:
        return {"ok": True, "skipped": "close-not-configured", "suggestions": []}
    linked = set(CloseLink.objects.values_list("tenant_id", flat=True))
    out = []
    for tenant in Tenant.objects.exclude(id__in=linked).order_by("name"):
        try:
            hits = close.search_leads(tenant.name, limit=5)
        except CloseError as exc:
            out.append({"tenant_id": tenant.id, "tenant": tenant.name, "error": str(exc)})
            continue
        scored = []
        for h in hits:
            label = h.get("display_name") or h.get("name") or ""
            ratio = difflib.SequenceMatcher(None, tenant.name.casefold(), label.casefold()).ratio()
            scored.append({"lead_id": h.get("id"), "lead": label, "status": h.get("status_label"),
                           "score": round(ratio, 2)})
        scored.sort(key=lambda s: -s["score"])
        out.append({"tenant_id": tenant.id, "tenant": tenant.name, "candidates": scored[:3]})
    return {"ok": True, "dry_run": True, "suggestions": out,
            "detail": "Suggestions only. Link with action=link (apply=true) or the linkTenantToClose mutation after review."}


def apply_link_map(mapping: dict, *, apply: bool, user=None) -> list[dict]:
    """``{"<tenant id>": "lead_..."}`` -> links. Dry run unless ``apply``."""
    from tenants.models import Tenant

    rows = []
    for raw_tenant, raw_lead in mapping.items():
        lead_id = str(raw_lead or "").strip()
        tenant = Tenant.objects.filter(id=int(raw_tenant)).first() if str(raw_tenant).isdigit() else None
        if tenant is None:
            rows.append({"tenant": raw_tenant, "ok": False, "reason": "tenant not found"})
            continue
        if not lead_id.startswith("lead_"):
            rows.append({"tenant": tenant.name, "ok": False, "reason": "close lead id must start with lead_"})
            continue
        if apply:
            CloseLink.objects.update_or_create(
                tenant=tenant, defaults={"close_lead_id": lead_id, "active": True, "linked_by": user}
            )
        rows.append({"tenant": tenant.name, "lead_id": lead_id, "ok": True, "applied": apply})
    return rows

