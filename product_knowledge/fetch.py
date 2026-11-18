"""A small, careful HTTP fetcher for brand websites and document links.

* Polite: one identifying User-Agent, short timeouts, no retries.
* Bounded: every body is read in a stream and cut off at a byte cap.
* Safe: only http(s), never a private / loopback / link-local / metadata
  address (checked on every redirect hop), and the caller decides which
  hosts a redirect may land on.

Tests build a :class:`Fetcher` over ``httpx.MockTransport`` with
``resolve_dns=False``; production code gets one from :func:`make_fetcher`.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urljoin, urlsplit

import httpx

logger = logging.getLogger(__name__)

USER_AGENT = "SparkBot/1.0 (+igniteproductions.co)"
TIMEOUT_SECONDS = 10.0
MAX_REDIRECTS = 4
HTML_MAX_BYTES = 2 * 1024 * 1024
DOC_MAX_BYTES = 15 * 1024 * 1024


@dataclass
class FetchResult:
    url: str
    status: int = 0
    content_type: str = ""
    data: bytes = b""
    error: str = ""
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return not self.error and 200 <= self.status < 300


def _ip_is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_global and not addr.is_multicast


class Fetcher:
    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        resolve_dns: bool = True,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        self.resolve_dns = resolve_dns
        self._client = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 6.0)),
            follow_redirects=False,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/json,"
                "application/pdf,application/xml;q=0.9,*/*;q=0.5",
                "Accept-Language": "en-US,en;q=0.8",
            },
        )
        self._host_ok: dict[str, bool] = {}
        self.requests = 0

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def host_is_safe(self, host: str) -> bool:
        """False for localhost, private networks, link-local (cloud metadata)
        and anything that only resolves there."""
        host = (host or "").strip("[]").lower()
        if (
            not host
            or host == "localhost"
            or host.endswith((".localhost", ".internal", ".local"))
        ):
            return False
        if host in self._host_ok:
            return self._host_ok[host]
        try:
            ipaddress.ip_address(host)
            ok = _ip_is_public(host)
        except ValueError:
            if not self.resolve_dns:
                ok = True
            else:
                try:
                    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
                    ips = {info[4][0] for info in infos}
                    ok = bool(ips) and all(_ip_is_public(ip) for ip in ips)
                except OSError:
                    ok = False
        self._host_ok[host] = ok
        return ok

    def get(
        self,
        url: str,
        *,
        max_bytes: int = HTML_MAX_BYTES,
        allow_url: Callable[[str], bool] | None = None,
    ) -> FetchResult:
        """GET ``url``, following up to MAX_REDIRECTS redirects that pass
        ``allow_url`` and the address check. Never raises."""
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            parts = urlsplit(current)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                return FetchResult(url=current, error="unsupported-url")
            if allow_url is not None and not allow_url(current):
                return FetchResult(url=current, error="off-domain")
            if not self.host_is_safe(parts.hostname):
                return FetchResult(url=current, error="blocked-host")
            self.requests += 1
            try:
                with self._client.stream("GET", current) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location", "")
                        if not location:
                            return FetchResult(
                                url=current,
                                status=resp.status_code,
                                error="bad-redirect",
                            )
                        current = urljoin(current, location)
                        continue
                    result = FetchResult(
                        url=str(resp.url) if resp.url else current,
                        status=resp.status_code,
                        content_type=resp.headers.get("content-type", ""),
                    )
                    if resp.status_code >= 400:
                        result.error = f"http-{resp.status_code}"
                        return result
                    declared = resp.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > max_bytes:
                        result.error = "too-large"
                        return result
                    buf = bytearray()
                    for chunk in resp.iter_bytes():
                        buf.extend(chunk)
                        if len(buf) > max_bytes:
                            result.truncated = True
                            del buf[max_bytes:]
                            break
                    result.data = bytes(buf)
                    return result
            except httpx.TimeoutException:
                return FetchResult(url=current, error="timeout")
            except httpx.HTTPError as exc:
                logger.info("fetch failed url=%s: %s", current, type(exc).__name__)
                return FetchResult(url=current, error="fetch-failed")
            except Exception:  # noqa: BLE001
                logger.exception("fetch crashed url=%s", current)
                return FetchResult(url=current, error="fetch-failed")
        return FetchResult(url=current, error="too-many-redirects")


def make_fetcher() -> Fetcher:
    """The production fetcher. Tests monkeypatch this."""
    return Fetcher()


class Deadline:
    """Wall-clock budget for one refresh."""

    def __init__(self, seconds: float) -> None:
        self.until = time.monotonic() + seconds

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.until
