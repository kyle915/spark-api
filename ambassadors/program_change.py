"""A BA fixes the program they picked on the walk-up link.

On a multi-program standing link the BA picks a program (event type) at
identify, and that pick decides the walk-in event's type, its title suffix and
its recap form. Picking the wrong one used to leave the BA stuck on the wrong
form until staff changed the event by hand (Torch, Nov 2026: "G7 Guerilla
Sampling" picked for a Retail Sampling shift).

``change_checkin_program`` switches it IN PLACE, but only for an event this
BA's own check-in opened that nobody else is on: the clock-ins, booking, GPS
pings and mileage all stay on the same event row. A shared or scheduled event
is refused with a plain "ask your lead" message, and nothing changes after the
BA's recap is filed (staff can still edit it).

Unsent answers move with the BA: fields that exist on both forms (same name,
same type) keep their answers, photos stay in their bucket when the new
program has it and otherwise move to the generic pile, and the rest of the old
draft is dropped. Every switch writes a ``CheckinProgramChange`` row.
"""

from __future__ import annotations

import logging
import re

from django.db import connection, transaction

logger = logging.getLogger(__name__)

ASK_LEAD_MESSAGE = (
    "This shift is shared with your team or was set up by your lead, so the "
    "program can't be changed from here. Message your lead and they'll switch it."
)
RECAP_FILED_MESSAGE = (
    "Your recap is already in, so the program can't be changed here. "
    "Message your lead if it's wrong."
)
NO_PROGRAMS_MESSAGE = "This link has only one program, so there's nothing to change."
TIMESHEET_MESSAGE = "This link is a time clock only, so there's no program to change."
SAME_PROGRAM_MESSAGE = "You're already on that program."
UNKNOWN_PROGRAM_MESSAGE = "Pick one of the programs on this link."
STORE_NAME_NEEDED_MESSAGE = "Enter the store name so your work counts for the right store."


class ProgramChangeRefused(ValueError):
    """The switch is not allowed. ``code`` is the JSON error code."""

    def __init__(self, code: str, message: str, status: int = 409):
        self.code = code
        self.status = status
        super().__init__(message)


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


# --------------------------------------------------------------------------
# Who may switch
# --------------------------------------------------------------------------
def program_change_refusal(*, event, ambassador) -> tuple[str, str] | None:
    """``(code, message)`` when this BA may NOT switch this event's program,
    else ``None``. Read-only; the switch re-checks under a row lock."""
    from ambassadors import checkin_web
    from ambassadors.models import AmbassadorEvent
    from events.models import Event
    from recaps.models import CustomRecap, Recap
    from tenants.models import Tenant

    tenant = getattr(event, "tenant", None)
    if tenant is None:
        return "no_programs", NO_PROGRAMS_MESSAGE
    if checkin_web.is_timesheet_only(tenant):
        return "timesheet_only", TIMESHEET_MESSAGE
    if len(checkin_web.selectable_event_types(tenant)) < 2:
        return "no_programs", NO_PROGRAMS_MESSAGE
    if checkin_web.has_recap(ambassador_id=ambassador.id, event_id=event.id):
        return "recap_filed", RECAP_FILED_MESSAGE

    # Scheduled (a request / plan gig / job board booking), a co-activation,
    # a form staff pinned, or an event someone else opened: staff own these.
    if getattr(event, "request_id", None):
        return "shared_event", ASK_LEAD_MESSAGE
    if getattr(event, "co_activation_host_id", None) or Event.objects.filter(
        co_activation_host_id=event.id
    ).exists():
        return "shared_event", ASK_LEAD_MESSAGE
    if getattr(event, "custom_recap_template_id", None):
        return "shared_event", ASK_LEAD_MESSAGE
    user_id = getattr(ambassador, "user_id", None)
    if not user_id or getattr(event, "created_by_id", None) != user_id:
        return "shared_event", ASK_LEAD_MESSAGE
    # Roaming market events are one per market per day for the whole crew,
    # and Product Seeding events are keyed on a stand-in address: switching
    # either would leave the event keyed on something the new program never
    # uses.
    if checkin_web.tenant_location_mode(tenant) == Tenant.CHECKIN_LOCATION_MARKET:
        return "shared_event", ASK_LEAD_MESSAGE
    if (event.address or "").startswith("Product Seeding · BA "):
        return "shared_event", ASK_LEAD_MESSAGE

    # Anyone else on it — booked, or with a recap row of any kind.
    if (
        AmbassadorEvent.objects.filter(event_id=event.id)
        .exclude(ambassador_id=ambassador.id)
        .exists()
    ):
        return "shared_event", ASK_LEAD_MESSAGE
    if (
        CustomRecap.objects.filter(event_id=event.id)
        .exclude(ambassador_id=ambassador.id)
        .exists()
        or Recap.objects.filter(event_id=event.id)
        .exclude(ambassador_id=ambassador.id)
        .exists()
    ):
        return "shared_event", ASK_LEAD_MESSAGE
    return None


def program_change_state(*, event, ambassador) -> dict:
    """``{allowed, code, message}`` for the check-in context, so the page can
    say "ask your lead" up front instead of after the BA picks."""
    refusal = program_change_refusal(event=event, ambassador=ambassador)
    if refusal is None:
        return {"allowed": True, "code": "", "message": ""}
    code, message = refusal
    return {"allowed": False, "code": code, "message": message}


# --------------------------------------------------------------------------
# Draft carry-over
# --------------------------------------------------------------------------
def template_fields(tpl) -> list[dict]:
    """``[{id, name, type, options}]`` for a template, in form order."""
    if tpl is None:
        return []
    from ambassadors.checkin_web import _template_sections

    sections, _ = _template_sections(tpl)
    return [f for s in sections for f in s["fields"]]


def _field_key(field: dict) -> tuple[str, str]:
    return _norm(field.get("name")), (field.get("type") or "").lower()


def _choice_type(kind: str) -> bool:
    return any(t in kind for t in ("select", "choice", "radio", "dropdown"))


def _fits_options(value, options: list) -> bool:
    """A choice answer fits the new field only when every picked option exists."""
    if not options:
        return True
    allowed = {_norm(o) for o in options}
    picked = value if isinstance(value, list) else [value]
    return all(_norm(str(v)) in allowed for v in picked if str(v).strip())


def build_field_map(old_fields: list[dict], new_fields: list[dict]) -> dict[str, str]:
    """old field id → new field id, for fields with the same name and type.

    Names are compared case- and punctuation-insensitively. A name that
    appears twice on either form is ambiguous and is not carried."""
    def index(fields):
        seen: dict[tuple, list] = {}
        for f in fields:
            seen.setdefault(_field_key(f), []).append(f)
        return seen

    old_ix, new_ix = index(old_fields), index(new_fields)
    out: dict[str, str] = {}
    for key, olds in old_ix.items():
        news = new_ix.get(key) or []
        if not key[0] or len(olds) != 1 or len(news) != 1:
            continue
        out[str(olds[0]["id"])] = str(news[0]["id"])
    return out


def carry_over_draft(
    draft: dict | None,
    *,
    old_fields: list[dict],
    new_fields: list[dict],
    new_bucket_ids: set[str],
    new_product_ids: set[str],
    same_template: bool,
) -> tuple[dict, dict]:
    """``(new_draft, summary)`` — the walk-up draft re-keyed for the new form.

    ``summary`` = ``{carried, dropped (field names), photos, photosMoved}``.
    Photos are never dropped: a bucket the new program lacks becomes the
    generic pile (``category: None``).
    """
    draft = dict(draft or {})
    values = draft.get("values") if isinstance(draft.get("values"), dict) else {}
    photos = draft.get("photos") if isinstance(draft.get("photos"), list) else None
    summary = {"carried": 0, "dropped": [], "photos": 0, "photosMoved": 0}

    if same_template:
        summary["carried"] = sum(1 for v in values.values() if _has_answer(v))
        summary["photos"] = len(photos or [])
        return draft, summary

    old_by_id = {str(f["id"]): f for f in old_fields}
    new_by_id = {str(f["id"]): f for f in new_fields}
    field_map = build_field_map(old_fields, new_fields)
    new_values: dict = {}
    for old_id, value in values.items():
        if not _has_answer(value):
            continue
        new_id = field_map.get(str(old_id))
        target = new_by_id.get(new_id) if new_id else None
        if target is not None and (
            not _choice_type(target.get("type") or "")
            or _fits_options(value, target.get("options") or [])
        ):
            new_values[new_id] = value
            summary["carried"] += 1
        else:
            name = (old_by_id.get(str(old_id)) or {}).get("name") or ""
            if name:
                summary["dropped"].append(name)
    draft["values"] = new_values

    samples = draft.get("samples") if isinstance(draft.get("samples"), dict) else {}
    draft["samples"] = {k: v for k, v in samples.items() if str(k) in new_product_ids}

    if photos is not None:
        moved = []
        for photo in photos:
            if not isinstance(photo, dict):
                continue
            cat = photo.get("category")
            if cat is not None and str(cat) not in new_bucket_ids:
                photo = {**photo, "category": None}
                summary["photosMoved"] += 1
            moved.append(photo)
        draft["photos"] = moved
        summary["photos"] = len(moved)

    # Per-brand co-activation answers are keyed on the old form's sections.
    draft.pop("coBrands", None)
    return draft, summary


def _has_answer(value) -> bool:
    if isinstance(value, list):
        return len(value) > 0
    return bool(str(value if value is not None else "").strip())


# --------------------------------------------------------------------------
# The switch
# --------------------------------------------------------------------------
def _template_for_type(event, event_type):
    """The form the event will get under ``event_type`` — the same order as
    ``resolve_template_for_event`` minus the "recap already filed" step, which
    would hand back the old form off this BA's own clock-out stub."""
    from recaps.models import CustomRecapTemplate
    from recaps.template_fallback import fallback_template

    tenant_qs = CustomRecapTemplate.objects.filter(tenant_id=event.tenant_id)
    if event_type is not None:
        match = tenant_qs.filter(event_type_id=event_type.id).order_by("id").first()
        if match:
            return match
    return fallback_template(tenant_qs)


def renamed_event(name: str, *, old_program: str, new_program: str, store_name: str = "") -> str:
    """The event title with its " · <Program>" suffix swapped.

    A title without the old suffix (renamed by staff, or opened before the
    brand ran two programs) keeps its text and gains the new suffix. A store
    name given during the switch goes in parentheses before the suffix, the
    way ``walkin_event_name`` writes it."""
    base = (name or "").strip()
    old = (old_program or "").strip()
    if old and base.endswith(f" · {old}"):
        base = base[: -len(f" · {old}")].rstrip()
    store = (store_name or "").strip()
    if store and _norm(store) not in _norm(base):
        base = f"{base} ({store})"
    label = (new_program or "").strip()
    if label and not base.endswith(f" · {label}"):
        base = f"{base} · {label}"
    return base[:255]


def change_checkin_program(
    *,
    event,
    ambassador,
    event_type_id,
    store_name: str = "",
    client_draft: dict | None = None,
) -> dict:
    """Switch this BA's walk-in event to another program, in place.

    Raises ``ProgramChangeRefused`` with a BA-facing message when it is not
    allowed (or a store name is needed first). Returns ``{change, draft,
    summary}`` where ``draft`` is the carried-over walk-up draft the page
    should restore."""
    from ambassadors import checkin_web
    from ambassadors.models import CheckinProgramChange, WalkupRecapDraft
    from events.models import Event
    from recaps.filed import custom_filed_q
    from recaps.models import CustomFieldValue, CustomRecap

    tenant = event.tenant
    new_type = checkin_web.resolve_checkin_event_type(tenant, event_type_id)
    offered_ids = {et.id for et in checkin_web.selectable_event_types(tenant)}
    if new_type is None or new_type.id not in offered_ids:
        raise ProgramChangeRefused("unknown_program", UNKNOWN_PROGRAM_MESSAGE, status=400)

    with transaction.atomic():
        locked = Event.objects.select_for_update(of=("self",)) if connection.vendor != "sqlite" else Event.objects
        event = (
            locked.select_related("tenant", "event_type", "retailer")
            .filter(pk=event.pk)
            .first()
        )
        refusal = program_change_refusal(event=event, ambassador=ambassador)
        if refusal is not None:
            raise ProgramChangeRefused(*refusal)
        old_type = event.event_type
        if old_type is not None and old_type.id == new_type.id:
            raise ProgramChangeRefused("same_program", SAME_PROGRAM_MESSAGE, status=400)

        typed_store = checkin_web._usable_store_name(store_name, event.address)
        if (
            checkin_web.store_name_required(tenant, new_type)
            and not event.retailer_id
            and not typed_store
        ):
            raise ProgramChangeRefused(
                "store_name_required", STORE_NAME_NEEDED_MESSAGE, status=400
            )

        old_tpl = checkin_web.resolve_template_for_event(event)
        old_buckets = checkin_web.serialize_photo_buckets(event)
        new_tpl = _template_for_type(event, new_type)

        old_name = event.name or ""
        new_name = renamed_event(
            old_name,
            old_program=getattr(old_type, "name", "") or "",
            new_program=new_type.name or "",
            store_name=typed_store,
        )
        actor = ambassador.user
        # update(), not save(): the address-geo mixin re-derives fields on save.
        Event.objects.filter(pk=event.pk).update(
            event_type=new_type, name=new_name, updated_by=actor
        )
        event.event_type = new_type
        event.event_type_id = new_type.id
        event.name = new_name
        if typed_store:
            try:
                checkin_web.stamp_walkin_location(event, store_name=typed_store, actor=actor)
            except Exception:  # noqa: BLE001 — never block the switch on a lookup
                logger.warning("program change: location stamp failed event=%s", event.id)

        # The clock-out stub (if any) carries the old form and would pin it
        # through resolve_template_for_event. Point it at the new form; its
        # field rows are empty by definition (a stub is unfiled).
        stub_ids = list(
            CustomRecap.objects.filter(event_id=event.id, ambassador_id=ambassador.id)
            .exclude(custom_filed_q())
            .values_list("id", flat=True)
        )
        if stub_ids and new_tpl is not None:
            CustomFieldValue.objects.filter(custom_recap_id__in=stub_ids).delete()
            CustomRecap.objects.filter(id__in=stub_ids).update(
                custom_recap_template=new_tpl, name=new_name
            )

        new_buckets = checkin_web.serialize_photo_buckets(event)
        new_fields = template_fields(new_tpl)
        new_product_ids = (
            {str(p["id"]) for p in checkin_web._event_products(event)}
            if new_tpl is not None
            else set()
        )
        same_template = (
            old_tpl is not None and new_tpl is not None and old_tpl.id == new_tpl.id
        )

        # The freshest copy of the BA's answers: the one the page sent, else
        # the server autosave.
        row = WalkupRecapDraft.objects.filter(
            event_id=event.id, ambassador_id=ambassador.id, scope="shift"
        ).first()
        server_draft = row.payload if row is not None and isinstance(row.payload, dict) else None
        source = None
        if isinstance(client_draft, dict):
            try:
                from ambassadors.walkup_draft import clean_walkup_draft

                source = clean_walkup_draft(
                    client_draft,
                    event=event,
                    previous_photos=(server_draft or {}).get("photos") or [],
                )
            except ValueError:
                source = None
        if source is None:
            source = server_draft
        carried, summary = carry_over_draft(
            source,
            old_fields=template_fields(old_tpl),
            new_fields=new_fields,
            new_bucket_ids={b["id"] for b in new_buckets},
            new_product_ids=new_product_ids,
            same_template=same_template,
        )
        if source is not None:
            carried.setdefault("v", 1)
            WalkupRecapDraft.objects.update_or_create(
                event_id=event.id,
                ambassador_id=ambassador.id,
                scope="shift",
                defaults={"payload": carried},
            )

        change = CheckinProgramChange.objects.create(
            tenant_id=event.tenant_id,
            event_id=event.id,
            ambassador_id=ambassador.id,
            from_event_type=old_type,
            to_event_type=new_type,
            from_name=(getattr(old_type, "name", "") or "")[:255],
            to_name=(new_type.name or "")[:255],
            from_template_id=getattr(old_tpl, "id", None),
            to_template_id=getattr(new_tpl, "id", None),
            old_event_name=old_name[:255],
            new_event_name=new_name[:255],
            store_name=typed_store[:255],
            detail={
                **summary,
                "oldBuckets": [b["id"] for b in old_buckets],
                "newBuckets": [b["id"] for b in new_buckets],
            },
        )

    logger.info(
        "checkin program change event=%s ambassador=%s %r -> %r carried=%s dropped=%s",
        event.id,
        ambassador.id,
        change.from_name,
        change.to_name,
        summary["carried"],
        len(summary["dropped"]),
    )
    return {
        "change": change,
        "event": event,
        "draft": carried if source is not None else None,
        "summary": summary,
    }


def latest_program_change(*, event_id: int, ambassador_id: int):
    from ambassadors.models import CheckinProgramChange

    return (
        CheckinProgramChange.objects.filter(event_id=event_id, ambassador_id=ambassador_id)
        .order_by("-created_at", "-id")
        .first()
    )
