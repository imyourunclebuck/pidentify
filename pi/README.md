# pidentify on the Raspberry Pi

Installer for a Raspberry Pi 5 camera node so the Jetson Camera Wall can find it and pull H.264. Fleet addresses, hostnames, and the stream contract are in [FLEET.md](FLEET.md).

Targets: Raspberry Pi 5, Raspberry Pi OS Bookworm or Trixie (64-bit), `rpicam-apps` or `libcamera-apps`. The three Myrtle boards share the system hostname `pi5`. Pass `--name` so each one gets its own mDNS name. Tailscale 1.104.2 is already installed and logged in on the nodes; this installer does not install or join Tailscale.

## Install

After this branch is merged:

```bash
curl -fsSL https://raw.githubusercontent.com/imyourunclebuck/pidentify/main/pi/install.sh | bash
```

A pipe cannot type a sudo password. If sudo is not passwordless:

```bash
curl -fsSL https://raw.githubusercontent.com/imyourunclebuck/pidentify/main/pi/install.sh | sudo bash
```

Before merge, the script on `pi-side` must also download the rest of `pi/` from that branch. `curl | bash` does not remember the URL it came from:

```bash
curl -fsSL https://raw.githubusercontent.com/imyourunclebuck/pidentify/pi-side/pi/install.sh | PIDENTIFY_REF=pi-side bash
curl -fsSL https://raw.githubusercontent.com/imyourunclebuck/pidentify/pi-side/pi/install.sh | bash -s -- --ref pi-side
```

Name each Myrtle node, for example on cam 1:

```bash
curl -fsSL https://raw.githubusercontent.com/imyourunclebuck/pidentify/pi-side/pi/install.sh | PIDENTIFY_REF=pi-side bash -s -- --name pi5-cam-1-myrtle
```

Use `pi5-cam-2-myrtle` and `pi5-cam-3-myrtle` on the other two. Re-running with no `--name`, `--push-to`, or `--local` keeps the values in `/etc/pidentify/config`.

The installer does not edit `/etc/hostname`, `/etc/hosts`, or `/boot/firmware/config.txt`, and it does not configure port forwarding or UPnP.

## What gets installed

| Piece | Where |
| --- | --- |
| MediaMTX v1.21.2 (arm64, checksum pinned) | `/opt/pidentify/mediamtx` |
| Config | `/etc/pidentify/mediamtx.yml`, `cam_settings.json`, `cameras.json`, `node.json`, `config` |
| Publisher, pusher, camctl, status, uninstall | `/usr/local/bin/pidentify-*` |
| User `pidentify` | nologin, groups `video` and `render` when those groups exist |
| Units | `pidentify-mediamtx.service`, `pidentify-camctl.service` (enabled, `Restart=on-failure`) |
| mDNS | `/etc/avahi/services/pidentify.service` and Avahi `host-name=` |

Listeners bind every IPv4 interface, including `tailscale0`:

| Port | Service |
| --- | --- |
| 8554/tcp | RTSP. UDP RTP :8000 and RTCP :8001 are not opened. Clients use RTSP/TCP (`ffmpeg -rtsp_transport tcp`, GStreamer `protocols=tcp`). |
| 8555 | camctl JSON API |
| 9997 | MediaMTX API. `GET /v3/paths/list` from the LAN or the tailnet needs no password. |

Local stream URLs, one per detected CSI camera:

```text
rtsp://<lan-or-tailnet-ip>:8554/cam0
rtsp://<magicdns-name>:8554/cam1
```

Default picture: H.264, 1280x720@30, about 2 Mbps. `runOnDemand` in local mode opens the camera only while someone is watching, so an idle camera sends nothing over the tailnet. `runOnDemandCloseAfter` is 10s.

### Why ffmpeg sits between rpicam-vid and MediaMTX

The on-disk hook stays `/usr/local/bin/pidentify-publish camN` (runOnDemand locally, runOnInit in push mode). That process picks the encoder from the board and pipes stdout to ffmpeg, which copies H.264 to `rtsp://127.0.0.1:8554/<path>` over TCP and does not re-encode. Push mode uses the same publisher; `pidentify-push` only copies the local RTSP path onward.

Pi 5 has no hardware H.264 encoder. `rpicam-vid --codec h264` still enters libav there, and `-o -` fails unless `--libav-format` is set (`Unable to choose output format for '-'`). Pi 5 therefore runs `--codec libav --libav-video-codec libx264 --libav-format mpegts -o -`, and ffmpeg reads `-f mpegts` with a 5 MB probe and a 2 s analyze window so the picture size is known. `--low-latency` is added when `rpicam-vid --help` lists it (Trixie does; Bookworm's rpicam-apps 1.5 has `--flush` and does not).

Pi 4, Pi 400, CM4, and earlier boards that report an H.264 block use `--codec h264 --inline --flush -o -`. ffmpeg reads that elementary stream with `-f h264 -framerate <fps>`. An unrecognised board uses the Pi 5 libav path, which still produces H.264. `PIDENTIFY_ENCODER=hardware|libav` overrides detection. MediaMTX's native `rpiCamera` source is not used.

### Names

The system hostname stays whatever Raspberry Pi OS has, including the shared `pi5`, so sudo and `/etc/hosts` are left alone. `--name pi5-cam-1-myrtle` (underscores are rewritten to hyphens) is stored and used for:

- Avahi `host-name=` under `[server]` only, so the mDNS hostname is `pi5-cam-1-myrtle.local`
- the mDNS service name
- the TXT `node=` value
- the default push prefix

Avahi advertises `_rtsp._tcp` on port 8554 with TXT `paths=` (only detected cameras), `camctl=8555`, `api=9997`, and `node=<name>`. `allow-interfaces` is the LAN NICs. `deny-interfaces` always includes `tailscale0`, plus `docker0` and other virtual interfaces. The summary and `pidentify-status` omit those interfaces. When the `tailscale` CLI is present they also print `tailscale ip -4` and the MagicDNS name from `tailscale status --json`, and the stream URLs that use them.

MediaMTX allows read and API from RFC1918, link-local, and Tailscale CGNAT `100.64.0.0/10` (IPv6 tailnet addresses fall in `fc00::/7`). Publish is localhost only. MediaMTX 1.21.2 has one `api` action, so a client who can `GET /v3/paths/list` can also call the config API. camctl is the control plane the Jetson should use. camctl itself answers only LAN and tailnet clients.

## Push mode

Local mode is the default. Push is the fallback when the Jetson should receive a continuous publish instead of pulling:

```bash
sudo bash install.sh --name pi5-cam-1-myrtle --push-to rtsp://jetson-camwall:8554/pi5-cam-1-myrtle
```

That publishes continuously to `rtsp://jetson-camwall:8554/pi5-cam-1-myrtle-cam0` and `-cam1`. A URL with no path uses `--name` as the prefix. `rtsp://192.168.1.218:8554/myrtle1` is the same idea with an explicit prefix.

The camera still belongs to the local MediaMTX. Push mode switches the path from `runOnDemand` to `runOnInit` (rpicam-vid piped to ffmpeg, restarted if it exits) and `runOnReady` (`pidentify-push`). The pusher copies `rtsp://127.0.0.1:8554/camN` to the Jetson with `ffmpeg -c copy` and retries with backoff if the Jetson or the network drops. `runOnReadyRestart` is the second supervisor if that process dies. A separate systemd unit that ran rpicam-vid straight at the Jetson would hold the CSI device, so local `:8554` could not open the same camera. Local viewers and camctl keep working; the camera stays open for as long as push mode is on.

Switch back with `sudo bash install.sh --local`. The stored push URL is kept and not used. Omit both flags on a later run to keep the current mode.

## camctl

Stdlib Python on `:8555`, bound on every IPv4 interface including `tailscale0`. Clients must be loopback, LAN, or Tailscale CGNAT. The control page is `http://<tailscale-name>:8555/ui`. The full surface is [Camera control API](#camera-control-api).

## Camera control API

Settings live in `/etc/pidentify/cam_settings.json`, one object per `cam0` or `cam1`. A write merges. `null` clears an optional control. `extra_args` is rejected over HTTP. A body may be the settings object, or `{"settings": {...}, "restart": false}`. `{"reset": true}` restores defaults and restarts that camera. The response is 200 after the file is saved; `restart.ok` says whether that camera's publisher was kicked. A bad value is 400 with `error` and `fields`.

GET `/cameras/<id>/settings` returns `settings` and `capabilities`. `capabilities.resolutions` is the sensor mode list from `rpicam-hello --list-cameras` plus the scaled sizes rpicam-vid can produce from those modes (for an imx708: 640x480, 1280x720, 1536x864, 1920x1080, 2304x1296, 4608x2592; for an imx477: 640x480, 1280x720, 1332x990, 1920x1080, 2028x1080, 2028x1520, 4056x3040). Each entry has `max_fps`. A size outside that list is 400.

Changing stream, image, or autofocus settings rebuilds only that camera's `rpicam-vid` command and restarts only that path (`runOnDemand` locally, `runOnInit` while pushing). The on-disk MediaMTX command stays `pidentify-publish camN`.

| Method | Path | Result |
| --- | --- | --- |
| GET | `/` | JSON index of these routes |
| GET | `/health` | `{"ok": true, ...}` |
| GET | `/ui` | control page (preview, sliders from capabilities, Autofocus, Snapshot, Record, reset) |
| GET | `/cameras` | detected cameras, settings, `streaming` |
| GET | `/cameras/<id>` | one camera |
| GET | `/cameras/<id>/settings` | settings plus capabilities |
| PUT, POST | `/cameras/<id>/settings` | merge, then kick that camera |
| POST | `/cameras/<id>/restart` | kick only. 502 if MediaMTX is down and nothing is publishing |
| POST | `/cameras/<id>/autofocus` | one-shot AF cycle. Returns `lens_position`. 400 `autofocus is not supported on imx477` for fixed-focus sensors |
| POST | `/cameras/<id>/snapshot` | JPEG body. Optional `{"width", "height", "quality"}`. `quality` here is JPEG 1–100, default 90. Omit width and height for the largest mode |
| GET | `/cameras/<id>/preview.jpg` | one JPEG from the live RTSP path |
| POST | `/cameras/<id>/record` | `{"seconds": 1..120}`. Saves an MP4 copied from `rtsp://127.0.0.1:8554/<id>` |
| GET | `/cameras/<id>/captures` and `/captures` | clip list, bytes used, and the cap |
| GET | `/captures/<name>` | download `video/mp4` |

Authentication is off unless `/etc/pidentify/config` sets `control_token`. Then every route needs `Authorization: Bearer <token>` or HTTP Basic with that password (any username). The browser prompt covers `/ui` and the preview image. `install.sh` keeps an existing token. `capture_limit_mb` defaults to 512; oldest clips in `/var/lib/pidentify/captures` are deleted first. A clip that by itself exceeds the cap is kept.

### Stream

| Key | Values |
| --- | --- |
| quality | `low`, `medium`, `high`, `max`, or null. Fills width, height, fps, bitrate, x264 preset/tune, and keyframe interval for a Pi 5 software encode. Other keys in the same request override the preset. `max` is the highest rate that still aims at 30 fps (1920x1080 on imx708, 2028x1520 on imx477), not the full sensor readout |
| width, height | a pair from `capabilities.resolutions`. Default 1280x720 |
| fps | integer 1–120, and not above that mode's `max_fps` |
| bitrate | 100000–25000000. Default 2000000 |
| keyframe_interval | frames 1–300, or null to use fps |
| x264_preset | ultrafast (default), superfast, veryfast, faster, fast, medium |
| x264_tune | zerolatency (default), film, animation, grain, fastdecode. On Pi 5 this is one argv element, `preset=NAME;tune=NAME`. rpicam-apps splits that string on `;`; a colon is part of the preset name and x264 rejects it. An unknown tune is omitted so the stream still starts. A Pi 4 hardware encoder stores the names and does not send them |
| hdr | off, auto, sensor, single-exp. JSON `true` is auto and `false` is off. imx708 only, and only at 2304x1296. Applying it restarts the camera. Other sensors get 400 |
| denoise | auto, off, cdn_off, cdn_fast, cdn_hq, or null |
| hflip, vflip | booleans. Default false |
| rotation | 0 or 180 |

### Image

| Key | Values |
| --- | --- |
| exposure | normal, short, long, or null |
| shutter | microseconds. `0` or null is auto (the flag is omitted). Max 10000000 |
| gain | analogue gain. `0` or null is auto. Max 64 |
| ev | -10..10, or null |
| metering | centre, spot, average, matrix, or null |
| awb | auto, incandescent, tungsten, fluorescent, indoor, daylight, cloudy, custom, or null. `greyworld` is accepted only when the model name contains `noir` |
| awbgains | `[red, blue]`, each 0..32, only when `awb` is `custom`. Switching AWB away from custom clears it |
| brightness | -1..1, or null |
| contrast, saturation | 0..32, or null |
| sharpness | 0..16, or null |

### Autofocus

imx708, including wide and NoIR, reports `capabilities.autofocus.supported: true`. imx477 and other fixed-focus sensors report `supported: false` and a 400 if a client sets any AF field.

| Key | Values |
| --- | --- |
| autofocus_mode | manual, auto, continuous, default, or null |
| af_range | normal, macro, full, or null |
| af_speed | normal, fast, or null |
| af_window | `{x, y, w, h}` normalized 0..1, with `x+w` and `y+h` at most 1, or null |
| lens_position | dioptres. imx708 is 0..10 (0 = infinity), or `"default"`, or null |

`POST /cameras/<id>/autofocus` stops that camera's publisher, runs `rpicam-still --autofocus-mode auto --autofocus-on-capture`, and restarts the publisher. The JSON body includes `lens_position`, `af_state`, `interrupted_ms`, and a note. The position is not written back; PUT `lens_position` to hold it.

### Snapshot and record

A snapshot needs the CSI device, so the live publisher for that camera is stopped, `rpicam-still` writes one JPEG, and the publisher is restarted. The response is `image/jpeg` with `X-Stream-Interrupted-Ms` and `X-Stream-Note`. Expect about 1–3 seconds of missing video. The preview image does not do this: it is one frame from the live RTSP path (`X-Stream-Interrupted-Ms: 0`). If the path is idle, MediaMTX starts it the same way a viewer would.

Record does not reopen the camera. ffmpeg copies the live RTSP feed to `/var/lib/pidentify/captures/camN-<utc>.mp4` for `seconds`. An idle on-demand path stays up for the length of the clip.

### Example: imx708, 1080p, continuous AF

```json
{
  "quality": "high",
  "width": 1920,
  "height": 1080,
  "fps": 30,
  "bitrate": 5000000,
  "keyframe_interval": 30,
  "x264_preset": "ultrafast",
  "x264_tune": "zerolatency",
  "hdr": "off",
  "denoise": "cdn_fast",
  "rotation": 0,
  "hflip": false,
  "vflip": false,
  "exposure": "normal",
  "shutter": 0,
  "gain": 0,
  "ev": 0,
  "metering": "centre",
  "awb": "auto",
  "awbgains": null,
  "brightness": 0,
  "contrast": 1,
  "saturation": 1,
  "sharpness": 1,
  "autofocus_mode": "continuous",
  "af_range": "normal",
  "af_speed": "normal",
  "af_window": null,
  "lens_position": null
}
```

`quality: "high"` alone sets the stream fields (1920x1080, 30 fps, 5 Mbps, ultrafast, zerolatency, keyframe interval 30). The image and autofocus fields above are explicit. HDR stays off because 1920x1080 is not the 2304x1296 HDR mode.

## Conflicts and uninstall

Before it starts anything, install looks for listeners on 8554, 8555, and 9997 and for processes holding `/dev/video*` or `/dev/media*` (a Picamera2 app such as `~/Desktop/app_full.py`, an existing `rpicam-vid`, or another mediamtx). It prints them and does not stop them. The units are still installed and enabled, but they are left stopped, so a reboot can race a program that is also enabled.

Listeners that belong to `pidentify-mediamtx`, `pidentify-camctl`, or the `pidentify` user are ours, including when `ss -p` cannot see the pid. `pidentify-status` does not call those conflicts.

Desktop images run WirePlumber, and its libcamera and v4l2 monitors open the camera devices. When that is the conflict, install writes a snippet and restarts WirePlumber for each logged-in user (`systemctl --user -M <user>@`, or `runuser` with `XDG_RUNTIME_DIR`). It does not stop or disable PipeWire, so audio and screencast keep working. WirePlumber 0.5 (Trixie) gets `/etc/wireplumber/wireplumber.conf.d/50-pidentify-no-camera.conf` (`monitor.libcamera` and `monitor.v4l2` disabled). WirePlumber 0.4 (Bookworm) gets `/etc/wireplumber/main.lua.d/51-pidentify-no-camera.lua` (`libcamera_monitor.enabled = false` and `v4l2_monitor.enabled = false`). The action is appended to `takeover.log`.

`--take-over` stops other conflicting processes. It does not uninstall or disable them, and it does not stop PipeWire. Each action is appended to `/var/lib/pidentify/takeover.log` with a reverse command (`systemctl start UNIT`, or restart a bare process by hand). It will not stop ssh, systemd, `user@` (that would kill the Pi Connect shell), Avahi, NetworkManager, or the installer's own parent processes. Its own units are not treated as conflicts. If anything is still holding a camera or port afterward, the new units stay stopped.

`pidentify-status` prints the units, the saved cameras, a live probe, conflicts, LAN addresses, Tailscale IPv4 and MagicDNS when `tailscale` is on the PATH, and an ffprobe of each local path when ffprobe is installed. The probe is a viewer, so in local mode it opens the camera. Exit 0 requires both units active and no conflicts; our own listeners are not conflicts.

`sudo pidentify-uninstall` removes the units, the Avahi service file, the binaries, `/opt/pidentify`, `/etc/pidentify`, and the WirePlumber snippet. After removing that snippet it restarts per-user WirePlumber so the desktop can open cameras again. It restores `avahi-daemon.conf` from the one-time backup, or deletes that file if this installer created it. Apt packages are removed only if this install added them and they are `ffmpeg`, `avahi-daemon`, `rpicam-apps`, or `libcamera-apps`.

If a successful probe finds zero cameras, the summary suggests checking `/boot/firmware/config.txt` and does not write it. A failed probe (camera busy, or no `rpicam-hello`) keeps the previous `cameras.json` and does not print that suggestion.

## Develop

```bash
python3 -m unittest discover -s pi/tests -v
shellcheck -x pi/install.sh pi/uninstall.sh pi/bin/*
```

The Python in `pi/` targets 3.9 and newer (Bullseye 3.9, Bookworm 3.11, Trixie 3.13). Annotations use `from __future__ import annotations`, so `X | Y` unions are not evaluated at runtime. `install.sh` byte-compiles `pidentify/` before it changes the system, and any failed step prints `INSTALL FAILED at step N` and exits non-zero.

`PIDENTIFY_ROOT` is a dry-run: no apt, useradd, or systemctl, and `--take-over` is ignored. `PIDENTIFY_SKIP_DOWNLOAD=1` skips the arm64 MediaMTX binary (required off a Pi). `PIDENTIFY_HOSTNAME` and `PIDENTIFY_LIST_CAMERAS_FILE` feed the hostname and a saved `rpicam-hello` transcript. `PIDENTIFY_ARCHIVE_URL` is a `file://` or `http://` tarball used instead of GitHub when the piped script has to download the rest of `pi/`. Installer progress is written to stderr so that download step can print the extracted path on stdout.

TEST (Pi 5, Trixie, Python 3.13) ran `40ef8e4` end to end: 1280x720@30 H.264, clean conflicts, the camctl index, and `rpicam-vid` at about 55% CPU. This revision compiles on Python 3.9 and newer before any system change and has not been re-run on a Pi. A Jetson client that only speaks RTSP/UDP is still untested. TCP-only RTSP is deliberate. The first viewer can wait up to `runOnDemandStartTimeout` (20s) while the camera opens.
