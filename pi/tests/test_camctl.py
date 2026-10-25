"""camctl HTTP API, exercised in-process and over a real localhost socket."""

from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pidentify.camctl import CamctlApp, make_server  # noqa: E402
from pidentify.settings import default_settings, save_settings  # noqa: E402


class FakeResponse:
    def __init__(self, status=200, payload=b"{}"):
        self.status = status
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._payload


class CamctlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.id().replace(".", "_"))
        # Isolated directory per test, removed in tearDown.
        import tempfile

        self._dir = tempfile.TemporaryDirectory()
        self.config = Path(self._dir.name)
        cameras = {
            "cameras": [
                {
                    "id": "cam0",
                    "index": 0,
                    "model": "imx708",
                    "modes": "4608x2592",
                    "device": "/base/cam0",
                    "present": True,
                }
            ]
        }
        (self.config / "cameras.json").write_text(json.dumps(cameras), encoding="utf-8")
        (self.config / "node.json").write_text(
            json.dumps(
                {
                    "hostname": "pi5_cam_1_myrtle",
                    "mdns_hostname": "pi5-cam-1-myrtle",
                    "mdns_fqdn": "pi5-cam-1-myrtle.local",
                }
            ),
            encoding="utf-8",
        )
        save_settings(str(self.config / "cam_settings.json"), {"cam0": default_settings()})
        self.kicks = []

        def kicker(cam_id, **kwargs):
            self.kicks.append((cam_id, kwargs))
            return {"ok": True, "method": "idle", "detail": "test"}

        self.app = CamctlApp(
            config_dir=str(self.config),
            kicker=kicker,
            path_lookup=lambda cam_id: False,
        )

    def tearDown(self):
        self._dir.cleanup()

    def test_root_is_an_endpoint_index(self):
        status, body = self.app.dispatch("GET", "/", None, "127.0.0.1")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "pidentify-camctl")
        paths = [item["path"] for item in body["endpoints"]]
        self.assertIn("/health", paths)
        self.assertIn("/cameras", paths)
        self.assertIn("/cameras/{id}/settings", paths)
        self.assertIn("/cameras/{id}/restart", paths)

    def test_health_and_lan_rejection(self):
        status, body = self.app.dispatch("GET", "/health", None, "127.0.0.1")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        status, body = self.app.dispatch("GET", "/cameras", None, "8.8.8.8")
        self.assertEqual(status, 403)

    def test_lists_only_detected_camera(self):
        status, body = self.app.dispatch("GET", "/cameras", None, "192.168.1.9")
        self.assertEqual(status, 200)
        self.assertEqual(body["node"], "pi5_cam_1_myrtle")
        self.assertEqual(body["mdns"], "pi5-cam-1-myrtle.local")
        self.assertEqual([cam["id"] for cam in body["cameras"]], ["cam0"])
        self.assertEqual(body["cameras"][0]["model"], "imx708")
        self.assertEqual(body["cameras"][0]["settings"]["width"], 1280)

    def test_unknown_camera_is_404(self):
        status, body = self.app.dispatch("GET", "/cameras/cam1/settings", None, "10.0.0.2")
        self.assertEqual(status, 404)
        status, _body = self.app.dispatch("GET", "/cameras/cam2/settings", None, "10.0.0.2")
        self.assertEqual(status, 404)

    def test_put_merges_and_kicks(self):
        payload = json.dumps({"brightness": -0.25, "awb": "cloudy", "hflip": True}).encode()
        status, body = self.app.dispatch("PUT", "/cameras/cam0/settings", payload, "127.0.0.1")
        self.assertEqual(status, 200)
        self.assertEqual(body["settings"]["brightness"], -0.25)
        self.assertEqual(body["settings"]["awb"], "cloudy")
        self.assertTrue(body["settings"]["hflip"])
        self.assertEqual(body["settings"]["fps"], 30)
        self.assertEqual(self.kicks[0][0], "cam0")
        status, again = self.app.dispatch("GET", "/cameras/cam0/settings", None, "127.0.0.1")
        self.assertEqual(status, 200)
        self.assertEqual(again["settings"]["brightness"], -0.25)

    def test_post_wrapper_and_restart_false(self):
        payload = json.dumps({"settings": {"saturation": 0}, "restart": False}).encode()
        status, body = self.app.dispatch("POST", "/cameras/cam0/settings", payload, "172.16.0.4")
        self.assertEqual(status, 200)
        self.assertEqual(body["settings"]["saturation"], 0)
        self.assertEqual(body["restart"]["method"], "skipped")
        self.assertEqual(self.kicks, [])

    def test_restart_endpoint(self):
        status, body = self.app.dispatch("POST", "/cameras/cam0/restart", b"", "127.0.0.1")
        self.assertEqual(status, 200)
        self.assertTrue(body["restarted"])

    def test_invalid_json_is_400(self):
        status, body = self.app.dispatch("POST", "/cameras/cam0/settings", b"{", "127.0.0.1")
        self.assertEqual(status, 400)
        self.assertIn("fields", body)

    def test_real_http_health(self):
        server = make_server(self.app, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
                payload = json.load(response)
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["service"], "pidentify-camctl")
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/cameras/cam0/settings",
                data=json.dumps({"contrast": 1.4}).encode(),
                method="PUT",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                payload = json.load(response)
            self.assertEqual(payload["settings"]["contrast"], 1.4)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
