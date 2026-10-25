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

Stdlib Python on `:8555`. No authentication. Partial JSON merge, not a full replace. `null` clears an optional control. `extra_args` is rejected over HTTP (edit `/etc/pidentify/cam_settings.json` on the Pi) so the API cannot inject a shell command. PUT and POST both merge. A body may be the settings object itself, or `{"settings": {...}, "restart": false}`. A settings write returns 200 after the file is saved; `restart.ok` says whether the live path was kicked.

| Method | Path | Result |
| --- | --- | --- |
| GET | `/` | JSON index of the endpoints below |
| GET | `/health` | `{"ok": true, ...}` |
| GET | `/cameras` | detected cameras, settings, `streaming` |
| GET | `/cameras/<id>` | one camera |
| GET | `/cameras/<id>/settings` | settings object |
| PUT, POST | `/cameras/<id>/settings` | merge, then kick that path |
| POST | `/cameras/<id>/restart` | kick only. 502 if MediaMTX is down and nothing is publishing |

`<id>` is `cam0` or `cam1`. The kick PATCHes MediaMTX `runOnDemand` (local) or `runOnInit` (push) with a generation token so the publisher re-reads `cam_settings.json`. The on-disk yml command stays `pidentify-publish camN`. If the API is down, the publisher process group gets SIGTERM and MediaMTX restarts it while a viewer is still connected.

| Key | Range |
| --- | --- |
| width, height | even, 160–8192 and 120–8192. Default 1280x720 |
| fps | integer 1–120. Default 30 |
| bitrate | 100000–25000000. Default 2000000 |
| brightness | -1..1, or null |
| contrast, saturation | 0..32, or null |
| sharpness | 0..16, or null |
| ev | -10..10, or null |
| awb | auto, incandescent, tungsten, fluorescent, indoor, daylight, cloudy, custom, or null |
| shutter | microseconds 1..10000000, or null |
| gain | 1..64, or null |
| autofocus_mode | default, manual, auto, continuous, or null |
| lens_position | dioptres 0..32 (0 = infinity), `"default"`, or null |
| hdr | off, auto, sensor, single-exp. JSON `true` is auto and `false` is off |
| hflip, vflip | booleans. Default false |
| rotation | 0 or 180 |

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
