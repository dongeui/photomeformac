# AGENTS_LIGHT

세션 시작용 quick checklist. 상세 정책은 `AGENTS.md`를 따른다.

## 체크리스트

- canonical은 `AGENTS.md`다. `AGENTS_LIGHT.md`는 요약본이다.
- active task는 하나만 둔다.
- 필요한 범위만 읽고 수정한다.
- 하드코딩 대신 공통 로직을 우선한다.
- 배포 산출물은 ai-pack 단일 빌드(CLIP/venv/weights 항상 번들)다. base는 배포하지 않는다.
- 단, 코드 레벨에서는 base import path와 ai-pack path 둘 다 깨지지 않게 만든다(torch optional 유지).
- 사람 이름/alias/merge는 source root/path 변경과 face 재분석 뒤에도 `file_id` 기준으로 보존한다.
- 긴 로그/중간 결과는 길게 보고하지 않는다.
- 작업 중 중간 출력은 하지 않는다. 막힐 때만 짧게 알리고, 끝나면 최종 결과만 compact하게 요약한다.
- 작업 단위가 끝나면 검증 후 commit/push한다.

## 현재 읽을 문서

- `AGENTS.md`
- `.codex/context/ALL_TASKS.md`
- `docs/engineering/ARCHITECTURE.md`
- `docs/ops/DOCKER.md`
- `docs/ops/RUNBOOK.md`
