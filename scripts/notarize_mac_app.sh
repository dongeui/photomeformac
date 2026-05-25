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

xcrun notarytool submit "$DMG_PATH" "${SUBMIT_ARGS[@]}" --wait
xcrun stapler staple "$DMG_PATH"
xcrun stapler validate "$DMG_PATH"
spctl -a -vv -t open --context context:primary-signature "$DMG_PATH"
