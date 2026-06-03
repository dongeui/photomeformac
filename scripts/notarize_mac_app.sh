#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="$ROOT_DIR/dist/mac"
APP_BUNDLE="$DIST_DIR/PhotomeForMac.app"
DMG_PATH="$DIST_DIR/PhotomeForMac.dmg"
PROFILE="${PHOTOME_NOTARY_PROFILE:-}"
APPLE_ID="${PHOTOME_NOTARY_APPLE_ID:-}"
TEAM_ID="${PHOTOME_NOTARY_TEAM_ID:-}"
PASSWORD="${PHOTOME_NOTARY_PASSWORD:-}"

if [[ ! -d "$APP_BUNDLE" || ! -f "$DMG_PATH" ]]; then
  echo "먼저 scripts/build_mac_app_bundle.sh 를 실행하세요." >&2
  exit 2
fi

if [[ -n "$PROFILE" ]]; then
  SUBMIT_ARGS=(--keychain-profile "$PROFILE")
elif [[ -n "$APPLE_ID" && -n "$TEAM_ID" && -n "$PASSWORD" ]]; then
  SUBMIT_ARGS=(--apple-id "$APPLE_ID" --team-id "$TEAM_ID" --password "$PASSWORD")
else
  cat >&2 <<'EOF'
notarization 인증 정보가 없습니다.
권장: xcrun notarytool store-credentials photome-notary --apple-id <email> --team-id <TEAMID> --password <app-specific-password>
그 다음: PHOTOME_NOTARY_PROFILE=photome-notary scripts/notarize_mac_app.sh
또는 환경변수 PHOTOME_NOTARY_APPLE_ID / PHOTOME_NOTARY_TEAM_ID / PHOTOME_NOTARY_PASSWORD 사용.
EOF
  exit 2
fi

echo "==> Submitting DMG to Apple notarization service (5~30 min typical)"
xcrun notarytool submit "$DMG_PATH" "${SUBMIT_ARGS[@]}" --wait
echo "==> Stapling notarization ticket to DMG"
xcrun stapler staple "$DMG_PATH"
xcrun stapler validate "$DMG_PATH"

echo "==> Also stapling .app inside the DMG (so installs survive after DMG eject)"
# stapler validate가 통과한 DMG 안의 .app에도 직접 ticket을 부착해야,
# 사용자가 .app을 Applications에 복사한 뒤에도 macOS가 ticket을 확인할 수 있다.
xcrun stapler staple "$APP_BUNDLE" || echo "warning: app bundle staple may have already inherited ticket from DMG"
xcrun stapler validate "$APP_BUNDLE" || true

echo "==> Final Gatekeeper assessment"
spctl -a -vv -t open --context context:primary-signature "$DMG_PATH"
spctl -a -vv "$APP_BUNDLE" || echo "warning: app bundle Gatekeeper check returned non-zero; verify on a fresh Mac"
