#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Isolated Python entry point; do not import from the deployment/current directory.
# --fallback and --failure-fallback are bounded, standard-module-only recovery paths.
set -eu
exec /usr/bin/python3 -I -B /usr/local/lib/cupid/adsp/adsp-start.py "$@"
