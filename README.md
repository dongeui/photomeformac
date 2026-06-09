# Photome for Mac

Docker 없이 실행되는 macOS 앱형 Photome. 기존 FastAPI 백엔드, 갤러리/대시보드, 사진 스캔, SQLite, CLIP 이미지 AI를 Mac 앱 내부 런타임으로 통합한다. Docker는 서버/NAS/Linux/Windows/CI용 보조 배포 경로로 유지한다.

> **배포 산출물은 ai-pack 단일 빌드다.** Mac DMG에는 항상 Python 런타임 + CLIP 모델이 동봉되어, 사용자는 인터넷 없이도 첫 실행부터 이미지 AI를 쓸 수 있다. AI 미포함 경량 빌드(`photome-base`)는 배포하지 않는다.

## 설치 (일반 사용자)

1. [Releases](https://github.com/dongeui/photomeformac/releases)에서 최신 `PhotomeForMac.dmg` 다운로드
2. DMG를 열고 **Photome 앱을 Applications 폴더로 드래그**
3. Applications에서 Photome 실행 → 사진 폴더 선택

설치/첫 실행 단계별 안내(Gatekeeper 경고 대처 포함)는 [INSTALL.md](INSTALL.md) 참고.

## 기능 요약

- 사진 전용 (영상 제외)
- OCR, 태그, 장소명, 사람 이름, 자연어 검색
- CLIP 기반 이미지 의미 검색 (DMG에 모델 동봉 — 첫 실행부터 동작)
- 썸네일, 얼굴 클러스터링, 자동 태그 (장소·사물·날씨 등)
- HEIC 포함 주요 이미지 포맷 지원 (pillow-heif 내장)
- 원본 다운로드 지원

## 개발/빌드

### Mac 앱 (권장)

```bash
# 개발: Xcode에서 직접 실행
open mac/PhotomeForMac/Package.swift

# 또는 DMG 빌드 (ad-hoc 서명)
scripts/build_mac_app_bundle.sh
# → dist/mac/PhotomeForMac.app, dist/mac/PhotomeForMac.dmg
```

자세한 Xcode 실행 가이드는 [docs/mac/XCODE_RUN.md](docs/mac/XCODE_RUN.md).
Developer ID 서명·notarization·릴리스 절차는 [docs/mac/RELEASE_CHECKLIST.md](docs/mac/RELEASE_CHECKLIST.md).

### Docker (서버/Linux/Windows)

```bash
cp .env.docker.example .env
# .env 에서 PHOTOME_SOURCE_ROOT 를 사진 폴더 경로로 수정
docker compose up -d --build photome
```

- 갤러리: <http://127.0.0.1:8002/gallery>
- 대시보드: <http://127.0.0.1:8002/dashboard>

Docker 구성은 macOS Finder 경로를 그대로 쓸 수 있도록 `/Volumes`와 `/Users`를 읽기 전용으로 컨테이너에 마운트한다.

### 로컬 Python

```bash
pip install -e .
photome
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

- [docs/mac/XCODE_RUN.md](docs/mac/XCODE_RUN.md) — Mac 앱 Xcode 개발 환경
- [docs/mac/RELEASE_CHECKLIST.md](docs/mac/RELEASE_CHECKLIST.md) — Mac 앱 서명·notarization·릴리스
- [docs/mac/RUNTIME_CONTRACT.md](docs/mac/RUNTIME_CONTRACT.md) — Mac shell ↔ 백엔드 계약
- [docs/engineering/ARCHITECTURE.md](docs/engineering/ARCHITECTURE.md) — 구조 개요
- [docs/ops/DOCKER.md](docs/ops/DOCKER.md) — Docker 실행 및 볼륨 설정
- [docs/ops/RUNBOOK.md](docs/ops/RUNBOOK.md) — 운영 규칙·장애 처리
