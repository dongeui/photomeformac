# PhotomeForMac SwiftUI MVP

이 디렉토리는 Photome for Mac의 초기 SwiftUI shell이다.

현재 범위:

1. 앱 창 표시
2. 백엔드 상태 placeholder
3. 시작/중지/대시보드 버튼
4. 다음 단계에서 Python 백엔드 프로세스 supervisor 연결

빌드:

```bash
cd mac/PhotomeForMac
swift build
```

실행:

```bash
swift run PhotomeForMac
```

주의: 현재는 실제 Python 백엔드를 띄우지 않는 shell MVP다. 실제 연결은 `BackendSupervisor.swift`에서 진행한다.
