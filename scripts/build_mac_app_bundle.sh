#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PACKAGE_DIR="$ROOT_DIR/mac/PhotomeForMac"
DIST_DIR="$ROOT_DIR/dist/mac"
# PRODUCT_NAME: SwiftPM 실행 타깃/바이너리 이름(소스 폴더 mac/PhotomeForMac 유지).
# APP_NAME: 사용자에게 보이는 .app·DMG·표시 이름(브랜드).
PRODUCT_NAME="PhotomeForMac"
APP_NAME="Trove"
APP_BUNDLE="$DIST_DIR/$APP_NAME.app"
CONTENTS_DIR="$APP_BUNDLE/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
DMG_STAGING="$DIST_DIR/dmg-staging"
DMG_PATH="$DIST_DIR/$APP_NAME.dmg"
SIGN_IDENTITY="${TROVE_MAC_SIGN_IDENTITY:--}"
BUNDLE_BACKEND="${TROVE_BUNDLE_BACKEND:-1}"
# CLIP은 정식 배포에서 항상 켜진 상태로 가는 정책이므로 Python venv도 기본 번들.
BUNDLE_PYTHON="${TROVE_BUNDLE_PYTHON:-1}"
# CLIP 모델 weights도 기본 번들 (사용자가 첫 실행 시 인터넷 다운로드 안 해도 됨).
BUNDLE_WEIGHTS="${TROVE_BUNDLE_WEIGHTS:-1}"
VERSION="${TROVE_MAC_VERSION:-0.1.0}"
BUILD_NUMBER="${TROVE_MAC_BUILD:-1}"
BUNDLE_ID="${TROVE_MAC_BUNDLE_ID:-com.trove.mac}"
# Sparkle 2 자동 업데이트 메타데이터. 둘 다 설정되어야 정상 동작한다.
# - SUFeedURL: appcast.xml의 정식 https URL (GitHub Pages 등 정적 호스팅).
# - SUPublicEDKey: Sparkle generate_keys로 만든 edDSA public key (base64).
SPARKLE_FEED_URL="${TROVE_SPARKLE_FEED_URL:-}"
SPARKLE_PUBLIC_ED_KEY="${TROVE_SPARKLE_PUBLIC_ED_KEY:-}"
# opt-in 크래시 리포팅(Sentry) DSN. 설정되면 Info.plist의 TroveSentryDSN으로
# 주입돼 앱에서 토글이 노출된다. 비어 있으면(개발 빌드) 기능 전체가 숨겨진다.
SENTRY_DSN="${TROVE_SENTRY_DSN:-}"

mkdir -p "$DIST_DIR"
rm -rf "$APP_BUNDLE" "$DMG_PATH" "$DMG_STAGING"

cd "$PACKAGE_DIR"
DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}" swift build -c release
SPM_BIN_DIR="$(DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}" swift build -c release --show-bin-path)"
BINARY_PATH="$SPM_BIN_DIR/$PRODUCT_NAME"

mkdir -p "$MACOS_DIR" "$RESOURCES_DIR"
cp "$BINARY_PATH" "$MACOS_DIR/$PRODUCT_NAME"
chmod 755 "$MACOS_DIR/$PRODUCT_NAME"

# SwiftPM은 Sparkle/Sentry 같은 dynamic framework를 자동으로
# .app/Contents/Frameworks/ 에 embed하지 않는다. 직접 복사하고 rpath를
# @executable_path/../Frameworks로 설정해야 사용자 Mac에서 dyld가 framework를 찾는다.
FRAMEWORKS_DIR="$CONTENTS_DIR/Frameworks"
EMBEDDED_FRAMEWORK=0
for fw in Sparkle Sentry; do
  if [[ -d "$SPM_BIN_DIR/$fw.framework" ]]; then
    mkdir -p "$FRAMEWORKS_DIR"
    rsync -a "$SPM_BIN_DIR/$fw.framework" "$FRAMEWORKS_DIR/"
    EMBEDDED_FRAMEWORK=1
  fi
done
if [[ "$EMBEDDED_FRAMEWORK" == "1" ]]; then
  # binary가 @rpath/<Framework>.framework/...로 link됐을 텐데, 그 rpath를
  # @executable_path/../Frameworks 로 정해줘야 한다(한 번만 추가).
  install_name_tool -add_rpath "@executable_path/../Frameworks" "$MACOS_DIR/$PRODUCT_NAME" 2>/dev/null || true
fi

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
    <string>Trove</string>
    <key>CFBundleExecutable</key>
    <string>PhotomeForMac</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundleIdentifier</key>
    <string>$BUNDLE_ID</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>Trove</string>
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
    <string>Trove가 사용자가 선택한 사진 폴더를 읽기 전용으로 스캔하고 로컬 라이브러리를 만듭니다.</string>
    <key>NSDocumentsFolderUsageDescription</key>
    <string>사용자가 선택한 사진 폴더를 읽기 전용으로 스캔하기 위해 접근합니다.</string>
    <key>NSDownloadsFolderUsageDescription</key>
    <string>사용자가 선택한 사진 폴더를 읽기 전용으로 스캔하기 위해 접근합니다.</string>
    <key>NSDesktopFolderUsageDescription</key>
    <string>사용자가 선택한 사진 폴더를 읽기 전용으로 스캔하기 위해 접근합니다.</string>
PLIST
if [[ -n "$SPARKLE_FEED_URL" ]]; then
  cat >> "$CONTENTS_DIR/Info.plist" <<PLIST
    <key>SUFeedURL</key>
    <string>$SPARKLE_FEED_URL</string>
PLIST
fi
if [[ -n "$SPARKLE_PUBLIC_ED_KEY" ]]; then
  cat >> "$CONTENTS_DIR/Info.plist" <<PLIST
    <key>SUPublicEDKey</key>
    <string>$SPARKLE_PUBLIC_ED_KEY</string>
PLIST
fi
if [[ -n "$SENTRY_DSN" ]]; then
  cat >> "$CONTENTS_DIR/Info.plist" <<PLIST
    <key>TroveSentryDSN</key>
    <string>$SENTRY_DSN</string>
PLIST
fi
cat >> "$CONTENTS_DIR/Info.plist" <<'PLIST'
</dict>
</plist>
PLIST
printf 'APPL????' > "$CONTENTS_DIR/PkgInfo"

if [[ "$BUNDLE_BACKEND" == "1" ]]; then
  BACKEND_DST="$RESOURCES_DIR/trove-backend"
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
  if [[ -n "${TROVE_PYTHON_BUNDLE_SRC:-}" ]]; then
    PY_SRC="$TROVE_PYTHON_BUNDLE_SRC"
  else
    PY_SRC=""
    for candidate in \
      "$ROOT_DIR/.venv311" \
      "$ROOT_DIR/.venv" \
      "$ROOT_DIR/venv" \
      "$HOME/Desktop/code/photome/.venv311" \
      "$HOME/Desktop/code/photome/.venv"; do
      if [[ -d "$candidate" && -x "$candidate/bin/python3" ]]; then
        # ensure CLIP deps actually installed in this venv
        if "$candidate/bin/python3" -c "import open_clip, torch" >/dev/null 2>&1; then
          PY_SRC="$candidate"
          break
        fi
      fi
    done
  fi
  if [[ -z "$PY_SRC" || ! -d "$PY_SRC" ]]; then
    echo "TROVE_BUNDLE_PYTHON=1 이지만 CLIP까지 설치된 venv를 찾지 못했습니다." >&2
    echo "  방법 1: 'python3.11 -m venv .venv311 && .venv311/bin/pip install -e .[clip]' 후 재실행" >&2
    echo "  방법 2: TROVE_PYTHON_BUNDLE_SRC=/path/to/venv 환경변수 지정" >&2
    exit 2
  fi
  rsync -a --delete "$PY_SRC/" "$RESOURCES_DIR/python-runtime/"
  # venv는 시스템 Python framework로 향하는 절대 경로 symlink를 자주 포함한다.
  # 번들 안에 들어가면 대상이 깨지므로 codesign이 거부한다. broken symlink는 제거하고
  # bin/ 안 인터프리터는 실제 파일 사본으로 교체한다.
  find "$RESOURCES_DIR/python-runtime" -type l ! -exec test -e {} \; -delete 2>/dev/null || true
  for link in "$RESOURCES_DIR/python-runtime/bin"/python*; do
    [[ -L "$link" ]] || continue
    target="$(readlink "$link")"
    if [[ "$target" != /* ]]; then
      target="$RESOURCES_DIR/python-runtime/bin/$target"
    fi
    if [[ -f "$target" ]]; then
      rm -f "$link"
      cp "$target" "$link"
    fi
  done
  echo "bundled python runtime from: $PY_SRC"
fi

if [[ "$BUNDLE_WEIGHTS" == "1" ]]; then
  WEIGHTS_DST="$RESOURCES_DIR/preinstalled-models/huggingface/hub"
  if [[ -n "${TROVE_WEIGHTS_SRC:-}" ]]; then
    WEIGHTS_SRC_CANDIDATES=("$TROVE_WEIGHTS_SRC")
  else
    WEIGHTS_SRC_CANDIDATES=(
      "$HOME/.cache/huggingface/hub"
      "$HOME/Desktop/code/photome/data/models/hf/hub"
      "$HOME/Desktop/code/photome/model_cache/hf/hub"
    )
  fi
  COPIED=0
  for src in "${WEIGHTS_SRC_CANDIDATES[@]}"; do
    if [[ -d "$src" ]]; then
      MODEL_DIR=$(find "$src" -maxdepth 1 -type d -name "models--timm--vit_base_patch32_clip_224.openai" -print -quit)
      if [[ -n "$MODEL_DIR" ]]; then
        mkdir -p "$WEIGHTS_DST"
        rsync -a "$MODEL_DIR" "$WEIGHTS_DST/"
        echo "bundled CLIP weights from: $MODEL_DIR"
        COPIED=1
        break
      fi
    fi
  done
  if [[ "$COPIED" != "1" ]]; then
    # 정식 배포 산출물은 ai-pack 단일 빌드이므로 weights 번들은 필수다.
    # weights 없는 빌드가 ai-pack으로 위장해 조용히 나가는 것을 막기 위해
    # (BUNDLE_PYTHON 미발견과 동일하게) 경고가 아닌 hard fail로 중단한다.
    # 의도적으로 weights를 빼려면 TROVE_BUNDLE_WEIGHTS=0 을 명시해야 한다.
    echo "TROVE_BUNDLE_WEIGHTS=1 이지만 CLIP ViT-B-32 weights를 찾지 못했습니다." >&2
    echo "  배포 산출물은 ai-pack 단일 빌드라 weights 번들이 필수입니다." >&2
    echo "  방법 1: 모델 캐시가 있는 경로를 TROVE_WEIGHTS_SRC=/path/to/huggingface/hub 로 지정" >&2
    echo "  방법 2: 한 번 CLIP을 실행해 ~/.cache/huggingface/hub 에 ViT-B-32를 받은 뒤 재실행" >&2
    echo "  (개발/디버그용으로 weights 없이 빌드하려면 TROVE_BUNDLE_WEIGHTS=0 을 명시)" >&2
    exit 2
  fi
fi

ENTITLEMENTS_FILE="$PACKAGE_DIR/Resources/PhotomeForMac.entitlements"
CODESIGN_ARGS=(--force --options runtime)
if [[ "$SIGN_IDENTITY" != "-" ]]; then
  # Developer ID 서명을 사용할 때만 --timestamp가 동작한다 (Apple 타임스탬프
  # 서버 사용). ad-hoc 서명에서는 --timestamp가 실패하므로 분기한다.
  CODESIGN_ARGS+=(--timestamp)
fi
if [[ -f "$ENTITLEMENTS_FILE" ]]; then
  CODESIGN_ARGS+=(--entitlements "$ENTITLEMENTS_FILE")
fi

# venv 안의 .so/.dylib을 먼저 sign한 다음 마지막에 .app bundle을 sign한다.
# --deep는 Apple 비추천이지만 venv 안 수천 개의 nested binary를 매번 개별
# sign하는 비용이 크므로 fallback으로 사용.
codesign "${CODESIGN_ARGS[@]}" --deep --sign "$SIGN_IDENTITY" "$APP_BUNDLE"
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

# Notarization 절차에서 .dmg 자체에도 Developer ID 서명이 있어야 한다.
# ad-hoc(-)으로는 codesign이 사실상 no-op이지만 명령어 자체는 통과한다.
DMG_CODESIGN_ARGS=(--force --sign "$SIGN_IDENTITY")
if [[ "$SIGN_IDENTITY" != "-" ]]; then
  DMG_CODESIGN_ARGS+=(--timestamp)
fi
codesign "${DMG_CODESIGN_ARGS[@]}" "$DMG_PATH"

printf '%s\n' "$APP_BUNDLE"
printf '%s\n' "$DMG_PATH"
