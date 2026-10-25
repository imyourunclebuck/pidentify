#!/usr/bin/env bash
# Simulate `curl | bash` bootstrap against a local GitHub-style tarball.
# Progress must stay on stderr. stdout from fetch_source is only the pi/ path,
# and a log line there used to make exec look for a file named "trying https://...".
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
pi="${repo}/pi"
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

stage="${work}/pidentify-testref"
mkdir -p "${stage}"
tar -C "${repo}" \
  --exclude='pi/pidentify/__pycache__' \
  --exclude='pi/tests/__pycache__' \
  --exclude='pi/**/__pycache__' \
  -cf - pi | tar -C "${stage}" -xf -
archive="${work}/src.tar.gz"
tar -C "${work}" -czf "${archive}" pidentify-testref

dest="${work}/root"
mkdir -p "${dest}"

set +e
PIDENTIFY_REF=testref \
  PIDENTIFY_ARCHIVE_URL="file://${archive}" \
  PIDENTIFY_ROOT="${dest}" \
  PIDENTIFY_SKIP_DOWNLOAD=1 \
  PIDENTIFY_HOSTNAME=pi \
  PIDENTIFY_LIST_CAMERAS_FILE="${pi}/tests/fixtures/cam1_only.txt" \
  bash -s -- --name pi5-test-hq \
  <"${pi}/install.sh" \
  >"${work}/stdout" 2>"${work}/stderr"
rc=$?
set -e

if [[ "${rc}" -ne 0 ]]; then
  echo "piped install failed (${rc})" >&2
  echo "---- stderr ----" >&2
  cat "${work}/stderr" >&2
  echo "---- stdout ----" >&2
  cat "${work}/stdout" >&2
  exit 1
fi

if grep -q 'trying ' "${work}/stdout" || grep -q '==>' "${work}/stdout"; then
  echo "progress leaked onto stdout:" >&2
  cat "${work}/stdout" >&2
  exit 1
fi
if ! grep -q 'trying file://' "${work}/stderr"; then
  echo "expected the archive URL on stderr" >&2
  cat "${work}/stderr" >&2
  exit 1
fi
if ! grep -q 'pi5-test-hq' "${work}/stderr"; then
  echo "expected the node name in the progress log" >&2
  cat "${work}/stderr" >&2
  exit 1
fi

python3 - "${dest}/etc/pidentify/node.json" <<'PY'
import json
import sys
node = json.load(open(sys.argv[1], encoding="utf-8"))
if node.get("node") != "pi5-test-hq":
    raise SystemExit(f"node name was not applied: {node}")
if node.get("system_hostname") != "pi":
    raise SystemExit(f"system hostname changed: {node}")
PY

yml="${dest}/etc/pidentify/mediamtx.yml"
grep -q 'runOnDemand:' "${yml}"
grep -q 'cam1:' "${yml}"
if grep -q 'cam0:' "${yml}"; then
  echo "cam0 was published from a cam1-only probe" >&2
  exit 1
fi
test -x "${dest}/usr/local/bin/pidentify-publish"
echo "piped bootstrap ok"
