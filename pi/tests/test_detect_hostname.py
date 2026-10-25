"""Camera probe parsing and DNS hostname derivation."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pidentify.avahi_conf import read_domain, render_service, set_avahi_host_name  # noqa: E402
from pidentify.detect import CONFIG_TXT_WARNING, parse_list_cameras, probe  # noqa: E402
from pidentify.hostname import dns_safe_hostname, identity  # noqa: E402
from pidentify.lan import is_lan_address  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class HostnameTest(unittest.TestCase):
    def test_underscore_host_becomes_hyphenated_mdns_name(self):
        info = identity("pi5_cam_1_myrtle")
        self.assertEqual(info["hostname"], "pi5_cam_1_myrtle")
        self.assertEqual(info["mdns_hostname"], "pi5-cam-1-myrtle")
        self.assertEqual(info["mdns_fqdn"], "pi5-cam-1-myrtle.local")

    def test_other_nodes(self):
        self.assertEqual(dns_safe_hostname("pi5_cam_2_myrtle"), "pi5-cam-2-myrtle")
        self.assertEqual(dns_safe_hostname("pi5_cam_3_myrtle"), "pi5-cam-3-myrtle")

    def test_already_safe_name_is_stable(self):
        self.assertEqual(dns_safe_hostname("pi5-cam-1-myrtle"), "pi5-cam-1-myrtle")

    def test_shared_system_hostname_uses_requested_name(self):
        info = identity("pi5", name="pi5-cam-1-myrtle")
        self.assertEqual(info["hostname"], "pi5")
        self.assertEqual(info["system_hostname"], "pi5")
        self.assertEqual(info["name"], "pi5-cam-1-myrtle")
        self.assertEqual(info["node"], "pi5-cam-1-myrtle")
        self.assertEqual(info["mdns_fqdn"], "pi5-cam-1-myrtle.local")
        underscored = identity("pi5", name="pi5_cam_3_myrtle")
        self.assertEqual(underscored["mdns_hostname"], "pi5-cam-3-myrtle")
        self.assertEqual(underscored["node"], "pi5-cam-3-myrtle")

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            dns_safe_hostname("___")


class DetectTest(unittest.TestCase):
    def test_two_cameras(self):
        cameras, warnings = parse_list_cameras((FIXTURES / "two_cameras.txt").read_text(encoding="utf-8"))
        self.assertEqual(warnings, [])
        self.assertEqual([camera.id for camera in cameras], ["cam0", "cam1"])
        self.assertEqual(cameras[0].model, "imx708_wide")
        self.assertIn("i2c@80000", cameras[0].device)
        self.assertEqual(cameras[1].model, "imx708")

    def test_index_one_only_and_ignores_extra(self):
        cameras, warnings = parse_list_cameras((FIXTURES / "cam1_only.txt").read_text(encoding="utf-8"))
        self.assertEqual([camera.id for camera in cameras], ["cam1"])
        self.assertEqual(cameras[0].model, "imx219")
        self.assertTrue(any("index 2" in warning for warning in warnings))

    def test_no_cameras_warning_mentions_config_txt_but_is_not_a_write(self):
        result = probe(list_file=str(FIXTURES / "no_cameras.txt"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["cameras"], [])
        self.assertTrue(any(CONFIG_TXT_WARNING in warning for warning in result["warnings"]))
        self.assertIn("did not modify", CONFIG_TXT_WARNING)
        self.assertNotIn(">", CONFIG_TXT_WARNING)


class AvahiTest(unittest.TestCase):
    def test_uncomments_host_name_only(self):
        original = "# something\n[server]\n#host-name=foo\n#domain-name=local\nuse-ipv4=yes\n[publish]\nhost-name=leave\n"
        updated = set_avahi_host_name(original, "pi5-cam-2-myrtle")
        self.assertIn("host-name=pi5-cam-2-myrtle\n", updated)
        self.assertIn("[publish]\nhost-name=leave\n", updated)
        self.assertIn("use-ipv4=yes", updated)
        self.assertEqual(read_domain(original), "local")

    def test_inserts_when_missing(self):
        updated = set_avahi_host_name("[server]\nuse-ipv4=yes\n", "pi5_cam_3_myrtle")
        self.assertIn("host-name=pi5-cam-3-myrtle\n", updated)

    def test_service_records_only_detected_paths(self):
        xml = render_service(paths=["cam1"], node="pi5_cam_2_myrtle")
        self.assertIn("<type>_rtsp._tcp</type>", xml)
        self.assertIn("<port>8554</port>", xml)
        self.assertIn("paths=cam1", xml)
        self.assertNotIn("cam0", xml)
        self.assertIn("camctl=8555", xml)
        self.assertIn("api=9997", xml)
        self.assertIn("node=pi5_cam_2_myrtle", xml)
        quoted = render_service(paths=["cam0"], node='pi "5"')
        self.assertIn("node=pi &quot;5&quot;", quoted)
        self.assertNotIn('node=pi "5"', quoted)


class LanTest(unittest.TestCase):
    def test_allows_lan_and_rejects_public(self):
        for good in (
            "127.0.0.1",
            "::1",
            "192.168.1.20",
            "10.0.0.5",
            "172.16.5.5",
            "169.254.1.1",
            "100.64.0.1",
            "100.127.255.254",
            "fe80::1",
            "fd7a:115c:a1e0::1",
            "::ffff:192.168.0.8",
        ):
            self.assertTrue(is_lan_address(good), good)
        for bad in ("8.8.8.8", "1.1.1.1", "100.63.255.255", "100.128.0.1", "2001:db8::1", "not-an-ip", ""):
            self.assertFalse(is_lan_address(bad), bad)


if __name__ == "__main__":
    unittest.main()
