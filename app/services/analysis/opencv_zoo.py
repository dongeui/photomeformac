"""Official OpenCV Zoo model resolution helpers for local face analysis."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _default_model_root() -> Path:
    data_root = Path(os.getenv("PHOTOMINE_DATA_ROOT", "./data")).expanduser().resolve()
    return data_root / "models"


@dataclass(frozen=True)
class OpenCVZooModelSpec:
    name: str
    relative_path: Path
    download_url: str


@dataclass(frozen=True)
class OpenCVFaceModelPaths:
    root: Path
    detector_path: Path
    recognizer_path: Path


YU_NET_MODEL = OpenCVZooModelSpec(
    name="face_detection_yunet",
    relative_path=Path("opencv_zoo") / "face_detection_yunet" / "face_detection_yunet_2023mar.onnx",
    download_url=(
        "https://github.com/opencv/opencv_zoo/raw/main/"
        "models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
    ),
)

SFACE_MODEL = OpenCVZooModelSpec(
    name="face_recognition_sface",
    relative_path=Path("opencv_zoo") / "face_recognition_sface" / "face_recognition_sface_2021dec.onnx",
    download_url=(
        "https://github.com/opencv/opencv_zoo/raw/main/"
        "models/face_recognition_sface/face_recognition_sface_2021dec.onnx"
    ),
)


class OpenCVZooModelResolver:
    """Resolve or lazily download the OpenCV Zoo face models."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        auto_download: bool = True,
        timeout_seconds: int = 60,
    ) -> None:
        self._root = (root or _default_model_root()).expanduser().resolve()
        self._auto_download = auto_download
        self._timeout_seconds = timeout_seconds

    @property
    def root(self) -> Path:
        return self._root

    def resolve_face_models(self) -> OpenCVFaceModelPaths:
        detector_path = self._ensure_model(YU_NET_MODEL)
        recognizer_path = self._ensure_model(SFACE_MODEL)
        return OpenCVFaceModelPaths(
            root=self._root,
            detector_path=detector_path,
            recognizer_path=recognizer_path,
        )

    def _ensure_model(self, spec: OpenCVZooModelSpec) -> Path:
        target_path = self._root / spec.relative_path
        if _is_valid_model_file(target_path):
            return target_path
        if not self._auto_download:
            raise FileNotFoundError(
                f"Missing or invalid OpenCV Zoo model '{spec.name}' at {target_path}. "
                f"Download it from {spec.download_url} or enable auto_download."
            )
        self._download_model(spec, target_path)
        if not _is_valid_model_file(target_path):
            raise RuntimeError(
                f"Downloaded OpenCV Zoo model '{spec.name}' is invalid at {target_path}. "
                f"Source URL may have returned a Git LFS pointer instead of the ONNX payload."
            )
        return target_path

    def _download_model(self, spec: OpenCVZooModelSpec, target_path: Path) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)

        tmp_path: Path | None = None
        request = Request(
            spec.download_url,
            headers={"User-Agent": "photome-face-analysis/0.1"},
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                with NamedTemporaryFile("wb", delete=False, dir=target_path.parent) as tmp_file:
                    tmp_path = Path(tmp_file.name)
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        tmp_file.write(chunk)
            if tmp_path is None or not _is_valid_model_file(tmp_path):
                raise RuntimeError(f"Downloaded model '{spec.name}' is empty or invalid")
            tmp_path.replace(target_path)
        except (HTTPError, URLError, TimeoutError) as exc:
            raise RuntimeError(f"Failed to download OpenCV Zoo model '{spec.name}': {exc}") from exc
        finally:
            if tmp_path is not None and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)


def _is_valid_model_file(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size <= 1024:
        return False
    return not _is_git_lfs_pointer(path)


def _is_git_lfs_pointer(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            header = handle.read(256)
    except OSError:
        return False
    return header.startswith(b"version https://git-lfs.github.com/spec/v1")
