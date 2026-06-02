# Photome for Mac Docs

이 디렉토리는 Mac 앱과 core/runtime 문서를 함께 관리한다. Mac 앱은 Docker판과 기능 차이를 두지 않고, Docker급 기능을 앱 내부 런타임으로 통합한다.

## 문서 목록

### Mac 앱
- [mac/XCODE_RUN.md](mac/XCODE_RUN.md) — Xcode 개발 환경 + 실행
- [mac/RUNTIME_CONTRACT.md](mac/RUNTIME_CONTRACT.md) — Mac shell ↔ 백엔드 계약
- [mac/UI_SHELL_DECISION.md](mac/UI_SHELL_DECISION.md) — Swift/SwiftUI + WebView + 메뉴바 조합 결정
- [mac/RELEASE_CHECKLIST.md](mac/RELEASE_CHECKLIST.md) — 서명·notarization·DMG 릴리스
- [mac/RELEASE_PLAN.md](mac/RELEASE_PLAN.md) — 단위별 진행 트래커 (현재 코드 작업 전부 DONE)

### 운영
- [ops/DOCKER.md](ops/DOCKER.md) — Docker 실행·볼륨·AI 설정
- [ops/RUNBOOK.md](ops/RUNBOOK.md) — 운영 규칙·GPS 복구·장애 처리
- [ops/DEPLOYMENT_STRATEGY.md](ops/DEPLOYMENT_STRATEGY.md) — Mac/Docker 배포 정책·LAN 범위

### 엔지니어링
- [engineering/ARCHITECTURE.md](engineering/ARCHITECTURE.md) — 구조·검색·처리 흐름

### 루트
- [../README.md](../README.md) — 빠른 시작

## 현재 상태 (2026-06-02)

### Core (web/backend)
- 3채널 하이브리드 검색 (OCR/CLIP/Shadow) + RRF + NL 플래너
- 한국어 alias 전 concept 완비 (34개)
- HEIC 포함 주요 이미지 포맷, GPS 자동 재추출
- 사람 관리: alias chip, 빈 이름+애칭 자동 승격, 모드 스위치(이름 필요만/이름 있음/전체)
- 대시보드 리소스 컨트롤 (워커/torch threads/batch sizes 런타임 조절)

### Mac shell
- WebView 통합 + 메뉴바 아이콘
- 백엔드 supervisor (자동 시작/중지/재시작 + 비정상 종료 자동 복구)
- 소스 폴더 NSOpenPanel + Finder Drag&Drop
- UserNotifications (작업 완료, 새 버전, 백엔드 재시작)
- Dock badge (스캔/AI/오류 표시)
- Quit 확인 (작업 진행 중 보호)
- LAN 공유 토글 + admin token 자동 발급
- 모델 다운로드 progress (MB 표시)
- 로그인 자동 시작, 로그 보기, 진단 내보내기
- GitHub Releases 폴링 기반 업데이트 확인
