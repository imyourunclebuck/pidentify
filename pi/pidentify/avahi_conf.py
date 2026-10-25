"""Edit Avahi's host-name and render the pidentify service advertisement.

Only ``host-name=`` inside the ``[server]`` section is changed. The rest of
avahi-daemon.conf (including domain-name) is preserved.
"""

from __future__ import annotations

import re
from xml.sax.saxutils import escape

from pidentify.hostname import dns_safe_hostname

_SECTION = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_HOST = re.compile(r"^(\s*)#?\s*host-name\s*=.*$")
_DOMAIN = re.compile(r"^\s*domain-name\s*=\s*(\S+)\s*$")

DEFAULT_AVAHI_CONF = """[server]
#host-name=foo
#domain-name=local
use-ipv4=yes
use-ipv6=yes
"""


def read_domain(conf_text: str) -> str:
    section = None
    domain = "local"
    for line in conf_text.splitlines():
        section_match = _SECTION.match(line)
        if section_match:
            section = section_match.group(1).strip().lower()
            continue
        if section != "server" or line.lstrip().startswith("#"):
            continue
        domain_match = _DOMAIN.match(line)
        if domain_match:
            domain = domain_match.group(1).strip(".") or "local"
    return domain


def set_avahi_host_name(conf_text: str, host_name: str) -> str:
    """Uncomment or insert host-name under [server]. Does not touch other lines."""
    safe = dns_safe_hostname(host_name)
    lines = conf_text.splitlines()
    section = None
    replaced = False
    out: list[str] = []
    server_at: int | None = None
    for line in lines:
        section_match = _SECTION.match(line)
        if section_match:
            section = section_match.group(1).strip().lower()
            out.append(line)
            if section == "server":
                server_at = len(out) - 1
            continue
        if section == "server" and not replaced and _HOST.match(line):
            indent = _HOST.match(line).group(1)  # type: ignore[union-attr]
            out.append(f"{indent}host-name={safe}")
            replaced = True
            continue
        out.append(line)
    if not replaced:
        if server_at is None:
            out = ["[server]", f"host-name={safe}", *out]
        else:
            out.insert(server_at + 1, f"host-name={safe}")
    trailing = "\n" if conf_text.endswith("\n") or conf_text == "" else ""
    # Preserve a trailing newline when the input had one, and always end
    # generated files with a newline.
    text = "\n".join(out)
    if conf_text.endswith("\n") or not conf_text.endswith("\n"):
        if not text.endswith("\n"):
            text += "\n"
    return text if text.endswith("\n") else text + trailing


def read_server_value(conf_text: str, key: str) -> str | None:
    """Return an uncommented key's value from [server], if present."""
    if not re.fullmatch(r"[A-Za-z0-9-]+", key):
        raise ValueError(key)
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.*?)\s*$")
    section = None
    for line in conf_text.splitlines():
        section_match = _SECTION.match(line)
        if section_match:
            section = section_match.group(1).strip().lower()
            continue
        if section != "server" or line.lstrip().startswith("#"):
            continue
        found = pattern.match(line)
        if found:
            return found.group(1)
    return None


def set_server_assignment(conf_text: str, key: str, value: str) -> str:
    """Set an uncommented key=value inside [server]. Commented examples stay."""
    if not re.fullmatch(r"[A-Za-z0-9-]+", key):
        raise ValueError(key)
    if any(ch in value for ch in "\n\r"):
        raise ValueError("assignment value cannot contain a newline")
    pattern = re.compile(rf"^(\s*){re.escape(key)}\s*=.*$")
    lines = conf_text.splitlines()
    section = None
    replaced = False
    out: list[str] = []
    server_at: int | None = None
    for line in lines:
        section_match = _SECTION.match(line)
        if section_match:
            section = section_match.group(1).strip().lower()
            out.append(line)
            if section == "server":
                server_at = len(out) - 1
            continue
        if section == "server" and not replaced and pattern.match(line) and not line.lstrip().startswith("#"):
            indent = pattern.match(line).group(1)  # type: ignore[union-attr]
            out.append(f"{indent}{key}={value}")
            replaced = True
            continue
        out.append(line)
    if not replaced:
        if server_at is None:
            out = ["[server]", f"{key}={value}", *out]
        else:
            out.insert(server_at + 1, f"{key}={value}")
    text = "\n".join(out)
    if not text.endswith("\n"):
        text += "\n"
    return text


def remove_server_assignment(conf_text: str, key: str) -> str:
    """Remove an uncommented key from [server]. Commented examples stay."""
    if not re.fullmatch(r"[A-Za-z0-9-]+", key):
        raise ValueError(key)
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=.*$")
    lines = conf_text.splitlines()
    section = None
    out: list[str] = []
    for line in lines:
        section_match = _SECTION.match(line)
        if section_match:
            section = section_match.group(1).strip().lower()
            out.append(line)
            continue
        if section == "server" and pattern.match(line) and not line.lstrip().startswith("#"):
            continue
        out.append(line)
    text = "\n".join(out)
    if not text.endswith("\n"):
        text += "\n"
    return text


def render_service(
    *,
    paths: list[str],
    node: str,
    service_name: str | None = None,
    rtsp_port: int = 8554,
    camctl_port: int = 8555,
    api_port: int = 9997,
) -> str:
    path_value = ",".join(paths)
    records = {
        "paths": path_value,
        "camctl": str(camctl_port),
        "api": str(api_port),
        "node": node,
    }
    body = "\n".join(
        f"    <txt-record>{escape(key)}={escape(value, {'"': '&quot;'})}</txt-record>"
        for key, value in records.items()
    )
    visible_name = service_name if service_name else node
    return f"""<?xml version="1.0" standalone="no"?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">{escape(visible_name)}</name>
  <service>
    <type>_rtsp._tcp</type>
    <port>{int(rtsp_port)}</port>
{body}
  </service>
</service-group>
"""
