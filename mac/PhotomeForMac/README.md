# PhotomeForMac Swift shell

이 디렉토리는 Trove for Mac의 SwiftUI 메뉴바 셸이다. 창 없는 메뉴바 전용 앱으로, 사진첩·설정 같은 웹 UI는 기본 브라우저로 연다(앱 내장 WebView 창 없음).

메뉴바 메뉴 (현재 노출):

1. 상태 / 지금(진행 중 작업) / 사진 현황 / 리소스 표시
2. 사진첩 열기 (브라우저)
3. 사진 폴더 선택 (NSOpenPanel + Finder Drag&Drop) — 첫 선택 시 백엔드 자동 시작
4. 설정 열기 (브라우저 — 웹 '설정' 탭)
5. 로그인 시 자동 시작 토글
6. 종료

백그라운드 동작:

- Python 백엔드 supervisor 실행/중지 (비정상 종료 1회 자동 복구)
- 자동 동기화(시작 직후 + 주기 + 폴더 변경/NAS 재연결) — 수동 '지금 동기화'·'다시 시작' 메뉴 없음
- Dock badge (동기화/AI/오류), UserNotifications (작업 완료·새 버전·백엔드 자동 재시작)
- Quit 확인 (동기화 진행 중 종료 보호)
- 업데이트 자동 확인 (GitHub Releases 폴링, 24h)

> 로그 보기·진단 내보내기·모델 캐시 열기는 `BackendSupervisor`에 구현돼 있으나 현재 메뉴에는 노출하지 않는다. LAN 공유는 코드까지 제거됐다 — Mac 앱은 항상 local-only이고, 네트워크 노출은 Docker/서버 배포가 담당한다.

빌드/테스트:

```bash
cd mac/PhotomeForMac
swift build
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift test
```

실행 (개발):

```bash
cd mac/PhotomeForMac
swift run PhotomeForMac
```

⚠️ `swift run`은 .app 번들 없이 bare executable로 실행되어 `SMAppService.mainApp` 같은 일부 macOS API가 비활성화된다 (로그인 자동 시작 토글 등은 disabled). 전체 기능 테스트는 `scripts/build_mac_app_bundle.sh`로 정식 .app을 만든 뒤 진행하라.

정식 .app + DMG 생성 (정식 배포용 기본값 = Python venv + CLIP weights 번들):

```bash
# 1. venv 준비 (한 번만)
python3.11 -m venv .venv311
.venv311/bin/pip install -e ".[clip]"

# 2. 빌드 (기본은 풀번들 — DMG 약 540MB)
scripts/build_mac_app_bundle.sh
```

기본 서명은 ad-hoc. 정식 배포용 Developer ID + Notarization은 `docs/mac/RELEASE_CHECKLIST.md`와 `docs/mac/USER_TODO.md`를 따른다.

Xcode 실행은 `Package.swift`를 열어서 `PhotomeForMac` scheme으로 진행한다.

```text
/Users/dongeui/Desktop/code/photomeformac/mac/PhotomeForMac/Package.swift
```

필수 환경변수와 실행 순서는 `docs/mac/XCODE_RUN.md`를 따른다.
