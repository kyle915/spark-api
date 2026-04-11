from __future__ import annotations

import base64
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


def should_embed_recap_file(recap_file) -> bool:
    """
    Determine if a recap file should be embedded as an image in the PDF.
    """
    extension = None
    if getattr(recap_file, "file_type", None) and recap_file.file_type.extension:
        extension = recap_file.file_type.extension
    else:
        file_value = getattr(recap_file, "file", None) or getattr(recap_file, "url", None)
        if file_value:
            file_name = str(file_value)
            if "." in file_name:
                extension = file_name.rsplit(".", 1)[-1]

    if not extension:
        if getattr(recap_file, "file_type", None) and recap_file.file_type.name:
            name = recap_file.file_type.name.lower()
            if any(
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
                )
            ):
                return True
        return False

    normalized = extension.lower()
    if not normalized.startswith("."):
        normalized = f".{normalized}"
    return normalized in IMAGE_EXTENSIONS


def _related_items(obj, attr: str) -> list:
    relation = getattr(obj, attr, None)
    if relation is None:
        return []
    if hasattr(relation, "all"):
        return list(relation.all())
    return list(relation)


def _submitted_at(recap):
    return getattr(recap, "submited_at", None) or getattr(recap, "submitted_at", None)


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


def build_recap_pdf_html(recap, images: Iterable[dict[str, bytes]]) -> str:
    ambassador_user = None
    if getattr(recap, "ambassador", None) and getattr(recap.ambassador, "user", None):
        ambassador_user = recap.ambassador.user
    ambassador_name = format_user_name(ambassador_user)
    ambassador_email = format_user_email(ambassador_user)

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
    custom_fields_html = ""
    if custom_field_sections:
        custom_fields_html = "".join(
            f"""
    <section class="card">
      <h2>{safe(section_name)}</h2>
      <div class="stack">
        {"".join(f"<div><span>{safe(field_name)}</span><p>{safe(value)}</p></div>" for field_name, value in fields)}
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
        <div><span>Event Date</span><strong>{format_date_only(getattr(getattr(recap, "event", None), "date", None))}</strong></div>
        <div><span>Total Engagements</span><strong>{safe(getattr(recap, "total_engagements", None))}</strong></div>
        <div><span>Used Corpo Card</span><strong>{format_bool(getattr(recap, "used_corpo_card", None))}</strong></div>
        <div><span>Timezone</span><strong>{format_object_name(getattr(recap, "timezone", None))}</strong></div>
        <div><span>Ambassador</span><strong>{safe(ambassador_name)}</strong></div>
        <div><span>Ambassador Email</span><strong>{safe(ambassador_email)}</strong></div>
        <div><span>State</span><strong>{format_object_name(getattr(recap, "state", None))}</strong></div>
        <div><span>City</span><strong>{format_object_name(getattr(recap, "location", None))}</strong></div>
        <div><span>Retailer</span><strong>{format_object_name(getattr(recap, "retailer", None))}</strong></div>
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
        <div><span>Event Date</span><strong>{format_date_only(getattr(recap.event, "date", None))}</strong></div>
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
        format_date_only(getattr(recap.event, "date", None))
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
    {custom_fields_html}
    <section class="card">
      <h2>Images</h2>
      {_build_images_html(image_groups)}
    </section>
  </body>
</html>
"""


def build_recap_pdf(recap, images: Iterable[dict[str, bytes]]) -> bytes:
    from weasyprint import HTML, CSS

    html = build_recap_pdf_html(recap, images)

    css = CSS(
        string="""
        @page {
            size: Letter;
            margin: 0.7in;
        }
        body {
            font-family: "Helvetica", "Arial", sans-serif;
            color: #1f2933;
            background: #f5f6f8;
            font-size: 11px;
        }
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
