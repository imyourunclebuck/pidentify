"""Configuration writer, dry-run installer, and the config.txt safety check."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pidentify.configure import configure_node  # noqa: E402
from pidentify.settings import default_settings, merge_patch, save_settings  # noqa: E402

PI = Path(__file__).resolve().parents[1]
REPO = PI.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class ConfigureTest(unittest.TestCase):
    def test_writes_paths_for_detected_cameras_and_preserves_settings(self):
        root = Path(self.id().replace(".", "_") + "_cfg")
        if root.exists():
            import shutil

            shutil.rmtree(root)
        root.mkdir()
        try:
            config = root / "etc" / "pidentify"
            state = root / "var" / "lib" / "pidentify"
            config.mkdir(parents=True)
            save_settings(
                str(config / "cam_settings.json"),
                {"cam0": merge_patch(default_settings(), {"brightness": 0.3, "fps": 25})},
            )
            os.environ["PIDENTIFY_LIST_CAMERAS_FILE"] = str(FIXTURES / "two_cameras.txt")
            summary = configure_node(
                config_dir=str(config),
                state_dir=str(state),
                avahi_conf=str(root / "etc" / "avahi" / "avahi-daemon.conf"),
                avahi_service=str(root / "etc" / "avahi" / "services" / "pidentify.service"),
                avahi_backup=str(state / "avahi-daemon.conf.bak"),
                publish_cmd="/usr/local/bin/pidentify-publish",
                hostname="pi5_cam_1_myrtle",
                hostname_file=None,
            )
            self.assertEqual(summary["paths"], ["cam0", "cam1"])
            self.assertEqual(summary["mdns_fqdn"], "pi5-cam-1-myrtle.local")
            self.assertEqual(summary["hostname"], "pi5_cam_1_myrtle")
            settings = json.loads((config / "cam_settings.json").read_text(encoding="utf-8"))
            self.assertEqual(settings["cam0"]["brightness"], 0.3)
            self.assertEqual(settings["cam0"]["fps"], 25)
            self.assertIn("cam1", settings)
            yml = (config / "mediamtx.yml").read_text(encoding="utf-8")
            self.assertIn("cam0:", yml)
            self.assertIn("cam1:", yml)
            self.assertIn("runOnDemand:", yml)
            self.assertNotIn("runOnInit:", yml)
            self.assertEqual(settings["cam0"]["bitrate"], 2_000_000)
            conf = (root / "etc" / "avahi" / "avahi-daemon.conf").read_text(encoding="utf-8")
            self.assertIn("host-name=pi5-cam-1-myrtle", conf)
            self.assertIn("tailscale0", conf)
            for line in conf.splitlines():
                if line.startswith("allow-interfaces="):
                    self.assertNotIn("tailscale", line)
                    self.assertNotIn("docker", line)
            service = (root / "etc" / "avahi" / "services" / "pidentify.service").read_text(encoding="utf-8")
            self.assertIn("paths=cam0,cam1", service)
            self.assertIn("node=pi5_cam_1_myrtle", service)
            self.assertFalse((root / "boot").exists())
        finally:
            os.environ.pop("PIDENTIFY_LIST_CAMERAS_FILE", None)
            import shutil

            shutil.rmtree(root)

    def test_no_cameras_warns_and_publishes_nothing(self):
        root = Path(self.id().replace(".", "_") + "_none")
        if root.exists():
            import shutil

            shutil.rmtree(root)
        root.mkdir()
        try:
            os.environ["PIDENTIFY_LIST_CAMERAS_FILE"] = str(FIXTURES / "no_cameras.txt")
            summary = configure_node(
                config_dir=str(root / "etc" / "pidentify"),
                state_dir=str(root / "var"),
                avahi_conf=str(root / "avahi.conf"),
                avahi_service=str(root / "pidentify.service"),
                avahi_backup=str(root / "avahi.bak"),
                publish_cmd="/usr/local/bin/pidentify-publish",
                hostname="pi5_cam_3_myrtle",
                hostname_file=None,
            )
            self.assertEqual(summary["paths"], [])
            self.assertTrue(any("/boot/firmware/config.txt" in warning for warning in summary["warnings"]))
            yml = (root / "etc" / "pidentify" / "mediamtx.yml").read_text(encoding="utf-8")
            self.assertIn("paths: {}", yml)
            service = (root / "pidentify.service").read_text(encoding="utf-8")
            self.assertIn("paths=", service)
            self.assertNotIn("paths=cam", service)
        finally:
            os.environ.pop("PIDENTIFY_LIST_CAMERAS_FILE", None)
            import shutil

            shutil.rmtree(root)


class InstallDryRunTest(unittest.TestCase):
    def test_install_is_idempotent_and_uninstall_removes_it(self):
        hostname_file = Path("/etc/hostname")
        before = hashlib.sha256(hostname_file.read_bytes()).hexdigest() if hostname_file.exists() else None
        root = Path(self.id().replace(".", "_") + "_root")
        if root.exists():
            import shutil

            shutil.rmtree(root)
        env = os.environ.copy()
        env.update(
            {
                "PIDENTIFY_ROOT": str(root),
                "PIDENTIFY_SKIP_DOWNLOAD": "1",
                "PIDENTIFY_HOSTNAME": "pi5_cam_2_myrtle",
                "PIDENTIFY_LIST_CAMERAS_FILE": str(FIXTURES / "two_cameras.txt"),
            }
        )
        try:
            subprocess.run(["bash", str(PI / "install.sh")], check=True, env=env, cwd=str(REPO))
            self.assertEqual((root / "var" / "lib" / "pidentify" / "start-plan").read_text(encoding="utf-8").strip(), "dry-run")
            yml = (root / "etc" / "pidentify" / "mediamtx.yml").read_text(encoding="utf-8")
            self.assertIn("cam0:", yml)
            self.assertIn("cam1:", yml)
            self.assertIn("runOnDemand:", yml)
            self.assertNotIn("runOnInit:", yml)
            self.assertTrue(os.access(root / "usr" / "local" / "bin" / "pidentify-push", os.X_OK))
            config_text = (root / "etc" / "pidentify" / "config").read_text(encoding="utf-8")
            self.assertIn("mode=local", config_text)
            avahi = (root / "etc" / "avahi" / "avahi-daemon.conf").read_text(encoding="utf-8")
            self.assertIn("tailscale0", avahi)
            conf = (root / "etc" / "avahi" / "avahi-daemon.conf").read_text(encoding="utf-8")
            self.assertIn("host-name=pi5-cam-2-myrtle", conf)
            unit = (root / "etc" / "systemd" / "system" / "pidentify-mediamtx.service").read_text(encoding="utf-8")
            self.assertIn("Restart=on-failure", unit)
            self.assertIn("User=pidentify", unit)
            self.assertTrue(os.access(root / "usr" / "local" / "bin" / "pidentify-publish", os.X_OK))
            settings_path = root / "etc" / "pidentify" / "cam_settings.json"
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            settings["cam0"]["sharpness"] = 1.5
            settings_path.write_text(json.dumps(settings), encoding="utf-8")
            subprocess.run(["bash", str(PI / "install.sh")], check=True, env=env, cwd=str(REPO))
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(settings["cam0"]["sharpness"], 1.5)
            self.assertFalse((root / "boot" / "firmware" / "config.txt").exists())
            if before is not None:
                self.assertEqual(hashlib.sha256(hostname_file.read_bytes()).hexdigest(), before)
            wp = root / "etc" / "wireplumber" / "wireplumber.conf.d" / "50-pidentify-no-camera.conf"
            lua = root / "etc" / "wireplumber" / "main.lua.d" / "51-pidentify-no-camera.lua"
            wp.parent.mkdir(parents=True)
            lua.parent.mkdir(parents=True)
            wp.write_text("wireplumber.profiles = { main = { monitor.libcamera = disabled } }\n", encoding="utf-8")
            lua.write_text("libcamera_monitor.enabled = false\n", encoding="utf-8")
            subprocess.run(["bash", str(PI / "uninstall.sh")], check=True, env=env, cwd=str(REPO))
            self.assertFalse(wp.exists())
            self.assertFalse(lua.exists())
            self.assertFalse((root / "etc" / "pidentify").exists())
            self.assertFalse((root / "opt" / "pidentify").exists())
            self.assertFalse((root / "etc" / "systemd" / "system" / "pidentify-mediamtx.service").exists())
            self.assertFalse((root / "etc" / "avahi" / "services" / "pidentify.service").exists())
            self.assertFalse((root / "usr" / "local" / "bin" / "pidentify-publish").exists())
            self.assertFalse((root / "usr" / "local" / "bin" / "pidentify-push").exists())
            # The dry-run created the Avahi file, so uninstall deletes it.
            self.assertFalse((root / "etc" / "avahi" / "avahi-daemon.conf").exists())
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)

    def test_scripts_are_executable(self):
        for name in ("install.sh", "uninstall.sh"):
            mode = (PI / name).stat().st_mode
            self.assertTrue(mode & stat.S_IXUSR)


class SafetyTest(unittest.TestCase):
    def test_no_script_writes_config_txt(self):
        pattern_tokens = (">>", "tee ", "sed -i", "install ")
        for path in PI.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in {".py", ".sh"} and path.parent.name != "bin":
                continue
            text = path.read_text(encoding="utf-8")
            for line in text.splitlines():
                if "config.txt" not in line:
                    continue
                self.assertFalse(
                    any(token in line for token in pattern_tokens),
                    f"{path} looks like it writes config.txt: {line}",
                )

    def test_pinned_mediamtx_hash_is_present(self):
        text = (PI / "install.sh").read_text(encoding="utf-8")
        self.assertIn("180a235374a548d9133cdd5e20f5dbd8b01317be1c86e0d7e66df585dc42ed6f", text)
        self.assertIn("v1.21.2", text)


if __name__ == "__main__":
    unittest.main()
