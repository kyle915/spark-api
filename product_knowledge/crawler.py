"""A bounded, polite crawl of one brand's own website.

What it does, in order:

1. Reads ``robots.txt`` (a 401/403 or an unreachable/5xx robots file stops
   the crawl — we only crawl when the site lets us) and honours its rules for
   the ``SparkBot`` agent.
2. Tries Shopify's public ``/products.json`` — clean title / type / body /
   options per product, no HTML scraping. When it works, product pages are not
   crawled again as HTML.
3. Seeds the homepage plus URLs from the sitemap(s), then repeatedly fetches
   the highest-scoring unvisited same-site URL: product, shop, collection,
   FAQ, about, nutrition and ingredient pages score high; carts, accounts,
   policies, search, blogs and other languages score low or are skipped.
4. Stops at ``max_pages`` pages or the time budget, whichever comes first.

Only URLs on the brand's own host (or its subdomains) are ever fetched —
links and redirects anywhere else are dropped. Pages are size-capped, and the
text is stored as untrusted data in ``KnowledgeSource``.
"""

from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from .extract import clean_text, clip, html_to_text, pdf_to_text, PAGE_TEXT_MAX_CHARS
from .fetch import DOC_MAX_BYTES, HTML_MAX_BYTES, Deadline, Fetcher
from .models import KnowledgeSource
from .store import existing, save_source, sha256, touch

logger = logging.getLogger(__name__)

ROBOTS_AGENT = "SparkBot"
MAX_PAGES = 25
MAX_SHOPIFY_PRODUCTS = 120
MAX_SITEMAP_URLS = 3000
MAX_CHILD_SITEMAPS = 4
MAX_CRAWL_DELAY = 2.0
SITEMAP_MAX_BYTES = 3 * 1024 * 1024
PRODUCTS_JSON_MAX_BYTES = 6 * 1024 * 1024

_ASSET_EXT = re.compile(
    r"\.(jpe?g|png|gif|webp|avif|svg|ico|css|js|mjs|json|xml|txt|zip|gz|mp4|mov|webm|mp3|"
    r"woff2?|ttf|eot|otf|csv|xlsx?|pptx?|docx?)$",
    re.I,
)
_LOCALE = re.compile(r"^/([a-z]{2})(-[a-z]{2})?(/|$)", re.I)

_POSITIVE = {
    "product": 30,
    "products": 30,
    "shop": 18,
    "collections": 12,
    "collection": 12,
    "faq": 35,
    "faqs": 35,
    "questions": 15,
    "about": 22,
    "story": 15,
    "our": 3,
    "nutrition": 40,
    "ingredients": 40,
    "ingredient": 40,
    "flavors": 28,
    "flavor": 28,
    "flavours": 28,
    "menu": 15,
    "drinks": 15,
    "beverages": 15,
    "lineup": 15,
    "benefits": 10,
    "how": 4,
    "works": 6,
    "learn": 10,
    "science": 12,
    "inside": 15,
    "specs": 15,
    "facts": 25,
    "lab": 10,
    "coa": 12,
    "help": 10,
    "allergens": 30,
    "caffeine": 30,
    "sustainability": 6,
    "mission": 8,
}
# Any of these anywhere in the path: never fetched.
_BLOCKED = {
    "cart",
    "checkout",
    "checkouts",
    "account",
    "accounts",
    "login",
    "logout",
    "register",
    "signin",
    "signup",
    "password",
    "search",
    "policies",
    "policy",
    "privacy",
    "terms",
    "wp-admin",
    "wp-json",
    "wp-login",
    "cdn-cgi",
    "feed",
    "tagged",
    "orders",
    "admin",
    "cookie",
    "cookies",
    "unsubscribe",
}
_NEGATIVE = {
    "blog": -25,
    "blogs": -25,
    "news": -20,
    "press": -20,
    "careers": -40,
    "jobs": -40,
    "locator": -25,
    "stores": -10,
    "store-locator": -25,
    "wholesale": -15,
    "affiliate": -30,
    "rewards": -20,
    "gift": -15,
    "gifts": -10,
    "contact": -8,
    "shipping": -30,
    "returns": -30,
    "refund": -30,
    "subscribe": -20,
    "author": -30,
    "tag": -30,
    "category": -10,
    "sitemap": -40,
    "page": -5,
}


# ---------------------------------------------------------------- URL rules


def normalize_site_url(raw: str) -> str:
    """``https://famusenergy.com`` from whatever staff typed, or "" if it
    isn't a plain public http(s) site address."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme.lower() not in ("http", "https") or not host or "." not in host:
        return ""
    if parts.username or parts.password or (parts.port and parts.port not in (80, 443)):
        return ""
    if not re.fullmatch(r"[a-z0-9.-]+", host) or re.fullmatch(r"[0-9.]+", host):
        return ""
    return f"{parts.scheme.lower()}://{host}"


def site_host(site_url: str) -> str:
    host = (urlsplit(site_url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def on_site(url: str, base_host: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host == base_host or host.endswith("." + base_host)


def canonical(url: str) -> str:
    """Scheme + host + path; no query, fragment or trailing slash."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), host, path, "", ""))


def display_url(url: str) -> str:
    """``famusenergy.com/products/og-luxe`` — how a source is shown to a BA."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parts.path or "").rstrip("/")
    return (host + path)[:160]


def score_url(url: str, *, shopify: bool = False) -> int:
    path = urlsplit(url).path.lower()
    if path in ("", "/"):
        return 100
    if _ASSET_EXT.search(path) and not path.endswith(".pdf"):
        return -100
    if (m := _LOCALE.match(path)) and m.group(1) != "en":
        return -100
    segments = [s for s in path.split("/") if s]
    words = set(re.split(r"[-_.]+", path.replace("/", "-")))
    if words & _BLOCKED or set(segments) & _BLOCKED:
        return -100
    # Shopify product pages are covered by products.json already.
    if shopify and len(segments) >= 2 and segments[-2] == "products":
        return -100
    score = 0
    for w in words:
        score += _POSITIVE.get(w, 0)
        score += _NEGATIVE.get(w, 0)
    for seg in segments:
        score += _NEGATIVE.get(seg, 0) if seg not in words else 0
    score -= 4 * max(0, len(segments) - 2)
    if path.endswith(".pdf"):
        score += 5
    return score


# ---------------------------------------------------------------- robots / sitemap


def _robots(
    fetcher: Fetcher, site: str, base_host: str
) -> tuple[RobotFileParser | None, str]:
    rp = RobotFileParser()
    res = fetcher.get(
        f"{site}/robots.txt",
        max_bytes=512 * 1024,
        allow_url=lambda u: on_site(u, base_host),
    )
    if res.status in (401, 403):
        return None, "robots.txt denies access"
    if res.ok:
        rp.parse(res.data.decode("utf-8", "replace").splitlines())
        return rp, ""
    if 400 <= res.status < 500:
        rp.parse([])  # no robots file = everything allowed
        return rp, ""
    return None, "robots.txt unreachable"


def _sitemap_locs(xml: bytes) -> tuple[list[str], bool]:
    text = xml.decode("utf-8", "replace")
    is_index = "<sitemapindex" in text[:2000].lower()
    locs = re.findall(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\s\]]+)", text, re.I)
    return locs[:MAX_SITEMAP_URLS], is_index


def _sitemap_urls(fetcher, rp, site, base_host, deadline, can) -> list[str]:
    roots = [u for u in (rp.site_maps() or []) if on_site(u, base_host)] or [
        f"{site}/sitemap.xml"
    ]
    urls: list[str] = []
    allow = lambda u: on_site(u, base_host)  # noqa: E731
    for root in roots[:3]:
        if deadline.expired:
            break
        res = fetcher.get(root, max_bytes=SITEMAP_MAX_BYTES, allow_url=allow)
        if not res.ok:
            continue
        locs, is_index = _sitemap_locs(res.data)
        if not is_index:
            urls.extend(locs)
            continue
        children = sorted(
            (u for u in locs if on_site(u, base_host)),
            key=lambda u: -max(
                (
                    _POSITIVE.get(w, 0)
                    for w in re.split(r"[-_./]+", urlsplit(u).path.lower())
                ),
                default=0,
            ),
        )
        for child in children[:MAX_CHILD_SITEMAPS]:
            if deadline.expired:
                break
            sub = fetcher.get(child, max_bytes=SITEMAP_MAX_BYTES, allow_url=allow)
            if sub.ok:
                urls.extend(_sitemap_locs(sub.data)[0])
    return [u for u in urls if on_site(u, base_host) and can(u)][:MAX_SITEMAP_URLS]


# ---------------------------------------------------------------- Shopify


def _html_fragment_text(fragment: str) -> str:
    return html_to_text(f"<div>{fragment or ''}</div>")["text"]


def shopify_product_text(product: dict) -> str:
    lines = [f"Product: {str(product.get('title') or '').strip()}"]
    ptype = str(product.get("product_type") or "").strip()
    if ptype:
        lines.append(f"Type: {ptype}")
    body = _html_fragment_text(str(product.get("body_html") or ""))
    if body:
        lines.append(body)
    for opt in product.get("options") or []:
        if not isinstance(opt, dict):
            continue
        name = str(opt.get("name") or "").strip()
        values = [str(v).strip() for v in (opt.get("values") or []) if str(v).strip()]
        if name and values and values != ["Default Title"]:
            lines.append(f"{name}: {', '.join(values[:30])}")
    tags = product.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    tags = [t for t in tags if t and not t.startswith("_") and ":" not in t][:25]
    if tags:
        lines.append("Tags: " + ", ".join(tags))
    return clip(clean_text("\n".join(lines)), PAGE_TEXT_MAX_CHARS)


def _shopify(fetcher, tenant_id, site, base_host, can, seen_refs, stats) -> bool:
    url = f"{site}/products.json"
    if not can(url):
        return False
    res = fetcher.get(
        url + "?limit=250",
        max_bytes=PRODUCTS_JSON_MAX_BYTES,
        allow_url=lambda u: on_site(u, base_host),
    )
    if not res.ok:
        return False
    try:
        data = json.loads(res.data.decode("utf-8", "replace"))
    except ValueError:
        return False
    products = data.get("products") if isinstance(data, dict) else None
    if not isinstance(products, list):
        return False
    stats["shopify"] = True
    for product in products[:MAX_SHOPIFY_PRODUCTS]:
        if not isinstance(product, dict):
            continue
        handle = re.sub(r"[^a-z0-9-]", "", str(product.get("handle") or "").lower())
        title = str(product.get("title") or "").strip()
        if not handle or not title:
            continue
        ref = canonical(f"{site}/products/{handle}")
        seen_refs.add(ref)
        prior = existing(tenant_id, ref)
        if prior is not None and prior.excluded:
            stats["skipped"] += 1
            continue
        text = shopify_product_text(product)
        _row, changed = save_source(
            tenant_id=tenant_id,
            kind=KnowledgeSource.KIND_SHOPIFY_PRODUCT,
            ref=ref,
            url=ref,
            title=title,
            text=text,
            raw_hash=sha256(text),
        )
        stats["products"] += 1
        stats["changed" if changed else "unchanged"] += 1
    return True


# ---------------------------------------------------------------- crawl


def crawl_website(
    tenant,
    fetcher: Fetcher,
    *,
    deadline: Deadline,
    max_pages: int = MAX_PAGES,
    sleep=time.sleep,
) -> dict:
    """Refresh this brand's website sources. Returns stats for the log/UI."""
    stats = {
        "pages": 0,
        "products": 0,
        "changed": 0,
        "unchanged": 0,
        "skipped": 0,
        "errors": 0,
        "offDomain": 0,
        "robotsBlocked": 0,
        "shopify": False,
        "pruned": 0,
        "complete": False,
        "message": "",
    }
    site = normalize_site_url(getattr(tenant, "brand_website_url", ""))
    if not site:
        stats["message"] = "No brand website set."
        return stats
    base_host = site_host(site)
    rp, why = _robots(fetcher, site, base_host)
    if rp is None:
        stats["message"] = why
        return stats
    delay = min(float(rp.crawl_delay(ROBOTS_AGENT) or 0), MAX_CRAWL_DELAY)

    def can(url: str) -> bool:
        try:
            return rp.can_fetch(ROBOTS_AGENT, url)
        except Exception:  # noqa: BLE001
            return False

    seen_refs: set[str] = set()
    shopify = _shopify(fetcher, tenant.id, site, base_host, can, seen_refs, stats)

    pool: dict[str, int] = {}

    def offer(url: str) -> None:
        if not on_site(url, base_host):
            stats["offDomain"] += 1
            return
        ref = canonical(url)
        if ref in pool or ref in visited:
            return
        pool[ref] = score_url(ref, shopify=shopify)

    visited: set[str] = set()
    offer(site + "/")
    for url in _sitemap_urls(fetcher, rp, site, base_host, deadline, can):
        offer(url)

    homepage_ok = False
    while stats["pages"] < max_pages:
        if deadline.expired:
            stats["message"] = "Stopped at the time limit."
            break
        candidates = [(s, u) for u, s in pool.items() if s >= 0]
        if not candidates:
            break
        _score, ref = max(candidates, key=lambda t: (t[0], -len(t[1]), t[1]))
        pool.pop(ref)
        visited.add(ref)
        if not can(ref):
            stats["robotsBlocked"] += 1
            continue
        prior = existing(tenant.id, ref)
        if prior is not None and prior.excluded:
            seen_refs.add(ref)
            stats["skipped"] += 1
            continue
        if delay and stats["pages"]:
            sleep(delay)
        is_pdf = ref.lower().endswith(".pdf")
        res = fetcher.get(
            ref,
            max_bytes=DOC_MAX_BYTES if is_pdf else HTML_MAX_BYTES,
            allow_url=lambda u: on_site(u, base_host),
        )
        stats["pages"] += 1
        if res.error == "off-domain":
            stats["offDomain"] += 1
            continue
        if not res.ok:
            stats["errors"] += 1
            if prior is not None:
                seen_refs.add(ref)
                save_source(
                    tenant_id=tenant.id,
                    kind=prior.kind,
                    ref=ref,
                    url=ref,
                    error=res.error,
                )
            continue
        seen_refs.add(ref)
        if ref == canonical(site + "/"):
            homepage_ok = True
        raw_hash = sha256(res.data)
        unchanged = (
            prior is not None
            and prior.raw_hash == raw_hash
            and prior.status == KnowledgeSource.STATUS_OK
        )
        ctype = res.content_type.lower()
        links: list[str] = []
        if is_pdf or "application/pdf" in ctype or res.data[:5] == b"%PDF-":
            if res.truncated:
                stats["errors"] += 1
                continue
            title = ref.rsplit("/", 1)[-1]
            text = "" if unchanged else pdf_to_text(res.data)
        elif "html" in ctype or not ctype:
            # Parsed even when unchanged: its links shape the rest of the crawl.
            parsed = html_to_text(res.data.decode("utf-8", "replace"))
            title, text, links = parsed["title"], parsed["text"], parsed["links"]
        else:
            stats["skipped"] += 1
            continue
        if unchanged:
            touch(prior)
            stats["unchanged"] += 1
        else:
            _row, changed = save_source(
                tenant_id=tenant.id,
                kind=KnowledgeSource.KIND_WEBSITE_PAGE,
                ref=ref,
                url=ref,
                title=title or display_url(ref),
                text=text,
                raw_hash=raw_hash,
            )
            stats["changed" if changed else "unchanged"] += 1
        for href in links[:400]:
            href = (href or "").strip()
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            offer(urljoin(res.url or ref, href))

    ran_out_of_time = stats["message"] == "Stopped at the time limit."
    stats["complete"] = (homepage_ok or shopify) and not ran_out_of_time
    if stats["complete"]:
        # Pages the site no longer has (or that fell out of the top pages)
        # stop answering questions. Excluded rows stay so the exclusion holds.
        stale = KnowledgeSource.objects.filter(
            tenant_id=tenant.id, kind__in=KnowledgeSource.WEBSITE_KINDS, excluded=False
        ).exclude(ref__in=seen_refs)
        stats["pruned"] = stale.count()
        stale.delete()
    elif not stats["message"]:
        stats["message"] = "Couldn't load the homepage."
    return stats
