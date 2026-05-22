# Docker 운영 가이드

## 시작

```bash
cp .env.docker.example .env
# .env 에서 PHOTOME_SOURCE_ROOT 를 사진 폴더 경로로 수정
docker compose up -d --build photome
```

접속:

- 갤러리: <http://127.0.0.1:8002/gallery>
- 대시보드: <http://127.0.0.1:8002/dashboard>

## 경로 설정

`.env` 의 핵심 변수:

```env
PHOTOME_SOURCE_ROOT=/Volumes/homes/user/Photos   # 호스트(macOS) 사진 경로
PHOTOME_SOURCE_MOUNT=/photos                      # 컨테이너 내부 마운트 경로 (기본값 유지)
PHOTOME_PORT=8002                                 # 외부 포트
```

- NAS 경로(`/Volumes/...`), 외장 하드, USB, 로컬 폴더 모두 지원
- 호스트 경로가 실제로 마운트되어 있어야 Docker가 읽을 수 있음
- Docker 안에서 `/Volumes/...` 경로는 접근 불가 → 스캔 폼은 `/photos` (컨테이너 경로)로 자동 전환

## 볼륨 구조

| 호스트 | 컨테이너 | 용도 |
|--------|----------|------|
| `/Volumes` | `/Volumes` | macOS Finder NAS·외장하드·USB 경로 직접 선택용 (읽기 전용) |
| `/Users` | `/Users` | Desktop/Pictures 등 로컬 사용자 폴더 직접 선택용 (읽기 전용) |
| `$PHOTOME_SOURCE_ROOT` | `/photos` | 원본 사진 (읽기 전용) |
| `./data` | `/var/lib/photome/data` | SQLite DB |
| `./derived_root` | `/var/lib/photome/derived` | 썸네일·임베딩 |
| `./model_cache` | `/var/lib/photome/models` | CLIP 모델 캐시 |

기본 Docker 구성은 macOS의 Finder 경로를 그대로 받기 위해 `/Volumes`와 `/Users`를 컨테이너에도 같은 경로로 읽기 전용 마운트한다. 따라서 대시보드에서 NAS(`/Volumes/...`), 외장하드, USB, Desktop/Pictures 폴더를 선택해도 별도 경로 변환 없이 같은 문자열로 접근할 수 있다.

`PHOTOME_SOURCE_ROOT`/`PHOTOME_SOURCE_MOUNT`는 단일 기본 사진 루트를 `/photos` 같은 컨테이너 별칭으로도 제공하기 위한 호환 설정이다. Finder에서 고른 실제 경로가 `/Volumes` 또는 `/Users` 아래라면 실제 경로를 우선 사용한다.

## 컨테이너 실행 사용자

컨테이너는 시작 시 `PHOTOME_RUN_UID`/`PHOTOME_RUN_GID`에 맞는 passwd/group 항목을 만들고 그 사용자로 권한을 낮춘다. 이렇게 해야 `Path.home()`, CLIP/HF 캐시, Pillow/torch 계열 라이브러리가 숫자 UID만 보고 `getpwuid()`에서 실패하지 않는다.

```env
PHOTOME_RUN_UID=1000
PHOTOME_RUN_GID=1000
```

호스트의 bind mount 권한 때문에 다른 UID/GID가 필요하면 `.env`에서만 바꾼다. `docker-compose.yml`에 특정 로컬 사용자의 `user: "501:20"` 같은 값을 직접 고정하지 않는다.

## AI 검색 (CLIP)

기본 이미지(`runtime-base`)는 PyTorch 없이 빌드된다.  
AI 검색을 사용하려면 `runtime-ai` 타겟으로 빌드하거나 `.env`에서 지정:

```env
PHOTOME_DOCKER_TARGET=runtime-ai
PHOTOME_CLIP_ENABLED=1
PHOTOME_OFFLINE_MODE=1   # 모델 자동 다운로드 차단 (로컬 캐시만 사용)
```

모델이 `model_cache` 볼륨에 없으면 대시보드에서 "모델 받기"로 다운로드.

## 주의

- Docker 실행 중 호스트에서 `data/photome.sqlite3`를 직접 열지 않는다.
- Docker Desktop이 꺼져 있으면 컨테이너가 시작되지 않는다 — 먼저 Docker Desktop을 실행한다.
- NAS 경로를 source root로 쓸 경우 macOS에서 NAS가 마운트된 상태여야 한다.
- Docker Desktop의 File sharing에서 `/Volumes`와 `/Users` 접근이 허용되어 있어야 한다. 접근이 막히면 대시보드 폴더 선택 또는 scan API가 명확한 오류를 반환해야 하며, 다른 기본 루트로 조용히 대체 스캔하면 안 된다.
