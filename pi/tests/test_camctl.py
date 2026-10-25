"""camctl HTTP API, exercised in-process and over a real localhost socket."""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pidentify.camctl import CamctlApp, RawBody, make_server  # noqa: E402
from pidentify.capture import prune_captures  # noqa: E402
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
            state_dir=str(self.config / "state"),
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
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/ui", timeout=3) as response:
                page = response.read().decode("utf-8")
            self.assertIn("text/html", response.headers["Content-Type"])
            self.assertIn("Autofocus", page)
            self.assertIn("Reset to defaults", page)
        finally:
            server.shutdown()
            server.server_close()

    def test_settings_include_imx708_capabilities_and_reject_bad_size(self):
        status, body = self.app.dispatch("GET", "/cameras/cam0/settings", None, "127.0.0.1")
        self.assertEqual(status, 200)
        self.assertTrue(body["capabilities"]["autofocus"]["supported"])
        self.assertIn({"width": 2304, "height": 1296}, body["capabilities"]["hdr"]["resolutions"])
        payload = json.dumps({"width": 1000, "height": 1000}).encode()
        status, body = self.app.dispatch("PUT", "/cameras/cam0/settings", payload, "127.0.0.1")
        self.assertEqual(status, 400)
        self.assertIn("mode list", body["fields"]["width"])

    def test_imx477_autofocus_is_rejected(self):
        cameras = {
            "cameras": [
                {"id": "cam0", "index": 0, "model": "imx477", "modes": "4056x3040", "present": True}
            ]
        }
        (self.config / "cameras.json").write_text(json.dumps(cameras), encoding="utf-8")
        payload = json.dumps({"autofocus_mode": "continuous"}).encode()
        status, body = self.app.dispatch("PUT", "/cameras/cam0/settings", payload, "127.0.0.1")
        self.assertEqual(status, 400)
        self.assertIn("not supported", body["error"] + body["fields"]["autofocus_mode"])
        status, body = self.app.dispatch("POST", "/cameras/cam0/autofocus", b"", "100.64.1.5")
        self.assertEqual(status, 400)
        self.assertIn("imx477", body["error"])

    def test_autofocus_snapshot_record_and_reset(self):
        events = []

        def pauser(cam_id):
            events.append(("pause", cam_id))
            return True

        def af_runner(argv):
            events.append(("af", argv))
            return {"lens_position": 2.25, "af_state": "focused"}

        def snapshot_runner(argv):
            events.append(("still", argv))
            return b"\xff\xd8\xff\xd9"

        def record_runner(argv):
            events.append(("record", argv[-1]))
            with open(argv[-1], "wb") as handle:
                handle.write(b"fake-mp4")

        self.app.pauser = pauser
        self.app.af_runner = af_runner
        self.app.snapshot_runner = snapshot_runner
        self.app.record_runner = record_runner
        status, body = self.app.dispatch("POST", "/cameras/cam0/autofocus", b"", "127.0.0.1")
        self.assertEqual(status, 200)
        self.assertEqual(body["lens_position"], 2.25)
        self.assertIn("interrupted_ms", body)
        self.assertEqual(events[0][0], "pause")
        self.assertEqual(events[1][0], "af")
        self.assertEqual(self.kicks[-1][0], "cam0")
        status, body = self.app.dispatch("POST", "/cameras/cam0/snapshot", b"{}", "127.0.0.1")
        self.assertEqual(status, 200)
        self.assertIsInstance(body, RawBody)
        self.assertEqual(body.content_type, "image/jpeg")
        self.assertIn("X-Stream-Interrupted-Ms", body.headers)
        self.assertTrue(body.data.startswith(b"\xff\xd8"))
        payload = json.dumps({"seconds": 3}).encode()
        status, body = self.app.dispatch("POST", "/cameras/cam0/record", payload, "127.0.0.1")
        self.assertEqual(status, 200)
        self.assertTrue(body["file"].startswith("cam0-"))
        self.assertIn("not opened a second time", body["note"])
        status, listing = self.app.dispatch("GET", "/cameras/cam0/captures", None, "127.0.0.1")
        self.assertEqual(listing["captures"][0]["name"], body["file"])
        status, download = self.app.dispatch("GET", "/captures/" + body["file"], None, "10.1.1.1")
        self.assertEqual(status, 200)
        self.assertEqual(download.data, b"fake-mp4")
        status, _missing = self.app.dispatch("GET", "/captures/..", None, "127.0.0.1")
        self.assertEqual(status, 400)
        payload = json.dumps({"reset": True}).encode()
        self.app.dispatch("PUT", "/cameras/cam0/settings", json.dumps({"bitrate": 4000000}).encode(), "127.0.0.1")
        status, body = self.app.dispatch("POST", "/cameras/cam0/settings", payload, "127.0.0.1")
        self.assertEqual(status, 200)
        self.assertEqual(body["settings"]["bitrate"], 2_000_000)
        self.assertEqual(body["settings"]["autofocus_mode"], None)

    def test_token_gates_the_api(self):
        (self.config / "config").write_text("mode=local\nname=pi\npush_url=\ncontrol_token=s3cret\n", encoding="utf-8")
        status, body = self.app.dispatch("GET", "/health", None, "127.0.0.1")
        self.assertEqual(status, 401)
        self.assertIn("authentication", body["error"])
        headers = {"Authorization": "Bearer s3cret"}
        status, body = self.app.dispatch("GET", "/health", None, "127.0.0.1", headers)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        basic = base64_token("pidentify", "s3cret")
        status, _body = self.app.dispatch("GET", "/ui", None, "100.64.8.8", {"Authorization": basic})
        self.assertEqual(status, 200)

    def test_prune_drops_oldest_clips(self):
        directory = self.config / "state" / "captures"
        directory.mkdir(parents=True)
        older = directory / "cam0-20261025T101500Z.mp4"
        newer = directory / "cam0-20261025T101530Z.mp4"
        older.write_bytes(b"12345")
        newer.write_bytes(b"12345")
        os.utime(older, (1, 1))
        os.utime(newer, (2, 2))
        removed = prune_captures(str(self.config / "state"), limit_bytes=6, protect=newer.name)
        self.assertEqual(removed, [older.name])
        self.assertTrue(newer.exists())
        self.assertFalse(older.exists())


def base64_token(user: str, password: str) -> str:
    import base64

    blob = base64.b64encode(("{0}:{1}".format(user, password)).encode("utf-8")).decode("ascii")
    return "Basic " + blob


if __name__ == "__main__":
    unittest.main()
