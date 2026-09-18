"""HTTP entry for retail-schedule Sheet → confirmation / cancel.

POST ``/internal/torch-sheet-event-confirmation`` or
``/internal/liquid-death-sheet-event-confirmation`` with ``X-Cron-Secret``.
Mounted outside ``/internal/cron/`` so the Apps Script URL stays short, but
uses the same secret gate as cron jobs.

Sheet id is allowlisted (Torch + Liquid Death). Passing the wrong id returns
400 even if the path is the Torch or LD endpoint.
"""

from __future__ import annotations

import json
import logging

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from events.sheet_event_confirmations import (
    LIQUID_DEATH_SHEET_ID,
    TORCH_PUBLIC_FORM_SHEET_ID,
    handle_action,
    validate_sheet_id,
)

logger = logging.getLogger(__name__)


def _check_secret(request: HttpRequest) -> JsonResponse | None:
    expected = getattr(settings, "INTERNAL_CRON_SECRET", None)
    if not expected:
        logger.error(
            "INTERNAL_CRON_SECRET is not configured — refusing sheet confirmation."
        )
        return JsonResponse(
            {"ok": False, "error": "secret-not-configured"}, status=503
        )
    provided = request.headers.get("X-Cron-Secret", "")
    if provided != expected:
        return JsonResponse({"ok": False, "error": "unauthorized"}, status=401)
    return None


def _parse_body(request: HttpRequest) -> dict:
    if request.body:
        try:
            data = json.loads(request.body.decode("utf-8") or "{}")
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return {k: request.POST.get(k) for k in request.POST.keys()}


@method_decorator(csrf_exempt, name="dispatch")
class SheetEventConfirmationView(View):
    """POST body::

        {
          "action": "send" | "cancel",
          "rowNumber": 42,
          "sheetId": "<allowlisted spreadsheet id>",
          "dryRun": false,
          "values": { "Date": "...", "BA Name": "...", ... }
        }

    ``values`` may also be flattened onto the top-level body.
    """

    default_sheet_id: str = TORCH_PUBLIC_FORM_SHEET_ID

    def post(self, request: HttpRequest) -> HttpResponse:
        deny = _check_secret(request)
        if deny is not None:
            return deny

        body = _parse_body(request)
        action = (body.get("action") or "send").strip().lower()
        try:
            row_number = int(body.get("rowNumber") or body.get("row_number") or 0)
        except (TypeError, ValueError):
            row_number = 0
        if row_number < 2:
            return JsonResponse(
                {
                    "ok": False,
                    "error": "rowNumber-required",
                    "message": "rowNumber must be >= 2 (data rows)",
                },
                status=400,
            )

        sheet_id = (
            body.get("sheetId") or body.get("sheet_id") or self.default_sheet_id
        )
        try:
            sheet_id = validate_sheet_id(str(sheet_id))
        except ValueError as exc:
            return JsonResponse(
                {"ok": False, "error": "invalid-sheet", "message": str(exc)},
                status=400,
            )

        dry_run = str(body.get("dryRun") or body.get("dry_run") or "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        values = body.get("values") if isinstance(body.get("values"), dict) else None
        if values is None:
            skip = {
                "action",
                "rowNumber",
                "row_number",
                "sheetId",
                "sheet_id",
                "dryRun",
                "dry_run",
                "values",
            }
            values = {k: v for k, v in body.items() if k not in skip}

        try:
            result = handle_action(
                action,
                values,
                row_number=row_number,
                sheet_id=sheet_id,
                dry_run=dry_run,
            )
        except ValueError as exc:
            return JsonResponse(
                {"ok": False, "error": "bad-request", "message": str(exc)},
                status=400,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("sheet-event-confirmation failed")
            return JsonResponse(
                {"ok": False, "error": "server-error", "message": str(exc)},
                status=500,
            )

        status_code = 200 if result.ok else 409
        details = result.details or {}
        already_sent = bool(details.get("already_sent"))
        return JsonResponse(
            {
                "ok": result.ok,
                "action": result.action,
                "status": result.status,
                "message": result.message,
                "confirmationUuid": result.confirmation_uuid,
                "timezone": result.timezone_name,
                "timezoneNote": result.timezone_note,
                "dryRun": result.dry_run,
                "alreadySent": already_sent,
                "details": details,
            },
            status=status_code,
        )


class TorchSheetEventConfirmationView(SheetEventConfirmationView):
    default_sheet_id = TORCH_PUBLIC_FORM_SHEET_ID


class LiquidDeathSheetEventConfirmationView(SheetEventConfirmationView):
    default_sheet_id = LIQUID_DEATH_SHEET_ID
