"""Unit tests for the SSRF guard in raasoa.connectors.net (F-011)."""
from __future__ import annotations

import pytest

from raasoa.connectors.net import UnsafeConnectorUrlError, validate_outbound_url


@pytest.mark.parametrize(
    "url",
    [
        "https://169.254.169.254",  # cloud metadata endpoint
        "https://127.0.0.1",  # loopback literal
        "https://localhost",  # loopback via DNS
        "https://10.0.0.5",  # RFC 1918 private
        "https://172.16.0.5",  # RFC 1918 private
        "https://192.168.1.5",  # RFC 1918 private
        "https://0.0.0.0",  # unspecified
        "https://[::1]",  # IPv6 loopback
    ],
)
def test_blocks_internal_and_private_hosts(url: str) -> None:
    with pytest.raises(UnsafeConnectorUrlError):
        validate_outbound_url(url)


def test_blocks_non_https_scheme() -> None:
    with pytest.raises(UnsafeConnectorUrlError):
        validate_outbound_url("http://example.atlassian.net")


def test_blocks_empty_url() -> None:
    with pytest.raises(UnsafeConnectorUrlError):
        validate_outbound_url("")


def test_blocks_url_without_hostname() -> None:
    with pytest.raises(UnsafeConnectorUrlError):
        validate_outbound_url("https:///no-host-path")


def test_allows_public_https_host() -> None:
    # A plausible Jira Cloud hostname — resolves to a real public IP.
    validate_outbound_url("https://example.atlassian.net")


def test_error_message_names_the_field() -> None:
    with pytest.raises(UnsafeConnectorUrlError, match="config.base_url"):
        validate_outbound_url("https://127.0.0.1", field_name="config.base_url")
