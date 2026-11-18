"""Readable text out of HTML pages, PDFs and Word documents.

Pure functions, stdlib + pypdf only (python-docx isn't a dependency: a .docx
is a zip whose ``word/document.xml`` holds the text, which is all we need).
Every extractor caps its output and never raises — a file it can't read
yields "" and the caller records the source as empty.
"""

from __future__ import annotations

import html
import io
import json
import logging
import re
import zipfile
from html.parser import HTMLParser
from xml.etree import ElementTree

logger = logging.getLogger(__name__)

PAGE_TEXT_MAX_CHARS = 20000
DOC_TEXT_MAX_CHARS = 60000
PDF_MAX_PAGES = 80

# Content we never want: chrome, scripts, forms, and anything hidden.
_SKIP_TAGS = {
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "nav",
    "footer",
    "header",
    "form",
    "iframe",
    "button",
    "select",
    "option",
    "dialog",
    "head",
}
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_BLOCK_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "main",
    "aside",
    "li",
    "ul",
    "ol",
    "dl",
    "dt",
    "dd",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "tr",
    "table",
    "blockquote",
    "pre",
    "figure",
    "figcaption",
    "summary",
    "details",
    "br",
    "hr",
}
_SKIP_ROLES = {
    "navigation",
    "banner",
    "contentinfo",
    "dialog",
    "search",
    "menu",
    "menubar",
}
_CHROME_CLASS = re.compile(
    r"(^|[\s_-])(cookie|consent|newsletter|announcement-bar|site-header|site-footer|"
    r"drawer|cart|modal|popup|breadcrumb)s?($|[\s_-])",
    re.I,
)


def clean_text(text: str) -> str:
    """Collapse runs of spaces, trim lines, drop empty and repeated lines."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in (text or "").replace("\r", "\n").split("\n"):
        line = re.sub(r"[ \t ​]+", " ", raw).strip()
        if not line:
            continue
        key = line.lower()
        # Repeated short lines are menus/badges that slipped through; a long
        # repeated line (a disclaimer per product) is still said once.
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    return "\n".join(out)


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    nl = cut.rfind("\n")
    return (cut[:nl] if nl > limit * 0.8 else cut).rstrip()


class _HtmlText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self.og_title = ""
        self.description = ""
        self.links: list[str] = []
        self.json_ld: list[str] = []
        self._skip_stack: list[str] = []
        self._in_title = False
        self._in_ld = False
        self._ld_buf: list[str] = []

    # A skipped element's children are skipped until its end tag.
    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        if tag == "meta":
            name = (a.get("name") or a.get("property") or "").lower()
            if name in ("description", "og:description") and not self.description:
                self.description = a.get("content", "")
            if name == "og:title" and not self.og_title:
                self.og_title = a.get("content", "")
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "script" and "ld+json" in a.get("type", "").lower():
            self._in_ld = True
            self._ld_buf = []
        if tag in _VOID_TAGS:
            if tag in ("br", "hr") and not self._skip_stack:
                self.parts.append("\n")
            return
        if self._skip_stack:
            self._skip_stack.append(tag)
            return
        hidden = (
            "hidden" in a
            or a.get("aria-hidden", "").lower() == "true"
            or a.get("role", "").lower() in _SKIP_ROLES
            or bool(_CHROME_CLASS.search(a.get("class", "") + " " + a.get("id", "")))
            or re.search(r"display\s*:\s*none", a.get("style", ""), re.I) is not None
        )
        if tag in _SKIP_TAGS or hidden:
            self._skip_stack.append(tag)
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "script" and self._in_ld:
            self._in_ld = False
            self.json_ld.append("".join(self._ld_buf))
        if self._skip_stack:
            # Pop back to the matching open tag; tolerate sloppy HTML.
            if tag in self._skip_stack:
                while self._skip_stack:
                    if self._skip_stack.pop() == tag:
                        break
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self._in_ld:
            self._ld_buf.append(data)
            return
        if self._skip_stack:
            return
        self.parts.append(data)


def _json_ld_products(blobs: list[str]) -> list[str]:
    """Product facts a page declares in schema.org JSON-LD (name, description)."""
    lines: list[str] = []

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
            return
        if not isinstance(node, dict):
            return
        kind = node.get("@type")
        kinds = kind if isinstance(kind, list) else [kind]
        if "Product" in kinds:
            name = str(node.get("name") or "").strip()
            desc = re.sub(r"<[^>]+>", " ", str(node.get("description") or "")).strip()
            if name or desc:
                lines.append(f"{name}: {html.unescape(desc)}".strip(": "))
        for key in ("@graph", "mainEntity", "itemListElement", "item"):
            if key in node:
                walk(node[key])

    for blob in blobs[:10]:
        try:
            walk(json.loads(blob))
        except (ValueError, TypeError, RecursionError):
            continue
    return lines


def html_to_text(raw: str) -> dict:
    """``{"title", "text", "links"}`` from an HTML document.

    Navigation, headers/footers, scripts, forms, cookie banners and hidden
    elements are dropped. JSON-LD Product data is appended because many shop
    themes render product facts with JavaScript but still declare them there.
    """
    parser = _HtmlText()
    try:
        parser.feed(raw or "")
        parser.close()
    except Exception:  # noqa: BLE001 — malformed HTML yields what was parsed
        logger.debug("html parse error", exc_info=True)
    title = clean_text(parser.og_title or parser.title).split("\n")[0][:300]
    body = clean_text("".join(parser.parts))
    extra = []
    desc = clean_text(parser.description)
    if desc and desc.lower() not in body.lower():
        extra.append(desc)
    ld = _json_ld_products(parser.json_ld)
    for line in ld:
        if line.lower() not in body.lower():
            extra.append(line)
    text = clean_text("\n".join([*extra, body]))
    return {
        "title": title,
        "text": clip(text, PAGE_TEXT_MAX_CHARS),
        "links": parser.links,
    }


def pdf_to_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:  # noqa: BLE001
                return ""
        pages = []
        total = 0
        for page in reader.pages[:PDF_MAX_PAGES]:
            chunk = page.extract_text() or ""
            pages.append(chunk)
            total += len(chunk)
            if total > DOC_TEXT_MAX_CHARS * 2:
                break
        return clip(clean_text("\n".join(pages)), DOC_TEXT_MAX_CHARS)
    except Exception:  # noqa: BLE001 — a broken PDF is "no readable text"
        logger.info("pdf extract failed", exc_info=True)
        return ""


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_to_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            info = zf.getinfo("word/document.xml")
            if info.file_size > 20 * 1024 * 1024:
                return ""
            xml = zf.read(info)
        # Word never writes a DTD; refusing one rules out entity expansion
        # tricks without needing defusedxml.
        if b"<!DOCTYPE" in xml[:4096].upper() or b"<!ENTITY" in xml.upper():
            return ""
        root = ElementTree.fromstring(xml)
        paras = []
        for p in root.iter(f"{_W}p"):
            texts = [t.text or "" for t in p.iter(f"{_W}t")]
            line = "".join(texts).strip()
            if line:
                paras.append(line)
        return clip(clean_text("\n".join(paras)), DOC_TEXT_MAX_CHARS)
    except Exception:  # noqa: BLE001
        logger.info("docx extract failed", exc_info=True)
        return ""


def sniff_kind(data: bytes, content_type: str = "", url: str = "") -> str:
    """ "pdf" | "docx" | "html" | "text" | "" (unsupported, e.g. images)."""
    ct = (content_type or "").split(";")[0].strip().lower()
    path = (url or "").split("?")[0].split("#")[0].lower()
    if data[:5] == b"%PDF-" or ct == "application/pdf" or path.endswith(".pdf"):
        return "pdf"
    if (
        ct == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        or path.endswith(".docx")
    ) and data[:2] == b"PK":
        return "docx"
    if ct.startswith("image/") or ct.startswith("video/") or ct.startswith("audio/"):
        return ""
    if ct in ("text/html", "application/xhtml+xml") or (
        not ct and data.lstrip()[:15].lower().startswith((b"<!doctype", b"<html"))
    ):
        return "html"
    if ct in ("text/plain", "text/markdown"):
        return "text"
    return ""


def bytes_to_text(data: bytes, content_type: str = "", url: str = "") -> dict:
    """``{"kind", "title", "text"}`` for any supported document."""
    kind = sniff_kind(data, content_type, url)
    if kind == "pdf":
        return {"kind": kind, "title": "", "text": pdf_to_text(data)}
    if kind == "docx":
        return {"kind": kind, "title": "", "text": docx_to_text(data)}
    if kind == "html":
        parsed = html_to_text(data.decode("utf-8", "replace"))
        return {"kind": kind, "title": parsed["title"], "text": parsed["text"]}
    if kind == "text":
        text = clean_text(data.decode("utf-8", "replace"))
        return {"kind": kind, "title": "", "text": clip(text, DOC_TEXT_MAX_CHARS)}
    return {"kind": "", "title": "", "text": ""}
