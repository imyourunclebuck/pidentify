"""camctl: HTTP API for per-camera image controls.

Stdlib only, so the Pi does not need Flask. Listens on 0.0.0.0:8555 and
rejects clients that are not on loopback, a private LAN, or the Tailscale
CGNAT range. There is no authentication. Do not publish this port on the
public Internet. The socket binds all IPv4 interfaces, including tailscale0.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.request import urlopen

from pidentify import __version__
from pidentify.hostname import identity
from pidentify.kick import restart_camera
from pidentify.lan import is_lan_address
from pidentify.settings import (
    CAMERA_IDS,
    SettingsError,
    default_settings,
    load_settings,
    merge_patch,
    public_settings,
    save_settings,
)

CONFIG_DIR = os.environ.get("PIDENTIFY_CONFIG_DIR", "/etc/pidentify")
BIND = os.environ.get("PIDENTIFY_BIND", "0.0.0.0")
PORT = int(os.environ.get("PIDENTIFY_PORT", "8555"))
API_BASE = os.environ.get("PIDENTIFY_MEDIAMTX_API", "http://127.0.0.1:9997")
PUBLISH_CMD = os.environ.get("PIDENTIFY_PUBLISH_CMD", "/usr/local/bin/pidentify-publish")
MAX_BODY = 65536


class CamctlApp:
    def __init__(
        self,
        config_dir: str = CONFIG_DIR,
        api_base: str = API_BASE,
        publish_cmd: str = PUBLISH_CMD,
        kicker=None,
        path_lookup=None,
    ):
        self.config_dir = config_dir
        self.api_base = api_base
        self.publish_cmd = publish_cmd
        self.kicker = kicker or restart_camera
        self.path_lookup = path_lookup or self._path_ready
        self._lock = threading.Lock()

    def dispatch(self, method: str, path: str, body: bytes | None, client_ip: str) -> tuple[int, dict]:
        if not is_lan_address(client_ip):
            return 403, {"error": "camctl accepts LAN and tailnet clients only", "client": client_ip}
        route = path.split("?", 1)[0].rstrip("/") or "/"
        parts = [part for part in route.split("/") if part]
        try:
            if method == "GET" and parts == ["health"]:
                return 200, {"ok": True, "service": "pidentify-camctl", "version": __version__}
            if method == "GET" and parts == ["cameras"]:
                return 200, self._cameras()
            if len(parts) == 2 and parts[0] == "cameras" and method == "GET":
                cam_id = self._known(parts[1])
                return 200, self._camera_document(cam_id)
            if len(parts) == 3 and parts[0] == "cameras" and parts[2] == "settings" and method == "GET":
                cam_id = self._known(parts[1])
                return 200, {"id": cam_id, "settings": self._settings_for(cam_id)}
            if len(parts) == 3 and parts[0] == "cameras" and parts[2] == "settings" and method in {"PUT", "POST"}:
                cam_id = self._known(parts[1])
                return self._update(cam_id, body)
            if len(parts) == 3 and parts[0] == "cameras" and parts[2] == "restart" and method == "POST":
                cam_id = self._known(parts[1])
                return self._restart(cam_id)
        except KeyError as exc:
            return 404, {"error": "unknown camera", "id": str(exc).strip("'")}
        except SettingsError as exc:
            return 400, {"error": str(exc), "fields": exc.fields}
        return 404, {"error": "not found", "path": route}

    def _known(self, cam_id: str) -> str:
        if cam_id not in CAMERA_IDS:
            raise KeyError(cam_id)
        known = self._known_ids()
        if known is not None and cam_id not in known:
            raise KeyError(cam_id)
        return cam_id

    def _known_ids(self) -> set[str] | None:
        path = os.path.join(self.config_dir, "cameras.json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
        return {str(camera.get("id")) for camera in document.get("cameras") or [] if camera.get("id") in CAMERA_IDS}

    def _settings_path(self) -> str:
        return os.path.join(self.config_dir, "cam_settings.json")

    def _settings_for(self, cam_id: str) -> dict:
        with self._lock:
            stored = load_settings(self._settings_path())
            current = stored.get(cam_id) or default_settings()
            return public_settings(current)

    def _node(self) -> dict:
        path = os.path.join(self.config_dir, "node.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and data.get("hostname"):
                return data
        return identity("pidentify")

    def _cameras(self) -> dict:
        node = self._node()
        path = os.path.join(self.config_dir, "cameras.json")
        cameras = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                document = json.load(handle)
            cameras = document.get("cameras") or []
        items = []
        for camera in cameras:
            cam_id = camera.get("id")
            if cam_id not in CAMERA_IDS:
                continue
            items.append(
                {
                    "id": cam_id,
                    "index": camera.get("index"),
                    "model": camera.get("model"),
                    "modes": camera.get("modes"),
                    "device": camera.get("device"),
                    "present": True,
                    "settings": self._settings_for(cam_id),
                    "streaming": self.path_lookup(cam_id),
                    "rtsp_path": f"/{cam_id}",
                }
            )
        node_name = node.get("node") or node.get("name") or node.get("hostname")
        return {"node": node_name, "mdns": node.get("mdns_fqdn"), "cameras": items}

    def _camera_document(self, cam_id: str) -> dict:
        listing = self._cameras()
        for camera in listing["cameras"]:
            if camera["id"] == cam_id:
                return camera
        return {"id": cam_id, "settings": self._settings_for(cam_id), "streaming": self.path_lookup(cam_id)}

    def _update(self, cam_id: str, body: bytes | None) -> tuple[int, dict]:
        patch, restart = _parse_settings_body(body)
        with self._lock:
            stored = load_settings(self._settings_path())
            current = stored.get(cam_id) or default_settings()
            stored[cam_id] = merge_patch(current, patch)
            save_settings(self._settings_path(), stored)
            saved = public_settings(stored[cam_id])
        restart_info = {"ok": True, "method": "skipped", "detail": "saved without restart"}
        if restart:
            restart_info = self.kicker(
                cam_id,
                api_base=self.api_base,
                publish_cmd=self.publish_cmd,
            )
        # The file is already saved. restart.ok tells the caller whether the
        # live stream was kicked; a down MediaMTX is not a failed write.
        return 200, {"id": cam_id, "settings": saved, "restart": restart_info}

    def _restart(self, cam_id: str) -> tuple[int, dict]:
        info = self.kicker(cam_id, api_base=self.api_base, publish_cmd=self.publish_cmd)
        status = 200 if info.get("ok") else 502
        return status, {"id": cam_id, "restarted": bool(info.get("ok")), "restart": info}

    def _path_ready(self, cam_id: str):
        url = f"{self.api_base.rstrip('/')}/v3/paths/get/{cam_id}"
        try:
            with urlopen(url, timeout=1.5) as response:
                data = json.load(response)
        except (OSError, URLError, json.JSONDecodeError, TimeoutError, ValueError):
            return None
        if isinstance(data, dict) and "ready" in data:
            return bool(data.get("ready"))
        return None


def _parse_settings_body(body: bytes | None) -> tuple[dict, bool]:
    if not body:
        raise SettingsError("invalid settings", {"_": "expected a JSON object"})
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SettingsError("invalid settings", {"_": "body is not JSON"}) from exc
    if not isinstance(payload, dict):
        raise SettingsError("invalid settings", {"_": "expected a JSON object"})
    restart = True
    if "restart" in payload:
        restart = payload["restart"]
        if not isinstance(restart, bool):
            raise SettingsError("invalid settings", {"restart": "expected true or false"})
    if "settings" in payload and isinstance(payload["settings"], dict):
        patch = dict(payload["settings"])
        extra = set(payload) - {"settings", "restart"}
        if extra:
            raise SettingsError("invalid settings", {key: "unknown field" for key in sorted(extra)})
    else:
        patch = {key: value for key, value in payload.items() if key != "restart"}
    return patch, restart


class _Handler(BaseHTTPRequestHandler):
    app: CamctlApp
    server_version = "pidentify-camctl/" + __version__

    def do_GET(self):  # noqa: N802 - stdlib handler name
        self._respond("GET")

    def do_PUT(self):  # noqa: N802
        self._respond("PUT")

    def do_POST(self):  # noqa: N802
        self._respond("POST")

    def _respond(self, method: str) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            self._send(400, {"error": "bad Content-Length"})
            return
        if length > MAX_BODY:
            self._send(413, {"error": "body too large"})
            return
        body = self.rfile.read(length) if length else None
        if method in {"PUT", "POST"} and body:
            content_type = self.headers.get("Content-Type", "")
            if "application/json" not in content_type:
                self._send(415, {"error": "Content-Type must be application/json"})
                return
        client = self.client_address[0] if self.client_address else ""
        status, payload = self.app.dispatch(method, self.path, body, client)
        self._send(status, payload)

    def _send(self, status: int, payload: dict) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[camctl] {self.address_string()} {fmt % args}", file=sys.stderr)


def make_server(app: CamctlApp, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    handler = type("CamctlHandler", (_Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def serve(host: str = BIND, port: int = PORT, config_dir: str = CONFIG_DIR) -> None:
    app = CamctlApp(config_dir=config_dir)
    server = make_server(app, host, port)
    print(f"[camctl] listening on {host}:{server.server_address[1]} config={config_dir}", file=sys.stderr)

    def _stop(_signum, _frame) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    server.serve_forever()
    server.server_close()


def main(argv: list[str] | None = None) -> int:
    del argv
    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
