# trove 작업 규칙

이 파일이 이 저장소의 canonical instruction이다. 다른 지침 파일은 여기의 요약 또는 adapter만 둔다.

## 우선순위

상충 시 아래 순서로 적용한다.

1. `AGENTS.md`
2. `AGENTS_LIGHT.md`
3. user prompt

기본 참조 원칙:

- 항상 먼저 보는 기준 문서는 `AGENTS.md`다.
- `AGENTS_LIGHT.md`는 빠른 체크리스트가 필요할 때만 함께 본다.
- 둘이 충돌하면 항상 `AGENTS.md`가 우선한다.

## 프로젝트 요약

- 이 저장소는 Trove for Mac 작업 공간이다.
- 목표는 Docker 설치 없이 실행되는 macOS 앱 안에 Trove의 FastAPI + scanner + processing pipeline + SQLite + web UI + local AI 기능을 통합하는 것이다.
- Trove는 NAS/로컬 원본 미디어를 읽기 전용으로 스캔하는 로컬 우선 사진 라이브러리다.
- 현재 제품 범위는 `이미지 중심`이다. 영상은 기본 sync/search 대상에서 제외한다.
- Docker는 기능 차별용이 아니라 서버/NAS/Linux/Windows/개발/CI용 보조 배포 경로로 유지한다.

## 커뮤니케이션

- 작업 진행 중 중간 출력(진행 상황 보고, 단계별 설명)은 하지 않는다. 막히거나 방향 전환이 필요할 때만 짧게 알린다.
- 요청 하나가 끝나면 최종 결과만 compact하게 요약해 전달한다.
- 진행 로그나 긴 테스트 출력은 사용자에게 복기하지 않는다.
- 막히지 않으면 확인 질문보다 구현과 검증을 우선한다.
- 작업 단위가 끝나면 commit/push까지 진행한다.

## 구현 원칙

- 하드코딩보다 동적 로직을 우선한다.
  - 사람/장소/검색 어휘는 DB 태그, 설정, 모델 결과를 우선 사용한다.
- 케이스별 패치보다 공통 로직을 선호한다.
- 원본 NAS는 source of truth이며 읽기 전용이다.
- `path`는 identity가 아니고 `file_id`가 identity다.
- 사람/인물 데이터도 `file_id` 기준으로 누적 보존한다. source root/NAS/drive/path 변경, 모델 재분석, face row 재생성 때문에 이미 지정한 이름, alias, merge 결과가 초기화되면 안 된다.
- cache/derived asset은 전부 재생성 가능해야 한다.
- NAS 오프라인, 파일 이동/이름 변경, 부분 업로드는 정상 시나리오로 취급한다.
- UI를 줄이거나 제거하는 커밋은 관련 가드 테스트와 빌드 설정(Package.swift 등)을 같은 커밋에서 갱신한다. (2026-06-11 감사에서 3건 재발)
- 기본값 폴백에 `x or Default()`를 쓰지 않는다 — `__len__`이 있는 객체(빈 캐시/컬렉션)는 falsy라서 멀쩡한 인스턴스가 버려진다. `x if x is not None else Default()`를 쓴다. (DirMtimeCache 영속화가 이 패턴으로 죽어 있었다)
- 환경변수 캐노니컬은 `TROVE_*`다. `PHOTOMINE_*`은 레거시 별칭으로만 읽고 새 코드에 쓰지 않는다.
- async 핸들러에서 수 초 이상 걸리는 동기 작업(스캔/분석/대량 집계)을 직접 호출하지 않는다 — `run_in_threadpool`로 내린다.

## 배포 호환성

### 배포 산출물 정책 (2026-06-09 확정)

- **정식 배포 산출물은 `trove-local-ai-pack` 단일 빌드뿐이다.** Mac DMG는 항상 Python venv + CLIP + ViT-B-32 weights를 번들한다(`TROVE_BUNDLE_PYTHON=1` / `TROVE_BUNDLE_WEIGHTS=1` 고정).
- **`trove-base`(AI 미포함)는 더 이상 배포 산출물로 만들지 않는다.** 용량 절감 목적의 경량 빌드는 현재 범위 밖이다.
- 단, 아래 "코드 레벨 base 계약"은 그대로 유지한다. base를 배포하지 않더라도 torch를 optional로 두는 규율이 startup 견고성에 도움이 되기 때문이다.

### 코드 레벨 base 계약 (유지)

배포물은 ai-pack 하나지만, 코드는 여전히 두 import 경로가 모두 깨지지 않게 작성한다.

1. base import path
   - local AI pack 없이도 import/startup/scan/gallery/status/search 동작
2. ai-pack path
   - 모델 캐시 기반 CLIP/semantic 검색 동작
   - offline mode에서 다운로드 시도 금지

세부 원칙:

- PyTorch/open_clip/모델 weight는 optional path에 격리한다.
- base runtime import 단계에서 local-AI 의존성 때문에 실패하면 안 된다(코드 레벨 계약).
- 모델/프로바이더/dimension 변경은 `semantic_embedding_version` 검토 대상이다.
- concept/alias 변경은 `semantic_auto_tag_version` 검토 대상이다.
- search document 구성 변경은 `semantic_search_version` 검토 대상이다.

## 지침 파일 역할

- `AGENTS.md`: 전체 규칙의 canonical source
- `AGENTS_LIGHT.md`: 세션 시작용 quick checklist
- `CLAUDE.md`: Claude/Codex 공통 adapter

새 정책은 먼저 `AGENTS.md`에 반영하고, 다른 파일은 필요한 최소 요약만 유지한다.

## 현재 참고 문서

- `README.md`
- `docs/README.md`
- `docs/engineering/ARCHITECTURE.md`
- `docs/mac/RUNTIME_CONTRACT.md`
- `docs/mac/RELEASE_CHECKLIST.md`
- `docs/mac/RELEASE_PLAN.md` (단위 진행 트래커)
- `docs/mac/XCODE_RUN.md`
- `docs/mac/UI_SHELL_DECISION.md`
- `docs/ops/DOCKER.md`
- `docs/ops/RUNBOOK.md`
- `docs/ops/DEPLOYMENT_STRATEGY.md`
- `docs/engineering/PEOPLE_UX_PLAN.md`
- `docs/mac/USER_TODO.md`

문서 인덱스는 `docs/README.md`에 모은다. 삭제된 문서 경로를 다시 참조하지 않는다.

## 역할 경계

- Orchestrator: 라우팅, owner 전환, stage 전이
- Developer: production code
- QA: validation/test
- Planner: scope/acceptance/non-goal

## 세션 시작

1. `AGENTS.md`
2. `AGENTS_LIGHT.md`
3. 필요한 현재 문서만 추가로 읽는다(`docs/README.md` 인덱스 참고).
