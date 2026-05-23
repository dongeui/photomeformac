# PhotomeForMac SwiftUI MVP

이 디렉토리는 Photome for Mac의 SwiftUI + WebView shell이다.

현재 범위:

1. 앱 창 표시
2. Python 백엔드 supervisor 실행/중지/재시작
3. WebView로 기존 Photome dashboard 표시
4. 메뉴바 아이콘에서 상태 확인, 폴더 선택, LAN 토글
5. 이미지 AI on/off, 오프라인/온라인 준비 모드 전환, 모델 캐시 폴더 열기/준비 요청

빌드:

```bash
cd mac/PhotomeForMac
swift build
```

실행:

```bash
cd mac/PhotomeForMac
swift run PhotomeForMac
```

Xcode 실행은 `Package.swift`를 열어서 `PhotomeForMac` scheme으로 진행한다.

```text
/Users/dongeui/Desktop/code/photomeformac/mac/PhotomeForMac/Package.swift
```

필수 환경변수와 실행 순서는 `docs/mac/XCODE_RUN.md`를 따른다.
