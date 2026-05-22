"""Local face analysis service built on top of OpenCV Zoo models."""

from app.services.analysis.opencv_zoo import OpenCVFaceModelPaths, OpenCVZooModelResolver
from app.services.analysis.service import (
    FaceAnalysis,
    FaceAnalysisConfig,
    FaceAnalysisError,
    FaceAnalysisService,
    FaceBoundingBox,
    ImageFaceAnalysisResult,
)

__all__ = [
    "FaceAnalysis",
    "FaceAnalysisConfig",
    "FaceAnalysisError",
    "FaceAnalysisService",
    "FaceBoundingBox",
    "ImageFaceAnalysisResult",
    "OpenCVFaceModelPaths",
    "OpenCVZooModelResolver",
]
