"""Parse ``rpicam-hello --list-cameras`` and map CSI indexes to cam0/cam1.

Pi 5 nodes have two CSI ports. libcamera's index is what ``rpicam-vid --camera``
selects, so path ``camN`` is camera index N. Indexes other than 0 and 1 are
reported and ignored (this installer only publishes two paths).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass

from pidentify.sensors import parse_mode_line

_CAM_LINE = re.compile(
    r"^\s*(\d+)\s*:\s*(\S+)\s*\[([^\]]*)\]\s*(?:\(([^)]*)\))?",
)
_NO_CAMERA = re.compile(r"no cameras available", re.IGNORECASE)

CONFIG_TXT_WARNING = (
    "No CSI cameras were detected (rpicam-hello --list-cameras). "
    "pidentify did not modify /boot/firmware/config.txt. "
    "Bookworm normally enables the camera stack with camera_auto_detect=1. "
    "If a camera is plugged in, check the CSI cable and which port it uses "
    "(Pi 5 has two). If the sensor needs a dtoverlay, edit "
    "/boot/firmware/config.txt yourself and reboot. This installer never "
    "changes that file."
)


@dataclass(frozen=True)
class Camera:
    index: int
    model: str
    modes: str
    device: str
    resolutions: tuple = ()

    @property
    def id(self) -> str:
        return f"cam{self.index}"

    def to_json(self) -> dict[str, object]:
        data = asdict(self)
        data["id"] = self.id
        data["present"] = True
        data["resolutions"] = [
            {"width": width, "height": height, "max_fps": fps}
            for width, height, fps in self.resolutions
        ]
        return data


def parse_list_cameras(text: str) -> tuple[list[Camera], list[str]]:
    """Return detected cameras (indexes 0 and 1 only) and human warnings."""
    found: dict[int, Camera] = {}
    pending: dict[int, list[tuple[int, int, float | None]]] = {}
    current: int | None = None
    warnings: list[str] = []
    for line in text.splitlines():
        match = _CAM_LINE.match(line)
        if match:
            index = int(match.group(1))
            current = index
            pending.setdefault(index, [])
            camera = Camera(
                index=index,
                model=match.group(2),
                modes=(match.group(3) or "").strip(),
                device=(match.group(4) or "").strip(),
            )
            if index not in (0, 1):
                warnings.append(
                    f"ignoring camera index {index} ({camera.model}); only cam0 and cam1 are published"
                )
                continue
            if index in found:
                warnings.append(f"duplicate camera index {index}; keeping the first ({found[index].model})")
                continue
            found[index] = camera
            continue
        if current is None:
            continue
        parsed = parse_mode_line(line)
        if parsed is None:
            continue
        pending.setdefault(current, []).append(parsed)
    cameras = []
    for index in sorted(found):
        camera = found[index]
        seen = []
        for width, height, fps in pending.get(index, []):
            if any(item[0] == width and item[1] == height for item in seen):
                continue
            seen.append((width, height, fps))
        cameras.append(
            Camera(
                index=camera.index,
                model=camera.model,
                modes=camera.modes,
                device=camera.device,
                resolutions=tuple(seen),
            )
        )
    return cameras, warnings


def detection_failed(text: str, returncode: int, cameras: list[Camera]) -> bool:
    """True when the probe did not produce a trustworthy inventory."""
    if cameras:
        return False
    if _NO_CAMERA.search(text or ""):
        return False
    if returncode == 0 and text.strip():
        # A successful command that printed neither cameras nor the
        # "no cameras" banner is still inconclusive.
        return True
    return returncode != 0 or not text.strip()


def read_probe_text(list_file: str | None = None, command: list[str] | None = None) -> tuple[str, int]:
    """Run rpicam-hello, or read a saved transcript for tests."""
    override = list_file or os.environ.get("PIDENTIFY_LIST_CAMERAS_FILE")
    if override:
        with open(override, encoding="utf-8") as handle:
            return handle.read(), 0
    argv = command or _default_list_command()
    if argv is None:
        return "rpicam-hello (and libcamera-hello) not found on PATH", 127
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return "rpicam-hello timed out after 30s", 124
    text = (completed.stdout or "") + (completed.stderr or "")
    return text, completed.returncode


def _default_list_command() -> list[str] | None:
    for binary in ("rpicam-hello", "libcamera-hello"):
        path = _which(binary)
        if path:
            return [path, "--list-cameras"]
    return None


def _which(name: str) -> str | None:
    paths = os.environ.get("PATH", "").split(os.pathsep)
    for directory in paths:
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def probe(list_file: str | None = None, command: list[str] | None = None) -> dict[str, object]:
    text, code = read_probe_text(list_file=list_file, command=command)
    cameras, warnings = parse_list_cameras(text)
    failed = detection_failed(text, code, cameras)
    if failed:
        warnings.append(
            "camera detection failed; not treating this as 'no cameras'. "
            f"probe exit {code}: {text.strip()[:400]}"
        )
    elif not cameras:
        warnings.append(CONFIG_TXT_WARNING)
    return {
        "ok": not failed,
        "returncode": code,
        "cameras": [camera.to_json() for camera in cameras],
        "warnings": warnings,
        "raw": text,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detect CSI cameras via rpicam-hello")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--from-config", default=None, help="read cameras.json instead of probing")
    args = parser.parse_args(argv)
    if args.from_config:
        with open(args.from_config, encoding="utf-8") as handle:
            data = json.load(handle)
        if args.json:
            print(json.dumps(data))
        else:
            for camera in data.get("cameras", []):
                print(f"{camera.get('id')}: {camera.get('model')} {camera.get('device', '')}")
        return 0
    result = probe()
    if args.json:
        printable = {key: value for key, value in result.items() if key != "raw"}
        print(json.dumps(printable))
    else:
        if not result["cameras"]:
            print("cameras: none")
        for camera in result["cameras"]:
            print(f"{camera['id']}: {camera['model']} {camera.get('device', '')}")
        for warning in result["warnings"]:
            print(f"warning: {warning}", file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
