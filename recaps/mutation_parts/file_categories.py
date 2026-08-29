"""File-category sentinel resolution for recap uploads."""
from graphql import GraphQLError

from recaps import models
from utils.graphql.mixins import resolve_id_to_int

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

# Name / slug aliases the web uploaders send so "1"/"2" never collide
# with a real FileRecapCategory PK (Liquid Death "Table Set Up" = PK 2).
# Mobile may still send the positional sentinels; those stay mapped.
_FILE_CATEGORY_ROLE_ALIASES = {
    "1": "1",
    "2": "2",
    "sampling photos": "1",
    "photo": "1",
    "photos": "1",
    "receipts": "2",
    "receipt": "2",
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
    # "spend" covers Torch THC "Product Spend" once classic "Receipts" is
    # retired — without it the receipt sentinel self-heals a fresh Receipts row.
    "2": ("receipt", "spend", "purchase", "expense"),
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


def _resolve_role_file_recap_category(sentinel, *, tenant_id):
    """Resolve a positional ROLE sentinel to the tenant's OWN role category.

    `sentinel` is the bare string an upload widget sends as a *slot marker* —
    "1" = the sampling-photos slot, "2" = the receipts slot. It is NOT a DB PK.
    Returns None when `sentinel` isn't a known role marker (or there's no tenant
    to scope to), so the caller can fall through to PK behaviour.

    Never resolves cross-tenant: a role that this tenant has no category for is
    self-healed into one rather than borrowed from whoever owns that PK.
    """
    role_key = _FILE_CATEGORY_ROLE_ALIASES.get(str(sentinel or "").strip().lower())
    sentinel_name = _FILE_CATEGORY_SENTINEL_NAMES.get(role_key) if role_key else None
    if sentinel_name is None or tenant_id is None:
        return None
    # 1) Exact seeded role name (fast path for tenants on the defaults).
    by_name = models.FileRecapCategory.objects.filter(
        tenant_id=tenant_id, name__iexact=sentinel_name
    ).first()
    if by_name is not None:
        return by_name
    # 2) Naming variant (custom-template tenants like Girl Beer): match the
    #    role by keyword so a receipt sentinel still lands on a category
    #    named "Receipt" / "Upload Receipt" / "Product Purchase Receipt"
    #    rather than mis-filing into "Table setup" via the PK fallback.
    #    Tenant-scoped; lowest id wins on ties.
    for keyword in _FILE_CATEGORY_SENTINEL_KEYWORDS.get(role_key, ()):
        by_keyword = (
            models.FileRecapCategory.objects.filter(
                tenant_id=tenant_id, name__icontains=keyword
            )
            .order_by("id")
            .first()
        )
        if by_keyword is not None:
            return by_keyword
    # 3) No matching role category for this tenant at all — SELF-HEAL: create
    #    the tenant's own role category under the seeded default name instead
    #    of falling through to the PK path. The PK path could only land the
    #    file in ANOTHER tenant's category (the Girl Beer leak: a tenant
    #    onboarded outside createTenant has no categories, so sentinel "2"
    #    fell through to the global PK-2 "Table setup" owned by a different
    #    tenant). Sentinels are role markers; they must never resolve
    #    cross-tenant.
    seeded, _created = models.FileRecapCategory.objects.get_or_create(
        tenant_id=tenant_id, name=sentinel_name
    )
    return seeded


def _resolve_explicit_file_recap_category(raw_id, *, tenant_id):
    """Resolve a category the caller picked EXPLICITLY, by real PK.

    Use this — NOT `_resolve_file_recap_category` — whenever `raw_id` is a
    genuine FileRecapCategory primary key: a category chosen in the management
    UI, a validated check-in bucket, an id echoed back from a previous read.
    This resolver never reads "1"/"2" as positional role sentinels, which is
    the entire point. A tenant's own category can legitimately HAVE PK 1 or 2
    (Liquid Death's "Table Set Up" is PK 2), so routing a real pick through the
    sentinel-aware helper files it under "Receipts" — the very mis-file that
    helper exists to prevent, arrived at from the other direction.

    Tenant-scoped: the tenant's own row with that exact PK, then the tenant's
    row sharing the referenced row's name, else None — never another tenant's
    row. (Only a tenantless call may return the raw global row.)

    Accepts a relay global id or a bare int, via `resolve_id_to_int`.

    Never raises — a stray category id must not lose the recap or its files.
    """
    if raw_id in (None, ""):
        return None
    try:
        category_id = resolve_id_to_int(raw_id)
    except (TypeError, ValueError, GraphQLError):
        return None
    if category_id is None:
        return None
    global_cat = models.FileRecapCategory.objects.filter(id=category_id).first()
    if tenant_id is None:
        return global_cat
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


def _resolve_file_recap_category(raw_id, *, tenant_id):
    """Resolve a FileRecapCategory for an upload whose id may be a ROLE MARKER.

    ⚠️ Which resolver do you want?

    * The value is a *positional slot marker* baked into an upload widget —
      "1" = photos, "2" = receipts, sent per-file by spark-mobile's
      RecapSubmitScreen, the admin SparkRecapCreate / SparkRecapView uploaders,
      and the Connecteam importer. → THIS function.
    * The value is a *real primary key* the user or caller explicitly chose (a
      management-UI pick, a validated check-in bucket, an id echoed back from a
      read). → `_resolve_explicit_file_recap_category`.

    The two cases are indistinguishable from the string alone: "2" is both the
    receipts slot marker and a perfectly valid PK. Overloading one path is what
    filed every Liquid Death "Table Set Up" photo (PK 2) under "Receipts", so
    pick the resolver that matches where the id came from rather than letting
    this one guess.

    FileRecapCategory rows are PER-TENANT, but the widgets send the same
    sentinel for every brand. The old code matched that sentinel as a raw PK
    (own tenant's exact PK first, then the global row's name): because defaults
    are seeded ["Sampling photos", "Table setup", "Receipts"], the global PK 2
    is "Table setup", so a receipt sentinel "2" landed under "Table setup"
    instead of "Receipts".

    Resolution order:
      1. A known positional sentinel ("1"/"2") resolves to the tenant's OWN
         role category — exact seeded role NAME, then role keyword, and if the
         tenant has no matching category at all, CREATE it (self-heal). A
         sentinel never resolves cross-tenant.
      2. An exact category NAME for this tenant (e.g. "Product Spend",
         "Table setup") — what RecapCustomView's upload / Move-to dropdowns
         send. Without this step a non-role name fell through to the PK
         parser, resolved to None, and the file landed Uncategorized.
      3. Anything else is treated as an explicit PK and handed to
         `_resolve_explicit_file_recap_category` (tenant-scoped).

    Never raises — a stray category id must not lose the recap or its files.
    """
    if raw_id in (None, ""):
        return None

    raw = str(raw_id).strip()

    by_role = _resolve_role_file_recap_category(raw, tenant_id=tenant_id)
    if by_role is not None:
        return by_role

    # Exact tenant category name (Product Spend, Table setup, …). Skip bare
    # digits so we don't treat "2" as a name after the role path already passed.
    if tenant_id is not None and not raw.isdigit():
        by_name = models.FileRecapCategory.objects.filter(
            tenant_id=tenant_id, name__iexact=raw
        ).first()
        if by_name is not None:
            return by_name

    return _resolve_explicit_file_recap_category(raw_id, tenant_id=tenant_id)
