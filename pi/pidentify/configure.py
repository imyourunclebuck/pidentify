"""Write the on-disk node configuration from a camera probe.

This module never opens /boot/firmware/config.txt. A missing camera produces
a warning string only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone

from pidentify.avahi_conf import (
    DEFAULT_AVAHI_CONF,
    read_domain,
    read_server_value,
    remove_server_assignment,
    render_service,
    set_avahi_host_name,
    set_server_assignment,
)
from pidentify.detect import probe
from pidentify.hostname import identity, resolve_system_hostname
from pidentify.ifaces import discover, interface_policy
from pidentify.manifest import load as load_manifest
from pidentify.manifest import save as save_manifest
from pidentify.mediamtx_conf import render_mediamtx_yml
from pidentify.nodeconfig import ConfigError, load_node_config, parse_push_target, save_node_config
from pidentify.settings import CAMERA_IDS, default_settings, load_settings, public_settings, save_settings


def _atomic_text(path: str, text: str, mode: int = 0o644) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _merge_settings(existing_path: str, detected_ids: list[str]) -> dict:
    # A corrupt settings file is an error. Replacing it would throw away
    # controls the operator already tuned.
    current = load_settings(existing_path)
    merged: dict = {}
    for cam_id in CAMERA_IDS:
        if cam_id in current:
            merged[cam_id] = current[cam_id]
    for cam_id in detected_ids:
        merged.setdefault(cam_id, default_settings())
    return merged


def configure_node(
    *,
    config_dir: str,
    state_dir: str,
    avahi_conf: str,
    avahi_service: str,
    avahi_backup: str,
    publish_cmd: str,
    push_cmd: str = "/usr/local/bin/pidentify-push",
    hostname: str | None = None,
    hostname_file: str | None = "/etc/hostname",
    manifest_path: str | None = None,
    name: str | None = None,
    push_to: str | None = None,
    local: bool = False,
    network: dict | None = None,
) -> dict:
    os.makedirs(config_dir, exist_ok=True)
    os.makedirs(state_dir, exist_ok=True)
    manifest_path = manifest_path or os.path.join(state_dir, "manifest.json")
    manifest = load_manifest(manifest_path)

    result = probe()
    cameras_path = os.path.join(config_dir, "cameras.json")
    warnings = list(result["warnings"])
    if result["ok"]:
        cameras = result["cameras"]
        document = {
            "detected_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "source": "rpicam-hello --list-cameras",
            "cameras": cameras,
        }
        _atomic_text(cameras_path, json.dumps(document, indent=2) + "\n")
    elif os.path.exists(cameras_path):
        with open(cameras_path, encoding="utf-8") as handle:
            document = json.load(handle)
        cameras = document.get("cameras") or []
        warnings.append(f"kept the existing inventory in {cameras_path}")
    else:
        cameras = []
        document = {
            "detected_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "source": "detection-failed",
            "cameras": [],
        }
        _atomic_text(cameras_path, json.dumps(document, indent=2) + "\n")

    path_ids = [str(camera["id"]) for camera in cameras if camera.get("id") in CAMERA_IDS]
    settings_path = os.path.join(config_dir, "cam_settings.json")
    settings = _merge_settings(settings_path, path_ids)
    # Keep settings for cameras that were detected before and unplugged,
    # but do not invent a settings entry for a camera we have never seen
    # unless it is detected now (handled above).
    save_settings(settings_path, settings)

    config_path = os.path.join(config_dir, "config")
    existing_cfg = load_node_config(config_path)
    if local and push_to is not None:
        raise ConfigError("pass either --local or --push-to, not both")
    chosen_name = existing_cfg["name"] if name is None else name.strip()
    if local:
        mode = "local"
        raw_push = existing_cfg["push_url"]
    elif push_to is not None:
        mode = "push"
        raw_push = push_to.strip()
    else:
        mode = existing_cfg["mode"]
        raw_push = existing_cfg["push_url"]

    system_hostname = resolve_system_hostname(hostname, hostname_file if not hostname else None)
    existed = os.path.isfile(avahi_conf)
    if existed:
        with open(avahi_conf, encoding="utf-8") as handle:
            original = handle.read()
    else:
        original = DEFAULT_AVAHI_CONF
    domain = read_domain(original)
    info = identity(system_hostname, domain, name=chosen_name or None)
    if info["mdns_hostname"] == "pi5":
        warnings.append(
            "mDNS name is pi5.local. These camera Pis share the system hostname pi5; "
            "re-run install.sh --name pi5-cam-N-myrtle so Avahi and the Jetson can tell them apart."
        )

    push_targets: dict[str, str] = {}
    stored_push = raw_push
    if mode == "push":
        if not raw_push:
            raise ConfigError("push mode requires --push-to rtsp://HOST:8554/PREFIX")
        target = parse_push_target(raw_push, default_prefix=info["mdns_hostname"])
        stored_push = target["url"]
        push_targets = {cam_id: target["targets"][cam_id] for cam_id in path_ids}
    elif stored_push:
        try:
            parse_push_target(stored_push, default_prefix=info["mdns_hostname"])
        except ConfigError as exc:
            warnings.append(f"stored push_url is unused in local mode and is not valid: {exc}")

    save_node_config(
        config_path,
        {"mode": mode, "name": info["name"], "push_url": stored_push},
    )

    yml_path = os.path.join(config_dir, "mediamtx.yml")
    _atomic_text(
        yml_path,
        render_mediamtx_yml(path_ids, publish_cmd=publish_cmd, push_cmd=push_cmd, mode=mode),
        mode=0o644,
    )

    updated = set_avahi_host_name(original, info["mdns_hostname"])
    if network is None:
        network = discover()
    deny_csv, allow_csv = interface_policy(
        network,
        read_server_value(updated, "deny-interfaces"),
        read_server_value(updated, "allow-interfaces"),
    )
    updated = set_server_assignment(updated, "deny-interfaces", deny_csv)
    if allow_csv:
        updated = set_server_assignment(updated, "allow-interfaces", allow_csv)
    elif network.get("detected"):
        updated = remove_server_assignment(updated, "allow-interfaces")
    if existed and updated != original:
        os.makedirs(os.path.dirname(avahi_backup) or ".", exist_ok=True)
        if not os.path.exists(avahi_backup):
            shutil.copy2(avahi_conf, avahi_backup)
            manifest["avahi_backup"] = avahi_backup
    if not existed:
        manifest["avahi_conf_created"] = True
    if updated != original or not existed:
        _atomic_text(avahi_conf, updated, mode=0o644)

    service_xml = render_service(paths=path_ids, node=info["node"], service_name=info["mdns_hostname"])
    _atomic_text(avahi_service, service_xml, mode=0o644)

    node_doc = dict(info)
    node_doc["mode"] = mode
    node_doc["push_url"] = stored_push
    node_doc["push_targets"] = push_targets
    node_path = os.path.join(config_dir, "node.json")
    _atomic_text(node_path, json.dumps(node_doc, indent=2) + "\n")

    manifest["version"] = manifest.get("version") or "1.0.0"
    save_manifest(manifest_path, manifest)

    summary = {
        "hostname": info["hostname"],
        "system_hostname": info["system_hostname"],
        "name": info["name"],
        "node": info["node"],
        "mdns_hostname": info["mdns_hostname"],
        "mdns_fqdn": info["mdns_fqdn"],
        "mode": mode,
        "push_url": stored_push,
        "push_targets": push_targets,
        "paths": path_ids,
        "cameras": cameras,
        "warnings": warnings,
        "detection_ok": bool(result["ok"]),
        "settings_path": settings_path,
        "mediamtx_yml": yml_path,
        "avahi_service": avahi_service,
        "avahi_conf": avahi_conf,
        "settings": {cam_id: public_settings(value) for cam_id, value in settings.items()},
    }
    _atomic_text(os.path.join(state_dir, "configure.json"), json.dumps(summary, indent=2) + "\n")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write pidentify configs for this node")
    parser.add_argument("--config-dir", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--avahi-conf", required=True)
    parser.add_argument("--avahi-service", required=True)
    parser.add_argument("--avahi-backup", required=True)
    parser.add_argument("--publish-cmd", default="/usr/local/bin/pidentify-publish")
    parser.add_argument("--push-cmd", default="/usr/local/bin/pidentify-push")
    parser.add_argument("--hostname", default=None)
    parser.add_argument("--hostname-file", default="/etc/hostname")
    parser.add_argument("--name", default=None)
    parser.add_argument("--push-to", default=None)
    parser.add_argument("--local", action="store_true")
    args = parser.parse_args(argv)
    try:
        summary = configure_node(
            config_dir=args.config_dir,
            state_dir=args.state_dir,
            avahi_conf=args.avahi_conf,
            avahi_service=args.avahi_service,
            avahi_backup=args.avahi_backup,
            publish_cmd=args.publish_cmd,
            push_cmd=args.push_cmd,
            hostname=args.hostname,
            hostname_file=args.hostname_file,
            name=args.name,
            push_to=args.push_to,
            local=args.local,
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"system hostname: {summary['system_hostname']}", file=sys.stderr)
    print(f"node: {summary['node']}", file=sys.stderr)
    print(f"mdns: {summary['mdns_fqdn']}", file=sys.stderr)
    print(f"mode: {summary['mode']}", file=sys.stderr)
    if summary["cameras"]:
        for camera in summary["cameras"]:
            print(
                f"camera {camera.get('id')}: {camera.get('model')} "
                f"({camera.get('modes')}) {camera.get('device')}",
                file=sys.stderr,
            )
    else:
        print("cameras: none", file=sys.stderr)
    for warning in summary["warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
