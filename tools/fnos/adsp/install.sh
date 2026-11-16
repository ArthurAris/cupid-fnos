#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Offline, pinned file deployment. Does not start/stop services or touch modules.
# Usage: install.sh <staging-dir> <externally-accepted-package-manifest-sha256>
# Use this trusted local wrapper, NOT a script from an unverified staging tree.
set -eu
[ "$#" -eq 2 ] || { printf '%s\n' 'Usage: install.sh <stage> <externally-accepted-package-manifest-sha256>' >&2; exit 2; }
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec /usr/bin/python3 -I -B "$HERE/deploy.py" install "$1" "$2"
