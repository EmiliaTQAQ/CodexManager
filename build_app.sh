#!/usr/bin/env bash
set -euo pipefail

pyinstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name "CodexManager" \
  --add-data "main.html:." \
  --add-data "demo.html:." \
  app.py

printf 'Built: dist/CodexManager.app\n'
