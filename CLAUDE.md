# Claude / Codex adapter

이 파일은 별도 정책집이 아니다. canonical source는 `AGENTS.md`다.

Claude/Codex 공통 추가 메모만 유지한다.

- 작업 중 중간 출력은 하지 않는다. 막히거나 방향 전환이 필요할 때만 짧게 알리고, 끝나면 최종 결과만 요약한다.
- 긴 터미널 출력과 테스트 로그는 사용자에게 길게 복기하지 않는다.
- 작업 단위가 끝나면 commit/push까지 시도한다.
- 하드코딩 대신 DB/설정/공통 로직을 우선한다.
- 배포 산출물은 ai-pack 단일 빌드다. base는 배포하지 않지만, 새 기능은 코드 레벨에서 base import path와 ai-pack path 둘 다 깨지지 않게 검토한다(torch optional 유지).
