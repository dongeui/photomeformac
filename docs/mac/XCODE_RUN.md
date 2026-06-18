# Xcode에서 Trove for Mac 실행

## 1. 백엔드 Python 준비

터미널에서 한 번만 실행한다.

```bash
cd ~/Desktop/code/photomeformac
bash scripts/bootstrap_mac_backend_venv.sh
```

완료 후 Python 경로는 다음이다.

```text
~/Desktop/code/photomeformac/.venv/bin/python
```

## 2. Xcode에서 열기

Xcode에서 아래 파일을 연다.

```text
~/Desktop/code/photomeformac/mac/PhotomeForMac/Package.swift
```

중요: 지금은 `.xcodeproj`가 아니라 Swift Package 구조다. Scheme은 `PhotomeForMac`을 선택한다.

## 3. Scheme 환경변수 설정

상단 Scheme 메뉴에서:

```text
PhotomeForMac > Edit Scheme... > Run > Arguments > Environment Variables
```

아래 2개를 추가하고 체크박스를 켠다.

```text
TROVE_REPO_ROOT=~/Desktop/code/photomeformac
TROVE_PYTHON=~/Desktop/code/photomeformac/.venv/bin/python
```

중요: `source .venv/bin/activate` 같은 활성화 스크립트를 Xcode에 넣는 게 아니다. Xcode에는 `TROVE_PYTHON` 값으로 실제 Python 실행 파일 경로를 넣는다.

## 4. Run

Run 버튼을 누르면 메뉴바에 Trove 아이콘이 뜨고, 앱이 내부에서 FastAPI 백엔드를 실행한다. 사진 폴더를 고르면 백엔드가 기동되고, 메뉴의 "사진첩 열기"·"설정 열기"로 기본 브라우저에서 UI를 연다(창 없는 메뉴바 전용 앱).

CLI에서 Xcode 빌드 검증만 먼저 하고 싶으면 아래로 확인 가능하다.

```bash
cd ~/Desktop/code/photomeformac/mac/PhotomeForMac
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild -scheme PhotomeForMac -destination 'platform=macOS' build
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift test
```

임시 `.app`/`.dmg` 산출물은 아래로 만들 수 있다.

```bash
cd ~/Desktop/code/photomeformac
scripts/build_mac_app_bundle.sh
```

기본은 로컬 테스트용 ad-hoc 서명이다. Developer ID 배포는 `TROVE_MAC_SIGN_IDENTITY="Developer ID Application: ..." scripts/build_mac_app_bundle.sh`로 서명 identity를 지정한 뒤 notarization을 붙인다. 전체 릴리즈 체크리스트는 `docs/mac/RELEASE_CHECKLIST.md`를 따른다.

## 자주 나는 오류

### Python import 오류

대부분 Xcode가 시스템 Python(`/usr/bin/python3`)을 잡아서 생긴다. `TROVE_PYTHON`이 아래 값인지 확인한다.

```text
~/Desktop/code/photomeformac/.venv/bin/python
```

### 메뉴바 아이콘은 뜨는데 사진첩/설정이 안 열림

앱은 창 없는 메뉴바 전용이다. "사진첩 열기"·"설정 열기"는 기본 브라우저로 열리며, 백엔드가 `실행 중`이 돼야 활성화된다. 한참 지나도 `실행 중`이 안 되면 메뉴바에서 `종료` 후 앱을 다시 켠다 — 별도 `재시작` 메뉴는 없고, 종료 후 재실행이 곧 백엔드 재기동이다.

### 포트 충돌

기본 포트는 `8000`이다. 기존에 같은 포트를 쓰는 프로세스가 있으면 종료 후 다시 실행한다.
