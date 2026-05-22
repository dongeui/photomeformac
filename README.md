# Photome for Mac

Photome for Mac은 Docker 설치 없이 실행되는 macOS 앱형 Photome 배포를 목표로 하는 작업 공간이다. 기존 Photome의 FastAPI 백엔드, 갤러리/대시보드, 사진 스캔, SQLite, CLIP 이미지 AI 기능을 Mac 앱 내부 런타임으로 통합한다.

현재 repo는 Mac 앱 전환 작업용이며, Docker는 서버/NAS/Linux/Windows/개발/CI용 보조 배포 경로로 유지한다.

## 기능 요약

- 사진 전용 (영상 제외)
- OCR, 태그, 장소명, 사람 이름, 자연어 검색
- CLIP 기반 이미지 의미 검색 (선택 — 로컬 모델 캐시 필요)
- 썸네일, 얼굴 클러스터링, 자동 태그 (장소·사물·날씨 등)
- HEIC 포함 주요 이미지 포맷 지원 (pillow-heif 내장)
- 원본 다운로드 지원

## 시작하기

### Docker

```bash
cp .env.docker.example .env
# .env 에서 PHOTOME_SOURCE_ROOT 를 사진 폴더 경로로 수정
docker compose up -d --build photome
```

- 갤러리: <http://127.0.0.1:8002/gallery>
- 대시보드: <http://127.0.0.1:8002/dashboard>

Docker 구성은 macOS Finder 경로를 그대로 쓸 수 있도록 `/Volumes`와 `/Users`를 읽기 전용으로 컨테이너에 마운트한다. 대시보드에서 NAS, 외장하드, USB, Desktop/Pictures 폴더를 선택할 때 사용자가 별도 Docker 경로를 신경 쓰지 않아도 된다.

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
| `GET /search?q=...` | 검색 API |
| `GET /media/{file_id}/download` | 원본 파일 다운로드 |

## 문서

- [docs/ops/DOCKER.md](docs/ops/DOCKER.md) — Docker 실행 및 볼륨 설정
- [docs/ops/RUNBOOK.md](docs/ops/RUNBOOK.md) — 운영 규칙·장애 처리
- [docs/engineering/ARCHITECTURE.md](docs/engineering/ARCHITECTURE.md) — 구조 개요
