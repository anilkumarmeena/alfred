"""Which web addresses JARVIS may open or fetch (the browser, skill downloads): only http and https, never one with a
login in it, and never this computer or its local network, checked by name and again by every address it resolves to.
"""

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlsplit

SCHEMES = ("http", "https")
# Names that only resolve on this computer or its LAN
LOCAL_NAMES = ("localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", "broadcasthost")
LOCAL_SUFFIXES = (".localhost", ".local", ".lan", ".home", ".internal", ".intranet", ".corp", ".home.arpa",
                  ".ts.net", ".tailscale.net")


class Refused(Exception):
    """Address refused; the message says why."""


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        ip = ipaddress.ip_address(host.strip("[]").split("%")[0])
    except ValueError:
        return None
    return ip.ipv4_mapped or ip if isinstance(ip, ipaddress.IPv6Address) else ip


def local_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Not a public internet address: loopback, private, link-local (cloud metadata), CGNAT/Tailscale, multicast,
    reserved."""
    return not ip.is_global or ip.is_multicast


def local_host(host: str) -> bool:
    """Whether a host is local by name or literal address. Numeric forms browsers accept (2130706433, 127.1) count
    as addresses."""
    host = host.lower().rstrip(".")
    if not host:
        return True
    if (ip := _ip(host)) is not None:
        return local_address(ip)
    if re.fullmatch(r"(0x[0-9a-f]+|\d+)(\.(0x[0-9a-f]+|\d+)){0,3}", host):
        try:
            return local_address(ipaddress.ip_address(socket.inet_aton(host)))
        except OSError:
            return True
    return "." not in host or host in LOCAL_NAMES or host.endswith(LOCAL_SUFFIXES)


def check_url(url: str) -> str:
    """Normalised URL (https:// added if bare). Raises Refused for non-web or local addresses."""
    url = url.strip()
    if not url:
        raise Refused("No address given.")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url) or re.match(r"^[^/:]+:\d+(/|$)", url):
        url = "https://" + url  # example.com, example.com:8080/x
    parts = urlsplit(url)
    if parts.scheme.lower() not in SCHEMES:
        raise Refused(f"Only web pages (http and https) can be opened, not {parts.scheme}: addresses.")
    if parts.username or parts.password:
        raise Refused("Addresses with a login in them aren't opened.")
    if local_host(parts.hostname or ""):
        raise Refused("Addresses on this computer or its local network aren't opened.")
    return url


async def address(host: str, port: int) -> str:
    """IP to connect to for `host`. Raises Refused if the host or any resolved address is local, OSError if it
    doesn't resolve."""
    if local_host(host):
        raise Refused(f"{host} is on this computer or its local network.")
    if (ip := _ip(host)) is not None:
        return str(ip)
    found = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    ips = [_ip(str(info[4][0])) for info in found]
    if not ips or any(ip is None or local_address(ip) for ip in ips):
        raise Refused(f"{host} looks up to an address on this computer or its local network.")
    return str(ips[0])


# ── Guard proxy ─────────────────────────────────────────────────
