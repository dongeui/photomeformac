from app.core.contracts import MediaKind, media_kind_from_path
from app.services.scanner.service import ScannerConfig, ScannerService


def test_apple_and_raw_photo_extensions_are_images() -> None:
    for filename in (
        "IMG_0001.HEIC",
        "IMG_0002.heic",
        "RAW_0003.DNG",
        "RAW_0004.dng",
    ):
        assert media_kind_from_path(filename) == MediaKind.IMAGE


def test_videos_are_still_not_images() -> None:
    assert media_kind_from_path("IMG_0005.MOV") == MediaKind.VIDEO
    assert media_kind_from_path("IMG_0006.mp4") == MediaKind.VIDEO


def test_scanner_treats_unavailable_source_root_as_empty(monkeypatch) -> None:
    def fail_exists(self):
        raise BlockingIOError(35, "Resource temporarily unavailable")

    monkeypatch.setattr("pathlib.Path.exists", fail_exists)

    scanner = ScannerService(ScannerConfig(source_roots=(__import__("pathlib").Path("/Volumes/NAS/Photos"),)))

    assert list(scanner.iter_files()) == []
