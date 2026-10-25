"""Push URLs, Tailscale address parsing, and Avahi interface filtering."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pidentify.avahi_conf import read_server_value, set_server_assignment  # noqa: E402
from pidentify.configure import configure_node  # noqa: E402
from pidentify.ifaces import discover, interface_policy  # noqa: E402
from pidentify.nodeconfig import ConfigError, load_node_config, parse_push_target  # noqa: E402
from pidentify.settings import build_push_ffmpeg_argv  # noqa: E402
from pidentify.tailscale import parse_ip4, parse_status  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class PushTargetTest(unittest.TestCase):
    def test_explicit_prefix_and_default_name(self):
        explicit = parse_push_target("rtsp://192.168.1.218:8554/myrtle1")
        self.assertEqual(explicit["targets"]["cam0"], "rtsp://192.168.1.218:8554/myrtle1-cam0")
        self.assertEqual(explicit["targets"]["cam1"], "rtsp://192.168.1.218:8554/myrtle1-cam1")
        named = parse_push_target("rtsp://jetson-camwall:8554", default_prefix="pi5-cam-1-myrtle")
        self.assertEqual(named["url"], "rtsp://jetson-camwall:8554/pi5-cam-1-myrtle")
        self.assertEqual(
            named["targets"]["cam0"],
            "rtsp://jetson-camwall:8554/pi5-cam-1-myrtle-cam0",
        )

    def test_rejects_credentials(self):
        with self.assertRaises(ConfigError):
            parse_push_target("rtsp://user:secret@192.168.1.218:8554/myrtle1")

    def test_ffmpeg_copy_targets_the_remote_path(self):
        argv = build_push_ffmpeg_argv("cam0", "rtsp://jetson-camwall:8554/pi5-cam-1-myrtle-cam0")
        self.assertIn("rtsp://127.0.0.1:8554/cam0", argv)
        self.assertEqual(argv[-1], "rtsp://jetson-camwall:8554/pi5-cam-1-myrtle-cam0")
        self.assertIn("-c", argv)
        self.assertIn("copy", argv)


class TailscaleParseTest(unittest.TestCase):
    def test_magic_dns_and_ipv4(self):
        parsed = parse_status(
            {
                "Self": {
                    "DNSName": "pi5-cam-1-myrtle.tail.ts.net.",
                    "TailscaleIPs": ["100.1.2.3", "fd7a:115c:a1e0::1"],
                    "Online": True,
                }
            }
        )
        self.assertEqual(parsed["magic_dns"], "pi5-cam-1-myrtle.tail.ts.net")
        self.assertEqual(parsed["ipv4"], ["100.1.2.3"])
        self.assertTrue(parsed["online"])
        self.assertEqual(parse_ip4("100.9.9.9\nnot-an-ip\n"), ["100.9.9.9"])


class IfaceTest(unittest.TestCase):
    def test_docker_and_tailscale_are_not_lan(self):
        root = Path(self.id().replace(".", "_") + "_sys")
        if root.exists():
            import shutil

            shutil.rmtree(root)
        (root / "eth0" / "device").mkdir(parents=True)
        (root / "wlan0" / "device").mkdir(parents=True)
        (root / "docker0").mkdir()
        (root / "tailscale0").mkdir()
        try:
            info = discover(
                link_text="\n".join(
                    [
                        "1: lo: <LOOPBACK,UP>",
                        "2: eth0: <BROADCAST,UP>",
                        "3: wlan0: <BROADCAST,UP>",
                        "4: docker0: <BROADCAST,UP>",
                        "5: tailscale0: <POINTOPOINT,UP>",
                    ]
                ),
                addr_text="\n".join(
                    [
                        "2: eth0    inet 192.168.2.43/24 brd 192.168.2.255 scope global eth0",
                        "3: wlan0    inet 192.168.2.99/24 brd 192.168.2.255 scope global wlan0",
                        "4: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0",
                        "5: tailscale0    inet 100.64.1.2/32 scope global tailscale0",
                    ]
                ),
                sysfs_root=str(root),
            )
            self.assertEqual([item["address"] for item in info["lan_ipv4"]], ["192.168.2.43", "192.168.2.99"])
            self.assertIn("docker0", info["deny_interfaces"])
            self.assertIn("tailscale0", info["deny_interfaces"])
            self.assertEqual(info["allow_interfaces"], ["eth0", "wlan0"])
            conf = "[server]\n#deny-interfaces=eth1\nuse-ipv4=yes\n"
            deny, allow = interface_policy(info, read_server_value(conf, "deny-interfaces"), None)
            updated = set_server_assignment(conf, "deny-interfaces", deny)
            updated = set_server_assignment(updated, "allow-interfaces", allow)
            self.assertIn("#deny-interfaces=eth1", updated)
            self.assertIn("tailscale0", deny)
            self.assertIn("docker0", deny)
            self.assertNotIn("eth1", deny.split(","))
            self.assertNotIn("tailscale0", allow)
            self.assertNotIn("docker0", allow)
        finally:
            import shutil

            shutil.rmtree(root)


class ConfigurePushTest(unittest.TestCase):
    def test_name_and_push_persist_until_local(self):
        root = Path(self.id().replace(".", "_") + "_cfg")
        if root.exists():
            import shutil

            shutil.rmtree(root)
        root.mkdir()
        network = {
            "detected": True,
            "deny_interfaces": ["docker0", "tailscale0"],
            "allow_interfaces": ["eth0", "wlan0"],
            "sysfs_root": "",
            "lan_ipv4": [],
            "interfaces": ["eth0", "wlan0", "docker0", "tailscale0"],
        }
        try:
            os.environ["PIDENTIFY_LIST_CAMERAS_FILE"] = str(FIXTURES / "two_cameras.txt")
            summary = configure_node(
                config_dir=str(root / "etc" / "pidentify"),
                state_dir=str(root / "var"),
                avahi_conf=str(root / "avahi.conf"),
                avahi_service=str(root / "pidentify.service"),
                avahi_backup=str(root / "avahi.bak"),
                publish_cmd="/usr/local/bin/pidentify-publish",
                hostname="pi5",
                hostname_file=None,
                name="pi5-cam-1-myrtle",
                push_to="rtsp://jetson-camwall:8554",
                network=network,
            )
            self.assertEqual(summary["hostname"], "pi5")
            self.assertEqual(summary["node"], "pi5-cam-1-myrtle")
            self.assertEqual(summary["mode"], "push")
            self.assertEqual(
                summary["push_targets"]["cam1"],
                "rtsp://jetson-camwall:8554/pi5-cam-1-myrtle-cam1",
            )
            yml = (root / "etc" / "pidentify" / "mediamtx.yml").read_text(encoding="utf-8")
            self.assertIn("runOnInit:", yml)
            self.assertNotIn("runOnDemand:", yml)
            service = (root / "pidentify.service").read_text(encoding="utf-8")
            self.assertIn("<name replace-wildcards=\"yes\">pi5-cam-1-myrtle</name>", service)
            self.assertIn("node=pi5-cam-1-myrtle", service)
            conf = (root / "avahi.conf").read_text(encoding="utf-8")
            self.assertIn("host-name=pi5-cam-1-myrtle", conf)
            self.assertIn("deny-interfaces=", conf)
            self.assertIn("tailscale0", conf)
            kept = configure_node(
                config_dir=str(root / "etc" / "pidentify"),
                state_dir=str(root / "var"),
                avahi_conf=str(root / "avahi.conf"),
                avahi_service=str(root / "pidentify.service"),
                avahi_backup=str(root / "avahi.bak"),
                publish_cmd="/usr/local/bin/pidentify-publish",
                hostname="pi5",
                hostname_file=None,
                network=network,
            )
            self.assertEqual(kept["mode"], "push")
            self.assertEqual(kept["name"], "pi5-cam-1-myrtle")
            local = configure_node(
                config_dir=str(root / "etc" / "pidentify"),
                state_dir=str(root / "var"),
                avahi_conf=str(root / "avahi.conf"),
                avahi_service=str(root / "pidentify.service"),
                avahi_backup=str(root / "avahi.bak"),
                publish_cmd="/usr/local/bin/pidentify-publish",
                hostname="pi5",
                hostname_file=None,
                local=True,
                network=network,
            )
            self.assertEqual(local["mode"], "local")
            local_yml = (root / "etc" / "pidentify" / "mediamtx.yml").read_text(encoding="utf-8")
            self.assertIn("runOnDemand:", local_yml)
            self.assertNotIn("runOnInit:", local_yml)
            stored = load_node_config(str(root / "etc" / "pidentify" / "config"))
            self.assertEqual(stored["mode"], "local")
            self.assertEqual(stored["push_url"], "rtsp://jetson-camwall:8554/pi5-cam-1-myrtle")
            node = json.loads((root / "etc" / "pidentify" / "node.json").read_text(encoding="utf-8"))
            self.assertEqual(node["system_hostname"], "pi5")
        finally:
            os.environ.pop("PIDENTIFY_LIST_CAMERAS_FILE", None)
            import shutil

            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
