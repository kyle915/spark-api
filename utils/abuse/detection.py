"""Phase 2 — daily suspicious-usage detector.

Reads the hourly ``UsageBucket`` rollups, the ``AuditLogEntry`` log and the
``AmbassadorReadLog`` for the last ``window_hours`` and compares each
NON-ADMIN user (and tenant) against their own prior baseline. Pure function
of the database — no side effects — so the cron endpoint and tests share it.

Signals (thresholds are settings, defaults in ``_t``):

- volume_spike        requests far above the user's own baseline (or a hard
                      ceiling for brand-new users with no baseline)
- tenant_volume_spike same, per tenant
- page_walking        many cursor/offset "next page" requests
- export_burst        many exports in the window or in a single hour
- non_browser_ua      user agents that aren't a browser (curl, python, node…)
- many_ips            many distinct IPs for one user
- off_hours_bulk      heavy traffic 00:00–05:00 Pacific
- ambassador_harvest  many DISTINCT ambassadors' records pulled
- throttled           repeatedly hit a rate limit
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Count, Sum

PACIFIC = ZoneInfo("America/Los_Angeles")

_BROWSER_RE = re.compile(r"Mozilla/5\.0", re.I)
_BOT_RE = re.compile(
    r"curl|wget|python|httpx|aiohttp|requests|node-fetch|axios|undici|go-http|okhttp|"
    r"java/|libwww|postman|insomnia|headless|puppeteer|playwright|selenium|scrapy|bot\b|"
    r"claude|anthropic|openai|gpt|langchain|mcp",
    re.I,
)


def _t(name: str, default: int | float):
    return type(default)(getattr(settings, name, default))


@dataclass
class Finding:
    scope: str  # "user" | "tenant"
    user_id: int | None
    email: str
    tenant_id: int | None
    signals: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "scope": self.scope, "user_id": self.user_id, "email": self.email,
            "tenant_id": self.tenant_id, "signals": self.signals, "stats": self.stats,
        }


def is_non_browser_ua(ua: str) -> bool:
    if not ua:
        return True
    return bool(_BOT_RE.search(ua)) or not _BROWSER_RE.search(ua)


def detect(now: datetime | None = None, window_hours: int = 24,
           baseline_days: int = 14) -> list[Finding]:
    from django.contrib.auth import get_user_model

    from .models import AmbassadorReadLog, AuditLogEntry, UsageBucket

    now = now or datetime.now(timezone.utc)
    start = now - timedelta(hours=window_hours)
    base_start = start - timedelta(days=baseline_days)

    th = {
        "min_requests": _t("ABUSE_DETECT_MIN_REQUESTS", 1500),
        "spike_multiplier": _t("ABUSE_DETECT_SPIKE_MULTIPLIER", 5.0),
        "new_user_requests": _t("ABUSE_DETECT_NEW_USER_REQUESTS", 3000),
        "paged": _t("ABUSE_DETECT_PAGED_REQUESTS", 300),
        "exports": _t("ABUSE_DETECT_EXPORTS", 15),
        "exports_hour": _t("ABUSE_DETECT_EXPORTS_PER_HOUR", 8),
        "ips": _t("ABUSE_DETECT_DISTINCT_IPS", 6),
        "off_hours": _t("ABUSE_DETECT_OFF_HOURS_REQUESTS", 300),
        "distinct_bas": _t("ABUSE_DETECT_DISTINCT_AMBASSADORS", 100),
        "throttled": _t("ABUSE_DETECT_THROTTLED", 20),
        "tenant_min_requests": _t("ABUSE_DETECT_TENANT_MIN_REQUESTS", 5000),
    }

    window_rows = list(
        UsageBucket.objects.filter(hour__gte=start, hour__lt=now, is_admin=False).values(
            "user_id", "tenant_id", "hour", "request_count", "paged_count",
            "throttled_count", "response_bytes", "ips", "user_agents",
        )
    )
    per_user: dict[int, dict] = defaultdict(lambda: {
        "requests": 0, "paged": 0, "throttled": 0, "bytes": 0, "ips": set(), "uas": set(),
        "off_hours": 0, "tenant_id": None,
    })
    per_tenant: dict[int, int] = defaultdict(int)
    for row in window_rows:
        u = per_user[row["user_id"]]
        u["requests"] += row["request_count"]
        u["paged"] += row["paged_count"]
        u["throttled"] += row["throttled_count"]
        u["bytes"] += row["response_bytes"]
        u["ips"].update(row["ips"] or [])
        u["uas"].update(row["user_agents"] or [])
        u["tenant_id"] = u["tenant_id"] or row["tenant_id"]
        if row["hour"].astimezone(PACIFIC).hour < 5:
            u["off_hours"] += row["request_count"]
        if row["tenant_id"]:
            per_tenant[row["tenant_id"]] += row["request_count"]

    baseline_user = {
        r["user_id"]: r["total"] or 0
        for r in UsageBucket.objects.filter(hour__gte=base_start, hour__lt=start, is_admin=False)
        .values("user_id").annotate(total=Sum("request_count"))
    }
    baseline_tenant = {
        r["tenant_id"]: r["total"] or 0
        for r in UsageBucket.objects.filter(hour__gte=base_start, hour__lt=start, is_admin=False)
        .exclude(tenant_id=None).values("tenant_id").annotate(total=Sum("request_count"))
    }

    exports = defaultdict(lambda: {"total": 0, "hours": defaultdict(int)})
    for row in AuditLogEntry.objects.filter(
        created_at__gte=start, created_at__lt=now, kind="export", is_admin=False
    ).values("user_id", "created_at"):
        if row["user_id"] is None:
            continue
        e = exports[row["user_id"]]
        e["total"] += 1
        e["hours"][row["created_at"].replace(minute=0, second=0, microsecond=0)] += 1

    distinct_bas = {
        r["user_id"]: r["n"]
        for r in AmbassadorReadLog.objects.filter(day__gte=start.date(), day__lte=now.date())
        .values("user_id").annotate(n=Count("ambassador_key", distinct=True))
    }

    user_ids = set(per_user) | set(exports) | set(distinct_bas)
    emails = dict(
        get_user_model().objects.filter(pk__in=user_ids).values_list("pk", "email")
    )

    days_scale = window_hours / 24.0
    findings: list[Finding] = []
    for uid in sorted(user_ids):
        u = per_user.get(uid) or {"requests": 0, "paged": 0, "throttled": 0, "bytes": 0,
                                   "ips": set(), "uas": set(), "off_hours": 0, "tenant_id": None}
        f = Finding("user", uid, emails.get(uid, ""), u["tenant_id"])
        base_daily = baseline_user.get(uid, 0) / float(baseline_days)
        expected = base_daily * days_scale
        if base_daily > 0:
            if u["requests"] >= th["min_requests"] and u["requests"] > expected * th["spike_multiplier"]:
                f.signals.append("volume_spike")
        elif u["requests"] >= th["new_user_requests"]:
            f.signals.append("volume_spike")
        if u["paged"] >= th["paged"]:
            f.signals.append("page_walking")
        ex = exports.get(uid)
        if ex and (ex["total"] >= th["exports"] or max(ex["hours"].values()) >= th["exports_hour"]):
            f.signals.append("export_burst")
        bot_uas = sorted(ua for ua in u["uas"] if is_non_browser_ua(ua))
        if bot_uas:
            f.signals.append("non_browser_ua")
        if len(u["ips"]) >= th["ips"]:
            f.signals.append("many_ips")
        if u["off_hours"] >= th["off_hours"]:
            f.signals.append("off_hours_bulk")
        if distinct_bas.get(uid, 0) >= th["distinct_bas"]:
            f.signals.append("ambassador_harvest")
        if u["throttled"] >= th["throttled"]:
            f.signals.append("throttled")
        if f.signals:
            f.stats = {
                "requests": u["requests"], "baseline_daily": round(base_daily, 1),
                "paged": u["paged"], "exports": ex["total"] if ex else 0,
                "distinct_ambassadors": distinct_bas.get(uid, 0), "ips": len(u["ips"]),
                "off_hours_requests": u["off_hours"], "throttled": u["throttled"],
                "response_mb": round(u["bytes"] / 1_000_000, 2),
                "non_browser_user_agents": bot_uas[:3],
            }
            findings.append(f)

    for tid, total in sorted(per_tenant.items()):
        base_daily = baseline_tenant.get(tid, 0) / float(baseline_days)
        expected = base_daily * days_scale
        if total >= th["tenant_min_requests"] and (
            base_daily == 0 or total > expected * th["spike_multiplier"]
        ):
            findings.append(Finding("tenant", None, "", tid, ["tenant_volume_spike"],
                                    {"requests": total, "baseline_daily": round(base_daily, 1)}))
    return findings


def render_report_html(findings: list[Finding], window_hours: int) -> str:
    import html

    rows = []
    for f in findings:
        who = html.escape(f.email or (f"user {f.user_id}" if f.user_id else f"tenant {f.tenant_id}"))
        stats = ", ".join(f"{k}={html.escape(str(v))}" for k, v in f.stats.items() if v not in (0, [], None))
        rows.append(
            f"<tr><td>{html.escape(f.scope)}</td><td>{who}</td><td>{f.tenant_id or '-'}</td>"
            f"<td><b>{html.escape(', '.join(f.signals))}</b></td><td>{stats}</td></tr>"
        )
    return (
        f"<h2 style='margin:0 0 8px'>Spark usage watch: {len(findings)} flagged</h2>"
        f"<p>Non-admin GraphQL usage in the last {window_hours}h compared with each user's "
        "own 14-day baseline. Review in Django admin → Abuse guardrails (audit log, usage "
        "buckets, ambassador read log).</p>"
        "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;"
        "font-size:13px'><tr><th>Scope</th><th>Who</th><th>Tenant</th><th>Signals</th>"
        "<th>Stats</th></tr>" + "".join(rows) + "</table>"
        "<p style='color:#888'>Internal Ignite alert — never forwarded to brands.</p>"
    )


def send_report(findings: list[Finding], window_hours: int) -> int:
    from utils.mailer import Envelope, Mailer

    recipients = list(getattr(settings, "ABUSE_ALERT_EMAILS", []) or [])
    recipients = [r for r in recipients if r.lower().endswith("@igniteproductions.co")]
    if not findings or not recipients:
        return 0
    body = render_report_html(findings, window_hours)

    class _UsageWatchMailer(Mailer):
        def envelope(self) -> Envelope:
            return Envelope(
                subject=f"[Spark usage watch] {len(findings)} flagged in the last {window_hours}h",
                html=body,
                to_emails=recipients,
            )

    _UsageWatchMailer().send_now()
    return len(recipients)


def prune_counters(now: datetime | None = None, keep_hours: int = 48) -> int:
    from .models import RateLimitCounter

    now = now or datetime.now(timezone.utc)
    deleted, _ = RateLimitCounter.objects.filter(
        window_start__lt=now - timedelta(hours=keep_hours)
    ).delete()
    return deleted
