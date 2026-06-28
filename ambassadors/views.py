"""Public (token-gated, no-login) one-click shift-extension approval page.

Linked from the admin "Extension requested" email. GET renders a small branded
page with the request details + Approve / Decline buttons; POST performs the
decision via ambassadors.extensions.resolve_extension and renders the result.

Mounted under /api/public/ (config/urls.py) alongside the other tokenized public
pages (request approval, receipts, recap reports). No JWT — the signed token in
the URL is the only credential, and the action is idempotent.
"""
from __future__ import annotations

import logging
from html import escape

from django.http import HttpRequest, HttpResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from ambassadors.extensions import resolve_extension, verify_extension_token

logger = logging.getLogger(__name__)


def _page(title: str, inner: str, *, status: int = 200) -> HttpResponse:
    html = f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{escape(title)} · Spark</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; background:#0a0d09; color:#f2f3ee;
    font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
    display:flex; min-height:100vh; align-items:center; justify-content:center; }}
  .card {{ width:100%; max-width:440px; margin:24px; padding:28px;
    background:#11140e; border:1px solid #1f2418; border-radius:18px; }}
  .brand {{ font-size:11px; letter-spacing:.22em; color:#c5f546; text-transform:uppercase; }}
  h1 {{ font-size:22px; margin:10px 0 16px; letter-spacing:-.02em; }}
  .row {{ font-size:15px; color:#cfd3c6; margin:6px 0; }}
  .row b {{ color:#f2f3ee; }}
  .note {{ margin:12px 0 0; padding:12px; background:#0a0d09; border:1px solid #1f2418;
    border-radius:10px; color:#cfd3c6; font-size:14px; }}
  form {{ display:inline; }}
  .actions {{ display:flex; gap:10px; margin-top:22px; flex-wrap:wrap; }}
  button {{ flex:1; min-width:140px; padding:14px 16px; border-radius:12px;
    font-size:15px; font-weight:700; border:none; cursor:pointer; }}
  .approve {{ background:#c5f546; color:#0a0d09; }}
  .decline {{ background:transparent; color:#ef5a2a; border:1px solid #3a2218; }}
  .label {{ font-size:11px; letter-spacing:.16em; color:#8b9180; text-transform:uppercase; }}
  .ok {{ color:#c5f546; }} .muted {{ color:#8b9180; font-size:13px; margin-top:18px; }}
</style></head><body><div class="card">
<div class="brand">Spark · Shift extension</div>
{inner}
</div></body></html>"""
    return HttpResponse(html, status=status, content_type="text/html; charset=utf-8")


@method_decorator(csrf_exempt, name="dispatch")
class ExtensionApprovalView(View):
    """GET → confirmation page with Approve/Decline. POST → resolve."""

    def _load(self, token: str):
        from ambassadors.models import ShiftExtensionRequest

        ext_id = verify_extension_token(token)
        if ext_id is None:
            return None
        return (
            ShiftExtensionRequest.objects.select_related(
                "event", "ambassador", "ambassador__user"
            )
            .filter(id=ext_id)
            .first()
        )

    def get(self, request: HttpRequest, token: str) -> HttpResponse:
        ext = self._load(token)
        if ext is None:
            return _page(
                "Link expired",
                "<h1>Link expired</h1><p class='row'>This approval link is no "
                "longer valid. Open the Spark dashboard to review the request.</p>",
                status=400,
            )
        ba = getattr(ext, "ambassador", None)
        ba_user = getattr(ba, "user", None)
        ba_name = (
            f"{getattr(ba_user, 'first_name', '') or ''} "
            f"{getattr(ba_user, 'last_name', '') or ''}"
        ).strip() or "A BA"
        venue = getattr(getattr(ext, "event", None), "name", None) or "their shift"

        if ext.status != "pending":
            return _page(
                "Already resolved",
                f"<h1>Already {escape(ext.status)}</h1>"
                f"<p class='row'><b>{escape(ba_name)}</b>'s "
                f"+{ext.minutes_requested} min request for "
                f"<b>{escape(venue)}</b> was already {escape(ext.status)}.</p>",
            )
        reason = (
            f"<div class='note'>“{escape(ext.reason[:400])}”</div>"
            if ext.reason else ""
        )
        inner = (
            f"<h1>{escape(ba_name)} needs more time</h1>"
            f"<p class='row'><span class='label'>Venue</span><br><b>{escape(venue)}</b></p>"
            f"<p class='row'><span class='label'>Extra time requested</span><br>"
            f"<b>{ext.minutes_requested} minutes</b></p>"
            f"{reason}"
            f"<div class='actions'>"
            f"<form method='post'><input type='hidden' name='action' value='approve'>"
            f"<button class='approve' type='submit'>Approve +{ext.minutes_requested} min</button></form>"
            f"<form method='post'><input type='hidden' name='action' value='decline'>"
            f"<button class='decline' type='submit'>Decline</button></form>"
            f"</div>"
            f"<p class='muted'>The BA keeps working until you decide. You can "
            f"also manage this in the Spark dashboard.</p>"
        )
        return _page("Extension requested", inner)

    def post(self, request: HttpRequest, token: str) -> HttpResponse:
        ext = self._load(token)
        if ext is None:
            return _page(
                "Link expired",
                "<h1>Link expired</h1><p class='row'>This approval link is no "
                "longer valid.</p>",
                status=400,
            )
        action = (request.POST.get("action") or "").strip().lower()
        if action not in ("approve", "decline"):
            return _page(
                "Hmm",
                "<h1>Couldn't read that</h1><p class='row'>Please use the "
                "Approve or Decline button.</p>",
                status=400,
            )
        try:
            result = resolve_extension(ext, approve=(action == "approve"))
        except Exception as exc:  # noqa: BLE001
            logger.exception("public extension resolve failed token-ext=%s", ext.id)
            return _page(
                "Something went wrong",
                f"<h1>Something went wrong</h1><p class='row'>{escape(str(exc))}"
                "</p><p class='row'>Try the Spark dashboard.</p>",
                status=500,
            )
        ba_name = escape(result.get("ba_name") or "The BA")
        venue = escape(result.get("venue") or "their shift")
        if result.get("already"):
            head = f"Already {escape(result.get('status') or 'resolved')}"
            msg = (
                f"<b>{ba_name}</b>'s request for <b>{venue}</b> was already "
                f"{escape(result.get('status') or 'resolved')}."
            )
        elif action == "approve":
            head = "Approved"
            msg = (
                f"<b>{ba_name}</b> is cleared for "
                f"<b>+{result.get('approved_minutes')} min</b> at <b>{venue}</b>. "
                "They've been notified."
            )
        else:
            head = "Declined"
            msg = (
                f"<b>{ba_name}</b>'s extra-time request for <b>{venue}</b> was "
                "declined. They've been notified."
            )
        return _page(
            head,
            f"<h1 class='ok'>{escape(head)}</h1><p class='row'>{msg}</p>"
            "<p class='muted'>You can close this window.</p>",
        )
