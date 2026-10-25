"""Read this node's Tailscale IPv4 and MagicDNS name.

install.sh does not install or configure Tailscale. When the tailscale CLI
is present, the summary and pidentify-status print `tailscale ip -4` and the
MagicDNS name from `tailscale status --json` (Self.DNSName).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess


def parse_ip4(text: str) -> list[str]:
    found = []
    for line in text.splitlines():
        line = line.strip()
        parts = line.split(".")
        if len(parts) != 4:
            continue
        if all(part.isdigit() and 0 <= int(part) <= 255 for part in parts):
            found.append(line)
    return found


def parse_status(document: dict) -> dict:
    self_node = document.get("Self") if isinstance(document, dict) else None
    if not isinstance(self_node, dict):
        self_node = {}
    dns = str(self_node.get("DNSName") or "").strip().rstrip(".")
    ips = []
    for value in self_node.get("TailscaleIPs") or []:
        text = str(value).strip()
        if ":" not in text and parse_ip4(text):
            ips.append(text)
    return {"magic_dns": dns, "ipv4": ips, "online": bool(self_node.get("Online"))}


def _run(args: list[str], timeout: float) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(args, check=False, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def query(binary: str | None = None) -> dict:
    binary = binary if binary is not None else shutil.which("tailscale")
    result = {"available": bool(binary), "ipv4": [], "magic_dns": "", "online": False, "error": ""}
    if not binary:
        result["error"] = "tailscale is not installed"
        return result
    code, stdout, stderr = _run([binary, "ip", "-4"], 5)
    result["ipv4"] = parse_ip4(stdout)
    if code != 0 and not result["ipv4"]:
        result["error"] = (stderr or stdout or "tailscale ip -4 failed").strip().splitlines()[-1]
    status_code, status_out, status_err = _run([binary, "status", "--json"], 8)
    if status_code == 0 and status_out.strip():
        try:
            parsed = parse_status(json.loads(status_out))
        except json.JSONDecodeError:
            result["error"] = result["error"] or "tailscale status --json was not JSON"
        else:
            result["magic_dns"] = parsed["magic_dns"]
            result["online"] = parsed["online"]
            if not result["ipv4"]:
                result["ipv4"] = parsed["ipv4"]
    elif not result["error"]:
        result["error"] = (status_err or "tailscale status --json failed").strip().splitlines()[-1]
    return result


def format_summary(info: dict | None = None) -> str:
    info = info if info is not None else query()
    if not info.get("available"):
        return "Tailscale: not installed (install.sh does not install it)"
    lines = ["Tailscale:"]
    if info.get("ipv4"):
        for address in info["ipv4"]:
            lines.append(f"  IPv4: {address}")
    else:
        lines.append("  IPv4: (none; tailscale ip -4 returned nothing)")
    if info.get("magic_dns"):
        lines.append(f"  MagicDNS: {info['magic_dns']}")
    else:
        lines.append("  MagicDNS: (none)")
    if info.get("error"):
        lines.append(f"  note: {info['error']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show this node's Tailscale IPv4 and MagicDNS name")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--ipv4", action="store_true")
    parser.add_argument("--magic-dns", action="store_true")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args(argv)
    info = query()
    if args.json:
        print(json.dumps(info))
    elif args.ipv4:
        for address in info["ipv4"]:
            print(address)
    elif args.magic_dns:
        if info["magic_dns"]:
            print(info["magic_dns"])
    else:
        print(format_summary(info))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
