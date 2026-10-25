"""Still snapshots, one-shot autofocus, and MP4 clips.

A snapshot and an autofocus cycle need the CSI device, so the live publisher
is stopped and then restarted. Recording copies the RTSP path MediaMTX is
already serving and does not open the camera again.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone

from pidentify.settings import CAMERA_IDS, SettingsError

CAPTURE_NAME = re.compile(r"^cam[01]-[0-9]{8}T[0-9]{6}Z\.mp4$")
DEFAULT_LIMIT_BYTES = 512 * 1024 * 1024
SNAPSHOT_NOTE = (
    "The live stream stops while the still capture uses the camera, then the publisher restarts. "
    "Expect roughly 1-3 seconds of missing video."
)
PREVIEW_NOTE = "Preview is one frame from the live RTSP path. It does not open the camera a second time."
RECORD_NOTE = (
    "Copied from the live RTSP feed. The camera was not opened a second time. "
    "If the path was idle, MediaMTX started the on-demand publisher for this clip."
)
_AF_STATE = {0: "idle", 1: "scanning", 2: "focused", 3: "failed"}


def captures_dir(state_dir: str) -> str:
    path = os.path.join(state_dir, "captures")
    os.makedirs(path, exist_ok=True)
    return path


def clip_name(cam_id: str, when: datetime | None = None) -> str:
    if cam_id not in CAMERA_IDS:
        raise SettingsError("unknown camera {0}".format(cam_id))
    moment = when or datetime.now(timezone.utc)
    return "{0}-{1}.mp4".format(cam_id, moment.strftime("%Y%m%dT%H%M%SZ"))


def safe_clip(name: str) -> str:
    base = os.path.basename(name or "")
    if not CAPTURE_NAME.match(base):
        raise SettingsError("unknown capture", {"name": "capture names look like cam0-20261025T101530Z.mp4"})
    return base


def list_captures(state_dir: str, cam_id: str | None = None) -> list[dict]:
    directory = captures_dir(state_dir)
    items = []
    for name in os.listdir(directory):
        if not CAPTURE_NAME.match(name):
            continue
        if cam_id and not name.startswith(cam_id + "-"):
            continue
        path = os.path.join(directory, name)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        items.append(
            {
                "name": name,
                "camera": name.split("-", 1)[0],
                "bytes": stat.st_size,
                "created": datetime.fromtimestamp(stat.st_mtime, timezone.utc).replace(microsecond=0).isoformat(),
                "download": "/captures/{0}".format(name),
            }
        )
    items.sort(key=lambda item: item["name"], reverse=True)
    return items


def used_bytes(state_dir: str) -> int:
    total = 0
    directory = captures_dir(state_dir)
    for name in os.listdir(directory):
        if not CAPTURE_NAME.match(name):
            continue
        try:
            total += os.path.getsize(os.path.join(directory, name))
        except OSError:
            continue
    return total


def prune_captures(state_dir: str, limit_bytes: int, protect: str | None = None) -> list[str]:
    """Delete oldest clips until the directory is within the cap.

    The clip named by ``protect`` (the one just written) is kept even when it
    alone is larger than the cap.
    """
    directory = captures_dir(state_dir)
    files = []
    for name in os.listdir(directory):
        if not CAPTURE_NAME.match(name):
            continue
        path = os.path.join(directory, name)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        files.append((stat.st_mtime, name, path, stat.st_size))
    files.sort()
    removed = []
    total = sum(item[3] for item in files)
    for _mtime, name, path, size in files:
        if total <= limit_bytes:
            break
        if name == protect:
            continue
        try:
            os.unlink(path)
        except OSError:
            continue
        total -= size
        removed.append(name)
    return removed


def parse_metadata(text: str) -> dict:
    """Read LensPosition and AfState out of an rpicam-still metadata dump."""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise SettingsError("autofocus did not return metadata", {"autofocus": text.strip()[:400] or "empty metadata"})
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise SettingsError("autofocus metadata was not JSON", {"autofocus": str(exc)}) from exc
    if not isinstance(payload, dict):
        raise SettingsError("autofocus metadata was not an object")
    lens = payload.get("LensPosition", payload.get("lens_position"))
    if isinstance(lens, bool) or not isinstance(lens, (int, float)):
        raise SettingsError(
            "autofocus metadata has no LensPosition",
            {"autofocus": "the sensor did not report a lens position"},
        )
    state = payload.get("AfState", payload.get("af_state"))
    if isinstance(state, bool) or not isinstance(state, (int, float, str)):
        label = None
    elif isinstance(state, str):
        label = state
    else:
        label = _AF_STATE.get(int(state), str(int(state)))
    return {"lens_position": float(lens), "af_state": label}


def run_pause_resume(pause, resume, action):
    """Stop the publisher, run action, always restart, and time the gap."""
    started = time.monotonic()
    stream_was_live = bool(pause())
    error = None
    result = None
    try:
        result = action()
    except Exception as exc:  # noqa: BLE001 - the caller turns this into HTTP 502
        error = exc
    restart = resume()
    elapsed = int((time.monotonic() - started) * 1000)
    return result, error, restart, elapsed, stream_was_live
