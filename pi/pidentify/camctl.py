"""camctl: HTTP API for per-camera image controls.

Stdlib only, so the Pi does not need Flask. Listens on 0.0.0.0:8555 and
rejects clients that are not on loopback, a private LAN, or the Tailscale
CGNAT range. Authentication is off unless ``control_token`` is set in
``/etc/pidentify/config``. Do not publish this port on the public Internet.
The socket binds all IPv4 interfaces, including tailscale0.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.request import urlopen

from pidentify import __version__
from pidentify.capture import (
    DEFAULT_LIMIT_BYTES,
    PREVIEW_NOTE,
    RECORD_NOTE,
    SNAPSHOT_NOTE,
    list_captures,
    parse_metadata,
    prune_captures,
    run_pause_resume,
    safe_clip,
    used_bytes,
)
from pidentify.hostname import identity
from pidentify.kick import _signal_groups, list_publishers, restart_camera
from pidentify.lan import is_lan_address
from pidentify.nodeconfig import ConfigError, load_node_config
from pidentify.sensors import allowed_resolutions, autofocus_supported, capabilities
from pidentify.settings import (
    CAMERA_IDS,
    SettingsError,
    build_autofocus_argv,
    build_preview_argv,
    build_record_argv,
    build_snapshot_argv,
    camera_index,
    default_settings,
    load_settings,
    merge_patch,
    public_settings,
    read_board_text,
    save_settings,
    select_encoder,
)
from pidentify.ui import render_ui


class RawBody:
    """Non-JSON response: HTML, JPEG, or MP4."""

    def __init__(self, data: bytes, content_type: str, headers: dict | None = None):
        self.data = data
        self.content_type = content_type
        self.headers = headers or {}

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
        state_dir: str | None = None,
        snapshot_runner=None,
        record_runner=None,
        preview_runner=None,
        af_runner=None,
        pauser=None,
    ):
        self.config_dir = config_dir
        self.api_base = api_base
        self.publish_cmd = publish_cmd
        self.state_dir = state_dir or os.environ.get("PIDENTIFY_STATE_DIR", "/var/lib/pidentify")
        self.kicker = kicker or restart_camera
        self.path_lookup = path_lookup or self._path_ready
        self.snapshot_runner = snapshot_runner or self._default_snapshot
        self.record_runner = record_runner or self._default_record
        self.preview_runner = preview_runner or self._default_preview
        self.af_runner = af_runner or self._default_af
        self.pauser = pauser or self._default_pause
        self._lock = threading.Lock()

    def dispatch(self, method: str, path: str, body: bytes | None, client_ip: str, headers=None):
        if not is_lan_address(client_ip):
            return 403, {"error": "camctl accepts LAN and tailnet clients only", "client": client_ip}
        if not self._authorized(headers):
            return 401, {"error": "authentication required"}
        route = path.split("?", 1)[0].rstrip("/") or "/"
        parts = [part for part in route.split("/") if part]
        try:
            if method == "GET" and not parts:
                return 200, self._index()
            if method == "GET" and parts == ["health"]:
                return 200, {"ok": True, "service": "pidentify-camctl", "version": __version__}
            if method == "GET" and parts == ["ui"]:
                page = render_ui().encode("utf-8")
                return 200, RawBody(page, "text/html; charset=utf-8")
            if method == "GET" and parts == ["captures"]:
                return 200, self._capture_list(None)
            if method == "GET" and len(parts) == 2 and parts[0] == "captures":
                return self._capture_download(parts[1])
            if method == "GET" and parts == ["cameras"]:
                return 200, self._cameras()
            if len(parts) == 2 and parts[0] == "cameras" and method == "GET":
                cam_id = self._known(parts[1])
                return 200, self._camera_document(cam_id)
            if len(parts) == 3 and parts[0] == "cameras" and parts[2] in {"preview", "preview.jpg"} and method == "GET":
                cam_id = self._known(parts[1])
                return self._preview(cam_id)
            if len(parts) == 3 and parts[0] == "cameras" and parts[2] == "settings" and method == "GET":
                cam_id = self._known(parts[1])
                return 200, self._settings_document(cam_id)
            if len(parts) == 3 and parts[0] == "cameras" and parts[2] == "settings" and method in {"PUT", "POST"}:
                cam_id = self._known(parts[1])
                return self._update(cam_id, body)
            if len(parts) == 3 and parts[0] == "cameras" and parts[2] == "restart" and method == "POST":
                cam_id = self._known(parts[1])
                return self._restart(cam_id)
            if len(parts) == 3 and parts[0] == "cameras" and parts[2] == "autofocus" and method == "POST":
                cam_id = self._known(parts[1])
                return self._autofocus(cam_id)
            if len(parts) == 3 and parts[0] == "cameras" and parts[2] == "snapshot" and method == "POST":
                cam_id = self._known(parts[1])
                return self._snapshot(cam_id, body)
            if len(parts) == 3 and parts[0] == "cameras" and parts[2] == "record" and method == "POST":
                cam_id = self._known(parts[1])
                return self._record(cam_id, body)
            if len(parts) == 3 and parts[0] == "cameras" and parts[2] == "captures" and method == "GET":
                cam_id = self._known(parts[1])
                return 200, self._capture_list(cam_id)
        except KeyError as exc:
            return 404, {"error": "unknown camera", "id": str(exc).strip("'")}
        except SettingsError as exc:
            return 400, {"error": str(exc), "fields": exc.fields}
        return 404, {"error": "not found", "path": route}

    def _index(self) -> dict:
        return {
            "service": "pidentify-camctl",
            "version": __version__,
            "endpoints": [
                {"method": "GET", "path": "/"},
                {"method": "GET", "path": "/health"},
                {"method": "GET", "path": "/cameras"},
                {"method": "GET", "path": "/cameras/{id}"},
                {"method": "GET", "path": "/cameras/{id}/settings"},
                {"method": "PUT", "path": "/cameras/{id}/settings"},
                {"method": "POST", "path": "/cameras/{id}/settings"},
                {"method": "POST", "path": "/cameras/{id}/restart"},
                {"method": "POST", "path": "/cameras/{id}/autofocus"},
                {"method": "POST", "path": "/cameras/{id}/snapshot"},
                {"method": "POST", "path": "/cameras/{id}/record"},
                {"method": "GET", "path": "/cameras/{id}/captures"},
                {"method": "GET", "path": "/cameras/{id}/preview.jpg"},
                {"method": "GET", "path": "/captures"},
                {"method": "GET", "path": "/captures/{name}"},
                {"method": "GET", "path": "/ui"},
            ],
        }

    def _authorized(self, headers) -> bool:
        token = self._token()
        if not token:
            return True
        header = ""
        if headers is not None and hasattr(headers, "get"):
            header = headers.get("Authorization") or ""
        if isinstance(header, bytes):
            header = header.decode("utf-8", "replace")
        scheme, _, rest = header.partition(" ")
        if scheme.lower() == "bearer":
            return hmac.compare_digest(rest.strip(), token)
        if scheme.lower() == "basic":
            try:
                decoded = base64.b64decode(rest.strip()).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return False
            password = decoded.split(":", 1)[1] if ":" in decoded else ""
            return hmac.compare_digest(password, token)
        return False

    def _node_file(self) -> dict:
        path = os.path.join(self.config_dir, "config")
        try:
            return load_node_config(path)
        except (OSError, ConfigError):
            return {}

    def _token(self) -> str:
        return str(self._node_file().get("control_token") or "")

    def _limit_bytes(self) -> int:
        raw = str(self._node_file().get("capture_limit_mb") or "")
        if raw.isdigit():
            return int(raw) * 1024 * 1024
        return DEFAULT_LIMIT_BYTES

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

    def _settings_document(self, cam_id: str) -> dict:
        return {
            "id": cam_id,
            "settings": self._settings_for(cam_id),
            "capabilities": self._capabilities(cam_id),
        }

    def _camera_record(self, cam_id: str) -> dict:
        path = os.path.join(self.config_dir, "cameras.json")
        if not os.path.exists(path):
            return {"id": cam_id}
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
        for camera in document.get("cameras") or []:
            if camera.get("id") == cam_id:
                return camera
        return {"id": cam_id}

    def _capabilities(self, cam_id: str) -> dict:
        return capabilities(self._camera_record(cam_id), encoder=select_encoder(read_board_text()))

    def _update(self, cam_id: str, body: bytes | None) -> tuple[int, dict]:
        patch, restart, reset = _parse_settings_body(body)
        camera = self._camera_record(cam_id)
        with self._lock:
            stored = load_settings(self._settings_path())
            current = stored.get(cam_id) or default_settings()
            if reset:
                stored[cam_id] = default_settings()
            else:
                stored[cam_id] = merge_patch(current, patch, camera=camera)
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

    def _require_autofocus(self, cam_id: str) -> None:
        camera = self._camera_record(cam_id)
        model = str(camera.get("model") or "this sensor")
        if not autofocus_supported(model):
            raise SettingsError(
                "autofocus is not supported on {0}".format(model),
                {"autofocus": "autofocus is not supported on {0}".format(model)},
            )

    def _resume(self, cam_id: str) -> dict:
        return self.kicker(cam_id, api_base=self.api_base, publish_cmd=self.publish_cmd)

    def _autofocus(self, cam_id: str) -> tuple[int, dict]:
        self._require_autofocus(cam_id)
        settings = self._settings_for(cam_id)
        argv = build_autofocus_argv(camera_index(cam_id), settings)

        def action():
            return self.af_runner(argv)

        result, error, restart, elapsed, live = run_pause_resume(lambda: self.pauser(cam_id), lambda: self._resume(cam_id), action)
        if error:
            return 502, {
                "error": str(error),
                "id": cam_id,
                "restart": restart,
                "interrupted_ms": elapsed,
            }
        note = SNAPSHOT_NOTE if live else "No live publisher was running. The next viewer starts the camera."
        return 200, {
            "id": cam_id,
            "ok": True,
            "lens_position": result.get("lens_position"),
            "af_state": result.get("af_state"),
            "interrupted_ms": elapsed,
            "stream_was_live": live,
            "restart": restart,
            "note": note,
        }

    def _snapshot(self, cam_id: str, body: bytes | None):
        width, height, jpeg_quality = _parse_snapshot_body(body)
        camera = self._camera_record(cam_id)
        if width is None or height is None:
            width, height = _largest_resolution(camera)
        allowed = {(int(item["width"]), int(item["height"])) for item in allowed_resolutions(camera)}
        if allowed and (width, height) not in allowed:
            listed = ", ".join("{0}x{1}".format(item["width"], item["height"]) for item in allowed_resolutions(camera))
            raise SettingsError(
                "invalid settings",
                {"width": "resolution {0}x{1} is not in this sensor's mode list ({2})".format(width, height, listed)},
            )
        settings = self._settings_for(cam_id)
        argv = build_snapshot_argv(
            camera_index(cam_id),
            settings,
            width=width,
            height=height,
            jpeg_quality=jpeg_quality,
        )

        def action():
            return self.snapshot_runner(argv)

        jpeg, error, _restart, elapsed, _live = run_pause_resume(
            lambda: self.pauser(cam_id),
            lambda: self._resume(cam_id),
            action,
        )
        if error or not jpeg:
            return 502, {"error": str(error or "snapshot produced no image"), "id": cam_id, "interrupted_ms": elapsed}
        return 200, RawBody(
            jpeg,
            "image/jpeg",
            {
                "X-Stream-Interrupted-Ms": str(elapsed),
                "X-Stream-Note": SNAPSHOT_NOTE,
                "Content-Disposition": 'inline; filename="{0}.jpg"'.format(cam_id),
            },
        )

    def _preview(self, cam_id: str):
        argv = build_preview_argv(cam_id)
        try:
            jpeg = self.preview_runner(argv)
        except Exception as exc:  # noqa: BLE001 - surface ffmpeg failures as 503
            return 503, {"error": "preview failed: {0}".format(exc), "id": cam_id}
        if not jpeg:
            return 503, {"error": "preview produced no image", "id": cam_id}
        return 200, RawBody(
            jpeg,
            "image/jpeg",
            {"X-Stream-Interrupted-Ms": "0", "X-Stream-Note": PREVIEW_NOTE, "Cache-Control": "no-store"},
        )

    def _record(self, cam_id: str, body: bytes | None) -> tuple[int, dict]:
        seconds = _parse_record_body(body)
        from pidentify.capture import captures_dir, clip_name

        directory = captures_dir(self.state_dir)
        name = clip_name(cam_id)
        dest = os.path.join(directory, name)
        argv = build_record_argv(cam_id, seconds, dest)
        try:
            self.record_runner(argv)
        except Exception as exc:  # noqa: BLE001
            return 502, {"error": "record failed: {0}".format(exc), "id": cam_id}
        if not os.path.isfile(dest):
            return 502, {"error": "record produced no file", "id": cam_id}
        removed = prune_captures(self.state_dir, self._limit_bytes(), protect=name)
        return 200, {
            "id": cam_id,
            "file": name,
            "bytes": os.path.getsize(dest),
            "seconds": seconds,
            "download": "/captures/{0}".format(name),
            "pruned": removed,
            "used_bytes": used_bytes(self.state_dir),
            "limit_bytes": self._limit_bytes(),
            "note": RECORD_NOTE,
        }

    def _capture_list(self, cam_id: str | None) -> dict:
        return {
            "captures": list_captures(self.state_dir, cam_id),
            "used_bytes": used_bytes(self.state_dir),
            "limit_bytes": self._limit_bytes(),
        }

    def _capture_download(self, name: str):
        base = safe_clip(name)
        path = os.path.join(self.state_dir, "captures", base)
        if not os.path.isfile(path):
            return 404, {"error": "unknown capture", "name": base}
        with open(path, "rb") as handle:
            data = handle.read()
        return 200, RawBody(
            data,
            "video/mp4",
            {"Content-Disposition": 'attachment; filename="{0}"'.format(base)},
        )

    def _default_pause(self, cam_id: str) -> bool:
        found = list_publishers("/proc", cam_id)
        if not found:
            return False
        _signal_groups(found, lambda pid, sig: os.kill(pid, sig))
        import time

        time.sleep(0.4)
        return True

    def _run(self, argv: list[str], timeout: int) -> subprocess.CompletedProcess:
        try:
            completed = subprocess.run(argv, check=False, capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(str(exc)) from exc
        if completed.returncode != 0:
            err = (completed.stderr or b"").decode("utf-8", "replace")[-500:]
            raise RuntimeError(err or "exit {0}".format(completed.returncode))
        return completed

    def _default_snapshot(self, argv: list[str]) -> bytes:
        return self._run(argv, 20).stdout

    def _default_preview(self, argv: list[str]) -> bytes:
        return self._run(argv, 15).stdout

    def _default_record(self, argv: list[str]) -> None:
        seconds = 10
        if "-t" in argv:
            seconds = int(argv[argv.index("-t") + 1])
        self._run(argv, seconds + 20)

    def _default_af(self, argv: list[str]) -> dict:
        completed = self._run(argv, 15)
        return parse_metadata(completed.stdout.decode("utf-8", "replace"))

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


def _parse_settings_body(body: bytes | None) -> tuple[dict, bool, bool]:
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
    reset = False
    if "reset" in payload:
        reset = payload["reset"]
        if not isinstance(reset, bool):
            raise SettingsError("invalid settings", {"reset": "expected true or false"})
    if reset and "settings" in payload:
        raise SettingsError("invalid settings", {"reset": "reset replaces the settings; do not send both"})
    if "settings" in payload and isinstance(payload["settings"], dict):
        patch = dict(payload["settings"])
        extra = set(payload) - {"settings", "restart", "reset"}
        if extra:
            raise SettingsError("invalid settings", {key: "unknown field" for key in sorted(extra)})
    else:
        patch = {key: value for key, value in payload.items() if key not in {"restart", "reset"}}
    return patch, restart, reset


def _parse_snapshot_body(body: bytes | None) -> tuple[int | None, int | None, int]:
    if not body:
        return None, None, 90
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SettingsError("invalid settings", {"_": "body is not JSON"}) from exc
    if payload is None:
        return None, None, 90
    if not isinstance(payload, dict):
        raise SettingsError("invalid settings", {"_": "expected a JSON object"})
    width = payload.get("width")
    height = payload.get("height")
    if (width is None) != (height is None):
        raise SettingsError("invalid settings", {"width": "pass both width and height, or neither for full resolution"})
    if width is not None:
        if isinstance(width, bool) or isinstance(height, bool):
            raise SettingsError("invalid settings", {"width": "expected integers"})
        width = int(width)
        height = int(height)
    quality = payload.get("quality", payload.get("jpeg_quality", 90))
    if isinstance(quality, bool) or not isinstance(quality, (int, float)) or not 1 <= int(quality) <= 100:
        raise SettingsError("invalid settings", {"quality": "JPEG quality must be 1..100"})
    return width, height, int(quality)


def _parse_record_body(body: bytes | None) -> int:
    if not body:
        raise SettingsError("invalid settings", {"seconds": "expected {seconds: 1..120}"})
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SettingsError("invalid settings", {"_": "body is not JSON"}) from exc
    if not isinstance(payload, dict) or "seconds" not in payload:
        raise SettingsError("invalid settings", {"seconds": "expected {seconds: 1..120}"})
    seconds = payload["seconds"]
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or int(seconds) != seconds:
        raise SettingsError("invalid settings", {"seconds": "expected a whole number of seconds"})
    seconds = int(seconds)
    if not 1 <= seconds <= 120:
        raise SettingsError("invalid settings", {"seconds": "expected 1..120"})
    return seconds


def _largest_resolution(camera: dict) -> tuple[int, int]:
    allowed = allowed_resolutions(camera)
    if not allowed:
        return 4056, 3040
    best = max(allowed, key=lambda item: int(item["width"]) * int(item["height"]))
    return int(best["width"]), int(best["height"])


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
        status, payload = self.app.dispatch(method, self.path, body, client, self.headers)
        self._send(status, payload)

    def _send(self, status: int, payload) -> None:
        extra = {}
        if isinstance(payload, RawBody):
            encoded = payload.data
            content_type = payload.content_type
            extra = dict(payload.headers)
        else:
            encoded = json.dumps(payload).encode("utf-8")
            content_type = "application/json"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        if "Cache-Control" not in extra:
            self.send_header("Cache-Control", "no-store")
        if status == 401:
            self.send_header("WWW-Authenticate", 'Basic realm="pidentify"')
        for key, value in extra.items():
            if key.lower() == "cache-control":
                continue
            self.send_header(key, value)
        if "Cache-Control" in extra:
            self.send_header("Cache-Control", extra["Cache-Control"])
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
