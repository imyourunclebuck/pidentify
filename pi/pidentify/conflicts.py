"""Find processes that already own the camera devices or the pidentify ports.

install.sh reports these and, unless --take-over is given, leaves the
pidentify units stopped. Take-over stops a dedicated systemd unit or signals
a single process. It does not uninstall anything, and it will not stop ssh,
the user manager, or the installer itself.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
from datetime import datetime, timezone

from pidentify import SERVICE_PORTS

OWN_UNITS = {"pidentify-mediamtx.service", "pidentify-camctl.service"}
OWN_CMDLINE_MARKERS = (
    "/opt/pidentify/mediamtx",
    "pidentify-publish",
    "pidentify.publish",
    "pidentify.camctl",
    "pidentify-camctl",
)
# systemctl stop is never issued for these. Session units are handled by
# signaling the offending pid instead (stopping user@ would kill the login).
PROTECTED_UNITS = (
    "ssh.service",
    "sshd.service",
    "dbus.service",
    "polkit.service",
    "NetworkManager.service",
    "avahi-daemon.service",
    "cron.service",
    "wpa_supplicant.service",
    "dhcpcd.service",
    "systemd-logind.service",
    "unattended-upgrades.service",
    "containerd.service",
    "docker.service",
)
PROTECTED_UNIT_PREFIXES = ("systemd-", "getty@", "serial-getty@", "user@")
PROTECTED_PROCESS_NAMES = {
    "systemd",
    "systemd-journald",
    "systemd-logind",
    "sshd",
    "ssh",
    "sudo",
    "dbus-daemon",
    "avahi-daemon",
}

_UNIT_RE = re.compile(r"([A-Za-z0-9@._:+\\-]+\.(?:service|scope))")


def parse_ss_listeners(text: str, protocol: str) -> list[dict]:
    """Parse `ss -H -l` output. UDP listeners show up as UNCONN."""
    found: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.lower().startswith(("state ", "netid ")):
            continue
        parts = line.split()
        if len(parts) < 5 or parts[0] not in {"LISTEN", "UNCONN"}:
            continue
        port = _port_of(parts[3])
        if port is None:
            continue
        users = line[line.find("users:(") :] if "users:(" in line else ""
        pids = re.findall(r"pid=(\d+)", users)
        names = re.findall(r'\("([^"]*)"', users)
        found.append(
            {
                "protocol": protocol,
                "port": port,
                "local": parts[3],
                "pid": int(pids[0]) if pids else None,
                "process": names[0] if names else "",
            }
        )
    return found


def parse_proc_net_listeners(text: str, protocol: str) -> list[dict]:
    """Fallback when ss is unavailable. State 0A is TCP LISTEN; UDP is any row."""
    found: list[dict] = []
    for index, raw in enumerate(text.splitlines()):
        if index == 0:
            continue
        cols = raw.split()
        if len(cols) < 4:
            continue
        state = cols[3]
        if protocol == "tcp" and state.upper() != "0A":
            continue
        port = _hex_port(cols[1])
        if port is None:
            continue
        found.append(
            {
                "protocol": protocol,
                "port": port,
                "local": cols[1],
                "pid": None,
                "process": "",
            }
        )
    return found


def _port_of(local: str) -> int | None:
    if local.startswith("["):
        # [addr]:port
        if "]:" not in local:
            return None
        port_text = local.rsplit("]:", 1)[1]
    else:
        if ":" not in local:
            return None
        port_text = local.rsplit(":", 1)[1]
    try:
        port = int(port_text)
    except ValueError:
        return None
    if 0 < port <= 65535:
        return port
    return None


def _hex_port(local: str) -> int | None:
    try:
        return int(local.rsplit(":", 1)[1], 16)
    except (IndexError, ValueError):
        return None


def unit_from_cgroup(text: str) -> str | None:
    matches = _UNIT_RE.findall(text or "")
    if not matches:
        return None
    # The last unit is the most specific (a user app's scope, or the service).
    return matches[-1]


def is_own(cmdline: str, unit: str | None) -> bool:
    if unit in OWN_UNITS:
        return True
    text = cmdline or ""
    return any(marker in text for marker in OWN_CMDLINE_MARKERS)


def is_protected_service(unit: str | None) -> bool:
    if not unit or not unit.endswith(".service"):
        return False
    if unit in PROTECTED_UNITS:
        return True
    return any(unit.startswith(prefix) for prefix in PROTECTED_UNIT_PREFIXES)


def is_session_unit(unit: str | None) -> bool:
    if not unit:
        return False
    return unit.endswith(".scope") or unit.startswith("user@") or unit.startswith("session-")


def read_cmdline(proc_root: str, pid: int) -> list[str]:
    path = os.path.join(proc_root, str(pid), "cmdline")
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return []
    parts = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
    return parts


def read_ppid(proc_root: str, pid: int) -> int:
    stat = _read_stat_fields(proc_root, pid)
    if not stat:
        return 0
    try:
        return int(stat[1])
    except (IndexError, ValueError):
        return 0


def read_pgrp(proc_root: str, pid: int) -> int:
    stat = _read_stat_fields(proc_root, pid)
    if not stat:
        return pid
    try:
        return int(stat[2])
    except (IndexError, ValueError):
        return pid


def _read_stat_fields(proc_root: str, pid: int) -> list[str]:
    path = os.path.join(proc_root, str(pid), "stat")
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return []
    # comm is wrapped in parentheses and may contain spaces.
    try:
        rest = text.rsplit(")", 1)[1].strip().split()
    except IndexError:
        return []
    return rest


def ancestor_pids(proc_root: str, start_pid: int | None = None) -> set[int]:
    pid = os.getpid() if start_pid is None else start_pid
    found = set()
    for _ in range(32):
        if pid <= 0 or pid in found:
            break
        found.add(pid)
        if pid == 1:
            break
        pid = read_ppid(proc_root, pid)
    return found


def scan_device_holders(proc_root: str = "/proc") -> list[dict]:
    holders: list[dict] = []
    try:
        names = os.listdir(proc_root)
    except OSError:
        return holders
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        fd_dir = os.path.join(proc_root, name, "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        devices: list[str] = []
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if target.startswith("/dev/video") or target.startswith("/dev/media"):
                devices.append(target)
        if not devices:
            continue
        argv = read_cmdline(proc_root, pid)
        cgroup = _read_text(os.path.join(proc_root, name, "cgroup"))
        holders.append(
            {
                "pid": pid,
                "devices": sorted(set(devices)),
                "argv": argv,
                "cmdline": " ".join(argv)[:500],
                "process": os.path.basename(argv[0]) if argv else "",
                "unit": unit_from_cgroup(cgroup),
            }
        )
    return holders


def _read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _enrich(proc_root: str, pid: int | None, process: str) -> dict:
    if not pid:
        return {"cmdline": "", "process": process, "unit": None, "argv": []}
    argv = read_cmdline(proc_root, pid)
    cgroup = _read_text(os.path.join(proc_root, str(pid), "cgroup"))
    return {
        "argv": argv,
        "cmdline": " ".join(argv)[:500],
        "process": os.path.basename(argv[0]) if argv else process,
        "unit": unit_from_cgroup(cgroup),
    }


def collect_conflicts(
    *,
    tcp_ss: str = "",
    udp_ss: str = "",
    tcp_proc: str = "",
    udp_proc: str = "",
    holders: list[dict] | None = None,
    proc_root: str = "/proc",
    ports: tuple[int, ...] = SERVICE_PORTS,
) -> dict:
    listeners = parse_ss_listeners(tcp_ss, "tcp") + parse_ss_listeners(udp_ss, "udp")
    if not tcp_ss and tcp_proc:
        listeners += parse_proc_net_listeners(tcp_proc, "tcp")
    if not udp_ss and udp_proc:
        listeners += parse_proc_net_listeners(udp_proc, "udp")
    conflicts: list[dict] = []
    own: list[dict] = []
    seen_ports: set[tuple] = set()
    for listener in listeners:
        if listener["port"] not in ports:
            continue
        key = (listener["protocol"], listener["port"], listener.get("pid"))
        if key in seen_ports:
            continue
        seen_ports.add(key)
        info = _enrich(proc_root, listener.get("pid"), listener.get("process") or "")
        item = {
            "kind": "port",
            "protocol": listener["protocol"],
            "port": listener["port"],
            "pid": listener.get("pid"),
            "process": info["process"],
            "cmdline": info["cmdline"],
            "unit": info["unit"],
        }
        if is_own(info["cmdline"], info["unit"]):
            own.append(item)
        else:
            conflicts.append(item)
    for holder in holders or []:
        item = {
            "kind": "device",
            "devices": holder.get("devices") or [],
            "pid": holder.get("pid"),
            "process": holder.get("process") or "",
            "cmdline": holder.get("cmdline") or "",
            "unit": holder.get("unit"),
        }
        if is_own(item["cmdline"], item["unit"]):
            own.append(item)
        else:
            conflicts.append(item)
    return {"conflicts": conflicts, "own": own}


def live_report(ports: tuple[int, ...] = SERVICE_PORTS, proc_root: str = "/proc") -> dict:
    tcp_ss, udp_ss = "", ""
    tcp_proc, udp_proc = "", ""
    try:
        tcp = subprocess.run(["ss", "-H", "-ltnp"], check=False, capture_output=True, text=True, timeout=5)
        udp = subprocess.run(["ss", "-H", "-lunp"], check=False, capture_output=True, text=True, timeout=5)
        if tcp.returncode == 0:
            tcp_ss = tcp.stdout
        if udp.returncode == 0:
            udp_ss = udp.stdout
    except (OSError, subprocess.TimeoutExpired):
        tcp_ss = ""
        udp_ss = ""
    if not tcp_ss:
        tcp_proc = _read_text("/proc/net/tcp") + "\n" + _read_text("/proc/net/tcp6")
    if not udp_ss:
        udp_proc = _read_text("/proc/net/udp") + "\n" + _read_text("/proc/net/udp6")
    return collect_conflicts(
        tcp_ss=tcp_ss,
        udp_ss=udp_ss,
        tcp_proc=tcp_proc,
        udp_proc=udp_proc,
        holders=scan_device_holders(proc_root),
        proc_root=proc_root,
        ports=ports,
    )


def apply_takeover(
    report: dict,
    *,
    proc_root: str = "/proc",
    run_systemctl=None,
    signaler=None,
    sleeper=None,
    alive=None,
    ancestors: set[int] | None = None,
) -> list[dict]:
    """Stop conflicting units or signal processes. Returns a reversible action log."""
    run_systemctl = run_systemctl or _default_systemctl
    signaler = signaler or (lambda pid, sig: os.kill(pid, sig))
    sleeper = sleeper or _sleep
    alive = alive or (lambda pid: os.path.isdir(os.path.join(proc_root, str(pid))))
    ancestors = ancestors if ancestors is not None else ancestor_pids(proc_root)

    actions: list[dict] = []
    stopped_units: set[str] = set()
    signaled: dict[int, dict] = {}

    for item in report.get("conflicts") or []:
        unit = item.get("unit")
        pid = item.get("pid")
        if unit and unit.endswith(".service") and not is_session_unit(unit):
            if is_protected_service(unit):
                actions.append(_action("refused", item, reason=f"refusing to stop protected unit {unit}"))
                continue
            if unit not in stopped_units:
                run_systemctl(["stop", unit])
                stopped_units.add(unit)
                actions.append(
                    _action(
                        "systemctl-stop",
                        item,
                        reverse=f"systemctl start {unit}",
                    )
                )
            continue
        refusal = _signal_refusal(item, ancestors)
        if refusal:
            actions.append(_action("refused", item, reason=refusal))
            continue
        if pid in signaled:
            continue
        signaler(pid, signal.SIGTERM)
        signaled[pid] = item
        actions.append(
            _action(
                "signal",
                item,
                signal_name="TERM",
                reverse="not a systemd unit; restart the program manually",
            )
        )

    if signaled:
        sleeper(1.0)
        for pid, item in signaled.items():
            if alive(pid):
                signaler(pid, signal.SIGKILL)
                actions.append(
                    _action(
                        "signal",
                        item,
                        signal_name="KILL",
                        reverse="process did not exit after TERM; sent KILL. Restart it manually",
                    )
                )
    return actions


def _signal_refusal(item: dict, ancestors: set[int]) -> str | None:
    pid = item.get("pid")
    if not pid:
        return "no pid available; cannot stop this listener safely"
    if pid <= 1 or pid in ancestors:
        return "refusing to signal the installer or pid 1"
    process = item.get("process") or ""
    if process in PROTECTED_PROCESS_NAMES:
        return f"refusing to signal protected process {process}"
    return None


def _action(kind: str, item: dict, *, reason: str = "", reverse: str = "", signal_name: str = "") -> dict:
    return {
        "action": kind,
        "kind": item.get("kind"),
        "port": item.get("port"),
        "protocol": item.get("protocol"),
        "devices": item.get("devices"),
        "pid": item.get("pid"),
        "unit": item.get("unit"),
        "process": item.get("process"),
        "cmdline": item.get("cmdline"),
        "signal": signal_name,
        "reason": reason,
        "reverse": reverse,
    }


def format_action_line(action: dict, now: datetime | None = None) -> str:
    moment = (now or datetime.now(timezone.utc)).replace(microsecond=0).isoformat()
    parts = [moment, action.get("action", "").upper()]
    for key in ("unit", "pid", "signal", "port", "protocol"):
        value = action.get(key)
        if value not in (None, "", []):
            parts.append(f"{key}={value}")
    if action.get("devices"):
        parts.append("devices=" + ",".join(action["devices"]))
    if action.get("cmdline"):
        parts.append("cmdline=" + str(action["cmdline"]))
    if action.get("reason"):
        parts.append("reason=" + str(action["reason"]))
    if action.get("reverse"):
        parts.append("reverse=" + str(action["reverse"]))
    return " ".join(parts)


def _default_systemctl(argv: list[str]) -> None:
    subprocess.run(["systemctl", *argv], check=False, timeout=30)


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def format_report(report: dict) -> str:
    lines = []
    conflicts = report.get("conflicts") or []
    own = report.get("own") or []
    if not conflicts:
        lines.append("conflicts: none")
    else:
        lines.append(f"conflicts: {len(conflicts)}")
        for item in conflicts:
            if item.get("kind") == "port":
                lines.append(
                    f"  port {item.get('protocol')}/{item.get('port')} "
                    f"pid={item.get('pid')} unit={item.get('unit') or '-'} "
                    f"cmd={item.get('cmdline') or item.get('process') or '-'}"
                )
            else:
                lines.append(
                    f"  device {','.join(item.get('devices') or [])} "
                    f"pid={item.get('pid')} unit={item.get('unit') or '-'} "
                    f"cmd={item.get('cmdline') or item.get('process') or '-'}"
                )
    if own:
        lines.append(f"pidentify processes holding ports or cameras: {len(own)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report or stop processes that block pidentify")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", default=None, help="write the JSON report to this path")
    parser.add_argument("--take-over", action="store_true")
    parser.add_argument("--log", default="/var/lib/pidentify/takeover.log")
    args = parser.parse_args(argv)
    report = live_report()
    actions: list[dict] = []
    if args.take_over:
        actions = apply_takeover(report)
        if actions:
            os.makedirs(os.path.dirname(args.log) or ".", exist_ok=True)
            with open(args.log, "a", encoding="utf-8") as handle:
                for action in actions:
                    handle.write(format_action_line(action) + "\n")
        report = live_report()
        report["actions"] = actions
    encoded = json.dumps(report)
    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
    if args.json and not args.output:
        # stdout is the JSON document. Status and other callers capture it.
        print(encoded)
    else:
        print(format_report(report), file=sys.stderr)
        for action in actions:
            print(format_action_line(action), file=sys.stderr)
    # Exit 0 when the scan itself succeeded. Callers read the JSON to see
    # whether anything is still in the way; a conflict is not a scanner error.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
