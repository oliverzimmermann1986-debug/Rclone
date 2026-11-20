#!/usr/bin/env bash
# Credential-free snapshot boundary for the dedicated Rclone image.
set -euo pipefail
set +x
umask 077
[[ $(id -u) == 1000 && ${PRUEFDIENST_PROFILE:-} == rclone && ${PRUEFDIENST_POSTGRES:-} == 0 ]]
export HOME=/work/home TMPDIR=/work/tmp CI=true HUSKY=0
mkdir -p "$HOME" "$TMPDIR"
git config --global --add safe.directory /snapshot
git -c core.hooksPath=/dev/null clone --no-hardlinks /snapshot /work/repo
cd /work/repo
git -c core.hooksPath=/dev/null -c advice.detachedHead=false checkout --detach "$PRUEFDIENST_SHA"
git remote set-url origin /snapshot
rm -f reports/unified-quality-report.json
exec bash -c "$1"
