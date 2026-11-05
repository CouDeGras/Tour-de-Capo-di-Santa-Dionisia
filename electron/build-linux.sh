#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_VERSION="$(node "$HERE/build-version.js")"

if [[ ! "$BUILD_VERSION" =~ ^[0-9]{2}\.(0?[1-9]|1[0-2])\.(0?[1-9]|[12][0-9]|3[01])$ ]]; then
  echo "Invalid generated build version: $BUILD_VERSION" >&2
  exit 1
fi

echo "==> Building Linux AppImage version $BUILD_VERSION"
bash "$HERE/build-backend.sh"
"$HERE/node_modules/.bin/electron-builder" --linux AppImage \
  "-c.extraMetadata.version=$BUILD_VERSION"
