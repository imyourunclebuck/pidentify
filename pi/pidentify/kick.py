"""Restart one MediaMTX on-demand path so a new rpicam-vid picks up settings.

The publisher reads cam_settings.json every time it starts. Kicking the path
is enough; the on-disk mediamtx.yml command stays stable. The control API
patch adds a numeric generation token so MediaMTX reloads the path. If that
API is down, the previous publisher process group gets SIGTERM and MediaMTX's
runOnDemandRestart brings it back while a viewer is connected.
"""

from __future__ import annotations

import json
import os
import re
import signal
import time
import urllib.error
import urllib.request

from pidentify.conflicts import read_cmdline, read_pgrp
from pidentify.mediamtx_conf import publish_command
from pidentify.settings import CAMERA_IDS


def _matches(argv: list[str], cam_id: str) -> bool:
    text = " ".join(argv)
    for needle in (f"pidentify-publish {cam_id}", f"pidentify.publish {cam_id}"):
        if re.search(rf"(^|[\s/]){re.escape(needle)}(\s|$)", text):
            return True
    return False


def list_publishers(proc_root: str, cam_id: str) -> list[dict]:
    found: list[dict] = []
    try:
        names = os.listdir(proc_root)
    except OSError:
        return found
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        argv = read_cmdline(proc_root, pid)
        if not argv or not _matches(argv, cam_id):
            continue
        found.append({"pid": pid, "pgrp": read_pgrp(proc_root, pid), "argv": argv})
    return found


def _publish_hook(mode: str | None, config_dir: str | None) -> str:
    if mode not in ("local", "push"):
        directory = config_dir or os.environ.get("PIDENTIFY_CONFIG_DIR", "/etc/pidentify")
        try:
            from pidentify.nodeconfig import load_node_config

            mode = load_node_config(os.path.join(directory, "config"))["mode"]
        except Exception:
            mode = "local"
    return "runOnInit" if mode == "push" else "runOnDemand"


def restart_camera(
    cam_id: str,
    *,
    api_base: str = "http://127.0.0.1:9997",
    publish_cmd: str = "/usr/local/bin/pidentify-publish",
    opener=None,
    proc_root: str = "/proc",
    signaler=None,
    sleeper=None,
    token: str | None = None,
    mode: str | None = None,
    config_dir: str | None = None,
) -> dict:
    if cam_id not in CAMERA_IDS:
        return {"ok": False, "method": "none", "detail": f"unknown camera {cam_id}"}
    opener = opener or urllib.request.urlopen
    signaler = signaler or (lambda pid, sig: os.kill(pid, sig))
    sleeper = sleeper or time.sleep
    token = token or str(time.time_ns())
    before = list_publishers(proc_root, cam_id)
    before_pids = {item["pid"] for item in before}
    command = publish_command(publish_cmd, cam_id, token)
    hook = _publish_hook(mode, config_dir)
    payload = json.dumps({hook: command, f"{hook}Restart": True}).encode("utf-8")
    request = urllib.request.Request(
        f"{api_base.rstrip('/')}/v3/config/paths/patch/{cam_id}",
        data=payload,
        method="PATCH",
        headers={"Content-Type": "application/json"},
    )
    api_error = None
    api_ok = False
    try:
        with opener(request, timeout=3) as response:
            status = getattr(response, "status", 200)
            if status >= 400:
                api_error = f"HTTP {status}"
            else:
                api_ok = True
    except urllib.error.HTTPError as exc:
        api_ok = False
        api_error = f"HTTP {exc.code}"
        if exc.code == 404:
            return {
                "ok": False,
                "method": "mediamtx-api",
                "detail": f"MediaMTX has no path {cam_id} (HTTP 404). Re-run install.sh after the camera is detected.",
            }
    except Exception as exc:  # noqa: BLE001 - surface any local API failure to the caller
        api_ok = False
        api_error = str(exc)

    if api_ok:
        if before_pids:
            sleeper(0.4)
            still = [item for item in list_publishers(proc_root, cam_id) if item["pid"] in before_pids]
            if still:
                _signal_groups(still, signaler)
                return {
                    "ok": True,
                    "method": "mediamtx-api+signal",
                    "detail": "patched the path and signaled the previous publisher",
                }
        if before_pids:
            return {"ok": True, "method": "mediamtx-api", "detail": "MediaMTX reloaded the path"}
        if hook == "runOnInit":
            return {
                "ok": True,
                "method": "continuous",
                "detail": "continuous publisher updated; it does not wait for a viewer",
            }
        return {
            "ok": True,
            "method": "idle",
            "detail": "path updated; the camera opens on the next viewer",
        }

    if before:
        _signal_groups(before, signaler)
        return {
            "ok": True,
            "method": "signal",
            "detail": f"MediaMTX API unavailable ({api_error}); signaled the publisher",
        }
    return {
        "ok": False,
        "method": "none",
        "detail": f"MediaMTX API unavailable ({api_error}) and no publisher is running",
    }


def _signal_groups(publishers: list[dict], signaler) -> None:
    groups = set()
    for item in publishers:
        pgrp = item.get("pgrp") or item["pid"]
        if pgrp <= 1 or pgrp in groups:
            continue
        groups.add(pgrp)
        # Negative pid signals the process group MediaMTX created for the hook.
        signaler(-pgrp, signal.SIGTERM)
