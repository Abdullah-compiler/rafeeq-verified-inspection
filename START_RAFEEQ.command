#!/bin/zsh
# Open Rafeeq from Finder; only synthetic data is used.
set -eu
cd "${0:A:h}"
if command -v uv >/dev/null 2>&1; then
  UV="$(command -v uv)"
elif [[ -x "$HOME/.hermes/bin/uv" ]]; then
  UV="$HOME/.hermes/bin/uv"
else
  print -u2 "uv is required but was not found. See README.md."
  exit 1
fi
if (( $# > 0 )); then
  exec "$UV" run --script --python 3.12 rafeeq.py "$@"
else
  exec "$UV" run --script --python 3.12 rafeeq.py chat --session demo
fi
