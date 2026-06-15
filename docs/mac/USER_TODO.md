# 사용자가 직접 해야 할 일

코드/스크립트 자동화는 끝났고, 아래 항목들만 사용자 환경(Apple 계정, 실 기기 QA 등)에서 채워야 한다.

---

## 1. Apple Developer 계정 (정식 배포 필수)

### 1.1 Apple Developer Program 가입
- **링크:** https://developer.apple.com/programs/enroll/
- **비용:** $99/년
- **소요:** 신청 후 24~48시간 내 승인 (개인은 보통 즉시)
- **필요한 것:** Apple ID + 신용카드 + (개인은) 본인 인증

### 1.2 Developer ID Application 인증서 발급
- https://developer.apple.com/account/resources/certificates/list 접속
- "+" → **Developer ID Application** 선택
- CSR(Certificate Signing Request) 만들기:
  1. **Keychain Access** 앱 열기
  2. 메뉴 → `Keychain Access` → `인증서 도우미` → `인증 기관에 인증서 요청`
  3. 이메일 + Common Name 입력 → "디스크에 저장" → `.certSigningRequest` 파일 저장
- 그 파일을 Apple 사이트에 업로드 → `.cer` 다운로드 → **더블클릭으로 Keychain에 설치**
- 확인:
  ```bash
  security find-identity -v -p codesigning
  # "Developer ID Application: <NAME> (<TEAMID>)" 항목이 보여야 함
  ```

### 1.3 App-Specific Password 생성
- https://appleid.apple.com → Sign-In and Security → App-Specific Passwords
- 이름: `trove notary` (임의)
- 생성된 16자 비밀번호 안전한 곳에 저장 (한 번만 보임)

### 1.4 notarytool keychain profile 저장 (재사용용, 한 번만)
```bash
xcrun notarytool store-credentials trove-notary \
  --apple-id <Apple ID 이메일> \
  --team-id <TEAMID> \
  --password <위에서 만든 16자 비번>
```

Team ID는 https://developer.apple.com/account → Membership에서 확인.

---

## 2. 로컬 빌드 (한 번 정상 동작 확인용)

```bash
export TROVE_MAC_SIGN_IDENTITY="Developer ID Application: <NAME> (<TEAMID>)"
export TROVE_NOTARY_PROFILE=trove-notary

# 1. venv 준비 (한 번만, 이미 있으면 skip)
python3.11 -m venv .venv311
.venv311/bin/pip install -e ".[clip]"

# 2. 빌드 (서명 자동 적용)
scripts/build_mac_app_bundle.sh

# 3. notarize (5~30분 소요)
scripts/notarize_mac_app.sh
```

결과: `dist/mac/Trove.dmg`가 notarized 상태로 만들어짐. 외부 Mac에서 더블클릭만으로 실행 가능.

---

## 3. GitHub Actions 자동 빌드 (선택, 권장)

수동 빌드 대신 tag push로 자동 빌드/Release 업로드.

### 3.1 Repository Secrets 추가
GitHub repo → Settings → Secrets and variables → Actions → New repository secret

| Secret 이름 | 값 |
|---|---|
| `MAC_SIGN_IDENTITY` | `Developer ID Application: <NAME> (<TEAMID>)` |
| `MAC_DEVELOPER_ID_CERT_BASE64` | 인증서 .p12 파일을 base64로 인코딩한 문자열 (아래 참고) |
| `MAC_DEVELOPER_ID_CERT_PASSWORD` | .p12 export 시 설정한 비밀번호 |
| `MAC_NOTARY_APPLE_ID` | Apple ID 이메일 |
| `MAC_NOTARY_TEAM_ID` | Team ID |
| `MAC_NOTARY_PASSWORD` | App-Specific Password (16자) |

**.p12 export 절차:**
```bash
# Keychain Access에서 "Developer ID Application: ..." 인증서 우클릭 → 내보내기 →
# .p12 형식 + 비번 설정 → developer_id.p12 저장
base64 -i developer_id.p12 | pbcopy   # 클립보드에 base64 복사
# 그 내용을 MAC_DEVELOPER_ID_CERT_BASE64 시크릿에 붙여넣기
```

### 3.2 릴리스 트리거
```bash
git tag mac-v0.1.0
git push origin mac-v0.1.0
# → Actions에서 자동 빌드/서명/notarize/Release 업로드
```

또는 Actions 탭에서 "Mac Release" workflow → "Run workflow" 수동 dispatch.

---

## 4. 실기기 QA

코드로 검증 불가능한 사용자 환경 의존 항목. 정식 공개 전 한 번씩.

### 4.1 Xcode GUI 실행 QA
- `mac/PhotomeForMac/Package.swift`를 Xcode에서 열고 `⌘R`
- 백엔드 자동 시작 → "사진첩 열기"·"설정 열기"가 기본 브라우저에서 열림 확인 (창 없는 메뉴바 앱)
- 메뉴바 아이콘 상태/진행 표시, 자동 동기화 동작 확인

### 4.2 LAN admin guard 크로스 디바이스 (Docker/서버 배포 한정)
> Mac 앱은 LAN 공유를 제거(local-only 고정)했다. 아래는 Docker/서버를 `0.0.0.0`으로 노출할 때만 확인한다.

- Docker/서버를 LAN(`0.0.0.0`)으로 노출하고 `TROVE_LAN_ADMIN_TOKEN` 설정
- 다른 기기 (스마트폰 브라우저 등)에서 `http://<서버 IP>:<포트>/` 접근
- 갤러리/대시보드는 표시되지만 `/scan`, `/people` 등 admin API는 401 (X-Trove-Admin-Token 없이) 확인

### 4.3 NAS / 대용량 라이브러리
- 작은 폴더(수백 장) 먼저 동기화/검색 동작 확인
- NAS 큰 폴더(수만 장) 동기화 → progress badge 갱신
- NAS 마운트 끊김 상황에서 앱이 죽지 않는지 (graceful degrade)
- 앱 재시작 후 source root 유지 확인
- 사람/별명/병합 결과가 source root 경로 변경 후에도 보존되는지

### 4.4 첫 외부 사용자 테스트
- notarized DMG를 사용자가 다운로드 → 더블클릭 → Applications 드래그 → 더블클릭
- Gatekeeper 경고 없이 정상 실행되는지
- 사진 폴더 선택 → 인덱싱 시작
- 며칠 후 다시 열어도 incremental scan(빠른 재실행) 동작하는지

---

## 5. 그 외 사용자 선택 사항

- **App icon 변경:** `mac/PhotomeForMac/Resources/Assets.xcassets/AppIcon.appiconset/` 안 PNG 교체
- **버전 변경:** 빌드 시 `TROVE_MAC_VERSION=0.2.0 scripts/build_mac_app_bundle.sh`
- **GitHub Release 노트:** workflow의 `--notes` 자동 문구 외 직접 수정 시 release page에서 편집
### Sparkle 2 자동 업데이트 셋업

코드 측 통합은 끝났고(`UpdateChecker.swift`가 Sparkle 기반으로 교체됨), 운영 측에서 다음 한 번 작업이 필요하다. 첫 정식 릴리스 v0.1.0부터 Sparkle-aware로 나가게 된다.

**1. Sparkle CLI 도구 설치:**
```bash
# Homebrew로 설치하거나
brew install --cask sparkle
# 또는 GitHub releases에서 직접: https://github.com/sparkle-project/Sparkle/releases
# 통상 ~/Downloads/Sparkle-2.x.x/bin/{generate_keys,sign_update,generate_appcast}
```

**2. edDSA key 쌍 생성 (한 번만):**
```bash
generate_keys
# → Public Key가 Keychain "Sparkle"에 저장됨, base64 출력됨 (예: "MCowB...wI=")
# 그 base64 문자열을 안전한 곳에도 백업 — 분실 시 모든 사용자가 자동 업데이트 못 받음
```

**3. appcast.xml 호스팅 결정:**
- 권장: GitHub Pages — `https://dongeui.github.io/photomeformac/appcast.xml`
  - repo의 `gh-pages` branch 또는 main의 `/docs` 디렉토리에 두면 자동 호스팅
- 대안: 자체 도메인, S3, Cloudflare R2 등 https 정적 호스팅이면 모두 OK

**4. 환경변수 설정해서 빌드:**
```bash
export TROVE_SPARKLE_FEED_URL="https://dongeui.github.io/photomeformac/appcast.xml"
export TROVE_SPARKLE_PUBLIC_ED_KEY="MCowB...wI="  # 2번에서 출력된 base64
scripts/build_mac_app_bundle.sh
# Info.plist에 SUFeedURL + SUPublicEDKey가 자동 부착됨
```

**5. 새 릴리스마다 appcast.xml 갱신:**
```bash
generate_appcast /path/to/release_dmgs/   # 이 폴더의 모든 *.dmg를 스캔해서 appcast.xml 작성
# → 자동으로 edDSA 서명, version 추출, release notes 생성 (markdown 파일 옆에 두면)
# 결과 appcast.xml + dmg 들을 GitHub Pages 또는 정적 호스팅에 푸시
```

**6. (선택) GitHub Actions로 자동화:**
새 tag push 시 `generate_appcast` 실행 + appcast.xml을 gh-pages branch에 커밋. workflow 추가는 향후 작업.

---

## 진행 트래커

- [ ] 1.1 Apple Developer Program 가입
- [ ] 1.2 Developer ID Application 인증서 발급/설치
- [ ] 1.3 App-Specific Password 생성
- [ ] 1.4 notarytool keychain profile 저장
- [ ] 2 로컬 빌드 + notarize 정상 동작 확인
- [ ] 3 GitHub Actions Secrets 설정 (선택)
- [ ] 4.1 Xcode GUI QA
- [ ] 4.2 LAN admin guard QA
- [ ] 4.3 NAS / 대용량 QA
- [ ] 4.4 외부 사용자 테스트
- [ ] 5. Sparkle CLI 도구 설치 + edDSA key 쌍 생성
- [ ] 5. appcast.xml 호스팅 위치 결정 (GitHub Pages 추천)
- [ ] 5. `TROVE_SPARKLE_FEED_URL` + `TROVE_SPARKLE_PUBLIC_ED_KEY` 환경변수로 첫 빌드
- [ ] 5. 첫 릴리스 후 generate_appcast로 appcast.xml 호스팅 push
