"""Settings validation and the rpicam/ffmpeg argv builders."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pidentify.settings import (  # noqa: E402
    SettingsError,
    build_ffmpeg_argv,
    build_rpicam_argv,
    default_settings,
    load_settings,
    merge_patch,
    public_settings,
    save_settings,
)


class SettingsTest(unittest.TestCase):
    def test_defaults_are_720p30(self):
        cfg = default_settings()
        self.assertEqual(cfg["width"], 1280)
        self.assertEqual(cfg["height"], 720)
        self.assertEqual(cfg["fps"], 30)
        self.assertEqual(cfg["bitrate"], 2_000_000)
        self.assertFalse(cfg["hflip"])
        self.assertEqual(cfg["rotation"], 0)

    def test_partial_update_keeps_other_fields(self):
        current = default_settings()
        updated = merge_patch(current, {"brightness": 0.2, "hflip": True})
        self.assertEqual(updated["brightness"], 0.2)
        self.assertTrue(updated["hflip"])
        self.assertEqual(updated["width"], 1280)
        self.assertIsNone(updated["contrast"])

    def test_null_clears_optional_control(self):
        current = merge_patch(default_settings(), {"ev": 1.5, "awb": "daylight"})
        cleared = merge_patch(current, {"ev": None})
        self.assertIsNone(cleared["ev"])
        self.assertEqual(cleared["awb"], "daylight")

    def test_rejects_unknown_and_bool_as_number(self):
        with self.assertRaises(SettingsError) as caught:
            merge_patch(default_settings(), {"brightness": True})
        self.assertIn("brightness", caught.exception.fields)
        with self.assertRaises(SettingsError):
            merge_patch(default_settings(), {"nope": 1})

    def test_rotation_only_0_or_180(self):
        with self.assertRaises(SettingsError):
            merge_patch(default_settings(), {"rotation": 90})
        self.assertEqual(merge_patch(default_settings(), {"rotation": 180})["rotation"], 180)

    def test_ranges_and_enums(self):
        with self.assertRaises(SettingsError):
            merge_patch(default_settings(), {"brightness": 2})
        with self.assertRaises(SettingsError):
            merge_patch(default_settings(), {"awb": "sunset"})
        with self.assertRaises(SettingsError):
            merge_patch(default_settings(), {"hdr": "always"})
        self.assertEqual(merge_patch(default_settings(), {"hdr": True})["hdr"], "auto")
        self.assertEqual(merge_patch(default_settings(), {"hdr": False})["hdr"], "off")
        self.assertEqual(merge_patch(default_settings(), {"lens_position": "default"})["lens_position"], "default")
        self.assertEqual(merge_patch(default_settings(), {"lens_position": 0})["lens_position"], 0.0)

    def test_http_cannot_set_extra_args(self):
        with self.assertRaises(SettingsError):
            merge_patch(default_settings(), {"extra_args": ["--denoise", "off"]})

    def test_rpicam_argv_is_a_list_and_ends_at_stdout(self):
        cfg = merge_patch(
            default_settings(),
            {
                "hflip": True,
                "vflip": True,
                "rotation": 180,
                "hdr": "sensor",
                "autofocus_mode": "manual",
                "lens_position": 2.5,
                "shutter": 8000,
                "gain": 1.5,
            },
        )
        cfg["extra_args"] = ["--denoise", "off"]
        argv = build_rpicam_argv(0, cfg, low_latency=True)
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[-2:], ["-o", "-"])
        self.assertIn("--codec", argv)
        self.assertEqual(argv[argv.index("--codec") + 1], "h264")
        self.assertIn("--inline", argv)
        self.assertIn("--flush", argv)
        self.assertIn("--low-latency", argv)
        self.assertIn("--hflip", argv)
        self.assertIn("--hdr", argv)
        self.assertEqual(argv[argv.index("--camera") + 1], "0")
        self.assertNotIn("--output", argv)
        joined = " ".join(argv)
        for bad in (";", "|", "&", "`", "$", "\n"):
            self.assertNotIn(bad, joined)

    def test_low_latency_is_optional(self):
        argv = build_rpicam_argv(1, default_settings(), low_latency=False)
        self.assertNotIn("--low-latency", argv)
        self.assertIn("--flush", argv)
        self.assertEqual(argv[argv.index("--camera") + 1], "1")

    def test_ffmpeg_publishes_tcp_rtsp_without_reencode(self):
        argv = build_ffmpeg_argv("cam0", default_settings(), rtsp_port=8554)
        self.assertIn("-c", argv)
        self.assertEqual(argv[argv.index("-c") + 1], "copy")
        self.assertIn("-rtsp_transport", argv)
        self.assertEqual(argv[argv.index("-rtsp_transport") + 1], "tcp")
        self.assertTrue(argv[-1].endswith("/cam0"))
        self.assertIn("127.0.0.1:8554", argv[-1])

    def test_round_trip_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "cam_settings.json")
            save_settings(path, {"cam0": merge_patch(default_settings(), {"contrast": 1.2})})
            loaded = load_settings(path)
            self.assertEqual(loaded["cam0"]["contrast"], 1.2)
            self.assertEqual(public_settings(loaded["cam0"])["fps"], 30)
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertIn("cam0", raw)

    def test_odd_resolution_rejected(self):
        with self.assertRaises(SettingsError):
            merge_patch(default_settings(), {"width": 1281})


if __name__ == "__main__":
    unittest.main()
