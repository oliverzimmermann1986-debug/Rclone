#!/usr/bin/env bash
# Run only in the disposable, nonprivileged central Pruefdienst container.
set -euo pipefail
exec python3 scripts/ci/verify.py
