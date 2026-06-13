import strawberry
from typing import List
from strawberry.scalars import JSON

from utils.graphql.inputs import SparkGraphQLInput


@strawberry.input
class RecapFiltersInput(SparkGraphQLInput):
    tenant_id: strawberry.ID | None = None
    event_id: strawberry.ID | None = None
    event_type: strawberry.ID | None = None
    rmm_asigned_id: strawberry.ID | None = None
    ambassador_id: strawberry.ID | None = None
    retailer_id: strawberry.ID | None = None
    location_id: strawberry.ID | None = None
    state_id: strawberry.ID | None = None
    event_date: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    event_address: str | None = None
    approved: bool | None = None
    edited: bool | None = None


@strawberry.input
class CustomRecapFiltersInput(RecapFiltersInput):
    custom_recap_template_id: strawberry.ID | None = None


@strawberry.input
class CustomRecapTemplateFiltersInput(SparkGraphQLInput):
    tenant_id: strawberry.ID | None = None
    event_type_id: strawberry.ID | None = None


@strawberry.input
class FileRecapCategoryFiltersInput(SparkGraphQLInput):
    tenant_id: strawberry.ID | None = None


@strawberry.input
class TypeOfGoodFiltersInput(SparkGraphQLInput):
    tenant_id: strawberry.ID | None = None


@strawberry.input
class RecapFileInput(SparkGraphQLInput):
    file: str
    file_type_id: strawberry.ID | None = None
    file_recap_category_id: strawberry.ID | None = None


@strawberry.input
class ConsumerEngagementsInput(SparkGraphQLInput):
    total_consumer: int | None = None
    first_time_consumers: int | None = None
    brand_aware_consumers: int | None = None
    willing_to_purchase_consumers: int | None = None
    not_willing_consumers: int | None = None


@strawberry.input
class ProductSampleInput(SparkGraphQLInput):
    product_id: strawberry.ID | None = None
    quantity: int | None = None


@strawberry.input
class SalesPerformanceInput(SparkGraphQLInput):
    product_id: strawberry.ID | None = None
    type_of_good_id: strawberry.ID | None = None
    price: float | None = None


@strawberry.input
class ConsumerFeedbackInput(SparkGraphQLInput):
    demographics: str | None = None
    feedback: str | None = None
    quotes: str | None = None
    positive_stories: str | None = None
    reasons_to_decline: str | None = None


@strawberry.input
class AccountFeedbackInput(SparkGraphQLInput):
    do_differently_feedback: str | None = None
    feedback: str | None = None
    corpo_card: str | None = None
    was_corpo_card_used: bool | None = None


@strawberry.input
class CreateRecapInput(SparkGraphQLInput):
    name: str
    event_id: strawberry.ID
    files: List[RecapFileInput]
    filling_for_ambassador: bool | None = None
    late: bool | None = None
    incomplete: bool | None = None
    
    products_sold: int | None = None
    total_cans_sold: int | None = None
    total_packs_sold: int | None = None
    total_earnings: float | None = None
    account_spend_amount: float | None = None
    traffic_description: str | None = None
    competitive_presence: str | None = None
    
    job_id: strawberry.ID | None = None
    retailer_id: strawberry.ID | None = None
    location_id: strawberry.ID | None = None
    state_id: strawberry.ID | None = None
    ambassador_id: strawberry.ID | None = None
    # Free-text BA name for reconciliation when the actual worker isn't
    # in Spark yet (sub-contractors, one-off helpers, not-yet-onboarded
    # BAs). Set alongside ambassador_id=null to record an "external" BA.
    # If both are sent, ambassador_id wins server-side.
    external_ba_name: str | None = None

    consumer_engagements: ConsumerEngagementsInput | None = None
    product_samples: List[ProductSampleInput] | None = None
    sales_performance: List[SalesPerformanceInput] | None = None
    consumer_feedback: ConsumerFeedbackInput | None = None
    account_feedback: AccountFeedbackInput | None = None


@strawberry.input
class UpdateRecapInput(CreateRecapInput):
    id: strawberry.ID


@strawberry.input
class DeleteRecapInput(SparkGraphQLInput):
    id: strawberry.ID


@strawberry.input
class DeleteCustomRecapInput(SparkGraphQLInput):
    """Delete a single CustomRecap (tenant-scoped, admin-only).

    Counterpart to DeleteRecapInput for the custom-template recap
    model. `id` is the CustomRecap's Relay-encoded global id.
    """

    id: strawberry.ID


@strawberry.input
class DeleteRecapFileInput(SparkGraphQLInput):
    id: strawberry.ID


@strawberry.input
class RemoveRecapFileInput(SparkGraphQLInput):
    """Detach + delete a single file from a recap.

    Unlike DeleteRecapFileInput (which refuses to touch files still
    linked to a recap), this is the explicit "remove this photo from
    the recap" action — it deletes the RecapFile row and its GCS blob
    even though it's linked. Returns the parent recap so the client can
    refresh the file grid. Admin use only (mutation is auth-gated).
    """

    id: strawberry.ID  # the RecapFile id


@strawberry.input
class DeleteCustomRecapFileInput(SparkGraphQLInput):
    """Remove a single file from a CustomRecap's Evidences gallery.

    Custom-template counterpart to RemoveRecapFileInput. Lets an admin
    delete a misfiled image/PDF (e.g. a receipt that landed under "Table
    setup") from the recap. Hard-deletes the CustomRecapFile row but
    leaves the GCS blob in place (audit / recoverability). Tenant-scoped
    + admin-only, gated server-side. Returns the parent custom recap so
    the client can re-render the gallery from the refreshed file list.
    """

    id: strawberry.ID  # the CustomRecapFile id


@strawberry.input
class AddRecapFileInput(SparkGraphQLInput):
    """Attach a single already-uploaded blob to an existing recap.

    Distinct from update_recap, which reconciles the *entire* file set
    and unconditionally overwrites scalar fields (products_sold, etc.) —
    using it just to add one photo would silently null those out. This
    input does the minimal, safe thing: create one RecapFile row linked
    to the recap, touching nothing else.
    """

    recap_id: strawberry.ID
    file: str  # GCS blob name (e.g. "recaps/<uuid>/<stamp>-photo.jpg")
    file_type_id: strawberry.ID | None = None
    file_recap_category_id: strawberry.ID | None = None


@strawberry.input
class AddCustomRecapFileInput(SparkGraphQLInput):
    """Attach a single already-uploaded blob to an existing custom recap.

    The custom-template (Borjomi, Girl Beer, …) counterpart to
    AddRecapFileInput. Distinct from update_custom_recap, which reconciles
    the entire file set and rewrites field values — using it to add one
    photo would risk clobbering recap data. This input does the minimal,
    safe thing: create one CustomRecapFile row, touching nothing else.
    """

    custom_recap_id: strawberry.ID
    file: str  # GCS blob name (e.g. "recap_files/<uuid>/<stamp>-photo.jpg")
    file_type_id: strawberry.ID | None = None
    file_recap_category_id: strawberry.ID | None = None


@strawberry.input
class ApproveRecapInput(SparkGraphQLInput):
    id: strawberry.ID
    approved: bool


@strawberry.input
class ApproveCustomRecapInput(SparkGraphQLInput):
    id: strawberry.ID
    approved: bool


@strawberry.input
class DeclineCustomRecapInput(SparkGraphQLInput):
    id: strawberry.ID


@strawberry.input
class GenerateRecapPdfInput(SparkGraphQLInput):
    id: strawberry.ID


@strawberry.input
class GenerateCustomRecapPdfInput(SparkGraphQLInput):
    id: strawberry.ID


@strawberry.input
class ExportRecapsXlsxInput(SparkGraphQLInput):
    tenant_id: strawberry.ID | None = None
    start_date: str | None = None
    end_date: str | None = None


@strawberry.input
class ReassignRecapEventInput(SparkGraphQLInput):
    """Move a recap from one Event to another within the same tenant.

    Used to fix wrong-event mis-links (BA picked the wrong shift when
    filing) without forcing a re-file. Backend validates both events
    belong to the same tenant so this can't be used to leak data
    across tenant boundaries.
    """

    recap_id: strawberry.ID
    event_id: strawberry.ID


@strawberry.input
class NudgeAmbassadorForRecapInput(SparkGraphQLInput):
    """Fire a "you still owe a recap" push to a BA who was assigned to
    a shift that already wrapped. `ambassador_event_uuid` is what the
    /recaps/missing UI hands us — uniquely identifies the BA + event
    pair without needing two ids.
    """

    ambassador_event_uuid: strawberry.ID
    # Optional override of the push title / body. If absent we use
    # the standard "Don't forget your recap" / event-name template
    # the recap-nudge cron already uses, so the BA gets the same
    # copy from both surfaces.
    title: str | None = None
    body: str | None = None


@strawberry.input
class EmailCampaignReportInput(SparkGraphQLInput):
    """Generate the campaign-report PDF + email it as an attachment.

    Same recap selection as `generateCampaignReportPdf`, plus a
    recipient list and an optional cover-letter `message`. Used from
    the Recaps page when kyle wants to send a deliverable directly
    to a client without going through the download → attach flow.

    `recipients` is a list of email addresses. Single-address case
    works; the backend de-duplicates and validates each before send.
    """

    recap_ids: list[strawberry.ID]
    recipients: list[str]
    title: str | None = None
    subtitle: str | None = None
    # Free-text cover letter rendered above the summary in the email
    # body. Optional — if omitted the email just says "Attached".
    message: str | None = None


@strawberry.input
class GenerateCampaignReportPdfInput(SparkGraphQLInput):
    """Bundle N recaps into one client-deliverable PDF.

    `recap_ids` is the relay-encoded ID list the front-end collects via
    multi-select on the Master Tracker or Recaps page. Both relay
    `Recap:N` IDs and bare ints/uuids resolve via the standard
    `resolve_id_to_int` helper.

    `title` and `subtitle` drive the cover page (e.g. "Liquid Death · May
    Sampling" with subtitle "Campaign Report"). Optional — sensible
    defaults applied when absent.
    """

    recap_ids: list[strawberry.ID]
    title: str | None = None
    subtitle: str | None = None


@strawberry.input
class ExportRecapXlsxInput(SparkGraphQLInput):
    id: strawberry.ID


@strawberry.input
class ExportCustomRecapsXlsxInput(SparkGraphQLInput):
    tenant_id: strawberry.ID | None = None
    custom_recap_template_id: strawberry.ID | None = None
    start_date: str | None = None
    end_date: str | None = None


@strawberry.input
class ExportCustomRecapXlsxInput(SparkGraphQLInput):
    id: strawberry.ID


@strawberry.input
class RecapFileDownloadUrlInput(SparkGraphQLInput):
    uuid: strawberry.ID


@strawberry.input
class CustomFieldValueInput(SparkGraphQLInput):
    custom_field_id: strawberry.ID | None = None
    custom_field_value_id: strawberry.ID | None = None
    value: str


@strawberry.input
class CreateCustomRecapInput(SparkGraphQLInput):
    name: str
    event_id: strawberry.ID
    custom_recap_template_id: strawberry.ID

    files: List[RecapFileInput] | None = None
    product_samples: List[ProductSampleInput] | None = None
    sales_performance: List[SalesPerformanceInput] | None = None

    total_engagements: int | None = None
    filling_for_ambassador: bool | None = None
    late: bool | None = None
    incomplete: bool | None = None
    approved: bool | None = None
    used_corpo_card: bool | None = None

    job_id: strawberry.ID | None = None
    retailer_id: strawberry.ID | None = None
    location_id: strawberry.ID | None = None
    state_id: strawberry.ID | None = None
    ambassador_id: strawberry.ID | None = None
    # Free-text BA name for reconciliation when the actual worker isn't
    # in Spark yet (sub-contractors, one-off helpers, not-yet-onboarded
    # BAs). Set alongside ambassador_id=null to record an "external" BA.
    # If both are sent, ambassador_id wins server-side. Mirrors
    # CreateRecapInput.external_ba_name.
    external_ba_name: str | None = None
    custom_field_values: List[CustomFieldValueInput] | None = None


@strawberry.input
class CreateCustomRecapMobileInput(SparkGraphQLInput):
    name: str
    job_id: strawberry.ID
    custom_recap_template_id: strawberry.ID

    files: List[RecapFileInput] | None = None
    product_samples: List[ProductSampleInput] | None = None
    sales_performance: List[SalesPerformanceInput] | None = None

    total_engagements: int | None = None
    filling_for_ambassador: bool | None = None
    late: bool | None = None
    incomplete: bool | None = None
    approved: bool | None = None
    used_corpo_card: bool | None = None

    custom_field_values: List[CustomFieldValueInput] | None = None


@strawberry.input
class UpdateCustomRecapInput(CreateCustomRecapInput):
    id: strawberry.ID


@strawberry.input
class UpdateCustomRecapMobileFilesInput(SparkGraphQLInput):
    add: List[RecapFileInput] | None = None
    remove: List[strawberry.ID] | None = None


@strawberry.input
class UpdateCustomRecapMobileInput(SparkGraphQLInput):
    id: strawberry.ID
    name: str

    files: UpdateCustomRecapMobileFilesInput | None = None
    product_samples: List[ProductSampleInput] | None = None
    sales_performance: List[SalesPerformanceInput] | None = None

    total_engagements: int | None = None
    filling_for_ambassador: bool | None = None
    late: bool | None = None
    incomplete: bool | None = None
    approved: bool | None = None
    used_corpo_card: bool | None = None

    custom_field_values: List[CustomFieldValueInput] | None = None


@strawberry.input
class CreateCustomFieldInput(SparkGraphQLInput):
    name: str
    custom_recap_template_id: strawberry.ID
    custom_field_type_id: strawberry.ID
    recap_section_id: strawberry.ID
    required: bool | None = None
    order: int | None = None


@strawberry.input
class UpdateCustomFieldInput(CreateCustomFieldInput):
    id: strawberry.ID


@strawberry.input
class CustomRecapTemplateFieldInput(SparkGraphQLInput):
    name: str
    custom_field_type_id: strawberry.ID
    recap_section_id: strawberry.ID
    id: strawberry.ID | None = None
    required: bool | None = None
    # Display order within the section. When omitted, the field's position in
    # this list is used (preserves the create-builder's top-to-bottom order).
    order: int | None = None


@strawberry.input
class CreateCustomRecapTemplateInput(SparkGraphQLInput):
    name: str
    event_type_id: strawberry.ID
    product_samples: bool | None = None
    sales_performance: bool | None = None
    layout: JSON | None = None
    custom_fields: List[CustomRecapTemplateFieldInput] | None = None


@strawberry.input
class RemoveCustomFieldInput(SparkGraphQLInput):
    """Force-delete a CustomField row from a template.

    The existing update_custom_recap_template / sync_fields path
    refuses to remove a field once any recap has submitted a value
    for it ("Cannot remove custom fields that already have submitted
    values"). That's correct as a default — accidental delete would
    nuke recap data.

    But there are real situations where a field WAS a mistake and
    needs to be removed retroactively (e.g. the Austin Psych Festival
    template has a duplicate metric that's confusing reports). For
    those, the admin sets ``delete_values=True`` and the mutation
    cascades the CustomFieldValue rows.
    """

    id: strawberry.ID
    # When True, also delete any CustomFieldValue rows tied to this
    # field. When False (default), the mutation errors out if any
    # value rows exist — same safety net as update_custom_recap_
    # template, just exposed as a separate, focused entry point.
    delete_values: bool = False


@strawberry.input
class UpdateCustomRecapTemplateInput(CreateCustomRecapTemplateInput):
    id: strawberry.ID


@strawberry.input
class CreateCustomRecapFieldTypeInput(SparkGraphQLInput):
    name: str


@strawberry.input
class UpdateCustomRecapFieldTypeInput(CreateCustomRecapFieldTypeInput):
    id: strawberry.ID


@strawberry.input
class CreateRecapSectionInput(SparkGraphQLInput):
    name: str
    tenant_id: strawberry.ID
    order: int | None = None


@strawberry.input
class UpdateRecapSectionInput(CreateRecapSectionInput):
    id: strawberry.ID


@strawberry.input
class MoveCustomFieldToSectionInput(SparkGraphQLInput):
    """Move an existing CustomField into a different RecapSection of the
    SAME template (template-structure edit — e.g. drag "Account Spend
    Amount" out of "Additional Insights" into "Engagement + Spend").

    This is a pure pointer change on ``CustomField.recap_section``: the
    field row and every ``CustomFieldValue`` already captured for it are
    preserved (the move mutation must NOT delete+recreate the field,
    which would orphan submitted answers). The target section must
    belong to the same template/tenant as the field — cross-template /
    cross-tenant moves are rejected.
    """

    # The CustomField being moved (Relay-encoded global id).
    field_id: strawberry.ID
    # The destination RecapSection (Relay-encoded global id). Must be a
    # section already in use by the field's template.
    section_id: strawberry.ID


@strawberry.input
class DeleteRecapSectionInput(SparkGraphQLInput):
    """Delete an (empty) RecapSection — the structural counterpart to
    moving fields out of a section first.

    Guard: the mutation REFUSES if the section still has any CustomField
    rows ("Move or remove this section's fields before deleting it.")
    rather than cascading away fields and their captured values. The FE
    requires the section be emptied (via moveCustomFieldToSection /
    removeCustomField) before this is offered.
    """

    # The RecapSection to delete (Relay-encoded global id).
    section_id: strawberry.ID


@strawberry.input
class ImportConnecteamRecapPdfInput(SparkGraphQLInput):
    """Drop a Connecteam-exported recap PDF onto an event, get back a
    pre-filled draft CustomRecap. Admin reviews/edits before approving."""

    event_id: strawberry.ID
    custom_recap_template_id: strawberry.ID
    # Base64-encoded PDF bytes. Avoids needing multipart upload spec
    # in the GraphQL transport. ~530KB encoded for a typical 400KB
    # Connecteam recap PDF — well under any practical request limit.
    pdf_base64: str
    # Optional override name; if blank we derive "Imported · <date>".
    name: str | None = None


@strawberry.input
class ParseConnecteamRecapPdfInput(SparkGraphQLInput):
    """Parse a Connecteam recap PDF WITHOUT creating anything — used by
    the standard admin recap-build form to pre-fill the numbers grid from
    a PDF, which the admin then reviews and submits via createRecap.

    Distinct from ImportConnecteamRecapPdfInput (which drafts a
    CustomRecap against a template). This path is read-only: decode,
    parse, map labels onto the legacy form fields, return the values."""

    # Base64-encoded PDF bytes (same transport as the import flow).
    pdf_base64: str
