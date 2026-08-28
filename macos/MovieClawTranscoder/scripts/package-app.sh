#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="MovieClaw Transcoder.app"
OUTPUT_DIR="${PROJECT_DIR}/dist"
APP_DIR="${OUTPUT_DIR}/${APP_NAME}"
SIGNING_IDENTITY="${MOVIECLAW_SIGNING_IDENTITY:--}"

swift build --package-path "${PROJECT_DIR}" -c release
rm -rf "${APP_DIR}"
mkdir -p "${APP_DIR}/Contents/MacOS" "${APP_DIR}/Contents/Resources"
cp "${PROJECT_DIR}/.build/release/movieclaw-transcoder" "${APP_DIR}/Contents/MacOS/movieclaw-transcoder"
cp "${PROJECT_DIR}/Resources/Info.plist" "${APP_DIR}/Contents/Info.plist"

codesign --force --deep --options runtime --sign "${SIGNING_IDENTITY}" "${APP_DIR}"
echo "已生成：${APP_DIR}"
