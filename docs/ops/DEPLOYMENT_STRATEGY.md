# Photome 배포 전략

## 결론

Photome의 주 배포 방향은 Mac 앱이다. Docker판과 Mac 앱판을 기능적으로 나누지 않는다. Mac 앱은 현재 Docker 배포가 제공하는 기능을 앱 안에 통합한 형태로 제공한다.

Windows 배포는 당장 별도 네이티브 앱으로 추진하지 않는다. 추후 Docker 기반 배포를 우선 고려한다.

## 지원 범위

### 1. Mac 앱: 메인 배포

목표 사용자는 개인 Mac 사용자다.

Mac 앱은 다음 기능을 포함한다.

1. FastAPI 서버 런타임
2. 사진 스캔/동기화 파이프라인
3. SQLite 데이터 저장소
4. 썸네일/derived asset 관리
5. CLIP 기반 이미지 AI 기능
6. 모델 다운로드/캐시 관리
7. 대시보드/갤러리 웹 UI 실행
8. 폴더 선택 UI
9. 로컬/LAN 접근 설정
10. 자동 시작, 상태 표시, 알림 등 macOS 앱 UX

사용자는 Docker를 설치하지 않아도 된다. 앱 설치 후 사진 폴더를 선택하면 로컬 서버와 웹 UI가 앱 내부 런타임으로 실행된다.

### 2. Docker: 보조/서버/개발 배포

Docker는 Mac 앱과 기능 차이를 두기 위한 배포가 아니다. 다음 용도로 유지한다.

1. 개발/CI 재현성
2. 홈서버/NAS/Linux 상시운영
3. Windows 사용자의 임시 배포 경로
4. 헤드리스 운영
5. 고급 사용자의 self-hosted 운영

즉 일반 Mac 사용자에게 Docker 설치를 요구하지 않는다.

### 3. Local Python: 개발자용

Python 직접 실행은 개발/디버깅용으로 유지한다.

일반 사용자 대상 공식 설치 경로는 Mac 앱을 우선한다.

## 네트워크 정책

Photome는 로컬 개인 사진 라이브러리다. 기본 지원 네트워크 범위는 다음과 같다.

1. Local-only
   - 기본값
   - `127.0.0.1` 바인딩
   - 사용자 본인 Mac에서만 접근

2. LAN
   - 공식 지원 범위에 포함
   - 사용자가 명시적으로 켜는 옵션
   - 같은 네트워크의 다른 기기에서 접근 가능

3. VPN/Tailscale/Cloudflare Access
   - 권장 외부 접근 방식
   - Photome 자체 public internet 공개보다 우선

4. Public internet
   - 현재 공식 지원 대상 아님
   - auth, 권한 분리, original download 보호, status 민감정보 분리 후 검토

## 폴더 접근 정책

Mac 앱에서는 사용자가 직접 사진 폴더를 선택한다. 따라서 폴더 경로 접근 자체를 주요 보안 문제로 보지 않는다.

중요한 원칙은 다음과 같다.

1. 사용자가 선택한 원본 폴더는 read-only source of truth다.
2. Photome는 원본 사진 폴더에 쓰지 않는다.
3. `file_id`가 identity이며 path는 durable identity가 아니다.
4. NAS/source root/path 변경으로 사람/태그/merge 상태가 초기화되면 안 된다.
5. 보안상 중요한 지점은 폴더 경로 자체보다 서버 접근 권한, 원본 다운로드, admin API 보호다.

## Mac 앱 아키텍처 방향

Mac 앱은 단순 WebView 껍데기가 아니라 Photome 런타임 통합 앱으로 간다.

권장 구조:

1. macOS shell app
   - Swift/SwiftUI 또는 Tauri/Electron 후보 검토
   - 메뉴바, 설정, 폴더 선택, 상태 표시 담당

2. bundled backend runtime
   - Python/FastAPI 서버 번들
   - 앱 실행 시 로컬 백그라운드 프로세스로 시작
   - 기본은 `127.0.0.1`에 바인딩
   - LAN 공유 옵션에서만 LAN 바인딩

3. persistent app data
   - SQLite DB
   - derived assets
   - model cache
   - user config

4. web UI
   - 기존 dashboard/gallery UI 재사용
   - 앱 내부 WebView 또는 브라우저 런처로 표시

5. AI model manager
   - 앱 기본 설치 용량을 줄이기 위해 모델은 첫 실행 후 다운로드/캐시 방식 권장
   - offline mode에서는 캐시된 모델만 사용

## Mac 앱과 Docker의 기능 관계

기능은 동일하게 유지한다.

| 기능 | Mac 앱 | Docker |
|---|---|---|
| 갤러리 | 지원 | 지원 |
| 대시보드 | 지원 | 지원 |
| 폴더 선택 | 지원 | 지원 |
| NAS/외장하드 | 지원 | 지원 |
| 사람/태그/검색 | 지원 | 지원 |
| CLIP 이미지 AI | 지원 | 지원 |
| 모델 캐시 | 지원 | 지원 |
| Local-only | 지원 | 지원 |
| LAN 공유 | 지원 | 지원 |
| 서버/NAS 상시운영 | 제한적 | 적합 |
| Linux/Windows | 미지원 또는 추후 | 지원 후보 |

Mac 앱은 사용 편의성이 중심이고, Docker는 운영 환경 유연성이 중심이다.

## 레포 전략

Mac 앱은 별도 레포로 분리하는 것을 권장한다.

권장 이유:

1. 기술 스택이 다르다.
   - 현재 Photome: Python/FastAPI/backend/web UI
   - Mac 앱: Swift/Tauri/Electron, packaging, signing, notarization, updater

2. 배포 산출물이 다르다.
   - Photome core: Python package, Docker image
   - Mac app: `.app`, `.dmg`, updater metadata, signing artifacts

3. 릴리즈 사이클이 다르다.
   - core 기능 업데이트와 앱 패키징/서명/업데이트 이슈를 분리할 수 있다.

4. 저장소가 덜 복잡해진다.
   - 현재 repo는 core/runtime/backend 중심으로 유지
   - Mac app repo는 shell, installer, embedded runtime, updater 중심으로 유지

5. Windows/Linux Docker 배포와 Mac 앱 배포를 분리 관리하기 쉽다.

단, core 코드는 중복하지 않는다.

권장 구조:

1. `photome`
   - 현재 레포
   - core backend
   - web UI
   - scanner/pipeline
   - Docker 배포
   - Python package

2. `photome-mac`
   - 새 레포
   - macOS 앱 shell
   - bundled runtime packaging
   - model manager UI
   - launch/update/sign/notarize
   - 현재 `photome`를 git submodule, package dependency, release artifact 중 하나로 포함

## 브랜치 분리보다 별도 레포가 나은 경우

별도 레포가 적합한 조건:

1. 앱 패키징 파일이 많아진다.
2. Swift/Tauri/Electron 프로젝트가 생긴다.
3. 코드서명/notarization/DMG/updater 설정이 필요하다.
4. 앱 릴리즈와 core 릴리즈를 독립적으로 관리하고 싶다.
5. Windows는 Docker로, Mac은 앱으로 가져가고 싶다.

현재 방향은 이 조건에 해당하므로 별도 레포를 권장한다.

## 단일 레포가 나은 경우

단일 레포가 나은 경우도 있다.

1. 앱이 아주 얇은 wrapper일 뿐이다.
2. 별도 앱 코드가 거의 없다.
3. 릴리즈를 항상 core와 동시에 한다.
4. CI/CD를 단순하게 유지하고 싶다.

하지만 현재 목표는 “Docker 기능을 Mac 앱 안에 통합”하는 것이므로 단일 레포는 장기적으로 복잡해질 가능성이 높다.

## 권장 진행 순서

1. 현재 Photome repo에서 core/runtime boundary 정리
2. Mac 앱 repo 생성
3. Mac 앱에서 Photome backend를 로컬 프로세스로 실행하는 최소 MVP 구현
4. 앱 설정에서 data/derived/model/source root 지정
5. WebView 또는 브라우저로 dashboard 열기
6. 모델 다운로드/캐시 UI 추가
7. LAN 공유 토글 추가
8. signing/notarization/DMG 배포 구성
9. Docker 배포는 서버/Windows/Linux 경로로 유지

## 결정 사항 초안

1. 공식 메인 배포: Mac 앱
2. 일반 사용자에게 Docker 설치 요구하지 않음
3. Mac 앱은 Docker판과 같은 기능을 앱 내부 런타임으로 제공
4. Local-only와 LAN까지 공식 지원
5. Public internet은 공식 지원 보류
6. Windows는 추후 Docker 기반으로 우선 제공
7. Mac 앱 개발은 별도 레포 권장
8. 현재 repo는 Photome core/runtime/Docker 배포의 기준 repo로 유지
