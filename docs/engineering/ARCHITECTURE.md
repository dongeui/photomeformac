# Architecture

## Runtime

- 서버: FastAPI + Uvicorn
- 저장소: SQLite (WAL 모드)
- 원본: 사용자가 지정한 사진 경로 (읽기 전용)
- 파생 데이터: 썸네일, 임베딩, 검색 문서
- UI: `/gallery`, `/dashboard`

## 처리 범위

- 사진만 처리 (HEIC, JPG, JPEG, PNG, DNG 등)
- 영상은 스캔, 썸네일, 분석, 검색 대상에서 제외
- 원본은 읽기 전용

## 처리 흐름

```
source root
  → scan (파일 탐색, fingerprint, metadata 추출)
      └─ HEIC: pillow-heif로 GPS EXIF 추출
  → thumbnail (Phase 1)
  → OCR / CLIP embedding / signals
  → auto-tag (CLIP zero-shot: 121 concepts, semantic_auto_tag_version=auto-v2)
  → geocoding (GPS → 장소 태그: geo, geo_detail, place)
  → face detection / clustering → person tags
  → search document (FTS + semantic)
  → gallery + NL search
```

semantic maintenance 사이클마다 GPS 누락 이미지를 자동 재추출(`_try_repair_gps`).

## 검색 아키텍처

3채널 하이브리드 검색 + RRF(Reciprocal Rank Fusion) + NL 플래너:

| 채널 | 방식 | 특징 |
|------|------|------|
| OCR | FTS (영어) + trigram (한국어) | 사진 속 텍스트 |
| CLIP | 벡터 유사도 (cosine) | 자연어 의미 검색 |
| Shadow | FTS + 태그 exact match | 태그·장소·사람 이름 |

- NL 플래너(`planner.py`): 쿼리를 `{person, place, visual, ocr, date}` 구조로 파싱, compound 조건 hard filter 적용
- 장소 검색: 지오코드 정규형까지 자동 확장 (`plan.place_terms`)
- 장소 검색 결과: 날짜 다양성 캡 미적용 → 전체 결과 반환
- compound condition fallback: 복합 쿼리가 0건이면 단일 조건(place/person/visual)으로 자동 완화
- 검색 문서 버전: `semantic_search_version` + content hash로 불필요한 재작성 방지
- 한국어: KoNLPy 형태소 분석 or heuristic fallback

## 자동 태그

- `app/services/analysis/clip_concepts.yaml`: 121개 concept (auto-v2), 각 영문 alias + 한국어 alias 포함
- 카테고리: 사람(6) · 화면(3) · 장면(39) · 사물(55) · 이벤트(18)
- `semantic_auto_tag_version` 변경 시 전체 파일 재태깅 (CLIP 임베딩 캐시 재사용, 분 단위 소요)
- `max_aliases_per_concept: 8`로 alias 수 제한

## 증분 스캔 캐시

- `DirMtimeCache` (`app/services/scanner/service.py`): 디렉토리 mtime을 기억해 변경 없는 폴더의 walk를 skip
- `data_root/scan_cache.json`에 디스크 persist — 백엔드 재시작 후에도 캐시 유지
- 첫 boot: 캐시 비어 있어 전체 walk → 완료 시 save
- 두 번째 boot부터: load → 변경된 디렉토리만 walk → 신규/갱신 카운트만 메시지에 노출

## 주요 경계

- `app/services/scanner`: 파일 탐색, 안정화 대기, dir mtime 캐시
- `app/services/processing`: 라이브러리 동기화, 자산 생성, 상태 전이, GPS 재추출
- `app/services/metadata`: EXIF/GPS 추출 (pillow-heif 포함)
- `app/services/geocoding`: GPS → 장소명 (로컬 GeoNames)
- `app/services/search`: hybrid search, FTS, vector search, planner, RRF, condition fallback
- `app/api`: gallery, dashboard, scan, media, people, search

## 상태 값

| 상태 | 의미 |
|------|------|
| `metadata_done` | 스캔·메타데이터 추출 완료, Phase 2 대기 |
| `thumb_done` | 썸네일 생성 완료 |
| `analysis_done` | Phase 2(embedding·OCR·태그·검색 문서) 완료 |
| `error` | 처리 중 오류 (재처리 대상) |
| `missing` | DB에 있으나 원본 없음 |
| `replaced` | 동일 fingerprint의 다른 경로로 대체됨 |
| `excluded` | 처리 대상 외 (legacy video 등) |

## 배포 타겟

정식 배포 산출물은 **ai-pack 단일 빌드**다 — Mac DMG는 항상 Python venv + CLIP weights를 번들하고(`TROVE_BUNDLE_*=1`), Docker는 `runtime-ai` 스테이지로 빌드한다. AI 미포함 빌드(`runtime-base`)는 배포하지 않으며, torch를 optional로 두는 **코드 레벨 import 계약**으로만 의미가 있다(startup 견고성 목적). 정책 상세는 `../../AGENTS.md`의 "배포 호환성" 참고.

`runtime-base`/`runtime-ai`는 `Dockerfile`의 빌드 스테이지명이다(`docker-compose.yml`의 `TROVE_DOCKER_TARGET` 기본값은 `runtime-ai`).

| 타겟 (Docker 스테이지) | 설명 | 배포 |
|------|------|------|
| `runtime-base` | PyTorch 없음. 기본 스캔·갤러리·OCR 동작 | 코드 계약만 (미배포) |
| `runtime-ai` | PyTorch + CLIP 포함. 의미 검색·자동 태그 동작 | **정식 배포 산출물 (Mac DMG / Docker 기본)** |

## 텔레메트리

opt-in **크래시/예외 리포팅(Sentry)**만 있고 기본 OFF다. 동의는 Mac 셸(Swift `CrashReporting`, UserDefaults)이 소유하고 백엔드(`app/core/telemetry.py`)는 env 게이트(`TROVE_CRASH_REPORTING`+`TROVE_SENTRY_DSN`)로만 켜진다. DSN을 빌드에 주입하지 않으면 기능 전체가 비활성이다. 사진·파일 경로·검색어 등 사용자 콘텐츠는 전송하지 않는다(경로 마스킹·`send_default_pii=False`·breadcrumb 제거·트레이싱 0). 셋업은 `../mac/USER_TODO.md`.
