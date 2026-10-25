# pidentify fleet

Device and network facts for the Jetson Camera Wall and the Raspberry Pi camera nodes. Verified October 25, 2026. Tailnet IPv4 addresses were verified October 25, 2026 at 5:05 AM ET. This file has no secrets.

## Sites

Two physical LANs. The Jetson cannot route to the office, so anything that has to cross sites uses Tailscale.

| Site | LAN | Router | Devices |
| --- | --- | --- | --- |
| Home | 192.168.1.0/24 | 192.168.1.1 | Jetson Orin Nano (Camera Wall, port 8080, LAN IPs 192.168.1.189 and 192.168.1.218), grokbot pi5, TEST pi |
| Office ("Myrtle") | 192.168.2.0/24 | separate site | pi5_cam_1_myrtle, pi5_cam_2_myrtle, pi5_cam_3_myrtle |

The office is a separate physical site. The Jetson cannot reach 192.168.2.x directly. All cross-site traffic goes over Tailscale.

## Remote access

Tailscale tailnet on Aaron's account, with MagicDNS (`--accept-dns`). Tailscale 1.104.2 is already installed and logged in on all five Pis and the Jetson. install.sh does not install it.

| Role | Tailnet hostname | Tailnet IPv4 |
| --- | --- | --- |
| Jetson Camera Wall | jetson-camwall | 100.123.2.92 |
| GrokBot | grokbot-pi5 | 100.83.57.70 |
| TEST pi | pi5-test-hq | 100.78.74.47 |
| Myrtle cam 1 | pi5-cam-1-myrtle | 100.91.138.55 |
| Myrtle cam 2 | pi5-cam-2-myrtle | 100.83.252.55 |
| Myrtle cam 3 | pi5-cam-3-myrtle | 100.105.161.77 |
| Aaron's iPhone | iphone181 | 100.118.190.44 |

There is no port forwarding. Streams are LAN and tailnet only. A future ACL tag is `tag:cam`.

## Devices

The nodes in the table are Raspberry Pi 5 8GB boards. System hostname and the login user are the same string on each one (`pi5 / pi5` means hostname `pi5`, user `pi5`). Those three Myrtle boards share the system hostname `pi5`, so the tailnet name is what distinguishes them.

| Pi Connect name | Tailnet name | System hostname / user | OS | LAN IPs | CSI cameras |
| --- | --- | --- | --- | --- | --- |
| GrokBot ("grokbot pi") | grokbot-pi5 | pi5 / pi5 | Bookworm | eth 192.168.1.162, wlan 192.168.1.163 | cam0 imx708 NoIR, cam1 Arducam 64MP OV64A40 |
| pi5_cam_1_myrtle | pi5-cam-1-myrtle | pi5 / pi5 | Bookworm | eth 192.168.2.43, wlan 192.168.2.99 (also docker0) | cam0 imx708_wide_noir, cam1 imx708_wide_noir |
| pi5_cam_2_myrtle | pi5-cam-2-myrtle | pi5 / pi5 | Bookworm | eth 192.168.2.158, wlan 192.168.2.159 | cam0 imx708_wide_noir, cam1 imx708_noir |
| pi5_cam_3_myrtle | pi5-cam-3-myrtle | pi5 / pi5 | Bookworm | eth 192.168.2.168, wlan 192.168.2.175 | cam0 imx708_noir, cam1 imx708_wide_noir |
| TEST | pi5-test-hq | pi / pi | Raspberry Pi OS 13 Trixie | wlan 192.168.1.184 only (eth down) | cam0 imx477 (HQ camera) |

### GrokBot

Pi Connect name "grokbot pi". cam1 is an Arducam 64MP OV64A40 (`dtoverlay=ov64a40,cam1`). The camera has had ribbon and I2C issues, and it streamed 2304x1296@15 on October 25, 2026. Legacy services live at `/home/pi5/noir-stream`: `noir-mediamtx.service` and `noir-camctl.service` on port 8555, paths `noir0` and `arducam`. Those units were stopped on October 25, 2026, and are still enabled.

### pi5_cam_1_myrtle

Raspberry Pi Connect id `aecaabe9-1546-42cb-bcc8-5c31b224f0a1`. `docker0` is present in addition to eth and wlan. `~/Desktop/app_full.py` is a Picamera2 app; it was not running on October 25, 2026. This Pi has a display, and `~/grok-status` shows progress.

### pi5_cam_2_myrtle

Raspberry Pi Connect id `25599a15-b2d1-4dcb-ad50-e8ebd9f1df2b`. `app_full.py` exists and was not running on October 25, 2026.

### pi5_cam_3_myrtle

Raspberry Pi Connect id `316d0fcb-3de7-4709-9807-db2b3c7faecb`.

### TEST

Raspberry Pi Connect id `5e8d6397-5a38-42a2-896d-3d6dbd746137`. Raspberry Pi OS 13 Trixie. Wireless only (eth is down). cam0 is an imx477 HQ camera. Sudo is passwordless.

### Not set up

These Raspberry Pi Connect devices are offline and not set up yet: `pi4_cam_0_monitor`, `pi5_cam_4_truman`, `pi5_cam_5_experimental`, `pi5_cam_7z_16gb`, `zero`.

## Stream contract (Jetson pulls)

The Jetson pulls from each Pi. Local mode is on demand, so an idle camera does not send video across the tailnet.

| What | URL or record |
| --- | --- |
| RTSP | `rtsp://<tailnet-name>:8554/cam0` and `rtsp://<tailnet-name>:8554/cam1` |
| Encoding | H.264, 1280x720@30, about 2 Mbps, `runOnDemand` |
| MediaMTX API | `http://<host>:9997/v3/paths/list` |
| camctl | `http://<host>:8555` |
| mDNS | `_rtsp._tcp` on the LAN, TXT `paths=`, `camctl=8555`, `api=9997`, `node=<name>` |
| Push-mode fallback | `rtsp://jetson-camwall:8554/<name>-camN` |
