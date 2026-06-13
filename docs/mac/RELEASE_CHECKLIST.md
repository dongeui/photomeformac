# Trove for Mac Release Checklist

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
   xcrun notarytool store-credentials trove-notary \
     --apple-id <apple-id-email> \
     --team-id <TEAMID> \
     --password <app-specific-password>
   ```

## 1. Developer ID 환경변수

```bash
security find-identity -v -p codesigning        # 인증서 확인
export TROVE_MAC_SIGN_IDENTITY="Developer ID Application: <NAME> (<TEAMID>)"
export TROVE_MAC_BUNDLE_ID="com.trove.mac"
export TROVE_MAC_VERSION="0.1.0"
export TROVE_MAC_BUILD="1"
```

## 2. notarization

권장 방식은 Keychain profile이다.

```bash
xcrun notarytool store-credentials trove-notary \
  --apple-id <apple-id-email> \
  --team-id <TEAMID> \
  --password <app-specific-password>

TROVE_NOTARY_PROFILE=trove-notary scripts/notarize_mac_app.sh
```

대체 환경변수:

```bash
export TROVE_NOTARY_APPLE_ID=<apple-id-email>
export TROVE_NOTARY_TEAM_ID=<TEAMID>
export TROVE_NOTARY_PASSWORD=<app-specific-password>
scripts/notarize_mac_app.sh
```

## 3. DMG polish

`scripts/build_mac_app_bundle.sh`는 다음을 생성한다.

- `dist/mac/Trove.app`
- `dist/mac/Trove.dmg`
- DMG 내부 `Applications` symlink
- ad-hoc 또는 Developer ID codesign

향후 Finder 배경/창 위치까지 꾸미려면 create-dmg 같은 별도 도구를 붙인다. 현재는 설치 가능한 표준 DMG까지 완료.

## 4. App icon / Bundle metadata

현재 포함:

- `CFBundleIdentifier`: `TROVE_MAC_BUNDLE_ID` 기본값 `com.trove.mac`
- version/build env override
- `CFBundleIconFile=AppIcon`
- 사진/문서/다운로드/데스크탑/로컬 네트워크 권한 설명
- `LSApplicationCategoryType=public.app-category.photography`

아이콘 원본은 `mac/PhotomeForMac/Resources/Assets.xcassets/AppIcon.appiconset/`에 있다.

## 5. Python runtime / backend bundling / Entitlements

**기본 동작 (정식 배포):**

```bash
scripts/build_mac_app_bundle.sh
```

- `TROVE_BUNDLE_BACKEND=1` (기본) — `app/` Python 소스를 Resources/trove-backend/ 에 복사
- `TROVE_BUNDLE_PYTHON=1` (기본) — venv 자동 탐색해서 Resources/python-runtime/ 에 복사
- `TROVE_BUNDLE_WEIGHTS=1` (기본) — `~/.cache/huggingface/hub`에서 ViT-B-32 가중치를 Resources/preinstalled-models/huggingface/hub/ 에 복사

**배포 산출물은 ai-pack 단일 빌드이므로 venv·weights 번들은 필수다.** CLIP 설치된 venv나 ViT-B-32 weights를 찾지 못하면 빌드 스크립트가 경고가 아닌 **오류로 중단**한다(weights 없는 ai-pack 빌드가 조용히 나가는 것을 막기 위함).

의도적으로 옵트아웃하려면(개발/디버그용) `TROVE_BUNDLE_PYTHON=0` 또는 `TROVE_BUNDLE_WEIGHTS=0`을 **명시**해야 한다. 명시한 경우에만 중단 없이 진행한다.

**venv가 없을 때:**

```bash
python3.11 -m venv .venv311
.venv311/bin/pip install -e ".[clip]"
```

이후 빌드 스크립트가 `.venv311`을 자동으로 발견한다. 또는 `TROVE_PYTHON_BUNDLE_SRC=/path/to/venv` 명시.

**Entitlements (`mac/PhotomeForMac/Resources/PhotomeForMac.entitlements`):**

빌드 스크립트가 Developer ID 서명 시 자동으로 `--entitlements`로 부착한다.
포함:
- `com.apple.security.cs.allow-jit`
- `com.apple.security.cs.allow-unsigned-executable-memory`
- `com.apple.security.cs.disable-library-validation`
- `com.apple.security.network.client/server`
- `com.apple.security.files.user-selected.read-only`

이게 없으면 hardened runtime 안에서 Python interpreter + PyTorch JIT가 차단된다.

앱 실행 시 탐색 순서:

1. `TROVE_REPO_ROOT` 명시값
2. 앱 리소스 `Contents/Resources/trove-backend`
3. 현재 작업 디렉토리 상위 탐색
4. 개발 경로 `/Users/dongeui/Desktop/code/photomeformac`

Python 탐색 순서:

1. `TROVE_PYTHON` 명시값
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

LAN 공유를 켜면 백엔드는 `0.0.0.0`에 바인딩한다. 원격 기기의 조회성 화면은 열 수 있지만, 스캔/사람 관리/검색 가중치/원본 다운로드 같은 관리자성 API는 `X-Trove-Admin-Token` 보호를 받는다. Mac 앱은 LAN 모드에서 앱 데이터 폴더의 `lan-admin-token`을 자동 생성해 백엔드에 전달한다.

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

**Sparkle 2** 기반 자동 업데이트가 코드에 통합되어 있다. 첫 정식 릴리스부터 사용자는 클릭 한 번으로 다음 버전을 받게 된다.

- `UpdateChecker.swift`가 `SPUUpdater`를 감싸 24시간마다 백그라운드 폴링.
- 새 버전 감지 → Sparkle 표준 다이얼로그 → 사용자 [지금 설치] → DMG 백그라운드 다운로드 + edDSA 서명 검증 + 자동 교체 + 앱 재시작.
- 운영 측 준비물: edDSA key 쌍 + appcast.xml 호스팅(GitHub Pages 등 정적 https).
- 빌드 시 `TROVE_SPARKLE_FEED_URL` + `TROVE_SPARKLE_PUBLIC_ED_KEY` 환경변수로 Info.plist에 자동 부착.
- 새 릴리스마다 `generate_appcast`로 appcast.xml 갱신 + 호스팅에 push.

설정 절차는 `docs/mac/USER_TODO.md`의 Sparkle 섹션 참고.

GitHub Actions `Mac Release` workflow가 tag(`mac-v*`) 또는 수동 dispatch에서 DMG artifact + Release asset 업로드까지 수행하며, 향후 appcast.xml 자동 갱신 단계 추가 예정.

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
TROVE_MAC_SIGN_IDENTITY="Developer ID Application: <NAME> (<TEAMID>)" scripts/build_mac_app_bundle.sh

# 3. notarization
TROVE_NOTARY_PROFILE=trove-notary scripts/notarize_mac_app.sh
```
