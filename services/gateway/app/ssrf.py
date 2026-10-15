"""SSRF guard for admin-supplied upstream URLs.

Threat: an admin (or a compromised admin account) registers a URL that makes the
gateway call something it should not: the cloud metadata service, the Kubernetes
API, the gateway's own database, another tenant's service...

Policy
- http/https only, no userinfo in the URL.
- Link-local (incl. 169.254.169.254 and fd00:ec2::254), unspecified, multicast and
  reserved ranges are ALWAYS blocked, even for allow-listed hosts.
- Loopback/private (RFC1918, ULA) addresses are blocked unless the hostname matches
  an operator-configured allow pattern (SSRF_ALLOW_HOSTS), e.g. the in-cluster
  WooCommerce MCP Service. Redirects are never followed (see upstream.py).
- Every hostname is resolved and ALL returned addresses must pass.
Checked on registration and again before every connection. Residual risk: DNS
rebinding between check and connect (documented in the threat model).
"""
from __future__ import annotations

import asyncio
import fnmatch
import ipaddress
import socket
from urllib.parse import urlsplit

from .config import Settings

METADATA = {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254")}


class SSRFError(ValueError):
    pass


def _normalize(ip):
    """Judge IPv4-mapped/6to4-style IPv6 (e.g. ::ffff:169.254.169.254) by the IPv4 address it carries,
    independent of how a given Python version classifies mapped addresses."""
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped if mapped is not None else ip


def _always_blocked(ip) -> str | None:
    if ip.is_loopback:
        return None  # loopback is handled with the private ranges below (allow-listable for local dev)
    if ip in METADATA or ip.is_link_local:
        return "link-local / cloud metadata address"
    if ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return "unspecified/multicast/reserved address"
    return None


async def validate_upstream_url(url: str, settings: Settings) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise SSRFError("URL must start with http:// or https://")
    if not parts.hostname:
        raise SSRFError("URL has no host")
    if parts.username or parts.password:
        raise SSRFError("Credentials in the URL are not allowed; use the credential field")
    host = parts.hostname.lower()
    allowed_host = any(fnmatch.fnmatch(host, pat.lower()) for pat in settings.ssrf_allow_hosts)

    try:
        literal = [ipaddress.ip_address(host)]
    except ValueError:
        literal = None
    if literal is None:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except socket.gaierror as e:
            raise SSRFError(f"Cannot resolve host '{host}'") from e
        ips = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos]
    else:
        ips = literal

    for ip in map(_normalize, ips):
        why = _always_blocked(ip)
        if why:
            raise SSRFError(f"Blocked: {host} resolves to a {why}")
        if (ip.is_private or ip.is_loopback) and not (allowed_host or settings.ssrf_allow_private):
            kind = "loopback" if ip.is_loopback else "private/internal"
            raise SSRFError(
                f"Blocked: {host} resolves to a {kind} address. "
                "Ask the operator to add it to SSRF_ALLOW_HOSTS if it is intentional.")
