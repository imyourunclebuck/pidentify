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

from pidentify.sensors import (
    AF_MODES,
    AF_RANGES,
    AF_SPEEDS,
    AWB_MODES,
    DENOISE_MODES,
    EXPOSURE_MODES,
    HDR_MODES,
    METERING_MODES,
    QUALITY_NAMES,
    X264_PRESETS,
    X264_TUNES,
    quality_preset,
    sensor_errors,
)

CAMERA_IDS = ("cam0", "cam1")

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
    "--denoise",
    "--exposure",
    "--metering",
    "--awbgains",
    "--autofocus-range",
    "--autofocus-speed",
    "--autofocus-window",
    "--quality",
    "--libav-format",
    "--libav-video-codec",
    "--libav-video-codec-opts",
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
    "quality",
    "keyframe_interval",
    "denoise",
    "exposure",
    "metering",
    "awbgains",
    "af_range",
    "af_speed",
    "af_window",
)
REQUIRED_KEYS = (
    "width",
    "height",
    "fps",
    "bitrate",
    "hflip",
    "vflip",
    "rotation",
    "x264_preset",
    "x264_tune",
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
        "quality": None,
        "keyframe_interval": None,
        "x264_preset": "ultrafast",
        "x264_tune": "zerolatency",
        "denoise": None,
        "exposure": None,
        "metering": None,
        "awbgains": None,
        "af_range": None,
        "af_speed": None,
        "af_window": None,
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


def normalize_settings(
    data: dict[str, Any],
    *,
    base: dict[str, Any] | None = None,
    camera: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate a full settings object. Unknown keys are rejected.

    ``camera`` is the cameras.json record. When it is present, resolution,
    HDR, NoIR white balance, and autofocus are checked against that sensor.
    Loading the file without a camera record still checks types and ranges.
    """
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
    out["shutter"] = _optional_int(merged["shutter"], "shutter", low=0, high=10_000_000)
    out["gain"] = _optional_number(merged["gain"], "gain", low=0.0, high=64.0)
    out["autofocus_mode"] = _optional_choice(merged["autofocus_mode"], "autofocus_mode", AF_MODES)
    out["lens_position"] = _optional_lens(merged["lens_position"])
    out["hdr"] = None if merged["hdr"] is None else _optional_hdr(merged["hdr"])
    out["quality"] = _optional_choice(merged["quality"], "quality", QUALITY_NAMES)
    out["keyframe_interval"] = _optional_int(merged["keyframe_interval"], "keyframe_interval", low=1, high=300)
    out["x264_preset"] = _required_choice(merged["x264_preset"], "x264_preset", X264_PRESETS)
    out["x264_tune"] = _required_choice(merged["x264_tune"], "x264_tune", X264_TUNES)
    out["denoise"] = _optional_choice(merged["denoise"], "denoise", DENOISE_MODES)
    out["exposure"] = _optional_choice(merged["exposure"], "exposure", EXPOSURE_MODES)
    out["metering"] = _optional_choice(merged["metering"], "metering", METERING_MODES)
    out["awbgains"] = _optional_gains(merged["awbgains"])
    out["af_range"] = _optional_choice(merged["af_range"], "af_range", AF_RANGES)
    out["af_speed"] = _optional_choice(merged["af_speed"], "af_speed", AF_SPEEDS)
    out["af_window"] = _optional_window(merged["af_window"])
    if out["awb"] != "custom":
        out["awbgains"] = None
    if "extra_args" in data or base is not None:
        out["extra_args"] = validate_extra_args(merged.get("extra_args") or [])
    else:
        out["extra_args"] = []
    sensor = sensor_errors(out, camera)
    if sensor:
        raise SettingsError("invalid settings", sensor)
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
        raise SettingsError("invalid settings", {name: "expected one of {0}".format(", ".join(choices))})
    return value


def _required_choice(value: Any, name: str, choices: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise SettingsError("invalid settings", {name: "expected one of {0}".format(", ".join(choices))})
    return value


def _optional_gains(value: Any) -> list[float] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise SettingsError("invalid settings", {"awbgains": "expected [red, blue]"})
    red = float(_number(value[0], "awbgains", low=0.0, high=32.0))
    blue = float(_number(value[1], "awbgains", low=0.0, high=32.0))
    return [red, blue]


def _optional_window(value: Any) -> dict[str, float] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SettingsError("invalid settings", {"af_window": "expected {x, y, w, h} in normalized 0..1 coordinates"})
    missing = [key for key in ("x", "y", "w", "h") if key not in value]
    if missing or set(value) - {"x", "y", "w", "h"}:
        raise SettingsError("invalid settings", {"af_window": "expected x, y, w, and h"})
    window = {}
    for key in ("x", "y", "w", "h"):
        window[key] = float(_number(value[key], "af_window", low=0.0, high=1.0))
    if window["w"] <= 0 or window["h"] <= 0:
        raise SettingsError("invalid settings", {"af_window": "w and h must be greater than 0"})
    if window["x"] + window["w"] > 1.0001 or window["y"] + window["h"] > 1.0001:
        raise SettingsError("invalid settings", {"af_window": "x+w and y+h must be at most 1"})
    return window


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


def merge_patch(
    current: dict[str, Any],
    patch: dict[str, Any],
    *,
    camera: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply a partial update. Null clears an optional control back to default.

    ``quality`` of low, medium, high, or max fills in the Pi 5 encode preset
    for this sensor. Other keys in the same patch override those fields.
    """
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
    applied = dict(patch)
    if "awbgains" in applied and applied["awbgains"] is not None:
        awb = applied["awb"] if "awb" in applied else current.get("awb")
        if awb != "custom":
            raise SettingsError(
                "invalid settings",
                {"awbgains": "set awb to custom before awbgains"},
            )
    quality = applied.get("quality")
    if quality:
        model = str((camera or {}).get("model") or "")
        preset = quality_preset(str(quality), model)
        if preset is None:
            raise SettingsError(
                "invalid settings",
                {"quality": "expected one of {0}".format(", ".join(QUALITY_NAMES))},
            )
        expanded = dict(preset)
        expanded.update(applied)
        applied = expanded
    merged = dict(current)
    merged.update(applied)
    return normalize_settings(merged, base=current, camera=camera)


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


# ffmpeg's default probe is 5 MB / 5 s. The previous 32-byte probe returned
# "unspecified size" before it had seen an SPS. One second of 720p30 at
# 2 Mbps fits in 5 MB, and runOnDemandStartTimeout is 20 s.
FFMPEG_PROBE_SIZE = "5000000"
FFMPEG_ANALYZE_DURATION = "2000000"


def read_board_text(model_path: str = "/proc/device-tree/model", compatible_path: str = "/proc/device-tree/compatible") -> str:
    """Board strings used to decide between the hardware encoder and libav."""
    override = os.environ.get("PIDENTIFY_PI_MODEL", "").strip()
    if override:
        return override
    parts: list[str] = []
    for path in (model_path, compatible_path):
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except OSError:
            continue
        text = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
        if text:
            parts.append(text)
    return " ".join(parts)


def select_encoder(board_text: str) -> str:
    """Return ``libav`` on Pi 5 (no H.264 block) and ``hardware`` on Pi 4 and earlier.

    ``PIDENTIFY_ENCODER=libav|hardware`` overrides detection. An unrecognised
    board uses libav: that path works on Pi 5, and it still produces H.264 on
    a Pi 4. Hardware mode is only selected when the board text says the SoC
    has the old H.264 encoder.
    """
    override = os.environ.get("PIDENTIFY_ENCODER", "").strip().lower()
    if override in {"libav", "hardware"}:
        return override
    text = (board_text or "").lower()
    if any(marker in text for marker in ("raspberry pi 5", "compute module 5", "bcm2712")):
        return "libav"
    if any(
        marker in text
        for marker in (
            "raspberry pi 4",
            "raspberry pi 400",
            "compute module 4",
            "raspberry pi 3",
            "raspberry pi zero 2",
            "bcm2711",
            "bcm2837",
            "bcm2836",
            "bcm2835",
        )
    ):
        return "hardware"
    return "libav"


def build_rpicam_argv(
    camera_index: int,
    settings: dict[str, Any],
    *,
    binary: str = "rpicam-vid",
    low_latency: bool = False,
    encoder: str = "hardware",
) -> list[str]:
    """H.264 on stdout. No shell involved.

    ``hardware`` is ``--codec h264`` (Pi 4 and earlier) and an elementary
    stream. ``libav`` is software x264 in MPEG-TS (Pi 5). On Pi 5, plain
    ``--codec h264`` still enters the libav encoder with no container, and
    ``-o -`` then fails with "try setting with --libav-format".
    """
    if encoder not in {"hardware", "libav"}:
        raise SettingsError(f"unsupported encoder {encoder!r}")
    cfg = normalize_settings(settings, base=settings)
    argv = [
        binary,
        "-t",
        "0",
        "-n",
        "--camera",
        str(camera_index),
    ]
    if encoder == "libav":
        argv += [
            "--codec",
            "libav",
            "--libav-video-codec",
            "libx264",
            "--libav-format",
            "mpegts",
        ]
    else:
        argv += ["--codec", "h264"]
    if encoder == "libav":
        argv += [
            "--libav-video-codec-opts",
            "preset={preset}:tune={tune}".format(
                preset=cfg["x264_preset"],
                tune=cfg["x264_tune"],
            ),
        ]
    gop = cfg["keyframe_interval"] if cfg["keyframe_interval"] is not None else cfg["fps"]
    argv += [
        "--width",
        str(cfg["width"]),
        "--height",
        str(cfg["height"]),
        "--framerate",
        str(cfg["fps"]),
        "--bitrate",
        str(cfg["bitrate"]),
        "--intra",
        str(gop),
    ]
    if encoder == "hardware":
        # Repeat SPS/PPS for a raw elementary stream. MPEG-TS carries its own.
        argv.append("--inline")
    argv.append("--flush")
    if low_latency:
        argv.append("--low-latency")
    argv += _image_args(cfg)
    argv += list(cfg["extra_args"])
    argv += ["-o", "-"]
    return argv


def _image_args(cfg: dict[str, Any]) -> list[str]:
    """rpicam image, denoise, HDR, and autofocus flags shared by vid and still."""
    argv: list[str] = []
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
    if cfg["exposure"] is not None:
        argv += ["--exposure", cfg["exposure"]]
    if cfg["metering"] is not None:
        argv += ["--metering", cfg["metering"]]
    if cfg["awb"] is not None:
        argv += ["--awb", cfg["awb"]]
    if cfg["awbgains"]:
        argv += [
            "--awbgains",
            "{red},{blue}".format(red=_fmt(cfg["awbgains"][0]), blue=_fmt(cfg["awbgains"][1])),
        ]
    if cfg["shutter"] not in (None, 0):
        argv += ["--shutter", str(cfg["shutter"])]
    if cfg["gain"] not in (None, 0, 0.0):
        argv += ["--gain", _fmt(cfg["gain"])]
    if cfg["denoise"] is not None:
        argv += ["--denoise", cfg["denoise"]]
    if cfg["hdr"] not in (None, "off"):
        argv += ["--hdr", cfg["hdr"]]
    if cfg["autofocus_mode"] is not None:
        argv += ["--autofocus-mode", cfg["autofocus_mode"]]
    if cfg["af_range"] is not None:
        argv += ["--autofocus-range", cfg["af_range"]]
    if cfg["af_speed"] is not None:
        argv += ["--autofocus-speed", cfg["af_speed"]]
    if cfg["af_window"] is not None:
        window = cfg["af_window"]
        argv += [
            "--autofocus-window",
            "{x},{y},{w},{h}".format(
                x=_fmt(window["x"]),
                y=_fmt(window["y"]),
                w=_fmt(window["w"]),
                h=_fmt(window["h"]),
            ),
        ]
    if cfg["lens_position"] is not None:
        shown = cfg["lens_position"] if isinstance(cfg["lens_position"], str) else _fmt(cfg["lens_position"])
        argv += ["--lens-position", shown]
    if cfg["hflip"]:
        argv.append("--hflip")
    if cfg["vflip"]:
        argv.append("--vflip")
    if cfg["rotation"]:
        argv += ["--rotation", str(cfg["rotation"])]
    return argv


def build_snapshot_argv(
    camera_index: int,
    settings: dict[str, Any],
    *,
    width: int,
    height: int,
    jpeg_quality: int = 90,
    binary: str = "rpicam-still",
) -> list[str]:
    """One JPEG on stdout. The caller must already have released the live publisher."""
    cfg = normalize_settings(settings, base=settings)
    cfg["width"] = int(width)
    cfg["height"] = int(height)
    # HDR on imx708 is only the 2304x1296 mode. A full-resolution still keeps
    # the other image controls and leaves HDR off when the size cannot use it.
    if (int(width), int(height)) != (2304, 1296):
        cfg["hdr"] = "off"
    if not 1 <= int(jpeg_quality) <= 100:
        raise SettingsError("invalid settings", {"jpeg_quality": "expected 1..100"})
    argv = [
        binary,
        "-n",
        "-t",
        "1000",
        "--camera",
        str(camera_index),
        "--width",
        str(int(width)),
        "--height",
        str(int(height)),
        "--quality",
        str(int(jpeg_quality)),
        "--immediate",
    ]
    argv += _image_args(cfg)
    argv += ["-o", "-"]
    return argv


def build_autofocus_argv(
    camera_index: int,
    settings: dict[str, Any],
    *,
    binary: str = "rpicam-still",
) -> list[str]:
    """One-shot AF. Metadata JSON on stdout, image discarded.

    ``--autofocus-on-capture`` runs a single cycle. The caller parses
    ``LensPosition`` from the metadata and restarts the live publisher.
    """
    cfg = normalize_settings(settings, base=settings)
    argv = [
        binary,
        "-n",
        "-t",
        "4000",
        "--camera",
        str(camera_index),
        "--autofocus-mode",
        "auto",
        "--autofocus-on-capture",
        "--width",
        "1280",
        "--height",
        "720",
    ]
    if cfg["af_range"] is not None:
        argv += ["--autofocus-range", cfg["af_range"]]
    if cfg["af_speed"] is not None:
        argv += ["--autofocus-speed", cfg["af_speed"]]
    if cfg["af_window"] is not None:
        window = cfg["af_window"]
        argv += [
            "--autofocus-window",
            "{x},{y},{w},{h}".format(
                x=_fmt(window["x"]),
                y=_fmt(window["y"]),
                w=_fmt(window["w"]),
                h=_fmt(window["h"]),
            ),
        ]
    argv += ["--metadata", "-", "-o", "/dev/null"]
    return argv


def build_record_argv(
    cam_id: str,
    seconds: int,
    dest: str,
    *,
    local_port: int = 8554,
    binary: str = "ffmpeg",
) -> list[str]:
    """Copy the live RTSP path into an MP4. Does not open the camera itself."""
    if cam_id not in CAMERA_IDS:
        raise SettingsError("refusing to record unexpected path {0}".format(cam_id))
    if not 1 <= int(seconds) <= 120:
        raise SettingsError("invalid settings", {"seconds": "expected 1..120"})
    if not dest or os.path.basename(dest) != dest and not dest.startswith("/"):
        raise SettingsError("refusing record path")
    return [
        binary,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-rtsp_transport",
        "tcp",
        "-i",
        "rtsp://127.0.0.1:{0}/{1}".format(int(local_port), cam_id),
        "-t",
        str(int(seconds)),
        "-an",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        dest,
    ]


def build_preview_argv(
    cam_id: str,
    *,
    local_port: int = 8554,
    binary: str = "ffmpeg",
) -> list[str]:
    """One JPEG frame from the live RTSP path, on stdout."""
    if cam_id not in CAMERA_IDS:
        raise SettingsError("refusing to preview unexpected path {0}".format(cam_id))
    return [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-rtsp_transport",
        "tcp",
        "-i",
        "rtsp://127.0.0.1:{0}/{1}".format(int(local_port), cam_id),
        "-frames:v",
        "1",
        "-f",
        "image2",
        "-update",
        "1",
        "pipe:1",
    ]


def build_ffmpeg_argv(
    cam_id: str,
    settings: dict[str, Any],
    *,
    rtsp_port: int = 8554,
    binary: str = "ffmpeg",
    container: str = "h264",
) -> list[str]:
    """Copy rpicam-vid's stdout into RTSP/TCP without re-encoding.

    ``container`` is ``mpegts`` for the Pi 5 libav path and ``h264`` for the
    hardware elementary stream. The elementary stream has no timestamps, so
    that path keeps wallclock timestamps and an explicit ``-framerate``.
    """
    cfg = normalize_settings(settings, base=settings)
    if not re.fullmatch(r"cam[01]", cam_id):
        raise SettingsError(f"refusing to publish unexpected path {cam_id!r}")
    if not 1 <= int(rtsp_port) <= 65535:
        raise SettingsError(f"bad RTSP port {rtsp_port!r}")
    if container not in {"h264", "mpegts"}:
        raise SettingsError(f"unsupported container {container!r}")
    argv = [
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
        FFMPEG_PROBE_SIZE,
        "-analyzeduration",
        FFMPEG_ANALYZE_DURATION,
    ]
    if container == "h264":
        argv += [
            "-use_wallclock_as_timestamps",
            "1",
            "-f",
            "h264",
            "-framerate",
            str(cfg["fps"]),
        ]
    else:
        argv += ["-f", "mpegts"]
    argv += [
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
    return argv


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
        "-probesize",
        FFMPEG_PROBE_SIZE,
        "-analyzeduration",
        FFMPEG_ANALYZE_DURATION,
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
