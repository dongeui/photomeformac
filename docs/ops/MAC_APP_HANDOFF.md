# Photome Mac 앱 작업 인수인계

## 현재 결정

1. Photome의 메인 사용자 배포는 Mac 앱으로 간다.
2. Mac 앱은 Docker판과 기능을 나누는 별도 제품이 아니다.
3. Docker에 있는 기능을 Mac 앱 내부 런타임으로 통합한다.
4. 일반 Mac 사용자는 Docker 설치 없이 앱만 설치해서 사용하게 한다.
5. 공식 지원 범위는 local-only와 LAN까지다.
6. Public internet 공개는 현재 공식 지원하지 않는다.
7. Windows 배포는 당장은 네이티브 앱이 아니라 추후 Docker 기반으로 본다.
8. Docker 배포는 서버/NAS/Linux/Windows/개발/CI용으로 유지한다.
9. Mac 앱 작업은 현재 photome repo 안 브랜치보다 별도 레포를 권장한다.

## 배포 방향

### Mac 앱

목표:
- 일반 사용자용 메인 배포
- Docker 설치 없이 앱 설치만으로 실행
- 로컬 사진 폴더/NAS/외장하드 선택 가능
- 기존 dashboard/gallery/web UI와 FastAPI backend를 앱 내부에서 실행

포함할 기능:
1. FastAPI backend 실행
2. SQLite DB 관리
3. 사진 스캔/동기화
4. 썸네일/derived asset 관리
5. 사람/태그/검색 기능
6. CLIP 기반 이미지 AI
7. 모델 다운로드/캐시 관리
8. WebView 또는 브라우저 런처
9. 폴더 선택 UI
10. Local-only/LAN 공유 설정
11. 앱 상태 표시, 자동 시작, 알림 후보

### Docker

목표:
- 보조 배포
- 서버/NAS/Linux/Windows/개발/CI용

역할:
1. core 기능 재현성 확보
2. 홈서버/헤드리스 운영
3. Windows 사용자의 추후 배포 경로
4. Mac 앱과 같은 core 기능 검증

## 네트워크 정책

1. Local-only
   - 기본값
   - `127.0.0.1` 바인딩
   - 인증 없이도 개인 Mac 사용 기준 허용 가능

2. LAN
   - 공식 지원
   - 사용자가 명시적으로 켜는 옵션
   - 같은 네트워크 기기에서 접근 가능
   - 원본 다운로드/admin API는 추후 보호 옵션 검토

3. VPN/Tailscale/Cloudflare Access
   - 외부 접근이 필요할 때 권장

4. Public internet
   - 현재 공식 지원 보류
   - auth, 권한 분리, 원본 다운로드 보호, status 정보 제한 필요

## 폴더 접근 정책

1. 사용자가 직접 사진 폴더를 선택하는 제품이다.
2. 폴더 경로 접근 자체를 주요 보안 문제로 보지 않는다.
3. 핵심은 서버 접근 권한, 원본 다운로드, admin API 보호다.
4. 원본 폴더는 read-only source of truth다.
5. `file_id`가 identity이며 path는 durable identity가 아니다.
6. NAS/source root/path 변경으로 사람/태그/merge 상태가 초기화되면 안 된다.

## 레포 전략

권장:
- 현재 repo: `photome`
  - core backend
  - web UI
  - scanner/pipeline
  - Docker 배포
  - Python package

- 새 repo 후보: `photome-mac`
  - macOS app shell
  - bundled runtime packaging
  - model manager UI
  - launch/update/sign/notarize
  - Photome core를 dependency/submodule/release artifact 방식으로 포함

별도 레포 권장 이유:
1. 기술 스택이 다름
2. 앱 패키징/서명/notarization/updater 파일이 많아짐
3. core 릴리즈와 앱 릴리즈 사이클이 다름
4. 현재 repo가 backend/runtime/Docker 중심으로 유지됨
5. Windows Docker 배포와 Mac 앱 배포를 분리 관리하기 쉬움

## 이미 작성한 문서

1. `/Users/dongeui/Desktop/code/photome/docs/ops/DEPLOYMENT_STRATEGY.md`
   - 배포 전략 상세 정리
   - Mac 앱/Docker/Python 직접 실행의 역할
   - 네트워크 정책
   - 레포 분리 권장안

2. `/Users/dongeui/Desktop/code/photome/docs/ops/MAC_APP_HANDOFF.md`
   - 새 Mac 앱 레포 작업 시작용 인수인계 문서

## 다음 작업 후보

새 레포/폴더를 받은 뒤 진행:

1. Mac 앱 기술 선택
   - Swift/SwiftUI
   - Tauri
   - Electron

2. MVP 구조 결정
   - 앱 실행
   - backend local process 시작
   - health check
   - dashboard 열기
   - 앱 종료 시 backend 정리

3. Photome core 포함 방식 결정
   - git submodule
   - pip package dependency
   - release artifact embedding

4. 앱 데이터 위치 결정
   - DB
   - derived_root
   - model_cache
   - config

5. 폴더 선택과 권한 처리

6. LAN 공유 토글 구현

7. 모델 다운로드/캐시 관리 UI 구현

8. signing/notarization/DMG/updater 설계

## 주의할 불변조건

1. 원본 NAS/source root는 읽기 전용이다.
2. `file_id`가 identity다.
3. 사람/person/alias/merge 상태는 path 변경에도 누적 보존되어야 한다.
4. 전체 동기화는 destructive reset처럼 보이면 안 된다.
5. 파일 현황과 이미지 AI 상태는 분리해서 설명해야 한다.
6. Mac 앱과 Docker판은 기능 차이가 아니라 배포/운영 방식 차이다.
