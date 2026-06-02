from __future__ import annotations

import base64
import re
from io import BytesIO
from typing import Iterable

from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

# We import WeasyPrint lazily inside build_recap_pdf to avoid startup crashes
# when the native dependencies are missing.


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}
HEIF_BRANDS = {
    b"heic",
    b"heix",
    b"hevc",
    b"hevx",
    b"heim",
    b"heis",
    b"mif1",
    b"msf1",
}


def _normalize_ext(ext: str | None) -> str | None:
    """Lowercase + dot-prefix an extension token, or None if empty."""
    if not ext:
        return None
    n = str(ext).lower().strip()
    if not n:
        return None
    return n if n.startswith(".") else f".{n}"


def should_embed_recap_file(recap_file) -> bool:
    """
    Determine if a recap file should be embedded as an image in the PDF.

    The real filename extension is the source of truth. Custom-recap
    files carry a FileType whose `extension` is a category label like
    "img" (not a real extension) — trusting that turned every custom
    recap photo into ".img", which isn't an image extension, so the PDF
    embedded nothing. We now check the actual file/url extension first,
    fall back to the FileType.extension, then to the FileType name.
    """
    file_type = getattr(recap_file, "file_type", None)

    # 1. Real filename extension (strip any query/fragment first).
    file_value = getattr(recap_file, "file", None) or getattr(
        recap_file, "url", None
    )
    url_ext = None
    if file_value:
        clean = str(file_value).split("?", 1)[0].split("#", 1)[0]
        if "." in clean:
            url_ext = clean.rsplit(".", 1)[-1]

    # 2. FileType.extension as a secondary signal (often bogus, e.g. "img").
    type_ext = file_type.extension if (file_type and file_type.extension) else None

    if _normalize_ext(url_ext) in IMAGE_EXTENSIONS:
        return True
    if _normalize_ext(type_ext) in IMAGE_EXTENSIONS:
        return True

    # 3. Fall back to the FileType name ("image", "photo", …). This also
    #    catches images whose filename has no extension at all.
    if file_type and file_type.name:
        name = file_type.name.lower()
        # An explicit non-image type (e.g. "pdf") must NOT slip through.
        if "pdf" not in name and any(
            token in name
            for token in (
                "image",
                "photo",
                "picture",
                "jpeg",
                "jpg",
                "png",
                "heic",
                "heif",
                "webp",
            )
        ):
            return True

    return False


def _related_items(obj, attr: str) -> list:
    relation = getattr(obj, attr, None)
    if relation is None:
        return []
    if hasattr(relation, "all"):
        return list(relation.all())
    return list(relation)


def _submitted_at(recap):
    return getattr(recap, "submited_at", None) or getattr(recap, "submitted_at", None)


def _event_date(recap):
    """Resolve the recap's Event Date with the same fallback chain as the
    ``event_date`` GraphQL resolver (recaps/types.py), so the PDF matches the
    custom-recap info panel.

    Events materialized before the create-path fix (#718) have
    ``Event.date IS NULL`` but ``Event.start_time`` populated (and the date
    also lives on the parent Request). Prefer, in order:
        event.date → event.start_time → request.date → request.start_time
    Returns the raw datetime/date (caller formats via ``format_date_only``),
    or None when every source is absent. Null-safe throughout.
    """
    event = getattr(recap, "event", None)
    if event is None:
        return None
    value = getattr(event, "date", None) or getattr(event, "start_time", None)
    if value:
        return value
    request = getattr(event, "request", None)
    if request is not None:
        value = getattr(request, "date", None) or getattr(
            request, "start_time", None
        )
    return value or None


def _event_state(recap):
    """State for the custom-recap PDF branch, matching the ``event_state``
    resolver's fallback so the PDF agrees with the info panel. Prefers the
    Recap's own State FK (what the internal create may set), then walks the
    event: event.state → event.location.state → event.retailer.location.state.
    Returns a State-like object (``format_object_name`` reads ``.name``) or
    None. Null-safe throughout."""
    state = getattr(recap, "state", None)
    if state is not None:
        return state
    event = getattr(recap, "event", None)
    if event is None:
        return None
    state = getattr(event, "state", None)
    if state is not None:
        return state
    location = getattr(event, "location", None)
    loc_state = getattr(location, "state", None) if location else None
    if loc_state is not None:
        return loc_state
    retailer = getattr(event, "retailer", None)
    r_loc = getattr(retailer, "location", None) if retailer else None
    return getattr(r_loc, "state", None) if r_loc else None


def _event_retailer(recap):
    """Retailer for the custom-recap PDF branch, matching the ``event_retailer``
    resolver's fallback so the PDF agrees with the info panel. Prefers the
    Recap's own Retailer FK, then walks the event:
        event.retailer → event.request.retailer → event.request.retailer_name
    Returns a Retailer-like object OR a plain string (the request's
    ``retailer_name`` free-text), both of which ``format_object_name`` renders;
    or None. Null-safe throughout."""
    retailer = getattr(recap, "retailer", None)
    if retailer is not None:
        return retailer
    event = getattr(recap, "event", None)
    if event is None:
        return None
    retailer = getattr(event, "retailer", None)
    if retailer is not None:
        return retailer
    request = getattr(event, "request", None)
    if request is None:
        return None
    retailer = getattr(request, "retailer", None)
    if retailer is not None:
        return retailer
    name = (getattr(request, "retailer_name", None) or "").strip()
    return name or None


def _custom_field_sections(recap) -> dict[str, list[tuple[str, str]]]:
    sections: dict[str, list[tuple[str, str]]] = {}
    for custom_field_value in _related_items(recap, "custom_field_value"):
        custom_field = getattr(custom_field_value, "custom_field", None)
        recap_section = getattr(custom_field, "recap_section", None)
        section_name = getattr(recap_section, "name", None) or "Custom Fields"
        field_name = getattr(custom_field, "name", None) or "Custom field"
        sections.setdefault(section_name, []).append(
            (field_name, custom_field_value.value)
        )
    return sections


def detect_image_type(data: bytes) -> str | None:
    if not data:
        return None
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in HEIF_BRANDS:
        return "heic"
    return None


def is_image_bytes(data: bytes) -> bool:
    return detect_image_type(data) is not None


def _convert_heic_to_jpeg_bytes(image_bytes: bytes) -> bytes | None:
    try:
        register_heif_opener()
        with Image.open(BytesIO(image_bytes)) as image:
            normalized_image = ImageOps.exif_transpose(image)
            if normalized_image.mode != "RGB":
                normalized_image = normalized_image.convert("RGB")
            output = BytesIO()
            normalized_image.save(output, format="JPEG", quality=90)
            return output.getvalue()
    except Exception:
        return None


def bytes_to_data_uri(image_bytes: bytes) -> str | None:
    image_type = detect_image_type(image_bytes)
    if not image_type:
        return None
    if image_type == "heic":
        converted_bytes = _convert_heic_to_jpeg_bytes(image_bytes)
        if not converted_bytes:
            return None
        image_bytes = converted_bytes
        image_type = "jpeg"
    base64_data = base64.b64encode(image_bytes).decode("ascii")
    return f"data:image/{image_type};base64,{base64_data}"


def format_date_only(value) -> str:
    if not value:
        return "N/A"
    try:
        return value.strftime("%m/%d/%Y")
    except Exception:
        return str(value)


def safe(value) -> str:
    return "N/A" if value in (None, "") else str(value)


def format_user_name(user) -> str:
    if not user:
        return "N/A"
    first_name = getattr(user, "first_name", "") or ""
    last_name = getattr(user, "last_name", "") or ""
    full_name = f"{first_name} {last_name}".strip()
    if full_name:
        return full_name
    return getattr(user, "username", None) or str(user)


def format_user_email(user) -> str:
    if not user:
        return "N/A"
    return getattr(user, "email", None) or "N/A"


def format_bool(value) -> str:
    if value is None:
        return "N/A"
    return "Yes" if bool(value) else "No"


def format_object_name(obj) -> str:
    if not obj:
        return "N/A"
    return safe(getattr(obj, "name", None) or str(obj))


def _build_images_html(image_groups: dict[str, list[dict[str, str]]]) -> str:
    return (
        "".join(
            f'''
          <div class="image-group">
            <h3>{safe(category)}</h3>
            <div class="gallery">
              {"".join(f'<figure><img src="{item["data_uri"]}" /><figcaption>{safe(item["name"])}</figcaption></figure>' for item in items)}
            </div>
          </div>
          '''
            for category, items in image_groups.items()
        )
        or '<p class="empty">N/A</p>'
    )


def build_recap_pdf_html(
    recap,
    images: Iterable[dict[str, bytes]],
    custom_field_images: dict[str, bytes] | None = None,
) -> str:
    # Image-type custom fields (e.g. "Product Purchase Receipt (Image)")
    # store a GCS blob path as their value. The resolver pre-fetches those
    # blobs into `custom_field_images` ({blob_path: image_bytes}); when a
    # field's value matches a key here we embed the image instead of
    # printing the raw path. Legacy / campaign callers don't pass this, so
    # default to an empty map (those flows have no image custom fields).
    custom_field_images = custom_field_images or {}
    ambassador_user = None
    if getattr(recap, "ambassador", None) and getattr(recap.ambassador, "user", None):
        ambassador_user = recap.ambassador.user
    ambassador_name = format_user_name(ambassador_user)
    ambassador_email = format_user_email(ambassador_user)
    # External-BA fallback: when the recap was attributed to a worker
    # who isn't in Spark yet (sub-contractor, not-yet-onboarded BA),
    # the linked Ambassador FK is null but recap.external_ba_name has
    # the typed name. Surface that with an "(external)" tag so the PDF
    # actually credits them instead of rendering "N/A". Email is left
    # blank — external BAs don't have a Spark account by definition.
    external_ba_name = (getattr(recap, "external_ba_name", None) or "").strip()
    if ambassador_user is None and external_ba_name:
        ambassador_name = f"{external_ba_name} (external)"
        ambassador_email = ""

    engagements = _related_items(recap, "consumer_engagements")
    engagement = engagements[0] if engagements else None

    samples = []
    product_samples = _related_items(recap, "product_samples") or _related_items(
        recap, "custom_recap_product_sample"
    )
    for sample in product_samples:
        product_name = getattr(sample.product, "name", "Unknown product")
        samples.append(f"{product_name} - Qty: {sample.quantity}")

    sales = []
    sales_performance = _related_items(recap, "sales_performance") or _related_items(
        recap, "custom_recap_sale_performance"
    )
    for sale in sales_performance:
        product_name = getattr(sale.product, "name", "Unknown product")
        type_name = getattr(sale.type_of_good, "name", "Unknown type")
        sales.append(f"{product_name} ({type_name}) - ${sale.price}")

    feedback = _related_items(recap, "consumer_feedback")
    feedback_entry = feedback[0] if feedback else None

    account_feedback = _related_items(recap, "account_feedback")
    account_entry = account_feedback[0] if account_feedback else None
    custom_field_sections = _custom_field_sections(recap)

    def _render_custom_field(field_name: str, value) -> str:
        # Image-type field: value is a blob path we pre-fetched bytes for.
        # Embed the image (reusing the gallery figure/img styling) rather
        # than printing the raw blob path. bytes_to_data_uri handles the
        # HEIC→JPG conversion, same as the attachments path.
        if isinstance(value, str) and value in custom_field_images:
            data_uri = bytes_to_data_uri(custom_field_images[value])
            if data_uri:
                return (
                    '<div><span>{label}</span>'
                    '<figure><img src="{src}" />'
                    "<figcaption>{label}</figcaption></figure></div>"
                ).format(label=safe(field_name), src=data_uri)
        # Non-image field (or image bytes that failed to decode): text.
        return f"<div><span>{safe(field_name)}</span><p>{safe(value)}</p></div>"

    custom_fields_html = ""
    if custom_field_sections:
        custom_fields_html = "".join(
            f"""
    <section class="card">
      <h2>{safe(section_name)}</h2>
      <div class="stack">
        {"".join(_render_custom_field(field_name, value) for field_name, value in fields)}
      </div>
    </section>
"""
            for section_name, fields in custom_field_sections.items()
        )

    image_groups: dict[str, list[dict[str, str]]] = {}
    for image in images:
        image_bytes = image.get("bytes") or b""
        data_uri = bytes_to_data_uri(image_bytes)
        if not data_uri:
            continue
        category = image.get("category") or "Uncategorized"
        image_groups.setdefault(category, []).append(
            {
                "name": image.get("name") or "Image",
                "data_uri": data_uri,
            }
        )

    tenant_slug = (
        getattr(getattr(recap, "event", None), "tenant", None)
        and getattr(recap.event.tenant, "slug", None)
    ) or ""
    tenant_slug = tenant_slug.strip().lower()
    is_custom_recap = hasattr(recap, "custom_recap_template") or bool(
        _related_items(recap, "custom_field_value")
    )

    if is_custom_recap:
        summary_html = f"""
    <section class="card">
      <h2>Summary</h2>
      <div class="grid">
        <div><span>Name</span><strong>{safe(getattr(recap, "name", None))}</strong></div>
        <div><span>Event</span><strong>{format_object_name(getattr(recap, "event", None))}</strong></div>
        <div><span>Event Date</span><strong>{format_date_only(_event_date(recap))}</strong></div>
        <div><span>Total Engagements</span><strong>{safe(getattr(recap, "total_engagements", None))}</strong></div>
        <div><span>Used Corpo Card</span><strong>{format_bool(getattr(recap, "used_corpo_card", None))}</strong></div>
        <div><span>Timezone</span><strong>{format_object_name(getattr(recap, "timezone", None))}</strong></div>
        <div><span>Ambassador</span><strong>{safe(ambassador_name)}</strong></div>
        <div><span>Ambassador Email</span><strong>{safe(ambassador_email)}</strong></div>
        <div><span>State</span><strong>{format_object_name(_event_state(recap))}</strong></div>
        <div><span>City</span><strong>{format_object_name(getattr(recap, "location", None))}</strong></div>
        <div><span>Retailer</span><strong>{format_object_name(_event_retailer(recap))}</strong></div>
      </div>
    </section>
"""
    elif tenant_slug == "total-wireless":
        summary_html = f"""
    <section class="card">
      <h2>Summary</h2>
      <div class="grid">
        <div><span>Name</span><strong>{safe(recap.name)}</strong></div>
        <div><span>Event</span><strong>{safe(getattr(recap.event, "name", None))}</strong></div>
        <div><span>Event Date</span><strong>{format_date_only(_event_date(recap))}</strong></div>
        <div><span>Address</span><strong>{safe(getattr(recap.event, "address", None))}</strong></div>
        <div><span>Event Type</span><strong>{safe(getattr(getattr(recap.event, "event_type", None), "name", None))}</strong></div>
        <div><span>Total Consumers</span><strong>{safe(getattr(engagement, "total_consumer", None))}</strong></div>
      </div>
    </section>

    <section class="card">
      <h2>Consumer Feedback</h2>
      <div class="stack">
        <div><span>Feedback</span><p>{safe(getattr(feedback_entry, "feedback", None))}</p></div>
        <div><span>Quotes</span><p>{safe(getattr(feedback_entry, "quotes", None))}</p></div>
      </div>
    </section>

    <section class="card">
      <h2>Account Feedback</h2>
      <div class="stack">
        <div><span>Do Differently</span><p>{safe(getattr(account_entry, "do_differently_feedback", None))}</p></div>
        <div><span>Was Corporate Card Used?</span><p>{format_bool(getattr(account_entry, "was_corpo_card_used", None) if account_entry else None)}</p></div>
      </div>
    </section>
"""
    else:
        summary_html = f"""
    <section class="card">
      <h2>Summary</h2>
      <div class="grid">
        <div><span>Event</span><strong>{
        safe(getattr(recap.event, "name", None))
    }</strong></div>
        <div><span>Event Date</span><strong>{
        format_date_only(_event_date(recap))
    }</strong></div>
        <div><span>Submitted At</span><strong>{
        format_date_only(_submitted_at(recap))
    }</strong></div>
        <div><span>Ambassador</span><strong>{safe(ambassador_name)}</strong></div>
        <div><span>Ambassador Email</span><strong>{safe(ambassador_email)}</strong></div>
        <div><span>Job</span><strong>{
        safe(getattr(recap.job, "name", None))
    }</strong></div>
        <div><span>Retailer</span><strong>{
        safe(getattr(recap.retailer, "name", None))
    }</strong></div>
        <div><span>Total Engagements</span><strong>{
        safe(getattr(recap, "total_engagements", None))
    }</strong></div>
        <div><span>Products Sold</span><strong>{
        safe(getattr(recap, "products_sold", None))
    }</strong></div>
        <div><span>Total Cans Sold</span><strong>{
        safe(getattr(recap, "total_cans_sold", None))
    }</strong></div>
        <div><span>Total Packs Sold</span><strong>{
        safe(getattr(recap, "total_packs_sold", None))
    }</strong></div>
        <div><span>Total Earnings</span><strong>{
        safe(getattr(recap, "total_earnings", None))
    }</strong></div>
      </div>
    </section>

    <section class="card">
      <h2>Consumer Engagements</h2>
      <div class="grid">
        <div><span>Total Consumers</span><strong>{
        safe(getattr(engagement, "total_consumer", None))
    }</strong></div>
        <div><span>First Time Consumers</span><strong>{
        safe(getattr(engagement, "first_time_consumers", None))
    }</strong></div>
        <div><span>Brand Aware</span><strong>{
        safe(getattr(engagement, "brand_aware_consumers", None))
    }</strong></div>
        <div><span>Willing To Purchase</span><strong>{
        safe(getattr(engagement, "willing_to_purchase_consumers", None))
    }</strong></div>
        <div><span>Not Willing</span><strong>{
        safe(getattr(engagement, "not_willing_consumers", None))
    }</strong></div>
      </div>
    </section>

    <section class="card">
      <h2>Product Samples</h2>
      <ul class="list">
        {"".join(f"<li>{safe(item)}</li>" for item in samples) or "<li>N/A</li>"}
      </ul>
    </section>

    <section class="card">
      <h2>Sales Performance</h2>
      <ul class="list">
        {"".join(f"<li>{safe(item)}</li>" for item in sales) or "<li>N/A</li>"}
      </ul>
    </section>

    <section class="card">
      <h2>Consumer Feedback</h2>
      <div class="stack">
        <div><span>Demographics</span><p>{
        safe(getattr(feedback_entry, "demographics", None))
    }</p></div>
        <div><span>Feedback</span><p>{
        safe(getattr(feedback_entry, "feedback", None))
    }</p></div>
        <div><span>Quotes</span><p>{
        safe(getattr(feedback_entry, "quotes", None))
    }</p></div>
        <div><span>Positive Stories</span><p>{
        safe(getattr(feedback_entry, "positive_stories", None))
    }</p></div>
        <div><span>Reasons To Decline</span><p>{
        safe(getattr(feedback_entry, "reasons_to_decline", None))
    }</p></div>
      </div>
    </section>

    <section class="card">
      <h2>Account Feedback</h2>
      <div class="stack">
        <div><span>Do Differently</span><p>{
        safe(getattr(account_entry, "do_differently_feedback", None))
    }</p></div>
        <div><span>Feedback</span><p>{
        safe(getattr(account_entry, "feedback", None))
    }</p></div>
        <div><span>Corpo Card</span><p>{
        safe(getattr(account_entry, "corpo_card", None))
    }</p></div>
      </div>
    </section>
"""

    # Per-SKU Product Samples + Sales Performance. The legacy/template
    # branches embed these sections inside their own `summary_html`, but the
    # custom-recap `summary_html` is a short block that omits them — so for a
    # custom recap the per-SKU rows (already computed above in `samples` /
    # `sales`, and prefetched by generate_custom_recap_pdf) were silently
    # dropped from the PDF. Render a shared copy of those sections only for
    # custom recaps and splice it in right after the summary; the non-custom
    # branches keep their inline copies, so their output is unchanged.
    samples_sales_html = ""
    if is_custom_recap:
        samples_sales_html = f"""
    <section class="card">
      <h2>Product Samples</h2>
      <ul class="list">
        {"".join(f"<li>{safe(item)}</li>" for item in samples) or "<li>N/A</li>"}
      </ul>
    </section>

    <section class="card">
      <h2>Sales Performance</h2>
      <ul class="list">
        {"".join(f"<li>{safe(item)}</li>" for item in sales) or "<li>N/A</li>"}
      </ul>
    </section>
"""

    return f"""
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Recap - {safe(recap.name)}</title>
  </head>
  <body>
    <header class="header">
      <div>
        <h1>Recap Report</h1>
        <p class="subtitle">{safe(recap.name)}</p>
      </div>
      <div class="badge">
        <span>{"Approved" if recap.approved else "Pending"}</span>
      </div>
    </header>

    {summary_html}
    {samples_sales_html}
    {custom_fields_html}
    <section class="card">
      <h2>Images</h2>
      {_build_images_html(image_groups)}
    </section>
  </body>
</html>
"""


# Shared stylesheet used by both the per-recap PDF and the multi-recap
# campaign report. Kept as a module-level string so the campaign builder
# below can extend it with cover-page + page-break rules without
# duplicating the base styles. WeasyPrint resolves these via
# `CSS(string=...)` at render time.
_PDF_BASE_CSS = """
        @page {
            size: Letter;
            margin: 0.7in;
        }
        body {
            font-family: "Helvetica", "Arial", sans-serif;
            color: #1f2933;
            background: #f5f6f8;
            font-size: 11px;
        }"""


def build_recap_pdf(
    recap,
    images: Iterable[dict[str, bytes]],
    custom_field_images: dict[str, bytes] | None = None,
) -> bytes:
    from weasyprint import HTML, CSS

    html = build_recap_pdf_html(recap, images, custom_field_images=custom_field_images)

    css = CSS(
        string=_PDF_BASE_CSS + """
        h1 {
            font-size: 28px;
            margin: 0 0 4px 0;
        }
        h2 {
            font-size: 16px;
            margin: 0 0 10px 0;
        }
        .subtitle {
            margin: 0;
            font-size: 12px;
            color: #52606d;
        }
        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 18px;
        }
        .badge {
            background: #111827;
            color: #f9fafb;
            padding: 8px 14px;
            border-radius: 999px;
            font-weight: 600;
            font-size: 10px;
            text-transform: uppercase;
            letter-spacing: 0.08em;
        }
        .card {
            background: #ffffff;
            border-radius: 12px;
            padding: 14px 16px;
            box-shadow: 0 6px 16px rgba(15, 23, 42, 0.08);
            margin-bottom: 16px;
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 10px 18px;
        }
        .grid div span {
            display: block;
            font-size: 9px;
            color: #6b7280;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            margin-bottom: 4px;
        }
        .grid div strong {
            font-size: 12px;
            color: #111827;
        }
        .list {
            margin: 0;
            padding-left: 16px;
        }
        .list li {
            margin-bottom: 4px;
        }
        .stack div {
            margin-bottom: 10px;
        }
        .stack span {
            display: block;
            font-size: 9px;
            color: #6b7280;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            margin-bottom: 4px;
        }
        .stack p {
            margin: 0;
            font-size: 11px;
            color: #111827;
        }
        .stack figure {
            margin: 4px 0 0 0;
            background: #f3f4f6;
            border-radius: 10px;
            padding: 8px;
            text-align: center;
            max-width: 320px;
        }
        .stack figure img {
            max-width: 100%;
            max-height: 220px;
            object-fit: contain;
            border-radius: 6px;
        }
        .stack figcaption {
            margin-top: 6px;
            font-size: 9px;
            color: #6b7280;
        }
        .gallery {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 12px;
        }
        .image-group {
            margin-bottom: 14px;
        }
        .image-group h3 {
            margin: 0 0 8px 0;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: #6b7280;
        }
        .gallery figure {
            margin: 0;
            background: #f3f4f6;
            border-radius: 10px;
            padding: 8px;
            text-align: center;
        }
        .gallery img {
            max-width: 100%;
            max-height: 220px;
            object-fit: contain;
            border-radius: 6px;
        }
        .gallery figcaption {
            margin-top: 6px;
            font-size: 9px;
        }
        .empty {
            margin: 0;
            color: #9ca3af;
            font-style: italic;
        }
        """
    )

    return HTML(string=html).write_pdf(stylesheets=[css])


# ─── Campaign report ────────────────────────────────────────────────
#
# Combines multiple recaps into one PDF — the deliverable an agency
# hands to a client at the end of a sampling campaign. v1 design:
#   - One cover page with the campaign title, date range, recap count,
#     and aggregate consumer numbers (so the client sees the headline
#     before the per-recap detail)
#   - One detail page per recap, reusing the same body the single-recap
#     PDF renders. A `page-break-before: always` between recaps so each
#     starts cleanly on a fresh page.
#
# We do HTML-glue (one big doc → WeasyPrint once) rather than
# multi-PDF merging so we don't pull in pypdf. Each per-recap body is
# pulled from the existing `build_recap_pdf_html` and the surrounding
# `<html>/<body>` shell is stripped via a tolerant regex.

_RECAP_BODY_RE = re.compile(
    r"<body[^>]*>(?P<body>.*?)</body>",
    re.DOTALL | re.IGNORECASE,
)


def _extract_body(single_html: str) -> str:
    """Pull the body content out of a full recap HTML doc.

    Falls back to the whole string if the regex misses (e.g. someone
    rewrites build_recap_pdf_html without `<body>` later) — better to
    over-include than to silently drop a recap's data.
    """
    match = _RECAP_BODY_RE.search(single_html)
    return match.group("body") if match else single_html


def _leading_int(value) -> int | None:
    """Pull a non-negative integer out of a free-text field value
    ('70', '70 cans', '  68 '). None when there's no number."""
    if value is None:
        return None
    m = re.search(r"\d+", str(value))
    return int(m.group()) if m else None


def _custom_engagement_totals(recap) -> dict[str, int]:
    """Map a custom recap's free-text engagement fields onto the four
    cover metrics by label. Mirrors the dashboard's label matching so the
    campaign report and dashboard report the same numbers. Borjomi/Girl
    Beer store these as CustomFieldValue rows, not consumer_engagements.
    """
    out = {
        "total_consumer": 0,
        "first_time_consumers": 0,
        "brand_aware_consumers": 0,
        "willing_to_purchase_consumers": 0,
    }
    for cfv in _related_items(recap, "custom_field_value"):
        cf = getattr(cfv, "custom_field", None)
        label = (getattr(cf, "name", "") or "").lower()
        val = _leading_int(getattr(cfv, "value", None))
        if val is None:
            continue
        if "consumers sampled" in label:
            out["total_consumer"] += val
        elif "first time" in label:
            out["first_time_consumers"] += val
        elif "knew about" in label:
            out["brand_aware_consumers"] += val
        elif "willing to purchase" in label and "not" not in label:
            # excludes "would NOT be willing to purchase"
            out["willing_to_purchase_consumers"] += val
    return out


def _aggregate_engagements(recaps: Iterable) -> dict[str, int]:
    """Sum the consumer-engagement numbers across recaps for the cover
    page. Returns zeros for any field that's missing on every recap so
    the template doesn't have to guard each cell.

    Legacy recaps carry a consumer_engagements row; custom recaps
    (Borjomi/Girl Beer) keep the same numbers as free-text custom fields
    — without folding those in, the cover read 0 for every custom recap.
    """
    totals = {
        "total_consumer": 0,
        "first_time_consumers": 0,
        "brand_aware_consumers": 0,
        "willing_to_purchase_consumers": 0,
    }
    for recap in recaps:
        engagements = _related_items(recap, "consumer_engagements")
        if engagements:
            eng = engagements[0]
            for key in totals.keys():
                val = getattr(eng, key, None)
                if isinstance(val, (int, float)):
                    totals[key] += int(val)
            continue
        # No legacy engagement row → try custom-field engagement values.
        custom = _custom_engagement_totals(recap)
        for key in totals.keys():
            totals[key] += custom[key]
    return totals


def _format_date_range(recaps: list) -> str:
    """Earliest → latest event date label for the cover page. Falls back
    to '—' when no events have a usable date.
    """
    dates: list = []
    for r in recaps:
        ev = getattr(r, "event", None)
        d = getattr(ev, "date", None) if ev else None
        if d:
            dates.append(d)
    if not dates:
        return "—"
    dates.sort()
    if dates[0] == dates[-1]:
        return dates[0].strftime("%b %d, %Y")
    return f"{dates[0].strftime('%b %d, %Y')} – {dates[-1].strftime('%b %d, %Y')}"


def build_campaign_report_pdf(
    *,
    title: str,
    subtitle: str,
    recaps_with_images: list,
) -> bytes:
    """Build a multi-recap campaign report PDF.

    `recaps_with_images` is a list of (recap, images) tuples — same
    shape `build_recap_pdf` takes per row.

    Returns the rendered PDF bytes; callers upload to GCS via the
    existing recap-file pattern.
    """
    from weasyprint import HTML, CSS

    if not recaps_with_images:
        raise ValueError("Campaign report requires at least one recap")

    recap_objs = [r for (r, _imgs) in recaps_with_images]
    totals = _aggregate_engagements(recap_objs)
    date_range = _format_date_range(recap_objs)
    count = len(recap_objs)

    # Cover page — campaign-level rollup so the client sees the
    # headline before flipping to per-recap detail.
    cover_html = f"""
    <section class="cover-card">
      <p class="cover-eyebrow">{safe(subtitle)}</p>
      <h1 class="cover-title">{safe(title)}</h1>
      <p class="cover-date">{safe(date_range)} · {count} recap{'s' if count != 1 else ''}</p>
      <div class="cover-accent"></div>

      <div class="cover-stats">
        <div>
          <span>Total Consumers</span>
          <strong>{totals['total_consumer']:,}</strong>
        </div>
        <div>
          <span>First-Time</span>
          <strong>{totals['first_time_consumers']:,}</strong>
        </div>
        <div>
          <span>Brand Aware</span>
          <strong>{totals['brand_aware_consumers']:,}</strong>
        </div>
        <div>
          <span>Willing To Buy</span>
          <strong>{totals['willing_to_purchase_consumers']:,}</strong>
        </div>
      </div>
    </section>
    """

    detail_pages: list[str] = []
    for idx, (recap, images) in enumerate(recaps_with_images):
        body = _extract_body(build_recap_pdf_html(recap, images))
        # Force each detail block to start on a fresh page (the cover
        # owns the first page).
        detail_pages.append(
            f'<div class="recap-detail" style="page-break-before: always">{body}</div>'
        )

    html_doc = f"""
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>{safe(title)}</title>
  </head>
  <body>
    {cover_html}
    {"".join(detail_pages)}
  </body>
</html>
"""

    cover_css = """
        .cover-card {
            background: #ffffff;
            border-radius: 16px;
            padding: 56px 48px;
            box-shadow: 0 12px 30px rgba(15, 23, 42, 0.12);
            text-align: center;
            margin-top: 40px;
        }
        .cover-eyebrow {
            margin: 0 0 8px 0;
            font-size: 11px;
            color: #6b7280;
            text-transform: uppercase;
            letter-spacing: 0.18em;
        }
        .cover-title {
            margin: 0 0 6px 0;
            font-size: 36px;
        }
        .cover-date {
            margin: 0 0 20px 0;
            font-size: 13px;
            color: #52606d;
        }
        .cover-accent {
            width: 48px;
            height: 4px;
            background: #c5f546;
            border-radius: 2px;
            margin: 0 auto 32px auto;
        }
        .cover-stats {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 16px;
            margin-top: 18px;
        }
        .cover-stats div {
            background: #f5f6f8;
            border-radius: 12px;
            border-top: 3px solid #c5f546;
            padding: 18px 10px;
        }
        .cover-stats span {
            display: block;
            font-size: 9px;
            color: #6b7280;
            text-transform: uppercase;
            letter-spacing: 0.10em;
            margin-bottom: 8px;
        }
        .cover-stats strong {
            font-size: 26px;
            font-weight: 700;
            color: #111827;
        }
        .recap-detail .header h1 {
            font-size: 22px;
        }
    """

    # Pull in the same base CSS the per-recap PDF uses so card / grid
    # / gallery styling is identical between the standalone and rollup
    # variants.
    single_pdf_css = """
        h1 {
            font-size: 28px;
            margin: 0 0 4px 0;
        }
        h2 {
            font-size: 16px;
            margin: 0 0 10px 0;
        }
        .subtitle { margin: 0; font-size: 12px; color: #52606d; }
        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 18px;
        }
        .badge {
            background: #111827;
            color: #f9fafb;
            padding: 8px 14px;
            border-radius: 999px;
            font-weight: 600;
            font-size: 10px;
            text-transform: uppercase;
            letter-spacing: 0.08em;
        }
        .card {
            background: #ffffff;
            border-radius: 12px;
            padding: 14px 16px;
            box-shadow: 0 6px 16px rgba(15, 23, 42, 0.08);
            margin-bottom: 16px;
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 10px 18px;
        }
        .grid div span {
            display: block;
            font-size: 9px;
            color: #6b7280;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            margin-bottom: 4px;
        }
        .grid div strong { font-size: 12px; color: #111827; }
        .list { margin: 0; padding-left: 16px; }
        .list li { margin-bottom: 4px; }
        .stack div { margin-bottom: 10px; }
        .stack span {
            display: block;
            font-size: 9px;
            color: #6b7280;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            margin-bottom: 4px;
        }
        .stack p { margin: 0; font-size: 11px; color: #111827; }
        .stack figure {
            margin: 4px 0 0 0;
            background: #f3f4f6;
            border-radius: 10px;
            padding: 8px;
            text-align: center;
            max-width: 320px;
        }
        .stack figure img {
            max-width: 100%;
            max-height: 220px;
            object-fit: contain;
            border-radius: 6px;
        }
        .stack figcaption { margin-top: 6px; font-size: 9px; color: #6b7280; }
        .gallery {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 12px;
        }
        .image-group { margin-bottom: 14px; }
        .image-group h3 {
            margin: 0 0 8px 0;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: #6b7280;
        }
        .gallery figure {
            margin: 0;
            background: #f3f4f6;
            border-radius: 10px;
            padding: 8px;
            text-align: center;
        }
        .gallery img {
            max-width: 100%;
            max-height: 220px;
            object-fit: contain;
            border-radius: 6px;
        }
        .gallery figcaption { margin-top: 6px; font-size: 9px; }
        .empty { margin: 0; color: #9ca3af; font-style: italic; }
    """

    css = CSS(string=_PDF_BASE_CSS + single_pdf_css + cover_css)
    return HTML(string=html_doc).write_pdf(stylesheets=[css])
