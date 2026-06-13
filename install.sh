#!/usr/bin/env bash
# openfirebase installer (Linux/macOS).
# The package is source-available (not on PyPI); install from git or source.
set -euo pipefail

REPO="git+https://github.com/cognis-digital/openfirebase.git"

echo "Installing openfirebase..."

# Prefer pipx (isolated CLI), then uv, then pip, then local source.
if command -v pipx >/dev/null 2>&1; then
  echo "-> using pipx"
  pipx install "$REPO"
elif command -v uv >/dev/null 2>&1; then
  echo "-> using uv"
  uv tool install "$REPO" || uv pip install "$REPO"
elif command -v pip >/dev/null 2>&1; then
  echo "-> using pip"
  pip install "$REPO"
elif command -v pip3 >/dev/null 2>&1; then
  echo "-> using pip3"
  pip3 install "$REPO"
else
  echo "-> no pipx/uv/pip found; installing from local source checkout"
  python3 -m pip install .
fi

echo "Done. Try: openfirebase serve --memory"
