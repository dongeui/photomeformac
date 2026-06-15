# Trove for Mac Docs

이 디렉토리는 Mac 앱과 core/runtime 문서를 함께 관리한다. Mac 앱은 Docker판과 기능 차이를 두지 않고, Docker급 기능을 앱 내부 런타임으로 통합한다.

> 배포 산출물은 ai-pack 단일 빌드(Python 런타임 + CLIP weights 항상 번들)다. AI 미포함 경량 빌드는 배포하지 않는다. 자세한 정책은 `../AGENTS.md`의 "배포 호환성" 참고.

## 문서 목록

### Mac 앱
- [mac/XCODE_RUN.md](mac/XCODE_RUN.md) — Xcode 개발 환경 + 실행
- [mac/RUNTIME_CONTRACT.md](mac/RUNTIME_CONTRACT.md) — Mac shell ↔ 백엔드 계약
- [mac/UI_SHELL_DECISION.md](mac/UI_SHELL_DECISION.md) — Swift/SwiftUI 메뉴바 셸 결정 (창 없는 메뉴바 전용 앱으로 수렴)
- [mac/RELEASE_CHECKLIST.md](mac/RELEASE_CHECKLIST.md) — 서명·notarization·DMG 릴리스
- [mac/RELEASE_PLAN.md](mac/RELEASE_PLAN.md) — 단위별 진행 트래커 (현재 코드 작업 전부 DONE)
- [mac/USER_TODO.md](mac/USER_TODO.md) — 사용자가 직접 해야 할 일 (Apple Developer 가입, 실기기 QA)
- [../INSTALL.md](../INSTALL.md) — 최종 사용자용 설치/첫 실행 가이드

### 운영
- [ops/DOCKER.md](ops/DOCKER.md) — Docker 실행·볼륨·AI 설정
- [ops/RUNBOOK.md](ops/RUNBOOK.md) — 운영 규칙·GPS 복구·장애 처리
- [ops/DEPLOYMENT_STRATEGY.md](ops/DEPLOYMENT_STRATEGY.md) — Mac/Docker 배포 정책·LAN 범위

### 엔지니어링
- [engineering/ARCHITECTURE.md](engineering/ARCHITECTURE.md) — 구조·검색·처리 흐름
- [engineering/PEOPLE_UX_PLAN.md](engineering/PEOPLE_UX_PLAN.md) — 사람(People) UX 개선 플랜

### 루트
- [../README.md](../README.md) — 빠른 시작

## 현재 상태 (2026-06-15)

### Core (web/backend)
- 3채널 하이브리드 검색 (OCR/CLIP/Shadow) + RRF + NL 플래너
- CLIP 자동 태그 121 concept (auto-v2), 영문 + 한국어 alias 동시 저장
- HEIC 포함 주요 이미지 포맷, GPS 자동 재추출
- 사람 관리: alias chip, 빈 이름+애칭 자동 승격, 모드 스위치(이름 필요만/이름 있음/전체)
- 대시보드 리소스 컨트롤 (워커/torch threads/batch sizes 런타임 조절)
- `DirMtimeCache` 디스크 persist — 백엔드 재시작 후에도 변경 없는 폴더 walk skip

### Mac shell (창 없는 메뉴바 전용 앱)
- 메뉴바 아이콘 + 상태/진행/사진 현황/리소스 표시, Dock badge
- 메뉴: 사진첩 열기·사진 폴더 선택·설정 열기·로그인 자동 시작 토글·종료 (웹 UI는 기본 브라우저로 열림)
- 자동 동기화(시작 직후 + 주기 + 폴더 변경/NAS 재연결) — 수동 '지금 동기화'·'다시 시작' 메뉴 없음, 설정은 웹 '설정' 탭으로 일원화
- 백엔드 supervisor (자동 시작/중지 + 비정상 종료 1회 자동 복구)
- 소스 폴더 NSOpenPanel + Finder Drag&Drop + 첫 선택 시 자동 시작
- UserNotifications (작업 완료, 새 버전, 백엔드 자동 재시작), Quit 확인 (동기화 진행 중 보호)
- 설치 시 언어 선택(한/영) + 네이티브 UI i18n
- CLIP / offline 토글 없음 — 정식 배포에서 항상 켜진 상태로 고정 (DMG에 weights 번들)
- 업데이트 자동 확인 (GitHub Releases 폴링, 24h) — 수동 '업데이트 확인'·About 메뉴는 제거
- LAN 공유·로그·진단·모델 캐시 열기는 `BackendSupervisor`에 구현돼 있으나 현재 메뉴 비노출

### 배포 자동화
- `TROVE_BUNDLE_PYTHON`/`TROVE_BUNDLE_WEIGHTS` 기본 1 — DMG에 venv + CLIP weights 동봉
- Entitlements (allow-jit, library-validation 우회 등) 자동 부착 — hardened runtime에서 PyTorch 실행
- DMG 자체 codesign + notarize → stapler staple .dmg + 내부 .app
- GitHub Actions workflow에 시크릿 기반 Developer ID 인증서 import + notarize 자동 단계
