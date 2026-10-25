"""WirePlumber camera-monitor override for Bookworm 0.4 and Trixie 0.5."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pidentify.wireplumber import (  # noqa: E402
    choose_kind,
    pipewire_conflict_count,
    release,
)


class WirePlumberTest(unittest.TestCase):
    def test_version_picks_conf_or_lua(self):
        self.assertEqual(choose_kind("wireplumber 0.5.8"), "0.5")
        self.assertEqual(choose_kind("0.4.17-1"), "0.4")

    def test_release_writes_trixie_conf_and_restarts_the_desktop_user(self):
        report = {
            "conflicts": [
                {
                    "kind": "device",
                    "process": "wireplumber",
                    "cmdline": "/usr/bin/wireplumber",
                    "pid": 9,
                    "devices": ["/dev/video0", "/dev/media0"],
                }
            ]
        }
        self.assertEqual(pipewire_conflict_count(report), 1)
        calls = []

        def runner(argv):
            calls.append(argv)
            return 0

        with tempfile.TemporaryDirectory() as tmp:
            result = release(
                report,
                prefix=tmp,
                version_text="wireplumber 0.5.8",
                restart=True,
                sessions=[("pi", 1000)],
                runner=runner,
                log_path=str(Path(tmp) / "takeover.log"),
            )
            path = Path(tmp) / "etc" / "wireplumber" / "wireplumber.conf.d" / "50-pidentify-no-camera.conf"
            self.assertEqual(result["path"], str(path))
            text = path.read_text(encoding="utf-8")
            self.assertIn("monitor.libcamera = disabled", text)
            self.assertIn("monitor.v4l2 = disabled", text)
            self.assertFalse((Path(tmp) / "etc" / "wireplumber" / "main.lua.d").exists())
            self.assertEqual(calls[0][:5], ["systemctl", "--user", "-M", "pi@", "restart"])
            self.assertIn("wireplumber", calls[0])
            self.assertIn("pipewire", calls[0])
            self.assertIn("pipewire-pulse", calls[0])
            self.assertNotIn("disable", " ".join(calls[0]))
            log = (Path(tmp) / "takeover.log").read_text(encoding="utf-8")
            self.assertIn("50-pidentify-no-camera.conf", log)

    def test_release_writes_bookworm_lua(self):
        report = {
            "conflicts": [
                {
                    "kind": "device",
                    "process": "pipewire",
                    "cmdline": "/usr/bin/pipewire",
                    "pid": 4,
                    "devices": ["/dev/video1"],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            result = release(
                report,
                prefix=tmp,
                version_text="0.4.13",
                restart=False,
                log_path="",
            )
            path = Path(result["path"])
            self.assertTrue(str(path).endswith("51-pidentify-no-camera.lua"))
            text = path.read_text(encoding="utf-8")
            self.assertIn("libcamera_monitor.enabled = false", text)
            self.assertIn("v4l2_monitor.enabled = false", text)
            self.assertFalse(Path(tmp).joinpath("etc/wireplumber/wireplumber.conf.d").exists())

    def test_release_ignores_unrelated_conflicts(self):
        report = {"conflicts": [{"kind": "device", "process": "python3", "cmdline": "app_full.py", "pid": 3}]}
        with tempfile.TemporaryDirectory() as tmp:
            result = release(report, prefix=tmp, version_text="0.5.8", restart=False)
            self.assertFalse(result["changed"])
            self.assertEqual(list(Path(tmp).rglob("*")), [])


if __name__ == "__main__":
    unittest.main()
