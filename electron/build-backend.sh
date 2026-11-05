#!/usr/bin/env bash
set -euo pipefail

# Stages a clean copy of the Python project + a self-contained venv into
# electron/resources/, for electron-builder's extraResources (see
# package.json) to bundle into the AppImage. Always wipes and rebuilds
# resources/ from scratch rather than syncing incrementally, so there's no
# risk of stale files (or this dev machine's db.sqlite3/data/) leaking into
# the bundle -- only the explicit whitelist below gets copied.
#
# Run via `npm run stage`, or automatically as part of `npm run build`.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ_ROOT="$(dirname "$HERE")"
RESOURCES="$HERE/resources"

echo "==> Cleaning $RESOURCES"
rm -rf "$RESOURCES"
mkdir -p "$RESOURCES/app"

echo "==> Copying Python project source"
for item in core dashboard static templates weather_mqtt.py manage.py requirements.txt; do
  cp -r "$PROJ_ROOT/$item" "$RESOURCES/app/$item"
done
find "$RESOURCES/app" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

echo "==> Building a self-contained venv (tied to this build machine's glibc -- see electron/README.md)"
python3 -m venv "$RESOURCES/venv"
"$RESOURCES/venv/bin/pip" install --upgrade pip --quiet
"$RESOURCES/venv/bin/pip" install -r "$PROJ_ROOT/requirements.txt" --quiet

echo "==> Done. Staged app + venv under $RESOURCES"
