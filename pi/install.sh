#!/usr/bin/env bash
# pidentify camera-node installer for Raspberry Pi OS Bookworm and Trixie (64-bit).
#
# After this file is on main:
#   curl -fsSL https://raw.githubusercontent.com/imyourunclebuck/pidentify/main/pi/install.sh | bash
#
# Before that merge, download this branch and the rest of pi/ from the same ref:
#   curl -fsSL https://raw.githubusercontent.com/imyourunclebuck/pidentify/pi-side/pi/install.sh | PIDENTIFY_REF=pi-side bash
#   curl -fsSL https://raw.githubusercontent.com/imyourunclebuck/pidentify/pi-side/pi/install.sh | bash -s -- --ref pi-side
#
# Name a node when several Pis share the system hostname pi5:
#   bash install.sh --name pi5-cam-1-myrtle
#
# Push mode (fallback; local RTSP stays available):
#   bash install.sh --name pi5-cam-1-myrtle --push-to rtsp://jetson-camwall:8554/pi5-cam-1-myrtle
# Switch back:
#   bash install.sh --local
#
# If sudo needs a password, a pipe cannot prompt. Use:
#   curl -fsSL https://raw.githubusercontent.com/imyourunclebuck/pidentify/main/pi/install.sh | sudo bash
#
# Does not install Tailscale, does not edit /etc/hostname or /etc/hosts,
# does not edit /boot/firmware/config.txt, and does not configure port
# forwarding or UPnP.
set -euo pipefail
umask 022

MEDIAMTX_VERSION="v1.21.2"
MEDIAMTX_TARBALL="mediamtx_v1.21.2_linux_arm64.tar.gz"
MEDIAMTX_SHA256="180a235374a548d9133cdd5e20f5dbd8b01317be1c86e0d7e66df585dc42ed6f"
MEDIAMTX_URL="https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/${MEDIAMTX_TARBALL}"
MEDIAMTX_CHECKSUM_URL="https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/checksums.sha256"
REPO="imyourunclebuck/pidentify"
TOTAL_STEPS=11
STEP_N=0
LOG_FILE=""
NEW_PKGS=()
CREATED_USER=0

usage() {
  cat <<'EOF'
Usage: install.sh [--ref REF] [--name NAME] [--push-to URL] [--local] [--take-over]

  --ref REF       Git branch, tag, or commit used to download the rest of pi/
                  when this script was piped from GitHub. Overrides
                  PIDENTIFY_REF. Default: main.
  --name NAME     Avahi host-name, mDNS service name, TXT node=, and the
                  default push prefix. DNS-safe (underscores become hyphens).
                  Does not change the system hostname. Saved in
                  /etc/pidentify/config. Example: pi5-cam-1-myrtle
  --push-to URL   Publish each camera continuously to URL-cam0 and URL-cam1,
                  for example rtsp://jetson-camwall:8554/pi5-cam-1-myrtle
                  or rtsp://192.168.1.218:8554/myrtle1. A URL with no path
                  uses --name as the prefix. Local RTSP on :8554 still works.
  --local         Switch back to on-demand local serving. This is the default
                  on a first install. A stored push URL is kept but not used.
  --take-over     Stop (do not uninstall) processes holding the CSI cameras
                  or ports 8554, 8555, and 9997. Each action is appended to
                  /var/lib/pidentify/takeover.log with a reverse command.

Omitting --name, --push-to, and --local keeps the values already in
/etc/pidentify/config. The system hostname and /boot/firmware/config.txt
are never modified. Tailscale, port forwarding, and UPnP are not configured.
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  if [[ -n "${LOG_FILE}" && -w "${LOG_FILE}" ]]; then
    printf 'ERROR: %s\n' "$*" >>"${LOG_FILE}"
  fi
  exit 1
}

say() {
  printf '%s\n' "$*"
  if [[ -n "${LOG_FILE}" && -w "${LOG_FILE}" ]]; then
    printf '%s\n' "$*" >>"${LOG_FILE}"
  fi
}

step() {
  STEP_N=$((STEP_N + 1))
  say ""
  say "==> [${STEP_N}/${TOTAL_STEPS}] $*"
}

validate_ref() {
  local ref="$1"
  if [[ ! "${ref}" =~ ^[A-Za-z0-9._/-]+$ ]] || [[ "${ref}" == *".."* ]]; then
    die "refusing git ref ${ref}"
  fi
}

dest() {
  local path="$1"
  if [[ -n "${PIDENTIFY_ROOT:-}" ]]; then
    printf '%s%s\n' "${PIDENTIFY_ROOT%/}" "${path}"
  else
    printf '%s\n' "${path}"
  fi
}

do_priv() {
  if [[ "${PIDENTIFY_SKIP_SYSTEM:-}" == "1" || "$(id -u)" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

ensure_log() {
  local log_dir
  LOG_FILE="$(dest /var/log/pidentify-install.log)"
  log_dir="$(dirname "${LOG_FILE}")"
  if [[ ! -d "${log_dir}" ]]; then
    do_priv mkdir -p "${log_dir}"
  fi
  if [[ ! -e "${LOG_FILE}" ]]; then
    do_priv touch "${LOG_FILE}"
  fi
  if [[ ! -w "${LOG_FILE}" ]]; then
    do_priv chmod 0644 "${LOG_FILE}" || true
  fi
  if [[ ! -w "${LOG_FILE}" ]]; then
    say "warning: cannot write ${LOG_FILE}; progress will only be printed here"
    LOG_FILE=""
  fi
}

fetch_source() {
  local ref="$1"
  local tmp archive url src ok
  tmp="$(mktemp -d /tmp/pidentify-src.XXXXXX)"
  archive="${tmp}/src.tar.gz"
  ok=0
  local urls=(
    "https://github.com/${REPO}/archive/refs/heads/${ref}.tar.gz"
    "https://github.com/${REPO}/archive/refs/tags/${ref}.tar.gz"
    "https://github.com/${REPO}/archive/${ref}.tar.gz"
  )
  for url in "${urls[@]}"; do
    say "trying ${url}"
    if curl -fsSL --retry 5 --retry-delay 2 -o "${archive}" "${url}"; then
      ok=1
      break
    fi
  done
  if [[ "${ok}" -ne 1 ]]; then
    die "could not download pidentify ref ${ref}"
  fi
  tar -xzf "${archive}" -C "${tmp}"
  src="$(find "${tmp}" -mindepth 2 -maxdepth 2 -type d -name pi -print -quit)"
  if [[ -z "${src}" || ! -f "${src}/pidentify/__init__.py" ]]; then
    die "downloaded archive has no pi/ tree"
  fi
  printf '%s\n' "${src}"
}

maybe_reexec() {
  if [[ "${PIDENTIFY_BOOTSTRAPPED:-}" == "1" ]]; then
    return 0
  fi
  local self="${BASH_SOURCE[0]:-}"
  if [[ -n "${self}" && -f "${self}" && -f "$(dirname "${self}")/pidentify/__init__.py" ]]; then
    return 0
  fi
  local ref="${PIDENTIFY_REF:-main}"
  local i=0
  local args=("$@")
  while [[ "${i}" -lt "${#args[@]}" ]]; do
    case "${args[$i]}" in
      --help|-h)
        usage
        exit 0
        ;;
      --ref)
        i=$((i + 1))
        ref="${args[$i]:-}"
        ;;
      --ref=*)
        ref="${args[$i]#*=}"
        ;;
    esac
    i=$((i + 1))
  done
  validate_ref "${ref}"
  say "==> Downloading pidentify ${ref} so the installer can read the rest of pi/"
  local src
  src="$(fetch_source "${ref}")"
  export PIDENTIFY_BOOTSTRAPPED=1
  export PIDENTIFY_SRC_TEMP=1
  exec bash "${src}/install.sh" "$@"
}

install_pkg() {
  local pkg="$1"
  if dpkg -s "${pkg}" >/dev/null 2>&1; then
    say "package already installed: ${pkg}"
    return 0
  fi
  say "installing package: ${pkg}"
  do_priv apt-get install -y --no-install-recommends "${pkg}"
  NEW_PKGS+=("${pkg}")
}

install_unit() {
  local name="$1"
  local src="${SRC}/systemd/${name}"
  local dst tmp line
  local member_groups=()
  dst="$(dest "/etc/systemd/system/${name}")"
  if getent group video >/dev/null 2>&1; then
    member_groups+=(video)
  fi
  if getent group render >/dev/null 2>&1; then
    member_groups+=(render)
  fi
  tmp="$(mktemp)"
  if [[ "${#member_groups[@]}" -gt 0 ]]; then
    line="SupplementaryGroups=${member_groups[*]}"
    sed "s/^SupplementaryGroups=.*/${line}/" "${src}" >"${tmp}"
  else
    grep -v '^SupplementaryGroups=' "${src}" >"${tmp}" || true
  fi
  do_priv install -m 0644 "${tmp}" "${dst}"
  rm -f "${tmp}"
}

list_lan_ipv4() {
  # ifaces.py drops docker, other virtual interfaces, and tailscale0.
  py -m pidentify.ifaces --lan-ipv4 2>/dev/null || true
}

py() {
  env PYTHONPATH="${SRC}" python3 "$@"
}

maybe_reexec "$@"

if [[ -n "${PIDENTIFY_ROOT:-}" ]]; then
  PIDENTIFY_SKIP_SYSTEM=1
fi

TAKE_OVER=0
WANT_LOCAL=0
NAME_SET=0
PUSH_SET=0
NODE_NAME=""
PUSH_TO=""
REF="${PIDENTIFY_REF:-main}"
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --ref)
      [[ "$#" -ge 2 ]] || die "--ref needs a value"
      REF="$2"
      shift 2
      ;;
    --ref=*)
      REF="${1#*=}"
      shift
      ;;
    --name)
      [[ "$#" -ge 2 ]] || die "--name needs a value"
      NODE_NAME="$2"
      NAME_SET=1
      shift 2
      ;;
    --name=*)
      NODE_NAME="${1#*=}"
      NAME_SET=1
      shift
      ;;
    --push-to)
      [[ "$#" -ge 2 ]] || die "--push-to needs an rtsp URL"
      PUSH_TO="$2"
      PUSH_SET=1
      shift 2
      ;;
    --push-to=*)
      PUSH_TO="${1#*=}"
      PUSH_SET=1
      shift
      ;;
    --local)
      WANT_LOCAL=1
      shift
      ;;
    --take-over)
      TAKE_OVER=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1 (try --help)"
      ;;
  esac
done
validate_ref "${REF}"
if [[ "${WANT_LOCAL}" -eq 1 && "${PUSH_SET}" -eq 1 ]]; then
  die "pass either --local or --push-to, not both"
fi
if [[ "${NAME_SET}" -eq 1 && -z "${NODE_NAME}" ]]; then
  die "--name must not be empty"
fi
if [[ "${PUSH_SET}" -eq 1 && -z "${PUSH_TO}" ]]; then
  die "--push-to must not be empty"
fi

if [[ -n "${PIDENTIFY_SRC:-}" ]]; then
  SRC="${PIDENTIFY_SRC}"
else
  SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [[ ! -f "${SRC}/pidentify/__init__.py" ]]; then
  die "installer source is incomplete at ${SRC}"
fi

cleanup_source() {
  if [[ "${PIDENTIFY_SRC_TEMP:-}" == "1" ]]; then
    rm -rf "$(cd "${SRC}/../.." && pwd)"
  fi
}
trap cleanup_source EXIT

if [[ "${PIDENTIFY_SKIP_SYSTEM:-}" != "1" && "$(id -u)" -ne 0 ]]; then
  if ! sudo -n true 2>/dev/null; then
    if [[ ! -t 0 ]]; then
      extra=()
      if [[ "${TAKE_OVER}" -eq 1 ]]; then
        extra+=(--take-over)
      fi
      if [[ "${REF}" != "main" ]]; then
        extra+=(--ref "${REF}")
      fi
      if [[ "${NAME_SET}" -eq 1 ]]; then
        extra+=(--name "${NODE_NAME}")
      fi
      if [[ "${PUSH_SET}" -eq 1 ]]; then
        extra+=(--push-to "${PUSH_TO}")
      fi
      if [[ "${WANT_LOCAL}" -eq 1 ]]; then
        extra+=(--local)
      fi
      extra_text=""
      for arg in "${extra[@]}"; do
        extra_text+=" $(printf '%q' "${arg}")"
      done
      cat >&2 <<EOF
ERROR: pidentify needs administrator privileges, and a piped script cannot prompt for a sudo password.

Re-run as:
  curl -fsSL https://raw.githubusercontent.com/${REPO}/${REF}/pi/install.sh | sudo bash -s --${extra_text}

Or, when sudo does not need a password, the original pipe works:
  curl -fsSL https://raw.githubusercontent.com/${REPO}/${REF}/pi/install.sh | bash
EOF
      exit 1
    fi
    sudo -v
  fi
fi

ensure_log
say "pidentify install started $(date -u +%Y-%m-%dT%H:%M:%SZ)"
say "source: ${SRC}"
say "ref: ${REF}"

step "Check this machine"
arch="$(uname -m)"
say "arch: ${arch}"
if [[ -r /proc/device-tree/model ]]; then
  say "model: $(tr -d '\0' </proc/device-tree/model)"
else
  say "model: unknown (this installer targets Raspberry Pi 5; continuing)"
fi
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  os_line="$(. /etc/os-release && printf '%s %s' "${PRETTY_NAME:-unknown}" "${VERSION_CODENAME:-}")"
  # shellcheck disable=SC1091
  os_code="$(. /etc/os-release && printf '%s' "${VERSION_CODENAME:-}")"
  say "os: ${os_line}"
  case "${os_code}" in
    bookworm|trixie)
      say "Raspberry Pi OS ${os_code} is supported."
      ;;
    "")
      say "warning: could not read VERSION_CODENAME. This installer targets Bookworm and Trixie."
      ;;
    *)
      say "warning: this installer is written for Raspberry Pi OS Bookworm and Trixie; continuing on ${os_code}."
      ;;
  esac
fi
if ! command -v python3 >/dev/null 2>&1; then
  die "python3 is required"
fi
if [[ "${arch}" != "aarch64" && "${PIDENTIFY_SKIP_DOWNLOAD:-}" != "1" ]]; then
  die "MediaMTX is installed from the linux arm64 release, but uname -m is ${arch}. Refusing to install the wrong binary."
fi
if [[ "${TAKE_OVER}" -eq 1 && "${PIDENTIFY_SKIP_SYSTEM:-}" == "1" ]]; then
  say "Ignoring --take-over in a dry-run. It would signal processes on this machine."
  TAKE_OVER=0
fi

step "Prepare directories"
do_priv mkdir -p \
  "$(dest /opt/pidentify)" \
  "$(dest /etc/pidentify)" \
  "$(dest /etc/avahi/services)" \
  "$(dest /etc/systemd/system)" \
  "$(dest /var/lib/pidentify)" \
  "$(dest /usr/local/bin)" \
  "$(dest /var/log)"

step "Install apt packages"
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
if [[ "${PIDENTIFY_SKIP_SYSTEM:-}" == "1" ]]; then
  say "Skipping apt (dry-run)."
else
  do_priv apt-get update -y
  if ! command -v curl >/dev/null 2>&1; then
    install_pkg curl
    install_pkg ca-certificates
  fi
  install_pkg ffmpeg
  install_pkg avahi-daemon
  # Tailscale is installed separately on each Pi. Do not install it here.
  if ! command -v ip >/dev/null 2>&1; then
    say "installing iproute2 so docker and tailscale addresses can be told apart"
    do_priv apt-get install -y --no-install-recommends iproute2
  fi
  if dpkg -s rpicam-apps >/dev/null 2>&1 || dpkg -s libcamera-apps >/dev/null 2>&1; then
    say "camera apps already installed"
  elif apt-cache show rpicam-apps >/dev/null 2>&1; then
    install_pkg rpicam-apps
  elif apt-cache show libcamera-apps >/dev/null 2>&1; then
    install_pkg libcamera-apps
  else
    die "neither rpicam-apps nor libcamera-apps is available from apt"
  fi
fi

step "Create the pidentify user"
if [[ "${PIDENTIFY_SKIP_SYSTEM:-}" == "1" ]]; then
  say "Skipping useradd (dry-run)."
elif id pidentify >/dev/null 2>&1; then
  say "user pidentify already exists"
else
  do_priv useradd \
    --system \
    --user-group \
    --home-dir /var/lib/pidentify \
    --shell /usr/sbin/nologin \
    pidentify
  CREATED_USER=1
  say "created system user pidentify"
fi
if [[ "${PIDENTIFY_SKIP_SYSTEM:-}" != "1" ]]; then
  member_groups=()
  if getent group video >/dev/null 2>&1; then
    member_groups+=(video)
  else
    say "warning: group video does not exist; rpicam-vid may not be able to open the camera"
  fi
  if getent group render >/dev/null 2>&1; then
    member_groups+=(render)
  fi
  if [[ "${#member_groups[@]}" -gt 0 ]]; then
    joined="$(IFS=,; echo "${member_groups[*]}")"
    do_priv usermod -aG "${joined}" pidentify
    say "pidentify groups: ${joined}"
  fi
fi

step "Install MediaMTX ${MEDIAMTX_VERSION}"
if [[ "${PIDENTIFY_SKIP_DOWNLOAD:-}" == "1" ]]; then
  say "Skipping MediaMTX download (PIDENTIFY_SKIP_DOWNLOAD=1)."
else
  stamp="$(dest /opt/pidentify/mediamtx.sha256)"
  binary="$(dest /opt/pidentify/mediamtx)"
  if [[ -x "${binary}" && -f "${stamp}" && "$(tr -d '[:space:]' <"${stamp}")" == "${MEDIAMTX_SHA256}" ]]; then
    say "MediaMTX already installed and checksum matches."
  else
    work="$(mktemp -d)"
    tarball="${work}/${MEDIAMTX_TARBALL}"
    say "Downloading ${MEDIAMTX_URL}"
    curl -fsSL --retry 5 --retry-delay 2 -o "${tarball}" "${MEDIAMTX_URL}"
    if ! printf '%s  %s\n' "${MEDIAMTX_SHA256}" "${tarball}" | sha256sum -c -; then
      die "MediaMTX tarball checksum does not match the pinned hash"
    fi
    if remote="$(curl -fsSL --retry 3 --retry-delay 2 "${MEDIAMTX_CHECKSUM_URL}")"; then
      remote_hash="$(printf '%s\n' "${remote}" | awk -v name="${MEDIAMTX_TARBALL}" '$2 == name || $2 == "*" name { print $1; exit }')"
      if [[ -n "${remote_hash}" && "${remote_hash}" != "${MEDIAMTX_SHA256}" ]]; then
        die "upstream checksums.sha256 disagrees with the pinned MediaMTX hash; refusing to install"
      fi
      if [[ -z "${remote_hash}" ]]; then
        say "warning: checksums.sha256 has no ${MEDIAMTX_TARBALL} line; the pinned hash matched the file"
      else
        say "upstream checksums.sha256 matches the pinned hash"
      fi
    else
      say "warning: could not download checksums.sha256; the pinned hash matched the tarball"
    fi
    tar -xzf "${tarball}" -C "${work}" mediamtx
    do_priv install -m 0755 "${work}/mediamtx" "${binary}"
    printf '%s\n' "${MEDIAMTX_SHA256}" | do_priv tee "${stamp}" >/dev/null
    rm -rf "${work}"
    say "installed ${binary}"
  fi
fi

step "Install pidentify programs"
do_priv mkdir -p "$(dest /opt/pidentify/pidentify)"
do_priv cp -a "${SRC}/pidentify/." "$(dest /opt/pidentify/pidentify)/"
do_priv find "$(dest /opt/pidentify/pidentify)" -type d -exec chmod 0755 {} +
do_priv find "$(dest /opt/pidentify/pidentify)" -type f -exec chmod 0644 {} +
for bin in pidentify-publish pidentify-push pidentify-camctl pidentify-status; do
  do_priv install -m 0755 "${SRC}/bin/${bin}" "$(dest "/usr/local/bin/${bin}")"
done
do_priv install -m 0755 "${SRC}/uninstall.sh" "$(dest /usr/local/bin/pidentify-uninstall)"

step "Look for port and camera conflicts"
conflict_file="$(dest /var/lib/pidentify/conflicts.json)"
do_priv env PYTHONPATH="${SRC}" python3 -m pidentify.conflicts --json --output "${conflict_file}"
say "Conflict report:"
do_priv env PYTHONPATH="${SRC}" python3 -m pidentify.conflicts || true
conflict_count="$(py -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("conflicts") or []))' "${conflict_file}")"
if [[ "${TAKE_OVER}" -eq 1 && "${conflict_count}" -gt 0 ]]; then
  say "Stopping conflicting processes (--take-over). This does not uninstall them."
  do_priv env PYTHONPATH="${SRC}" python3 -m pidentify.conflicts \
    --take-over \
    --log "$(dest /var/lib/pidentify/takeover.log)" \
    --json \
    --output "${conflict_file}"
  say "Take-over log: $(dest /var/lib/pidentify/takeover.log)"
  if [[ -f "$(dest /var/lib/pidentify/takeover.log)" ]]; then
    say "---- takeover.log ----"
    cat "$(dest /var/lib/pidentify/takeover.log)"
    say "---- end ----"
  fi
  conflict_count="$(py -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("conflicts") or []))' "${conflict_file}")"
fi
if [[ "${conflict_count}" -gt 0 ]]; then
  say "Conflicts remain (${conflict_count}). Services will be installed and enabled but not started."
  say "Stop the other program, or re-run with --take-over. --take-over only stops processes; it does not uninstall them."
  START_PLAN="blocked"
else
  START_PLAN="clear"
fi

step "Stop our own services before replacing files"
if [[ "${PIDENTIFY_SKIP_SYSTEM:-}" == "1" ]]; then
  say "Skipping systemctl (dry-run)."
else
  do_priv systemctl stop pidentify-mediamtx.service pidentify-camctl.service || true
fi

step "Detect cameras and write configuration"
if [[ -n "${PIDENTIFY_HOSTNAME:-}" ]]; then
  SYS_HOSTNAME="${PIDENTIFY_HOSTNAME}"
elif [[ -r /etc/hostname ]]; then
  SYS_HOSTNAME="$(head -n 1 /etc/hostname | tr -d '\r' | awk '{ print $1 }')"
else
  SYS_HOSTNAME="$(uname -n)"
fi
say "system hostname (unchanged): ${SYS_HOSTNAME}"
CONFIG_ARGS=()
if [[ "${NAME_SET}" -eq 1 ]]; then
  CONFIG_ARGS+=(--name "${NODE_NAME}")
fi
if [[ "${PUSH_SET}" -eq 1 ]]; then
  CONFIG_ARGS+=(--push-to "${PUSH_TO}")
fi
if [[ "${WANT_LOCAL}" -eq 1 ]]; then
  CONFIG_ARGS+=(--local)
fi
if [[ -n "${PIDENTIFY_LIST_CAMERAS_FILE:-}" ]]; then
  PIDENTIFY_LIST_CAMERAS_FILE="$(readlink -f "${PIDENTIFY_LIST_CAMERAS_FILE}")"
  export PIDENTIFY_LIST_CAMERAS_FILE
fi
do_priv env \
  PYTHONPATH="${SRC}" \
  PIDENTIFY_LIST_CAMERAS_FILE="${PIDENTIFY_LIST_CAMERAS_FILE:-}" \
  python3 -m pidentify.configure \
    --config-dir "$(dest /etc/pidentify)" \
    --state-dir "$(dest /var/lib/pidentify)" \
    --avahi-conf "$(dest /etc/avahi/avahi-daemon.conf)" \
    --avahi-service "$(dest /etc/avahi/services/pidentify.service)" \
    --avahi-backup "$(dest /var/lib/pidentify/avahi-daemon.conf.bak)" \
    --publish-cmd /usr/local/bin/pidentify-publish \
    --push-cmd /usr/local/bin/pidentify-push \
    --hostname "${SYS_HOSTNAME}" \
    "${CONFIG_ARGS[@]}"
manifest_args=(
  python3 -m pidentify.manifest update
  --path "$(dest /var/lib/pidentify/manifest.json)"
  --mediamtx-sha256 "${MEDIAMTX_SHA256}"
)
if [[ "${CREATED_USER}" -eq 1 ]]; then
  manifest_args+=(--created-user)
fi
avahi_created="$(py -c 'import json,sys; print("yes" if json.load(open(sys.argv[1])).get("avahi_conf_created") else "no")' "$(dest /var/lib/pidentify/manifest.json)")"
if [[ "${avahi_created}" == "yes" ]]; then
  manifest_args+=(--avahi-conf-created)
fi
backup_path="$(dest /var/lib/pidentify/avahi-daemon.conf.bak)"
if [[ -f "${backup_path}" ]]; then
  manifest_args+=(--avahi-backup "${backup_path}")
fi
for pkg in "${NEW_PKGS[@]}"; do
  manifest_args+=(--add-package "${pkg}")
done
do_priv env PYTHONPATH="${SRC}" "${manifest_args[@]}" >/dev/null

if [[ "${PIDENTIFY_SKIP_SYSTEM:-}" != "1" ]]; then
  do_priv chown root:pidentify "$(dest /etc/pidentify)" || true
  do_priv chmod 2775 "$(dest /etc/pidentify)"
  do_priv chown -R root:pidentify "$(dest /etc/pidentify)" || true
  do_priv chmod 0664 "$(dest /etc/pidentify/cam_settings.json)" "$(dest /etc/pidentify/cameras.json)" || true
  do_priv chmod 0644 "$(dest /etc/pidentify/config)" "$(dest /etc/pidentify/node.json)" "$(dest /etc/pidentify/mediamtx.yml)" || true
  do_priv chown -R pidentify:pidentify "$(dest /var/lib/pidentify)" || true
fi

step "Install systemd units"
install_unit pidentify-mediamtx.service
install_unit pidentify-camctl.service
if [[ "${PIDENTIFY_SKIP_SYSTEM:-}" == "1" ]]; then
  say "Skipping systemctl enable (dry-run)."
  printf '%s\n' "dry-run" >"$(dest /var/lib/pidentify/start-plan)"
else
  do_priv systemctl daemon-reload
  do_priv systemctl enable pidentify-mediamtx.service pidentify-camctl.service
  if [[ -f "$(dest /etc/avahi/avahi-daemon.conf)" ]]; then
    do_priv systemctl enable avahi-daemon.service || true
    do_priv systemctl restart avahi-daemon.service || say "warning: avahi-daemon did not restart; mDNS may be stale until it does"
  fi
  if [[ "${START_PLAN}" == "clear" ]]; then
    do_priv systemctl reset-failed pidentify-mediamtx.service pidentify-camctl.service || true
    do_priv systemctl start pidentify-mediamtx.service
    do_priv systemctl start pidentify-camctl.service
    printf '%s\n' "started" >"$(dest /var/lib/pidentify/start-plan)"
    say "Started pidentify-mediamtx and pidentify-camctl."
  else
    printf '%s\n' "blocked" >"$(dest /var/lib/pidentify/start-plan)"
    say "Left pidentify-mediamtx and pidentify-camctl stopped because something else still owns a camera or port."
    say "They are enabled, so a reboot may race with the other program if that program is also enabled."
    say "Re-run install.sh after it is gone, or re-run with --take-over. The log lists the reverse command for each process that was stopped."
  fi
fi

step "Summary"
say "system hostname: ${SYS_HOSTNAME} (not modified; sudo and /etc/hosts keep this name)"
node_json="$(dest /etc/pidentify/node.json)"
NODE_MODE="local"
if [[ -f "${node_json}" ]]; then
  mdns_name="$(py -c 'import json,sys; print(json.load(open(sys.argv[1])).get("mdns_fqdn",""))' "${node_json}")"
  node_name="$(py -c 'import json,sys; print(json.load(open(sys.argv[1])).get("node",""))' "${node_json}")"
  NODE_MODE="$(py -c 'import json,sys; print(json.load(open(sys.argv[1])).get("mode","local"))' "${node_json}")"
  say "node name: ${node_name}"
  say "mDNS: ${mdns_name}"
  say "Avahi host-name is the node name with underscores turned into hyphens. /etc/hostname was not written."
  say "mode: ${NODE_MODE}"
fi
if ! command -v ip >/dev/null 2>&1; then
  say "warning: ip(8) is not installed, so LAN addresses could not be classified. Avahi still denies tailscale0."
fi
say "IPv4 addresses:"
while IFS= read -r line; do
  [[ -z "${line}" ]] && continue
  say "${line}"
done < <(py -m pidentify.ifaces --summary || true)
while IFS= read -r line; do
  [[ -z "${line}" ]] && continue
  say "${line}"
done < <(py -m pidentify.tailscale --summary || true)
mapfile -t IPS < <(list_lan_ipv4 || true)
mapfile -t TS_IPS < <(py -m pidentify.tailscale --ipv4 || true)
TS_DNS="$(py -m pidentify.tailscale --magic-dns || true)"
say "Detected sensors:"
cameras_json="$(dest /etc/pidentify/cameras.json)"
if [[ -f "${cameras_json}" ]]; then
  while IFS= read -r line; do
    say "${line}"
  done < <(py -c 'import json,sys
data=json.load(open(sys.argv[1]))
cams=data.get("cameras") or []
if not cams:
    print("  none")
for cam in cams:
    print("  {id}: {model} [{modes}] {device}".format(**cam))' "${cameras_json}")
fi
say "Stream URLs (RTSP/TCP :8554 on every interface, including tailscale0):"
if [[ -f "${cameras_json}" ]]; then
  mapfile -t PATHS < <(py -c 'import json,sys
for cam in json.load(open(sys.argv[1])).get("cameras") or []:
    if cam.get("id"):
        print(cam["id"])' "${cameras_json}")
else
  PATHS=()
fi
if [[ "${#PATHS[@]}" -eq 0 ]]; then
  say "  no camera paths were published"
else
  if [[ "${#IPS[@]}" -eq 0 && "${#TS_IPS[@]}" -eq 0 && -z "${TS_DNS}" ]]; then
    for path_id in "${PATHS[@]}"; do
      say "  rtsp://<pi-ip>:8554/${path_id}"
    done
  fi
  for ip in "${IPS[@]}"; do
    for path_id in "${PATHS[@]}"; do
      say "  rtsp://${ip}:8554/${path_id}"
    done
  done
  for ip in "${TS_IPS[@]}"; do
    for path_id in "${PATHS[@]}"; do
      say "  rtsp://${ip}:8554/${path_id}"
    done
  done
  if [[ -n "${TS_DNS}" ]]; then
    for path_id in "${PATHS[@]}"; do
      say "  rtsp://${TS_DNS}:8554/${path_id}"
    done
  fi
fi
if [[ "${NODE_MODE}" == "push" && -f "${node_json}" ]]; then
  say "Push targets (continuous fallback; local RTSP above still works):"
  while IFS= read -r line; do
    [[ -z "${line}" ]] && continue
    say "  ${line}"
  done < <(py -c 'import json,sys
node=json.load(open(sys.argv[1]))
for cam, url in (node.get("push_targets") or {}).items():
    print("%s %s" % (cam, url))' "${node_json}")
fi
say "camctl:  http://<host>:8555/health   (all IPv4 interfaces, including tailscale0)"
say "MediaMTX API: http://<host>:9997/v3/paths/list"
say "No port forwarding and no UPnP. Streams are LAN and tailnet only."
say "units: pidentify-mediamtx.service, pidentify-camctl.service"
say "status: pidentify-status"
say "undo: sudo pidentify-uninstall"
configure_json="$(dest /var/lib/pidentify/configure.json)"
if [[ -f "${configure_json}" ]]; then
  while IFS= read -r line; do
    [[ -z "${line}" ]] && continue
    say "WARNING: ${line}"
  done < <(py -c 'import json,sys
for warning in json.load(open(sys.argv[1])).get("warnings") or []:
    print(warning)' "${configure_json}")
fi
say ""
say "Install finished. /boot/firmware/config.txt was not modified."
