"""Outbound URL validation for tenant-configured connectors (SSRF guard).

Some connectors (Jira today) take a tenant-supplied base URL and issue
server-side HTTP requests to it. Without validation, any caller who can
create a source can point that URL at cloud metadata endpoints
(169.254.169.254), loopback, or other internal-network hosts and read
the response back through the sync-error message. Validate both at
source-creation time (immediate feedback) and again immediately before
each outbound call (the config could have been created before this
check existed, and DNS can change between calls).
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeConnectorUrlError(ValueError):
    """Raised when a tenant-configured connector URL is not safe to call."""


def validate_outbound_url(url: str, *, field_name: str = "base_url") -> None:
    """Raise ``UnsafeConnectorUrlError`` unless ``url`` is an https URL
    that does not resolve to a private, loopback, link-local, or other
    non-public address."""
    if not url:
        raise UnsafeConnectorUrlError(f"{field_name} is required")

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise UnsafeConnectorUrlError(f"{field_name} must use https")
    if not parsed.hostname:
        raise UnsafeConnectorUrlError(f"{field_name} must include a hostname")

    hostname = parsed.hostname
    try:
        addrs = {
            info[4][0]
            for info in socket.getaddrinfo(hostname, None)
        }
    except OSError as e:
        raise UnsafeConnectorUrlError(
            f"{field_name} hostname could not be resolved: {hostname}"
        ) from e

    for addr in addrs:
        ip = ipaddress.ip_address(addr)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise UnsafeConnectorUrlError(
                f"{field_name} resolves to a non-public address ({addr}); "
                "internal/private hosts are not allowed"
            )
