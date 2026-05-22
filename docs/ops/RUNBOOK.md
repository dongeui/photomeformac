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
- 자동 태그 버전: `auto-v1` (변경 시 전체 재태깅 발생).

## 에러 처리

- 원본 경로가 없으면 `missing`
- legacy video rows는 `excluded`
- 활성 이미지 오류가 생기면 `current_path` 존재 여부를 먼저 확인
- Docker 안에서 NAS 경로(`/Volumes/...`)가 접근 불가한 경우 → 스캔 폼은 컨테이너 마운트 경로로 자동 전환, API는 해당 경로를 스킵

## 오프라인 / 로컬 정책

- 기본 목표는 로컬 우선 동작.
- 오프라인 모드에서는 외부 caption, 온라인 reverse geocoding, 자동 모델 다운로드를 차단.
- 로컬 GeoNames/Natural Earth 데이터와 로컬 CLIP 캐시는 허용.

## AI 검색

- CLIP은 선택 기능.
- 모델이 없으면 기본 검색, OCR, 태그 검색은 계속 동작.
- 모델 활성화 후 Phase 1 동기화와 semantic maintenance가 누락된 embedding/search document를 채운다.

## Docker 안전 규칙

- Docker 실행 중 호스트에서 `data/photome.sqlite3`를 직접 열지 않는다.
- 상태 확인은 `/status`, `/dashboard`, `/gallery`를 사용.

## 복구 절차

- Docker Desktop이 죽으면 먼저 Docker를 다시 올린다.
- 원본 경로가 바뀌었으면 Finder 기준 경로를 다시 입력하고 동기화를 돌린다.
- stale path가 많으면 full scan 1회로 `missing` 정리 우선.
- HEIC GPS 누락이 대량이면 `POST /scan/repair-metadata?batch_size=50000` 후 동기화 1회 실행.
