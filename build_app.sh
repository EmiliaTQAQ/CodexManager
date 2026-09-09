#!/usr/bin/env bash
set -euo pipefail

# Keep PyInstaller's cache outside user-level locations that may be unavailable
# in restricted environments and CI runners.
export PYINSTALLER_CONFIG_DIR="${PYINSTALLER_CONFIG_DIR:-${TMPDIR:-/tmp}/codex-manager-pyinstaller}"

pyinstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name "CodexManager" \
  --add-data "main.html:." \
  --add-data "demo.html:." \
  app.py

printf 'Built: dist/CodexManager.app\n'
