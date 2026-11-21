"""Read-only recap preview for the public check-in link.

A BA on one bar of signal clocks in late and then finds out the recap wants a
shelf photo they never took and a "cans sampled" count they never kept. This
module answers "what will the recap ask me?" BEFORE any of that, from the link
alone, without identifying the BA, clocking anything in, or creating an event
or booking.

It exposes FORM STRUCTURE ONLY: section names, question labels, field types,
choice options, required flags, photo bucket names + suggested counts, and the
product names the real form already shows a BA. Never the event, the store,
other BAs, notes, contacts, field/category ids beyond what wiring the form's
own "only asked when" rules needs, or anything brand-private.

The template is resolved exactly the way a real walk-in check-in resolves it
(``find_or_create_walkin_event`` stamps the BA's program, else the tenant's
pinned ``checkin_event_type``; ``resolve_template_for_event`` then matches the
tenant's templates on that event type before the sole-template fallback), so
the preview shows the form the BA will actually get.
"""

from __future__ import annotations

from types import SimpleNamespace

from ambassadors import checkin_web

# The page always refuses a recap with no photo at all (RecapNeedsAPhoto), so
# even a brand with no labelled buckets needs one.
MIN_PHOTOS_PER_RECAP = 1


class PreviewUnavailable(Exception):
    """This link has no recap to preview (``code`` says why)."""

    def __init__(self, code: str, message: str, status: int = 404):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def template_for_program(tenant, event_type):
    """The recap template a NEW walk-in event of ``event_type`` would get.

    Mirrors ``checkin_web.resolve_template_for_event`` for an event that has no
    pinned template FK and no recap filed yet (which is every walk-in the
    standing link creates): tenant + event-type match, else the tenant's sole
    (non-activation) template.
    """
    from recaps.models import CustomRecapTemplate
    from recaps.template_fallback import fallback_template

    if tenant is None:
        return None
    qs = CustomRecapTemplate.objects.filter(tenant_id=tenant.id)
    if event_type is not None:
        match = qs.filter(event_type_id=event_type.id).order_by("id").first()
        if match is not None:
            return match
    return fallback_template(qs)


def _public_program(et, options: list[dict]) -> dict | None:
    if et is None:
        return None
    for opt in options:
        if opt.get("id") == str(et.id):
            return {
                "id": opt["id"],
                "name": opt.get("name") or "",
                "label": opt.get("label") or "",
            }
    return {"id": str(et.id), "name": et.name or "", "label": ""}


def _public_fields(fields: list[dict]) -> list[dict]:
    out = []
    for f in fields:
        row = {
            "id": f.get("id"),
            "name": f.get("name") or "",
            "type": f.get("type") or "text",
            "required": bool(f.get("required")),
            "options": [str(o) for o in (f.get("options") or [])],
            "placeholder": f.get("placeholder") or "",
        }
        show = f.get("showWhen")
        if isinstance(show, dict):
            row["showWhen"] = {
                "fieldId": show.get("fieldId"),
                "includes": show.get("includes"),
            }
        out.append(row)
    return out


def _public_template(tpl, *, products_source, hide_sections=frozenset()) -> dict | None:
    if tpl is None:
        return None
    sections, include_products = checkin_web._template_sections(tpl)
    from recaps.sample_qty import sample_qty_labels

    qty_label, _ = sample_qty_labels(tpl)
    products = []
    if include_products:
        for p in checkin_web._event_products(products_source):
            products.append({"name": p["name"], "category": p.get("category")})
    return {
        "name": tpl.name or "",
        "productSamples": bool(tpl.product_samples),
        "sampleQtyLabel": qty_label or "",
        "sections": [
            {"name": s["name"], "fields": _public_fields(s["fields"])}
            for s in sections
            if (s.get("name") or "") not in hide_sections
        ],
        "products": products,
    }


def _public_buckets(tenant, event_type) -> list[dict]:
    buckets = checkin_web.serialize_photo_buckets(
        SimpleNamespace(tenant=tenant, event_type=event_type)
    )
    return [
        {"name": b["name"], "helper": b.get("helper") or "", "min": int(b.get("min") or 0)}
        for b in buckets
    ]


def build_recap_preview(code: str, program_id=None) -> dict:
    """Payload for ``GET /api/public/checkin/<code>/preview``.

    Raises :class:`PreviewUnavailable` for a dead code or a link that has no
    recap (time-sheet-only brands). Reads only — never writes a row.
    """
    kind, target = checkin_web.resolve_checkin_target(code)
    if kind is None:
        raise PreviewUnavailable(
            "not_found",
            "This check-in link isn't active. Ask your lead for a current one.",
        )

    title = ""
    if kind == "event":
        event = target
        tenant = getattr(event, "tenant", None)
        if checkin_web.is_timesheet_only(tenant):
            raise PreviewUnavailable("timesheet_only", "This link has no recap to preview.")
        et = getattr(event, "event_type", None)
        tpl = checkin_web.resolve_template_for_event(event)
        programs: list[dict] = []
        program = (
            {"id": str(et.id), "name": et.name or "", "label": ""} if et else None
        )
        products_source = event
        hide = frozenset()
    else:
        tenant = target
        agency = checkin_web.is_third_party_code(code, tenant)
        recap_only = checkin_web.is_recap_only_code(code, tenant)
        if checkin_web.is_timesheet_only(tenant) and not recap_only:
            raise PreviewUnavailable("timesheet_only", "This link has no recap to preview.")
        options, title = ([], "") if agency else checkin_web.checkin_program_picker(tenant)
        multi = len(options) > 1
        chosen = (
            None if agency else checkin_web.resolve_checkin_event_type(tenant, program_id)
        )
        if chosen is not None and multi and str(chosen.id) not in {o["id"] for o in options}:
            # Tenant-scoped already; also keep it to the programs this link offers.
            chosen = None
        if chosen is None and multi:
            return {
                "brand": checkin_web._brand_payload(tenant),
                "programs": [
                    {"id": o["id"], "name": o["name"], "label": o["label"],
                     "description": o["description"]}
                    for o in options
                ],
                "programPickerTitle": title,
                "program": None,
                "needsProgram": True,
                "template": None,
                "photoBuckets": [],
                "minPhotos": MIN_PHOTOS_PER_RECAP,
            }
        et = chosen or checkin_web._default_event_type(tenant)
        tpl = template_for_program(tenant, et)
        programs = (
            [
                {"id": o["id"], "name": o["name"], "label": o["label"],
                 "description": o["description"]}
                for o in options
            ]
            if multi
            else []
        )
        program = _public_program(et, options) if multi else None
        products_source = SimpleNamespace(request=None, tenant=tenant)
        hide = checkin_web.RECAP_ONLY_HIDDEN_SECTIONS if agency else frozenset()

    template = _public_template(tpl, products_source=products_source, hide_sections=hide)
    return {
        "brand": checkin_web._brand_payload(tenant),
        "programs": programs,
        "programPickerTitle": title if programs else "",
        "program": program,
        "needsProgram": False,
        "template": template,
        "photoBuckets": _public_buckets(tenant, et),
        "minPhotos": MIN_PHOTOS_PER_RECAP,
    }
