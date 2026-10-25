"""Per-camera settings stored in /etc/pidentify/cam_settings.json.

Values map onto rpicam-vid flags (rpicam-apps on Bookworm and Trixie). Ranges
follow the rpicam-vid help text and the libcamera control descriptions used by
Picamera2. Null optional controls are omitted so rpicam-vid keeps its own
defaults. The default bitrate is 2 Mbps so an idle-looking 720p30 stream stays
small on the tailnet; each camera can override it.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from typing import Any

CAMERA_IDS = ("cam0", "cam1")

AWB_MODES = (
    "auto",
    "incandescent",
    "tungsten",
    "fluorescent",
    "indoor",
    "daylight",
    "cloudy",
    "custom",
)
AF_MODES = ("default", "manual", "auto", "continuous")
HDR_MODES = ("off", "auto", "sensor", "single-exp")

# Flags the publisher owns. extra_args must not override them.
_FORBIDDEN_EXTRA = {
    "-o",
    "--output",
    "--listen",
    "-l",
    "--codec",
    "-t",
    "--timeout",
    "--camera",
    "-n",
    "--nopreview",
    "--width",
    "--height",
    "--framerate",
    "--bitrate",
    "-b",
    "--inline",
    "--flush",
    "--low-latency",
    "--intra",
    "-g",
    "--hflip",
    "--vflip",
    "--rotation",
    "--brightness",
    "--contrast",
    "--saturation",
    "--sharpness",
    "--ev",
    "--awb",
    "--shutter",
    "--gain",
    "--autofocus-mode",
    "--lens-position",
    "--hdr",
}

_FLAG_RE = re.compile(r"^--?[A-Za-z0-9][A-Za-z0-9._:-]*$")
_VALUE_RE = re.compile(r"^[A-Za-z0-9_./:+,-]+$")

OPTIONAL_KEYS = (
    "brightness",
    "contrast",
    "saturation",
    "sharpness",
    "ev",
    "awb",
    "shutter",
    "gain",
    "autofocus_mode",
    "lens_position",
    "hdr",
)
REQUIRED_KEYS = (
    "width",
    "height",
    "fps",
    "bitrate",
    "hflip",
    "vflip",
    "rotation",
)
SETTING_KEYS = REQUIRED_KEYS + OPTIONAL_KEYS


class SettingsError(ValueError):
    def __init__(self, message: str, fields: dict[str, str] | None = None):
        super().__init__(message)
        self.fields = fields or {}


def default_settings() -> dict[str, Any]:
    return {
        "width": 1280,
        "height": 720,
        "fps": 30,
        "bitrate": 2_000_000,
        "brightness": None,
        "contrast": None,
        "saturation": None,
        "sharpness": None,
        "ev": None,
        "awb": None,
        "shutter": None,
        "gain": None,
        "autofocus_mode": None,
        "lens_position": None,
        "hdr": None,
        "hflip": False,
        "vflip": False,
        "rotation": 0,
        "extra_args": [],
    }


def _reject_bool(value: Any, name: str) -> None:
    if isinstance(value, bool):
        raise SettingsError("invalid settings", {name: "expected a number, got a boolean"})


def _number(value: Any, name: str, *, low: float, high: float, integer: bool = False) -> int | float:
    _reject_bool(value, name)
    if isinstance(value, str):
        raise SettingsError("invalid settings", {name: "expected a number"})
    try:
        if integer:
            if isinstance(value, float) and not value.is_integer():
                raise ValueError
            number: int | float = int(value)
        else:
            number = float(value)
    except (TypeError, ValueError) as exc:
        raise SettingsError("invalid settings", {name: "expected a number"}) from exc
    if number < low or number > high:
        raise SettingsError("invalid settings", {name: f"must be between {low} and {high}"})
    return number


def validate_extra_args(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SettingsError("invalid settings", {"extra_args": "expected a list of strings"})
    cleaned: list[str] = []
    for item in value:
        if item in _FORBIDDEN_EXTRA or item.split("=", 1)[0] in _FORBIDDEN_EXTRA:
            raise SettingsError(
                "invalid settings",
                {"extra_args": f"{item} is reserved for the publisher"},
            )
        if not (_FLAG_RE.match(item) or _VALUE_RE.match(item)):
            raise SettingsError(
                "invalid settings",
                {"extra_args": f"unsupported token {item!r}"},
            )
        cleaned.append(item)
    return cleaned


def _optional_hdr(value: Any) -> str | None:
    if isinstance(value, bool):
        return "auto" if value else "off"
    if not isinstance(value, str) or value not in HDR_MODES:
        allowed = ", ".join(HDR_MODES)
        raise SettingsError("invalid settings", {"hdr": f"expected one of {allowed} (or a boolean)"})
    return value


def normalize_settings(data: dict[str, Any], *, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate a full settings object. Unknown keys are rejected."""
    if not isinstance(data, dict):
        raise SettingsError("invalid settings", {"_": "expected an object"})
    unknown = sorted(set(data) - set(SETTING_KEYS) - {"extra_args"})
    if unknown:
        raise SettingsError("invalid settings", {key: "unknown setting" for key in unknown})
    merged = default_settings()
    if base:
        merged.update({key: base[key] for key in SETTING_KEYS if key in base})
        merged["extra_args"] = list(base.get("extra_args") or [])
    merged.update(data)
    out = default_settings()
    out["width"] = int(_number(merged["width"], "width", low=160, high=8192, integer=True))
    out["height"] = int(_number(merged["height"], "height", low=120, high=8192, integer=True))
    if out["width"] % 2 or out["height"] % 2:
        fields = {}
        if out["width"] % 2:
            fields["width"] = "must be a multiple of 2"
        if out["height"] % 2:
            fields["height"] = "must be a multiple of 2"
        raise SettingsError("invalid settings", fields)
    out["fps"] = int(_number(merged["fps"], "fps", low=1, high=120, integer=True))
    out["bitrate"] = int(_number(merged["bitrate"], "bitrate", low=100_000, high=25_000_000, integer=True))
    out["hflip"] = _bool(merged["hflip"], "hflip")
    out["vflip"] = _bool(merged["vflip"], "vflip")
    rotation = int(_number(merged["rotation"], "rotation", low=0, high=180, integer=True))
    if rotation not in (0, 180):
        raise SettingsError("invalid settings", {"rotation": "only 0 or 180 are supported by rpicam-vid"})
    out["rotation"] = rotation
    out["brightness"] = _optional_number(merged["brightness"], "brightness", low=-1.0, high=1.0)
    out["contrast"] = _optional_number(merged["contrast"], "contrast", low=0.0, high=32.0)
    out["saturation"] = _optional_number(merged["saturation"], "saturation", low=0.0, high=32.0)
    out["sharpness"] = _optional_number(merged["sharpness"], "sharpness", low=0.0, high=16.0)
    out["ev"] = _optional_number(merged["ev"], "ev", low=-10.0, high=10.0)
    out["awb"] = _optional_choice(merged["awb"], "awb", AWB_MODES)
    out["shutter"] = _optional_int(merged["shutter"], "shutter", low=1, high=10_000_000)
    out["gain"] = _optional_number(merged["gain"], "gain", low=1.0, high=64.0)
    out["autofocus_mode"] = _optional_choice(merged["autofocus_mode"], "autofocus_mode", AF_MODES)
    out["lens_position"] = _optional_lens(merged["lens_position"])
    out["hdr"] = None if merged["hdr"] is None else _optional_hdr(merged["hdr"])
    if "extra_args" in data or base is not None:
        out["extra_args"] = validate_extra_args(merged.get("extra_args") or [])
    else:
        out["extra_args"] = []
    return out


def _bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise SettingsError("invalid settings", {name: "expected true or false"})


def _optional_number(value: Any, name: str, *, low: float, high: float) -> float | None:
    if value is None:
        return None
    return float(_number(value, name, low=low, high=high))


def _optional_int(value: Any, name: str, *, low: int, high: int) -> int | None:
    if value is None:
        return None
    return int(_number(value, name, low=low, high=high, integer=True))


def _optional_choice(value: Any, name: str, choices: tuple[str, ...]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in choices:
        raise SettingsError("invalid settings", {name: f"expected one of {', '.join(choices)}"})
    return value


def _optional_lens(value: Any) -> float | str | None:
    if value is None:
        return None
    if isinstance(value, str):
        if value == "default":
            return "default"
        raise SettingsError(
            "invalid settings",
            {"lens_position": 'expected a number (dioptres, 0 = infinity) or "default"'},
        )
    return float(_number(value, "lens_position", low=0.0, high=32.0))


def merge_patch(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Apply a partial update. Null clears an optional control back to default."""
    if not isinstance(patch, dict):
        raise SettingsError("invalid settings", {"_": "expected an object"})
    if "extra_args" in patch:
        raise SettingsError(
            "invalid settings",
            {"extra_args": "edit extra_args in cam_settings.json on the Pi; the HTTP API does not set it"},
        )
    unknown = sorted(set(patch) - set(SETTING_KEYS))
    if unknown:
        raise SettingsError("invalid settings", {key: "unknown setting" for key in unknown})
    merged = dict(current)
    merged.update(patch)
    return normalize_settings(merged, base=current)


def public_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Settings returned by the HTTP API (includes extra_args so operators can see them)."""
    full = normalize_settings(settings, base=settings)
    return {key: full[key] for key in (*SETTING_KEYS, "extra_args")}


def load_settings(path: str) -> dict[str, dict[str, Any]]:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise SettingsError("cam_settings.json must be an object of camera ids")
    loaded: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if key not in CAMERA_IDS:
            raise SettingsError(f"unsupported camera id {key!r} in {path}")
        if not isinstance(value, dict):
            raise SettingsError(f"{key} settings must be an object")
        loaded[key] = normalize_settings(value, base=value)
    return loaded


def save_settings(path: str, settings: dict[str, dict[str, Any]]) -> None:
    payload = {cam_id: public_settings(value) for cam_id, value in sorted(settings.items())}
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".cam_settings.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o664)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def ensure_camera(settings: dict[str, dict[str, Any]], cam_id: str) -> dict[str, Any]:
    if cam_id not in settings:
        settings[cam_id] = default_settings()
    return settings[cam_id]


def _fmt(number: float | int) -> str:
    if isinstance(number, float) and number.is_integer():
        return str(int(number))
    return str(number)


def build_rpicam_argv(
    camera_index: int,
    settings: dict[str, Any],
    *,
    binary: str = "rpicam-vid",
    low_latency: bool = False,
) -> list[str]:
    """Hardware H.264 elementary stream on stdout. No shell involved."""
    cfg = normalize_settings(settings, base=settings)
    argv = [
        binary,
        "-t",
        "0",
        "-n",
        "--camera",
        str(camera_index),
        "--codec",
        "h264",
        "--width",
        str(cfg["width"]),
        "--height",
        str(cfg["height"]),
        "--framerate",
        str(cfg["fps"]),
        "--bitrate",
        str(cfg["bitrate"]),
        "--intra",
        str(cfg["fps"]),
        "--inline",
        "--flush",
    ]
    if low_latency:
        argv.append("--low-latency")
    if cfg["brightness"] is not None:
        argv += ["--brightness", _fmt(cfg["brightness"])]
    if cfg["contrast"] is not None:
        argv += ["--contrast", _fmt(cfg["contrast"])]
    if cfg["saturation"] is not None:
        argv += ["--saturation", _fmt(cfg["saturation"])]
    if cfg["sharpness"] is not None:
        argv += ["--sharpness", _fmt(cfg["sharpness"])]
    if cfg["ev"] is not None:
        argv += ["--ev", _fmt(cfg["ev"])]
    if cfg["awb"] is not None:
        argv += ["--awb", cfg["awb"]]
    if cfg["shutter"] is not None:
        argv += ["--shutter", str(cfg["shutter"])]
    if cfg["gain"] is not None:
        argv += ["--gain", _fmt(cfg["gain"])]
    if cfg["autofocus_mode"] is not None:
        argv += ["--autofocus-mode", cfg["autofocus_mode"]]
    if cfg["lens_position"] is not None:
        argv += ["--lens-position", _fmt(cfg["lens_position"]) if not isinstance(cfg["lens_position"], str) else cfg["lens_position"]]
    if cfg["hdr"] not in (None, "off"):
        argv += ["--hdr", cfg["hdr"]]
    if cfg["hflip"]:
        argv.append("--hflip")
    if cfg["vflip"]:
        argv.append("--vflip")
    if cfg["rotation"]:
        argv += ["--rotation", str(cfg["rotation"])]
    argv += list(cfg["extra_args"])
    argv += ["-o", "-"]
    return argv


def build_ffmpeg_argv(cam_id: str, settings: dict[str, Any], *, rtsp_port: int = 8554, binary: str = "ffmpeg") -> list[str]:
    """Copy the elementary stream into RTSP/TCP without re-encoding."""
    cfg = normalize_settings(settings, base=settings)
    if not re.fullmatch(r"cam[01]", cam_id):
        raise SettingsError(f"refusing to publish unexpected path {cam_id!r}")
    if not 1 <= int(rtsp_port) <= 65535:
        raise SettingsError(f"bad RTSP port {rtsp_port!r}")
    return [
        binary,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-fflags",
        "+nobuffer+discardcorrupt+genpts",
        "-flags",
        "low_delay",
        "-probesize",
        "32",
        "-analyzeduration",
        "0",
        "-use_wallclock_as_timestamps",
        "1",
        "-f",
        "h264",
        "-framerate",
        str(cfg["fps"]),
        "-i",
        "pipe:0",
        "-an",
        "-c",
        "copy",
        "-muxdelay",
        "0",
        "-muxpreload",
        "0",
        "-rtsp_transport",
        "tcp",
        "-f",
        "rtsp",
        f"rtsp://127.0.0.1:{int(rtsp_port)}/{cam_id}",
    ]


def build_push_ffmpeg_argv(
    cam_id: str,
    remote_url: str,
    *,
    local_port: int = 8554,
    binary: str = "ffmpeg",
) -> list[str]:
    """Copy a local RTSP/TCP path to a remote RTSP server without re-encoding."""
    if cam_id not in CAMERA_IDS:
        raise SettingsError(f"refusing to push unexpected path {cam_id!r}")
    if not 1 <= int(local_port) <= 65535:
        raise SettingsError(f"bad RTSP port {local_port!r}")
    remote = str(remote_url).strip()
    if not re.fullmatch(r"rtsp://[A-Za-z0-9._:-]+(?::[0-9]{1,5})?/[A-Za-z0-9._-]+", remote) and not re.fullmatch(
        r"rtsp://\[[0-9A-Fa-f:]+\](?::[0-9]{1,5})?/[A-Za-z0-9._-]+",
        remote,
    ):
        raise SettingsError(f"refusing push target {remote_url!r}")
    path = remote.rsplit("/", 1)[-1]
    if not path.endswith(f"-{cam_id}"):
        raise SettingsError(f"push target for {cam_id} must end with -{cam_id}")
    local = f"rtsp://127.0.0.1:{int(local_port)}/{cam_id}"
    return [
        binary,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-rtsp_transport",
        "tcp",
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-i",
        local,
        "-an",
        "-c",
        "copy",
        "-muxdelay",
        "0",
        "-muxpreload",
        "0",
        "-rtsp_transport",
        "tcp",
        "-f",
        "rtsp",
        remote,
    ]


def camera_index(cam_id: str) -> int:
    if cam_id not in CAMERA_IDS:
        raise SettingsError(f"unknown camera {cam_id!r}")
    return int(cam_id[-1])
