#!/usr/bin/env bash
# devcontainer postCreate entry point.
#
# Kept as a script rather than a 300-character `&&` chain so each step reports
# its own result and a failure in one step does not hide the next. The final
# `doctor` run is the summary: a green table at the end of a rebuild means the
# container is actually usable.
#
# It reports and continues; the exit status is nonzero if any step failed, so
# VS Code still marks the create as failed instead of pretending all is well.
set -uo pipefail

cd "$(dirname "$0")/.."
rc=0

step() {
    local name="$1"
    shift
    echo
    echo "== $name"
    if "$@"; then
        echo "-- ok: $name"
    else
        echo "-- FAILED: $name"
        rc=1
    fi
}

# Mountpoints created by Docker can come up root-owned, which breaks mise,
# cargo and playwright writes with EACCES. Best effort: a missing dir is not an
# error, and this must never be the step that aborts a create.
step "fix mount ownership" bash -c '
    for d in "$HOME/.local" "$HOME/.config" "$HOME/.cache" "$HOME/.cargo"; do
        [ -e "$d" ] && sudo -n chown -R "$(id -u):$(id -g)" "$d" || true
    done
'

step "mise trust" mise trust
step "mise install" mise install
step "project setup" mise run setup
# Non-fatal on purpose: this patches the HOST's global OpenCode config through
# the mount, so it can legitimately fail (read-only file, an older rtk, a host
# config another tool owns). That must not mark the whole create as failed.
step "rtk opencode integration (optional)" bash -c '
    mise run init-rtk && exit 0
    echo "-- skipped: rtk could not patch the mounted OpenCode config"
    exit 0
'

echo
if [ "$rc" -eq 0 ]; then
    echo "post-create: all steps ok"
else
    echo "post-create: one or more steps failed — see above and /tmp/doctor.log"
fi
exit "$rc"
