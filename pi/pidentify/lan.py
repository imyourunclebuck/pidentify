"""Clients camctl will answer, and the same ranges MediaMTX allows to read.

The list is explicit. Python's ``is_private`` flag is not used: it has grown
documentation prefixes, and it does not include Tailscale's CGNAT range on
Bookworm's Python 3.11. Accepted: loopback, RFC1918, IPv4 link-local,
Tailscale 100.64.0.0/10, IPv6 unique-local (which covers Tailscale IPv6),
and IPv6 link-local.
"""

from __future__ import annotations

import ipaddress

_LAN_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def is_lan_address(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value.strip())
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if ip.is_loopback:
        return True
    return any(ip in network for network in _LAN_NETWORKS)
