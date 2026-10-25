"""Stop WirePlumber from grabbing CSI cameras without stopping PipeWire.

Desktop images start WirePlumber for the logged-in user. Its libcamera and
v4l2 monitors open ``/dev/video*`` and ``/dev/media*``, so rpicam-vid cannot.
The installer writes a config snippet and restarts that user's WirePlumber.
Audio and screencast (xdg-desktop-portal, grim) stay enabled. PipeWire is
never stopped or disabled.

WirePlumber 0.5 (Trixie) reads ``wireplumber.conf.d``. WirePlumber 0.4
(Bookworm) reads ``main.lua.d``.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import subprocess
import sys
from datetime import datetime, timezone

from pidentify.conflicts import is_pipewire_holder

CONF_REL = "etc/wireplumber/wireplumber.conf.d/50-pidentify-no-camera.conf"
LUA_REL = "etc/wireplumber/main.lua.d/51-pidentify-no-camera.lua"

CONF_BODY = """# Written by pidentify. Disables camera monitors so rpicam-vid can open the CSI device.
# Audio and screencast stay enabled. pidentify-uninstall removes this file.
wireplumber.profiles = {
  main = {
    monitor.libcamera = disabled
    monitor.v4l2 = disabled
  }
}
"""

LUA_BODY = """-- Written by pidentify. Disables camera monitors so rpicam-vid can open the CSI device.
-- Audio and screencast stay enabled. pidentify-uninstall removes this file.
libcamera_monitor.enabled = false
v4l2_monitor.enabled = false
"""

USER_UNITS = ("wireplumber", "pipewire", "pipewire-pulse")


class WirePlumberError(RuntimeError):
    pass


def pipewire_conflict_count(report: dict) -> int:
    return sum(1 for item in report.get("conflicts") or [] if is_pipewire_holder(item))


def parse_version(text: str) -> tuple[int, int] | None:
    match = re.search(r"(\d+)\.(\d+)", text or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def choose_kind(version_text: str, share_root: str | None = None) -> str:
    """Return ``0.5`` (conf snippet) or ``0.4`` (lua snippet)."""
    parsed = parse_version(version_text)
    if parsed is not None:
        return "0.5" if parsed >= (0, 5) else "0.4"
    share = share_root if share_root is not None else "/usr/share/wireplumber"
    lua_dir = os.path.join(share, "main.lua.d")
    conf_file = os.path.join(share, "wireplumber.conf")
    conf_dir = os.path.join(share, "wireplumber.conf.d")
    if os.path.isdir(lua_dir) and not os.path.exists(conf_file):
        return "0.4"
    if os.path.exists(conf_file) or os.path.isdir(conf_dir):
        return "0.5"
    raise WirePlumberError("cannot tell whether WirePlumber is 0.4 or 0.5")


def detect_version_text() -> str:
    override = os.environ.get("PIDENTIFY_WIREPLUMBER_VERSION", "").strip()
    if override:
        return override
    commands = (
        ["wireplumber", "--version"],
        ["dpkg-query", "-W", "-f", "${Version}", "wireplumber"],
    )
    for argv in commands:
        try:
            completed = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        text = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
        if completed.returncode == 0 and parse_version(text):
            return text
    return ""


def config_for_kind(kind: str) -> tuple[str, str]:
    if kind == "0.5":
        return CONF_REL, CONF_BODY
    if kind == "0.4":
        return LUA_REL, LUA_BODY
    raise WirePlumberError(f"unsupported WirePlumber config kind {kind!r}")


def _join(prefix: str, relative: str) -> str:
    if not prefix:
        return "/" + relative
    return os.path.join(prefix, relative)


def write_override(prefix: str, kind: str) -> str:
    relative, body = config_for_kind(kind)
    path = _join(prefix, relative)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(body)
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)
    return path


def logged_in_users(run_user: str = "/run/user") -> list[tuple[str, int]]:
    """Users with a running user manager. The pidentify service user is skipped."""
    found: list[tuple[str, int]] = []
    try:
        names = os.listdir(run_user)
    except OSError:
        return found
    for name in sorted(names):
        if not name.isdigit():
            continue
        uid = int(name)
        try:
            user = pwd.getpwuid(uid).pw_name
        except KeyError:
            continue
        if user == "pidentify":
            continue
        found.append((user, uid))
    return found


def _default_runner(argv: list[str]) -> int:
    try:
        completed = subprocess.run(argv, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"wireplumber: {' '.join(argv)} failed: {exc}", file=sys.stderr)
        return 1
    return int(completed.returncode)


def restart_user(user: str, uid: int, runner) -> list[list[str]]:
    """Restart the user PipeWire stack. A failing unit does not skip the others."""
    combined = ["systemctl", "--user", "-M", f"{user}@", "restart", *USER_UNITS]
    if runner(combined) == 0:
        return [combined]
    used: list[list[str]] = []
    runtime = f"/run/user/{uid}"
    for unit in USER_UNITS:
        argv = ["systemctl", "--user", "-M", f"{user}@", "restart", unit]
        if runner(argv) != 0:
            argv = [
                "runuser",
                "-u",
                user,
                "--",
                "env",
                f"XDG_RUNTIME_DIR={runtime}",
                f"DBUS_SESSION_BUS_ADDRESS=unix:path={runtime}/bus",
                "systemctl",
                "--user",
                "restart",
                unit,
            ]
            code = runner(argv)
            if code != 0:
                print(f"wireplumber: could not restart {unit} for {user}", file=sys.stderr)
        used.append(argv)
    return used


def _append_log(path: str, line: str) -> None:
    if not path:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def release(
    report: dict,
    *,
    prefix: str = "",
    version_text: str = "",
    share_root: str | None = None,
    restart: bool = True,
    sessions: list[tuple[str, int]] | None = None,
    runner=None,
    log_path: str = "",
    run_user: str = "/run/user",
) -> dict:
    """Write the monitor override when PipeWire holds a camera, then restart users."""
    count = pipewire_conflict_count(report)
    if count == 0:
        return {"changed": False, "path": "", "restarts": []}
    text = version_text or detect_version_text()
    kind = choose_kind(text, share_root)
    path = write_override(prefix, kind)
    print(f"wireplumber: wrote {path} for WirePlumber {kind}", file=sys.stderr)
    restarts: list[list[str]] = []
    if restart:
        runner = runner or _default_runner
        targets = sessions if sessions is not None else logged_in_users(run_user)
        if not targets:
            print("wireplumber: no logged-in user to restart; the override applies at next login", file=sys.stderr)
        for user, uid in targets:
            restarts.extend(restart_user(user, uid, runner))
    moment = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    _append_log(
        log_path,
        f"{moment} WIREPLUMBER path={path} kind={kind} cameras={count} "
        f"reverse=rm -f {path} && systemctl --user -M <user>@ restart wireplumber pipewire pipewire-pulse",
    )
    return {"changed": True, "path": path, "kind": kind, "restarts": restarts}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Disable WirePlumber camera monitors")
    sub = parser.add_subparsers(dest="cmd", required=True)
    rel = sub.add_parser("release")
    rel.add_argument("--conflicts", required=True, help="conflicts JSON from pidentify.conflicts")
    rel.add_argument("--prefix", default=os.environ.get("PIDENTIFY_ROOT", ""))
    rel.add_argument("--log", default="")
    rel.add_argument("--no-restart", action="store_true")
    rel.add_argument("--version-text", default="")
    args = parser.parse_args(argv)
    if args.cmd == "release":
        try:
            with open(args.conflicts, encoding="utf-8") as handle:
                report = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"wireplumber: cannot read conflicts: {exc}", file=sys.stderr)
            return 0
        try:
            release(
                report,
                prefix=args.prefix,
                version_text=args.version_text,
                restart=not args.no_restart,
                log_path=args.log,
            )
        except WirePlumberError as exc:
            print(f"wireplumber: {exc}", file=sys.stderr)
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
