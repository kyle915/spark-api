"""Read-only misuse review: did anyone use the access holes before they closed?

#1424, #1426 and #1427 closed GraphQL operations that anonymous or
under-privileged callers could reach. This module reads what the database can
still say about the window before each fix. It never writes.

Sections (each has its own default end, the merge time of the PR that closed
the hole, overridable with ``end``):

- ``users_created``: accounts created in the window. Elevated accounts
  (staff / superuser / spark-admin role / Ignite domain) are listed with
  email so they can be recognised. ``inviteUser`` never set ``created_by``,
  so a null creator is normal and is NOT by itself a red flag.
- ``elevated_accounts``: every elevated account that exists today, whenever
  it was created. An escalation that happened before the window still shows
  up here.
- ``deactivations``: users, tenant links and ambassador profiles that are
  inactive now and whose row was last saved in the window. ``deleteUser``
  recorded no actor; ``disableAmbassador`` did (``updated_by``).
- ``password_changes``: NOT recorded. Proxies only (see section note).
- ``memberships``: tenant links created in the window, ambassadors who
  gained a second tenant, and ambassador invitations sent by non-admins.
- ``tenant_moves``: there is no history table (no simple-history, no
  auditlog). Derived signals only: activity-log rows whose tenant no longer
  matches their request, events whose tenant differs from their request's,
  and rows last edited by a non-admin with no tie to the row's tenant.
- ``attendance``: punches created or last edited by someone who is neither
  an admin nor the ambassador on the punch.
- ``payroll``: approvals by non-admins; every ``paid_at`` stamp in the
  window (mark-paid recorded no actor).
- ``uploads``: not derivable from the database.

Ambassadors and clients appear as ids only. Emails appear only for elevated
accounts.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone as _tz
from typing import Any, Iterable

from django.apps import apps
from django.db.models import F, Q

from utils.graphql.permissions import IGNITE_EMAIL_DOMAIN

DEFAULT_START = datetime(2026, 6, 1, tzinfo=_tz.utc)
# Merge times of the PRs that closed each hole (develop). Deploy lands a few
# minutes later; pass ``end`` to widen if the deploy lagged.
FIX_1424 = datetime(2026, 11, 16, 11, 20, 2, tzinfo=_tz.utc)
FIX_1426 = datetime(2026, 11, 16, 16, 31, 23, tzinfo=_tz.utc)
FIX_1427 = datetime(2026, 11, 16, 16, 59, 41, tzinfo=_tz.utc)

DEFAULT_ROW_CAP = 500


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def parse_when(raw: str | None) -> datetime | None:
    """YYYY-MM-DD or full ISO timestamp -> aware UTC datetime."""
    if not raw:
        return None
    raw = raw.strip()
    if len(raw) == 10:
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=_tz.utc)
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=_tz.utc)


class _People:
    """Who is who: elevated flags, roles, tenant ties. Loaded once."""

    def __init__(self) -> None:
        User = apps.get_model("tenants", "User")
        TenantedUser = apps.get_model("tenants", "TenantedUser")
        AmbassadorEvent = apps.get_model("ambassadors", "AmbassadorEvent")
        from utils.graphql.permissions import _is_admin_access

        self.info: dict[int, dict[str, Any]] = {}
        for uid, email, slug, is_staff, is_super, active in User.objects.values_list(
            "id", "email", "role__slug", "is_staff", "is_superuser", "is_active"
        ):
            admin = _is_admin_access(slug, is_staff, is_super, email)
            elevated_record = bool(
                is_staff
                or is_super
                or slug == "spark-admin"
                or (email or "").lower().endswith(IGNITE_EMAIL_DOMAIN)
            )
            self.info[uid] = {
                "role": slug,
                "admin": admin,
                # Show the email for any account that carries an admin
                # marker, including one removed via IGNITE_ADMIN_EXCLUDE.
                "show_email": elevated_record,
                "email": email,
                "is_staff": bool(is_staff),
                "is_superuser": bool(is_super),
                "is_active": bool(active),
            }

        # (user, tenant) pairs the user is tied to: any membership row,
        # active or not, plus any booking for an ambassador.
        self.ties: set[tuple[int, int]] = set(
            TenantedUser.objects.values_list("user_id", "tenant_id")
        )
        self.ties |= set(
            AmbassadorEvent.objects.exclude(ambassador__user_id=None)
            .exclude(tenant_id=None)
            .values_list("ambassador__user_id", "tenant_id")
        )

    def is_admin(self, uid: int | None) -> bool:
        return bool(uid and self.info.get(uid, {}).get("admin"))

    def role(self, uid: int | None) -> str | None:
        return self.info.get(uid, {}).get("role") if uid else None

    def tied(self, uid: int | None, tenant_id: int | None) -> bool:
        return bool(uid and tenant_id and (uid, tenant_id) in self.ties)

    def actor(self, uid: int | None) -> dict[str, Any] | None:
        if not uid:
            return None
        row = self.info.get(uid)
        if row is None:
            return {"user_id": uid, "role": None}
        out = {"user_id": uid, "role": row["role"], "admin": row["admin"]}
        if row["show_email"]:
            out["email"] = row["email"]
        return out


def _capped(rows: list[dict], cap: int) -> dict[str, Any]:
    return {"count": len(rows), "truncated": len(rows) > cap, "rows": rows[:cap]}


# ─── sections ──────────────────────────────────────────────────────────────


def _users_created(people: _People, start, end, cap) -> dict[str, Any]:
    User = apps.get_model("tenants", "User")
    TenantedUser = apps.get_model("tenants", "TenantedUser")
    AmbassadorInvitation = apps.get_model("ambassadors", "AmbassadorInvitation")

    users = list(
        User.objects.filter(created_at__gte=start, created_at__lt=end)
        .order_by("created_at")
        .values("id", "created_at", "created_by_id", "is_active")
    )
    ids = [u["id"] for u in users]
    tenants_by_user: dict[int, list[int]] = defaultdict(list)
    for uid, tid in TenantedUser.objects.filter(
        user_id__in=ids, is_active=True
    ).values_list("user_id", "tenant_id"):
        tenants_by_user[uid].append(tid)
    # A BA who came in through an invitation sent by an admin.
    admin_invited = {
        uid
        for uid, inviter in AmbassadorInvitation.objects.filter(
            ambassador__user_id__in=ids
        ).values_list("ambassador__user_id", "invited_by_id")
        if people.is_admin(inviter)
    }

    rows = []
    by_role: dict[str, int] = defaultdict(int)
    flags: dict[str, int] = defaultdict(int)
    for u in users:
        info = people.info.get(u["id"], {})
        role = info.get("role")
        by_role[role or "none"] += 1
        row_flags = []
        if info.get("is_staff") or info.get("is_superuser"):
            row_flags.append("django_staff_or_superuser")
        if role == "spark-admin":
            row_flags.append("spark_admin_role")
        if row_flags and not (info.get("email") or "").lower().endswith(
            IGNITE_EMAIL_DOMAIN
        ):
            row_flags.append("elevated_non_ignite_email")
        if role == "client" and not people.is_admin(u["created_by_id"]):
            row_flags.append("client_without_admin_creator")
        if u["created_by_id"] and not people.is_admin(u["created_by_id"]):
            row_flags.append("created_by_non_admin")
        for f in row_flags:
            flags[f] += 1
        row = {
            "user_id": u["id"],
            "created_at": _iso(u["created_at"]),
            "role": role,
            "is_staff": info.get("is_staff"),
            "is_superuser": info.get("is_superuser"),
            "is_active": u["is_active"],
            "created_by": people.actor(u["created_by_id"]),
            "active_tenant_ids": sorted(tenants_by_user.get(u["id"], [])),
            "admin_invitation": u["id"] in admin_invited,
            "flags": row_flags,
        }
        if info.get("show_email"):
            row["email"] = info.get("email")
        rows.append(row)

    # Flagged rows first so they survive the cap.
    rows.sort(key=lambda r: (not r["flags"], r["created_at"] or ""))
    return {
        "window": [_iso(start), _iso(end)],
        "by_role": dict(by_role),
        "flag_counts": dict(flags),
        "note": (
            "inviteUser never recorded created_by, so a null creator is normal "
            "for staff invites too. Look at spark_admin_role / staff rows with "
            "a non-Ignite email, and client rows you don't recognise."
        ),
        **_capped(rows, cap),
    }


def _elevated_accounts(people: _People) -> dict[str, Any]:
    User = apps.get_model("tenants", "User")
    rows = []
    for u in (
        User.objects.filter(
            Q(is_staff=True)
            | Q(is_superuser=True)
            | Q(role__slug="spark-admin")
            | Q(email__iendswith=IGNITE_EMAIL_DOMAIN)
        )
        .order_by("created_at")
        .values("id", "email", "created_at", "updated_at", "last_login", "is_active")
    ):
        info = people.info.get(u["id"], {})
        rows.append(
            {
                "user_id": u["id"],
                "email": u["email"],
                "role": info.get("role"),
                "is_staff": info.get("is_staff"),
                "is_superuser": info.get("is_superuser"),
                "admin_access_today": info.get("admin"),
                "is_active": u["is_active"],
                "created_at": _iso(u["created_at"]),
                "updated_at": _iso(u["updated_at"]),
                "last_login": _iso(u["last_login"]),
                "ignite_email": (u["email"] or "")
                .lower()
                .endswith(IGNITE_EMAIL_DOMAIN),
            }
        )
    return {
        "note": "Every account carrying an admin marker today. Any you don't recognise matters more than when it was made.",
        "non_ignite_email_count": sum(1 for r in rows if not r["ignite_email"]),
        "count": len(rows),
        "rows": rows,
    }


def _deactivations(people: _People, start, end_user, end_ba, cap) -> dict[str, Any]:
    User = apps.get_model("tenants", "User")
    TenantedUser = apps.get_model("tenants", "TenantedUser")
    Ambassador = apps.get_model("ambassadors", "Ambassador")

    users = []
    for u in (
        User.objects.filter(
            is_active=False, updated_at__gte=start, updated_at__lt=end_user
        )
        .order_by("updated_at")
        .values("id", "updated_at", "updated_by_id")
    ):
        info = people.info.get(u["id"], {})
        row = {
            "user_id": u["id"],
            "role": info.get("role"),
            "row_last_saved_at": _iso(u["updated_at"]),
            "updated_by": people.actor(u["updated_by_id"]),
        }
        if info.get("show_email"):
            row["email"] = info.get("email")
        users.append(row)

    links = [
        {
            "user_id": uid,
            "tenant_id": tid,
            "role": people.role(uid),
            "row_last_saved_at": _iso(ts),
            "updated_by": people.actor(by),
        }
        for uid, tid, ts, by in TenantedUser.objects.filter(
            is_active=False, updated_at__gte=start, updated_at__lt=end_user
        )
        .order_by("updated_at")
        .values_list("user_id", "tenant_id", "updated_at", "updated_by_id")
    ]

    bas = []
    suspicious_bas = 0
    for amb_id, uid, ts, by, user_active in (
        Ambassador.objects.filter(
            is_active=False, updated_at__gte=start, updated_at__lt=end_ba
        )
        .order_by("updated_at")
        .values_list("id", "user_id", "updated_at", "updated_by_id", "user__is_active")
    ):
        # disableAmbassador by a client (or another BA) leaves a
        # non-admin, non-self updated_by.
        flagged = bool(by and by != uid and not people.is_admin(by))
        suspicious_bas += flagged
        bas.append(
            {
                "ambassador_id": amb_id,
                "user_id": uid,
                "user_is_active": user_active,
                "row_last_saved_at": _iso(ts),
                "updated_by": people.actor(by),
                "flag_non_admin_not_self": flagged,
            }
        )
    bas.sort(key=lambda r: not r["flag_non_admin_not_self"])

    return {
        "window_users": [_iso(start), _iso(end_user)],
        "window_ambassadors": [_iso(start), _iso(end_ba)],
        "note": (
            "Timestamps are updated_at (last save), so a later unrelated save "
            "moves them; the real deactivation time is not stored. deleteUser "
            "recorded no actor. Ambassador profiles start inactive until "
            "approved, so inactive BAs with no actor are mostly never-approved "
            "sign-ups."
        ),
        "users": _capped(users, cap),
        "tenant_links": _capped(links, cap),
        "ambassadors": {**_capped(bas, cap), "flag_non_admin_not_self": suspicious_bas},
    }


def _password_changes(people: _People, start, end, cap) -> dict[str, Any]:
    User = apps.get_model("tenants", "User")
    PasswordResetCode = apps.get_model("tenants", "PasswordResetCode")

    elevated_saved = []
    for u in (
        User.objects.filter(updated_at__gte=start, updated_at__lt=end)
        .order_by("-updated_at")
        .values("id", "updated_at", "updated_by_id", "last_login")
    ):
        info = people.info.get(u["id"], {})
        if not info.get("show_email"):
            continue
        elevated_saved.append(
            {
                "user_id": u["id"],
                "email": info.get("email"),
                "row_last_saved_at": _iso(u["updated_at"]),
                "updated_by": people.actor(u["updated_by_id"]),
                "last_login": _iso(u["last_login"]),
            }
        )

    resets = defaultdict(int)
    for uid in PasswordResetCode.objects.filter(
        used_at__gte=start, used_at__lt=end
    ).values_list("user_id", flat=True):
        resets[uid] += 1

    logins: dict[int, dict[str, Any]] = {}
    try:
        RefreshToken = apps.get_model("gqlauth", "RefreshToken")
        for uid, created in RefreshToken.objects.filter(
            created__gte=start, created__lt=end
        ).values_list("user_id", "created"):
            if not people.info.get(uid, {}).get("show_email"):
                continue
            row = logins.setdefault(uid, {"count": 0, "first": created, "last": created})
            row["count"] += 1
            row["first"] = min(row["first"], created)
            row["last"] = max(row["last"], created)
    except LookupError:
        pass

    return {
        "window": [_iso(start), _iso(end)],
        "recorded": False,
        "note": (
            "Spark stores no password-change time or actor; changeUserPassword "
            "only re-saved the user row. Proxies below: elevated accounts "
            "whose row was saved in the window (a password change is one "
            "cause, not the only one), forgot-password codes used, and "
            "refresh tokens issued (sign-ins) for elevated accounts."
        ),
        "elevated_rows_saved": _capped(elevated_saved, cap),
        "password_reset_codes_used": {
            "count": sum(resets.values()),
            "elevated": [
                {"user_id": uid, "email": people.info[uid]["email"], "codes": n}
                for uid, n in resets.items()
                if people.info.get(uid, {}).get("show_email")
            ],
            "non_elevated_user_count": sum(
                1 for uid in resets if not people.info.get(uid, {}).get("show_email")
            ),
        },
        "elevated_sign_ins": [
            {
                "user_id": uid,
                "email": people.info[uid]["email"],
                "refresh_tokens": v["count"],
                "first": _iso(v["first"]),
                "last": _iso(v["last"]),
            }
            for uid, v in sorted(logins.items())
        ],
    }


def _memberships(people: _People, start, end, cap) -> dict[str, Any]:
    TenantedUser = apps.get_model("tenants", "TenantedUser")
    AmbassadorInvitation = apps.get_model("ambassadors", "AmbassadorInvitation")

    all_links: dict[int, list[tuple[int, Any]]] = defaultdict(list)
    for uid, tid, created in TenantedUser.objects.values_list(
        "user_id", "tenant_id", "created_at"
    ):
        all_links[uid].append((tid, created))

    added = []
    by_role: dict[str, int] = defaultdict(int)
    for link_id, uid, tid, created, by, active in (
        TenantedUser.objects.filter(created_at__gte=start, created_at__lt=end)
        .order_by("created_at")
        .values_list("id", "user_id", "tenant_id", "created_at", "created_by_id", "is_active")
    ):
        role = people.role(uid)
        by_role[role or "none"] += 1
        earlier = [t for t, c in all_links[uid] if t != tid and c < created]
        added.append(
            {
                "link_id": link_id,
                "user_id": uid,
                "tenant_id": tid,
                "role": role,
                "created_at": _iso(created),
                "is_active": active,
                "created_by": people.actor(by),
                "already_in_tenant_ids": sorted(set(earlier)),
            }
        )

    second_tenant = [
        r for r in added if r["role"] == "ambassador" and r["already_in_tenant_ids"]
    ]
    # A spark-admin invite links the user to every tenant at once; collapse
    # those so they don't drown the list.
    added.sort(key=lambda r: (r["role"] == "spark-admin", r["created_at"] or ""))

    invites = []
    for inv_id, tid, inviter, created, used, amb_id in (
        AmbassadorInvitation.objects.filter(created_at__gte=start, created_at__lt=end)
        .order_by("created_at")
        .values_list("id", "tenant_id", "invited_by_id", "created_at", "is_used", "ambassador_id")
    ):
        if people.is_admin(inviter):
            continue
        invites.append(
            {
                "invitation_id": inv_id,
                "tenant_id": tid,
                "ambassador_id": amb_id,
                "invited_by": people.actor(inviter),
                "inviter_tied_to_tenant": people.tied(inviter, tid),
                "created_at": _iso(created),
                "is_used": used,
            }
        )

    return {
        "window": [_iso(start), _iso(end)],
        "links_added_by_role": dict(by_role),
        "links_added": _capped(added, cap),
        "ambassador_joined_another_tenant": _capped(second_tenant, cap),
        "ambassador_invitations_by_non_admin": _capped(invites, cap),
    }


def _tenant_scoped_models() -> list:
    """Models with a ``tenant`` FK to Tenant plus updated_by / updated_at."""
    Tenant = apps.get_model("tenants", "Tenant")
    out = []
    for model in apps.get_models():
        names = {f.name: f for f in model._meta.get_fields() if getattr(f, "concrete", False)}
        tenant = names.get("tenant")
        if not tenant or getattr(tenant, "related_model", None) is not Tenant:
            continue
        if "updated_by" in names and "updated_at" in names:
            out.append(model)
    return sorted(out, key=lambda m: m._meta.label)


def _tenant_moves(people: _People, start, end_core, end_generic, cap) -> dict[str, Any]:
    Request = apps.get_model("events", "Request")
    Event = apps.get_model("events", "Event")
    RequestActivityLog = apps.get_model("events", "RequestActivityLog")

    # 1. Activity-log rows stamped with a tenant the request no longer has.
    log_mismatch = [
        {
            "request_id": rid,
            "log_tenant_id": log_tid,
            "request_tenant_id_now": req_tid,
            "kind": kind,
            "logged_at": _iso(ts),
            "actor": people.actor(actor),
        }
        for rid, log_tid, req_tid, kind, ts, actor in RequestActivityLog.objects.exclude(
            tenant_id=None
        )
        .filter(created_at__gte=start)
        .exclude(tenant_id=F("request__tenant_id"))
        .order_by("request_id", "created_at")
        .values_list(
            "request_id", "tenant_id", "request__tenant_id", "kind", "created_at", "actor_user_id"
        )
    ]

    # 2. Events whose tenant differs from their request's.
    event_mismatch = [
        {
            "event_id": eid,
            "event_tenant_id": etid,
            "request_id": rid,
            "request_tenant_id": rtid,
            "event_updated_at": _iso(ts),
            "event_updated_by": people.actor(by),
        }
        for eid, etid, rid, rtid, ts, by in Event.objects.exclude(request_id=None)
        .exclude(tenant_id=F("request__tenant_id"))
        .order_by("id")
        .values_list(
            "id", "tenant_id", "request_id", "request__tenant_id", "updated_at", "updated_by_id"
        )
    ]

    # 3. Rows whose last edit came from a non-admin with no tie to the row's
    #    tenant (cross-brand edit still in place), or whose non-admin creator
    #    has no tie to the tenant the row sits in now (possible re-home).
    per_model: dict[str, Any] = {}
    flagged_total = 0
    for model in _tenant_scoped_models():
        label = model._meta.label
        end = end_core if model in (Request, Event) else end_generic
        has_created_by = any(f.name == "created_by" for f in model._meta.get_fields())
        fields = ["id", "tenant_id", "updated_by_id", "updated_at"]
        if has_created_by:
            fields.append("created_by_id")
        qs = (
            model._default_manager.filter(updated_at__gte=start, updated_at__lt=end)
            .exclude(updated_by_id=None)
            .values_list(*fields)
        )
        rows = []
        for vals in qs.iterator(chunk_size=2000):
            rec = dict(zip(fields, vals))
            editor = rec["updated_by_id"]
            if people.is_admin(editor):
                continue
            reasons = []
            if not people.tied(editor, rec["tenant_id"]):
                reasons.append("editor_not_tied_to_row_tenant")
            creator = rec.get("created_by_id")
            if (
                creator
                and creator != editor
                and not people.is_admin(creator)
                and not people.tied(creator, rec["tenant_id"])
            ):
                reasons.append("creator_not_tied_to_row_tenant")
            if reasons:
                rows.append(
                    {
                        "id": rec["id"],
                        "tenant_id": rec["tenant_id"],
                        "updated_at": _iso(rec["updated_at"]),
                        "updated_by": people.actor(editor),
                        "created_by": people.actor(creator),
                        "reasons": reasons,
                    }
                )
        if rows:
            flagged_total += len(rows)
            per_model[label] = _capped(rows, cap)

    return {
        "window_requests_events": [_iso(start), _iso(end_core)],
        "window_other_models": [_iso(start), _iso(end_generic)],
        "history_table": None,
        "note": (
            "No model history is kept (no simple-history / auditlog), and "
            "updated_by holds only the LAST editor, so a move that a later "
            "admin edit overwrote is invisible. These are derived signals. "
            "'tied' = any membership row (active or not) or, for a BA, any "
            "booking in that tenant."
        ),
        "activity_log_tenant_mismatch": _capped(log_mismatch, cap),
        "event_vs_request_tenant_mismatch": _capped(event_mismatch, cap),
        "non_admin_edits_outside_own_tenants": {
            "flagged_rows": flagged_total,
            "models_scanned": [m._meta.label for m in _tenant_scoped_models()],
            "by_model": per_model,
        },
    }


def _attendance(people: _People, start, end, cap) -> dict[str, Any]:
    Attendance = apps.get_model("ambassadors", "Attendance")
    rows = []
    for (
        att_id, amb_id, amb_user, event_id, event_tenant, created, updated, cby, uby
    ) in (
        Attendance.objects.filter(
            Q(created_at__gte=start, created_at__lt=end)
            | Q(updated_at__gte=start, updated_at__lt=end)
        )
        .order_by("created_at")
        .values_list(
            "id", "ambassador_id", "ambassador__user_id", "event_id",
            "event__tenant_id", "created_at", "updated_at", "created_by_id", "updated_by_id",
        )
        .iterator(chunk_size=2000)
    ):
        reasons = []
        if cby and cby != amb_user and not people.is_admin(cby) and start <= created < end:
            reasons.append("created_by_other_non_admin")
        if uby and uby != amb_user and not people.is_admin(uby) and start <= updated < end:
            reasons.append("updated_by_other_non_admin")
        if not reasons:
            continue
        rows.append(
            {
                "attendance_id": att_id,
                "ambassador_id": amb_id,
                "event_id": event_id,
                "tenant_id": event_tenant,
                "created_at": _iso(created),
                "updated_at": _iso(updated),
                "created_by": people.actor(cby),
                "updated_by": people.actor(uby),
                "reasons": reasons,
            }
        )
    return {
        "window": [_iso(start), _iso(end)],
        "note": "updated_by is only the last editor; earlier edits are not kept.",
        **_capped(rows, cap),
    }


def _payroll(people: _People, start, end, cap) -> dict[str, Any]:
    PayrollApproval = apps.get_model("events", "PayrollApproval")
    approvals = []
    for pid, tid, amb, ps, pe, by, at, paid in (
        PayrollApproval.objects.filter(approved_at__gte=start, approved_at__lt=end)
        .order_by("approved_at")
        .values_list(
            "id", "tenant_id", "ambassador_id", "period_start", "period_end",
            "approved_by_id", "approved_at", "paid_at",
        )
    ):
        if people.is_admin(by):
            continue
        approvals.append(
            {
                "approval_id": pid,
                "tenant_id": tid,
                "ambassador_id": amb,
                "period": [str(ps), str(pe)],
                "approved_by": people.actor(by),
                "approved_at": _iso(at),
                "paid_at": _iso(paid),
            }
        )
    paid = [
        {
            "approval_id": pid,
            "tenant_id": tid,
            "ambassador_id": amb,
            "approved_by": people.actor(by),
            "paid_at": _iso(ts),
        }
        for pid, tid, amb, by, ts in PayrollApproval.objects.filter(
            paid_at__gte=start, paid_at__lt=end
        )
        .order_by("paid_at")
        .values_list("id", "tenant_id", "ambassador_id", "approved_by_id", "paid_at")
    ]
    return {
        "window": [_iso(start), _iso(end)],
        "note": (
            "markPayrollPaid recorded no actor, so every paid_at stamp in the "
            "window is listed for you to recognise. unapprovePayrollHours "
            "DELETES the row, so an un-approval leaves no trace."
        ),
        "approved_by_non_admin": _capped(approvals, cap),
        "marked_paid": _capped(paid, cap),
    }


UPLOADS_NOTE = (
    "Not derivable from the database: getUploadUrl let a caller choose the "
    "object key, and the bucket keeps no versions. Only object metadata or "
    "logs can show an overwrite. The bucket has 7-day soft delete, so "
    "`gcloud storage objects list gs://<bucket>/** --soft-deleted` shows "
    "objects overwritten or deleted in the last 7 days. Older overwrites "
    "need GCS Data Access audit logs, which are not enabled on the project."
)


def build_misuse_audit(
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    cap: int = DEFAULT_ROW_CAP,
) -> dict[str, Any]:
    start = start or DEFAULT_START
    e1424 = end or FIX_1424
    e1426 = end or FIX_1426
    e1427 = end or FIX_1427
    people = _People()

    sections = {
        "users_created": _users_created(people, start, e1424, cap),
        "elevated_accounts": _elevated_accounts(people),
        "deactivations": _deactivations(people, start, e1424, e1427, cap),
        "password_changes": _password_changes(people, start, e1426, cap),
        "memberships": _memberships(people, start, e1426, cap),
        "tenant_moves": _tenant_moves(people, start, e1424, e1426, cap),
        "attendance": _attendance(people, start, e1426, cap),
        "payroll": _payroll(people, start, e1427, cap),
        "uploads": {"derivable": False, "note": UPLOADS_NOTE},
    }
    tm = sections["tenant_moves"]
    summary = {
        "users_created": sections["users_created"]["count"],
        "users_created_flagged": sum(
            1 for r in sections["users_created"]["rows"] if r["flags"]
        ),
        "elevated_accounts_today": sections["elevated_accounts"]["count"],
        "elevated_accounts_non_ignite_email": sections["elevated_accounts"][
            "non_ignite_email_count"
        ],
        "users_deactivated": sections["deactivations"]["users"]["count"],
        "tenant_links_deactivated": sections["deactivations"]["tenant_links"]["count"],
        "ambassadors_inactive_saved": sections["deactivations"]["ambassadors"]["count"],
        "ambassadors_disabled_by_non_admin_not_self": sections["deactivations"][
            "ambassadors"
        ]["flag_non_admin_not_self"],
        "elevated_rows_saved_in_window": sections["password_changes"][
            "elevated_rows_saved"
        ]["count"],
        "tenant_links_added": sections["memberships"]["links_added"]["count"],
        "ambassador_joined_another_tenant": sections["memberships"][
            "ambassador_joined_another_tenant"
        ]["count"],
        "ambassador_invitations_by_non_admin": sections["memberships"][
            "ambassador_invitations_by_non_admin"
        ]["count"],
        "activity_log_tenant_mismatch": tm["activity_log_tenant_mismatch"]["count"],
        "event_vs_request_tenant_mismatch": tm["event_vs_request_tenant_mismatch"]["count"],
        "non_admin_edits_outside_own_tenants": tm[
            "non_admin_edits_outside_own_tenants"
        ]["flagged_rows"],
        "attendance_written_by_other_non_admin": sections["attendance"]["count"],
        "payroll_approved_by_non_admin": sections["payroll"]["approved_by_non_admin"]["count"],
        "payroll_marked_paid": sections["payroll"]["marked_paid"]["count"],
    }
    return {
        "window": {
            "start": _iso(start),
            "end_pr_1424": _iso(e1424),
            "end_pr_1426": _iso(e1426),
            "end_pr_1427": _iso(e1427),
        },
        "summary": summary,
        "unrecoverable": [
            "Who called a closed mutation, and from where: GraphQL operation "
            "names and bodies are not logged; Cloud Run request logs keep 30 days.",
            "Password changes: no timestamp or actor stored.",
            "Real deactivation times: only the row's last-save time.",
            "Tenant moves overwritten by a later edit: no history table.",
            "Who marked payroll paid; any un-approval (row deleted).",
            "Upload overwrites older than the bucket's 7-day soft delete.",
            "Reads of BA PII / GPS / pay and getDownloadUrl lookups: reads leave no row.",
        ],
        "sections": sections,
    }
