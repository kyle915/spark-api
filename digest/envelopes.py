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
