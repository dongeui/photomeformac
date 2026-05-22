# photome 작업 규칙

이 파일이 이 저장소의 canonical instruction이다. 다른 지침 파일은 여기의 요약 또는 adapter만 둔다.

## 우선순위

상충 시 아래 순서로 적용한다.

1. `AGENTS.md`
2. `AGENTS_LIGHT.md`
3. `.codex/context/ALL_TASKS.md`
4. user prompt

기본 참조 원칙:

- 항상 먼저 보는 기준 문서는 `AGENTS.md`다.
- `AGENTS_LIGHT.md`는 빠른 체크리스트가 필요할 때만 함께 본다.
- 둘이 충돌하면 항상 `AGENTS.md`가 우선한다.

## 프로젝트 요약

- Photome는 NAS/로컬 원본 미디어를 읽기 전용으로 스캔하는 로컬 우선 사진 라이브러리다.
- 런타임은 FastAPI + scanner + processing pipeline + SQLite다.
- 현재 제품 범위는 `이미지 중심`이다. 영상은 기본 sync/search 대상에서 제외한다.

## 커뮤니케이션

- 중간 업데이트는 짧게 한다.
- 진행 로그나 긴 테스트 출력은 사용자에게 자세히 복기하지 않는다.
- 막히지 않으면 확인 질문보다 구현과 검증을 우선한다.
- 작업 단위가 끝나면 commit/push까지 진행한다.
- 사용자가 별도 요청하지 않으면 중간 단계 리포트는 최소화하고, 최종 결과만 compact하게 요약한다.

## 구현 원칙

- 하드코딩보다 동적 로직을 우선한다.
  - 사람/장소/검색 어휘는 DB 태그, 설정, 모델 결과를 우선 사용한다.
- 케이스별 패치보다 공통 로직을 선호한다.
- 원본 NAS는 source of truth이며 읽기 전용이다.
- `path`는 identity가 아니고 `file_id`가 identity다.
- 사람/인물 데이터도 `file_id` 기준으로 누적 보존한다. source root/NAS/drive/path 변경, 모델 재분석, face row 재생성 때문에 이미 지정한 이름, alias, merge 결과가 초기화되면 안 된다.
- cache/derived asset은 전부 재생성 가능해야 한다.
- NAS 오프라인, 파일 이동/이름 변경, 부분 업로드는 정상 시나리오로 취급한다.

## 배포 호환성

항상 두 배포 경로를 같이 고려한다.

1. `photome-base`
   - local AI pack 없이도 import/startup/scan/gallery/status/search 동작
2. `photome-local-ai-pack`
   - 모델 캐시 기반 CLIP/semantic 검색 동작
   - offline mode에서 다운로드 시도 금지

세부 원칙:

- PyTorch/open_clip/모델 weight는 optional path에 격리한다.
- base runtime import 단계에서 local-AI 의존성 때문에 실패하면 안 된다.
- 모델/프로바이더/dimension 변경은 `semantic_embedding_version` 검토 대상이다.
- concept/alias 변경은 `semantic_auto_tag_version` 검토 대상이다.
- search document 구성 변경은 `semantic_search_version` 검토 대상이다.

## 지침 파일 역할

- `AGENTS.md`: 전체 규칙의 canonical source
- `AGENTS_LIGHT.md`: 세션 시작용 quick checklist
- `CLAUDE.md`: Claude/Codex 공통 adapter
- `.codex/agents/*.md`: 역할별 축약 규칙

새 정책은 먼저 `AGENTS.md`에 반영하고, 다른 파일은 필요한 최소 요약만 유지한다.

## 현재 참고 문서

- `README.md`
- `docs/README.md`
- `docs/engineering/ARCHITECTURE.md`
- `docs/ops/DOCKER.md`
- `docs/ops/RUNBOOK.md`
- `.codex/context/ALL_TASKS.md`

삭제된 문서 경로를 다시 참조하지 않는다.

## 역할 경계

- Orchestrator: 라우팅, owner 전환, stage 전이
- Developer: production code
- QA: validation/test
- Planner: scope/acceptance/non-goal

역할별 상세 제약은 `.codex/agents/*.md`에 둔다.

## 세션 시작

1. `AGENTS.md`
2. `AGENTS_LIGHT.md`
3. 필요 시 역할별 `.codex/agents/*.md`
4. 필요 시 `.codex/context/ALL_TASKS.md`
5. 필요한 현재 문서만 추가로 읽는다.
