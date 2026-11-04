#!/bin/sh
# Chapter 5-compatible entry. Default plan is read-only; apply needs all gates.
set -eu
exec python3 /usr/lib/eeg-system/eeg_emmc_install.py "$@"
