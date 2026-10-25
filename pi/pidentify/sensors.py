"""What each detected sensor can actually do.

Resolutions come from ``rpicam-hello --list-cameras`` plus the scaled sizes
rpicam-vid will produce from those modes (720p is not a native IMX708 mode,
but it is the stream we ship). Quality presets are sized for a Pi 5 doing
x264 in software: full-sensor sizes stay available as manual resolutions and
are not the "max" preset.
"""

from __future__ import annotations

import re

AWB_MODES = (
    "auto",
    "incandescent",
    "tungsten",
    "fluorescent",
    "indoor",
    "daylight",
    "cloudy",
    "custom",
    "greyworld",
)
AF_MODES = ("default", "manual", "auto", "continuous")
AF_RANGES = ("normal", "macro", "full")
AF_SPEEDS = ("normal", "fast")
HDR_MODES = ("off", "auto", "sensor", "single-exp")
DENOISE_MODES = ("auto", "off", "cdn_off", "cdn_fast", "cdn_hq")
EXPOSURE_MODES = ("normal", "short", "long")
METERING_MODES = ("centre", "spot", "average", "matrix")
X264_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium")
X264_TUNES = ("zerolatency", "film", "animation", "grain", "fastdecode")
QUALITY_NAMES = ("low", "medium", "high", "max")

# width, height, max_fps. max_fps is the sensor mode we scale from, not a
# promise that software x264 will hold that rate.
_IMX708 = (
    (640, 480, 120.0),
    (1280, 720, 120.0),
    (1536, 864, 120.0),
    (1920, 1080, 60.0),
    (2304, 1296, 56.0),
    (4608, 2592, 14.0),
)
_IMX477 = (
    (640, 480, 120.0),
    (1280, 720, 60.0),
    (1332, 990, 120.0),
    (1920, 1080, 50.0),
    (2028, 1080, 50.0),
    (2028, 1520, 40.0),
    (4056, 3040, 10.0),
)
_FAMILY_RESOLUTIONS = {"imx708": _IMX708, "imx477": _IMX477}
_COMMON_SCALED = (
    (640, 480, 60.0),
    (1280, 720, 60.0),
    (1920, 1080, 30.0),
)
_HDR_RESOLUTIONS = {"imx708": ((2304, 1296),)}
_LENS_RANGE = {"imx708": (0.0, 10.0)}

# Pi 5 software encode. "max" stays at a size that can still approach 30 fps
# in ultrafast/zerolatency; the full sensor readout is a manual resolution.
_PRESET_FIELDS = (
    "width",
    "height",
    "fps",
    "bitrate",
    "x264_preset",
    "x264_tune",
    "keyframe_interval",
)
_PRESETS = {
    "imx708": {
        "low": (640, 480, 15, 800_000, "ultrafast", "zerolatency", 15),
        "medium": (1280, 720, 30, 2_000_000, "ultrafast", "zerolatency", 30),
        "high": (1920, 1080, 30, 5_000_000, "ultrafast", "zerolatency", 30),
        "max": (1920, 1080, 30, 8_000_000, "ultrafast", "zerolatency", 30),
    },
    "imx477": {
        "low": (1280, 720, 15, 800_000, "ultrafast", "zerolatency", 15),
        "medium": (1280, 720, 30, 2_000_000, "ultrafast", "zerolatency", 30),
        "high": (2028, 1080, 30, 6_000_000, "ultrafast", "zerolatency", 30),
        "max": (2028, 1520, 30, 8_000_000, "ultrafast", "zerolatency", 30),
    },
    "default": {
        "low": (640, 480, 15, 800_000, "ultrafast", "zerolatency", 15),
        "medium": (1280, 720, 30, 2_000_000, "ultrafast", "zerolatency", 30),
        "high": (1920, 1080, 30, 5_000_000, "ultrafast", "zerolatency", 30),
        "max": (1920, 1080, 30, 8_000_000, "ultrafast", "zerolatency", 30),
    },
}

_SIZE = re.compile(r"(?<![\d/])(\d{2,5})x(\d{2,5})")
_MODE_LINE = re.compile(
    r"(?<![\d/])(\d{2,5})x(\d{2,5})(?:\s*\[([0-9.]+)\s*fps)?"
)
HDR_NOTE = (
    "HDR on imx708 restarts the camera and only works at 2304x1296. "
    "Other resolutions must set hdr to off."
)


def sensor_family(model: str | None) -> str | None:
    text = (model or "").lower().replace("-", "_")
    if "imx708" in text:
        return "imx708"
    if "imx477" in text:
        return "imx477"
    return None


def is_noir(model: str | None) -> bool:
    return "noir" in (model or "").lower()


def autofocus_supported(model: str | None) -> bool:
    """Camera Module 3 variants (imx708, including wide and NoIR) have PDAF."""
    return sensor_family(model) == "imx708"


def parse_mode_line(line: str) -> tuple[int, int, float | None] | None:
    """Pull the mode size off one ``--list-cameras`` detail line.

    The crop that follows, such as ``(0, 0)/4608x2592``, is not a mode.
    """
    match = _MODE_LINE.search(line)
    if not match:
        return None
    fps = float(match.group(3)) if match.group(3) else None
    return int(match.group(1)), int(match.group(2)), fps


def quality_preset(name: str, model: str | None) -> dict[str, object] | None:
    family = sensor_family(model) or "default"
    row = _PRESETS.get(family, _PRESETS["default"]).get(name)
    if row is None:
        return None
    return dict(zip(_PRESET_FIELDS, row))


def _sensor_max(camera: dict) -> tuple[int, int]:
    widths = []
    heights = []
    for match in _SIZE.finditer(str(camera.get("modes") or "")):
        widths.append(int(match.group(1)))
        heights.append(int(match.group(2)))
    for item in camera.get("resolutions") or []:
        if isinstance(item, dict) and item.get("width") and item.get("height"):
            widths.append(int(item["width"]))
            heights.append(int(item["height"]))
    if not widths:
        return 0, 0
    return max(widths), max(heights)


def allowed_resolutions(camera: dict | None) -> list[dict[str, object]]:
    """Sizes the UI may offer. Empty means any even size in the numeric range."""
    if not camera:
        return []
    family = sensor_family(str(camera.get("model") or ""))
    detected: dict[tuple[int, int], float | None] = {}
    for item in camera.get("resolutions") or []:
        if not isinstance(item, dict):
            continue
        try:
            key = (int(item["width"]), int(item["height"]))
        except (KeyError, TypeError, ValueError):
            continue
        fps = item.get("max_fps")
        if isinstance(fps, (int, float)) and not isinstance(fps, bool) and fps > 0:
            detected[key] = float(fps)
        else:
            detected[key] = None
    max_w, max_h = _sensor_max(camera)
    if not family and not detected and not max_w:
        return []
    merged: dict[tuple[int, int], float] = {}
    table = _FAMILY_RESOLUTIONS.get(family or "", ())
    for width, height, fps in table:
        if max_w and (width > max_w or height > max_h):
            continue
        merged[(width, height)] = fps
    if not table:
        for width, height, fps in _COMMON_SCALED:
            if max_w and (width > max_w or height > max_h):
                continue
            if max_w:
                merged[(width, height)] = fps
    for key, fps in detected.items():
        if max_w and (key[0] > max_w or key[1] > max_h):
            continue
        if fps is not None:
            merged[key] = fps
        else:
            merged.setdefault(key, 120.0)
    if not merged:
        return []
    ordered = sorted(merged, key=lambda item: (item[0] * item[1], item[0]))
    return [
        {"width": width, "height": height, "max_fps": merged[(width, height)]}
        for width, height in ordered
    ]


def _resolution_index(camera: dict | None) -> dict[tuple[int, int], float]:
    return {
        (int(item["width"]), int(item["height"])): float(item["max_fps"])
        for item in allowed_resolutions(camera)
    }


def sensor_errors(cfg: dict, camera: dict | None) -> dict[str, str]:
    """Field errors for values the detected sensor cannot do. Empty if ok."""
    if not camera:
        return {}
    model = str(camera.get("model") or "this sensor")
    errors: dict[str, str] = {}
    allowed = _resolution_index(camera)
    if allowed:
        key = (int(cfg["width"]), int(cfg["height"]))
        if key not in allowed:
            listed = ", ".join("{0}x{1}".format(w, h) for w, h in allowed)
            errors["width"] = "resolution {0}x{1} is not in this sensor's mode list ({2})".format(
                cfg["width"], cfg["height"], listed
            )
        elif int(cfg["fps"]) > allowed[key]:
            errors["fps"] = "{0}x{1} on {2} supports up to {3:g} fps".format(
                key[0], key[1], model, allowed[key]
            )
    family = sensor_family(model)
    hdr = cfg.get("hdr")
    if hdr not in (None, "off"):
        if family != "imx708":
            errors["hdr"] = "HDR is not supported on {0}".format(model)
        else:
            hdr_sizes = _HDR_RESOLUTIONS["imx708"]
            if (int(cfg["width"]), int(cfg["height"])) not in hdr_sizes:
                errors["hdr"] = HDR_NOTE
    if cfg.get("awb") == "greyworld" and not is_noir(model):
        errors["awb"] = "greyworld AWB is only for NoIR cameras ({0} is not NoIR)".format(model)
    if not autofocus_supported(model):
        note = "autofocus is not supported on {0}".format(model)
        for field in ("autofocus_mode", "af_range", "af_speed", "af_window", "lens_position"):
            if cfg.get(field) is not None:
                errors[field] = note
    else:
        lens = cfg.get("lens_position")
        low, high = _LENS_RANGE.get(family or "", (0.0, 10.0))
        if isinstance(lens, (int, float)) and not isinstance(lens, bool) and (lens < low or lens > high):
            errors["lens_position"] = "lens position for {0} must be between {1:g} and {2:g} dioptres".format(
                model, low, high
            )
    return errors


def capabilities(camera: dict | None, *, encoder: str) -> dict:
    model = str((camera or {}).get("model") or "")
    family = sensor_family(model)
    noir = is_noir(model)
    af = autofocus_supported(model)
    resolutions = allowed_resolutions(camera)
    awb = [mode for mode in AWB_MODES if mode != "greyworld" or noir]
    lens = None
    af_note = None
    if af:
        low, high = _LENS_RANGE.get(family or "", (0.0, 10.0))
        lens = {"min": low, "max": high, "unit": "dioptres", "zero": "infinity"}
    else:
        af_note = "autofocus is not supported on {0}".format(model or "this sensor")
    hdr_supported = family == "imx708"
    return {
        "model": model or None,
        "family": family,
        "noir": noir,
        "resolutions": resolutions,
        "resolution_constrained": bool(resolutions),
        "fps": {"min": 1, "max": 120},
        "bitrate": {"min": 100000, "max": 25000000},
        "quality": list(QUALITY_NAMES),
        "x264_preset": list(X264_PRESETS),
        "x264_tune": list(X264_TUNES),
        "x264_applies": encoder == "libav",
        "x264_note": (
            "x264 preset and tune are passed to libav on Pi 5. "
            "A Pi 4 hardware encoder ignores them."
        ),
        "keyframe_interval": {"min": 1, "max": 300, "empty": "same as fps"},
        "hdr": {
            "supported": hdr_supported,
            "values": list(HDR_MODES),
            "resolutions": [
                {"width": width, "height": height}
                for width, height in _HDR_RESOLUTIONS.get(family or "", ())
            ],
            "note": HDR_NOTE if hdr_supported else "HDR is not supported on {0}".format(model or "this sensor"),
        },
        "denoise": list(DENOISE_MODES),
        "rotation": [0, 180],
        "exposure": list(EXPOSURE_MODES),
        "shutter_us": {"min": 0, "max": 10000000, "auto": 0},
        "gain": {"min": 0, "max": 64, "auto": 0},
        "ev": {"min": -10, "max": 10},
        "metering": list(METERING_MODES),
        "awb": awb,
        "awbgains": {
            "min": 0,
            "max": 32,
            "note": "Two numbers, red then blue. Only used when awb is custom.",
        },
        "brightness": {"min": -1, "max": 1},
        "contrast": {"min": 0, "max": 32},
        "saturation": {"min": 0, "max": 32},
        "sharpness": {"min": 0, "max": 16},
        "autofocus": {
            "supported": af,
            "note": af_note,
            "mode": ["manual", "auto", "continuous"],
            "range": list(AF_RANGES),
            "speed": list(AF_SPEEDS),
            "window": "normalized x, y, w, h, each 0..1, x+w and y+h at most 1",
            "lens_position": lens,
        },
        "encoder": encoder,
        "snapshot_note": (
            "A full-resolution snapshot stops the live publisher, runs rpicam-still, "
            "then restarts that camera only. The stream is interrupted for about 1-3 seconds."
        ),
        "record_note": (
            "Record copies the live RTSP feed to an MP4 and does not open the camera a second time. "
            "An idle on-demand path starts for the length of the clip."
        ),
    }
