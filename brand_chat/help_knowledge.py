"""Spark Help Knowledge Base — hybrid BM25 + OpenAI semantic retrieval.

Powers route-aware Spark Help inside Ask Spark (for both brand clients and
Ignite admins), AI reply drafting inside `/chat`, and the Admin-only Knowledge
Base & Unanswered Questions (Knowledge Gaps) console.

Strict Client vs. Admin boundary:
- ``audience="admin"`` docs and articles are NEVER returned when
  ``is_admin=False``.
- Clients may NEVER create, edit, delete, or submit knowledge articles.
- Only Ignite admins (DB-backed ``is_staff``, ``is_superuser``, ``spark-admin``,
  or verified ``@igniteproductions.co``) can write knowledge or inspect gaps.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from django.core.cache import cache
from django.db.models import F, Q
from django.utils import timezone

logger = logging.getLogger(__name__)

# Hybrid scoring weights when OpenAI semantic embeddings are available.
_SEMANTIC_WEIGHT = 0.58
_BM25_WEIGHT = 0.42
_ROUTE_BOOST = 0.24
_MIN_CONFIDENT_SCORE = 0.30

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+?1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]?\d{3}[-.\s]?\d{4}")

_STOP_WORDS = set(
    """a an and are as at be but by can do does for from has have how i if in is it its
    me my of on or our so than that the their them then there these they this to
    us was we what when where which who why will with you your yours about any all
    page screen tab work works use using spark please tell know""".split()
)

# ─── Built-in Spark Help Docs (Audience-Tagged) ──────────────────────────

BUILTIN_HELP_DOCS: tuple[dict[str, Any], ...] = (
    # --- Client & Admin ("all") ---
    {
        "id": "builtin:setup-guide",
        "title": "Spark Setup & Feature Guide",
        "category": "getting_started",
        "audience": "all",
        "route_path": "/setup-guide",
        "route_label": "Open Setup Guide",
        "tags": ["setup", "onboarding", "playbook", "getting started", "checklist", "guide"],
        "body": (
            "Open Setup Guide (/setup-guide) from the Workspace sidebar or Help page for interactive, "
            "step-by-step walkthroughs covering Shelf & Voice, Buyer QBR Decks (.pptx), Saved Report "
            "Views & Friday Broker Digests, Outbound Webhooks, and Spark MCP (Claude/ChatGPT). "
            "Saved Views and Outbound Webhooks auto-check once configured in your workspace."
        ),
    },
    {
        "id": "builtin:shelf-voice",
        "title": "Shelf Compliance, Pricing & Competitor Voice Hub",
        "category": "shelf_voice",
        "audience": "all",
        "route_path": "/shelf-voice",
        "route_label": "Open Shelf & Voice",
        "tags": ["shelf", "osa", "msrp", "price", "drift", "tag", "oos", "out of stock", "facings", "competitor", "voice"],
        "body": (
            "Reports -> Shelf & Voice (/shelf-voice) tracks on-shelf availability (OSA %), price-tag "
            "compliance, average front facings, and MSRP price drift. Enter your Target MSRP $ at the "
            "top (defaults to observed median) to flag any store drifting +/-5% or more. Use quick filters "
            "(Needs Attention, OOS / Not Found, Missing Tag, Low Facings) to isolate problem doors, click "
            "any store row to open StoreSheet, inspect Before & After repeat-visit shelf proof, and watch "
            "Rising Competitor Mentions by Market (Rising, New, Steady, Cooling). Make sure retail recap "
            "templates include a Shelf, Cooler, or Display photo bucket so Spark's vision auditor reads "
            "photos automatically on approved recaps."
        ),
    },
    {
        "id": "builtin:buyer-deck",
        "title": "One-Click Retail Buyer QBR Decks & PowerPoint (.pptx) Export",
        "category": "reports",
        "audience": "all",
        "route_path": "/buyer-report?deck=1",
        "route_label": "Open Buyer Deck Builder",
        "tags": ["buyer", "qbr", "deck", "pitch", "powerpoint", "pptx", "slides", "export", "line review", "share link"],
        "body": (
            "Build co-branded Buyer QBR / Line-Review Decks from Buyer Report (/buyer-report?deck=1) or "
            "Photo Wall (/photo-wall). First star your hero field and shelf photos on /photo-wall, then "
            "filter Buyer Report to the exact Retailer Banner, Market, or Date Window for your meeting — "
            "the deck inherits your active slice. Add a buyer name, intro note, optional passcode, and "
            "toggle demo cost visibility. Click Share Link for a live web deck, or Download PowerPoint "
            "(.pptx) for an editable 16:9 deck with KPI scorecards, US activation footprint map, 4-week "
            "POS lift halo, Shelf OSA, and shopper voice quotes."
        ),
    },
    {
        "id": "builtin:saved-views",
        "title": "Saved Report Views & Automated Friday Broker Digests",
        "category": "reports",
        "audience": "all",
        "route_path": "/buyer-report",
        "route_label": "Open Buyer Report",
        "tags": ["saved view", "slice", "digest", "friday", "broker", "distributor", "email", "csv", "schedule"],
        "body": (
            "On Buyer Report (/buyer-report) or Shelf & Voice (/shelf-voice), set your date range, "
            "retailer, market, provider, or Target MSRP filters and click Save current slice. Name the "
            "view, toggle Schedule automated email digest, pick Weekly (Friday default), Bi-weekly, or "
            "Monthly, choose Summary + CSV attachment, and enter comma-separated broker or distributor "
            "emails. Click the paper-plane Send now button on any saved view pill to test immediately."
        ),
    },
    {
        "id": "builtin:webhooks",
        "title": "Outbound Webhooks (HMAC-SHA256 Signed)",
        "category": "integrations",
        "audience": "all",
        "route_path": "/ai-connector#webhooks",
        "route_label": "Configure Webhooks",
        "tags": ["webhook", "zapier", "hubspot", "klaviyo", "airtable", "slack", "hmac", "signature", "recap.approved", "shelf.oos_detected"],
        "body": (
            "In AI Connector & Webhooks (/ai-connector#webhooks), click Add Webhook Endpoint, paste an "
            "HTTPS catch URL from Zapier, HubSpot, Klaviyo, or Airtable, and select events: "
            "recap.approved, request.created, and/or shelf.oos_detected. Save the one-time whsec_ signing "
            "secret (you can rotate it anytime), verify payloads via the X-Spark-Signature HMAC-SHA256 "
            "header, and click Test to send a live webhook.test ping. Webhook payloads are strictly "
            "buyer-safe and never include BA labor pay or personal ambassador contact info."
        ),
    },
    {
        "id": "builtin:ai-connector",
        "title": "Connect Claude or ChatGPT via Spark MCP",
        "category": "integrations",
        "audience": "all",
        "route_path": "/ai-connector",
        "route_label": "Open AI Connector",
        "tags": ["mcp", "claude", "chatgpt", "ai connector", "oauth", "connect ai"],
        "body": (
            "Visit AI Connector (/ai-connector) to copy your official Spark MCP Connector URL. In Claude "
            "go to Settings -> Connectors -> Add custom connector (leave OAuth fields blank). In ChatGPT "
            "enable developer mode under Settings -> Apps & Connectors. Sign in with your Spark account "
            "and approve read-only brand access. You can inspect 30-day call counts or disconnect any "
            "AI app in one click from /ai-connector."
        ),
    },
    {
        "id": "builtin:demo-costs",
        "title": "Demo Costs, Unit Economics & Budget Pacing",
        "category": "billing_costs",
        "audience": "all",
        "route_path": "/demo-costs",
        "route_label": "Open Demo Costs",
        "tags": ["demo costs", "cost", "cost per sample", "cost per unit", "spend", "budget", "pacing", "roi", "provider agreement", "invoice"],
        "body": (
            "Use Demo Costs (/demo-costs) to track true all-in spend per activation, cost per sample, "
            "and cost per unit sold across your field providers. Set up default provider rate agreements "
            "(flat shift fee or hourly + product/travel caps) so Spark auto-computes supported visit "
            "costs and links receipt or invoice evidence. Pair with Budget Pacing (/roi-pacing) to "
            "monitor monthly spend vs. target budget and projected end-of-period spend."
        ),
    },
    {
        "id": "builtin:retailer-pos",
        "title": "Retailer POS Uploads & 4-Week Velocity Lift Matching",
        "category": "reports",
        "audience": "all",
        "route_path": "/retailer-pos",
        "route_label": "Open Retailer POS",
        "tags": ["pos", "velocity", "lift", "halo", "csv", "spins", "nielsen", "retailer sales", "matching", "unmatched"],
        "body": (
            "Upload weekly retailer POS CSVs (store name/number, week start, units sold, optional dollars) "
            "on Retailer POS (/retailer-pos). Spark matches rows to your Account Map store directory "
            "and compares 2 weeks pre-demo baseline against demo week and +1 to +3 weeks post-demo to "
            "calculate adjusted POS velocity lift. Unmatched store rows can be mapped once and remembered "
            "for future imports."
        ),
    },
    {
        "id": "builtin:recaps",
        "title": "Reviewing, Filtering & Sharing Recaps",
        "category": "recaps",
        "audience": "all",
        "route_path": "/recaps/list",
        "route_label": "Open Recaps",
        "tags": ["recap", "recaps", "approve", "approved", "quick peek", "photos", "share", "email", "pdf"],
        "body": (
            "Open Recaps (/recaps/list) to scan, review, and approve field activation recaps. Filter by "
            "status (Needs review, Approved, Shared, Archived), activation type (Retail, Event, Seeding), "
            "date window, or search by store or R-ID. Click any row to open the Quick-Peek drawer "
            "(keyboard j/k to step, a to approve) or click through to the full recap to inspect photos, "
            "metrics, shopper quotes, and share via email or PDF."
        ),
    },
    {
        "id": "builtin:requests-approvals",
        "title": "Submitting Activation Requests, Bulk Scheduling & Approvals",
        "category": "requests",
        "audience": "all",
        "route_path": "/requests/list",
        "route_label": "Open Requests",
        "tags": ["request", "schedule", "bulk upload", "plain english", "approval", "approvals", "wave planner", "account map"],
        "body": (
            "Create single or multi-store activation requests from Create Request (/requests/create) "
            "using standard forms, CSV Bulk Upload (/requests/bulk-upload), Plain-English bulk scheduling "
            "(for example 'Every Fri & Sat next month from 12pm to 4pm at our top 5 Austin stores'), or "
            "the Account Map Wave Planner (/account-map). Track status in Master Tracker / My Requests "
            "(/requests/list) and clear pending items in Approvals (/my-approvals)."
        ),
    },
    {
        "id": "builtin:workspace-agencies",
        "title": "Brand Workspaces, Agencies, Provider Imports & Finance Queue",
        "category": "getting_started",
        "audience": "all",
        "route_path": "/workspace",
        "route_label": "Open Workspace & Agencies",
        "tags": ["workspace", "agency", "agencies", "provider", "invite", "csv import", "finance queue", "team", "members"],
        "body": (
            "Manage brand teammates and external field-marketing agencies in Workspace & Agencies "
            "(/workspace). Each external agency only sees their own assigned visits and never sees "
            "other agencies' pricing or private brand POS data. External agencies can submit completed "
            "recaps via mobile submission links or Provider CSV Imports (/imports), and brand leads "
            "approve cost/invoice exceptions in Finance Queue (/finance-queue)."
        ),
    },
    {
        "id": "builtin:help-support",
        "title": "Contacting Ignite Support & Help Center",
        "category": "getting_started",
        "audience": "all",
        "route_path": "/help",
        "route_label": "Open Help & Support",
        "tags": ["support", "ticket", "contact", "bug", "email", "help"],
        "body": (
            "Open Help & Support (/help) to submit a support ticket directly to the Ignite team, browse "
            "FAQs, or reach staffing@igniteproductions.co (scheduling and recaps) and "
            "finance@igniteproductions.co (billing and invoices)."
        ),
    },
    # --- Ignite Admins ONLY ("admin") — NEVER surfaced to brand clients! ---
    {
        "id": "builtin:admin-ba-invites",
        "title": "Inviting Brand Ambassadors & Managing Shift Rosters (Admin)",
        "category": "ba_ops",
        "audience": "admin",
        "route_path": "/requests/list",
        "route_label": "Open Master Tracker",
        "tags": ["invite ba", "ambassador", "roster", "staffing", "shift", "accepted", "pending", "talent", "push"],
        "body": (
            "To invite a Brand Ambassador to a shift, open the request in Master Tracker (/requests/list) "
            "and click INVITE BA. Selecting a BA sends a 'New shift offered' push notification to their "
            "mobile Shifts tab where they can Accept or Decline. Track commitments in the ROSTER panel "
            "(Accepted vs Pending) and cancel or swap BAs directly from the panel."
        ),
    },
    {
        "id": "builtin:admin-recap-due",
        "title": "Recap Due Queue, Flag Review & Missing Recaps (Admin)",
        "category": "ba_ops",
        "audience": "admin",
        "route_path": "/recaps/missing",
        "route_label": "Open Missing Recaps",
        "tags": ["recap due", "missing recap", "flags", "qa", "review", "admin edit", "connecteam"],
        "body": (
            "The Recap Due badge counts completed shifts whose end time has passed without a submitted "
            "recap. Open Missing Recaps (/recaps/missing) to follow up with BAs or dismiss invalid shifts. "
            "Inside any recap, Ignite admins can inspect automated QA flags (geo, photo, duplicate, or "
            "metric warnings), edit numbers directly, review expense receipts, and approve or reject."
        ),
    },
    {
        "id": "builtin:admin-payroll-wingspan",
        "title": "BA Timesheets, Payroll Hours & Wingspan Payments (Admin)",
        "category": "ba_ops",
        "audience": "admin",
        "route_path": "/payroll",
        "route_label": "Open Payroll",
        "tags": ["payroll", "timesheet", "hours", "wingspan", "payments", "pay", "ba pay", "clock in", "reimbursement"],
        "body": (
            "Ignite admins manage ambassador shift hours and clock-in records under Payroll · Hours "
            "(/payroll) and disburse contractor payouts/reimbursements through Payments · Wingspan "
            "(/payments). Always verify approved recap + receipt evidence before releasing payouts. "
            "BA labor rates and Wingspan payout details are strictly internal and never exposed to clients."
        ),
    },
    {
        "id": "builtin:admin-cron-digests",
        "title": "Cloud Scheduler Cron Setup for Digests & Jobs (Admin)",
        "category": "integrations",
        "audience": "admin",
        "route_path": "/setup-guide#saved-views",
        "route_label": "View Cron Setup",
        "tags": ["cron", "cloud scheduler", "x-cron-secret", "send-saved-view-digests", "internal", "ops"],
        "body": (
            "Automated Saved View Friday digests run via a daily Cloud Scheduler HTTP POST to "
            "/internal/cron/send-saved-view-digests authenticated with the X-Cron-Secret header. "
            "One-click 'Send now' works immediately without cron setup."
        ),
    },
    {
        "id": "builtin:admin-chat-inbox",
        "title": "Spark Chat Inbox, BA DMs, Broadcasts & AI Reply Drafting (Admin)",
        "category": "ba_ops",
        "audience": "admin",
        "route_path": "/chat",
        "route_label": "Open Chat Inbox",
        "tags": ["chat", "dm", "broadcast", "ambassador message", "draft reply", "teach spark", "inbox"],
        "body": (
            "Use Chat (/chat) for two-way messaging with Brand Ambassadors across general DMs and per-job "
            "threads (every reply sends a mobile push). Click 'Draft AI reply' in any thread composer to "
            "draft a response grounded in shift details, brand product knowledge, and Spark SOPs — always "
            "review before sending. Click 'Teach Spark' on any strong admin reply to distill a PII-scrubbed "
            "entry into the Spark Help Knowledge Base (/help-knowledge)."
        ),
    },
    {
        "id": "builtin:admin-sandbox",
        "title": "Admin-Only Sample Brand Sandbox Preview (Admin)",
        "category": "getting_started",
        "audience": "admin",
        "route_path": "/sandbox",
        "route_label": "Open Admin Sandbox",
        "tags": ["sandbox", "meadowlark", "demo workspace", "preview", "sales demo"],
        "body": (
            "Ignite admins can preview the upcoming Sample Brand Sandbox at /sandbox (also linked from "
            "Settings and Setup Guide). It uses 100% fictional brand (Meadowlark Botanicals), retailer, "
            "store, and competitor data and is locked to Ignite admins until public launch."
        ),
    },
)


# ─── Authoritative Admin Check ───────────────────────────────────────────


def is_ignite_admin_user(user) -> bool:
    """Authoritative DB-backed Ignite admin check matching GraphQL permissions."""
    from django.contrib.auth import get_user_model
    from utils.graphql.permissions import (
        UntrustedIgniteEmail,
        _demote_if_excluded,
        _is_admin_access,
        ignite_domain_account_trusted,
        is_ignite_domain_email,
    )

    if not user or not getattr(user, "is_authenticated", False):
        return False

    db_user = None
    pk = getattr(user, "pk", None)
    req_email = (getattr(user, "email", "") or "").strip()
    try:
        User = get_user_model()
        if pk is not None:
            db_user = User.objects.select_related("role").filter(pk=pk).first()
        if db_user is None and req_email:
            db_user = (
                User.objects.select_related("role")
                .filter(email__iexact=req_email)
                .first()
            )
    except Exception:  # noqa: BLE001
        db_user = None

    if db_user is not None:
        if not db_user.is_active:
            return False
        role_slug = (
            (getattr(db_user.role, "slug", "") or "").lower()
            if db_user.role
            else None
        )
        email = (db_user.email or "").lower()
        if is_ignite_domain_email(email):
            from gqlauth.models import UserStatus

            verified = UserStatus.objects.filter(
                user_id=db_user.pk, verified=True
            ).exists()
            if not ignite_domain_account_trusted(
                role_slug=role_slug,
                is_active=db_user.is_active,
                verified=verified,
            ):
                email = UntrustedIgniteEmail(email)
        role_slug, is_staff, is_super, email = _demote_if_excluded(
            role_slug,
            bool(db_user.is_staff),
            bool(db_user.is_superuser),
            email,
        )
        return _is_admin_access(role_slug, is_staff, is_super, email)

    # Fallback for non-DB test doubles (never grants domain-only admin).
    if getattr(user, "is_active", True) is False:
        return False
    raw_role = getattr(user, "role", None)
    role_slug = (getattr(raw_role, "slug", None) or "").lower() or None
    email = req_email.lower()
    if is_ignite_domain_email(email):
        email = UntrustedIgniteEmail(email)
    role_slug, is_staff, is_super, email = _demote_if_excluded(
        role_slug,
        bool(getattr(user, "is_staff", False)),
        bool(getattr(user, "is_superuser", False)),
        email,
    )
    return _is_admin_access(role_slug, is_staff, is_super, email)


# ─── Tokenization, Hashing & Scrubbing ───────────────────────────────────


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", (text or "").lower())
    return [_stem(w) for w in words if w not in _STOP_WORDS and (len(w) > 1 or w.isdigit())]


def normalize_route(path: str | None) -> str:
    raw = (path or "").strip()
    if not raw:
        return ""
    clean = raw.split("?")[0].split("#")[0].rstrip("/")
    return clean or "/"


def scrub_gap_question(text: str) -> str:
    cleaned = " ".join((text or "").split())[:500]
    cleaned = _EMAIL_RE.sub("[email]", cleaned)
    cleaned = _PHONE_RE.sub("[phone]", cleaned)
    return cleaned.strip()


def normalize_gap_key(text: str) -> str:
    tokens = tokenize(scrub_gap_question(text))
    if not tokens:
        return re.sub(r"\s+", " ", scrub_gap_question(text).lower())[:180]
    return " ".join(tokens[:24])[:180]


def article_embedding_text(title: str, body: str, tags: list[str] | None = None, route_path: str = "") -> str:
    tag_str = ", ".join(str(t).strip() for t in (tags or []) if str(t).strip())
    parts = [title.strip()]
    if route_path:
        parts.append(f"Route: {route_path.strip()}")
    if tag_str:
        parts.append(f"Topics: {tag_str}")
    parts.append(body.strip())
    return "\n".join(p for p in parts if p)


def compute_content_hash(title: str, body: str, tags: list[str] | None = None, route_path: str = "") -> str:
    raw = article_embedding_text(title, body, tags, route_path)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _allowed_audiences(*, is_admin: bool) -> tuple[str, ...]:
    if is_admin:
        return ("all", "client", "admin")
    return ("all", "client")


# ─── Semantic Embeddings Helpers ─────────────────────────────────────────


def _builtin_embeddings_map() -> dict[str, list[float]]:
    """Lazy-cached OpenAI embeddings for built-in docs (gracefully empty if unavailable)."""
    cache_key = "spark_help:builtin_embeddings:v1"
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        return cached
    from utils.ai_text import AiUnavailable, embed_texts

    texts = [
        article_embedding_text(
            d["title"], d["body"], d.get("tags"), d.get("route_path", "")
        )
        for d in BUILTIN_HELP_DOCS
    ]
    try:
        vectors, _model = embed_texts(texts, timeout=12.0)
    except AiUnavailable:
        # Cache short negative result so unconfigured environments don't retry every request.
        cache.set(cache_key, {}, 300)
        return {}
    mapping = {
        doc["id"]: vec for doc, vec in zip(BUILTIN_HELP_DOCS, vectors) if vec
    }
    cache.set(cache_key, mapping, 60 * 60 * 12)
    return mapping


def _query_embedding(query: str) -> list[float] | None:
    cleaned = " ".join((query or "").split())[:500]
    if not cleaned:
        return None
    digest = hashlib.sha256(cleaned.lower().encode("utf-8")).hexdigest()[:24]
    cache_key = f"spark_help:qemb:v1:{digest}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached if isinstance(cached, list) and cached else None
    from utils.ai_text import embed_text

    vec, _model = embed_text(cleaned, timeout=8.0)
    cache.set(cache_key, vec or [], 600)
    return vec


def embed_article_fields(
    title: str, body: str, tags: list[str] | None = None, route_path: str = ""
) -> tuple[list[float], str, str]:
    """Return ``(embedding_vector, embedding_model, content_hash)``."""
    from utils.ai_text import embed_text

    chash = compute_content_hash(title, body, tags, route_path)
    vec, model_name = embed_text(
        article_embedding_text(title, body, tags, route_path), timeout=12.0
    )
    return (vec or []), (model_name if vec else ""), chash


# ─── Hybrid Retrieval (BM25 + Semantic Cosine + Route Context) ───────────


@dataclass
class HelpHit:
    doc_id: str
    title: str
    body: str
    category: str
    audience: str
    route_path: str
    route_label: str
    source_kind: str
    score: float
    bm25_score: float
    semantic_score: float
    matched_via: str
    article_pk: int | None = None


def _load_candidates(*, is_admin: bool, tenant_id: int | None) -> list[dict[str, Any]]:
    from .models import SparkHelpArticle

    allowed = _allowed_audiences(is_admin=is_admin)
    docs: list[dict[str, Any]] = []
    for d in BUILTIN_HELP_DOCS:
        if d["audience"] in allowed:
            docs.append(
                {
                    "id": d["id"],
                    "article_pk": None,
                    "title": d["title"],
                    "body": d["body"],
                    "category": d["category"],
                    "audience": d["audience"],
                    "route_path": d.get("route_path", ""),
                    "route_label": d.get("route_label", ""),
                    "tags": list(d.get("tags") or []),
                    "source_kind": "builtin",
                    "embedding": None,
                }
            )

    try:
        qs = SparkHelpArticle.objects.filter(
            is_active=True, audience__in=allowed
        )
        if tenant_id is not None:
            qs = qs.filter(Q(tenant_id__isnull=True) | Q(tenant_id=tenant_id))
        else:
            qs = qs.filter(tenant_id__isnull=True)
        for art in qs.order_by("-updated_at")[:250]:
            docs.append(
                {
                    "id": f"article:{art.uuid}",
                    "article_pk": art.id,
                    "title": art.title,
                    "body": art.body,
                    "category": art.category,
                    "audience": art.audience,
                    "route_path": art.route_path or "",
                    "route_label": art.route_label or "",
                    "tags": list(art.tags or []),
                    "source_kind": art.source_kind,
                    "embedding": art.embedding if isinstance(art.embedding, list) and art.embedding else None,
                }
            )
    except Exception:  # noqa: BLE001
        logger.debug("SparkHelpArticle query skipped (table unavailable or test without DB).")

    return docs


def search_help_knowledge(
    question: str,
    *,
    is_admin: bool,
    tenant_id: int | None = None,
    current_path: str | None = None,
    top_k: int = 4,
    use_embeddings: bool = True,
) -> tuple[list[HelpHit], str]:
    """Hybrid BM25 + OpenAI semantic search over Spark Help docs & admin FAQs.

    Strictly filters out ``audience="admin"`` when ``is_admin=False``.
    Returns ``(hits, mode)`` where ``mode`` is ``"hybrid"`` or ``"bm25"``.
    """
    from utils.ai_text import cosine_similarity

    candidates = _load_candidates(is_admin=is_admin, tenant_id=tenant_id)
    if not candidates:
        return [], "bm25"

    norm_current = normalize_route(current_path)
    q_terms = list(dict.fromkeys(tokenize(question)))

    # 1. BM25 lexical scoring
    tokenized_docs: list[list[str]] = []
    for doc in candidates:
        # Repeat title + tags so exact title/topic matches rank strongly.
        head = tokenize(f"{doc['title']} {doc['title']} {' '.join(str(t) for t in doc['tags'])} {doc['route_path']}")
        body = tokenize(doc["body"])
        tokenized_docs.append(head + body)

    n = len(tokenized_docs)
    avg_len = (sum(len(d) for d in tokenized_docs) / n) or 1.0
    df: Counter = Counter()
    for d in tokenized_docs:
        df.update(set(d))

    k1, b = 1.4, 0.75
    raw_bm25: list[float] = []
    for d in tokenized_docs:
        tf = Counter(d)
        score = 0.0
        for term in q_terms:
            freq = tf.get(term, 0)
            if not freq:
                continue
            idf = math.log(1.0 + (n - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * freq * (k1 + 1.0) / (freq + k1 * (1.0 - b + b * len(d) / avg_len))
        raw_bm25.append(score)

    max_bm25 = max(raw_bm25) if raw_bm25 else 0.0

    # 2. Optional OpenAI semantic embeddings (text-embedding-3-small)
    q_vec: list[float] | None = None
    builtin_vecs: dict[str, list[float]] = {}
    used_semantic = False
    if use_embeddings:
        q_vec = _query_embedding(
            f"{question} (page: {norm_current})" if norm_current else question
        )
        if q_vec:
            builtin_vecs = _builtin_embeddings_map()
            used_semantic = True

    hits: list[HelpHit] = []
    for doc, b_raw in zip(candidates, raw_bm25):
        b_norm = (b_raw / max_bm25) if max_bm25 > 0 else 0.0
        # Penalize tiny single-word incidental matches slightly when max_bm25 is weak.
        if max_bm25 > 0 and max_bm25 < 1.5:
            b_norm *= max_bm25 / 1.5

        doc_vec = doc["embedding"] or builtin_vecs.get(doc["id"])
        sem_raw = cosine_similarity(q_vec, doc_vec) if (q_vec and doc_vec) else 0.0
        # Rescale cosine similarity above baseline (~0.22 noise floor for text-embedding-3-small).
        sem_norm = max(0.0, min(1.0, (sem_raw - 0.22) / 0.58)) if sem_raw > 0.22 else 0.0

        doc_route = normalize_route(doc["route_path"])
        route_bonus = 0.0
        if norm_current and doc_route and (
            norm_current == doc_route or norm_current.startswith(doc_route + "/")
        ):
            # Stronger boost when user asks about "this page" / current screen.
            low_q = (question or "").lower()
            deictic = any(
                phrase in low_q
                for phrase in ("this page", "this screen", "this tab", "here", "how does this work", "what is this")
            )
            route_bonus = (_ROUTE_BOOST * 1.8) if deictic else _ROUTE_BOOST

        if used_semantic and doc_vec:
            combined = (_SEMANTIC_WEIGHT * sem_norm) + (_BM25_WEIGHT * b_norm) + route_bonus
        else:
            combined = b_norm + route_bonus

        # Slight preference for admin-authored custom articles when scores tie.
        if doc["source_kind"] != "builtin" and combined > 0.15:
            combined += 0.04

        if combined > 0.08:
            hits.append(
                HelpHit(
                    doc_id=doc["id"],
                    title=doc["title"],
                    body=doc["body"],
                    category=doc["category"],
                    audience=doc["audience"],
                    route_path=doc["route_path"],
                    route_label=doc["route_label"] or "Open in Spark",
                    source_kind=doc["source_kind"],
                    score=round(combined, 4),
                    bm25_score=round(b_norm, 4),
                    semantic_score=round(sem_norm, 4),
                    matched_via="hybrid" if (used_semantic and doc_vec) else "bm25",
                    article_pk=doc["article_pk"],
                )
            )

    hits.sort(key=lambda h: (-h.score, h.title))
    top_hits = hits[: max(1, min(top_k, 8))]

    # Increment usage count on matched DB articles (best-effort).
    article_pks = [h.article_pk for h in top_hits if h.article_pk]
    if article_pks:
        try:
            from .models import SparkHelpArticle

            SparkHelpArticle.objects.filter(id__in=article_pks).update(
                usage_count=F("usage_count") + 1
            )
        except Exception:  # noqa: BLE001
            pass

    mode = "hybrid" if any(h.matched_via == "hybrid" for h in top_hits) else "bm25"
    return top_hits, mode


# ─── Knowledge Gaps Logging ──────────────────────────────────────────────


def record_help_gap(
    question: str,
    *,
    is_admin: bool,
    route_path: str | None = None,
    source_surface: str = "ask_spark",
    reason: str = "no_match",
    tenant_id: int | None = None,
    user=None,
):
    """Log or increment an open Knowledge Gap for Ignite admins to resolve."""
    from .models import SparkHelpGap

    cleaned = scrub_gap_question(question)
    if len(cleaned) < 4:
        return None
    norm_key = normalize_gap_key(cleaned)
    caller_role = SparkHelpGap.ROLE_ADMIN if is_admin else SparkHelpGap.ROLE_CLIENT
    clean_route = normalize_route(route_path)[:120]

    try:
        existing = (
            SparkHelpGap.objects.filter(
                normalized_key=norm_key,
                caller_role=caller_role,
                status=SparkHelpGap.STATUS_OPEN,
            )
            .order_by("-id")
            .first()
        )
        if existing is not None:
            existing.asked_count = int(existing.asked_count or 1) + 1
            existing.last_asked_at = timezone.now()
            if clean_route and not existing.route_path:
                existing.route_path = clean_route
            existing.save(update_fields=["asked_count", "last_asked_at", "route_path"])
            return existing

        return SparkHelpGap.objects.create(
            question=cleaned,
            normalized_key=norm_key,
            route_path=clean_route,
            caller_role=caller_role,
            source_surface=source_surface[:24],
            reason=reason[:32],
            tenant_id=tenant_id,
            asked_by=user if (user and getattr(user, "pk", None)) else None,
        )
    except Exception:  # noqa: BLE001
        logger.debug("Could not persist SparkHelpGap", exc_info=True)
        return None


# ─── Direct Spark Help Q&A (Admin Help Mode & Simulator) ─────────────────


_HELP_QA_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer", "confidence", "gap_detected", "used_titles"],
    "properties": {
        "answer": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "gap_detected": {"type": "boolean"},
        "used_titles": {"type": "array", "items": {"type": "string"}},
    },
}


def answer_spark_help_question(
    question: str,
    *,
    is_admin: bool,
    tenant_id: int | None = None,
    user=None,
    current_path: str | None = None,
    log_gaps: bool = True,
) -> dict[str, Any]:
    """Answer a Spark how-to / SOP question strictly within the caller's audience."""
    from utils.ai_text import AiUnavailable, generate_json

    clean_q = " ".join((question or "").split())[:500]
    if not clean_q:
        return {
            "ok": False,
            "reason": "A question is required.",
            "answer": "",
            "confidence": "low",
            "matched_via": "bm25",
            "sources": [],
            "gap_logged": False,
            "is_admin_scope": is_admin,
        }

    hits, mode = search_help_knowledge(
        clean_q,
        is_admin=is_admin,
        tenant_id=tenant_id,
        current_path=current_path,
        top_k=4,
    )
    top_score = hits[0].score if hits else 0.0
    weak = (not hits) or top_score < _MIN_CONFIDENT_SCORE

    if not hits:
        gap = (
            record_help_gap(
                clean_q,
                is_admin=is_admin,
                route_path=current_path,
                source_surface="ask_spark",
                reason="no_match",
                tenant_id=tenant_id,
                user=user,
            )
            if log_gaps
            else None
        )
        msg = (
            "I don't have a Spark Help article covering that yet. "
            + (
                "I logged this in Knowledge Gaps (/help-knowledge) so you can add an answer."
                if is_admin
                else "Reach out to the Ignite team on the Help page (/help) and we'll get you sorted."
            )
        )
        return {
            "ok": True,
            "reason": None,
            "answer": msg,
            "confidence": "low",
            "matched_via": mode,
            "sources": [
                {
                    "title": "Help Knowledge & Gaps" if is_admin else "Help & Support",
                    "route_path": "/help-knowledge" if is_admin else "/help",
                    "route_label": "Open Help Knowledge" if is_admin else "Open Help & Support",
                    "audience": "admin" if is_admin else "all",
                    "score": 0.0,
                }
            ],
            "gap_logged": bool(gap),
            "is_admin_scope": is_admin,
        }

    context_blocks = []
    for idx, h in enumerate(hits, start=1):
        context_blocks.append(
            f"[DOC {idx}] {h.title} (route: {h.route_path or 'n/a'}, audience: {h.audience})\n{h.body}"
        )

    role_rule = (
        "The reader is an Ignite Admin; you may cite both client and admin-only ops SOPs from the provided docs."
        if is_admin
        else "The reader is a Brand Client. NEVER mention internal Ignite admin tools, BA pay, Wingspan, Cloud Scheduler cron secrets, or admin-only features."
    )
    system = (
        "You are Spark's built-in product help assistant. Answer the user's question using ONLY "
        "the provided Spark Help documents.\n"
        f"{role_rule}\n"
        "Rules:\n"
        "1. Keep the answer concise, practical, and scannable (2-5 short sentences or '-' bullets).\n"
        "2. Do NOT invent features, buttons, or URLs that are not in the provided documents.\n"
        "3. If the provided documents do not actually answer the question, set gap_detected=true, "
        "confidence='low', and state plainly what is missing.\n"
        "4. Return strict JSON matching the schema."
    )
    user_prompt = (
        f"Current page: {normalize_route(current_path) or 'unknown'}\n"
        f"Question: {clean_q}\n\n"
        "SPARK HELP DOCUMENTS:\n"
        + "\n\n".join(context_blocks)
    )

    answer_text = ""
    confidence = "medium" if not weak else "low"
    gap_detected = weak
    used_titles: list[str] = []

    try:
        parsed = generate_json(
            system,
            user_prompt,
            schema=_HELP_QA_SCHEMA,
            schema_name="spark_help_answer",
            max_tokens=900,
        )
        answer_text = str(parsed.get("answer") or "").strip()
        confidence = str(parsed.get("confidence") or confidence)
        gap_detected = bool(parsed.get("gap_detected")) or weak
        used_titles = [str(t) for t in (parsed.get("used_titles") or []) if str(t).strip()]
    except (AiUnavailable, Exception):  # noqa: BLE001
        # Deterministic fallback when OpenAI is unavailable: return the top doc cleanly.
        top = hits[0]
        answer_text = f"**{top.title}**\n\n{top.body}"
        confidence = "medium" if not weak else "low"
        gap_detected = weak
        used_titles = [top.title]

    gap_logged = False
    if log_gaps and (gap_detected or confidence == "low"):
        gap = record_help_gap(
            clean_q,
            is_admin=is_admin,
            route_path=current_path,
            source_surface="ask_spark",
            reason="low_confidence",
            tenant_id=tenant_id,
            user=user,
        )
        gap_logged = bool(gap)

    chosen = [h for h in hits if h.title in used_titles] or hits[:2]
    sources = [
        {
            "title": h.title,
            "route_path": h.route_path or "/setup-guide",
            "route_label": h.route_label or "Open in Spark",
            "audience": h.audience,
            "score": h.score,
        }
        for h in chosen
    ]

    return {
        "ok": True,
        "reason": None,
        "answer": answer_text,
        "confidence": confidence,
        "matched_via": mode,
        "sources": sources,
        "gap_logged": gap_logged,
        "is_admin_scope": is_admin,
    }


# ─── Admin Knowledge CRUD & Embedding Sync ───────────────────────────────


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:120] or "help-article"


def upsert_help_article(
    *,
    user,
    title: str,
    body: str,
    uuid_str: str | None = None,
    category: str = "general",
    audience: str = "all",
    route_path: str = "",
    route_label: str = "",
    tags: list[str] | None = None,
    source_kind: str = "admin_faq",
    tenant_id: int | None = None,
    is_active: bool = True,
):
    """Create or update a SparkHelpArticle and compute its OpenAI embedding."""
    from .models import SparkHelpArticle

    clean_title = " ".join((title or "").split())[:180]
    clean_body = (body or "").strip()[:8000]
    if not clean_title or not clean_body:
        raise ValueError("Title and body are required.")

    valid_audiences = {c[0] for c in SparkHelpArticle.AUDIENCE_CHOICES}
    clean_audience = audience if audience in valid_audiences else SparkHelpArticle.AUDIENCE_ALL
    valid_sources = {c[0] for c in SparkHelpArticle.SOURCE_CHOICES}
    clean_source = source_kind if source_kind in valid_sources else SparkHelpArticle.SOURCE_ADMIN_FAQ
    clean_tags = [
        " ".join(str(t).split())[:40]
        for t in (tags or [])
        if str(t).strip()
    ][:15]
    clean_route = (route_path or "").strip()[:120]
    clean_route_label = " ".join((route_label or "").split())[:80]
    clean_category = re.sub(r"[^a-z0-9_-]+", "_", (category or "general").lower()).strip("_")[:60] or "general"

    article = None
    if uuid_str:
        article = SparkHelpArticle.objects.filter(uuid=uuid_str).first()

    new_hash = compute_content_hash(clean_title, clean_body, clean_tags, clean_route)
    needs_embed = (
        article is None
        or article.embedding_hash != new_hash
        or not (isinstance(article.embedding, list) and article.embedding)
    )
    vec: list[float] = list(article.embedding) if (article and not needs_embed) else []
    emb_model: str = article.embedding_model if (article and not needs_embed) else ""
    if needs_embed:
        vec, emb_model, new_hash = embed_article_fields(
            clean_title, clean_body, clean_tags, clean_route
        )

    if article is None:
        article = SparkHelpArticle.objects.create(
            title=clean_title,
            slug=_slugify(clean_title),
            category=clean_category,
            audience=clean_audience,
            route_path=clean_route,
            route_label=clean_route_label,
            tags=clean_tags,
            body=clean_body,
            source_kind=clean_source,
            tenant_id=tenant_id,
            embedding=vec,
            embedding_model=emb_model,
            embedding_hash=new_hash,
            is_active=is_active,
            created_by=user if getattr(user, "pk", None) else None,
            updated_by=user if getattr(user, "pk", None) else None,
        )
    else:
        article.title = clean_title
        article.slug = _slugify(clean_title)
        article.category = clean_category
        article.audience = clean_audience
        article.route_path = clean_route
        article.route_label = clean_route_label
        article.tags = clean_tags
        article.body = clean_body
        article.is_active = is_active
        article.embedding = vec
        article.embedding_model = emb_model
        article.embedding_hash = new_hash
        if getattr(user, "pk", None):
            article.updated_by = user
        article.save()

    return article


def sync_missing_embeddings(*, limit: int = 50) -> dict[str, int]:
    """Backfill or refresh OpenAI text-embedding-3-small vectors for active articles."""
    from utils.ai_text import AiUnavailable, embed_texts
    from .models import SparkHelpArticle

    # Also refresh built-in docs embedding cache.
    cache.delete("spark_help:builtin_embeddings:v1")
    builtin_map = _builtin_embeddings_map()

    articles = list(SparkHelpArticle.objects.filter(is_active=True).order_by("-updated_at")[:limit])
    stale: list[tuple[SparkHelpArticle, str, str]] = []
    for art in articles:
        chash = compute_content_hash(art.title, art.body, art.tags or [], art.route_path or "")
        has_vec = isinstance(art.embedding, list) and len(art.embedding) > 0
        if not has_vec or art.embedding_hash != chash:
            stale.append(
                (art, article_embedding_text(art.title, art.body, art.tags or [], art.route_path or ""), chash)
            )

    updated = 0
    if stale:
        try:
            vectors, model_name = embed_texts([t for _, t, _ in stale], timeout=20.0)
            for (art, _txt, chash), vec in zip(stale, vectors):
                art.embedding = vec
                art.embedding_model = model_name
                art.embedding_hash = chash
                art.save(update_fields=["embedding", "embedding_model", "embedding_hash", "updated_at"])
                updated += 1
        except AiUnavailable:
            pass

    return {
        "articles_checked": len(articles),
        "articles_embedded": updated,
        "builtin_embedded": len(builtin_map),
    }
