"""GraphQL mutations for the Consumer Receipt Validation feature (clients schema).

`reviewReceipt` is the admin action: flip a pending receipt to `validated`
or `rejected`, stamping the reviewer, the review time, and an optional note.
Tenant-scoped — a client-role user can only review receipts in their own
tenant; admins (spark-admin / staff / Ignite) review anything.
"""

from __future__ import annotations

import strawberry
from asgiref.sync import sync_to_async
from graphql import GraphQLError

from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone
from django.utils.text import slugify

from receipts import inputs, models, ocr, types
from receipts.queries import (
    ConsumerReceiptQueriesService,
    _enforce_client_tenant,
    _require_admin_or_client,
)
from utils.gcs import download_blob_bytes, extract_blob_name_from_url
from utils.graphql.mixins import SparkGraphQLMixin, resolve_id_to_int
from utils.graphql.permissions import StrictIsAuthenticated
from utils.graphql.relay import ensure_relay_mutation
from utils.utils import build_mutation_response

ensure_relay_mutation()
from strawberry import relay  # noqa: E402  (relay.mutation shim ensured above)

# Statuses an admin may move a receipt INTO via review. You can validate or
# reject, but you can't review something back to "pending".
_REVIEWABLE_STATUSES = {
    models.ConsumerReceipt.STATUS_VALIDATED,
    models.ConsumerReceipt.STATUS_REJECTED,
}


def _to_decimal(value, *, default: Decimal | None = Decimal("0")) -> Decimal | None:
    """Coerce a float/str reward into a 2dp Decimal (default on bad/empty)."""
    if value is None:
        return default
    try:
        return Decimal(str(value)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    except Exception:
        return default


def _budget_cap(value) -> Decimal | None:
    """A positive 2dp Decimal cap, or None for 'no cap' (value None or <= 0)."""
    if value is None:
        return None
    dec = _to_decimal(value, default=None)
    if dec is None or dec <= 0:
        return None
    return dec


@strawberry.type
class ReceiptMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def review_receipt(
        self,
        info: strawberry.Info,
        input: inputs.ReviewReceiptInput,
    ) -> types.ReviewReceiptResponse:
        """Validate or reject a consumer receipt; stamp the reviewer + note."""
        try:
            await _require_admin_or_client(info)

            status = (input.status or "").strip().lower()
            if status not in _REVIEWABLE_STATUSES:
                raise GraphQLError(
                    "status must be 'validated' or 'rejected'."
                )

            try:
                receipt_id = resolve_id_to_int(input.id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError(f"Invalid ID: {input.id}")

            service = ConsumerReceiptQueriesService()
            user = await service.get_user(info)

            try:
                receipt = await sync_to_async(
                    models.ConsumerReceipt.objects.select_related(
                        "event", "campaign", "tenant", "reviewed_by"
                    ).get
                )(id=receipt_id)
            except models.ConsumerReceipt.DoesNotExist:
                raise GraphQLError("Receipt not found.")

            # Tenant gate: a client-role user may only review their own
            # tenant's receipts. Admins pass through. Surfaced as
            # "not found" so we don't leak cross-tenant existence.
            if service.get_role_slug(user) == "client":
                user_tenant = await service.get_user_tenant(info)
                if receipt.tenant_id != user_tenant.id:
                    raise GraphQLError("Receipt not found.")

            receipt.status = status
            receipt.reviewed_by = user
            receipt.reviewed_at = timezone.now()
            receipt.review_note = (input.note or "").strip()
            update_fields = [
                "status",
                "reviewed_by",
                "reviewed_at",
                "review_note",
                "updated_at",
            ]
            # Lock the reward onto the receipt at validation time so a later
            # change to the campaign's reward_amount can't rewrite history.
            # (campaign is select_related-cached above, so this is async-safe.)
            if (
                status == models.ConsumerReceipt.STATUS_VALIDATED
                and receipt.reward_amount is None
                and receipt.campaign_id
                and receipt.campaign is not None
            ):
                receipt.reward_amount = receipt.campaign.reward_amount
                update_fields.append("reward_amount")
            await sync_to_async(receipt.save)(update_fields=update_fields)

            # Reload through the queries service queryset so the response's
            # nested fields (publicUrl / reviewedBy / eventName) read the
            # same select_related relations the list does.
            receipt = await sync_to_async(
                ConsumerReceiptQueriesService().get_queryset().get
            )(id=receipt.id)

            return build_mutation_response(
                types.ReviewReceiptResponse,
                success=True,
                message=f"Receipt {status}.",
                input_obj=input,
                receipt=receipt,
            )
        except GraphQLError as exc:
            return build_mutation_response(
                types.ReviewReceiptResponse,
                success=False,
                message=str(exc),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_receipt_campaign(
        self,
        info: strawberry.Info,
        input: inputs.CreateReceiptCampaignInput,
    ) -> types.ReceiptCampaignResponse:
        """Create a per-tenant consumer rebate campaign."""
        try:
            await _require_admin_or_client(info)
            name = (input.name or "").strip()
            if not name:
                raise GraphQLError("Campaign name is required.")

            service = ConsumerReceiptQueriesService()
            user = await service.get_user(info)

            tenant_id_raw = (
                resolve_id_to_int(input.tenant_id)
                if input.tenant_id not in (None, "")
                else None
            )
            tenant_id = await _enforce_client_tenant(service, info, tenant_id_raw)
            if not tenant_id:
                raise GraphQLError("A tenant is required to create a campaign.")

            reward = _to_decimal(input.reward_amount)

            def _create() -> models.ReceiptCampaign:
                base = (slugify(input.slug or name) or "campaign")[:72]
                slug = base
                suffix = 2
                while models.ReceiptCampaign.objects.filter(slug=slug).exists():
                    slug = f"{base}-{suffix}"[:80]
                    suffix += 1
                return models.ReceiptCampaign.objects.create(
                    tenant_id=tenant_id,
                    name=name,
                    slug=slug,
                    headline=(input.headline or "").strip(),
                    description=(input.description or "").strip(),
                    product=(input.product or "").strip(),
                    reward_amount=reward,
                    budget_cap=_budget_cap(input.budget_cap),
                    payout_note=(input.payout_note or "").strip(),
                    is_active=(
                        True if input.is_active is None else bool(input.is_active)
                    ),
                    created_by=user,
                    updated_by=user,
                )

            campaign = await sync_to_async(_create, thread_sensitive=True)()
            return build_mutation_response(
                types.ReceiptCampaignResponse,
                success=True,
                message="Campaign created.",
                input_obj=input,
                campaign=campaign,
            )
        except GraphQLError as exc:
            return build_mutation_response(
                types.ReceiptCampaignResponse,
                success=False,
                message=str(exc),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_receipt_campaign(
        self,
        info: strawberry.Info,
        input: inputs.UpdateReceiptCampaignInput,
    ) -> types.ReceiptCampaignResponse:
        """Update mutable fields on a campaign (tenant-scoped)."""
        try:
            await _require_admin_or_client(info)
            try:
                campaign_id = resolve_id_to_int(input.id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError(f"Invalid ID: {input.id}")

            service = ConsumerReceiptQueriesService()
            user = await service.get_user(info)

            try:
                campaign = await sync_to_async(
                    models.ReceiptCampaign.objects.select_related("tenant").get
                )(id=campaign_id)
            except models.ReceiptCampaign.DoesNotExist:
                raise GraphQLError("Campaign not found.")

            if service.get_role_slug(user) == "client":
                user_tenant = await service.get_user_tenant(info)
                if campaign.tenant_id != user_tenant.id:
                    raise GraphQLError("Campaign not found.")

            update_fields: list[str] = ["updated_by", "updated_at"]
            campaign.updated_by = user
            if input.name is not None and input.name.strip():
                campaign.name = input.name.strip()
                update_fields.append("name")
            if input.headline is not None:
                campaign.headline = input.headline.strip()
                update_fields.append("headline")
            if input.description is not None:
                campaign.description = input.description.strip()
                update_fields.append("description")
            if input.product is not None:
                campaign.product = input.product.strip()
                update_fields.append("product")
            if input.payout_note is not None:
                campaign.payout_note = input.payout_note.strip()
                update_fields.append("payout_note")
            if input.reward_amount is not None:
                campaign.reward_amount = _to_decimal(input.reward_amount)
                update_fields.append("reward_amount")
            if input.budget_cap is not None:
                # <= 0 clears the cap (NULL); > 0 sets it.
                campaign.budget_cap = _budget_cap(input.budget_cap)
                update_fields.append("budget_cap")
            if input.is_active is not None:
                campaign.is_active = bool(input.is_active)
                update_fields.append("is_active")
            if input.slug is not None and input.slug.strip():
                base = (slugify(input.slug) or "campaign")[:72]

                def _unique_slug() -> str:
                    slug = base
                    suffix = 2
                    while (
                        models.ReceiptCampaign.objects.filter(slug=slug)
                        .exclude(id=campaign.id)
                        .exists()
                    ):
                        slug = f"{base}-{suffix}"[:80]
                        suffix += 1
                    return slug

                campaign.slug = await sync_to_async(_unique_slug)()
                update_fields.append("slug")

            await sync_to_async(campaign.save)(update_fields=update_fields)
            return build_mutation_response(
                types.ReceiptCampaignResponse,
                success=True,
                message="Campaign updated.",
                input_obj=input,
                campaign=campaign,
            )
        except GraphQLError as exc:
            return build_mutation_response(
                types.ReceiptCampaignResponse,
                success=False,
                message=str(exc),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def mark_receipt_paid(
        self,
        info: strawberry.Info,
        input: inputs.MarkReceiptPaidInput,
    ) -> types.ReviewReceiptResponse:
        """Stamp the payout audit on a validated receipt.

        Spark moves no money — the admin sends the Venmo payment (via the
        `payoutLink`) and confirms here, which records paid_at/paid_by and
        locks the paid reward amount.
        """
        try:
            await _require_admin_or_client(info)
            try:
                receipt_id = resolve_id_to_int(input.id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError(f"Invalid ID: {input.id}")

            service = ConsumerReceiptQueriesService()
            user = await service.get_user(info)

            try:
                receipt = await sync_to_async(
                    models.ConsumerReceipt.objects.select_related(
                        "event", "campaign", "tenant", "reviewed_by"
                    ).get
                )(id=receipt_id)
            except models.ConsumerReceipt.DoesNotExist:
                raise GraphQLError("Receipt not found.")

            if service.get_role_slug(user) == "client":
                user_tenant = await service.get_user_tenant(info)
                if receipt.tenant_id != user_tenant.id:
                    raise GraphQLError("Receipt not found.")

            if receipt.status != models.ConsumerReceipt.STATUS_VALIDATED:
                raise GraphQLError(
                    "Only a validated receipt can be marked paid."
                )

            # Amount: explicit override → existing snapshot → campaign reward.
            amount = receipt.reward_amount
            if input.amount is not None:
                amount = _to_decimal(input.amount)
            elif amount is None and receipt.campaign is not None:
                amount = receipt.campaign.reward_amount

            update_fields = ["paid_at", "paid_by", "updated_at"]
            receipt.paid_at = timezone.now()
            receipt.paid_by = user
            if amount is not None:
                receipt.reward_amount = amount
                update_fields.append("reward_amount")
            if input.payout_handle is not None and input.payout_handle.strip():
                receipt.payout_handle = input.payout_handle.strip().lstrip("@")
                update_fields.append("payout_handle")

            await sync_to_async(receipt.save)(update_fields=update_fields)

            # Reload through the queries service queryset so the response's
            # nested fields read the same select_related relations the list does.
            receipt = await sync_to_async(
                ConsumerReceiptQueriesService().get_queryset().get
            )(id=receipt.id)

            return build_mutation_response(
                types.ReviewReceiptResponse,
                success=True,
                message="Receipt marked paid.",
                input_obj=input,
                receipt=receipt,
            )
        except GraphQLError as exc:
            return build_mutation_response(
                types.ReviewReceiptResponse,
                success=False,
                message=str(exc),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def delete_receipt_campaign(
        self,
        info: strawberry.Info,
        input: inputs.DeleteReceiptCampaignInput,
    ) -> types.ReceiptCampaignResponse:
        """Delete a campaign — hard-delete when empty, soft-archive otherwise.

        An empty campaign (no receipts) is removed outright. A campaign that
        already has submitted receipts is archived (deleted_at set, paused)
        so the proof-of-purchase rows + payout history survive; it just
        disappears from the dashboard + public page.
        """
        try:
            await _require_admin_or_client(info)
            try:
                campaign_id = resolve_id_to_int(input.id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError(f"Invalid ID: {input.id}")

            service = ConsumerReceiptQueriesService()
            user = await service.get_user(info)

            try:
                campaign = await sync_to_async(
                    models.ReceiptCampaign.objects.select_related("tenant").get
                )(id=campaign_id, deleted_at__isnull=True)
            except models.ReceiptCampaign.DoesNotExist:
                raise GraphQLError("Campaign not found.")

            if service.get_role_slug(user) == "client":
                user_tenant = await service.get_user_tenant(info)
                if campaign.tenant_id != user_tenant.id:
                    raise GraphQLError("Campaign not found.")

            def _delete() -> str:
                has_receipts = models.ConsumerReceipt.objects.filter(
                    campaign_id=campaign.id
                ).exists()
                if has_receipts:
                    campaign.deleted_at = timezone.now()
                    campaign.is_active = False
                    campaign.updated_by = user
                    campaign.save(
                        update_fields=[
                            "deleted_at",
                            "is_active",
                            "updated_by",
                            "updated_at",
                        ]
                    )
                    return (
                        "Campaign archived — it has submitted receipts, so its "
                        "data is preserved and it's hidden from the dashboard."
                    )
                campaign.delete()
                return "Campaign deleted."

            message = await sync_to_async(_delete, thread_sensitive=True)()
            return build_mutation_response(
                types.ReceiptCampaignResponse,
                success=True,
                message=message,
                input_obj=input,
            )
        except GraphQLError as exc:
            return build_mutation_response(
                types.ReceiptCampaignResponse,
                success=False,
                message=str(exc),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def run_receipt_ocr(
        self,
        info: strawberry.Info,
        input: inputs.RunReceiptOcrInput,
    ) -> types.ReviewReceiptResponse:
        """Admin-triggered OCR — best-effort auto-read of store/amount/date.

        Reads the stored receipt image, runs Google Cloud Vision, and saves
        the parsed store/amount/date onto the receipt for the admin to verify.
        Degrades gracefully: if Vision is unavailable the receipt is unchanged
        besides ocr_ran_at and the response reports why.
        """
        try:
            await _require_admin_or_client(info)
            try:
                receipt_id = resolve_id_to_int(input.id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError(f"Invalid ID: {input.id}")

            service = ConsumerReceiptQueriesService()
            user = await service.get_user(info)

            try:
                receipt = await sync_to_async(
                    models.ConsumerReceipt.objects.select_related(
                        "event", "campaign", "tenant", "reviewed_by"
                    ).get
                )(id=receipt_id)
            except models.ConsumerReceipt.DoesNotExist:
                raise GraphQLError("Receipt not found.")

            if service.get_role_slug(user) == "client":
                user_tenant = await service.get_user_tenant(info)
                if receipt.tenant_id != user_tenant.id:
                    raise GraphQLError("Receipt not found.")

            blob_name = (
                extract_blob_name_from_url(receipt.image) or receipt.image
            )

            def _run_and_save() -> tuple[bool, str]:
                image_bytes = download_blob_bytes(blob_name)
                if not image_bytes:
                    return False, "Could not read the receipt image."
                result = ocr.run_ocr(image_bytes)
                receipt.ocr_ran_at = timezone.now()
                receipt.ocr_text = result.text or ""
                receipt.ocr_store = result.store or ""
                receipt.ocr_amount = result.amount
                receipt.ocr_date = result.purchase_date
                receipt.save(
                    update_fields=[
                        "ocr_ran_at",
                        "ocr_text",
                        "ocr_store",
                        "ocr_amount",
                        "ocr_date",
                        "updated_at",
                    ]
                )
                if not result.ok:
                    return False, result.reason or "OCR failed."
                return True, "Receipt read."

            ok, message = await sync_to_async(
                _run_and_save, thread_sensitive=True
            )()

            receipt = await sync_to_async(
                ConsumerReceiptQueriesService().get_queryset().get
            )(id=receipt.id)

            return build_mutation_response(
                types.ReviewReceiptResponse,
                success=ok,
                message=message,
                input_obj=input,
                receipt=receipt,
            )
        except GraphQLError as exc:
            return build_mutation_response(
                types.ReviewReceiptResponse,
                success=False,
                message=str(exc),
                input_obj=input,
            )
