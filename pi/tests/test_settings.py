"""Settings validation and the rpicam/ffmpeg argv builders."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pidentify.sensors import X264_PRESETS, X264_TUNES  # noqa: E402
from pidentify.settings import (  # noqa: E402
    SettingsError,
    build_autofocus_argv,
    build_ffmpeg_argv,
    build_record_argv,
    build_rpicam_argv,
    build_snapshot_argv,
    default_settings,
    libav_video_codec_opts,
    load_settings,
    merge_patch,
    public_settings,
    save_settings,
    select_encoder,
)

IMX708 = {"id": "cam0", "model": "imx708", "modes": "4608x2592 10-bit RGGB"}
IMX708_NOIR = {"id": "cam0", "model": "imx708_wide_noir", "modes": "4608x2592 10-bit RGGB"}
IMX477 = {"id": "cam0", "model": "imx477", "modes": "4056x3040 12-bit RGGB"}


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
        cfg["extra_args"] = ["--verbose"]
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
        self.assertIn("--verbose", argv)
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
        self.assertEqual(argv[argv.index("-probesize") + 1], "5000000")
        self.assertNotEqual(argv[argv.index("-analyzeduration") + 1], "0")
        self.assertEqual(argv[argv.index("-framerate") + 1], "30")

    def test_pi5_uses_libav_mpegts_and_pi4_keeps_hardware_h264(self):
        self.assertEqual(select_encoder("Raspberry Pi 5 Model B Rev 1.0"), "libav")
        self.assertEqual(select_encoder("Raspberry Pi Compute Module 5"), "libav")
        self.assertEqual(select_encoder("brcm,bcm2712"), "libav")
        self.assertEqual(select_encoder("Raspberry Pi 4 Model B Rev 1.5"), "hardware")
        self.assertEqual(select_encoder("Raspberry Pi Compute Module 4"), "hardware")
        self.assertEqual(select_encoder("Raspberry Pi Zero 2 W"), "hardware")
        cfg = default_settings()
        rpicam = build_rpicam_argv(0, cfg, binary="rpicam-vid", encoder="libav", low_latency=True)
        ffmpeg = build_ffmpeg_argv("cam0", cfg, rtsp_port=8554, binary="ffmpeg", container="mpegts")
        self.assertEqual(
            rpicam,
            [
                "rpicam-vid",
                "-t",
                "0",
                "-n",
                "--camera",
                "0",
                "--codec",
                "libav",
                "--libav-video-codec",
                "libx264",
                "--libav-format",
                "mpegts",
                "--libav-video-codec-opts",
                "preset=ultrafast;tune=zerolatency",
                "--width",
                "1280",
                "--height",
                "720",
                "--framerate",
                "30",
                "--bitrate",
                "2000000",
                "--intra",
                "30",
                "--flush",
                "--low-latency",
                "-o",
                "-",
            ],
        )
        self.assertNotIn("--inline", rpicam)
        self.assertEqual(ffmpeg[ffmpeg.index("-f") + 1], "mpegts")
        self.assertNotIn("-framerate", ffmpeg)
        self.assertEqual(ffmpeg[-1], "rtsp://127.0.0.1:8554/cam0")
        hardware = build_rpicam_argv(0, cfg, binary="rpicam-vid", encoder="hardware", low_latency=False)
        self.assertEqual(hardware[hardware.index("--codec") + 1], "h264")
        self.assertIn("--inline", hardware)
        self.assertNotIn("--libav-format", hardware)

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

    def test_imx708_quality_and_controls_build_a_libav_command(self):
        cfg = merge_patch(
            default_settings(),
            {
                "quality": "high",
                "denoise": "cdn_fast",
                "exposure": "short",
                "metering": "spot",
                "autofocus_mode": "continuous",
                "af_range": "normal",
                "af_speed": "fast",
                "af_window": {"x": 0.2, "y": 0.1, "w": 0.5, "h": 0.4},
                "shutter": 0,
                "gain": 0,
            },
            camera=IMX708,
        )
        self.assertEqual(cfg["width"], 1920)
        self.assertEqual(cfg["height"], 1080)
        self.assertEqual(cfg["fps"], 30)
        self.assertEqual(cfg["bitrate"], 5_000_000)
        self.assertEqual(cfg["x264_preset"], "ultrafast")
        self.assertEqual(cfg["x264_tune"], "zerolatency")
        self.assertEqual(cfg["keyframe_interval"], 30)
        argv = build_rpicam_argv(0, cfg, encoder="libav", low_latency=False)
        self.assertEqual(argv[argv.index("--libav-video-codec-opts") + 1], "preset=ultrafast;tune=zerolatency")
        self.assertEqual(argv[argv.index("--width") + 1], "1920")
        self.assertEqual(argv[argv.index("--intra") + 1], "30")
        self.assertEqual(argv[argv.index("--denoise") + 1], "cdn_fast")
        self.assertEqual(argv[argv.index("--exposure") + 1], "short")
        self.assertEqual(argv[argv.index("--metering") + 1], "spot")
        self.assertEqual(argv[argv.index("--autofocus-mode") + 1], "continuous")
        self.assertEqual(argv[argv.index("--autofocus-range") + 1], "normal")
        self.assertEqual(argv[argv.index("--autofocus-speed") + 1], "fast")
        self.assertEqual(argv[argv.index("--autofocus-window") + 1], "0.2,0.1,0.5,0.4")
        self.assertNotIn("--shutter", argv)
        self.assertNotIn("--gain", argv)
        self.assertEqual(argv[-2:], ["-o", "-"])

    def test_imx708_hdr_only_at_2304x1296_and_noir_awb(self):
        with self.assertRaises(SettingsError) as caught:
            merge_patch(default_settings(), {"hdr": "sensor"}, camera=IMX708)
        self.assertIn("2304x1296", caught.exception.fields["hdr"])
        hdr = merge_patch(
            default_settings(),
            {"width": 2304, "height": 1296, "fps": 30, "hdr": "sensor"},
            camera=IMX708,
        )
        self.assertEqual(hdr["hdr"], "sensor")
        with self.assertRaises(SettingsError) as caught:
            merge_patch(default_settings(), {"awb": "greyworld"}, camera=IMX708)
        self.assertIn("NoIR", caught.exception.fields["awb"])
        noir = merge_patch(default_settings(), {"awb": "greyworld"}, camera=IMX708_NOIR)
        self.assertEqual(noir["awb"], "greyworld")
        with self.assertRaises(SettingsError) as caught:
            merge_patch(default_settings(), {"width": 640, "height": 360}, camera=IMX708)
        self.assertIn("mode list", caught.exception.fields["width"])

    def test_imx477_rejects_autofocus_and_hdr(self):
        with self.assertRaises(SettingsError) as caught:
            merge_patch(default_settings(), {"autofocus_mode": "continuous"}, camera=IMX477)
        self.assertIn("not supported", caught.exception.fields["autofocus_mode"])
        self.assertIn("imx477", caught.exception.fields["autofocus_mode"])
        with self.assertRaises(SettingsError) as caught:
            merge_patch(default_settings(), {"hdr": "auto"}, camera=IMX477)
        self.assertIn("not supported", caught.exception.fields["hdr"])
        kept = merge_patch(
            default_settings(),
            {"width": 2028, "height": 1520, "fps": 30, "awb": "daylight", "awbgains": None},
            camera=IMX477,
        )
        self.assertEqual(kept["width"], 2028)
        self.assertIsNone(kept["autofocus_mode"])
        preset = merge_patch(default_settings(), {"quality": "max"}, camera=IMX477)
        self.assertEqual((preset["width"], preset["height"]), (2028, 1520))
        self.assertEqual(preset["bitrate"], 8_000_000)

    def test_awbgains_require_custom_and_still_autofocus_commands(self):
        with self.assertRaises(SettingsError) as caught:
            merge_patch(default_settings(), {"awbgains": [1.2, 1.8]}, camera=IMX708)
        self.assertIn("custom", caught.exception.fields["awbgains"])
        cfg = merge_patch(
            default_settings(),
            {"awb": "custom", "awbgains": [1.2, 1.8]},
            camera=IMX708,
        )
        argv = build_rpicam_argv(0, cfg, encoder="hardware")
        self.assertEqual(argv[argv.index("--awbgains") + 1], "1.2,1.8")
        self.assertNotIn("--libav-video-codec-opts", argv)
        still = build_snapshot_argv(0, cfg, width=4608, height=2592, jpeg_quality=90)
        self.assertIn("--quality", still)
        self.assertEqual(still[-2:], ["-o", "-"])
        self.assertNotIn("--hdr", still)
        focus = build_autofocus_argv(0, cfg)
        self.assertIn("--autofocus-on-capture", focus)
        self.assertIn("--metadata", focus)
        record = build_record_argv("cam0", 5, "/var/lib/pidentify/captures/cam0-20261025T101530Z.mp4")
        self.assertEqual(record[record.index("-c") + 1], "copy")
        self.assertTrue(record[-1].endswith(".mp4"))
        self.assertNotIn("rpicam", " ".join(record))

    def test_quality_presets_pass_semicolon_codec_opts_as_one_argv(self):
        expected = {
            "low": ("640", "480", "15", "800000", "15"),
            "medium": ("1280", "720", "30", "2000000", "30"),
            "high": ("1920", "1080", "30", "5000000", "30"),
        }
        for name, (width, height, fps, bitrate, intra) in expected.items():
            cfg = merge_patch(default_settings(), {"quality": name}, camera=IMX708)
            argv = build_rpicam_argv(0, cfg, binary="rpicam-vid", encoder="libav", low_latency=False)
            opts = "preset=ultrafast;tune=zerolatency"
            self.assertEqual(
                argv,
                [
                    "rpicam-vid",
                    "-t",
                    "0",
                    "-n",
                    "--camera",
                    "0",
                    "--codec",
                    "libav",
                    "--libav-video-codec",
                    "libx264",
                    "--libav-format",
                    "mpegts",
                    "--libav-video-codec-opts",
                    opts,
                    "--width",
                    width,
                    "--height",
                    height,
                    "--framerate",
                    fps,
                    "--bitrate",
                    bitrate,
                    "--intra",
                    intra,
                    "--flush",
                    "-o",
                    "-",
                ],
            )
            index = argv.index("--libav-video-codec-opts")
            self.assertEqual(argv[index + 1], opts)
            self.assertNotIn(":", argv[index + 1])
            self.assertEqual(argv.count(opts), 1)

    def test_codec_opts_join_every_known_preset_and_tune_on_semicolon(self):
        for preset in X264_PRESETS:
            for tune in X264_TUNES:
                opts = libav_video_codec_opts(preset, tune)
                self.assertEqual(opts, "preset={0};tune={1}".format(preset, tune))
                self.assertNotIn(":", opts)
        self.assertEqual(libav_video_codec_opts("ultrafast", None), "preset=ultrafast")
        self.assertEqual(libav_video_codec_opts("ultrafast", "not-a-tune"), "preset=ultrafast")
        self.assertEqual(libav_video_codec_opts("placebo", "zerolatency"), "tune=zerolatency")
        self.assertEqual(
            libav_video_codec_opts("ultrafast:tune=zerolatency", "zerolatency"),
            "tune=zerolatency",
        )
        self.assertIsNone(libav_video_codec_opts("placebo", "not-a-tune"))
        self.assertIsNone(libav_video_codec_opts(None, None))

    def test_rejected_preset_or_tune_still_builds_a_stream(self):
        bad_tune = dict(default_settings())
        bad_tune["x264_tune"] = "not-a-tune"
        argv = build_rpicam_argv(0, bad_tune, binary="rpicam-vid", encoder="libav", low_latency=False)
        self.assertEqual(argv[-2:], ["-o", "-"])
        self.assertEqual(argv[argv.index("--codec") + 1], "libav")
        opts = argv[argv.index("--libav-video-codec-opts") + 1]
        self.assertEqual(opts, "preset=ultrafast")
        self.assertNotIn(";", opts)
        self.assertNotIn(":", opts)

        bad_preset = dict(default_settings())
        bad_preset["x264_preset"] = "ultrafast:tune=zerolatency"
        argv = build_rpicam_argv(0, bad_preset, binary="rpicam-vid", encoder="libav", low_latency=False)
        self.assertEqual(argv[-2:], ["-o", "-"])
        self.assertEqual(
            argv[argv.index("--libav-video-codec-opts") + 1],
            "preset=ultrafast;tune=zerolatency",
        )

        both = dict(default_settings())
        both["x264_preset"] = "placebo"
        both["x264_tune"] = "stillimage"
        argv = build_rpicam_argv(0, both, binary="rpicam-vid", encoder="libav", low_latency=False)
        self.assertEqual(argv[-2:], ["-o", "-"])
        self.assertEqual(argv[argv.index("--libav-video-codec-opts") + 1], "preset=ultrafast")
        self.assertNotIn("--libav-video-codec-opts", build_rpicam_argv(0, both, encoder="hardware"))

        with self.assertRaises(SettingsError) as caught:
            merge_patch(default_settings(), {"x264_preset": "placebo"})
        self.assertIn("ultrafast", caught.exception.fields["x264_preset"])
        with self.assertRaises(SettingsError) as caught:
            merge_patch(default_settings(), {"x264_tune": "stillimage"})
        self.assertIn("zerolatency", caught.exception.fields["x264_tune"])
        omitted = merge_patch(default_settings(), {"x264_tune": None})
        self.assertIsNone(omitted["x264_tune"])
        argv = build_rpicam_argv(0, omitted, encoder="libav", low_latency=False)
        self.assertEqual(argv[argv.index("--libav-video-codec-opts") + 1], "preset=ultrafast")


if __name__ == "__main__":
    unittest.main()
