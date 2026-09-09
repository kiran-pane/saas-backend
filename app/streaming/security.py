"""SSRF guard for any user-supplied stream source URL (RTSP/RTMP/HTTP
CCTV feeds, remote video URLs for VOD import). A tenant registering a
"camera" pointing at http://169.254.169.254/ (cloud metadata endpoint) or
an internal admin panel is a real, common attack vector for any feature
that makes a server fetch a user-supplied URL — this must be validated
BEFORE ffmpeg (or anything else) ever connects to it, and re-validated
immediately before each connection attempt since DNS can change between
registration and use (DNS rebinding).
"""
import ipaddress
import socket
from urllib.parse import urlparse

ALLOWED_SCHEMES = {"rtsp", "rtmp", "http", "https"}

# Private/reserved ranges no tenant-supplied source URL may resolve to,
# regardless of scheme. This blocks cloud metadata endpoints (169.254.0.0/16
# covers AWS/GCP/Azure's 169.254.169.254), loopback, and RFC1918 space.
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / cloud metadata
    ipaddress.ip_network("127.0.0.0/8"),       # loopback
    ipaddress.ip_network("10.0.0.0/8"),        # RFC1918
    ipaddress.ip_network("172.16.0.0/12"),     # RFC1918
    ipaddress.ip_network("192.168.0.0/16"),    # RFC1918
    ipaddress.ip_network("::1/128"),           # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
]


class UnsafeStreamSourceError(Exception):
    pass


def _is_blocked_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparseable — fail closed
    return any(ip in net for net in _BLOCKED_NETWORKS)


def validate_stream_source_url(url: str, allow_private_networks: bool = False) -> None:
    """Raises UnsafeStreamSourceError if the URL's scheme or resolved
    address is disallowed. allow_private_networks exists ONLY for a
    genuine on-prem CCTV deployment where the streaming-worker itself
    runs inside the customer's private network and cameras legitimately
    live on RFC1918 addresses — must be an explicit, tenant-level opt-in
    (never a global default), since enabling it removes the SSRF
    protection entirely for that tenant."""
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise UnsafeStreamSourceError(f"Unsupported scheme: {parsed.scheme!r}")

    hostname = parsed.hostname
    if not hostname:
        raise UnsafeStreamSourceError("URL has no resolvable hostname")

    if allow_private_networks:
        return

    try:
        # Resolve ALL addresses the hostname maps to (DNS rebinding can
        # return different results on subsequent lookups, and a hostname
        # can have multiple A/AAAA records) — block if ANY resolves privately.
        addr_infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as e:
        raise UnsafeStreamSourceError(f"Could not resolve hostname: {hostname}") from e

    for family, _, _, _, sockaddr in addr_infos:
        ip_str = sockaddr[0]
        if _is_blocked_ip(ip_str):
            raise UnsafeStreamSourceError(
                f"Stream source resolves to a disallowed address ({ip_str}) — "
                "internal/private network addresses are not permitted"
            )
