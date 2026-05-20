from utils.mailer import Envelope, Mailer
from .services import TenantDigest


class AdminDigestMailer(Mailer):
    """Daily/weekly Spark digest for tenant admins.

    Caller is responsible for deciding whether to send (e.g. skip
    empty digests) — this just builds the envelope.
    """

    def __init__(
        self,
        digest: TenantDigest,
        *,
        to_emails: list[str],
        web_app_base_url: str = "https://spark-new-admin.web.app",
    ):
        self.digest = digest
        self.to_emails = to_emails
        self.web_app_base_url = web_app_base_url.rstrip("/")

    def envelope(self) -> Envelope:
        d = self.digest
        action_chip = (
            f"{d.pending_approvals.count} pending · {d.unfiled_recaps.count} unfiled recaps"
            if d.total_action_items
            else "All clear"
        )
        subject = f"Spark {d.window_label.lower()} digest — {d.tenant_name} · {action_chip}"
        return Envelope(
            subject=subject,
            template="digest.templates.emails.admin_digest",
            to_emails=self.to_emails,
            context={
                "digest": d,
                "web_app_base_url": self.web_app_base_url,
                "tracker_url": f"{self.web_app_base_url}/requests/list",
                "inbox_url": f"{self.web_app_base_url}/inbox",
                "approvals_url": f"{self.web_app_base_url}/approvals",
                "recaps_url": f"{self.web_app_base_url}/recaps/list",
            },
        )


# ─── Executive summary (weekly) ─────────────────────────────────

from .exec_services import ExecutiveSummary


class ExecutiveSummaryMailer(Mailer):
    """Weekly top-line stats email for tenant admins.

    Pairs with the daily admin digest in this same file. Both fire
    from cron — the daily one runs every weekday morning, this one
    runs Monday mornings against the prior 7 days.
    """

    def __init__(
        self,
        summary: ExecutiveSummary,
        *,
        to_emails: list[str],
        web_app_base_url: str = "https://spark-new-admin.web.app",
    ):
        self.summary = summary
        self.to_emails = to_emails
        self.web_app_base_url = web_app_base_url.rstrip("/")

    def envelope(self) -> Envelope:
        s = self.summary
        # Subject reads like a Slack rollup so the inbox preview tells
        # the whole story: "Spark · Liquid Death · 23 recaps, 4,200
        # consumers · Week of May 13 – May 19".
        chip = f"{s.recap_count} recap{'s' if s.recap_count != 1 else ''}"
        if s.consumer_reach:
            chip += f", {s.consumer_reach:,} consumers"
        subject = f"Spark · {s.tenant_name} · {chip}"
        return Envelope(
            subject=subject,
            template="digest.templates.emails.executive_summary",
            to_emails=self.to_emails,
            context={
                "summary": s,
                "web_app_base_url": self.web_app_base_url,
                "tracker_url": f"{self.web_app_base_url}/requests/list",
                "recaps_url": f"{self.web_app_base_url}/recaps/list",
                "reports_url": f"{self.web_app_base_url}/reports",
            },
        )
