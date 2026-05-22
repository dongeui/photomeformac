"""SQLAlchemy models."""

from app.models.annotation import MediaAnnotation
from app.models.asset import DerivedAsset
from app.models.base import Base
from app.models.face import Face
from app.models.media import MediaFile
from app.models.job import ProcessingJob
from app.models.observation import ScanObservation
from app.models.person import Person
from app.models.runtime import SchedulerRuntimeConfig
from app.models.semantic import (
    GeocodingCache,
    MediaAnalysisSignal,
    MediaAutoTagState,
    MediaCaption,
    MediaEmbedding,
    MediaOCR,
    MediaOCRBlock,
    MediaOCRGram,
    SearchDocument,
    SearchFeedback,
    SearchWeightProfile,
)
from app.models.tag import Tag

__all__ = [
    "Base",
    "DerivedAsset",
    "Face",
    "GeocodingCache",
    "MediaAnalysisSignal",
    "SearchWeightProfile",
    "MediaAnnotation",
    "MediaAutoTagState",
    "MediaCaption",
    "MediaEmbedding",
    "MediaFile",
    "MediaOCR",
    "MediaOCRBlock",
    "MediaOCRGram",
    "Person",
    "ProcessingJob",
    "ScanObservation",
    "SchedulerRuntimeConfig",
    "SearchDocument",
    "SearchFeedback",
    "Tag",
]
