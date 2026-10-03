"""Move a tenant's On-Premise requests / events onto its Retail Sampling types.

For brands that run no on-premise program (Torch). Insights reads a recap's
activation type as request type -> event type -> template name and buckets
names matching On-Premise / Bar / Venue as ``onprem``; the "By activation
type" card counts Requests the same way. createTenant seeds every tenant with
"On-Premise" + "Bar Sampling" request types and an "On-Premise Sampling"
event type, and the request pickers list whatever the tenant has, so a
requestor can file a Torch request as On-Premise.

This command, for ONE tenant (exact slug, then request_url_name, then id):

  - lists every Request / Event / CustomRecapTemplate on an on-prem-bucketed
    type, with the recaps under those events (where the classification comes
    from, BA, status);
  - repoints them to the tenant's own "Retail Sampling" RequestType /
    EventType (the one most of its rows already use);
  - drops on-prem event types from the tenant's walk-up picker;
  - RETIRES (deletes) the tenant's on-prem RequestType / EventType rows once
    nothing references them, so they leave every picker. Rows of other
    tenants (e.g. Mark Anthony Brands On-Premise) are never touched.

Never changes a recap metric, store name, approval/status, 3rd-party flag or
check-in code. Uses ``.update()`` so ``updated_at`` is not bumped.

DRY-RUN unless ``--apply``. Logs before/after per row.

    python manage.py reclassify_onpremise_to_retail --tenant torch-thc
    python manage.py reclassify_onpremise_to_retail --tenant torch-thc --apply
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count

DEFAULT_TENANT = "torch-thc"
RETAIL_NAME = "Retail Sampling"


def _is_onprem(name: str | None) -> bool:
    from recaps.tenant_overview import _activation_bucket_for_type_name

    return _activation_bucket_for_type_name(name)[0] == "onprem"


class Command(BaseCommand):
    help = "Reclassify a tenant's On-Premise requests/events to Retail Sampling (dry-run default)."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default=DEFAULT_TENANT)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument(
            "--keep-types",
            action="store_true",
            help="Repoint rows but leave the on-prem type rows in place.",
        )

    @staticmethod
    def _resolve_tenant(ident: str):
        from tenants.models import Tenant

        ident = (ident or DEFAULT_TENANT).strip()
        if ident.isdigit():
            tenant = Tenant.objects.filter(id=int(ident)).first()
        else:
            tenant = (
                Tenant.objects.filter(slug=ident).order_by("id").first()
                or Tenant.objects.filter(request_url_name=ident).order_by("id").first()
            )
        if tenant is None:
            raise CommandError(f"tenant-not-found: {ident}")
        return tenant

    @staticmethod
    def _ba(amb, fallback: str = "") -> str:
        if amb is not None and amb.user is not None:
            name = f"{amb.user.first_name or ''} {amb.user.last_name or ''}".strip()
            if name:
                return name
        return fallback or ""

    def _recap_lines(self, event) -> list[str]:
        from recaps.models import CustomRecap, Recap
        from recaps.types import _consumers_sampled_from_fields, _sold_units_from_fields

        out = []
        customs = (
            CustomRecap.objects.filter(event=event)
            .select_related("ambassador__user", "custom_recap_template")
            .prefetch_related("custom_field_value__custom_field")
            .order_by("id")
        )
        for r in customs:
            pairs = [(v.custom_field.name, v.value) for v in r.custom_field_value.all()]
            status = "archived" if r.archived_at else ("approved" if r.approved else "needs_review")
            tmpl = r.custom_recap_template.name if r.custom_recap_template_id else ""
            out.append(
                f"      custom recap #{r.id} [{status}] ba={self._ba(r.ambassador, r.external_ba_name or '')!r} "
                f"template={tmpl!r} 3rd_party={r.is_third_party} "
                f"sampled={_consumers_sampled_from_fields(pairs)} units={_sold_units_from_fields(pairs)}"
            )
        for r in Recap.objects.filter(event=event).select_related("ambassador__user").order_by("id"):
            status = "approved" if r.approved else "needs_review"
            out.append(
                f"      legacy recap #{r.id} [{status}] ba={self._ba(r.ambassador, r.external_ba_name or '')!r} "
                f"engagements={r.total_engagements} sold={r.products_sold}"
            )
        return out

    @staticmethod
    def _store(req) -> str:
        name = (req.retailer_name or (req.retailer.name if req.retailer_id else "") or req.name or "").strip()
        num = (req.store_number or "").strip()
        return f"{name} #{num}" if num and f"#{num}" not in name else name

    def handle(self, *args, **opts):
        from events.models import Event, EventType, Request, RequestType
        from recaps.models import CustomRecapTemplate
        from tenants.models import Tenant

        apply = bool(opts.get("apply"))
        retire = not opts.get("keep_types")
        tenant = self._resolve_tenant(opts.get("tenant"))
        w = self.stdout.write
        w(f"{'APPLY' if apply else 'DRY RUN'} — tenant [{tenant.id}] {tenant.name!r} slug={tenant.slug!r}")

        rts = list(RequestType.objects.filter(tenant=tenant).order_by("id"))
        ets = list(EventType.objects.filter(tenant=tenant).order_by("id"))
        onprem_rts = [t for t in rts if _is_onprem(t.name)]
        onprem_ets = [t for t in ets if _is_onprem(t.name)]
        w(f"request types: {', '.join(f'[{t.id}] {t.name}' for t in rts) or '(none)'}")
        w(f"event types:   {', '.join(f'[{t.id}] {t.name}' for t in ets) or '(none)'}")
        w(f"on-prem request types: {[t.name for t in onprem_rts]}  on-prem event types: {[t.name for t in onprem_ets]}")

        rt_target = (
            RequestType.objects.filter(tenant=tenant, name__iexact=RETAIL_NAME)
            .annotate(n=Count("requests"))
            .order_by("-n", "id")
            .first()
        )
        et_target = (
            EventType.objects.filter(tenant=tenant, name__iexact=RETAIL_NAME)
            .annotate(n=Count("events"))
            .order_by("-n", "id")
            .first()
        )
        if rt_target is None or et_target is None:
            raise CommandError(f"tenant has no {RETAIL_NAME!r} RequestType and EventType to move onto")
        w(f"target RequestType [{rt_target.id}] {rt_target.name!r} ({rt_target.n} requests)")
        w(f"target EventType   [{et_target.id}] {et_target.name!r} ({et_target.n} events)")

        reqs = list(
            Request.objects.filter(tenant=tenant, request_type__in=onprem_rts)
            .select_related("request_type", "status", "retailer")
            .order_by("date", "id")
        )
        w(f"\nRequests on an on-prem request type: {len(reqs)}")
        for req in reqs:
            when = req.date or req.start_time
            state = "DELETED " if req.deleted_at else ""
            w(
                f"  {state}request #{req.id} R-{str(req.uuid)[-4:].upper()} {when.date() if when else '-'} "
                f"store={self._store(req)!r} status={req.status.name if req.status_id else '-'!r} "
                f"requestor={req.requestor_email or ''!r}  request_type: {req.request_type.name!r} -> {rt_target.name!r}"
            )
            for ev in Event.objects.filter(request=req).select_related("event_type").order_by("id"):
                et_name = ev.event_type.name if ev.event_type_id else None
                move = " -> " + repr(et_target.name) if et_name and _is_onprem(et_name) else " (unchanged)"
                w(f"    event #{ev.id} {ev.name!r} event_type: {et_name!r}{move}")
                for line in self._recap_lines(ev):
                    w(line)

        evs = list(
            Event.objects.filter(tenant=tenant, event_type__in=onprem_ets)
            .select_related("event_type", "request__request_type")
            .order_by("date", "id")
        )
        w(f"\nEvents on an on-prem event type: {len(evs)}")
        for ev in evs:
            when = ev.date or ev.start_time
            rt_name = ev.request.request_type.name if ev.request_id and ev.request.request_type_id else None
            w(
                f"  event #{ev.id} {when.date() if when else '-'} {ev.name!r} request_type={rt_name!r}  "
                f"event_type: {ev.event_type.name!r} -> {et_target.name!r}"
            )
            for line in self._recap_lines(ev):
                w(line)

        tmpls = list(
            CustomRecapTemplate.objects.filter(tenant=tenant)
            .select_related("event_type")
            .order_by("id")
        )
        tmpl_move = [t for t in tmpls if t.event_type_id and t.event_type in onprem_ets]
        tmpl_named = [t for t in tmpls if _is_onprem(t.name)]
        w(f"\nRecap templates on an on-prem event type: {len(tmpl_move)}")
        for t in tmpl_move:
            w(f"  template #{t.id} {t.name!r} event_type: {t.event_type.name!r} -> {et_target.name!r}")
        w(f"Recap templates NAMED on-prem (report only): {len(tmpl_named)}")
        for t in tmpl_named:
            w(f"  template #{t.id} {t.name!r}")

        default_on = tenant.checkin_event_type_id in {t.id for t in onprem_ets}
        picker_on = list(tenant.checkin_event_types.filter(id__in=[t.id for t in onprem_ets]))
        w(
            f"\nwalk-up default event type on-prem: {default_on}"
            f"{f' -> {et_target.name!r}' if default_on else ''}; "
            f"walk-up picker on-prem entries to drop: {[t.name for t in picker_on]}"
        )

        def _refs_after(model, t) -> int:
            if model is RequestType:
                return Request.objects.filter(request_type=t).count()
            return (
                Event.objects.filter(event_type=t).count()
                + CustomRecapTemplate.objects.filter(event_type=t).count()
                + Tenant.objects.filter(checkin_event_type=t).exclude(id=tenant.id).count()
                + Tenant.objects.filter(checkin_event_types=t).exclude(id=tenant.id).count()
            )

        if not apply:
            w(
                f"\nDRY RUN — would move {len(reqs)} request(s), {len(evs)} event(s), "
                f"{len(tmpl_move)} template(s) to {RETAIL_NAME!r}"
                + (
                    f" and retire {[t.name for t in onprem_rts]} request type(s), "
                    f"{[t.name for t in onprem_ets]} event type(s) for this tenant."
                    if retire
                    else "; on-prem types kept (--keep-types)."
                )
            )
            return

        with transaction.atomic():
            n_req = Request.objects.filter(tenant=tenant, request_type__in=onprem_rts).update(request_type=rt_target)
            n_ev = Event.objects.filter(tenant=tenant, event_type__in=onprem_ets).update(event_type=et_target)
            n_tmpl = CustomRecapTemplate.objects.filter(tenant=tenant, event_type__in=onprem_ets).update(
                event_type=et_target
            )
            if default_on:
                Tenant.objects.filter(id=tenant.id).update(checkin_event_type=et_target)
            if picker_on:
                tenant.checkin_event_types.remove(*picker_on)
            retired, kept = [], []
            if retire:
                for model, t in [(RequestType, t) for t in onprem_rts] + [(EventType, t) for t in onprem_ets]:
                    refs = _refs_after(model, t)
                    if refs:
                        kept.append(f"{t.name} ({refs} refs)")
                        continue
                    t.delete()
                    retired.append(f"{model.__name__} [{t.id}] {t.name}")
        w(f"\nAPPLIED — moved {n_req} request(s), {n_ev} event(s), {n_tmpl} template(s) to {RETAIL_NAME!r}.")
        if retire:
            w(f"retired for this tenant: {retired or 'none'}")
            if kept:
                w(f"kept (still referenced elsewhere): {kept}")
