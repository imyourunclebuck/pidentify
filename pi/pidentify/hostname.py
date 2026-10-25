"""DNS-safe mDNS hostnames without touching the system hostname.

Raspberry Pi OS hostnames for these nodes look like ``pi5_cam_1_myrtle``.
Underscores are legal in a Unix hostname (and in ``/etc/hosts``, which sudo
resolves) but they are not legal in a DNS label, and Avahi will not publish
``pi5_cam_1_myrtle.local``. Replacing ``_`` with ``-`` yields
``pi5-cam-1-myrtle``, which Avahi can publish as ``pi5-cam-1-myrtle.local``.

That string is written only to Avahi's ``host-name=`` setting. ``/etc/hostname``
and ``/etc/hosts`` stay as the underscore name so ``hostname`` and sudo keep
working.
"""

from __future__ import annotations

import argparse
import json
import re
import socket

_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def dns_safe_hostname(hostname: str) -> str:
    """Return a single DNS label derived from a system hostname."""
    cleaned = hostname.strip().lower().replace("_", "-")
    cleaned = re.sub(r"[^a-z0-9-]", "-", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    if len(cleaned) > 63:
        cleaned = cleaned[:63].rstrip("-")
    if not cleaned or not _LABEL.match(cleaned):
        raise ValueError(f"cannot derive a DNS hostname from {hostname!r}")
    return cleaned


def mdns_fqdn(mdns_hostname: str, domain: str = "local") -> str:
    domain = domain.strip().strip(".") or "local"
    return f"{mdns_hostname}.{domain}"


def read_hostname_file(path: str) -> str:
    """Read the first hostname from an /etc/hostname-style file."""
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.split("#", 1)[0].strip()
            if line:
                return line.split()[0]
    return ""


def resolve_system_hostname(explicit: str | None = None, hostname_file: str | None = None) -> str:
    if explicit and explicit.strip():
        return explicit.strip().split()[0]
    if hostname_file:
        try:
            found = read_hostname_file(hostname_file)
        except OSError:
            found = ""
        if found:
            return found
    detected = socket.gethostname().split(".")[0].strip()
    return detected or "pidentify"


def identity(hostname: str, domain: str = "local", *, name: str | None = None) -> dict[str, str]:
    """Build the names this node advertises.

    ``hostname`` is the system hostname and is not rewritten. ``name`` is the
    operator-chosen node name (``--name``). When it is set, Avahi's host-name,
    the mDNS service name, the TXT ``node`` value, and the default push prefix
    all use its DNS-safe form. When it is omitted, mDNS is still the DNS-safe
    form of the system hostname, and TXT ``node`` stays the system hostname.
    """
    system = hostname.strip()
    chosen = (name or "").strip()
    if chosen:
        mdns = dns_safe_hostname(chosen)
        node = mdns
        stored_name = mdns
    else:
        mdns = dns_safe_hostname(system)
        node = system
        stored_name = ""
    return {
        "hostname": system,
        "system_hostname": system,
        "name": stored_name,
        "node": node,
        "mdns_hostname": mdns,
        "mdns_fqdn": mdns_fqdn(mdns, domain),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show the system hostname and the Avahi mDNS name")
    parser.add_argument("--hostname", default=None, help="override the system hostname")
    parser.add_argument("--hostname-file", default="/etc/hostname")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    info = identity(resolve_system_hostname(args.hostname, args.hostname_file))
    if args.json:
        print(json.dumps(info))
    else:
        print(f"hostname: {info['hostname']}")
        print(f"mdns: {info['mdns_fqdn']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
