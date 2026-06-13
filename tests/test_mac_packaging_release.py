from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "scripts" / "build_mac_app_bundle.sh"
NOTARY_SCRIPT = ROOT / "scripts" / "notarize_mac_app.sh"
APP_SWIFT = ROOT / "mac" / "PhotomeForMac" / "Sources" / "PhotomeForMac" / "PhotomeForMacApp.swift"
BACKEND_SWIFT = ROOT / "mac" / "PhotomeForMac" / "Sources" / "PhotomeForMac" / "BackendSupervisor.swift"
RELEASE_DOC = ROOT / "docs" / "mac" / "RELEASE_CHECKLIST.md"
ICONSET = ROOT / "mac" / "PhotomeForMac" / "Resources" / "Assets.xcassets" / "AppIcon.appiconset"
WORKFLOW = ROOT / ".github" / "workflows" / "mac-release.yml"


def test_build_script_creates_signed_dmg_with_applications_symlink_and_backend_bundle():
    text = BUILD_SCRIPT.read_text()
    assert "TROVE_MAC_SIGN_IDENTITY" in text
    assert "codesign --verify" in text
    assert "ln -s /Applications" in text
    assert "TROVE_BUNDLE_BACKEND" in text
    assert "trove-backend" in text
    assert "TROVE_BUNDLE_PYTHON" in text
    assert "NSLocalNetworkUsageDescription" in text


def test_notarize_script_uses_keychain_profile_or_env_without_committed_secret():
    text = NOTARY_SCRIPT.read_text()
    assert "TROVE_NOTARY_PROFILE" in text
    assert "notarytool submit" in text
    assert "stapler staple" in text
    assert "app-specific-password" in text
    forbidden = ["AC_PASSWORD=", "APPLE_PASSWORD=", "-----BEGIN PRIVATE KEY-----"]
    assert not any(token in text for token in forbidden)


def test_app_has_login_item_menu_and_service_management():
    text = APP_SWIFT.read_text()
    assert "import ServiceManagement" in text
    assert "SMAppService.mainApp" in text
    assert "로그인 시 자동 시작" in text
    # 진단 내보내기 메뉴는 c86c6e6에서 메뉴바 간소화로 제거됨.
    # 번들 생성 로직(createDiagnosticsBundle)은 BackendSupervisor 가드가 따로 검증한다.


def test_backend_prefers_bundled_backend_and_python_runtime():
    text = BACKEND_SWIFT.read_text()
    assert "Bundle.main.resourceURL?.appendingPathComponent(\"trove-backend\"" in text
    assert "python-runtime/bin/python" in text
    assert "TROVE_REPO_ROOT" in text
    assert "createDiagnosticsBundle" in text
    assert "diagnostics.json" in text


def test_release_checklist_tracks_remaining_deployment_qa_items():
    text = RELEASE_DOC.read_text()
    for phrase in [
        "Developer ID",
        "notarization",
        "DMG",
        "App icon",
        "Python runtime",
        "Xcode 실행 QA",
        "권한/사진 접근 UX",
        "LAN 공유 보호",
        "launch-at-login",
        "자동 업데이트 전략",
        "NAS/대용량 라이브러리 QA",
    ]:
        assert phrase in text


def test_app_iconset_contains_required_sizes():
    assert (ICONSET / "Contents.json").exists()
    for size in [16, 32, 64, 128, 256, 512, 1024]:
        assert (ICONSET / f"icon_{size}x{size}.png").exists()


def test_mac_release_workflow_uploads_dmg_and_can_publish_release():
    text = WORKFLOW.read_text()
    assert "workflow_dispatch" in text
    assert "scripts/build_mac_app_bundle.sh" in text
    assert "actions/upload-artifact" in text
    assert "gh release upload" in text
    assert "Trove.dmg" in text
