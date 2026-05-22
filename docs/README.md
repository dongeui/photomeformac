# Photome for Mac Docs

이 디렉토리는 Photome Mac 앱 전환 작업과 기존 Photome core/runtime 문서를 함께 관리한다. Mac 앱은 Docker판과 기능 차이를 두지 않고, Docker급 기능을 앱 내부 런타임으로 통합하는 방향이다.

## 문서 목록

- [mac/RUNTIME_CONTRACT.md](mac/RUNTIME_CONTRACT.md) — Mac 앱 shell과 백엔드 런타임 계약
- [plans/2026-05-22-mac-app-conversion-prep.md](plans/2026-05-22-mac-app-conversion-prep.md) — Mac 앱 전환 준비 플랜
- [ops/DEPLOYMENT_STRATEGY.md](ops/DEPLOYMENT_STRATEGY.md) — 배포 전략
- [ops/MAC_APP_HANDOFF.md](ops/MAC_APP_HANDOFF.md) — Mac 앱 작업 인수인계

- [../README.md](../README.md) — 빠른 시작
- [ops/DOCKER.md](ops/DOCKER.md) — Docker 실행·볼륨·AI 설정
- [ops/RUNBOOK.md](ops/RUNBOOK.md) — 운영 규칙·GPS 복구·장애 처리
- [engineering/ARCHITECTURE.md](engineering/ARCHITECTURE.md) — 구조·검색·처리 흐름

## 현재 상태 (2026-05-15)

- T1~T24 전체 구현 완료
- 라이브러리 동기화 단일 흐름 (Phase 1 + 2 내부 처리)
- HEIC GPS 자동 재추출: semantic maintenance 사이클마다 GPS 누락 이미지 처리
- 3채널 하이브리드 검색 (OCR/CLIP/Shadow) + RRF
- 자연어 검색: NL 플래너, 복합 조건 hard filter, condition fallback
- 시간 표현 처리: 작년, 지난달, 2024년 여름 등
- 장소 검색: 지오코드 정규형 자동 확장, 날짜 다양성 캡 미적용
- 한국어 alias 전 concept 완비 (34개)
- 사람 관리 UI: alias chip, 다중 선택 벌크 이동
- 원본 파일 다운로드 (`/media/{file_id}/download`)
