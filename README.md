# Trove for Mac

Docker 없이 실행되는 macOS 앱형 Trove. 기존 FastAPI 백엔드, 갤러리/대시보드, 사진 스캔, SQLite, CLIP 이미지 AI를 Mac 앱 내부 런타임으로 통합한다. Docker는 서버/NAS/Linux/Windows/CI용 보조 배포 경로로 유지한다.

> **배포 산출물은 ai-pack 단일 빌드다.** Mac DMG에는 항상 Python 런타임 + CLIP 모델이 동봉되어, 사용자는 인터넷 없이도 첫 실행부터 이미지 AI를 쓸 수 있다. AI 미포함 경량 빌드(`trove-base`)는 배포하지 않는다.

## 설치 (일반 사용자)

1. [Releases](https://github.com/dongeui/photomeformac/releases)에서 최신 `Trove.dmg` 다운로드
2. DMG를 열고 **Trove 앱을 Applications 폴더로 드래그**
3. Applications에서 Trove 실행 → 사진 폴더 선택

설치/첫 실행 단계별 안내(Gatekeeper 경고 대처 포함)는 [INSTALL.md](INSTALL.md) 참고.

## 기능 요약

- 사진 전용 (영상 제외)
- OCR, 태그, 장소명, 사람 이름, 자연어 검색
- CLIP 기반 이미지 의미 검색 (DMG에 모델 동봉 — 첫 실행부터 동작)
- 썸네일, 얼굴 클러스터링, 자동 태그 (장소·사물·날씨 등)
- HEIC 포함 주요 이미지 포맷 지원 (pillow-heif 내장)
- 원본 다운로드 지원

## 앱 동작 방식 (동기화 타이밍)

Trove는 **창 없는 메뉴바 앱**이다. 사진첩/검색/설정은 메뉴의 "사진첩 열기"·"설정 열기"로 기본 브라우저에서 열고, 네이티브 조작(폴더 선택·로그인 자동 시작)만 메뉴바 아이콘에 모여 있다. 앱을 켜면 선택해 둔 사진 폴더가 있을 경우 백엔드(FastAPI + 이미지 AI)가 자동으로 함께 뜬다.

### 동기화는 전부 자동이다

"동기화" 하나로 **스캔 → 썸네일·얼굴·검색 색인 → 이미지 AI/검색 문서 갱신**까지 한 번에 처리한다. 수동 "지금 동기화" 버튼은 없다 — 아래 시점에 알아서 돈다:

- **앱/백엔드 시작 직후** (백엔드가 뜨고 약 30초 뒤 첫 실행)
- **주기적으로** 기본 **10분(600초)**마다, 할 일(새 파일·미색인 백로그)이 있을 때만
- **사진 폴더를 바꿨을 때** (백엔드를 다시 띄우며 동기화)
- **NAS가 재연결**됐을 때
- 앱을 **종료했다 다시 켤 때** (켜지면 백엔드가 자동 기동 → 시작 직후 동기화)

> 주기·자동 동기화 on/off는 웹 **설정** 탭에서 조절한다. 환경변수 `TROVE_SYNC_SCHEDULER_INTERVAL_SECONDS`(기본 600), `TROVE_SYNC_SCHEDULER_ENABLED`로도 바꿀 수 있다.

### 새로 넣은 사진이 검색에 보이기까지

앱을 켜둔 채 사진을 추가하면 다음 동기화 주기(최대 ~10분)에 색인된다. 동기화는 **느린 전체 스캔(특히 NAS)이 끝나기 전에 이미 발견된 미처리 사진(백로그)을 먼저 처리**하므로, 스캔이 중간에 끊겨도 매 동기화마다 색인이 전진한다. 메뉴의 "사진 현황 / 지금 …"에서 진행 상황을 볼 수 있다.

### 동기화 중 메뉴 동작

동기화가 도는 동안에는 백엔드를 내려 작업을 끊는 조작("사진 폴더 선택")이 잠긴다. "사진첩 열기"·"설정 열기"는 동기화 중에도 열 수 있다. 별도의 "다시 시작" 메뉴는 없다 — 문제가 생기면 **"종료" 후 앱을 다시 켜면** 백엔드가 깨끗하게 재기동된다.

## 개발/빌드

### Mac 앱 (권장)

```bash
# 개발: Xcode에서 직접 실행
open mac/PhotomeForMac/Package.swift

# 또는 DMG 빌드 (ad-hoc 서명)
scripts/build_mac_app_bundle.sh
# → dist/mac/Trove.app, dist/mac/Trove.dmg
```

자세한 Xcode 실행 가이드는 [docs/mac/XCODE_RUN.md](docs/mac/XCODE_RUN.md).
Developer ID 서명·notarization·릴리스 절차는 [docs/mac/RELEASE_CHECKLIST.md](docs/mac/RELEASE_CHECKLIST.md).

### Docker (서버/Linux/Windows)

```bash
cp .env.docker.example .env
# .env 에서 TROVE_SOURCE_ROOT 를 사진 폴더 경로로 수정
docker compose up -d --build trove
```

- 갤러리: <http://127.0.0.1:8002/gallery>
- 대시보드: <http://127.0.0.1:8002/dashboard>

Docker 구성은 macOS Finder 경로를 그대로 쓸 수 있도록 `/Volumes`와 `/Users`를 읽기 전용으로 컨테이너에 마운트한다.

### 로컬 Python

```bash
pip install -e .
trove
```

## 주요 엔드포인트

| 엔드포인트 | 설명 |
|---|---|
| `GET /gallery` | 사진 갤러리 (검색·필터 포함) |
| `GET /dashboard` | 대시보드 |
| `POST /scan/async` | 라이브러리 동기화 (비동기) |
| `POST /scan/semantic-maintenance/async` | AI 분석·검색 문서 갱신 |
| `POST /scan/repair-metadata` | GPS 누락 이미지 재추출 |
| `POST /settings/performance` | 워커/배치 사이즈 런타임 조절 |
| `GET /search?q=...` | 검색 API |
| `GET /media/{file_id}/download` | 원본 파일 다운로드 |

## 문서

전체 문서 인덱스는 **[docs/README.md](docs/README.md)**. 자주 보는 문서:

- [docs/mac/XCODE_RUN.md](docs/mac/XCODE_RUN.md) — Mac 앱 Xcode 개발 환경
- [docs/mac/RELEASE_CHECKLIST.md](docs/mac/RELEASE_CHECKLIST.md) — Mac 앱 서명·notarization·릴리스
- [docs/mac/RUNTIME_CONTRACT.md](docs/mac/RUNTIME_CONTRACT.md) — Mac shell ↔ 백엔드 계약
- [docs/engineering/ARCHITECTURE.md](docs/engineering/ARCHITECTURE.md) — 구조 개요
- [docs/ops/DOCKER.md](docs/ops/DOCKER.md) — Docker 실행 및 볼륨 설정
- [docs/ops/RUNBOOK.md](docs/ops/RUNBOOK.md) — 운영 규칙·장애 처리
