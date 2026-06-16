# Runbook

## 라이브러리 동기화 규칙

1. 사용자에게는 `라이브러리 동기화` 하나만 노출한다.
2. 한 번에 하나의 library job만 실행한다.
3. 신규 스캔과 재처리는 모두 사진 기준으로만 처리한다.
4. 영상은 `excluded`로 두고 다시 태우지 않는다.
5. 한 줄에 하나씩 지정한 source root 아래의 모든 하위 폴더를 재귀적으로 스캔한다.
6. 검색·갤러리·대시보드는 선택 폴더 전체 트리의 이미지를 대상으로 한다.
7. 동기화 중 semantic maintenance가 자동으로 실행되며 AI 분석, 검색 문서, GPS 재추출을 처리한다.

## GPS / 장소 태그

- **HEIC**: pillow-heif로 GPS EXIF 추출. semantic maintenance 실행 시 자동으로 GPS 누락 파일을 재추출한다.
- **JPG**: GPS 없는 파일은 원래부터 없는 것 — 자동 복구 불가.
- **PNG**: 대부분 스크린샷 — GPS 없는 게 정상.
- GPS 일괄 복구가 필요하면 `POST /scan/repair-metadata?batch_size=50000` 호출. 파일에 접근 가능한 환경(NAS가 마운트된 서버)에서 실행해야 한다.

## 검색 동작

- 검색은 OCR, CLIP(벡터), Shadow(FTS/태그) 3채널 RRF 결합.
- 장소 검색(`스위스`, `제주` 등)은 지오코드 정규형(예: `Schweiz/Suisse/Svizzera/Svizra`)까지 자동 확장.
- 장소 검색 결과에는 날짜 다양성 캡(하루 5장 제한)을 적용하지 않는다 — 같은 장소 사진을 전부 표시.
- 기본 검색 결과는 최대 500개, 갤러리에서 48장씩 페이지네이션.
- 자동 태그 버전: `auto-v2` (121 concept). 변경 시 전체 재태깅 발생하지만 CLIP 임베딩 캐시는 재사용 → 분 단위로 끝남.

## 에러 처리

- legacy video rows는 `excluded`
- 활성 이미지 오류가 생기면 `current_path` 존재 여부를 먼저 확인
- Docker 안에서 NAS 경로(`/Volumes/...`)가 접근 불가한 경우 → 스캔 폼은 컨테이너 마운트 경로로 자동 전환, API는 해당 경로를 스킵

## 원본 가용성 상태 (썸네일은 항상 로컬)

썸네일·검색·AI 결과는 로컬 `derived/`에 있어 원본과 독립적이다. 원본(`current_path`)
가용성은 3가지로 구분하며, "원본이 없다"가 곧 "사진이 사라진다"가 아니다.

1. **정상 (active)** — 원본 루트 연결됨 + 파일 존재. status=`thumb_done`/`analysis_done`.
   갤러리 노출 O, 원본 다운로드 O.
2. **보관 (archived)** — 사용자가 watch 목록(설정 source_roots)에서 그 폴더를 **뺐을 때**.
   `retire_missing_source`가 `archived`로 전이 → 썸네일·검색·AI 보존하고 **갤러리에
   계속 노출**, 처리 큐(thumb_done/analysis_done)에서만 제외해 재분석/오류 반복을 막는다.
3. **없음 (missing)** — 루트는 **마운트돼 활성인데** 개별 원본 파일이 삭제/이동됨.
   `mark_missing` → status=`missing` → **갤러리에서 숨김**. 스캔/NAS 재연결이 복원 관리.

### 오프라인 루트 (외장하드·NAS 분리) — missing 아님

가장 헷갈리는 지점. 저장소가 **지금 마운트 해제**된 상태는 DB status가 아니라 런타임
상태다. 스캔이 마운트된 루트만 `active_source_roots`로 잡고(`_path_exists`), 추가로
"전 루트 unavailable / 스캔 실패 / 100개 미만"이면 missing 정리를 통째로 스킵하는
false-missing 가드가 있어, **분리 중 동기화가 돌아도 그 사진들은 `missing`이 되지 않고
갤러리에 그대로 남는다**(썸네일로 계속 브라우징 가능). 갤러리 라이트박스는 렌더 시
원본 루트 연결 여부를 루트 단위로 확인해, 분리 상태면 **원본 다운로드 버튼만 비활성화**
(별도 뱃지 없음)하고, 그새 빠진 경우엔 다운로드 엔드포인트가 "원본 다운로드 불가"
안내를 띄운다. 재연결하면 자동으로 정상으로 돌아온다.

## 오프라인 / 로컬 정책

- 기본 목표는 로컬 우선 동작.
- 오프라인 모드에서는 외부 caption, 온라인 reverse geocoding, 자동 모델 다운로드를 차단.
- 로컬 GeoNames/Natural Earth 데이터와 로컬 CLIP 캐시는 허용.

## AI 검색

- CLIP은 선택 기능.
- 모델이 없으면 기본 검색, OCR, 태그 검색은 계속 동작.
- 모델 활성화 후 Phase 1 동기화와 semantic maintenance가 누락된 embedding/search document를 채운다.

## 크래시 리포팅 (opt-in)

- 기본 OFF. Mac 메뉴 토글 "익명 오류 보고"로 켜고, 켜면 백엔드가 재시작되며 env 게이트가 반영된다.
- 백엔드는 `TROVE_CRASH_REPORTING=1` + `TROVE_SENTRY_DSN`이 모두 있을 때만 Sentry를 초기화한다(둘 중 하나만 있으면 no-op). DSN 미주입 빌드는 기능 자체가 비활성.
- 크래시·예외만 전송하고 사진·경로·검색어 등 콘텐츠는 보내지 않는다. 셋업·프라이버시 상세는 `../mac/USER_TODO.md`.

## Docker 안전 규칙

- Docker 실행 중 호스트에서 `data/photome.sqlite3`를 직접 열지 않는다.
- 상태 확인은 `/status`, `/dashboard`, `/gallery`를 사용.

## 복구 절차

- Docker Desktop이 죽으면 먼저 Docker를 다시 올린다.
- 원본 경로가 바뀌었으면 Finder 기준 경로를 다시 입력하고 동기화를 돌린다.
- stale path가 많으면 full scan 1회로 `missing` 정리 우선.
- HEIC GPS 누락이 대량이면 `POST /scan/repair-metadata?batch_size=50000` 후 동기화 1회 실행.
