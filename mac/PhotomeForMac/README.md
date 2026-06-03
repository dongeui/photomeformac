# PhotomeForMac Swift shell

이 디렉토리는 Photome for Mac의 SwiftUI + WebView shell이다.

기능:

1. 앱 창 + WebView로 기존 Photome dashboard 표시
2. Python 백엔드 supervisor 실행/중지/재시작 (Process.terminationHandler로 비정상 종료 1회 자동 복구)
3. 메뉴바 아이콘 + 상태 표시, Dock badge
4. 사진 폴더 선택 (NSOpenPanel + Finder Drag&Drop) — 첫 선택 시 백엔드 자동 시작
5. LAN 공유 토글 (admin token 자동 발급)
6. 모델 준비/재로드, 모델 캐시 폴더 열기
7. 전체 동기화 / 이미지 AI 이어서 분석 빠른 실행
8. UserNotifications (작업 완료, 새 버전, 백엔드 재시작 알림)
9. Quit 확인 (스캔/AI 진행 중 종료 보호)
10. 로그인 자동 시작, 로그 보기, 진단 내보내기
11. 업데이트 확인 (GitHub Releases 폴링)
12. 표준 macOS About panel

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
