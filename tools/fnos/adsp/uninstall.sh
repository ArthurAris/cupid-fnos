#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Restore recorded before-files/modes/enable state, refusing target drift first.
# Requires an inactive unit and the shared startup/deploy lock. No automatic stop.
# Already loaded ADSP modules remain unchanged until reboot; never unload them.
# Optional --pmic-fallback invokes bounded standard pmic_glink only.
# Backups, journals and startup counters are retained; this is not safe any time.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec /usr/bin/python3 -I -B "$HERE/deploy.py" uninstall "$@"
