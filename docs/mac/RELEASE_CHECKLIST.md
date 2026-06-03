# Photome for Mac Release Checklist

정식 외부 배포(GitHub Release 등)를 위해서는 **Apple Developer Program 가입 + Developer ID 인증서 + Notarization** 이 필요하다. App Store에 올리지 않더라도 macOS Sequoia(15) 이후 외부 사용자가 더블클릭으로 실행하려면 둘 다 있어야 한다.

비밀값은 저장소에 넣지 않고 로컬 Keychain/환경변수만 사용한다.

## 0. 사전 준비 (한 번만)

1. **Apple Developer Program** 가입 — $99/년. [developer.apple.com](https://developer.apple.com)
2. **Developer ID Application 인증서** 발급
   - developer.apple.com → Certificates → "+" → Developer ID Application 선택
   - CSR 생성 (Keychain Access → 인증서 도우미 → 인증 기관에 인증서 요청)
   - 발급된 .cer 다운로드 → 더블클릭으로 Keychain에 설치
3. **App-Specific Password** 생성 — appleid.apple.com → Sign-In and Security → App-Specific Passwords
4. **notarytool keychain profile** 저장 (재사용 가능):

   ```bash
   xcrun notarytool store-credentials photome-notary \
     --apple-id <apple-id-email> \
     --team-id <TEAMID> \
     --password <app-specific-password>
   ```

## 1. Developer ID 환경변수

```bash
security find-identity -v -p codesigning        # 인증서 확인
export PHOTOME_MAC_SIGN_IDENTITY="Developer ID Application: <NAME> (<TEAMID>)"
export PHOTOME_MAC_BUNDLE_ID="com.photome.mac"
export PHOTOME_MAC_VERSION="0.1.0"
export PHOTOME_MAC_BUILD="1"
```

## 2. notarization

권장 방식은 Keychain profile이다.

```bash
xcrun notarytool store-credentials photome-notary \
  --apple-id <apple-id-email> \
  --team-id <TEAMID> \
  --password <app-specific-password>

PHOTOME_NOTARY_PROFILE=photome-notary scripts/notarize_mac_app.sh
```

대체 환경변수:

```bash
export PHOTOME_NOTARY_APPLE_ID=<apple-id-email>
export PHOTOME_NOTARY_TEAM_ID=<TEAMID>
export PHOTOME_NOTARY_PASSWORD=<app-specific-password>
scripts/notarize_mac_app.sh
```

## 3. DMG polish

`scripts/build_mac_app_bundle.sh`는 다음을 생성한다.

- `dist/mac/PhotomeForMac.app`
- `dist/mac/PhotomeForMac.dmg`
- DMG 내부 `Applications` symlink
- ad-hoc 또는 Developer ID codesign

향후 Finder 배경/창 위치까지 꾸미려면 create-dmg 같은 별도 도구를 붙인다. 현재는 설치 가능한 표준 DMG까지 완료.

## 4. App icon / Bundle metadata

현재 포함:

- `CFBundleIdentifier`: `PHOTOME_MAC_BUNDLE_ID` 기본값 `com.photome.mac`
- version/build env override
- `CFBundleIconFile=AppIcon`
- 사진/문서/다운로드/데스크탑/로컬 네트워크 권한 설명
- `LSApplicationCategoryType=public.app-category.photography`

아이콘 원본은 `mac/PhotomeForMac/Resources/Assets.xcassets/AppIcon.appiconset/`에 있다.

## 5. Python runtime / backend bundling

기본 패키징은 backend source를 앱 리소스에 복사한다.

```bash
PHOTOME_BUNDLE_BACKEND=1 scripts/build_mac_app_bundle.sh
```

Python runtime까지 넣으려면 로컬 venv를 지정한다.

```bash
PHOTOME_BUNDLE_PYTHON=1 \
PHOTOME_PYTHON_BUNDLE_SRC=/absolute/path/to/.venv \
scripts/build_mac_app_bundle.sh
```

앱 실행 시 탐색 순서:

1. `PHOTOME_REPO_ROOT` 명시값
2. 앱 리소스 `Contents/Resources/photome-backend`
3. 현재 작업 디렉토리 상위 탐색
4. 개발 경로 `/Users/dongeui/Desktop/code/photomeformac`

Python 탐색 순서:

1. `PHOTOME_PYTHON` 명시값
2. bundled backend `.venv` / `.venv311`
3. 앱 리소스 `python-runtime/bin/python*`
4. 개발 repo venv
5. `/usr/bin/python3`

## 6. Xcode 실행 QA

```bash
cd mac/PhotomeForMac
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild -scheme PhotomeForMac -destination 'platform=macOS' build
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift test
```

GUI QA:

1. Xcode에서 `mac/PhotomeForMac/Package.swift` 열기
2. `PhotomeForMac` scheme 실행
3. 앱 시작 후 WebView가 `/dashboard`로 전환되는지 확인
4. 메뉴바에서 백엔드 시작/중지/재시작 확인
5. 전체 동기화/이미지 AI 이어서 분석 버튼 disabled/enabled 상태 확인

## 7. 권한/사진 접근 UX

확인할 점:

- 사진 폴더 선택은 `NSOpenPanel`로 사용자가 명시 선택
- 원본 폴더는 읽기 전용 입력으로 취급
- source-root 변경이 catalog/person 매핑을 삭제하지 않는지 확인
- NAS 오프라인/마운트 해제 시 앱이 크래시하지 않는지 확인

## 8. LAN 공유 보호

LAN 공유를 켜면 백엔드는 `0.0.0.0`에 바인딩한다. 원격 기기의 조회성 화면은 열 수 있지만, 스캔/사람 관리/검색 가중치/원본 다운로드 같은 관리자성 API는 `X-Photome-Admin-Token` 보호를 받는다. Mac 앱은 LAN 모드에서 앱 데이터 폴더의 `lan-admin-token`을 자동 생성해 백엔드에 전달한다.

확인할 점:

- 같은 Mac의 WebView/localhost 요청은 토큰 없이 계속 동작
- 다른 기기의 관리자 API 호출은 토큰 없으면 401
- 토큰 파일은 앱 데이터 폴더에만 있고 저장소에는 커밋하지 않음

## 9. launch-at-login

메뉴바에 `로그인 시 자동 시작 켜기/끄기`가 추가됐다. macOS `SMAppService.mainApp`를 사용한다.

주의:

- 실제 `.app` bundle로 실행해야 정상 등록된다.
- SwiftPM `swift run`에서는 OS 정책상 실패할 수 있으며 실패 메시지를 상태바에 표시한다.

## 10. 자동 업데이트 전략

GitHub Releases 폴링 방식이 기본 구현돼 있다.

- `UpdateChecker`(Swift)가 6시간 주기로 `api.github.com/repos/<owner>/<repo>/releases/latest`를 호출한다.
- semver 비교(`mac-v0.1.1` → `0.1.1`)로 새 버전 여부를 판단한다.
- 새 버전이 처음 감지되면 UNNotification 한 번 발사 + 메뉴에 "새 버전 X 다운로드…" 항목이 추가된다.
- 다운로드는 자동 설치 없이 GitHub Release 페이지를 연다 (Gatekeeper에서 검증 후 사용자가 수동 교체).
- GitHub Actions `Mac Release` workflow가 tag(`mac-v*`) 또는 수동 dispatch에서 ad-hoc DMG artifact + Release asset 업로드까지 수행한다.

Sparkle 2는 후보로 남겨둔다. 도입 시 필요한 것:
- edDSA signing key
- appcast.xml 호스팅
- Sparkle 프레임워크 vendoring

현재 권장: 첫 공개 전까지는 GitHub Releases + notarized DMG, 트래픽이 늘면 Sparkle 2 도입을 재검토한다.

## 11. NAS/대용량 라이브러리 QA

실제 배포 전 QA 순서:

1. 로컬 소형 라이브러리로 앱 시작/스캔/검색 확인
2. NAS source root 선택 후 전체 동기화
3. 앱 재시작 후 source root 유지 확인
4. 일부 NAS 경로 오프라인 상태에서 dashboard/status가 죽지 않는지 확인
5. 대용량 라이브러리에서 progress badge가 계속 갱신되는지 확인
6. 이미지 AI backlog 실행 후 CLIP 완료/예정/오류 카운트 확인
7. source root path 변경 후 person/name/alias/merge가 보존되는지 확인

## Release command sequence

```bash
# 1. 검증
./.venv/bin/pytest tests/test_mac_app_webview_reload.py tests/test_mac_packaging_release.py -q
cd mac/PhotomeForMac
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift test
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild -scheme PhotomeForMac -destination 'platform=macOS' build
cd ../..

# 2. 패키징
PHOTOME_MAC_SIGN_IDENTITY="Developer ID Application: <NAME> (<TEAMID>)" scripts/build_mac_app_bundle.sh

# 3. notarization
PHOTOME_NOTARY_PROFILE=photome-notary scripts/notarize_mac_app.sh
```
