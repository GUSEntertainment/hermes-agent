#!/usr/bin/env bash
# Entered only inside a private user/mount/PID namespace by run_tests.sh.
set -euo pipefail

SUITE_HOME="$1"
shift
SHADOW_ROOT="$SUITE_HOME/control-plane-shadow"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$SHADOW_ROOT"

# Hide every live control socket/scandir before Python/pytest collection. The
# mounts exist only in this namespace; the host mounts are never changed.
for target in /run/service /run/s6 /run/systemd /var/run/dbus; do
  if [ -e "$target" ]; then
    shadow="$SHADOW_ROOT${target//\//_}"
    mkdir -p "$shadow"
    mount --bind "$shadow" "$target"
    mount -o remount,bind,ro "$target"
  fi
done

# unshare may retain an outer leader while executing this shell as a child.
# Authenticate this exact shell identity before exec turns it into pytest;
# startup then removes the token before any test module can observe it.
PYTHONPATH= "$1" "$SCRIPT_DIR/register_test_namespace_process.py"
exec "$@"
