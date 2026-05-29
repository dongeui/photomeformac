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
DMG_STAGING="$DIST_DIR/dmg-staging"
DMG_PATH="$DIST_DIR/$APP_NAME.dmg"
SIGN_IDENTITY="${PHOTOME_MAC_SIGN_IDENTITY:--}"
BUNDLE_BACKEND="${PHOTOME_BUNDLE_BACKEND:-1}"
BUNDLE_PYTHON="${PHOTOME_BUNDLE_PYTHON:-0}"
VERSION="${PHOTOME_MAC_VERSION:-0.1.0}"
BUILD_NUMBER="${PHOTOME_MAC_BUILD:-1}"
BUNDLE_ID="${PHOTOME_MAC_BUNDLE_ID:-com.photome.mac}"

mkdir -p "$DIST_DIR"
rm -rf "$APP_BUNDLE" "$DMG_PATH" "$DMG_STAGING"

cd "$PACKAGE_DIR"
DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}" swift build -c release
BINARY_PATH="$(DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}" swift build -c release --show-bin-path)/$APP_NAME"

mkdir -p "$MACOS_DIR" "$RESOURCES_DIR"
cp "$BINARY_PATH" "$MACOS_DIR/$APP_NAME"
chmod 755 "$MACOS_DIR/$APP_NAME"

if [[ -d "$PACKAGE_DIR/Resources/Assets.xcassets/AppIcon.appiconset" ]]; then
  ICONSET_DIR="$RESOURCES_DIR/AppIcon.iconset"
  mkdir -p "$ICONSET_DIR"
  ICONSET_SRC="$PACKAGE_DIR/Resources/Assets.xcassets/AppIcon.appiconset"
  for size in 16 32 128 256 512; do
    [[ -f "$ICONSET_SRC/icon_${size}x${size}.png" ]] && cp "$ICONSET_SRC/icon_${size}x${size}.png" "$ICONSET_DIR/icon_${size}x${size}.png"
    [[ -f "$ICONSET_SRC/icon_${size}x${size}@2x.png" ]] && cp "$ICONSET_SRC/icon_${size}x${size}@2x.png" "$ICONSET_DIR/icon_${size}x${size}@2x.png"
  done
  if command -v iconutil >/dev/null 2>&1; then
    if ! iconutil -c icns "$ICONSET_DIR" -o "$RESOURCES_DIR/AppIcon.icns" 2>/dev/null; then
      echo "warning: iconutil failed; AppIcon.icns may be missing" >&2
    fi
  fi
fi

cat > "$CONTENTS_DIR/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>ko</string>
    <key>CFBundleDisplayName</key>
    <string>Photome</string>
    <key>CFBundleExecutable</key>
    <string>PhotomeForMac</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundleIdentifier</key>
    <string>$BUNDLE_ID</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>Photome</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>$VERSION</string>
    <key>CFBundleVersion</key>
    <string>$BUILD_NUMBER</string>
    <key>LSMinimumSystemVersion</key>
    <string>14.0</string>
    <key>LSApplicationCategoryType</key>
    <string>public.app-category.photography</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSSupportsAutomaticTermination</key>
    <false/>
    <key>NSSupportsSuddenTermination</key>
    <false/>
    <key>NSPhotoLibraryUsageDescription</key>
    <string>Photome가 사용자가 선택한 사진 폴더를 읽기 전용으로 스캔하고 로컬 라이브러리를 만듭니다.</string>
    <key>NSDocumentsFolderUsageDescription</key>
    <string>사용자가 선택한 사진 폴더를 읽기 전용으로 스캔하기 위해 접근합니다.</string>
    <key>NSDownloadsFolderUsageDescription</key>
    <string>사용자가 선택한 사진 폴더를 읽기 전용으로 스캔하기 위해 접근합니다.</string>
    <key>NSDesktopFolderUsageDescription</key>
    <string>사용자가 선택한 사진 폴더를 읽기 전용으로 스캔하기 위해 접근합니다.</string>
    <key>NSLocalNetworkUsageDescription</key>
    <string>LAN 공유를 켠 경우 같은 네트워크의 기기에서 Photome 대시보드에 접근할 수 있게 합니다.</string>
</dict>
</plist>
PLIST
printf 'APPL????' > "$CONTENTS_DIR/PkgInfo"

if [[ "$BUNDLE_BACKEND" == "1" ]]; then
  BACKEND_DST="$RESOURCES_DIR/photome-backend"
  mkdir -p "$BACKEND_DST"
  rsync -a --delete \
    --exclude '.git/' \
    --exclude '.venv/' \
    --exclude '.venv311/' \
    --exclude '.pytest_cache/' \
    --exclude '.ruff_cache/' \
    --exclude '__pycache__/' \
    --exclude 'dist/' \
    --exclude 'mac/PhotomeForMac/.build/' \
    --exclude 'mac/PhotomeForMac/.swiftpm/' \
    --exclude 'data/*.sqlite*' \
    --exclude 'db/*.sqlite*' \
    "$ROOT_DIR/app" "$ROOT_DIR/scripts" "$ROOT_DIR/pyproject.toml" "$ROOT_DIR/README.md" "$BACKEND_DST/"
fi

if [[ "$BUNDLE_PYTHON" == "1" ]]; then
  if [[ -n "${PHOTOME_PYTHON_BUNDLE_SRC:-}" ]]; then
    PY_SRC="$PHOTOME_PYTHON_BUNDLE_SRC"
  else
    PY_SRC=""
    for candidate in "$ROOT_DIR/.venv311" "$ROOT_DIR/.venv" "$ROOT_DIR/venv"; do
      if [[ -d "$candidate" && -x "$candidate/bin/python3" ]]; then
        PY_SRC="$candidate"
        break
      fi
    done
  fi
  if [[ -z "$PY_SRC" || ! -d "$PY_SRC" ]]; then
    echo "PHOTOME_BUNDLE_PYTHON=1 이지만 Python runtime source를 찾을 수 없습니다 (PHOTOME_PYTHON_BUNDLE_SRC 또는 ./.venv311/./.venv 필요)" >&2
    exit 2
  fi
  rsync -a --delete "$PY_SRC/" "$RESOURCES_DIR/python-runtime/"
  echo "bundled python runtime from: $PY_SRC"
fi

codesign --force --deep --options runtime --sign "$SIGN_IDENTITY" "$APP_BUNDLE"
codesign --verify --deep --strict --verbose=2 "$APP_BUNDLE"

mkdir -p "$DMG_STAGING"
cp -R "$APP_BUNDLE" "$DMG_STAGING/"
ln -s /Applications "$DMG_STAGING/Applications"

DMG_RW="$DIST_DIR/$APP_NAME-rw.dmg"
rm -f "$DMG_RW"
hdiutil create \
  -volname "$APP_NAME" \
  -srcfolder "$DMG_STAGING" \
  -ov \
  -format UDRW \
  "$DMG_RW" >/dev/null
rm -rf "$DMG_STAGING"

MOUNT_DIR="$(hdiutil attach "$DMG_RW" -readwrite -noverify -noautoopen | awk '/\/Volumes\// {print $3; exit}')"
if [[ -n "$MOUNT_DIR" ]]; then
  osascript <<APPLESCRIPT 2>/dev/null || true
tell application "Finder"
  tell disk "$APP_NAME"
    open
    set current view of container window to icon view
    set toolbar visible of container window to false
    set statusbar visible of container window to false
    set the bounds of container window to {200, 200, 760, 540}
    set theViewOptions to the icon view options of container window
    set arrangement of theViewOptions to not arranged
    set icon size of theViewOptions to 128
    set position of item "$APP_NAME.app" of container window to {140, 170}
    set position of item "Applications" of container window to {420, 170}
    update without registering applications
    delay 0.5
    close
  end tell
end tell
APPLESCRIPT
  sync
  hdiutil detach "$MOUNT_DIR" >/dev/null 2>&1 || true
fi

hdiutil convert "$DMG_RW" -format UDZO -imagekey zlib-level=9 -o "$DMG_PATH" >/dev/null
rm -f "$DMG_RW"

printf '%s\n' "$APP_BUNDLE"
printf '%s\n' "$DMG_PATH"
