#!/usr/bin/env bash
# Build AgentFlow.app from the SwiftPM target.
# Output: macapp/.build/AgentFlow.app
set -euo pipefail

cd "$(dirname "$0")/.."

CONFIG="${CONFIG:-release}"
APP_NAME="AgentFlow"
APP_DIR=".build/${APP_NAME}.app"

swift build -c "$CONFIG"

rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"
cp ".build/${CONFIG}/${APP_NAME}" "$APP_DIR/Contents/MacOS/${APP_NAME}"
cp "Sources/AgentFlow/Resources/Info.plist" "$APP_DIR/Contents/Info.plist"

echo
echo "Built $APP_DIR"
du -sh "$APP_DIR"
