"""LAN addresses and the Avahi interface allow/deny lists.

docker0, veth, bridges, and tailscale0 are not LAN interfaces. Avahi is
limited to real LAN NICs. Tailscale is reached because MediaMTX and camctl
listen on every interface, not because Avahi advertises on tailscale0.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess

_LINK = re.compile(r"^\d+:\s+([^:\s@]+)")
_ADDR = re.compile(r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)")
_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,32}$")
_VIRTUAL = re.compile(
    r"^(?:"
    r"lo\d*"
    r"|docker\d*"
    r"|podman\d*"
    r"|br-[0-9A-Za-z]+"
    r"|veth[0-9A-Za-z]+"
    r"|virbr\d*"
    r"|vnet\d*"
    r"|tun\d*"
    r"|tap\d*"
    r"|wg\d*"
    r"|zt[0-9A-Za-z]*"
    r"|dummy\d*"
    r"|ifb\d*"
    r"|gre\d*"
    r"|gretap\d*"
    r"|erspan\d*"
    r"|ip6tnl\d*"
    r"|ip6gre\d*"
    r"|sit\d*"
    r"|tailscale\d*"
    r"|cni[0-9A-Za-z]*"
    r"|flannel[0-9A-Za-z.]*"
    r"|kube-[0-9A-Za-z-]+"
    r")$"
)


def is_tailscale_iface(name: str) -> bool:
    return name.lower().startswith("tailscale")


def is_virtual_iface(name: str, sysfs_root: str | None = "/sys/class/net") -> bool:
    """True for loopback, Tailscale, docker, and other non-LAN interfaces."""
    if is_tailscale_iface(name) or _VIRTUAL.match(name):
        return True
    if not sysfs_root:
        return False
    base = os.path.join(sysfs_root, name)
    # A missing directory means we have no sysfs opinion. Name rules already ran.
    if os.path.isdir(base) and not os.path.exists(os.path.join(base, "device")):
        return True
    return False


def parse_ip_link(text: str) -> list[str]:
    names = []
    for line in text.splitlines():
        match = _LINK.match(line.strip())
        if match and _NAME.match(match.group(1)):
            names.append(match.group(1))
    return names


def parse_ip_addr(text: str) -> list[tuple[str, str]]:
    found = []
    for line in text.splitlines():
        match = _ADDR.match(line.strip())
        if not match:
            continue
        iface, address = match.group(1), match.group(2)
        if _NAME.match(iface):
            found.append((iface, address))
    return found


def _run(args: list[str]) -> str:
    try:
        completed = subprocess.run(args, check=False, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout or ""


def discover(
    *,
    link_text: str | None = None,
    addr_text: str | None = None,
    sysfs_root: str | None = "/sys/class/net",
) -> dict:
    if link_text is None:
        link_text = _run(["ip", "-o", "link", "show"])
    if addr_text is None:
        addr_text = _run(["ip", "-4", "-o", "addr", "show"])
    names = parse_ip_link(link_text)
    # ip(8) is missing in some dry-runs. Interface names still come from sysfs,
    # which is enough to keep docker0 and tailscale0 out of Avahi.
    if not names and sysfs_root and os.path.isdir(sysfs_root):
        names = sorted(name for name in os.listdir(sysfs_root) if _NAME.match(name))
    addresses = parse_ip_addr(addr_text)
    for iface, _address in addresses:
        if iface not in names:
            names.append(iface)
    deny = {"tailscale0"}
    allow = []
    for name in names:
        if name == "lo" or re.fullmatch(r"lo\d*", name):
            continue
        if is_tailscale_iface(name) or is_virtual_iface(name, sysfs_root):
            deny.add(name)
        else:
            allow.append(name)
    lan = []
    seen = set()
    for iface, address in addresses:
        if address.startswith("127.") or iface == "lo":
            continue
        if is_tailscale_iface(iface) or is_virtual_iface(iface, sysfs_root):
            continue
        if address in seen:
            continue
        seen.add(address)
        lan.append({"iface": iface, "address": address})
    return {
        "detected": bool(names),
        "interfaces": names,
        "deny_interfaces": sorted(deny),
        "allow_interfaces": sorted(set(allow)),
        "lan_ipv4": lan,
        "sysfs_root": sysfs_root or "",
    }


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip() and _NAME.match(part.strip())]


def interface_policy(discovered: dict, existing_deny: str | None, existing_allow: str | None) -> tuple[str, str | None]:
    """Merge discovery with an existing Avahi config.

    tailscale0 is always denied. Detected LAN names are not removed from an
    admin deny list. Tailscale and other virtual names are never allowed.
    """
    sysfs = discovered.get("sysfs_root") or None
    deny = set(discovered.get("deny_interfaces") or [])
    deny.add("tailscale0")
    deny.update(_split_csv(existing_deny))
    allow = set(discovered.get("allow_interfaces") or [])
    for name in _split_csv(existing_allow):
        if is_tailscale_iface(name) or is_virtual_iface(name, sysfs):
            continue
        allow.add(name)
    allow -= deny
    allow_csv = ",".join(sorted(allow)) if allow else None
    if not discovered.get("detected"):
        # Do not invent or erase an allow list when link discovery failed.
        allow_csv = existing_allow if existing_allow else None
    return ",".join(sorted(deny)), allow_csv


def format_summary(info: dict | None = None) -> str:
    info = info or discover()
    lines = ["LAN IPv4 (docker, other virtual interfaces, and tailscale0 omitted):"]
    if not info["lan_ipv4"]:
        lines.append("  (none found)")
    for item in info["lan_ipv4"]:
        lines.append(f"  {item['address']} ({item['iface']})")
    lines.append("Avahi allow-interfaces: " + (",".join(info["allow_interfaces"]) or "(unset)"))
    lines.append("Avahi deny-interfaces: " + ",".join(info["deny_interfaces"]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show LAN addresses and Avahi interface policy")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--lan-ipv4", action="store_true", help="print LAN IPv4 addresses, one per line")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args(argv)
    info = discover()
    if args.json:
        print(json.dumps({key: info[key] for key in ("detected", "interfaces", "deny_interfaces", "allow_interfaces", "lan_ipv4")}))
    elif args.lan_ipv4:
        for item in info["lan_ipv4"]:
            print(item["address"])
    else:
        print(format_summary(info))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
