#!/bin/zsh
set -eu
cd -- "${0:A:h}"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v exiftool >/dev/null 2>&1; then
  print 'ExifTool is required. Install it once with: brew install exiftool'
  read -r '?Press Return to close.'
  exit 1
fi
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
if ! .venv/bin/python -c 'import flask' >/dev/null 2>&1; then
  print 'Installing local Python dependencies (internet needed for this first run)…'
  .venv/bin/python -m pip install -r requirements.txt
fi
exec .venv/bin/python app.py
