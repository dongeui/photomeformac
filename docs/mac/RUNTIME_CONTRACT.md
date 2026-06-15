# Trove Mac 앱 백엔드 런타임 계약

## 목적

Mac 앱은 Docker 없이 Trove 백엔드를 로컬 프로세스로 실행한다. 이 문서는 macOS 앱 shell이 Python/FastAPI 백엔드를 시작, 감시, 종료할 때 지켜야 할 계약이다.

## 시작 명령

개발 모드에서는 repo 루트에서 다음 형태로 실행한다.

```bash
python3 -m app.main
```

패키징 단계에서는 앱 내부에 번들된 Python 런타임 또는 백엔드 실행 파일을 사용한다. 실행 방식이 바뀌어도 아래 환경 변수 계약은 유지한다.

## 기본 네트워크

1. 기본값은 local-only다.
2. `TROVE_SERVER_HOST=127.0.0.1`을 사용한다.
3. Mac 앱은 LAN 공유를 제공하지 않는다 — 항상 local-only다. 네트워크 노출이 필요하면 Docker/서버 배포(`TROVE_SERVER_HOST=0.0.0.0` + admin token 가드)를 쓴다.
4. Public internet 공개는 공식 지원 범위가 아니다.

## 필수 환경 변수

Mac 앱은 백엔드 실행 전에 다음 값을 만든다.

```env
TROVE_SERVER_HOST=127.0.0.1
TROVE_SERVER_PORT=8000
TROVE_DATA_ROOT=<AppData>/data
TROVE_DERIVED_ROOT=<AppData>/derived
TROVE_MODEL_ROOT=<AppData>/models
TROVE_GEODATA_ROOT=<AppData>/models/geodata
TROVE_DATABASE_PATH=<AppData>/data/photome.sqlite3
TROVE_OFFLINE_MODE=1
TROVE_CLIP_ENABLED=1
TROVE_SYNC_SCHEDULER_ENABLED=1
```

`TROVE_SOURCE_ROOTS`는 사용자가 선택한 macOS 원본 폴더 목록을 콤마로 연결한다. Docker 호환용 `/photos` 경로로 바꾸지 않는다.

## 앱 데이터 위치

초기 권장 위치는 다음과 같다.

```text
~/Library/Application Support/Trove/data/photome.sqlite3
~/Library/Application Support/Trove/derived/
~/Library/Application Support/Trove/models/
~/Library/Logs/Trove/
```

원본 사진은 앱 데이터로 복사하지 않는다. 사용자가 선택한 NAS, 외장하드, 로컬 폴더를 read-only source of truth로 취급한다.

## 상태 확인

앱 shell은 백엔드 시작 후 다음 순서로 확인한다.

1. 프로세스가 살아 있는지 확인한다.
2. `GET /healthz` 또는 `GET /status`를 폴링한다.
3. 정상 응답 후 대시보드 URL을 연다.

## 종료

앱 종료 또는 사용자의 중지 요청 시 child process에 정상 종료 신호를 보낸다. DB, derived asset, model cache, source root는 삭제하지 않는다.

## 로그

stdout/stderr는 앱 로그로 라우팅한다. 패키징 후에는 `~/Library/Logs/Trove/`에 저장하는 것을 기본으로 한다.

## AI/오프라인 정책

1. 앱은 CLIP 모델 없이도 실행되어야 한다.
2. `TROVE_OFFLINE_MODE=1`에서는 자동 다운로드를 시도하지 않는다.
3. 모델이 없으면 대시보드/앱 UI가 “모델 없음/다운로드 필요”를 설명한다.
4. 파일 처리 완료 수와 이미지 AI 완료 수는 다를 수 있으며, UI에서 이유를 설명한다.

## 불변조건

1. 원본 source root는 읽기 전용이다.
2. `file_id`가 identity다.
3. path 변경, NAS 재마운트, source root 변경으로 사람/alias/merge 상태가 초기화되면 안 된다.
