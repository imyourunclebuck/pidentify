"""Node mode file at /etc/pidentify/config.

install.sh rewrites this file. A later run with no --name, --push-to, or
--local keeps the stored values. The file is key=value, with no secrets.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from urllib.parse import urlsplit

from pidentify.hostname import dns_safe_hostname
from pidentify.settings import CAMERA_IDS

_KEYS = ("mode", "name", "push_url")
_PREFIX = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62})$")


class ConfigError(ValueError):
    pass


def empty_config() -> dict[str, str]:
    return {"mode": "local", "name": "", "push_url": ""}


def load_node_config(path: str) -> dict[str, str]:
    result = empty_config()
    if not path or not os.path.exists(path):
        return result
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.split("#", 1)[0].strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key in _KEYS:
                result[key] = value.strip()
    if result["mode"] not in ("local", "push"):
        raise ConfigError(f"mode in {path} must be local or push, not {result['mode']!r}")
    return result


def render_node_config(cfg: dict[str, str]) -> str:
    mode = cfg.get("mode") or "local"
    if mode not in ("local", "push"):
        raise ConfigError(f"mode must be local or push, not {mode!r}")
    name = cfg.get("name") or ""
    push_url = cfg.get("push_url") or ""
    return (
        "# pidentify node configuration. Rewritten by install.sh.\n"
        "# Run install.sh again with no --name, --push-to, or --local to keep these values.\n"
        "# mode=local  RTSP on this Pi, camera open only while a viewer is connected.\n"
        "# mode=push   also publish continuously to push_url-cam0 and push_url-cam1.\n"
        "# Switch back to local with: install.sh --local\n"
        f"mode={mode}\n"
        f"name={name}\n"
        f"push_url={push_url}\n"
    )


def save_node_config(path: str, cfg: dict[str, str]) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    text = render_node_config(cfg)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def parse_push_target(url: str, *, default_prefix: str | None = None) -> dict:
    """Normalize rtsp://HOST[:PORT]/PREFIX.

    Cameras publish to that URL plus ``-cam0`` and ``-cam1``. A missing path
    uses default_prefix (the node name).
    """
    raw = (url or "").strip()
    if not raw:
        raise ConfigError("push URL is empty; use --push-to rtsp://HOST:8554/PREFIX")
    parts = urlsplit(raw)
    if parts.scheme != "rtsp":
        raise ConfigError("push URL must start with rtsp://")
    if parts.username or parts.password:
        raise ConfigError("push URL must not contain a username or password")
    if parts.query or parts.fragment:
        raise ConfigError("push URL must not contain a query or fragment")
    host = parts.hostname
    if not host:
        raise ConfigError("push URL is missing a host")
    try:
        port = parts.port or 8554
    except ValueError as exc:
        raise ConfigError("push URL port is invalid") from exc
    if not 1 <= port <= 65535:
        raise ConfigError("push URL port is invalid")
    prefix = parts.path.strip("/")
    if "/" in prefix:
        raise ConfigError("push prefix must be a single path segment")
    if not prefix:
        if not default_prefix:
            raise ConfigError("push URL has no prefix; pass --name or add /PREFIX")
        prefix = dns_safe_hostname(default_prefix)
    if not _PREFIX.match(prefix):
        raise ConfigError("push prefix may contain only letters, digits, dot, underscore, and hyphen")
    host_fmt = f"[{host}]" if ":" in host else host
    base = f"rtsp://{host_fmt}:{port}/{prefix}"
    targets = {cam_id: f"{base}-{cam_id}" for cam_id in CAMERA_IDS}
    return {"url": base, "host": host, "port": port, "prefix": prefix, "targets": targets}


def format_status(config_dir: str) -> str:
    path = os.path.join(config_dir, "config")
    lines = [f"config: {path}"]
    if not os.path.exists(path):
        lines.append("mode: not configured")
        return "\n".join(lines)
    try:
        cfg = load_node_config(path)
    except (OSError, ConfigError) as exc:
        lines.append(f"error: {exc}")
        return "\n".join(lines)
    lines.append(f"mode: {cfg['mode']}")
    lines.append(f"name: {cfg['name'] or '(unset, derived from the system hostname)'}")
    lines.append(f"push_url: {cfg['push_url'] or '(none)'}")
    node_path = os.path.join(config_dir, "node.json")
    if os.path.exists(node_path):
        try:
            with open(node_path, encoding="utf-8") as handle:
                node = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            lines.append(f"node.json: {exc}")
            return "\n".join(lines)
        if node.get("mdns_fqdn"):
            lines.append(f"mdns: {node['mdns_fqdn']}")
        if node.get("system_hostname"):
            lines.append(f"system hostname: {node['system_hostname']}")
        targets = node.get("push_targets") or {}
        if cfg["mode"] == "push" and isinstance(targets, dict):
            for cam_id in CAMERA_IDS:
                if cam_id in targets:
                    lines.append(f"push {cam_id}: {targets[cam_id]}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show /etc/pidentify/config")
    parser.add_argument("--config-dir", default=os.environ.get("PIDENTIFY_CONFIG_DIR", "/etc/pidentify"))
    args = parser.parse_args(argv)
    print(format_status(args.config_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
