# Photome for Mac 배포 진행 플랜

> 이 문서는 단위별 진행 트래커. 사용자가 `배포까지남은과정진행` 명령을 보내면
> 가장 빠른 미완료 단위(`status: TODO` 또는 `IN_PROGRESS`)부터 이어서 작업한다.
>
> 각 단위 끝나면 status 갱신 + git commit. 컨텍스트 끊겨도 다음 세션에서
> 이 문서만 보면 어디서 이어할지 알 수 있다.

## 표기

- `status: DONE` — 완료, 커밋됨
- `status: IN_PROGRESS` — 진행 중 (다음 세션 우선 처리)
- `status: TODO` — 미착수
- `status: BLOCKED:<이유>` — 사용자 환경/자격증명 등 외부 요건 필요

---

## Stage 0. 베이스라인 정합성

### S0.1 photome 핵심 패치 백포팅 — `status: DONE`

오늘자 photome에서 photomeformac으로 검증·이식.

- `5c4ac22 + b07818f + 9ef8aaa + 74a54d7` — alias 저장 + people 매니저 UI ✅
- `30fc787` — person preview cache ✅
- `4591ab1` — AI backlog 진행 중 표시 (이미 적용됨) ✅
- `3ded092` — bounded asset workers (이미 적용됨) ✅

### S0.2 추가 백포팅 후보 — `status: TODO`

- `698139f` Add dashboard resource controls — Mac 앱은 env를 직접 주입하므로 `.env` 쓰기 경로를
  `PHOTOME_ENV_FILE` 환경변수 우회로 처리해야 함. 백엔드 env 생성기에서 path 지정 필요.
  파일들: `app/api/performance_settings.py` (신규), `app/api/router.py`, `app/core/settings.py`,
  `app/scheduler/service.py`, `app/services/processing/pipeline.py`, dashboard CSS/HTML/JS.
- `f00dbde + 7404a19` 배경 이미지 AI 상태 표시 — 일부 이미 반영된 듯, diff 확인 필요.
- `553eb09` OCR heuristics + delta scan cache 수정 — 정확성 패치.

### S0.3 photomeformac 자체 테스트 검증 — `status: DONE`

`tests/` 184 passed (test_ux_e2e.py 제외).

---

## Stage 1. Mac shell UX 강화

### S1.1 Finder Drag & Drop으로 source root 추가 — `status: TODO`

`ContentView.swift` 또는 메뉴바에서 폴더 드래그 → `backend.appendSourceRoot(url)`.
SwiftUI `onDrop(of: [.fileURL])` 사용.

**파일:** `mac/PhotomeForMac/Sources/PhotomeForMac/ContentView.swift`,
`BackendSupervisor.swift` (appendSourceRoot 추가).

**테스트:** Swift unit test로 URL → string 정규화 검증.

### S1.2 macOS UserNotifications — `status: TODO`

스캔/AI 작업 완료 시 알림. `UNUserNotificationCenter` 사용. 사용자 권한 요청 흐름 필요.

**파일:** `BackendSupervisor.swift`에 NotificationCenter 헬퍼, `PhotomeForMacApp.swift`에서
`requestAuthorization` 호출.

**검증:** library job status가 running → succeeded로 바뀌는 순간 알림 발사.

### S1.3 Dock badge — `status: TODO`

활성 작업 있을 때 `NSApp.dockTile.badgeLabel`에 진행 % 또는 "..." 표시.

**파일:** `BackendSupervisor.swift`의 libraryJobStatus 옵저버에 묶음.

### S1.4 Quit confirmation — `status: TODO`

스캔/AI 진행 중 앱 종료 시 confirm dialog. `NSApplication.shouldTerminate` 처리.

**파일:** `PhotomeForMacApp.swift` AppDelegate adapter 또는 `applicationShouldTerminate`.

### S1.5 모델 다운로드 progress UI — `status: TODO`

현재 텍스트만 표시. 진행률 (다운로드 byte / total) → SwiftUI ProgressView.

**제약:** 백엔드 `/ai-pack` API에 progress fraction 노출 여부 확인 필요.
**Selected as MVP-out** 가능. 필요 시 Stage 3에서 결정.

---

## Stage 2. 배포 인프라

### S2.1 자동 업데이트 — `status: TODO`

옵션:
1. **Sparkle 2** — macOS 표준, edDSA 서명, appcast.xml 호스팅 필요.
2. **GitHub Releases 폴링** — Mac shell이 GitHub API로 latest tag 비교.

권장: GitHub Releases 최소 구현 먼저 → 베타 후 Sparkle 2 추가.

**파일:** `BackendSupervisor.swift` 또는 별도 `UpdateChecker.swift`,
`mac/PhotomeForMac/Resources/Info.plist`에 SUFeedURL (Sparkle 사용 시).

### S2.2 DMG 비주얼 폴리시 — `status: TODO`

현재는 표준 DMG. create-dmg 도구 또는 hdiutil layout 스크립트로 배경/창 위치 지정.

**파일:** `scripts/build_mac_app_bundle.sh` (선택적 단계 추가).

### S2.3 App icon 마무리 — `status: TODO`

`mac/PhotomeForMac/Resources/Assets.xcassets/AppIcon.appiconset/`의 모든 사이즈 채워졌는지 확인.

**검증:** Xcode 또는 `iconutil` 출력 확인.

### S2.4 Python runtime 번들 자동화 — `status: TODO`

현재 `PHOTOME_BUNDLE_PYTHON=1` + `PHOTOME_PYTHON_BUNDLE_SRC=...` 수동.
사용자가 venv 위치 지정 필요. GitHub Actions workflow에서 자동 빌드 가능하게.

**파일:** `.github/workflows/mac-release.yml`, `scripts/build_mac_app_bundle.sh`.

---

## Stage 3. 사전 QA

### S3.1 Xcode GUI 기본 동작 QA — `status: BLOCKED:user-required`

사용자가 Xcode에서 직접 실행하여 확인:
1. 백엔드 자동 시작
2. WebView 대시보드 로드
3. 메뉴바 동작
4. 전체 동기화/이미지 AI 버튼 enable/disable

### S3.2 LAN admin guard 크로스 디바이스 — `status: BLOCKED:user-required`

다른 기기에서 LAN URL 접근 → 관리자 API 401 확인.

### S3.3 NAS/대용량 라이브러리 시나리오 — `status: BLOCKED:user-required`

`RELEASE_CHECKLIST.md` Section 11. 사용자 환경 필요.

### S3.4 백엔드 크래시 복구 — `status: TODO`

`BackendSupervisor`가 backend process 비정상 종료 감지 시 자동 재시작 시도 1회 후 사용자 알림.

**파일:** `BackendSupervisor.swift`.

---

## Stage 4. 서명 & 노타리

### S4.1 Developer ID 인증서 — `status: BLOCKED:user-required`

사용자 환경에서:
```bash
security find-identity -v -p codesigning
export PHOTOME_MAC_SIGN_IDENTITY="Developer ID Application: <NAME> (<TEAMID>)"
```

### S4.2 notarytool 자격 저장 — `status: BLOCKED:user-required`

```bash
xcrun notarytool store-credentials photome-notary \
  --apple-id <email> --team-id <TEAMID> --password <app-specific>
```

### S4.3 첫 정식 DMG 빌드 — `status: BLOCKED:user-required`

S4.1, S4.2 완료 후:
```bash
PHOTOME_MAC_SIGN_IDENTITY=... scripts/build_mac_app_bundle.sh
PHOTOME_NOTARY_PROFILE=photome-notary scripts/notarize_mac_app.sh
```

### S4.4 GitHub Release 첫 업로드 — `status: BLOCKED:user-required`

`.github/workflows/mac-release.yml` 검토 후 tag push (`mac-v0.1.0`).

---

## 진행 기록 (가장 최근부터)

- 2026-05-29: S0.1 백포팅 완료 (alias + people UI + preview cache, 184 tests pass).
- 2026-05-29: 플랜 문서 생성.
