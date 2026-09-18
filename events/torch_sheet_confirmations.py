"""Torch retail-schedule Sheet → Event Confirmation (thin re-export).

Implementation lives in ``events.sheet_event_confirmations`` so Liquid Death
(and future tenants) share the same mailer + stamp path.
"""

from __future__ import annotations

from events.sheet_event_confirmations import (  # noqa: F401
    CONFIRMATION_EXTRA_HEADERS,
    DEFAULT_EVENT_TYPE_LABEL,
    DEFAULT_TIMEZONE_NAME,
    SENT_STATUS_HEADER,
    STATUS_CANCELLED,
    STATUS_ERROR,
    STATUS_QUEUED,
    STATUS_SENT,
    TORCH_CONFIG,
    TORCH_TENANT_SLUG,
    ActionResult,
    SheetRowPayload,
    cancel_from_sheet_row,
    config_for_sheet_id,
    handle_action,
    parse_sheet_clock,
    parse_sheet_date_iso,
    payload_from_mapping,
    products_from_skus_cell,
    resolve_timezone_for_row,
    send_from_sheet_row,
    validate_sheet_id,
    write_row_status,
    _KNOWN_STATUS_COLS,
    _RETAIL_TAB_TITLE,
    _already_cancelled,
    _already_queued,
    _already_sent,
    _find_mailed_confirmation,
    _torch_tenant,
)
from utils.torch_public_form_sheet import TORCH_PUBLIC_FORM_SHEET_ID  # noqa: F401

__all__ = [
    "CONFIRMATION_EXTRA_HEADERS",
    "DEFAULT_EVENT_TYPE_LABEL",
    "DEFAULT_TIMEZONE_NAME",
    "SENT_STATUS_HEADER",
    "STATUS_CANCELLED",
    "STATUS_ERROR",
    "STATUS_QUEUED",
    "STATUS_SENT",
    "TORCH_CONFIG",
    "TORCH_PUBLIC_FORM_SHEET_ID",
    "TORCH_TENANT_SLUG",
    "ActionResult",
    "SheetRowPayload",
    "cancel_from_sheet_row",
    "config_for_sheet_id",
    "handle_action",
    "parse_sheet_clock",
    "parse_sheet_date_iso",
    "payload_from_mapping",
    "products_from_skus_cell",
    "resolve_timezone_for_row",
    "send_from_sheet_row",
    "validate_sheet_id",
    "write_row_status",
]
