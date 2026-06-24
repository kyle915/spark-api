import strawberry
from strawberry import relay
from graphql import GraphQLError
from asgiref.sync import sync_to_async
from typing import Any
from decimal import Decimal
import logging
import re

from django.contrib.auth import get_user_model
from django.db.models import Model, Prefetch, Q
from django.db import transaction
from django.utils import timezone as django_timezone
from django.conf import settings
from django.utils.text import slugify

from recaps import types
from recaps import models
from recaps import inputs
from recaps import heic_conversion
from recaps.envelopes import (
    RecapApprovedNotificationMailer,
    RecapReadyForReviewAdminMailer,
)
from recaps.queries import RecapQueriesService, CustomRecapQueriesService
from ambassadors.models import FileType, Ambassador, Attendance
from events.models import Event, Retailer, Location, State, TimeZone, EventType
from jobs.models import Job, AmbassadorJob
from tenants.models import Role, TenantedUser, Tenant
from utils.graphql.inputs import SparkGraphQLInput
from utils.graphql.permissions import (
    StrictIsAuthenticated,
    resolve_request_user_access,
    _is_admin_access,
)
from utils.graphql.relay import ensure_relay_mutation
from utils.graphql.mixins import SparkGraphQLMixin, resolve_id_to_int
from utils.utils import ROLE_ID, build_mutation_response
from utils.gcs import (
    public_url,
    extract_blob_name_from_url,
    delete_blob,
    upload_bytes,
    download_blob_bytes,
    generate_download_url,
    get_gcs_client,
)
from utils.onesignal import OneSignalError, one_signal_client
from utils.cloud_tasks import enqueue
from recaps.pdf import (
    build_recap_pdf,
    build_campaign_report_pdf,
    should_embed_recap_file,
    is_image_bytes,
    IMAGE_EXTENSIONS,
)
from recaps.excel import build_recaps_xlsx

ensure_relay_mutation()


# Positional file-category sentinels sent by the upload widgets (web + mobile).
# These are NOT database PKs — they are stable *role* markers baked into the
# clients: "1" = the sampling photos slot, "2" = the receipts slot. They must
# resolve to the uploading tenant's OWN category that plays that role, found by
# its seeded NAME, never by raw PK. (Default categories are seeded per-tenant in
# the order ["Sampling photos", "Table setup", "Receipts"], so PK 2 happens to
# be "Table setup" — treating sentinel "2" as a PK is exactly what mis-filed
# receipts into "Table setup".) Names anchor on tenants.mutations'
# DEFAULT_FILE_RECAP_CATEGORIES so the two never drift.
_PHOTOS_CATEGORY_NAME = "Sampling photos"
_RECEIPTS_CATEGORY_NAME = "Receipts"
_FILE_CATEGORY_SENTINEL_NAMES = {
    "1": _PHOTOS_CATEGORY_NAME,
    "2": _RECEIPTS_CATEGORY_NAME,
}

# Keyword fallbacks for tenants whose role category isn't named the exact
# seeded default. A tenant onboarded with a CUSTOM recap template can label its
# receipt bucket "Receipt", "Upload Receipt", "Product Purchase Receipt", etc.
# — none of which match name__iexact="Receipts" — so the receipt sentinel "2"
# used to fall through to the PK fallback and mis-file into "Table setup" (the
# Girl Beer report). Matching the role by case-insensitive keyword lands the
# file in the right bucket regardless of the exact label.
_FILE_CATEGORY_SENTINEL_KEYWORDS = {
    "1": ("photo",),
    "2": ("receipt",),
}

# Anchor the sentinel role names on the seeded defaults so a rename of the
# tenant seeds can't silently break sentinel resolution. (Local import keeps the
# tenants.mutations dependency lazy and one-directional.)
def _assert_sentinel_names_match_seeds():
    from tenants.mutations import DEFAULT_FILE_RECAP_CATEGORIES

    seeded = {name.lower() for name in DEFAULT_FILE_RECAP_CATEGORIES}
    for role_name in _FILE_CATEGORY_SENTINEL_NAMES.values():
        assert role_name.lower() in seeded, (
            f"File-category sentinel role {role_name!r} is no longer a seeded "
            f"default ({DEFAULT_FILE_RECAP_CATEGORIES}); update "
            "_FILE_CATEGORY_SENTINEL_NAMES in recaps.mutations to match."
        )


_assert_sentinel_names_match_seeds()


def _resolve_file_recap_category(raw_id, *, tenant_id):
    """Resolve a FileRecapCategory for an uploaded recap file — tenant-scoped,
    semantic, and graceful.

    FileRecapCategory rows are PER-TENANT, but the upload widgets (web + mobile)
    send a stable *positional* sentinel — "1" = photos, "2" = receipts — that is
    a ROLE marker, not a DB PK. The old code matched the sentinel as a raw PK
    (own tenant's exact PK first, then the global row's name): because defaults
    are seeded ["Sampling photos", "Table setup", "Receipts"], the global PK 2
    is "Table setup", so a receipt sentinel "2" landed under "Table setup"
    instead of "Receipts".

    Resolution order:
      1. If `raw_id` is a known positional sentinel ("1"/"2"), resolve it to the
         tenant's OWN category — exact seeded role NAME, then role keyword, and
         if the tenant has no matching category at all, CREATE the tenant's own
         role category (self-heal). A sentinel never resolves cross-tenant.
      2. Otherwise (a real explicit category id, e.g. one the user picked in the
         category-management UI): the tenant's own row with that exact PK, then
         the tenant's row sharing that row's name, else None — never another
         tenant's row. (Only a tenantless call may return the raw global row.)

    Never raises — a stray category id must not lose the recap or its files.
    """
    if raw_id in (None, ""):
        return None

    # Positional sentinel -> tenant's own category by seeded role name. We match
    # on the *string* sentinel the clients actually send ("1"/"2"); only fall
    # through to PK behavior when there is no name match for that tenant.
    sentinel = str(raw_id).strip()
    sentinel_name = _FILE_CATEGORY_SENTINEL_NAMES.get(sentinel)
    if sentinel_name is not None and tenant_id is not None:
        # 1) Exact seeded role name (fast path for tenants on the defaults).
        by_name = models.FileRecapCategory.objects.filter(
            tenant_id=tenant_id, name__iexact=sentinel_name
        ).first()
        if by_name is not None:
            return by_name
        # 2) Naming variant (custom-template tenants like Girl Beer): match the
        #    role by keyword so a receipt sentinel still lands on a category
        #    named "Receipt" / "Upload Receipt" / "Product Purchase Receipt"
        #    rather than mis-filing into "Table setup" via the PK fallback
        #    below. Tenant-scoped; lowest id wins on ties.
        for keyword in _FILE_CATEGORY_SENTINEL_KEYWORDS.get(sentinel, ()):
            by_keyword = (
                models.FileRecapCategory.objects.filter(
                    tenant_id=tenant_id, name__icontains=keyword
                )
                .order_by("id")
                .first()
            )
            if by_keyword is not None:
                return by_keyword
        # 3) No matching role category for this tenant at all — SELF-HEAL:
        #    create the tenant's own role category under the seeded default
        #    name instead of falling through to the PK path. The PK path could
        #    only land the file in ANOTHER tenant's category (the Girl Beer
        #    leak: a tenant onboarded outside createTenant has no categories,
        #    so sentinel "2" fell through to the global PK-2 "Table setup"
        #    owned by a different tenant). Sentinels are role markers; they
        #    must never resolve cross-tenant.
        seeded, _created = models.FileRecapCategory.objects.get_or_create(
            tenant_id=tenant_id, name=sentinel_name
        )
        return seeded

    try:
        category_id = resolve_id_to_int(raw_id)
    except (TypeError, ValueError, GraphQLError):
        return None
    if category_id is None:
        return None
    global_cat = models.FileRecapCategory.objects.filter(id=category_id).first()
    if tenant_id is not None:
        own = models.FileRecapCategory.objects.filter(
            tenant_id=tenant_id, id=category_id
        ).first()
        if own is not None:
            return own
        if global_cat is not None:
            same_name = models.FileRecapCategory.objects.filter(
                tenant_id=tenant_id, name__iexact=global_cat.name
            ).first()
            if same_name is not None:
                return same_name
        # An explicit id that is neither the tenant's own row nor name-mappable
        # onto one resolves to None (uncategorized) — never another tenant's
        # category. A cross-tenant category renders fine in the UI (views group
        # by the file's category), which is exactly why this leak went unseen.
        return None
    return global_cat

User = get_user_model()
logger = logging.getLogger(__name__)


async def _resolve_recap_pdf_attachment(
    recap: models.Recap | models.CustomRecap,
) -> list[dict] | None:
    """If the recap has a generated PDF (CustomRecapFile with .pdf
    extension or RecapFile equivalent), return an `attachments` list
    shaped for the Mailer. Returns None when no PDF exists or the
    blob fetch fails — caller falls back to a link-only email.
    """
    def _find_blob() -> tuple[str, str] | None:
        try:
            if isinstance(recap, models.CustomRecap):
                qs = recap.custom_recap_files.filter(
                    file_type__extension__iexact=".pdf"
                ) | recap.custom_recap_files.filter(
                    file_type__extension__iexact="pdf"
                )
                pdf = qs.order_by("-id").first()
            else:
                qs = recap.recap_files.filter(
                    file_type__extension__iexact=".pdf"
                ) | recap.recap_files.filter(
                    file_type__extension__iexact="pdf"
                )
                pdf = qs.order_by("-id").first()
            if not pdf:
                return None
            blob = extract_blob_name_from_url(str(pdf.url)) or str(pdf.url)
            return blob, (pdf.name or f"recap-{recap.uuid}.pdf")
        except Exception:
            return None

    found = await sync_to_async(_find_blob)()
    if not found:
        return None
    blob_name, friendly_name = found
    try:
        pdf_bytes = await sync_to_async(download_blob_bytes)(blob_name)
    except Exception as exc:
        logger.warning(
            "Could not fetch PDF blob %s for recap %s: %s",
            blob_name,
            recap.id,
            exc,
        )
        return None
    if not pdf_bytes:
        return None
    safe_name = friendly_name if friendly_name.lower().endswith(".pdf") else f"{friendly_name}.pdf"
    return [
        {
            "filename": safe_name,
            "content": pdf_bytes,
            "content_type": "application/pdf",
        }
    ]


async def _resolve_recap_requestor_recipients(
    recap: models.Recap | models.CustomRecap,
) -> list[tuple[str, str]]:
    """Pull the original request's requestor (created_by + the
    requestor_email override). Returns a list of (email, first_name)
    tuples, deduped, ready to merge into the approval recipient set.
    """
    def _collect() -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        try:
            req = getattr(recap.event, "request", None)
        except Exception:
            req = None
        if not req:
            return out

        def add(email: str | None, first: str | None):
            e = (email or "").strip()
            if not e or e.lower() in seen:
                return
            seen.add(e.lower())
            out.append((e, (first or "").strip()))

        # `requestor_email` is the public-form override — wins if set.
        add(getattr(req, "requestor_email", None), None)
        # Authenticated creator (admin/client portal submission).
        cb = getattr(req, "created_by", None)
        if cb:
            add(getattr(cb, "email", None), getattr(cb, "first_name", None))
        return out

    return await sync_to_async(_collect)()


async def _notify_recap_approved_to_rmm_or_clients(
    recap: models.Recap | models.CustomRecap,
) -> None:
    event = recap.event
    rmm_user = getattr(event, "rmm_asigned", None)
    fallback_reply_to = "events@igniteproductions.co"
    reply_to_email = (
        getattr(rmm_user, "email", None) or ""
    ).strip() or fallback_reply_to

    # Build recipient list: the event's RMM (if assigned) + the tenant's
    # client contacts (always) + the original requestor (request.created_by
    # + requestor_email). Per-row dedupe so an address matching more than
    # one role still gets a single email.
    recipients: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _push(email: str | None, first: str | None):
        e = (email or "").strip()
        if not e or e.lower() in seen:
            return
        seen.add(e.lower())
        recipients.append((e, (first or "").strip()))

    if rmm_user and rmm_user.email:
        _push(rmm_user.email, rmm_user.first_name)

    # Always include the tenant's client contacts (the brand) — not only as
    # an RMM fallback. The client should receive the approved recap whether
    # or not an RMM is assigned, and regardless of how the BA/recap was
    # created (e.g. an admin manually filing for an externally-staffed BA).
    client_rows = await sync_to_async(list)(
        TenantedUser.objects.filter(
            tenant_id=event.tenant_id,
            is_active=True,
            user__role__slug=Role.CLIENT_SLUG,
        ).values("user__email", "user__first_name")
    )
    for row in client_rows:
        _push(row.get("user__email"), row.get("user__first_name"))

    # Add the tenant's explicitly-configured recap recipients. Brands
    # without a client-role user still need the approved recap to reach
    # a human, so staff can list addresses on Tenant.recap_recipient_emails
    # (comma/newline/semicolon-separated). Parsed best-effort and deduped
    # through the same _push() as the RMM/client/requestor rows.
    configured = await sync_to_async(
        lambda: Tenant.objects.filter(id=event.tenant_id)
        .values_list("recap_recipient_emails", flat=True)
        .first()
    )()
    for token in re.split(r"[,\n;]+", configured or ""):
        candidate = token.strip()
        if "@" in candidate and "." in candidate:
            _push(candidate, None)

    # Add the original requestor — same activation owner the admin
    # CC's on the request approval email. Closes the loop: requestor
    # → request approved → recap filed → recap approved.
    for email, first in await _resolve_recap_requestor_recipients(recap):
        _push(email, first)

    if not recipients:
        return

    # Resolve PDF once and reuse — saves one GCS fetch per recipient.
    attachments = await _resolve_recap_pdf_attachment(recap)

    for email, first_name in recipients:
        mailer = RecapApprovedNotificationMailer(
            recap=recap,
            to_emails=[email],
            recipient_first_name=first_name or None,
            reply_to_email=reply_to_email,
            attachments=attachments,
        )
        try:
            await sync_to_async(mailer.send)()
        except Exception:
            logger.exception(
                "Failed to send recap-approved email to %s for recap=%s",
                email,
                getattr(recap, "id", None),
            )


async def _notify_recap_approved_to_ambassador_by_push(
    recap: models.Recap,
) -> None:
    ambassador = getattr(recap, "ambassador", None)
    user = getattr(ambassador, "user", None)
    if not user:
        return

    deep_link = f"spark://recaps/{recap.id}"

    try:
        await one_signal_client.send_push(
            external_ids=[str(user.uuid)],
            title="Recap approved",
            message=f"Your recap for {recap.name} was approved.",
            url=deep_link,
            data={
                "type": "recap_approved",
                "recap_id": str(recap.id),
                "deep_link": deep_link,
            },
        )
    except OneSignalError as exc:
        logger.warning(
            "Failed to send OneSignal recap approval push for recap=%s: %s",
            recap.id,
            exc,
        )


async def _notify_recap_ready_for_review_to_admins(
    recap: models.Recap | models.CustomRecap,
    created_by: User | None,
) -> None:
    if not created_by:
        return

    role_slug = await sync_to_async(
        lambda: User.objects.filter(id=created_by.id)
        .values_list("role__slug", flat=True)
        .first()
    )()
    role_slug = (role_slug or "").strip()

    if role_slug == Role.AMBASSADOR_SLUG:
        # BA filed it from the app → notify the admin review list (the
        # original behavior). Recipients come from RECAP_REVIEW_COPY_EMAILS.
        recipients = [
            email.strip()
            for email in getattr(settings, "RECAP_REVIEW_COPY_EMAILS", [])
            if (email or "").strip()
        ]
    else:
        # An admin filed the recap on a BA's behalf. The review list doesn't
        # need a "ready for review" alert (an admin already handled it), but
        # the admin who filed it still expects a confirmation. Scope the
        # email to that filer ONLY — never a team broadcast — so this can't
        # flood the review list on imports / multi-recap entry.
        filer_email = (getattr(created_by, "email", "") or "").strip()
        recipients = [filer_email] if filer_email else []

    if not recipients:
        return

    # Name shown in the email is the BA the recap is FOR (linked ambassador
    # or write-in external BA), falling back to the creator's own name.
    def _ba_label() -> str:
        try:
            amb = getattr(recap, "ambassador", None)
            user = getattr(amb, "user", None) if amb else None
            if user:
                full = (user.get_full_name() or "").strip()
                if full:
                    return full
        except Exception:
            pass
        return (getattr(recap, "external_ba_name", "") or "").strip()

    ba_name = await sync_to_async(_ba_label)()
    if not ba_name:
        ba_name = (
            created_by.get_full_name().strip()
            if hasattr(created_by, "get_full_name")
            else ""
        ) or created_by.email

    mailer = RecapReadyForReviewAdminMailer(
        recap=recap,
        to_emails=recipients,
        ambassador_name=ba_name,
    )
    # Best-effort: a mail failure must never break recap creation/approval.
    try:
        await sync_to_async(mailer.send)()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "recap ready-for-review email failed for recap %s: %s",
            getattr(recap, "id", None),
            exc,
        )


class RecapMutationService(SparkGraphQLMixin):
    """Service for recap mutations."""

    input: SparkGraphQLInput | None = None
    info: strawberry.Info | None = None
    user: User | None = None

    @classmethod
    def with_input(cls, input: SparkGraphQLInput) -> "RecapMutationService":
        """Create a new instance of the service with the input."""
        service = cls()
        service.set_input(input)
        return service

    def set_input(self, input: SparkGraphQLInput) -> "RecapMutationService":
        """Set the input for the service."""
        self.input = input
        return self

    async def set_user(self, info: strawberry.Info) -> "RecapMutationService":
        """Set the user for the service."""
        self.info = info
        self.user = await self.get_user(info)
        return self

    @staticmethod
    def _has_complete_consumer_engagements(
        consumer_engagements: inputs.ConsumerEngagementsInput | None,
    ) -> bool:
        if consumer_engagements is None:
            return False
        return all(
            value is not None
            for value in (
                consumer_engagements.total_consumer,
                consumer_engagements.first_time_consumers,
                consumer_engagements.brand_aware_consumers,
                consumer_engagements.willing_to_purchase_consumers,
                consumer_engagements.not_willing_consumers,
            )
        )

    @staticmethod
    def _has_any_consumer_engagements(
        consumer_engagements: inputs.ConsumerEngagementsInput | None,
    ) -> bool:
        if consumer_engagements is None:
            return False
        return any(
            value is not None
            for value in (
                consumer_engagements.total_consumer,
                consumer_engagements.first_time_consumers,
                consumer_engagements.brand_aware_consumers,
                consumer_engagements.willing_to_purchase_consumers,
                consumer_engagements.not_willing_consumers,
            )
        )

    @staticmethod
    def _has_complete_product_sample(
        product_sample: inputs.ProductSampleInput | None,
    ) -> bool:
        if product_sample is None:
            return False
        return (
            product_sample.product_id not in (None, "")
            and product_sample.quantity is not None
        )

    @classmethod
    def _has_complete_product_samples(
        cls,
        product_samples: list[inputs.ProductSampleInput] | None,
    ) -> bool:
        return bool(
            product_samples
            and any(
                cls._has_complete_product_sample(sample) for sample in product_samples
            )
        )

    @staticmethod
    def _has_complete_sales_performance(
        sale: inputs.SalesPerformanceInput | None,
    ) -> bool:
        if sale is None:
            return False
        return (
            sale.product_id not in (None, "")
            and sale.type_of_good_id not in (None, "")
            and sale.price is not None
        )

    @classmethod
    def _has_complete_sales_performance_items(
        cls,
        sales_performance: list[inputs.SalesPerformanceInput] | None,
    ) -> bool:
        return bool(
            sales_performance
            and any(
                cls._has_complete_sales_performance(sale) for sale in sales_performance
            )
        )

    def _is_recap_fully_completed(self) -> bool:
        """Validate that recap input includes all optional sections and files."""
        if not isinstance(self.input, inputs.CreateRecapInput):
            return False
        if self.input.incomplete is True:
            return False

        has_files = bool(self.input.files and len(self.input.files) > 0)
        has_metrics = all(
            value is not None
            for value in (
                self.input.products_sold,
                self.input.total_cans_sold,
                self.input.total_packs_sold,
                self.input.total_earnings,
                self.input.account_spend_amount,
            )
        )
        has_consumer_engagements = self._has_complete_consumer_engagements(
            self.input.consumer_engagements
        )
        has_product_samples = self._has_complete_product_samples(
            self.input.product_samples
        )
        has_sales_performance = self._has_complete_sales_performance_items(
            self.input.sales_performance
        )
        has_consumer_feedback = self.input.consumer_feedback is not None and all(
            bool((value or "").strip())
            for value in (
                self.input.consumer_feedback.demographics,
                self.input.consumer_feedback.feedback,
                self.input.consumer_feedback.quotes,
                self.input.consumer_feedback.positive_stories,
                self.input.consumer_feedback.reasons_to_decline,
            )
        )
        has_account_feedback = self.input.account_feedback is not None and all(
            bool((value or "").strip())
            for value in (
                self.input.account_feedback.do_differently_feedback,
                self.input.account_feedback.feedback,
                self.input.account_feedback.corpo_card,
            )
        )

        return all(
            (
                has_files,
                has_metrics,
                has_consumer_engagements,
                has_product_samples,
                has_sales_performance,
                has_consumer_feedback,
                has_account_feedback,
            )
        )

    @staticmethod
    def _resolve_custom_recap_template_field_input(
        field_input: inputs.CustomRecapTemplateFieldInput,
        *,
        allow_id: bool,
    ) -> tuple[int | None, int, int]:
        custom_field_id = None
        if field_input.id not in (None, ""):
            if not allow_id:
                raise GraphQLError(
                    "Custom field ID cannot be provided when creating a template."
                )
            try:
                custom_field_id = resolve_id_to_int(field_input.id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError(f"Invalid custom field ID: {field_input.id}")

        try:
            custom_field_type_id = resolve_id_to_int(field_input.custom_field_type_id)
        except (TypeError, ValueError, GraphQLError):
            raise GraphQLError(
                f"Invalid custom field type ID: {field_input.custom_field_type_id}"
            )

        try:
            recap_section_id = resolve_id_to_int(field_input.recap_section_id)
        except (TypeError, ValueError, GraphQLError):
            raise GraphQLError(
                f"Invalid recap section ID: {field_input.recap_section_id}"
            )

        return custom_field_id, custom_field_type_id, recap_section_id

    def _create_custom_recap_template_fields(
        self,
        custom_recap_template: models.CustomRecapTemplate,
        field_inputs: list[inputs.CustomRecapTemplateFieldInput] | None,
    ) -> None:
        if not field_inputs:
            return

        for idx, field_input in enumerate(field_inputs):
            (
                custom_field_id,
                custom_field_type_id,
                recap_section_id,
            ) = self._resolve_custom_recap_template_field_input(
                field_input,
                allow_id=False,
            )
            if custom_field_id is not None:
                raise GraphQLError(
                    "Custom field ID cannot be provided when creating a template."
                )

            custom_field_type = models.CustomRecapFieldType.objects.filter(
                id=custom_field_type_id
            ).first()
            if not custom_field_type:
                raise GraphQLError("Custom field type not found.")

            recap_section = models.RecapSection.objects.filter(
                id=recap_section_id
            ).first()
            if not recap_section:
                raise GraphQLError("Recap section not found.")

            if recap_section.tenant_id != custom_recap_template.tenant_id:
                raise GraphQLError(
                    "Recap section does not belong to the template tenant."
                )

            models.CustomField.objects.create(
                name=field_input.name,
                custom_recap_template=custom_recap_template,
                custom_field_type=custom_field_type,
                recap_section=recap_section,
                created_by=self.user,
                required=bool(field_input.required),
                options=list(field_input.options or []),
                order=(
                    field_input.order if field_input.order is not None else idx
                ),
            )

    def _sync_custom_recap_template_fields(
        self,
        custom_recap_template: models.CustomRecapTemplate,
        field_inputs: list[inputs.CustomRecapTemplateFieldInput] | None,
    ) -> None:
        if field_inputs is None:
            return

        existing_fields = {
            field.id: field
            for field in models.CustomField.objects.filter(
                custom_recap_template=custom_recap_template
            )
        }
        final_field_ids: set[int] = set()

        for idx, field_input in enumerate(field_inputs):
            (
                custom_field_id,
                custom_field_type_id,
                recap_section_id,
            ) = self._resolve_custom_recap_template_field_input(
                field_input,
                allow_id=True,
            )
            order_val = (
                field_input.order if field_input.order is not None else idx
            )

            custom_field_type = models.CustomRecapFieldType.objects.filter(
                id=custom_field_type_id
            ).first()
            if not custom_field_type:
                raise GraphQLError("Custom field type not found.")

            recap_section = models.RecapSection.objects.filter(
                id=recap_section_id
            ).first()
            if not recap_section:
                raise GraphQLError("Recap section not found.")

            if recap_section.tenant_id != custom_recap_template.tenant_id:
                raise GraphQLError(
                    "Recap section does not belong to the template tenant."
                )

            if custom_field_id is None:
                custom_field = models.CustomField.objects.create(
                    name=field_input.name,
                    custom_recap_template=custom_recap_template,
                    custom_field_type=custom_field_type,
                    recap_section=recap_section,
                    created_by=self.user,
                    required=bool(field_input.required),
                    options=list(field_input.options or []),
                    order=order_val,
                )
                final_field_ids.add(custom_field.id)
                continue

            if custom_field_id in final_field_ids:
                raise GraphQLError("Duplicate custom field ID in input.")

            custom_field = existing_fields.get(custom_field_id)
            if not custom_field:
                raise GraphQLError("Custom field not found for this template.")

            custom_field.name = field_input.name
            custom_field.custom_field_type = custom_field_type
            custom_field.recap_section = recap_section
            custom_field.updated_by = self.user
            if field_input.required is not None:
                custom_field.required = field_input.required
            if field_input.options is not None:
                custom_field.options = list(field_input.options)
            custom_field.order = order_val
            custom_field.save()
            final_field_ids.add(custom_field.id)

        custom_field_ids_to_delete = [
            field_id for field_id in existing_fields if field_id not in final_field_ids
        ]
        if not custom_field_ids_to_delete:
            return

        if models.CustomFieldValue.objects.filter(
            custom_field_id__in=custom_field_ids_to_delete
        ).exists():
            raise GraphQLError(
                "Cannot remove custom fields that already have submitted values."
            )

        models.CustomField.objects.filter(id__in=custom_field_ids_to_delete).delete()

    @staticmethod
    def _normalize_attendance_slug(slug: str | None) -> str:
        return (slug or "").strip().lower().replace("-", "_")

    async def _apply_time_based_recap_payment_rule(
        self,
        *,
        event: Event,
        job: Job | None,
        ambassador: Ambassador | None,
    ) -> None:
        """
        Rule:
        - If recap is 100% complete:
          - worked time <= 49% of event duration: set real_amount to 40% of rate.
          - worked time >= 50% of event duration: set real_amount to 65% of rate.
        - If recap is incomplete and worked time is 100% (or more):
          - set real_amount to 85% of rate.
        """
        if not ambassador or not job:
            return

        is_full_recap = self._is_recap_fully_completed()

        if (
            not event.start_time
            or not event.end_time
            or event.end_time <= event.start_time
        ):
            return

        attendance_filters = Q(ambassador=ambassador, event=event)
        if job:
            attendance_filters &= Q(job=job)

        attendances = await sync_to_async(list)(
            Attendance.objects.select_related("attendace_type")
            .filter(attendance_filters)
            .order_by("clock_time")
        )
        if not attendances:
            return

        clock_in_times = [
            record.clock_time
            for record in attendances
            if self._normalize_attendance_slug(
                getattr(record.attendace_type, "slug", None)
            )
            == "clock_in"
        ]
        clock_out_times = [
            record.clock_time
            for record in attendances
            if self._normalize_attendance_slug(
                getattr(record.attendace_type, "slug", None)
            )
            == "clock_out"
        ]

        event_minutes = int((event.end_time - event.start_time).total_seconds() // 60)
        if event_minutes <= 0:
            return

        # Business rule: no clock-in means 0% worked time.
        if not clock_in_times:
            worked_percentage = 0
        elif not clock_out_times:
            worked_percentage = 0
        else:
            worked_minutes = int(
                (max(clock_out_times) - min(clock_in_times)).total_seconds() // 60
            )
            if worked_minutes <= 0:
                worked_percentage = 0
            else:
                worked_ratio = worked_minutes / event_minutes
                worked_percentage = worked_ratio * 100

        ambassador_job = await sync_to_async(
            lambda: AmbassadorJob.objects.select_related("rate")
            .filter(ambassador=ambassador, job=job)
            .order_by("-created_at")
            .first()
        )()
        if (
            not ambassador_job
            or not ambassador_job.rate
            or ambassador_job.rate.amount is None
        ):
            return

        if is_full_recap:
            if worked_percentage >= 100:
                ambassador_job.real_amount = ambassador_job.rate.amount
            elif worked_percentage <= 49:
                ambassador_job.real_amount = ambassador_job.rate.amount * Decimal(
                    "0.40"
                )
            elif worked_percentage >= 50:
                ambassador_job.real_amount = ambassador_job.rate.amount * Decimal(
                    "0.65"
                )
            else:
                return
        elif worked_percentage >= 100:
            ambassador_job.real_amount = ambassador_job.rate.amount * Decimal("0.85")
        else:
            return

        await sync_to_async(ambassador_job.save)(
            update_fields=["real_amount", "updated_at"]
        )

    async def create_recap(self) -> models.Recap:
        """Create a recap with multiple files."""
        if not isinstance(self.input, inputs.CreateRecapInput):
            raise GraphQLError("Invalid input type.")
        # This mutation currently only accepts CreateRecapInput.
        is_mobile_input = False

        # Validate event exists
        try:
            event_id = resolve_id_to_int(self.input.event_id)
            event = await sync_to_async(Event.objects.get)(id=event_id)
        except (Event.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Event not found.")

        job = None
        if self.input.job_id:
            try:
                job_id = resolve_id_to_int(self.input.job_id)
                job = await sync_to_async(Job.objects.get)(id=job_id)
            except (Job.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Job not found.")

        # Event-derived defaults for retailer/location/state when the caller
        # omits them. The mobile path already inherited these from the event;
        # we now apply the SAME defaulting to the admin/internal create path so
        # an internally-created recap (the "Log event" → recap flow) doesn't
        # persist a null retailer/state — which is what made the recap PDF show
        # "State / Retailer: N/A" for internally-created recaps (the PDF custom
        # branch reads recap.state / recap.retailer directly). Read off the
        # event inside sync_to_async (its FK chain isn't select_related here).
        @sync_to_async
        def _event_defaults():
            ev_retailer = getattr(event, "retailer", None)
            ev_location = getattr(event, "location", None)
            if ev_location is None and ev_retailer is not None:
                ev_location = getattr(ev_retailer, "location", None)
            ev_state = getattr(event, "state", None)
            if ev_state is None and ev_location is not None:
                ev_state = getattr(ev_location, "state", None)
            return ev_retailer, ev_location, ev_state

        event_retailer, event_location, event_state = await _event_defaults()

        retailer = None
        if is_mobile_input:
            retailer = event_retailer
        elif self.input.retailer_id:
            try:
                retailer_id = resolve_id_to_int(self.input.retailer_id)
                retailer = await sync_to_async(Retailer.objects.get)(id=retailer_id)
            except (Retailer.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Retailer not found.")
        else:
            retailer = event_retailer

        ambassador = None
        if self.input.ambassador_id:
            try:
                ambassador_id = resolve_id_to_int(self.input.ambassador_id)
                ambassador = await sync_to_async(Ambassador.objects.get)(
                    id=ambassador_id
                )
            except (Ambassador.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Ambassador not found.")

        location = None
        if is_mobile_input:
            location = event_location
            if location is None:
                location = getattr(retailer, "location", None) if retailer else None
        elif self.input.location_id:
            try:
                location_id = resolve_id_to_int(self.input.location_id)
                location = await sync_to_async(Location.objects.get)(id=location_id)
            except (Location.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Location not found.")
        else:
            location = event_location

        state = None
        if is_mobile_input:
            state = event_state
            if state is None:
                state = getattr(location, "state", None) if location else None
        elif self.input.state_id:
            try:
                state_id = resolve_id_to_int(self.input.state_id)
                state = await sync_to_async(State.objects.get)(id=state_id)
            except (State.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("State not found.")
        else:
            state = event_state

        if not self.input.files or len(self.input.files) == 0:
            raise GraphQLError("At least one file is required.")

        # Use transaction to ensure atomicity
        @sync_to_async
        def create_recap_with_files():
            with transaction.atomic():
                # Create RecapFile instances for each file
                recap_files = []
                for file_input in self.input.files:
                    file_url = file_input.file
                    # Extract blob name from GCS URL
                    blob_name = extract_blob_name_from_url(file_url)
                    if not blob_name:
                        raise GraphQLError("Invalid recap file path.")

                    file_type = None
                    if file_input.file_type_id not in (None, ""):
                        try:
                            file_type_id = resolve_id_to_int(file_input.file_type_id)
                            file_type = FileType.objects.get(id=file_type_id)
                        except (
                            FileType.DoesNotExist,
                            TypeError,
                            ValueError,
                            GraphQLError,
                        ):
                            raise GraphQLError("File type not found.")

                    # Get default file type (you may want to make this configurable)
                    if not file_type:
                        file_type = FileType.objects.first()
                    if not file_type:
                        raise GraphQLError(
                            "No file type available. Please create a file type first."
                        )

                    file_recap_category = _resolve_file_recap_category(
                        file_input.file_recap_category_id,
                        tenant_id=getattr(event, "tenant_id", None),
                    )

                    recap_file = models.RecapFile(
                        name=f"Recap file for {self.input.name}",
                        file=blob_name,
                        file_type=file_type,
                        file_recap_category=file_recap_category,
                        approved=False,
                        created_by=self.user,
                    )
                    recap_file.save()
                    recap_files.append(recap_file)

                # Create the Recap instance
                total_engagements = None
                if self.input.consumer_engagements is not None:
                    total_engagements = self.input.consumer_engagements.total_consumer

                recap = models.Recap(
                    name=self.input.name,
                    event=event,
                    created_by=self.user,
                    total_engagements=total_engagements,
                    products_sold=self.input.products_sold,
                    total_cans_sold=self.input.total_cans_sold,
                    total_packs_sold=self.input.total_packs_sold,
                    total_earnings=self.input.total_earnings,
                    account_spend_amount=self.input.account_spend_amount,
                    traffic_description=self.input.traffic_description,
                    competitive_presence=self.input.competitive_presence,
                    job=job,
                    retailer=retailer,
                    ambassador=ambassador,
                    location=location,
                    state=state,
                )
                if self.input.filling_for_ambassador is not None:
                    recap.filling_for_ambassador = self.input.filling_for_ambassador
                if self.input.late is not None:
                    recap.late = self.input.late
                if self.input.incomplete is not None:
                    recap.incomplete = self.input.incomplete
                recap.save()

                # Link recap to all recap files
                models.RecapFile.objects.filter(
                    id__in=[recap_file.id for recap_file in recap_files]
                ).update(recap=recap)

                # HEIC sibling generation — for each .heic/.heif file the
                # BA uploaded, kick off a server-side conversion to .jpg
                # and store the result as a sibling RecapFile row pointing
                # at the same recap. Browsers can't render HEIC natively
                # so the recap-list hero picker prefers the .jpg variant
                # and shows it without the slow in-browser libheif WASM
                # fallback. Best-effort — a failure on any single file
                # logs + keeps the HEIC alone, never aborts the upload.
                for heic_rf in recap_files:
                    if not heic_conversion.is_heic_blob(str(heic_rf.file)):
                        continue
                    heic_conversion.ensure_jpg_sibling(
                        heic_blob_name=str(heic_rf.file),
                        recap_id=recap.id,
                        file_type=heic_rf.file_type,
                        file_recap_category=heic_rf.file_recap_category,
                        created_by=self.user,
                    )

                # Create related objects
                if self._has_any_consumer_engagements(self.input.consumer_engagements):
                    models.ConsumerEngagements.objects.create(
                        recap=recap,
                        created_by=self.user,
                        total_consumer=self.input.consumer_engagements.total_consumer,
                        first_time_consumers=self.input.consumer_engagements.first_time_consumers,
                        brand_aware_consumers=self.input.consumer_engagements.brand_aware_consumers,
                        willing_to_purchase_consumers=self.input.consumer_engagements.willing_to_purchase_consumers,
                        not_willing_consumers=self.input.consumer_engagements.not_willing_consumers,
                    )

                if self.input.product_samples:
                    for sample in self.input.product_samples:
                        if not self._has_complete_product_sample(sample):
                            continue
                        try:
                            product_id = resolve_id_to_int(sample.product_id)
                            models.ProductSamples.objects.create(
                                recap=recap,
                                created_by=self.user,
                                product_id=product_id,
                                quantity=sample.quantity,
                            )
                        except (TypeError, ValueError, GraphQLError):
                            raise GraphQLError(
                                f"Invalid product ID: {sample.product_id}"
                            )

                if self.input.sales_performance:
                    for sale in self.input.sales_performance:
                        if not self._has_complete_sales_performance(sale):
                            continue
                        try:
                            product_id = resolve_id_to_int(sale.product_id)
                            type_of_good_id = resolve_id_to_int(sale.type_of_good_id)
                            models.SalesPerformance.objects.create(
                                recap=recap,
                                created_by=self.user,
                                product_id=product_id,
                                type_of_good_id=type_of_good_id,
                                price=sale.price,
                            )
                        except (TypeError, ValueError, GraphQLError):
                            raise GraphQLError(f"Invalid product or type of good ID")

                if self.input.consumer_feedback:
                    models.ConsumerFeedback.objects.create(
                        recap=recap,
                        created_by=self.user,
                        demographics=self.input.consumer_feedback.demographics,
                        feedback=self.input.consumer_feedback.feedback,
                        quotes=self.input.consumer_feedback.quotes,
                        positive_stories=self.input.consumer_feedback.positive_stories,
                        reasons_to_decline=self.input.consumer_feedback.reasons_to_decline,
                    )

                if self.input.account_feedback:
                    models.AccountFeedback.objects.create(
                        recap=recap,
                        created_by=self.user,
                        do_differently_feedback=self.input.account_feedback.do_differently_feedback,
                        feedback=self.input.account_feedback.feedback,
                        corpo_card=self.input.account_feedback.corpo_card,
                        was_corpo_card_used=bool(
                            self.input.account_feedback.was_corpo_card_used
                        ),
                    )

                return recap

        recap = await create_recap_with_files()
        await self._apply_time_based_recap_payment_rule(
            event=event,
            job=job,
            ambassador=ambassador,
        )
        await _notify_recap_ready_for_review_to_admins(recap, self.user)
        return recap

    async def update_recap(self) -> models.Recap:
        """Update a recap."""
        if not isinstance(self.input, inputs.UpdateRecapInput):
            raise GraphQLError("Invalid input type.")
        # This mutation currently only accepts UpdateRecapInput.
        is_mobile_input = False

        try:
            recap_id = resolve_id_to_int(self.input.id)
            recap = await sync_to_async(
                models.Recap.objects.select_related("event").get
            )(id=recap_id)
        except (models.Recap.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Recap not found.")

        # Cross-tenant write gate (value-edit). Scope off the recap's CURRENT
        # tenant (via its event) before applying any caller-supplied changes,
        # so a foreign-tenant caller can't edit this recap by guessing its id.
        await self._assert_caller_authorized_for_recap_tenant(
            recap.event.tenant_id if recap.event_id else None,
            action="update",
        )

        # Validate event exists
        try:
            event_id = resolve_id_to_int(self.input.event_id)
            event = await sync_to_async(Event.objects.get)(id=event_id)
        except (Event.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Event not found.")

        job = None
        if self.input.job_id:
            try:
                job_id = resolve_id_to_int(self.input.job_id)
                job = await sync_to_async(Job.objects.get)(id=job_id)
            except (Job.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Job not found.")

        retailer = None
        if is_mobile_input:
            retailer = getattr(event, "retailer", None)
        elif self.input.retailer_id:
            try:
                retailer_id = resolve_id_to_int(self.input.retailer_id)
                retailer = await sync_to_async(Retailer.objects.get)(id=retailer_id)
            except (Retailer.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Retailer not found.")

        ambassador = None
        if self.input.ambassador_id:
            try:
                ambassador_id = resolve_id_to_int(self.input.ambassador_id)
                ambassador = await sync_to_async(Ambassador.objects.get)(
                    id=ambassador_id
                )
            except (Ambassador.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Ambassador not found.")

        location = None
        if is_mobile_input:
            location = getattr(event, "location", None)
            if location is None:
                location = getattr(retailer, "location", None) if retailer else None
        elif self.input.location_id:
            try:
                location_id = resolve_id_to_int(self.input.location_id)
                location = await sync_to_async(Location.objects.get)(id=location_id)
            except (Location.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Location not found.")

        state = None
        if is_mobile_input:
            state = getattr(event, "state", None)
            if state is None:
                state = getattr(location, "state", None) if location else None
        elif self.input.state_id:
            try:
                state_id = resolve_id_to_int(self.input.state_id)
                state = await sync_to_async(State.objects.get)(id=state_id)
            except (State.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("State not found.")

        if not self.input.files or len(self.input.files) == 0:
            raise GraphQLError("At least one file is required.")

        @sync_to_async
        def update_recap_with_files():
            with transaction.atomic():
                existing_files = list(
                    models.RecapFile.objects.filter(recap=recap).distinct()
                )
                blob_to_file = {
                    extract_blob_name_from_url(str(file.file)): file
                    for file in existing_files
                    if extract_blob_name_from_url(str(file.file))
                }

                final_files: list[models.RecapFile] = []
                for file_input in self.input.files:
                    file_url = file_input.file
                    blob_name = extract_blob_name_from_url(file_url)
                    if not blob_name:
                        raise GraphQLError("Invalid recap file path.")

                    if blob_name in blob_to_file:
                        # Reuse existing file; mark as kept by popping
                        existing_file = blob_to_file.pop(blob_name)
                        updated_fields = []
                        if file_input.file_type_id not in (None, ""):
                            try:
                                file_type_id = resolve_id_to_int(
                                    file_input.file_type_id
                                )
                                file_type = FileType.objects.get(id=file_type_id)
                            except (
                                FileType.DoesNotExist,
                                TypeError,
                                ValueError,
                                GraphQLError,
                            ):
                                raise GraphQLError("File type not found.")
                            if existing_file.file_type_id != file_type.id:
                                existing_file.file_type = file_type
                                updated_fields.append("file_type")

                        if file_input.file_recap_category_id not in (None, ""):
                            try:
                                category_id = resolve_id_to_int(
                                    file_input.file_recap_category_id
                                )
                                file_recap_category = (
                                    models.FileRecapCategory.objects.get(id=category_id)
                                )
                            except (
                                models.FileRecapCategory.DoesNotExist,
                                TypeError,
                                ValueError,
                                GraphQLError,
                            ):
                                raise GraphQLError("File recap category not found.")
                            if (
                                existing_file.file_recap_category_id
                                != file_recap_category.id
                            ):
                                existing_file.file_recap_category = file_recap_category
                                updated_fields.append("file_recap_category")

                        if updated_fields:
                            existing_file.save(update_fields=updated_fields)
                        final_files.append(existing_file)
                        continue

                    file_type = None
                    if file_input.file_type_id not in (None, ""):
                        try:
                            file_type_id = resolve_id_to_int(file_input.file_type_id)
                            file_type = FileType.objects.get(id=file_type_id)
                        except (
                            FileType.DoesNotExist,
                            TypeError,
                            ValueError,
                            GraphQLError,
                        ):
                            raise GraphQLError("File type not found.")
                    if not file_type:
                        file_type = FileType.objects.first()
                    if not file_type:
                        raise GraphQLError("No file type available.")

                    # New file added during an update: resolve through the
                    # shared tenant-aware resolver so positional "1"/"2"
                    # sentinels map to this tenant's photos/receipts by name.
                    file_recap_category = _resolve_file_recap_category(
                        file_input.file_recap_category_id,
                        tenant_id=getattr(event, "tenant_id", None),
                    )

                    recap_file = models.RecapFile(
                        name=f"Recap file for {self.input.name}",
                        file=blob_name,
                        file_type=file_type,
                        file_recap_category=file_recap_category,
                        recap=recap,
                        approved=False,
                        created_by=self.user,
                    )
                    recap_file.save()
                    final_files.append(recap_file)

                removed_files = list(blob_to_file.values())

                # Update the recap
                recap.name = self.input.name
                recap.event = event
                recap.products_sold = self.input.products_sold
                recap.total_cans_sold = self.input.total_cans_sold
                recap.total_packs_sold = self.input.total_packs_sold
                recap.total_earnings = self.input.total_earnings
                recap.account_spend_amount = self.input.account_spend_amount
                recap.traffic_description = self.input.traffic_description
                recap.competitive_presence = self.input.competitive_presence
                if self.input.consumer_engagements is not None:
                    recap.total_engagements = (
                        self.input.consumer_engagements.total_consumer
                    )
                if self.input.job_id is not None:
                    recap.job = job
                if self.input.retailer_id is not None:
                    recap.retailer = retailer
                if self.input.ambassador_id is not None:
                    recap.ambassador = ambassador
                # external_ba_name: only touch when explicitly provided
                # (None means "leave it alone"; pass an empty string to
                # clear). When a real ambassador is picked we also clear
                # any prior external name so the two never disagree.
                if self.input.external_ba_name is not None:
                    val = self.input.external_ba_name.strip()
                    recap.external_ba_name = val or None
                if self.input.ambassador_id is not None and ambassador is not None:
                    recap.external_ba_name = None
                if self.input.location_id is not None:
                    recap.location = location
                if self.input.state_id is not None:
                    recap.state = state
                if self.input.filling_for_ambassador is not None:
                    recap.filling_for_ambassador = self.input.filling_for_ambassador
                if self.input.late is not None:
                    recap.late = self.input.late
                if self.input.incomplete is not None:
                    recap.incomplete = self.input.incomplete
                recap.updated_by = self.user
                recap.save()

                if self.input.consumer_feedback is not None:
                    consumer_feedback = (
                        models.ConsumerFeedback.objects.filter(recap=recap)
                        .order_by("-created_at")
                        .first()
                    )
                    if consumer_feedback:
                        consumer_feedback.demographics = (
                            self.input.consumer_feedback.demographics
                        )
                        consumer_feedback.feedback = (
                            self.input.consumer_feedback.feedback
                        )
                        consumer_feedback.quotes = self.input.consumer_feedback.quotes
                        consumer_feedback.positive_stories = (
                            self.input.consumer_feedback.positive_stories
                        )
                        consumer_feedback.reasons_to_decline = (
                            self.input.consumer_feedback.reasons_to_decline
                        )
                        consumer_feedback.updated_by = self.user
                        consumer_feedback.save(
                            update_fields=[
                                "demographics",
                                "feedback",
                                "quotes",
                                "positive_stories",
                                "reasons_to_decline",
                                "updated_by",
                                "updated_at",
                            ]
                        )
                    else:
                        models.ConsumerFeedback.objects.create(
                            recap=recap,
                            created_by=self.user,
                            demographics=self.input.consumer_feedback.demographics,
                            feedback=self.input.consumer_feedback.feedback,
                            quotes=self.input.consumer_feedback.quotes,
                            positive_stories=self.input.consumer_feedback.positive_stories,
                            reasons_to_decline=self.input.consumer_feedback.reasons_to_decline,
                        )

                if self.input.account_feedback is not None:
                    account_feedback = (
                        models.AccountFeedback.objects.filter(recap=recap)
                        .order_by("-created_at")
                        .first()
                    )
                    if account_feedback:
                        account_feedback.do_differently_feedback = (
                            self.input.account_feedback.do_differently_feedback
                        )
                        account_feedback.feedback = self.input.account_feedback.feedback
                        account_feedback.corpo_card = (
                            self.input.account_feedback.corpo_card
                        )
                        account_feedback.was_corpo_card_used = bool(
                            self.input.account_feedback.was_corpo_card_used
                        )
                        account_feedback.updated_by = self.user
                        account_feedback.save(
                            update_fields=[
                                "do_differently_feedback",
                                "feedback",
                                "corpo_card",
                                "was_corpo_card_used",
                                "updated_by",
                                "updated_at",
                            ]
                        )
                    else:
                        models.AccountFeedback.objects.create(
                            recap=recap,
                            created_by=self.user,
                            do_differently_feedback=self.input.account_feedback.do_differently_feedback,
                            feedback=self.input.account_feedback.feedback,
                            corpo_card=self.input.account_feedback.corpo_card,
                            was_corpo_card_used=bool(
                                self.input.account_feedback.was_corpo_card_used
                            ),
                        )

                if self._has_any_consumer_engagements(self.input.consumer_engagements):
                    consumer_engagement = (
                        models.ConsumerEngagements.objects.filter(recap=recap)
                        .order_by("-created_at")
                        .first()
                    )
                    if consumer_engagement:
                        consumer_engagement.total_consumer = (
                            self.input.consumer_engagements.total_consumer
                        )
                        consumer_engagement.first_time_consumers = (
                            self.input.consumer_engagements.first_time_consumers
                        )
                        consumer_engagement.brand_aware_consumers = (
                            self.input.consumer_engagements.brand_aware_consumers
                        )
                        consumer_engagement.willing_to_purchase_consumers = self.input.consumer_engagements.willing_to_purchase_consumers
                        consumer_engagement.not_willing_consumers = (
                            self.input.consumer_engagements.not_willing_consumers
                        )
                        consumer_engagement.updated_by = self.user
                        consumer_engagement.save(
                            update_fields=[
                                "total_consumer",
                                "first_time_consumers",
                                "brand_aware_consumers",
                                "willing_to_purchase_consumers",
                                "not_willing_consumers",
                                "updated_by",
                                "updated_at",
                            ]
                        )
                    else:
                        models.ConsumerEngagements.objects.create(
                            recap=recap,
                            created_by=self.user,
                            total_consumer=self.input.consumer_engagements.total_consumer,
                            first_time_consumers=self.input.consumer_engagements.first_time_consumers,
                            brand_aware_consumers=self.input.consumer_engagements.brand_aware_consumers,
                            willing_to_purchase_consumers=self.input.consumer_engagements.willing_to_purchase_consumers,
                            not_willing_consumers=self.input.consumer_engagements.not_willing_consumers,
                        )

                if self.input.product_samples is not None:
                    models.ProductSamples.objects.filter(recap=recap).delete()
                    for sample in self.input.product_samples:
                        if not self._has_complete_product_sample(sample):
                            continue
                        try:
                            product_id = resolve_id_to_int(sample.product_id)
                            models.ProductSamples.objects.create(
                                recap=recap,
                                created_by=self.user,
                                product_id=product_id,
                                quantity=sample.quantity,
                            )
                        except (TypeError, ValueError, GraphQLError):
                            raise GraphQLError(
                                f"Invalid product ID: {sample.product_id}"
                            )

                if self.input.sales_performance is not None:
                    models.SalesPerformance.objects.filter(recap=recap).delete()
                    for sale in self.input.sales_performance:
                        if not self._has_complete_sales_performance(sale):
                            continue
                        try:
                            product_id = resolve_id_to_int(sale.product_id)
                            type_of_good_id = resolve_id_to_int(sale.type_of_good_id)
                            models.SalesPerformance.objects.create(
                                recap=recap,
                                created_by=self.user,
                                product_id=product_id,
                                type_of_good_id=type_of_good_id,
                                price=sale.price,
                            )
                        except (TypeError, ValueError, GraphQLError):
                            raise GraphQLError("Invalid product or type of good ID")

                for recap_file in final_files:
                    if recap_file.recap_id != recap.id:
                        recap_file.recap = recap
                        recap_file.save(update_fields=["recap"])

                removed_blob_names = [
                    extract_blob_name_from_url(str(file.file)) for file in removed_files
                ]

                if removed_files:
                    models.RecapFile.objects.filter(
                        id__in=[file.id for file in removed_files]
                    ).delete()

                return recap, removed_blob_names

        recap, removed_blob_names = await update_recap_with_files()

        for blob_name in removed_blob_names:
            if blob_name:
                delete_blob(blob_name)

        if recap.filling_for_ambassador or recap.late or recap.incomplete:
            await self._apply_time_based_recap_payment_rule(
                event=recap.event,
                job=recap.job,
                ambassador=recap.ambassador,
            )

        return recap

    async def create_custom_recap(self) -> models.CustomRecap:
        """Create a custom recap."""
        if not isinstance(
            self.input,
            (inputs.CreateCustomRecapInput, inputs.CreateCustomRecapMobileInput),
        ):
            raise GraphQLError("Invalid input type.")

        is_mobile_input = isinstance(self.input, inputs.CreateCustomRecapMobileInput)

        job = None
        if is_mobile_input:
            try:
                job_id = resolve_id_to_int(self.input.job_id)
                job = await sync_to_async(
                    Job.objects.select_related(
                        "event",
                        "event__timezone",
                        "event__location",
                        "event__state",
                        "event__retailer",
                        "event__retailer__location",
                        "event__retailer__location__state",
                    ).get
                )(id=job_id)
            except (Job.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Job not found.")

            event = job.event
            if event is None:
                raise GraphQLError("Event not found.")
        else:
            try:
                event_id = resolve_id_to_int(self.input.event_id)
                event = await sync_to_async(
                    Event.objects.select_related(
                        "timezone",
                        "location",
                        "state",
                        "retailer",
                        "retailer__location",
                        "retailer__location__state",
                    ).get
                )(id=event_id)
            except (Event.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Event not found.")

        try:
            template_id = resolve_id_to_int(self.input.custom_recap_template_id)
            custom_recap_template = await sync_to_async(
                models.CustomRecapTemplate.objects.get
            )(id=template_id)
        except (
            models.CustomRecapTemplate.DoesNotExist,
            TypeError,
            ValueError,
            GraphQLError,
        ):
            raise GraphQLError("Custom recap template not found.")

        if custom_recap_template.tenant_id != event.tenant_id:
            raise GraphQLError(
                "Custom recap template does not belong to the event tenant."
            )

        input_timezone_id = getattr(self.input, "timezone_id", None)
        timezone = None
        if is_mobile_input:
            timezone = getattr(event, "timezone", None)
        elif input_timezone_id:
            try:
                timezone_id = resolve_id_to_int(input_timezone_id)
                timezone = await sync_to_async(TimeZone.objects.get)(id=timezone_id)
            except (TimeZone.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Time zone not found.")
        else:
            timezone = getattr(event, "timezone", None)

        input_job_id = getattr(self.input, "job_id", None)
        input_retailer_id = getattr(self.input, "retailer_id", None)
        input_ambassador_id = getattr(self.input, "ambassador_id", None)
        input_location_id = getattr(self.input, "location_id", None)
        input_state_id = getattr(self.input, "state_id", None)
        if input_job_id and not is_mobile_input:
            try:
                job_id = resolve_id_to_int(input_job_id)
                job = await sync_to_async(Job.objects.get)(id=job_id)
            except (Job.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Job not found.")

        retailer = None
        if is_mobile_input:
            retailer = getattr(event, "retailer", None)
        elif input_retailer_id:
            try:
                retailer_id = resolve_id_to_int(input_retailer_id)
                retailer = await sync_to_async(Retailer.objects.get)(id=retailer_id)
            except (Retailer.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Retailer not found.")

        ambassador = None
        if is_mobile_input:
            ambassador = await sync_to_async(
                Ambassador.objects.filter(user=self.user).first
            )()
            if not ambassador:
                raise GraphQLError("Ambassador not found.")
        elif input_ambassador_id:
            try:
                ambassador_id = resolve_id_to_int(input_ambassador_id)
                ambassador = await sync_to_async(Ambassador.objects.get)(
                    id=ambassador_id
                )
            except (Ambassador.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Ambassador not found.")

        location = None
        if is_mobile_input:
            location = getattr(event, "location", None)
            if location is None:
                location = getattr(retailer, "location", None) if retailer else None
        elif input_location_id:
            try:
                location_id = resolve_id_to_int(input_location_id)
                location = await sync_to_async(Location.objects.get)(id=location_id)
            except (Location.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Location not found.")

        state = None
        if is_mobile_input:
            state = getattr(event, "state", None)
            if state is None:
                state = getattr(location, "state", None) if location else None
        elif input_state_id:
            try:
                state_id = resolve_id_to_int(input_state_id)
                state = await sync_to_async(State.objects.get)(id=state_id)
            except (State.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("State not found.")

        @sync_to_async
        def create_custom_recap_transaction():
            with transaction.atomic():
                custom_recap = models.CustomRecap.objects.create(
                    name=self.input.name,
                    submitted_at=django_timezone.now(),
                    event=event,
                    timezone=timezone,
                    total_engagements=self.input.total_engagements,
                    job=job,
                    retailer=retailer,
                    ambassador=ambassador,
                    location=location,
                    state=state,
                    tenant_id=event.tenant_id,
                    custom_recap_template=custom_recap_template,
                    created_by=self.user,
                )
                if self.input.filling_for_ambassador is not None:
                    custom_recap.filling_for_ambassador = (
                        self.input.filling_for_ambassador
                    )
                if self.input.late is not None:
                    custom_recap.late = self.input.late
                if self.input.incomplete is not None:
                    custom_recap.incomplete = self.input.incomplete
                if self.input.approved is not None:
                    custom_recap.approved = self.input.approved
                if self.input.used_corpo_card is not None:
                    custom_recap.used_corpo_card = self.input.used_corpo_card
                # Free-text "external" BA name (web input only; mobile
                # always resolves a real Ambassador from auth.user).
                # Persist when explicitly provided, but a resolved
                # ambassador FK always wins — clear the write-in so a real
                # Spark BA takes precedence on display. Mirrors the legacy
                # Recap external_ba_name handling in update_recap.
                external_ba_name = getattr(self.input, "external_ba_name", None)
                if external_ba_name is not None:
                    val = external_ba_name.strip()
                    custom_recap.external_ba_name = val or None
                if ambassador is not None:
                    custom_recap.external_ba_name = None
                custom_recap.save()

                if self.input.custom_field_values is not None:
                    for custom_field_value_input in self.input.custom_field_values:
                        try:
                            custom_field_id = resolve_id_to_int(
                                custom_field_value_input.custom_field_id
                            )
                        except (TypeError, ValueError, GraphQLError):
                            raise GraphQLError(
                                f"Invalid custom field ID: {custom_field_value_input.custom_field_id}"
                            )

                        custom_field = models.CustomField.objects.filter(
                            id=custom_field_id,
                            custom_recap_template_id=custom_recap_template.id,
                        ).first()
                        if not custom_field:
                            raise GraphQLError(
                                "Custom field not found for the selected template."
                            )

                        models.CustomFieldValue.objects.create(
                            custom_recap=custom_recap,
                            custom_field=custom_field,
                            value=custom_field_value_input.value,
                            created_by=self.user,
                        )

                if self.input.product_samples is not None:
                    for sample in self.input.product_samples:
                        if not self._has_complete_product_sample(sample):
                            continue
                        try:
                            product_id = resolve_id_to_int(sample.product_id)
                            models.CustomRecapProductSample.objects.create(
                                custom_recap=custom_recap,
                                created_by=self.user,
                                product_id=product_id,
                                quantity=sample.quantity,
                            )
                        except (TypeError, ValueError, GraphQLError):
                            raise GraphQLError(
                                f"Invalid product ID: {sample.product_id}"
                            )

                if self.input.sales_performance is not None:
                    for sale in self.input.sales_performance:
                        if not self._has_complete_sales_performance(sale):
                            continue
                        try:
                            product_id = resolve_id_to_int(sale.product_id)
                            type_of_good_id = resolve_id_to_int(sale.type_of_good_id)
                            models.CustomRecapSalePerformance.objects.create(
                                custom_recap=custom_recap,
                                created_by=self.user,
                                product_id=product_id,
                                type_of_good_id=type_of_good_id,
                                price=sale.price,
                            )
                        except (TypeError, ValueError, GraphQLError):
                            raise GraphQLError("Invalid product or type of good ID")

                if self.input.files is not None:
                    for file_input in self.input.files:
                        file_url = file_input.file
                        blob_name = extract_blob_name_from_url(file_url)
                        if not blob_name:
                            raise GraphQLError("Invalid custom recap file path.")

                        file_type = None
                        if file_input.file_type_id not in (None, ""):
                            try:
                                file_type_id = resolve_id_to_int(
                                    file_input.file_type_id
                                )
                                file_type = FileType.objects.get(id=file_type_id)
                            except (
                                FileType.DoesNotExist,
                                TypeError,
                                ValueError,
                                GraphQLError,
                            ):
                                raise GraphQLError("File type not found.")

                        if not file_type:
                            file_type = FileType.objects.first()
                        if not file_type:
                            raise GraphQLError("No file type available.")

                        file_recap_category = _resolve_file_recap_category(
                            file_input.file_recap_category_id,
                            tenant_id=getattr(event, "tenant_id", None),
                        )

                        models.CustomRecapFile.objects.create(
                            name=f"Custom recap file for {self.input.name}",
                            url=blob_name,
                            file_type=file_type,
                            file_recap_category=file_recap_category,
                            custom_recap=custom_recap,
                            approved=False,
                            created_by=self.user,
                        )
                        # HEIC → JPG sibling blob so the gallery serves a
                        # plain <img> (displayUrl rewrites .heic→.jpg).
                        if heic_conversion.is_heic_blob(blob_name):
                            heic_conversion.ensure_jpg_sibling_blob(blob_name)
                return custom_recap

        custom_recap = await create_custom_recap_transaction()
        await _notify_recap_ready_for_review_to_admins(custom_recap, self.user)
        return custom_recap

    async def update_custom_recap(self) -> models.CustomRecap:
        """Update a custom recap."""
        if not isinstance(
            self.input,
            (inputs.UpdateCustomRecapInput, inputs.UpdateCustomRecapMobileInput),
        ):
            raise GraphQLError("Invalid input type.")
        is_mobile_input = isinstance(self.input, inputs.UpdateCustomRecapMobileInput)

        try:
            custom_recap_id = resolve_id_to_int(self.input.id)
            custom_recap = await sync_to_async(
                models.CustomRecap.objects.select_related(
                    "job__event",
                    "job__event__timezone",
                    "job__event__location",
                    "job__event__state",
                    "job__event__retailer",
                    "job__event__retailer__location",
                    "job__event__retailer__location__state",
                    "event",
                    "event__timezone",
                    "event__location",
                    "event__state",
                    "event__retailer",
                    "event__retailer__location",
                    "event__retailer__location__state",
                    "custom_recap_template",
                ).get
            )(id=custom_recap_id)
        except (models.CustomRecap.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Custom recap not found.")

        # Cross-tenant write gate (value-edit; covers both the web
        # updateCustomRecap and the BA-driven updateCustomRecapMobile, which
        # share this method). Scope off the recap's CURRENT tenant (direct FK)
        # before applying any caller-supplied changes. BAs edit their own
        # recaps on mobile, so ambassadors are not blocked — only
        # foreign-tenant access is.
        await self._assert_caller_authorized_for_recap_tenant(
            custom_recap.tenant_id,
            action="update",
            record_label="Custom recap",
        )

        job = None
        if is_mobile_input:
            job = custom_recap.job
            event = custom_recap.event
            if event is None and job is not None:
                event = getattr(job, "event", None)
            if event is None:
                raise GraphQLError("Event not found.")
        else:
            try:
                event_id = resolve_id_to_int(self.input.event_id)
                event = await sync_to_async(Event.objects.get)(id=event_id)
            except (Event.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Event not found.")

        if is_mobile_input:
            custom_recap_template = custom_recap.custom_recap_template
        else:
            try:
                template_id = resolve_id_to_int(self.input.custom_recap_template_id)
                custom_recap_template = await sync_to_async(
                    models.CustomRecapTemplate.objects.get
                )(id=template_id)
            except (
                models.CustomRecapTemplate.DoesNotExist,
                TypeError,
                ValueError,
                GraphQLError,
            ):
                raise GraphQLError("Custom recap template not found.")

        if custom_recap_template.tenant_id != event.tenant_id:
            raise GraphQLError(
                "Custom recap template does not belong to the event tenant."
            )

        timezone = None
        if is_mobile_input:
            timezone = getattr(event, "timezone", None)
        else:
            input_timezone_id = getattr(self.input, "timezone_id", None)
        if not is_mobile_input and input_timezone_id:
            try:
                timezone_id = resolve_id_to_int(input_timezone_id)
                timezone = await sync_to_async(TimeZone.objects.get)(id=timezone_id)
            except (TimeZone.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Time zone not found.")

        input_job_id = getattr(self.input, "job_id", None)
        input_retailer_id = getattr(self.input, "retailer_id", None)
        input_ambassador_id = getattr(self.input, "ambassador_id", None)
        input_location_id = getattr(self.input, "location_id", None)
        input_state_id = getattr(self.input, "state_id", None)
        input_timezone_id = getattr(self.input, "timezone_id", None)

        if input_job_id and not is_mobile_input:
            try:
                job_id = resolve_id_to_int(input_job_id)
                job = await sync_to_async(Job.objects.get)(id=job_id)
            except (Job.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Job not found.")

        retailer = None
        if is_mobile_input:
            retailer = getattr(event, "retailer", None)
        elif input_retailer_id:
            try:
                retailer_id = resolve_id_to_int(input_retailer_id)
                retailer = await sync_to_async(Retailer.objects.get)(id=retailer_id)
            except (Retailer.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Retailer not found.")

        ambassador = None
        if is_mobile_input:
            ambassador = await sync_to_async(
                Ambassador.objects.filter(user=self.user).first
            )()
            if not ambassador:
                raise GraphQLError("Ambassador not found.")
        elif input_ambassador_id:
            try:
                ambassador_id = resolve_id_to_int(input_ambassador_id)
                ambassador = await sync_to_async(Ambassador.objects.get)(
                    id=ambassador_id
                )
            except (Ambassador.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Ambassador not found.")

        location = None
        if is_mobile_input:
            location = getattr(event, "location", None)
            if location is None:
                location = getattr(retailer, "location", None) if retailer else None
        elif input_location_id:
            try:
                location_id = resolve_id_to_int(input_location_id)
                location = await sync_to_async(Location.objects.get)(id=location_id)
            except (Location.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("Location not found.")

        state = None
        if is_mobile_input:
            state = getattr(event, "state", None)
            if state is None:
                state = getattr(location, "state", None) if location else None
        elif input_state_id:
            try:
                state_id = resolve_id_to_int(input_state_id)
                state = await sync_to_async(State.objects.get)(id=state_id)
            except (State.DoesNotExist, TypeError, ValueError, GraphQLError):
                raise GraphQLError("State not found.")

        @sync_to_async
        def update_custom_recap_transaction():
            with transaction.atomic():
                custom_recap.name = self.input.name
                custom_recap.event = event
                custom_recap.custom_recap_template = custom_recap_template
                # Preserve the saved value when the input omits it. A plain
                # assignment nulled a manually-entered Total Engagements anytime
                # an edit left this field out of the payload (e.g. toggling
                # Corporate Card on the detail view) — the "24 disappeared after
                # I edited/approved" bug. None == "not provided / keep current";
                # the editor sends a number to change it.
                if self.input.total_engagements is not None:
                    custom_recap.total_engagements = self.input.total_engagements
                custom_recap.updated_by = self.user
                custom_recap.tenant_id = event.tenant_id

                if is_mobile_input or input_timezone_id is not None:
                    custom_recap.timezone = timezone
                if is_mobile_input or input_job_id is not None:
                    custom_recap.job = job
                if is_mobile_input or input_retailer_id is not None:
                    custom_recap.retailer = retailer
                if is_mobile_input or input_ambassador_id is not None:
                    custom_recap.ambassador = ambassador
                if is_mobile_input or input_location_id is not None:
                    custom_recap.location = location
                if is_mobile_input or input_state_id is not None:
                    custom_recap.state = state
                if self.input.filling_for_ambassador is not None:
                    custom_recap.filling_for_ambassador = (
                        self.input.filling_for_ambassador
                    )
                if self.input.late is not None:
                    custom_recap.late = self.input.late
                if self.input.incomplete is not None:
                    custom_recap.incomplete = self.input.incomplete
                if self.input.approved is not None:
                    custom_recap.approved = self.input.approved
                if self.input.used_corpo_card is not None:
                    custom_recap.used_corpo_card = self.input.used_corpo_card
                # external_ba_name: only touch when explicitly provided.
                # A resolved ambassador FK set in this same update wins —
                # clear the write-in so a real Spark BA takes precedence.
                external_ba_name = getattr(self.input, "external_ba_name", None)
                if external_ba_name is not None:
                    val = external_ba_name.strip()
                    custom_recap.external_ba_name = val or None
                if (
                    input_ambassador_id is not None or is_mobile_input
                ) and ambassador is not None:
                    custom_recap.external_ba_name = None

                custom_recap.save()

                if self.input.custom_field_values is not None:
                    final_custom_field_value_ids: list[int] = []
                    seen_custom_field_value_ids: set[int] = set()
                    seen_custom_field_ids: set[int] = set()

                    for custom_field_value_input in self.input.custom_field_values:
                        custom_field = None
                        custom_field_value = None

                        if custom_field_value_input.custom_field_value_id not in (
                            None,
                            "",
                        ):
                            try:
                                custom_field_value_id = resolve_id_to_int(
                                    custom_field_value_input.custom_field_value_id
                                )
                            except (TypeError, ValueError, GraphQLError):
                                raise GraphQLError(
                                    "Invalid custom field value ID: "
                                    f"{custom_field_value_input.custom_field_value_id}"
                                )

                            if custom_field_value_id in seen_custom_field_value_ids:
                                raise GraphQLError(
                                    "Duplicate custom field value in the input."
                                )
                            seen_custom_field_value_ids.add(custom_field_value_id)

                            custom_field_value = (
                                models.CustomFieldValue.objects.select_related(
                                    "custom_field"
                                )
                                .filter(
                                    id=custom_field_value_id,
                                    custom_recap=custom_recap,
                                )
                                .first()
                            )
                            if (
                                not custom_field_value
                                or custom_field_value.custom_field.custom_recap_template_id
                                != custom_recap_template.id
                            ):
                                raise GraphQLError(
                                    "Custom field value not found for the selected custom recap."
                                )

                            custom_field = custom_field_value.custom_field
                            if custom_field_value_input.custom_field_id not in (
                                None,
                                "",
                            ):
                                try:
                                    custom_field_id = resolve_id_to_int(
                                        custom_field_value_input.custom_field_id
                                    )
                                except (TypeError, ValueError, GraphQLError):
                                    raise GraphQLError(
                                        "Invalid custom field ID: "
                                        f"{custom_field_value_input.custom_field_id}"
                                    )
                                if custom_field_id != custom_field.id:
                                    raise GraphQLError(
                                        "Custom field ID does not match the custom field value."
                                    )
                        else:
                            if custom_field_value_input.custom_field_id in (None, ""):
                                raise GraphQLError(
                                    "customFieldId is required when "
                                    "customFieldValueId is not provided."
                                )

                            try:
                                custom_field_id = resolve_id_to_int(
                                    custom_field_value_input.custom_field_id
                                )
                            except (TypeError, ValueError, GraphQLError):
                                raise GraphQLError(
                                    "Invalid custom field ID: "
                                    f"{custom_field_value_input.custom_field_id}"
                                )

                            custom_field = models.CustomField.objects.filter(
                                id=custom_field_id,
                                custom_recap_template_id=custom_recap_template.id,
                            ).first()
                            if not custom_field:
                                raise GraphQLError(
                                    "Custom field not found for the selected template."
                                )

                            existing_values = list(
                                models.CustomFieldValue.objects.filter(
                                    custom_recap=custom_recap,
                                    custom_field=custom_field,
                                )[:2]
                            )
                            if len(existing_values) > 1:
                                raise GraphQLError(
                                    "Multiple custom field values found for the "
                                    "selected custom field."
                                )
                            if existing_values:
                                custom_field_value = existing_values[0]

                        if custom_field.id in seen_custom_field_ids:
                            raise GraphQLError("Duplicate custom field in the input.")
                        seen_custom_field_ids.add(custom_field.id)

                        if custom_field_value:
                            custom_field_value.value = custom_field_value_input.value
                            custom_field_value.updated_by = self.user
                            custom_field_value.save(
                                update_fields=["value", "updated_by", "updated_at"]
                            )
                        else:
                            custom_field_value = models.CustomFieldValue.objects.create(
                                custom_recap=custom_recap,
                                custom_field=custom_field,
                                value=custom_field_value_input.value,
                                created_by=self.user,
                            )
                        final_custom_field_value_ids.append(custom_field_value.id)

                    custom_field_values_to_delete = (
                        models.CustomFieldValue.objects.filter(
                            custom_recap=custom_recap
                        )
                    )
                    if final_custom_field_value_ids:
                        custom_field_values_to_delete = (
                            custom_field_values_to_delete.exclude(
                                id__in=final_custom_field_value_ids
                            )
                        )
                    custom_field_values_to_delete.delete()

                if self.input.product_samples is not None:
                    models.CustomRecapProductSample.objects.filter(
                        custom_recap=custom_recap
                    ).delete()
                    for sample in self.input.product_samples:
                        if not self._has_complete_product_sample(sample):
                            continue
                        try:
                            product_id = resolve_id_to_int(sample.product_id)
                            models.CustomRecapProductSample.objects.create(
                                custom_recap=custom_recap,
                                created_by=self.user,
                                product_id=product_id,
                                quantity=sample.quantity,
                            )
                        except (TypeError, ValueError, GraphQLError):
                            raise GraphQLError(
                                f"Invalid product ID: {sample.product_id}"
                            )

                if self.input.sales_performance is not None:
                    models.CustomRecapSalePerformance.objects.filter(
                        custom_recap=custom_recap
                    ).delete()
                    for sale in self.input.sales_performance:
                        if not self._has_complete_sales_performance(sale):
                            continue
                        try:
                            product_id = resolve_id_to_int(sale.product_id)
                            type_of_good_id = resolve_id_to_int(sale.type_of_good_id)
                            models.CustomRecapSalePerformance.objects.create(
                                custom_recap=custom_recap,
                                created_by=self.user,
                                product_id=product_id,
                                type_of_good_id=type_of_good_id,
                                price=sale.price,
                            )
                        except (TypeError, ValueError, GraphQLError):
                            raise GraphQLError("Invalid product or type of good ID")

                removed_blob_names: list[str] = []
                if self.input.files is not None:
                    existing_files = list(
                        models.CustomRecapFile.objects.filter(custom_recap=custom_recap)
                    )
                    if is_mobile_input:
                        files_to_add = self.input.files.add or []
                        file_ids_to_remove = self.input.files.remove or []

                        files_to_delete: list[models.CustomRecapFile] = []
                        for file_id in file_ids_to_remove:
                            try:
                                file_int_id = resolve_id_to_int(file_id)
                            except (TypeError, ValueError, GraphQLError):
                                raise GraphQLError(f"Invalid file ID: {file_id}")
                            file_to_delete = next(
                                (
                                    file
                                    for file in existing_files
                                    if file.id == file_int_id
                                ),
                                None,
                            )
                            if not file_to_delete:
                                raise GraphQLError(
                                    f"Custom recap file not found for ID: {file_id}"
                                )
                            files_to_delete.append(file_to_delete)

                        if files_to_delete:
                            removed_blob_names = [
                                extract_blob_name_from_url(str(file.url))
                                for file in files_to_delete
                            ]
                            models.CustomRecapFile.objects.filter(
                                id__in=[file.id for file in files_to_delete]
                            ).delete()

                        for file_input in files_to_add:
                            file_url = file_input.file
                            blob_name = extract_blob_name_from_url(file_url)
                            if not blob_name:
                                raise GraphQLError("Invalid custom recap file path.")

                            file_type = None
                            if file_input.file_type_id not in (None, ""):
                                try:
                                    file_type_id = resolve_id_to_int(
                                        file_input.file_type_id
                                    )
                                    file_type = FileType.objects.get(id=file_type_id)
                                except (
                                    FileType.DoesNotExist,
                                    TypeError,
                                    ValueError,
                                    GraphQLError,
                                ):
                                    raise GraphQLError("File type not found.")
                            if not file_type:
                                file_type = FileType.objects.first()
                            if not file_type:
                                raise GraphQLError("No file type available.")

                            # Mobile-added new file: resolve through the shared
                            # tenant-aware resolver so positional "1"/"2"
                            # sentinels map to this tenant's photos/receipts by
                            # name (CustomRecap carries a direct tenant FK).
                            file_recap_category = _resolve_file_recap_category(
                                file_input.file_recap_category_id,
                                tenant_id=getattr(custom_recap, "tenant_id", None),
                            )

                            models.CustomRecapFile.objects.create(
                                name=f"Custom recap file for {self.input.name}",
                                url=blob_name,
                                file_type=file_type,
                                file_recap_category=file_recap_category,
                                custom_recap=custom_recap,
                                approved=False,
                                created_by=self.user,
                            )
                            if heic_conversion.is_heic_blob(blob_name):
                                heic_conversion.ensure_jpg_sibling_blob(blob_name)
                    else:
                        blob_to_file = {
                            extract_blob_name_from_url(str(file.url)): file
                            for file in existing_files
                            if extract_blob_name_from_url(str(file.url))
                        }

                        final_files: list[models.CustomRecapFile] = []
                        for file_input in self.input.files:
                            file_url = file_input.file
                            blob_name = extract_blob_name_from_url(file_url)
                            if not blob_name:
                                raise GraphQLError("Invalid custom recap file path.")

                            if blob_name in blob_to_file:
                                existing_file = blob_to_file.pop(blob_name)
                                updated_fields = []

                                if file_input.file_type_id not in (None, ""):
                                    try:
                                        file_type_id = resolve_id_to_int(
                                            file_input.file_type_id
                                        )
                                        file_type = FileType.objects.get(
                                            id=file_type_id
                                        )
                                    except (
                                        FileType.DoesNotExist,
                                        TypeError,
                                        ValueError,
                                        GraphQLError,
                                    ):
                                        raise GraphQLError("File type not found.")
                                    if existing_file.file_type_id != file_type.id:
                                        existing_file.file_type = file_type
                                        updated_fields.append("file_type")

                                if file_input.file_recap_category_id not in (None, ""):
                                    try:
                                        category_id = resolve_id_to_int(
                                            file_input.file_recap_category_id
                                        )
                                        file_recap_category = (
                                            models.FileRecapCategory.objects.get(
                                                id=category_id
                                            )
                                        )
                                    except (
                                        models.FileRecapCategory.DoesNotExist,
                                        TypeError,
                                        ValueError,
                                        GraphQLError,
                                    ):
                                        raise GraphQLError(
                                            "File recap category not found."
                                        )
                                    if (
                                        existing_file.file_recap_category_id
                                        != file_recap_category.id
                                    ):
                                        existing_file.file_recap_category = (
                                            file_recap_category
                                        )
                                        updated_fields.append("file_recap_category")

                                if updated_fields:
                                    existing_file.save(update_fields=updated_fields)
                                final_files.append(existing_file)
                                continue

                            file_type = None
                            if file_input.file_type_id not in (None, ""):
                                try:
                                    file_type_id = resolve_id_to_int(
                                        file_input.file_type_id
                                    )
                                    file_type = FileType.objects.get(id=file_type_id)
                                except (
                                    FileType.DoesNotExist,
                                    TypeError,
                                    ValueError,
                                    GraphQLError,
                                ):
                                    raise GraphQLError("File type not found.")
                            if not file_type:
                                file_type = FileType.objects.first()
                            if not file_type:
                                raise GraphQLError("No file type available.")

                            # New file added during a (non-mobile) update:
                            # resolve through the shared tenant-aware resolver so
                            # positional "1"/"2" sentinels map to this tenant's
                            # photos/receipts by name.
                            file_recap_category = _resolve_file_recap_category(
                                file_input.file_recap_category_id,
                                tenant_id=getattr(custom_recap, "tenant_id", None),
                            )

                            custom_recap_file = models.CustomRecapFile.objects.create(
                                name=f"Custom recap file for {self.input.name}",
                                url=blob_name,
                                file_type=file_type,
                                file_recap_category=file_recap_category,
                                custom_recap=custom_recap,
                                approved=False,
                                created_by=self.user,
                            )
                            if heic_conversion.is_heic_blob(blob_name):
                                heic_conversion.ensure_jpg_sibling_blob(blob_name)
                            final_files.append(custom_recap_file)

                        removed_files = list(blob_to_file.values())
                        if removed_files:
                            removed_blob_names = [
                                extract_blob_name_from_url(str(file.url))
                                for file in removed_files
                            ]
                            models.CustomRecapFile.objects.filter(
                                id__in=[file.id for file in removed_files]
                            ).delete()

                return custom_recap, removed_blob_names

        custom_recap, removed_blob_names = await update_custom_recap_transaction()
        for blob_name in removed_blob_names:
            if blob_name:
                delete_blob(blob_name)
        return custom_recap

    async def create_custom_field(self) -> models.CustomField:
        """Create a custom field."""
        if not isinstance(self.input, inputs.CreateCustomFieldInput):
            raise GraphQLError("Invalid input type.")

        try:
            custom_recap_template_id = resolve_id_to_int(
                self.input.custom_recap_template_id
            )
            custom_recap_template = await sync_to_async(
                models.CustomRecapTemplate.objects.get
            )(id=custom_recap_template_id)
        except (
            models.CustomRecapTemplate.DoesNotExist,
            TypeError,
            ValueError,
            GraphQLError,
        ):
            raise GraphQLError("Custom recap template not found.")

        try:
            custom_field_type_id = resolve_id_to_int(self.input.custom_field_type_id)
            custom_field_type = await sync_to_async(
                models.CustomRecapFieldType.objects.get
            )(id=custom_field_type_id)
        except (
            models.CustomRecapFieldType.DoesNotExist,
            TypeError,
            ValueError,
            GraphQLError,
        ):
            raise GraphQLError("Custom field type not found.")

        try:
            recap_section_id = resolve_id_to_int(self.input.recap_section_id)
            recap_section = await sync_to_async(models.RecapSection.objects.get)(
                id=recap_section_id
            )
        except (models.RecapSection.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Recap section not found.")

        if recap_section.tenant_id != custom_recap_template.tenant_id:
            raise GraphQLError("Recap section does not belong to the template tenant.")

        @sync_to_async
        def create_custom_field_transaction():
            with transaction.atomic():
                custom_field = models.CustomField.objects.create(
                    name=self.input.name,
                    custom_recap_template=custom_recap_template,
                    custom_field_type=custom_field_type,
                    recap_section=recap_section,
                    created_by=self.user,
                    required=bool(self.input.required),
                    options=list(self.input.options or []),
                    order=self.input.order if self.input.order is not None else 0,
                )
                return custom_field

        return await create_custom_field_transaction()

    async def update_custom_field(self) -> models.CustomField:
        """Update a custom field."""
        if not isinstance(self.input, inputs.UpdateCustomFieldInput):
            raise GraphQLError("Invalid input type.")

        try:
            custom_field_id = resolve_id_to_int(self.input.id)
            custom_field = await sync_to_async(models.CustomField.objects.get)(
                id=custom_field_id
            )
        except (models.CustomField.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Custom field not found.")

        try:
            custom_recap_template_id = resolve_id_to_int(
                self.input.custom_recap_template_id
            )
            custom_recap_template = await sync_to_async(
                models.CustomRecapTemplate.objects.get
            )(id=custom_recap_template_id)
        except (
            models.CustomRecapTemplate.DoesNotExist,
            TypeError,
            ValueError,
            GraphQLError,
        ):
            raise GraphQLError("Custom recap template not found.")

        try:
            custom_field_type_id = resolve_id_to_int(self.input.custom_field_type_id)
            custom_field_type = await sync_to_async(
                models.CustomRecapFieldType.objects.get
            )(id=custom_field_type_id)
        except (
            models.CustomRecapFieldType.DoesNotExist,
            TypeError,
            ValueError,
            GraphQLError,
        ):
            raise GraphQLError("Custom field type not found.")

        try:
            recap_section_id = resolve_id_to_int(self.input.recap_section_id)
            recap_section = await sync_to_async(models.RecapSection.objects.get)(
                id=recap_section_id
            )
        except (models.RecapSection.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Recap section not found.")

        if recap_section.tenant_id != custom_recap_template.tenant_id:
            raise GraphQLError("Recap section does not belong to the template tenant.")

        @sync_to_async
        def update_custom_field_transaction():
            with transaction.atomic():
                custom_field.name = self.input.name
                custom_field.custom_recap_template = custom_recap_template
                custom_field.custom_field_type = custom_field_type
                custom_field.recap_section = recap_section
                custom_field.updated_by = self.user
                if self.input.required is not None:
                    custom_field.required = self.input.required
                if self.input.options is not None:
                    custom_field.options = list(self.input.options)
                if self.input.order is not None:
                    custom_field.order = self.input.order
                custom_field.save()
                return custom_field

        return await update_custom_field_transaction()

    async def create_custom_recap_template(self) -> models.CustomRecapTemplate:
        """Create a custom recap template."""
        if not isinstance(self.input, inputs.CreateCustomRecapTemplateInput):
            raise GraphQLError("Invalid input type.")

        try:
            event_type_id = resolve_id_to_int(self.input.event_type_id)
            event_type = await sync_to_async(EventType.objects.get)(id=event_type_id)
        except (EventType.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Event type not found.")

        @sync_to_async
        def create_custom_recap_template_transaction():
            with transaction.atomic():
                custom_recap_template = models.CustomRecapTemplate.objects.create(
                    name=self.input.name,
                    event_type=event_type,
                    tenant_id=event_type.tenant_id,
                    product_samples=bool(self.input.product_samples),
                    sales_performance=bool(self.input.sales_performance),
                    layout=self.input.layout or {},
                    created_by=self.user,
                )
                self._create_custom_recap_template_fields(
                    custom_recap_template,
                    self.input.custom_fields,
                )
                return custom_recap_template

        return await create_custom_recap_template_transaction()

    async def remove_custom_field(self) -> models.CustomField:
        """Force-delete a CustomField from a template, optionally
        cascading its submitted values.

        Counterpart to the strict "Cannot remove custom fields that
        already have submitted values" guard in
        ``_sync_custom_recap_template_fields``. Used when an admin
        needs to retroactively prune a template (e.g. a duplicate
        metric that's distorting reports — Nevena's Austin Psych
        Festival report).
        """
        if not isinstance(self.input, inputs.RemoveCustomFieldInput):
            raise GraphQLError("Invalid input type.")

        try:
            custom_field_id = resolve_id_to_int(self.input.id)
        except (TypeError, ValueError, GraphQLError):
            raise GraphQLError(f"Invalid custom field ID: {self.input.id}")

        try:
            custom_field = await sync_to_async(
                models.CustomField.objects.select_related(
                    "custom_recap_template"
                ).get
            )(id=custom_field_id)
        except models.CustomField.DoesNotExist:
            raise GraphQLError("Custom field not found.")

        @sync_to_async
        def _delete():
            value_qs = models.CustomFieldValue.objects.filter(
                custom_field=custom_field
            )
            value_count = value_qs.count()
            if value_count and not self.input.delete_values:
                raise GraphQLError(
                    f"Field has {value_count} submitted value(s). "
                    "Pass deleteValues=true to remove the field and "
                    "cascade its values."
                )
            with transaction.atomic():
                if value_count:
                    value_qs.delete()
                custom_field.delete()
            return custom_field

        return await _delete()

    async def update_custom_recap_template(self) -> models.CustomRecapTemplate:
        """Update a custom recap template."""
        if not isinstance(self.input, inputs.UpdateCustomRecapTemplateInput):
            raise GraphQLError("Invalid input type.")

        try:
            custom_recap_template_id = resolve_id_to_int(self.input.id)
            custom_recap_template = await sync_to_async(
                models.CustomRecapTemplate.objects.get
            )(id=custom_recap_template_id)
        except (
            models.CustomRecapTemplate.DoesNotExist,
            TypeError,
            ValueError,
            GraphQLError,
        ):
            raise GraphQLError("Custom recap template not found.")

        try:
            event_type_id = resolve_id_to_int(self.input.event_type_id)
            event_type = await sync_to_async(EventType.objects.get)(id=event_type_id)
        except (EventType.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Event type not found.")

        @sync_to_async
        def update_custom_recap_template_transaction():
            with transaction.atomic():
                custom_recap_template.name = self.input.name
                custom_recap_template.event_type = event_type
                custom_recap_template.tenant_id = event_type.tenant_id
                custom_recap_template.updated_by = self.user
                if self.input.product_samples is not None:
                    custom_recap_template.product_samples = self.input.product_samples
                if self.input.sales_performance is not None:
                    custom_recap_template.sales_performance = (
                        self.input.sales_performance
                    )
                if self.input.layout is not None:
                    custom_recap_template.layout = self.input.layout
                custom_recap_template.save()
                self._sync_custom_recap_template_fields(
                    custom_recap_template,
                    self.input.custom_fields,
                )
                return custom_recap_template

        return await update_custom_recap_template_transaction()

    async def create_custom_recap_field_type(self) -> models.CustomRecapFieldType:
        """Create a custom recap field type."""
        if not isinstance(self.input, inputs.CreateCustomRecapFieldTypeInput):
            raise GraphQLError("Invalid input type.")

        @sync_to_async
        def create_custom_recap_field_type_transaction():
            with transaction.atomic():
                custom_recap_field_type = models.CustomRecapFieldType.objects.create(
                    name=self.input.name,
                    created_by=self.user,
                )
                return custom_recap_field_type

        return await create_custom_recap_field_type_transaction()

    async def update_custom_recap_field_type(self) -> models.CustomRecapFieldType:
        """Update a custom recap field type."""
        if not isinstance(self.input, inputs.UpdateCustomRecapFieldTypeInput):
            raise GraphQLError("Invalid input type.")

        try:
            custom_recap_field_type_id = resolve_id_to_int(self.input.id)
            custom_recap_field_type = await sync_to_async(
                models.CustomRecapFieldType.objects.get
            )(id=custom_recap_field_type_id)
        except (
            models.CustomRecapFieldType.DoesNotExist,
            TypeError,
            ValueError,
            GraphQLError,
        ):
            raise GraphQLError("Custom recap field type not found.")

        @sync_to_async
        def update_custom_recap_field_type_transaction():
            with transaction.atomic():
                custom_recap_field_type.name = self.input.name
                custom_recap_field_type.updated_by = self.user
                custom_recap_field_type.save()
                return custom_recap_field_type

        return await update_custom_recap_field_type_transaction()

    async def create_recap_section(self) -> models.RecapSection:
        """Create a recap section."""
        if not isinstance(self.input, inputs.CreateRecapSectionInput):
            raise GraphQLError("Invalid input type.")

        try:
            tenant_id = resolve_id_to_int(self.input.tenant_id)
            tenant = await sync_to_async(Tenant.objects.get)(id=tenant_id)
        except (Tenant.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Tenant not found.")

        @sync_to_async
        def create_recap_section_transaction():
            with transaction.atomic():
                recap_section = models.RecapSection.objects.create(
                    name=self.input.name,
                    tenant=tenant,
                    created_by=self.user,
                    order=self.input.order if self.input.order is not None else 0,
                )
                return recap_section

        return await create_recap_section_transaction()

    async def update_recap_section(self) -> models.RecapSection:
        """Update a recap section."""
        if not isinstance(self.input, inputs.UpdateRecapSectionInput):
            raise GraphQLError("Invalid input type.")

        try:
            recap_section_id = resolve_id_to_int(self.input.id)
            recap_section = await sync_to_async(models.RecapSection.objects.get)(
                id=recap_section_id
            )
        except (models.RecapSection.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Recap section not found.")

        try:
            tenant_id = resolve_id_to_int(self.input.tenant_id)
            tenant = await sync_to_async(Tenant.objects.get)(id=tenant_id)
        except (Tenant.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Tenant not found.")

        @sync_to_async
        def update_recap_section_transaction():
            with transaction.atomic():
                recap_section.name = self.input.name
                recap_section.tenant = tenant
                recap_section.updated_by = self.user
                if self.input.order is not None:
                    recap_section.order = self.input.order
                recap_section.save()
                return recap_section

        return await update_recap_section_transaction()

    async def move_custom_field_to_section(self) -> models.CustomField:
        """Move a CustomField into a different RecapSection of the SAME
        template (template-structure edit).

        Reassigns ``CustomField.recap_section`` only — the field row and
        all ``CustomFieldValue`` rows captured for it are preserved (a
        pointer change, never delete+recreate, so no submitted answer is
        orphaned). The destination section must belong to the field's
        own template/tenant; cross-template / cross-tenant moves are
        rejected (a section reused by a *different* template is not a
        valid target). Tenant-scoped via the shared recap write gate.
        """
        if not isinstance(self.input, inputs.MoveCustomFieldToSectionInput):
            raise GraphQLError("Invalid input type.")

        try:
            custom_field_id = resolve_id_to_int(self.input.field_id)
            custom_field = await sync_to_async(
                models.CustomField.objects.select_related(
                    "custom_recap_template"
                ).get
            )(id=custom_field_id)
        except (models.CustomField.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Custom field not found.")

        try:
            recap_section_id = resolve_id_to_int(self.input.section_id)
            recap_section = await sync_to_async(models.RecapSection.objects.get)(
                id=recap_section_id
            )
        except (models.RecapSection.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Recap section not found.")

        # Tenant write gate — only an admin or a member of the field's
        # template tenant may restructure it. `custom_recap_template` is
        # select_related above so reading tenant_id here doesn't trip a
        # sync DB fetch inside the async resolver.
        await self._assert_caller_authorized_for_recap_tenant(
            custom_field.custom_recap_template.tenant_id,
            action="modify",
            record_label="Custom field",
        )

        # The destination section must belong to the SAME template as the
        # field. We anchor on the template (not just the tenant) because a
        # section is template-scoped via its fields; a section that only
        # other templates use — even in the same tenant — is not a valid
        # target. Checked by: does any CustomField of this template
        # already live in the target section? (Sections are created per
        # template in the builder, so every in-use section has >=1 field.)
        same_template = await sync_to_async(
            models.CustomField.objects.filter(
                custom_recap_template_id=custom_field.custom_recap_template_id,
                recap_section_id=recap_section.id,
            ).exists
        )()
        # A no-op move (field already in the target section) is allowed and
        # trivially same-template — short-circuit so it isn't rejected.
        if (
            recap_section.id != custom_field.recap_section_id
            and not same_template
        ):
            raise GraphQLError(
                "Target section does not belong to the same template as "
                "the field."
            )

        @sync_to_async
        def move_custom_field_transaction():
            with transaction.atomic():
                custom_field.recap_section = recap_section
                custom_field.updated_by = self.user
                # Pointer-only update: name / type / template / values
                # untouched, so captured CustomFieldValue rows are intact.
                custom_field.save(update_fields=["recap_section", "updated_by", "updated_at"])
                return custom_field

        return await move_custom_field_transaction()

    async def delete_recap_section(self) -> models.RecapSection:
        """Delete an EMPTY RecapSection (template-structure edit).

        Returns the deleted section instance (detached) so the caller can
        surface its uuid for a Relay store prune. Refuses if the section
        still has CustomField rows — the FE must move/remove those fields
        first (via moveCustomFieldToSection / removeCustomField) so
        deleting a section can never cascade away fields and their
        captured CustomFieldValue data. Tenant-scoped via the shared
        recap write gate against the section's own tenant.
        """
        if not isinstance(self.input, inputs.DeleteRecapSectionInput):
            raise GraphQLError("Invalid input type.")

        try:
            recap_section_id = resolve_id_to_int(self.input.section_id)
            recap_section = await sync_to_async(models.RecapSection.objects.get)(
                id=recap_section_id
            )
        except (models.RecapSection.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Recap section not found.")

        # Tenant write gate — RecapSection carries its own tenant FK
        # (it has no template FK), so authorize against that directly.
        await self._assert_caller_authorized_for_recap_tenant(
            recap_section.tenant_id,
            action="delete",
            record_label="Recap section",
        )

        @sync_to_async
        def delete_recap_section_transaction():
            with transaction.atomic():
                # Guard: refuse if any field still points at this section.
                # Re-checked inside the transaction so a concurrent
                # create_custom_field can't slip a field in after the read.
                field_count = models.CustomField.objects.filter(
                    recap_section=recap_section
                ).count()
                if field_count:
                    raise GraphQLError(
                        "Move or remove this section's fields before "
                        "deleting it."
                    )
                recap_section.delete()
                return recap_section

        return await delete_recap_section_transaction()

    async def _assert_caller_authorized_for_recap_tenant(
        self,
        tenant_id: int | None,
        *,
        action: str = "modify",
        block_ambassadors: bool = False,
        record_label: str = "Recap",
    ) -> None:
        """Authorize the current user to act on a recap owned by `tenant_id`.

        This is the cross-tenant write gate for the recap mutation cluster.
        The READ resolvers (recaps/customRecaps queries) are already tenant
        scoped; the *mutation* write path historically loaded a recap by raw
        PK gated only by StrictIsAuthenticated, so any authenticated user
        could approve / decline / edit / add-file-to another tenant's recap
        by guessing its global id (IDOR / cross-tenant write).

        Authorization mirrors the hardened `approve_request` / `decline_request`
        precedent (events.mutations) and the recaps-queries isolation sweep:
          - Admins (spark-admin / is_staff / is_superuser / any
            @igniteproductions.co email) may act in ANY tenant. We resolve
            this authoritatively via `resolve_request_user_access`, because
            the JWT request.user often does not hydrate its role FK / flags
            inside async resolvers (reading `user.role` directly returns
            empty and would deny genuine admins).
          - Optionally (approve/decline only) ambassadors are blocked
            outright — approval is an admin/client action, never a BA one
            (matches the `delete_request` / `approve_request` precedent).
          - Any other role (client, and BA on the mobile edit path) may act
            ONLY inside a tenant they belong to. `get_tenant` raises for a
            non-member, which we convert into a denial.

        Denial message posture (non-existence-leaking): a cross-tenant caller
        (or a recap whose tenant can't be resolved) is told the record was
        "not found" — never "you're not authorized for this tenant", which
        would confirm the record exists in some other brand. `record_label`
        picks the noun ("Recap" / "Custom recap") so the message matches the
        surface the caller hit. The ambassador-block path keeps its explicit
        "not authorized to {action} recaps" wording: that's a role limit, not
        a record-existence signal (a same-tenant BA legitimately sees it), so
        it leaks nothing about a specific recap.

        Raises GraphQLError when not allowed (the resolver converts that into
        a success=False response; nothing is mutated and we never 500).
        """
        user = self.user
        if user is None:
            raise GraphQLError("Authentication required.")

        # Admins (staff / superuser / spark-admin / @igniteproductions.co)
        # bypass the per-tenant check. Resolved from the DB row, not the
        # (often unhydrated) JWT user, so real admins aren't wrongly denied.
        role_slug, is_staff, is_super, email = await resolve_request_user_access(
            user
        )
        if _is_admin_access(role_slug, is_staff, is_super, email):
            return

        if block_ambassadors and (
            role_slug == Role.AMBASSADOR_SLUG
            or getattr(user, "role_id", None) == ROLE_ID.Ambassadors
        ):
            raise GraphQLError(f"You are not authorized to {action} recaps.")

        # A recap with no resolvable tenant can't be ownership-checked; deny
        # rather than fall open (defensive — every recap has a tenant). Read
        # as "not found" so we don't hint that an unscopeable record exists.
        if tenant_id is None:
            raise GraphQLError(f"{record_label} not found.")

        # Non-admins may only act inside a tenant they belong to. get_tenant
        # raises Tenant.DoesNotExist for a non-member -> deny. Read as "not
        # found" (not "...for this tenant") so a cross-tenant probe can't
        # confirm the record exists under another brand.
        try:
            await sync_to_async(user.get_tenant)(tenant_id=tenant_id)
        except Exception:
            raise GraphQLError(f"{record_label} not found.")

    async def _assert_can_delete_recap(
        self, tenant_id: int, *, record_label: str = "Recap"
    ) -> None:
        """Authorize the current user to delete a recap in `tenant_id`.

        Mirrors the `delete_request` precedent (events.mutations):
          - Ambassadors are blocked outright.
          - admins (spark-admin / staff / superuser / @igniteproductions.co)
            may delete in any tenant.
          - any other role (client / RMM) may only delete inside a
            tenant they belong to.
        Raises GraphQLError when not allowed. `record_label` flows through to
        the shared gate's non-existence-leaking denial message.
        """
        await self._assert_caller_authorized_for_recap_tenant(
            tenant_id,
            action="delete",
            block_ambassadors=True,
            record_label=record_label,
        )

    async def delete_recap(self) -> models.Recap:
        """Delete a legacy Recap (tenant-scoped, admin-only).

        Returns the deleted recap instance (detached from the DB) so the
        caller can surface its uuid for a Relay store prune. Auth follows
        the `delete_request` precedent: ambassadors blocked, spark-admin
        anywhere, other roles only inside their own tenant.

        A real delete (not soft) — Recap has no archived_at column, and
        the user explicitly wants bad test recaps gone AND the event
        freed for a fresh recap. Child rows that FK to the recap with
        on_delete=RESTRICT (ConsumerEngagements, ProductSamples,
        SalesPerformance, ConsumerFeedback, AccountFeedback) are removed
        first so the parent delete doesn't trip the RESTRICT guard;
        RecapFile rows are detached (recap=None) to preserve the blobs.
        """
        if not isinstance(self.input, inputs.DeleteRecapInput):
            raise GraphQLError("Invalid input type.")

        try:
            recap_id = resolve_id_to_int(self.input.id)
            recap = await sync_to_async(
                models.Recap.objects.select_related("event").get
            )(id=recap_id)
        except (models.Recap.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Recap not found.")

        # `event` is select_related above so reading event.tenant_id here
        # doesn't trigger a synchronous DB fetch in the async context.
        await self._assert_can_delete_recap(recap.event.tenant_id)

        @sync_to_async
        def delete_recap_with_files():
            with transaction.atomic():
                # Drop child rows whose FK to Recap is on_delete=RESTRICT;
                # a bare recap.delete() would otherwise raise.
                models.ConsumerEngagements.objects.filter(recap=recap).delete()
                models.ProductSamples.objects.filter(recap=recap).delete()
                models.SalesPerformance.objects.filter(recap=recap).delete()
                models.ConsumerFeedback.objects.filter(recap=recap).delete()
                models.AccountFeedback.objects.filter(recap=recap).delete()
                # Detach recap files before deleting recap (keep the blobs).
                models.RecapFile.objects.filter(recap=recap).update(recap=None)
                # Delete the recap
                recap.delete()
            return recap

        return await delete_recap_with_files()

    async def delete_custom_recap(self) -> models.CustomRecap:
        """Delete a CustomRecap (tenant-scoped, admin-only).

        Custom-template counterpart to delete_recap. Returns the deleted
        instance so the caller can surface its uuid. Auth is identical
        (delete_request precedent). All child rows that FK to CustomRecap
        with on_delete=RESTRICT (CustomFieldValue, CustomRecapProductSample,
        CustomRecapSalePerformance) are removed first; CustomRecapFile rows
        are detached (custom_recap=None) so the GCS blobs survive.
        """
        if not isinstance(self.input, inputs.DeleteCustomRecapInput):
            raise GraphQLError("Invalid input type.")

        try:
            recap_id = resolve_id_to_int(self.input.id)
            recap = await sync_to_async(models.CustomRecap.objects.get)(
                id=recap_id
            )
        except (
            models.CustomRecap.DoesNotExist,
            TypeError,
            ValueError,
            GraphQLError,
        ):
            raise GraphQLError("Recap not found.")

        # CustomRecap carries its own tenant_id column (denormalized at
        # create time), so scope off that directly.
        await self._assert_can_delete_recap(
            recap.tenant_id, record_label="Custom recap"
        )

        @sync_to_async
        def delete_custom_recap_with_children():
            with transaction.atomic():
                models.CustomFieldValue.objects.filter(
                    custom_recap=recap
                ).delete()
                models.CustomRecapProductSample.objects.filter(
                    custom_recap=recap
                ).delete()
                models.CustomRecapSalePerformance.objects.filter(
                    custom_recap=recap
                ).delete()
                # Detach files (keep the blobs in GCS for audit).
                models.CustomRecapFile.objects.filter(
                    custom_recap=recap
                ).update(custom_recap=None)
                recap.delete()
            return recap

        return await delete_custom_recap_with_children()

    async def add_recap_file(self) -> models.Recap:
        """Attach one already-uploaded blob to an existing recap.

        Safe, minimal counterpart to update_recap: creates a single
        RecapFile row pointing at `file`, linked to the recap. Does not
        touch products_sold / engagements / feedback / other files —
        so calling it to add a photo can't wipe recap data the way a
        partial update_recap would.
        """
        if not isinstance(self.input, inputs.AddRecapFileInput):
            raise GraphQLError("Invalid input type.")

        try:
            recap_id = resolve_id_to_int(self.input.recap_id)
            recap = await sync_to_async(
                models.Recap.objects.select_related("event").get
            )(id=recap_id)
        except (models.Recap.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Recap not found.")

        # Cross-tenant write gate (Recap scopes tenant via its event). BAs
        # legitimately attach files to their own-tenant recaps (mobile), so
        # ambassadors are not blocked here — only foreign-tenant access is.
        await self._assert_caller_authorized_for_recap_tenant(
            recap.event.tenant_id if recap.event_id else None,
            action="attach files to",
        )

        blob_name = extract_blob_name_from_url(self.input.file)
        if not blob_name:
            raise GraphQLError("Invalid recap file path.")

        @sync_to_async
        def create_file():
            with transaction.atomic():
                file_type = None
                if self.input.file_type_id not in (None, ""):
                    try:
                        file_type = FileType.objects.get(
                            id=resolve_id_to_int(self.input.file_type_id)
                        )
                    except (
                        FileType.DoesNotExist,
                        TypeError,
                        ValueError,
                        GraphQLError,
                    ):
                        raise GraphQLError("File type not found.")
                # Default to the first FileType — mirrors create_recap's
                # behavior so a caller that omits the id still works.
                if not file_type:
                    file_type = FileType.objects.first()
                if not file_type:
                    raise GraphQLError(
                        "No file type available. Please create a file type first."
                    )

                # Resolve through the shared tenant-aware resolver so the
                # positional "1"/"2" sentinels the upload widget sends map to
                # this recap's tenant's own photos/receipts categories by name
                # (Recap has no direct tenant FK — it scopes through the event).
                file_recap_category = _resolve_file_recap_category(
                    self.input.file_recap_category_id,
                    tenant_id=getattr(recap.event, "tenant_id", None),
                )

                recap_file = models.RecapFile(
                    name=f"Recap file for {recap.name}",
                    file=blob_name,
                    file_type=file_type,
                    file_recap_category=file_recap_category,
                    recap=recap,
                    approved=False,
                    created_by=self.user,
                )
                recap_file.save()

                # HEIC sibling generation (mirrors create_recap): if the
                # attached blob is .heic/.heif, server-convert it to a
                # .jpg sibling RecapFile row so the recap-list hero picker
                # and Files grid render it without the in-browser libheif
                # fallback. Best-effort — failures log + keep the HEIC.
                if heic_conversion.is_heic_blob(blob_name):
                    heic_conversion.ensure_jpg_sibling(
                        heic_blob_name=blob_name,
                        recap_id=recap.id,
                        file_type=file_type,
                        file_recap_category=file_recap_category,
                        created_by=self.user,
                    )
                return recap

        return await create_file()

    async def add_custom_recap_file(self) -> models.CustomRecap:
        """Attach one already-uploaded blob to an existing custom recap.

        Custom-template counterpart to add_recap_file. Creates a single
        CustomRecapFile row (url=blob) linked to the custom recap; does
        not touch field values or the rest of the file set, so adding a
        photo can't wipe recap data the way a partial update_custom_recap
        would.
        """
        if not isinstance(self.input, inputs.AddCustomRecapFileInput):
            raise GraphQLError("Invalid input type.")

        try:
            custom_recap_id = resolve_id_to_int(self.input.custom_recap_id)
            custom_recap = await sync_to_async(models.CustomRecap.objects.get)(
                id=custom_recap_id
            )
        except (
            models.CustomRecap.DoesNotExist,
            TypeError,
            ValueError,
            GraphQLError,
        ):
            raise GraphQLError("Custom recap not found.")

        # Cross-tenant write gate. CustomRecap carries a direct tenant FK.
        # BAs legitimately attach files to their own-tenant recaps (mobile),
        # so ambassadors are not blocked here — only foreign-tenant access is.
        await self._assert_caller_authorized_for_recap_tenant(
            custom_recap.tenant_id,
            action="attach files to",
            record_label="Custom recap",
        )

        blob_name = extract_blob_name_from_url(self.input.file)
        if not blob_name:
            raise GraphQLError("Invalid recap file path.")

        @sync_to_async
        def create_file():
            with transaction.atomic():
                file_type = None
                if self.input.file_type_id not in (None, ""):
                    try:
                        file_type = FileType.objects.get(
                            id=resolve_id_to_int(self.input.file_type_id)
                        )
                    except (
                        FileType.DoesNotExist,
                        TypeError,
                        ValueError,
                        GraphQLError,
                    ):
                        raise GraphQLError("File type not found.")
                # Default to the first FileType — mirrors create_custom_recap
                # so a caller that omits the id still works.
                if not file_type:
                    file_type = FileType.objects.first()
                if not file_type:
                    raise GraphQLError(
                        "No file type available. Please create a file type first."
                    )

                # Resolve through the shared tenant-aware resolver so the
                # positional "1"/"2" sentinels map to this custom recap's
                # tenant's own photos/receipts categories by name (CustomRecap
                # carries a direct tenant FK).
                file_recap_category = _resolve_file_recap_category(
                    self.input.file_recap_category_id,
                    tenant_id=getattr(custom_recap, "tenant_id", None),
                )

                models.CustomRecapFile.objects.create(
                    name=f"Custom recap file for {custom_recap.name}",
                    url=blob_name,
                    file_type=file_type,
                    file_recap_category=file_recap_category,
                    custom_recap=custom_recap,
                    approved=False,
                    created_by=self.user,
                )
                # HEIC → JPG sibling blob so the gallery renders a plain
                # <img> (displayUrl rewrites .heic→.jpg when present).
                if heic_conversion.is_heic_blob(blob_name):
                    heic_conversion.ensure_jpg_sibling_blob(blob_name)
                return custom_recap

        return await create_file()

    async def remove_recap_file(self) -> models.Recap:
        """Detach + delete one file from a recap, return the parent recap.

        The explicit "remove this photo from the recap" action. Unlike
        delete_recap_file (which guards against deleting recap-linked
        files), this deletes the linked RecapFile row and its GCS blob,
        then returns the parent recap so the client can re-render the
        file grid from the mutation response (no refetch needed).
        """
        if not isinstance(self.input, inputs.RemoveRecapFileInput):
            raise GraphQLError("Invalid input type.")

        try:
            recap_file_id = resolve_id_to_int(self.input.id)
            recap_file = await sync_to_async(
                models.RecapFile.objects.select_related("recap__event").get
            )(id=recap_file_id)
        except (models.RecapFile.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Recap file not found.")

        recap_id = recap_file.recap_id
        if not recap_id:
            raise GraphQLError("This file isn't attached to a recap.")

        # Cross-tenant write gate (delete of a recap-linked file + its blob).
        # Tenant scopes through recap.event; recap__event is select_related
        # above. BAs may remove files from their own-tenant recaps (mobile),
        # so ambassadors are not blocked — only foreign-tenant access is.
        await self._assert_caller_authorized_for_recap_tenant(
            recap_file.recap.event.tenant_id
            if recap_file.recap and recap_file.recap.event_id
            else None,
            action="remove files from",
        )

        @sync_to_async
        def remove():
            with transaction.atomic():
                blob_name = extract_blob_name_from_url(str(recap_file.file))
                recap_file.delete()
                recap = models.Recap.objects.get(id=recap_id)
                return recap, blob_name

        recap, blob_name = await remove()
        # Delete the GCS object outside the transaction — if it fails the
        # DB row is already gone, which is the user-visible outcome they
        # asked for; an orphaned blob is harmless and swept later.
        if blob_name:
            try:
                delete_blob(blob_name)
            except Exception:
                logger.exception(
                    "remove_recap_file: blob delete failed for %s", blob_name
                )
        return recap

    async def delete_custom_recap_file(self) -> models.CustomRecap:
        """Remove one file from a CustomRecap's Evidences gallery.

        Custom-template counterpart to remove_recap_file. Hard-deletes the
        CustomRecapFile row so a misfiled image (e.g. a receipt that landed
        under "Table setup") can be removed, then returns the parent custom
        recap so the client re-renders the gallery from the response.

        Unlike remove_recap_file, the GCS blob is LEFT in place (audit /
        recoverability) — only the DB row is removed. Auth mirrors
        delete_custom_recap: ambassadors blocked, spark-admin anywhere,
        other roles only inside their own tenant.
        """
        if not isinstance(self.input, inputs.DeleteCustomRecapFileInput):
            raise GraphQLError("Invalid input type.")

        try:
            recap_file_id = resolve_id_to_int(self.input.id)
            recap_file = await sync_to_async(
                models.CustomRecapFile.objects.select_related("custom_recap").get
            )(id=recap_file_id)
        except (
            models.CustomRecapFile.DoesNotExist,
            TypeError,
            ValueError,
            GraphQLError,
        ):
            raise GraphQLError("Recap file not found.")

        custom_recap = recap_file.custom_recap
        if custom_recap is None:
            raise GraphQLError("This file isn't attached to a custom recap.")

        # CustomRecap carries its own denormalized tenant_id — scope off it.
        await self._assert_can_delete_recap(custom_recap.tenant_id)

        @sync_to_async
        def remove():
            with transaction.atomic():
                recap_file.delete()
            return models.CustomRecap.objects.get(id=custom_recap.id)

        return await remove()

    async def set_custom_recap_file_category(self) -> models.CustomRecap:
        """Move one CustomRecapFile into a different section.

        Re-files a clumped/miscategorized image — e.g. a receipt the
        Connecteam import left uncategorized — into the tenant's Receipts
        (or any) section without re-uploading. The recap view groups files
        by category, so the file re-groups immediately. Returns the parent
        custom recap.

        `file_recap_category_id` resolves through the shared tenant-aware
        resolver (real id, "1"/"2" sentinels, or null → uncategorized).
        Auth mirrors add_custom_recap_file — foreign-tenant access blocked.
        """
        if not isinstance(self.input, inputs.SetCustomRecapFileCategoryInput):
            raise GraphQLError("Invalid input type.")

        try:
            recap_file_id = resolve_id_to_int(self.input.id)
            recap_file = await sync_to_async(
                models.CustomRecapFile.objects.select_related("custom_recap").get
            )(id=recap_file_id)
        except (
            models.CustomRecapFile.DoesNotExist,
            TypeError,
            ValueError,
            GraphQLError,
        ):
            raise GraphQLError("Recap file not found.")

        custom_recap = recap_file.custom_recap
        if custom_recap is None:
            raise GraphQLError("This file isn't attached to a custom recap.")

        await self._assert_caller_authorized_for_recap_tenant(
            custom_recap.tenant_id,
            action="recategorize files on",
            record_label="Custom recap",
        )

        @sync_to_async
        def apply():
            with transaction.atomic():
                category = _resolve_file_recap_category(
                    self.input.file_recap_category_id,
                    tenant_id=getattr(custom_recap, "tenant_id", None),
                )
                recap_file.file_recap_category = category
                recap_file.save(update_fields=["file_recap_category"])
            return models.CustomRecap.objects.get(id=custom_recap.id)

        return await apply()

    async def delete_recap_file(self) -> bool:
        """Delete a recap file and its blob from GCS."""
        if not isinstance(self.input, inputs.DeleteRecapFileInput):
            raise GraphQLError("Invalid input type.")

        try:
            recap_file_id = resolve_id_to_int(self.input.id)
            recap_file = await sync_to_async(models.RecapFile.objects.get)(
                id=recap_file_id
            )
        except (models.RecapFile.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Recap file not found.")

        @sync_to_async
        def delete_file_with_references():
            with transaction.atomic():
                if recap_file.recap_id:
                    raise GraphQLError(
                        "Recap file is linked to a recap. Update the recap before deleting this file."
                    )

                blob_name = extract_blob_name_from_url(str(recap_file.file))
                recap_file.delete()
                return blob_name

        blob_name = await delete_file_with_references()
        if blob_name:
            delete_blob(blob_name)
        return True

    async def approve_recap(self) -> models.Recap:
        """Approve or decline a recap."""
        if not isinstance(self.input, inputs.ApproveRecapInput):
            raise GraphQLError("Invalid input type.")

        try:
            recap_id = resolve_id_to_int(self.input.id)
            recap = await sync_to_async(
                models.Recap.objects.select_related("event").get
            )(id=recap_id)
        except (models.Recap.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Recap not found.")

        # Cross-tenant write gate: Recap scopes its tenant through the event
        # (no direct tenant FK). `event` is select_related above so reading
        # event.tenant_id here doesn't trigger a sync DB fetch in async ctx.
        await self._assert_caller_authorized_for_recap_tenant(
            recap.event.tenant_id if recap.event_id else None,
            action="approve",
            block_ambassadors=True,
        )

        @sync_to_async
        def approve_recap_transaction():
            with transaction.atomic():
                recap.approved = self.input.approved
                recap.updated_by = self.user
                recap.save()
                return recap

        recap = await approve_recap_transaction()
        if self.input.approved:
            recap = await sync_to_async(
                models.Recap.objects.select_related(
                    "event",
                    "event__tenant",
                    "event__rmm_asigned",
                    "event__timezone",
                    "job",
                    "retailer",
                    "timezone",
                    "ambassador",
                    "ambassador__user",
                ).get
            )(id=recap.id)
            try:
                # Offload the slow client/RMM email + PDF to Cloud Tasks when
                # the feature is configured so this mutation returns fast. When
                # it's off (the default), enqueue() returns False and we run the
                # exact same notify inline — behavior unchanged from before.
                enqueued = await sync_to_async(enqueue)(
                    "/api/tasks/recap-approved-notify",
                    {"recap_id": recap.id, "recap_kind": "legacy"},
                )
                if not enqueued:
                    await _notify_recap_approved_to_rmm_or_clients(recap)
            except Exception:
                logger.exception(
                    "recap-approved notification failed for recap=%s", recap.id
                )
            await _notify_recap_approved_to_ambassador_by_push(recap)

        return recap

    async def approve_custom_recap(self) -> models.CustomRecap:
        """Approve or decline a custom recap."""
        if not isinstance(self.input, inputs.ApproveCustomRecapInput):
            raise GraphQLError("Invalid input type.")

        try:
            custom_recap_id = resolve_id_to_int(self.input.id)
            custom_recap = await sync_to_async(models.CustomRecap.objects.get)(
                id=custom_recap_id
            )
        except (models.CustomRecap.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Custom recap not found.")

        # Cross-tenant write gate. CustomRecap carries a direct tenant FK.
        await self._assert_caller_authorized_for_recap_tenant(
            custom_recap.tenant_id,
            action="approve",
            block_ambassadors=True,
            record_label="Custom recap",
        )

        @sync_to_async
        def approve_custom_recap_transaction():
            with transaction.atomic():
                custom_recap.approved = self.input.approved
                custom_recap.updated_by = self.user
                custom_recap.save()
                return custom_recap

        custom_recap = await approve_custom_recap_transaction()
        if self.input.approved:
            custom_recap = await sync_to_async(
                models.CustomRecap.objects.select_related(
                    "event",
                    "event__tenant",
                    "event__rmm_asigned",
                    "event__timezone",
                    "job",
                    "retailer",
                    "timezone",
                    "ambassador",
                    "ambassador__user",
                ).get
            )(id=custom_recap.id)
            try:
                # Offload the slow client/RMM email + PDF to Cloud Tasks when
                # the feature is configured so this mutation returns fast. When
                # it's off (the default), enqueue() returns False and we run the
                # exact same notify inline — behavior unchanged from before.
                enqueued = await sync_to_async(enqueue)(
                    "/api/tasks/recap-approved-notify",
                    {"recap_id": custom_recap.id, "recap_kind": "custom"},
                )
                if not enqueued:
                    await _notify_recap_approved_to_rmm_or_clients(custom_recap)
            except Exception:
                logger.exception(
                    "recap-approved notification failed for custom_recap=%s",
                    custom_recap.id,
                )

        return custom_recap

    async def decline_custom_recap(self) -> models.CustomRecap:
        """Decline a custom recap."""
        if not isinstance(self.input, inputs.DeclineCustomRecapInput):
            raise GraphQLError("Invalid input type.")

        try:
            custom_recap_id = resolve_id_to_int(self.input.id)
            custom_recap = await sync_to_async(models.CustomRecap.objects.get)(
                id=custom_recap_id
            )
        except (models.CustomRecap.DoesNotExist, TypeError, ValueError, GraphQLError):
            raise GraphQLError("Custom recap not found.")

        # Cross-tenant write gate. CustomRecap carries a direct tenant FK.
        await self._assert_caller_authorized_for_recap_tenant(
            custom_recap.tenant_id,
            action="decline",
            block_ambassadors=True,
            record_label="Custom recap",
        )

        @sync_to_async
        def decline_custom_recap_transaction():
            with transaction.atomic():
                custom_recap.approved = False
                custom_recap.updated_by = self.user
                custom_recap.save()
                return custom_recap

        return await decline_custom_recap_transaction()

    async def generate_recap_pdf(self) -> models.RecapFile:
        """Generate a PDF with recap details and images, upload it, and return the file."""
        if not isinstance(self.input, inputs.GenerateRecapPdfInput):
            raise GraphQLError("Invalid input type.")

        try:
            recap_id = resolve_id_to_int(self.input.id)
        except (TypeError, ValueError, GraphQLError):
            raise GraphQLError("Recap not found.")

        @sync_to_async
        def fetch_recap():
            return (
                models.Recap.objects.select_related(
                    "event",
                    "event__tenant",
                    "event__event_type",
                    # event_date PDF fallback walks to the parent request when
                    # Event.date is null (pre-#718 events); prefetch it so the
                    # render stays a single query.
                    "event__request",
                    "job",
                    "retailer",
                    "ambassador",
                    "ambassador__user",
                )
                .prefetch_related(
                    Prefetch(
                        "recap_files",
                        queryset=models.RecapFile.objects.select_related(
                            "file_type",
                            "file_recap_category",
                        ),
                    ),
                    "consumer_engagements",
                    Prefetch(
                        "product_samples",
                        queryset=models.ProductSamples.objects.select_related(
                            "product"
                        ),
                    ),
                    Prefetch(
                        "sales_performance",
                        queryset=models.SalesPerformance.objects.select_related(
                            "product",
                            "type_of_good",
                        ),
                    ),
                    "consumer_feedback",
                    "account_feedback",
                )
                .get(id=recap_id)
            )

        try:
            recap = await fetch_recap()
        except models.Recap.DoesNotExist:
            raise GraphQLError("Recap not found.")

        # Cross-tenant READ gate (follow-up to #708). This accessor loaded a
        # Recap by raw PK gated only by StrictIsAuthenticated, so any
        # authenticated user could render + persist another tenant's recap as
        # a PDF — and read its embedded image content — by guessing the id.
        # `event` is select_related above, so reading event.tenant_id here is
        # async-safe. Deny => GraphQLError => the field returns success=False.
        await self._assert_caller_authorized_for_recap_tenant(
            recap.event.tenant_id, action="export"
        )

        # Pre-filter to image-typed files BEFORE downloading — we used
        # to download every blob (including PDFs / docs / videos) then
        # drop non-images, paying a full GCS round-trip per skipped
        # file. `should_embed_recap_file` looks at extension + file_type
        # without I/O so we can cull early.
        candidates: list[tuple[models.RecapFile, str]] = []
        for recap_file in recap.recap_files.all():
            if not should_embed_recap_file(recap_file):
                continue
            blob_name = extract_blob_name_from_url(str(recap_file.file))
            if not blob_name:
                continue
            candidates.append((recap_file, blob_name))

        # Parallel blob fetch — biggest single perf win. Sequential
        # downloads on a 60-image recap were ~18–30s of pure I/O wait;
        # 16 workers brings that under 3s for the same recap.
        import concurrent.futures as _cf

        def _fetch_one(item):
            recap_file, blob_name = item
            try:
                data = download_blob_bytes(blob_name)
            except Exception:
                return None
            if not data:
                return None
            # Last-resort content sniff for files where the extension
            # lies (BAs renaming a .heic to .jpg etc).
            if not is_image_bytes(data):
                return None
            return {
                "name": recap_file.name,
                "bytes": data,
                "category": (
                    recap_file.file_recap_category.name
                    if recap_file.file_recap_category
                    else "Uncategorized"
                ),
            }

        image_entries = []
        if candidates:
            # 16 ~ sweet spot for GCS HTTP/2 keep-alive in the Cloud
            # Run container. More than this and we just queue inside
            # urllib's connection pool.
            with _cf.ThreadPoolExecutor(max_workers=16) as pool:
                for entry in pool.map(_fetch_one, candidates):
                    if entry is not None:
                        image_entries.append(entry)

        # Render PDF off-thread so a slow WeasyPrint pass doesn't block
        # the event loop and so we don't trip Django's async-context
        # warning when downstream ORM access happens during render.
        # fresh_db_connection: build_recap_pdf does ORM reads while rendering,
        # and the non-thread-sensitive pool thread can otherwise reuse a
        # server-closed connection ("the connection is closed"). Force a fresh
        # one per call (same guard as ambassadorsBookedOnDate).
        from utils.db import fresh_db_connection

        pdf_bytes = await sync_to_async(
            fresh_db_connection(build_recap_pdf), thread_sensitive=False
        )(recap, image_entries)
        timestamp = django_timezone.now().strftime("%Y%m%d%H%M%S")
        blob_name = f"recaps/pdfs/{recap.uuid}-{timestamp}.pdf"
        upload_bytes(blob_name, pdf_bytes, content_type="application/pdf")

        @sync_to_async
        def create_recap_pdf_file():
            file_type = FileType.objects.filter(
                Q(extension__iexact=".pdf") | Q(extension__iexact="pdf")
            ).first()
            if not file_type:
                raise GraphQLError("No PDF file type available.")
            existing_files = list(
                models.RecapFile.objects.filter(recap=recap, file_type=file_type)
            )
            existing_blob_names = [
                extract_blob_name_from_url(str(item.file)) for item in existing_files
            ]
            if existing_files:
                models.RecapFile.objects.filter(
                    id__in=[item.id for item in existing_files]
                ).delete()
            recap_file = models.RecapFile.objects.create(
                name=f"Recap PDF - {recap.name}",
                file=blob_name,
                file_type=file_type,
                recap=recap,
                approved=False,
                created_by=self.user,
            )
            return recap_file, existing_blob_names

        try:
            recap_file, existing_blob_names = await create_recap_pdf_file()
        except Exception:
            delete_blob(blob_name)
            raise
        for existing_blob_name in existing_blob_names:
            if existing_blob_name:
                delete_blob(existing_blob_name)
        return recap_file

    async def build_campaign_report_pdf_with_meta(
        self,
        *,
        recap_ids: list,
        title: str | None,
        subtitle: str | None,
    ) -> tuple:
        """Shared core for both `generate_campaign_report_pdf` (which
        uploads to GCS and returns a URL) and `email_campaign_report`
        (which attaches the bytes to an email).

        Returns a 6-tuple:
          (pdf_bytes, recap_count, total_consumers, title, subtitle, tenant_name)

        Side-effects: none — pure compute + GCS reads. Caller decides
        whether to upload + return a URL or hand the bytes to an email.
        """
        raw_ids = list(recap_ids or [])
        if not raw_ids:
            raise GraphQLError("At least one recap is required.")
        if len(raw_ids) > 50:
            raise GraphQLError(
                "Campaign report is limited to 50 recaps per export."
            )

        # Recaps can be legacy (Recap) OR custom (CustomRecap) — two tables
        # whose PKs collide, so we decode the Relay global-id TYPE (never the
        # bare int) to route each id to the right table. Selecting a custom
        # recap used to resolve to nothing here → "None of the supplied
        # recap ids were found".
        import base64 as _b64

        decoded: list[tuple[str, int]] = []
        for raw in raw_ids:
            kind = "Recap"
            pk: int | None = None
            if isinstance(raw, int):
                pk = raw
            elif isinstance(raw, str) and raw.isdigit():
                pk = int(raw)
            else:
                try:
                    tname, sid = (
                        _b64.b64decode(str(raw).encode()).decode().split(":", 1)
                    )
                    if "customrecap" in tname.lower():
                        kind = "CustomRecap"
                    pk = int(sid)
                except Exception:
                    raise GraphQLError(f"Invalid recap id: {raw!r}")
            decoded.append((kind, pk))

        @sync_to_async
        def fetch_recaps():
            legacy_pks = [pk for (k, pk) in decoded if k == "Recap"]
            custom_pks = [pk for (k, pk) in decoded if k == "CustomRecap"]

            legacy_by_id: dict[int, object] = {}
            if legacy_pks:
                legacy_by_id = {
                    r.id: r
                    for r in models.Recap.objects.select_related(
                        "event",
                        "event__tenant",
                        "event__event_type",
                        "event__retailer",
                        "event__state",
                        "job",
                        "retailer",
                        "ambassador",
                        "ambassador__user",
                    )
                    .prefetch_related(
                        Prefetch(
                            "recap_files",
                            queryset=models.RecapFile.objects.select_related(
                                "file_type",
                                "file_recap_category",
                            ),
                        ),
                        "consumer_engagements",
                        Prefetch(
                            "product_samples",
                            queryset=models.ProductSamples.objects.select_related(
                                "product"
                            ),
                        ),
                        Prefetch(
                            "sales_performance",
                            queryset=models.SalesPerformance.objects.select_related(
                                "product",
                                "type_of_good",
                            ),
                        ),
                        "consumer_feedback",
                        "account_feedback",
                    )
                    .filter(id__in=legacy_pks)
                }

            custom_by_id: dict[int, object] = {}
            if custom_pks:
                custom_by_id = {
                    r.id: r
                    for r in models.CustomRecap.objects.select_related(
                        "event",
                        "event__tenant",
                        "event__retailer",
                        "event__state",
                    )
                    .prefetch_related(
                        Prefetch(
                            "custom_recap_files",
                            queryset=models.CustomRecapFile.objects.select_related(
                                "file_type",
                                "file_recap_category",
                            ),
                        ),
                        Prefetch(
                            "custom_field_value",
                            queryset=models.CustomFieldValue.objects.select_related(
                                "custom_field",
                                "custom_field__recap_section",
                            ),
                        ),
                    )
                    .filter(id__in=custom_pks)
                }

            ordered: list[tuple[str, object]] = []
            for (k, pk) in decoded:
                obj = (custom_by_id if k == "CustomRecap" else legacy_by_id).get(pk)
                if obj is not None:
                    ordered.append((k, obj))
            return ordered

        ordered = await fetch_recaps()
        if not ordered:
            raise GraphQLError("None of the supplied recap ids were found.")

        # Everything below reads recap relations (files, file_type,
        # consumer_engagements, event__tenant) and renders the PDF. ALL of
        # it must run in a sync thread: a single missed prefetch (e.g.
        # should_embed_recap_file reading file_type) on the async event loop
        # raises SynchronousOnlyOperation — which is exactly what broke
        # download + email for selections containing custom recaps. Mirrors
        # the single-recap generate_custom_recap_pdf pattern.
        @sync_to_async
        def render_campaign_pdf():
            recaps = [obj for (_k, obj) in ordered]

            # Campaign reports bundle N recaps × M files each. Build one flat
            # candidate list (recap_idx, file, blob), fetch them all in
            # parallel, then re-bucket. Branch the file relation by recap
            # type: legacy uses recap_files/.file, custom uses
            # custom_recap_files/.url.
            import concurrent.futures as _cf

            candidates: list[tuple[int, object, str]] = []
            for idx, (kind, recap) in enumerate(ordered):
                files = (
                    recap.custom_recap_files.all()
                    if kind == "CustomRecap"
                    else recap.recap_files.all()
                )
                for rf in files:
                    if not should_embed_recap_file(rf):
                        continue
                    raw_url = rf.url if kind == "CustomRecap" else rf.file
                    blob_name = extract_blob_name_from_url(str(raw_url))
                    if not blob_name:
                        continue
                    candidates.append((idx, rf, blob_name))

            def _fetch(item):
                idx, rf, blob_name = item
                try:
                    data = download_blob_bytes(blob_name)
                except Exception:
                    return None
                if not data or not is_image_bytes(data):
                    return None
                cat = getattr(rf, "file_recap_category", None)
                return (
                    idx,
                    {
                        "name": getattr(rf, "name", None),
                        "bytes": data,
                        "category": cat.name if cat else "Uncategorized",
                    },
                )

            per_recap: dict[int, list[dict]] = {
                i: [] for i in range(len(ordered))
            }
            if candidates:
                with _cf.ThreadPoolExecutor(max_workers=16) as pool:
                    for result in pool.map(_fetch, candidates):
                        if result is not None:
                            idx, entry = result
                            per_recap[idx].append(entry)

            recaps_with_images: list = []
            total_consumers = 0
            any_consumer_data = False
            for idx, (kind, recap) in enumerate(ordered):
                recaps_with_images.append((recap, per_recap[idx]))
                if kind == "CustomRecap":
                    te = getattr(recap, "total_engagements", None)
                    if isinstance(te, (int, float)) and te:
                        total_consumers += int(te)
                        any_consumer_data = True
                else:
                    for eng in recap.consumer_engagements.all():
                        tc = getattr(eng, "total_consumer", None)
                        if isinstance(tc, (int, float)):
                            total_consumers += int(tc)
                            any_consumer_data = True
                        break  # only the first row

            # Title / subtitle defaults
            resolved_title = (title or "").strip() or "Campaign Report"
            first_event = getattr(recaps[0], "event", None)
            tenant = (
                getattr(first_event, "tenant", None) if first_event else None
            )
            tenant_name = getattr(tenant, "name", None) or ""
            resolved_subtitle = (subtitle or "").strip() or (
                tenant_name or "Sampling Campaign"
            )

            # ─── Event metadata block for the email body ─────────────────
            # We surface the event name(s), date(s), state(s), and store
            # name(s) above the KPI panel so the recipient sees campaign
            # context without opening the PDF. All collapsing logic is
            # defensive — events with no date/state/retailer just drop out
            # of the label instead of rendering "None".
            from datetime import datetime as _dt

            def _fmt_date(dt):
                if not dt:
                    return None
                try:
                    return dt.strftime("%b %-d, %Y")
                except Exception:
                    return None

            events_seen = []
            event_names: list[str] = []
            event_dates: list = []
            state_codes: list[str] = []
            retailer_names: list[str] = []
            store_addresses: list[str] = []
            for recap in recaps:
                ev = getattr(recap, "event", None)
                if ev is None or ev.id in {e.id for e in events_seen}:
                    continue
                events_seen.append(ev)
                if ev.name:
                    event_names.append(ev.name)
                d = getattr(ev, "date", None) or getattr(ev, "start_time", None)
                if d:
                    event_dates.append(d)
                st = getattr(ev, "state", None)
                if st and getattr(st, "code", None):
                    state_codes.append(st.code)
                ret = getattr(ev, "retailer", None)
                if ret and getattr(ret, "name", None):
                    retailer_names.append(ret.name)
                addr = (getattr(ev, "address", None) or "").strip()
                if addr:
                    store_addresses.append(addr)

            event_count = len(events_seen)

            # Event name: single name, or "<first> + N more"
            if not event_names:
                event_label = None
            elif len(event_names) == 1:
                event_label = event_names[0]
            else:
                event_label = (
                    f"{event_names[0]} + {len(event_names) - 1} more"
                )

            # Date / date range: single date, identical dates, or range.
            date_label = None
            if event_dates:
                days = sorted({d.date() for d in event_dates if d})
                if len(days) == 1:
                    date_label = days[0].strftime("%b %-d, %Y")
                elif days:
                    same_year = days[0].year == days[-1].year
                    start_fmt = "%b %-d" if same_year else "%b %-d, %Y"
                    date_label = (
                        f"{days[0].strftime(start_fmt)} – "
                        f"{days[-1].strftime('%b %-d, %Y')}"
                    )

            # State: unique codes, comma-joined; cap at 3.
            unique_states = sorted(set(state_codes))
            if not unique_states:
                state_label = None
            elif len(unique_states) <= 3:
                state_label = ", ".join(unique_states)
            else:
                state_label = (
                    f"{', '.join(unique_states[:3])} + "
                    f"{len(unique_states) - 3} more"
                )

            # Location: single retailer name + address if both present;
            # otherwise dedup retailer names or store count.
            location_label = None
            unique_retailers = []
            seen_r = set()
            for n in retailer_names:
                if n.lower() not in seen_r:
                    seen_r.add(n.lower())
                    unique_retailers.append(n)
            if len(events_seen) == 1:
                # Single-event: combine retailer + address on one line.
                bits: list[str] = []
                if unique_retailers:
                    bits.append(unique_retailers[0])
                if store_addresses:
                    bits.append(store_addresses[0])
                if bits:
                    location_label = " · ".join(bits)
            elif unique_retailers:
                if len(unique_retailers) == 1:
                    location_label = (
                        f"{unique_retailers[0]} ({event_count} stores)"
                    )
                elif len(unique_retailers) <= 3:
                    location_label = ", ".join(unique_retailers)
                else:
                    location_label = (
                        f"{', '.join(unique_retailers[:3])} + "
                        f"{len(unique_retailers) - 3} more"
                    )

            event_meta = {
                "event_count": event_count,
                "event_label": event_label,
                "date_label": date_label,
                "state_label": state_label,
                "location_label": location_label,
                "client_name": tenant_name or None,
            }

            pdf_bytes = build_campaign_report_pdf(
                title=resolved_title,
                subtitle=resolved_subtitle,
                recaps_with_images=recaps_with_images,
            )

            return (
                pdf_bytes,
                len(recaps),
                total_consumers if any_consumer_data else None,
                resolved_title,
                resolved_subtitle,
                tenant_name,
                event_meta,
            )

        return await render_campaign_pdf()

    async def generate_campaign_report_pdf(self) -> str:
        """Combine N recaps into one client-deliverable PDF.

        Returns the public GCS URL of the rendered file. We don't tie
        this to a single RecapFile row because the deliverable spans
        many recaps; it lives at `campaign-reports/<uuid>-<ts>.pdf` and
        is purely a one-shot artifact callers can re-generate any time.

        Permission posture: requires authenticated user (set at the
        resolver layer). Cross-tenant isolation is enforced by filtering
        the recap set to the caller's accessible tenants — see
        `_accessible_tenant_ids` on the parent service.
        """
        if not isinstance(self.input, inputs.GenerateCampaignReportPdfInput):
            raise GraphQLError("Invalid input type.")

        # Delegate to the shared builder (handles legacy + custom recaps),
        # then upload the bytes and return the public URL. This used to
        # duplicate a legacy-only fetch, so selecting a custom recap failed
        # with "None of the supplied recap ids were found".
        (
            pdf_bytes,
            recap_count,
            *_rest,
        ) = await self.build_campaign_report_pdf_with_meta(
            recap_ids=self.input.recap_ids or [],
            title=self.input.title,
            subtitle=self.input.subtitle,
        )

        timestamp = django_timezone.now().strftime("%Y%m%d%H%M%S")
        blob_name = f"campaign-reports/{timestamp}-{recap_count}-recaps.pdf"
        upload_bytes(blob_name, pdf_bytes, content_type="application/pdf")
        return public_url(blob_name)

    async def generate_custom_recap_pdf(self) -> models.CustomRecapFile:
        """Generate a PDF with custom recap details and images."""
        if not isinstance(self.input, inputs.GenerateCustomRecapPdfInput):
            raise GraphQLError("Invalid input type.")

        try:
            custom_recap_id = resolve_id_to_int(self.input.id)
        except (TypeError, ValueError, GraphQLError):
            raise GraphQLError("Custom recap not found.")

        @sync_to_async
        def fetch_custom_recap():
            return (
                models.CustomRecap.objects.select_related(
                    "event",
                    "event__tenant",
                    "event__event_type",
                    # Event Date / State / Retailer fallbacks (mirror the
                    # event_date / event_state / event_retailer resolvers):
                    # the PDF helpers walk these chains when the recap's own
                    # FKs are null, so prefetch them to keep the render a
                    # single query and resolve the "Event Date / State /
                    # Retailer shows N/A" symptom on pre-#718 events.
                    "event__request",
                    "event__request__retailer",
                    "event__location__state",
                    "event__state",
                    "event__retailer__location__state",
                    "job",
                    "retailer",
                    "location",
                    "state",
                    "tenant",
                    "timezone",
                    "ambassador",
                    "ambassador__user",
                    "custom_recap_template",
                    "created_by",
                    "updated_by",
                )
                .prefetch_related(
                    Prefetch(
                        "custom_recap_files",
                        queryset=models.CustomRecapFile.objects.select_related(
                            "file_type",
                            "file_recap_category",
                        ),
                    ),
                    Prefetch(
                        "custom_recap_product_sample",
                        queryset=models.CustomRecapProductSample.objects.select_related(
                            "product"
                        ),
                    ),
                    Prefetch(
                        "custom_recap_sale_performance",
                        queryset=models.CustomRecapSalePerformance.objects.select_related(
                            "product",
                            "type_of_good",
                        ),
                    ),
                    Prefetch(
                        "custom_field_value",
                        queryset=models.CustomFieldValue.objects.select_related(
                            "custom_field",
                            "custom_field__recap_section",
                        ),
                    ),
                )
                .get(id=custom_recap_id)
            )

        try:
            custom_recap = await fetch_custom_recap()
        except models.CustomRecap.DoesNotExist:
            raise GraphQLError("Custom recap not found.")

        # Cross-tenant READ gate (follow-up to #708). Same leak as the legacy
        # generate_recap_pdf path: loaded by raw PK gated only by
        # StrictIsAuthenticated. CustomRecap carries a direct tenant FK.
        await self._assert_caller_authorized_for_recap_tenant(
            custom_recap.tenant_id,
            action="export",
            record_label="Custom recap",
        )

        @sync_to_async
        def build_custom_recap_pdf_bytes():
            # Parallel blob fetch — same approach as the standard recap
            # path. Big custom recaps (Liquid Death has 60+ images per
            # event) used to take 20s+ sequentially.
            import concurrent.futures as _cf

            candidates: list[tuple[object, str]] = []
            for crf in custom_recap.custom_recap_files.all():
                if not should_embed_recap_file(crf):
                    continue
                blob_name = extract_blob_name_from_url(str(crf.url))
                if not blob_name:
                    continue
                candidates.append((crf, blob_name))

            def _fetch(item):
                crf, blob_name = item
                try:
                    data = download_blob_bytes(blob_name)
                except Exception:
                    return None
                if not data or not is_image_bytes(data):
                    return None
                return {
                    "name": crf.name,
                    "bytes": data,
                    "category": (
                        crf.file_recap_category.name
                        if crf.file_recap_category
                        else "Uncategorized"
                    ),
                }

            image_entries: list[dict] = []
            if candidates:
                with _cf.ThreadPoolExecutor(max_workers=16) as pool:
                    for entry in pool.map(_fetch, candidates):
                        if entry is not None:
                            image_entries.append(entry)

            # Image-type custom FIELDS (e.g. "Product Purchase Receipt
            # (Image)") store the GCS blob path as their CustomFieldValue
            # .value. Those aren't custom_recap_files, so the loop above
            # never fetched them — they rendered as raw path text in the
            # PDF. Collect any field value that looks like an image blob
            # (non-empty string whose path ends in a known image
            # extension) and fetch it through the SAME parallel pool. We
            # store raw bytes keyed by the original blob path; the HTML
            # builder calls bytes_to_data_uri, which performs the same
            # HEIC→JPG conversion the attachments path relies on (so a
            # .heic receipt still renders — WeasyPrint can't decode HEIC).
            field_candidates: list[tuple[str, str]] = []
            seen_field_blobs: set[str] = set()
            for cfv in custom_recap.custom_field_value.all():
                raw_value = cfv.value
                if not isinstance(raw_value, str) or not raw_value.strip():
                    continue
                path = raw_value.split("?", 1)[0].split("#", 1)[0]
                _, _, ext = path.rpartition(".")
                if not ext or f".{ext.lower()}" not in IMAGE_EXTENSIONS:
                    continue
                if raw_value in seen_field_blobs:
                    continue
                blob_name = extract_blob_name_from_url(raw_value)
                if not blob_name:
                    continue
                seen_field_blobs.add(raw_value)
                # Carry the original value as the key so the renderer can
                # match it against CustomFieldValue.value.
                field_candidates.append((raw_value, blob_name))

            def _fetch_field(item):
                value_key, blob_name = item
                try:
                    data = download_blob_bytes(blob_name)
                except Exception:
                    return None
                if not data or not is_image_bytes(data):
                    return None
                return (value_key, data)

            custom_field_images: dict[str, bytes] = {}
            if field_candidates:
                with _cf.ThreadPoolExecutor(max_workers=16) as pool:
                    for result in pool.map(_fetch_field, field_candidates):
                        if result is not None:
                            value_key, data = result
                            custom_field_images[value_key] = data

            return build_recap_pdf(
                custom_recap,
                image_entries,
                custom_field_images=custom_field_images,
            )

        pdf_bytes = await build_custom_recap_pdf_bytes()
        timestamp = django_timezone.now().strftime("%Y%m%d%H%M%S")
        blob_name = f"recaps/pdfs/custom-{custom_recap.uuid}-{timestamp}.pdf"
        upload_bytes(blob_name, pdf_bytes, content_type="application/pdf")

        @sync_to_async
        def create_custom_recap_pdf_file():
            file_type = FileType.objects.filter(
                Q(extension__iexact=".pdf") | Q(extension__iexact="pdf")
            ).first()
            if not file_type:
                raise GraphQLError("No PDF file type available.")
            existing_files = list(
                models.CustomRecapFile.objects.filter(
                    custom_recap=custom_recap,
                    file_type=file_type,
                )
            )
            existing_blob_names = [
                extract_blob_name_from_url(str(item.url)) for item in existing_files
            ]
            if existing_files:
                models.CustomRecapFile.objects.filter(
                    id__in=[item.id for item in existing_files]
                ).delete()
            custom_recap_file = models.CustomRecapFile.objects.create(
                name=f"Custom Recap PDF - {custom_recap.name}",
                url=blob_name,
                file_type=file_type,
                custom_recap=custom_recap,
                approved=False,
                created_by=self.user,
            )
            return custom_recap_file, existing_blob_names

        try:
            (
                custom_recap_file,
                existing_blob_names,
            ) = await create_custom_recap_pdf_file()
        except Exception:
            delete_blob(blob_name)
            raise
        for existing_blob_name in existing_blob_names:
            if existing_blob_name:
                delete_blob(existing_blob_name)
        return custom_recap_file

    async def export_recaps_xlsx(self) -> str:
        """Generate an Excel report with all recaps for a tenant and return a signed URL."""
        if not isinstance(self.input, inputs.ExportRecapsXlsxInput):
            raise GraphQLError("Invalid input type.")

        resolved_tenant_id: int | None = None
        if self.input.tenant_id not in (None, ""):
            try:
                resolved_tenant_id = resolve_id_to_int(self.input.tenant_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid tenant ID.")

        if self.is_spark_schema_request(self.info, user=self.user):
            if resolved_tenant_id is None:
                raise GraphQLError("Tenant ID is required.")
            tenant = await self._get_tenant_without_membership(
                tenant_id=resolved_tenant_id
            )
        else:
            tenant = await self.get_user_tenant(
                self.info,
                tenant_id=resolved_tenant_id,
                user=self.user,
            )
        start_date = self.input.start_date
        end_date = self.input.end_date

        frontend_base_url = settings.ADMIN_FRONTEND_URL

        @sync_to_async
        def build_xlsx_for_tenant():
            service = RecapQueriesService()
            queryset = service.get_filtered_queryset(
                tenant_id=tenant.id,
                start_date=start_date,
                end_date=end_date,
            )
            recaps = list(
                queryset.select_related(
                    "event__request__retailer",
                    "event__request__distributor",
                    "ambassador",
                    "ambassador__user",
                )
            )
            return build_recaps_xlsx(recaps, frontend_base_url=frontend_base_url)

        xlsx_bytes = await build_xlsx_for_tenant()

        timestamp = django_timezone.now().strftime("%Y%m%d%H%M%S")
        tenant_slug = slugify(getattr(tenant, "name", "") or "tenant")
        export_prefix = f"recaps/exports/{tenant_slug}-"
        blob_name = f"{export_prefix}{timestamp}.xlsx"

        @sync_to_async
        def delete_previous_exports():
            client = get_gcs_client()
            bucket = client.bucket(settings.GS_BUCKET_NAME)
            for blob in bucket.list_blobs(prefix=export_prefix):
                if blob.name != blob_name:
                    blob.delete()

        await delete_previous_exports()
        upload_bytes(
            blob_name,
            xlsx_bytes,
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
        return public_url(blob_name)

    async def export_recap_xlsx(self) -> str:
        """Generate an Excel report for a single recap and return a signed URL."""
        if not isinstance(self.input, inputs.ExportRecapXlsxInput):
            raise GraphQLError("Invalid input type.")

        try:
            recap_id = resolve_id_to_int(self.input.id)
        except (TypeError, ValueError, GraphQLError):
            raise GraphQLError("Invalid recap ID.")

        frontend_base_url = settings.ADMIN_FRONTEND_URL

        # Cross-tenant READ gate (follow-up to #708) — resolve the recap's
        # owning tenant up front and authorize BEFORE building/uploading the
        # export. This accessor loaded a single Recap by raw PK gated only by
        # StrictIsAuthenticated, so any authenticated user could export
        # another tenant's recap data by guessing the id.
        @sync_to_async
        def fetch_recap_tenant_id():
            return (
                models.Recap.objects.select_related("event")
                .filter(id=recap_id)
                .values_list("event__tenant_id", flat=True)
                .first()
            )

        recap_tenant_id = await fetch_recap_tenant_id()
        if recap_tenant_id is None:
            raise GraphQLError("Recap not found.")
        await self._assert_caller_authorized_for_recap_tenant(
            recap_tenant_id, action="export"
        )

        @sync_to_async
        def build_xlsx_for_recap():
            try:
                recap = (
                    RecapQueriesService()
                    .get_queryset()
                    .select_related(
                        "event__request__retailer",
                        "event__request__distributor",
                        "event__tenant",
                        "ambassador",
                        "ambassador__user",
                    )
                    .get(id=recap_id)
                )
            except models.Recap.DoesNotExist:
                return None, None, None
            tenant_name = getattr(getattr(recap, "event", None), "tenant", None)
            return (
                build_recaps_xlsx([recap], frontend_base_url=frontend_base_url),
                recap.uuid,
                getattr(tenant_name, "name", None),
            )

        xlsx_bytes, recap_uuid, tenant_name = await build_xlsx_for_recap()
        if xlsx_bytes is None or recap_uuid is None:
            raise GraphQLError("Recap not found.")

        timestamp = django_timezone.now().strftime("%Y%m%d%H%M%S")
        tenant_slug = slugify(tenant_name or "tenant")
        export_prefix = f"recaps/exports/{tenant_slug}-{recap_uuid}-"
        blob_name = f"{export_prefix}{timestamp}.xlsx"

        @sync_to_async
        def delete_previous_exports():
            client = get_gcs_client()
            bucket = client.bucket(settings.GS_BUCKET_NAME)
            for blob in bucket.list_blobs(prefix=export_prefix):
                if blob.name != blob_name:
                    blob.delete()

        await delete_previous_exports()
        upload_bytes(
            blob_name,
            xlsx_bytes,
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
        return public_url(blob_name)

    async def export_custom_recaps_xlsx(self) -> str:
        """Generate an Excel report with all custom recaps for a tenant."""
        if not isinstance(self.input, inputs.ExportCustomRecapsXlsxInput):
            raise GraphQLError("Invalid input type.")

        resolved_tenant_id: int | None = None
        if self.input.tenant_id not in (None, ""):
            try:
                resolved_tenant_id = resolve_id_to_int(self.input.tenant_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid tenant ID.")

        resolved_template_id: int | None = None
        if self.input.custom_recap_template_id not in (None, ""):
            try:
                resolved_template_id = resolve_id_to_int(
                    self.input.custom_recap_template_id
                )
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid custom recap template ID.")

        if self.is_spark_schema_request(self.info, user=self.user):
            if resolved_tenant_id is None:
                raise GraphQLError("Tenant ID is required.")
            tenant = await self._get_tenant_without_membership(
                tenant_id=resolved_tenant_id
            )
        else:
            tenant = await self.get_user_tenant(
                self.info,
                tenant_id=resolved_tenant_id,
                user=self.user,
            )

        start_date = self.input.start_date
        end_date = self.input.end_date
        frontend_base_url = settings.ADMIN_FRONTEND_URL

        @sync_to_async
        def build_xlsx_for_tenant():
            service = CustomRecapQueriesService()
            queryset = service.get_filtered_queryset(
                tenant_id=tenant.id,
                custom_recap_template_id=resolved_template_id,
                start_date=start_date,
                end_date=end_date,
            )
            custom_recaps = list(queryset)
            return build_recaps_xlsx(
                custom_recaps,
                frontend_base_url=frontend_base_url,
            )

        xlsx_bytes = await build_xlsx_for_tenant()

        timestamp = django_timezone.now().strftime("%Y%m%d%H%M%S")
        tenant_slug = slugify(getattr(tenant, "name", "") or "tenant")
        export_prefix = f"custom-recaps/exports/{tenant_slug}-"
        blob_name = f"{export_prefix}{timestamp}.xlsx"

        @sync_to_async
        def delete_previous_exports():
            client = get_gcs_client()
            bucket = client.bucket(settings.GS_BUCKET_NAME)
            for blob in bucket.list_blobs(prefix=export_prefix):
                if blob.name != blob_name:
                    blob.delete()

        await delete_previous_exports()
        upload_bytes(
            blob_name,
            xlsx_bytes,
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
        return public_url(blob_name)

    async def export_custom_recap_xlsx(self) -> str:
        """Generate an Excel report for a single custom recap."""
        if not isinstance(self.input, inputs.ExportCustomRecapXlsxInput):
            raise GraphQLError("Invalid input type.")

        try:
            custom_recap_id = resolve_id_to_int(self.input.id)
        except (TypeError, ValueError, GraphQLError):
            raise GraphQLError("Invalid custom recap ID.")

        frontend_base_url = settings.ADMIN_FRONTEND_URL

        # Cross-tenant READ gate (follow-up to #708) — resolve the custom
        # recap's owning tenant up front and authorize BEFORE building the
        # export. Single-recap-by-id accessor previously gated only by
        # StrictIsAuthenticated. CustomRecap carries a direct tenant FK.
        @sync_to_async
        def fetch_custom_recap_tenant_id():
            return (
                models.CustomRecap.objects.filter(id=custom_recap_id)
                .values_list("tenant_id", flat=True)
                .first()
            )

        custom_recap_tenant_id = await fetch_custom_recap_tenant_id()
        if custom_recap_tenant_id is None:
            raise GraphQLError("Custom recap not found.")
        await self._assert_caller_authorized_for_recap_tenant(
            custom_recap_tenant_id,
            action="export",
            record_label="Custom recap",
        )

        @sync_to_async
        def build_xlsx_for_custom_recap():
            try:
                custom_recap = CustomRecapQueriesService().get_queryset().get(
                    id=custom_recap_id
                )
            except models.CustomRecap.DoesNotExist:
                return None, None, None

            tenant_name = getattr(custom_recap.tenant, "name", None) or getattr(
                getattr(custom_recap, "event", None), "tenant", None
            )
            return (
                build_recaps_xlsx([custom_recap], frontend_base_url=frontend_base_url),
                custom_recap.uuid,
                getattr(tenant_name, "name", None)
                if not isinstance(tenant_name, str)
                else tenant_name,
            )

        xlsx_bytes, custom_recap_uuid, tenant_name = await build_xlsx_for_custom_recap()
        if xlsx_bytes is None or custom_recap_uuid is None:
            raise GraphQLError("Custom recap not found.")

        timestamp = django_timezone.now().strftime("%Y%m%d%H%M%S")
        tenant_slug = slugify(tenant_name or "tenant")
        export_prefix = f"custom-recaps/exports/{tenant_slug}-{custom_recap_uuid}-"
        blob_name = f"{export_prefix}{timestamp}.xlsx"

        @sync_to_async
        def delete_previous_exports():
            client = get_gcs_client()
            bucket = client.bucket(settings.GS_BUCKET_NAME)
            for blob in bucket.list_blobs(prefix=export_prefix):
                if blob.name != blob_name:
                    blob.delete()

        await delete_previous_exports()
        upload_bytes(
            blob_name,
            xlsx_bytes,
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
        return public_url(blob_name)

    async def get_recap_file_download_url(self) -> str:
        """Return a signed download URL for a recap or custom recap file.

        Cross-tenant READ gate (follow-up to #708). This accessor returns a
        download URL for the file *content* of a recap file looked up by raw
        uuid. The old code split on `is_spark_schema_request` (a role-slug
        check that misses staff / superuser / @igniteproductions.co admins)
        and otherwise scoped to the caller's default tenant only — neither
        reused the authoritative admin model from #708. We now resolve the
        file's owning tenant from its parent recap and authorize via the
        shared gate: admins (resolved from the DB row) get any tenant, every
        other role only their own. A file with no resolvable parent recap
        (detached blob, recap=None) has no tenant and is denied.
        """
        if not isinstance(self.input, inputs.RecapFileDownloadUrlInput):
            raise GraphQLError("Invalid input type.")

        recap_file_uuid = str(self.input.uuid)

        @sync_to_async
        def fetch_recap_file():
            recap_file = (
                models.RecapFile.objects.select_related(
                    "recap",
                    "recap__event",
                )
                .filter(uuid=recap_file_uuid)
                .first()
            )
            if recap_file is not None:
                return recap_file
            return (
                models.CustomRecapFile.objects.select_related(
                    "custom_recap",
                    "custom_recap__event",
                )
                .filter(uuid=recap_file_uuid)
                .first()
            )

        recap_file = await fetch_recap_file()
        if recap_file is None:
            raise GraphQLError("Recap file not found.")

        # Derive the file's owning tenant from its parent recap. RecapFile
        # scopes tenant via recap.event.tenant_id; CustomRecapFile via
        # custom_recap.tenant_id. Both parents are select_related above so the
        # reads here are async-safe. A detached file (parent is None) yields
        # tenant_id=None, which the gate treats as a denial.
        if isinstance(recap_file, models.CustomRecapFile):
            parent = recap_file.custom_recap
            file_tenant_id = getattr(parent, "tenant_id", None)
            record_label = "Custom recap"
        else:
            parent = recap_file.recap
            event = getattr(parent, "event", None) if parent is not None else None
            file_tenant_id = getattr(event, "tenant_id", None)
            record_label = "Recap"

        await self._assert_caller_authorized_for_recap_tenant(
            file_tenant_id, action="download", record_label=record_label
        )

        file_field = getattr(recap_file, "file", None) or getattr(recap_file, "url", None)
        blob_name = extract_blob_name_from_url(str(file_field))
        if not blob_name:
            raise GraphQLError("Recap file not found.")
        return public_url(blob_name)


@strawberry.type
class RecapMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_recap(
        self,
        info: strawberry.Info,
        input: inputs.CreateRecapInput,
    ) -> types.RecapDetailResponse:
        """Create a new recap with multiple files."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            recap = await service.create_recap()
            # Reload with the same related-object strategy used by recap queries
            # so mutation responses can include nested recap data consistently.
            recap = await sync_to_async(RecapQueriesService().get_queryset().get)(
                id=recap.id
            )
            return build_mutation_response(
                types.RecapDetailResponse,
                success=True,
                message="Recap created successfully.",
                input_obj=input,
                recap=recap,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_recap(
        self,
        info: strawberry.Info,
        input: inputs.UpdateRecapInput,
    ) -> types.RecapDetailResponse:
        """Update an existing recap."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            recap = await service.update_recap()
            return build_mutation_response(
                types.RecapDetailResponse,
                success=True,
                message="Recap updated successfully.",
                input_obj=input,
                recap=recap,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_custom_recap(
        self,
        info: strawberry.Info,
        input: inputs.CreateCustomRecapInput,
    ) -> types.CustomRecapDetailResponse:
        """Create a new custom recap."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            custom_recap = await service.create_custom_recap()
            return build_mutation_response(
                types.CustomRecapDetailResponse,
                success=True,
                message="Custom recap created successfully.",
                input_obj=input,
                custom_recap=custom_recap,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.CustomRecapDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def import_connecteam_recap_pdf(
        self,
        info: strawberry.Info,
        input: inputs.ImportConnecteamRecapPdfInput,
    ) -> types.ImportConnecteamRecapPdfResponse:
        """Parse a Connecteam recap PDF and draft a CustomRecap from it.

        The flow:
          1. base64-decode + run through recaps.connecteam.parse_pdf_bytes
          2. Fetch the Event + CustomRecapTemplate (+ all CustomField rows)
          3. Match parsed labels → CustomField via normalize + fuzzy
          4. Create the CustomRecap shell + CustomFieldValue rows for
             every matched, non-empty value
          5. Return the recap + a per-field stats report so the admin
             can see exactly what landed where (and what didn't).
        """
        import base64

        from recaps.connecteam import (
            parse_pdf_bytes,
            match_fields,
            route_single_label_images,
            is_receipt_label,
        )

        user = info.context.request.user

        try:
            event_id = resolve_id_to_int(input.event_id)
            event = await sync_to_async(
                Event.objects.select_related("tenant").get
            )(id=event_id)
        except (Event.DoesNotExist, TypeError, ValueError, GraphQLError):
            return build_mutation_response(
                types.ImportConnecteamRecapPdfResponse,
                success=False,
                message="Event not found.",
                input_obj=input,
            )

        try:
            template_id = resolve_id_to_int(input.custom_recap_template_id)
            template = await sync_to_async(
                models.CustomRecapTemplate.objects.get
            )(id=template_id)
        except (
            models.CustomRecapTemplate.DoesNotExist,
            TypeError,
            ValueError,
            GraphQLError,
        ):
            return build_mutation_response(
                types.ImportConnecteamRecapPdfResponse,
                success=False,
                message="Custom recap template not found.",
                input_obj=input,
            )

        if template.tenant_id != event.tenant_id:
            return build_mutation_response(
                types.ImportConnecteamRecapPdfResponse,
                success=False,
                message="Template does not belong to the event tenant.",
                input_obj=input,
            )

        try:
            pdf_bytes = base64.b64decode(input.pdf_base64, validate=True)
        except Exception:
            return build_mutation_response(
                types.ImportConnecteamRecapPdfResponse,
                success=False,
                message="pdf_base64 is not valid base64.",
                input_obj=input,
            )

        try:
            parsed = await sync_to_async(parse_pdf_bytes)(pdf_bytes)
        except Exception as e:
            logging.getLogger(__name__).exception(
                "connecteam-import: PDF parse failed event_id=%s", event.id,
            )
            return build_mutation_response(
                types.ImportConnecteamRecapPdfResponse,
                success=False,
                message=f"Couldn't read PDF: {e}",
                input_obj=input,
            )

        if not parsed.raw_pairs:
            # Diagnostic: show the first ~200 chars of what pypdf
            # actually extracted, so the admin (and we, debugging
            # later) can tell whether the PDF was empty, image-only,
            # or just a layout the parser doesn't know yet.
            total_text = "\n".join(parsed.page_texts)
            preview = total_text[:200].replace("\n", " ⏎ ").strip()
            text_len = len(total_text)
            page_count = len(parsed.page_texts)
            image_count = len(parsed.images)
            return build_mutation_response(
                types.ImportConnecteamRecapPdfResponse,
                success=False,
                message=(
                    f"No labeled fields found in PDF "
                    f"(pages={page_count}, text={text_len}c, "
                    f"images={image_count}). The parser looks for "
                    f"'Label::' or 'Label:' pairs. Extracted text "
                    f"started with: {preview!r}"
                ),
                input_obj=input,
            )

        custom_fields = await sync_to_async(list)(
            models.CustomField.objects.filter(custom_recap_template=template)
            .select_related("custom_field_type", "recap_section")
        )

        match_results = match_fields(parsed, custom_fields)

        # Default name → the event's OWN name (+ its date) so a recap list
        # full of Connecteam imports isn't a wall of identical "Imported
        # from Connecteam · <today>" rows (Kyle's report: every import was
        # named the same, which is messy in the recap list). The stamp comes
        # from the EVENT date — not today — so two same-named stores on
        # different days stay distinguishable. An explicit input.name (the
        # import modal's "Recap title" field) always wins; the generic
        # "Imported from Connecteam" stamp is only the last resort for an
        # event with no name.
        name = (input.name or "").strip()
        if not name:
            ev_name = (getattr(event, "name", "") or "").strip()
            ev_date = getattr(event, "date", None)
            stamp = (
                ev_date.date().isoformat()
                if ev_date
                else django_timezone.now().strftime("%Y-%m-%d")
            )
            name = (
                f"{ev_name} · {stamp}"
                if ev_name
                else f"Imported from Connecteam · {stamp}"
            )

        def _create() -> models.CustomRecap:
            from django.core.files.base import ContentFile

            with transaction.atomic():
                recap = models.CustomRecap.objects.create(
                    name=name,
                    event=event,
                    tenant=event.tenant,
                    custom_recap_template=template,
                    created_by=user,
                    submitted_at=django_timezone.now(),
                )
                for mr in match_results:
                    if mr.field_id is None:
                        continue
                    if not mr.pdf_value:
                        continue
                    models.CustomFieldValue.objects.create(
                        custom_recap=recap,
                        custom_field_id=mr.field_id,
                        value=mr.pdf_value,
                        created_by=user,
                    )

                # Stash the source PDF as a CustomRecapFile so the
                # admin can audit / re-download the original from the
                # recap view. Without this, the PDF the user uploaded
                # is gone the moment the mutation responds — only the
                # extracted values remain.
                #
                # File-recap-category is intentionally NOT set —
                # Kyle's team wants imported files to render as one
                # flat gallery, not grouped by category (PR #543
                # added grouping; this reverts that on Kyle's
                # explicit ask).
                try:
                    pdf_filetype, _ = FileType.objects.get_or_create(
                        name="pdf",
                    )
                    source_file = models.CustomRecapFile(
                        custom_recap=recap,
                        file_type=pdf_filetype,
                        name="Connecteam source PDF",
                        approved=False,
                        created_by=user,
                    )
                    source_file.url.save(
                        f"connecteam-source-{recap.uuid}.pdf",
                        ContentFile(pdf_bytes),
                        save=False,
                    )
                    source_file.save()
                except Exception:
                    # Non-fatal — the recap itself was created
                    # successfully. Audit-trail file is nice-to-have.
                    logging.getLogger(__name__).exception(
                        "connecteam-import: source PDF attach failed "
                        "recap_id=%s", recap.id,
                    )

                # Extract embedded images from the PDF (sampling photos,
                # table-setup pics, in-stock product, receipt, etc.)
                # and attach each as a CustomRecapFile. Without this
                # step, Kyle's team has to manually re-upload every
                # photo even after a successful field-text import.
                #
                # Kyle's call: imported photos render as one flat gallery —
                # NO FileRecapCategory tagging — EXCEPT the receipt, which
                # groups under the tenant's "Receipts" category so it lands in
                # "Evidences & Attachments" under a Receipts group (like a
                # native recap). The preceding-label hint drives both the
                # per-file `name` and the receipt detection.
                try:
                    image_filetype, _ = FileType.objects.get_or_create(
                        name="image",
                    )
                    # Tenant "Receipts" category (sentinel "2"); get-or-create,
                    # tenant-scoped. None-safe — a failure just leaves the
                    # receipt uncategorized rather than blocking the import.
                    receipts_category = _resolve_file_recap_category(
                        "2", tenant_id=event.tenant_id,
                    )
                    attached_images: list = []
                    for parsed_img in parsed.images:
                        # Skip obvious zero-byte / placeholder entries.
                        if not parsed_img.bytes_:
                            continue
                        if len(parsed_img.bytes_) < 1024:
                            # Sub-1KB blobs are almost always icons,
                            # logos, or rendering artifacts — not the
                            # full-size sampling photos we want.
                            continue
                        # Name carries the preceding-label hint so the
                        # admin can tell receipt from sampling photo
                        # at a glance, even though the gallery is flat.
                        nice_name = (
                            parsed_img.preceding_label
                            or f"PDF page {parsed_img.page_index + 1}"
                        )
                        is_receipt = is_receipt_label(
                            parsed_img.preceding_label
                        )
                        file_row = models.CustomRecapFile(
                            custom_recap=recap,
                            file_type=image_filetype,
                            name=nice_name,
                            approved=False,
                            created_by=user,
                            file_recap_category=(
                                receipts_category if is_receipt else None
                            ),
                        )
                        file_row.url.save(
                            (
                                f"connecteam-img-{recap.uuid}"
                                f"-p{parsed_img.page_index}"
                                f"-i{parsed_img.image_index}"
                                f"{parsed_img.extension}"
                            ),
                            ContentFile(parsed_img.bytes_),
                            save=False,
                        )
                        file_row.save()
                        # Receipts live in Evidences under "Receipts" — NOT
                        # also routed onto the receipt field (Kyle picked
                        # Evidences over the dedicated field). Only non-receipt
                        # images are eligible for single-label field routing.
                        if not is_receipt:
                            attached_images.append(
                                (parsed_img, file_row.url.name)
                            )

                    # Route a single, unambiguously-labeled image (the
                    # receipt) onto its IMAGE field's VALUE so it renders in
                    # place, not just the flat gallery. Narrow by design
                    # (exactly-one exact-label match — see
                    # route_single_label_images), so multi-image sampling /
                    # table photos stay flat. The image stays in the gallery
                    # too; this only ALSO sets the field value.
                    image_fields = [
                        cf
                        for cf in custom_fields
                        if getattr(cf.custom_field_type, "name", "") == "image"
                    ]
                    for fid, blob in route_single_label_images(
                        attached_images, image_fields
                    ).items():
                        models.CustomFieldValue.objects.get_or_create(
                            custom_recap=recap,
                            custom_field_id=fid,
                            defaults={"value": blob, "created_by": user},
                        )
                except Exception:
                    logging.getLogger(__name__).exception(
                        "connecteam-import: image attach failed "
                        "recap_id=%s", recap.id,
                    )

                return recap

        try:
            recap = await sync_to_async(_create)()
        except Exception as e:
            logging.getLogger(__name__).exception(
                "connecteam-import: DB write failed event_id=%s", event.id,
            )
            return build_mutation_response(
                types.ImportConnecteamRecapPdfResponse,
                success=False,
                message=f"Couldn't create draft recap: {e}",
                input_obj=input,
            )

        matched = sum(1 for mr in match_results if mr.field_id and mr.pdf_value)
        unmatched = sum(1 for mr in match_results if not mr.field_id)
        stats = [
            types.ImportConnecteamRecapPdfStat(
                pdf_label=mr.pdf_label,
                pdf_value=mr.pdf_value,
                field_name=mr.field_name,
                field_id=strawberry.ID(str(mr.field_id)) if mr.field_id else None,
                score=mr.score,
                skipped_reason=mr.skipped_reason,
            )
            for mr in match_results
        ]
        return build_mutation_response(
            types.ImportConnecteamRecapPdfResponse,
            success=True,
            message=(
                f"Drafted recap from PDF: {matched} field(s) imported, "
                f"{unmatched} unmatched."
            ),
            input_obj=input,
            custom_recap=recap,
            matched_count=matched,
            unmatched_count=unmatched,
            stats=stats,
        )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def parse_connecteam_recap_pdf(
        self,
        info: strawberry.Info,
        input: inputs.ParseConnecteamRecapPdfInput,
    ) -> types.ParseConnecteamRecapPdfResponse:
        """Parse a Connecteam recap PDF and return its values mapped onto
        the STANDARD recap form's fields — WITHOUT creating anything.

        Powers the "Import from Connecteam PDF" pre-fill on the admin
        recap-build form (SparkRecapCreate): the admin drops a PDF, we
        scrape the numbers, the form fills in, and they review + edit
        before submitting via createRecap. Read-only: no DB writes, no
        event/template needed. (The CustomRecap import flow lives in
        import_connecteam_recap_pdf.)
        """
        import base64

        from recaps.connecteam import parse_pdf_bytes, map_legacy_fields

        try:
            pdf_bytes = base64.b64decode(input.pdf_base64, validate=True)
        except Exception:
            return build_mutation_response(
                types.ParseConnecteamRecapPdfResponse,
                success=False,
                message="pdf_base64 is not valid base64.",
                input_obj=input,
            )

        try:
            parsed = await sync_to_async(parse_pdf_bytes)(pdf_bytes)
        except Exception as e:
            logging.getLogger(__name__).exception(
                "connecteam-parse: PDF parse failed",
            )
            return build_mutation_response(
                types.ParseConnecteamRecapPdfResponse,
                success=False,
                message=f"Couldn't read PDF: {e}",
                input_obj=input,
            )

        if not parsed.raw_pairs:
            total_text = "\n".join(parsed.page_texts)
            preview = total_text[:200].replace("\n", " ⏎ ").strip()
            return build_mutation_response(
                types.ParseConnecteamRecapPdfResponse,
                success=False,
                message=(
                    f"No labeled fields found in PDF "
                    f"(pages={len(parsed.page_texts)}, "
                    f"text={len(total_text)}c). The parser looks for "
                    f"'Label::' or 'Label:' pairs. Extracted text "
                    f"started with: {preview!r}"
                ),
                input_obj=input,
            )

        fields, matched = map_legacy_fields(parsed)
        raw_pairs = [
            types.ConnecteamRawPair(label=label, value=str(value))
            for label, value in parsed.raw_pairs.items()
        ]

        return build_mutation_response(
            types.ParseConnecteamRecapPdfResponse,
            success=True,
            message=(
                f"Parsed PDF: {matched} field(s) recognized "
                f"out of {len(parsed.raw_pairs)} found. Review the "
                f"pre-filled values before submitting."
            ),
            input_obj=input,
            matched_count=matched,
            raw_pairs=raw_pairs,
            total_consumer=fields.get("total_consumer"),
            first_time=fields.get("first_time"),
            brand_aware=fields.get("brand_aware"),
            willing=fields.get("willing"),
            not_willing=fields.get("not_willing"),
            products_sold=fields.get("products_sold"),
            total_cans_sold=fields.get("total_cans_sold"),
            total_packs_sold=fields.get("total_packs_sold"),
            account_spend=fields.get("account_spend"),
            traffic_description=fields.get("traffic_description"),
            competitive_presence=fields.get("competitive_presence"),
            quotes=fields.get("quotes"),
            feedback=fields.get("feedback"),
            demographics=fields.get("demographics"),
            positive_stories=fields.get("positive_stories"),
            reasons_to_decline=fields.get("reasons_to_decline"),
            do_differently=fields.get("do_differently"),
            account_notes=fields.get("account_notes"),
        )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_custom_recap_mobile(
        self,
        info: strawberry.Info,
        input: inputs.CreateCustomRecapMobileInput,
    ) -> types.CustomRecapDetailResponse:
        """Create a new custom recap scoped to the logged ambassador (mobile)."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            custom_recap = await service.create_custom_recap()
            return build_mutation_response(
                types.CustomRecapDetailResponse,
                success=True,
                message="Custom recap created successfully.",
                input_obj=input,
                custom_recap=custom_recap,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.CustomRecapDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_custom_recap(
        self,
        info: strawberry.Info,
        input: inputs.UpdateCustomRecapInput,
    ) -> types.CustomRecapDetailResponse:
        """Update an existing custom recap."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            custom_recap = await service.update_custom_recap()
            return build_mutation_response(
                types.CustomRecapDetailResponse,
                success=True,
                message="Custom recap updated successfully.",
                input_obj=input,
                custom_recap=custom_recap,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.CustomRecapDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_custom_recap_mobile(
        self,
        info: strawberry.Info,
        input: inputs.UpdateCustomRecapMobileInput,
    ) -> types.CustomRecapDetailResponse:
        """Update a custom recap scoped to the logged ambassador (mobile)."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            custom_recap = await service.update_custom_recap()
            return build_mutation_response(
                types.CustomRecapDetailResponse,
                success=True,
                message="Custom recap updated successfully.",
                input_obj=input,
                custom_recap=custom_recap,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.CustomRecapDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_custom_field(
        self,
        info: strawberry.Info,
        input: inputs.CreateCustomFieldInput,
    ) -> types.CustomFieldDetailResponse:
        """Create a new custom field."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            custom_field = await service.create_custom_field()
            return build_mutation_response(
                types.CustomFieldDetailResponse,
                success=True,
                message="Custom field created successfully.",
                input_obj=input,
                custom_field=custom_field,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.CustomFieldDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_custom_field(
        self,
        info: strawberry.Info,
        input: inputs.UpdateCustomFieldInput,
    ) -> types.CustomFieldDetailResponse:
        """Update an existing custom field."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            custom_field = await service.update_custom_field()
            return build_mutation_response(
                types.CustomFieldDetailResponse,
                success=True,
                message="Custom field updated successfully.",
                input_obj=input,
                custom_field=custom_field,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.CustomFieldDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_custom_recap_template(
        self,
        info: strawberry.Info,
        input: inputs.CreateCustomRecapTemplateInput,
    ) -> types.CustomRecapTemplateDetailResponse:
        """Create a new custom recap template."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            custom_recap_template = await service.create_custom_recap_template()
            return build_mutation_response(
                types.CustomRecapTemplateDetailResponse,
                success=True,
                message="Custom recap template created successfully.",
                input_obj=input,
                custom_recap_template=custom_recap_template,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.CustomRecapTemplateDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_custom_recap_template(
        self,
        info: strawberry.Info,
        input: inputs.UpdateCustomRecapTemplateInput,
    ) -> types.CustomRecapTemplateDetailResponse:
        """Update an existing custom recap template."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            custom_recap_template = await service.update_custom_recap_template()
            return build_mutation_response(
                types.CustomRecapTemplateDetailResponse,
                success=True,
                message="Custom recap template updated successfully.",
                input_obj=input,
                custom_recap_template=custom_recap_template,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.CustomRecapTemplateDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def remove_custom_field(
        self,
        info: strawberry.Info,
        input: inputs.RemoveCustomFieldInput,
    ) -> types.CustomFieldDetailResponse:
        """Force-delete a custom field from a template (admin-only
        cleanup path). Default: errors if the field has submitted
        values. Pass deleteValues=true to cascade those rows."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            custom_field = await service.remove_custom_field()
            return build_mutation_response(
                types.CustomFieldDetailResponse,
                success=True,
                message="Custom field removed.",
                input_obj=input,
                custom_field=custom_field,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.CustomFieldDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_custom_recap_field_type(
        self,
        info: strawberry.Info,
        input: inputs.CreateCustomRecapFieldTypeInput,
    ) -> types.CustomRecapFieldTypeDetailResponse:
        """Create a new custom recap field type."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            custom_recap_field_type = await service.create_custom_recap_field_type()
            return build_mutation_response(
                types.CustomRecapFieldTypeDetailResponse,
                success=True,
                message="Custom recap field type created successfully.",
                input_obj=input,
                custom_recap_field_type=custom_recap_field_type,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.CustomRecapFieldTypeDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_custom_recap_field_type(
        self,
        info: strawberry.Info,
        input: inputs.UpdateCustomRecapFieldTypeInput,
    ) -> types.CustomRecapFieldTypeDetailResponse:
        """Update an existing custom recap field type."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            custom_recap_field_type = await service.update_custom_recap_field_type()
            return build_mutation_response(
                types.CustomRecapFieldTypeDetailResponse,
                success=True,
                message="Custom recap field type updated successfully.",
                input_obj=input,
                custom_recap_field_type=custom_recap_field_type,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.CustomRecapFieldTypeDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_recap_section(
        self,
        info: strawberry.Info,
        input: inputs.CreateRecapSectionInput,
    ) -> types.RecapSectionDetailResponse:
        """Create a new recap section."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            recap_section = await service.create_recap_section()
            return build_mutation_response(
                types.RecapSectionDetailResponse,
                success=True,
                message="Recap section created successfully.",
                input_obj=input,
                recap_section=recap_section,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapSectionDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_recap_section(
        self,
        info: strawberry.Info,
        input: inputs.UpdateRecapSectionInput,
    ) -> types.RecapSectionDetailResponse:
        """Update an existing recap section."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            recap_section = await service.update_recap_section()
            return build_mutation_response(
                types.RecapSectionDetailResponse,
                success=True,
                message="Recap section updated successfully.",
                input_obj=input,
                recap_section=recap_section,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapSectionDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def move_custom_field_to_section(
        self,
        info: strawberry.Info,
        input: inputs.MoveCustomFieldToSectionInput,
    ) -> types.CustomFieldDetailResponse:
        """Move a custom field into a different section of the same
        template (structure edit). Preserves the field + its captured
        values; rejects cross-template / cross-tenant moves. Tenant-scoped."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            custom_field = await service.move_custom_field_to_section()
            return build_mutation_response(
                types.CustomFieldDetailResponse,
                success=True,
                message="Custom field moved successfully.",
                input_obj=input,
                custom_field=custom_field,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.CustomFieldDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def delete_recap_section(
        self,
        info: strawberry.Info,
        input: inputs.DeleteRecapSectionInput,
    ) -> types.DeleteRecapSectionResponse:
        """Delete an EMPTY recap section (structure edit). Refuses if the
        section still has fields ("Move or remove this section's fields
        before deleting it."). Tenant-scoped."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            recap_section = await service.delete_recap_section()
            return build_mutation_response(
                types.DeleteRecapSectionResponse,
                success=True,
                message="Recap section deleted successfully.",
                input_obj=input,
                deleted_recap_section_uuid=str(recap_section.uuid),
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.DeleteRecapSectionResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def delete_recap(
        self,
        info: strawberry.Info,
        input: inputs.DeleteRecapInput,
    ) -> types.DeleteRecapResponse:
        """Delete a legacy Recap (tenant-scoped, admin-only).

        Frees the event for a fresh recap and removes the row from every
        list. Returns the deleted uuid so the web client can prune the
        Relay store without a refetch.
        """
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            recap = await service.delete_recap()
            return build_mutation_response(
                types.DeleteRecapResponse,
                success=True,
                message="Recap deleted successfully.",
                input_obj=input,
                deleted_recap_uuid=str(recap.uuid),
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.DeleteRecapResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def delete_custom_recap(
        self,
        info: strawberry.Info,
        input: inputs.DeleteCustomRecapInput,
    ) -> types.DeleteCustomRecapResponse:
        """Delete a CustomRecap (tenant-scoped, admin-only).

        Custom-template counterpart to deleteRecap. Frees the event for a
        new recap and drops the row from the recaps list. Returns the
        deleted uuid for a Relay store prune.
        """
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            recap = await service.delete_custom_recap()
            return build_mutation_response(
                types.DeleteCustomRecapResponse,
                success=True,
                message="Recap deleted successfully.",
                input_obj=input,
                deleted_custom_recap_uuid=str(recap.uuid),
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.DeleteCustomRecapResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def add_recap_file(
        self,
        info: strawberry.Info,
        input: inputs.AddRecapFileInput,
    ) -> types.RecapDetailResponse:
        """Attach an already-uploaded blob to an existing recap."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            recap = await service.add_recap_file()
            return build_mutation_response(
                types.RecapDetailResponse,
                success=True,
                message="File attached to recap.",
                input_obj=input,
                recap=recap,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def add_custom_recap_file(
        self,
        info: strawberry.Info,
        input: inputs.AddCustomRecapFileInput,
    ) -> types.CustomRecapDetailResponse:
        """Attach an already-uploaded blob to an existing custom recap."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            custom_recap = await service.add_custom_recap_file()
            return build_mutation_response(
                types.CustomRecapDetailResponse,
                success=True,
                message="File attached to recap.",
                input_obj=input,
                custom_recap=custom_recap,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.CustomRecapDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def remove_recap_file(
        self,
        info: strawberry.Info,
        input: inputs.RemoveRecapFileInput,
    ) -> types.RecapDetailResponse:
        """Remove a photo/receipt from a recap (deletes file + blob)."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            recap = await service.remove_recap_file()
            return build_mutation_response(
                types.RecapDetailResponse,
                success=True,
                message="File removed from recap.",
                input_obj=input,
                recap=recap,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def delete_custom_recap_file(
        self,
        info: strawberry.Info,
        input: inputs.DeleteCustomRecapFileInput,
    ) -> types.CustomRecapDetailResponse:
        """Remove a file from a custom recap's Evidences gallery.

        Hard-deletes the CustomRecapFile row (leaves the GCS blob) and
        returns the parent custom recap so the gallery re-renders from the
        refreshed file list. Tenant-scoped + admin-only.
        """
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            custom_recap = await service.delete_custom_recap_file()
            return build_mutation_response(
                types.CustomRecapDetailResponse,
                success=True,
                message="File removed from recap.",
                input_obj=input,
                custom_recap=custom_recap,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.CustomRecapDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def set_custom_recap_file_category(
        self,
        info: strawberry.Info,
        input: inputs.SetCustomRecapFileCategoryInput,
    ) -> types.CustomRecapDetailResponse:
        """Move a custom-recap file into a different section (e.g. Receipts).

        Re-files a clumped image into the tenant's chosen FileRecapCategory
        and returns the parent custom recap so the gallery re-groups from the
        refreshed file list. Tenant-scoped.
        """
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            custom_recap = await service.set_custom_recap_file_category()
            return build_mutation_response(
                types.CustomRecapDetailResponse,
                success=True,
                message="File moved.",
                input_obj=input,
                custom_recap=custom_recap,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.CustomRecapDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def delete_recap_file(
        self,
        info: strawberry.Info,
        input: inputs.DeleteRecapFileInput,
    ) -> types.RecapFileDetailResponse:
        """Delete a recap file and its blob."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            await service.delete_recap_file()
            return build_mutation_response(
                types.RecapFileDetailResponse,
                success=True,
                message="Recap file deleted successfully.",
                input_obj=input,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapFileDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def approve_recap(
        self,
        info: strawberry.Info,
        input: inputs.ApproveRecapInput,
    ) -> types.RecapDetailResponse:
        """Approve or decline a recap."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            recap = await service.approve_recap()
            message = (
                "Recap approved successfully."
                if input.approved
                else "Recap declined successfully."
            )
            return build_mutation_response(
                types.RecapDetailResponse,
                success=True,
                message=message,
                input_obj=input,
                recap=recap,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def approve_custom_recap(
        self,
        info: strawberry.Info,
        input: inputs.ApproveCustomRecapInput,
    ) -> types.CustomRecapDetailResponse:
        """Approve or decline a custom recap."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            custom_recap = await service.approve_custom_recap()
            message = (
                "Custom recap approved successfully."
                if input.approved
                else "Custom recap declined successfully."
            )
            return build_mutation_response(
                types.CustomRecapDetailResponse,
                success=True,
                message=message,
                input_obj=input,
                custom_recap=custom_recap,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.CustomRecapDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def decline_custom_recap(
        self,
        info: strawberry.Info,
        input: inputs.DeclineCustomRecapInput,
    ) -> types.CustomRecapDetailResponse:
        """Decline a custom recap."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            custom_recap = await service.decline_custom_recap()
            return build_mutation_response(
                types.CustomRecapDetailResponse,
                success=True,
                message="Custom recap declined successfully.",
                input_obj=input,
                custom_recap=custom_recap,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.CustomRecapDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def generate_recap_pdf(
        self,
        info: strawberry.Info,
        input: inputs.GenerateRecapPdfInput,
    ) -> types.RecapFileDetailResponse:
        """Generate a recap PDF and return the resulting file."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            recap_file = await service.generate_recap_pdf()
            return build_mutation_response(
                types.RecapFileDetailResponse,
                success=True,
                message="Recap PDF generated successfully.",
                input_obj=input,
                recap_file=recap_file,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapFileDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def email_campaign_report(
        self,
        info: strawberry.Info,
        input: inputs.EmailCampaignReportInput,
    ) -> types.RecapExportResponse:
        """Generate the campaign-report PDF + email it as an
        attachment to the supplied recipients.

        Same recap selection + caps as `generateCampaignReportPdf`.
        Cover-letter `message` is optional. Returns success/error
        message; the response file_url field is left null (the PDF
        lives in the recipient's inbox, not GCS).
        """
        import re
        from recaps.envelopes import CampaignReportMailer

        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)

            raw_recipients = list(input.recipients or [])
            if not raw_recipients:
                raise GraphQLError("At least one recipient is required.")
            # Light email-format check — Resend will reject bad
            # addresses anyway but a structured error here is friendlier
            # than waiting for the API to bounce.
            email_re = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
            for r in raw_recipients:
                if not email_re.match((r or "").strip()):
                    raise GraphQLError(
                        f"Recipient {r!r} doesn't look like a valid email."
                    )

            # Build the PDF first — if there are no recaps or the
            # render fails we bail before composing the email.
            (
                pdf_bytes,
                recap_count,
                total_consumers,
                title,
                subtitle,
                tenant_name,
                event_meta,
            ) = await service.build_campaign_report_pdf_with_meta(
                recap_ids=input.recap_ids,
                title=input.title,
                subtitle=input.subtitle,
            )

            from django.utils import timezone as django_timezone
            timestamp = django_timezone.now().strftime("%Y%m%d-%H%M")
            # Filename uses a sanitized title slug so the inbox preview
            # is readable. Falls back to a generic name if the title
            # only has non-alphanum characters.
            safe_title = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-").lower()
            if not safe_title:
                safe_title = "campaign-report"
            pdf_filename = f"{safe_title}-{timestamp}.pdf"

            mailer = CampaignReportMailer(
                recipients=raw_recipients,
                campaign_title=title,
                campaign_subtitle=subtitle,
                cover_message=input.message,
                recap_count=recap_count,
                total_consumers=total_consumers,
                sender_tenant_name=tenant_name,
                event_meta=event_meta,
                pdf_bytes=pdf_bytes,
                pdf_filename=pdf_filename,
            )
            # send_async_now: don't wait for the RQ worker (no Redis
            # on Cloud Run) but also don't fire-and-forget — the
            # caller wants to know if the send actually queued.
            await mailer.send_async_now()

            return build_mutation_response(
                types.RecapExportResponse,
                success=True,
                message=(
                    f"Sent {recap_count}-recap report to "
                    f"{len(raw_recipients)} recipient"
                    f"{'s' if len(raw_recipients) != 1 else ''}."
                ),
                input_obj=input,
                file_url=None,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapExportResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def generate_campaign_report_pdf(
        self,
        info: strawberry.Info,
        input: inputs.GenerateCampaignReportPdfInput,
    ) -> types.RecapExportResponse:
        """Bundle N recaps into one PDF deliverable for a client.

        Returns the public GCS URL on success — front-end opens it in
        a new tab (same pattern as the per-recap PDF). Single PDF, no
        attached RecapFile row, no auto-emailing.
        """
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            file_url = await service.generate_campaign_report_pdf()
            return build_mutation_response(
                types.RecapExportResponse,
                success=True,
                message="Campaign report generated.",
                input_obj=input,
                file_url=file_url,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapExportResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def generate_custom_recap_pdf(
        self,
        info: strawberry.Info,
        input: inputs.GenerateCustomRecapPdfInput,
    ) -> types.CustomRecapFileDetailResponse:
        """Generate a custom recap PDF and return the resulting file."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            custom_recap_file = await service.generate_custom_recap_pdf()
            return build_mutation_response(
                types.CustomRecapFileDetailResponse,
                success=True,
                message="Custom recap PDF generated successfully.",
                input_obj=input,
                custom_recap_file=custom_recap_file,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.CustomRecapFileDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def reassign_recap_event(
        self,
        info: strawberry.Info,
        input: inputs.ReassignRecapEventInput,
    ) -> types.RecapDetailResponse:
        """Move a recap from one Event to another within the same
        tenant. Fixes wrong-event mis-links (BA picked the wrong shift
        when filing) without forcing a re-file.

        Both events must belong to the same tenant — this avoids the
        cross-tenant leak surface and lines up with the implicit
        invariant elsewhere in the schema (recap.event.tenant matches
        the caller's tenant).

        Returns the updated recap so the UI can re-render with the
        new event link in place.
        """
        from events import models as e_models

        try:
            try:
                recap_pk = resolve_id_to_int(input.recap_id)
                new_event_pk = resolve_id_to_int(input.event_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid recap_id or event_id.")

            service = RecapMutationService.with_input(input)
            await service.set_user(info)

            # Cross-tenant write gate: verify the CALLER owns the recap's
            # CURRENT tenant before moving it. The inner _reassign already
            # forbids moving across tenants, but without this a foreign-tenant
            # caller could still re-link another tenant's recap to a different
            # event WITHIN that tenant by guessing its id. Scope off the
            # recap's current event tenant. BAs aren't blocked (this can be a
            # same-tenant fix-up); only foreign-tenant access is denied.
            recap_tenant_id = await sync_to_async(
                lambda: models.Recap.objects.select_related("event")
                .filter(pk=recap_pk)
                .values_list("event__tenant_id", flat=True)
                .first()
            )()
            if recap_tenant_id is None:
                raise GraphQLError("Recap not found.")
            await service._assert_caller_authorized_for_recap_tenant(
                recap_tenant_id,
                action="reassign",
            )

            @sync_to_async
            def _reassign() -> models.Recap:
                recap = (
                    models.Recap.objects.select_related("event").filter(pk=recap_pk).first()
                )
                if not recap:
                    raise GraphQLError("Recap not found.")
                new_event = (
                    e_models.Event.objects.select_related("tenant")
                    .filter(pk=new_event_pk)
                    .first()
                )
                if not new_event:
                    raise GraphQLError("Target event not found.")
                # Cross-tenant guard: the new event must live in the
                # same tenant as the recap's current event. We compare
                # tenant_id rather than tenant objects to avoid an
                # extra fetch.
                current_tenant_id = (
                    recap.event.tenant_id if recap.event_id else None
                )
                if (
                    current_tenant_id is not None
                    and new_event.tenant_id is not None
                    and current_tenant_id != new_event.tenant_id
                ):
                    raise GraphQLError(
                        "Cannot move a recap across tenants."
                    )
                recap.event = new_event
                recap.save(update_fields=["event"])
                return recap

            recap = await _reassign()
            return build_mutation_response(
                types.RecapDetailResponse,
                success=True,
                message="Recap moved to the selected event.",
                input_obj=input,
                recap=recap,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def nudge_ambassador_for_recap(
        self,
        info: strawberry.Info,
        input: inputs.NudgeAmbassadorForRecapInput,
    ) -> types.NudgeRecapResponse:
        """Push "you still owe a recap" notification to a specific BA
        on a specific event. Powers the per-row "Nudge" button on the
        /recaps/missing admin drill-down.

        Sends to every active device on the BA's account. Returns the
        device count so the UI can show "Nudged on 2 devices" vs
        "Nudged but BA has no registered devices" (encourages admin to
        DM them instead).

        No-op (success=False) for AmbassadorEvents that already have
        a recap — avoids embarrassing nudges to a BA who already
        filed. Same idempotency guard the recap-nudge cron uses.
        """
        from ambassadors.push import send_push_to_user
        from ambassadors import models as a_models
        from events import models as e_models

        try:
            ae_uuid = str(input.ambassador_event_uuid)
            if not ae_uuid:
                raise GraphQLError("ambassador_event_uuid is required.")

            @sync_to_async
            def _fetch():
                ae = (
                    a_models.AmbassadorEvent.objects.select_related(
                        "ambassador__user",
                        "event",
                        "event__retailer",
                    )
                    .filter(uuid=ae_uuid)
                    .first()
                )
                if not ae:
                    return (None, None, False)
                # Idempotency: if a recap already exists for this
                # event, don't push. The Inbox/Tracker UI will refresh
                # off the existing recap.
                already_filed = e_models.Event.objects.filter(
                    pk=ae.event_id, recaps__isnull=False
                ).exists()
                return (ae, ae.ambassador.user if ae.ambassador else None,
                        already_filed)

            ae, user, already_filed = await _fetch()
            if not ae:
                raise GraphQLError("Shift assignment not found.")
            if not user:
                raise GraphQLError("Ambassador has no user account.")
            if already_filed:
                return build_mutation_response(
                    types.NudgeRecapResponse,
                    success=False,
                    message="Recap already filed — no nudge sent.",
                    input_obj=input,
                    devices_notified=0,
                )

            event = ae.event
            event_name = (
                getattr(event, "name", None)
                or getattr(getattr(event, "retailer", None), "name", None)
                or "your shift"
            )
            title = (input.title or "").strip() or "Don't forget your recap"
            body = (
                (input.body or "").strip()
                or f"Submit your recap for {event_name}"
            )

            devices_notified = await send_push_to_user(
                user,
                title=title,
                body=body,
                data={
                    "screen": "recap",
                    "eventUuid": str(getattr(event, "uuid", "")),
                },
            )

            # Audit log: write a nudge_sent entry on the request that
            # spawned this event so the timeline panel picks it up.
            # Best-effort; the nudge itself is the important action.
            try:
                req = None
                if getattr(event, "request_id", None):
                    req = await sync_to_async(
                        lambda: e_models.Request.objects.filter(
                            id=event.request_id
                        ).first()
                    )()
                if req is not None:
                    ambassador_obj = ae.ambassador
                    ba_name = (
                        " ".join(
                            filter(
                                None,
                                [
                                    getattr(ambassador_obj, "first_name", None),
                                    getattr(ambassador_obj, "last_name", None),
                                ],
                            )
                        )
                        or getattr(ambassador_obj, "email", "")
                        or "BA"
                    )
                    # The user calling this mutation is the admin, not
                    # the ambassador receiving the nudge. Pull from info.
                    actor = None
                    try:
                        actor = info.context.request.user
                    except Exception:
                        actor = None
                    await sync_to_async(
                        e_models.RequestActivityLog.objects.create
                    )(
                        tenant=req.tenant,
                        request=req,
                        kind=e_models.RequestActivityLog.KIND_NUDGE_SENT,
                        actor_user=actor if getattr(actor, "id", None) else None,
                        summary=f"Nudged {ba_name} for recap",
                        metadata={
                            "ba_name": ba_name,
                            "devices_notified": devices_notified,
                            "event_uuid": str(getattr(event, "uuid", "")),
                        },
                    )
            except Exception:
                pass

            return build_mutation_response(
                types.NudgeRecapResponse,
                success=True,
                message=(
                    f"Nudged on {devices_notified} device"
                    f"{'s' if devices_notified != 1 else ''}."
                    if devices_notified > 0
                    else "Nudge attempted, but the BA has no registered devices."
                ),
                input_obj=input,
                devices_notified=devices_notified,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.NudgeRecapResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def export_recaps_xlsx(
        self,
        info: strawberry.Info,
        input: inputs.ExportRecapsXlsxInput,
    ) -> types.RecapExportResponse:
        """Export all recaps for a tenant to an Excel file."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            file_url = await service.export_recaps_xlsx()
            return build_mutation_response(
                types.RecapExportResponse,
                success=True,
                message="Recaps exported successfully.",
                input_obj=input,
                file_url=file_url,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapExportResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def export_recap_xlsx(
        self,
        info: strawberry.Info,
        input: inputs.ExportRecapXlsxInput,
    ) -> types.RecapExportResponse:
        """Export a single recap to an Excel file."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            file_url = await service.export_recap_xlsx()
            return build_mutation_response(
                types.RecapExportResponse,
                success=True,
                message="Recap exported successfully.",
                input_obj=input,
                file_url=file_url,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapExportResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def export_custom_recaps_xlsx(
        self,
        info: strawberry.Info,
        input: inputs.ExportCustomRecapsXlsxInput,
    ) -> types.RecapExportResponse:
        """Export all custom recaps for a tenant to an Excel file."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            file_url = await service.export_custom_recaps_xlsx()
            return build_mutation_response(
                types.RecapExportResponse,
                success=True,
                message="Custom recaps exported successfully.",
                input_obj=input,
                file_url=file_url,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapExportResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def export_custom_recap_xlsx(
        self,
        info: strawberry.Info,
        input: inputs.ExportCustomRecapXlsxInput,
    ) -> types.RecapExportResponse:
        """Export a single custom recap to an Excel file."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            file_url = await service.export_custom_recap_xlsx()
            return build_mutation_response(
                types.RecapExportResponse,
                success=True,
                message="Custom recap exported successfully.",
                input_obj=input,
                file_url=file_url,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapExportResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def recap_file_download_url(
        self,
        info: strawberry.Info,
        input: inputs.RecapFileDownloadUrlInput,
    ) -> types.RecapFileUrlResponse:
        """Return a signed download URL for a recap file."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            file_url = await service.get_recap_file_download_url()
            return build_mutation_response(
                types.RecapFileUrlResponse,
                success=True,
                message="Recap file URL generated successfully.",
                input_obj=input,
                file_url=file_url,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapFileUrlResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )
