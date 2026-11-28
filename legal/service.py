"""Who must accept the legal documents, whether they have, and recording it.

Scope (Kyle, 2026-11-17): every ADMIN and CLIENT (brand) user — spark-admins,
client-role users, and anyone with platform-admin access (is_staff /
is_superuser / an @igniteproductions.co email). Brand ambassadors are out of
scope here: they sign in through the mobile app or the no-login web check-in,
neither of which goes through this gate.

All functions here are synchronous (call them through ``sync_to_async`` from
async resolvers). They read the user's role from the DB by pk: the JWT
request user does not reliably hydrate ``role`` inside async resolvers.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Q

from . import versions
from .models import LegalAcceptance

GATED_ROLE_SLUGS = ("client", "spark-admin")

# Per-instance cache of "this user has accepted the current bundle". Only the
# positive answer is cached (acceptance never un-happens; a version bump
# changes the key), so a stale entry can only ever let through someone who
# really did accept.
_CACHE_TTL_SECONDS = 300


# ---------------------------------------------------------------------------
# Client IP / user agent
# ---------------------------------------------------------------------------

def _meta(request) -> dict:
    meta = getattr(request, "META", None)
    if meta is None:
        meta = getattr(getattr(request, "wsgi_request", None), "META", None)
    return meta or {}


def client_ip(request) -> str:
    """Real client IP behind Cloud Run.

    Google's front end APPENDS the address it received the connection from to
    ``X-Forwarded-For``; everything left of that was sent by the caller and is
    spoofable. So the trustworthy entry is ``ABUSE_TRUSTED_PROXY_COUNT`` from
    the RIGHT (1 = direct to Cloud Run / a domain mapping, 2 = an external
    load balancer in front). Same rule as ``utils.abuse.identity.client_ip``
    on feat/abuse-guardrails — once that lands, this should delegate to it.
    Never the first entry (``events/checkin_views._client_ip`` takes the
    first, which is the spoofable one).
    """
    meta = _meta(request)
    xff = meta.get("HTTP_X_FORWARDED_FOR", "") or ""
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    trusted = max(int(getattr(settings, "ABUSE_TRUSTED_PROXY_COUNT", 1) or 1), 1)
    if hops:
        idx = len(hops) - trusted
        return (hops[idx] if idx >= 0 else hops[0])[:64]
    return ((meta.get("REMOTE_ADDR") or "").strip() or "unknown")[:64]


def user_agent(request) -> str:
    return (_meta(request).get("HTTP_USER_AGENT") or "")[:300]


# ---------------------------------------------------------------------------
# Who is gated
# ---------------------------------------------------------------------------

def _ignite_domain() -> str:
    from utils.graphql.permissions import IGNITE_EMAIL_DOMAIN

    return IGNITE_EMAIL_DOMAIN


def is_gated(role_slug, is_staff, is_superuser, email) -> bool:
    """Admin or client user → must accept. Ambassadors (and anyone else
    without admin access) → not gated here."""
    return bool(
        (role_slug or "") in GATED_ROLE_SLUGS
        or is_staff
        or is_superuser
        or (email or "").lower().endswith(_ignite_domain())
    )


def gated_users_queryset():
    """Active people who must accept (the population for the staff report and
    the ``legal_acceptance_report`` command)."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    return (
        User.objects.filter(is_active=True)
        .filter(
            Q(role__slug__in=GATED_ROLE_SLUGS)
            | Q(is_staff=True)
            | Q(is_superuser=True)
            | Q(email__iendswith=_ignite_domain())
        )
        # Seeded service accounts (system@ / admin@spark.local) never sign in.
        .exclude(email__iendswith="@spark.local")
        .select_related("role")
    )


def _user_signals(user_id: int):
    from django.contrib.auth import get_user_model

    row = (
        get_user_model()
        .objects.filter(pk=user_id)
        .values_list("role__slug", "is_staff", "is_superuser", "email")
        .first()
    )
    return row


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@dataclass
class DocumentStatus:
    key: str
    title: str
    path: str
    version: str
    accepted_version: str | None
    accepted_at: datetime | None

    @property
    def needs_acceptance(self) -> bool:
        return self.accepted_version != self.version


@dataclass
class LegalStatusData:
    required: bool
    current_version: str
    documents: list[DocumentStatus] = field(default_factory=list)
    change_summary: str = ""

    @property
    def needs_acceptance(self) -> bool:
        return self.required and any(d.needs_acceptance for d in self.documents)

    @property
    def is_update(self) -> bool:
        """They accepted an earlier version of something — show "What changed"."""
        return self.needs_acceptance and any(
            d.accepted_version is not None for d in self.documents
        )


def _latest_acceptances(user_id: int) -> dict[str, LegalAcceptance]:
    """Most recent acceptance per document for this user."""
    latest: dict[str, LegalAcceptance] = {}
    for row in LegalAcceptance.objects.filter(user_id=user_id).order_by("-accepted_at"):
        latest.setdefault(row.document, row)
    return latest


def _current_rows(user_id: int) -> dict[str, LegalAcceptance]:
    """This user's acceptance of the CURRENT version of each document."""
    current = versions.current_versions()
    q = Q()
    for key, ver in current.items():
        q |= Q(document=key, version=ver)
    return {
        r.document: r for r in LegalAcceptance.objects.filter(user_id=user_id).filter(q)
    }


def legal_status(user_id: int) -> LegalStatusData:
    signals = _user_signals(user_id)
    required = bool(signals) and is_gated(*signals)
    docs = versions.current_documents()
    current = _current_rows(user_id)
    latest = _latest_acceptances(user_id) if len(current) < len(docs) else current
    out: list[DocumentStatus] = []
    for d in docs:
        row = current.get(d.key) or latest.get(d.key)
        out.append(
            DocumentStatus(
                key=d.key,
                title=d.title,
                path=d.path,
                version=d.version,
                accepted_version=row.version if row else None,
                accepted_at=row.accepted_at if row else None,
            )
        )
    return LegalStatusData(
        required=required,
        current_version=versions.current_version(),
        documents=out,
        change_summary=versions.change_summary(),
    )


def _bundle_key(user_id: int) -> str:
    vers = versions.current_versions()
    digest = hashlib.sha1(
        "|".join(f"{k}={vers[k]}" for k in sorted(vers)).encode()
    ).hexdigest()[:12]
    return f"legal:accepted:{user_id}:{digest}"


def needs_acceptance(user_id: int) -> bool:
    """Hot path for the GraphQL gate: one cache read, and on a miss one user
    read + one acceptance count."""
    key = _bundle_key(user_id)
    try:
        if cache.get(key):
            return False
    except Exception:  # noqa: BLE001 - cache trouble never blocks
        pass
    signals = _user_signals(user_id)
    if not signals:
        return False
    if not is_gated(*signals):
        try:
            cache.set(key, 1, _CACHE_TTL_SECONDS)
        except Exception:  # noqa: BLE001
            pass
        return False
    if len(_current_rows(user_id)) >= len(versions.DOCUMENT_KEYS):
        try:
            cache.set(key, 1, _CACHE_TTL_SECONDS)
        except Exception:  # noqa: BLE001
            pass
        return False
    return True


# ---------------------------------------------------------------------------
# Accept
# ---------------------------------------------------------------------------

class VersionMismatch(Exception):
    pass


def accept_current(user_id: int, *, version: str, ip: str, ua: str) -> LegalStatusData:
    """Record acceptance of every current document for this user.

    ``version`` must equal ``versions.current_version()`` — the web client
    echoes what it showed, so a tab left open across a version bump can't
    accept text it never displayed. Idempotent: re-accepting the same version
    keeps the original timestamp.
    """
    if (version or "").strip() != versions.current_version():
        raise VersionMismatch(versions.current_version())

    signals = _user_signals(user_id)
    role_slug, _staff, _super, email = signals or (None, False, False, "")
    for doc in versions.current_documents():
        try:
            with transaction.atomic():
                LegalAcceptance.objects.get_or_create(
                    user_id=user_id,
                    document=doc.key,
                    version=doc.version,
                    defaults={
                        "ip_address": (ip or "")[:64],
                        "user_agent": (ua or "")[:300],
                        "method": LegalAcceptance.METHOD_CLICKWRAP,
                        "email": (email or "")[:254],
                        "role": (role_slug or "")[:50],
                    },
                )
        except IntegrityError:
            # Double-click race: the other request already wrote it.
            pass
    try:
        cache.set(_bundle_key(user_id), 1, _CACHE_TTL_SECONDS)
    except Exception:  # noqa: BLE001
        pass
    return legal_status(user_id)


# ---------------------------------------------------------------------------
# Staff report
# ---------------------------------------------------------------------------

@dataclass
class ReportRow:
    user_id: int
    user_uuid: str
    email: str
    name: str
    role: str
    status: str  # "accepted" | "outdated" | "never"
    accepted_version: str | None
    accepted_at: datetime | None
    ip_address: str
    user_agent: str


def acceptance_report(*, pending_only: bool = False, search: str = "") -> list[ReportRow]:
    """One row per active admin/client user, with their Terms acceptance."""
    users = gated_users_queryset()
    if search:
        s = search.strip()
        users = users.filter(
            Q(email__icontains=s) | Q(first_name__icontains=s) | Q(last_name__icontains=s)
        )
    users = list(users.order_by("email"))
    ids = [u.pk for u in users]

    vers = versions.current_versions()
    q = Q()
    for key, ver in vers.items():
        q |= Q(document=key, version=ver)
    counts: dict[int, int] = {}
    for uid in LegalAcceptance.objects.filter(user_id__in=ids).filter(q).values_list(
        "user_id", flat=True
    ):
        counts[uid] = counts.get(uid, 0) + 1
    current_ok = {uid for uid, n in counts.items() if n >= len(vers)}

    latest_terms: dict[int, LegalAcceptance] = {}
    for row in LegalAcceptance.objects.filter(
        user_id__in=ids, document=versions.TERMS
    ).order_by("-accepted_at"):
        latest_terms.setdefault(row.user_id, row)

    out: list[ReportRow] = []
    for u in users:
        row = latest_terms.get(u.pk)
        if u.pk in current_ok:
            status = "accepted"
        elif row is not None:
            status = "outdated"
        else:
            status = "never"
        if pending_only and status == "accepted":
            continue
        out.append(
            ReportRow(
                user_id=u.pk,
                user_uuid=str(u.uuid),
                email=u.email or "",
                name=f"{u.first_name or ''} {u.last_name or ''}".strip(),
                role=getattr(getattr(u, "role", None), "slug", "") or "",
                status=status,
                accepted_version=row.version if row else None,
                accepted_at=row.accepted_at if row else None,
                ip_address=row.ip_address if row else "",
                user_agent=row.user_agent if row else "",
            )
        )
    return out
