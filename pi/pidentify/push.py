"""Copy one local MediaMTX path to the Jetson RTSP server.

MediaMTX starts this from runOnReady while push mode is on. It does not open
the CSI camera. pidentify-publish already feeds the local path; ffmpeg copies
that RTSP/TCP stream to push_url-camN. The process retries with backoff when
the Jetson restarts or the network drops. SIGTERM (MediaMTX stopping the hook
because the local path went down) exits without another retry.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from pidentify.nodeconfig import ConfigError, load_node_config, parse_push_target
from pidentify.publish import which
from pidentify.settings import CAMERA_IDS, SettingsError, build_push_ffmpeg_argv

CONFIG_DIR = os.environ.get("PIDENTIFY_CONFIG_DIR", "/etc/pidentify")
_BACKOFF_START = 2
_BACKOFF_MAX = 15


def rtsp_port() -> int:
    raw = os.environ.get("RTSP_PORT", "8554")
    if str(raw).isdigit() and 1 <= int(raw) <= 65535:
        return int(raw)
    return 8554


def push_argv(cam_id: str, config_dir: str = CONFIG_DIR) -> list[str]:
    if cam_id not in CAMERA_IDS:
        raise SettingsError(f"unknown camera {cam_id}")
    cfg = load_node_config(os.path.join(config_dir, "config"))
    if cfg["mode"] != "push":
        raise ConfigError(f"mode is {cfg['mode']}; pidentify-push is only used in push mode")
    target = parse_push_target(cfg["push_url"])
    remote = target["targets"][cam_id]
    ffmpeg = which("ffmpeg")
    if not ffmpeg:
        raise SettingsError("ffmpeg is not installed")
    return build_push_ffmpeg_argv(cam_id, remote, local_port=rtsp_port(), binary=ffmpeg)


def _sleep(seconds: float, stop: dict) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if stop["flag"]:
            return False
        time.sleep(0.2)
    return not stop["flag"]


def run(cam_id: str, config_dir: str = CONFIG_DIR) -> int:
    stop = {"flag": False}

    def _stop(_signum, _frame) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    delay = _BACKOFF_START
    while not stop["flag"]:
        try:
            argv = push_argv(cam_id, config_dir)
        except (ConfigError, SettingsError, OSError) as exc:
            print(f"[push] {cam_id}: {exc}", file=sys.stderr)
            if not _sleep(delay, stop):
                return 0
            delay = min(delay * 2, _BACKOFF_MAX)
            continue
        print(f"[push] {cam_id}: {' '.join(argv)}", file=sys.stderr)
        try:
            proc = subprocess.Popen(argv)
        except OSError as exc:
            print(f"[push] {cam_id}: failed to start ffmpeg: {exc}", file=sys.stderr)
            if not _sleep(delay, stop):
                return 0
            delay = min(delay * 2, _BACKOFF_MAX)
            continue
        started = time.monotonic()
        while proc.poll() is None:
            if stop["flag"]:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return 0
            time.sleep(0.2)
        ran = time.monotonic() - started
        if ran >= 10:
            delay = _BACKOFF_START
        print(
            f"[push] {cam_id}: ffmpeg exited {proc.returncode}; retrying in {delay}s",
            file=sys.stderr,
        )
        if not _sleep(delay, stop):
            return 0
        delay = min(delay * 2, _BACKOFF_MAX)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in CAMERA_IDS:
        print("usage: pidentify-push cam0|cam1", file=sys.stderr)
        return 2
    return run(args[0])


if __name__ == "__main__":
    raise SystemExit(main())
