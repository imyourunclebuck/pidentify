"""Read and update /var/lib/pidentify/manifest.json.

The manifest is how uninstall knows which apt packages this installer added
and whether it created the pidentify user. Packages are restricted to an
allowlist so a bad manifest cannot remove python3 or curl.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone

from pidentify import MEDIAMTX_VERSION, __version__

PACKAGE_ALLOWLIST = ("ffmpeg", "avahi-daemon", "rpicam-apps", "libcamera-apps")


def empty_manifest() -> dict:
    return {
        "version": __version__,
        "mediamtx_version": MEDIAMTX_VERSION,
        "mediamtx_sha256": "",
        "packages_installed": [],
        "created_user": False,
        "avahi_conf_created": False,
        "avahi_backup": "",
        "installed_at": "",
        "updated_at": "",
    }


def load(path: str) -> dict:
    data = empty_manifest()
    if not os.path.exists(path):
        return data
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        return data
    data.update({key: raw[key] for key in data if key in raw})
    packages = raw.get("packages_installed") or []
    if not isinstance(packages, list):
        packages = []
    data["packages_installed"] = [pkg for pkg in packages if pkg in PACKAGE_ALLOWLIST]
    data["created_user"] = bool(raw.get("created_user"))
    data["avahi_conf_created"] = bool(raw.get("avahi_conf_created"))
    return data


def save(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".manifest.", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update(
    path: str,
    *,
    add_packages: tuple[str, ...] | list[str] = (),
    created_user: bool | None = None,
    avahi_conf_created: bool | None = None,
    avahi_backup: str | None = None,
    mediamtx_sha256: str | None = None,
) -> dict:
    data = load(path)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    packages = list(data["packages_installed"])
    for pkg in add_packages:
        if pkg in PACKAGE_ALLOWLIST and pkg not in packages:
            packages.append(pkg)
    data["packages_installed"] = packages
    if created_user:
        data["created_user"] = True
    if avahi_conf_created:
        data["avahi_conf_created"] = True
    if avahi_backup:
        data["avahi_backup"] = avahi_backup
    if mediamtx_sha256:
        data["mediamtx_sha256"] = mediamtx_sha256
    data["version"] = __version__
    data["mediamtx_version"] = MEDIAMTX_VERSION
    if not data["installed_at"]:
        data["installed_at"] = now
    data["updated_at"] = now
    save(path, data)
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Update the pidentify install manifest")
    sub = parser.add_subparsers(dest="cmd", required=True)
    upd = sub.add_parser("update")
    upd.add_argument("--path", required=True)
    upd.add_argument("--add-package", action="append", default=[])
    upd.add_argument("--created-user", action="store_true")
    upd.add_argument("--avahi-conf-created", action="store_true")
    upd.add_argument("--avahi-backup", default=None)
    upd.add_argument("--mediamtx-sha256", default=None)
    show = sub.add_parser("show")
    show.add_argument("--path", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "show":
        print(json.dumps(load(args.path), indent=2, sort_keys=True))
        return 0
    data = update(
        args.path,
        add_packages=args.add_package,
        created_user=True if args.created_user else None,
        avahi_conf_created=True if args.avahi_conf_created else None,
        avahi_backup=args.avahi_backup,
        mediamtx_sha256=args.mediamtx_sha256,
    )
    print(json.dumps(data, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
