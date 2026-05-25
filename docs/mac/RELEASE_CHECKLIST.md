# Photome for Mac Release Checklist

이 문서는 `1~10 남은 작업`의 실제 진행 기준이다. 비밀값은 저장소에 넣지 않고 로컬 Keychain/환경변수만 사용한다.

## 1. Developer ID 인증서/Team ID

필수 로컬 준비:

```bash
security find-identity -v -p codesigning
export PHOTOME_MAC_SIGN_IDENTITY="Developer ID Application: <NAME> (<TEAMID>)"
export PHOTOME_MAC_BUNDLE_ID="com.photome.mac"
export PHOTOME_MAC_VERSION="0.1.0"
export PHOTOME_MAC_BUILD="1"
```

Team ID와 인증서 이름은 Apple Developer 계정/Keychain에서 확인한다. 저장소에는 실제 비밀값을 커밋하지 않는다.

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

## 8. launch-at-login

메뉴바에 `로그인 시 자동 시작 켜기/끄기`가 추가됐다. macOS `SMAppService.mainApp`를 사용한다.

주의:

- 실제 `.app` bundle로 실행해야 정상 등록된다.
- SwiftPM `swift run`에서는 OS 정책상 실패할 수 있으며 실패 메시지를 상태바에 표시한다.

## 9. 자동 업데이트 전략

아직 updater binary는 붙이지 않았다. 다음 중 하나를 선택한다.

1. Sparkle 2
   - macOS 표준에 가까움
   - edDSA signing key 필요
   - appcast.xml 호스팅 필요
2. GitHub Releases 수동 다운로드
   - 구현 단순
   - 자동 업데이트 UX는 약함

현재 권장: 첫 공개 전까지는 GitHub Releases + notarized DMG, 이후 Sparkle 2 추가.

## 10. NAS/대용량 라이브러리 QA

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
