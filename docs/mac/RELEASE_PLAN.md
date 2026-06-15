# Trove for Mac 배포 진행 플랜

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

### S0.1 trove 핵심 패치 백포팅 — `status: DONE`

오늘자 trove에서 photomeformac으로 검증·이식.

- `5c4ac22 + b07818f + 9ef8aaa + 74a54d7` — alias 저장 + people 매니저 UI ✅
- `30fc787` — person preview cache ✅
- `4591ab1` — AI backlog 진행 중 표시 (이미 적용됨) ✅
- `3ded092` — bounded asset workers (이미 적용됨) ✅

### S0.2 추가 백포팅 후보

- `f00dbde + 7404a19` 배경 이미지 AI 상태 표시 — 확인 결과 이미 적용됨 ✅ `status: DONE`
- `553eb09` OCR heuristics + delta scan cache — 확인 결과 이미 적용됨 ✅ `status: DONE`
- **`698139f` Dashboard resource controls** — 분할 진행:
  - **S0.2a** 백엔드 infra + performance API + Mac env path 적응 — `status: DONE`
  - **S0.2b** 대시보드 UI (CSS/HTML/JS) — `status: DONE`

### S0.3 photomeformac 자체 테스트 검증 — `status: DONE`

`tests/` 184 passed (test_ux_e2e.py 제외).

---

## Stage 1. Mac shell UX 강화

### S1.1 Finder Drag & Drop으로 source root 추가 — `status: DONE`

`ContentView.swift` 또는 메뉴바에서 폴더 드래그 → `backend.appendSourceRoot(url)`.
SwiftUI `onDrop(of: [.fileURL])` 사용.

**파일:** `mac/PhotomeForMac/Sources/PhotomeForMac/ContentView.swift`,
`BackendSupervisor.swift` (appendSourceRoot 추가).

**테스트:** Swift unit test로 URL → string 정규화 검증.

### S1.2 macOS UserNotifications — `status: DONE`

스캔/AI 작업 완료 시 알림. `UNUserNotificationCenter` 사용. 사용자 권한 요청 흐름 필요.

**파일:** `BackendSupervisor.swift`에 NotificationCenter 헬퍼, `PhotomeForMacApp.swift`에서
`requestAuthorization` 호출.

**검증:** library job status가 running → succeeded로 바뀌는 순간 알림 발사.

### S1.3 Dock badge — `status: DONE`

활성 작업 있을 때 `NSApp.dockTile.badgeLabel`에 진행 % 또는 "..." 표시.

**파일:** `BackendSupervisor.swift`의 libraryJobStatus 옵저버에 묶음.

### S1.4 Quit confirmation — `status: DONE`

스캔/AI 진행 중 앱 종료 시 confirm dialog. `NSApplication.shouldTerminate` 처리.

**파일:** `PhotomeForMacApp.swift` AppDelegate adapter 또는 `applicationShouldTerminate`.

### S1.5 모델 다운로드 progress UI — `status: DONE`

현재 텍스트만 표시. 진행률 (다운로드 byte / total) → SwiftUI ProgressView.
백엔드 `/ai-pack`이 progress(bytes_downloaded/bytes_estimated/fraction)를 노출하며 완료됨(2026-06-02 기록 참조).

---

## Stage 2. 배포 인프라

### S2.1 자동 업데이트 — `status: DONE`

옵션:
1. **Sparkle 2** — macOS 표준, edDSA 서명, appcast.xml 호스팅 필요.
2. **GitHub Releases 폴링** — Mac shell이 GitHub API로 latest tag 비교.

권장: GitHub Releases 최소 구현 먼저 → 베타 후 Sparkle 2 추가.

**파일:** `BackendSupervisor.swift` 또는 별도 `UpdateChecker.swift`,
`mac/PhotomeForMac/Resources/Info.plist`에 SUFeedURL (Sparkle 사용 시).

### S2.2 DMG 비주얼 폴리시 — `status: DONE`

현재는 표준 DMG. create-dmg 도구 또는 hdiutil layout 스크립트로 배경/창 위치 지정.

**파일:** `scripts/build_mac_app_bundle.sh` (선택적 단계 추가).

### S2.3 App icon 마무리 — `status: DONE`

`mac/PhotomeForMac/Resources/Assets.xcassets/AppIcon.appiconset/`의 모든 사이즈 채워졌는지 확인.

**검증:** Xcode 또는 `iconutil` 출력 확인.

### S2.4 Python runtime 번들 자동화 — `status: DONE`

현재 `TROVE_BUNDLE_PYTHON=1` + `TROVE_PYTHON_BUNDLE_SRC=...` 수동.
사용자가 venv 위치 지정 필요. GitHub Actions workflow에서 자동 빌드 가능하게.

**파일:** `.github/workflows/mac-release.yml`, `scripts/build_mac_app_bundle.sh`.

---

## Stage 3. 사전 QA

### S3.1 Xcode GUI 기본 동작 QA — `status: BLOCKED:user-required`

사용자가 Xcode에서 직접 실행하여 확인:
1. 백엔드 자동 시작
2. "사진첩 열기"·"설정 열기"가 기본 브라우저에서 열림 (창 없는 메뉴바 앱)
3. 메뉴바 상태/진행 표시 동작
4. 자동 동기화 진행 + 동기화 중 "사진 폴더 선택" 잠금 확인

### S3.2 LAN admin guard 크로스 디바이스 — `status: BLOCKED:user-required`

> Mac 앱은 LAN 공유를 제거(local-only 고정)했다. 이 QA는 Docker/서버 배포(`0.0.0.0` 바인딩)에서만 해당한다.

다른 기기에서 LAN URL 접근 → 관리자 API 401 확인.

### S3.3 NAS/대용량 라이브러리 시나리오 — `status: BLOCKED:user-required`

`RELEASE_CHECKLIST.md` Section 11. 사용자 환경 필요.

### S3.4 백엔드 크래시 복구 — `status: DONE`

`BackendSupervisor`가 backend process 비정상 종료 감지 시 자동 재시작 시도 1회 후 사용자 알림.

**파일:** `BackendSupervisor.swift`.

---

## Stage 4. 서명 & 노타리

### S4.1 Developer ID 인증서 — `status: BLOCKED:user-required`

사용자 환경에서:
```bash
security find-identity -v -p codesigning
export TROVE_MAC_SIGN_IDENTITY="Developer ID Application: <NAME> (<TEAMID>)"
```

### S4.2 notarytool 자격 저장 — `status: BLOCKED:user-required`

```bash
xcrun notarytool store-credentials trove-notary \
  --apple-id <email> --team-id <TEAMID> --password <app-specific>
```

### S4.3 첫 정식 DMG 빌드 — `status: BLOCKED:user-required`

S4.1, S4.2 완료 후:
```bash
TROVE_MAC_SIGN_IDENTITY=... scripts/build_mac_app_bundle.sh
TROVE_NOTARY_PROFILE=trove-notary scripts/notarize_mac_app.sh
```

### S4.4 GitHub Release 첫 업로드 — `status: BLOCKED:user-required`

`.github/workflows/mac-release.yml` 검토 후 tag push (`mac-v0.1.0`).

---

## 진행 기록 (가장 최근부터)

- 2026-06-11: 설계-구현 일치성 감사(`docs/mac/AUDIT_2026-06-11.md`) 수행 및 A/B/D 항목 일괄 수정 — dir mtime 캐시 영속화 버그(매 스캔 전체 재워크 원인), /status·동기 스캔 엔드포인트의 이벤트 루프 블로킹, 수치 단일화(analyzed_current), 설정 탭 다이어트(시작/진행은 메뉴바), 임베딩 우선 maintenance, 메뉴바 리소스 표시, phase2 카드·EXIF 패널 죽은 코드 제거, 환경변수 TROVE_* 캐노니컬 통일.
- 2026-06-09: 배포 정책 확정 — 배포 산출물은 ai-pack 단일 빌드(CLIP/venv/weights 항상 번들). `trove-base`는 배포 제외(코드 레벨 import 계약만 유지). 용량(DMG ~540MB)은 인지하고 보류. 문서 일체 정리(AGENTS/AGENTS_LIGHT/CLAUDE/README/docs/DEPLOYMENT_STRATEGY/ARCHITECTURE/RELEASE_CHECKLIST) + 최종 사용자용 `INSTALL.md` 추가. 후속 코드 작업 완료: 빌드 스크립트 weights 누락 hard-fail(`build_mac_app_bundle.sh`), 포트 8000 자동 폴백(`BackendSupervisor.isPortAvailable`).
- 2026-06-03: Notarization 준비 — `.entitlements` 추가 (allow-jit, allow-unsigned-executable-memory, disable-library-validation, network.client/server, files.user-selected.read-only). build 스크립트가 Developer ID 서명 시 `--timestamp` + entitlements 자동 적용. DMG 자체도 codesign + stapler staple. notarize 스크립트가 .app까지 staple. GitHub Actions workflow에 시크릿 기반 인증서 import 단계 + 정식 빌드 + notarize 자동화 추가.
- 2026-06-03: 사용자 컨펌으로 정식 외부 배포 방향 결정 — Developer ID + Notarization. App Store 미경유, GitHub Release 단독 배포.
- 2026-06-03: First-run UX 폴리시 — 폴더 선택/Drag&Drop 시 자동 시작, source root 폴더명+경로 2줄 표시, 메뉴 라벨 동사화, AI Mode 토글 제거(offlineMode 상수화), 표준 About panel, landing 첫 분석 시간 안내.
- 2026-06-02: Xcode toolchain 빌드/테스트 검증 완료 — `xcodebuild build` SUCCEEDED, `xcodebuild test` 5/5 passed (webViewReload, jobSummary 등). swift test는 CLT 한정 `Testing` 모듈 부재 이슈로 Xcode toolchain 권장.
- 2026-06-02: landing UX 개선 — 폴더 있을 때 [백엔드 시작] prominent, [로그/진단] landing에서 제거 → 메뉴바에만 유지. startupHint로 포트 충돌 등 원인별 안내.
- 2026-06-02: S1.5 + S2.1 완료. /ai-pack/* progress(bytes_downloaded/bytes_estimated/fraction) 노출 + Mac shell summary에 MB 표시. mac_app_backend_env가 HF_HOME/TORCH_HOME을 model_root 하위로 라우팅. UpdateChecker가 6시간 주기로 GitHub Releases /releases/latest 폴링, 새 버전 발견 시 UNNotification + 메뉴에 "새 버전 다운로드…" 항목. swift build OK, 185 tests pass.
- 2026-05-30: people_stats + 1000명 cap으로 확장. 실데이터(1.14GB DB, 26,787장/176명) 기준 UI/UX 통합 점검: /healthz 200, /status 200, /dashboard 346KB+95 새 컴포넌트, /search?q=동이 적중, /people/40/preview 3장, POST /settings/performance 정상 저장 (워커 2/스레드 8, profile "절약"/"보통"), alias 승격(빈 이름+alias "테스트동이" → display_name "테스트동이") 확인 후 SQL 원복. 184 tests 회귀 없음.
- 2026-05-30: S2.2/2.3/2.4 완료 — build 스크립트가 iconset filter, Finder DMG layout(osascript), .venv311/.venv 자동 탐색 + GitHub Actions workflow에 bundle_python input 추가.
- 2026-05-30: S0.2b 완료 — 대시보드에 리소스 설정 카드 추가 (CPU 슬라이더, AI threads, batch sizes). 184 tests pass.
- 2026-05-30: S1.1-1.4 + S3.4 완료 — drag&drop, notifications, dock badge, quit confirmation, crash recovery. swift build OK.
- 2026-05-30: trove DB(.recover로 손상 복구) photomeformac data + Mac 앱 Library 양쪽에 배치. 26787 media / 595 people / 10710 faces.
- 2026-05-29: S0.2a 완료 — performance settings API + 백엔드 infra. Mac 앱은 `scripts/mac_app_backend_env.py`가 구성한 환경으로 백엔드를 띄우며, 거기서 `TROVE_ENV_FILE`이 앱 데이터 폴더의 `photome.env`로 지정된다. 188 tests pass.
- 2026-05-29: S0.1 백포팅 완료 (alias + people UI + preview cache, 184 tests pass).
- 2026-05-29: 플랜 문서 생성.
