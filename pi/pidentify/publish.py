"""On-demand publisher started by MediaMTX for one CSI camera.

MediaMTX runs this when a client opens the path (local mode) and also from
runOnInit while push mode is on. It sends SIGINT to the process group when
the last reader has been gone for runOnDemandCloseAfter. The command
publishes H.264 back to MediaMTX over RTSP/TCP.

Pi 4 and earlier have a hardware H.264 encoder: ``--codec h264 --inline
--flush -o -`` is an elementary stream, and ffmpeg reads ``-f h264``. Pi 5
has no H.264 block. There ``--codec h264`` still enters libav, and ``-o -``
fails unless ``--libav-format`` is set, so Pi 5 uses ``--codec libav
--libav-video-codec libx264 --libav-format mpegts``. ffmpeg copies either
stdout into ``rtsp://127.0.0.1:8554/<path>`` and does not re-encode.
``--low-latency`` is added only when this rpicam-vid's --help lists it.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time

from pidentify.settings import (
    CAMERA_IDS,
    SettingsError,
    build_ffmpeg_argv,
    build_rpicam_argv,
    camera_index,
    load_settings,
    read_board_text,
    select_encoder,
)

CONFIG_DIR = os.environ.get("PIDENTIFY_CONFIG_DIR", "/etc/pidentify")


def which(name: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def rpicam_binary() -> str | None:
    return which("rpicam-vid") or which("libcamera-vid")


def supports_low_latency(binary: str) -> bool:
    try:
        completed = subprocess.run(
            [binary, "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    text = (completed.stdout or "") + (completed.stderr or "")
    return "--low-latency" in text


def rtsp_port() -> int:
    raw = os.environ.get("RTSP_PORT", "8554")
    if not raw.isdigit():
        return 8554
    port = int(raw)
    return port if 1 <= port <= 65535 else 8554


def _pump(pipe, prefix: bytes) -> None:
    try:
        for line in iter(pipe.readline, b""):
            sys.stderr.buffer.write(prefix + line)
            sys.stderr.buffer.flush()
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _terminate(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + 2
    for process in processes:
        remaining = deadline - time.monotonic()
        if process.poll() is None and remaining > 0:
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                pass
        if process.poll() is None:
            process.kill()


def run(cam_id: str) -> int:
    if cam_id not in CAMERA_IDS:
        print(f"[pidentify-publish] refusing camera id {cam_id}", file=sys.stderr)
        return 2
    binary = rpicam_binary()
    ffmpeg = which("ffmpeg")
    if not binary or not ffmpeg:
        print(
            f"[pidentify-publish] missing dependency rpicam={binary} ffmpeg={ffmpeg}",
            file=sys.stderr,
        )
        time.sleep(2)
        return 1
    try:
        settings = load_settings(os.path.join(CONFIG_DIR, "cam_settings.json"))
    except (OSError, SettingsError, ValueError) as exc:
        print(f"[pidentify-publish] cannot read settings: {exc}", file=sys.stderr)
        time.sleep(2)
        return 1
    cfg = settings.get(cam_id)
    if cfg is None:
        print(f"[pidentify-publish] no settings for {cam_id}", file=sys.stderr)
        time.sleep(2)
        return 1
    low_latency = supports_low_latency(binary)
    board = read_board_text()
    encoder = select_encoder(board)
    container = "mpegts" if encoder == "libav" else "h264"
    rpicam_argv = build_rpicam_argv(
        camera_index(cam_id),
        cfg,
        binary=binary,
        low_latency=low_latency,
        encoder=encoder,
    )
    ffmpeg_argv = build_ffmpeg_argv(
        cam_id,
        cfg,
        rtsp_port=rtsp_port(),
        binary=ffmpeg,
        container=container,
    )
    print(
        f"[pidentify-publish] encoder={encoder} board={board or 'unknown'}",
        file=sys.stderr,
    )
    print(f"[pidentify-publish] starting {' '.join(rpicam_argv)}", file=sys.stderr)
    print(f"[pidentify-publish] ffmpeg {' '.join(ffmpeg_argv)}", file=sys.stderr)
    started = time.monotonic()
    rpicam = subprocess.Popen(rpicam_argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if rpicam.stdout is None:
        print("[pidentify-publish] failed to capture rpicam-vid stdout", file=sys.stderr)
        _terminate([rpicam])
        return 1
    try:
        ffmpeg_proc = subprocess.Popen(
            ffmpeg_argv,
            stdin=rpicam.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        print(f"[pidentify-publish] failed to start ffmpeg: {exc}", file=sys.stderr)
        _terminate([rpicam])
        time.sleep(2)
        return 1
    rpicam.stdout.close()
    threads = []
    if rpicam.stderr is not None:
        threads.append(threading.Thread(target=_pump, args=(rpicam.stderr, b"[rpicam] "), daemon=True))
    if ffmpeg_proc.stderr is not None:
        threads.append(threading.Thread(target=_pump, args=(ffmpeg_proc.stderr, b"[ffmpeg] "), daemon=True))
    for thread in threads:
        thread.start()

    stopping = {"value": False}

    def _stop(_signum, _frame) -> None:
        stopping["value"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    exit_code = 0
    signaled = False
    while True:
        if stopping["value"]:
            signaled = True
            _terminate([ffmpeg_proc, rpicam])
            exit_code = 0
            break
        rpicam_code = rpicam.poll()
        ffmpeg_code = ffmpeg_proc.poll()
        if rpicam_code is not None or ffmpeg_code is not None:
            _terminate([ffmpeg_proc, rpicam])
            exit_code = 0 if rpicam_code in (0, None) and ffmpeg_code in (0, None) else 1
            break
        time.sleep(0.2)
    # A fast exit usually means the sensor or the ffmpeg flags failed.
    # Sleep so MediaMTX's runOnDemandRestart does not tight-loop. A signal
    # from MediaMTX (the viewer left) should return immediately.
    if not signaled and time.monotonic() - started < 3:
        time.sleep(2)
        exit_code = 1
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: pidentify-publish cam0|cam1 [ignored-generation]", file=sys.stderr)
        return 2
    return run(args[0])


if __name__ == "__main__":
    raise SystemExit(main())
