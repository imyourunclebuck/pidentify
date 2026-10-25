#!/usr/bin/env bash
# Remove everything the pidentify installer added.
# Safe to run more than once. Does not modify /etc/hostname, /etc/hosts,
# or /boot/firmware/config.txt.
#
#   sudo pidentify-uninstall
#   sudo bash pi/uninstall.sh
#
# Apt packages are removed only when the install manifest says this installer
# added them, and only if they are ffmpeg, avahi-daemon, rpicam-apps, or
# libcamera-apps. A dry-run with PIDENTIFY_ROOT=... removes files under that
# prefix and does not touch the host.
set -euo pipefail

LOG_FILE=""

# Same rule as install.sh: progress on stderr, so a command substitution
# never picks up a log line.
say() {
  printf '%s\n' "$*" >&2
  if [[ -n "${LOG_FILE}" && -w "${LOG_FILE}" ]]; then
    printf '%s\n' "$*" >>"${LOG_FILE}"
  fi
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

dest() {
  local path="$1"
  if [[ -n "${PIDENTIFY_ROOT:-}" ]]; then
    printf '%s%s\n' "${PIDENTIFY_ROOT%/}" "${path}"
  else
    printf '%s\n' "${path}"
  fi
}

if [[ -n "${PIDENTIFY_ROOT:-}" ]]; then
  PIDENTIFY_SKIP_SYSTEM=1
fi

do_priv() {
  if [[ "${PIDENTIFY_SKIP_SYSTEM:-}" == "1" || "$(id -u)" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

if [[ "${PIDENTIFY_SKIP_SYSTEM:-}" != "1" && "$(id -u)" -ne 0 ]]; then
  if ! sudo -n true 2>/dev/null; then
    if [[ ! -t 0 ]]; then
      die "re-run as root: sudo pidentify-uninstall   or   curl .../pi/uninstall.sh | sudo bash"
    fi
    sudo -v
  fi
fi

LOG_FILE="$(dest /var/log/pidentify-install.log)"
say "pidentify uninstall started $(date -u +%Y-%m-%dT%H:%M:%SZ)"

manifest="$(dest /var/lib/pidentify/manifest.json)"
packages=""
created_user="no"
avahi_created="no"
avahi_backup=""
if [[ -f "${manifest}" ]]; then
  packages="$(python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1])).get("packages_installed") or []))' "${manifest}")"
  created_user="$(python3 -c 'import json,sys; print("yes" if json.load(open(sys.argv[1])).get("created_user") else "no")' "${manifest}")"
  avahi_created="$(python3 -c 'import json,sys; print("yes" if json.load(open(sys.argv[1])).get("avahi_conf_created") else "no")' "${manifest}")"
  avahi_backup="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("avahi_backup") or "")' "${manifest}")"
  say "read manifest ${manifest}"
else
  say "no manifest at ${manifest}; removing known pidentify files only"
fi

if [[ "${PIDENTIFY_SKIP_SYSTEM:-}" == "1" ]]; then
  say "Skipping systemctl (dry-run)."
else
  do_priv systemctl disable --now pidentify-mediamtx.service pidentify-camctl.service || true
fi

do_priv rm -f \
  "$(dest /etc/systemd/system/pidentify-mediamtx.service)" \
  "$(dest /etc/systemd/system/pidentify-camctl.service)" \
  "$(dest /etc/avahi/services/pidentify.service)"

if [[ "${PIDENTIFY_SKIP_SYSTEM:-}" != "1" ]]; then
  do_priv systemctl daemon-reload || true
fi

conf="$(dest /etc/avahi/avahi-daemon.conf)"
if [[ -n "${avahi_backup}" && -f "${avahi_backup}" ]]; then
  say "restoring Avahi config from ${avahi_backup}"
  do_priv cp -a "${avahi_backup}" "${conf}"
elif [[ "${avahi_created}" == "yes" ]]; then
  say "removing Avahi config created by pidentify"
  do_priv rm -f "${conf}"
fi
if [[ "${PIDENTIFY_SKIP_SYSTEM:-}" != "1" ]] && command -v systemctl >/dev/null 2>&1; then
  do_priv systemctl try-reload-or-restart avahi-daemon.service || true
fi

do_priv rm -f \
  "$(dest /usr/local/bin/pidentify-publish)" \
  "$(dest /usr/local/bin/pidentify-push)" \
  "$(dest /usr/local/bin/pidentify-camctl)" \
  "$(dest /usr/local/bin/pidentify-status)" \
  "$(dest /usr/local/bin/pidentify-uninstall)"
do_priv rm -rf "$(dest /opt/pidentify)" "$(dest /etc/pidentify)"

if [[ "${PIDENTIFY_SKIP_SYSTEM:-}" != "1" && -n "${packages}" ]]; then
  while IFS= read -r pkg; do
    [[ -z "${pkg}" ]] && continue
    case "${pkg}" in
      ffmpeg|avahi-daemon|rpicam-apps|libcamera-apps)
        say "removing apt package ${pkg}"
        do_priv apt-get remove -y "${pkg}" || say "warning: could not remove ${pkg}"
        ;;
      *)
        say "not removing unexpected package ${pkg}"
        ;;
    esac
  done <<< "${packages}"
fi

if [[ "${PIDENTIFY_SKIP_SYSTEM:-}" != "1" && "${created_user}" == "yes" ]] && id pidentify >/dev/null 2>&1; then
  say "removing system user pidentify"
  do_priv userdel pidentify || say "warning: userdel pidentify failed; the user may still be logged in"
fi

do_priv rm -rf "$(dest /var/lib/pidentify)"
say "pidentify uninstall finished"
say "The system hostname and /boot/firmware/config.txt were not modified."
if [[ -n "${LOG_FILE}" && -w "${LOG_FILE}" ]]; then
  say "install log left at ${LOG_FILE}"
fi
