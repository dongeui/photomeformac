"""EXIF orientation 처리 회귀 테스트.

cv2.imdecode와 orientation 미적용 PIL 경로는 회전 촬영 사진(아이폰 세로
사진 등)을 누운 채로 처리해서 — 썸네일이 갤러리에 돌아간 채 나오고, 얼굴
검출이 실패하거나(검출 0) 회전된 얼굴 임베딩이 만들어져 기존 인물과 매칭이
안 되고 새 클러스터로 빠졌다(2026-06-12 person-000600 사고의 한 축).
"""

from __future__ import annotations

from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from app.core.contracts import MediaKind  # noqa: E402
from app.services.thumbnail.service import ThumbnailConfig, ThumbnailService  # noqa: E402


def _write_rotated_jpeg(path: Path, *, width: int = 100, height: int = 50) -> None:
    """orientation=6(표시하려면 시계방향 90도 회전 필요) JPEG 생성."""
    image = Image.new("RGB", (width, height), color=(200, 30, 30))
    exif = Image.Exif()
    exif[0x0112] = 6
    image.save(path, format="JPEG", exif=exif)


def test_thumbnail_applies_exif_orientation(tmp_path: Path) -> None:
    source = tmp_path / "rotated.jpg"
    _write_rotated_jpeg(source)  # 저장 픽셀 100x50, 표시 방향은 50x100(세로)

    service = ThumbnailService(ThumbnailConfig(derived_root=tmp_path / "derived", size=512))
    location = service.generate(source, "f" * 64, MediaKind.IMAGE)

    with Image.open(location.absolute_path) as thumb:
        # 픽셀이 표시 방향(세로)으로 저장돼야 한다 — 썸네일 JPEG에는 EXIF가
        # 없으므로 여기서 안 돌리면 영영 누운 채로 보인다.
        assert thumb.height > thumb.width


def test_face_analysis_image_loader_applies_exif_orientation(tmp_path: Path) -> None:
    np = pytest.importorskip("numpy")
    from app.services.analysis.service import _load_image_with_pillow

    source = tmp_path / "rotated.jpg"
    _write_rotated_jpeg(source)

    array = _load_image_with_pillow(source)

    assert array is not None
    assert isinstance(array, np.ndarray)
    # (h, w) = 표시 방향. 회전 미적용이면 (50, 100)으로 나온다.
    assert array.shape[:2] == (100, 50)
