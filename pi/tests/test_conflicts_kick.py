"""Conflict classification, take-over, and the MediaMTX kick."""

from __future__ import annotations

import os
import signal
import sys
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pidentify.conflicts import (  # noqa: E402
    apply_takeover,
    collect_conflicts,
    format_action_line,
    is_own,
    is_pipewire_holder,
    parse_proc_net_listeners,
    parse_ss_listeners,
    unit_from_cgroup,
)
from pidentify.kick import list_publishers, restart_camera  # noqa: E402
from pidentify.mediamtx_conf import render_mediamtx_yml  # noqa: E402


SS = textwrap.dedent(
    """
    LISTEN 0 128 0.0.0.0:8554 0.0.0.0:* users:(("python3",pid=42,fd=5))
    LISTEN 0 128 127.0.0.1:22 0.0.0.0:* users:(("sshd",pid=7,fd=3))
    LISTEN 0 128 *:8555 *:* users:(("python3",pid=99,fd=4))
    """
)


class ConflictParseTest(unittest.TestCase):
    def test_parse_ss_ignores_unrelated_ports(self):
        listeners = parse_ss_listeners(SS, "tcp")
        ports = sorted(item["port"] for item in listeners)
        self.assertEqual(ports, [22, 8554, 8555])
        by_port = {item["port"]: item for item in listeners}
        self.assertEqual(by_port[8554]["pid"], 42)
        self.assertEqual(by_port[8554]["process"], "python3")

    def test_proc_net_listen_state(self):
        text = "  sl local_address rem_address st\n 0: 00000000:216A 00000000:0000 0A\n"
        found = parse_proc_net_listeners(text, "tcp")
        self.assertEqual(found[0]["port"], 8554)

    def test_own_units_are_not_conflicts(self):
        self.assertTrue(is_own("/opt/pidentify/mediamtx /etc/pidentify/mediamtx.yml", "pidentify-mediamtx.service"))
        self.assertTrue(is_own("python3 -m pidentify.publish cam0", None))
        self.assertFalse(is_own("python3 /home/pi/Desktop/app_full.py", None))
        self.assertTrue(is_own("", None, uid=991, service_uid=991))
        self.assertFalse(is_own("", None, uid=1000, service_uid=991))

    def test_listeners_without_pid_owned_by_pidentify_are_not_conflicts(self):
        # ss -p omits other users. 8554=0x216A, 8555=0x216B, 9997=0x270D.
        tcp_proc = textwrap.dedent(
            """
            sl local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode
             0: 00000000:216A 00000000:0000 0A 00000000:00000000 00:00000000 00000000 991 0 1 1 0 100 0 0 10 0
             1: 00000000:216B 00000000:0000 0A 00000000:00000000 00:00000000 00000000 991 0 2 1 0 100 0 0 10 0
             2: 00000000:270D 00000000:0000 0A 00000000:00000000 00:00000000 00000000 991 0 3 1 0 100 0 0 10 0
             3: 0100007F:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000 0 0 4 1 0 100 0 0 10 0
            """
        )
        ss = textwrap.dedent(
            """
            LISTEN 0 128 0.0.0.0:8554 0.0.0.0:*
            LISTEN 0 128 0.0.0.0:8555 0.0.0.0:*
            LISTEN 0 128 0.0.0.0:9997 0.0.0.0:*
            LISTEN 0 128 0.0.0.0:22 0.0.0.0:*
            """
        )
        report = collect_conflicts(
            tcp_ss=ss,
            tcp_proc=tcp_proc,
            proc_root="/nonexistent",
            ports=(8554, 8555, 9997),
            service_uid=991,
        )
        self.assertEqual(report["conflicts"], [])
        self.assertEqual(sorted(item["port"] for item in report["own"]), [8554, 8555, 9997])
        self.assertTrue(all(item["pid"] is None for item in report["own"]))
        foreign = collect_conflicts(
            tcp_ss="LISTEN 0 128 0.0.0.0:8554 0.0.0.0:*\n",
            tcp_proc=" 0: 00000000:216A 00000000:0000 0A 00000000:00000000 00:00000000 00000000 1000 0 1 1 0 100 0 0 10 0\n",
            proc_root="/nonexistent",
            ports=(8554,),
            service_uid=991,
        )
        self.assertEqual(len(foreign["conflicts"]), 1)
        self.assertEqual(foreign["own"], [])

    def test_takeover_does_not_stop_pipewire(self):
        item = {
            "kind": "device",
            "devices": ["/dev/video0"],
            "pid": 77,
            "process": "wireplumber",
            "cmdline": "/usr/bin/wireplumber",
            "unit": "wireplumber.service",
        }
        self.assertTrue(is_pipewire_holder(item))
        stopped = []
        signaled = []
        actions = apply_takeover(
            {"conflicts": [item]},
            proc_root="/nonexistent",
            run_systemctl=lambda argv: stopped.append(argv),
            signaler=lambda pid, sig: signaled.append((pid, sig)),
            sleeper=lambda _seconds: None,
            alive=lambda _pid: False,
            ancestors={1},
        )
        self.assertEqual(stopped, [])
        self.assertEqual(signaled, [])
        self.assertIn("PipeWire", actions[0]["reason"])
        self.assertEqual(
            unit_from_cgroup("0::/user.slice/user-1000.slice/user@1000.service/app.slice/app.scope"),
            "app.scope",
        )

    def _proc(self, root: Path, pid: int, cmdline: str, cgroup: str, devices=()):
        proc = root / str(pid)
        (proc / "fd").mkdir(parents=True)
        (proc / "cmdline").write_bytes(cmdline.encode() + b"\0")
        (proc / "cgroup").write_text(cgroup, encoding="utf-8")
        # state ppid pgrp: fields after comm
        (proc / "stat").write_text(f"{pid} (python3) S 1 {pid} {pid} 0\n", encoding="utf-8")
        for index, device in enumerate(devices):
            os.symlink(device, proc / "fd" / str(index))

    def test_classify_and_takeover_stops_unit_but_not_ssh_or_user_manager(self):
        root = Path(self.id().replace(".", "_") + "_proc")
        if root.exists():
            import shutil

            shutil.rmtree(root)
        root.mkdir()
        try:
            self._proc(root, 42, "python3 /home/pi/Desktop/app_full.py", "0::/system.slice/app-full.service", ["/dev/video0"])
            self._proc(root, 7, "/usr/sbin/sshd", "0::/system.slice/ssh.service")
            self._proc(root, 50, "rpicam-vid -t 0", "0::/user.slice/user-1000.slice/user@1000.service", ["/dev/media0"])
            self._proc(root, 80, "/opt/pidentify/mediamtx /etc/pidentify/mediamtx.yml", "0::/system.slice/pidentify-mediamtx.service", ["/dev/video1"])
            holders = []
            from pidentify.conflicts import scan_device_holders

            holders = scan_device_holders(str(root))
            report = collect_conflicts(tcp_ss=SS, holders=holders, proc_root=str(root), ports=(8554, 8555, 9997))
            kinds = {(item["kind"], item.get("port"), item.get("pid")) for item in report["conflicts"]}
            self.assertIn(("port", 8554, 42), kinds)
            self.assertIn(("port", 8555, 99), kinds)
            self.assertNotIn(("port", 22, 7), kinds)
            self.assertTrue(any(item["pid"] == 42 and item["kind"] == "device" for item in report["conflicts"]))
            self.assertTrue(any(item["pid"] == 80 for item in report["own"]))
            stopped = []
            signaled = []

            def run_systemctl(argv):
                stopped.append(argv)

            def signaler(pid, sig):
                signaled.append((pid, sig))

            actions = apply_takeover(
                report,
                proc_root=str(root),
                run_systemctl=run_systemctl,
                signaler=signaler,
                sleeper=lambda _seconds: None,
                alive=lambda _pid: False,
                ancestors={1, 999},
            )
            self.assertIn(["stop", "app-full.service"], stopped)
            self.assertNotIn(["stop", "ssh.service"], stopped)
            self.assertNotIn(["stop", "user@1000.service"], stopped)
            self.assertTrue(any(pid == 50 and sig == signal.SIGTERM for pid, sig in signaled))
            log = "\n".join(format_action_line(action) for action in actions)
            self.assertIn("systemctl start app-full.service", log)
            self.assertIn("reverse=", log)
        finally:
            import shutil

            shutil.rmtree(root)


class KickTest(unittest.TestCase):
    def test_api_patch_uses_generation_token(self):
        seen = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def opener(request, timeout=0):
            seen["url"] = request.full_url
            seen["method"] = request.get_method()
            seen["body"] = request.data.decode()
            seen["timeout"] = timeout
            return Response()

        result = restart_camera(
            "cam0",
            opener=opener,
            proc_root=str(Path("/definitely/missing")),
            sleeper=lambda _seconds: None,
            token="12345",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "idle")
        self.assertEqual(seen["method"], "PATCH")
        self.assertIn("/v3/config/paths/patch/cam0", seen["url"])
        self.assertIn("pidentify-publish cam0 12345", seen["body"])
        self.assertIn("runOnDemand", seen["body"])
        self.assertNotIn("runOnInit", seen["body"])

    def test_push_mode_patches_run_on_init(self):
        seen = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def opener(request, timeout=0):
            seen["body"] = request.data.decode()
            return Response()

        result = restart_camera(
            "cam0",
            opener=opener,
            proc_root=str(Path("/definitely/missing")),
            sleeper=lambda _seconds: None,
            token="99",
            mode="push",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "continuous")
        self.assertIn("runOnInit", seen["body"])
        self.assertNotIn("runOnDemand", seen["body"])

    def test_signals_publisher_group_when_api_is_down(self):
        root = Path(self.id().replace(".", "_") + "_pub")
        if root.exists():
            import shutil

            shutil.rmtree(root)
        proc = root / "77"
        proc.mkdir(parents=True)
        try:
            (proc / "cmdline").write_bytes(b"/usr/local/bin/pidentify-publish\0cam0\0")
            (proc / "stat").write_text("77 (pidentify-pub) S 70 70 70 0\n", encoding="utf-8")
            found = list_publishers(str(root), "cam0")
            self.assertEqual(found[0]["pid"], 77)
            self.assertEqual(found[0]["pgrp"], 70)
            signaled = []

            def opener(_request, timeout=0):
                raise OSError("connection refused")

            result = restart_camera(
                "cam0",
                opener=opener,
                proc_root=str(root),
                signaler=lambda pid, sig: signaled.append((pid, sig)),
                sleeper=lambda _seconds: None,
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["method"], "signal")
            self.assertEqual(signaled, [(-70, signal.SIGTERM)])
            self.assertEqual(list_publishers(str(root), "cam1"), [])
        finally:
            import shutil

            shutil.rmtree(root)


class MediamtxConfigTest(unittest.TestCase):
    def test_yml_exposes_only_requested_ports_and_paths(self):
        text = render_mediamtx_yml(["cam0", "cam1"])
        self.assertIn("rtspAddress: :8554", text)
        self.assertIn("rtspTransports: [tcp]", text)
        self.assertIn("api: yes", text)
        self.assertIn("apiAddress: :9997", text)
        self.assertIn("rtmp: no", text)
        self.assertIn("hls: no", text)
        self.assertIn("webrtc: no", text)
        self.assertIn("srt: no", text)
        self.assertIn("moq: no", text)
        self.assertIn('runOnDemand: "/usr/local/bin/pidentify-publish cam0"', text)
        self.assertIn("runOnDemandRestart: yes", text)
        self.assertIn("runOnDemandCloseAfter: 10s", text)
        self.assertNotIn("runOnInit:", text)
        self.assertIn("100.64.0.0/10", text)
        self.assertIn("No port forwarding", text)
        self.assertIn("rtspAddress: :8554", text)
        self.assertNotIn("action: publish", text.split("192.168.0.0/16", 1)[1])
        empty = render_mediamtx_yml([])
        self.assertIn("paths: {}", empty)
        pushed = render_mediamtx_yml(["cam0"], mode="push")
        self.assertIn('runOnInit: "/usr/local/bin/pidentify-publish cam0"', pushed)
        self.assertIn('runOnReady: "/usr/local/bin/pidentify-push cam0"', pushed)
        self.assertNotIn("runOnDemand:", pushed)


if __name__ == "__main__":
    unittest.main()
