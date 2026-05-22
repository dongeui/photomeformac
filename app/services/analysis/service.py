"""Local face analysis service using OpenCV Zoo YuNet and SFace models."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from shutil import which
from tempfile import NamedTemporaryFile
from typing import Any

from app.services.analysis.opencv_zoo import OpenCVFaceModelPaths, OpenCVZooModelResolver

try:
    import cv2
except ImportError:  # pragma: no cover - optional dependency
    cv2 = None  # type: ignore[assignment]

try:
    import numpy as np
except ImportError:  # pragma: no cover - optional dependency
    np = None  # type: ignore[assignment]


class FaceAnalysisError(RuntimeError):
    """Raised when local face analysis cannot run."""


@dataclass(frozen=True)
class FaceBoundingBox:
    x: int
    y: int
    width: int
    height: int
    confidence: float
    landmarks: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class FaceAnalysis:
    face_index: int
    person_label_suggestion: str
    bbox: FaceBoundingBox
    embedding: tuple[float, ...]


@dataclass(frozen=True)
class ImageFaceAnalysisResult:
    image_path: Path
    image_width: int
    image_height: int
    model_paths: OpenCVFaceModelPaths
    faces: tuple[FaceAnalysis, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class FaceAnalysisConfig:
    model_root: Path | None = None
    auto_download_models: bool = True
    download_timeout_seconds: int = 60
    detection_score_threshold: float = 0.8
    detection_nms_threshold: float = 0.3
    detection_top_k: int = 5000
    min_face_size: int = 20
    normalize_embeddings: bool = True
    person_label_prefix: str = "person"
    person_label_start_index: int = 1


class FaceAnalysisService:
    """Analyze image files with OpenCV's local face detector and recognizer."""

    def __init__(
        self,
        config: FaceAnalysisConfig | None = None,
        *,
        model_resolver: OpenCVZooModelResolver | None = None,
    ) -> None:
        self._config = config or FaceAnalysisConfig()
        self._model_resolver = model_resolver or OpenCVZooModelResolver(
            root=self._config.model_root,
            auto_download=self._config.auto_download_models,
            timeout_seconds=self._config.download_timeout_seconds,
        )

    def analyze_image_file(self, image_path: Path | str) -> ImageFaceAnalysisResult:
        self._ensure_runtime_dependencies()

        resolved_path = Path(image_path).expanduser().resolve()
        if not resolved_path.is_file():
            raise FaceAnalysisError(f"Image file not found: {resolved_path}")

        image = _load_image(resolved_path)
        image_height, image_width = image.shape[:2]
        model_paths = self.ensure_local_models()

        detector = self._create_detector(model_paths.detector_path, image_width=image_width, image_height=image_height)
        recognizer = self._create_recognizer(model_paths.recognizer_path)

        _, detected_faces = detector.detect(image)
        if detected_faces is None or len(detected_faces) == 0:
            return ImageFaceAnalysisResult(
                image_path=resolved_path,
                image_width=image_width,
                image_height=image_height,
                model_paths=model_paths,
                faces=(),
            )

        faces = _filter_and_sort_faces(detected_faces, min_face_size=self._config.min_face_size)
        analyzed_faces: list[FaceAnalysis] = []
        warnings: list[str] = []

        for offset, face_row in enumerate(faces):
            try:
                aligned_face = recognizer.alignCrop(image, face_row)
                embedding = recognizer.feature(aligned_face)
                analyzed_faces.append(
                    FaceAnalysis(
                        face_index=offset,
                        person_label_suggestion=_format_person_label(
                            self._config.person_label_prefix,
                            self._config.person_label_start_index + offset,
                        ),
                        bbox=_build_bounding_box(face_row),
                        embedding=_embedding_to_tuple(
                            embedding,
                            normalize=self._config.normalize_embeddings,
                        ),
                    )
                )
            except Exception as exc:
                warnings.append(f"face[{offset}] embedding failed: {exc}")

        return ImageFaceAnalysisResult(
            image_path=resolved_path,
            image_width=image_width,
            image_height=image_height,
            model_paths=model_paths,
            faces=tuple(analyzed_faces),
            warnings=tuple(warnings),
        )

    def _create_detector(self, detector_path: Path, *, image_width: int, image_height: int) -> Any:
        try:
            detector = cv2.FaceDetectorYN_create(  # type: ignore[union-attr]
                str(detector_path),
                "",
                (image_width, image_height),
                self._config.detection_score_threshold,
                self._config.detection_nms_threshold,
                self._config.detection_top_k,
            )
        except Exception as exc:
            raise FaceAnalysisError(f"Failed to initialize YuNet detector: {exc}") from exc
        detector.setInputSize((image_width, image_height))
        return detector

    def _create_recognizer(self, recognizer_path: Path) -> Any:
        try:
            return cv2.FaceRecognizerSF_create(str(recognizer_path), "")  # type: ignore[union-attr]
        except Exception as exc:
            raise FaceAnalysisError(f"Failed to initialize SFace recognizer: {exc}") from exc

    def _ensure_runtime_dependencies(self) -> None:
        missing: list[str] = []
        if cv2 is None:
            missing.append("cv2")
        if np is None:
            missing.append("numpy")
        if missing:
            joined = ", ".join(missing)
            raise FaceAnalysisError(
                f"Missing runtime dependency: {joined}. Install OpenCV with numpy support to enable local face analysis."
            )
        if not hasattr(cv2, "FaceDetectorYN_create") or not hasattr(cv2, "FaceRecognizerSF_create"):  # type: ignore[union-attr]
            raise FaceAnalysisError(
                "Installed OpenCV build does not expose FaceDetectorYN_create / FaceRecognizerSF_create. "
                "Use a newer OpenCV package that includes the OpenCV Zoo face APIs."
            )

    def ensure_local_models(self) -> OpenCVFaceModelPaths:
        try:
            return self._model_resolver.resolve_face_models()
        except Exception as exc:
            raise FaceAnalysisError(f"Failed to resolve local face models: {exc}") from exc


def _load_image(image_path: Path) -> Any:
    if cv2 is None or np is None:
        raise FaceAnalysisError("OpenCV runtime dependencies are not available")

    try:
        encoded = np.fromfile(str(image_path), dtype=np.uint8)
    except OSError as exc:
        raise FaceAnalysisError(f"Failed to read image bytes: {image_path}") from exc
    if encoded.size == 0:
        raise FaceAnalysisError(f"Image file is empty or unreadable: {image_path}")
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if image is None:
        image = _load_image_with_sips(image_path)
    if image is None:
        raise FaceAnalysisError(f"Failed to decode image: {image_path}")
    return image


def _load_image_with_sips(image_path: Path) -> Any:
    if cv2 is None or np is None:
        return None

    sips = which("sips")
    if sips is None:
        return None

    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(suffix=".jpg", delete=False) as temp_file:
            temp_path = Path(temp_file.name)

        command = [
            sips,
            "-s",
            "format",
            "jpeg",
            str(image_path),
            "--out",
            str(temp_path),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            return None

        encoded = np.fromfile(str(temp_path), dtype=np.uint8)
        if encoded.size == 0:
            return None
        return cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink(missing_ok=True)


def _filter_and_sort_faces(face_rows: Any, *, min_face_size: int) -> list[Any]:
    filtered: list[Any] = []
    for row in face_rows:
        width = float(row[2])
        height = float(row[3])
        if width < min_face_size or height < min_face_size:
            continue
        filtered.append(row)
    filtered.sort(key=lambda row: (round(float(row[1]), 3), round(float(row[0]), 3), -float(row[14])))
    return filtered


def _build_bounding_box(face_row: Any) -> FaceBoundingBox:
    landmarks = tuple((float(face_row[index]), float(face_row[index + 1])) for index in range(4, 14, 2))
    return FaceBoundingBox(
        x=int(round(float(face_row[0]))),
        y=int(round(float(face_row[1]))),
        width=int(round(float(face_row[2]))),
        height=int(round(float(face_row[3]))),
        confidence=float(face_row[14]),
        landmarks=landmarks,
    )


def _embedding_to_tuple(embedding: Any, *, normalize: bool) -> tuple[float, ...]:
    if np is None:
        raise FaceAnalysisError("numpy is required to build face embeddings")

    vector = np.asarray(embedding, dtype=np.float32).reshape(-1)
    if vector.size == 0:
        raise FaceAnalysisError("Received empty face embedding from OpenCV")

    if normalize:
        magnitude = float(np.linalg.norm(vector))
        if magnitude > 0.0:
            vector = vector / magnitude
    return tuple(float(value) for value in vector.tolist())


def _format_person_label(prefix: str, index: int) -> str:
    normalized_prefix = prefix.strip() or "person"
    if index < 0:
        raise ValueError("person label index must be non-negative")
    return f"{normalized_prefix}-{index:06d}"
