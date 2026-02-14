from __future__ import annotations

from io import BytesIO
from typing import Iterable

from openpyxl import Workbook

from utils.gcs import extract_blob_name_from_url, generate_download_url

SIGNED_URL_EXPIRATION_MINUTES = 60 * 24 * 7


def _format_dt(value) -> str:
    if not value:
        return ""
    try:
        return value.isoformat()
    except Exception:
        return str(value)


def _format_date_mdy(value) -> str:
    if not value:
        return ""
    try:
        return value.strftime("%m/%d/%Y")
    except Exception:
        return str(value)


def _safe(value) -> str:
    if value is None:
        return ""
    return str(value)


def _format_user_name(user) -> str:
    if not user:
        return ""
    first_name = getattr(user, "first_name", "") or ""
    last_name = getattr(user, "last_name", "") or ""
    full_name = f"{first_name} {last_name}".strip()
    if full_name:
        return full_name
    return getattr(user, "username", None) or str(user)


def _get_retailer_name(recap) -> str:
    retailer = getattr(recap, "retailer", None)
    if retailer and getattr(retailer, "name", None):
        return retailer.name
    event = getattr(recap, "event", None)
    request = getattr(event, "request", None) if event else None
    request_retailer = getattr(request, "retailer", None) if request else None
    return getattr(request_retailer, "name", "") or ""


def _get_distributor_name(recap) -> str:
    event = getattr(recap, "event", None)
    request = getattr(event, "request", None) if event else None
    distributor = getattr(request, "distributor", None) if request else None
    return getattr(distributor, "name", "") or ""


def build_recaps_xlsx(recaps: Iterable[object]) -> bytes:
    """Build an Excel report containing recap data and related tables."""
    wb = Workbook()

    recap_sheet = wb.active
    recap_sheet.title = "Recaps"
    recap_sheet.append(
        [
            "recap_uuid",
            "recap_name",
            "approved",
            "event_uuid",
            "event_name",
            "event_date",
            "event_address",
            "retailer",
            "distributor",
            "ambassador",
            "job",
            "total_engagements",
            "products_sold",
            "total_cans_sold",
            "total_packs_sold",
            "total_earnings",
            "account_spend_amount",
            "created_at",
            "updated_at",
        ]
    )

    engagements_sheet = wb.create_sheet(title="ConsumerEngagements")
    engagements_sheet.append(
        [
            "recap_uuid",
            "recap_name",
            "total_consumer",
            "first_time_consumers",
            "brand_aware_consumers",
            "willing_to_purchase_consumers",
            "not_willing_consumers",
        ]
    )

    samples_sheet = wb.create_sheet(title="ProductSamples")
    samples_sheet.append(
        [
            "recap_uuid",
            "recap_name",
            "product",
            "quantity",
        ]
    )

    sales_sheet = wb.create_sheet(title="SalesPerformance")
    sales_sheet.append(
        [
            "recap_uuid",
            "recap_name",
            "product",
            "type_of_good",
            "price",
        ]
    )

    consumer_feedback_sheet = wb.create_sheet(title="ConsumerFeedback")
    consumer_feedback_sheet.append(
        [
            "recap_uuid",
            "recap_name",
            "demographics",
            "feedback",
            "quotes",
            "positive_stories",
            "reasons_to_decline",
        ]
    )

    account_feedback_sheet = wb.create_sheet(title="AccountFeedback")
    account_feedback_sheet.append(
        [
            "recap_uuid",
            "recap_name",
            "do_differently_feedback",
            "feedback",
            "corpo_card",
        ]
    )

    files_sheet = wb.create_sheet(title="RecapFiles")
    files_sheet.append(
        [
            "recap_uuid",
            "recap_name",
            "file_name",
            "file_type",
            "file_category",
            "approved",
            "file_path",
        ]
    )

    for recap in recaps:
        ambassador_user = None
        if getattr(recap, "ambassador", None) and getattr(recap.ambassador, "user", None):
            ambassador_user = recap.ambassador.user

        recap_sheet.append(
            [
                _safe(getattr(recap, "uuid", None)),
                _safe(getattr(recap, "name", None)),
                bool(getattr(recap, "approved", False)),
                _safe(getattr(getattr(recap, "event", None), "uuid", None)),
                _safe(getattr(getattr(recap, "event", None), "name", None)),
                _format_date_mdy(getattr(getattr(recap, "event", None), "date", None)),
                _safe(getattr(getattr(recap, "event", None), "address", None)),
                _get_retailer_name(recap),
                _get_distributor_name(recap),
                _format_user_name(ambassador_user),
                _safe(getattr(getattr(recap, "job", None), "name", None)),
                _safe(getattr(recap, "total_engagements", None)),
                _safe(getattr(recap, "products_sold", None)),
                _safe(getattr(recap, "total_cans_sold", None)),
                _safe(getattr(recap, "total_packs_sold", None)),
                _safe(getattr(recap, "total_earnings", None)),
                _safe(getattr(recap, "account_spend_amount", None)),
                _format_dt(getattr(recap, "created_at", None)),
                _format_dt(getattr(recap, "updated_at", None)),
            ]
        )

        for engagement in getattr(recap, "consumer_engagements", []).all():
            engagements_sheet.append(
                [
                    _safe(getattr(recap, "uuid", None)),
                    _safe(getattr(recap, "name", None)),
                    _safe(getattr(engagement, "total_consumer", None)),
                    _safe(getattr(engagement, "first_time_consumers", None)),
                    _safe(getattr(engagement, "brand_aware_consumers", None)),
                    _safe(getattr(engagement, "willing_to_purchase_consumers", None)),
                    _safe(getattr(engagement, "not_willing_consumers", None)),
                ]
            )

        for sample in getattr(recap, "product_samples", []).all():
            samples_sheet.append(
                [
                    _safe(getattr(recap, "uuid", None)),
                    _safe(getattr(recap, "name", None)),
                    _safe(getattr(getattr(sample, "product", None), "name", None)),
                    _safe(getattr(sample, "quantity", None)),
                ]
            )

        for sale in getattr(recap, "sales_performance", []).all():
            sales_sheet.append(
                [
                    _safe(getattr(recap, "uuid", None)),
                    _safe(getattr(recap, "name", None)),
                    _safe(getattr(getattr(sale, "product", None), "name", None)),
                    _safe(getattr(getattr(sale, "type_of_good", None), "name", None)),
                    _safe(getattr(sale, "price", None)),
                ]
            )

        for feedback in getattr(recap, "consumer_feedback", []).all():
            consumer_feedback_sheet.append(
                [
                    _safe(getattr(recap, "uuid", None)),
                    _safe(getattr(recap, "name", None)),
                    _safe(getattr(feedback, "demographics", None)),
                    _safe(getattr(feedback, "feedback", None)),
                    _safe(getattr(feedback, "quotes", None)),
                    _safe(getattr(feedback, "positive_stories", None)),
                    _safe(getattr(feedback, "reasons_to_decline", None)),
                ]
            )

        for feedback in getattr(recap, "account_feedback", []).all():
            account_feedback_sheet.append(
                [
                    _safe(getattr(recap, "uuid", None)),
                    _safe(getattr(recap, "name", None)),
                    _safe(getattr(feedback, "do_differently_feedback", None)),
                    _safe(getattr(feedback, "feedback", None)),
                    _safe(getattr(feedback, "corpo_card", None)),
                ]
            )

        for recap_file in getattr(recap, "recap_files", []).all():
            file_path = getattr(recap_file, "file", None)
            signed_url = ""
            blob_name = extract_blob_name_from_url(str(file_path)) if file_path else None
            if blob_name:
                signed_url = generate_download_url(
                    blob_name, expiration_minutes=SIGNED_URL_EXPIRATION_MINUTES
                )
            display_name = getattr(recap_file, "name", None) or ""
            if not display_name and file_path:
                try:
                    display_name = str(file_path).rsplit("/", 1)[-1]
                except Exception:
                    display_name = ""
            files_sheet.append(
                [
                    _safe(getattr(recap, "uuid", None)),
                    _safe(getattr(recap, "name", None)),
                    _safe(getattr(recap_file, "name", None)),
                    _safe(getattr(getattr(recap_file, "file_type", None), "name", None)),
                    _safe(
                        getattr(
                            getattr(recap_file, "file_recap_category", None),
                            "name",
                            None,
                        )
                    ),
                    bool(getattr(recap_file, "approved", False)),
                    display_name or signed_url,
                ]
            )
            if signed_url:
                cell = files_sheet.cell(row=files_sheet.max_row, column=7)
                cell.hyperlink = signed_url
                cell.style = "Hyperlink"

    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return output.read()
