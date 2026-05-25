#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PACKAGE_DIR="$ROOT_DIR/mac/PhotomeForMac"
DIST_DIR="$ROOT_DIR/dist/mac"
APP_NAME="PhotomeForMac"
APP_BUNDLE="$DIST_DIR/$APP_NAME.app"
CONTENTS_DIR="$APP_BUNDLE/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
DMG_PATH="$DIST_DIR/$APP_NAME.dmg"
SIGN_IDENTITY="${PHOTOME_MAC_SIGN_IDENTITY:--}"

mkdir -p "$DIST_DIR"
rm -rf "$APP_BUNDLE" "$DMG_PATH"

cd "$PACKAGE_DIR"
DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}" swift build -c release
BINARY_PATH="$(swift build -c release --show-bin-path)/$APP_NAME"

mkdir -p "$MACOS_DIR" "$RESOURCES_DIR"
cp "$BINARY_PATH" "$MACOS_DIR/$APP_NAME"
chmod 755 "$MACOS_DIR/$APP_NAME"

cat > "$CONTENTS_DIR/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>ko</string>
    <key>CFBundleExecutable</key>
    <string>PhotomeForMac</string>
    <key>CFBundleIdentifier</key>
    <string>com.photome.mac</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>Photome</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>0.1.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSMinimumSystemVersion</key>
    <string>14.0</string>
    <key>LSApplicationCategoryType</key>
    <string>public.app-category.photography</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSPhotoLibraryUsageDescription</key>
    <string>Photome가 사용자가 선택한 사진 폴더를 읽기 전용으로 스캔합니다.</string>
    <key>NSDocumentsFolderUsageDescription</key>
    <string>사용자가 선택한 사진 폴더를 읽기 전용으로 스캔하기 위해 접근합니다.</string>
    <key>NSDownloadsFolderUsageDescription</key>
    <string>사용자가 선택한 사진 폴더를 읽기 전용으로 스캔하기 위해 접근합니다.</string>
    <key>NSDesktopFolderUsageDescription</key>
    <string>사용자가 선택한 사진 폴더를 읽기 전용으로 스캔하기 위해 접근합니다.</string>
</dict>
</plist>
PLIST
printf 'APPL????' > "$CONTENTS_DIR/PkgInfo"

codesign --force --deep --options runtime --sign "$SIGN_IDENTITY" "$APP_BUNDLE"
codesign --verify --deep --strict --verbose=2 "$APP_BUNDLE"

hdiutil create \
  -volname "$APP_NAME" \
  -srcfolder "$APP_BUNDLE" \
  -ov \
  -format UDZO \
  "$DMG_PATH" >/dev/null

printf '%s\n' "$APP_BUNDLE"
printf '%s\n' "$DMG_PATH"
