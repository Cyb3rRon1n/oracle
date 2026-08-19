#!/usr/bin/env bash
# Records a short animated demo of the client (asciinema -> svg-term SVG)
# for the README's "Screenshots" section - the piece that shows narration
# actually streaming in rather than a before/after pair.
#
# Needs a server already running on SERVER_URI's host (default
# ws://localhost:8765): against a real Ollama/Anthropic backend for
# genuine streaming narration, or any backend at all just to record the
# UI. Asciinema records the real terminal, so just play a few turns by
# hand and quit the client (Ctrl-D) when done.
#
# Not runnable in CI or on this dev box (asciinema + svg-term aren't
# installed here) - the recording is done by hand against a live session
# and the resulting SVG is committed. Install the two tools first:
#   pip install asciinema
#   npm install -g svg-term-cli
#
# Usage: scripts/record_demo.sh [output.svg]
set -euo pipefail

OUT="${1:-docs/screenshots/demo.svg}"
CAST="$(mktemp --suffix=.cast)"
trap 'rm -f "$CAST"' EXIT

command -v asciinema >/dev/null || { echo "missing asciinema - pip install asciinema" >&2; exit 1; }
command -v svg-term >/dev/null || { echo "missing svg-term - npm install -g svg-term-cli" >&2; exit 1; }

# 120x40 matches scripts/generate_screenshots.py's SIZE for a consistent look.
asciinema rec --cols 120 --rows 40 --title "Oracle" --overwrite "$CAST" \
  --command "python -m client.main"

svg-term --in "$CAST" --out "$OUT" --window
echo "Wrote $OUT"