"""pidentify Raspberry Pi camera-node tools.

The Jetson Camera Wall discovers each node over mDNS and plays RTSP from
MediaMTX. camctl is the small local API that edits per-camera settings.
"""

__version__ = "1.0.0"

RTSP_PORT = 8554
CAMCTL_PORT = 8555
API_PORT = 9997
MEDIAMTX_VERSION = "v1.21.2"

# Ports this node is responsible for. RTSP is TCP-only so RTP does not also
# open :8000 and :8001; see mediamtx_conf.py.
SERVICE_PORTS = (RTSP_PORT, CAMCTL_PORT, API_PORT)
